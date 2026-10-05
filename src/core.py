# -*- coding: utf-8 -*-
"""
核心层：配置读写、BetterGI 探测、计划任务管理、电源设置、启动一条龙与校验。

设计要点
--------
* 全程用 Python 标准库，不依赖任何第三方包（打包 exe 后体积小、无需装运行时）。
* 所有"会改动系统"的操作（计划任务、电源设置）都通过 schtasks / powercfg 调用，
  权限不足时抛 PermissionError，由 GUI 负责弹 UAC 重新自提权。
* 长耗时操作（等窗口出现、等一条龙开跑）都用轮询，不阻塞界面太久。
"""

import configparser
import ctypes
import ctypes.wintypes
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta

APP_NAME = "原神一条龙助手"
APP_VERSION = "1.0.0"

TASK_TRIGGER_NAME = "AutoBGI_Onedragon"
WAKE_TEST_TASK = "AutoBGI_WakeTest_OneShot"
# 按需接力的计划任务（**没有触发器**，只在「立即接力一次」时手动启动）。
#
# 为什么要这个：ok-ww 启动 PC 版鸣潮强制要求管理员权限，非管理员会静默
# 放弃。原先「立即接力」是**提权重启自己** —— 依赖 UAC 弹窗。
# 而这台机器的 UAC 是 `ConsentPromptBehaviorAdmin=0`（提权不提示），
# 表现就是「什么都没发生」；反过来若 UAC 是默认级别，人不在跟前就点不了。
# 改成「触发一个已存在的最高权限任务」后，**两种情况都不需要点任何东西**
# —— 任务由计划任务服务拉起，不由我们提权。
RELAY_TASK = "AutoBGI_Relay"

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def app_dir() -> str:
    """程序所在目录（打包成 exe 后是 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def writable_dirs():
    """按优先级返回**可靠可写**的目录。

    坑：`C:\\ProgramData\\...` 这类目录**可以新建文件，但不能覆盖/追加已存在
    的文件**（本机 2026-10-03 实测，非沙箱下同样如此）。所以「写固定名字的
    报告文件」第二次就会 PermissionError。用户数据目录始终可写，放第一位。
    """
    out = []
    for d in (user_data_dir(),
              app_dir(),
              os.environ.get("TEMP"),
              os.environ.get("USERPROFILE")):
        if d and d not in out:
            out.append(d)
    return out


def write_report(name, text):
    """把文本写进报告文件，返回实际写入的路径（失败返回 None）。

    依次尝试：各可写目录下的原名 → 带时间戳的名字（绕开"不能覆盖"限制）。
    绝不抛异常 —— 报告写不出来不该让主流程崩掉。
    """
    stamp = datetime.now().strftime("%H%M%S")
    for d in writable_dirs():
        for fn in (name, "%s_%s" % (os.path.splitext(name)[0] + "_" + stamp,
                                    os.path.splitext(name)[1].lstrip("."))):
            p = os.path.join(d, fn)
            try:
                os.makedirs(d, exist_ok=True)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text)
                return p
            except OSError:
                continue
    return None


def _norm_path(p):
    """把路径规范化成 Windows 反斜杠形式。

    手动编辑配置或用其他工具写配置时很容易写成正斜杠；
    多数 Windows API 能接受，但**计划任务、某些原生程序**会挑刺，
    所以统一规范化（`D:/ok-ww` → `D:\\ok-ww`）。
    """
    if not p:
        return ""
    return os.path.normpath(str(p).replace("/", "\\"))


def user_data_dir() -> str:
    """可写的用户数据目录（配置与日志都放这里）。"""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.environ.get("APPDATA")
    if not base:
        base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    d = os.path.join(base, "AutoBGI")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def install_report_path() -> str:
    """提权安装的结果回执文件路径。

    GUI 提权后会一直等这个文件出现，再把内容显示在界面上 ——
    这样即使弹出的对话框被误关，结果也不会丢，用户也**不用**重开程序。
    """
    return os.path.join(user_data_dir(), "install_report.txt")


def config_path() -> str:
    """配置文件位置 —— **唯一的、确定性的**一个。

    血泪史（2026-10-03）：
    早先的实现「优先放 exe 同级，不可写再退回用户目录」，探测方式是
    `open(p, "a")`。结果在 `C:\\ProgramData\\...` 这类目录下出现**翻转**：
      · 首次运行：文件不存在 → 能创建 → 选 exe 同级
      · 之后每次：文件已存在 → 普通用户无法修改 → 退回 %LOCALAPPDATA%
    于是**用户手改 exe 旁边的 ini 完全不起作用**，改的是一份从没被读过的
    文件（本机实测踩过）。exe 同级目录还会留下一个误导人的空配置。

    现在改为**始终使用 %LOCALAPPDATA%\\AutoBGI\\AutoBGISettings.ini** ——
    确定性、每用户隔离、永远可写。
    同时做一次**一次性导入**：若用户目录里的配置是空的（或没有），
    而 exe 同级存在配置，就把它的内容搬过来，保证老用户的设置不丢。
    """
    dst = os.path.join(user_data_dir(), "AutoBGISettings.ini")
    legacy = os.path.join(app_dir(), "AutoBGISettings.ini")

    if not os.path.isfile(dst) and os.path.isfile(legacy):
        try:
            with open(legacy, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(data)
        except OSError:
            pass
    return dst


def log_dir() -> str:
    """日志目录 —— 与配置一致，固定放 %LOCALAPPDATA%\\AutoBGI\\logs。

    同样不能"先试 exe 同级"：那里能新建文件但**无法追加已存在文件**，
    会造成「首次运行日志在 A 处、之后都在 B 处」的错乱（本机实测）。
    """
    d = os.path.join(user_data_dir(), "logs")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        pass
    # 兜底：临时目录
    d = os.path.join(os.environ.get("TEMP") or ".", "AutoBGILogs")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def bgi_log_dir(bgi_dir: str) -> str:
    return os.path.join(bgi_dir, "log")


# ---------------------------------------------------------------------------
# 提权
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_as_admin() -> bool:
    """用 ShellExecute 以管理员身份重启自己（不带参数）。"""
    if not getattr(sys, "frozen", False):
        return False
    try:
        import ctypes
        params = subprocess.list2cmdline(sys.argv[1:])
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, app_dir(), 1
        )
        return int(ret) > 32
    except Exception:
        return False


def run_as_admin_with_args(args) -> bool:
    """以管理员身份启动指定命令行。

    args 是**完整**的命令行列表（含 exe 自身）。用于 --clean-all 这类
    必须提权才能完成的维护动作。
    """
    try:
        import ctypes
        if not args:
            return False
        exe = args[0]
        params = subprocess.list2cmdline(list(args[1:]))
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, app_dir(), 0
        )
        return int(ret) > 32
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 执行外部命令
# ---------------------------------------------------------------------------

def run(cmd, timeout=30, encoding=None):
    """执行命令，返回 (returncode, stdout+stderr 文本)。不抛异常。

    cmd 一律用**列表**形式（不走 cmd.exe），否则命令行里的 `|` `&` `>` 会被
    cmd 当成 shell 元字符，把整条命令拆坏。
    encoding 为 None 时自动在 utf-8 / gbk / cp936 之间挑一个能正常解码的
    （中文 Windows 的 powercfg 等命令输出是 GBK）。
    """
    if isinstance(cmd, str):
        cmd = [cmd]
    if encoding is None:
        encoding = "auto"

    def _decode(raw):
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        for enc in (["utf-8", "gbk", "cp936", "latin-1"] if encoding == "auto"
                    else [encoding]):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    try:
        cp = subprocess.run(
            cmd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return cp.returncode, _decode(cp.stdout).strip()
    except FileNotFoundError as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired:
        return 124, "命令执行超时"
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def ps(script, timeout=45):
    """执行一段 PowerShell 脚本。

    关键点：强制 PowerShell 以 UTF-8 输出。否则中文系统上它会按控制台代码页
    （GBK/936）输出，中文路径在程序里就变成乱码。
    """
    wrapped = (
        "$OutputEncoding=[Console]::OutputEncoding="
        "[System.Text.Encoding]::UTF8; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$ProgressPreference='SilentlyContinue'; " + script
    )
    return run(["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", wrapped],
               timeout=timeout, encoding="utf-8")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULTS = {
    "BgiDir": "",
    "BgiExe": "",
    "OneDragonConfig": "默认配置",
    "TaskName": TASK_TRIGGER_NAME,
    "TaskTime": "03:30",
    # 随机误差上限（分钟）。0 = 精确到点。
    # 到点时间会在 task_time ~ task_time+RandomMinutes 之间随机，
    # 避免每天同一秒唤醒（更自然，也避开固定时刻被"看穿"）。
    "RandomMinutes": "30",
    # 默认不限制时间窗：任务时间本身就是触发条件，再加一道窄窗
    # 只会造成"任务触发了却被自己挡住"的静默失败（2026-10-03 实测踩过）
    "WindowStart": "0000",
    "WindowEnd": "0000",
    "AutoDisableConsoleLock": "1",
    "WaitWindowSec": "180",
    "VerifySec": "420",
    "AutoStartTask": "0",
    "KeepScreenOn": "0",
    # 开跑一条龙时把其它窗口全部最小化，只留原神（BetterGI 的遮罩窗口
    # 也保留）。有窗口盖住游戏或抢焦点时，BetterGI 会反复
    # 「切换角色卡住，执行脱困」—— 2026-10-05 凌晨实测空转 806 次。
    "MinimizeOthers": "1",
    # 开跑前/运行中关掉挡路的「开始菜单 / 搜索」浮层（Win 键打开的那个）。
    # 2026-10-05 用户反馈：睡眠唤醒后有时会卡在这个界面上 ——
    # 它抢走焦点后，游戏收不到模拟键鼠、BetterGI 也识别不到画面。
    # 关掉它的手段就是发一个 Esc（实测有效，且无副作用）。
    "DismissShellFloat": "1",

    # ---- 接力鸣潮（BetterGI 一条龙跑完 → 关原神 → 跑 ok-ww 鸣潮日常）----
    # 0 = 只跑原神；1 = 一条龙完成后自动接力鸣潮
    "RunOkww": "0",
    # ok-ww 安装目录（含 ok-ww.exe 与 data\\apps\\ok-ww\\）
    "OkwwDir": "",
    # ok-ww 里的一条龙任务名（类名）。鸣潮「日常」= DailyTask
    "OkwwTask": "DailyTask",
    # 跑 ok-ww 时是否显示它的程序界面（1=显示，推荐；0=完全无界面后台跑）
    # 显示界面的好处：你能亲眼看到 ok-ww 在跑、跑到哪一步；
    # 代价：多占一点资源，且万一弹出需要点确认的对话框会打断（很少见）。
    # 两种模式都会**自动**跑指定任务，不需要人工点任何按钮。
    "OkwwGuiMode": "1",

    # ---- 多游戏「一条龙」阶段（顺序可自定义）----
    # 逗号分隔的游戏 id，**顺序即执行顺序**。
    #   genshin=原神(BetterGI) / wuwa=鸣潮(ok-ww)
    #   starrail=崩铁(三月七小助手) / zzz=绝区零(ZZZ一条龙)
    # 默认只启用原神 + 鸣潮；崩铁 / 绝区零装好工具再手动加进来。
    "StageOrder": "genshin,wuwa",
    # 崩铁：三月七小助手（March7th Launcher.exe 所在目录）
    "M7Dir": "",
    # 崩铁要跑的任务：main=完整运行 / daily=每日实训 / power=清体力
    "M7Task": "main",
    # 绝区零：ZZZ-OneDragon 安装目录（含 OneDragon-Launcher.exe）
    "ZzzDir": "",
    # 绝区零启动参数：-o=跑一条龙，-c=结束后关闭游戏
    "ZzzArgs": "-o -c",
    # 通用阶段：等工具启动的上限（秒）、跑完的上限（分钟）
    "StageBootSec": "180",
    "StageTimeoutMin": "180",
    # 关原神：0=不关（保留原神）；1=关掉原神腾出性能给鸣潮
    "KillGenshin": "1",
    # 原神主程序（用于精准关闭；留空则按进程名关）
    "GenshinExe": "",
    # 鸣潮启动器（**已弃用**：鸣潮改由 ok-ww 自己拉起，本程序不再开游戏）
    "WuwaExe": "",
    # 一条龙最长等多久（分钟），超时就放弃等待
    "OneDragonTimeoutMin": "300",
    # 关掉原神后等多久再交给 ok-ww（等游戏进程完全退出）
    "WuwaGapSec": "20",
    # ok-ww 跑日常最长等多久（分钟），仅用于记录结果
    "OkwwTimeoutMin": "120",
}

_CN = {
    "BgiDir": "BetterGI 目录",
    "BgiExe": "BetterGI 主程序",
    "OneDragonConfig": "一条龙配置名",
    "TaskName": "计划任务名称",
    "TaskTime": "每日运行时间",
    "RandomMinutes": "随机误差上限(分钟)",
    "WindowStart": "时间窗开始",
    "WindowEnd": "时间窗结束",
    "AutoDisableConsoleLock": "自动关闭唤醒需登录",
    "WaitWindowSec": "等待主窗口秒数",
    "VerifySec": "校验一条龙秒数",
    "AutoStartTask": "登录后自动启动程序",
    "KeepScreenOn": "运行时阻止休眠",
    "MinimizeOthers": "开跑时最小化其它窗口",
    "DismissShellFloat": "开跑时关闭开始菜单",
    "RunOkww": "一条龙后接力鸣潮(0/1)",
    "OkwwDir": "ok-ww 安装目录",
    "OkwwTask": "鸣潮任务名",
    "OkwwGuiMode": "运行 ok-ww 时显示界面(0/1)",
    "StageOrder": "游戏执行顺序(逗号分隔)",
    "M7Dir": "崩铁 三月七小助手 目录",
    "M7Task": "崩铁任务(main/daily/power)",
    "ZzzDir": "绝区零一条龙 目录",
    "ZzzArgs": "绝区零启动参数",
    "StageBootSec": "阶段启动等待(秒)",
    "StageTimeoutMin": "阶段运行上限(分钟)",
    "KillGenshin": "接力时关闭原神(0/1)",
    "GenshinExe": "原神主程序路径",
    "WuwaExe": "鸣潮启动器路径(已弃用)",
    "OneDragonTimeoutMin": "一条龙等待上限(分钟)",
    "WuwaGapSec": "关原神后宽限(秒)",
    "OkwwTimeoutMin": "ok-ww 等待上限(分钟)",
}


class Config(object):
    """config.ini 的读写封装。"""

    def __init__(self, path=None):
        self.path = path or config_path()
        self.cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        self.cp.add_section("AutoBGI")
        for k, v in DEFAULTS.items():
            self.cp.set("AutoBGI", k, v)
        self.load_ok = True
        self.load_msg = ""
        self.load()

    def load(self):
        """读配置。返回 (成功, 说明)。

        **绝不能静默吞异常** —— 这条规则是用血换来的（2026-10-04）：
        早先 save() 写出的是**没有 `[AutoBGI]` 段头**的扁平 key=value，
        而 load() 用 `cp.read()` 读它时抛 `MissingSectionHeaderError`，
        被 `except Exception: pass` 悄悄吃掉。
        结果：配置**写进去了但永远读不回来**，程序永远跑默认值。
        因为 `TaskTime` 的默认值恰好就是 03:30，这个 bug 伪装了很久，
        直到用户改了时间却发现"完全没反应"才暴露。

        现在：① 兼容没有段头的旧文件（自动补 `[AutoBGI]`）；
              ② 读取失败会记录原因，不再假装没事。
        """
        self.load_ok = True
        self.load_msg = "使用默认配置（尚无配置文件）"
        if not os.path.isfile(self.path):
            return True, self.load_msg
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                txt = fh.read()
        except OSError as exc:
            self.load_ok = False
            self.load_msg = "读不到配置文件：%s" % exc
            return False, self.load_msg

        if not re.search(r"^\s*\[", txt, re.M):
            # 旧格式（没有段头）→ 自动补一个，保证能读进来
            txt = "[AutoBGI]\n" + txt
        try:
            self.cp.read_string(txt)
            self.load_msg = self.path
            return True, self.path
        except Exception as exc:  # noqa: BLE001
            self.load_ok = False
            self.load_msg = "配置内容无法解析（已改用默认值）：%s" % exc
            return False, self.load_msg

    def save(self):
        """写盘。返回 (成功, 说明)。

        **绝不静默失败**：返回失败说明，调用方必须转告用户。
        （2026-10-04 事故：配置写在 exe 同级目录时无法覆盖已存在文件，
        保存静默失败 → 用户改了时间却"完全没有变动"，完全查不出原因。）

        注意：**必须写出 `[AutoBGI]` 段头**，否则 configparser 读不回来。
        """
        buf = []
        buf.append("; " + APP_NAME + " 配置文件")
        buf.append("; 界面上的每个输入框都会自动保存到这里，也可手动编辑。")
        buf.append("; 位置：%s" % self.path)
        buf.append("")
        buf.append("[AutoBGI]")
        for k in DEFAULTS:
            buf.append("; %s" % _CN.get(k, k))
            buf.append("%s=%s" % (k, self.get(k)))
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(buf) + "\n")
            return True, self.path
        except OSError as exc:
            return False, ("无法写入配置文件：%s\n（%s）" % (self.path, exc))

    def get(self, key, default=""):
        try:
            return self.cp.get("AutoBGI", key).strip()
        except Exception:
            return DEFAULTS.get(key, default)

    def set(self, key, value):
        self.cp.set("AutoBGI", key, "" if value is None else str(value).strip())

    # 便捷属性 -----------------------------------------------------------
    @property
    def bgi_dir(self):
        return self.get("BgiDir")

    # -- 接力鸣潮相关 -----------------------------------------------------
    @property
    def run_okww(self):
        """是否启用「一条龙跑完接力鸣潮」。"""
        return str(self.get("RunOkww")).strip() in ("1", "true", "True", "yes")

    @property
    def kill_genshin(self):
        return str(self.get("KillGenshin")).strip() in ("1", "true", "True",
                                                        "yes")

    @property
    def okww_dir(self):
        """ok-ww 安装目录（缓存到 self._okww_cache，避免反复扫盘）。"""
        cached = getattr(self, "_okww_cache", None)
        if cached is not None:
            return cached
        d = _norm_path(self.get("OkwwDir"))
        if not (d and os.path.isfile(os.path.join(d, "ok-ww.exe"))):
            found = detect_okww(d)
            if found:
                d = found
                if self.get("OkwwDir") != found:
                    self.set("OkwwDir", found)
                    self.save()
        self._okww_cache = d or ""
        return self._okww_cache

    @property
    def okww_task(self):
        return self.get("OkwwTask") or "DailyTask"

    @property
    def genshin_exe(self):
        """原神主程序路径（用于精准关闭）。找不到返回空。"""
        v = _norm_path(self.get("GenshinExe"))
        if v and os.path.isfile(v):
            return v
        found = find_genshin_exe()
        if found:
            self.set("GenshinExe", found)
            self.save()
        return found or ""

    @property
    def wuwa_exe(self):
        """**ok-ww** 接力时会启动的鸣潮主程序路径（不是本程序去启动）。

        2026-10-05 改：鸣潮由 ok-ww 自己拉起（它的 `start_device()` 会
        `starting game <path>` 并等窗口就绪），本程序**不再**另外开游戏。
        这里改为回显 ok-ww 实际会用的那个文件，便于界面核验。
        """
        return okww_game_exe(self)[0]

    @property
    def bgi_exe(self):
        """BetterGI 主程序完整路径。

        BgiExe 为空时从 BgiDir 推导；都为空则自动探测一次。
        计划任务在无 GUI 环境下运行，拿不到界面上的临时值，
        所以这里必须能自给自足。
        """
        v = self.get("BgiExe")
        if v and os.path.isfile(v):
            # 顺手补全 BgiDir（界面上「安装目录」要显示它）
            if not self.get("BgiDir"):
                self.set("BgiDir", os.path.dirname(v))
                self.save()
            return v
        d = self.get("BgiDir")
        if d:
            p = os.path.join(d, "BetterGI.exe")
            if os.path.isfile(p):
                if self.get("BgiExe") != p:
                    self.set("BgiExe", p)
                    self.save()
                return p
        # 兜底：现场探测（会花几秒）
        found = find_bgi_exe()
        if found:
            # 探测结果回写，下次就不用再探
            try:
                self.set("BgiExe", found)
                self.set("BgiDir", os.path.dirname(found))
                self.save()
            except OSError:
                pass
        return found or ""

    @property
    def one_dragon(self):
        return self.get("OneDragonConfig")

    @property
    def task_name(self):
        return self.get("TaskName") or TASK_TRIGGER_NAME

    @property
    def task_time(self):
        v = self.get("TaskTime") or "03:30"
        return v if re.match(r"^\d{1,2}:\d{2}$", v) else "03:30"

    @property
    def random_minutes(self):
        """随机误差上限（分钟）。0 = 精确到点，不随机。

        范围限制在 0~720（12 小时），超过就没意义了（会跨到第二天）。
        """
        try:
            v = int(str(self.get("RandomMinutes") or "0").strip())
        except (TypeError, ValueError):
            v = 30
        return max(0, min(720, v))

    def base_run_time(self, after=None):
        """基准运行时刻（不含随机误差）：今天/明天的 task_time。"""
        hh, mm = self.task_time.split(":")
        now = after or datetime.now()
        t = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t

    def next_run(self, after=None, rand=None):
        """下一次**实际**运行时刻 = 基准时刻 + [0, random_minutes] 随机分钟。

        rand 可注入一个 0~1 的随机数（便于测试复现）。
        """
        t = self.base_run_time(after)
        r = self.random_minutes
        if r > 0:
            f = random.random() if rand is None else float(rand)
            f = min(max(f, 0.0), 1.0)
            t += timedelta(minutes=int(round(f * r)))
        return t

    def describe_run(self):
        """人类可读的"下一次运行"说明。"""
        t = self.next_run()
        base = self.base_run_time()
        r = self.random_minutes
        if r <= 0:
            return t.strftime("%m-%d %H:%M"), "固定时间（无随机误差）"
        delta = int((t - base).total_seconds() // 60)
        return (t.strftime("%m-%d %H:%M"),
                "基准 %s + 随机 %d 分钟（上限 %d 分钟）"
                % (base.strftime("%H:%M"), delta, r))

    @property
    def window(self):
        """时间窗 —— 防止白天误触发的兜底闸门。

        设计要点（2026-10-03 改）：时间窗是**第二道**闸门，容易和
        任务时间打架 —— 用户把任务改到 01:12 却忘了改时间窗，
        结果任务被自己的时间窗挡住，**静默跳过、看起来像从没运行过**。

        现在：
        - WindowStart/End 留空（"0000"）= 不限制，完全交给任务时间决定
        - 非法值回退到一个**足够宽**的默认窗（00:00–23:59），
          而不是 0300-0400 这种和默认任务时间绑死的窄窗
        """
        def norm(x):
            v = (self.get(x) or "").replace(":", "").strip()
            return v if re.match(r"^\d{4}$", v) else ""
        lo, hi = norm("WindowStart"), norm("WindowEnd")
        if not lo or not hi or lo == "0000" or hi == "0000" or lo >= hi:
            return "0000", "2359"          # 不限制
        return lo, hi

    @property
    def window_enabled(self):
        """时间窗是否真正启用（界面用来提示用户）。"""
        lo, hi = self.window
        return not (lo == "0000" and hi == "2359")

    def in_window(self, now_hm=None):
        """当前时间是否落在时间窗内。返回 (是否允许, 说明)。

        随机误差会让实际运行时刻比基准晚最多 RandomMinutes 分钟，
        所以时间窗的**上沿会自动外扩**这么多 —— 否则用户设了
        0300-0330 又开了 30 分钟随机，任务就会在 03:45 被自己的时间窗
        拦掉（又一次"静默跳过"的坑）。
        """
        lo, hi = self.window
        if lo == "0000" and hi == "2359":
            return True, "不限制"
        r = self.random_minutes
        if r > 0:
            hh, mm = int(hi[:2]), int(hi[2:])
            tot = min(hh * 60 + mm + r, 23 * 60 + 59)
            hi = "%02d%02d" % (tot // 60, tot % 60)
        now = now_hm or datetime.now().strftime("%H%M")
        ok = lo <= now <= hi
        note = "%s-%s（当前 %s）" % (lo, hi, now)
        if r > 0 and ok:
            note += "，含随机误差外扩 %d 分钟" % r
        return ok, note

    def i(self, key, default):
        try:
            return int(self.get(key))
        except Exception:
            return default


# ---------------------------------------------------------------------------
# BetterGI 探测
# ---------------------------------------------------------------------------

_EXE_NAME_LOWER = "bettergi.exe"


def _list_drives():
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = letter + ":\\"
        if os.path.exists(root):
            out.append(root)
    return out


def find_bgi_exe():
    """按 进程 → 快捷方式 → 注册表 → 常见位置 → 全盘扫描 的顺序找。

    性能注意：每个 ps() 调用都会启动一个 powershell 进程（约 1.5 秒），
    所以把注册表的多个 key 合并到**一次** ps 里查，探测整体控制在几秒内。
    """
    # 1) 正在运行的进程
    code, out = ps(
        "(Get-Process BetterGI -ErrorAction SilentlyContinue).Path"
    )
    m = re.search(r"([A-Za-z]:\\[^\r\n]*?BetterGI\.exe)", out, re.IGNORECASE)
    if m and os.path.isfile(m.group(1)):
        return m.group(1)

    # 2) 桌面 / 开始菜单快捷方式（一次性把所有候选 .lnk 交给一个 PowerShell 解析）
    bases = [
        os.path.join(os.environ.get("USERPROFILE") or "", "Desktop"),
        os.path.join(os.environ.get("USERPROFILE") or "", "OneDrive",
                     "Desktop"),
        os.path.join(os.environ.get("USERPROFILE") or "", "OneDrive", "桌面"),
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                     "Start Menu", "Programs"),
        os.path.join(os.environ.get("ProgramData", ""), "Microsoft", "Windows",
                     "Start Menu", "Programs"),
    ]
    lnks = []
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        try:
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    if fn.lower().endswith(".lnk") and \
                            "bettergi" in fn.lower():
                        lnks.append(os.path.join(root, fn))
        except OSError:
            continue
    if lnks:
        arr = ",".join("'%s'" % p.replace("'", "''") for p in lnks[:20])
        code, out = ps(
            "$sh=New-Object -ComObject WScript.Shell;"
            "@(%s) | ForEach-Object { "
            "try { $sh.CreateShortcut($_).TargetPath } catch {} }" % arr)
        for line in out.splitlines():
            line = line.strip().strip('"')
            if line.lower().endswith(_EXE_NAME_LOWER) and os.path.isfile(line):
                return line

    # 3) 注册表（一次 ps 查所有相关 key）
    views = ["HKCU:\\Software\\Classes\\Applications\\BetterGI.exe",
             "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\"
             "App Paths\\BetterGI.exe",
             "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\"
             "App Paths\\BetterGI.exe"]
    script = " ".join(
        "(Get-ItemProperty -Path '%s' -ErrorAction SilentlyContinue)"
        ".'(default)';" % v for v in views)
    code, out = ps(script)
    m = re.search(r"([A-Za-z]:\\[^\r\n]*?BetterGI\.exe)", out, re.IGNORECASE)
    if m and os.path.isfile(m.group(1)):
        return m.group(1)

    # 4) 常见安装位置（限深度，避免全盘慢扫）
    hints = []
    for drive in _list_drives():
        hints += [
            drive + "BetterGI",
            drive + "Program Files\\BetterGI",
            drive + "Program Files (x86)\\BetterGI",
            drive + "Games\\BetterGI",
        ]
    for h in hints:
        p = os.path.join(h, "BetterGI.exe")
        if os.path.isfile(p):
            return p

    # 5) 有限深度全盘扫描（较慢，交给调用方在界面上提示）
    skip = ("$windows", "$recycle", "system volume information",
            "windows", "program files", "program files (x86)")
    for drive in _list_drives():
        for root, dirs, files in os.walk(drive):
            rel = os.path.relpath(root, drive).lower()
            if rel.count(os.sep) >= 4:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d.lower() not in skip
                       and not d.lower().startswith("$")]
            for fn in files:
                if fn.lower() == _EXE_NAME_LOWER:
                    return os.path.join(root, fn)
    return ""


def list_onedragon_configs(bgi_dir):
    """读取 BetterGI\\User\\OneDragon\\*.json，取出配置名。"""
    d = os.path.join(bgi_dir, "User", "OneDragon")
    names = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".json"):
                names.append(os.path.splitext(fn)[0])
    return names


# ---------------------------------------------------------------------------
# 计划任务（schtasks）
# ---------------------------------------------------------------------------

_PS_TASK_QUERY = (
    "Get-ScheduledTask -TaskName '%s' -TaskPath '\\' -ErrorAction SilentlyContinue"
)


def task_exists(name):
    code, out = ps(
        "$t=Get-ScheduledTask -TaskName '%s' -TaskPath '\\' "
        "-ErrorAction SilentlyContinue; if($t){'EXISTS'}else{'NO'}" % name)
    return "EXISTS" in out


def task_info(name):
    """返回 dict：State / LastRunTime / LastResult / NextRunTime。"""
    script = (
        "$t=Get-ScheduledTask -TaskName '%s' -TaskPath '\\' -ErrorAction SilentlyContinue;"
        "if(-not $t){ 'NONE'; exit };"
        "$i=Get-ScheduledTaskInfo -TaskName '%s' -TaskPath '\\';"
        "'STATE=' + $t.State;"
        "'LAST=' + $i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss');"
        "'RESULT=' + $i.LastTaskResult;"
        "'NEXT=' + $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss');"
        "'WAKETORUN=' + $t.Settings.WakeToRun;"
        "'BATTERY=' + $t.Settings.DisallowStartIfOnBatteries"
    ) % (name, name)
    code, out = ps(script)
    if "NONE" in out:
        return None
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    # 把 Windows 的返回码翻译成人话（0x41303=从未运行 这类不是错误）
    try:
        rc = int(d.get("RESULT", "0"))
        d["RESULT_HEX"] = "0x%X" % rc
        d["RESULT_TEXT"] = _task_result_text(rc)
    except (TypeError, ValueError):
        d["RESULT_TEXT"] = ""
    return d


# 计划任务返回码字典（Task Scheduler 的 SCHED_S_* 系列）
_TASK_RESULTS = {
    0x0: "成功",
    0x1: "任务已在运行（未重复启动）",
    0x41300: "任务未运行（被条件阻止）",
    0x41301: "任务正在运行",
    0x41303: "任务从未运行过（刚创建，属正常）",
    0x41306: "任务被终止",
    0x41325: "休眠状态下无法唤醒（RTC 唤醒被禁用）",
}


def _task_result_text(rc):
    if rc in _TASK_RESULTS:
        return _TASK_RESULTS[rc]
    if rc == 267009:      # 0x41301
        return _TASK_RESULTS[0x41301]
    if rc == 267011:      # 0x41303
        return _TASK_RESULTS[0x41303]
    if rc == 267014:      # 0x41306
        return _TASK_RESULTS[0x41306]
    if rc == 267025:      # 0x41309
        return "任务被条件阻止（如未插电）"
    return ""


def _win_verify_waketimer():
    """检查本机是否允许唤醒定时器（S3 + 唤醒前提）。返回 (ac, dc)。"""
    code, out = run(["powercfg", "/q", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE"])
    ac = dc = None
    m = re.search(r"当前交流电源设置索引:\s*0x([0-9a-fA-F]+)", out)
    if m:
        ac = int(m.group(1), 16)
    m = re.search(r"当前直流电源设置索引:\s*0x([0-9a-fA-F]+)", out)
    if m:
        dc = int(m.group(1), 16)
    if ac is None and dc is None:
        # 英文系统回退
        m = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", out)
        if m:
            ac = int(m.group(1), 16)
        m = re.search(r"Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)", out)
        if m:
            dc = int(m.group(1), 16)
    return ac, dc


def _set_waketimer(enable=True):
    val = "1" if enable else "0"
    run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_SLEEP",
         "RTCWAKE", val])
    run(["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", "SUB_SLEEP",
         "RTCWAKE", val])
    run(["powercfg", "/setactive", "SCHEME_CURRENT"])


# ---------------------------------------------------------------------------
# 让本程序始终以管理员身份运行
# ---------------------------------------------------------------------------

_LAYERS_KEY = (r"Software\Microsoft\Windows NT\CurrentVersion"
               r"\AppCompatFlags\Layers")


def self_exe():
    """本程序 exe 的绝对路径。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(sys.argv[0])


def runasadmin_enabled():
    """当前 exe 是否已带「以管理员身份运行」标记。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _LAYERS_KEY) as k:
            v, _ = winreg.QueryValueEx(k, self_exe())
        return "RUNASADMIN" in str(v).upper()
    except OSError:
        return False


def cleanup_stale_exe():
    """清掉升级时留下的旧 exe（「改名法」升级的副产物）。

    Windows 不允许覆盖正在运行的 exe，所以升级只能
    「把旧的改名 → 放新的」；旧文件会被锁到旧进程退出为止。
    这里在每次启动时顺手删掉它，避免目录里越堆越多。
    """
    d = os.path.dirname(self_exe())
    removed = []
    try:
        for f in os.listdir(d):
            if not f.lower().endswith(".old.exe"):
                continue
            try:
                os.remove(os.path.join(d, f))
                removed.append(f)
            except OSError:
                pass
    except OSError:
        pass
    return removed


def set_runasadmin(on=True):
    """给本程序打 / 取消「始终以管理员身份运行」标记。

    写的是 HKCU\\...\\AppCompatFlags\\Layers，**不需要管理员权限**。

    为什么需要这个：ok-ww 启动 PC 版鸣潮强制要求管理员身份（游戏自己的
    清单也是 requireAdministrator）。靠"用到时才提权"有两个致命问题：
      · 这台机器 UAC 是 `ConsentPromptBehaviorAdmin=0`（提权不提示），
        用户什么都看不到，以为"根本没反应"（2026-10-05 用户实测反馈）；
      · 若 UAC 是默认级别，凌晨三点没人能点那个弹窗。
    加上这个标记后：**双击图标就自动带管理员权限启动**，
    之后「安装任务」「立即接力」全都不再需要任何提权动作。
    （计划任务是自己带 Highest 令牌启动的，不受此标记影响。）
    """
    try:
        import winreg
        exe = self_exe()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _LAYERS_KEY) as k:
            cur = ""
            try:
                cur, _ = winreg.QueryValueEx(k, exe)
                cur = str(cur).strip()
            except OSError:
                pass
            words = [w for w in cur.split() if w and w != "~"]
            has = any(w.upper() == "RUNASADMIN" for w in words)
            if on and not has:
                words.append("RUNASADMIN")
            elif not on and has:
                words = [w for w in words if w.upper() != "RUNASADMIN"]
            if words:
                winreg.SetValueEx(k, exe, 0, winreg.REG_SZ,
                                  "~ " + " ".join(words))
            else:
                try:
                    winreg.DeleteValue(k, exe)
                except OSError:
                    pass
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 运行时保持唤醒
# ---------------------------------------------------------------------------

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


def keep_awake(on=True, with_display=True):
    """运行期间阻止系统自动休眠（可选连显示器一起保持点亮）。

    为什么需要：一条龙/鸣潮日常要跑一两个小时，如果中途系统自动睡眠，
    BetterGI 的截屏识别和键鼠模拟会一起失效 —— 表现是**任务卡死在那里
    但进程还活着**（本机 2026-10-05 凌晨实测：BetterGI 卡在自动秘境，
    「切换角色卡住，执行脱困」空转了 806 次、白等 5 小时）。

    用 SetThreadExecutionState（不需要管理员）：调用后对**本进程**有效，
    进程退出自动失效，所以退出前调一次 `keep_awake(False)` 恢复。

    返回 True/False 表示调用是否成功。失败不影响主流程。
    """
    try:
        import ctypes
        flags = _ES_CONTINUOUS
        if on:
            flags |= _ES_SYSTEM_REQUIRED
            if with_display:
                # 强制显示器常亮。半夜房间会亮着，但能最大程度保证
                # 游戏正常渲染（渲染冻结是"卡住"的常见成因之一）。
                flags |= _ES_DISPLAY_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 按需接力任务（不需要 UAC 的手动接力）
# ---------------------------------------------------------------------------

def relay_report_path():
    """按需接力任务的报告文件路径（固定，GUI 靠它回读结果）。"""
    return os.path.join(user_data_dir(), "chain_report.txt")


def install_relay_task(cfg, log_cb=None):
    """创建/更新「按需接力」计划任务（无触发器，最高权限）。

    这样「立即接力一次」只需要 `Start-ScheduledTask`，
    **不需要 UAC、也不需要人点任何东西**。
    注意：创建这个任务本身需要管理员 —— 它跟主任务一起在 --install 里装。
    """
    emit = log_cb or (lambda m: None)
    name = RELAY_TASK
    exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                          else __file__)
    rpt = relay_report_path()
    args = '--chain-now --report "%s"' % rpt
    user = _principal_script().replace("'", "''")
    n = name.replace("'", "''")

    def _register(logon_type, run_level):
        script = (
            "if(Get-ScheduledTask -TaskName '%s' -TaskPath '\\' "
            "-ErrorAction SilentlyContinue){ "
            "  Unregister-ScheduledTask -TaskName '%s' -TaskPath '\\' "
            "  -Confirm:$false -ErrorAction SilentlyContinue };"
            # **故意不给任何 Trigger** —— 只在手动启动时运行
            "$a=New-ScheduledTaskAction -Execute '%s' -Argument '%s';"
            "$st=New-ScheduledTaskSettingsSet -StartWhenAvailable "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-MultipleInstances IgnoreNew "
            "-ExecutionTimeLimit (New-TimeSpan -Hours 8);"
            "$pr=New-ScheduledTaskPrincipal -UserId '%s' -LogonType %s "
            "-RunLevel %s;"
            "Register-ScheduledTask -TaskName '%s' -TaskPath '\\' "
            "-Action $a -Settings $st -Principal $pr -Force "
            "-ErrorAction Stop | Out-Null;"
            "if(Get-ScheduledTask -TaskName '%s' -TaskPath '\\' "
            "-ErrorAction SilentlyContinue){ 'OK' } else { 'VERIFYFAIL' }"
        ) % (n, n, exe.replace("'", "''"), args.replace("'", "''"),
             user, logon_type, run_level, n, n)
        return ps(script, timeout=90)

    last = ""
    for logon_type, run_level in (("Interactive", "Highest"),
                                  ("Interactive", "Limited")):
        code, out = _register(logon_type, run_level)
        out = (out or "").strip()
        last = out
        if "OK" in out:
            emit("按需接力任务已就绪（%s / %s）—— 以后手动接力不用点 UAC。"
                 % (logon_type, run_level))
            return True, "OK"
    emit("按需接力任务创建失败：%s" % last[-300:])
    return False, last[-300:]


def start_relay_task(log_cb=None):
    """启动「按需接力」任务（不需要管理员权限）。返回 (成功, 说明)。"""
    emit = log_cb or (lambda m: None)
    name = RELAY_TASK.replace("'", "''")
    # 先清掉上一次的旧报告，免得把上次的结果当成本次
    try:
        os.remove(relay_report_path())
    except OSError:
        pass
    code, out = ps(
        "$ErrorActionPreference='SilentlyContinue';"
        "if(-not (Get-ScheduledTask -TaskName '%s' -TaskPath '\\')){"
        "  'NOTASK'; exit };"
        "Start-ScheduledTask -TaskName '%s' -TaskPath '\\' "
        "-ErrorAction Stop;"
        "Start-Sleep -Seconds 3;"
        "$t=Get-ScheduledTask -TaskName '%s' -TaskPath '\\';"
        "'STATE=' + $t.State" % (name, name, name), timeout=90)
    out = (out or "").strip()
    if "NOTASK" in out:
        return False, ("还没创建「按需接力」任务。\n"
                       "请先在「自动运行」页点一次「安装 / 更新任务」"
                       "（那一步需要提权一次，之后手动接力就不用再点了）。")
    if "STATE=Running" in out:
        emit("按需接力任务已启动（它本身是最高权限，不需要 UAC）。")
        return True, "已启动"
    # 有些环境 Start-ScheduledTask 立刻返回，State 还没变成 Running
    if "STATE=" in out:
        emit("按需接力任务已触发（%s）。" % out.replace("STATE=", "状态="))
        return True, "已触发"
    return False, out[-300:] or "启动失败"


def _disable_console_lock():
    """关闭「唤醒时需要重新登录」。返回 (成功, 说明)。

    这个开关非常关键：唤醒后若停在登录界面，BetterGI 的键鼠模拟
    全部失效，一条龙等于白跑。

    现实情况：注册表里这个子项默认**不存在**（只有 Windows 在需要时
    才materialize），所以「读不到」不等于「设置失败」。
    真正生效的是下面的 powercfg 直下发 —— 它会创建/更新该值。
    因此判定标准是：powercfg 有没有报错。
    """
    guid = "0e796bdb-100d-47d6-a2d5-f7d2daa51f51"
    # 注意：脚本里含大量 {} （PowerShell 块），不能用 str.format() ——
    # 会被当成占位符报 KeyError。这里用 %s 拼接。
    script = (
        "$s='" + guid + "';"
        "$base='HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Power\\User\\"
        "PowerSchemes';"
        "$act=(Get-ItemProperty -Path $base -Name ActivePowerScheme "
        "-ErrorAction SilentlyContinue).ActivePowerScheme;"
        "if(-not $act){ 'NOSCHEME'; exit };"
        "$k = Join-Path -Path $base -ChildPath "
        "($act + '\\fea3413e-7e05-4911-9a71-700331f1c294\\' + $s);"
        "if (-not (Test-Path $k)) {"
        "  New-Item -Path $k -Force -ErrorAction SilentlyContinue | Out-Null"
        "};"
        "if (Test-Path $k) {"
        "  Set-ItemProperty -Path $k -Name 'ACSettingIndex' -Value 0 "
        "-Type DWord -Force -ErrorAction SilentlyContinue;"
        "  Set-ItemProperty -Path $k -Name 'DCSettingIndex' -Value 0 "
        "-Type DWord -Force -ErrorAction SilentlyContinue;"
        "  'OK'"
        "} else { 'NOTFOUND' }"
    )
    code, out = ps(script)
    reg_ok = "OK" in out

    # powercfg 直下发：这一步才是真正生效的（会创建/更新该值）
    r1 = run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_NONE",
              guid, "0"])
    r2 = run(["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", "SUB_NONE",
              guid, "0"])
    r3 = run(["powercfg", "/setactive", "SCHEME_CURRENT"])
    pcg_ok = (r1[0] == 0 and r2[0] == 0 and r3[0] == 0)

    if pcg_ok:
        return True, "已关闭（唤醒后直接进桌面）"
    if reg_ok:
        return True, "已通过注册表关闭"
    # 两种方式都没成功 —— 多半是权限不够
    if not is_admin():
        return False, "需要管理员权限：请点「应用电源设置」并允许 UAC"
    return False, ("powercfg 返回 %s/%s/%s（%s）"
                   % (r1[0], r2[0], r3[0], (r1[1] or r2[1] or "").strip()[:200]))


def _principal_script():
    """构造任务主体。

    必须用「当前用户 + Interactive + 最高权限」，不能用 SYSTEM：
    SYSTEM 账户没有交互桌面，BetterGI 的键鼠模拟会完全失效。
    """
    user = "%s\\%s" % (os.environ.get("USERDOMAIN") or
                        os.environ.get("COMPUTERNAME") or ".",
                        os.environ.get("USERNAME") or "SYSTEM")
    return user


def install_task(cfg, log_cb=None):
    """创建/更新每日唤醒型计划任务。返回 (成功, 说明)。

    语义明确：**先删后建**（不是 -Force 覆盖）。
    原因：-Force 在「旧任务正在运行」时会静默失败，用户看到"创建成功"
    但任务其实是旧的（本机 2026-10-03 就中过这个招，任务时间一直停在
    01:12，而界面显示每次都装成功）。

    触发时间带**随机误差**：task_time ~ task_time+RandomMinutes 之间随机，
    避免每天同一秒唤醒。
    """
    emit = log_cb or (lambda m: None)
    name = cfg.task_name
    start = cfg.next_run()
    r = cfg.random_minutes
    if r > 0:
        emit("随机误差已启用：基准 %s，上限 %d 分钟 → 本次触发 %s"
             % (cfg.task_time, r, start.strftime("%m-%d %H:%M")))
    else:
        emit("随机误差已关闭（0 分钟），每天固定 %s 触发" % cfg.task_time)

    exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                          else __file__)
    args = '--run-daily --task-name "%s"' % name
    user = _principal_script().replace("'", "''")
    n = name.replace("'", "''")

    existed = task_info(name) is not None
    if existed:
        emit("检测到已有同名任务，将先删除再重建（保证设置真正生效）…")

    def _register(logon_type, run_level):
        # 先删 + 回读验证：注册成功 ≠ 设置生效
        script = (
            "if(Get-ScheduledTask -TaskName '%s' -TaskPath '\\' "
            "-ErrorAction SilentlyContinue){ "
            "  Unregister-ScheduledTask -TaskName '%s' -TaskPath '\\' "
            "  -Confirm:$false -ErrorAction SilentlyContinue };"
            "$a=New-ScheduledTaskAction -Execute '%s' -Argument '%s';"
            "$tr=New-ScheduledTaskTrigger -Daily -At '%s';"
            "$st=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-MultipleInstances IgnoreNew "
            # 时限必须给足：一条龙本身可能跑 1~2 小时，
            # 启用「接力鸣潮」后还要再加鸣潮日常的时间。
            # 给 2 小时会被 Windows 中途杀掉，链子断在半路。
            "-ExecutionTimeLimit (New-TimeSpan -Hours 8);"
            "$pr=New-ScheduledTaskPrincipal -UserId '%s' -LogonType %s "
            "-RunLevel %s;"
            "Register-ScheduledTask -TaskName '%s' -TaskPath '\\' -Action $a "
            "-Trigger $tr -Settings $st -Principal $pr -Force "
            "-ErrorAction Stop | Out-Null;"
            # 验证必须静默（任务刚建好时 CIM 查询会偶发 ObjectNotFound）
            "if(Get-ScheduledTask -TaskName '%s' -TaskPath '\\' "
            "-ErrorAction SilentlyContinue){ 'OK' } else { 'VERIFYFAIL' }"
        ) % (n, n, exe.replace("'", "''"), args.replace("'", "''"),
             start.strftime("%Y-%m-%dT%H:%M:%S"), user, logon_type,
             run_level, n, n)
        return globals()["ps"](script, timeout=90)

    last = ""
    # 降级顺序说明：Interactive+Highest 是首选（有交互桌面 + 最高权限）。
    # Interactive+Limited 次之。**不要用 InteractiveOrPassword** ——
    # 本机账户未设密码，Windows 会直接拒绝
    # （"Account restrictions are preventing this user from signing in"）。
    for logon_type, run_level in (("Interactive", "Highest"),
                                  ("Interactive", "Limited")):
        code, out = _register(logon_type, run_level)
        last = out
        if "OK" in out:
            verb = "已更新" if existed else "已创建"
            emit("计划任务%s：%s" % (verb, name))
            if run_level != "Highest":
                emit("提示：任务以普通权限创建，运行时可能弹一次 UAC")
            # 回读并把「实际生效的设置」原样报给用户，
            # 杜绝「界面说成功、实际还是旧设置」的情况
            info = task_info(name)
            if info:
                emit("实际生效 → 下次运行 %s ｜ 唤醒=%s ｜ 电池限制=%s ｜ "
                     "权限=%s"
                     % (info.get("NEXT"), info.get("WAKETORUN"),
                        info.get("BATTERY"), run_level), "ok")
                want = start.strftime("%Y-%m-%d %H:%M:%S")[:16]
                got = (info.get("NEXT") or "").strip()[:16]
                if got and got != want:
                    emit("⚠ 校验不一致：期望下次 %s，实际 %s。"
                         "说明有更高优先级的同名任务或策略在覆盖。"
                         % (want, got), "warn")
            # 顺手把「按需接力」任务也装上。它让「立即接力一次」变成
            # **不需要 UAC**（触发已存在的最高权限任务即可）——
            # 用户明确反馈过：UAC 弹不出来 / 凌晨没人能点。
            # 放在这里是因为装它同样需要管理员，而这一步已经提权了。
            try:
                install_relay_task(cfg, emit)
            except Exception as exc:  # noqa: BLE001
                emit("提示：按需接力任务创建异常（%s），"
                     "手动接力会退回提权方式。" % exc)
            return True, out.strip()

    # 全部失败：把原因翻译成人话
    reason = "未知原因"
    low = last.lower()
    if "account restrictions" in low or "blank password" in low:
        reason = ("账户限制：Windows 拒绝了该任务的登录方式。"
                  "通常是因为账户没设密码 —— 请给账户设个密码，"
                  "或用「Interactive + Limited」方式。")
    elif "access is denied" in low or "拒绝访问" in last:
        reason = "权限不足：需要以管理员身份运行（请点「安装 / 更新任务」并允许 UAC）"
    elif "verifail" in low:
        reason = "任务已提交但回读不到，可能仍被策略限制"
    emit("安装失败：%s" % reason, "err")
    return False, reason + " | " + last.strip()[-400:]


def reschedule_task(cfg, when=None, log_cb=None):
    """把每日任务的触发时间改成 when（默认用带随机误差的 next_run）。

    用途：**每次跑完就把明天的触发时间随机化**，这样"唤醒时刻"本身
    每天都不一样，而不是固定在同一个秒。

    实现要点：
    - 用 `Set-ScheduledTask -Trigger` 热更新，不删任务（任务正在运行时
      也能改，比先删后建安全）。
    - 失败绝不能影响本次一条龙 —— 返回 (成功, 说明)，由调用方决定是否记录。
    """
    emit = log_cb or (lambda m: None)
    when = when or cfg.next_run()
    name = cfg.task_name
    n = name.replace("'", "''")
    at = when.strftime("%Y-%m-%dT%H:%M:%S")
    script = (
        "$t=Get-ScheduledTask -TaskName '%s' -TaskPath '\\' "
        "-ErrorAction SilentlyContinue;"
        "if(-not $t){ 'NOTASK'; exit };"
        "try{"
        "  $tr=New-ScheduledTaskTrigger -Daily -At '%s';"
        "  $t.Triggers = $tr;"
        "  Set-ScheduledTask -InputObject $t -ErrorAction Stop | Out-Null;"
        "  'OK'"
        "}catch{ 'ERR ' + $_.Exception.Message }"
    ) % (n, at)
    code, out = ps(script, timeout=60)
    if "OK" in out:
        emit("已把下次运行随机到 %s" % when.strftime("%m-%d %H:%M"))
        return True, when.strftime("%Y-%m-%d %H:%M")
    if "NOTASK" in out:
        return False, "任务不存在"
    return False, out.strip()[-200:]


def survey_autostarts():
    """全量扫描：所有与本程序 / BetterGI 相关的计划任务与自启项。

    用户经常搞不清"每次点安装到底是新建还是覆盖、系统里现在有几个"。
    这个函数把真相一次性摊开：任务名、指向哪个 exe、触发时间、
    是否有重复触发、开机自启、当前有没有 BetterGI 在跑。

    返回 (文本, 发现的条目数)。文本直接可读，适合塞进日志页。
    """
    ps_ = globals()["ps"]
    # 分两段做，避开本机踩过的三个坑：
    # 1) 先只按**任务名**筛（便宜、不会抛错），结果用 @() 包成数组
    #    —— Where-Object 只命中一条时返回单个对象，.Count 会是空字符串。
    # 2) 再对命中的少数几个去读 Actions/Triggers，并逐项 try/catch
    #    —— 某些系统任务访问这些属性会抛异常，若在 Where-Object 里
    #    直接对全部 220 个任务求值，整个管道会中断、结果全丢。
    code, out = ps_(
        "$names = @(Get-ScheduledTask -ErrorAction SilentlyContinue |"
        "  Where-Object { $_.TaskName -match 'AutoBGI|BetterGI|OneDragon' });"
        "if($names.Count -gt 0){ $names | ForEach-Object { 'N:' + $_.TaskName } }"
    )
    names = []
    for ln in out.splitlines():
        if ln.startswith("N:"):
            names.append(ln[2:].strip())

    # 补充：按 Action 内容再扫一遍（任务名不含关键字但指向本程序的）
    if not names:
        code2, out2 = ps_(
            "$hit = @();"
            "foreach($t in @(Get-ScheduledTask -ErrorAction SilentlyContinue)){"
            "  try{"
            "    foreach($a in $t.Actions){"
            "      if(($a.Execute + ' ' + $a.Arguments) "
            "         -match 'AutoBGI|BetterGI'){ $hit += $t.TaskName; break } }"
            "  }catch{ }"
            "}"
            "if($hit.Count -gt 0){ $hit | ForEach-Object { 'N:' + $_ } }"
        )
        for ln in out2.splitlines():
            if ln.startswith("N:"):
                names.append(ln[2:].strip())

    tasks = ""
    n = 0
    if not names:
        tasks = "（没有找到任何相关计划任务）"
    else:
        for nm in names:
            n += 1
            code3, out3 = ps_(
                "$n='%s';"
                "$t = Get-ScheduledTask -TaskName $n -TaskPath '\\' "
                "-ErrorAction SilentlyContinue;"
                "if(-not $t){ 'MISSING'; exit };"
                "$i = Get-ScheduledTaskInfo -TaskName $n -TaskPath '\\' "
                "-ErrorAction SilentlyContinue;"
                "'名称  : ' + $n;"
                "'状态  : ' + $t.State;"
                "'上次  : ' + $i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss');"
                "'结果码: ' + $i.LastTaskResult;"
                "'下次  : ' + $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss');"
                # 属性访问用 -ErrorAction SilentlyContinue，
                # **不要用 try/catch** —— PowerShell 5.1 里
                # "}catch{ '字面量';" 这种紧跟写法会触发 ParserError
                # （本机实测，整段脚本直接语法错误、什么都读不出来）。
                "'指向  : ' + (($t.Actions | ForEach-Object {"
                "   $_.Execute + ' ' + $_.Arguments }) -join ' ; ');"
                "'触发  : ' + (($t.Triggers | ForEach-Object {"
                "   $_.CimClass.CimClassName + ' @' + $_.StartBoundary }) "
                "-join ' ; ');"
                "'重复  : ' + (($t.Triggers | ForEach-Object {"
                "   if($_.Repetition.Interval){ '每 ' + $_.Repetition.Interval"
                "   + ' 持续 ' + $_.Repetition.Duration } else { '无' } }) "
                "-join ' ; ');"
                "'唤醒  : ' + $t.Settings.WakeToRun;"
                "'主体  : ' + $t.Principal.UserId + ' / ' "
                "+ $t.Principal.LogonType + ' / ' + $t.Principal.RunLevel"
                % nm.replace("'", "''")
            )
            if "MISSING" in out3:
                continue
            block = ["==="]
            block += ["  " + ln.strip()
                      for ln in out3.splitlines() if ln.strip()]
            tasks += "\n".join(block) + "\n"
        if not tasks.strip():
            tasks = "（没有找到任何相关计划任务）"
            n = 0

    # 注册表自启
    code2, out2 = ps_(
        "$r=@();"
        "foreach($p in @("
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run',"
        "'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run')){"
        "  $k=Get-ItemProperty -Path $p -ErrorAction SilentlyContinue;"
        "  if($k){ $k.PSObject.Properties | Where-Object {"
        "     $_.Name -notmatch '^PS' -and "
        "     ($_.Name -match 'AutoBGI|BetterGI' -or "
        "      [string]$_.Value -match 'AutoBGI|BetterGI') } |"
        "     ForEach-Object { $r += $_.Name + ' = ' + $_.Value } } };"
        "if($r.Count){ $r -join \"`n\" } else { 'NONE' }")
    reg = out2.strip() if out2.strip() and "NONE" not in out2 else "（无）"

    # 开机启动文件夹
    folders = []
    for d in (os.path.join(os.environ.get("APPDATA", ""), "Microsoft",
                           "Windows", "Start Menu", "Programs", "Startup"),
              os.path.join(os.environ.get("ProgramData", ""), "Microsoft",
                           "Windows", "Start Menu", "Programs", "Startup")):
        if d and os.path.isdir(d):
            hits = [f for f in os.listdir(d)
                    if "autobgi" in f.lower() or "bettergi" in f.lower()]
            if hits:
                folders.append("%s → %s" % (d, "、".join(hits)))
    startup = "\n".join(folders) if folders else "（无）"

    # 正在跑的进程
    code3, out3 = ps_(
        "Get-Process BetterGI -ErrorAction SilentlyContinue | "
        "ForEach-Object { 'PID ' + $_.Id + '  启动于 ' + "
        "$_.StartTime.ToString('HH:mm:ss') }")
    running = out3.strip() or "（无）"

    text = (
        "【计划任务】（共 %d 个）\n%s\n\n"
        "【注册表自启】\n%s\n\n"
        "【开机启动文件夹】\n%s\n\n"
        "【正在运行的 BetterGI】\n%s"
        % (n, tasks, reg, startup, running)
    )
    return text, n


def remove_task(name):
    """删除计划任务。返回 (成功, 说明)。

    语义明确：成功=True 且说明里写"已删除"；任务本来就不存在也算成功
    （达成目标了）。失败时必须说清原因，通常是权限不足。

    绝对不要碰 schtasks.exe —— 本机沙箱把它列入程序黑名单，
    调用会**直接阻断整个进程**，不是返回失败码。
    """
    code, out = ps(
        "if(Get-ScheduledTask -TaskName '%s' -TaskPath '\\' "
        "-ErrorAction SilentlyContinue){ "
        "try{ Unregister-ScheduledTask -TaskName '%s' -TaskPath '\\' "
        "-Confirm:$false -ErrorAction Stop; 'DELETED' } "
        "catch{ 'DELERR ' + $_.Exception.Message } } else { 'ABSENT' }"
        % (name.replace("'", "''"), name.replace("'", "''")))
    if "DELETED" in out:
        return True, "已删除"
    if "ABSENT" in out:
        return True, "本来就���在，无需删除"
    if "DELERR" in out:
        detail = out.split("DELERR", 1)[1].strip()[:200]
        if "denied" in detail.lower() or "拒绝" in detail:
            return False, "权限不足：删除计划任务需要管理员权限"
        return False, "删除失败：%s" % detail
    # 没拿到明确结论
    if task_info(name) is None:
        return True, "已不存在"
    return False, "删除未成功（可能权限不足）"


def run_task_now(name):
    """立即触发计划任务。返回 (成功, 说明)。

    注意：这里必须显式返回二元组，调用方按 (ok, msg) 解包 ——
    直接把 ps() 的 (code, out) 透传会让上层把 code 当成"成功"标志。

    【2026-10-05 改】原先只写了一句
        Start-ScheduledTask ... -ErrorAction Stop; if($?){'OK'}
    失败时只会得到一句空话，看不出到底是「任务不存在」「已在运行」
    还是「权限不足」。现在把每种情况分开报。
    """
    n = name.replace("'", "''")
    code, out = ps(
        "$ErrorActionPreference='SilentlyContinue';"
        "if(-not (Get-ScheduledTask -TaskName '%s' -TaskPath '\\')){"
        "  'NOTASK'; exit };"
        "$t=Get-ScheduledTask -TaskName '%s' -TaskPath '\\';"
        "if($t.State -eq 'Running'){ 'ALREADY'; exit };"
        "try{"
        "  Start-ScheduledTask -TaskName '%s' -TaskPath '\\' "
        "-ErrorAction Stop;"
        "  Start-Sleep -Seconds 2;"
        "  $t2=Get-ScheduledTask -TaskName '%s' -TaskPath '\\';"
        "  'OK ' + $t2.State"
        "}catch{ 'ERR ' + $_.Exception.Message }" % (n, n, n, n), timeout=90)
    out = (out or "").strip()
    if "OK" in out:
        return True, "已触发"
    if "NOTASK" in out:
        return False, "计划任务不存在 —— 请先点「安装 / 更新任务」创建它"
    if "ALREADY" in out:
        return False, "任务已经在运行中，不需要重复触发"
    if "ERR" in out:
        return False, out.replace("ERR ", "")
    return False, out or "触发失败（PowerShell 没有返回原因）"


def stop_task_now(name):
    """强制结束正在运行的计划任务。返回 (成功, 说明)。"""
    n = name.replace("'", "''")
    code, out = ps(
        "$ErrorActionPreference='SilentlyContinue';"
        "try{ Stop-ScheduledTask -TaskName '%s' -TaskPath '\\' "
        "-ErrorAction Stop; 'OK' }"
        "catch{ 'ERR ' + $_.Exception.Message }" % n, timeout=90)
    out = (out or "").strip()
    return ("OK" in out), (out or "无返回")


def set_autostart(enable, exe_path):
    """把本程序加入/移出登录自启（注册表 Run 键）。"""
    key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
    val = "AutoBGIManager"
    if enable:
        code, out = ps(
            "New-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\"
            "CurrentVersion\\Run' -Name 'AutoBGIManager' -Value '\"%s\"' "
            "-PropertyType String -Force | Out-Null; 'OK'"
            % exe_path.replace("'", "''"))
    else:
        code, out = ps(
            "Remove-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\"
            "CurrentVersion\\Run' -Name 'AutoBGIManager' "
            "-ErrorAction SilentlyContinue; 'OK'")
    return "OK" in out, out


def get_autostart():
    code, out = ps(
        "(Get-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\"
        "CurrentVersion\\Run' -Name AutoBGIManager "
        "-ErrorAction SilentlyContinue).AutoBGIManager")
    return out.strip().strip('"')


# ---------------------------------------------------------------------------
# 睡眠唤醒测试
# ---------------------------------------------------------------------------

def create_wake_test(minutes=2, log_cb=None):
    """建一次性唤醒任务，到点自动跑一条龙。返回 (成功, 说明)。

    踩过的坑（2026-10-03）：
    1. `New-ScheduledTaskTrigger -Once` **必须给 EndBoundary**，
       否则 Register-ScheduledTask 报
       "The task XML is missing a required element or attribute. (47,4)"
       一次性任务的 EndBoundary 要显式设成一天后。
    2. `LogonType` 的合法枚举值只有
       None/Password/S4U/Interactive/Group/ServiceAccount/InteractiveOrPassword
       —— **没有 InteractiveToken**（那是运行期 API 的说法，不是枚举值）。
    3. 验证用的 `Get-ScheduledTask` 必须带 -ErrorAction SilentlyContinue，
       否则任务刚注册好也可能因 CIM 抖动抛错，误报 VERIFYFAIL。
    """
    emit = log_cb or (lambda m: None)
    exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                          else __file__)
    when = datetime.now() + timedelta(minutes=minutes)
    end = when + timedelta(days=1)
    # 触发时间用 ISO 8601。一次性任务必须带 EndBoundary，
    # 且格式必须是 ISO 8601（含时区），否则 XML 校验失败：
    #   "EndBoundary 2026-10-04 00:57:13"  → value is incorrectly formatted
    at_str = when.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%S")
    args = '--run-onedragon --skip-window --from-wake'
    user = _principal_script().replace("'", "''")

    def _register(logon_type, run_level):
        script = (
            "$a=New-ScheduledTaskAction -Execute '%s' -Argument '%s';"
            # EndBoundary 必填，且必须是 ISO 8601（见上文坑 1）
            # **绝对不要加 -RepetitionInterval**：一次性任务不需要重复，
            # 加了会变成"每分钟跑一次，持续一整天" —— 本机曾因此
            # 每分钟启动一次 BetterGI，持续 24 小时（2026-10-03 事故）。
            "$tr=New-ScheduledTaskTrigger -Once -At '%s';"
            "$tr.EndBoundary='%s';"
            "$st=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-MultipleInstances IgnoreNew "
            # 这个任务体本身就是在跑「一条龙」（--run-onedragon），
            # 时限给 5 分钟会在中途被 Windows 杀掉 —— 必须和每日任务一致。
            "-ExecutionTimeLimit (New-TimeSpan -Hours 8);"
            "$st.DeleteExpiredTaskAfter='PT2M';"
            "$pr=New-ScheduledTaskPrincipal -UserId '%s' -LogonType %s "
            "-RunLevel %s;"
            # 先删再建，避免 -Force 在有运行中实例时静默失败
            "if(Get-ScheduledTask -TaskName '%s' -TaskPath '\\' "
            "-ErrorAction SilentlyContinue){ "
            "  Unregister-ScheduledTask -TaskName '%s' -TaskPath '\\' "
            "  -Confirm:$false -ErrorAction SilentlyContinue };"
            "Register-ScheduledTask -TaskName '%s' -TaskPath '\\' -Action $a "
            "-Trigger $tr -Settings $st -Principal $pr -Force "
            "-ErrorAction Stop | Out-Null;"
            # 验证必须静默，否则 CIM 抖动会抛错落到 else（见上文坑 3）
            "if(Get-ScheduledTask -TaskName '%s' -TaskPath '\\' "
            "-ErrorAction SilentlyContinue){ 'OK' } else { 'VERIFYFAIL' }"
        ) % (exe.replace("'", "''"), args.replace("'", "''"),
             at_str, end_str, user, logon_type, run_level,
             WAKE_TEST_TASK, WAKE_TEST_TASK, WAKE_TEST_TASK,
             WAKE_TEST_TASK)
        return globals()["ps"](script, timeout=90)

    last = ""
    # 不用 InteractiveOrPassword：本机账户未设密码，Windows 会拒绝
    # （"Account restrictions are preventing this user from signing in"）
    for logon_type, run_level in (("Interactive", "Highest"),
                                  ("Interactive", "Limited")):
        code, out = _register(logon_type, run_level)
        last = out
        if "OK" in out:
            emit("测试任务已创建，%s 触发（%d 分钟后）"
                 % (when.strftime("%H:%M:%S"), minutes))
            return True, out.strip()

    reason = "未知原因"
    low = last.lower()
    if "endboundary" in low or "missing a required element" in low:
        reason = "触发器 XML 不合法（EndBoundary 问题）"
    elif "account restrictions" in low or "blank password" in low:
        reason = "账户限制：账户未设密码，Windows 拒绝了该登录方式"
    elif "access is denied" in low or "拒绝访问" in last:
        reason = "权限不足：需要以管理员身份运行（请允许 UAC）"
    emit("创建失败：%s" % reason, "err")
    return False, reason + " | " + last.strip()[-400:]


def sleep_now(seconds_delay=20):
    """让系统进入睡眠。"""
    run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
    return True


def last_wake_info():
    code, out = run(["powercfg", "/lastwake"])
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return "\n".join(lines)


def wake_timers():
    code, out = run(["powercfg", "/waketimers"])
    return out


def available_sleep_states():
    code, out = run(["powercfg", "/a"])
    return out


# ---------------------------------------------------------------------------
# 启动一条龙
# ---------------------------------------------------------------------------

def is_process_running(name="BetterGI"):
    code, out = ps(
        "if(Get-Process %s -ErrorAction SilentlyContinue){'Y'}else{'N'}" % name)
    return "Y" in out


def kill_bgi():
    ps("Get-Process BetterGI -ErrorAction SilentlyContinue "
       "| Stop-Process -Force; 'DONE'")


def window_shown(exe_path):
    script = (
        "Get-Process BetterGI -ErrorAction SilentlyContinue | "
        "Where-Object { $_.MainWindowHandle -ne 0 } | "
        "Select-Object -First 1 -ExpandProperty Id"
    )
    code, out = ps(script)
    return bool(out.strip())


def start_onedragon(cfg, log_cb=None, skip_window=False, window_lo=0,
                    window_hi=2400):
    """启动 BetterGI 并请求执行一条龙。返回 (成功, 说明)。"""
    emit = log_cb or (lambda m: None)
    exe = cfg.bgi_exe
    if not exe or not os.path.isfile(exe):
        return False, "找不到 BetterGI.exe，请先在上方指定路径"
    if not cfg.one_dragon:
        return False, "未填写一条龙配置名"

    if not skip_window:
        now = datetime.now().strftime("%H%M")
        lo, hi = cfg.window
        if not (lo <= now <= hi):
            emit("当前时间 %s 不在时间窗 %s-%s 内，跳过（如需立即执行请勾选"
                 "「忽略时间窗」）" % (now, lo, hi))
            return None, "不在时间窗"

    bdir = os.path.dirname(exe)
    args = ["--startOneDragon", cfg.one_dragon]

    # 启动 BGI **之前**先把开始菜单/搜索清掉。
    # 这一步不能等到 BGI 起来之后再做：浮层一旦拿到焦点，
    # BGI 启动瞬间的键鼠模拟就已经失效了（用户反馈："睡眠唤醒后会
    # 卡在 Win 键打开的那个界面上，导致无法识别操作"）。
    if str(cfg.get("DismissShellFloat", "1")).strip() in ("1", "true",
                                                          "True", "yes"):
        okf, whyf = dismiss_shell_float(emit)
        if not okf:
            emit("⚠ 开始菜单/搜索没能关掉，BetterGI 可能无法正常操作。"
                 "可以按一下 Esc 手动关掉。", "warn")

    emit("正在启动：%s" % (" ".join([exe] + args)))
    try:
        subprocess.Popen([exe] + args, cwd=bdir,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception as exc:  # noqa: BLE001
        return False, "启动失败：%s" % exc

    wait = max(10, cfg.i("WaitWindowSec", 180))
    ok = False
    for i in range(wait):
        time.sleep(1)
        if window_shown(exe):
            emit("BetterGI 主窗口已出现（%d 秒）" % (i + 1))
            ok = True
            break
    if not ok:
        emit("等待主窗口超时（%d 秒）" % wait)
        return False, "主窗口未出现"

    lg_dir = bgi_log_dir(bdir)
    day_log = os.path.join(lg_dir,
                           "better-genshin-impact%s.log"
                           % datetime.now().strftime("%Y%m%d"))
    verify = max(15, cfg.i("VerifySec", 420))
    deadline = time.time() + verify
    while time.time() < deadline:
        if os.path.isfile(day_log):
            code, text = read_tail(day_log, 400)
            if re.search(r"一条龙任务执行|启用一条龙配置", text):
                emit("校验通过：一条龙已启动")
                # 先腾空桌面：只留原神和 BetterGI（含它的遮罩窗口）。
                # 别的窗口盖住游戏 / 抢焦点会让 BetterGI 的截屏识别
                # 和模拟键鼠失效 —— 表现为反复「切换角色卡住，执行脱困」。
                if str(cfg.get("MinimizeOthers")).strip() in ("1", "true",
                                                              "True", "yes"):
                    n, m = minimize_other_windows(emit)
                    emit("已清理桌面：%s" % m)
                return True, "一条龙已启动"
            if re.search(r"一条龙在启动阶段被取消", text):
                emit("一条龙被取消（BGI 内该配置可能被禁用）")
                return False, "一条龙被取消"
        time.sleep(3)
    emit("超时未在 BGI 日志中看到一条龙启动，请检查 BetterGI 内"
         "「一条龙」页面是否启用")
    return False, "校验超时"


def read_tail(path, lines=200):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return 0, "".join(fh.readlines()[-lines:])
    except OSError as exc:
        return 1, str(exc)


def auto_log(cfg, text):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(os.path.join(log_dir(), "autobgi.log"), "a",
                  encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (ts, text))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 进程工具 + 接力鸣潮（ok-ww）
# ---------------------------------------------------------------------------

# BetterGI 日志里的「一条龙终态」标志（本机 0.66.0 实测）
ONEDRAGON_DONE = "一条龙和配置组任务结束"
ONEDRAGON_CANCEL = "任务被取消，退出执行"
ONEDRAGON_CANCEL2 = "一条龙在启动阶段被取消"
BGI_WINDOW_EXIT = "主窗体退出"

# 各游戏/工具的进程名（去掉 .exe，Get-Process 用的是进程名）
BGI_PROC = ["BetterGI"]
GENSHIN_PROC = ["YuanShen", "GenshinImpact"]
WUWA_PROC = ["Wuthering Waves", "Client-Win64-Shipping",
             "Client-Win32-Shipping"]
# 崩坏：星穹铁道 —— 三月七小助手（March7thAssistant）
#   工具进程：March7th Assistant.exe（主程序）/ March7th Launcher.exe（启动器）
#   游戏进程：StarRail.exe
STARRAIL_GAME_PROC = ["StarRail"]
MARCH7TH_PROC = ["March7th Assistant", "March7th Launcher"]
# 绝区零 —— ZenlessZoneZero-OneDragon（ZZZ 一条龙）
#   工具进程：OneDragon-RuntimeLauncher.exe（集成）/ OneDragon-Launcher.exe（普通）
#   游戏进程：ZenlessZoneZero.exe
ZZZ_GAME_PROC = ["ZenlessZoneZero"]
ZZZOD_PROC = ["OneDragon-RuntimeLauncher", "OneDragon-Launcher"]
# ok-ww 自己的进程（python.exe 跑 main.py）
OKWW_TOOL_PROC = ["python", "pythonw"]


# ---------------------------------------------------------------------------
# 多游戏「一条龙」阶段表
# ---------------------------------------------------------------------------
# 执行顺序由配置 `StageOrder` 决定（逗号分隔的游戏 id），例如
#     genshin,wuwa,starrail,zzz
# **默认只启用「原神 + 鸣潮」** —— 这两个是 BetterGI 与 ok-ww，
# 一般玩家本地就有；崩铁 / 绝区零默认**关闭**，装了工具再打开即可。
#
# 启动命令全部按各工具**官方文档/README** 核对过（2026-10-05 联网核实）：
#   · BetterGI          : BetterGI.exe --startOneDragon <配置名>
#   · ok-ww             : python.exe main.py -t <任务> -e
#   · March7thAssistant : "March7th Launcher.exe" <任务> -e
#                         任务 main = 完整运行；-e = 完成后自动退出
#   · ZZZ-OneDragon     : OneDragon-Launcher.exe -o -c
#                         -o = 跑一条龙；-c = 结束后关闭游戏
#
# 每个阶段的「完成」判定（借鉴 OneDragon 官方「千机链」的思路）：
#   **工具/游戏进程出现过、并随后全部消失** 才算跑完。
# 原神与鸣潮因为有日志可读，用更精确的专用判定（见下方 kind）。
STAGES = (
    {
        "id": "genshin",
        "label": "原神",
        "tool": "BetterGI",
        "kind": "builtin",                 # 专用实现（读 BGI 日志）
        "procs": tuple(BGI_PROC),
        "game_procs": tuple(GENSHIN_PROC),
        "dir_key": "BgiDir",
        "exe_names": ("BetterGI.exe",),
        "home": "https://github.com/babalae/better-genshin-impact",
        "howto": "下载 BetterGI 并解压到任意目录；本程序会自动找到它。",
    },
    {
        "id": "wuwa",
        "label": "鸣潮",
        "tool": "ok-ww",
        "kind": "builtin",                 # 专用实现（带卡死检测）
        "procs": tuple(OKWW_TOOL_PROC),
        "game_procs": tuple(WUWA_PROC),
        "dir_key": "OkwwDir",
        "exe_names": ("ok-ww.exe",),
        "home": "https://github.com/ok-oldking/ok-wuthering-waves",
        "howto": "下载 ok-ww 压缩包解压即可（目录里应有 ok-ww.exe）。",
    },
    {
        "id": "starrail",
        "label": "崩坏：星穹铁道",
        "tool": "三月七小助手（March7thAssistant）",
        "kind": "external",                # 通用实现（启动 → 等进程退出）
        "procs": tuple(MARCH7TH_PROC),
        "game_procs": tuple(STARRAIL_GAME_PROC),
        "dir_key": "M7Dir",
        "exe_names": ("March7th Launcher.exe", "March7th Assistant.exe"),
        "task_key": "M7Task",
        "default_task": "main",
        "task_hint": "main=完整运行（推荐）；也可填 daily 只做每日实训、"
                     "power 只清体力",
        "home": "https://github.com/moesnow/March7thAssistant",
        "howto": "到 Release 下载压缩包，解压到一个**全英文路径**的目录；"
                 "目录里应有「March7th Assistant.exe」。首次要先用图形界面"
                 "配好游戏路径。",
    },
    {
        "id": "zzz",
        "label": "绝区零",
        "tool": "绝区零一条龙（ZZZ-OneDragon）",
        "kind": "external",
        "procs": tuple(ZZZOD_PROC),
        "game_procs": tuple(ZZZ_GAME_PROC),
        "dir_key": "ZzzDir",
        "exe_names": ("OneDragon-Launcher.exe",
                      "OneDragon-RuntimeLauncher.exe"),
        "args_key": "ZzzArgs",
        "default_args": "-o -c",
        "args_hint": "-o = 跑一条龙，-c = 结束后关闭游戏（默认已填好）",
        "home": ("https://github.com/OneDragon-Anything/"
                 "ZenlessZoneZero-OneDragon"),
        "howto": "到 Release 下载压缩包，**必须解压到全英文、无空格的目录**"
                 "（它自己的硬性要求），然后运行安装器。首次要在它界面里"
                 "配好游戏路径并下载资源。",
    },
)

STAGE_MAP = {s["id"]: s for s in STAGES}
ALL_STAGE_IDS = tuple(s["id"] for s in STAGES)



def proc_list(names=None):
    """列出正在运行的进程，返回 [(名字, PID), ...]。

    names 给定时只保留这些（大小写不敏感、可带或不带 .exe）。
    """
    code, out = ps(
        "Get-Process -ErrorAction SilentlyContinue | ForEach-Object {"
        " $_.ProcessName + '|' + $_.Id };")
    want = None
    if names:
        want = set(n.lower().replace(".exe", "") for n in names)
    res = []
    for ln in out.splitlines():
        ln = ln.strip()
        if "|" not in ln:
            continue
        nm, _, pid = ln.rpartition("|")
        nm = nm.strip()
        if want is not None and nm.lower().replace(".exe", "") not in want:
            continue
        try:
            res.append((nm, int(pid)))
        except ValueError:
            continue
    return res


def proc_alive(names):
    return bool(proc_list(names))


def kill_proc(names, log_cb=None, wait_sec=25):
    """按进程名强制结束。返回 (是否已全部退出, 说明)。"""
    emit = log_cb or (lambda m: None)
    alive = proc_list(names)
    if not alive:
        return True, "本来就没在运行"
    emit("正在关闭 %s" % "、".join("%s(PID %d)" % p for p in alive))
    name_args = ",".join("'%s'" % n.replace("'", "''")
                         for n in names)
    ps("Get-Process -Name %s -ErrorAction SilentlyContinue |"
       " Stop-Process -Force -ErrorAction SilentlyContinue;" % name_args)
    for i in range(max(1, wait_sec)):
        time.sleep(1)
        if not proc_alive(names):
            return True, "已关闭（用时 %d 秒）" % (i + 1)
    return False, "仍未退出：%s" % (proc_list(names),)


def detect_okww(base=None):
    """探测 ok-ww 安装目录（判定标准：目录下有 ok-ww.exe）。"""
    cands = []
    if base:
        cands.append(base)
    for drive in ("E:", "D:", "C:"):
        cands.append(drive + "\\ok-ww")
    up = os.environ.get("USERPROFILE") or ""
    if up:
        cands.append(os.path.join(up, "ok-ww"))
    for d in cands:
        try:
            if d and os.path.isfile(os.path.join(d, "ok-ww.exe")):
                return d
        except OSError:
            continue
    return ""


def find_genshin_exe():
    """找原神主程序。

    首选 BetterGI 自己的配置（`<BgiDir>\\User\\config.json` 里的
    `genshinStartConfig.installPath`），这是最可靠的来源；
    找不到再扫常见目录。
    """
    try:
        bdir = Config().bgi_dir or (os.path.dirname(Config().bgi_exe))
    except Exception:  # noqa: BLE001
        bdir = ""
    if bdir:
        cfgj = os.path.join(bdir, "User", "config.json")
        if os.path.isfile(cfgj):
            try:
                with open(cfgj, encoding="utf-8", errors="replace") as fh:
                    data = json.load(fh)
                p = (data.get("genshinStartConfig") or {}).get("installPath")
                if p and os.path.isfile(p):
                    return p
            except Exception:  # noqa: BLE001
                pass
    for drive in ("E:", "D:", "C:", "F:"):
        for sub in ("原神\\Genshin Impact Game\\YuanShen.exe",
                    "Genshin Impact\\Genshin Impact Game\\YuanShen.exe",
                    "Genshin Impact Game\\YuanShen.exe",
                    "Genshin Impact\\Genshin Impact Game\\GenshinImpact.exe"):
            p = os.path.join(drive + "\\", sub)
            if os.path.isfile(p):
                return p
    return ""


def find_wuwa_exe():
    """找鸣潮启动器（优先 launcher.exe，其次游戏主程序）。"""
    roots = []
    for drive in ("C:", "D:", "E:", "F:"):
        roots.append(drive + "\\Wuthering Waves")
    for r in roots:
        for sub in ("launcher.exe",
                    "Wuthering Waves Game\\Wuthering Waves.exe"):
            p = os.path.join(r, sub)
            if os.path.isfile(p):
                return p
    return ""


def find_wuwa_game_exe():
    """找鸣潮**主程序** `Wuthering Waves Game\\Wuthering Waves.exe`。

    这是 ok-ww 真正要启动的那个文件（它会拿设备记录往上退 4 层再拼这个名字）。
    注意区别于根目录的 `launcher.exe`（启动器，ok-ww 不用它）。
    """
    for drive in ("C:", "D:", "E:", "F:"):
        for sub in ("Wuthering Waves\\Wuthering Waves Game\\"
                    "Wuthering Waves.exe",
                    "Wuthering Waves\\Wuthering Waves.exe"):
            p = os.path.join(drive + "\\", sub)
            if os.path.isfile(p):
                return p
    return ""


def okww_game_exe(cfg):
    """算出 **ok-ww 会启动哪个游戏主程序**。返回 (路径, 来源说明)。

    复刻 ok-ww 自己的规则（`src/config.py: calculate_pc_exe_path`）：
        folder = parents[3](devices.json 里的 pc_full_path)
        game   = folder / "Wuthering Waves.exe"
    ok-ww 只有在这个文件**存在**时才会去开游戏，否则报
    「Game path does not exist, Please open game manually!」——
    这正是本机踩过的坑：devices.json 里存的还是 E 盘旧路径，
    而游戏其实装在 C 盘，于是 ok-ww 死活开不了游戏。
    """
    d = getattr(cfg, "okww_dir", "") or ""
    saved = ""
    if d:
        cfgf = os.path.join(d, "data", "apps", "ok-ww", "working",
                            "configs", "devices.json")
        try:
            with open(cfgf, encoding="utf-8", errors="replace") as fh:
                saved = (json.load(fh) or {}).get("pc_full_path") or ""
        except Exception:  # noqa: BLE001
            saved = ""
    if saved:
        folder = saved
        for _ in range(4):                  # parents[3]
            folder = os.path.dirname(folder)
        p = os.path.join(folder, "Wuthering Waves.exe")
        if os.path.isfile(p):
            return p, "按 ok-ww 记录推导"
    found = find_wuwa_game_exe()
    if found:
        return found, ("ok-ww 记录里的路径已失效，改用实际位置"
                       if saved else "自动探测")
    return "", ("ok-ww 记录的路径已失效：%s" % saved) if saved else "未找到"


def device_json_path(cfg):
    d = getattr(cfg, "okww_dir", "") or ""
    if not d:
        return ""
    return os.path.join(d, "data", "apps", "ok-ww", "working",
                        "configs", "devices.json")


def fix_okww_game_path(cfg, log_cb=None):
    """把 ok-ww 的设备记录指向真实存在的游戏主程序。返回 (ok, 说明)。

    不改的话 ok-ww 永远开不了鸣潮（它只认 devices.json 里那条路径）。
    """
    emit = log_cb or (lambda m: None)
    p = device_json_path(cfg)
    if not p or not os.path.isfile(p):
        return False, "找不到 ok-ww 的设备记录：%s" % p
    game = find_wuwa_game_exe()
    if not game:
        return False, "没找到鸣潮主程序（Wuthering Waves Game\\Wuthering Waves.exe）"
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return False, "读不了设备记录：%s" % exc

    # 游戏主程序在 ...\Client\Binaries\Win64\ 下
    client = os.path.join(os.path.dirname(game), "Client", "Binaries",
                          "Win64", "Client-Win64-Shipping.exe")
    if not os.path.isfile(client):
        client = game            # 兜底：万一是别的目录结构
    old = data.get("pc_full_path", "")
    if old and os.path.normcase(old) == os.path.normcase(client):
        return True, "本来就是对的：%s" % client
    bak = p + ".bak"
    if not os.path.isfile(bak):
        try:
            shutil.copy2(p, bak)
            emit("已备份原设备记录 -> %s" % bak)
        except OSError:
            pass
    data["pc_full_path"] = client
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=4)
    except OSError as exc:
        return False, "写设备记录失败：%s" % exc
    emit("ok-ww 游戏路径已修正：")
    emit("  旧：%s" % (old or "(空)"))
    emit("  新：%s" % client)
    return True, client


def bgi_log_candidates(bgi_dir):
    """今明两天的 BGI 日志路径（跑过午夜也不会丢）。"""
    lg = bgi_log_dir(bgi_dir)
    out = []
    for delta in (0, -1):
        day = (datetime.now() + timedelta(days=delta)).strftime("%Y%m%d")
        out.append(os.path.join(lg, "better-genshin-impact%s.log" % day))
    return out


def _read_from(path, offset):
    """从 offset 起读文件新增内容，返回 (新 offset, 文本)。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return offset, ""
    if size < offset:
        offset = 0                 # 文件被截断/重建
    if size == offset:
        return offset, ""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return offset, ""
    return offset + len(data), data.decode("utf-8", "replace")


def find_genshin_hwnd():
    """找原神主窗口句柄（Unity 引擎类名固定 UnityWndClass）。0 = 没找到。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        h = u.FindWindowW("UnityWndClass", None)
        if h:
            return h
        return u.FindWindowW(None, "Genshin Impact") or 0
    except Exception:  # noqa: BLE001
        return 0


def _norm_proc(name):
    """进程名归一化：转小写 + 去掉 .exe。

    必须做这一步 —— `WUWA_PROC` 之类常量写的是 `Client-Win64-Shipping`
    （不带扩展名），而窗口探测返回的是 `client-win64-shipping.exe`，
    直接比较永远匹配不上（2026-10-05 踩到：鸣潮明明在跑，
    `find_window_by_proc` 却返回 0）。
    """
    n = (name or "").strip().lower()
    if n.endswith(".exe"):
        n = n[:-4]
    return n


def find_window_by_proc(proc_names):
    """按进程名找可见的顶层窗口句柄（返回第一个）。找不到返回 0。"""
    want = tuple(_norm_proc(p) for p in proc_names)
    for hwnd, title, proc, _mini, _ex in list_top_windows():
        if _norm_proc(proc) in want:
            return hwnd
    return 0


def restore_window(hwnd, foreground=False):
    """把被最小化的窗口还原（可选置前）。"""
    if not hwnd:
        return False
    try:
        u = ctypes.windll.user32
        u.ShowWindow(hwnd, 9)                     # SW_RESTORE
        if foreground:
            _force_foreground(hwnd)
        return True
    except Exception:  # noqa: BLE001
        return False


def _window_proc_name(hwnd):
    """取窗口所属进程的可执行文件名（小写）。失败返回空串。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        pid = ctypes.wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.wintypes.DWORD(1024)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        pass
    return ""


# WS_EX_NOREDIRECTIONBITMAP：这类窗口**不参与 DWM 合成、不渲染任何内容**。
# `Progman`(桌面) / `ApplicationFrameWindow` / `DummyDWMListenerWindow` /
# `EdgeUiInputTopWndClass` 全是这个特征 —— 它们 `IsWindowVisible()=True`
# 且 `GetWindowRect` 占满全屏，看起来"挡在屏幕上"，其实完全看不见。
# 不排除掉的话，清场逻辑会把一堆无害窗口当目标（2026-10-05 实测）。
EX_NOREDIRECTIONBITMAP = 0x00200000
# WS_EX_TOOLWINDOW：工具窗口（不在任务栏/alt-tab 出现）。
# **注意：开始菜单的宿主窗口带这个标志**，所以这里不能据此跳过 ——
# 之前就是"跳过 TOOLWINDOW + 跳过空标题"两道判断，把开始菜单整个漏掉了。
EX_TOOLWINDOW = 0x00000080


def list_top_windows():
    """列出所有**真的占着屏幕**的顶层窗口。

    返回 [(hwnd, 标题, 进程名, 是否最小化, 扩展样式)]。

    筛选口径（三处都必须满足，缺一个就会误判）：
      ① `IsWindowVisible()` 且未被最小化
      ② `GetWindowRect` 面积 > 0（尺寸为 0 的占位窗口不算）
      ③ **没有** `WS_EX_NOREDIRECTIONBITMAP`（有的话它根本不渲染）

    刻意**不**按「标题为空」或「TOOLWINDOW」过滤：
    Win11 的开始菜单 / 搜索就是 `explorer.exe` 的空标题 TOOLWINDOW ——
    被这两条挡掉过一个版本，结果"开始菜单挡住游戏"完全查不出来。
    """
    u = ctypes.windll.user32
    out = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND,
                            ctypes.wintypes.LPARAM)

    def _cb(hwnd, _l):
        try:
            if not u.IsWindowVisible(hwnd):
                return True
            ex = int(u.GetWindowLongW(hwnd, -20))     # GWL_EXSTYLE
            if ex & EX_NOREDIRECTIONBITMAP:
                return True
            r = ctypes.wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left <= 0 or r.bottom - r.top <= 0:
                return True
            n = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 2)
            u.GetWindowTextW(hwnd, buf, n + 2)
            out.append((int(hwnd), buf.value, _window_proc_name(hwnd),
                        bool(u.IsIconic(hwnd)), ex))
        except Exception:  # noqa: BLE001
            pass
        return True

    u.EnumWindows(CB(_cb), 0)
    return out


def _win_class(hwnd):
    """取窗口类名（小写）。"""
    try:
        u = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(300)
        u.GetClassNameW(hwnd, buf, 300)
        return buf.value.lower()
    except Exception:  # noqa: BLE001
        return ""


def _win_rect(hwnd):
    """取窗口矩形 (x, y, w, h)。失败返回 (0, 0, 0, 0)。"""
    try:
        u = ctypes.windll.user32
        r = ctypes.wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:  # noqa: BLE001
        return 0, 0, 0, 0


# ---------------------------------------------------------------------------
# Shell 浮层：开始菜单 / 搜索 / 任务视图
# ---------------------------------------------------------------------------
# 原神用户反馈（2026-10-05）：从睡眠唤醒后，有时会**卡在 Win 键打开的那个
# 界面**上 —— 游戏收不到模拟键鼠、BetterGI 也识别不到画面，整条链路停摆。
#
# 这台 Win11 实测：开始菜单/搜索**不是独立窗口**，
#   · `StartMenuExperienceHost.exe` 进程压根不在（Win11 新版合并了）
#   · 搜索是 `searchhost.exe` 的 `Windows.UI.Core.CoreWindow`（标题='搜索'）
#   · 开始菜单是 `explorer.exe` 的 `XamlExplorerHostIslandWindow_WASDK`
# 它们带 TOOLWINDOW、标题常为空，用普通的"可见顶层窗口"口径抓不到。
SHELL_FLOAT_PROCS = ("searchhost.exe", "startmenuexperiencehost.exe",
                     "shellexperiencehost.exe")
# 类名前缀匹配
SHELL_FLOAT_CLASSES = ("xamlexplorerhostislandwindow",
                       "windows.ui.core.corewindow")


def find_shell_float(foreground_only=False):
    """找正在挡路的 shell 浮层窗口（开始菜单/搜索/任务视图）。0 = 没有。

    优先看**前台**窗口 —— 只有浮层拿到焦点才会真正抢走游戏的操作。

    foreground_only=True 时**只**查前台（两次 API 调用，几乎零开销），
    供运行中的轮询使用；日常排障用默认值（会全量扫描兜底，
    能发现"浮层开着但焦点在别处"的情况）。
    """
    u = ctypes.windll.user32
    try:
        fg = int(u.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        fg = 0

    def _is_float(hwnd):
        if not hwnd or not u.IsWindowVisible(hwnd):
            return False
        cls = _win_class(hwnd)
        proc = _window_proc_name(hwnd)
        if proc in SHELL_FLOAT_PROCS:
            return True
        if any(cls.startswith(c) for c in SHELL_FLOAT_CLASSES):
            # 只认 shell 自己的；别把普通 UWP 应用算进来
            if proc in ("explorer.exe",) or proc in SHELL_FLOAT_PROCS:
                return True
        return False

    if _is_float(fg):
        return fg
    if foreground_only:
        return 0

    sw = u.GetSystemMetrics(0) or 1920
    sh = u.GetSystemMetrics(1) or 1080
    best, best_area = 0, 0
    for hwnd, _title, _proc, mini, _ex in list_top_windows():
        if mini or not _is_float(hwnd):
            continue
        _x, _y, cw, ch = _win_rect(hwnd)
        area = cw * ch
        if area > best_area and area > (sw * sh) * 0.02:
            best, best_area = hwnd, area
    return best


def shell_float_open(foreground_only=False):
    """当前是否有开始菜单/搜索这类浮层挡在前面。返回 (是否, 说明)。"""
    h = find_shell_float(foreground_only)
    if not h:
        return False, "没有"
    _x, _y, cw, ch = _win_rect(h)
    return True, "%s（%s）%dx%d" % (
        _window_proc_name(h) or "?", _win_class(h) or "?", cw, ch)


def _send_key(vk, emit=None):
    """发一次「按下 + 抬起」（keybd_event）。不需要管理员，发给前台窗口。"""
    try:
        u = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        u.keybd_event(vk, 0, 0, 0)
        time.sleep(0.06)
        u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception as exc:  # noqa: BLE001
        (emit or (lambda m: None))("（发送按键失败：%s）" % exc)
        return False


VK_ESCAPE = 0x1B
VK_LWIN = 0x5B


def dismiss_shell_float(log_cb=None, tries=2):
    """把挡路的开始菜单 / 搜索关掉。返回 (是否已清干净, 说明)。

    实测（2026-10-05）：**ESC 能干净关掉它** ——
    发 Win 键打开搜索、再发 ESC，窗口状态与之前 0 处差异。
    所以顺序是 ESC → ESC → Win 键（Win 是开关键，只在 ESC 无效时才用，
    否则本来没开反而被打开）。
    """
    emit = log_cb or (lambda m: None)
    opened, why = shell_float_open()
    if not opened:
        return True, "未打开"

    emit("检测到 shell 浮层挡在前面：%s" % why)
    for i in range(max(1, tries)):
        emit("第 %d 次尝试：发送 Esc 关闭…" % (i + 1))
        _send_key(VK_ESCAPE, emit)
        time.sleep(0.7)
        if not shell_float_open()[0]:
            emit("已关闭 ✓", "ok")
            return True, "Esc 已关闭"

    emit("Esc 无效，改用 Win 键切换…", "warn")
    _send_key(VK_LWIN, emit)
    time.sleep(0.9)
    if not shell_float_open()[0]:
        emit("已关闭 ✓", "ok")
        return True, "Win 键已关闭"

    still = shell_float_open()[1]
    emit("⚠ 仍然关不掉：%s" % still, "warn")
    return False, "仍未能关闭（%s）" % still


# 要保留不动的进程（游戏本体）。
# **BetterGI 的主界面窗口不保留** —— 用户明确要求"主界面只有原神"，
# BetterGI 那个标题为「更好的原神」的程序窗口也要最小化掉。
KEEP_PROCS = ("yuanshen.exe", "genshinimpact.exe")

# 但「叠在游戏上面」的辅助窗口必须保留：BetterGI 的**遮罩窗口**
# （一条龙跑起来才出现）就是画在游戏上的，最小化它 BGI 会出问题。
# 这类窗口的扩展样式有共同特征，用位掩码识别。
#   TOPMOST | TRANSPARENT | LAYERED | NOACTIVATE | TOOLWINDOW
OVERLAY_EX_MASK = (0x00000008 | 0x00000020 | 0x00080000
                   | 0x08000000 | 0x00000080)
# 只有这些进程的"叠层窗口"才特殊对待（别的程序的悬浮窗该最小化就最小化）
OVERLAY_PROCS = ("bettergi.exe",)

# 这些**类名**的窗口永远不碰（任务栏 / 桌面）。
# 任务栏严格说也"占屏幕"，但它只占底部一条，全屏游戏会盖住它；
# 而把它最小化会让任务栏整条消失 —— 半夜跑完醒来看到"任务栏没了"
# 比"被任务栏挡住的 48 像素"糟糕得多。
KEEP_CLASSES = ("shell_traywnd", "shell_secondarytraywnd",
                "progman", "workerw")


def minimize_other_windows(log_cb=None, dry_run=False,
                           keep_procs=KEEP_PROCS, also_keep_pid=None):
    """清场：先关掉开始菜单/搜索，再把其它窗口最小化，最后把原神拉到前台。

    为什么要关开始菜单：它一旦拿到焦点，游戏就收不到模拟键鼠、
    BetterGI 也截不到正确画面（2026-10-05 用户反馈"睡醒卡在 Win 键
    打开的界面"，就是这么来的）。它属于 shell 浮层，普通窗口口径抓不到。
    为什么要最小化其它窗口：只要别的窗口盖住游戏，BetterGI 就会以为
    「角色切换失败」，然后一直「脱困」出不来（实测空转 806 次、白等 5 小时）。

    返回 (处理了几个, 说明)。dry_run=True 只报告不动手（自检用）。
    """
    emit = log_cb or (lambda m: None)
    u = ctypes.windll.user32

    # 0) 开始菜单 / 搜索 —— 必须最先处理，它是最"毒"的那种遮挡
    if not dry_run:
        dismiss_shell_float(emit)

    keep = tuple(_norm_proc(p) for p in keep_procs)
    overlay = tuple(_norm_proc(p) for p in OVERLAY_PROCS)
    targets = []
    kept_overlay = []
    for hwnd, title, proc, minimized, ex in list_top_windows():
        if minimized:
            continue
        if _win_class(hwnd) in KEEP_CLASSES:
            continue
        np = _norm_proc(proc)
        if np in keep:
            continue
        # BetterGI 的遮罩窗口：画在游戏上面，最小化它会毁掉 BGI 的识别
        if np in overlay and (ex & OVERLAY_EX_MASK):
            kept_overlay.append(title or proc)
            continue
        if also_keep_pid:
            pid = ctypes.wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == also_keep_pid:
                continue
        targets.append((hwnd, title or (proc or "?"), proc))

    if dry_run:
        emit("干跑：以下 %d 个窗口会被最小化：" % len(targets))
        for hwnd, title, proc in targets:
            emit("   · [%s] %s" % (proc, title[:60]))
        if kept_overlay:
            emit("保留（叠在游戏上的辅助窗口）：%s"
                 % "、".join(kept_overlay[:5]))
        return len(targets), "干跑（未做任何改动）"

    SW_MINIMIZE = 6
    done = 0
    for hwnd, title, proc in targets:
        try:
            u.ShowWindow(hwnd, SW_MINIMIZE)
            done += 1
        except Exception:  # noqa: BLE001
            pass

    # 再把原神拉到最前
    gh = find_genshin_hwnd()
    if gh:
        try:
            SW_RESTORE = 9
            u.ShowWindow(gh, SW_RESTORE)
            _force_foreground(gh)
        except Exception:  # noqa: BLE001
            pass
    return done, ("已最小化 %d 个窗口%s"
                  % (done, "，原神已置于最前" if gh else
                     "（原神窗口还没出现，先腾空桌面）"))


def _force_foreground(hwnd):
    """可靠地把窗口置前（后台进程直接 SetForegroundWindow 会失败）。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        fg = u.GetForegroundWindow()
        tid_fg = u.GetWindowThreadProcessId(fg, None) if fg else 0
        tid_me = k.GetCurrentThreadId()
        if tid_fg and tid_fg != tid_me:
            u.AttachThreadInput(tid_me, tid_fg, True)
            u.SetForegroundWindow(hwnd)
            u.AttachThreadInput(tid_me, tid_fg, False)
        else:
            u.SetForegroundWindow(hwnd)
        u.BringWindowToTop(hwnd)
        return True
    except Exception:  # noqa: BLE001
        return False


def _foreground_genshin(emit=None):
    """把原神窗口拉到前台（尽量）。返回是否找到了窗口。"""
    emit = emit or (lambda m: None)
    gh = find_genshin_hwnd()
    if not gh:
        emit("（没找到原神窗口，跳过前置）")
        return False
    try:
        ctypes.windll.user32.ShowWindow(gh, 9)     # SW_RESTORE
        _force_foreground(gh)
        return True
    except Exception as exc:  # noqa: BLE001
        emit("（前置窗口失败：%s）" % exc)
        return False


def wait_onedragon_done(cfg, log_cb=None, timeout_min=None, poll=8):
    """等 BetterGI 的一条龙跑完。返回 (原因, 详情)。

    原因取值：
      done     正常完成（日志出现「一条龙和配置组任务结束」）
      cancel   被取消（「任务被取消，退出执行」/「启动阶段被取消」）
      bgi_exit BGI 进程消失（且此前确实见过一条龙活动）
      no_start 一条龙压根没开始（视为失败，不应接力）
      timeout  超时

    判定同时看**日志**与**进程**：
    - 只看进程会漏掉「BGI 不退出」的情况；
    - 只看日志会漏掉「BGI 崩了但没写终态」的情况。
    """
    emit = log_cb or (lambda m: None)
    exe = cfg.bgi_exe
    if not exe:
        return "no_start", "没有 BetterGI 路径"
    bdir = os.path.dirname(exe)
    limit = int(timeout_min or cfg.i("OneDragonTimeoutMin", 300)) * 60

    # 关键：**从当前文件末尾开始**只读新增内容。
    # 否则会把「今天早先那次运行」留下的完成标志当成这一次的结果 ——
    # 直接假成功、立刻去关原神（本机实测过这个坑）。
    offsets = {}
    for path in bgi_log_candidates(bdir):
        try:
            offsets[path] = os.path.getsize(path)
        except OSError:
            offsets[path] = 0

    # 但"有没有开跑过"要回溯看一眼尾部（BGI 可能比我们早一步写日志）
    saw_activity = False
    progress = ""
    try:
        today = bgi_log_candidates(bdir)[0]
        if os.path.isfile(today):
            code, tail = read_tail(today, 120)
            if re.search(r"一条龙任务执行|启用一条龙配置", tail or ""):
                saw_activity = True
                emit("（日志尾部已有一条龙活动记录）")
                # 顺手把最后一条进度也取出来 —— 卡死判定要靠它显示
                for line in (tail or "").splitlines():
                    m = re.search(r"一条龙任务执行:\s*(\S+)", line)
                    if m:
                        progress = m.group(1)
                if progress:
                    emit("（从尾部读到当前进度 %s）" % progress)
    except Exception:  # noqa: BLE001
        pass

    gone_streak = 0
    t0 = time.time()
    next_report = 0

    # ---- 卡死检测 ----
    # 2026-10-05 凌晨实测：BetterGI 的自动秘境卡住，日志里
    # 「切换角色卡住，执行脱困」刷了 **806 次**、进度停在 2/6 整整 5 小时，
    # 我这边的等待循环毫不知情，一直等到 300 分钟上限才放弃。
    # 这里用「脱困反复出现 + 进度不动」两个条件一起判定，
    # 单看任何一个都会误判（有些战斗本来就会脱困几次）。
    stuck_hits = 0
    last_stuck_at = 0.0
    last_prog = progress
    prog_since = time.time()
    stuck_limit = max(20, cfg.i("OneDragonStuckHits", 40))
    stuck_grace = max(60, int(cfg.i("OneDragonStuckQuietMin", 6)) * 60)
    recovered = False
    desktop_cleaned = False
    float_guard = str(cfg.get("DismissShellFloat", "1")).strip() in (
        "1", "true", "True", "yes")
    STUCK_PAT = re.compile(r"脱困|切换角色卡住")

    while time.time() - t0 < limit:
        # --- 读日志 ---
        for path in bgi_log_candidates(bdir):
            off, txt = _read_from(path, offsets.get(path, 0))
            offsets[path] = off
            if not txt:
                continue
            for line in txt.splitlines():
                s = line.strip()
                if not s:
                    continue
                if STUCK_PAT.search(s):
                    stuck_hits += 1
                    last_stuck_at = time.time()
                if "一条龙任务执行" in s or "启用一条龙配置" in s:
                    saw_activity = True
                    m = re.search(r"一条龙任务执行:\s*(\S+)", s)
                    if m:
                        progress = m.group(1)
                if ONEDRAGON_DONE in s:
                    emit("检测到一条龙完成标志：%s" % s[:80])
                    return "done", progress or "已结束"
                if ONEDRAGON_CANCEL in s or ONEDRAGON_CANCEL2 in s:
                    emit("检测到一条龙被取消：%s" % s[:80])
                    return "cancel", progress or "被取消"

        # 进度变了就重置计时（正常推进不算卡）
        if progress != last_prog:
            last_prog = progress
            prog_since = time.time()

        # 每轮扫一眼：有没有开始菜单/搜索抢走了焦点。
        # 用户实测反馈：睡眠唤醒后偶尔会卡在 Win 键打开的界面上，
        # 这时游戏收不到模拟键鼠、BetterGI 也识别不到画面。
        # 只看前台（两次 API 调用），所以每 8 秒做一次也不心疼。
        if float_guard and shell_float_open(foreground_only=True)[0]:
            emit("⚠ 检测到开始菜单/搜索层挡住了游戏，正在按 Esc 关闭…",
                 "warn")
            okf, whyf = dismiss_shell_float(emit)
            if okf:
                _foreground_genshin(emit)

        # 原神窗口一出现，再清一次桌面（游戏刚开时常有别的窗口压在上面）
        if not desktop_cleaned and find_genshin_hwnd():
            desktop_cleaned = True
            if str(cfg.get("MinimizeOthers")).strip() in ("1", "true",
                                                          "True", "yes"):
                n, m = minimize_other_windows(emit)
                emit("原神已打开 → 清理桌面：%s" % m)
            else:
                _foreground_genshin(emit)

        # 判定卡死：脱困刷得够多 + 进度够久没动 + 现在还在刷
        now = time.time()
        if (stuck_hits >= stuck_limit
                and now - prog_since >= stuck_grace
                and now - last_stuck_at <= 120):
            if not recovered:
                # 卡住的第一反应：**清桌面 + 把游戏拉到最前**。
                # 「切角色卡住」多半是游戏窗口丢了焦点或被挡住，
                # 清一次桌面往往就能让 BetterGI 重新动起来。
                recovered = True
                emit("⚠ 检测到卡住（进度 %s，脱困已刷 %d 次）。"
                     "尝试清空桌面并把原神置前…" % (progress or "?", stuck_hits))
                n, m = minimize_other_windows(emit)
                emit("恢复动作：%s" % m)
                if find_genshin_hwnd():
                    emit("再观察 3 分钟…")
                    prog_since = now
                    stuck_hits = 0
                    time.sleep(180)
                    continue
            emit("判定一条龙卡死：进度停在 %s，脱困循环 %d 次，"
                 "已堵 %.0f 分钟。" % (progress or "?", stuck_hits,
                                       (now - prog_since) / 60.0))
            return "stuck", (progress or "无进度") + "（卡死）"

        # --- 看进程 ---
        if proc_alive(BGI_PROC):
            gone_streak = 0
        else:
            gone_streak += 1
            if gone_streak >= 2:      # 连续两次不在，确认退出
                if saw_activity:
                    emit("BetterGI 进程已退出（此前已完成到 %s）"
                         % (progress or "未知"))
                    return "bgi_exit", progress or "BGI 已退出"
                emit("BetterGI 进程已退出，且没有看到一条龙活动。")
                return "no_start", "BGI 未成功开跑"

        # --- 定期报进度 ---
        now = time.time()
        if now >= next_report:
            next_report = now + 60
            mins = int((now - t0) // 60)
            emit("等待一条龙结束… 已等 %d 分钟，进度 %s"
                 % (mins, progress or "（尚未开始）"))

        time.sleep(poll)

    emit("等待一条龙超时（上限 %d 分钟），进度 %s"
         % (limit // 60, progress or "未知"))
    return "timeout", progress or "超时"


def okww_spec(cfg):
    """构造 ok-ww 的运行命令。返回 (argv, cwd, 说明)。

    ok-ww 由 pyappify 包装：**`ok-ww.exe` 只是个启动器**（Tauri 窗口，
    上面有「启动应用」按钮和「自动启动」开关），它自己**不转发命令行参数**。
    点「启动应用」的本质就是 `pythonw main.py`（不带参数 → 进 GUI）。
    所以要让它**自动跑指定任务**，必须用它自带的 python 直接跑 main.py。

    ★ 默认**带界面**（不加 `--headless`）：
        python.exe main.py -t DailyTask -e
    本机实测（2026-10-05 12:09）：这样会正常弹出 ok-ww 主界面
    （窗口标题 `OK-WW v3.7.3   - OK-WW`，Qt），日志写
    `MainWindow:Window has fully displayed`
    + `ok:start runtime with task name DailyTask`
    —— **既看得到 ok-ww 程序界面，又自动跑任务**。
    之前写死 `--headless` 会导致完全无界面，用户以为"ok-ww 根本没启动"
    （2026-10-05 12:01 用户反馈"启动的根本不是 exe 程序"）。

    `OkwwGuiMode=0` 时退回无界面模式（更省资源、不怕弹窗打断）。

    注意 **不要用 `-h`**：那是 logger 模块里另一个 argparse 的 `--help`，
    会被它截走并直接打印帮助退出。无界面必须写长参数 `--headless`。
    """
    d = cfg.okww_dir
    if not d:
        return None, None, "没找到 ok-ww（目录下应有 ok-ww.exe）"
    base = os.path.join(d, "data", "apps", "ok-ww")
    py = os.path.join(base, "python", "python.exe")
    work = os.path.join(base, "working")
    if not os.path.isfile(py):
        return None, None, "找不到 ok-ww 自带的 python：%s" % py
    if not os.path.isfile(os.path.join(work, "main.py")):
        return None, None, "找不到 ok-ww 的 main.py：%s" % work
    gui = True
    try:
        gui = str(cfg.get("OkwwGuiMode", "1")).strip().lower() in (
            "1", "true", "yes")
    except Exception:  # noqa: BLE001
        # 自检用的临时对象（只有属性、没有 get()）→ 用默认值
        gui = True
    argv = [py, "main.py", "-t", cfg.okww_task, "-e"]
    if not gui:
        argv.append("--headless")
    return argv, work, ""


def run_okww(cfg, log_cb=None, wait_min=None):
    """启动 ok-ww 跑鸣潮日常。返回 (成功, 说明)。"""
    emit = log_cb or (lambda m: None)
    argv, cwd, why = okww_spec(cfg)
    if not argv:
        emit("✗ %s" % why)
        return False, why

    # ★ ok-ww 的 StartController.start_device() 里有这么一段（v3.7.3 第 183 行）：
    #     if device['device'] == "windows" and not is_admin():
    #         communicate.restart_admin.emit(); return False
    #   —— 非管理员时**直接放弃，而且一行日志都不写**（它走的是 GUI 信号
    #   通道，headless 下没人接收）。外部只能看到一句 "Start task failed"，
    #   完全猜不出真因。本机 2026-10-05 02:15 就是这样卡住的：
    #   GUI 双击打开 → 非管理员 → 子进程 ok-ww 也非管理员 → 静默失败。
    #   所以这里提前拦住，把真因直接讲明白。
    if not is_admin():
        msg = ("需要管理员权限：ok-ww 启动 PC 版鸣潮时会检查管理员身份，"
               "非管理员会静默放弃（连日志都不写，只报 Start task failed）。"
               "请以管理员身份运行本程序后再试。")
        emit("✗ " + msg)
        return False, msg

    logf = os.path.join(user_data_dir(), "okww_last_run.log")
    gui = "--headless" not in argv
    emit("启动 ok-ww：%s" % " ".join(argv))
    emit("工作目录：%s" % cwd)
    emit("（ok-ww.exe 只是启动器，实际跑的是它内部的 main.py %s）"
         % ("带界面" if gui else "无界面"))
    env = dict(os.environ)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        fh = open(logf, "w", encoding="utf-8", errors="replace")
    except OSError:
        fh = None
    try:
        # ★ 进程创建标志按模式区分：
        #   无界面：必须 CREATE_NO_WINDOW —— ok-ww 是控制台程序，
        #     而本程序是无控制台的窗口程序（PyInstaller windowed）。
        #     不给这个标志时 Windows 会替它**新开一个黑色控制台窗口**，
        #     那个窗口一旦被关掉，ok-ww（以及挂在同一控制台上的进程，
        #     比如它刚拉起的游戏）会一起收到 CTRL_CLOSE_EVENT 被杀，
        #     退出码 0xC000013A，日志戛然而止（2026-10-05 02:46 实测）。
        #   带界面：用 DETACHED_PROCESS —— 让它脱离本进程的进程组独立存活
        #     （实测这样 Qt 主窗口能正常显示、且脚本退出后不会被带走）。
        if "--headless" in argv:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(argv, cwd=cwd, env=env,
                                stdout=fh or subprocess.DEVNULL,
                                stderr=subprocess.STDOUT,
                                creationflags=flags)
    except Exception as exc:  # noqa: BLE001
        emit("✗ 启动 ok-ww 失败：%s" % exc)
        return False, "启动失败：%s" % exc
    emit("ok-ww 已启动（PID %d），输出写到 %s" % (proc.pid, logf))

    limit = int(wait_min or cfg.i("OkwwTimeoutMin", 120)) * 60
    t0 = time.time()
    # ---- 鸣潮崩溃 / ok-ww 卡死检测 ----
    # 2026-10-05 实测：鸣潮在 ok-ww 开始操作后不久自己崩掉
    # （崩溃目录 `...\CrashReportClient\UE4CC-Windows-*` 会新增一条），
    # 留下一个失效的窗口句柄，ok-ww 于是**每 80 秒刷一次
    # 「PostMessage error ... 无效的窗口句柄」，却永远不退出**。
    # 这边如果不盯，就会一路白等到 OkwwTimeoutMin（默认 120 分钟）。
    saw_wuwa = bool(proc_alive(WUWA_PROC))
    gone_streak = 0
    while time.time() - t0 < limit:
        if proc.poll() is not None:
            emit("ok-ww 已结束，返回码 %s" % proc.returncode)
            if fh:
                fh.close()
            ok, msg = _okww_verdict(logf, proc.returncode)
            emit(msg)
            return ok, msg

        # 游戏进程盯着（ok-ww 会自己拉起它，所以先等到它出现再开始算）
        if proc_alive(WUWA_PROC):
            saw_wuwa = True
            gone_streak = 0
        elif saw_wuwa:
            gone_streak += 1
            if gone_streak == 6:            # 约 30 秒都没回来
                emit("⚠ 鸣潮进程消失且 30 秒未恢复。检查 ok-ww 输出…",
                     "warn")
                _c, tail = read_tail(logf, 60)
                tail = tail or ""
                crashed = ("PostMessage" in tail
                           or "无效的窗口句柄" in tail
                           or "BitBlt failed" in tail)
                if crashed:
                    emit("ok-ww 正在反复报「无效的窗口句柄 / BitBlt failed」——"
                         "游戏已崩溃或退出，它却卡住不退出。", "warn")
                    emit("判定为卡死：现在终止 ok-ww，不再空等 "
                         "%d 分钟。" % (limit // 60))
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                    if fh:
                        fh.close()
                    return False, ("鸣潮崩溃后 ok-ww 卡死（已主动终止）。"
                                   "详见 %s" % logf)
                if gone_streak >= 24:       # 再给 2 分钟宽限
                    emit("鸣潮消失已 2 分钟且无明确错误，仍判定卡死，"
                         "终止 ok-ww。", "warn")
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                    if fh:
                        fh.close()
                    return False, "鸣潮进程消失，ok-ww 无响应（已终止）"
        time.sleep(5)
    emit("ok-ww 已跑满 %d 分钟仍未结束 —— 不再等待（它会在后台继续）。"
         % (limit // 60))
    if fh:
        fh.close()
    return True, "已启动，等待超时但仍在运行"


def _okww_verdict(logf, rc):
    """从 ok-ww 输出里判断结果。返回 (成功, 说明)。"""
    txt = ""
    try:
        if logf and os.path.isfile(logf):
            txt = open(logf, encoding="utf-8", errors="replace").read()
    except OSError:
        pass
    tail = [l for l in txt.splitlines() if l.strip()][-25:]
    if "Start task failed" in txt:
        # 分辨真因 —— 别再一律说成「鸣潮没起来/没登录」。
        # ok-ww 的 start_device() 有两个分支都是**静默**的（只发 GUI 信号，
        # headless 下没人接），必须靠日志特征反推。
        if "starting game" not in txt:
            if not is_admin():
                return False, (
                    "ok-ww 没能启动鸣潮：**它需要管理员权限**。\n"
                    "真因：ok-ww 的启动流程检测到自己不是管理员就直接放弃，"
                    "而且不写日志（所以日志里看不到 'starting game'）。\n"
                    "解决：以管理员身份运行本程序，或让计划任务来跑"
                    "（计划任务已是最高权限）。\n详见 %s" % logf)
            return False, (
                "ok-ww 没能启动鸣潮（日志里没有 'starting game' 这一步，"
                "说明它在启动游戏之前就返回了）。\n"
                "可能原因：① 找不到游戏主程序；② 设备/窗口识别失败。\n"
                "详见 %s" % logf)
        return False, (
            "ok-ww 已经开始启动鸣潮，但任务启动失败了。\n"
            "常见原因：游戏启动太慢、停在登录/选择角色界面、分辨率不符。\n"
            "详见 %s" % logf)
    if "Task not found" in txt:
        return False, ("ok-ww 说找不到这个任务（请到「接力鸣潮」页核对任务名）。"
                       "详见 %s" % logf)

    # ★ 游戏中途消失 —— 这是 2026-10-05 实测遇到的真实现象：
    #   ok-ww 已经连上窗口、图像识别成功、点在游戏里操作了，
    #   然后窗口 `exists:0`、进程没了。
    #   日志里找不到任何 Python 异常，因为不是 ok-ww 的问题。
    #   必须把这个特征认出来，否则用户只会看到一句没头没脑的返回码。
    if ("starting game" in txt and "exists:0" in txt
            and "hwnd_window:do_update_window_size changed,visible:False,"
                "exists:0" in txt):
        return False, (
            "**鸣潮中途退出了**（不是 ok-ww 的问题）。\n"
            "ok-ww 已经连上游戏窗口并在里面正常操作过，之后游戏窗口"
            "忽然消失（日志里的 exists:0）。\n"
            "常见原因：\n"
            "  ① 游戏文件损坏 → 打开鸣潮启动器做一次「校验/修复文件」；\n"
            "  ② 游戏被强制改了分辨率（本程序默认让 ok-ww 自动调整窗口大小）\n"
            "     —— 建议在游戏里手动设成 1920x1080，并关掉 ok-ww 的\n"
            "     Auto Resize Game Window；\n"
            "  ③ 反作弊对模拟输入有反应。\n"
            "崩溃报告目录（有新的说明是游戏崩了）：\n"
            "  C:\\Wuthering Waves\\Wuthering Waves Game\\Client\\Saved"
            "\\Config\\CrashReportClient\n详见 %s" % logf)

    if rc == 0:
        return True, "ok-ww 正常结束（返回码 0）"
    return False, ("ok-ww 以返回码 %s 结束，尾部日志：\n%s"
                   % (rc, "\n".join(tail)))


def chain_to_wuwa(cfg, log_cb=None):
    """一条龙跑完后：关 BGI/原神 → 把鸣潮日常交给 ok-ww。

    **不在这里打开鸣潮** —— 鸣潮由 ok-ww 自己拉起：
    它的 `StartController.start_device()` 发现游戏没连上时会
    `starting game <path>` 并等窗口就绪，这条路径才是它自己的正常流程。
    从外面先开游戏反而会打乱它的设备识别（本机踩过）。

    返回 (成功, 说明)。任何一步失败都只记录并继续尝试下一步 ——
    好不容易把原神挂完，不该因为某个小环节直接放弃。
    """
    emit = log_cb or (lambda m: None)
    emit("")
    emit("==== 接力鸣潮（Wuthering Waves）====")

    # 0) 权限闸门 —— 必须放在**一切动作之前**。
    #    ok-ww 要有管理员权限才能启动 PC 版鸣潮（它内部 `is_admin()` 检查，
    #    不满足就静默放弃）。如果先关了原神才发现没权限，那是纯白折腾，
    #    而且用户第二天早上会看到"原神被关了、鸣潮没跑"。
    if not is_admin():
        msg = ("接力需要管理员权限：ok-ww 启动 PC 版鸣潮时会检查管理员身份，"
               "非管理员会静默放弃。请以管理员身份运行本程序"
               "（或让计划任务来跑，它已是最高权限）。")
        emit("✗ %s" % msg)
        emit("（已在动手关游戏之前中止，没有影响任何正在运行的程序。）")
        return False, msg

    # 1) 关 BetterGI（它通常自己会退，给它一点时间）
    if proc_alive(BGI_PROC):
        emit("BetterGI 仍在运行，等待它自行退出…")
        for _ in range(15):
            time.sleep(1)
            if not proc_alive(BGI_PROC):
                break
    if proc_alive(BGI_PROC):
        ok, msg = kill_proc(BGI_PROC, emit)
        emit("关闭 BetterGI：%s" % msg)
    else:
        emit("BetterGI 已自行退出。")

    # 2) 关原神（腾出显卡/内存给鸣潮）
    if cfg.kill_genshin:
        if proc_alive(GENSHIN_PROC):
            ok, msg = kill_proc(GENSHIN_PROC, emit)
            emit("关闭原神：%s" % msg)
            if not ok:
                emit("⚠ 原神没关掉，可能会和鸣潮抢显卡/内存。")
        else:
            emit("原神本来就没在运行。")
        gap = max(0, cfg.i("WuwaGapSec", 20))
        if gap:
            emit("等待 %d 秒，让显卡/内存释放干净…" % gap)
            time.sleep(gap)
    else:
        emit("按设置保留原神不关闭。")

    # 3) 检查 ok-ww 到时候能不能自己把鸣潮拉起来
    game, src = okww_game_exe(cfg)
    if game:
        emit("ok-ww 将自行启动鸣潮：%s（%s）" % (game, src))
    else:
        emit("⚠ 定位不到 ok-ww 要启动的鸣潮主程序（%s）。" % src)
        emit("  它很可能报「Game path does not exist」而开不了游戏。"
             "请到「接力鸣潮」页点「修正 ok-ww 游戏路径」。")

    # 4) 交给 ok-ww（它会开游戏 → 跑日常 → 结束后退出）
    #    如果鸣潮本来就在跑：它此前可能被"清空桌面"最小化了，
    #    ok-ww 拿最小化的窗口会抓瞎 —— 先还原出来。
    #    顺便把开始菜单/搜索清掉：ok-ww 同样靠截屏+模拟键鼠，
    #    前面浮层挡着它一样动不了（原神的清场管不到这一程）。
    if str(cfg.get("DismissShellFloat", "1")).strip() in ("1", "true",
                                                          "True", "yes"):
        fl, why = shell_float_open()
        if fl:
            emit("交 ok-ww 之前发现 shell 浮层：%s" % why)
            dismiss_shell_float(emit)
    hw = find_window_by_proc(WUWA_PROC)
    if hw:
        emit("鸣潮本来就在运行，先把它从最小化还原…")
        restore_window(hw, foreground=True)
    ok, msg = run_okww(cfg, emit)
    emit("接力结果：%s" % msg)
    return ok, msg


def _read_from_end(path, n=40):
    try:
        code, txt = read_tail(path, n)
        return txt
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# 多游戏阶段调度（顺序可自定义）
# ---------------------------------------------------------------------------
# 这些进程名太通用 —— **绝不**按名字批量结束，否则会误杀用户别的程序
# （python 上可能跑着他的爬虫、node 上可能跑着开发服务器）。
NEVER_KILL_PROCS = ("python", "pythonw", "java", "node", "cmd", "conhost")

# 各工具常见的安装位置（自动探测用，找不到不报错）
TOOL_SEARCH_DIRS = {
    "starrail": ("March7thAssistant", "三月七小助手", "March7th"),
    "zzz": ("ZZZ-OneDragon", "ZenlessZoneZero-OneDragon", "绝区零一条龙",
            "zzz-od"),
}


def stage_order(cfg):
    """本次要跑的阶段 id 列表（有序，只含已启用的）。

    配置 `StageOrder` 用逗号分隔，例如 `genshin,wuwa,starrail,zzz`。
    没配置过时从旧配置推导，保证老用户升级后行为不变：
        RunOkww=1 → ["genshin", "wuwa"]；否则 ["genshin"]
    """
    raw = str(cfg.get("StageOrder") or "").strip()
    ids = [x.strip().lower() for x in raw.split(",") if x.strip()]
    ids = [i for i in ids if i in STAGE_MAP]
    if not ids:
        try:
            ids = ["genshin", "wuwa"] if cfg.run_okww else ["genshin"]
        except Exception:  # noqa: BLE001
            ids = ["genshin", "wuwa"]
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def set_stage_order(cfg, ids):
    """写入阶段顺序（同时表达「启用哪些」和「谁先谁后」）。"""
    clean = [i for i in ids if i in STAGE_MAP]
    cfg.set("StageOrder", ",".join(clean))
    return cfg.save()


def stage_enabled(cfg, sid):
    return sid in stage_order(cfg)


def _scan_for_tool(names, roots=None, max_depth=3):
    """在常见根目录下按目录名找工具（只往下 max_depth 层，避免全盘扫描慢）。"""
    if roots is None:
        roots = []
        for drive in ("C", "D", "E", "F"):
            d = drive + ":\\"
            if os.path.isdir(d):
                roots.append(d)
    low = tuple(n.lower() for n in names)
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for dirpath, dirnames, _files in os.walk(root):
                depth = dirpath[len(root):].count(os.sep)
                if depth >= max_depth:
                    dirnames[:] = []
                base = os.path.basename(dirpath).lower()
                if any(k in base for k in low):
                    return dirpath
        except OSError:
            continue
    return ""


def detect_tool_dir(sid, hint="", deep=False):
    """自动找某个游戏工具的安装目录。找不到返回空串。

    deep=False（默认）只做**低成本**探测：配置路径 + 正在运行的进程。
    deep=True 才去扫常见目录（几十秒都有可能），只有点「自动检测全部」
    时才会用到 —— 每次启动都扫盘会把体验拖垮。
    """
    stage = STAGE_MAP.get(sid)
    if not stage:
        return ""
    # 1) 已有配置 / 传入的提示
    for cand in (hint, str(cfg_get_cached(sid) or "")):
        if cand and os.path.isdir(cand):
            for n in stage["exe_names"]:
                if os.path.isfile(os.path.join(cand, n)):
                    return cand
    # 2) 从正在运行的进程反查（工具正在跑时最准，且几乎零成本）
    for proc in stage["procs"]:
        if proc.lower() in NEVER_KILL_PROCS:
            continue
        path = _proc_path(proc)
        if path:
            d = os.path.dirname(path)
            for n in stage["exe_names"]:
                if os.path.isfile(os.path.join(d, n)):
                    return d
            return d
    # 3) 按名字扫常见目录（慢，需要显式允许）
    names = TOOL_SEARCH_DIRS.get(sid)
    if deep and names:
        hit = _scan_for_tool(names)
        if hit:
            return hit
    return ""


_CACHED_CFG = {}


def cfg_get_cached(sid):
    """detect_tool_dir 内部用：拿当前 cfg 里该阶段的目录（避免传参绕一圈）。"""
    return _CACHED_CFG.get(sid, "")


def _proc_path(name):
    """取某个进程的可执行文件完整路径（多个取第一个）。"""
    n = name.replace(".exe", "")
    code, out = ps(
        "(Get-Process -Name '%s' -ErrorAction SilentlyContinue | "
        "Select-Object -First 1).Path" % n, timeout=60)
    return (out or "").strip().splitlines()[0].strip() if out else ""


def stage_tool_path(cfg, stage, deep=False):
    """该阶段要启动的程序绝对路径。找不到返回空串（并把探测结果写回配置）。"""
    sid = stage["id"]
    # 原神 / 鸣潮有自己的专用探测（会自己回写配置），直接复用，
    # 别绕过它们 —— 否则用户已经配好的路径反而可能被忽略。
    try:
        if sid == "genshin":
            p = cfg.bgi_exe
            if p and os.path.isfile(p):
                return p
        elif sid == "wuwa":
            d = cfg.okww_dir
            if d and os.path.isfile(os.path.join(d, "ok-ww.exe")):
                return os.path.join(d, "ok-ww.exe")
    except Exception:  # noqa: BLE001
        pass

    d = str(cfg.get(stage["dir_key"]) or "").strip()
    if d and os.path.isdir(d):
        for n in stage["exe_names"]:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    _CACHED_CFG[sid] = d
    found = detect_tool_dir(sid, hint=d, deep=deep)
    _CACHED_CFG.pop(sid, None)
    if found:
        for n in stage["exe_names"]:
            p = os.path.join(found, n)
            if os.path.isfile(p):
                if d != found:
                    cfg.set(stage["dir_key"], found)
                    cfg.save()
                return p
    return ""


def stage_argv(cfg, stage, exe):
    """构造该阶段的启动命令行。"""
    sid = stage["id"]
    if sid == "starrail":
        task = str(cfg.get(stage.get("task_key", "M7Task"))
                   or stage["default_task"]).strip() or "main"
        return [exe, task, "-e"]
    if sid == "zzz":
        raw = str(cfg.get(stage.get("args_key", "ZzzArgs"))
                  or stage["default_args"]).strip()
        return [exe] + (raw.split() if raw else [])
    return [exe]


def run_external_stage(cfg, stage, emit):
    """通用阶段：启动外部一条龙工具 → 等它跑完。返回 (ok, msg)。

    完成判定借鉴 OneDragon 官方「千机链」的做法：
    **工具/游戏进程出现过、并随后全部消失** 才算跑完。
    这样不管是什么工具、用什么方式跑，判定口径都一致。
    """
    exe = stage_tool_path(cfg, stage)
    if not exe:
        msg = ("没找到 %s（%s）。\n下载：%s\n安装提示：%s"
               % (stage["tool"], stage["label"], stage["home"],
                  stage["howto"]))
        emit("✗ " + msg)
        return False, "未安装 %s" % stage["tool"]
    argv = stage_argv(cfg, stage, exe)
    emit("启动：%s" % " ".join('"%s"' % a if " " in a else a for a in argv))
    if not is_admin():
        emit("提示：多数一条龙工具需要管理员权限，当前不是管理员，"
             "可能会失败。")
    try:
        subprocess.Popen(argv, cwd=os.path.dirname(exe),
                         creationflags=getattr(
                             subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception as exc:  # noqa: BLE001
        emit("✗ 启动失败：%s" % exc)
        return False, "启动失败：%s" % exc

    boot = max(30, cfg.i("StageBootSec", 180))
    appear = False
    t0 = time.time()
    while time.time() - t0 < boot:
        time.sleep(2)
        if proc_alive(list(stage["procs"])) or \
                proc_alive(list(stage["game_procs"])):
            appear = True
            emit("进程已出现（%d 秒）" % int(time.time() - t0))
            break
        if int(time.time() - t0) % 20 < 2:
            emit("等待它启动…（已等 %d 秒）" % int(time.time() - t0))
    if not appear:
        emit("⚠ 等了 %d 秒没看到相关进程，可能没起来；仍然继续等一会。"
             % boot)

    limit = max(5, cfg.i("StageTimeoutMin", 180)) * 60
    t1 = time.time()
    last_beat = 0
    while time.time() - t1 < limit:
        time.sleep(5)
        alive_t = proc_alive(list(stage["procs"]))
        alive_g = proc_alive(list(stage["game_procs"]))
        if appear and not alive_t and not alive_g:
            cost = int(time.time() - t1)
            emit("进程已全部退出（用时 %d 分 %d 秒）—— 判定为跑完。"
                 % (cost // 60, cost % 60))
            return True, "已完成"
        if time.time() - last_beat > 120:
            last_beat = time.time()
            emit("仍在运行…（已等 %d 分钟）%s"
                 % (int((time.time() - t1) / 60),
                    "工具在跑" if alive_t else "工具已退出"))
    emit("⚠ 超过上限 %d 分钟仍未结束，不再等它。" % (limit // 60))
    return True, "超时（仍在运行）"


def cleanup_stage(cfg, stage, emit, gap=None):
    """跑完一个阶段后清理：关掉它的工具进程和游戏进程。

    为什么必须清理：下一个游戏要抢显卡和内存，上一个不退会一起卡
    （千机链也是这么做的：组间清理进程 + 等 10 秒再进下一组）。
    """
    for names, what in ((list(stage["procs"]), "工具"),
                        (list(stage["game_procs"]), "游戏")):
        safe = [n for n in names
                if n.replace(".exe", "").lower() not in NEVER_KILL_PROCS]
        if not safe:
            continue
        if proc_alive(safe):
            _ok, msg = kill_proc(safe, emit)
            emit("关闭%s进程：%s" % (what, msg))
    gap = max(0, cfg.i("WuwaGapSec", 20)) if gap is None else gap
    if gap:
        emit("等待 %d 秒，让显卡/内存释放干净…" % gap)
        time.sleep(gap)


def stage_snapshot(cfg):
    """给界面/自检用：每个阶段的状态一览。

    返回 [{"id","label","tool","enabled","order","path","ready"}, ...]
    """
    order = stage_order(cfg)
    out = []
    for s in STAGES:
        sid = s["id"]
        d = str(cfg.get(s["dir_key"]) or "").strip()
        exe = ""
        if d and os.path.isdir(d):
            for n in s["exe_names"]:
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    exe = p
                    break
        out.append({
            "id": sid,
            "label": s["label"],
            "tool": s["tool"],
            "enabled": sid in order,
            "order": order.index(sid) + 1 if sid in order else 0,
            "path": exe,
            "dir": d,
            "ready": bool(exe),
            "home": s["home"],
        })
    return out


def run_stage(sid, cfg, emit):
    """执行一个游戏阶段。返回 (成功, 说明)。"""
    stage = STAGE_MAP.get(sid)
    if not stage:
        return False, "未知阶段 %s" % sid
    if sid == "genshin":
        return _stage_genshin(cfg, emit)
    if sid == "wuwa":
        return _stage_wuwa(cfg, emit)
    return run_external_stage(cfg, stage, emit)


def _stage_genshin(cfg, emit):
    """原神阶段：启动 BetterGI 一条龙，等它跑完。

    注意：这里**不再顺带接力鸣潮** —— 顺序由 `StageOrder` 决定，
    跑完自然会有下一个阶段，中间由 `cleanup_stage()` 腾资源。
    """
    ok, msg = start_onedragon(cfg, emit, skip_window=False)
    emit("启动结果：%s" % msg)
    if not ok:
        return False, msg
    emit("一条龙已启动，开始等它跑完…（上限 %d 分钟）"
         % cfg.i("OneDragonTimeoutMin", 300))
    reason, detail = wait_onedragon_done(cfg, emit)
    emit("一条龙结束判定：%s（%s）" % (reason, detail))
    if reason == "no_start":
        return False, "BetterGI 没有真正开跑一条龙"
    if reason == "stuck":
        return False, "一条龙卡死（%s）" % detail
    if reason == "cancel":
        return True, "一条龙被取消但已结束"
    if reason == "timeout":
        return True, "等待超时（仍算结束，继续下一阶段）"
    return True, "已完成"


def _stage_wuwa(cfg, emit):
    """鸣潮阶段：交给 ok-ww（它会自己启动鸣潮并跑日常）。"""
    # ★ 权限闸门放最前面：**没权限就绝不去关任何游戏进程**，
    #   否则早上起来会看到"游戏被关了、鸣潮也没跑"。
    if not is_admin():
        msg = ("需要管理员权限：ok-ww 启动 PC 版鸣潮时会检查管理员身份，"
               "非管理员会静默放弃（连日志都不写，只报 Start task failed）。"
               "请以管理员身份运行本程序后再试。")
        emit("✗ " + msg)
        return False, msg

    try:
        game, src = okww_game_exe(cfg)
        if game:
            emit("ok-ww 将自行启动鸣潮：%s（按 %s 推导）" % (game, src))
        else:
            emit("⚠ ok-ww 记录的游戏路径不可用，它很可能报 "
                 "「Game path does not exist」而开不了游戏。"
                 "请到「接力鸣潮」页点「修正 ok-ww 游戏路径」。")
    except Exception as exc:  # noqa: BLE001
        emit("（检查 ok-ww 游戏路径时出错：%s）" % exc)

    if str(cfg.get("DismissShellFloat", "1")).strip() in ("1", "true",
                                                          "True", "yes"):
        fl, why = shell_float_open()
        if fl:
            emit("发现 shell 浮层挡住：%s" % why)
            dismiss_shell_float(emit)

    hw = find_window_by_proc(WUWA_PROC)
    if hw:
        emit("鸣潮本来就在运行，先把它从最小化还原…")
        restore_window(hw, foreground=True)

    return run_okww(cfg, emit)


# ---------------------------------------------------------------------------
# 每日计划任务入口（无 GUI 模式）
# ---------------------------------------------------------------------------

def daily_mode(cfg, record_wake=False):
    """每日计划任务入口（无 GUI）。"""
    lines = []

    def emit(msg, kind="info", progress=None):
        """日志回调。

        必须**容忍多余的 kind / progress 参数**：GUI 那边是
        `emit(msg, kind, progress)`，如果这里只写 `def emit(msg)`，
        任何一处 `emit("...", "warn")` 都会在凌晨计划任务里抛
        TypeError 把整条链路打断（而且是无界面运行，报错没人看得见）。
        """
        lines.append(msg)
        auto_log(cfg, msg)

    emit("==== 每日计划任务触发 ====")
    wake_src = ""
    if record_wake:
        wake_src = last_wake_info()
        emit("[唤醒取证] lastwake:\n%s" % wake_src)
    emit("当前时间 %s，任务设定 %s（随机上限 %d 分钟）"
         % (datetime.now().strftime("%H:%M:%S"), cfg.task_time,
            cfg.random_minutes))

    # 不管后面成功与否，先把**明天的**触发时刻随机化。
    # 放在最前面是有意的：这一步很快，而且即使稍后一条龙失败，
    # 明天的唤醒时间也已经换过了（否则天天卡在同一个秒）。
    try:
        ok_rs, msg_rs = reschedule_task(cfg, log_cb=emit)
        if not ok_rs:
            emit("提示：本次未能随机下次时间（%s），明天会沿用旧时刻。"
                 % msg_rs)
    except Exception as exc:  # noqa: BLE001
        emit("提示：随机下次时间时出错（%s）" % exc)

    # 先确认 BetterGI 路径可用（无 GUI 环境必须自给自足）
    exe = cfg.bgi_exe
    if not exe or not os.path.isfile(exe):
        emit("未配置 BetterGI 路径，正在自动搜索…")
        exe = cfg.bgi_exe            # 属性内部会探测并回写
        if exe:
            emit("已自动找到：%s" % exe)
        else:
            emit("仍然找不到 BetterGI.exe。请打开主界面点「自动检测」"
                 "并保存，或手动填写安装目录。")
            emit("结果：失败（缺少 BetterGI 路径）")
            return 1

    # 运行期间保持唤醒。否则跑了一小时后系统自动睡眠，
    # BetterGI 的截屏识别和键鼠模拟会一起失效 ——
    # 现象是"卡死但进程还活着"（2026-10-05 凌晨实测：自动秘境卡住、
    # 「切换角色卡住，执行脱困」空转 806 次、白等 5 小时）。
    # 注意：SetThreadExecutionState 只对本线程有效，进程退出自动失效。
    with_disp = str(cfg.get("KeepScreenOn")).strip() in ("1", "true", "True",
                                                         "yes")
    if keep_awake(True, with_display=with_disp):
        emit("运行期间保持唤醒已启用%s。"
             % ("（显示器也保持常亮）" if with_disp else "（允许息屏，但不会睡眠）"))
    else:
        emit("提示：保持唤醒调用失败（不影响本次运行）。")

    if not cfg.one_dragon:
        emit("未填写一条龙配置名，请到主界面「基础设置」选择。")
        emit("结果：失败（缺少配置名）")
        return 1

    # 时间窗只是兜底闸门：默认不限制，完全交给任务时间决定
    allowed, why = cfg.in_window()
    if not allowed:
        emit("当前时间不在时间窗 %s，本次跳过。" % why)
        emit("  提示：这通常意味着「运行时间」和「允许运行的时间窗」不一致。"
             "任务已按 %s 触发，但被时间窗挡住了。"
             "请到「自动运行」页把时间窗设为 0000-0000（不限制）。"
             % cfg.task_time)
        emit("结果：已跳过（不在时间窗）")
        return 0
    emit("时间窗检查通过（%s）" % why)

    # ---- 按「游戏与顺序」依次执行各阶段 ----
    order = stage_order(cfg)
    if not order:
        emit("⚠ 没有启用任何游戏阶段，本次什么都没做。"
             "请到「自动运行」页 →「游戏与顺序」勾选至少一个游戏。")
        return 0
    emit("本次执行顺序：%s"
         % " → ".join("%s（%s）" % (STAGE_MAP[i]["label"],
                                  STAGE_MAP[i]["tool"]) for i in order))

    results = []
    for idx, sid in enumerate(order):
        stage = STAGE_MAP[sid]
        emit("")
        emit("════════ 第 %d/%d 阶段：%s —— %s ════════"
             % (idx + 1, len(order), stage["label"], stage["tool"]))
        try:
            sok, smsg = run_stage(sid, cfg, emit)
        except Exception as exc:  # noqa: BLE001
            sok, smsg = False, "阶段出错：%s" % exc
        emit("阶段结果：%s —— %s" % ("成功" if sok else "失败", smsg))
        results.append((sid, sok, smsg))

        # 不是最后一个 → 清理本阶段进程，给下一个腾出显卡/内存
        if idx < len(order) - 1:
            try:
                emit("清理 %s 的相关进程，准备进入下一阶段…"
                     % stage["label"])
                cleanup_stage(cfg, stage, emit)
            except Exception as exc:  # noqa: BLE001
                emit("（清理时出错，不影响后续阶段：%s）" % exc)

    emit("")
    emit("════════ 全部阶段结束 ════════")
    for sid, sok, smsg in results:
        emit("  %s %-10s %s" % ("✓" if sok else "✗",
                                STAGE_MAP[sid]["label"], smsg))

    ok_count = sum(1 for _s, sok, _m in results if sok)
    emit("汇总：%d/%d 个阶段成功。" % (ok_count, len(results)))
    return 0 if ok_count else 1


def onedragon_mode(cfg):
    """--run-onedragon：直接跑一条龙（供唤醒任务调用）。"""
    lines = []

    def emit(msg, kind="info", progress=None):
        """日志回调。

        必须**容忍多余的 kind / progress 参数**：GUI 那边是
        `emit(msg, kind, progress)`，如果这里只写 `def emit(msg)`，
        任何一处 `emit("...", "warn")` 都会在凌晨计划任务里抛
        TypeError 把整条链路打断（而且是无界面运行，报错没人看得见）。
        """
        lines.append(msg)
        auto_log(cfg, msg)
        print(msg)

    emit("==== 唤醒后自动执行一条龙 ====")
    emit("[唤醒取证] lastwake:\n%s" % last_wake_info())
    ok, msg = start_onedragon(cfg, emit, skip_window=True)
    emit("结果：%s" % msg)
    return 0 if ok else 1


def main_cli():
    """命令行模式：无参数则启动 GUI。"""
    args = sys.argv[1:]
    cfg = Config()
    if args and args[0] == "--run-daily":
        return daily_mode(cfg, record_wake=True)
    if args and args[0] == "--run-onedragon":
        if "--skip-window" in args:
            return onedragon_mode(cfg)
    return None
# -*- coding: utf-8 -*-
"""程序入口。优先处理命令行（计划任务调用），其余情况打开图形界面。"""

import os
import sys

import core


def selftest():
    """自检模式：把关键环境信息写到 selftest.txt，用于排查打包问题。"""
    lines = [
        "frozen = %s" % getattr(sys, "frozen", False),
        "executable = %s" % sys.executable,
        "_MEIPASS = %s" % getattr(sys, "_MEIPASS", None),
        "app_dir = %s" % core.app_dir(),
        "config_path = %s" % core.config_path(),
        "log_dir = %s" % core.log_dir(),
        "--- files in app_dir ---",
    ]
    try:
        for fn in sorted(os.listdir(core.app_dir())):
            lines.append("  " + fn)
    except OSError as exc:
        lines.append("  (listdir failed: %s)" % exc)
    lines.append("--- icon ---")
    try:
        raw = None
        base = getattr(sys, "_MEIPASS", None)
        cands = []
        if base:
            cands.append(os.path.join(base, "app.png"))
        cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "app.png"))
        cands.append(os.path.join(core.app_dir(), "app.png"))
        for p in cands:
            lines.append("  try %s -> %s" % (p, os.path.isfile(p)))
            if os.path.isfile(p):
                with open(p, "rb") as fh:
                    raw = fh.read()
                break
        lines.append("  icon bytes = %s" % (len(raw) if raw else None))
    except Exception as exc:  # noqa: BLE001
        lines.append("  icon check failed: %s" % exc)
    lines.append("--- bettergi ---")
    try:
        exe = core.find_bgi_exe()
        lines.append("  bgi = %s" % exe)
        lines.append("  configs = %s" % core.list_onedragon_configs(
            os.path.dirname(exe) if exe else ""))
    except Exception as exc:  # noqa: BLE001
        lines.append("  probe failed: %s" % exc)
    path = core.write_report("selftest.txt", "\n".join(lines) + "\n")
    print("selftest 报告: %s" % path)
    return 0


def _report(title, text, ok=True, path=None):
    """命令行维护命令的可见回执。

    打包后是 windowed 程序（console=False），`print()` 无处可见 ——
    用户点了「安装任务」、过了 UAC，然后什么都看不到，完全没法判断
    成功与否。所以这里：① 写一份报告文件；② 用对话框弹出来。

    path 指定时写到该路径（GUI 提权后会读这个文件把结果显示在界面上，
    这样即使对话框被误关，结果也不会丢）。
    **调用方显式给了 path，就说明有人在外面等这个文件**（GUI 提权场景），
    此时不弹对话框 —— 否则那个模态框会一直阻塞提权进程，
    外面的等待线程虽然能读到文件，却看着一个没人点的窗口挂着。
    """
    caller_waits = bool(path)
    if not path:
        name = "AutoBGI_%s.txt" % ("install" if "安装" in title else "report")
        path = core.write_report(name, text)
    else:
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            path = None

    if caller_waits:
        return path

    body = text
    if path:
        body += "\n\n（完整报告已保存到：%s）" % path
    if len(body) > 1500:
        body = body[:1500] + "\n…（内容较长，请看上面的报告文件）"
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        if ok:
            messagebox.showinfo(title, body)
        else:
            messagebox.showwarning(title, body)
        root.destroy()
    except Exception:
        pass
    return path


def _toolbox_cmd(argv):
    """命令行维护工具。

        --survey     列出所有相关计划任务与自启项
        --clean-all  提权删除所有相关任务 + 结束 BetterGI 进程
        --install    提权安装每日任务（先删后建）+ 回读校验

    这些命令需要**管理员权限**才有效果；当前不是管理员时，
    自动用 runas 以管理员身份重新执行自己。
    """
    wants = ("--survey" in argv or "--clean-all" in argv
             or "--install" in argv or "--apply-power" in argv
             or "--wake-test" in argv or "--chain-now" in argv)
    if not wants:
        return None

    if not core.is_admin() and "--no-elevate" not in argv:
        clean = [a for a in argv if a != "--no-elevate"]
        if getattr(sys, "frozen", False):
            args = [sys.executable] + clean
        else:
            args = [sys.executable, os.path.abspath(__file__)] + clean
        if core.run_as_admin_with_args(args):
            return 0
        _report("需要管理员权限",
                "无法自动提权。\n\n请右键本程序 →「以管理员身份运行」，\n"
                "然后再执行一次。", ok=False)
        return 1

    buf = []

    def say(s=""):
        buf.append(str(s))

    # GUI 提权时会带上 --report <路径>，把结果写回该文件供 GUI 读取
    rpath = None
    if "--report" in argv:
        try:
            rpath = argv[argv.index("--report") + 1]
        except IndexError:
            rpath = None

    def finish(title, ok=True):
        _report(title, "\n".join(buf), ok=ok, path=rpath)
        return 0 if ok else 1

    if "--install" in argv:
        cfg = core.Config()
        say("AutoDragon —— 安装定时任务报告")
        say("=" * 52)
        say("管理员权限 : %s" % core.is_admin())
        say("配置文件   : %s" % cfg.path)
        say("BetterGI   : %s" % (cfg.bgi_exe or "(未找到)"))
        say("运行时间   : 每天 %s" % cfg.task_time)
        say("随机误差   : %s" % ("上限 %d 分钟" % cfg.random_minutes
                                 if cfg.random_minutes else "关闭（固定时刻）"))
        say("时间窗     : %s-%s" % cfg.window)
        say("任务名     : %s" % cfg.task_name)
        say("-" * 52)
        if not cfg.bgi_exe:
            say("✗ 没找到 BetterGI.exe，安装中止。")
            say("  请先打开主界面点「自动检测」并保存。")
            return finish("安装失败：缺少 BetterGI 路径", ok=False)

        ok, msg = core.install_task(cfg, lambda m, k="info": say("· " + m))
        say("-" * 52)
        say("安装结果   : %s" % ("成功" if ok else "失败"))
        if not ok:
            say("失败原因   : %s" % msg[:300])

        # 顺便处理唤醒前提（best-effort，失败不影响任务本身）
        say()
        say("唤醒前提检查：")
        try:
            wok, wmsg = core._disable_console_lock()
            say("· 唤醒时需重新登录 : %s（%s）"
                % ("已关闭" if wok else "未能关闭", wmsg))
        except Exception as exc:  # noqa: BLE001
            say("· 唤醒时需重新登录 : 检查异常 %s" % exc)
        try:
            core._set_waketimer(True)
            ac, dc = core._win_verify_waketimer()
            say("· 允许唤醒定时器   : 交流=%s 直流=%s（1=启用）" % (ac, dc))
        except Exception as exc:  # noqa: BLE001
            say("· 允许唤醒定时器   : 检查异常 %s" % exc)

        # 给程序打「始终以管理员身份运行」标记（写 HKCU，不需要提权）。
        # 这一条把"什么时候会弹 UAC"彻底消掉：双击图标即带管理员权限，
        # 「立即接力」不再需要任何提权动作。
        try:
            if core.set_runasadmin(True):
                say("· 始终以管理员身份运行 : 已设置 ✓"
                    "（以后双击图标即自动提权，不再需要 UAC）")
            else:
                say("· 始终以管理员身份运行 : 设置失败（可手动在"
                    "「右键 → 属性 → 兼容性」里勾选）")
        except Exception as exc:  # noqa: BLE001
            say("· 始终以管理员身份运行 : 异常 %s" % exc)

        # 按需接力任务（让手动接力彻底不需要提权）
        try:
            info = core.task_info(core.RELAY_TASK)
            say("· 按需接力任务     : %s"
                % ("已就绪（" + str(info.get("STATE")) + "）" if info
                   else "未创建"))
        except Exception as exc:  # noqa: BLE001
            say("· 按需接力任务     : 异常 %s" % exc)

        say()
        text, n = core.survey_autostarts()
        say("当前系统里共有 %d 个相关任务：" % n)
        say(text)
        return finish("安装定时任务" + ("" if ok else "失败"), ok=ok)

    if "--wake-test" in argv:
        try:
            idx = argv.index("--wake-test")
            mins = int(argv[idx + 1])
        except (ValueError, IndexError):
            mins = 2
        mins = max(1, min(60, mins))
        say("唤醒测试设置")
        say("=" * 52)
        say("将在 %d 分钟后触发；确认后电脑进入睡眠。" % mins)
        say("-" * 52)
        ok, msg = core.create_wake_test(mins, lambda m, k="info": say("· " + m))
        say("-" * 52)
        say("任务创建 : %s" % ("成功" if ok else "失败"))
        if not ok:
            say("原因     : %s" % msg[:300])
            _report("唤醒测试：创建失败", "\n".join(buf), ok=False)
            return 1
        say("已登记的唤醒定时器：")
        say(core.wake_timers())
        # 明确的最后确认点：点了才睡
        go = True
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            go = messagebox.askokcancel(
                "准备睡眠",
                "唤醒测试任务已创建，%d 分钟后触发。\n\n"
                "点「确定」→ 电脑立即进入睡眠，到点自动唤醒并执行一条龙。\n"
                "点「取消」→ 放弃测试并删除该任务。" % mins)
            root.destroy()
        except Exception:
            go = True
        if not go:
            core.remove_task(core.WAKE_TEST_TASK)
            say()
            say("已取消，测试任务已删除。")
            _report("唤醒测试：已取消", "\n".join(buf), ok=True)
            return 0
        say()
        say("执行睡眠…")
        core.sleep_now()
        _report("唤醒测试：已进入睡眠", "\n".join(buf), ok=True)
        return 0

    if "--apply-power" in argv:
        say("电源设置应用报告")
        say("=" * 52)
        say("管理员权限 : %s" % core.is_admin())
        say("-" * 52)
        try:
            core._set_waketimer(True)
            ac, dc = core._win_verify_waketimer()
            say("· 允许唤醒定时器   : 交流=%s 直流=%s（1=启用）" % (ac, dc))
        except Exception as exc:  # noqa: BLE001
            say("· 允许唤醒定时器   : 异常 %s" % exc)
        try:
            ok2, msg2 = core._disable_console_lock()
            say("· 唤醒时需重新登录 : %s（%s）"
                % ("已关闭" if ok2 else "未关闭", msg2))
        except Exception as exc:  # noqa: BLE001
            say("· 唤醒时需重新登录 : 异常 %s" % exc)
        say()
        say("可用睡眠状态：")
        say(core.available_sleep_states())
        _report("电源设置已完成", "\n".join(buf), ok=True)
        return 0

    if "--chain-now" in argv:
        # 手动接力：GUI 非管理员时会带着这个参数提权重启自己再执行。
        # 必须提权 —— ok-ww 启动 PC 版鸣潮强制要求管理员（见 core.run_okww）。
        cfg = core.Config()
        say("AutoDragon —— 手动接力鸣潮报告")
        say("=" * 52)
        say("管理员权限 : %s" % core.is_admin())
        say("ok-ww 目录 : %s" % (cfg.okww_dir or "(未找到)"))
        game, gsrc = core.okww_game_exe(cfg)
        say("鸣潮主程序 : %s" % (game or "(未找到)"))
        say("             （由 ok-ww 自己启动，来源：%s）" % gsrc)
        say("鸣潮任务名 : %s" % cfg.okww_task)
        say("关闭原神   : %s" % ("是" if cfg.kill_genshin else "否"))
        say("-" * 52)
        ok, msg = core.chain_to_wuwa(
            cfg, lambda m, k="info": say("· " + str(m)))
        say("-" * 52)
        say("接力结果   : %s" % ("成功" if ok else "失败"))
        if not ok:
            say("说明       : %s" % msg)
        return finish("接力鸣潮" + ("" if ok else "失败"), ok=ok)

    text, n = core.survey_autostarts()
    say(text)
    say("=" * 52)
    if "--clean-all" in argv:
        cfg = core.Config()
        for nm in (core.WAKE_TEST_TASK, cfg.task_name):
            ok, msg = core.remove_task(nm)
            say("清理 %s → %s（%s）"
                % (nm, "成功" if ok else "失败", msg))
        core.ps("Get-Process BetterGI -ErrorAction SilentlyContinue "
                "| Stop-Process -Force; 'KILLED'")
        say("已结束正在运行的 BetterGI 进程。")
        say()
        text, n = core.survey_autostarts()
        say("清理后复查（应为 0 个任务）：")
        say(text)
        _report("清理完成", "\n".join(buf), ok=(n == 0))
        return 0

    _report("系统启动项总览", "\n".join(buf), ok=True)
    return 0


def _crash_log(exc_text):
    """把未捕获异常写到文件 + 弹窗。

    打包后是 windowed 程序（无控制台），异常默认**完全静默** ——
    双击图标后什么都没发生，用户完全不知道怎么回事（本机 2026-10-03
    就是这个问题：GUI 构造失败，进程却还活着，没有任何提示）。
    所以任何异常都必须落盘 + 弹窗。
    """
    text = ("AutoDragon —— 启动失败\n"
            "时间: %s\n"
            "frozen: %s\n"
            "exe: %s\n"
            "config: %s\n"
            "%s\n%s"
            % (__import__("datetime").datetime.now(),
               getattr(sys, "frozen", False), sys.executable,
               core.config_path(), "-" * 56, exc_text))
    path = core.write_report("AutoBGI_crash.txt", text)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "程序启动失败",
            "界面初始化时出错，程序无法打开。\n\n"
            "错误摘要：\n%s\n\n"
            "%s" % (exc_text.strip().splitlines()[-1][:300],
                    ("详细报告：%s" % path) if path else
                    "（报告文件写入失败）"))
        root.destroy()
    except Exception:
        pass
    return path


def main():
    # 升级残留自清理（改名法升级留下的 *.old.exe），best-effort
    try:
        core.cleanup_stale_exe()
    except Exception:  # noqa: BLE001
        pass
    if "--whoami" in sys.argv:
        # 诊断用：报告「当前进程是不是管理员」以及「有没有设置始终以
        # 管理员身份运行」。用途是验证双击图标到底有没有自动提权 ——
        # 这台机器 UAC 设为「不提示」，提权成功也不会有任何可见反馈。
        rp = None
        if "--report" in sys.argv:
            try:
                rp = sys.argv[sys.argv.index("--report") + 1]
            except IndexError:
                rp = None
        txt = ("AutoDragon —— 权限自检\n"
               "%s\n"
               "当前是否管理员          : %s\n"
               "已设置「始终管理员运行」: %s\n"
               "程序路径                : %s\n"
               "配置文件                : %s\n"
               "管理员权限开关的注册表项:\n"
               "  HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion"
               "\\AppCompatFlags\\Layers\n"
               "  %s\n"
               % ("-" * 46, core.is_admin(),
                  core.runasadmin_enabled(), core.self_exe(),
                  core.config_path(),
                  os.environ.get("__COMPAT_LAYER", "(未设置)")))
        if rp:
            try:
                d = os.path.dirname(rp)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(rp, "w", encoding="utf-8") as fh:
                    fh.write(txt)
            except OSError:
                pass
        core.write_report("whoami.txt", txt)
        return 0
    if "--selftest" in sys.argv:
        return selftest()
    if "--gui-check" in sys.argv:
        # 构造一次 GUI 就退出，把结果写文件（用于打包后验证）
        try:
            import gui
            app = gui.App()
            app.update_idletasks()
            for key in ("base", "auto", "wake", "log"):
                app.show_page(key)
                app.update_idletasks()
                app.update()
            app.destroy()
            p = core.write_report("gui_check.txt",
                                  "GUI OK: 四个页面均构造成功\n")
            print("gui_check: %s" % p)
            return 0
        except Exception:
            import traceback
            _crash_log(traceback.format_exc())
            return 1
    rc = _toolbox_cmd(sys.argv)
    if rc is not None:
        return rc
    rc = core.main_cli()
    if rc is not None:
        return rc
    try:
        import gui
        return gui.main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        _crash_log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
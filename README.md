# AutoBGI · 多游戏「一条龙」自动接力助手

> 睡觉时电脑自动唤醒，按你设定的顺序，依次跑完各个游戏的每日「一条龙」。
> 全程无人值守，跑完自动关机/休眠（可选）。

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6.svg)]()
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)]()

---

## 它解决什么问题

原神、鸣潮、崩铁、绝区零……每个游戏都有一套每日任务。单机玩家一天要花
1~3 小时在上面，而且**必须开着电脑守着**。

每个游戏社区其实都有很成熟的「一条龙」自动化工具，但它们各自独立：

- 各自有各自的界面，要一个个手动点开始；
- 要定时就得自己配 Windows 计划任务，配 N 个；
- 前一个游戏不退，后一个游戏会和它抢显卡和内存；
- 半夜跑完没人管，电脑空转一整夜。

**AutoBGI 就是这些工具之间的「调度器」**：把它们串成一条流水线，
定时唤醒 → 按顺序一个个跑 → 中间自动清理进程腾资源 → 跑完收工。

> ⚠️ **本项目不包含任何游戏自动化代码。** 它只负责：启动、等待、清理。
> 实际的游戏操作全部由各游戏社区的开源工具完成（见下方「致谢」）。

---

## 支持的游戏

| 游戏 | 使用的开源工具 | 一条龙启动方式 |
|---|---|---|
| **原神** | [BetterGI](https://github.com/babalae/better-genshin-impact) | `BetterGI.exe --startOneDragon <配置名>` |
| **鸣潮** | [ok-ww](https://github.com/ok-oldking/ok-wuthering-waves) | `python main.py -t DailyTask -e` |
| **崩坏：星穹铁道** | [三月七小助手 March7thAssistant](https://github.com/moesnow/March7thAssistant) | `March7th Launcher.exe main -e` |
| **绝区零** | [ZenlessZoneZero-OneDragon](https://github.com/OneDragon-Anything/ZenlessZoneZero-OneDragon) | `OneDragon-Launcher.exe -o -c` |

**默认只启用「原神 + 鸣潮」**。崩铁和绝区零默认关闭 —— 等你装了对应的
开源工具，在界面上勾选即可启用，顺序随便调。

> 这些工具都**不在本仓库内**，需要你自己去它们的 Release 页下载。
> 界面上每个游戏都提供「下载地址」和安装要点提示。

---

## 功能特性

**多游戏流水线，顺序自由**
在「游戏与顺序」里勾选要跑哪几个游戏、用 ↑↓ 调整先后。默认
`原神 → 鸣潮`，也可以配成 `崩铁 → 绝区零 → 原神` 等任意组合。

**定时自动唤醒**
写入一个 Windows 计划任务（最高权限 + 唤醒计算机），每天定时把电脑
从睡眠中叫醒执行。支持**随机误差**：例如基准 03:30、随机上限 30 分钟，
则每天在 03:30~04:00 之间随机一个时刻触发，避免每天固定同一秒。

**自动清理，互不干扰**
每个游戏跑完后自动结束它的工具进程与游戏进程，等显卡/内存释放干净，
再进入下一个。不会出现「上一个没退、下一个卡死」。

**对付「游戏窗口被挡住」这类老大难**
一条龙工具靠截屏识别工作，一旦有窗口盖住游戏、或**开始菜单抢走焦点**，
就会反复「切换角色卡住，执行脱困」空转到天亮。本程序在开跑前会
清空桌面（只留游戏），运行期间还会周期性检查并关掉开始菜单/搜索浮层。

**卡死能识别，不白等**
进度长时间不动 + 异常日志反复出现 → 判定卡死，提前结束当前游戏、
继续下一个，而不是一路等到超时。

**出问题看得见**
所有操作都有实时进度、结果回读和明示报错，不会「点了按钮什么都没发生」。

---

## 快速开始

### 1. 准备各游戏的一条龙工具

按需下载（**至少装一个**）：

- 原神 → [BetterGI Releases](https://github.com/babalae/better-genshin-impact/releases)
- 鸣潮 → [ok-ww Releases](https://github.com/ok-oldking/ok-wuthering-waves/releases)
- 崩铁 → [March7thAssistant Releases](https://github.com/moesnow/March7thAssistant/releases)
- 绝区零 → [ZZZ-OneDragon Releases](https://github.com/OneDragon-Anything/ZenlessZoneZero-OneDragon/releases)

> 绝区零有硬性要求：**安装路径必须是全英文、不含空格**，且需要管理员权限。
> 崩铁建议也放在英文路径下。

每个工具**第一次都要先手动跑一次**，把游戏路径、账号、分辨率（16:9，
推荐 1920×1080 窗口模式）等配置好，确认它能正常完成一次日常。

### 2. 运行 AutoBGI

有两种拿程序的方式：

- **直接从仓库下载（推荐，免 Python 环境）**：打开
  [`release/原神一条龙助手.exe`](release/原神一条龙助手.exe)，
  点文件页右上角的 **Download** 即可。这是已经打包好的可执行文件。
- **从 Releases 下载**：见仓库右侧的 Releases 页（维护者会把带说明的压缩包挂在那里）。

下载后**双击**即可运行（程序会自动请求管理员权限 —— 绝大多数一条龙工具都需要）。

建议放到一个固定目录，比如 `D:\AutoBGI\`。
配置和日志会生成在 `%LOCALAPPDATA%\AutoBGI\`。

### 3. 配置

1. **基础设置** → 点「自动检测」，让它找到各工具的安装位置；
2. **自动运行** → 「游戏与顺序」里勾选要跑的游戏、排好顺序；
3. **自动运行** → 「每日定时任务」里设好运行时间和随机误差；
4. 点「安装 / 更新任务」，按提示确认一次权限。

完成。之后每天到点它就会自己醒过来干活。

想先试一次，点「立即执行一次」（会立刻按当前顺序跑一遍全流程）。

---

## 工作原理

```
  计划任务（定时 + 唤醒）
        │
        ▼
  ┌─────────────────────────────────────────┐
  │  按「游戏与顺序」依次执行                  │
  │                                          │
  │  ① 原神    启动 BetterGI 一条龙           │
  │     │      等日志出现「一条龙和配置组任务结束」│
  │     ▼                                    │
  │    清理：关 BetterGI + 关原神，等 20 秒    │
  │                                          │
  │  ② 鸣潮    交给 ok-ww（它自己启动鸣潮）    │
  │     │      等进程退出                     │
  │     ▼                                    │
  │    清理：关 ok-ww + 关鸣潮                │
  │                                          │
  │  ③ 崩铁 / ④ 绝区零（默认关闭）             │
  │           启动工具 → 等进程出现 → 等进程消失 │
  └─────────────────────────────────────────┘
        │
        ▼
     汇总结果、写日志
```

**「跑完了」怎么判断？**

- 原神：读 BetterGI 日志里的终态标志（`一条龙和配置组任务结束` /
  `任务被取消，退出执行`），同时结合进程状态。
- 鸣潮：等 ok-ww 进程退出，并根据它的输出判断成败。
- 崩铁 / 绝区零：**工具或游戏进程出现过、并随后全部消失** 视为完成
  （这个判定口径参考了 OneDragon 官方「千机链」的做法）。

---

## 配置说明

配置文件在 `%LOCALAPPDATA%\AutoBGI\AutoBGISettings.ini`，
界面上的每一项都会自动保存到这里，也可以手动编辑。

常用项：

| 配置项 | 说明 |
|---|---|
| `StageOrder` | **游戏执行顺序**，逗号分隔。`genshin`=原神、`wuwa`=鸣潮、`starrail`=崩铁、`zzz`=绝区零 |
| `TaskTime` / `RandomMinutes` | 每日基准运行时间 / 随机误差上限（分钟） |
| `MinimizeOthers` | 开跑时是否最小化其它窗口（默认开） |
| `DismissShellFloat` | 开跑时是否自动关掉开始菜单/搜索浮层（默认开） |
| `KeepScreenOn` | 运行期间是否让显示器保持常亮（默认关） |
| `OneDragonTimeoutMin` | 原神一条龙最长等待（分钟） |
| `StageTimeoutMin` | 其它游戏阶段的运行上限（分钟） |
| `M7Task` | 崩铁运行哪个任务：`main`（完整）/ `daily` / `power` |
| `ZzzArgs` | 绝区零启动参数，默认 `-o -c`（跑一条龙 + 结束后关游戏） |

---

## 常见问题

**Q：必须四个游戏都装吗？**
不用，装几个就跑几个。默认只启用原神 + 鸣潮。

**Q：我不用原神/鸣潮，能只跑崩铁吗？**
可以。在「游戏与顺序」里把原神、鸣潮取消勾选，勾上崩铁即可。

**Q：绝区零提示找不到主程序？**
它的安装路径**必须全英文且不含空格**，这是它自己的硬性要求。
另外要在它的界面里完成过一次安装和资源配置。

**Q：跑完能自动关机吗？**
绝区零可以传 `-s 秒数`（如 `-o -s 60`）在跑完后关机。
其它游戏可以用 Windows 自带的 `shutdown /s /t 300` 配合计划任务。

**Q：会不会被游戏封号？**
本程序本身不碰游戏。各一条龙工具都是「模拟人工操作」，不修改内存、
不注入进程。但仍请自行评估风险，并遵守各游戏的服务条款。

---

## 免责声明

- 本项目**只做调度**，不包含、也不分发任何游戏自动化代码或游戏资源。
- 使用自动化工具可能违反游戏服务条款，由此产生的一切后果由使用者自负。
- 请合理设置操作间隔、遵守游戏规则，把时间留给真正值得体验的内容。

---

## 致谢

本项目站在这些优秀开源项目的肩膀上：

- [BetterGI](https://github.com/babalae/better-genshin-impact) —— 原神一条龙
- [ok-ww (ok-wuthering-waves)](https://github.com/ok-oldking/ok-wuthering-waves) —— 鸣潮日常
- [March7thAssistant](https://github.com/moesnow/March7thAssistant) —— 崩坏：星穹铁道
- [ZenlessZoneZero-OneDragon](https://github.com/OneDragon-Anything/ZenlessZoneZero-OneDragon) —— 绝区零一条龙
- [OneDragon 千机链](https://onedragon-anything.github.io/tools/zh/script_chainer.html) —— 多脚本串联的完成判定思路

## License

[MIT](LICENSE)

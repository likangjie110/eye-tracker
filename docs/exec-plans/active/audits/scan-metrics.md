# 机械扫描结果（scripts/analysis/scan_metrics.py）

生成时间：2026-11-06T10:37:48+08:00  
根目录：`/Users/kukan/PycharmProjects/eye-tracker/.orca/worktrees/eye-tracker/lionfish`

## 总量

- src Python 文件：76，代码行：39147
- tests Python 文件：59，代码行：37775
- 函数：2205，类：217，内部 import 边：271

## 超长文件（>=600 行）

| 文件 | 行数 |
|---|---|
| src/eye_tracker/engine/controller.py | 3429 ⚠️ |
| src/eye_tracker/platform/hotkeys.py | 2740 ⚠️ |
| src/eye_tracker/platform/linux.py | 2528 ⚠️ |
| src/eye_tracker/ui/settings_dialog.py | 2029 ⚠️ |
| src/eye_tracker/ui/calibration_window.py | 1766 ⚠️ |
| src/eye_tracker/vision/backends/facemesh_backend.py | 1454 ⚠️ |
| src/eye_tracker/platform/windows.py | 1339 ⚠️ |
| src/eye_tracker/diagnostics.py | 1319 ⚠️ |
| src/eye_tracker/app.py | 1317 ⚠️ |
| src/eye_tracker/platform/macos.py | 1230 ⚠️ |
| src/eye_tracker/ui/wizard.py | 998 |
| src/eye_tracker/ipc.py | 970 |
| src/eye_tracker/gaze/model.py | 959 |
| src/eye_tracker/platform/autostart.py | 883 |
| src/eye_tracker/vision/worker.py | 776 |
| src/eye_tracker/cli.py | 768 |
| src/eye_tracker/platform/win_uia.py | 750 |
| src/eye_tracker/gaze/store.py | 731 |
| src/eye_tracker/vision/camera.py | 708 |
| src/eye_tracker/ui/tray.py | 702 |

## 超长函数（>=60 行，按行数）

| 位置 | 函数 | 行数 | 决策点 | 嵌套 |
|---|---|---|---|---|
| src/eye_tracker/app.py:195 | build_app | 128 | 24 | 2 |
| src/eye_tracker/cli.py:104 | build_parser | 114 | 0 | 0 |
| src/eye_tracker/diagnostics.py:730 | _calibration_section | 61 | 15 | 3 |
| src/eye_tracker/diagnostics.py:814 | _problems | 108 | 58 | 3 |
| src/eye_tracker/diagnostics.py:1117 | run_bench | 68 | 9 | 4 |
| src/eye_tracker/diagnostics.py:1235 | _bench_mode | 81 | 17 | 3 |
| src/eye_tracker/engine/controller.py:659 | __init__ | 206 | 7 | 0 |
| src/eye_tracker/engine/controller.py:867 | start | 75 | 12 | 2 |
| src/eye_tracker/engine/controller.py:1237 | apply_settings | 63 | 20 | 2 |
| src/eye_tracker/engine/decision.py:256 | update | 98 | 22 | 3 |
| src/eye_tracker/engine/guard.py:154 | update | 108 | 30 | 3 |
| src/eye_tracker/engine/presence.py:135 | update | 69 | 17 | 2 |
| src/eye_tracker/gaze/calibration.py:537 | evaluate | 112 | 24 | 1 |
| src/eye_tracker/gaze/model.py:613 | select_model | 61 | 16 | 3 |
| src/eye_tracker/gaze/model.py:478 | from_dict | 64 | 27 | 2 |
| src/eye_tracker/ipc.py:644 | _exchange | 73 | 10 | 1 |
| src/eye_tracker/panes/decider.py:261 | update | 87 | 27 | 3 |
| src/eye_tracker/platform/autostart.py:494 | _win_split | 65 | 20 | 4 |
| src/eye_tracker/platform/hotkeys.py:498 | register | 62 | 17 | 4 |
| src/eye_tracker/platform/hotkeys.py:955 | __init__ | 81 | 0 | 0 |
| src/eye_tracker/platform/windows.py:275 | __init__ | 150 | 1 | 1 |
| src/eye_tracker/ui/about.py:139 | __init__ | 92 | 1 | 1 |
| src/eye_tracker/ui/calibration_window.py:947 | __init__ | 62 | 0 | 0 |
| src/eye_tracker/ui/calibration_window.py:1538 | _show_result | 75 | 15 | 3 |
| src/eye_tracker/ui/icons.py:189 | _paint_tray_glyph | 63 | 16 | 3 |

## 高复杂度函数（决策点 top 20，>=15 行）

| 位置 | 函数 | 决策点 | 行数 | 嵌套 |
|---|---|---|---|---|
| src/eye_tracker/diagnostics.py:814 | _problems | 58 | 108 | 3 |
| src/eye_tracker/engine/guard.py:154 | update | 30 | 108 | 3 |
| src/eye_tracker/vision/worker.py:409 | _cycle | 29 | 111 | 2 |
| src/eye_tracker/panes/decider.py:261 | update | 27 | 87 | 3 |
| src/eye_tracker/gaze/model.py:478 | from_dict | 27 | 64 | 2 |
| src/eye_tracker/vision/backends/facemesh_backend.py:921 | process | 25 | 77 | 3 |
| src/eye_tracker/app.py:195 | build_app | 24 | 128 | 2 |
| src/eye_tracker/gaze/calibration.py:537 | evaluate | 24 | 112 | 1 |
| src/eye_tracker/vision/backends/face_geometry.py:108 | parse_face_geometry | 24 | 43 | 7 |
| src/eye_tracker/engine/decision.py:256 | update | 22 | 98 | 3 |
| src/eye_tracker/config.py:588 | _coerce | 22 | 36 | 2 |
| src/eye_tracker/platform/autostart.py:494 | _win_split | 20 | 65 | 4 |
| src/eye_tracker/engine/controller.py:1237 | apply_settings | 20 | 63 | 2 |
| src/eye_tracker/diagnostics.py:488 | _hotkeys_section | 19 | 59 | 2 |
| src/eye_tracker/engine/input_state.py:122 | poll | 19 | 58 | 2 |
| src/eye_tracker/platform/macos.py:882 | activate_window | 19 | 56 | 3 |
| src/eye_tracker/ui/calibration_window.py:664 | set_running | 19 | 47 | 4 |
| src/eye_tracker/platform/linux.py:1708 | capabilities | 18 | 33 | 2 |
| src/eye_tracker/diagnostics.py:1235 | _bench_mode | 17 | 81 | 3 |
| src/eye_tracker/engine/presence.py:135 | update | 17 | 69 | 2 |

## God Object 候选（>=300 行或 >=20 方法）

| 位置 | 类 | 行数 | 方法数 | 公开方法 |
|---|---|---|---|---|
| src/eye_tracker/app.py:380 | EyeTrackerApp | 733 | 54 | 21 |
| src/eye_tracker/engine/controller.py:585 | Controller | 2845 | 155 | 38 |
| src/eye_tracker/gaze/model.py:203 | GazeModel | 339 | 16 | 12 |
| src/eye_tracker/platform/base.py:38 | PlatformServices | 231 | 27 | 26 |
| src/eye_tracker/platform/hotkeys.py:1499 | _Carbon | 301 | 11 | 8 |
| src/eye_tracker/platform/hotkeys.py:2257 | X11HotkeyManager | 408 | 23 | 1 |
| src/eye_tracker/platform/linux.py:707 | _EwmhClient | 273 | 24 | 12 |
| src/eye_tracker/platform/linux.py:1580 | LinuxPlatform | 945 | 53 | 20 |
| src/eye_tracker/platform/macos.py:471 | MacPlatform | 760 | 47 | 24 |
| src/eye_tracker/platform/win_uia.py:618 | ControlView | 133 | 23 | 16 |
| src/eye_tracker/platform/windows.py:272 | _Win32 | 451 | 41 | 38 |
| src/eye_tracker/platform/windows.py:861 | WindowsPlatform | 479 | 36 | 18 |
| src/eye_tracker/ui/calibration_window.py:600 | _Surface | 315 | 17 | 8 |
| src/eye_tracker/ui/calibration_window.py:918 | CalibrationWindow | 849 | 52 | 23 |
| src/eye_tracker/ui/countdown.py:69 | CountdownToast | 272 | 20 | 10 |
| src/eye_tracker/ui/curtain.py:182 | PrivacyCurtain | 188 | 20 | 7 |
| src/eye_tracker/ui/settings_dialog.py:669 | SettingsDialog | 1351 | 69 | 17 |
| src/eye_tracker/ui/tray.py:122 | TrayIcon | 581 | 48 | 15 |
| src/eye_tracker/ui/wizard.py:508 | FirstRunWizard | 491 | 35 | 14 |
| src/eye_tracker/update/service.py:126 | UpdateService | 218 | 20 | 11 |
| src/eye_tracker/vision/backends/facemesh_backend.py:759 | FaceMeshBackend | 692 | 25 | 9 |
| src/eye_tracker/vision/worker.py:84 | VisionWorker | 693 | 33 | 11 |

## 包间依赖计数（行 → 列）

| from \ to | (pkg) | (root) | engine | gaze | panes | platform | ui | update | vision |
|---|---|---|---|---|---|---|---|---|---|
| (pkg) |  |  |  |  |  |  |  |  |  |
| (root) | 6 |  | 2 | 2 |  | 9 | 11 | 1 | 5 |
| engine | 1 | 10 |  | 4 | 5 | 2 |  |  | 4 |
| gaze |  | 5 |  |  |  |  |  |  |  |
| panes |  | 11 | 1 |  |  | 3 |  |  |  |
| platform | 3 | 9 |  |  |  |  |  |  |  |
| ui | 6 | 22 | 2 | 3 |  | 13 |  | 4 | 3 |
| update | 4 | 3 |  |  |  |  |  |  |  |
| vision | 3 | 8 |  |  |  |  |  |  |  |

## 循环依赖（模块级强连通分量）

- SCC（18 个模块）：eye_tracker.app, eye_tracker.cli, eye_tracker.diagnostics, eye_tracker.engine.controller, eye_tracker.ipc, eye_tracker.platform.hotkeys, eye_tracker.ui.about, eye_tracker.ui.calibration_window, eye_tracker.ui.countdown, eye_tracker.ui.curtain, eye_tracker.ui.icons, eye_tracker.ui.overlay, eye_tracker.ui.preview, eye_tracker.ui.settings_dialog, eye_tracker.ui.tray, eye_tracker.ui.update_dialog, eye_tracker.ui.util, eye_tracker.ui.wizard
  - 成员间依赖边：
    - `eye_tracker.app -> eye_tracker.cli`
    - `eye_tracker.app -> eye_tracker.engine.controller`
    - `eye_tracker.app -> eye_tracker.ipc`
    - `eye_tracker.app -> eye_tracker.ui.about`
    - `eye_tracker.app -> eye_tracker.ui.calibration_window`
    - `eye_tracker.app -> eye_tracker.ui.countdown`
    - `eye_tracker.app -> eye_tracker.ui.curtain`
    - `eye_tracker.app -> eye_tracker.ui.icons`
    - `eye_tracker.app -> eye_tracker.ui.overlay`
    - `eye_tracker.app -> eye_tracker.ui.preview`
    - `eye_tracker.app -> eye_tracker.ui.settings_dialog`
    - `eye_tracker.app -> eye_tracker.ui.tray`
    - `eye_tracker.app -> eye_tracker.ui.update_dialog`
    - `eye_tracker.app -> eye_tracker.ui.wizard`
    - `eye_tracker.cli -> eye_tracker.app`
    - `eye_tracker.cli -> eye_tracker.diagnostics`
    - `eye_tracker.cli -> eye_tracker.ipc`
    - `eye_tracker.diagnostics -> eye_tracker.cli`
    - `eye_tracker.diagnostics -> eye_tracker.ipc`
    - `eye_tracker.diagnostics -> eye_tracker.platform.hotkeys`
    - `eye_tracker.engine.controller -> eye_tracker.platform.hotkeys`
    - `eye_tracker.ipc -> eye_tracker.cli`
    - `eye_tracker.platform.hotkeys -> eye_tracker.cli`
    - `eye_tracker.ui.about -> eye_tracker.ui.icons`
    - `eye_tracker.ui.about -> eye_tracker.ui.util`
    - `eye_tracker.ui.calibration_window -> eye_tracker.ui.util`
    - `eye_tracker.ui.countdown -> eye_tracker.engine.controller`
    - `eye_tracker.ui.countdown -> eye_tracker.ui.util`
    - `eye_tracker.ui.curtain -> eye_tracker.ui.icons`
    - `eye_tracker.ui.curtain -> eye_tracker.ui.util`
    - `eye_tracker.ui.icons -> eye_tracker.ui.util`
    - `eye_tracker.ui.overlay -> eye_tracker.ui.util`
    - `eye_tracker.ui.preview -> eye_tracker.ui.icons`
    - `eye_tracker.ui.preview -> eye_tracker.ui.util`
    - `eye_tracker.ui.settings_dialog -> eye_tracker.cli`
    - `eye_tracker.ui.settings_dialog -> eye_tracker.diagnostics`
    - `eye_tracker.ui.settings_dialog -> eye_tracker.platform.hotkeys`
    - `eye_tracker.ui.settings_dialog -> eye_tracker.ui.icons`
    - `eye_tracker.ui.settings_dialog -> eye_tracker.ui.util`
    - `eye_tracker.ui.tray -> eye_tracker.cli`
- SCC（2 个模块）：eye_tracker.update.fetch, eye_tracker.update.winhttp
  - 成员间依赖边：
    - `eye_tracker.update.fetch -> eye_tracker.update.winhttp`
    - `eye_tracker.update.winhttp -> eye_tracker.update.fetch`

## 近似重复函数（跨模块，>=12 行）

| A | B | 较小行数 | 相似度 |
|---|---|---|---|
| src/eye_tracker/engine/decision.py:418 _guard | src/eye_tracker/panes/decider.py:403 _guard | 21 | 0.92 |

## 卫生标记计数

| 标记 | 次数 |
|---|---|
| TODO/FIXME/XXX | 0 |
| type: ignore | 13 |
| noqa | 6 |
| bare except | 1 |
| print() in src | 0 |
| subprocess shell=True | 0 |
| os.system | 0 |
| eval/exec | 7 |
| pickle | 0 |
| yaml.load | 0 |
| requests/urllib | 109 |
| time.sleep | 4 |
| mutable default | 0 |
| assert in src | 13 |


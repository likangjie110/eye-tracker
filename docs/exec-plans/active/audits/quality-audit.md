# 测试与文档审计（阶段 1）

> 审计对象：`/Users/kukan/PycharmProjects/eye-tracker/.orca/worktrees/eye-tracker/lionfish`
> 基线：HEAD `9644527`，`git describe --tags` = `v0.3.0`（2026-10-02）。
> 环境：macOS 14.1.2，Python 3.12.6（`.venv`），pytest 9.1.1；`coverage` 未安装（未安装新依赖）。
> 方式：只读；仅运行 pytest / 静态扫描 / `git status`；报告写在 /tmp，未改仓库、未 git add/commit。
> 工作区状态：审计期间有并行任务写入未跟踪文件：`docs/turn-project-to-harness-rules.md`、`scripts/analysis/scan_metrics.py`、`docs/CI.md`（10:39）、`docs/exec-plans/active/harness-migration.md`（10:43）。本报告以 10:35 的测试基线 + 10:44 的文档快照为准，D/F 中标注这些并发产物，未把它们当作产品代码缺陷。

## A. 测试基线（命令、结果、耗时、环境限制）

命令（仓库根）：`.venv/bin/python -m pytest -q`

| 运行 | 结果 | 用例数 | 耗时 | 退出码 |
|---|---|---|---|---|
| 第 1 次（10:35） | **2952 passed, 55 skipped, 0 failed** | 3007 collected | **35.59 s** | 0 |
| 第 2 次（10:36，带 `--junitxml`） | **2952 passed, 55 skipped, 0 failed** | 3007 | **34.58 s** | 0 |

- 两次结果完全一致，说明本机（macOS）可稳定复现；`.venv` 中 pytest/ruff/mypy 已就绪，无需安装。
- 跳过原因分布（`-ra` 输出汇总，合计 55）：

| 原因类别 | 数量 | 代表位置 |
|---|---:|---|
| Windows 专属（Win32 API / named pipe / token / 布局解析 / oleaut32 / COM+UIA） | 33 | `tests/test_platform_windows.py`、`test_hotkeys.py`、`test_ipc.py`、`test_panes_windows_terminal.py` |
| 需真实人脸照片（`EYE_TRACKER_TEST_FACE`） | 10 | `tests/test_facemesh.py:1107/1148/1169/1211`、`test_backends.py:443/455/470` |
| Linux 专属（/dev/videoN、inotify、X11、AppImage） | 7 | `tests/test_camera.py:658/726/733`、`test_platform_linux.py:2109` 等 |
| macOS 专属（点单位、⌃⌥ 字形、AVFoundation 权限） | 4 | `tests/test_ui_basic.py:359/375/977`、`test_camera.py:360` |
| 构建工具缺失（Inno Setup 6） | 1 | `tests/test_build_scripts.py:572` |

- 环境限制：无真实显示器/摄像头（Qt 用 `QT_QPA_PLATFORM=offscreen`，见 `tests/conftest.py:14`）；Windows/Linux 运行时路径本机不可执行；无网络；`coverage` 不可用（`ModuleNotFoundError: No module named 'coverage'`），故 B 用静态导入映射代替覆盖率。
- 测试规模：55 个 `tests/test_*.py`，约 37,775 行，1994 个测试函数（parametrize 展开后 3007 用例）。

## B. 模块-测试覆盖矩阵（无测试模块清单）

### B.1 包级概况（直接导入映射，非行覆盖率）

| 包 | 源码行数 | 收集用例（归属估算） | 用例/千行 | 专属测试文件 |
|---|---:|---:|---:|---|
| platform | 9,804 | 831 | 84.8 | test_platform_{base,linux,macos,windows}、test_hotkeys、test_autostart、test_ipc |
| ui | 8,229 | 207 | **25.2（最低）** | test_ui_basic、test_ui_dialogs、test_update_ui |
| root（app/cli/config/ipc/diag/...） | 5,712 | 700 | — | test_app、test_cli、test_config、test_diagnostics、test_paths、test_privacy 等 |
| engine | 5,116 | 379 | 74.1 | 9 个模块各 1 个测试文件 + controller/ipc |
| vision | 3,992 | 309 | 77.4 | test_camera、test_motion、test_worker、test_backends、test_facemesh 等 |
| gaze | 2,848 | 281 | 98.7 | test_{calibration,filters,learning,model,store} |
| panes | 2,409 | 179 | 74.3 | 8 个测试文件（按 provider/decider/worker 拆分） |
| update | 1,037 | 138 | 133.1 | test_update_{fetch,installer,release,service,ui} |

### B.2 无直接测试引用的模块（66 个非 `__init__` 模块中）

| 模块 | 行数 | 状态 |
|---|---:|---|
| `src/eye_tracker/__main__.py` | 8 | **无任何测试**（`python -m eye_tracker` 入口） |
| `src/eye_tracker/update/winhttp.py` | 175 | **无行为测试**；仅 `tests/test_privacy.py:90` 扫其文本 allowlist（真实传输层零覆盖） |
| `src/eye_tracker/vision/backends/overlay.py` | 94 | **无直接单测**；经 `backend.annotate()` 间接覆盖（`tests/test_backends.py:271-298,321-330`） |

### B.3 全量映射（模块 → 直接 import 它的测试文件；`test_` 前缀与 `.py` 后缀省略）

| 模块（行数） | 测试（`tests/test_<名>.py`） |
|---|---|
| eye_tracker/__init__.py (11) | 版本/常量模块，52 个测试文件导入（略） |
| eye_tracker/__main__.py (8) | — |
| eye_tracker/app.py (1317) | app, cli, update_ui |
| eye_tracker/cli.py (768) | autostart, build_scripts, cli, diagnostics, hotkeys, ipc |
| eye_tracker/config.py (623) | app, cli, config, controller, decision, diagnostics, docs, guard, hotkeys, panes_chromium_apps, panes_decider, panes_registry, presence, scheduler, ui_basic, ui_dialogs |
| eye_tracker/diagnostics.py (1319) | controller, diagnostics, paths, platform_linux, ui_dialogs |
| eye_tracker/ipc.py (970) | app, cli, controller, diagnostics, ipc |
| eye_tracker/logging_setup.py (245) | app, cli |
| eye_tracker/paths.py (97) | app, autostart, backends, cli, controller, diagnostics, face_geometry, facemesh, fetch_models_licences, ipc, paths, ui_basic, ui_dialogs |
| eye_tracker/trace.py (84) | trace |
| eye_tracker/types.py (270) | app, backends, calibration, controller, decision, diagnostics, facemesh, learning, model, panes_*, platform_*, scheduler, store, ui_*, window_memory, worker（27 文件） |
| engine/camera_yield.py (76) | camera_yield |
| engine/controller.py (3429) | controller, ipc, ui_basic |
| engine/decision.py (442) | decision, reading |
| engine/guard.py (301) | guard |
| engine/input_state.py (229) | input_state |
| engine/presence.py (215) | presence |
| engine/reading.py (157) | reading |
| engine/scheduler.py (136) | scheduler |
| engine/window_memory.py (114) | window_memory |
| gaze/calibration.py (670) | calibration, controller, diagnostics, learning, model, store, ui_dialogs |
| gaze/filters.py (119) | filters |
| gaze/learning.py (359) | controller, learning |
| gaze/model.py (959) | controller, diagnostics, learning, model, store, ui_dialogs |
| gaze/store.py (731) | controller, diagnostics, store, ui_dialogs |
| panes/decider.py (433) | panes_decider |
| panes/providers/chromium_apps.py (517) | panes_chromium_apps |
| panes/providers/command.py (114) | panes_command, panes_tmux, panes_wezterm |
| panes/providers/tmux.py (279) | panes_tmux |
| panes/providers/wezterm.py (261) | panes_wezterm |
| panes/providers/windows_terminal.py (174) | panes_windows_terminal |
| panes/registry.py (215) | controller, panes_chromium_apps, panes_registry, panes_windows_terminal, panes_worker |
| panes/types.py (91) | 8 个 panes/controller 测试文件 |
| panes/worker.py (310) | controller, panes_chromium_apps, panes_registry, panes_windows_terminal, panes_worker |
| platform/autostart.py (883) | app, autostart, build_scripts, cli, diagnostics, paths, ui_basic, ui_dialogs |
| platform/base.py (268) | app, controller, diagnostics, platform_base, platform_linux, platform_macos, platform_windows, ui_basic, ui_dialogs |
| platform/hotkeys.py (2740) | app, controller, diagnostics, hotkeys, ui_basic, ui_dialogs |
| platform/linux.py (2528) | platform_linux |
| platform/macos.py (1230) | platform_macos |
| platform/win_uia.py (750) | panes_chromium_apps, panes_windows_terminal |
| platform/windows.py (1339) | platform_windows |
| ui/about.py (237) | ui_basic |
| ui/calibration_window.py (1766) | ui_dialogs |
| ui/countdown.py (340) | ui_basic |
| ui/curtain.py (369) | ui_basic |
| ui/icons.py (436) | ui_basic |
| ui/overlay.py (275) | ui_basic |
| ui/preview.py (366) | ui_basic |
| ui/settings_dialog.py (2029) | ui_dialogs, update_ui |
| ui/tray.py (702) | app, ui_basic, update_ui |
| ui/update_dialog.py (177) | update_ui |
| ui/util.py (513) | ui_basic |
| ui/wizard.py (998) | ui_dialogs |
| update/fetch.py (188) | update_fetch, update_installer, update_service, update_ui |
| update/installer.py (177) | update_installer, update_service, update_ui |
| update/release.py (142) | update_installer, update_release, update_service, update_ui |
| update/service.py (348) | update_service, update_ui |
| update/winhttp.py (175) | — |
| vision/backends/base.py (70) | worker |
| vision/backends/face_geometry.py (163) | face_geometry, facemesh |
| vision/backends/facemesh_backend.py (1454) | backends, facemesh |
| vision/backends/lite_backend.py (288) | backends, vision_threads |
| vision/backends/overlay.py (94) | —（仅间接） |
| vision/camera.py (708) | camera, controller, diagnostics, ui_dialogs, worker |
| vision/motion.py (228) | motion |
| vision/threads.py (42) | vision_threads |
| vision/worker.py (776) | controller, worker |

关键路径结论：vision/engine/update 的纯逻辑模块有直接单测且分层清晰（decision、guard、scheduler、gaze 全部注入时钟，见 `docs/architecture.md:210-215`）；UI 只有 `test_ui_*` + FakeController 级测试；platform 三个 OS 文件各有专属测试文件，Windows 分支靠 FakeWin32 在任意 OS 跑，但真 Win32/UIA 只在 Windows CI 执行。

## C. 测试质量缺口（证据 file:line）

做得好的部分（不构成问题，供对照）：`tests/test_guard.py`、`test_decision.py`、`test_worker.py`、`test_ipc.py`、`test_privacy.py`（291 个参数化用例）覆盖了大量边界与资源释放；`test_platform_*` 用 fake 让三平台逻辑在任意 OS 可跑；`tests/test_ui_dialogs.py` 覆盖了校准/向导的取消、重试、布局变化等异常路径。

缺口：

1. **无覆盖率度量**：`coverage` 未安装，`pyproject.toml:71-76` 的 dev 组只有 pytest/ruff/mypy，CI 只跑 `pytest -q`（`.github/workflows/ci.yml:118-119`），没有任何覆盖率阈值——测试数量增长与关键路径覆盖无关联。
2. **网络传输层零行为测试**：`src/eye_tracker/update/winhttp.py` 175 行（ctypes/WinHTTP、证书/代理/重定向语义）没有任何测试 import；`tests/test_update_fetch.py` 只覆盖上层 `fetch.py` 的 FakeTransport；唯一涉及的 `tests/test_privacy.py:90-97` 只按文本扫描 allowlist。该文件是 0.3.0 唯一的网络代码，风险最高。
3. **断言过弱/缺失**：AST 扫描 1994 个测试函数，3 个**完全没有断言**——`tests/test_facemesh.py:411`（`test_landmark_self_test_passes` 仅调用）、`tests/test_hotkeys.py:899`（`test_dispatch_ignores_unknown_ids` 注释 "must not raise"）、`tests/test_update_service.py:238`（`test_a_clock_set_back_does_not_block_checks` 只用 `wait_for` 等待，无断言）。另有 9 个用例的全部断言是 `is not None`/`isinstance`/常量（如 `test_hotkeys.py:350`、`test_platform_windows.py:355,359`、`test_backends.py:115`）。
4. **时序/睡眠依赖**：8 个测试函数直接 `time.sleep`（`test_worker.py:246/345/364/439/550/734/764`、`test_ipc.py:403`），另有墙钟 deadline 轮询（`test_update_service.py:112-118` 5 s、`test_app.py:749-754` 3 s）与紧上界（`test_store.py:710-740` 要求 <0.25 s；`test_ipc.py:504` 要求 <3 s）。`pyproject.toml:117-120` 没有 pytest-timeout/超时配置，慢 CI 上存在 flaky 与挂起风险（CI 仅靠 job 级 30 min 超时，`ci.yml:81`）。
5. **性能声明无回归测试**：README 发布 0.55 % CPU / 11 ms 分析耗时（`README.md:55,149-160`），但测试里只有 motion gate 的微基准（`tests/test_motion.py:306-320`，阈值 2 ms）。PERF 块由 `<!-- PERF:BEGIN/END -->` 标记手工维护，没有任何脚本/测试引用（全仓 grep 只命中两个 README）。
6. **UI 测试密度最低**：ui 包 8,229 行对应 207 个用例（25.2/千行，其他包 74.1-133.1）；`settings_dialog.py` 2,029 行约 29 个用例（`tests/test_ui_dialogs.py:335-860`），`calibration_window.py` 1,766 行 11 个，`wizard.py` 998 行 14 个。UI 用 FakeController（`tests/test_ui_dialogs.py:79-150`）复刻 Controller 的信号/方法，而真实 Controller 有 192 个函数、`test_app.py:230-247` 只验证窗口打开/关闭——接口漂移不会被 UI 测试发现。
7. **入口与边缘模块**：`src/eye_tracker/__main__.py` 无测试（`tests/test_cli.py:281` 只断言程序名是 `python -m eye_tracker`）；`vision/backends/overlay.py` 的 6 个函数无直接单测（仅经 `backend.annotate` 间接）。
8. **平台分支**：本机 55 个 skip 中 33 个是 Windows 专属；CI 矩阵三 OS 覆盖了这些（`.github/workflows/ci.yml:84-95`），但 Windows/macOS 只跑 Python 3.12，3.11/3.13 只在 Linux；`requires-python` 允许 3.14（`pyproject.toml:14`）却没有 3.14 classifier（`:37-39`）或任何 CI 组合。`tests/test_build_scripts.py:572` 的 Inno Setup 用例在 PR CI 也跳过：ci.yml 的 test job 不装 Inno Setup，`build.yml:290` 虽 `choco install innosetup` 但只构建安装器、不跑 pytest。
9. **配置卫生**：`pyproject.toml:117-120` 无 `--strict-markers`/`--strict-config`/超时；`filterwarnings = ["ignore::DeprecationWarning"]` 全局吞掉弃用警告，Qt/numpy 升级前的信号会被掩盖。

## D. 文档一致性与缺口（证据 file:line / 命令输出）

### D.1 现状与抽查通过项

- 跟踪文档约 2,314 行（README 327 + README.tr 332 + CONTRIBUTING 113 + SECURITY 64 + CHANGELOG 124 + docs/ 8 篇 1,354），外加审计期间新增的未跟踪 `docs/CI.md`（104 行）和 `docs/turn-project-to-harness-rules.md`（1,101 行）。
- **配置参考是生成型且同步**：`python scripts/gen_config_docs.py --check` → `docs/configuration.md is up to date`（exit 0），生成头注明 "Generated by scripts/gen_config_docs.py … do not edit by hand"（`docs/configuration.md:17`）；`tests/test_docs.py:26-29` 每次 pytest 都跑 `--check`。
- **配置项无遗漏**：脚本交叉核对 `describe_settings()` 的 59 个 key 全部在文档中出现（59/59），文档中无伪造 key。
- **数值承诺与代码一致（抽查）**：dwell 300 ms（`engine/decision.py:127`）、away 45 s + 倒计时 10 s（`config.py:249-262`）、second face 2 s（`config.py:303`）、478 landmarks（`facemesh_backend.py:230`）、日志 1 MB × 3（`logging_setup.py:43-44`）、首次更新检查 90 s（`update/service.py:49`）、checksums 64 KB（`update/release.py:33`）、UIA 400 步（`panes/providers/chromium_apps.py:109`）——均与文档描述一致（`README.md:42-56,147-168`、`docs/architecture.md:138-152`、`docs/privacy.md:39,59`、`docs/panes.md:58-59`）。
- **相对链接零断链**：对 `test_docs.py:69-75` 的 PAGES（15 页）手工复跑链接检查 → `BROKEN: none`。
- **隐私文档与隐私门禁挂钩**：`docs/privacy.md:10-11,30-42` 与 `SECURITY.md:53-57` 都把 `scripts/check_privacy.py` 作为"no network / no frames on disk"的强制手段；CI lint job 执行它（`.github/workflows/ci.yml:65-66`），`tests/test_privacy.py` 用 52 个测试函数（291 用例）验证该脚本规则与 allowlist。文档声明的唯一例外 `update/winhttp.py` 与 `SOURCE_ALLOWLIST` 一致。

### D.2 一致性问题

- **"零网络"绝对表述与 0.3.0 的 opt-in 更新检查矛盾**：`README.md:37-38`（"never sends a single byte over the network"）、`README.tr.md:37-38`（"ağa tek bir bayt bile göndermez"）、`docs/architecture.md:57`（"this app never touches the network"）与同文件后文 `README.md:77-78,139`、`README.tr.md:78,141`、`docs/privacy.md:10,30-42` 自相矛盾。徽章已改成 "off by default"，正文没有。
- **SECURITY.md 支持版本过期**：`SECURITY.md:12` 写 `0.1.x (latest)`，实际 `src/eye_tracker/__init__.py:3` 为 `0.3.0`，`CHANGELOG.md:9` 最新为 0.3.0，`git describe --tags` 为 `v0.3.0`。
- **隐私文档 User-Agent 示例过期**：`docs/privacy.md:41` 硬编码 `EyeTracker/0.2.1 (...)`，代码是动态版本 `USER_AGENT = f"EyeTracker/{__version__} ..."`（`src/eye_tracker/update/winhttp.py:33`），运行时是 0.3.0。
- **构建/发布示例全部停在 0.2.0**：`docs/building.md:64-66,74-75,96-97,110,186`（smoke test、iscc、zip、dmg、AppImage、`git tag v0.2.0`）；`scripts/smoke_test_bundle.py:9` docstring 示例还是 `0.1.0`。
- **architecture.md 缺 `update/` 子系统**：全文 0 次 "update"，code map（`docs/architecture.md:194-215`）没有 `update/`（`src/eye_tracker/update/` 已有 5 个模块 1,037 行，0.3.0 新增的自更新链路），也没有更新检查的线程/网络边界描述。
- **troubleshooting.md 在仓库文档中零入链**：入链统计 `docs/troubleshooting.md` = 0（README/README.tr/CONTRIBUTING/其他 docs 均未链接），只有网站页脚链接 `site/index.html:455` 与 `site/tr/index.html`。
- **双 README 的 PERF 数据块手工重复**：`README.md:149-160` 与 `README.tr.md:151-162` 数值相同但各自维护，`<!-- PERF:BEGIN/END -->` 无生成器/校验器引用。
- **site 的文档链接是绝对 GitHub main URL，门禁只查本地资源**：`site/index.html:248,352,360,455` 等指向 `https://github.com/.../blob/main/docs/*.md`，`tests/test_site.py:82-100` 仅校验构建产物里的本地 `src/href`；docs 目录重组/改名会静默断链。
- **Python 版本声明不一致**：`pyproject.toml:14` 允许 `>=3.11,<3.15`（即 3.14），classifier 只到 3.13（`:37-39`），CI 只在 Linux 测 3.11/3.13（`.github/workflows/ci.yml:92-95`）；README `:239` 和 `docs/building.md:8-13` 均未写明支持的 Python 范围（`.python-version` 固定 3.12）。
- **文档门禁覆盖面**：`tests/test_docs.py` 仅 6 个用例（`tests/test_docs.py:26,31,47,62,87,98`），PAGES = README.md/README.tr.md/CONTRIBUTING.md/SECURITY.md/CHANGELOG.md + `docs/*.md`（`:69-75`），只校验：配置生成同步、hotkey 默认值渲染、presence 数字、每个 setting 有 doc、相对链接/图片存在。**不校验**：版本一致性、锚点（`:91` 直接 `target.split("#",1)[0]`）、外部 URL、命令/代码示例、双语结构与内容一致性、文档新鲜度——D.2 的前四条过期内容因此全部漏检。
- **无 Markdown/外链/文档新鲜度 CI**：`ci.yml` 无 markdownlint/link checker 步骤（grep 无 markdown 相关 step），文档防腐仅靠上述 6 个 pytest 用例。

### D.3 harness 目标态缺口（对照 `docs/turn-project-to-harness-rules.md:88-121`）

| 目标项 | 现状 | 证据 |
|---|---|---|
| `AGENTS.md`（必需） | **缺失** | 根目录 `test -e AGENTS.md` = false |
| `ARCHITECTURE.md`（必需） | **缺失**（可复用/迁移 `docs/architecture.md`，215 行） | 根目录不存在 |
| `docs/exec-plans/`（必需：active/completed/tech-debt-tracker.md） | 审计开始时缺失；10:43 并行任务已创建 `active/harness-migration.md`（未跟踪，内容未审计）；仍缺 `tech-debt-tracker.md` 与 `completed/` | `git status` = `?? docs/exec-plans/`；规则 `:93-100,313-314` |
| `docs/QUALITY_SCORE.md`（必需） | **缺失** | 文件不存在；规则 `:116,121,384` |
| `docs/design-docs/`（index.md + core-beliefs.md） | **缺失** | 目录不存在；规则 `:96-98,393` |
| `docs/CI.md` | 审计期间由并行任务创建，未跟踪（内容与 ci.yml 描述一致） | `git status` = `?? docs/CI.md`；10:39 创建 |
| `docs/references/`、`product-specs/`、`PLANS.md` 等 | 缺失（规则称"不必一次建全"） | 同上 |
| 文档校验脚本 + CI 作业 | 只有 `tests/test_docs.py` 6 个用例；无独立 linter/新鲜度任务 | 规则 `:387-394` |

## E. 问题清单

> 共 29 条（P1 5 条 / P2 24 条 / P0 0 条）。所有条目均有 file:line 或命令输出证据；"是否改变业务行为"针对于建议修复。文档类条目以 10:44 工作区快照为准；并行任务随后仍可能新增文件（见 F.5）。

| ID | 标题 | 位置 file:line | 类别 | 严重度 | 证据 | 修复建议 | 是否改变业务行为 |
|---|---|---|---|---|---|---|---|
| GW-QA-01 | 无覆盖率度量与门禁 | `pyproject.toml:71-76`; `.github/workflows/ci.yml:118-119` | 测试 | P1 | `import coverage` → ModuleNotFoundError；dev 组仅 pytest/ruff/mypy；CI 仅 `pytest -q`，无 `--cov`/阈值 | 加 pytest-cov，先记录基线再 ratchet；CI 阈值门禁 | 无 |
| GW-QA-02 | `update/winhttp.py` 零行为测试 | `src/eye_tracker/update/winhttp.py:1-175`; `tests/test_update_fetch.py:12-14`; `tests/test_privacy.py:90-97` | 测试 | P1 | 静态导入扫描无测试引用 winhttp；fetch 测试用 FakeTransport；privacy 测试只匹配 allowlist 文本 | 参照 `test_platform_windows.py` 的 FakeWin32 用假 `ctypes.WinDLL` 覆盖 443/证书/代理/重定向/错误码 | 无 |
| GW-QA-03 | 文档门禁仅"链接存在性"，6 个用例 | `tests/test_docs.py:26-118` | 测试 | P1 | PAGES 只做链接存在检查，`:91` 丢弃 anchor；无版本/命令/双语断言；D.2 的 0.2.1/0.2.0/0.1.x 过期全部漏检 | 增加：版本一致性、锚点、代码块命令、README↔README.tr 结构/数值、外链白名单测试 | 无 |
| GW-QA-04 | Windows 专属与 Inno Setup 用例在本地/PR CI 跳过 | `tests/test_build_scripts.py:572`; `.github/workflows/ci.yml:78-96`; `.github/workflows/build.yml:290` | 测试 | P2 | 基线 55 skip 中 33 个 Windows；PR test job 不装 Inno Setup；build job 装 choco 但不跑 pytest | bundle/额外 Windows job 安装 Inno Setup 后跑该用例；或标注仅 release 验证 | 无 |
| GW-QA-05 | 3 个用例完全没有断言 | `tests/test_facemesh.py:411`; `tests/test_hotkeys.py:899`; `tests/test_update_service.py:238-244` | 测试 | P2 | AST 扫描 1994 个测试函数 → 3 个零 assert、零 raises、零 helper | 至少断言状态/副作用（net 初始化成功、dispatch 无副作用、service 继续可用） | 无 |
| GW-QA-06 | 9 个用例仅有弱断言（is not None/isinstance） | `tests/test_hotkeys.py:350`; `tests/test_platform_windows.py:355,359`; `tests/test_backends.py:115` 等 | 测试 | P2 | AST：全部断言为 `is not None`/`isinstance`/常量的用例 9 个 | 断言具体值/类型分支/副作用 | 无 |
| GW-QA-07 | 时序/睡眠依赖 + 无 pytest-timeout | `tests/test_worker.py:246,345,439`; `tests/test_ipc.py:498,504`; `tests/test_store.py:710-740`; `pyproject.toml:117-120` | 测试 | P2 | 8 个测试含 `time.sleep`；墙钟上界 0.25 s/3 s；pytest 配置无 timeout/strict 插件 | 启用 pytest-timeout；用 fake clock/Event 替代 sleep；放宽/去紧上界 | 无 |
| GW-QA-08 | 性能声明无回归测试 | `README.md:55,149-160`; `tests/test_motion.py:306-320` | 测试 | P2 | 只有 motion 微基准（<2 ms/次）；PERF 块无生成器/测试引用（grep 仅两 README） | 把 `eye-tracker bench --json` 纳入 nightly/CI 记录 CPU/延迟趋势，不硬卡阈值 | 无 |
| GW-QA-09 | UI 测试密度最低、设置/校准/向导偏薄 | `src/eye_tracker/ui/`（8,229 行）; `tests/test_ui_dialogs.py:335-860` | 测试 | P2 | ui = 207 用例/8,229 行 = 25.2/千行；settings 29 例/2,029 行、calibration 11 例/1,766 行、wizard 14 例/998 行 | 补设置保存/非法输入/权限变化、校准失败重试、向导取消后的状态回滚 | 无 |
| GW-QA-10 | FakeController 与真实 Controller 无契约测试 | `tests/test_ui_dialogs.py:79-150`; `tests/test_app.py:230-247` | 测试 | P2 | fake 手工复刻信号/方法；真实 Controller 有 192 个 `def`；app 集成测试只验证窗口开关 | 加断言 fake 覆盖真实 Controller 的公开 API 子集（签名/MRO），或让部分 UI 测试用真实 Controller | 无 |
| GW-QA-11 | `python -m eye_tracker` 入口无测试 | `src/eye_tracker/__main__.py:1-8` | 测试 | P2 | 无测试引用 `__main__`；`tests/test_cli.py:281` 只断言程序名 | 子进程 smoke：`python -m eye_tracker --version` / `doctor --json` | 无 |
| GW-QA-12 | `vision/backends/overlay.py` 无直接单测 | `src/eye_tracker/vision/backends/overlay.py:26-94`; `tests/test_backends.py:321-330` | 测试 | P2 | 6 个公开函数无测试 import；仅经 `backend.annotate()` 间接断言"像素有变化" | 补 `to_bgr/as_bgr/draw_box/line_thickness` 的灰度/尺寸/边界测试 | 无 |
| GW-QA-13 | 人脸精度声明测试从不自动运行 | `tests/conftest.py:44-52`; `docs/privacy.md:24-26`; `.github/workflows/*.yml` | 测试 | P2 | `EYE_TRACKER_TEST_FACE` 未在任何 workflow 设置 → 10 个人脸用例永远 skip；文档却引用其作为 0.2 % 精度证据 | 提交可公开的测试图或合成图；或 CI/nightly 用 secret 提供图并标注 | 无 |
| GW-QA-14 | pytest 配置无 strict/超时，全局吞掉弃用警告 | `pyproject.toml:117-120` | 测试 | P2 | `addopts="-ra"`；无 `--strict-markers`；`filterwarnings=["ignore::DeprecationWarning"]` | 加 `--strict-markers/--strict-config`、pytest-timeout；只 ignore 已审阅的具体警告 | 无 |
| GW-QA-15 | README/README.tr/architecture 的"零网络"表述与 0.3.0 更新检查矛盾 | `README.md:37-38`; `README.tr.md:37-38`; `docs/architecture.md:57` vs `README.md:77-78,139`; `docs/privacy.md:10,30-42` | 文档 | P1 | 同文件/同仓库两处互斥；0.3.0 CHANGELOG 明确新增 opt-in HTTPS | 统一改为 "no network access by default; the only opt-in exception is the update check (Windows)" | 无 |
| GW-QA-16 | SECURITY.md 支持版本停在 0.1.x | `SECURITY.md:12` | 文档 | P2 | 与 `src/eye_tracker/__init__.py:3`（0.3.0）、`CHANGELOG.md:9`、`git describe`=v0.3.0 不符 | 改为 `0.3.x (latest)` 或"仅最新 release"表述 | 无 |
| GW-QA-17 | 隐私文档 UA 示例硬编码 0.2.1 | `docs/privacy.md:41`; `src/eye_tracker/update/winhttp.py:33` | 文档 | P2 | 代码 `f"EyeTracker/{__version__}"` 运行时为 0.3.0；文档写 0.2.1 | 改为 `EyeTracker/<version>` 占位并由生成/测试替换 | 无 |
| GW-QA-18 | building.md 构建/发布示例全部停在 0.2.0 | `docs/building.md:64-66,74-75,96-97,110,186`; `scripts/smoke_test_bundle.py:9` | 文档 | P2 | 8 处 `0.2.0` + 1 处 `0.1.0`，当前版本 0.3.0 | 用 `<version>` 占位或当前版本；示例纳入 GW-QA-03 的命令校验 | 无 |
| GW-QA-19 | architecture.md code map 缺 `update/` 子系统 | `docs/architecture.md:194-215`; `src/eye_tracker/update/`（1,037 行） | 文档 | P2 | 全文 0 次 "update"；0.3.0 新增的更新服务/传输层无架构位置 | 补 update 的职责、线程/网络边界、与隐私门禁的关系 | 无 |
| GW-QA-20 | troubleshooting.md 在仓库文档中零入链 | `docs/troubleshooting.md:1`; `site/index.html:455` | 文档 | P2 | 入链统计 = 0（README/README.tr/CONTRIBUTING/其他 docs 均不链接） | README FAQ 或平台文档加入链；纳入文档索引/AGENTS.md | 无 |
| GW-QA-21 | 缺 `AGENTS.md` 入口地图 | 仓库根 | 文档 | P1 | `test -e AGENTS.md` false；规则 `:121` 列为必需 | 按规则 §2.2 建 ~100 行：定位/命令/目录/硬约束/延伸阅读 | 无 |
| GW-QA-22 | `docs/exec-plans/` 未入库且台账/归档仍缺 | `docs/exec-plans/`；`git status` | 文档 | P2 | 审计基线时整个目录缺失；10:43 并行任务创建 `active/harness-migration.md`（未跟踪）；仍无 `tech-debt-tracker.md`/`completed/`；规则 `:93-100,313-314` | 阶段 1 结束前补齐台账与归档目录，并把本次清单迁入；随阶段 2 一起提交 | 无 |
| GW-QA-23 | 缺根级 `ARCHITECTURE.md` | 仓库根 | 文档 | P2 | 根不存在；`docs/architecture.md` 已有 215 行可迁移/引用 | 阶段 2 建顶层地图并让 `docs/architecture.md` 作为细节页 | 无 |
| GW-QA-24 | 缺 `docs/QUALITY_SCORE.md` 与 `docs/design-docs/` | `docs/` | 文档 | P2 | 两路径均不存在；规则 `:116,121,384,393` | 建质量评分（测试覆盖/文档完备/复杂度/依赖方向）与设计文档索引 | 无 |
| GW-QA-25 | Python 支持版本声明不一致 | `pyproject.toml:14,37-39`; `.github/workflows/ci.yml:92-95`; `README.md:239`; `docs/building.md:8-13` | 文档 | P2 | 允许 3.14 但无 classifier/CI；3.11/3.13 仅 Linux；文档未写支持范围 | 文档写明 3.11–3.14 的支持级别；CI 补 Win/macOS 3.13 或收敛 requires-python | 无 |
| GW-QA-26 | 双 README 的 PERF 数据块手工重复 | `README.md:149-160`; `README.tr.md:151-162` | 文档 | P2 | 数值一致但无生成/同步测试；PERF marker 无脚本引用 | 用脚本从一次 benchmark 结果生成两个语言块，或加测试比较数值 | 无 |
| GW-QA-27 | site 的 docs 链接是绝对 GitHub main URL，无校验 | `site/index.html:248,352,360,455`; `tests/test_site.py:82-100` | 文档 | P2 | 站点测试只查构建产物本地 src/href；docs 重组会静默断链 | 站点构建/测试把 `blob/main/docs/*.md` 映射回仓库路径校验存在 | 无 |
| GW-QA-28 | 无 Markdown linter/外链/新鲜度 CI | `.github/workflows/ci.yml`（全文无 markdown 步骤）; `tests/test_docs.py` | 文档 | P2 | 唯一文档门禁是 6 个 pytest 用例；规则 `:150-152,387` 要求机械校验+园丁任务 | 阶段 3 加 markdownlint + lychee（外链定期）+ 文档新鲜度检查 job | 无 |
| GW-QA-29 | `docs/*.md` glob 把未跟踪的 1,101 行 harness 规则文档纳入产品文档门禁 | `docs/turn-project-to-harness-rules.md:1`; `tests/test_docs.py:75`; `git status` | 文档 | P2 | 文件未跟踪、73 KB、无 `](` 链接；因 glob 被 test_docs 当作产品页检查 | 阶段 2 决定归档位置（如 `docs/harness/` 或 `.github/`），不要混入 `docs/` 产品页 glob | 无 |

## F. 无法确认项

1. **Windows/Linux 运行时行为**：仅阅读 CI 配置，未在对应 OS 执行；`test_platform_windows.py` 的 FakeWin32 通过不代表真 Win32/UIA/命名管道行为。
2. **真实摄像头/人脸精度**：无摄像头、无 `EYE_TRACKER_TEST_FACE`；`docs/privacy.md:24-26` 的 0.2 % MediaPipe 对比未被本次审计验证。
3. **外部链接健康**：无网络，GitHub badge/release/网站外链全部未抓取；本地相对链接已确认 0 断链。
4. **覆盖率数字**：`coverage` 不可用且禁止安装，B 节为静态导入映射，不是行/分支覆盖率；`ui/` 的低密度是启发式风险而非证明。
5. **并行产物的准确性**：`docs/CI.md`（10:39）与 `docs/exec-plans/active/harness-migration.md`（10:43）在审计开始后才由并行任务创建且均未跟踪；内容未审计（仅抽查 `docs/CI.md` 与 `ci.yml` 描述一致）；`scripts/analysis/scan_metrics.py` 同为并行产物，不属于本任务范围。并行任务在报告快照（10:44）后仍可能继续写入新文件。
6. **tag/release 远端状态**：只确认本地 `v0.3.0` 标签指向 HEAD；未 fetch，无法确认远端 release/标签是否一致。
7. **其他 Python 版本**：只在 `.venv` 的 3.12.6 上跑过；3.11/3.13/3.14 的实际兼容性只能依据 CI 矩阵与 classifier。

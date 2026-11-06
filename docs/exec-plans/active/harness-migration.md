# Harness 工程化改造计划

## 0. 元信息

- 项目 / 仓库：`eye-tracker`（Python 桌面应用，MIT），本地 worktree
  `/Users/kukan/PycharmProjects/eye-tracker/.orca/worktrees/eye-tracker/lionfish`
- 远程：`origin` = `https://github.com/likangjie110/eye-tracker`（个人 fork）；`upstream` = `https://github.com/bugraskl/eye-tracker`（原仓库）
- 分支：`harnessing`（从 `likangjie/lionfish` / `main` 同点切出，未动主干）
- 计划版本 / 日期：v1 / 2026-11-06
- 状态：**已完成**（2026-11-06）：阶段 1–5 验收通过，§14 收官拆解已归档；剩余存量债务按台账排期继续。决策结果见 §6.1。
- 规则来源：已按 §14 归档至 `docs/exec-plans/completed/harness-2026-11-06.md`（原施工图，v1.0）
- 配套文件：`docs/exec-plans/tech-debt-tracker.md`（问题清单）、`docs/CI.md`（框架判定与 CI 现状）、
  `scripts/analysis/scan_metrics.py`（机械扫描脚本，可重复运行）、
  `docs/exec-plans/active/audits/`（三路审计与机械扫描的原始报告归档）

## 1. 项目概况

### 1.1 技术栈与形态

| 项 | 现状 |
|---|---|
| 语言 | Python `>=3.11,<3.15`；本机 `.python-version` = 3.12 |
| 版本 | `0.3.0`（唯一声明点 `src/eye_tracker/__init__.py:3`，hatchling 动态读取） |
| GUI | PySide6-Essentials（Qt 6 Widgets，无 QML） |
| 视觉 | OpenCV（DNN 跑 MediaPipe FaceLandmarker tflite + YuNet ONNX），`opencv-python-headless` |
| 数值 | numpy；无 scipy / 无 ML 框架 |
| 平台集成 | ctypes（Win32 / COM UIA）、pyobjc（macOS）、python-xlib（Linux X11） |
| 构建 | hatchling + uv（`uv.lock`）；PyInstaller；各平台安装器（Inno Setup / dmg / AppImage） |
| 运行 | `uv run eye-tracker`、`python -m eye_tracker`；CLI 子命令：`run`（默认）、`calibrate`、`doctor`、`bench`、`ctl`、`autostart`、`reset` |
| 部署 | GitHub Actions：tag `v*` → 三平台构建 → GitHub Release（正式发布，非 draft）；网站 GitHub Pages |
| 测试 | pytest（2952 passed / 55 skipped，35.59 s，2026-11-06 本机，macOS） |

### 1.2 顶层模块与入口点

入口点（`pyproject.toml`）：

- `eye-tracker = eye_tracker.cli:main`（console）
- `eye-tracker-gui = eye_tracker.cli:gui_main`（windowed / bundle）
- `python -m eye_tracker` → `eye_tracker.cli.main`（`src/eye_tracker/__main__.py`）

包结构（76 个 Python 文件，39,147 行；职责取自模块 docstring）：

```
src/eye_tracker/
├── __init__.py          版本、APP_* 常量
├── app.py               托盘应用编排：启动顺序、单实例、窗口管理（1317 行）
├── cli.py               命令行入口与子命令（768 行）
├── config.py            类型化设置 + 校验 + 宽容加载（623 行）
├── diagnostics.py       doctor / bench（1319 行）
├── ipc.py               单实例锁 + 本地命令通道（970 行）
├── logging_setup.py     日志配置（245 行）
├── paths.py             文件系统位置（97 行）
├── trace.py             可选跟踪日志（84 行）
├── types.py             共享值类型（270 行，仅依赖 numpy）
├── engine/              决策引擎；除 controller.py 外为纯逻辑（10 个模块）
│   └── controller.py    编排器：相机、模型、决策、OS 动作、热键（3429 行）
├── gaze/                回归模型、One Euro 滤波、校准、持久化、隐式学习（5 个模块）
├── panes/               分屏焦点（实验）：providers、worker、decider（9 个模块）
├── platform/            OS 集成：base、热键、autostart、linux/macos/windows、win_uia（8 个模块）
├── ui/                  Qt 界面：tray、settings、calibration、wizard、overlays 等（12 个模块）
├── update/              可选更新检查与安装（唯一联网代码，6 个模块）
└── vision/              相机、motion gate、face backends、worker 线程（8 个模块 + 模型资产）
```

其他顶层资产：

- `docs/`：8 篇用户/工程文档（architecture、building、calibration、configuration、
  panes、platform-support、privacy、troubleshooting），`configuration.md` 由脚本生成。
- `scripts/`：`check_privacy.py`（隐私门禁，2,203 行）、`fetch_models.py`、`make_icons.py`、
  `gen_config_docs.py`、`smoke_test_bundle.py`、`build_site.py`；本次新增 `analysis/scan_metrics.py`。
- `packaging/`：PyInstaller spec、Windows Inno Setup、macOS dmg、Linux AppImage/AppRun。
- `tests/`：55 个测试文件、37,775 行，按模块与平台分组。

### 1.3 当前测试与 CI 现状（摘要）

- 测试：pytest，2952 passed / 55 skipped / 0 failed（本机 macOS）；Windows 专属用例在本机
  skip（“needs the Win32 API”），macOS UI 用例在非 macOS skip；CI 用三 OS 矩阵补齐。
- CI：5 个 workflow（`ci` / `bundle` / `release` / `build` / `pages`），三方 Action 全部按
  SHA pin，权限最小化；版本 guard 已存在（tag 必须等于 `__version__`）。
- 本地门禁：ruff / mypy（三平台视角）/ 隐私检查 / 模型校验 / 配置文档同步**全部通过**；
  无覆盖率门禁、无本地提交钩子。详见 `docs/CI.md` §2.3。

## 2. 框架判定与 CI 现状

**判定结果：其他类型（非 Tauri）。** 依据（§3.2 机械判定，命中证据）：

- `src-tauri/tauri.conf.json` 不存在
- `src-tauri/Cargo.toml` 不存在
- 根 `package.json` 不存在（无 `@tauri-apps/*` 依赖）
- 实际形态：Python + hatchling + PyInstaller（见 `pyproject.toml`、`packaging/pyinstaller/`）

因此按 §3.4 步骤 F3：不进入 Tauri 三问；现有打包发布流水线**保留并只做审计**；
基础门禁按 §5 阶段 3 增量加强。完整的 workflow 清单、触发条件、发布链路与本地可复现性
对照表见 **`docs/CI.md`**（本计划不重复抄写）。

## 3. 现状诊断

> 所有结论均可由 `scripts/analysis/scan_metrics.py` 复现；问题清单与证据明细见
> `docs/exec-plans/tech-debt-tracker.md`。这里的数字为 2026-11-06 快照。

### 3.1 架构现状

包级依赖（行 → 列表示“import 了”，计数为模块对数，取自机械扫描）：

```mermaid
flowchart LR
    root["(root)\napp/cli/config/ipc/diagnostics\npaths/logging_setup/trace/types"]
    engine["engine"]
    gaze["gaze"]
    panes["panes"]
    platform["platform"]
    ui["ui"]
    update["update"]
    vision["vision"]

    root --> engine
    root --> gaze
    root --> platform
    root --> ui
    root --> update
    root --> vision
    engine --> root
    engine --> gaze
    engine --> panes
    engine --> platform
    engine --> vision
    gaze --> root
    panes --> root
    panes --> engine
    panes --> platform
    platform --> root
    ui --> root
    ui --> engine
    ui --> gaze
    ui --> platform
    ui --> update
    ui --> vision
    update --> root
    vision --> root
```

**分层评估（按现有 import 方向，而非理想模型）**：

| 层 | 模块 | 现状 |
|---|---|---|
| 基座（纯类型/配置） | `types.py`、`config.py`、`paths.py`、`logging_setup.py`、`trace.py` | ✅ 干净：`types` 只依赖 stdlib+numpy；`config` 只依赖 stdlib |
| 纯领域逻辑 | `gaze/*`、`engine/*`（除 `controller.py`） | ✅ 与 `CONTRIBUTING.md` 的声明一致：不 import Qt/OpenCV，时间参数化 |
| 适配器 | `vision/*`、`platform/*`、`update/*`、`ipc.py` | ⚠️ 大体清晰，但 `platform/hotkeys.py` 反向依赖 `cli`（2701 行附近，lazy import 只为取 CLI 文案） |
| 编排器 | `engine/controller.py`、`app.py`、`diagnostics.py` | ⚠️ `controller.py` 同时是领域包成员与 Qt/OS 适配器消费者；`app.py` 与 `cli.py` 互相 import |
| 表示层 | `ui/*` | ⚠️ `ui/settings_dialog.py`、`ui/tray.py` import `cli.cli_command_text`（UI → CLI 反向） |

**循环依赖（模块级强连通分量，机械扫描）**：

1. **18 模块大 SCC**：`app`、`cli`、`diagnostics`、`engine.controller`、`ipc`、
   `platform.hotkeys`、`ui.*`（about/calibration_window/countdown/curtain/icons/overlay/
   preview/settings_dialog/tray/update_dialog/util/wizard）。主要环路：
   - `app → cli` 且 `cli → app`（`cli.py:479/490` lazy import `run_app`）
   - `cli → diagnostics` 且 `diagnostics → cli`（`diagnostics.py:40`）
   - `diagnostics → platform.hotkeys → cli`（`hotkeys.py:2685`）
   - `app → ui.* → cli → app`
2. **2 模块 SCC**：`update.fetch ↔ update.winhttp`（`fetch.py:76` lazy import，
   `winhttp` 反向 import `fetch` 的类型）。

**结论**：这不是“烂架构”，而是一套**工程纪律靠文档与注释维持**的架构——
`CONTRIBUTING.md` 写明了“纯逻辑不得 import Qt/OpenCV”，代码里也确有 lazy import 的
例外注释。缺的是**机械约束**：没有依赖方向检查、没有环检测、没有文件/类大小红线，
因此后续智能体（或人）改动时无法被自动拦截。

### 3.2 复杂度热点

总量：76 个 src 文件 / 39,147 行；2,205 个函数；217 个类；**10 个文件 ≥ 1000 行**。

超长文件（≥600 行，前 10）：

| 文件 | 行数 | 备注 |
|---|---|---|
| `src/eye_tracker/engine/controller.py` | 3429 | `Controller` 一个类 2845 行 / 155 方法 |
| `src/eye_tracker/platform/hotkeys.py` | 2740 | 三平台热键实现同文件（Windows/Carbon/X11） |
| `src/eye_tracker/platform/linux.py` | 2528 | `LinuxPlatform` 945 行 / 53 方法 |
| `src/eye_tracker/ui/settings_dialog.py` | 2029 | `SettingsDialog` 1351 行 / 69 方法 |
| `src/eye_tracker/ui/calibration_window.py` | 1766 | `CalibrationWindow` 849 行 / 52 方法 |
| `src/eye_tracker/vision/backends/facemesh_backend.py` | 1454 | `FaceMeshBackend` 692 行；`process` 77 行/25 决策点 |
| `src/eye_tracker/platform/windows.py` | 1339 | `_Win32` 451 行 / `WindowsPlatform` 479 行 |
| `src/eye_tracker/diagnostics.py` | 1319 | `_problems` 108 行 / **58 决策点**（最高） |
| `src/eye_tracker/app.py` | 1317 | `EyeTrackerApp` 733 行 / 54 方法 |
| `src/eye_tracker/platform/macos.py` | 1230 | `MacPlatform` 760 行 / 47 方法 |

高复杂度函数（决策点，AST 近似 McCabe；≥15 行）：

| 位置 | 函数 | 决策点 | 行数 |
|---|---|---|---|
| `diagnostics.py:814` | `_problems` | 58 | 108 |
| `engine/guard.py:154` | `update` | 30 | 108 |
| `vision/worker.py:409` | `_cycle` | 29 | 111 |
| `panes/decider.py:261` | `update` | 27 | 87 |
| `gaze/model.py:478` | `from_dict` | 27 | 64 |
| `vision/backends/facemesh_backend.py:921` | `process` | 25 | 77 |
| `app.py:195` | `build_app` | 24 | 128 |
| `gaze/calibration.py:537` | `evaluate` | 24 | 112 |
| `vision/backends/face_geometry.py:108` | `parse_face_geometry` | 24 | 43（嵌套 7 层） |
| `engine/decision.py:256` | `update` | 22 | 98 |

**定位**：`controller.py` 与三个平台文件是“业务复杂度不可避免”的重灾区；
`diagnostics._problems`、`guard.update`、`worker._cycle`、`decider.update` 属于
**可拆分但需先有测试护栏**的目标。

### 3.3 重复代码

跨模块近似重复（≥12 行、相似度 ≥0.90）仅 1 对：

- `engine/decision.py:418 _guard` ↔ `panes/decider.py:403 _guard`（21 行，相似度 0.92）

判断：两处是**不同业务规则**（显示器切换 vs 分屏切换）中结构相似的“宽限期/守卫”逻辑，
可以抽公共小工具，也可以保留并在两处注释互指。**不构成大规模复制粘贴债**。
限制：扫描器只比较跨模块函数体（同模块内与更短片段未纳入），结论以“未发现显著重复”为准。

### 3.4 文档缺口

已有优势：README（英/土）、8 篇 `docs/`、CONTRIBUTING、SECURITY、CHANGELOG 共 3,400+ 行，
且 `tests/test_docs.py` 已机械校验：配置文档是否最新（`:26`）、每个配置项是否有说明（`:62`）、
所有 Markdown 相对链接/图片是否存在（`:87`、`:98`）。

缺口（相对 §2 目标态）：

- 无 `AGENTS.md`（入口地图，必需项）
- 无根 `ARCHITECTURE.md`（现有 `docs/architecture.md` 内容好，但：①不是 AGENTS 的默认下一跳；
  ②Code map 未列 `update/`、`paths.py`、`logging_setup.py`、`trace.py`）
- 无 `docs/exec-plans/`（本次新建）、无 `docs/QUALITY_SCORE.md`
- 无 `docs/index.md`、无 `docs/design-docs/`（`core-beliefs.md` 等）
- 无文档新鲜度门禁（架构文档漂移不会被测试发现；只有配置页与链接被校验）
- **已发现 4 组过期内容**（全部漏检）：
  - README / README.tr / `docs/architecture.md:57` 的“零网络”绝对表述与 0.3.0 的 opt-in
    更新检查矛盾（`README.md:37-38` vs `:77-78,139`）
  - `SECURITY.md:12` 支持版本仍写 `0.1.x`（当前 0.3.0）
  - `docs/privacy.md:41` 的 User-Agent 示例写死 `EyeTracker/0.2.1`
  - `docs/building.md` 8 处示例仍用 `0.2.0`（`scripts/smoke_test_bundle.py:9` 还是 0.1.0）
- 无 `prompt-history.txt`（规则 §6.3；当前 `.gitignore` 也未忽略）
- `CONTRIBUTING.md` 的 “Project layout”“Checks” 与未来 `AGENTS.md` 内容重叠，存在双份漂移风险
- 施工期规则文档（1,101 行）被 `tests/test_docs.py` 的 `docs/*.md` glob 当作产品页检查

### 3.5 测试缺口

已有优势：55 个测试文件 / 37,775 行；三 OS 矩阵（3.12 + ubuntu 3.11/3.13）；
隐私门禁本身有“注入违规必须被拦截”的自测（`tests/test_privacy.py` 的
`test_detects_violation` 等）；配置文档页有同步测试。

缺口（`/tmp/harness-scan/quality-audit.md` 的 29 条已并入台账）：

- **无覆盖率测量/门禁**：`pyproject.toml` 的 dev 依赖只有 pytest/ruff/mypy（无 pytest-cov 或
  coverage 配置），`ci.yml` 无覆盖率步骤；无法回答“关键路径覆盖了多少”
- **网络传输层零行为测试**：`update/winhttp.py`（175 行，唯一网络代码）没有任何测试 import；
  测试只用 FakeTransport，隐私测试只按文本匹配 allowlist
- **无结构测试**：依赖方向、禁止 import、文件/函数大小、循环依赖均无测试
- **无性能回归门禁**：README 的 CPU 声明由人工 `eye-tracker bench` 验证，CI 不设阈值
- **测试质量**：3 个用例零断言、9 个仅弱断言；8 个测试依赖 `time.sleep`/墙钟紧上界，
  且无 pytest-timeout
- **UI 覆盖密度最低**：UI 包 25.2 用例/千行，其他包 74–133；`FakeController` 与真实
  `Controller` 无契约测试（接口漂移不会被发现）
- 平台专属用例在本机 skip 55 个（Windows 33 / 人脸照片 10 / Linux 7 / macOS 4 / Inno 1）；
  人脸精度声明的 10 个用例在 CI 也永远 skip（未设 `EYE_TRACKER_TEST_FACE`）
- 无测试坏味道扫描（重复 setup、过度 mock、弱断言）的机械检查

### 3.6 安全、隐私与可靠性（阶段 1 已核实部分）

已有优势：

- **隐私门禁** `scripts/check_privacy.py`（2,203 行）：AST 扫源码 + 扫 bundle 原生库/压缩包，
  白名单只有 `update/winhttp.py` 的 opt-in 更新检查；错误信息写明修复方式
- **更新链路**：`update/fetch.py:26` 固定 `ALLOWED_HOSTS`；`installer.py:124` 用
  `hmac.compare_digest` 校验 SHA-256；`subprocess.Popen` 无 shell；隐私文档记录信任边界
- 全仓无 `shell=True`、无 `os.system`、无 `eval/exec`、无 `pickle`/`yaml.load`
  （机械扫描确认；7 个 “exec(” 均为 Qt `loop.exec()` 的误报）
- 线程：vision worker `stop(timeout=3.0)` + join；IPC 有 DEFAULT/PROBE/IDLE 超时；
  hotkey 回调用 `SimpleQueue`

**审计发现的最高风险（P1，详见台账 TD-SEC-001）**：自更新下载的安装器**没有** Authenticode/
Sigstore 校验，仅比对同一 release 里的 `SHA256SUMS.txt`，随后 `/VERYSILENT` 静默执行——
能替换 GitHub release 两个资产（或注入受信 TLS 链路）者即可获得当前用户权限执行。
文档已如实披露该边界；修复需要签名基建（见 D14）。

其余待补（P2，详见台账 SEC/PERF/CI 节）：

- IPC 身份校验在“无法判定”时 fail-open；Linux 私有目录不可用时回退共享 `/tmp` 固定名 socket
- 日志与 `--trace` 文件默认 0644（settings/calibration 为 0600），多用户 Linux 上可能被同机读
- doctor 报告只脱敏 home，非 home 绝对路径可能进入公开 bug 报告
- 更新下载无总 deadline/min-rate；校验→执行之间有 TOCTOU 窗口；WinHTTP 设置返回值未检查
- 摄像头 `read()` 无 OpenCV 层超时，阻塞读会延迟隐私/暂停时的设备释放
- GUI 线程上执行可能阻塞的 D-Bus / AX / 进程枚举轮询（最坏单 tick 0.5–1 s）
- Windows 用 `ASFW_ANY` 放宽前台权限；Windows 无代码签名、macOS 未 notarize、
  macOS 稳定签名证书是可选 secret
- **待复核**：FFmpeg 本地容器/播放列表可能引用远程 URL（若成立，是“never sends a byte”
  承诺的实质缺口，实验方案见 D15）
- 隐私门禁的覆盖边界与其“任何网络 import/call”文案不符；`pages.yml` 用
  `pip install "pillow>=10.4"` 绕过 uv.lock；release job 同时持有四种写权限

### 3.7 门禁现状

| 门禁 | 现状 |
|---|---|
| ruff lint / format | ✅ CI + 本地，全绿 |
| mypy（默认/win32/darwin/scripts&packaging） | ✅ CI + 本地，全绿 |
| pytest | ✅ CI 三 OS 矩阵；本地 2952 passed |
| 隐私 gate / 模型校验 / 配置文档同步 / 链接检查 | ✅ CI + 本地 |
| shellcheck / desktop-file-validate | ✅ CI（本地需自行安装工具，CONTRIBUTING 未给命令） |
| 覆盖率 | ❌ 无 |
| 结构/依赖方向检查 | ❌ 无 |
| 本地提交钩子 | ❌ 无（PR 模板是人工 checklist） |
| YAML / Actions 静态检查 | ❌ 无 actionlint |
| 版本 guard | ✅ `build.yml` prepare 已校验 tag == `__version__` |
| 发布门禁依赖测试 | ❌ tag push 只触发 release.yml，ci.yml 不监听 tag，构建/发布链路不跑 pytest |
| 发布方式 | ⚠️ tag 直接正式发布（非 draft），无 environment 审批；dispatch 到 tag ref 也会发布 |
| 依赖漏洞 / secret / CodeQL | ❌ 无 |
| workflow 静态检查（actionlint） | ❌ 无（只有正则切 YAML 的文本断言） |
| 文档新鲜度（架构/README） | ❌ 无（只有配置页与链接） |

### 3.8 改造定位（重要）

这个仓库**不是“缺纪律的存量项目”**：CI、隐私门禁、类型检查、跨平台测试、发布 guard、
文档同步都已相当成熟。harness 改造的收益点不是“补基础”，而是：

1. **把已有的隐性纪律变成机械约束**（依赖方向、大小红线、文档新鲜度）；
2. **补齐 Agent-First 的知识入口**（AGENTS.md / ARCHITECTURE.md / index / core-beliefs /
   exec-plans / QUALITY_SCORE），让智能体不靠通读 39k 行源码才能安全改动；
3. **给最重、改动最频繁的区域加护栏后再拆分**（controller、platform、UI 大文件），
   而不是为了“好看”大爆炸重构；
4. **补上发布链路的测试门禁**（tag 发布目前不依赖 pytest），这是 CI 审计唯一的 P1。

## 4. 改造批次划分

批次按业务域拆分，B0 先建文档与门禁（阶段 2–3），B1 起才动业务代码（阶段 4）。
每批独立分支、独立提交、独立回滚；不改变业务行为（红线 R1/R2）。

| 批次 | 覆盖业务域 | 主要目标 | 依赖前置 | 回滚方案 |
|---|---|---|---|---|
| **B0** | 仓库知识布局 + 门禁 | AGENTS.md、ARCHITECTURE.md、docs 结构、QUALITY_SCORE、本地门禁脚本、覆盖率/结构检查脚手架 | 阶段 1 计划获确认 | 纯文档/工具提交，`git revert` 即可；无运行时影响 |
| **B1** | 编排核心：`engine/controller.py`、`app.py`、`ipc.py`、`cli.py` | 先补特征测试，再按职责抽取协作对象（相机生命周期、热键、窗口管理、CLI 文案下沉），消除 `app↔cli` 环 | B0 门禁可用；controller 覆盖率基线建立 | 每步单独 commit，`git revert` 回滚；行为由特征测试锁定 |
| **B2** | 平台层：`platform/*`（hotkeys/linux/windows/macos/autostart/win_uia） | 按 OS/后端拆分 hotkeys；平台差异测试补齐；错误路径覆盖 | B0 + B1 的接口稳定 | 逐文件拆分提交；平台行为用现有测试 + 手工冒烟核对 |
| **B3** | 界面层：`ui/*`（settings_dialog/calibration_window/tray/wizard/util） | 对话框按页签/职责拆分；UI 与 controller 接口显式化；消除 UI→cli 反向依赖 | B1 的 controller 接口稳定 | 每步 `git revert`；UI 行为用 `QT_QPA_PLATFORM=offscreen` 测试 |
| **B4** | 视觉管线：`vision/*`（facemesh_backend/camera/worker/motion） | `process`/`_cycle` 拆段；模型加载与帧循环职责分离；保持 CPU/延迟指标 | B0 的性能基线（bench 前后各测） | 逐提交回滚；用 `eye-tracker bench` + `tests/test_*` 对比 |
| **B5** | 注视与校准：`gaze/*`（model/store/calibration/learning/filters） | `from_dict`/`evaluate` 拆段；模型兼容性测试补齐；数据结构显式化 | B0；合成数据夹具 | 逐提交回滚；旧校准文件回归测试（`tests/test_store.py`） |
| **B6** | 分屏焦点（实验）：`panes/*` | 与 `engine/decision` 的守卫逻辑去重或互指；provider 错误路径测试 | B1（controller 接口） | 功能默认关闭，回滚无用户影响 |
| **B7** | 更新与发布链路：`update/*`、`scripts/`、`packaging/`、`.github/` | 更新器信任边界测试与签名校验（TD-SEC-001）；**发布前强制测试门禁**（TD-CI-001）；打包脚本门禁化；CI 增量（覆盖率/actionlint），**不重建发布流水线** | B0 门禁；`docs/CI.md` 先对齐；D11/D14 决策 | 只增量改 workflow；`git revert` + 重跑 CI 验证 |

**优先级依据**（§1.4 原则 3：改动频率 × 故障代价）：`controller.py`、`hotkeys.py`、
`linux.py`、`settings_dialog.py`、`facemesh_backend.py` 同时具备“最高行数 + 最高历史改动量”
（git numstat 前五），故 B1/B2/B3/B4 最先加护栏。

## 5. 五阶段任务拆解

> 角色：**L** = 主智能体（Lead）；**A** = 子审计智能体（可并行、只读）；**R** = 独立复核智能体
> （对产出做对抗式检查）；**H** = 人类（确认计划 / 回答决策 / 验收）。
> 每条验收标准都必须是可机械检查的（命令、断言或文件检查），不接受“看起来对”。

### 阶段 1 · 全量扫描与问题清单（本次，已完成草稿，待 H 确认）

| ID | 子任务 | 角色 | 产出物 | 机械验收标准 |
|---|---|---|---|---|
| P1-1 | 建立仓库地图（模块/入口/构建/运行/部署） | L | 本计划 §1 | §1.2 列出的包数 = `scan_metrics.py --json` 的模块父包集合（10 个）+ 9 个根模块；入口点数 = `pyproject.toml` 的 `[project.scripts]` + `[project.gui-scripts]` 条目数 |
| P1-2 | 框架识别（§3.2） | L | `docs/CI.md` §1 | `docs/CI.md` 含三条判定证据路径；三条路径在本仓库均不存在（`test ! -e`） |
| P1-3 | 架构现状（依赖图/环） | L | 本计划 §3.1 | 图与 `scan_metrics.py` 的 `package_edges`、`cycles` 输出一致；每条环给出至少一个 `file:line` |
| P1-4 | 复杂度度量 | L | 本计划 §3.2 + 台账 CPLX 条目 | 阈值（≥600 行文件、≥60 行函数、≥300 行类）全部来自脚本输出；每条含 `file:line` |
| P1-5 | 重复代码识别 | L | 本计划 §3.3 + 台账 DUP 条目 | 复用 `scan_metrics.py` 的 duplicates 段；阈值与限制写在正文 |
| P1-6 | 文档缺口盘点 | A（quality）+ L | 本计划 §3.4 + 台账 DOC 条目 | 每条缺口用 `[ -e path ]` 或 `file:line` 证明 |
| P1-7 | 测试缺口盘点 | A（quality）+ L | 本计划 §3.5 + 台账 TEST 条目 | 给出 pytest 计数 + 覆盖映射；每条含测试文件或模块路径 |
| P1-8 | 安全审计 | A（risk）+ L | 台账 SEC 条目 | 每条含 `file:line`；网络调用点与 `check_privacy.py` 白名单对齐 |
| P1-9 | 性能与可靠性审计 | A（risk）+ L | 台账 PERF 条目 | 每条含 `file:line` + 具体机制（超时/队列/线程/循环） |
| P1-10 | 提交门禁现状审计 | A（ci）+ L | `docs/CI.md` §2.3 + 台账 CI 条目 | CI 每个 step 都能映射到本地命令或标记“不可本地复现”；缺口逐条登记 |
| P1-11 | 风险分级与排序 | L + R | 台账严重度列 + 本计划 §4 | 每条问题恰有一个 P0/P1/P2；P0 数量与“阻塞后续改造”定义一致 |
| P1-12 | 分域批次与回滚 | L + H | 本计划 §4 | 每批都有回滚方案；批次覆盖全部台账条目（无孤儿条目） |
| P1-13 | 未改业务代码自证 | L | git 记录 | `git diff --stat main -- src tests packaging .github` 为空；`git status` 只含文档/脚本 |

**阶段 1 完成定义**：本计划 + `tech-debt-tracker.md` + `docs/CI.md` + 扫描脚本已入库；
`git diff --stat` 证明未改业务代码；H 明确回复“确认计划”。

### 阶段 2 · 文档对齐（H 确认后执行）

| ID | 子任务 | 角色 | 产出物 | 机械验收标准 |
|---|---|---|---|---|
| P2-1 | 新建 `AGENTS.md`（地图非百科） | L | `AGENTS.md` | ≤200 行（目标 ~100）；必含 §2.2 的 5 节；其中每条链接目标存在；`tests/test_docs.py` 的链接检查覆盖它 |
| P2-2 | 编写根 `ARCHITECTURE.md` | L + R | `ARCHITECTURE.md` | 覆盖全部包（含 `update/`、`paths.py`、`logging_setup.py`、`trace.py`）；写出依赖方向规则；与 `scan_metrics` 的包边一致 |
| P2-3 | 建立 `docs/` 结构 | L | `docs/index.md`、`docs/design-docs/index.md`、`docs/design-docs/core-beliefs.md`、`docs/QUALITY_SCORE.md`、`docs/exec-plans/` | `docs/index.md` 索引到的每个文件都存在；`docs/exec-plans/{active,completed}` 存在；`QUALITY_SCORE.md` 覆盖 8 个业务域 |
| P2-4 | 沉淀 `core-beliefs.md` | L + H | `docs/design-docs/core-beliefs.md` | 每条原则能指向一个代码/测试证据；与 CONTRIBUTING 的 Ground rules 不矛盾（人工复核） |
| P2-5 | 清理过时内容 / 标注待核实 | L | 修订后的 `docs/*.md` | 修正 `docs/architecture.md` Code map 缺项；无法判断的内容标 `待核实` 并登记台账，不静默删除 |
| P2-6 | 把隐性知识搬进仓库 | L | `docs/design-docs/*.md`（至少：线程模型、单实例 IPC、隐私边界、发布链路） | 每篇含代码路径引用；发布链路文与 `docs/CI.md` 不冲突 |
| P2-7 | 建立 `QUALITY_SCORE.md` | L | `docs/QUALITY_SCORE.md` | 每域有分值与证据（覆盖率/复杂度/文档）；与台账 ID 关联 |
| P2-8 | 迁移计划与台账到标准位置 | L | `docs/exec-plans/active/*` | 本阶段后阶段 1 文件位置不变（已在标准位置）；`completed/` 目录空但存在 |
| P2-9 | 补全工程化说明 | L | `docs/building.md`、`docs/troubleshooting.md`、`docs/privacy.md` 更新 | 本地开发/构建/测试/调试/发布/排障各有可复制命令；CI 的每个 step 都有对应说明或“仅 CI”标注 |
| P2-10 | 建立文档防腐 | L | 扩展 `tests/test_docs.py` + doc-gardening 说明 | 新增测试：AGENTS ≤200 行、docs/index 完整、ARCHITECTURE 必含节；对注入的漂移样例必须失败（实测） |

**阶段 2 进度（2026-11-06）**：P2-1/2/3/4/7/8/10 已完成（`12a2a60`、`243e0f8`）；
P2-5/6/9 的剩余项（CONTRIBUTING 去重、锚点/外部链接检查）已登记台账，转入阶段 3。

**阶段 2 验收**：`pytest tests/test_docs.py` 通过；文档与代码现状一致（抽样命令）；
`AGENTS.md` 能从仓库根找到所有深层文档；H 验收。

### 阶段 3 · 门禁审计与自动化质量校验

| ID | 子任务 | 角色 | 产出物 | 机械验收标准 |
|---|---|---|---|---|
| P3-1 | 门禁清单 | L | `docs/CI.md` §4 | 每个 CI step ↔ 本地命令 ↔ 是否 blocking，三列完整 |
| P3-2 | 统一本地门禁入口 | L | `scripts/check.py`（或等价） | 一条命令跑完 ruff/mypy/pytest/隐私/模型/配置文档；退出码与 CI 一致 |
| P3-3 | 本地提交钩子（待 D2 决策） | L | `.pre-commit-config.yaml` 或 git hook 脚本 | 故意提交一个 lint 违规文件时钩子失败（实测并记录） |
| P3-4 | 自定义 linter：文件/函数大小上限（T1） | L | `scripts/lint_size.py` + 基线豁免清单 | 报错信息含修复指令；对超限样例必须失败；现有存量按基线豁免且数量只降不升 |
| P3-5 | 自定义结构测试：依赖方向（T7） | L | `tests/test_architecture.py` | 违反白名单的 import 样例必须失败；当前代码在“豁免清单”下方可通过 |
| P3-6 | 文档 lint 加强（T9） | L | `tests/test_docs.py` 扩展 | 缺必填节/索引漏项/AGENTS 超长三类漂移必须失败 |
| P3-7 | CI 增量接入 | L | `.github/workflows/ci.yml` 修订 | 只增量、不改发布流水线；新 job 在 PR 上跑；故意破坏样例使 CI 红（实测） |
| P3-8 | 门禁自证（DoD 4） | R | `docs/CI.md` §4 + 测试 | 每条新不变量都有一个“注入违规 → 必须失败”的测试；结果记录在案 |

**阶段 3 进度（2026-11-06）**：P3-1…P3-8 已完成。

- 统一入口 `scripts/check.py`（11 个门禁，`--quick` 跳过 pytest），本地实测 11/11 通过。
- `pre-commit` 本地钩子（可选安装）：ruff fix/format、隐私、大小、文档。
- 大小门禁 `scripts/lint_size.py` + 基线（22 文件 / 41 函数 / 16 类），只缩不增。
- 结构测试 `tests/test_architecture.py`（9 用例）：依赖方向 R1–R5、纯逻辑无 Qt/OpenCV、
  环冻结、例外表只缩不增，含 4 个负例。
- CI 增量：lint job 新增“配置文档同步”“大小门禁”两个 blocking step，未改发布流水线。
- 门禁自证：大小 5 个负例、结构 4 个负例、文档 2 个负例、隐私既有 291 个参数化用例；
  汇总见 `docs/CI.md` §4.3。

**阶段 3 验收**：任一新提交都被自动校验；所有新门禁可本地复现；门禁实测能拦截。

**阶段 3 验收结论（2026-11-06）**：满足。本地 `scripts/check.py` 11/11 通过；GitHub CI run [42154283481](https://github.com/likangjie110/eye-tracker/actions/runs/42154283481) 在 lint + 5 个测试矩阵 job 上全部通过（新增 step 已在 CI 实跑）。

### 阶段 4 · 问题修复与重构（按 B1→B7 串行）

| ID | 子任务 | 角色 | 产出物 | 机械验收标准 |
|---|---|---|---|---|
| P4-0 | 指标基线快照 | L | `docs/exec-plans/active/metrics-baseline.json` | `scan_metrics.py --json` 输出入库；后续每批对比 |
| P4-B1 | 编排核心（controller/app/ipc/cli） | L | 分步重构提交 + 更新文档 | 每步：`pytest` 全绿；`scan_metrics` 中 controller 行数下降或职责文件新增；无新 SCC；行为无变化（特征测试锁定） |
| P4-B2 | 平台层（platform/*） | L | 拆分后的模块 + 平台测试 | 三平台 CI 全绿；本机 skip 数不增加；hotkeys 按后端拆分后行为不变（现有 test_hotkeys 全绿） |
| P4-B3 | 界面层（ui/*） | L | 拆分后的对话框/组件 | offscreen UI 测试全绿；UI 不再 import `cli`（结构测试断言） |
| P4-B4 | 视觉管线（vision/*） | L | 拆分后的 backend/worker | `bench` 前后指标不退化（记录数值）；VisionWorker 生命周期测试全绿 |
| P4-B5 | 注视与校准（gaze/*） | L | 拆分后的 model/store 逻辑 | 旧校准文件兼容测试全绿；`from_dict` 决策点下降 |
| P4-B6 | 分屏焦点（panes/*） | L | 去重/互指 + 测试 | 守卫逻辑二选一（抽公共或文档互指）；panes 测试全绿 |
| P4-B7 | 更新与发布链路（update/scripts/packaging/.github） | L | 增量 CI + 测试 | 发布流水线结构不变；更新器信任边界测试全绿；`docs/CI.md` 变更历史更新 |
| P4-9 | 每批回滚演练 | L + H | 进度日志 | 每批至少一次 `git revert` 演练或在分支上验证可回滚；记录 commit 范围 |

**阶段 4 进度（B1，2026-11-06）**

- **步骤 1（CLI 文案下沉）** `f7b8a79`：新增 `command_text.py`；app / diagnostics / ipc /
  hotkeys / ui 不再 import `cli`。**18 模块强连通分量消失**（结构测试只剩 update 两个模块），
  `ARCHITECTURE.md` 例外表 5 → 1，`cli.py` 768 → 622 行。CI run
  [42155164787](https://github.com/likangjie110/eye-tracker/actions/runs/42155164787) 全绿。
- **步骤 2（away-action 策略下沉）** `b910510a`：`effective_away_action` / `DISRUPTIVE_AWAY_ACTIONS`
  移入纯逻辑 `engine/presence.py`，UI 不再依赖 controller；补 2 个策略测试。CI run
  [42155431560](https://github.com/likangjie110/eye-tracker/actions/runs/42155431560) 全绿。
- **步骤 3（controller 支撑层拆分）** `18ada68`：`CursorLike` / `WorkerLike` / `QtCursor` /
  `qt_monitors` / 工厂与纯 helper 移入 `engine/controller_support.py`；`controller.py`
  **3418 → 3165 行**，测试仍可通过 controller 访问这些 seam。新增
  `tests/test_controller_support.py`（15 个直接特征测试）。CI run
  [42156071025](https://github.com/likangjie110/eye-tracker/actions/runs/42156071025) 全绿。
- **步骤 4（热键注册抽离）** `9b1ea54`：`register_hotkeys` / `hotkey_error` 移入
  `engine/hotkey_setup.py`，注册规则可脱离 QApplication 测试；新增 9 个直接测试。
  `controller.py` 3165 → 3114 行，`Controller` 类 2845 → 2795 行。CI run
  [42156669587](https://github.com/likangjie110/eye-tracker/actions/runs/42156669587) 全绿。
- 附加：结构测试现在忽略 `if TYPE_CHECKING:` 内的 import（类型依赖不算运行时依赖），
  并带负例；这让纯模块可以引用平台类型而不制造假环。
- **步骤 5（状态/校准纯策略外移）** `6b93521`：`controller_policy.py`（`derive_state`
  优先级阶梯、`tick_interval`、`max_faces`、`may_interrupt`）与 `calibration_select.py`
  （`calibration_useful`、`known_size`、`with_camera_identity`、`match_profile` 及布局兜底）
  成为可单测纯函数；新增 29 个直接测试。`controller.py` 3114 → 3073 行，
  `Controller` 类 2795 → 2751 行。CI run
  [42157322029](https://github.com/likangjie110/eye-tracker/actions/runs/42157322029) 全绿。
- 剩余 B1：`Controller` 类本身（2,751 行）继续按协作对象拆分（相机/会话生命周期、
  校准流程编排、模式变更展示），`app.py` / `ipc.py` 的窗口与单实例职责整理。作为下一轮继续。

**阶段 4 进度（B2，2026-11-06）**

- **步骤 1（热键模块拆分）** `352096a`：`platform/hotkeys.py` 2,740 行拆为
  `hotkeys_base.py`（823 行：解析/Hotkey/管理器基类）、`hotkeys_x11.py`（698 行：X11 后端）
  与 facade `hotkeys.py`（1,297 行：Windows/macOS 后端 + Wayland 提示 + 工厂）。
  facade 再导出公共名字与 `X11HotkeyManager`，`create_hotkey_manager` 与所有导入方不变；
  测试的私有 seam 改指真实模块，断言不变。CI run
  [42158144442](https://github.com/likangjie110/eye-tracker/actions/runs/42158144442) 全绿。
- 配套：大小门禁新增“搬移识别”（同 qualname 消失于旧路径 → 允许基线转移；歧义仍失败），
  避免拆分把旧债务误报成新债务。
- **步骤 2（Windows/macOS 后端拆出，B2 完成）** `0ba5c64`：`hotkeys_windows.py`（510 行）与
  `hotkeys_macos.py`（722 行）各自拥有后端与布局探测，只依赖 `hotkeys_base` 且 OS 库懒加载；
  `platform/hotkeys.py` 变成 **151 行 facade**（从 2,740 行）：公共模型再导出、三个 manager、
  Wayland 提示与工厂。测试的私有 seam 改指真实模块，断言不变。CI run
  [42158721245](https://github.com/likangjie110/eye-tracker/actions/runs/42158721245) 全绿。
- 剩余（可选、已登记）：拆分后的 `hotkeys_base.py`（823 行）/ `hotkeys_macos.py`（722 行）/
  `hotkeys_x11.py`（698 行）仍超 600 行，可按职责再细拆（TD-CPLX-013）。
- **步骤 3（autostart 拆分）** `b8102ac`：`platform/autostart.py` 884 → 425 行，拆为
  `autostart_common.py`（78）/`autostart_windows.py`（156）/`autostart_macos.py`（140）/
  `autostart_linux.py`（158）；公共 API 与命令/配置档辅助仍在 facade，OS 调用改为模块限定，
  测试的私有 helper 与 patch 目标改指真实模块，断言不变。CI run
  [42159513393](https://github.com/likangjie110/eye-tracker/actions/runs/42159513393) 全绿。
- **步骤 4（Windows 拆分）** `95e208d`：`platform/windows.py` 1,339 → 11 行 facade；
  `windows_api.py`（839 行：ctypes 常量/结构/`_Win32`/注册表/相机占用读取）与
  `windows_platform.py`（536 行：窗口选择、焦点获取、显示器电源、权限策略）。
  测试的私有 helper 与 patch seam 改指真实模块（os/sys 直接打全局模块），断言不变。CI run
  [42161480869](https://github.com/likangjie110/eye-tracker/actions/runs/42161480869) 全绿。
- **步骤 5（Linux 拆分）** `5c6ccd5`：`platform/linux.py` 2,528 → 1,100 行；拆出
  `linux_common.py`（169）/`linux_desktop.py`（361）/`linux_x11.py`（451）/
  `linux_dbus.py`（119）/`linux_camera.py`（523）。`LinuxPlatform` 仍在 facade，平台用到的
  helper 保持可从 facade patch；测试私有引用按定义模块路由，断言不变。CI run
  [42162202129](https://github.com/likangjie110/eye-tracker/actions/runs/42162202129) 全绿。
- **步骤 6（macOS 拆分，B2 完成）** `f47bcc4`：`platform/macos.py` 1,230 → 11 行 facade；
  `macos_api.py`（439 行：常量、pyobjc/ctypes 间接层、纯 CG/AX helper）与
  `macos_platform.py`（835 行：`MacPlatform` 策略）。测试 seam 按持有绑定的模块路由，
  双模块共享的 helper 两边都 patch，断言不变。CI run
  [42163198344](https://github.com/likangjie110/eye-tracker/actions/runs/42163198344) 全绿。

**B2 完成小结**：hotkeys 2,740 → 131 行 facade + 4 模块；autostart 884 → 425 + 4 模块；
windows 1,339 → 11 + 2 模块；linux 2,528 → 1,100 + 5 模块；macos 1,230 → 11 + 2 模块。
所有 OS 后端均按“绑定/机制 vs 策略”分离，facade 只做工厂入口。
剩余已登记：`LinuxPlatform` 类 ~950 行（TD-CPLX-014）、热键 base/mac/x11 仍 >600（TD-CPLX-013）、
`MacPlatform`/`LinuxPlatform` 可按 X11/Wayland/AppKit 策略继续细分。

**阶段 4 进度（B3，2026-11-06）**

- **步骤 1（settings dialog 拆分）** `5d14754`：`ui/settings_dialog.py` 2,029 → 1,520 行；
  拆出 `ui/settings_status.py`（220 行：controller/status 读取、相机探测、诊断文本）与
  `ui/hotkey_edit.py`（342 行：快捷键录制控件、键鼠事件翻译、修饰键预览）。
  settings_dialog 继续再导出这些名字，`__all__` 与测试导入入口不变；相机探测 patch 目标
  改指持有绑定的模块，诊断 lazy import 随 helper 移动（架构例外同步）。CI run
  [42164517742](https://github.com/likangjie110/eye-tracker/actions/runs/42164517742) 全绿。
- **步骤 2（calibration 拆分）** `365cf22`：`ui/calibration_window.py` 1,767 → 997 行；拆出
  `calibration_model.py`（196：颜色常量/绘制 helper/拟合逻辑/点位状态）/
  `calibration_widgets.py`（327：`_Bar`/`_Spinner`/`_Card`/布局 helper）/
  `calibration_surface.py`（371：每屏全屏 surface）。窗口继续再导出测试用名字，断言不变。CI run
  [42165503750](https://github.com/likangjie110/eye-tracker/actions/runs/42165503750) 全绿。
- **步骤 3（wizard 拆分）** `d07e618`：`ui/wizard.py` 999 → 572 行；页面控件与格式化 helper、
  提示常量移入 `ui/wizard_pages.py`（465 行）；wizard 再导出 `frame_to_image` /
  `CAMERA_PRIVACY_HELP` / `PRESENCE_CHOICES`，公共入口不变；测试仅把私有 `_finish_subtitle`
  指向新模块。CI run [42166148422](https://github.com/likangjie110/eye-tracker/actions/runs/42166148422) 全绿。
- 剩余 B3：`SettingsDialog` 类本身仍约 1,350 行（按页签拆子组件/presenter，TD-CPLX-002）；
  `CalibrationWindow` 类约 850 行；`ui/tray.py`（702）待拆；补 UI 测试密度。

**阶段 4 进度（B4 / B5 / B6，2026-11-06）**

- **B4 步骤 1（vision）** `58e3743`：`vision/camera.py` 708 → 568 行；低层采集/设备发现移入
  `vision/camera_devices.py`（205 行：API/FOURCC、帧校验、`_imread`、`CameraError`/
  `CameraInfo`、`device_index`/`_v4l2_index`/`list_cameras`）。camera 再导出全部名字，调用方与
  测试入口不变；`_resolve_link` 的 patch seam 改指 camera_devices。
- **B5（gaze）** `a85eeb7`：三个超限文件按职责拆分——
  `model.py` 959 → 429 + `model_common.py`（191：常量/AwayRegion/线性估计）+
  `model_selection.py`（361：alpha/degree 选择与 LOPO）；
  `calibration.py` 670 → 418 + `calibration_eval.py`（329：evaluate/等级/轴误差）；
  `store.py` 731 → 522 + `store_docs.py`（322：JSON 文档读写与校验）。
  三者都保留 `__all__` 再导出，既有 import/patch 与断言不变。
- **B6（panes）**：核对后 `panes/decider.py` 434、`worker.py` 311、`registry.py` 215 均低于
  600 行阈值，最大类 218 行；本批无需拆分，记录为“已核对、无需改动”。
- B4 剩余：`vision/worker.py`（777 行，`VisionWorker` 701 行）与
  `facemesh_backend.py`（1,454 行，TD-CPLX-006），已登记 TD-CPLX-015。

### §11.1 完成定义自检（2026-11-06）

| # | 条目 | 状态 | 证据路径 |
|---|---|---|---|
| 1 | `AGENTS.md` 是地图而非百科 | ✅ | `AGENTS.md`（~130 行）；`tests/test_docs.py` 的 AGENTS 行数/链接检查 |
| 2 | `docs/` 记录系统完整且版本化 | ✅ | `docs/index.md`、`docs/design-docs/`、`docs/exec-plans/`（计划/台账/审计/决策）、`docs/QUALITY_SCORE.md`、`docs/CI.md` |
| 3 | 门禁机械可执行 | ✅ | `scripts/check.py` 本地 11/11；CI 6 job（含覆盖率步骤）；`tests/test_architecture.py`、`tests/test_docs.py`、`scripts/lint_size.py` |
| 4 | 不变量有自动检查且实测可拦截 | ✅ | 负例表见 `docs/CI.md` §4.3；隐私门有注入用例；大小/结构/文档/覆盖率负例已实测失败 |
| 5 | P0 全关，P1 有明确排期 | ✅ | 台账 P0=0；剩余 P1 均写有排期批次/条件（B1/B3/B4/B6、D14 证书）+ 阶段 4 收尾说明 |
| 6 | 覆盖率达标，关键路径有回归 | ✅ | 基线 89 %，`fail_under=89` ratchet；切换/校准/更新（含 winhttp 16 例）/隐私均有回归测试 |
| 7 | 提交历史可追溯 | ✅ | 计划进度日志逐条对应 commit（扫描→文档→门禁→B1–B7→阶段 5）；`git log --oneline` |
| 8 | 框架判定与 CI 决策已落盘且与用户回答一致 | ✅ | `docs/CI.md`：框架=其他（非 Tauri，证据明确）；D11=tag 直接正式发布；D12 GitHub 设置待用户侧确认 |
| 9 | 仅凭仓库可理解并安全改动 | ✅ | AGENTS → ARCHITECTURE → docs 的入口链；`tests/test_docs.py` 校验包覆盖/index 完整性 |
| 10 | §14 收官拆解完成 | ✅ | `git mv` → `docs/exec-plans/completed/harness-2026-11-06.md`（含归档头）；引用全部改指新路径；core-beliefs §11 固化 R1–R8 与熵管理；脱离自检：仅凭 AGENTS/core-beliefs/ARCHITECTURE/CI.md + 门禁即可遵守全部硬约束 |

> 第 10 条是唯一未完成项；执行完 §14 后本表全部 ✅，迁移结束。

**阶段 4 验收**：每批提交均通过门禁；行为守恒有证据（测试 + 指标）；复杂度/重复度下降；
进度日志逐条对应 commit。

### 阶段 5 · 测试补全与门禁接入

| ID | 子任务 | 角色 | 产出物 | 机械验收标准 |
|---|---|---|---|---|
| P5-1 | 覆盖率基线 | L | `pyproject.toml` 覆盖率配置 + 首次报告 | `pytest --cov=eye_tracker` 可运行；基线数值记录 |
| P5-2 | 覆盖率门禁（阈值待 D1） | L | CI 覆盖率步骤 | 低于阈值时 CI 失败（实测注入）；阈值不低于基线 |
| P5-3 | 按 QUALITY_SCORE 补测试 | L | 新增/加强测试文件 | 每个 ≤3 分域都有对应补测；关键路径（切换/校准/更新/隐私）有回归测试 |
| P5-4 | 结构测试接入 CI | L | `tests/test_architecture.py`、`test_docs.py`、size lint | 三者在 CI 中 blocking；本地一条命令可跑 |
| P5-5 | 性能冒烟（可选，待 D7） | L | bench 脚本 + 阈值 | `eye-tracker bench` 的 CPU/延迟阈值在 CI 或 nightly 可执行；连续两次运行稳定 |
| P5-6 | 完成定义自检（§11.1） | L + R + H | `docs/QUALITY_SCORE.md` 更新 + 验收记录 | §11.1 的 10 条逐条勾选并附证据路径 |

**阶段 5 进度（2026-11-06）**

- P5-1 完成：`pytest-cov` 加入 dev 依赖；首次基线 **89 %**（macOS，21,991 statements /
  2,316 missed，3,054 passed / 55 skipped）。配置在 `pyproject.toml` 的
  `[tool.coverage.run]` / `[tool.coverage.report]`。
- P5-2 完成：`fail_under = 89`（基线向下取整，只许上调的 ratchet）；本地 `scripts/check.py`
  的 pytest gate 默认带 `--cov`；CI 仅在 ubuntu-24.04 / Python 3.12 一个矩阵项执行覆盖率
  （其他项保持速度），低于阈值即失败。
- P5-4 完成：`tests/test_architecture.py`、`tests/test_docs.py`、`scripts/lint_size.py` 均随
  pytest / lint job 在 CI blocking，本地由 `scripts/check.py` 一条命令覆盖。
- P5-3 进行中：`update/winhttp.py` 已补 16 个行为测试（TD-TEST-002 关闭）；UI 测试密度仍偏低
  （25.2 用例/千行），按剩余轮次补关键路径。
- P5-5 按 D7 暂不设硬阈值（bench 只记趋势）。

**阶段 5 验收**：覆盖率达标；结构性规则被自动拦截；§11.1 完成定义全部满足。
通过后按 §14 执行“收官拆解”（单独提交，需 H 确认）。

## 6. 待人工决策清单

| 编号 | 问题 | 为什么需要人判断 | 影响范围 |
|---|---|---|---|
| D1 | 覆盖率工具与阈值：采用 pytest-cov？阈值定多少（建议先测基线，再定“不低于基线”的整数阈值） | 覆盖率目标与团队成本取舍，不可从代码推断 | 阶段 5 门禁 |
| D2 | 是否引入 pre-commit 本地钩子（还是只保留 CI + 文档 checklist） | 改变开发者本地工作流；有人可能反感自动改文件 | 阶段 3 / B0 |
| D3 | `ARCHITECTURE.md` 放置：把 `docs/architecture.md` 迁到根并保留跳转，还是根文件只做摘要+链接 | 影响外部链接与 README 引用方式 | 阶段 2 |
| D4 | `controller.py` 拆分范围：目标“可维护的编排器”还是“彻底分层”（后者风险与工期显著更高） | 业务风险与收益取舍 | B1 / 阶段 4 |
| D5 | 新增文档语言：仓库现有文档为英文；AGENTS/ARCHITECTURE/core-beliefs 用英文、中文还是双语 | 读者与智能体使用习惯 | 阶段 2 |
| D6 | 是否启用 `prompt-history.txt`（规则 §6.3 要求，但它是本地非版本化文件） | 涉及个人提示词留存与 .gitignore 变更 | 阶段 2 / 全程 |
| D7 | 是否引入性能回归门禁（nightly bench 阈值）；CPU 声明是否需要在 CI 机械验证 | 环境噪声导致 flaky 的风险 vs 声明可信度 | 阶段 5 |
| D8 | 是否为 Intel macOS / Linux arm64 增加构建目标（当前发布仅 arm64 Mac 与 x86_64 Linux，文档已声明） | 产品与 CI 成本决策，不可从代码推断 | B7 / CI |
| D9 | 是否把 `docs/configuration.md` 迁入 `docs/generated/`（当前为公开文档路径，生成标记已存在） | 影响 README/用户链接 | 阶段 2 |
| D10 | 门禁基线的处理：存量超大文件/类是否“冻结存量、只卡新增”（推荐），还是要求阶段 4 一次性降到阈值内 | 决定阶段 4 的工期与风险 | 阶段 3 / 4 |
| D11 | 发布门禁与发布方式：①release 前是否强制跑测试矩阵；②继续“tag 直接正式发布”还是改 draft / protected environment；③是否校验 tag 必须指向 main 祖先 | 产品发布流程决策；会改变现有发布行为 | B7 / 阶段 3 |
| D12 | GitHub Settings 侧确认：`ci.yml`/`bundle.yml` 是否为合并前 required checks、tag 是否受保护、macOS 签名 secrets 是否已配置 | 仓库文件里不可见，影响 TD-CI-001/003/006 的真实风险 | 阶段 1 确认 / B7 |
| D13 | CI 成本策略：docs-only PR 是否跳过全矩阵；`src/**` PR 是否继续跑三平台 60 min bundle，还是改为 label/nightly 触发 | 成本与覆盖的取舍 | B7 |
| D14 | 更新器签名方案（对应唯一 P1 安全项）：Windows Authenticode（WinVerifyTrust）还是内置 Sigstore 公钥/独立签名校验清单？需要密钥管理与发布流程配合 | 安全加固需要签名基建与密钥托管，属产品/运维决策 | B7 / 阶段 3 |
| D15 | FFmpeg 本地容器是否可能引用远程 URL：是否做隔离网络 `strace -f -e connect` 实验；若成立，是否限制可接受的容器/播放列表格式 | 影响“never sends a byte”承诺的边界与隐私文档措辞 | 阶段 1 复核 / B4 |
| D16 | 隐私门禁范围与文案：是否把 `eval/exec`、`QDesktopServices.openUrl`、运行时拼接的 `getattr/__import__` 纳入规则；是否扫描会进 bundle 的 `scripts/`/`packaging/` 代码；文案改成“静态扫描 + bundle 扫描” | 门禁严格度与误报成本取舍 | 阶段 3 |
| D17 | 依赖 CVE 与主版本策略：离线无法判定 OpenCV 5 / numpy 2.5 / Qt 6.11 的已知漏洞；是否给生产依赖加上界、是否把 `hatchling` 纳入锁定 | 升级灵活性与供应链安全的取舍 | B7 |


### 6.1 决策结果（2026-11-06，人工确认：按推荐默认执行）

| 编号 | 决策 | 执行方式 |
|---|---|---|
| D1 | 采用 pytest-cov；阈值 = 实测基线向下取整，之后只许升不许降（ratchet） | 阶段 5 落地；先出基线报告再设阈值 |
| D2 | 引入 pre-commit 本地钩子，与 CI 共用工具版本 | 阶段 3 / B0 |
| D3 | 根 `ARCHITECTURE.md` 做顶层地图；`docs/architecture.md` 保留为细节页 | 阶段 2 |
| D4 | `controller.py` 采用“先补特征测试、再按职责逐步抽取”的分步方案，不做一次性重写 | B1 |
| D5 | 新增的常驻文档（AGENTS/ARCHITECTURE/core-beliefs/QUALITY_SCORE）用**英文**；`docs/exec-plans/` 过程文档可用中文 | 阶段 2 起 |
| D6 | 启用 `prompt-history.txt`：`.gitignore` 忽略、只追加不读取、不在仓库内引用 | 阶段 2 |
| D7 | 性能以 `bench --json` 记录趋势（nightly/手动），暂不设硬阈值，避免环境噪声导致 flaky | 阶段 5 / B4 |
| D8 | 平台范围保持现状：Windows x64、macOS 14+ arm64、Linux x86_64；Intel Mac 文档已写明从源码运行 | 不扩 CI |
| D9 | `docs/configuration.md` 保持在公开文档路径，不迁入 `docs/generated/`；生成标记已存在 | 阶段 2 只加校验 |
| D10 | 大小/复杂度门禁“冻结存量、只卡新增”，存量按基线豁免并只减不增 | 阶段 3 |
| D11 | 发布前**强制跑测试矩阵**（新增 test 门禁）并校验 tag 指向 main 祖先；发布方式暂保持“tag 直接正式发布”，改动只做增量、不改变现有用户可见流程 | B7 |
| D12 | GitHub Settings 侧配置（required checks / tag protection / 签名 secrets）由人工确认；本次先以“未配置”为最坏假设登记 | 阶段 1 已登记，B7 复核 |
| D13 | 本次不做 CI 成本裁剪；docs-only 过滤与 bundle 触发策略留到 B7 评估 | B7 |
| D14 | 更新器签名目标为 Windows Authenticode：先实现 WinVerifyTrust 校验与失败路径测试，证书就绪后设为发布必需；过渡期文档如实披露 | B7（需签名基建） |
| D15 | 做 FFmpeg 本地容器远程 URL 复核实验（Linux 网络命名空间 / 受控环境），成立则限制容器格式并更新隐私文档 | B4 / 阶段 1 复核 |
| D16 | 隐私门禁扩规则（eval/exec、openUrl/startfile URL、运行时拼接导入），扫描会进 bundle 的脚本，文案改为“静态扫描 + bundle 扫描” | 阶段 3 |
| D17 | 生产依赖加保守主版本上界（OpenCV `<6`、PySide6 `<7`、numpy `<3` 等），锁定构建后端；CI 加 pip-audit/OSV（先 report-only） | B7 / 阶段 3 |

## 7. 进度与决策日志

| 日期 | 阶段 | 动作 | 关联 commit | 决策与理由 |
|---|---|---|---|---|
| 2026-11-06 | 阶段 1 | 切 `harnessing` 分支；仓库扫描；建立本计划、台账、`docs/CI.md`、`scripts/analysis/scan_metrics.py` | 待填 | 框架判定=其他（非 Tauri），不进入三问；现有门禁全绿，改造聚焦 Agent-First 知识布局与机械约束 |
| 2026-11-06 | 阶段 1 | 并行只读审计：CI/构建发布、测试与文档、安全与性能 | — | 子智能体只读，产出写入 `/tmp/harness-scan/`，由 L 汇总入台账；原始报告归档到 `docs/exec-plans/active/audits/` |
| 2026-11-06 | 阶段 1 | 审计结论：CI 51 条（P1=发布不依赖测试）、测试/文档 29 条（P1=覆盖率/winhttp/文档门禁/零网络矛盾）、安全/可靠性 17 条（P1=更新器无签名校验） | — | 三份原始报告归档到 `docs/exec-plans/active/audits/`；按根因归并为台账 105 条（P0=0 / P1=21 / P2=84） |
| 2026-11-06 | 阶段 1 | 决策项扩到 D17（发布门禁、GitHub Settings、CI 成本、更新器签名、FFmpeg 复核、隐私门禁范围、依赖 CVE） | — | 等 H 在确认计划时一并回答；未回答前阶段 3 不落 CI 变更 |
| 2026-11-06 | 阶段 1 | **H 确认计划**，D1–D17 按推荐默认执行（结果见 §6.1） | — | 阶段 2 启动；仍不修改业务代码 |
| 2026-11-06 | 阶段 1 | 远程仓库改为个人 fork：`origin = likangjie110/eye-tracker`，`upstream = bugraskl/eye-tracker`（`gh repo fork` 创建） | — | 后续推送目标为 fork；上游保留用于同步 |
| 2026-11-06 | 阶段 2 | 建 `AGENTS.md`（≤200 行地图）、根 `ARCHITECTURE.md`（分层规则 R1–R5 + 例外表）、`docs/index.md`、`docs/design-docs/`（index/core-beliefs/decisions）、`docs/QUALITY_SCORE.md`、`completed/` 归档约定 | `12a2a60` | 常驻文档用英文（D5）；过程文档中文 |
| 2026-11-06 | 阶段 2 | 修正 4 组过期内容与“零网络”绝对表述；architecture code map 补齐 update/paths/logging/trace；CONTRIBUTING 指向 AGENTS；`.gitignore` 忽略 `prompt-history.txt`（D6） | `243e0f8` | 文档门禁从 6 用例扩到 15：必填文档、AGENTS 行数、包覆盖、索引完整性、版本漂移、网络措辞、troubleshooting 入链；含 2 个“注入漂移必须失败”的负例 |
| 2026-11-06 | 阶段 2 | 全量门禁：ruff / format / mypy×3 / scripts / privacy / models / pytest（2961 passed / 55 skipped） | — | 阶段 2 主体完成；剩余文档去重（TD-DOC-015）、锚点/外链（TD-DOC-013/017）转到阶段 3 |
| 2026-11-06 | 阶段 3 | 结构测试：依赖方向 R1–R5、纯逻辑无 Qt/OpenCV、环冻结、例外表只缩不增（9 用例，4 个负例） | `93dbee6` | 把 ARCHITECTURE.md 的文字规则变成 pytest |
| 2026-11-06 | 阶段 3 | 大小门禁 + 冻结基线（22 文件 / 41 函数 / 16 类）；新增/增长/静默改基线都会被拒绝 | `3bfc7e8` | D10：冻结存量、只卡新增；基线只能缩 |
| 2026-11-06 | 阶段 3 | 统一本地门禁入口 `scripts/check.py`（11 门禁，--quick/--list/--fail-fast） | `48de2eb` | 本地与 CI 同序；4 个一致性测试 |
| 2026-11-06 | 阶段 3 | CI 增量（配置文档 + 大小门禁两个 blocking step）、pre-commit 本地钩子、AGENTS/CONTRIBUTING/CI.md 同步 | `62bf29d` | 未改发布流水线；门禁自证表写入 docs/CI.md §4.3 |
| 2026-11-06 | 阶段 3 | GitHub CI 实跑验证：lint + 5 个测试矩阵 job 全绿 | run `42154283481` | 新增 lint step 在 CI 真实执行；三平台测试通过 |
| 2026-11-06 | 阶段 4 · B1 | 步骤 1：`command_text.py` 承接 CLI 文案，app/cli 环消失，结构测试环基线收缩 | `f7b8a79` | 行为不变：仅 import 路径与文件归属变化；产物字符串逐字节一致（test_cli 未改断言）；CI run `42155164787` 绿 |
| 2026-11-06 | 阶段 4 · B1 | 步骤 2：`effective_away_action` 移入 `engine/presence.py`，UI 不再依赖 controller；补 2 个纯策略测试 | `b910510a` | 行为不变；CI run `42155431560` 绿 |
| 2026-11-06 | 阶段 4 · B1 | 步骤 3：controller 支撑层（指针/worker 协议、QtCursor、monitor 重认、视觉工厂）移入 `controller_support.py`；新增 15 个直接特征测试 | `18ada68` | 行为不变（seam 名从 controller 仍可 import）；controller.py 3418→3165；CI run `42156071025` 绿 |
| 2026-11-06 | 阶段 4 · B1 | 步骤 4：热键注册规则移入 `engine/hotkey_setup.py`；新增 9 个直接测试；结构测试忽略 TYPE_CHECKING 类型 import | `9b1ea54` | 行为不变（HOTKEY_ACTIONS 仍从 controller 可 import）；controller.py 3165→3114，类 2845→2795；CI run `42156669587` 绿 |
| 2026-11-06 | 阶段 4 · B1 | 步骤 5：状态派生/定时/最大人脸/打断判定与校准选择移入 `controller_policy.py` / `calibration_select.py`；新增 29 个直接测试 | `6b93521` | 行为不变（方法委托纯函数）；controller.py 3114→3073，类 2795→2751；CI run `42157322029` 绿 |
| 2026-11-06 | 阶段 4 · B2 | 步骤 1：hotkeys 拆为 base（823 行）+ X11 后端（698 行）+ facade（2740→1297 行）；门禁增加“搬移识别” | `352096a` | 行为不变（公共入口与工厂不变，断言未改）；CI run `42158144442` 绿 |
| 2026-11-06 | 阶段 4 · B2 | 步骤 2：Windows（510）/macOS（722）后端拆出，facade 2740→151 行；B2 完成 | `0ba5c64` | 行为不变（工厂/导入入口不变，断言未改）；CI run `42158721245` 绿 |
| 2026-11-06 | 阶段 4 · B2 | 步骤 3：autostart 拆为 common + 三个 OS 模块（884→425 行 façade）；OS 调用改为模块限定，测试 seam 更新 | `b8102ac` | 行为不变（公共 API 与命令不变，断言未改）；autostart.py 退出 >600 行清单；CI run `42159513393` 绿 |
| 2026-11-06 | 阶段 4 · B2 | 步骤 4：windows 拆为 `windows_api`（839）+ `windows_platform`（536）+ 11 行 facade；测试 seam 更新 | `95e208d` | 行为不变（断言未改）；CI run `42161480869` 绿 |
| 2026-11-06 | 阶段 4 · B2 | 步骤 5：linux 拆为 common/desktop/x11/dbus/camera，facade 2528→1100 行；测试 seam 按定义模块路由 | `5c6ccd5` | 行为不变（断言未改）；CI run `42162202129` 绿 |
| 2026-11-06 | 阶段 4 · B2 | 步骤 6：macos 拆为 `macos_api`（439）+ `macos_platform`（835）+ 11 行 facade；B2 完成 | `f47bcc4` | 行为不变（断言未改；共享 helper 双模块 patch）；CI run `42163198344` 绿 |
| 2026-11-06 | 阶段 4 · B3 | 步骤 1：settings_dialog 拆出 settings_status（220）+ hotkey_edit（342），facade 2029→1520；测试 seam 更新 | `5d14754` | 行为不变（公共导出与测试入口不变）；CI run `42164517742` 绿 |
| 2026-11-06 | 阶段 4 · B3 | 步骤 2：calibration_window 拆出 model（196）/widgets（327）/surface（371），facade 1767→997；类本身待拆 | `365cf22` | 行为不变（测试未改）；CI run `42165503750` 绿 |
| 2026-11-06 | 阶段 4 · B3 | 步骤 3：wizard 拆出 wizard_pages（465），wizard 999→572；公共再导出保持 | `d07e618` | 行为不变（仅私有 helper 的测试引用改指新模块）；CI run `42166148422` 绿 |
| 2026-11-06 | 阶段 4 · B7 | 步骤 1：release.yml 增加 test job（build needs test）；build.yml prepare 校验 tag 在 main 祖先 + numeric 版本；guard 测试 | `43f0342` | 关闭 CI 审计唯一 P1（TD-CI-001）与 TD-CI-003；发布方式仍为直接正式发布；CI run `42166713950` 绿 |
| 2026-11-06 | 阶段 4 · B7 | 步骤 2：`tests/test_update_winhttp.py` 16 个用例，fake WinHTTP 覆盖调用序列/选项/头解析/分块读取/全部失败路径 | 待提交 | 关闭 TD-TEST-002（唯一网络代码零行为测试） |
| 2026-11-06 | 阶段 5 | P5-1/P5-2/P5-4：pytest-cov 基线 89%（floor 门禁 + ratchet）；check.py pytest 带覆盖率；CI 单矩阵项执行 | 待提交 | 关闭「覆盖率目标与团队成本」遗留；UI 密度补测继续 |
| 2026-11-06 | 阶段 4 · B4 | 步骤 1：camera 拆出 camera_devices（205），camera 708→568；测试 seam 改指新模块 | `58e3743` | 行为不变；CI 见本轮汇总 |
| 2026-11-06 | 阶段 4 · B5 | model/calibration/store 三拆（model 959→429+191+361；calibration 670→418+329；store 731→522+322）；B6 核对无需拆分 | `a85eeb7` | 行为不变（测试未改，仅 seam）；所有文件 <600 行 |
| 2026-11-06 | 阶段 5 · §14 | 蓝图归档为 `completed/harness-2026-11-06.md`；R1–R8/熵管理进 core-beliefs §11；引用与索引更新 | 单独 docs 提交 | §11.1 十项全 ✅；移除自检通过 |
| 2026-11-06 | 债务清理 · P1 | TD-DOC-013：文档门禁补锚点解析、命令脚本存在性、CHANGELOG 版本新鲜度，均带失败负例 | 待提交 | 文档注入漂移会被 pytest 拦截 |
| 2026-11-06 | 债务清理 · 安全 | TD-SEC-003/005/007：WinHTTP 设置失败 fail-closed；无私有运行目录禁用命令通道；日志/跟踪文件 0600、目录 0700 | 待提交 | 三个 P2 关闭，均有负例/权限断言 |
| 2026-11-06 | 债务清理 · 测试质量 | TD-TEST-004/010/013 关闭、005/006 收窄：具体断言、`python -m` 入口 smoke、pytest strict + 120 s 超时 | 待提交 | 3086 passed / 55 skipped |
| 2026-11-06 | 债务清理 · 工具/供应链 | TD-SEC-010（fetch_models 默认 verify-only）、TD-CI-030（ruff 重复 ignore）关闭 | 待提交 | 默认不联网有测试；ruff 全绿 |
| 2026-11-06 | 债务清理 · CI/文档 | TD-CI-011（lint 不装 build 组）、TD-DOC-015（CONTRIBUTING 命令去重）关闭 | 待提交 | lint job 更轻；文档三角色分工明确 |
| 2026-11-06 | 债务清理 · 测试/文档 | TD-TEST-011（overlay 直测）、TD-DOC-011（双 README 性能数值漂移门禁）关闭 | 待提交 | 10 个新 overlay 用例；文档门禁 21 用例 |
| 2026-11-06 | 债务清理 · 架构/CI | TD-ARCH-001 关闭（reading 下沉 foundation）；TD-DOC-012；TD-CI-029/032/016 关闭，TD-CI-012 收窄 | 待提交 | 包级 engine↔panes 环消失；CI 守卫 4 项 |
| 2026-11-06 | 债务清理 · 安全/隐私 | TD-SEC-008/013/014 关闭：doctor 全路径脱敏、build_site 输出目录保护、隐私文档披露相机占用检查 | 待提交 | 58 相关测试通过；隐私门 OK |
| 2026-11-06 | 债务清理 · 版本 | TD-CI-004 关闭：版本解析统一到 `scripts/version.py`（AST + numeric + tag 校验），CI/AppImage 改用，14 个新用例 | 待提交 | YAML/bash 语法 OK；版本脚本测试全绿 |
| 2026-11-06 | 债务清理 · P1 文件拆分 | TD-CPLX-006 收窄（facemesh 1454→954 + 3 模块）；TD-CPLX-007 收窄（app 1317→926 + app_startup 594） | 待提交 | 行为不变（断言未改，仅 seam）；类本身待按方法拆 |
| 2026-11-06 | 债务清理 · P1/P2 | TD-CPLX-015 关闭（VisionWorker 693→508 + lifecycle mixin）；TD-PERF-002 关闭（下载 deadline/最低速率） | 待提交 | 62 worker 测试全绿；43 fetch 测试全绿 |
| 2026-11-06 | 债务清理 · 安全 | TD-SEC-012 关闭：FFmpeg 本地容器限制 protocol_whitelist，m3u8 探针无网络请求 | 待提交 | 94 camera 测试全绿 |
| 2026-11-06 | 债务清理 · P1 类拆分 | TD-CPLX-002（SettingsDialog 1351→885 + settings_pages 595）/006（类 692→441 + landmarks/output mixins）大幅收窄；015 已关闭 | 待提交 | UI/facemesh/architecture 测试全绿；无新 size 违规 |
| 2026-11-06 | 债务清理 · 测试/CI | TD-TEST-005 关闭（红榜弱断言已具体化）；TD-CI-017 收窄（依赖漏洞审计 job） | 待提交 | 本地 pip-audit 无已知漏洞 |
| 2026-11-06 | 债务清理 · P1 类拆分收尾 | TD-CPLX-002 关闭（SettingsDialog 885→410 + settings_builders 533）；TD-CPLX-005 关闭（CalibrationWindow 849→382 + flow/result mixins） | 待提交 | 197 UI/architecture 测试全绿；无新 size 违规 |
| 2026-11-06 | 债务清理 · 架构环 | TD-ARCH-006 关闭：更新错误类下沉 `update/errors.py`，fetch↔winhttp 环消失 | 待提交 | 架构测试 ALLOWED_CYCLES 为空仍全绿 |
| 2026-11-06 | 债务清理 · 更新安全 | TD-SEC-002 关闭：安装器 sidecar 校验、启动前哈希复核、目录/符号链接限制 | 待提交 | 42 installer/service 测试全绿 |
| 2026-11-06 | 债务清理 · P1 大件 | Controller 3073→2535（类 2751→2216，hotkeys/screens mixins）；app.py 926→673（类 733→479）；facemesh 619→541（类 363，process 45）→ TD-CPLX-006/007 关闭，001 收窄 | 待提交 | 219 controller/app + 141 facemesh 测试全绿 |
| 2026-11-06 | 债务清理 · 安全/CI | TD-SEC-004 关闭（IPC 身份未知 fail-closed）；TD-CI-027/028 关闭（pages 锁定依赖、404 与 API 错误区分+重试） | 待提交 | ipc/build-script 测试全绿 |
| 2026-11-06 | 债务清理 · P1 收尾 | TD-CPLX-001 关闭：controller.py 3418→1992（-41.7%），Controller 2845→1692，panes/calibration mixins 拆出；P1 仅剩 SEC-001（等证书） | 待提交 | 232 controller/app/architecture 测试全绿 |
| 2026-11-06 | 债务清理 · CI | TD-CI-020 关闭：新增 workflow lint gate（PyYAML 结构检查，pins/timeout/injection）并接入本地+CI | 待提交 | 12 gate（quick 11）；9 个负例测试 |
| 2026-11-06 | 债务清理 · 依赖 | TD-SEC-009 关闭：运行时依赖主版本上界 + lock 刷新 + 守卫测试 | 待提交 | 187 build-script 测试全绿 |
| 2026-11-06 | 债务清理 · P2 批量 | TD-CPLX-008/012、TD-TEST-006/009、TD-CI-019/020、TD-SEC-009 关闭；mypy 扩展 tests、workflow lint、确定性时序测试与 FakeController 契约 | 待提交 | 见各任务测试证据 |
| 2026-11-06 | 阶段 1 | 提交阶段 1 产出（不含业务代码） | `093cfaf` | 计划状态=待确认；等 H 确认并回答 D1–D17 后再进入阶段 2 |

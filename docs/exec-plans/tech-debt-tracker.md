# 技术债台账（Harness 阶段 1）

- 快照日期：2026-11-06
- 代码基线：`harnessing` 分支，起点 commit `9644527`（= `main` = tag `v0.3.0`）
- 来源：机械扫描（`scripts/analysis/scan_metrics.py`）+ 三路只读审计
  （CI/构建发布、测试与文档、安全与性能）+ 人工复核
- 字段定义与优先级：见 `docs/turn-project-to-harness-rules.md` 附录 B
  - **P0**：影响正确性/安全性/可用性，或阻塞后续所有改造 → 当前批次内必须解决
  - **P1**：显著抬升维护成本或缺陷概率，不立即阻断 → 计划内排期
  - **P2**：体验与整洁度问题 → 有空再做，登记不丢
- 状态：未开始 / 处理中 / 已完成 / 已豁免（含理由）
- 关联批次见 `docs/exec-plans/active/harness-migration.md` §4；**P0 = 0 条**，
  因为本仓库基础门禁（lint/类型/测试/隐私/发布 guard）已全部就绪，没有阻塞项。

## 汇总

| 类别 | 条目数 | P0 | P1 | P2 |
|---|---:|---:|---:|---:|
| ARCH（架构） | 6 | 0 | 3 | 3 |
| CPLX（复杂度） | 13 | 0 | 7 | 6 |
| DUP（重复） | 2 | 0 | 0 | 2 |
| DOC（文档） | 16 | 0 | 5 | 11 |
| TEST（测试） | 16 | 0 | 4 | 12 |
| SEC（安全） | 14 | 0 | 1 | 13 |
| PERF（性能/可靠性） | 4 | 0 | 0 | 4 |
| CI（门禁/流水线） | 33 | 0 | 1 | 32 |
| META（过程/元） | 2 | 0 | 0 | 2 |
| **合计** | **106** | **0** | **21** | **85** |

归并规则：三份原始审计共 97 条（CI 51 / 测试与文档 29 / 安全 17），与机械扫描、人工复核合并后
按**同一根因**归并为 105 条（含少量本次新增）；原始报告完整归档在
`docs/exec-plans/active/audits/`（`ci-audit.md`、`quality-audit.md`、`risk-audit.md`、
`scan-metrics.md`、`local-gates-baseline.txt`）。
唯一 P1 安全项：TD-SEC-001（更新器执行无 Authenticode/Sigstore 校验的远端安装器，需签名基建，
见计划 D14）；唯一 P1 CI 项：TD-CI-001（tag 发布不依赖测试）。

---

## ARCH · 架构

| ID | 标题 | 位置 | 严重度 | 证据 | 修复建议 | 业务行为影响 | 批次 / 验收 | 状态 |
|---|---|---|---|---|---|---|---|---|
| TD-ARCH-001 | `engine` 与 `panes` 包级双向依赖 | `engine/controller.py:106-116`；`panes/decider.py:44` | P1 | controller 依赖 panes.*；panes.decider 反向 import `engine.reading`；机械扫描包边 engine→panes 5、panes→engine 1 | 把纯规则 `reading` 下沉到共享层，或规定 panes 只依赖 engine 的纯接口子模块；结构测试断言一条方向 | 无 | B1/B6；`tests/test_architecture.py` 断言无环 | 未开始 |
| TD-ARCH-002 | ~~18 模块强连通分量（app/cli/diagnostics/hotkeys/ui）~~ 已消除 | `app.py:54`、`cli.py:479,490`、`diagnostics.py:40`、`hotkeys.py:2685`、`ui/settings_dialog.py:68`、`ui/tray.py:23` | P1 | `scan_metrics.py` SCC 输出：app↔cli、cli↔diagnostics、diagnostics→hotkeys→cli、app→ui→cli→app | 拆“组合根”职责：`cli` 只解析/格式化，`app` 依赖 `cli` 而非反向；`hotkeys` 的文案改由调用方注入；逐步缩小 SCC | 无 | B1；SCC 成员数单调下降并记录基线 | 已完成（阶段 4 B1，2026-11-06） |
| TD-ARCH-003 | 无机械依赖方向约束 | 全仓（无 `tests/test_architecture.py`，无 import-linter 配置） | P1 | `CONTRIBUTING.md:17-21` 用文字规定“纯逻辑不得 import Qt/OpenCV”；无测试/CI 检查；例外只靠代码注释（如 `hotkeys.py:2701` 的 “the cli module imports nothing from here”） | 阶段 3 加结构测试：模块→允许层的白名单 + 现有例外清单（只减不增）；报错信息给出目标层与替代方案 | 无 | B0/阶段 3；注入违规必须失败 | 已完成（阶段 3，2026-11-06） |
| TD-ARCH-004 | 适配器反向依赖 CLI：`platform/hotkeys.py` → `cli` | `platform/hotkeys.py:2685` | P2 | `from ..cli import cli_command_text`，只为生成快捷键提示文案；platform 层因此依赖表示/入口层 | 把 `cli_command_text` 移到 `types`/`paths` 或由调用方传入；结构测试禁止 platform→cli | 无 | B2；结构测试断言 | 已完成（阶段 4 B1，2026-11-06） |
| TD-ARCH-005 | UI 反向依赖 CLI：settings/tray → `cli.cli_command_text` | `ui/settings_dialog.py:68`、`ui/tray.py:23` | P2 | UI 为了展示命令文本 import CLI 模块；加深 ui→cli→app→ui 环 | 同 TD-ARCH-004：文案生成下沉到无依赖的小模块 | 无 | B3；结构测试断言 | 已完成（阶段 4 B1，2026-11-06） |
| TD-ARCH-006 | `update.fetch` ↔ `update.winhttp` lazy 循环 | `update/fetch.py:76`；`update/winhttp.py` | P2 | fetch 在函数内 import winhttp（默认 transport），winhttp 反向 import fetch 的类型/常量；SCC 2 模块 | 抽 `update/transport.py` 放协议与异常类型，两侧单向依赖 | 无 | B7；SCC 消失 | 未开始 |

## CPLX · 复杂度

| ID | 标题 | 位置 | 严重度 | 证据 | 修复建议 | 业务行为影响 | 批次 / 验收 | 状态 |
|---|---|---|---|---|---|---|---|---|
| TD-CPLX-001 | `engine/controller.py` 超大：单类 2,845 行 / 155 方法 | `engine/controller.py:585` | P1 | 文件 3,429 行；`Controller.__init__` 206 行（`:659`）；`start` 75 行（`:867`）；`apply_settings` 63 行（`:1237`） | 先补特征测试；按生命周期抽取协作对象（相机/会话、热键、窗口记忆、更新），controller 只保留编排 | 无（重构） | B1；controller 行数下降 ≥30%，测试全绿 | 处理中（B1 进行中：controller.py 3418→3073，Controller 类 2845→2751） |
| TD-CPLX-002 | `ui/settings_dialog.py` 超大：单类 1,351 行 / 69 方法 | `ui/settings_dialog.py:669` | P1 | 文件 2,029 行；`SettingsDialog` 69 方法；UI 测试仅 ~29 例 | 按页签拆子组件（General/Camera/Privacy/Hotkeys…），状态读写集中到 presenter | 无 | B3；类行数 <600，offscreen 测试全绿 | 未开始 |
| TD-CPLX-003 | `platform/hotkeys.py` 2,740 行装三平台实现 | `platform/hotkeys.py`（`_Carbon:1499`、`X11HotkeyManager:2257`、Windows 类 ~955） | P1 | 文件 2,740 行；`register` 62 行 / 17 决策点（`:498`）；Windows `__init__` 81 行（`:955`） | 拆 `hotkeys/{windows,macos,x11,base}.py`，公共解析/格式化留一处 | 无 | B2；三平台测试全绿；文件 <600 行 | 已完成（阶段 4 B2，2026-11-06） |
| TD-CPLX-004 | `platform/linux.py` 超大：`LinuxPlatform` 945 行 / 53 方法 | `platform/linux.py:1580` | P1 | 文件 2,528 行；`_EwmhClient` 273 行 / 24 方法（`:707`）；`capabilities` 33 行 / 18 决策点（`:1708`） | 拆 X11 窗口管理 / 光标 / DPMS / 热键后端；Wayland 分支单独文件 | 无 | B2；Linux 测试全绿 | 未开始 |
| TD-CPLX-005 | `ui/calibration_window.py` 超大：类 849 行 / 52 方法 | `ui/calibration_window.py:918` | P1 | 文件 1,766 行；`_Surface` 315 行（`:600`）；`set_running` 47 行 / 19 决策点（`:664`） | 拆校准状态机（可测纯逻辑）与渲染 surface；采集/拟合/结果分文件 | 无 | B3；UI 测试补校准失败/重试路径 | 未开始 |
| TD-CPLX-006 | `vision/backends/facemesh_backend.py` 1,454 行，`process` 77 行 / 25 决策点 | `facemesh_backend.py:759`、`:921` | P1 | 类 692 行 / 25 方法；`process` 25 决策点；模型加载自检逻辑同文件 | 拆“模型加载/自检”“单帧推理”“后处理”；保持帧循环接口不变 | 无 | B4；bench 前后数值不退化 | 未开始 |
| TD-CPLX-007 | `app.py` 与 `EyeTrackerApp` 过大 | `app.py:380`、`:195` | P1 | 文件 1,317 行；类 733 行 / 54 方法；`build_app` 128 行 / 24 决策点 | 拆启动装配（build_app）与运行期窗口管理；单实例/登录启动逻辑独立 | 无 | B1；`tests/test_app.py` 全绿 | 未开始 |
| TD-CPLX-008 | `diagnostics._problems` 108 行 / 58 决策点（全仓最高） | `diagnostics.py:814` | P2 | 机械扫描：decisions=58，lines=108，nesting=3 | 拆成每个诊断维度一个小函数，规则表驱动；输出顺序由测试锁定 | 无 | B7；决策点 <20 | 未开始 |
| TD-CPLX-009 | 三个状态机 `update()` 偏长：guard/decider/decision | `engine/guard.py:154`（30/108）、`panes/decider.py:261`（27/87）、`engine/decision.py:256`（22/98） | P2 | 机械扫描决策点与行数 | 把“宽限期/迟滞/防抖”提为共享小状态机或各自拆成 `_should_*` 判定；行为由现有测试锁定 | 无 | B1/B6；决策点下降 | 未开始 |
| TD-CPLX-010 | `parse_face_geometry` 嵌套 7 层 | `vision/backends/face_geometry.py:108` | P2 | 43 行、24 决策点、max nesting=7 | 每层解析拆函数，失败返回结构化错误；模型包格式测试锁定 | 无 | B4；嵌套 ≤3 | 未开始 |
| TD-CPLX-011 | `GazeModel.from_dict` 64 行 / 27 决策点 | `gaze/model.py:478`；类在 `:203` | P2 | 机械扫描；旧模型兼容分支集中在一个函数 | 按版本/字段逐项解析，未知字段策略显式化；旧文件回归测试 | 无 | B5；旧校准文件测试全绿 | 未开始 |
| TD-CPLX-012 | `config._coerce` 36 行 / 22 决策点 | `config.py:588` | P2 | 机械扫描 | 每种设置类型的强制转换表驱动；非法值回归测试 | 无 | B1；测试全绿 | 未开始 |
| TD-CPLX-013 | 热键模块拆分后仍有 3 个文件 >600 行 | `hotkeys_base.py` 823、`hotkeys_macos.py` 722、`hotkeys_x11.py` 698 | P2 | 机械扫描；这是 B2 从 2,740 行拆分后的自然结果 | 按职责再拆（例如 base 的解析 vs 管理器记账；macOS 的 Carbon 绑定 vs 布局探测）；优先级低于其他批次 | 无 | B2 后续；文件降到 600 以下 | 未开始 |

## DUP · 重复

| ID | 标题 | 位置 | 严重度 | 证据 | 修复建议 | 业务行为影响 | 批次 / 验收 | 状态 |
|---|---|---|---|---|---|---|---|---|
| TD-DUP-001 | `_guard` 近似重复（显示器切换 vs 分屏切换） | `engine/decision.py:418`；`panes/decider.py:403` | P2 | 21 行，相似度 0.92（机械扫描）；语义是两个不同守卫 | 抽公共“宽限期守卫”小工具，或保留并在两处注释互指，说明为何不合并 | 无 | B6；决定后记录理由 | 未开始 |
| TD-DUP-002 | 无重复度门禁，扫描器只手工运行 | `scripts/analysis/scan_metrics.py`；CI 无调用 | P2 | 扫描器本次才新增；CI 无重复检查步骤；同模块内重复未纳入 | 阶段 3/5 把重复度纳入快照对比（只报告新增重复，不阻塞存量）；扩展同模块扫描 | 无 | B0；基线入库 | 未开始 |

## DOC · 文档

| ID | 标题 | 位置 | 严重度 | 证据 | 修复建议 | 业务行为影响 | 批次 / 验收 | 状态 |
|---|---|---|---|---|---|---|---|---|
| TD-DOC-001 | “零网络”绝对表述与 0.3.0 opt-in 更新检查自相矛盾 | `README.md:37-38`、`README.tr.md:37-38`、`docs/architecture.md:57` vs `README.md:77-78,139`、`docs/privacy.md:10,30-42` | P1 | 同仓库两处互斥；徽章已写 “off by default”，正文未改；质量审计 D.2 确认 | 统一为 “no network by default; the only opt-in exception is the update check (Windows)”；双语同步 | 无（文档） | B0；`tests/test_docs.py` 新增一致性断言 | 已完成（阶段 2，2026-11-06） |
| TD-DOC-002 | `SECURITY.md` 支持版本停在 0.1.x | `SECURITY.md:12` | P2 | 当前 `__version__ = 0.3.0`（`src/eye_tracker/__init__.py:3`），`git describe` = v0.3.0 | 改为 “latest release only” 或 0.3.x；纳入版本一致性测试 | 无 | B0；测试断言 | 已完成（阶段 2，2026-11-06） |
| TD-DOC-003 | 隐私文档 User-Agent 示例硬编码 0.2.1 | `docs/privacy.md:41`；`src/eye_tracker/update/winhttp.py:33` | P2 | 代码 `f"EyeTracker/{__version__}"`，运行时 0.3.0 | 用 `<version>` 占位或由生成脚本替换；纳入版本一致性测试 | 无 | B0 | 已完成（阶段 2，2026-11-06） |
| TD-DOC-004 | 构建/发布示例停在 0.2.0 / 0.1.0 | `docs/building.md:64-66,74-75,96-97,110,186`；`scripts/smoke_test_bundle.py:9` | P2 | 8 处 0.2.0 + 1 处 0.1.0 | 改 `<version>` 占位或当前版本；示例命令纳入测试 | 无 | B0 | 已完成（阶段 2，2026-11-06） |
| TD-DOC-005 | `docs/architecture.md` code map 缺 `update/` 等 | `docs/architecture.md:194-215` | P2 | 全文 0 次 “update”；缺 `update/`（1,037 行）、`paths.py`、`logging_setup.py`、`trace.py` | 阶段 2 重写 ARCHITECTURE 时补全，并描述更新链路的线程/网络边界 | 无 | B0 | 已完成（阶段 2，2026-11-06） |
| TD-DOC-006 | `docs/troubleshooting.md` 在仓库文档中零入链 | `docs/troubleshooting.md:1`；`site/index.html:455` | P2 | 入链统计 = 0（仅站点页脚） | README FAQ / platform-support / AGENTS.md 加入链；docs/index.md 索引 | 无 | B0 | 已完成（阶段 2，2026-11-06） |
| TD-DOC-007 | 缺 `AGENTS.md` 入口地图 | 仓库根 | P1 | `test -e AGENTS.md` = false；规则 §2.1 必需项 | 阶段 2 按 §2.2 建 ~100 行：定位/命令/目录地图/硬约束/延伸阅读 | 无 | B0；≤200 行 + 链接检查 | 已完成（阶段 2，2026-11-06） |
| TD-DOC-008 | 缺根级 `ARCHITECTURE.md` | 仓库根 | P1 | 根不存在；`docs/architecture.md` 已有 215 行可迁移/引用 | 建根地图（域 + 分层 + 依赖方向规则），细节页保留 | 无 | B0；必含节测试 | 已完成（阶段 2，2026-11-06） |
| TD-DOC-009 | 缺 `docs/QUALITY_SCORE.md` / `docs/index.md` / `docs/design-docs/`（含 core-beliefs） | `docs/` | P1 | 四个路径均不存在；规则 §2.1 要求 exec-plans + QUALITY_SCORE，§2.1/§2.2 要求索引与 core-beliefs | 阶段 2 建索引、质量评分（按 8 个业务域）、`core-beliefs.md`；`QUALITY_SCORE` 与台账 ID 关联 | 无 | B0；文件存在 + 必含节测试 | 已完成（阶段 2，2026-11-06） |
| TD-DOC-011 | 双 README 的 PERF 数据块手工重复 | `README.md:149-160`；`README.tr.md:151-162` | P2 | 数值一致但无生成/校验；`<!-- PERF:BEGIN/END -->` 无脚本引用 | 由 benchmark 结果生成两语言块，或加测试比较数值 | 无 | B4 | 未开始 |
| TD-DOC-012 | 站点文档链接是绝对 GitHub `main` URL，无校验 | `site/index.html:248,352,360,455`；`tests/test_site.py:82-100` | P2 | 站点测试只查构建产物本地 src/href；docs 重组会静默断链 | 站点构建/测试把 `blob/main/docs/*.md` 映射回仓库路径校验存在 | 无 | B7 | 未开始 |
| TD-DOC-013 | 文档门禁只有 6 个用例，缺版本/锚点/命令/新鲜度校验 | `tests/test_docs.py:26,31,47,62,87,98` | P1 | 只校验配置同步、hotkey 渲染、presence 数字、setting 有 doc、相对链接/图片存在；`:91` 丢弃 anchor；D.2 的过期内容全部漏检 | 阶段 2/3 扩展：版本一致性、锚点、代码块命令、双语结构、必填节、docs/index 完整性；加 markdownlint/外链（定期） | 无 | B0/阶段 3；注入漂移必须失败 | 处理中（阶段 2 已扩展 15 用例；锚点/外链/markdownlint 留阶段 3） |
| TD-DOC-014 | 施工期规则文档（1,101 行）被 `docs/*.md` 门禁 glob 纳入产品文档检查 | `docs/turn-project-to-harness-rules.md`；`tests/test_docs.py:75` | P2 | 施工期文档 73 KB，被当作产品页做链接检查；阶段 5 后按 §14 归档 | 归档路径定在 `docs/exec-plans/completed/harness-<date>.md`；在文档门禁中显式排除归档目录 | 无 | 收官 | 未开始 |
| TD-DOC-015 | `CONTRIBUTING.md` 与未来 AGENTS/ARCHITECTURE 内容重叠 | `CONTRIBUTING.md:57-77`（Checks）、`:80-100`（Project layout） | P2 | 检查命令与目录地图将出现在 AGENTS.md；双份必然漂移 | AGENTS.md 作为唯一入口，CONTRIBUTING 链接过去；保留人类贡献流程细节 | 无 | B0 | 处理中（CONTRIBUTING 已指向 AGENTS；内容去重留阶段 4） |
| TD-DOC-016 | 缺 `prompt-history.txt`（规则 §6.3）且 `.gitignore` 未忽略 | 仓库根；`.gitignore` | P2 | 文件不存在；`.gitignore` 无条目 | 阶段 2 按 D6 决策启用：创建文件、加 `.gitignore`、AGENTS 不引用 | 无 | B0 | 已完成（阶段 2，2026-11-06） |
| TD-DOC-017 | 无 Markdown/外链/文档新鲜度 CI | `ci.yml`（无 markdown 步骤） | P2 | 唯一文档门禁是 6 个 pytest 用例 | 阶段 3 加 markdownlint + 外链定期检查 + 文档园丁任务说明 | 无 | 阶段 3 | 未开始 |

## TEST · 测试

| ID | 标题 | 位置 | 严重度 | 证据 | 修复建议 | 业务行为影响 | 批次 / 验收 | 状态 |
|---|---|---|---|---|---|---|---|---|
| TD-TEST-001 | 无覆盖率工具、配置与门禁 | `pyproject.toml:71-76`；`ci.yml:118-119` | P1 | `coverage` 未安装（ModuleNotFoundError）；dev 组仅 pytest/ruff/mypy；CI 仅 `pytest -q`，无 `--cov`/阈值 | 加 pytest-cov；先记录基线再 ratchet；CI 阈值门禁（阈值见 D1） | 无 | 阶段 5；注入低覆盖必须失败 | 未开始 |
| TD-TEST-002 | `update/winhttp.py` 零行为测试 | `src/eye_tracker/update/winhttp.py:1-175`；`tests/test_update_fetch.py:12-14`；`tests/test_privacy.py:90-97` | P1 | 唯一网络代码，175 行 ctypes/WinHTTP；测试只用 FakeTransport，privacy 测试只扫 allowlist 文本 | 仿 `test_platform_windows.py` 的 FakeWin32，用假 `ctypes.WinDLL` 覆盖 TLS/证书/代理/重定向/错误码 | 无 | B7；新增测试 | 未开始 |
| TD-TEST-003 | Windows 专属与 Inno Setup 用例在本地/PR CI 跳过 | `tests/test_build_scripts.py:572`；`ci.yml:78-96`；`build.yml:290` | P2 | 55 skip 中 33 个 Windows；PR test job 不装 Inno Setup；build job 装 choco 但不跑 pytest | bundle/额外 Windows job 装 Inno Setup 后跑该用例；或标注仅 release 验证 | 无 | B7 | 未开始 |
| TD-TEST-004 | 3 个用例完全没有断言 | `tests/test_facemesh.py:411`；`tests/test_hotkeys.py:899`；`tests/test_update_service.py:238-244` | P2 | AST 扫描 1,994 个测试函数 → 3 个零 assert/零 raises | 至少断言状态或副作用 | 无 | 阶段 5 | 未开始 |
| TD-TEST-005 | 9 个用例仅有弱断言（is not None/isinstance） | `tests/test_hotkeys.py:350`；`tests/test_platform_windows.py:355,359`；`tests/test_backends.py:115` 等 | P2 | AST 扫描 | 断言具体值/类型分支/副作用 | 无 | 阶段 5 | 未开始 |
| TD-TEST-006 | 时序/睡眠依赖 + 无 pytest-timeout | `tests/test_worker.py:246,345,439`；`tests/test_ipc.py:498,504`；`tests/test_store.py:710-740`；`pyproject.toml:117-120` | P2 | 8 个测试含 `time.sleep`；墙钟上界 0.25 s/3 s；无 timeout 配置，仅 job 级 30 min | 启用 pytest-timeout；用 fake clock/Event 替代 sleep；放宽紧上界 | 无 | 阶段 5 | 未开始 |
| TD-TEST-007 | 性能声明无回归测试 | `README.md:55,149-160`；`tests/test_motion.py:306-320` | P2 | 只有 motion 微基准（<2 ms/次）；PERF 块无生成器/测试引用 | `eye-tracker bench --json` 记趋势（nightly），不硬卡阈值（见 D7） | 无 | 阶段 5 / B4 | 未开始 |
| TD-TEST-008 | UI 测试密度最低 | `src/eye_tracker/ui/`（8,229 行）；`tests/test_ui_dialogs.py:335-860` | P2 | ui = 207 用例/8,229 行 = 25.2/千行；其他包 74-117/千行；settings 29 例、calibration 11 例、wizard 14 例 | 补设置保存/非法输入/权限变化、校准失败重试、向导取消回滚 | 无 | B3 | 未开始 |
| TD-TEST-009 | FakeController 与真实 Controller 无契约测试 | `tests/test_ui_dialogs.py:79-150`；`tests/test_app.py:230-247` | P2 | fake 手工复刻信号/方法；真实 Controller 155 方法/192 个 def；app 集成只验窗口开关 | 加契约测试：fake 覆盖真实公开 API 子集（签名存在性/子集断言），或部分 UI 用例用真实 Controller | 无 | B3 | 未开始 |
| TD-TEST-010 | `python -m eye_tracker` 入口无测试 | `src/eye_tracker/__main__.py:1-8` | P2 | 无测试引用 `__main__`；`tests/test_cli.py:281` 只断言程序名 | 子进程 smoke：`python -m eye_tracker --version` / `doctor --json` | 无 | 阶段 5 | 未开始 |
| TD-TEST-011 | `vision/backends/overlay.py` 无直接单测 | `src/eye_tracker/vision/backends/overlay.py:26-94`；`tests/test_backends.py:321-330` | P2 | 6 个公开函数无测试 import，仅经 `backend.annotate()` 间接断言像素变化 | 补灰度/尺寸/边界测试 | 无 | B4 | 未开始 |
| TD-TEST-012 | 人脸精度声明的测试从不自动运行 | `tests/conftest.py:44-52`；`docs/privacy.md:24-26`；workflows | P2 | `EYE_TRACKER_TEST_FACE` 未在任何 workflow 设置 → 10 个人脸用例永远 skip | 提交可公开测试图或合成图；或 nightly 用 secret 提供并标注 | 无 | 阶段 5（见 D7） | 未开始 |
| TD-TEST-013 | pytest 配置无 strict/超时，全局吞弃用警告 | `pyproject.toml:117-120` | P2 | `addopts="-ra"`；无 `--strict-markers`；`ignore::DeprecationWarning` 全局 | 加 strict/timeout；只 ignore 已审阅的具体警告 | 无 | 阶段 5 | 未开始 |
| TD-TEST-014 | 无结构测试（依赖方向/大小/环） | 全仓 | P1 | 无 `tests/test_architecture.py`；CI 无 import 图检查；CONTRIBUTING 的约束仅文字 | 阶段 3 加结构测试 + 大小 lint；存量豁免清单只减不增 | 无 | 阶段 3；注入违规必须失败 | 已完成（阶段 3，2026-11-06） |
| TD-TEST-015 | 除隐私门外，其他门禁无“注入违规必须失败”自证 | `tests/test_privacy.py`（有自测）vs 其他 | P1 | 规则 §11.1 DoD 4 要求每条不变量实测可拦截；目前只有 privacy gate 有参数化注入测试 | 为 ruff/mypy/覆盖率/结构测试各加一个负例自测（fixture 故意违规→断言失败） | 无 | 阶段 3/5 | 处理中（大小/结构/文档负例已加；覆盖率负例留阶段 5） |
| TD-TEST-016 | 测试文件本身超大 | `tests/test_controller.py:3289`；`test_hotkeys.py:2660`；`test_build_scripts.py:2597`；`test_platform_linux.py:2475` | P2 | wc -l | 按行为拆分测试文件；与源码拆分同步进行 | 无 | B1/B2 | 未开始 |

## SEC · 安全

> 来源：`/tmp/harness-scan/risk-audit.md`（17 条原始发现，归并为下列 14 条；已去重文档类问题，
> 见 DOC 节）。正面基线：无硬编码凭据、无 `.env` 读取、无 shell=True/os.system/eval/exec/pickle、
> 无崩溃上报 SDK、无帧落盘；`subprocess` 全部 argv 列表 + 默认 `shell=False` + DEVNULL/stdin；
> settings/calibration 写入为 0600（mkstemp+replace）。

| ID | 标题 | 位置 | 严重度 | 证据 | 修复建议 | 业务行为影响 | 批次 / 验收 | 状态 |
|---|---|---|---|---|---|---|---|---|
| TD-SEC-001 | 自更新下载的安装器无 Authenticode/Sigstore 校验即被静默执行 | `update/installer.py:106-126,134-149`；`docs/privacy.md:69-73` | P1 | 仅比对同一 release 的 `SHA256SUMS.txt`；随后 `Popen(exe, /VERYSILENT)`；能替换 release 两个资产（或注入响应）者即可获得当前用户权限；文档自述该边界 | 用 WinVerifyTrust 校验 Authenticode；或内置发布公钥校验 Sigstore/对 `SHA256SUMS` 独立签名（需签名基建，见 D14） | 无（安全加固） | B7 + D14；伪造校验失败用例必须拒绝执行 | 未开始 |
| TD-SEC-002 | 哈希校验与启动安装器之间存在 TOCTOU 窗口 | `update/installer.py:124-131`；`update/service.py:312-343` | P2 | 校验后 `part.replace(final)`，再经 Qt 队列跳回主线程才启动；期间本机进程可替换文件，启动前无二次校验 | 启动前对同一路径重新计算 SHA-256（或验签）；收紧下载目录 ACL、拒绝 reparse point；缩短“校验→执行”路径 | 无 | B7；可注入 launcher 的测试 | 未开始 |
| TD-SEC-003 | WinHTTP 安全/超时设置返回值被忽略 | `update/winhttp.py:143,156-159` | P2 | `WinHttpSetTimeouts` 与 `WinHttpSetOption(REDIRECT_POLICY, NEVER)` 的 BOOL 均未检查；设置失败则“不自行重定向”的安全假设无运行时保证，超时可能退化 | 检查返回值，失败即抛 `NetworkError`（fail closed）；mock WinHTTP 覆盖失败路径 | 无 | B7；测试 | 未开始 |
| TD-SEC-004 | IPC 身份校验在无法判定时默认放行（fail-open） | `ipc.py:739-756,877-908` | P2 | `_client_is_trusted` 非 Windows 恒 True；Windows 句柄异常时 `verdict=None` 仍 `return True`；pipe 探测异常按“空闲”处理 | 身份不可判定时拒绝连接并告警；异常时拒绝 listen 并提示；Unix 补 `SO_PEERCRED`/目录归属复核 | 修复bug（异常路径） | B1；异常路径测试 | 未开始 |
| TD-SEC-005 | Linux 私有运行目录不可用时回退共享 `/tmp` 固定名 socket | `ipc.py:116-128,139-164,436-493,749-750` | P2 | 私有目录校验失败返回 `None` 后仍用固定名；服务端仅 `UserAccessOption`，Unix 无对端凭据校验；另一本地账户可预占 socket 名 | 私有目录不可用时 fail closed（禁用 ctl 并提示），或 `mkdtemp(0700)`；Unix 加 `SO_PEERCRED` | 修复bug | B1；测试 | 未开始 |
| TD-SEC-006 | 隐私门禁的覆盖边界与“任何网络 import/call”文案不符 | `check_privacy.py:140-144,164-165,631`；`README.md:139`；`docs/privacy.md:10`；`SECURITY.md:49-55` | P2 | 门禁自述看不到运行时拼接的模块名；默认只扫 `src/eye_tracker`；规则未覆盖 `eval/exec`、`getattr/__import__`、`openUrl`/`os.startfile` 的 URL 形态 | 增加上述规则与常量折叠；扫描范围纳入会进 bundle 的 `scripts/`/`packaging/`；文案改为“静态扫描 + bundle 扫描”（见 D16） | 无 | 阶段 3；新增规则自测 | 未开始 |
| TD-SEC-007 | 日志与 `--trace` 文件默认 0644（settings/calibration 为 0600） | `logging_setup.py:90-104`；`trace.py:36-54`；`config.py:463-476` | P2 | RotatingFileHandler/TraceWriter 未指定权限；本机实测 0o644；日志含未捕获异常 traceback（home 绝对路径），trace 含指针/凝视数值 | 创建后 `os.chmod(0o600)` 或 `os.open(...,0600)`；目录 0700；文档说明本地可见性 | 无 | B1/阶段 5；权限断言测试 | 未开始 |
| TD-SEC-008 | doctor 报告只脱敏 home，非 home 绝对路径进入公开 bug 报告 | `diagnostics.py:1043-1075,1078-1093,677-687,711-727` | P2 | `_redact` 仅替换 `Path.home()`；`--config-dir /Volumes/...`、`pause_for_apps` 路径、AppImage 绝对路径会原样输出 | 额外脱敏用户名独立出现与可识别绝对路径；或提供白名单字段的 `--share` 模式 | 无（输出字段可能变） | B7；测试 | 未开始 |
| TD-SEC-009 | 依赖主版本无上界，lock 已解析到 OpenCV 5 / numpy 2.5 / Qt 6.11 | `pyproject.toml:44-55`；`uv.lock:306-311,388-393,442-450,793-794` | P2 | 生产依赖只写 `>=`；lock 固定 opencv 5.0.0.93、numpy 2.5.3、PySide6 6.11.2；无 pip-audit/osv；build-system `hatchling>=1.27` 未在 lock 中 | CI 加 pip-audit/OSV/Dependency Review（与 TD-CI-017 合并执行）；记录主版本升级门槛；构建后端纳入锁定 | 无 | B7 + D17 | 未开始 |
| TD-SEC-010 | `scripts/fetch_models.py` 默认模式联网下载 | `fetch_models.py:336-365,220-235`；`ci.yml:69`；`docs/privacy.md:27-28` | P2 | 无参数运行且模型缺失/校验失败即 `urllib.request` 下载；CI 只用 `--check`；应用不 import | 默认 verify-only，联网需显式 `--fetch`；文档标注默认行为 | 变更行为（仅开发者工具） | B7 | 未开始 |
| TD-SEC-011 | Windows 前台权限使用 `ASFW_ANY`，放宽到任意进程 | `ipc.py:211-229`；`platform/windows.py:526-527,1085` | P2 | ctl 每条命令前 `AllowSetForegroundWindow(0xFFFFFFFF)`；应用自身 activate 也用 `ASFW_ANY` | ctl 只对服务端 PID 授权；应用只对目标 hwnd 所属进程授权 | 修复bug | B1/B2；测试 | 未开始 |
| TD-SEC-012 | FFmpeg 本地容器可能引用远程 URL（“never sends a byte”潜在缺口） | `vision/camera.py:456-459,607-611`；`docs/privacy.md:88-90` | P2（待复核） | 代码只检查输入路径字符串，不解析容器/播放列表；FFmpeg concat/HLS demuxer 可引用 URL | 在网络命名空间用构造的 `.txt/.m3u8` + `strace -f -e connect` 复核；若成立，限制容器格式或改用自有解码（见 D15） | 无（若成立需改行为） | 阶段 1 复核 / B4 | 待复核 |
| TD-SEC-013 | `privacy.yield_camera` 默认扫描本机其他进程的摄像头占用，隐私文档未列出 | `config.py:282-286`；`platform/linux.py:2416-2455`；`platform/windows.py:1201-1220`；`docs/privacy.md` | P2 | 默认 true；Linux 扫 `/proc/<pid>/fd`，Windows 读摄像头 consent 注册表；均本地只读、未外传 | 在隐私文档 “What the app does not do” 补一句本机进程检查；保持实现不变 | 无（文档） | B0 | 未开始 |
| TD-SEC-014 | `scripts/build_site.py --out DIR` 无保护地 `shutil.rmtree(DIR)` | `scripts/build_site.py:103-109` | P2 | 无路径白名单/空目录校验；误传 `--out /` 会删除任意目录（仅开发者/CI 手动运行） | 拒绝非白名单/非空目录，或要求 `--force` | 无 | B7 | 未开始 |


## PERF · 性能与可靠性

> 来源：`/tmp/harness-scan/risk-audit.md`。正面基线：vision worker `stop(timeout=3.0)` + join；
> IPC 有 DEFAULT/PROBE/IDLE 超时；hotkey 回调用 `SimpleQueue`；锁屏/分屏任务在短生命周期线程。

| ID | 标题 | 位置 | 严重度 | 证据 | 修复建议 | 业务行为影响 | 批次 / 验收 | 状态 |
|---|---|---|---|---|---|---|---|---|
| TD-PERF-001 | 退出流程在 GUI 主线程同步 join，最坏约 7–9 s 无响应 | `engine/controller.py:943-970,246-253,1586`；`vision/worker.py:193-215`；`panes/worker.py:44-47,150-157` | P2 | shutdown 依次停热键、`worker.stop()`（3 s）、pane worker（2 s）、lock thread（2 s），热键线程自身 join 2 s，全部在 GUI 线程 | 异步 shutdown + 进度提示；缩短 join 并让 daemon 自退出；记录超时指标 | 无 | B1；测试 | 未开始 |
| TD-PERF-002 | 更新下载无整体截止时间/最低速率，慢速或停滞响应可长期停留 DOWNLOADING | `update/winhttp.py:44-45`；`update/fetch.py:136-167`；`update/service.py:285-316` | P2 | 仅 WinHTTP 分阶段超时；`fetch.download` 只在块间检查取消；无总时长/最低速率/最大空读 | 总 deadline + min-rate 检测；失败自动重试一次；停滞提示 | 修复bug | B7；测试 | 未开始 |
| TD-PERF-003 | 摄像头 `read()` 无 OpenCV 层超时，阻塞读延迟隐私/暂停时的设备释放 | `vision/camera.py:43-48,72-76,583-630`；`vision/worker.py:658-677` | P2 | 代码自述某些驱动 read 会阻塞数秒；仅 Linux 设 `OPENCV_VIDEOIO_V4L_SELECT_TIMEOUT=2`；`_go_idle` 要等当前 read 返回才释放 | 打开时设置 read/open timeout 属性（支持的后端），或可取消 grab/非阻塞轮询；文档注明最坏延迟 | 无 | B4；测试/文档 | 未开始 |
| TD-PERF-004 | GUI 线程执行可能阻塞的平台轮询（D-Bus/AX/进程枚举） | `controller.py:851-857,1447-1470`；`linux.py:99-105,1033-1060,1743-1762`；`macos.py:97-103,773-782`；`windows.py:1201-1220` | P2 | Linux `bus.call(..., Block, 500ms)`，回退 `gdbus/loginctl` 超时 1 s；macOS AX 0.25 s；Windows registry+`psutil.process_iter`；最坏单 tick 卡 0.5–1 s | 轮询移到后台线程，以最新快照驱动 tick；或给 GUI 线程调用设更短硬超时 | 无 | B1/B2；测试 | 未开始 |


## CI · 门禁与流水线

> 来源：`/tmp/harness-scan/ci-audit.md`（51 条原始发现，按根因归并为下列 29 条）。
> 正面基线（不需修）：26 条外部 action 全 SHA pin、默认 `contents: read`、无
> `pull_request_target`、checkout `persist-credentials: false`、隐私 gate 源码+bundle 双重执行、
> `build.yml` 为唯一构建定义且有回归测试。

| ID | 标题 | 位置 | 严重度 | 证据 | 修复建议 | 业务行为影响 | 批次 / 验收 | 状态 |
|---|---|---|---|---|---|---|---|---|
| TD-CI-001 | 发布链路不依赖测试门禁：打 tag 即可发布未经测试的提交 | `release.yml:10-14,24-41`；`ci.yml:3-8,119` | P1 | tag push 只触发 release.yml；ci.yml 的 push 仅 `branches: [main]`；全仓 workflow 只有 `ci.yml:119` 跑 pytest，build/release 无 pytest/needs CI | 把 test 矩阵抽成可复用 `test.yml` 供 release 调用，或规定 tag 必须指向 main 上 CI 通过的 commit | **变更行为**（发布门禁） | B7 + D11；先用演练 tag 验证 | 未开始 |
| TD-CI-002 | 自动正式发布，无 draft / environment 审批；dispatch 到 tag ref 会意外发布 | `release.yml:5-6,14,37-49,89-103` | P2 | 创建 Release 无 `draft:`；无 `environment:`；注释称手动“只构建不发布”，但 `if: github.ref_type == 'tag'` 对 dispatch 同样成立 | 二选一：`draft: true` 由人审批，或 release job 挂 protected environment；条件改为 `github.event_name == 'push'` 或加 `publish` input；决定写入 docs/CI.md | 变更行为 | B7 + D11 | 未开始 |
| TD-CI-003 | tag 可指向任意 commit，发布不校验它在 main/受保护分支上 | `release.yml:11-13`；`build.yml:84-87` | P2 | 唯一 guard 是 tag 名 == `__version__`；无 `git merge-base --is-ancestor` | prepare 中断言 tag commit 是默认分支祖先；或依赖受保护 tag ruleset（仓库外） | 变更行为 | B7 + D11 | 未开始 |
| TD-CI-004 | 版本解析有 4 套实现且无空值校验 | `build.yml:79,88-89`；`eye-tracker.spec:74-101`；`build_appimage.sh:69-72`；`pyproject.toml:85-86` | P2 | build.yml/build_appimage 用 `sed`（格式一变即读不到）；spec 用 AST；hatchling 也解析；`numeric` 空串时 ISO 生成非法版本 | 抽 `scripts/version.py`（AST 解析）供全部调用；prepare 校验 numeric 非空 | 修复bug | B7；版本脚本单测 | 未开始 |
| TD-CI-005 | 资产名/架构名/仓库 URL 在 5+ 处硬编码重复 | `build.yml:117,157-171,198-201`；`release.yml:63-69`；`installer.iss:102`；`build_appimage.sh:82-84`；`make_dmg.sh:58-59` | P2 | 发布说明、release 校验、Inno/AppImage/dmg 各自拼 `EyeTracker-$VERSION-...`；更新器还依赖其中 2 个名字 | 生成一份资产 manifest（JSON/脚本）共用；更新器测试继续断言两个稳定名 | 无 | B7 | 未开始 |
| TD-CI-006 | macOS 稳定签名是可选 secret，release 仍可在 ad-hoc 下成功 | `build.yml:483-496`；`release.yml:31-35` | P2 | 无证书时 `exit 0` 继续；无 notarization；用户升级后需重新授权无障碍权限 | tag 发布时把证书设为必需（缺失 fail），或把“允许 ad-hoc”作为显式开关记录 | 变更行为 | B7 + D11 | 未开始 |
| TD-CI-007 | Windows 无代码签名、macOS 未 notarize | `installer.iss:60-110`；`make_dmg.sh:77-98`；`docs/platform-support.md:82,97` | P2 | 安装器无 SignTool；README 将“Signed and notarised builds”列为未完成；SmartScreen/Gatekeeper 需用户绕过 | 规划 Windows 签名 + Apple notarization（可先可选步骤）；文档保持如实 | 无 | B7 + D11 | 未开始 |
| TD-CI-008 | CI 成本：文档 PR 跑全矩阵，src PR 跑三平台 60 min bundle | `ci.yml:3-8,84-95`；`bundle.yml:14-26`；`build.yml:225,443,591` | P2 | `pull_request` 无路径过滤；bundle 命中 `src/**` 即三平台打包 | docs-only 加 `paths-ignore` 或轻量 job；bundle 用 label/nightly 控制，PR 默认单平台 | 无 | B7 | 未开始 |
| TD-CI-009 | `bundle.yml` 路径列表 PR/push 逐字重复两份 | `bundle.yml:15-26,30-41` | P2 | 11 项列表复制；`tests/test_build_scripts.py:926-945` 用 count==2 锁定 | YAML anchor/单一来源；测试改为结构断言 | 无 | B7 | 未开始 |
| TD-CI-010 | 构建/测试 runner 与解释器来源漂移 | `ci.yml:85-88,109-118`；`build.yml:224,442,48-51,238-250` | P2 | 测试 `macos-latest` vs 发布 `macos-14`；Windows 浮动 `windows-latest`；ci 用 uv 托管 Python、build 用 setup-python；ci 的 uv 未显式传 matrix.python（本地实验：`.python-version` 会压过 PATH） | 测试与发布统一 runner 标签；ci 显式 `--python "${{ matrix.python }}"` 并打印版本断言 | 无 | B7 | 未开始 |
| TD-CI-011 | lint/test job 也安装 build 组（PyInstaller/Pillow） | `pyproject.toml:82-83`；`ci.yml:42-43,115-116` | P2 | `default-groups = ["dev","build"]`；5 个 test job + lint 都 `uv sync --locked` | test/lint 用 `--no-group build` 或拆 test 组 | 无 | B7 | 未开始 |
| TD-CI-012 | Python 支持矩阵与声明不一致 | `pyproject.toml:14,37-39,122-127`；`ci.yml:84-95`；`README.md:239`；`docs/building.md:8-13` | P2 | 允许 `<3.15`（含 3.14）但无 3.14 classifier/job；3.11/3.13 只在 ubuntu；mypy 固定 3.12；README 未写支持范围 | 明确支持级别并写进文档；CI 补组合或收敛下限/上限（成本见 D9） | 无 | B7 + B0 | 未开始 |
| TD-CI-013 | 外部构建工具/apt 包版本未 pin | `ci.yml:71-76`；`build.yml:281-295,602-613`；`build_appimage.sh:134-155` | P2 | `choco install innosetup` 无版本；`7z` 未声明；apt 无版本；appimagetool 有 SHA pin | 预装/缓存并 pin Inno/7z/shellcheck；记录外部工具清单 | 无 | B7 | 未开始 |
| TD-CI-014 | 发布/打包没有一条本地命令可复现；安装器冒烟是 120 行内联 PowerShell | `docs/building.md:16-128`；`build.yml:101-209,303-425` | P2 | 无 `scripts/ci-local.py`；安装/升级/PATH/自启/卸载断言无法本地跑；CHANGELOG 解析也是 100 行内联 Python | 抽 `scripts/release_notes.py`、`scripts/installer_smoke.ps1`，提供本地入口并单测 | 无 | B7 | 未开始 |
| TD-CI-015 | CONTRIBUTING/PR 模板的本机命令与 CI 不一致 | `CONTRIBUTING.md:41-57`；`PULL_REQUEST_TEMPLATE.md:25`；`ci.yml:51-76` | P2 | shellcheck/desktop-file-validate 无命令；PR 模板 mypy 弱于 CI（缺 win32/darwin/scripts） | 增加两平台 lint 命令/`scripts/check_packaging.sh`；模板引用统一脚本 | 无 | B7 / B0 | 处理中（CONTRIBUTING 已统一命令；PR 模板与 shellcheck 本地命令待补） |
| TD-CI-016 | CI 重新生成图标但不校验提交的图标是否最新 | `build.yml:255-256,469-470,632-633`；`make_icons.py:114-116,164-171` | P2 | CI 跑写模式；`--check` 只判断存在，不比较内容 | CI 渲染到临时目录比对 hash；`--check` 改为内容校验 | 无 | B4/B7 | 未开始 |
| TD-CI-017 | 无依赖漏洞 / secret / CodeQL 扫描门禁 | `.github/workflows/`（5 个 workflow，grep 无 codeql/gitleaks/pip-audit/osv） | P2 | Dependabot 只做版本更新，不阻断已知漏洞 | 增 `pip-audit`/OSV（report-only 起步）、secret scanning、CodeQL | 无 | 阶段 3 | 未开始 |
| TD-CI-018 | 无 pre-commit / 本地提交钩子 | 仓库根无 `.pre-commit-config.yaml`；`CONTRIBUTING.md:39-57` | P2 | 所有检查靠人工 checklist | 加 `.pre-commit-config.yaml`（ruff/format/mypy/privacy），与 CI 共用版本（见 D2） | 无 | 阶段 3 | 已完成（阶段 3，2026-11-06） |
| TD-CI-019 | mypy 不检查 `tests/` | `pyproject.toml:122-127`；`ci.yml:62-63` | P2 | `files = ["src/eye_tracker"]`；CI 仅加 scripts/packaging | 增 `mypy tests`（可先 per-file ignore） | 无 | 阶段 3 | 未开始 |
| TD-CI-020 | 无 actionlint/yamllint，workflow 只在 GitHub 端验证 | 全仓 grep 无 actionlint；`tests/test_build_scripts.py:641-659` | P2 | 正则切 YAML 文本，无法验证 Actions 语义 | lint job 加 actionlint；guard 测试改 YAML parser | 无 | 阶段 3 | 未开始 |
| TD-CI-021 | `test_build_scripts.py` 2,597 行逐字断言 YAML 文本 | `tests/test_build_scripts.py:641-659,762` | P2 | 两空格缩进正则切分；引号/重排/anchor 即红；无语义校验 | 保留关键语义断言 + YAML parser + actionlint；workflow 逻辑抽脚本后单测 | 无 | 阶段 3 | 未开始 |
| TD-CI-022 | 打包后的 GUI/托盘无功能级冒烟 | `build.yml:339,541-573`；`smoke_test_bundle.py:15-29` | P2 | Windows GUI 只 `--version`；macOS 只 smoke CLI；无 offscreen 启动/托盘/设置页 | 加 `QT_QPA_PLATFORM=offscreen` GUI 启动→doctor→退出冒烟 | 无 | B7 | 未开始 |
| TD-CI-023 | `docs/building.md` 称每个 workflow 都有 concurrency，`build.yml` 没有 | `docs/building.md:162-164`；`build.yml:17-18`；`tests/test_build_scripts.py:783-793` | P2 | build.yml 仅 `workflow_call`，测试明确断言无 concurrency | 文档改为“除被复用的 build.yml 外；build.yml 继承调用方 concurrency” | 无 | B7 / DOC-013 | 已完成（阶段 2，2026-11-06） |
| TD-CI-024 | 本地 Windows 安装器命令漏 `AppVersionNumeric`，预发布版本生成非法版本资源 | `docs/building.md:73-76`；`installer.iss:27-30,72-74`；对照 `build.yml:294` | P2 | ISO 默认 `AppVersionNumeric=AppVersion`；`0.3.0rc1` 非纯数字 | 文档命令补 `/DAppVersionNumeric=<x.y.z>` 并解释去字母 | 无 | B7 / DOC-013 | 已完成（阶段 2，2026-11-06） |
| TD-CI-025 | 第三方 `softprops/action-gh-release` 持有 `contents: write` 并接收发布说明 | `release.yml:89-103` | P2 | 第三方 action 在 release job token 下运行；SHA pin 缓解 tag 漂移 | 优先用官方 `gh release create/upload`；或单独 job + 最小权限 | 无 | B7 | 未开始 |
| TD-CI-026 | release job 为触发 pages 持有 `actions: write` | `release.yml:43-49,105-112` | P2 | 同一 job 同时有 contents/id-token/attestations/actions write | 拆独立 job 只持 `actions: write`；或用 `release: published` 触发 | 无 | B7 | 未开始 |
| TD-CI-027 | Pages 用 `pip install "pillow>=10.4"` 绕过 uv.lock | `pages.yml:47-48` | P2 | Pages job 不用 uv；每次取最新 Pillow；favicon 渲染随版本变化 | `uv sync --locked --group build` 或 `uv run --with pillow==<pin>` | 无 | B7 | 未开始 |
| TD-CI-028 | `gh api releases/latest \|\| echo '{}'` 把鉴权/限流错误也静默降级 | `pages.yml:50-56` | P2 | 任何非零退出都写 `{}` 并部署成功；404 合理，401/403/5xx 不应 | 区分 404 与其它错误；加一次重试 | 无 | B7 | 未开始 |
| TD-CI-029 | Dependabot 把全部 GitHub Actions 合为一个 PR | `.github/dependabot.yml:18-21` | P2 | `patterns: ["*"]` 单组；多 action 同升难定位/难审计 | 按来源/单个 action 分组，保留 SHA+版本注释更新 | 无 | 阶段 3 | 未开始 |
| TD-CI-030 | 死配置：ruff per-file ignores 重复全局 ignore | `pyproject.toml:100,107,115` | P2 | `PLR2004` 已全局 ignore；`S101` 属 flake8-bandit，未启用 S | 删除 `tests/**` 的 PLR2004/S101，只留生效项 | 无 | 阶段 3 | 未开始 |
| TD-CI-031 | 打包/签名腐烂：`codesign --deep`、HFS+ DMG、`uname -m` 命名与 release 固定名不一致 | `make_dmg.sh:77,108-119,58-59`；`build_appimage.sh:73,82-84`；`release.yml:63-69` | P2 | Apple 长期建议显式签 nested code；HFS+ 在新 macOS 支持不确定；主机架构变化会产生错名 | 逐组件签名；评估 APFS；显式传 `--arch` 由 matrix 决定 | 无 | B7 | 未开始 |
| TD-CI-032 | build-linux 安装未使用的 `desktop-file-utils` | `build.yml:602-613` vs `ci.yml:76` | P2 | build-linux 后续无 `desktop-file-validate` 调用 | 删除该包，或在 build-linux 真正执行校验 | 无 | B7 | 未开始 |
| TD-CI-033 | 分支保护/required checks 在仓库外，无法从代码确认 | GitHub Settings（仓库内无 ruleset 文件） | P2 | `ci.yml`/`bundle.yml` 是否为合并前必需检查不可见；tag 是否受保护不可见 | 人工确认并记录到 docs/CI.md；若未配置，TD-CI-001 风险更高 | 无 | D11 | 待确认 |


## META · 过程与元数据

| ID | 标题 | 位置 | 严重度 | 证据 | 修复建议 | 业务行为影响 | 批次 / 验收 | 状态 |
|---|---|---|---|---|---|---|---|---|
| TD-META-001 | 规则文档施工期常驻，竣工后必须按 §14 拆解归档 | `docs/turn-project-to-harness-rules.md`（1,101 行 / 73 KB） | P2 | 规则 §14；当前全文常驻 docs/ | 阶段 5 验收后单独提交：一次性流程归档 `docs/exec-plans/completed/harness-<date>.md`，长效契约抽进 AGENTS/core-beliefs/ARCHITECTURE | 无 | 收官；H 确认 | 未开始 |
| TD-META-002 | 机械扫描只覆盖 `src/`，tests/scripts/docs 未纳入 | `scripts/analysis/scan_metrics.py` | P2 | 脚本 `iter_py(PKG)` 仅 `src/eye_tracker`；测试大文件也超阈值但未进报告 | 扩展扫描范围与阈值分组；基线快照纳入阶段 4 对比 | 无 | B0 | 未开始 |

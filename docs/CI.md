# CI 配置决定

> 本文件由 harness 工程化改造阶段 1 建立，记录**框架判定结果**与**现有 CI 现状**。
> 交互询问（Tauri 专用）不适用，原因见 §3；阶段 3 的门禁改造在此追加 §4。

## 1. 框架判定

- **判定结果：其他类型（非 Tauri）**
- **判定日期：2026-11-06**
- **判定依据（§3.2 机械判定）**：
  - `src-tauri/tauri.conf.json` —— 不存在
  - `src-tauri/Cargo.toml`（依赖中含 `tauri`）—— 不存在
  - 根 `package.json`（依赖含 `@tauri-apps/cli` / `@tauri-apps/api`）—— 不存在
  - 实际形态：Python src-layout 桌面应用
    - `pyproject.toml`：`build-backend = "hatchling.build"`，`requires-python = ">=3.11,<3.15"`
    - 入口：`project.scripts.eye-tracker = "eye_tracker.cli:main"`、
      `project.gui-scripts.eye-tracker-gui = "eye_tracker.cli:gui_main"`
    - 打包：PyInstaller（`packaging/pyinstaller/eye-tracker.spec`）+ 各平台安装器
      （Windows Inno Setup、macOS dmg、Linux AppImage）
- **结论**：按 §3.4 步骤 F3 处理——不进入 §3.3 的 Tauri 三问，不生成 Tauri 多平台
  打包流水线模板；现有 CI **只审计、不删除**。基础门禁（lint / test / build）仍按
  §5 阶段 3 建立与加强。

## 2. 现有 CI 现状（存量项目必填）

### 2.1 workflow 清单

| 文件 | 触发条件 | 覆盖平台 | 是否自动发布 | 备注 |
|---|---|---|---|---|
| `.github/workflows/ci.yml` | `push` → `main`；`pull_request`（全部）；`workflow_dispatch` | ubuntu-24.04；测试矩阵另含 windows-latest、macos-latest | 否 | 基础门禁：ruff lint/format、mypy（默认 + win32 + darwin + scripts）、隐私检查、模型校验、shellcheck、desktop-file-validate；测试矩阵 Python 3.12 × 三 OS，外加 ubuntu 3.11/3.13 |
| `.github/workflows/bundle.yml` | `pull_request` / `push main`，路径过滤（`src/**`、`packaging/**`、`scripts/check_privacy.py` 等、`pyproject.toml`、`uv.lock`、构建相关 workflow）；`workflow_dispatch` | 复用 `build.yml`（Windows x64 / macOS arm64 / Linux x86_64） | 否（`upload: false`） | 发布前的打包冒烟：三平台构建 + 隐私 gate + 安装/升级/卸载冒烟 |
| `.github/workflows/release.yml` | `push` tag `v*`；`workflow_dispatch` | 复用 `build.yml`（同上） | **是**：tag 触发即创建正式 GitHub Release（非 draft） | 版本 guard 在 `build.yml` 的 `prepare` job；`release` job 校验 5 个产物、生成 `SHA256SUMS.txt`、Sigstore 构建溯源、发布后触发 `pages.yml` |
| `.github/workflows/pages.yml` | `push main`（`site/**`、`assets/**`、`scripts/build_site.py`、自身）；`release: published`；`workflow_dispatch` | ubuntu-24.04，GitHub Pages | 否（发布网站） | 构建 `site/` 并部署 Pages；发布后重建以更新下载链接 |
| `.github/workflows/build.yml` | `workflow_call`（被 `bundle.yml` / `release.yml` 调用） | Windows x64 / macOS arm64 / Linux x86_64 | 由调用方决定 | 33 KB 可复用工作流：`prepare`（版本 guard + CHANGELOG 发布说明）+ 三个平台构建 job，含隐私 gate、冒烟测试、可选 macOS 签名 |

### 2.2 已有发布链路现状

```
tag vX.Y.Z
  └─ release.yml (push tag)
       └─ build.yml (workflow_call, upload: true)
            ├─ prepare: 读 src/eye_tracker/__init__.py 的 __version__
            │            guard: GITHUB_REF_NAME == "v$version"，否则 fail
            │            生成 numeric 版本 / prerelease 标记 / 从 CHANGELOG.md 抽取发布说明
            ├─ build-windows (windows-latest, 60 min): PyInstaller → 安装器/便携 ZIP → 冒烟
            ├─ build-macos   (macos-14, 60 min): PyInstaller → 可选签名 → dmg → 冒烟
            └─ build-linux   (ubuntu-22.04, 60 min): PyInstaller → AppImage/tar.gz → 冒烟
       └─ release job: 校验 5 个产物存在 → SHA256SUMS.txt → Sigstore attestation
                       → softprops/action-gh-release 创建正式 Release
                       → 触发 pages.yml 重建网站
```

- **版本单一事实源**：`src/eye_tracker/__init__.py` 的 `__version__`（hatchling 动态读取，
  `pyproject.toml` 不再重复声明）。tag 与版本不一致时 `prepare` 直接失败——已有机械 guard。
- **发布方式**：tag 推送后**直接正式发布**，不使用 draft；发布说明来自 `CHANGELOG.md`。
- **平台覆盖**：Windows 10/11 x64、macOS 14+ Apple silicon（arm64）、Linux x86_64。
  与 `docs/platform-support.md`、README 的下载表一致；Intel Mac 明确为“从源码运行”。
- **签名**：macOS 可选证书（仓库 secrets 缺失时 ad hoc 签名）；Windows 无代码签名
  （`docs/platform-support.md` 有说明）。
- **依赖与供应链**：三方 Actions 全部按 commit SHA pin（带 `# vX.Y.Z` 注释），Dependabot
  同时维护 SHA 与版本注释；workflow `permissions` 最小化（默认 `contents: read`）。

### 2.3 本地可复现性现状

`CONTRIBUTING.md` 的 "Checks" 节已把 CI 的主门禁写成可复制命令；本地实测（2026-11-06）：

| 门禁 | 本地命令 | 现状 |
|---|---|---|
| Ruff lint | `uv run ruff check .` | ✅ 通过 |
| Ruff format | `uv run ruff format --check .` | ✅ 通过（159 files） |
| Mypy（默认） | `uv run mypy` | ✅ 通过（76 source files） |
| Mypy（Windows 视角） | `uv run mypy --platform win32` | ✅ 通过 |
| Mypy（macOS 视角） | `uv run mypy --platform darwin` | ✅ 通过 |
| Mypy（脚本/打包） | `uv run mypy scripts packaging/pyinstaller/entry.py` | ✅ 通过 |
| 隐私检查 | `uv run python scripts/check_privacy.py` | ✅ 通过（76 files，仅允许 update/winhttp.py） |
| 模型校验 | `uv run python scripts/fetch_models.py --check` | ✅ 通过（sha256 verified） |
| 测试 | `uv run pytest -q` | ✅ 2952 passed / 55 skipped（35.59 s，2026-11-06 本机） |
| 配置文档同步 | `uv run python scripts/gen_config_docs.py --check`（另有 `tests/test_docs.py`） | ✅ 通过（阶段 3 起 CI 独立 step） |
| 大小门禁 | `uv run python scripts/lint_size.py --check` | ✅ 通过（存量冻结，基线 22 文件 / 41 函数 / 16 类） |
| 结构测试 | `uv run python -m pytest tests/test_architecture.py -q` | ✅ 通过（9 用例，含负例） |
| 打包脚本 lint | `shellcheck` + `desktop-file-validate`（CI 安装工具后跑） | ⚠️ 本机未验证（工具未安装，CONTRIBUTING 也未给命令） |
| Windows 安装/升级/卸载冒烟 | —— | ❌ CI-only：`build.yml:303-425` 内联 PowerShell，无本地脚本 |
| 发布校验 / attestation / 建 Release | —— | ❌ CI-only（设计使然，不可本地复现） |
| 覆盖率 | —— | ❌ 无覆盖率工具、无覆盖率门禁（阶段 5） |
| 本地提交钩子 | `uvx pre-commit install` 后自动运行快子集 | ✅ 配置已就位（`.pre-commit-config.yaml`，可选安装） |
| 一键本地门禁 | `uv run python scripts/check.py`（`--quick` 跳过 pytest） | ✅ 与 CI 同序 |

> 说明：`ci.yml` 的 `lint` job 只跑在 ubuntu-24.04；平台专属类型检查通过
> `mypy --platform win32/darwin` 在同一 job 内覆盖，属已有优势。
> 覆盖率与提交钩子是阶段 3 / 阶段 5 的待建项，见
> `docs/exec-plans/active/harness-migration.md`。

### 2.4 阶段 1 CI 审计结论（摘要，完整 51 条见台账）

- **P1（唯一）**：发布链路不依赖测试门禁。tag push 只触发 `release.yml`；`ci.yml` 的 push
  仅监听 `main`/PR；`build.yml` 与 `release.yml` 均不跑 pytest。打 tag 即可发布未经测试套件
  验证的提交。
- **发布方式**：tag 直接创建正式 Release（非 draft），无 protected environment/审批；
  `workflow_dispatch` 选择 tag ref 时也会走发布 job（与 `release.yml:5-6` 注释矛盾）；
  tag 可指向任意 commit，无“必须在 main 上”的检查。
- **可复现性**：Windows 安装/升级/卸载冒烟、release 校验/attestation 无法本地复现；
  `shellcheck` / `desktop-file-validate` 无本地命令；无覆盖率、依赖漏洞、secret 扫描。
- **可复现性细节**：测试用 `macos-latest` 而发布固定 `macos-14`；Windows runner 浮动；
  ci 用 uv 托管的 Python、build 用 `setup-python`；ci 的 `uv sync` 未显式传 matrix Python。
- **正面基线**（无需改）：26 条外部 `uses:` 全部 40 位 SHA + 版本注释 pin；默认
  `permissions: contents: read`；无 `pull_request_target`；所有 checkout `persist-credentials:
  false`；隐私 gate 源码 + bundle 双重执行；`build.yml` 为唯一构建定义，并有 guard 测试。
- 相关人工决策：D11（发布门禁/方式）、D12（分支保护与 secrets 是否配置）、D13（CI 成本）。

## 3. 交互确认记录（仅 Tauri 填写）

**不适用。** 按 §3.2 判定为“其他类型”，按 §3.4 不进入 §3.3 三问流程；
用户在阶段 3 不需要回答“是否生成 GitHub CI / 目标平台 / Release 发布方式”三问。
现有打包发布流水线**保留现状**，阶段 3 只做审计与门禁加强，不重建、不删除。

## 4. 落地说明（阶段 3）

### 4.1 门禁清单（CI ↔ 本地 ↔ 是否 blocking）

| 检查 | CI 位置 | 本地等价命令 | Blocking |
|---|---|---|---|
| Ruff lint | `ci.yml` lint job | `uv run ruff check .` | ✅ |
| Ruff format | 同上 | `uv run ruff format --check .` | ✅ |
| Mypy（默认 / win32 / darwin） | 同上，3 个 step | `uv run mypy`、`--platform win32/darwin` | ✅ |
| Mypy（scripts + packaging entry） | 同上 | `uv run mypy scripts packaging/pyinstaller/entry.py` | ✅ |
| 隐私 gate（源码） | 同上 | `uv run python scripts/check_privacy.py` | ✅ |
| 模型校验 | 同上 | `uv run python scripts/fetch_models.py --check` | ✅ |
| 配置文档同步 | 同上（阶段 3 新增独立 step） | `uv run python scripts/gen_config_docs.py --check` | ✅ |
| 大小门禁 | 同上（阶段 3 新增） | `uv run python scripts/lint_size.py --check` | ✅ |
| 结构测试（依赖方向/环/纯逻辑） | `ci.yml` test job（pytest） | `uv run pytest tests/test_architecture.py -q` | ✅ |
| 文档门禁 | `ci.yml` test job（pytest） | `uv run pytest tests/test_docs.py -q` | ✅ |
| 全量测试 | `ci.yml` test job × 5 组合 | `uv run pytest -q` | ✅ |
| shellcheck / desktop-file-validate | `ci.yml` lint job（apt 安装工具） | 无（CI-only；TD-CI-015） | ✅ |
| 打包冒烟（三平台） | `bundle.yml` / `build.yml` | 分平台手工步骤（`docs/building.md`） | ✅（PR 路径命中时） |
| 版本 guard | `build.yml` prepare | 无（CI-only，tag 时执行） | ✅（tag） |
| 发布（checksum / attestation） | `release.yml` | 无（CI-only） | ✅（tag） |

一键本地入口：`uv run python scripts/check.py`（`--quick` 跳过 pytest，`--list` 列出门禁）。
`pre-commit` 钩子是它的快速子集：ruff（自动修复）、ruff-format、隐私 gate、大小门禁、文档门禁
（`.pre-commit-config.yaml`，`uvx pre-commit install` 安装，decision D2）。

### 4.2 本阶段变更（相对阶段 1 现状）

- `ci.yml` lint job 增量新增 2 个 step：配置文档同步、大小门禁。**没有**新建 workflow、
  重建打包流水线或改变发布行为（Tauri 三问不适用，见 §3）。
- **发布门禁（B7）**：`release.yml` 新增 `test` job（ubuntu-24.04，`uv sync --locked` +
  `pytest -q`），`build` job `needs: test`——tag 不可能再发布未经测试的提交。
  `build.yml` 的 `prepare` 校验 tag 必须指向 `origin/main` 的祖先，且 `__version__` 必须含
  纯数字版本（Windows/CFBundleVersion 需要）。
- **发布方式未变**：仍为 tag 直接正式发布（非 draft），平台范围与签名行为保持原样（D11）。
- **覆盖率门禁（阶段 5 / D1）**：`pytest-cov` 基线 89 %，`fail_under = 89` 且只许上调；
  本地 `scripts/check.py` 的 pytest gate 默认执行覆盖率，CI 只在 ubuntu-24.04 / Python 3.12
  一个矩阵项加 `--cov`（其余保持速度），低于阈值即红灯。
- 新增 `scripts/lint_size.py` + `scripts/analysis/size-baseline.json`：文件 >600 行、
  函数 >60 行、类 >300 行必须已在基线内，基线只许收缩（D10）。
- 新增 `tests/test_architecture.py`：解析全仓 import 图，强制 `ARCHITECTURE.md` 的 R1–R5、
  纯逻辑无 Qt/OpenCV、例外表只缩不增、模块级环不新增。
- `tests/test_docs.py` 从 6 个用例扩到 15 个（阶段 2 已完成）；`tests/test_lint_size.py`
  与 `tests/test_check_runner.py` 提供门禁自身的负例与一致性检查。
- 新增 `scripts/check.py` 与 `.pre-commit-config.yaml`；`AGENTS.md` / `CONTRIBUTING.md` 同步。

### 4.3 门禁自证（规则 §11.1 第 4 条）

实跑验证：GitHub Actions run
[42154283481](https://github.com/likangjie110/eye-tracker/actions/runs/42154283481)
（2026-11-06，fork 的 `harnessing` 分支）：lint job（含新增两个 step）与 5 个测试矩阵 job
全部通过。

每一条新不变量都有“注入违规必须失败”的测试，而不是只靠人工：

| 不变量 | 自证测试 |
|---|---|
| 大小门禁拒绝新增/增长 | `tests/test_lint_size.py::test_new_oversized_function_is_a_violation`、`test_baselined_function_may_not_grow`、`test_adding_a_baseline_entry_needs_force` |
| 依赖方向拒绝反向 import | `tests/test_architecture.py::test_synthetic_wrong_direction_is_rejected` |
| 纯逻辑拒绝 Qt/OpenCV | `tests/test_architecture.py::test_synthetic_pure_module_with_qt_is_rejected` |
| 新循环依赖被拒绝 | `tests/test_architecture.py::test_synthetic_new_cycle_is_rejected` |
| 例外表只能收缩 | `tests/test_architecture.py::test_exception_table_only_shrinks`、`test_synthetic_stale_exception_is_rejected` |
| 文档索引/版本漂移被拒绝 | `tests/test_docs.py::test_docs_index_check_detects_a_missing_entry`、`test_stale_version_check_detects_drift` |
| 隐私边界可拦截 | `tests/test_privacy.py`（既有 291 个参数化用例，含注入违规） |
| 发布前测试门禁 / tag 在 main 上 / numeric 版本 | `tests/test_build_scripts.py::test_release_requires_the_test_suite_and_a_main_ancestor` |
| 覆盖率低于阈值 | `pytest --cov` + `[tool.coverage.report].fail_under`（本地实测：注入未覆盖代码即失败） |

### 4.4 与现有配置的关系

增量改造 `ci.yml`（2 个新 step）。`build.yml` / `release.yml` / `bundle.yml` / `pages.yml`
未改动；发布方式、平台范围与签名行为保持阶段 1 记录的原样。

## 5. 变更历史

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-11-06 | 建立本文件：框架判定 = 其他；记录现有 5 个 workflow 与发布链路 | harness 阶段 1（§3.2 / 附录 C） |
| 2026-11-06 | 阶段 3：新增配置文档同步与大小门禁两个 CI step；建立本地门禁入口、pre-commit、结构测试与负例自证 | harness 阶段 3（§5） |
| 2026-11-06 | 阶段 4 B7：release 增加 test 门禁；tag 必须位于 main；numeric 版本 guard | 解决 CI 审计唯一 P1（TD-CI-001）与 tag 来源问题（TD-CI-003） |

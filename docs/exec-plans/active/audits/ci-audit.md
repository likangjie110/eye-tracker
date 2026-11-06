# CI / 构建发布审计（阶段 1）

- 审计对象：`/Users/kukan/PycharmProjects/eye-tracker/.orca/worktrees/eye-tracker/lionfish`，HEAD `9644527`（2026-10-02），审计日期 2026-11-06。
- 范围：`.github/workflows/{ci,bundle,build,release,pages}.yml`、`pyproject.toml`、`scripts/*.py`、`packaging/**`、发布相关 docs/tests。
- 方式：只读静态审计 + 只读命令复核（grep/nl/git/uv `python find` 本地小实验）；未联网、未安装依赖、未跑 pytest（避免写 `.pytest_cache`）、未改仓库。`git status` 仅显示队友新增的未跟踪文件（`docs/CI.md`、`docs/turn-project-to-harness-rules.md`、`scripts/analysis/`）。
- 结论：**P0 = 0，P1 = 1，P2 = 50**。CI 基线整体很扎实（action 全 SHA pin、权限默认 `contents: read`、无 `pull_request_target`、checkout `persist-credentials: false`、隐私 gate 双重执行、打包产物有冒烟测试与 workflow guard 测试）；主要缺口集中在 **发布门禁未依赖测试**、**自动发布无 draft/审批**、**本地可复现性与覆盖率/安全扫描缺失**、**部分 runner/工具未 pin**。

## A. 现有 CI 全景表

| 文件 | 触发 | 平台 / matrix | 是否发布 | 关键检查 | 备注 |
|---|---|---|---|---|---|
| `ci.yml` | `push` 仅 `main`；全部 `pull_request`；`workflow_dispatch`（`ci.yml:3-8`） | lint 固定 `ubuntu-24.04`；test 矩阵 `ubuntu-24.04 / windows-latest / macos-latest`，Python 3.12，另加 ubuntu 3.11/3.13（`ci.yml:84-95`） | 否（`contents: read`，`ci.yml:10-11`） | Ruff lint/format、mypy（默认/win32/darwin/scripts）、源码隐私检查、模型校验、shellcheck、desktop-file-validate、pytest（`ci.yml:45-76,118-119`） | concurrency 同分支/PR 取消旧 run（`ci.yml:14-16`）；仅 `ci.yml:119` 跑 pytest |
| `bundle.yml` | PR + `push main`，路径过滤 `src/**`、`packaging/**`、4 个脚本、`pyproject.toml`、`uv.lock`、3 个 workflow；`workflow_dispatch`（`bundle.yml:13-42`） | 复用 `build.yml`：Windows x64 / macOS arm64 / Linux x86_64 | 否（`upload: false`，`bundle.yml:52-58`） | 完整打包 + 隐私 gate + 各平台安装/升级/卸载与包冒烟；无 secret、无产物 | 路径列表在 PR/ push 两段逐字重复（`bundle.yml:15-26,30-41`） |
| `build.yml` | 仅 `workflow_call`（`build.yml:17-41`） | `prepare` ubuntu-24.04；Windows `windows-latest`、macOS `macos-14`、Linux `ubuntu-22.04` | 由调用方 `upload` 决定；不自行发布 | 版本 guard + CHANGELOG 发布说明、PyInstaller、bundle 隐私 gate、Windows 安装器/PATH/自启冒烟、macOS 签名+dmg、Linux AppImage/tar | 689 行，唯一的构建定义；`release.yml`/`bundle.yml` 都只调用它 |
| `release.yml` | `push` tag `v*`；`workflow_dispatch`（`release.yml:10-14`） | 复用 `build.yml`，`upload: true` | **是，自动正式发布（非 draft）**（`release.yml:89-103`） | 5 个产物存在性、`SHA256SUMS.txt`、Sigstore provenance、GitHub Release、触发 `pages.yml` | release job 持有 `contents/id-token/attestations/actions: write`（`release.yml:43-49`）；无 environment 审批 |
| `pages.yml` | `push main` 路径过滤 `site/**`、`assets/**`、`build_site.py`、自身；`release: published`；`workflow_dispatch`（`pages.yml:9-21`） | ubuntu-24.04 | 否（部署 GitHub Pages） | Pillow 安装、`gh api` 读 latest release、`build_site.py`、Pages deploy | 部署 job 才有 `pages: write` + `id-token: write`（`pages.yml:69-83`）；pip 安装未走 uv.lock |

时间/失败要点：lint 15 min、test 30 min、各 build job 60 min、release 15 min、pages 两 job 10 min；test 矩阵 `fail-fast: false`（`ci.yml:82-83`）；无 `continue-on-error`；build 任一平台失败则 release job 因 `needs: build` 被跳过（`release.yml:37-41`）；Windows 安装器失败会打印 setup 日志（`build.yml:416-425`）。

## B. 发布链路现状（含证据文件:行）

### B1. tag → workflow → Release 全路径

1. **触发**：`push.tags: ["v*"]` 或手动 dispatch（`release.yml:10-14`）；CI 的 push 只监听 `branches: [main]`（`ci.yml:4-6`），**tag push 不会触发 ci.yml**。
2. **版本/说明 guard（prepare）**：从 `src/eye_tracker/__init__.py` 读 `__version__`（`build.yml:76-99`），tag 不等于 `v$version` 即失败（`build.yml:84-87`）；计算纯数字版本、prerelease（`build.yml:88-93`）；从 CHANGELOG.md 抽版本节，tag 场景下 `[Unreleased]` 有内容或无对应节即失败（`build.yml:101-209`）。
3. **三平台构建**：`build-windows`（`build.yml:221-437`）、`build-macos`（`build.yml:439-583`）、`build-linux`（`build.yml:585-689`），均 `needs: prepare`；每平台：`uv sync --locked --python <setup-python path>` → `fetch_models.py --check` → `make_icons.py` → PyInstaller → `check_privacy.py --bundle` → 打包 → `smoke_test_bundle.py`。
4. **上传**：仅 `inputs.upload` 为真时上传（Windows `build.yml:427-437`；macOS `575-583`；Linux `679-689`；release-notes `212-219`），保留 14 天，release-notes 7 天。
5. **发布 job（仅 tag）**：`if: github.ref_type == 'tag'`（`release.yml:39`）→ 下载全部 artifact（`53-57`）→ 校验 5 个固定文件名非空（`59-75`）→ `sha256sum` 生成 `SHA256SUMS.txt`（`73`）→ `actions/attest-build-provenance`（`77-87`）→ `softprops/action-gh-release` 创建/更新 Release（`89-103`）→ 非 prerelease 时 `gh workflow run pages.yml --ref main`（`105-112`）。
6. **网站**：`pages.yml` 被 release 事件或手动 dispatch 触发（`pages.yml:18-21`），用 `gh api releases/latest` 生成下载链接（`pages.yml:50-56`）后部署。
7. **应用内更新契约**：只认安装器名 `EyeTracker-<ver>-windows-x64-setup.exe` 与 `SHA256SUMS.txt`（`src/eye_tracker/update/release.py:25-31,61-63`；`tests/test_update_release.py:13-31`；docs/building.md:186-189），与 `release.yml:63-69` 的固定列表一致。
8. **现状证据**：`__version__ = "0.3.0"`（`src/eye_tracker/__init__.py:3`）、CHANGELOG 有 `## [0.3.0] - 2026-10-02`（`CHANGELOG.md:8`）、本仓库 tag 含 `v0.1.0`…`v0.3.0`，说明链路已实际使用。

### B2. `scripts/` 与 `packaging/` 的职责/CI 角色

| 组件 | 职责 | 在 CI/发布中的位置 |
|---|---|---|
| `scripts/check_privacy.py` | 静态扫描 `src/eye_tracker` 网络/帧落盘代码；`--bundle DIR` 扫描冻结产物中的原生库/压缩 Python（`scripts/check_privacy.py:1-140,2134-2163`） | 源码 gate `ci.yml:65-66`；bundle gate `build.yml:261-262,477-478,651-652`（打包前执行） |
| `scripts/fetch_models.py` | 下载/校验人脸模型与许可证；`--check` 只校验不联网（`fetch_models.py:9-28,336-384`） | `ci.yml:68-69`；三个 build job（`build.yml:252-253,466-467,629-630`） |
| `scripts/make_icons.py` | 从应用图标代码渲染 PNG/ICO/ICNS（`make_icons.py:1-28,82-116`） | 三个 build job 在 PyInstaller 前执行（`build.yml:255-256,469-470,632-633`）；CI 不检查提交图标新鲜度 |
| `scripts/smoke_test_bundle.py` | 通过 CLI 校验版本/frozen/模型/双 backend/视频解码/摄像头探测（`smoke_test_bundle.py:15-29,122-170`） | Windows bundle/安装副本、macOS DMG 内 CLI、Linux AppImage（`build.yml:266-271,345-346,541-573,659-665`） |
| `scripts/gen_config_docs.py` | 从 `eye_tracker.config` 生成 `docs/configuration.md`；`--check` 校验（`gen_config_docs.py:1-9,159-172`） | 仅通过 pytest 间接执行（`tests/test_docs.py:17-28`），无独立 CI step |
| `scripts/build_site.py` | 用 latest release JSON 渲染 `site/` 到 `_site`（`build_site.py:1-13,105-131`） | 仅 `pages.yml:58-59`；`tests/test_site.py` 做无网构建测试 |
| `packaging/pyinstaller/eye-tracker.spec` | 各平台 onedir 构建、模型/许可证收集、大小裁剪、悬空符号链接检查、Info.plist（`eye-tracker.spec:1-27,74-125,809-896,991-1168`） | 三个 build job 的 `pyinstaller ... --noconfirm --clean`（`build.yml:258-259,474-475,635-636`） |
| `packaging/linux/build_appimage.sh` | 生成 AppImage + tar.gz；appimagetool/runtime 版本与 SHA-256 pin（`build_appimage.sh:40-53,134-155,193-239`） | `build.yml:656-657`；下载发生在构建时 |
| `packaging/macos/make_dmg.sh` | 悬空链接检查、codesign、hdiutil 生成 DMG（`make_dmg.sh:62-98,108-128`） | `build.yml:527-533` |
| `packaging/windows/installer.iss` | 免管理员 per-user Inno Setup、PATH/自启/升级/卸载逻辑（`installer.iss:1-30,60-110,130-155`） | `build.yml:278-295` + 安装/升级/卸载冒烟（`build.yml:303-425`） |

`pyproject.toml` 无 release/build 命令脚本；入口只有 `eye-tracker` / `eye-tracker-gui`（`pyproject.toml:65-69`），构建/发布完全由 YAML 编排；`dev` 组 pytest/ruff/mypy，`build` 组 pyinstaller/pillow（`pyproject.toml:71-83`）。

### B3. 安全基线（现状良好，非问题）

- 26 条外部 `uses:`（10 种 action）全部 40 位 SHA + `# vX.Y.Z` 注释（`grep uses .github/workflows/ | grep -v './'` 计数；`tests/test_build_scripts.py:663-680` 强制）。
- 默认 `permissions: contents: read`；写权限只出现在 release job 与 pages deploy job（`release.yml:43-49`、`pages.yml:74-76`）。
- 无 `pull_request_target` / `workflow_run`（全仓 grep 为空）；所有 checkout 都 `persist-credentials: false`（如 `ci.yml:32-34`、`build.yml:72-74`）。
- 只有 macOS 签名 secrets（`release.yml:31-35`）；`bundle.yml` 不传 secret（`bundle.yml:44-58`），PR 构建无法拿到签名材料。
- 无 `secrets: inherit`；`tests/test_build_scripts.py:740-775` 对权限/secret 做了回归断言。

## C. 本地门禁可复现性对照表

| CI 检查/步骤 | CI 位置与命令 | 本地等价命令 | 差异 / 缺口 |
|---|---|---|---|
| Ruff lint | `ci.yml:45-46` `uv run --no-sync ruff check --output-format=github .` | `uv run ruff check .`（CONTRIBUTING:44） | ✅ 等价（本地不使用 `--no-sync`） |
| Ruff format | `ci.yml:48-49` | `uv run ruff format --check .`（CONTRIBUTING:45） | ✅ |
| mypy 包（默认/win32/darwin） | `ci.yml:51-60` | `uv run mypy` / `--platform linux` / `--platform win32` / `--platform darwin`（CONTRIBUTING:46-48） | ⚠️ CI 默认 step 在 Linux 上等价 linux；本地需三条都跑 |
| mypy 脚本 | `ci.yml:62-63` | `uv run mypy scripts packaging/pyinstaller/entry.py`（CONTRIBUTING:49） | ✅ |
| 源码隐私 gate | `ci.yml:65-66` | `uv run python scripts/check_privacy.py`（CONTRIBUTING:50） | ✅ |
| 模型校验 | `ci.yml:68-69` | `uv run python scripts/fetch_models.py --check`（CONTRIBUTING:51） | ✅ |
| shellcheck | `ci.yml:71-76`（apt 安装后跑 3 个脚本） | CONTRIBUTING:56-57 只有一句说明，**无命令** | ⚠️ CI 有、本地无成文命令；docs/CI.md §2.3 记录本机未装 tool |
| desktop-file-validate | `ci.yml:76` | 同上，无命令 | ⚠️ 同上 |
| pytest 全量 | `ci.yml:118-119`（5 个 job：3 OS × 3.12 + ubuntu 3.11/3.13） | `uv run pytest`（CONTRIBUTING:52） | ✅ 可跑；`docs/CI.md` §2.3 记录队友本机 2952 passed / 55 skipped（非本次复跑） |
| 配置文档同步 | 仅 `tests/test_docs.py:17-28` 间接执行 | `uv run python scripts/gen_config_docs.py --check`（CONTRIBUTING:55-56） | ⚠️ CI 无独立 step，失败信息藏在 pytest 里 |
| PyInstaller 构建 | `build.yml:258-259,474-475,635-636`（每 OS） | docs/building.md:18-23 单步命令；需对应 OS（docs/building.md:5-6） | ⚠️ 无一条命令复现完整 build.yml |
| bundle 隐私 gate | `build.yml:261-262,477-478,651-652` | `uv run python scripts/check_privacy.py --bundle <dist>`（docs/building.md:43-47） | ✅ |
| `smoke_test_bundle.py` | `build.yml:266-271,345-346,573,664` | docs/building.md:63-67 | ✅ 脚本可本地跑；依赖对应 OS 的冻结产物 |
| Windows 安装/升级/卸载冒烟 | `build.yml:303-425`（120 行内联 PowerShell，改 HKCU PATH/自启） | **无**（docs/building.md 只给 iscc 命令，无冒烟脚本） | ❌ CI-only，本地无法复现 |
| macOS 签名 + 挂载 DMG 冒烟 | `build.yml:483-573` | docs/building.md:95-98 可生成 dmg；挂载/plutil/codesign 断言未脚本化 | ⚠️ 部分可手工复现；签名证书 secret 本地需自备 |
| Linux AppImage/tar 冒烟 | `build.yml:656-677` | docs/building.md:109-114 | ✅ 构建可跑；完整断言未脚本化 |
| 网站构建 | `pages.yml:47-59` | docs/building.md:174-180 | ✅ `build_site.py` 可本地跑；Pages 部署不可本地复现 |
| release 校验/attest/发布 | `release.yml:53-112` | **无** | ❌ CI-only（设计使然） |
| 覆盖率 | 无 | 无 | ❌ CI 与本地都没有 |
| 漏洞/secret/静态安全扫描 | 无 | 无 | ❌ CI 与本地都没有 |
| workflow YAML 语法（actionlint） | 无 | 无 | ❌ 只靠 GitHub 端与 `tests/test_build_scripts.py` 文本断言 |

“本地有但 CI 不跑”：`scripts/make_icons.py --check` 未被任何 workflow 调用（只查文件存在，见 `make_icons.py:114-116,164-171`）；`gen_config_docs.py` 的写模式、`fetch_models.py` 的下载模式只在开发时用，CI 仅 `--check`（符合预期）。

## D. 问题清单

### D1. 发布链路与版本（GW-CI-01 … 10）

- GW-CI-01 | Release 发布链路不依赖测试门禁 | `.github/workflows/release.yml:10-14,24-41`；`.github/workflows/ci.yml:3-8,119` | 发布门禁 | **P1** | tag push 只进入 release.yml；ci.yml 的 push 仅 `branches: [main]`，tag 不触发；全仓 workflow 只有 `ci.yml:119` 跑 pytest（`grep -rn "pytest" .github/workflows/` 仅此一条），release.yml/build.yml 无 pytest/needs CI；因此打 tag 即可发布未经测试套件验证的提交 | release job 前增加 test job（把 ci.yml 的 test 矩阵抽成可复用 `test.yml`，或 `workflow_run` 等待 CI 成功），或把 tag 规则改为“必须由 main 上 CI 通过的 commit 打 tag” | 无
- GW-CI-02 | tag push 自动正式发布，无 draft / environment 审批 | `.github/workflows/release.yml:89-103` | 发布门禁 | P2 | `softprops/action-gh-release` 段无 `draft:`，`release.yml:37-49` 无 `environment:`；失败/误发无法在发布前拦截；docs/CI.md §2.2 也记录“tag 触发即创建正式 Release（非 draft）” | 改为 `draft: true` 由人审批，或给 release job 挂 protected environment；把选择记录到 docs/CI.md | 变更行为
- GW-CI-03 | `workflow_dispatch` 在 tag ref 上仍会发布，与注释“手动只构建不发布”矛盾 | `.github/workflows/release.yml:5-6,14,39` | 发布门禁 | P2 | 注释写 “Run it manually to build the packages without publishing”，但 release job 条件是 `if: github.ref_type == 'tag'`；GitHub 允许 dispatch 选择 tag ref，此时 ref_type=tag，会走发布 job | 条件改为 `github.event_name == 'push' && github.ref_type == 'tag'`（或加 `publish` boolean input） | 变更行为
- GW-CI-04 | tag 可指向任意 commit，发布不校验它是否在 main/受保护分支上 | `.github/workflows/release.yml:11-13`；`.github/workflows/build.yml:84-87` | 发布门禁 | P2 | 触发只有 `tags: v*`；唯一的 guard 是 tag 名与 `__version__` 字符串相等；没有 `git merge-base --is-ancestor`/branch/tag 保护检查 | 在 prepare 中断言 tag commit 是默认分支祖先（或改用受保护的 tag 规则） | 变更行为
- GW-CI-05 | 版本解析有 4 套实现，语义/健壮性不一致 | `.github/workflows/build.yml:79`；`packaging/pyinstaller/eye-tracker.spec:74-101`；`packaging/linux/build_appimage.sh:69-72`；`pyproject.toml:85-86` | 腐烂/版本 | P2 | build.yml 与 build_appimage.sh 用 `sed -n 's/^__version__ = "\(.*\)"$/\1/p'`（单引号/类型注解/空格变化即读不到）；spec 用 AST 读字符串常量；hatchling 也解析同一文件 | 抽 `scripts/version.py`（AST 解析）供三处调用，并加单测 | 无
- GW-CI-06 | `numeric` 版本为空时仍继续构建 | `.github/workflows/build.yml:88-89`；`packaging/windows/installer.iss:27-30,72-74` | 发布链路/健壮性 | P2 | `numeric="$(grep -oE '^[0-9]+(\.[0-9]+){0,2}' <<<"$version")"` 无空值检查；`__version__` 若为 `dev`/`nightly` 会得到空串，ISO 的 `VersionInfoVersion=` 非法（本地命令默认 `AppVersionNumeric=AppVersion`） | 在 prepare 中校验 `numeric` 非空并给出注释错误 | 修复bug
- GW-CI-07 | 发布说明/资产名/仓库地址硬编码且多处重复 | `.github/workflows/build.yml:117,157-171,198-201`；`.github/workflows/release.yml:63-69` | 腐烂/发布链接 | P2 | footer 写死 `github.com/bugraskl/eye-tracker/blob/main/docs` 与 5 个 `EyeTracker-$VERSION-*` 文件名；release.yml 再写一遍同样的 5 个名字；组织/分支重命名或新增资产时多处漂移 | 由 `github.repository`/`GITHUB_REF` 派生 URL，资产名抽一份 manifest 或共享脚本 | 无
- GW-CI-08 | macOS 稳定签名是可选 secret，release 仍可在 ad-hoc 下成功 | `.github/workflows/build.yml:483-496`；`.github/workflows/release.yml:31-35` | 发布质量/签名 | P2 | `if [[ -z "$CERT_P12" ]]; then ... exit 0`；release.yml 三个 secret 都是可选映射；无 secret 时发布 ad-hoc 包，用户升级后需重新授权无障碍权限（release notes 也这么写，build.yml:179-197） | 在 tag 发布时把证书设为必需（缺失则 fail），或把“允许 ad-hoc 发布”作为显式开关记录 | 变更行为
- GW-CI-09 | Windows 安装器/便携包无代码签名 | `packaging/windows/installer.iss:60-110`（`grep -n "SignTool" installer.iss` 无结果）；`docs/platform-support.md:82`；`README.md:71` | 发布质量/安全 | P2 | 安装器无 `SignTool` 指令，发布说明明确“not signed with a paid certificate”（`build.yml:179-181`），SmartScreen 会拦截；文档已承认 | 引入 Windows 代码签名 secret 与签名步骤（可先做可选），或在发布检查中明确标注 | 无
- GW-CI-10 | macOS 包未 notarize，首次启动仍需用户绕过 Gatekeeper | `packaging/macos/make_dmg.sh:77-98`；`docs/platform-support.md:97`；`README.md:301` | 发布质量/安全 | P2 | make_dmg.sh 只做 `codesign`（可选证书；`--timestamp=none`），没有 `notarytool`/staple；README roadmap 把“Signed and notarised builds”列为未完成 | 规划 Apple Developer ID + notarization（至少加可选 job），并补文档 | 无

### D2. CI 结构、触发与成本（GW-CI-11 … 21）

- GW-CI-11 | `ci.yml` 无 `paths`/`paths-ignore`，文档类 PR 也跑 5 个 job | `.github/workflows/ci.yml:3-8,84-95` | CI 成本 | P2 | `pull_request:` 无过滤；矩阵固定产生 lint + 3 个 3.12 job + 2 个 ubuntu 3.11/3.13 job；纯 docs/README PR 也触发三平台测试 | 加 `paths-ignore`（`docs/**`、`**/*.md` 等）或拆 docs-only 轻量 job | 无
- GW-CI-12 | 每个改 `src/**` 的 PR 都跑 3 平台 × 60 min 打包 | `.github/workflows/bundle.yml:14-26`；`.github/workflows/build.yml:225,443,591` | CI 成本 | P2 | bundle 在 PR 路径含 `src/**` 时调用 build.yml；三个 build job 各 `timeout-minutes: 60`，含 PyInstaller/安装/冒烟；多数功能 PR 都会命中 | 用 label/nightly 控制全量 bundle，PR 默认只跑隐私 gate + 单平台打包，或按改动路径分平台 | 无
- GW-CI-13 | `bundle.yml` 的路径列表在 PR/push 两段逐字重复，易漏改 | `.github/workflows/bundle.yml:15-26,30-41` | 腐烂/触发 | P2 | 同一 11 项列表复制两份；`tests/test_build_scripts.py:926-945` 用 `count(...) == 2` 锁定，新增会决定打包结果的脚本时必须改两处+测试 | 用 YAML anchor/单一 `paths` 列表（或生成 job 配置的脚本） | 无
- GW-CI-14 | 测试用 `macos-latest`，发布构建用 `macos-14`；Windows 两端都是 `windows-latest` | `.github/workflows/ci.yml:85-88`；`.github/workflows/build.yml:442` | 可复现性 | P2 | 测试 runner 随 `macos-latest` 漂移，发布固定在 `macos-14`；可能出现“测试 OS 通过、发布 OS 失败”或反之 | 测试 matrix 与 build 使用同一 runner 标签（或至少同一 macOS 大版本） | 无
- GW-CI-15 | CI 使用 uv 托管的 Python，发布使用 setup-python 的 CPython，解释器来源不同 | `.github/workflows/ci.yml:36-40,109-113`；`.github/workflows/build.yml:48-51,238-250,452-456,615-619` | 可复现性 | P2 | ci.yml 只给 setup-uv `python-version`；build.yml 用 `actions/setup-python` 并设 `UV_PYTHON_DOWNLOADS: never`（明确要求 python.org/distro CPython） | 让 ci.yml 也用 setup-python（或统一 `UV_PYTHON_DOWNLOADS`/解释器来源） | 无
- GW-CI-16 | `ci.yml` 的 `uv sync/uv run` 未显式传 `matrix.python`，版本选择依赖 `.python-version` 优先级 | `.github/workflows/ci.yml:109-118`；`.python-version:1`；对照 `.github/workflows/build.yml:250` | 可复现性 | P2 | ci 的 `uv sync --locked` 无 `--python`，而 build.yml 显式 `--python "${{ steps.python.outputs.python-path }}"`；本地 uv 0.12.0 实测：目录有 `.python-version=3.12` 时，即使 PATH 首位是 3.14，`uv run --no-project python -c '...'` 仍返回 3.12.6，只有 `--python 3.14`（或 `UV_PYTHON=3.14`）才返回 3.14.6；setup-uv 是否设置 `UV_PYTHON` 需实测确认 | 在 ci 的 sync/run 显式加 `--python "${{ matrix.python }}"`，并打印 `python -V` 断言版本 | 无
- GW-CI-17 | 默认安装 dev+build 两组依赖，测试/ lint job 也装 PyInstaller/Pillow | `pyproject.toml:82-83`；`.github/workflows/ci.yml:42-43,115-116` | CI 成本 | P2 | `[tool.uv] default-groups = ["dev", "build"]`；ci 的 5 个 test job + lint job 都 `uv sync --locked`，无需 build 组（PyInstaller/Pillow 仅打包用） | test/lint 用 `uv sync --locked --no-group build`（或单独 `test` 组） | 无
- GW-CI-18 | 声明支持 Python `<3.15`，但 matrix 无 3.14 | `pyproject.toml:14,37-39`；`.github/workflows/ci.yml:84-95` | 门禁覆盖 | P2 | `requires-python = ">=3.11,<3.15"`，classifier 只到 3.13；matrix 仅 3.11/3.12/3.13；`uv.lock` 已声明 `requires-python = ">=3.11, <3.15"`，但无 3.14 job | 加 3.14 到 matrix/classifier，或把 `requires-python` 收窄到 `<3.14` | 无
- GW-CI-19 | 3.11/3.13 只在 ubuntu 测；Windows/macOS 只覆盖 3.12 | `.github/workflows/ci.yml:84-95` | 门禁覆盖 | P2 | `include` 只追加 `os: ubuntu-24.04` 的 3.11/3.13；Windows/macOS 只跑 3.12 | 至少在一个非 Linux OS 上各加一个最老/最新 Python，或用平台相关代码只在对应 OS 测 | 无
- GW-CI-20 | release 的 Windows runner 用浮动 `windows-latest`（另外两个固定） | `.github/workflows/build.yml:224`（对照 442/590）；`tests/test_build_scripts.py:718-724` 锁定 | 可复现性 | P2 | `runs-on: windows-latest` 会随镜像更新；macOS 固定 `macos-14`、Linux 固定 `ubuntu-22.04`；Windows 工具链（MSVC/7z/Inno）随之漂移 | 固定到 `windows-2022`/`windows-2025`，并在 docs/CI.md 记录支持窗口 | 无
- GW-CI-21 | shellcheck/desktop-file-utils 与 apt 包版本未 pin，每次 apt update | `.github/workflows/ci.yml:71-76`；`.github/workflows/build.yml:602-613` | 可复现性 | P2 | `sudo apt-get update && sudo apt-get install -y --no-install-recommends desktop-file-utils shellcheck` 无版本；build-linux 每次装 18 个包；runner 镜像升级会改变 shellcheck 规则/包版本 | 固定工具版本（或容器镜像），缓存 apt 层；把 shellcheck 版本写入 docs/CI.md | 无

### D3. 本地可复现性与门禁缺口（GW-CI-22 … 37）

- GW-CI-22 | 发布链路没有一条本地命令可复现（尤其 Windows 安装器冒烟） | `docs/building.md:16-128`；`.github/workflows/build.yml:303-425` | 可复现性 | P2 | docs 只给分步命令；`build.yml:303-425` 的安装/升级/自启/PATH/卸载断言是 120 行内联 PowerShell，无 `scripts/installer_smoke.ps1` 可复用；docs/CI.md §2.3 记录本机未验证打包脚本 lint，安装器冒烟本身也无本地脚本 | 把内联脚本抽到 `scripts/` 并提供 `scripts/ci-local.py` 入口；在 docs 写明可跑/不可跑项 | 无
- GW-CI-23 | CONTRIBUTING 声称“跑 CI 的全部检查”，但 shellcheck/desktop-file-validate 无命令 | `CONTRIBUTING.md:41-57`；`.github/workflows/ci.yml:71-76` | 本地门禁 | P2 | 代码块只列 ruff/mypy/privacy/models/pytest；`shellcheck` 与 `desktop-file-validate` 仅在 prose 中被提到“CI also runs”；本地无复制即用命令 | 在 CONTRIBUTING 加这两条命令与依赖安装说明（或 `scripts/check_packaging.sh`） | 无
- GW-CI-24 | PR 模板的 mypy 检查弱于 CI | `.github/PULL_REQUEST_TEMPLATE.md:25`；`.github/workflows/ci.yml:51-63` | 本地门禁/文档漂移 | P2 | 模板只勾 `uv run mypy`（config 的 files=src）；CI 还跑 `--platform win32/darwin` 与 `mypy scripts packaging/pyinstaller/entry.py` | 模板改成与 CONTRIBUTING 相同的 4 条命令，或引用一个脚本 | 无
- GW-CI-25 | 构建时重新生成图标，但不校验提交的图标是否最新 | `.github/workflows/build.yml:255-256,469-470,632-633`；`scripts/make_icons.py:114-116,164-171` | 门禁缺口 | P2 | CI 执行 `make_icons.py`（写文件）而非 `--check`；`--check` 只判断文件是否存在，不比较内容；提交的 `assets/icon.png`/packaging 图标可能与 `ui/icons.py` 源不一致而 CI 全绿 | 让 CI 在 `--out` 临时目录渲染并比对 hash，或 `git diff --exit-code assets packaging`；`--check` 改为内容校验 | 无
- GW-CI-26 | 无覆盖率工具与覆盖率门禁 | `pyproject.toml:71-76`；`.github/workflows/ci.yml:118-119` | 门禁缺口 | P2 | dev 组只有 pytest/ruff/mypy；`grep -n "pytest-cov\|coverage" pyproject.toml uv.lock` 无结果；CI 只跑 `pytest -q --durations=15` | 加 pytest-cov（或 coverage.py）与整体/关键模块阈值，产出报告 artifact | 无
- GW-CI-27 | 无依赖漏洞、secret、CodeQL 等安全扫描门禁 | `.github/workflows/`（仅 5 个 workflow）；`grep -rln "codeql\|gitleaks\|pip-audit\|trufflehog\|osv-scanner" .github/ pyproject.toml uv.lock` 无结果；`.github/dependabot.yml` 仅版本更新 | 安全门禁 | P2 | 没有审计 job；Dependabot 只做常规升级，不产生“已知漏洞阻断”；没有 secret scanning/CodeQL 配置 | 增加 `uv run pip-audit`（或 OSV）job、secret scanning、CodeQL；先以 report-only 起步 | 无
- GW-CI-28 | 无 pre-commit/本地 hook | 仓库根无 `.pre-commit-config.yaml`（`ls` 不存在）；`CONTRIBUTING.md:39-57` 依赖人工 | 本地门禁 | P2 | 所有检查靠 contributor 记得手动跑；PR 模板/CONTRIBUTING 只是 checklist | 加 `.pre-commit-config.yaml`（ruff/format/mypy/privacy 子集），与 CI 共用版本 | 无
- GW-CI-29 | 真实人脸测试在 CI 永远 skip | `tests/conftest.py:43-53`；`.github/workflows/*.yml` 无 `EYE_TRACKER_TEST_FACE`（grep 为空） | 测试覆盖 | P2 | `face_image` fixture 在没有环境变量/文件时 `pytest.skip`；CI 从未设置；`tests/test_facemesh.py`、`tests/test_backends.py` 依赖它 | 增加一个带真人脸样本的受限 job/artifact（注意隐私：样本不入库，用受控 secret/对象存储），或明确记录该覆盖缺口 | 无
- GW-CI-30 | mypy 不检查 `tests/` | `pyproject.toml:122-127`；`.github/workflows/ci.yml:62-63` | 门禁缺口 | P2 | `files = ["src/eye_tracker"]`；CI 显式只加 `scripts packaging/pyinstaller/entry.py`；tests 的类型错误不会被门禁捕获 | 增加 `mypy tests`（可先 per-file ignore 过渡） | 无
- GW-CI-31 | 无 actionlint/yamllint，workflow YAML 只在 GitHub 端验证 | 全仓 `grep -rln "actionlint\|yamllint"` 无结果；`tests/test_build_scripts.py:641-659` 用正则文本断言 | 门禁缺口 | P2 | 语法错误/无效表达式只能等 push 后 GitHub 解析失败；现有 guard 测试只匹配字符串，不能验证 YAML/Actions 语义 | 加 actionlint（release 二进制或容器）到 lint job，并加 shellcheck 同等门禁 | 无
- GW-CI-32 | 2597 行 guard 测试逐字断言 YAML 文本，改格式即红且无法发现语义错误 | `tests/test_build_scripts.py:641-659,762`（全文 2597 行） | 腐烂/测试维护 | P2 | `_jobs`/`_steps` 用两空格缩进正则切分文本；`assert "    if: github.ref_type == 'tag'\n" in publish`（762 行）；重排/换引号/用 YAML anchor 都会让测试失败 | 仅保留关键语义断言，改用 YAML parser + actionlint 做结构校验；把大段 workflow 逻辑抽成脚本后单测脚本 | 无
- GW-CI-33 | 打包后的 GUI/托盘没有功能级冒烟，只测 CLI 与 `--version` | `.github/workflows/build.yml:339`（GUI exe `--version`）、`build.yml:541-573`（macOS 只跑 CLI）；`scripts/smoke_test_bundle.py:15-29` | 测试覆盖 | P2 | Windows 对 `eye-tracker.exe --version` 只验退出码；macOS 只 smoke `Contents/MacOS/eye-tracker-cli`；无 GUI 启动/托盘/设置页的 offscreen 启动测试 | 加 `QT_QPA_PLATFORM=offscreen` 的 GUI 启动冒烟（启动→读 doctor/退出），或至少对 GUI exe 做 `--help`/短超时启动断言 | 无
- GW-CI-34 | 文档称“每个 workflow 都在 concurrency group”，build.yml 实际没有 | `docs/building.md:162-164`；`.github/workflows/build.yml:17-18`；`tests/test_build_scripts.py:783-793` | 文档漂移 | P2 | build.yml 只有 `on: workflow_call`，无 `concurrency`（test 明确断言 `"concurrency" not in _workflow("build.yml")`），依赖调用方 group | 文档改为“除被复用的 build.yml 外；build.yml 继承调用方 concurrency” | 无
- GW-CI-35 | 本地 Windows 安装器命令漏传 `AppVersionNumeric`，预发布版本会生成非法版本资源 | `docs/building.md:73-76`；`packaging/windows/installer.iss:27-30,72-74`；对照 `.github/workflows/build.yml:294` | 本地可复现性 | P2 | docs 只给 `iscc /DAppVersion=0.2.0`；ISO 的 `#ifndef AppVersionNumeric` 默认等于 `AppVersion`；对 `0.3.0rc1` 会写 `VersionInfoVersion=0.3.0rc1`（非纯数字） | 文档命令补 `/DAppVersionNumeric=<x.y.z>`，并说明从 `__version__` 去字母 | 无
- GW-CI-36 | pytest 全局忽略 `DeprecationWarning`，升级弃用不会进 CI 视野 | `pyproject.toml:117-120` | 门禁缺口 | P2 | `filterwarnings = ["ignore::DeprecationWarning"]`；PySide6/依赖的弃用告警被吞，未来版本断点无法提前暴露 | 改为只 ignore 已知第三方告警（模块前缀），其余保持可见；CI 加 `-W error::DeprecationWarning`（分阶段） | 无
- GW-CI-37 | mypy `python_version = "3.12"` 与 `requires-python >=3.11` 不一致 | `pyproject.toml:14,122-127` | 门禁覆盖 | P2 | 声明支持 3.11，但 mypy 始终以 3.12 语法/标准库检查；3.11 专属不兼容可能只在 3.11 test job 运行时才暴露 | 将 mypy 改为 3.11（或按 test matrix 分版本跑），或把 `requires-python` 下限提高 | 无

### D4. 安全 / 供应链（GW-CI-38 … 43）

- GW-CI-38 | 第三方 `softprops/action-gh-release` 持有 `contents: write` 并接收发布说明 | `.github/workflows/release.yml:89-103` | 供应链 | P2 | 该 action 是第三方（SHA pin 缓解 tag 漂移，但仍以 release job token 运行）；`tag_name`/`body_path`/`files` 都交给它 | 优先用官方 `gh release create/upload`；若保留，固定 SHA + 定期审计 + 最小权限（单独 job） | 无
- GW-CI-39 | release job 为触发 pages 持有 `actions: write`，与其发布职责混在一起 | `.github/workflows/release.yml:43-49,105-112` | 权限最小化 | P2 | 同一 job 同时有 `contents: write`、`id-token: write`、`attestations: write`、`actions: write`；`actions: write` 仅用于最后 `gh workflow run pages.yml`，却能取消/重跑任意 workflow | 把“触发 pages”拆到独立 job（只 `actions: write`），或用 `release: published` + 人工/`workflow_run` 机制；评估 `gh workflow run` 的替代 | 无
- GW-CI-40 | 发布构建在运行时下载/安装外部工具，且版本未全部 pin | `.github/workflows/build.yml:281-295,276`；`packaging/linux/build_appimage.sh:134-155` | 供应链/可复现性 | P2 | Windows 缺 Inno Setup 时 `choco install innosetup`（无版本），且硬编码 `Inno Setup 6` 路径；`7z` 未声明；Linux 构建时从 GitHub 下载 appimagetool/runtime（有 SHA-256，但仍依赖网络与上游可用性） | 预装/缓存并按版本 pin Inno Setup 与 7z；优先用打包好的工具镜像；在 docs 记录外部工具清单 | 无
- GW-CI-41 | Pages 用 `pip install "pillow>=10.4"` 绕过 uv.lock | `.github/workflows/pages.yml:47-48` | 供应链/可复现性 | P2 | Pages job 不用 uv/`uv.lock`，每次取最新 Pillow；favicon 渲染结果可能随 Pillow 版本变化，且与项目锁文件无关 | 用 `uv sync --locked --group build`（或 `uv run --with pillow==...`）并固定版本 | 无
- GW-CI-42 | `gh api releases/latest || echo '{}'` 把鉴权/限流错误也静默降级 | `.github/workflows/pages.yml:50-56` | 可靠性 | P2 | 任何非零退出都写 `{}`，站点随后用“releases 页面”兜底并部署成功；404（首次发布）是合理降级，401/403/5xx 不应静默 | 区分 404 与其它错误：404 写 `{}`，其余失败退出；加一次重试 | 无
- GW-CI-43 | Dependabot 把全部 GitHub Actions 合为一个 PR | `.github/dependabot.yml:18-21` | 供应链/维护 | P2 | `patterns: ["*"]` 单组；一次 PR 可能同时升 checkout/setup-uv/upload/download/attest/release 多个 action，CI 变红时难定位；也不利于逐个审计第三方 action | 按 action 或 major 分组（如 GitHub 官方组、astral 组、softprops 单列），保留 SHA+版本注释更新 | 无

### D5. 腐烂 / 重复 / 漂移（GW-CI-44 … 51）

- GW-CI-44 | ruff per-file ignores 含死规则 | `pyproject.toml:100,107,115` | 死配置 | P2 | `PLR2004` 已在全局 `ignore`（107 行），per-file 又列一次；`S101` 属 flake8-bandit，`select` 列表（100 行）未启用 S 规则 | 删除 `tests/**` 中的 `PLR2004`/`S101`，只留真正生效的 `PLC2701` | 无
- GW-CI-45 | Python 版本硬编码分散 4 处 | `.python-version:1`；`.github/workflows/ci.yml:39`；`.github/workflows/build.yml:48`；`.github/workflows/pages.yml:45` | 腐烂/漂移 | P2 | 四处都写 3.12；升级 Python 时容易漏一处；build.yml 的 `PYTHON_VERSION` 还同时喂给三个 setup-python | 统一用 `python-version-file: .python-version`（CI/test/build/pages）或单一 env | 无
- GW-CI-46 | build.yml 689 行，发布说明与安装器冒烟都是内联脚本 | `.github/workflows/build.yml:101-209,303-425` | 可维护性 | P2 | 100 行内联 Python 做 CHANGELOG 解析，120 行内联 PowerShell 做安装/升级/PATH/自启矩阵；只能通过 YAML 文本断言测试（`tests/test_build_scripts.py` 从 YAML 里抽脚本执行） | 抽成 `scripts/release_notes.py`、`scripts/installer_smoke.ps1`，由 CI 调用并单测；YAML 只保留编排 | 无
- GW-CI-47 | 资产名/架构名在 5 处重复 | `.github/workflows/build.yml:164-168`；`.github/workflows/release.yml:63-69`；`packaging/windows/installer.iss:102`；`packaging/linux/build_appimage.sh:82-84`；`packaging/macos/make_dmg.sh:58-59` | 腐烂/发布 | P2 | 发布说明、release 校验、Inno 输出名、AppImage/tar 名、dmg 名各自拼 `EyeTracker-$VERSION-<os>-<arch>...`；改命名规则需同步 5 处（更新器还依赖其中 2 个名字，见 `src/eye_tracker/update/release.py:30,61-63`） | 生成一份资产清单（脚本/JSON），build/release/docs 共用；更新器测试继续断言两个稳定名 | 无
- GW-CI-48 | macOS 签名使用已不推荐的 `codesign --deep` | `packaging/macos/make_dmg.sh:77` | 腐烂/签名质量 | P2 | `SIGN_ARGS=(--force --deep --sign ...)`；Apple 长期建议显式签 nested code，`--deep` 可能漏签或误签新嵌套组件 | 逐个签 `Contents/Frameworks` 内组件后再签 app，或改用 `--deep` 的现代替代并验证 | 无
- GW-CI-49 | DMG 仍用 HFS+ 文件系统创建 | `packaging/macos/make_dmg.sh:108-119` | 腐烂/平台 | P2 | `hdiutil create -fs HFS+ ...`；新 macOS 上 HFS+ 创建/校验的长期支持不确定，CI 未来可能红 | 评估 APFS（或至少加 `hdiutil verify` 的兼容测试与升级路径），先在本地/实验 job 验证 | 无
- GW-CI-50 | 打包脚本按 `uname -m` 命名，而 release 固定期望 `macos-arm64`/`linux-x86_64` | `packaging/macos/make_dmg.sh:58-59`；`packaging/linux/build_appimage.sh:73,82-84`；`.github/workflows/build.yml:580,685`；`.github/workflows/release.yml:63-69` | 平台一致性 | P2 | 脚本用主机架构生成文件名，release 校验/上传用固定名；若 runner 架构或本地跨架构构建变化，会生成 `macos-x86_64.dmg`/`linux-aarch64.*` 导致 upload/checksum 失败或错标 | 显式传 `--arch`（由 CI 从 matrix 传入），release 列表由同一变量生成 | 无
- GW-CI-51 | build-linux 安装了 `desktop-file-utils`，但该 job 未使用 | `.github/workflows/build.yml:602-613`（613 行含 `desktop-file-utils`）；对照 `ci.yml:76` 才调用 `desktop-file-validate` | 死配置 | P2 | build-linux 的后续步骤只做 find/隐私 gate/AppImage/smoke，没有 desktop-file-validate 调用；这是每次 release 多装一个无用 apt 包 | 从 build-linux apt 列表删除，或在该 job 真正加一步 `desktop-file-validate` | 无

## E. 无法确认 / 需人工决策项

- E-01 | 分支保护/required checks 不可见：ruleset/branch protection 是 GitHub Settings，不在仓库文件里；无法确认 `ci.yml`/`bundle.yml` 是否为合并前必需检查，也无法确认 tag 是否受保护。若未配置，GW-CI-01 的实际风险更高。证据：`.github/` 只有 CODEOWNERS、dependabot.yml、ISSUE_TEMPLATE、workflows；仓库内无 ruleset 文件。
- E-02 | Release 的 environment 保护/审批规则在仓库外（GitHub Settings），本地无法确认；GW-CI-02/03 的最终风险取决于此。
- E-03 | secrets 是否存在无法验证：`MACOS_SIGNING_CERT_P12/PASSWORD/IDENTITY` 是否已在仓库配置不可从代码判断；若缺失，release 会走 ad-hoc（GW-CI-08）。证据：`.github/workflows/release.yml:31-35` 仅声明映射。
- E-04 | Action SHA 与版本注释的对应关系、以及上游 action 的可信度需联网核对；本次只验证了“26 个外部 uses 全为 40 位 SHA”（`tests/test_build_scripts.py:663-680`），未验证 SHA 是否确实等于注释中的 tag。
- E-05 | runner 镜像实际内容（Inno Setup 版本/路径、7z、Chocolatey、macOS runner 架构）会随时间变化，需在 GitHub runner-images 文档/实跑中确认；GW-CI-20/40 的影响面依赖此。
- E-06 | `setup-uv` 的 `python-version` 是否设置 `UV_PYTHON`（从而覆盖 `.python-version`）需在 CI 实跑确认；本地 uv 实验只能证明 PATH 不会覆盖 `.python-version`（GW-CI-16）。
- E-07 | 发布方式（直接正式发布 vs 先 draft）是产品决策；docs/CI.md §2.2 已记录“直接正式发布（非 draft）”，harness 阶段 3 若要求 draft 需先征得用户同意。相关：GW-CI-02。
- E-08 | 平台范围决策：macOS 仅 arm64（Intel 从源码，`docs/platform-support.md:49`）、Windows 仅 x64、无 Windows arm64；是否扩展 CI 属于产品范围，不在本次只读审计下结论。相关：`README.md:298-303`。
- E-09 | 工作区存在队友未跟踪产物 `docs/CI.md`、`docs/turn-project-to-harness-rules.md`、`scripts/analysis/scan_metrics.py`；它们未入库，不影响本次 CI 结论。若 `docs/*.md` 入库，`tests/test_docs.py:69-84` 会开始扫描这些文档；`docs/CI.md` 与本报告 A/B 节内容重叠，提交前应去重/互链。另：`docs/exec-plans/active/harness-migration.md` 尚不存在（docs/CI.md 引用了它）。
- E-10 | 本次审计未执行任何 workflow（无 GitHub 凭据/不联网），所有“会失败/会发布”判断均来自静态路径与本地只读实验；建议在阶段 3 用 `act`/临时仓库或 `workflow_dispatch` 空跑验证 GW-CI-01/03/16。

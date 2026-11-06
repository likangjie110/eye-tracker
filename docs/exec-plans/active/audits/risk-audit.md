# 安全 / 隐私 / 可靠性审计（阶段 1）

- 审计对象：`/Users/kukan/PycharmProjects/eye-tracker/.orca/worktrees/eye-tracker/lionfish`（HEAD `9644527`，v0.3.0，Python ≥3.11,<3.15）
- 方法：只读静态审查（源码、脚本、CI/打包、docs、`uv.lock`）+ 全仓 grep + 本机权限小实验；未联网、未安装依赖、未执行应用、未 docker。
- 约束遵守：仓库内未写文件、未 `git add/commit`；git 命令均用 `env -u GIT_CONFIG_*` 前缀。
- 结论：**未发现 P0**；1 项 P1（自更新执行未签名的远端二进制），其余为 P2（防御纵深、文档一致性、本地权限、可靠性/性能）。隐私承诺（默认离线、无遥测、帧不落盘、更新检查 opt-in）与实现**总体一致**，证据见 A/B 节。

## A. 威胁与网络调用全景（含 opt-in 证据）

### A.1 网络出口清单（全仓，含脚本/打包）

| # | 入口 | 位置 | 目标 | 触发条件 | 携带数据 | 结论 |
|---|---|---|---|---|---|---|
| 1 | 更新检查/下载（唯一运行时网络代码） | `src/eye_tracker/update/winhttp.py`、`fetch.py`、`service.py`、`installer.py` | `api.github.com`、`github.com`、`objects.githubusercontent.com`、`release-assets.githubusercontent.com`（443/TLS） | 设置 `updates.check=true` 时启动后 90s 起每日一次；或用户点“Check for updates…” | `GET`、`User-Agent: EyeTracker/<版本>`、`Accept`；无 cookie/标识/设置/校准 | ✅ opt-in 且默认关闭，见 A.2 |
| 2 | 模型下载脚本（开发者工具，不随应用运行） | `scripts/fetch_models.py`（`urllib.request`，`:220-235`） | MediaPipe/OpenCV Zoo 的固定 URL | 手动运行且模型缺失/校验失败；CI 只跑 `--check`（`ci.yml:69`） | 固定 URL + 固定 SHA-256；无应用数据 | ⚠️ 默认联网（GW-RISK-12），产品运行期不涉及 |
| 3 | 外部浏览器/系统设置页 | `ui/update_dialog.py:177`（`QDesktopServices.openUrl`）；`platform/macos.py:1226-1230`（`open`）；`platform/windows.py:224-233,1337-1339`（`os.startfile`） | GitHub 发布页、macOS/Windows 隐私设置页 | 用户点击 | 仅 URL/URI 常量或由发布 tag 构造的地址 | ✅ 用户主动，应用自身不发数据 |
| 4 | 本地单实例/控制通道 | `ipc.py:49,476`（`QLocalServer/QLocalSocket`）；`ipc.py:121-164` | 本机 Unix socket / Windows 命名管道 | 启动、`ctl`、桌面快捷键 | 固定白名单命令（`ipc.py:67-81`），单行文本 | ✅ 本地，无网络；身份校验见 GW-RISK-04/05 |
| 5 | Linux 桌面集成 | `platform/linux.py:19,983-1060`（QtDBus）、`platform/linux.py:1743-1762`（`gdbus/loginctl/xdg-screensaver` 等） | 本机 D-Bus / X11 | 轮询锁屏/空闲、锁屏、唤醒 | 时间戳/会话 id | ✅ 本地 |
| 6 | 终端 pane 集成 | `panes/providers/command.py:42-66`、`tmux.py`、`wezterm.py` | 本机 `tmux`/`wezterm cli`/`wsl` | 开启 `panes.*` 且看向终端 | argv 列表，无 shell；pane id 严格校验（`tmux.py:75-76,230-233`；`wezterm.py:211-214`） | ✅ 本地 |
| 7 | OpenCV/FFmpeg 视频源 | `vision/camera.py:583-630,453-460` | 仅本地文件/摄像头 | 用户配置视频源 | 拒绝 URL/协议前缀（`camera.py:64-70,607-611`） | ✅ 路径层面；本地容器内引用远程 URL 见 D1（需复核） |

全仓确认：`src/eye_tracker` 中**没有** `import socket/ssl/http/urllib/requests/asyncio`/`QtNetwork` 远程类；QtNetwork 只导入 `QLocalServer`、`QLocalSocket`（`ipc.py:49`）。唯一的原生网络加载是 `winhttp`，且在 `scripts/check_privacy.py:185-193` 的审查白名单中。

### A.2 更新检查 opt-in 证据链（逐环节）

1. 默认关闭：`config.py:390-398`，`UpdateSettings.check: bool = _opt(False, …)`。
2. 启动只按该设置调度：`app.py:905-914`（`updates.start(self._settings.updates.check)`）；`service.py:197-209`（`set_auto` 才启动 `QTimer`）。
3. 自动检查时序：`service.py:49`（90s）、`service.py:51`（24h）、`service.py:237-244`（到期才 `_begin_check(manual=False)`）。
4. 手动检查：`update_dialog.py:84`（打开窗口时 `check_now()`）、`service.py:216-219`；文档明示设置关闭时也可手动（`docs/privacy.md:32-36`）。
5. 安装必须用户确认：`service.py:221-230`（`install()` 才下载）；`update_dialog.py:115,143,169-170`（按钮动作）。
6. 请求内容：`fetch.py:118-134`（只有 URL + `Accept`）；`winhttp.py:160-163`（只发 `Accept`）；文档 `docs/privacy.md:37-43`。
7. 主机白名单 + 逐跳重定向校验：`fetch.py:26-33,81-96,170-188`；`winhttp.py:156-159`（禁止 WinHTTP 自行重定向）。
8. TLS：WinHTTP 走 Windows 根存储（`winhttp.py:1-16,150`），不引入 OpenSSL/Python ssl。
9. 保留数据：`updates.json` 仅 `last_check`、`notified`（`service.py:102-123`），无标识符。

### A.3 密钥/敏感信息与危险 API 扫描（正证据）

- 硬编码凭据：全仓 grep `token|api_key|password|secret|private key|BEGIN … PRIVATE KEY|credential`，产品代码/脚本/打包中**无**真实密钥；仅 CI 里以 GitHub secrets 引用 macOS 签名证书（`build.yml:24-33,483-507`），`bundle.yml` 无 secrets。
- `.env`：应用**不读取** `.env`/`python-dotenv`；环境变量仅用于路径/会话提示与 OpenCV 日志级别（`paths.py:77`、`ipc.py:133`、`diagnostics.py:292-295,309-310`、`logging_setup.py:189`、`camera.py:72-76`）。
- 日志/诊断：崩溃由 `logging_setup.py:208-239` 写入本地滚动日志，**无崩溃上报 SDK**（全仓无 sentry/crashpad/breakpad/telemetry）；日志不含窗口标题、按键内容、图像/特征（`docs/privacy.md:109`）。
- 帧落盘：全仓无 `imwrite/imencode/VideoWriter/QImage.save`；`ui/preview.py:1-10` 明示只在内存。
- 危险 API：无 `shell=True`、`os.system`、`os.popen`、`eval/exec`、`pickle`、`yaml.load`；7 个 `subprocess` 调用点（6 个文件：`installer.py:142`、`cli.py:531`、`platform/linux.py:1749`、`platform/macos.py:140,684`、`platform/autostart.py:692`、`panes/providers/command.py:51`）全部 argv 列表 + `shell=False` 默认 + 超时/DEVNULL stdin；动态导入均为常量名（`platform/__init__.py:49`、`panes/registry.py:205`、`panes/../command` 解析）；zip 解包只按固定成员名并取 `Path(name).name`（`scripts/fetch_models.py:259-284`）。
- 写盘权限：`settings.json`/`calibration.json` 经 `mkstemp`（0600）→ `os.replace`（`config.py:463-476`，本机实测 0o600）；日志与 `--trace` 为 0644（见 GW-RISK-07）。

## B. 更新链路安全评估

**数据流**：`service._begin_check` → `check_for_update`（`fetch.get(LATEST_RELEASE_URL, max 1 MiB)`）→ `release.parse_release`（tag 必须 `X.Y.Z`；`installer`/`checksums` 的 URL 必须等于由 repo+tag+文件名推导的地址；size 上限）→ `is_newer`（严格大于运行版本 → 防降级）→ 用户确认 → `installer.download_installer`（先取 `SHA256SUMS.txt`（≤64 KiB），把安装器写入 `updates/*.part`（≤400 MiB），校验长度与 SHA-256，通过后 rename）→ `service._start_installer`（Qt 队列回主线程）→ `installer.launch_installer`（`subprocess.Popen([exe, /VERYSILENT, /SUPPRESSMSGBOXES, /NORESTART], DETACHED_PROCESS)`）。

**已做对的**：opt-in 默认关；仅 HTTPS + 4 个 GitHub 主机；重定向逐跳用同一规则重校验；API 响应大小/资源大小/校验文件大小都有上限；安装器 URL 不接受响应里的任意地址；版本严格解析且只升不降；`hmac.compare_digest` 比较哈希；失败/取消删除 `.part`；启动时清理旧下载；签名证书/证书链由 Windows 校验；不发送标识符；隐私文档对残留风险（同 release 的校验和、无签名、attestation 不校验）如实披露（`docs/privacy.md:69-73`）。

**缺口**：① 安装器无 Authenticode/代码签名验证，SHA-256 来自同一 release，只防损坏（GW-RISK-01，P1）；② 校验（`installer.py:124-126`）与执行（`:142`）之间隔着 rename + Qt 队列跳回主线程，存在 TOCTOU（GW-RISK-02，P2）；③ WinHTTP 的 redirect-policy/timeout 设置返回值被丢弃，安全假设无运行时保证（GW-RISK-03，P2）；④ 下载无总时限/最低速率（GW-RISK-14，P2）。降级、主机放通、大小限制、取消/清理路径未发现问题。

## C. 问题清单

格式：`编号 | 标题 | 位置 | 类别 | 严重度 | 证据 | 修复建议 | 是否改变业务行为`（严重度 P0/P1/P2；行为：无/修复bug/变更行为）

- **GW-RISK-01** | 自更新下载的安装器在无 Authenticode/Sigstore 校验下被静默执行 | `src/eye_tracker/update/installer.py:106-126,134-149`；`docs/privacy.md:69-73` | 安全 | **P1** | `download_installer` 仅比对同一 release 的 `SHA256SUMS.txt`（`:106-126`），`launch_installer` 直接 `Popen` 该 exe 且参数为 `/VERYSILENT`（`:139-149`）；文档自述“校验和与安装器同一 release，只防损坏”“安装器尚未代码签名，app 不校验 Sigstore attestation”（`docs/privacy.md:69-73`）。能替换 GitHub release 两个资产或能在受信 TLS 链路上注入响应者，即可获得当前用户权限执行 | 用 WinVerifyTrust 校验 Authenticode，或内置发布公钥校验 Sigstore/固定签名的 `SHA256SUMS`；至少对校验清单做独立签名 | 无（安全加固，需签名基建） |
- **GW-RISK-02** | 哈希校验与启动安装器之间的 TOCTOU 窗口 | `src/eye_tracker/update/installer.py:124-131`；`src/eye_tracker/update/service.py:312-343` | 安全 | P2 | 校验发生在 `:124-126`，随后 `part.replace(final)`（`:126`），再经 `_finished` 队列信号由主线程 `_start_installer`→`launch_installer`（`service.py:312-343`）执行；期间任何能写 `updates/` 的本机进程可替换文件，启动前无二次校验 | 启动前对同一路径重新计算 SHA-256（或校验 Authenticode）；把下载目录 ACL 收紧为仅当前用户，拒绝 reparse point；缩短“校验→执行”路径（同线程直接启动并二次校验） | 无 |
- **GW-RISK-03** | WinHTTP 安全/可靠性相关设置返回值未检查 | `src/eye_tracker/update/winhttp.py:143,156-159` | 安全/可靠性 | P2 | `WinHttpSetTimeouts(...)` 的 BOOL 返回值被忽略（`:143`）；`WinHttpSetOption(WINHTTP_OPTION_REDIRECT_POLICY, NEVER)` 的返回值也被忽略（`:156-159`）。若策略设置失败，“WinHTTP 不自行跟随重定向”这一安全假设无运行时保证，而 `fetch.py:170-188` 的主机白名单只在传输层返回 3xx 时才生效；超时设置失败则读可能按默认值长期阻塞 | 检查两个返回值，失败即抛 `NetworkError`（fail closed）；对取消/失败路径加单测（mock WinHTTP） | 无 |
- **GW-RISK-04** | IPC 身份校验在无法判定时默认放行（fail-open） | `src/eye_tracker/ipc.py:739-756,877-908` | 安全 | P2 | `_client_is_trusted` 对非 Windows 恒 True，Windows 上 `GetNamedPipeClientProcessId`/句柄异常时 `verdict=None` → `return True`，随后仍读取请求（`:748-756`）；`_pipe_name_taken` 对 `WaitNamedPipeW` 的非“存在/不存在/忙”错误按“空闲”处理（`:877-908`），另一账户先建同名 pipe 时仍可能 listen，而 Qt 会把实例挂到该 pipe 已有的安全描述符上（模块文档 `:23-27` 自述此风险） | 身份不可判定时拒绝连接并告警；pipe-name 探测异常时拒绝 listen 并提示控制通道不可用；Unix 侧补 `SO_PEERCRED`/目录归属复核 | 修复bug（异常路径） |
- **GW-RISK-05** | Linux 私有运行目录不可用时回退到共享 `/tmp` 的固定名 socket | `src/eye_tracker/ipc.py:121-128,139-164,436-493,749-750` | 安全/可靠性 | P2 | `_private_temp_dir` 发现目录不属于当前用户/权限过宽时返回 `None`（`:161-163`）；`server_name` 随后 `return name` 交给 Qt，即注释所说“shared /tmp，another account could create the socket first”（`:116-119`）。此时服务端仅靠 `UserAccessOption`（`:476`），Unix 不做对端凭据校验（`:749-750`）；另一本地账户可预占 `/tmp/eye-tracker-<uid>` 与可预测 socket 名，使控制通道不可用（`:484-489` 仅 warning，应用继续运行） | 私有目录不可用时 fail closed（明确报错/禁用 ctl 并提示），或用 `mkdtemp(0700)` 随机目录；Linux 增加 `SO_PEERCRED` 校验 | 修复bug |
- **GW-RISK-06** | 隐私门禁覆盖边界与“任何网络 import/call”文案不符 | `scripts/check_privacy.py:140-144,164-165,631`；`README.md:139`；`docs/privacy.md:10`；`SECURITY.md:49-55` | 隐私/合规 | P2 | 门禁自述“cannot see through values computed at runtime（模块名由变量拼出等）”（`check_privacy.py:140-144`）；默认只扫 `src/eye_tracker`（`:164-165`），跳过 `__pycache__` 等（`:631`）；规则集未覆盖 `eval/exec`、运行时拼接的 `getattr/__import__`、`QDesktopServices.openUrl`/`os.startfile` 的 URL 形态（全仓 grep：无 `openUrl`/`QDesktopServices` 规则；`openUrl` 使用见 `ui/update_dialog.py:177`）。而 README/隐私文档/安全策略称其“fails CI on any networking import or call” | 增加 `eval/exec` 与 `QDesktopServices/openUrl/startfile` URL 规则、可常量折叠的数据流；把文案改成“静态扫描 + bundle 扫描”；把会进入发布物的 `scripts/` 代码也纳入扫描 | 无（门禁与文档） |
- **GW-RISK-07** | 日志与 `--trace` 文件默认 0644，而 settings/calibration 为 0600 | `src/eye_tracker/logging_setup.py:90-104`；`src/eye_tracker/trace.py:36-54`；`src/eye_tracker/config.py:463-476` | 隐私 | P2 | `RotatingFileHandler` 未指定权限（默认 open → 0666&umask=0644）；`TraceWriter` 用 `self.path.open("a")`（`:54`，同样 0644）；本机实验：两者 0o644，而 `atomic_write_text` 的 mkstemp+replace 为 0o600。日志会记录未捕获异常的完整 traceback（含 home 绝对路径，`logging_setup.py:208-239`）；trace 含指针位置/凝视数值（文档 `docs/privacy.md:118-122`）。多用户 Linux 上同机其他账户可读日志/trace | 创建后 `os.chmod(0o600)`（或 `os.open(..., 0o600)` + 自定义 handler），目录 0700；文档说明本地可见性 | 无 |
- **GW-RISK-08** | doctor 报告只脱敏 home 目录，非 home 绝对路径会进入公开 bug 报告 | `src/eye_tracker/diagnostics.py:1043-1075,1078-1093,677-687,711-727` | 隐私 | P2 | `_redact` 仅替换 `Path.home()` 的多种拼写为 `~`（`:1043-1075`）；`_redacted_command`/`_paths_section`/`_shareable_setting` 全部依赖它（`:648-687,719-727`）。`--config-dir /Volumes/…/alice/…`、`pause_for_apps` 的完整路径、AppImage/脚本绝对路径会原样进入报告；docstring 表明报告用于公开 issue，但只承诺不泄露账户名（`:6-10`） | 额外脱敏 `getpass.getuser()` 的独立出现与可识别的非 home 绝对路径（或提供仅含白名单字段的 `--share` 模式） | 无（输出字段可能变化） |
- **GW-RISK-09** | README 绝对化表述与 opt-in 更新检查矛盾 | `README.md:36-38,75-78`；`docs/privacy.md:30-43` | 隐私/合规 | P2 | README 首段称 “never sends a single byte over the network”（`:37-38`），同文件 `:75-78` 与隐私文档说明可选更新检查会发 HTTPS 请求（默认关） | 改为“默认不联网；唯一可联网的是默认关闭的更新检查” | 无（文案） |
- **GW-RISK-10** | 安全/隐私文档版本信息漂移 | `SECURITY.md:10-13`；`docs/privacy.md:37-43`；`src/eye_tracker/__init__.py:3` | 隐私/合规 | P2 | SECURITY 支持版本仍写 `0.1.x (latest)`，实际 `__version__ = "0.3.0"`（CHANGELOG 0.3.0）；privacy.md 的 User-Agent 示例固定为 `EyeTracker/0.2.1`，实际由 `__version__` 拼接（`update/winhttp.py:33`） | 改为“latest release (0.3.x)”并让文档由版本号生成/由 CI 校验；加文档-版本一致性测试 | 无 |
- **GW-RISK-11** | 无依赖漏洞扫描；生产依赖主版本无上界（lock 已解析到 OpenCV 5 / numpy 2.5 / Qt 6.11） | `pyproject.toml:44-55`；`uv.lock:442-450,306-311,388-393,793-794`；`.github/dependabot.yml:36-51` | 供应链/安全 | P2 | `pyproject.toml` 只写 `>=`（`opencv-python-headless>=4.10`、`PySide6-Essentials>=6.7`、`numpy>=1.26`）；lock 固定 `opencv 5.0.0.93`、`numpy 2.4.6/2.5.3`、`PySide6-Essentials 6.11.2`，与 pyproject 声明一致且有 502 个哈希；CI 用 `uv sync --locked`（`ci.yml:43,116`），但**无** pip-audit/osv-scanner，Dependabot 只对 production 的 patch 自动分组且排除 opencv/PySide6；`[build-system] hatchling>=1.27` 不在 lock 中（构建期未固定）。这些未来版本的具体 CVE 无法离线判定 → 需复核 | CI 加 `pip-audit`/`osv-scanner`/Dependency Review；记录 OpenCV/Qt 主版本升级门槛或加上界；构建后端也纳入锁定/哈希校验 | 无 |
- **GW-RISK-12** | `scripts/fetch_models.py` 默认模式会联网下载 | `scripts/fetch_models.py:336-365,220-235`；`ci.yml:69`；`docs/privacy.md:27-28` | 隐私/供应链 | P2 | 无参数运行时，缺失/校验失败的模型会调用 `download()`（`urllib.request`，URL 与 SHA-256 固定）；该脚本在 `src/` 之外且应用不导入（grep 无 import），CI 只用 `--check`。不违反产品承诺，但开发者工具默认产生网络请求 | 默认改为 verify-only，联网需显式 `--fetch`；或在文档中显著标注默认下载行为 | 变更行为（仅开发者工具） |
- **GW-RISK-13** | 退出流程在主线程同步 join，最坏约 7–9s 无响应 | `src/eye_tracker/engine/controller.py:943-970,246-253,1586`；`src/eye_tracker/vision/worker.py:193-215`；`src/eye_tracker/panes/worker.py:44-47,150-157` | 性能/可靠性 | P2 | `shutdown` 依次停止热键、`worker.stop()`（默认 3s）、`_stop_pane_worker(wait=True)`（`PANE_JOIN_TIMEOUT_S=2.0`，`:1586`）、`lock_thread.join(2.0)`（`:965`），热键线程自身 join 还有 2s（`platform/hotkeys.py:720,740,766`），全部在 GUI 线程；摄像头 open/read 卡住时 vision worker 3s 内无法退出，只记 warning（`vision/worker.py:208-211`） | 异步 shutdown + 进度提示，或缩短 join 并让 daemon 线程自退出；记录超时指标 | 无 |
- **GW-RISK-14** | 更新下载没有整体截止时间/最低速率，慢速服务器可长期停留 DOWNLOADING | `src/eye_tracker/update/winhttp.py:44-45`；`src/eye_tracker/update/fetch.py:136-167`；`src/eye_tracker/update/service.py:285-316` | 可靠性 | P2 | 仅有 WinHTTP 分阶段超时（resolve 10s/connect 10s/send 15s/receive 30s，`winhttp.py:44-45`）；`fetch.download` 只在块间检查取消（`:155`），无总时长/最低速率/最大空读；`MAX_INSTALLER_BYTES` 只限总量（`release.py:31-32`）。已建立连接但低速/停滞的响应（不超过大小上限）可让状态机长时间停在 DOWNLOADING（用户可手动取消） | 总 deadline + min-rate 检测；失败自动重试一次；进度停滞提示 | 修复bug |
- **GW-RISK-15** | 摄像头 read 无 OpenCV 层超时，阻塞读会延迟隐私/暂停时释放设备 | `src/eye_tracker/vision/camera.py:43-48,72-76,583-630`；`src/eye_tracker/vision/worker.py:658-677,596-611` | 隐私/可靠性 | P2 | 代码自述某些驱动 read 会阻塞数秒（`camera.py:43-48`），仅 Linux 通过 `OPENCV_VIDEOIO_V4L_SELECT_TIMEOUT=2` 缩短（`:72-76`）；源码中无 `CAP_PROP_READ_TIMEOUT_MSEC`/`CAP_PROP_OPEN_TIMEOUT_MSEC` 引用；`_go_idle` 只能在当前 `read()` 返回后才会 `_release_source()`（`worker.py:658-677`），因此用户按隐私/暂停后摄像头 LED 可能延迟熄灭（与 `docs/privacy.md:13` 的即时承诺有差距） | 打开时设置 OpenCV read/open timeout 属性（支持的后端），或把 grab 放入可取消线程/非阻塞轮询；文档注明最坏延迟 | 无 |
- **GW-RISK-16** | GUI 线程上也执行可能阻塞的平台轮询（D-Bus/AX/进程枚举） | `src/eye_tracker/engine/controller.py:851-857,1447-1470`；`src/eye_tracker/platform/linux.py:99-105,1033-1060,1743-1762`；`src/eye_tracker/platform/macos.py:97-103,773-782`；`src/eye_tracker/platform/windows.py:1201-1220`；`src/eye_tracker/platform/base.py:220-231` | 性能 | P2 | tick 由 `QTimer` 在 GUI 线程驱动（`controller.py:851-857,1447-1470`）：`_poll_input` 在 Linux 上可走 `_QtDBus.call` 的阻塞调用 `bus.call(..., Block, 500ms)`（`linux.py:1057`），D-Bus 不可用时回退 `gdbus`/`loginctl`，`_run` 超时 1s（`:99-100,1743-1762`）；macOS 每 0.5s 采集前台窗口，AX 超时 0.25s（`macos.py:100,773-782`）；Windows 每 3s 做 registry + `psutil.process_iter`（`windows.py:1201-1220`、`base.py:220-231`）。已有缓存/退避，但最坏情况单 tick 可卡 0.5–1s，表现为光标/托盘抖动 | 将轮询移到后台线程并以最新快照驱动 tick；或给 GUI 线程调用设置更短硬超时 | 无 |
- **GW-RISK-17** | Windows 前台权限使用 `ASFW_ANY`，放宽到任意进程 | `src/eye_tracker/ipc.py:211-229`；`src/eye_tracker/platform/windows.py:526-527,1085` | 安全 | P2 | ctl 客户端在每次命令前调用 `AllowSetForegroundWindow(0xFFFFFFFF)`（`ipc.py:227`）；应用自身在 activate 路径也调用 `ASFW_ANY`（`windows.py:526-527,1085`）。这比“只授权目标进程”更宽，窗口期内任何本地进程都可抢前台（本地 UI 欺骗面扩大） | ctl 侧只对服务端 PID 授权（`AllowSetForegroundWindow(server_pid)`）；应用侧只对目标 hwnd 所属进程授权 | 修复bug |

> 未列入 C 的候选（证据不足或非产品运行期）：见 D。

## D. 需人工决策/需复核项

- **D1（需实验复核）FFmpeg 本地容器是否可能引用远程 URL。** `docs/privacy.md:88-90` 声称“本地文件（如播放列表）中的引用只会指向本地数据”，但 `vision/camera.py:456-459,607-611` 只检查**输入路径字符串**，不解析容器/播放列表内容；FFmpeg 的 concat/HLS 等 demuxer 可以引用 URL。建议在隔离网络命名空间里用构造的本地 `.txt/.m3u8` 做一次 `strace -f -e trace=connect` 验证；若成立，这是“never sends a byte”承诺的实质缺口（修复：OpenCV 打开前用 `AVFormatContext` 白名单不可行，可改用自有解码/只允许图像+特定封装）。
- **D2（产品决策）Windows 安装器签名/attestation。** 当前 release 说明“安装器尚未代码签名”（`docs/privacy.md:72-73`）；是否采购 Authenticode、或内置 Sigstore 公钥在应用内验证 attestation（需要离线可验证的 bundle 或透明日志），会影响发布流程与密钥管理。
- **D3（产品决策）macOS ad-hoc 签名 → 每次更新后 Accessibility 授权失效。** `platform/macos.py:13-21` 与 `docs/platform-support.md` 已把这个失败模式显式处理为 “stale” 并给用户操作指引，但用户每次更新都要重新授权；是否改用 Developer ID + notarization（`docs/building.md:104` 已有 secrets 支架）需产品/发布决策。
- **D4（门禁策略）`check_privacy.py` 的盲区与文案。** 见 GW-RISK-06：是否把 eval/exec、`QDesktopServices.openUrl`、运行时拼接的 `getattr/__import__` 纳入规则；是否把扫描范围从 `src/eye_tracker` 扩到所有会进入 bundle 的 Python（`packaging/pyinstaller`、`scripts/` 中被 spec 引用的部分）；以及是否把 README/docs 的 “any networking import or call” 改成“静态可检测的 import/call + bundle 扫描”。
- **D5（依赖）CVE 复核与版本策略。** 离线环境无法确认 `opencv-python-headless 5.0.0.93`、`numpy 2.5.3`、`PySide6-Essentials 6.11.2`、`pyobjc 12.2.2`、`psutil 7.2.2`、`python-xlib 0.33` 的已知漏洞；建议在有网环境跑 `pip-audit`/osv-scanner 并决定是否给主版本加上界。另外 OpenCV 5 是主版本跃迁（pyproject 允许 `>=4.10`），需确认 DNN/videoio 行为与模型兼容性（代码已有 OpenCV 5 兼容注释，见 `vision/backends/facemesh_backend.py:616` 与 `logging_setup.py:184-188`）。
- **D6（隐私文档）`privacy.yield_camera` 默认 true 时读取本机其他进程的摄像头占用。** `config.py:282-286` 默认 true；Linux 通过 `/proc/<pid>/fd`（`platform/linux.py:2416-2455`）扫描，Windows 通过摄像头 consent 注册表（`platform/windows.py:1201-1220`）。均为本地只读，未外传；但 `docs/privacy.md` 的“What the app does *not* do”未明确列出该本机进程检查，建议补一句以维持文档-实现一致（`docs/configuration.md:110`、`docs/platform-support.md:75` 已描述功能）。
- **D7（开发工具安全）`scripts/build_site.py --out DIR` 会 `shutil.rmtree(DIR)`。** `scripts/build_site.py:103-109` 无路径白名单/空目录校验，`--out /` 之类的误传会删除任意目录（仅开发者/CI 手动运行）。建议拒绝非空/非白名单目录或要求 `--force`。同类：`packaging/linux/build_appimage.sh` 的 `--allow-unpinned` 是显式逃生口（`:99-101`），默认仍强制 SHA-256，风险可接受。
- **D8（测试覆盖）建议补的回归测试。** ① 伪造 WinHTTP 设置失败时 Fetcher 必须 fail closed（GW-RISK-03）；② 安装器在 rename 后被替换时不得执行（GW-RISK-02，需要可注入的 launcher/clock）；③ Linux 私有目录不可用时不得回退共享 `/tmp`（GW-RISK-05）；④ 日志/trace 文件权限断言 0600（GW-RISK-07）。

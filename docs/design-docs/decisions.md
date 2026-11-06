# Architectural decisions

Durable decisions, ADR-style. Each records the context, the decision, and what would make it worth
revisiting. "Accepted" means the code follows it today. These are not all comfortable decisions —
the ones with known costs say so.

## ADR-001: One version source, guarded at release time

**Status:** accepted.

**Context.** Desktop packaging needs the version in several places: hatchling metadata, the
Windows file version, the macOS bundle version, the release tag and the update check. Scattering
declarations guarantees drift.

**Decision.** `src/eye_tracker/__init__.py:__version__` is the only declaration. hatchling reads it,
`packaging/pyinstaller/eye-tracker.spec` derives the platform versions from it, and the release
workflow refuses a tag whose name is not `v$version`. The update check compares against the same
value.

**Consequences.** A release is impossible with a mismatched tag. Any tool that needs the version
must parse that file (four implementations today — tracked as TD-CI-004).

## ADR-002: The update check is the only network code

**Status:** accepted; residual risk tracked as TD-SEC-001.

**Context.** The product promise is "offline by default". Telemetry and crash reporting were
excluded from the start.

**Decision.** All networking lives in `update/`: a fixed host allow-list in `fetch.py`, a Windows
WinHTTP transport in `winhttp.py`, byte caps everywhere, no redirects, and an installer verified
against `SHA256SUMS.txt` from the same release. The check is opt-in and off by default. The privacy
gate enforces the boundary over source and bundle.

**Consequences.** Windows-only update installation (the product's only platform with an installer
that can self-replace); the installer is not code-signed, so the checksum only detects corruption,
not a replaced release. Documented in `docs/privacy.md`; fixing it needs signing infrastructure.

## ADR-003: MediaPipe models via OpenCV DNN, not the MediaPipe runtime

**Status:** accepted.

**Context.** The face-landmark model is available through the MediaPipe runtime, which is easy but
ships a native library containing a usage-logging uploader.

**Decision.** Run the same `.tflite` model through OpenCV's DNN module and fit the canonical face
model with `solvePnP`. The runtime is not a dependency.

**Consequences.** More code to own (the 478-landmark pipeline, face-region tracking, YuNet
fallback), but no telemetry surface and fewer native dependencies. Do not reintroduce the runtime.

## ADR-004: Derive `TrackingState` from flags, do not transition it

**Status:** accepted.

**Context.** Tracking can be off for several overlapping reasons: user pause, privacy mode, locked
session, camera yielded to a call, user away, camera error, calibrating, invalid calibration.

**Decision.** Keep the causes as independent flags and derive the public state with a documented
priority order. Opening and closing the camera follows from the derived state.

**Consequences.** Overlapping causes resolve naturally when one ends; no transition table to
maintain. New causes must be added to the priority order and the documentation together.

## ADR-005: One calibration profile per desk setup, numbers only

**Status:** accepted.

**Context.** Monitor layout, camera and backend decide the mapping from features to screen. A
laptop moving between docks changes it; a user should not recalibrate on every dock.

**Decision.** Calibrations are stored as small JSON files with one profile per setup signature;
the controller picks the profile that fits the current layout and switches automatically. Files
contain numbers only — never images or frames.

**Consequences.** Old profiles are kept and matched, not overwritten. Compatibility with older
files must be maintained (see TD-CPLX-011 for the parsing hotspot).

## ADR-006: Move the cursor and focus the window; never synthesise clicks

**Status:** accepted.

**Context.** A click would be the simplest way to focus a window, but it triggers actions in the
target application (and can be destructive).

**Decision.** A switch warps the cursor to the remembered position and asks the platform layer to
focus the last window used on that monitor. The pane feature asks the terminal to focus a pane
instead of activating a window. No synthetic input events are sent.

**Consequences.** Some windows cannot be focused without a click on some platforms; the app
degrades to moving the cursor there. This is documented in `docs/platform-support.md`.

## ADR-007: Pure decision modules with injected time

**Status:** accepted.

**Context.** Switching, presence and guard rules are time-based (dwell, grace periods, hysteresis)
and were the hardest things to test reliably.

**Decision.** Decision modules take `now` as an argument and import only the standard library,
numpy and value types. Qt and OpenCV are confined to `controller.py`, `vision/` and `ui/`.

**Consequences.** Deterministic unit tests without a camera; new time-based logic must not call
`time.monotonic()` directly. Enforced by prose today, by a structural test in phase 3.

## ADR-008: Lazy imports as a circuit breaker, not a pattern

**Status:** accepted with debt (TD-ARCH-002, TD-ARCH-006).

**Context.** A few import cycles appeared while the app grew (app ↔ cli, update fetch ↔ transport,
platform hotkeys → CLI wording). Some are broken with function-level imports. The CLI-text cycle
was removed in phase 4 by moving the naming helpers into `command_text.py`; the update transport
pair remains.

**Decision.** A lazy import is acceptable only as a temporary circuit breaker, with a comment
explaining why and a tracker entry. It does not make the cycle disappear; it hides it from the
runtime.

**Consequences.** Module import graphs still contain the cycles; phase 3 will enforce direction and
phase 4 will remove the exceptions listed in `ARCHITECTURE.md`.

## ADR-009: Atomic, private writes for user data

**Status:** accepted; log permissions are a known gap (TD-SEC-007).

**Context.** `settings.json` and `calibration.json` can be corrupted by a crash mid-write, and can
contain personal data (hotkeys, app names, calibration numbers).

**Decision.** Write through `mkstemp` + `os.replace`, with mode 0600. Logs and traces are separate,
append-only files.

**Consequences.** A crash leaves the old valid file, never a half file. Logs and traces are
currently 0644, readable by other local accounts — tracked for hardening.

## ADR-010: One build definition for release, bundle and pull requests

**Status:** accepted (release gating gap: TD-CI-001).

**Context.** The build has many platform-specific steps; duplicating them between release and
pull-request pipelines lets them drift.

**Decision.** `build.yml` is the only build definition, called by `release.yml` (upload and
publish) and by `bundle.yml` (pull requests, no upload). Version guard and release notes extraction
live in its `prepare` job.

**Consequences.** One place to change, and pull requests exercise the same packaging steps as a
release. The release job itself still does not run the test suite; tracked as the only P1 CI issue.

## ADR-011: Three-OS CI, with the platform layer faked locally

**Status:** accepted.

**Context.** Platform code is the highest-risk part and cannot be exercised on a developer's
machine for the other two OSes.

**Decision.** Test on Linux, Windows and macOS in CI; on any single OS, fake the platform layer
(`tests/test_platform_*.py` use `FakeWin32`-style doubles) so most logic still runs. Windows-only
and macOS-only tests skip elsewhere by design.

**Consequences.** 55 skips on macOS are expected, not a bug. Real native behaviour is only proven
on its own OS; change the fake and the CI in the same commit.

## ADR-012: The privacy gate is code, and is itself tested

**Status:** accepted.

**Context.** A privacy promise maintained by review will erode.

**Decision.** `scripts/check_privacy.py` statically scans source imports/calls and frozen bundles
(including native libraries and zip-embedded modules), with a single reviewed allow-list for the
update transport. Its own test suite injects violating code and asserts that each rule fails.

**Consequences.** The gate has blind spots (runtime-composed names, URL-opening calls); they are
tracked as TD-SEC-006. Documentation must not overstate the guarantee — "static analysis + bundle
scan", not "provably no network".

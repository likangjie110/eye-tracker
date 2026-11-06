# Architecture

Eye Tracker runs as one process: a Qt tray application with a background vision worker. A camera
frame becomes an `Observation`, the gaze model maps it to a point on the virtual desktop, the
decision engine decides whether to switch, and the platform layer moves the cursor and keyboard
focus. This file is the **map**: package responsibilities, the dependency direction we enforce,
threads, and what lives on disk. The frame-by-frame pipeline and the switching rules are explained
in depth in [`docs/architecture.md`](docs/architecture.md).

## Packages and their layer

```text
Layer 0 — foundation (no project imports, or only other Layer 0 modules)
  command_text.py   the command lines shown to users (CLI, tray app, --config-dir)
  types.py          shared value types (Rect, Monitor, Observation, TrackingState)
  config.py         typed settings, validation, atomic persistence
  paths.py          config / data / log locations
  logging_setup.py  rotating local logs
  trace.py          opt-in numeric trace

Layer 1 — domain logic and adapters
  gaze/             gaze model, filters, calibration, calibration store, learning
  engine/           pure decision logic: decision, guard, presence, reading, scheduler,
                    input_state, window_memory, camera_yield (plus the controller seams)
  panes/            split-pane focus: decider (pure), worker, providers
  vision/           OpenCV adapter: camera, motion gate, face backends
  platform/         OS adapter: autostart, hotkeys, monitors, cursor, windows
  update/           network adapter: the opt-in update check and installer

Layer 2 — orchestration
  engine/controller.py         owns the pipeline state and the Qt timers
  engine/controller_support.py the pointer/worker seams and vision factories the controller injects
  app.py                startup sequence, single instance, owns windows and the controller
  ipc.py                single-instance guard and local command channel
  diagnostics.py        doctor / bench aggregation

Layer 3 — presentation
  ui/               Qt widgets; they call the Controller and own no business logic

Entry points
  cli.py, __main__.py
```

Package boundaries are historical as well as logical. `engine/controller.py` and its
`controller_support.py` seams sit in `engine/` because they coordinate the engine, not because they
are pure logic — `CONTRIBUTING.md` and the `engine/__init__.py` docstring say so explicitly.

## Allowed dependency direction

The target rule, from bottom to top: **a layer may import the layers below it, never the ones
above it.**

- **R1** Layer 0 imports only the standard library, numpy (`types.py`), and other Layer 0 modules.
- **R2** Layer 1 imports Layer 0 and its own package. `gaze/`, the pure `engine/` modules and
  `panes/decider.py` import no Qt and no OpenCV; `vision/` is the OpenCV adapter; `update/` is the
  network adapter.
- **R3** Layer 2 may import Layers 0 and 1. `app.py` also owns the windows, so it imports `ui/`;
  it is the composition root and the one place where "everything" is allowed.
- **R4** Layer 3 (`ui/`) imports Layer 0 and the public Controller API; it must not import
  `cli.py`, `app.py`, `ipc.py`, `diagnostics.py`, or application state directly.
- **R5** A new exception is allowed only with a `TD-ARCH-*` entry in the tech-debt tracker and a
  note here. Exceptions shrink; they do not grow.

These rules are prose today. Phase 3 of the harness migration adds a structural test that fails a
pull request which violates them.

### Known exceptions (tracked, to be removed)

| Exception | Why it exists today | Tracker |
|---|---|---|
| `engine/controller.py` and `controller_support.py` import PySide6, `panes/`, `platform/`, `vision/` | they are the orchestrator seams, and `engine/` is where they grew | TD-CPLX-001, TD-ARCH-001 |
| `panes/decider.py` → `engine/reading.py` | reuse of a pure rule | TD-ARCH-001 |
| `diagnostics.py` → `platform/hotkeys.py`, `engine/scheduler.py` | a diagnostic report deliberately aggregates every layer | TD-ARCH-002 |
| `update/fetch.py` ↔ `update/winhttp.py` (lazy import) | transport protocol lives in `fetch.py` | TD-ARCH-006 |

Phase 4 removed the former `app ↔ cli`, `platform → cli` and `ui → cli` exceptions: the
command-line naming helpers now live in the dependency-free `command_text.py`, which every layer
may import.

## Threads

| Thread | Owns | Rule |
|---|---|---|
| Qt main thread | `app.py`, `controller.py`, all `ui/`, QTimers, input polling | never do blocking I/O or long joins here |
| Vision worker | camera capture, motion gate, face backend | results arrive as Qt signals; `stop(timeout=3.0)` |
| Pane worker | pane providers (tmux/WezTerm/terminal/UIA calls) | never on the GUI thread; join timeout 2 s |
| Update thread | update check and download | results as signals; the installer starts on the main thread |
| Lock thread | OS screen lock / display off | short-lived because locking can block |

Cross-thread calls are marshalled through queued signals. The authoritative description of the
controller's threading and state derivation is its module docstring
(`src/eye_tracker/engine/controller.py:1-80`). Camera frames exist only in memory.

## Data on disk

All paths come from `paths.py`; the privacy page documents what is stored and why
(`docs/privacy.md`).

| File | Content | Permission |
|---|---|---|
| `settings.json` | user settings | 0600, atomic replace |
| `calibration.json` | calibration profiles (numbers only, never images) | 0600, atomic replace |
| `logs/` | rotating local logs, no frames or input content | 0644 (tracked in TD-SEC-007) |
| `updates.json` | last check time, last notified version | default |
| `--trace FILE` | opt-in numeric tracking trace | 0644 (tracked in TD-SEC-007) |

## The privacy boundary

`update/winhttp.py` is the only module that performs network I/O; `update/fetch.py` decides what
may be fetched (fixed host allow-list, byte limits, no redirects) and `update/installer.py` verifies
the downloaded installer against `SHA256SUMS.txt`. The update check is opt-in and off by default.
`scripts/check_privacy.py` enforces the boundary statically over the source tree and the frozen
bundle; the residual risks (no code signature on the installer, no notarisation) are documented in
`docs/privacy.md` and tracked as TD-SEC-001 / TD-CI-007.

## Where the detail lives

- `docs/architecture.md` — the frame → cursor pipeline, switching rules, presence states,
  calibration, coordinates, and the original code map.
- `docs/privacy.md` — the data the app does and does not touch.
- `docs/design-docs/` — first principles (`core-beliefs.md`) and the durable decisions
  (`decisions.md`).
- `docs/CI.md` — workflows, the release chain and the local gate list.
- `docs/QUALITY_SCORE.md` — per-domain quality and the gap to target.

## Keeping this file current

When a package is added or removed, or when an import direction changes, update this file in the
same commit. If a new exception to R1–R5 is introduced, add it to the table above with a
`TD-ARCH-*` id — phase 3 will turn that table into an enforced test.

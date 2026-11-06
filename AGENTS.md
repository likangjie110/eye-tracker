# AGENTS.md

Eye Tracker is a cross-platform Python desktop app: the webcam watches where you look and moves
the cursor and keyboard focus to the monitor you are looking at. It is private and offline by
default, and degrades rather than crashes.

This file is a **map, not a manual**. Read it first, then follow the one link you need.

## Where to go next

- `ARCHITECTURE.md` — packages, allowed dependency direction, threads, data on disk.
- `docs/index.md` — every document, indexed with its status.
- `docs/design-docs/core-beliefs.md` — the first principles that settle arguments.
- `docs/exec-plans/active/harness-migration.md` — current migration plan, batches, decisions.
- `docs/exec-plans/tech-debt-tracker.md` — known problems, with evidence and IDs.
- `docs/CI.md` — framework decision, the five workflows, the release chain.
- `docs/QUALITY_SCORE.md` — per-domain score and the gap to target.
- `docs/architecture.md` — the deep pipeline explanation (frame → cursor).
- `CONTRIBUTING.md` — the human contribution guide; still valid for agents.

## Commands (from the repository root)

```bash
uv sync --locked                                   # install exactly the lock file
uv run eye-tracker                                 # start the tray app
uv run eye-tracker doctor                          # environment/permission report
uv run pytest -q                                   # 3054 passed / 55 skipped (macOS, 2026-11-06)
uv run pytest -q --cov=eye_tracker                 # coverage gate, floor 89 % (ratchet)
uv run ruff check .                                # lint
uv run ruff format --check .                       # formatting
uv run mypy                                        # types, as on Linux (the default)
uv run mypy --platform win32                       # types behind sys.platform == "win32"
uv run mypy --platform darwin                      # types behind sys.platform == "darwin"
uv run mypy scripts packaging/pyinstaller/entry.py # scripts and the PyInstaller entry
uv run python scripts/check_privacy.py             # no network code, no frames written
uv run python scripts/fetch_models.py --check      # committed face models and licences
uv run python scripts/gen_config_docs.py --check   # docs/configuration.md is up to date
uv run python scripts/lint_size.py --check         # file/function/class size gate (frozen debt)
uv run python scripts/check.py                     # all of the above + pytest, one command
```

Run this list (or `scripts/check.py`) before opening a pull request; CI runs the same checks on
three operating systems. `scripts/check.py --quick` skips pytest. Building installers is described
in `docs/building.md`.

## Repository map

```text
src/eye_tracker/
  types.py         shared value types (Rect, Monitor, Observation, TrackingState)
  config.py        typed settings, validation, atomic JSON persistence
  paths.py         config / data / log locations
  logging_setup.py rotating local logs (no telemetry, no crash upload)
  trace.py         opt-in numeric trace for tuning and bug reports
  ipc.py           single instance and the local command channel (eye-tracker ctl)
  app.py           startup sequence, single instance, window ownership
  cli.py           command line: run / calibrate / doctor / bench / ctl / autostart / reset
  diagnostics.py   the doctor and bench subcommands
  engine/          decision logic; controller.py is the Qt orchestrator
  gaze/            regression model, smoothing, calibration, implicit learning
  vision/          camera, motion gate, face backends, capture worker thread
  panes/           split-pane focus (experimental, off by default)
  platform/        OS integration: autostart, hotkeys, monitors, cursor, windows
  ui/              Qt widgets; holds no business logic
  update/          the only networking code: opt-in update check and installer
scripts/           privacy gate, model fetch, icons, config docs, site build, analysis
packaging/         PyInstaller spec, Windows installer, macOS dmg, Linux AppImage
tests/             pytest suite (offscreen Qt, fake platform layer, synthetic frames)
docs/              user and engineering documentation (index: docs/index.md)
site/              project website published through GitHub Pages
assets/            README hero image and demo animation
```

## Hard constraints

1. **Privacy is a hard constraint, not a feature.** No network code under `src/` except the reviewed
   exception in `update/` (`scripts/check_privacy.py` enforces it); camera frames live in memory
   only and are never written to disk or sent anywhere.
2. **Pure logic stays pure.** `gaze/*`, `engine/*` except `controller.py`, and `panes/decider.py`
   import neither Qt nor OpenCV, and take the current time as an argument. That is what makes the
   switching rules testable without a camera.
3. **One version source.** `src/eye_tracker/__init__.py:__version__` is the only declaration.
   A release tag `vX.Y.Z` must match it, or `build.yml` fails before anything is published.
4. **Refactors preserve behaviour; behaviour changes are separate.** Never mix the two in one
   commit. Add tests (or a characterization test) before changing a module that decides anything.
5. **Degrade, never crash.** Platform features are best effort: unsupported calls return
   `False`/`None` and the app keeps running.
6. **No dangerous process APIs.** No `shell=True`, `os.system`, `eval`/`exec`; subprocesses use
   argv lists. Networking exists only in the reviewed update transport.
7. **Dependency direction follows `ARCHITECTURE.md`.** Known exceptions are listed there and may
   only shrink, never grow.
8. **Everything goes into the repository.** Decisions belong in `docs/`, debt in the tracker,
   checks in tests or scripts — never only in a chat, an issue, or someone's memory.

## How to make a change

1. Read `docs/exec-plans/active/harness-migration.md` and the relevant `TD-*` entry first.
2. For a refactor, add or extend the tests that pin the current behaviour before touching code.
3. Keep commits single-purpose (`feat:` / `fix:` / `refactor:` / `docs:` / `test:` / `chore:`),
   with the reason, the blast radius and the checks that were run in the body.
4. Update the docs in the same change when behaviour, a setting, a command or a dependency changes.
5. Register new debt in `docs/exec-plans/tech-debt-tracker.md`; do not fix it out of scope.
6. Run the full command list above before claiming the change is done.

## Facts that bite

- Tests run Qt offscreen (`QT_QPA_PLATFORM=offscreen`); platform-specific tests skip off their OS.
  The CI matrix (Linux/Windows/macOS) covers all three, so a local skip is not a failure.
- 55 tests skip on macOS by design: Windows API, real face photos (`EYE_TRACKER_TEST_FACE`),
  Linux-only paths and Inno Setup. Do not make the suite green by deleting them.
- The MediaPipe *runtime* is intentionally not used (it contains a usage-logging uploader); the
  face models are run through OpenCV's DNN module. Do not reintroduce it.
- Pre-commit hooks exist (`.pre-commit-config.yaml`, decision D2) but are optional until
  installed: `uvx pre-commit install`. They run the fast subset (ruff, privacy, size, docs);
  mypy and pytest stay in `scripts/check.py`/CI.
- The harness migration is in progress: prefer following the active plan over inventing a new
  process, and record deviations in its decision log.

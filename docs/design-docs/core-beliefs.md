# Core beliefs

These are the principles this project is built on. They are stable on purpose: when two solutions
look equally good, or a reviewer and an author disagree, this is what decides. Each belief points
at the code or tests that make it real — a belief without evidence is a slogan.

## 1. Privacy is a hard constraint, not a feature

The camera is the most sensitive input a desktop app can touch, so the product promise is enforced
by code, not by intention:

- no network code under `src/` except the reviewed update transport
  (`src/eye_tracker/update/winhttp.py`);
- camera frames exist only in memory;
- the update check is **opt-in and off by default**, and it sends no identifier;
- the boundary is checked twice — over the source tree and over the frozen bundle —
  by `scripts/check_privacy.py`, with `tests/test_privacy.py` proving the checker catches
  violations (291 cases).

Evidence: `docs/privacy.md`, `scripts/check_privacy.py`, `tests/test_privacy.py`.

## 2. Behaviour is the product; refactoring does not change it

A refactor that changes behaviour is a feature change in disguise. The two never share a commit.
Before a module that decides anything is restructured, its current behaviour is pinned with tests
(characterization), and the tests stay unchanged through the refactor.

Evidence: `docs/turn-project-to-harness-rules.md` red lines R1/R2; the batch rules in
`docs/exec-plans/active/harness-migration.md` §4.

## 3. Pure logic is testable logic

`gaze/*`, the pure `engine/*` modules and `panes/decider.py` import neither Qt nor OpenCV and take
the current time as an argument. That is why the switching, presence and calibration rules can be
tested deterministically, without a camera, on three operating systems.

Evidence: `src/eye_tracker/engine/__init__.py`, `CONTRIBUTING.md` ("Pure logic stays pure"),
`tests/test_decision.py`, `tests/test_guard.py`, `tests/test_scheduler.py`.

## 4. Derive state, don't transition it

`TrackingState` is computed from a small set of independent flags (privacy, locked, calibrating,
paused, yielded, away, camera error, needs calibration) in a documented priority order. Overlapping
causes resolve naturally when one ends; there is no state machine with dozens of edges to get lost
in.

Evidence: `src/eye_tracker/engine/controller.py:1-80`, `src/eye_tracker/types.py`.

## 5. Degrade, never crash

Every platform feature is best effort. `PlatformServices` returns `False`/`None` when the OS cannot
provide something, and the app keeps running with fewer features. An unsupported call is not an
exception path.

Evidence: `src/eye_tracker/platform/base.py`, `src/eye_tracker/platform/__init__.py`.

## 6. One source of truth

The version lives in `src/eye_tracker/__init__.py` and nowhere else; hatchling reads it, the release
guard checks the tag against it, and the Windows resource version is derived from it. Generated
documentation is generated, not maintained by hand: `docs/configuration.md` comes from
`src/eye_tracker/config.py` and a test fails when it is stale.

Evidence: `pyproject.toml`, `.github/workflows/build.yml` (version guard),
`scripts/gen_config_docs.py`, `tests/test_docs.py`.

## 7. Measure claims before publishing them

Performance and accuracy claims in the README are measured, and any change that affects CPU or
latency should come with `eye-tracker bench` numbers before and after. Privacy claims are backed by
a gate, not a promise.

Evidence: `CONTRIBUTING.md` ("Measure performance claims"), `src/eye_tracker/diagnostics.py`,
`README.md` performance section.

## 8. Small, reversible changes

One commit does one thing. A batch can be reverted without taking the previous one with it. When a
change is risky, the risk is paid down in small steps with tests first, not in one large rewrite.

Evidence: git history style; batch rollback column in the migration plan; red line R8
("every step reversible").

## 9. Boring, inspectable dependencies

Prefer libraries whose behaviour can be reasoned about and tested: numpy, OpenCV, Qt, the standard
library. The MediaPipe *runtime* is deliberately not used because it contains a usage-logging
uploader — the same models are run through OpenCV's DNN module instead.

Evidence: `docs/privacy.md` ("Why not the MediaPipe runtime"),
`src/eye_tracker/vision/backends/facemesh_backend.py`,
`src/eye_tracker/vision/models/NOTICE.md`.

## 10. Everything goes into the repository

A decision that only exists in a chat, an issue or someone's memory does not exist for the next
agent. Plans, debt, decisions, checks and the reasons behind them live in the repository as plain
versioned text.

Evidence: this directory, `docs/exec-plans/`, `docs/CI.md`, `AGENTS.md`.

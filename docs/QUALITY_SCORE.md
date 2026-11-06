# Quality score

A per-domain score of how safe it is to change this code today, and what the gap to "agent-first"
looks like. This is a **working document**: it is updated at the end of each migration batch and
whenever a domain's evidence changes. Scores are judgements from the evidence linked below, not a
coverage report.

Scale: **1** = fragile, change at your own risk; **2** = fragile with tests/fakes; **3** = workable;
**4** = solid; **5** = safely changeable by an agent with mechanical feedback. Dimensions:
**Tests** (protection before touching), **Docs** (can a newcomer understand it), **Complexity**
(size/coupling of the change surface), **Deps** (dependency direction clean), **Gate** (mechanical
checks that fail on drift).

Snapshot: 2026-11-06, `harnessing` branch, code `v0.3.0` + phases 1–3 and B1 steps 1–2.

| Domain (packages) | Tests | Docs | Complexity | Deps | Gate | Overall | Biggest gap |
|---|---:|---:|---:|---:|---:|---:|---|
| vision (`vision/`) | 4 | 4 | 2 | 4 | 3 | **3.4** | `facemesh_backend.py` 1,454 lines; `process` 25 decision points (TD-CPLX-006); face-photo tests never run in CI (TD-TEST-012) |
| gaze (`gaze/`) | 5 | 4 | 3 | 5 | 3 | **4.0** | `GazeModel.from_dict` 27 decision points (TD-CPLX-011); no coverage numbers (TD-TEST-001) |
| engine, pure rules (`engine/` minus controller) | 5 | 4 | 3 | 5 | 3 | **4.0** | three `update()` state machines 87–108 lines (TD-CPLX-009) |
| orchestration (`engine/controller.py`, `app.py`, `ipc.py`) | 3 | 5 | 1 | 3 | 3 | **3.0** | controller 3,429 lines / 155 methods (TD-CPLX-001); B1 removed the app↔cli cycle and the UI→controller import |
| platform (`platform/`) | 4 | 4 | 3 | 3 | 3 | **3.4** | hotkey backends split into 151-line facade + four modules (B2); `linux.py` / `windows.py` / `macos.py` still large; real native paths only on their OS |
| panes (`panes/`) | 4 | 4 | 4 | 3 | 3 | **3.6** | `panes.decider → engine.reading` (TD-ARCH-001); duplicated guard (TD-DUP-001) |
| UI (`ui/`) | 2 | 3 | 1 | 3 | 3 | **2.4** | 25.2 tests/1k lines (others 74–133); `settings_dialog.py` 2,029 / `calibration_window.py` 1,766 lines; B1 removed UI→cli and UI→controller |
| update and release (`update/`, `packaging/`, `.github/`) | 4 | 4 | 4 | 4 | 4 | **4.0** | `winhttp.py` zero behaviour tests (TD-TEST-002); unsigned installer execution (TD-SEC-001); release not test-gated (TD-CI-001) |
| repository and docs (root, `docs/`, `scripts/`, `tests/`) | 4 | 4 | 4 | 4 | 4 | **4.0** | no coverage gate, no structural tests, no pre-commit (TD-TEST-001/014/015, TD-CI-018) |

## How to read this

- **Tests 1–2** means there is no way to know a change is safe; do not refactor before adding
  characterization tests.
- **Complexity 1–2** means the file is large enough that a reviewer cannot hold it in mind; split
  before changing behaviour inside it.
- **Gate** is about *mechanical* feedback (CI, linters, structural tests), not about how careful the
  people are. Every domain is currently 3–4: good CI, but no coverage/architecture enforcement.

## Targets for phase 5

- Every domain **Overall ≥ 4.0**, with `ui/` and `orchestration/` as the two hard cases.
- No domain below **3** on any dimension.
- Gate ≥ 4 for every domain: the structural test, coverage gate and size lint must exist.
- The gaps above are the tracker entries to close; the migration plan maps them to batches B1–B7.

## Update procedure

1. Re-run the mechanical evidence: `uv run python scripts/analysis/scan_metrics.py`,
   `uv run pytest -q`, and (from phase 5) the coverage report.
2. Update the row and the snapshot date.
3. If a domain moves down, add or update a `TD-*` entry explaining why.
4. Append the change to the migration plan's progress log in the same commit.

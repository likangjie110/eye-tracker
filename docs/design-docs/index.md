# Design documents

The durable design knowledge of Eye Tracker, split into two kinds:

- [`core-beliefs.md`](core-beliefs.md) — the principles that settle arguments. Short, stable, and
  meant to be cited when a trade-off is contested.
- [`decisions.md`](decisions.md) — the architectural decisions already made, why, and what would
  make them worth revisiting. ADR-style: each has a status, context, decision and consequences.

New design documents go in this directory and must be added to this index. A design document is
warranted when a choice is expensive to reverse, spans packages, or keeps being re-litigated.
A comment in the code is enough otherwise.

| Document | Status | Last reviewed |
|---|---|---|
| [`core-beliefs.md`](core-beliefs.md) | current | 2026-11-06 |
| [`decisions.md`](decisions.md) | current | 2026-11-06 |

Related material that is not a design document: `../architecture.md` (the pipeline),
`../privacy.md` (the privacy boundary), `../CI.md` (CI and release chain), `../QUALITY_SCORE.md`
(quality per domain).

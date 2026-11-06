# Documentation index

Every document in this repository, with its status and audience. Status means:

- **current** — maintained, reviewed against the code in the harness migration.
- **generated** — produced by a script; never edit by hand.
- **construction** — part of the current harness migration; archived when phase 5 is done.
- **archived** — historical record; not maintained, kept for provenance.

## Entry points

| Document | Audience | Status |
|---|---|---|
| [`../AGENTS.md`](../AGENTS.md) | agents: map, commands, hard constraints | current |
| [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | developers/agents: packages, dependency rules, threads | current |
| [`../README.md`](../README.md) | users: what it is, install, features | current |
| [`../README.tr.md`](../README.tr.md) | users (Turkish) | current |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | contributors: setup, checks, PR rules | current |
| [`../SECURITY.md`](../SECURITY.md) | security reporters: supported versions, privacy gate | current |
| [`../CHANGELOG.md`](../CHANGELOG.md) | users/agents: release history, source of release notes | current |

## User and engineering guides (`docs/`)

| Document | What it covers | Status |
|---|---|---|
| [`architecture.md`](architecture.md) | deep pipeline: frame → cursor, switching rules, threads, calibration | current |
| [`building.md`](building.md) | building from source, installers, release steps | current |
| [`calibration.md`](calibration.md) | how to calibrate and what the results mean | current |
| [`configuration.md`](configuration.md) | every setting with default and allowed range | generated |
| [`panes.md`](panes.md) | split-pane focus (experimental) and its providers | current |
| [`platform-support.md`](platform-support.md) | platform matrix, permissions, known limits | current |
| [`privacy.md`](privacy.md) | what is stored/sent, the update check, the privacy gate | current |
| [`troubleshooting.md`](troubleshooting.md) | common failures and fixes | current |
| [`CI.md`](CI.md) | framework decision, workflows, release chain, local gates | current |
| [`QUALITY_SCORE.md`](QUALITY_SCORE.md) | per-domain quality scores and gaps | current |

## Design documents (`docs/design-docs/`)

| Document | What it covers | Status |
|---|---|---|
| [`design-docs/index.md`](design-docs/index.md) | index of first principles and durable decisions | current |
| [`design-docs/core-beliefs.md`](design-docs/core-beliefs.md) | the non-negotiable principles, with evidence | current |
| [`design-docs/decisions.md`](design-docs/decisions.md) | durable architectural decisions (ADR-style) | current |

## Execution plans (`docs/exec-plans/`)

| Document | What it covers | Status |
|---|---|---|
| [`exec-plans/active/harness-migration.md`](exec-plans/active/harness-migration.md) | the harness migration plan: batches, phases, decisions, progress log | construction |
| [`exec-plans/tech-debt-tracker.md`](exec-plans/tech-debt-tracker.md) | known debt, each with evidence, severity and acceptance | construction |
| [`exec-plans/active/audits/README.md`](exec-plans/active/audits/README.md) | the phase-1 audits: CI, tests/docs, security, scan output | construction |
| [`exec-plans/completed/README.md`](exec-plans/completed/README.md) | archived plans (empty until phase 5) | archived |
| [`turn-project-to-harness-rules.md`](turn-project-to-harness-rules.md) | the construction blueprint that drives the migration | construction |

## How to keep this file honest

`tests/test_docs.py` checks that every top-level Markdown page under `docs/` is listed here,
that relative links resolve, and that the required entry files exist. Adding a page without
updating this index fails the test suite.

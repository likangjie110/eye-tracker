#!/usr/bin/env python3
"""Run every local gate, in the same order CI runs them.

The point is that a contributor (human or agent) does not have to remember ten commands, and that
"it passed locally" means the same thing as "it passed CI". CI calls the individual steps so it can
report them separately; this script is the local contract (``CONTRIBUTING.md``, ``AGENTS.md``).

Usage::

    uv run python scripts/check.py             # all gates, including pytest
    uv run python scripts/check.py --quick     # everything except pytest
    uv run python scripts/check.py --list      # print the gate list
    uv run python scripts/check.py --fail-fast

Exit code is 0 only when every gate passed. The summary at the end names each failure.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _tool(name: str) -> str:
    """Prefer the tool installed next to the interpreter, fall back to PATH."""
    local = Path(sys.executable).parent / name
    return str(local) if local.exists() else name


@dataclass(frozen=True)
class Check:
    """One named command that must succeed."""

    name: str
    command: tuple[str, ...]
    slow: bool = False


CHECKS: tuple[Check, ...] = (
    Check("ruff lint", (_tool("ruff"), "check", ".")),
    Check("ruff format", (_tool("ruff"), "format", "--check", ".")),
    Check("mypy (package)", (_tool("mypy"),)),
    Check("mypy (win32)", (_tool("mypy"), "--platform", "win32")),
    Check("mypy (darwin)", (_tool("mypy"), "--platform", "darwin")),
    Check(
        "mypy (scripts, packaging)",
        (_tool("mypy"), "scripts", "packaging/pyinstaller/entry.py"),
    ),
    Check("privacy gate", (PYTHON, "scripts/check_privacy.py")),
    Check("face models", (PYTHON, "scripts/fetch_models.py", "--check")),
    Check("config docs", (PYTHON, "scripts/gen_config_docs.py", "--check")),
    Check("workflows", (PYTHON, "scripts/check_workflows.py")),
    Check("size gate", (PYTHON, "scripts/lint_size.py", "--check")),
    Check(
        "pytest",
        (PYTHON, "-m", "pytest", "-q", "--cov=eye_tracker", "--cov-report=term-missing"),
        slow=True,
    ),
)


@dataclass
class Result:
    """What one check did."""

    check: Check
    code: int
    seconds: float

    @property
    def ok(self) -> bool:
        return self.code == 0


def selected(*, quick: bool) -> tuple[Check, ...]:
    return tuple(check for check in CHECKS if quick is False or check.slow is False)


def run(checks: tuple[Check, ...], *, fail_fast: bool) -> list[Result]:
    results: list[Result] = []
    for index, check in enumerate(checks, start=1):
        print(f"[{index}/{len(checks)}] {check.name}: {' '.join(check.command)}", flush=True)
        started = time.monotonic()
        code = subprocess.run(check.command, cwd=ROOT, check=False).returncode
        seconds = time.monotonic() - started
        results.append(Result(check, code, seconds))
        status = "ok" if code == 0 else f"FAILED (exit {code})"
        print(f"[{index}/{len(checks)}] {check.name}: {status} in {seconds:.1f}s\n", flush=True)
        if code != 0 and fail_fast:
            break
    return results


def summary(results: list[Result]) -> str:
    lines = ["", "== gate summary =="]
    for result in results:
        status = "ok" if result.ok else f"FAILED ({result.code})"
        lines.append(f"  {result.check.name:<24} {status:<16} {result.seconds:6.1f}s")
    failed = [result for result in results if not result.ok]
    lines.append(f"  {len(results) - len(failed)}/{len(results)} gates passed")
    if failed:
        lines.append("  failures: " + ", ".join(result.check.name for result in failed))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="skip the slow pytest gate")
    parser.add_argument("--fail-fast", action="store_true", help="stop at the first failure")
    parser.add_argument("--list", action="store_true", help="list the gates and exit")
    args = parser.parse_args(argv)

    checks = selected(quick=args.quick)
    if args.list:
        for check in checks:
            print(f"{check.name:<24} {' '.join(check.command)}")
        return 0
    results = run(checks, fail_fast=args.fail_fast)
    print(summary(results))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())

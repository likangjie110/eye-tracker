#!/usr/bin/env python3
"""File / function / class size gate (ARCHITECTURE.md, tech-debt TD-CPLX-*).

The policy is "freeze the existing debt, gate the new code" (migration decision D10): an
oversized file, function or class that already exists is recorded in
``scripts/analysis/size-baseline.json``; from then on it may only shrink. Anything not in the
baseline must stay below the free thresholds, and a baseline entry that grows fails the check.

Usage::

    uv run python scripts/lint_size.py --check           # default; non-zero on violations
    uv run python scripts/lint_size.py --write-baseline  # record the current oversized set
    uv run python scripts/lint_size.py --json            # machine-readable result

The thresholds are deliberately generous: a file over 600 lines, a function over 60 lines or a
class over 300 lines is where a reviewer stops holding the code in their head. They are not style
preferences; the error message says how to fix the violation.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "eye_tracker"
BASELINE = ROOT / "scripts" / "analysis" / "size-baseline.json"

FREE_FILE_LINES = 600
FREE_FUNCTION_LINES = 60
FREE_CLASS_LINES = 300

FIX_HINT = (
    "split the module/function/class along its responsibilities (see the batch plans in "
    "docs/exec-plans/active/harness-migration.md); if this is pre-existing debt, record it in "
    "scripts/analysis/size-baseline.json with --write-baseline (the baseline may only shrink)"
)


@dataclass
class Entry:
    """One measured unit and the line count it is allowed to have."""

    key: str
    lines: int
    allowed: int | None = None
    kind: str = ""

    @property
    def violation(self) -> bool:
        if self.allowed is None:
            return True
        return self.lines > self.allowed


@dataclass
class Snapshot:
    files: dict[str, int] = field(default_factory=dict)
    functions: dict[str, int] = field(default_factory=dict)
    classes: dict[str, int] = field(default_factory=dict)


@dataclass
class Report:
    violations: list[Entry] = field(default_factory=list)
    shrinkable: list[Entry] = field(default_factory=list)
    missing_baseline: bool = False
    stale_baseline: list[str] = field(default_factory=list)


def _walk_body(body: list[ast.stmt], prefix: str) -> Iterator[tuple[str, str, int]]:
    """Yield ``(kind, qualname, lines)`` for functions and classes, nested ones included."""
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            name = f"{prefix}{node.name}"
            yield "function", name, (node.end_lineno or node.lineno) - node.lineno + 1
            yield from _walk_body(node.body, f"{name}.")
        elif isinstance(node, ast.ClassDef):
            name = f"{prefix}{node.name}"
            yield "class", name, (node.end_lineno or node.lineno) - node.lineno + 1
            yield from _walk_body(node.body, f"{name}.")


def collect(source: Path = SOURCE) -> Snapshot:
    snapshot = Snapshot()
    for path in sorted(source.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            rel = str(path.relative_to(ROOT))
        except ValueError:
            rel = str(path)
        text = path.read_text(encoding="utf-8")
        snapshot.files[rel] = len(text.splitlines())
        tree = ast.parse(text)
        for kind, qualname, lines in _walk_body(tree.body, ""):
            key = f"{rel}::{qualname}"
            (snapshot.functions if kind == "function" else snapshot.classes)[key] = lines
    return snapshot


def load_baseline(path: Path = BASELINE) -> Snapshot | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Snapshot(
        files=dict(data.get("files", {})),
        functions=dict(data.get("functions", {})),
        classes=dict(data.get("classes", {})),
    )


def check(snapshot: Snapshot, baseline: Snapshot) -> Report:
    report = Report()
    groups = (
        ("file", snapshot.files, baseline.files, FREE_FILE_LINES),
        ("function", snapshot.functions, baseline.functions, FREE_FUNCTION_LINES),
        ("class", snapshot.classes, baseline.classes, FREE_CLASS_LINES),
    )
    for kind, current, allowed, threshold in groups:
        for key, lines in current.items():
            if lines <= threshold:
                continue
            allowed_lines = allowed.get(key)
            entry = Entry(key=key, lines=lines, allowed=allowed_lines, kind=kind)
            if entry.violation:
                report.violations.append(entry)
            elif allowed_lines is not None and lines < allowed_lines:
                report.shrinkable.append(entry)
        for key in allowed:
            if key not in current:
                report.stale_baseline.append(key)
    report.violations.sort(key=lambda entry: (entry.kind, -entry.lines, entry.key))
    report.shrinkable.sort(key=lambda entry: (entry.kind, entry.key))
    return report


def write_baseline(snapshot: Snapshot, *, force: bool, path: Path = BASELINE) -> list[str]:
    """Record the oversized set. Returns the entries that would be added or grown."""
    first_time = not path.is_file()
    previous = load_baseline(path) or Snapshot()
    additions: list[str] = []
    for kind, current, old, threshold in (
        ("file", snapshot.files, previous.files, FREE_FILE_LINES),
        ("function", snapshot.functions, previous.functions, FREE_FUNCTION_LINES),
        ("class", snapshot.classes, previous.classes, FREE_CLASS_LINES),
    ):
        for key, lines in current.items():
            if lines <= threshold:
                continue
            if key not in old:
                additions.append(f"{kind} {key} ({lines} lines)")
            elif lines > old[key]:
                additions.append(f"{kind} {key} grew {old[key]} -> {lines}")
    if additions and not force and not first_time:
        return additions
    data: dict[str, Any] = {
        "note": "Frozen size debt for scripts/lint_size.py (D10: freeze existing, gate new).",
        "thresholds": {
            "file_lines": FREE_FILE_LINES,
            "function_lines": FREE_FUNCTION_LINES,
            "class_lines": FREE_CLASS_LINES,
        },
        "files": {
            key: lines for key, lines in sorted(snapshot.files.items()) if lines > FREE_FILE_LINES
        },
        "functions": {
            key: lines
            for key, lines in sorted(snapshot.functions.items())
            if lines > FREE_FUNCTION_LINES
        },
        "classes": {
            key: lines
            for key, lines in sorted(snapshot.classes.items())
            if lines > FREE_CLASS_LINES
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return []


def render(report: Report) -> str:
    lines: list[str] = []
    if report.missing_baseline:
        lines.append(
            "size gate: no baseline found. Run `uv run python scripts/lint_size.py "
            "--write-baseline` once and commit the result."
        )
        return "\n".join(lines)
    for entry in report.violations:
        allowed = "new" if entry.allowed is None else f"baseline {entry.allowed}"
        lines.append(
            f"size gate: {entry.kind} {entry.key} is {entry.lines} lines ({allowed}).\n"
            f"  fix: {FIX_HINT}"
        )
    for key in report.stale_baseline:
        lines.append(
            f"size gate: baseline entry no longer exists: {key}\n"
            "  fix: rerun --write-baseline to prune it"
        )
    for entry in report.shrinkable:
        lines.append(
            f"size gate: {entry.kind} {entry.key} shrank {entry.allowed} -> {entry.lines}; "
            "rerun --write-baseline to tighten the ratchet"
        )
    if not report.violations:
        lines.append("size gate: OK")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail on violations (default)")
    mode.add_argument(
        "--write-baseline", action="store_true", help="record the current oversized set"
    )
    parser.add_argument(
        "--force", action="store_true", help="allow growing/adding baseline entries"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--source", type=Path, default=SOURCE, help="tree to scan (tests use this)")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    args = parser.parse_args(argv)

    snapshot = collect(args.source)
    if args.write_baseline:
        additions = write_baseline(snapshot, force=args.force, path=args.baseline)
        if additions:
            print("size gate: refusing to grow the baseline without --force:", file=sys.stderr)
            for addition in additions:
                print(f"  {addition}", file=sys.stderr)
            return 1
        print(f"size gate: baseline written to {args.baseline}")
        return 0

    baseline = load_baseline(args.baseline)
    report = Report(missing_baseline=True) if baseline is None else check(snapshot, baseline)
    if args.json:
        print(
            json.dumps(
                {
                    "violations": [
                        {
                            "kind": entry.kind,
                            "key": entry.key,
                            "lines": entry.lines,
                            "baseline": entry.allowed,
                        }
                        for entry in report.violations
                    ],
                    "shrinkable": [
                        {
                            "kind": entry.kind,
                            "key": entry.key,
                            "lines": entry.lines,
                            "baseline": entry.allowed,
                        }
                        for entry in report.shrinkable
                    ],
                    "stale_baseline": report.stale_baseline,
                    "ok": not report.violations and not report.missing_baseline,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render(report))
    return 1 if report.violations or report.missing_baseline else 0


if __name__ == "__main__":
    sys.exit(main())

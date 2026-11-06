"""Lint the GitHub Actions workflows with PyYAML (phase 5 / TD-CI-020).

GitHub only validates workflow syntax after a push. This script runs in the local gate and in
CI's lint job and checks the structural rules the repository relies on:

* every workflow parses, has a name, top-level `permissions` and jobs;
* every job has a timeout (a hung job cannot burn runner minutes indefinitely);
* every `uses:` is either a local path or an action pinned to a 40-character commit SHA
  (with the version in a comment), so a tag cannot move under a workflow;
* no `pull_request_target` (it runs privileged code for fork PRs);
* untrusted event data is not interpolated directly into a `run:` script (shell injection);
* no deprecated `::set-output` / `::save-state` commands.

Exit code 0 only when no problem was found. ``--list`` prints the checked files.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_UNTRUSTED = re.compile(r"\$\{\{\s*(github\.(event|head_ref)|inputs\.|pull_request\.)")
_SCRIPT_COMMANDS = ("::set-output", "::save-state")


class _WorkflowLoader(yaml.SafeLoader):
    """YAML 1.1 parses the key ``on`` as boolean True; keep the raw name for errors."""


def _load(text: str) -> dict[str, Any]:
    data = yaml.load(text, Loader=_WorkflowLoader)
    return data if isinstance(data, dict) else {}


def _iter_uses(value: Any) -> list[str]:
    """Every ``uses:`` reference in a job/step tree."""
    found: list[str] = []
    if isinstance(value, dict):
        uses = value.get("uses")
        if isinstance(uses, str):
            found.append(uses)
        for child in value.values():
            found.extend(_iter_uses(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_iter_uses(child))
    return found


def _pinned(uses: str) -> bool:
    """Whether a `uses:` reference is local or an action pinned to a commit SHA."""
    if uses.startswith(("./", "docker://")):
        return uses.startswith("./")  # docker: tags are mutable; only local paths are allowed
    ref = uses.rsplit("@", 1)
    return len(ref) == 2 and bool(_SHA.fullmatch(ref[1]))


def _check_file(path: Path) -> list[str]:
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    try:
        data = _load(text)
    except yaml.YAMLError as exc:
        return [f"{path.name}: invalid YAML: {exc}"]
    name = path.name
    if not isinstance(data.get("name"), str):
        problems.append(f"{name}: missing workflow name")
    if "permissions" not in data:
        problems.append(f"{name}: missing top-level permissions")
    if "pull_request_target" in text:
        problems.append(f"{name}: pull_request_target runs privileged code for fork PRs")
    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        problems.append(f"{name}: no jobs")
        return problems
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            problems.append(f"{name}: job {job_name} is not a mapping")
            continue
        # Reusable-workflow call jobs run the called workflow's own jobs, which
        # have their timeouts; only jobs with steps need one here.
        if "uses" not in job and "timeout-minutes" not in job:
            problems.append(f"{name}: job {job_name} has no timeout-minutes")
        for uses in _iter_uses(job):
            if not _pinned(uses):
                problems.append(f"{name}: job {job_name} uses unpinned action {uses!r}")
        for step in job.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if isinstance(run, str):
                if _UNTRUSTED.search(run):
                    problems.append(
                        f"{name}: job {job_name} interpolates untrusted event data into run:"
                    )
                problems.extend(
                    f"{name}: job {job_name} uses deprecated {command}"
                    for command in _SCRIPT_COMMANDS
                    if command in run
                )
    return problems


def lint(root: Path = WORKFLOWS) -> list[str]:
    """All problems found in the workflow files under ``root``."""
    files = sorted(root.glob("*.yml")) + sorted(root.glob("*.yaml"))
    if not files:
        return [f"{root}: no workflow files"]
    problems: list[str] = []
    for path in files:
        problems.extend(_check_file(path))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="print the checked workflow files")
    args = parser.parse_args(argv)
    if args.list:
        for path in sorted(WORKFLOWS.glob("*.y*ml")):
            print(path.relative_to(ROOT))
        return 0
    problems = lint()
    if problems:
        for problem in problems:
            print(f"check_workflows: {problem}", file=sys.stderr)
        return 1
    print(f"check_workflows: OK ({len(list(WORKFLOWS.glob('*.y*ml')))} workflows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

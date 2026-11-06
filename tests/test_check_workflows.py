"""Tests for scripts/check_workflows.py (TD-CI-020)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_workflows", ROOT / "scripts" / "check_workflows.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load()

GOOD = """\
name: CI
on: [push]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - run: echo hello
"""


def _write(tmp_path: Path, text: str, name: str = "ci.yml") -> Path:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_current_workflows_are_clean() -> None:
    assert check.lint() == []


def test_a_clean_minimal_workflow_passes(tmp_path: Path) -> None:
    assert check.lint(_write(tmp_path, GOOD)) == []


def test_unpinned_action_fails(tmp_path: Path) -> None:
    bad = GOOD.replace(
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
        "actions/checkout@v7",
    )
    assert any("unpinned action" in p for p in check.lint(_write(tmp_path, bad)))


def test_missing_timeout_fails(tmp_path: Path) -> None:
    bad = GOOD.replace("    timeout-minutes: 10\n", "")
    assert any("no timeout-minutes" in p for p in check.lint(_write(tmp_path, bad)))


def test_pull_request_target_fails(tmp_path: Path) -> None:
    bad = GOOD.replace("on: [push]", "on: [pull_request_target]")
    assert any("pull_request_target" in p for p in check.lint(_write(tmp_path, bad)))


def test_untrusted_event_data_in_run_fails(tmp_path: Path) -> None:
    bad = GOOD.replace("echo hello", "echo ${{ github.event.pull_request.title }}")
    assert any("untrusted event data" in p for p in check.lint(_write(tmp_path, bad)))


def test_deprecated_script_commands_fail(tmp_path: Path) -> None:
    bad = GOOD.replace("echo hello", "echo ::set-output name=x::1")
    assert any("set-output" in p for p in check.lint(_write(tmp_path, bad)))


def test_invalid_yaml_and_empty_directory_report_problems(tmp_path: Path) -> None:
    _write(tmp_path, "name: [unclosed\n")
    assert any("invalid YAML" in p for p in check.lint(tmp_path))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert any("no workflow files" in p for p in check.lint(empty))


def test_cli_check_and_list(capsys: object) -> None:
    assert check.main([]) == 0
    assert check.main(["--list"]) == 0

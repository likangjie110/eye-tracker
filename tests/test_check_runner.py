"""The local gate runner must stay in sync with the CI gate list."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("check", ROOT / "scripts" / "check.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_gate_list_covers_every_check() -> None:
    check = _load()
    names = {gate.name for gate in check.CHECKS}
    assert names == {
        "ruff lint",
        "ruff format",
        "mypy (package)",
        "mypy (win32)",
        "mypy (darwin)",
        "mypy (scripts, packaging)",
        "privacy gate",
        "face models",
        "config docs",
        "size gate",
        "pytest",
    }


def test_quick_mode_only_drops_pytest() -> None:
    check = _load()
    quick = {gate.name for gate in check.selected(quick=True)}
    full = {gate.name for gate in check.selected(quick=False)}
    assert full - quick == {"pytest"}
    assert quick == full - {"pytest"}


def test_every_gate_points_at_an_existing_entry_point() -> None:
    check = _load()
    for gate in check.CHECKS:
        script = next((arg for arg in gate.command if arg.endswith(".py")), None)
        if script is None:
            continue
        assert (check.ROOT / script).is_file(), (gate.name, script)


def test_list_mode_prints_the_gates(capsys) -> None:
    check = _load()
    assert check.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "pytest" in out
    assert "size gate" in out

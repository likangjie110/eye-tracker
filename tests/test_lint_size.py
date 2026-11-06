"""The size gate must reject new oversized code, growth, and silent baseline edits.

``scripts/lint_size.py`` is policy, not decoration: these tests prove it fails when it should
(migration rule §11.1 item 4: every invariant must be shown to catch a violation).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("lint_size", ROOT / "scripts" / "lint_size.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tree(tmp_path: Path, lines: int) -> Path:
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    body = "\n".join(["def big() -> None:"] + ["    x = 1"] * (lines - 1))
    (source / "module.py").write_text(body + "\n", encoding="utf-8")
    return source


def test_new_oversized_function_is_a_violation(tmp_path: Path) -> None:
    lint = _load()
    snapshot = lint.collect(_tree(tmp_path, 70))
    baseline = lint.Snapshot()
    report = lint.check(snapshot, baseline)
    assert [(entry.kind, entry.key) for entry in report.violations] == [
        ("function", f"{tmp_path / 'src' / 'module.py'}::big")
    ]


def test_baselined_function_may_not_grow(tmp_path: Path) -> None:
    lint = _load()
    source = _tree(tmp_path, 70)
    key = f"{source / 'module.py'}::big"
    baseline = lint.Snapshot(functions={key: 70})
    grown = lint.collect(_tree(tmp_path, 75))
    assert [entry.key for entry in lint.check(grown, baseline).violations] == [key]

    shrunk = lint.collect(_tree(tmp_path, 65))
    report = lint.check(shrunk, baseline)
    assert report.violations == []
    assert [entry.key for entry in report.shrinkable] == [key]


def test_adding_a_baseline_entry_needs_force(tmp_path: Path) -> None:
    lint = _load()
    snapshot = lint.collect(_tree(tmp_path, 70))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"files": {}, "functions": {}, "classes": {}}), encoding="utf-8")

    additions = lint.write_baseline(snapshot, force=False, path=baseline)
    assert additions
    assert "70" in additions[0]
    assert json.loads(baseline.read_text(encoding="utf-8"))["functions"] == {}

    assert lint.write_baseline(snapshot, force=True, path=baseline) == []
    written = json.loads(baseline.read_text(encoding="utf-8"))
    assert list(written["functions"].values()) == [70]


def test_check_exit_code_and_message(tmp_path: Path, capsys) -> None:
    lint = _load()
    source = _tree(tmp_path, 70)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"files": {}, "functions": {}, "classes": {}}), encoding="utf-8")
    code = lint.main(["--check", "--source", str(source), "--baseline", str(baseline)])
    assert code == 1
    assert "size gate:" in capsys.readouterr().out


def test_missing_baseline_is_a_failure(tmp_path: Path) -> None:
    lint = _load()
    source = _tree(tmp_path, 2)
    assert (
        lint.main(["--check", "--source", str(source), "--baseline", str(tmp_path / "no.json")])
        == 1
    )

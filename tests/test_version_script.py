"""The shared version reader (scripts/version.py, TD-CI-004)."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import version as version_script

from eye_tracker import __version__


def test_reads_the_current_version() -> None:
    assert version_script.read_version() == __version__


def test_requires_a_literal_non_empty_string(tmp_path: Path) -> None:
    bad = tmp_path / "__init__.py"
    bad.write_text("__version__ = '1.2.3'\n", encoding="utf-8")
    assert version_script.read_version(bad) == "1.2.3"

    bad.write_text("__version__ = ''\n", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        version_script.read_version(bad)

    bad.write_text("__version__ = str(1)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="literal"):
        version_script.read_version(bad)

    bad.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no __version__"):
        version_script.read_version(bad)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.3.0", "0.3.0"), ("0.3", "0.3"), ("0.3.0-beta.1", "0.3.0"), ("12.4.5.6", "12.4.5")],
)
def test_numeric_prefix(raw: str, expected: str) -> None:
    assert version_script.numeric_version(raw) == expected


@pytest.mark.parametrize("raw", ["", "v0.3.0", "beta"])
def test_numeric_prefix_requires_digits(raw: str) -> None:
    with pytest.raises(ValueError, match="numeric"):
        version_script.numeric_version(raw)


def test_tag_must_be_v_plus_the_exact_version() -> None:
    assert version_script.tag_matches("0.3.0", "v0.3.0")
    assert not version_script.tag_matches("0.3.0", "0.3.0")
    assert not version_script.tag_matches("0.3.0", "v0.3.1")


def test_cli_prints_version_and_numeric(capsys: pytest.CaptureFixture[str]) -> None:
    assert version_script.main([]) == 0
    assert capsys.readouterr().out.strip() == __version__
    assert version_script.main(["--numeric"]) == 0
    assert capsys.readouterr().out.strip() == version_script.numeric_version(__version__)


def test_cli_rejects_a_mismatched_tag(capsys: pytest.CaptureFixture[str]) -> None:
    assert version_script.main(["--tag", "v999.0.0"]) == 1
    err = capsys.readouterr().err
    assert "does not match" in err
    assert "v999.0.0" in err


def test_cli_reports_a_broken_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "__init__.py"
    bad.write_text("__version__ = 1\n", encoding="utf-8")
    monkeypatch.setattr(version_script, "INIT", bad)
    assert version_script.main([]) == 1
    assert "version:" in capsys.readouterr().err

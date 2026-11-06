"""Local log and trace files are owner-only (phase 5 / TD-SEC-007)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from eye_tracker import logging_setup, trace

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful")


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_log_file_and_directory_are_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "logs" / "eye-tracker.log"
    monkeypatch.setattr(logging_setup.paths, "log_file", lambda: log_path)
    try:
        logging_setup.setup_logging("INFO", log_to_file=True)
        logging.getLogger("test").warning("hello")
    finally:
        logging_setup.shutdown_logging()
    assert _mode(log_path) == 0o600
    assert _mode(log_path.parent) == 0o700


def test_rotated_backup_stays_owner_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = tmp_path / "logs" / "eye-tracker.log"
    monkeypatch.setattr(logging_setup.paths, "log_file", lambda: log_path)
    monkeypatch.setattr(logging_setup, "MAX_BYTES", 10)
    try:
        logging_setup.setup_logging("INFO", log_to_file=True)
        log = logging.getLogger("test-rotate")
        log.warning("x" * 40)
        log.warning("y" * 40)
    finally:
        logging_setup.shutdown_logging()
    assert log_path.exists()
    backups = list(log_path.parent.glob("eye-tracker.log.*"))
    assert backups, "the rotating handler should have created a backup"
    for file in [log_path, *backups]:
        assert _mode(file) == 0o600


def test_trace_file_is_owner_only_and_tightens_existing(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "trace.jsonl"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    os.chmod(path, 0o644)
    writer = trace.TraceWriter(path)
    writer.write({"t": 1.0})
    writer.close()
    assert path.read_text(encoding="utf-8").splitlines()[0] == "old"
    assert _mode(path) == 0o600


def test_trace_creates_its_directory_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "fresh" / "trace.jsonl"
    writer = trace.TraceWriter(path)
    writer.write({"t": 1.0})
    writer.close()
    assert _mode(path.parent) == 0o700

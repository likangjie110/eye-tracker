"""The ``python -m eye_tracker`` entry point works (TD-TEST-010)."""

from __future__ import annotations

import subprocess
import sys

from eye_tracker import __version__


def test_module_entry_point_prints_the_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "eye_tracker", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert __version__ in result.stdout

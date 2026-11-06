"""Shared pieces of the start-at-login implementations (phase 4 / B2).

The OS modules import the constants, the error type, the entry model and the small file helpers
from here; ``autostart.py`` imports them too. This module imports no other autostart module.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .. import APP_ID, APP_NAME, APP_SLUG
from ..config import atomic_write_text

log = logging.getLogger(__name__)

BACKGROUND_FLAG = "--background"
CONFIG_DIR_FLAG = "--config-dir"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: Where Task Manager's "Startup apps" page records entries the user disabled.
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
RUN_VALUE_NAME = "EyeTracker"
LAUNCH_AGENT_LABEL = APP_ID
DESKTOP_FILE_NAME = f"{APP_SLUG}.desktop"

#: Names the windowed executable may have next to a console ``*-cli`` executable.
_GUI_EXECUTABLE_NAMES = ("EyeTracker.exe", "EyeTracker", "Eye Tracker", APP_SLUG)
#: Characters that force quoting inside a Desktop Entry ``Exec`` value.
_DESKTOP_RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")
#: Characters escaped with a backslash inside a quoted ``Exec`` argument.
_DESKTOP_QUOTED_ESCAPES = frozenset('"`$\\')
#: Key-file string escapes (applied before ``Exec`` quoting is interpreted).
_DESKTOP_STRING_ESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}

_TRANSIENT_LOCATION_MESSAGE = (
    f"{APP_NAME} is running from its disk image or from a temporary location, "
    "which will not exist at the next login. Move it to the Applications folder, "
    "open it from there, then turn on Start at login."
)
_LOGIN_ITEMS_MESSAGE = (
    f"macOS keeps {APP_NAME} from starting at login. Allow it in System Settings › "
    "General › Login Items (under “Allow in the Background”)."
)

#: launchd's command-line tool, by absolute path (an app may start with a bare PATH).
_LAUNCHCTL = "/bin/launchctl"
_LAUNCHCTL_TIMEOUT_S = 2.0
#: One line of ``launchctl print-disabled``: ``"<label>" => <state>``.
_LAUNCHD_ENTRY_RE = re.compile(r'^\s*"(?P<label>[^"]+)"\s*=>\s*(?P<state>\S+)\s*$')


class AutostartError(OSError):
    """Enabling or disabling start at login failed (message is user-presentable)."""


@dataclass(frozen=True, slots=True)
class _Entry:
    """A login entry as found on disk or in the registry."""

    #: Its command line split into arguments (empty when there is none).
    command: list[str]
    #: ``False`` when the OS or desktop switched it off without deleting it.
    active: bool


def _write_text(path: Path, text: str) -> None:
    atomic_write_text(path, text)
    try:
        # mkstemp creates 0600 files; login entries are conventionally 0644.
        path.chmod(0o644)
    except OSError:
        log.debug("Could not chmod %s", path, exc_info=True)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)

"""macOS start-at-login: a LaunchAgent plist and the launchd disabled database.

Split out of ``platform/autostart.py`` (phase 4 / B2), verbatim except for imports.
"""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

from .autostart_common import (
    _LAUNCHCTL,
    _LAUNCHCTL_TIMEOUT_S,
    _LAUNCHD_ENTRY_RE,
    _LOGIN_ITEMS_MESSAGE,
    LAUNCH_AGENT_LABEL,
    AutostartError,
    _Entry,
    _write_text,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------- macOS
def _mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _mac_plist(command: Sequence[str]) -> bytes:
    agent = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": list(command),
        "RunAtLoad": True,
        # A user-facing app: no background QoS throttling of the camera loop.
        "ProcessType": "Interactive",
        # Only in the graphical login session (not SSH or the login window).
        "LimitLoadToSessionType": "Aqua",
    }
    return plistlib.dumps(agent, fmt=plistlib.FMT_XML, sort_keys=True)


def _mac_enable(command: Sequence[str]) -> None:
    _write_text(_mac_plist_path(), _mac_plist(command).decode("utf-8"))
    if not _mac_disabled_in_launchd():
        return
    # Switched off in launchd's records, which outlive the plist: without this
    # the checkbox would silently do nothing. ``enable`` only clears that
    # record; it does not load the agent, which still starts at the next login.
    domain = _mac_launchd_domain()
    if domain is not None:
        _launchctl("enable", f"{domain}/{LAUNCH_AGENT_LABEL}")
    if _mac_disabled_in_launchd():
        raise AutostartError(_LOGIN_ITEMS_MESSAGE)


def _mac_disabled_in_launchd() -> bool:
    """Whether launchd itself keeps the agent from starting, whatever the plist says.

    ``launchctl disable`` (and ``unload -w``) record a switch-off in launchd's
    per-user database and leave the plist untouched; the Login Items switch in
    System Settings may do so as well. ``False`` when that cannot be read.
    """
    domain = _mac_launchd_domain()
    if domain is None:
        return False
    listing = _launchctl("print-disabled", domain)
    return listing is not None and _launchd_lists_disabled(listing, LAUNCH_AGENT_LABEL)


def _launchd_lists_disabled(listing: str, label: str) -> bool:
    """Whether ``launchctl print-disabled`` output switches ``label`` off.

    Its lines read ``"<label>" => disabled`` (``=> true`` on older macOS);
    other sections of the output map labels to app identifiers instead.
    """
    for line in listing.splitlines():
        match = _LAUNCHD_ENTRY_RE.match(line)
        if match and match["label"] == label and match["state"].lower() in {"disabled", "true"}:
            return True
    return False


def _mac_launchd_domain() -> str | None:
    """launchd's domain for this user's login session (``gui/<uid>``); ``None`` off POSIX."""
    getuid = getattr(os, "getuid", None)
    return f"gui/{getuid()}" if getuid is not None else None


def _launchctl(*args: str) -> str | None:
    """Run ``launchctl`` and return its output; ``None`` when it failed (tests replace it)."""
    argv = [_LAUNCHCTL, *args]
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("%s failed: %s", " ".join(argv), exc)
        return None
    if result.returncode != 0:
        log.debug(
            "%s exited with %s: %s",
            " ".join(argv),
            result.returncode,
            (result.stderr or "").strip()[:200],
        )
        return None
    return result.stdout or ""


def _mac_entry() -> _Entry | None:
    path = _mac_plist_path()
    try:
        agent = plistlib.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except Exception:
        log.debug("Unreadable LaunchAgent %s", path, exc_info=True)
        return _Entry([], active=False)  # launchd would ignore it as well
    if not isinstance(agent, dict):
        return _Entry([], active=False)
    arguments = agent.get("ProgramArguments")
    command = (
        [str(arg) for arg in arguments]
        if isinstance(arguments, list) and all(isinstance(a, str) for a in arguments)
        else []
    )
    active = agent.get("Disabled") is not True
    if active and command and _mac_disabled_in_launchd():
        active = False  # launchd will not start it at login (see _mac_disabled_in_launchd)
    return _Entry(command, active=active)

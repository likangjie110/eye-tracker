"""Pure desktop/session detection and the lock/display-power command tables.

Split out of ``platform/linux.py`` (phase 4 / B2).
"""

from __future__ import annotations

import ctypes
import importlib.util
import logging
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from .linux_common import (
    _CAMERA_BROKERS,
    _COMPOSITOR_SOCKET_VARS,
    _DBUS_MISSING_MARKERS,
    _GDBUS_UINT_RE,
    _GNOME_SHELL_VENDORS,
    _LOGIND_LOCK_DESKTOPS,
    _XSET_DPMS_RE,
    _YDOTOOL_DEFAULT_SOCKETS,
)

log = logging.getLogger(__name__)


# pure helpers
# ---------------------------------------------------------------------------
def _getuid() -> int | None:
    """The real user id; ``None`` on systems without one (the tests run on Windows)."""
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid is not None else None


def _parse_gdbus_uint(text: str | None) -> int | None:
    """Parse a single unsigned integer from ``gdbus call`` output, e.g. ``(uint64 12345,)``."""
    match = _GDBUS_UINT_RE.search(text or "")
    return int(match.group(1)) if match else None


def _parse_locked_hint(text: str | None) -> bool | None:
    """Parse ``loginctl show-session -p LockedHint [--value]`` output."""
    lines = (text or "").strip().splitlines()
    if not lines:
        return None
    value = lines[0].strip()
    if "=" in value:
        value = value.split("=", 1)[1].strip()
    value = value.lower()
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def _parse_xset_dpms(text: str | None) -> tuple[bool, bool] | None:
    """``(capable, enabled)`` from ``xset q`` output; ``None`` when it does not say."""
    lowered = (text or "").lower()
    if "does not have the dpms extension" in lowered or "not capable of dpms" in lowered:
        return False, False
    match = _XSET_DPMS_RE.search(lowered)
    if match is None:
        return None
    return True, match.group(1) == "enabled"


def _is_missing_service(text: str | None) -> bool:
    """Whether a D-Bus error says the service or method does not exist (vs. a hiccup)."""
    return any(marker in (text or "") for marker in _DBUS_MISSING_MARKERS)


def _desktop_tokens(env: Mapping[str, str]) -> set[str]:
    """Lower-cased desktop names from ``XDG_CURRENT_DESKTOP`` and friends."""
    raw = ":".join(
        env.get(key, "")
        for key in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")
    )
    return {token.strip().lower() for token in re.split(r"[:;]", raw) if token.strip()}


def _is_wayland_env(env: Mapping[str, str]) -> bool:
    return env.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland" or bool(
        env.get("WAYLAND_DISPLAY")
    )


def _is_kde_env(env: Mapping[str, str]) -> bool:
    tokens = _desktop_tokens(env)
    return bool(env.get("KDE_FULL_SESSION")) or any(
        "kde" in token or "plasma" in token for token in tokens
    )


def _is_gnome_env(env: Mapping[str, str]) -> bool:
    # Covers "GNOME", "ubuntu:GNOME", "pop:GNOME", "GNOME-Classic", "Budgie:GNOME" …
    return any("gnome" in token for token in _desktop_tokens(env))


def _standalone_compositor(env: Mapping[str, str]) -> bool:
    """Whether a tiling window manager / compositor runs the session.

    Such sessions often export ``XDG_CURRENT_DESKTOP=GNOME`` (or KDE) so that
    portals and settings apps work; their own sockets tell the truth, and then
    no GNOME or KDE lock screen exists to react to logind or report the lock.
    """
    return any(env.get(key) for key in _COMPOSITOR_SOCKET_VARS)


def _desktop_handles_logind_lock(env: Mapping[str, str]) -> bool:
    """Whether the desktop's own locker reacts to ``loginctl lock-session``."""
    if _standalone_compositor(env):
        return False
    if env.get("KDE_FULL_SESSION"):
        return True
    return any(name in token for token in _desktop_tokens(env) for name in _LOGIND_LOCK_DESKTOPS)


def _session_desktop(env: Mapping[str, str]) -> str:
    """The session's own desktop: the first ``XDG_CURRENT_DESKTOP`` entry (later
    ones are compatibility fallbacks), else the session name; lower-cased."""
    for key in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
        for token in re.split(r"[:;]", env.get(key, "")):
            if token.strip():
                return token.strip().lower()
    return ""


def _runs_gnome_shell(env: Mapping[str, str]) -> bool:
    """Whether GNOME Shell (or GNOME Flashback) runs the session, as opposed to a
    desktop that merely lists GNOME as a fallback (see :data:`_GNOME_SHELL_VENDORS`)."""
    own = _session_desktop(env)
    return "gnome" in own or own in _GNOME_SHELL_VENDORS


def _locked_hint_authoritative(env: Mapping[str, str]) -> bool:
    """Desktops whose lock screen always reports itself through logind's ``LockedHint``.

    Only these may have a locker's claimed success checked against the hint;
    elsewhere a lock that the hint never shows would be taken for a failure,
    and the next locker would lock the user a second time.
    """
    if _standalone_compositor(env):
        return False
    return _is_kde_env(env) or _runs_gnome_shell(env)


def _lock_commands(session_id: str | None) -> list[list[str]]:
    """Screen-lock strategies, most universal first."""
    commands: list[list[str]] = [
        # logind asks whichever locker the desktop registered (GNOME, KDE, Cinnamon,
        # xss-lock for tiling WMs …). Without an id it targets the caller's session.
        ["loginctl", "lock-session", session_id] if session_id else ["loginctl", "lock-session"],
        ["xdg-screensaver", "lock"],
        ["gnome-screensaver-command", "-l"],
        ["dm-tool", "lock"],
        ["xflock4"],
    ]
    # KDE's qdbus is packaged under several names depending on distro and Qt version.
    commands.extend(
        [tool, "org.freedesktop.ScreenSaver", "/ScreenSaver", "Lock"]
        for tool in ("qdbus", "qdbus6", "qdbus-qt6", "qdbus-qt5")
    )
    commands.append(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.ScreenSaver",
            "--object-path",
            "/ScreenSaver",
            "--method",
            "org.freedesktop.ScreenSaver.Lock",
        ]
    )
    return commands


def _lock_method_label(argv: Sequence[str]) -> str:
    """How ``doctor`` names a :func:`_lock_commands` entry (ASCII, like the report).

    The D-Bus clients are named with the call they make; "gdbus call" alone
    would not say what it does.
    """
    if argv[0] == "gdbus" or argv[0].startswith("qdbus"):
        return f"{argv[0]} org.freedesktop.ScreenSaver.Lock"
    return " ".join(argv[:2])


def _display_power_commands(env: Mapping[str, str], on: bool) -> list[list[str]]:
    """Desktop-specific display power strategies, in the order they are tried.

    X11 DPMS is handled separately (:meth:`LinuxPlatform._x11_dpms`): under
    XWayland it would only affect a virtual output, and ``xset`` needs care.
    """
    wayland = _is_wayland_env(env)
    commands: list[list[str]] = []
    if wayland and _is_kde_env(env):
        commands.append(["kscreen-doctor", "--dpms", "on" if on else "off"])
    if _is_gnome_env(env):
        mode = "0" if on else "1"  # Mutter PowerSaveMode: 0 = on, 1 = standby
        commands.append(
            [
                "busctl",
                "--user",
                "set-property",
                "org.gnome.Mutter.DisplayConfig",
                "/org/gnome/Mutter/DisplayConfig",
                "org.gnome.Mutter.DisplayConfig",
                "PowerSaveMode",
                "i",
                mode,
            ]
        )
        commands.append(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.gnome.Mutter.DisplayConfig",
                "--object-path",
                "/org/gnome/Mutter/DisplayConfig",
                "--method",
                "org.freedesktop.DBus.Properties.Set",
                "org.gnome.Mutter.DisplayConfig",
                "PowerSaveMode",
                f"<int32 {mode}>",
            ]
        )
    if wayland and env.get("SWAYSOCK"):
        state = "on" if on else "off"
        # "power" replaced "dpms" in sway 1.8; older versions only know "dpms".
        commands.append(["swaymsg", "output", "*", "power", state])
        commands.append(["swaymsg", "output", "*", "dpms", state])
    if wayland and env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        commands.append(["hyprctl", "dispatch", "dpms", "on" if on else "off"])
    return commands


def _ydotool_socket_present(env: Mapping[str, str]) -> bool:
    """Whether ``ydotoold`` appears to be running (its socket exists).

    ydotool 1.x cannot inject anything without the daemon; the client looks at
    ``$YDOTOOL_SOCKET``, else ``$XDG_RUNTIME_DIR/.ydotool_socket`` or
    ``/tmp/.ydotool_socket`` depending on its version.
    """
    explicit = env.get("YDOTOOL_SOCKET", "").strip()
    if explicit:
        candidates: list[str] = [explicit]
    else:
        runtime = env.get("XDG_RUNTIME_DIR", "").strip()
        candidates = [os.path.join(runtime, ".ydotool_socket")] if runtime else []
        candidates.extend(_YDOTOOL_DEFAULT_SOCKETS)
    return any(os.path.exists(path) for path in candidates)


def _pointer_command_ok(argv: Sequence[str], result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 0:
        return False
    if argv[0] == "hyprctl":
        # hyprctl exits with 0 even when the dispatcher failed; it prints "ok" on success.
        return (result.stdout or "").strip().lower() in ("", "ok")
    return True


def _xcb_plugin_usable() -> bool:
    """Whether Qt's xcb platform plugin can load.

    Qt >= 6.5 needs ``libxcb-cursor.so.0``, which many Wayland-first installs lack.
    """
    try:
        ctypes.CDLL("libxcb-cursor.so.0")
    except OSError:
        return False
    return True


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


#: ``/proc/<pid>/loginuid`` of a process that did not come from a login: (uid_t)-1.
_AUDIT_UID_UNSET = 0xFFFF_FFFF


def _login_uid(proc_dir: str) -> int | None:
    """The audit login UID of a process; ``None`` when unset or unreadable."""
    text = _read_text(os.path.join(proc_dir, "loginuid")).strip()
    if not text.isdigit():
        return None
    value = int(text)
    return None if value == _AUDIT_UID_UNSET else value


def _belongs_to_user(entry: os.DirEntry[str], own_login: int | None, uid: int | None) -> bool:
    """Whether the process at ``entry`` belongs to this user (see ``_locker_running``).

    It does when this user owns its ``/proc`` entry, or when its audit login
    UID is ours (``own_login``, or ``uid`` when this process has none): the
    latter survives the setuid tricks of slock & co. A login UID that is unset
    on either side proves nothing, so then only the owner counts.
    """
    if uid is None:
        return True  # no users to tell apart (not a POSIX system: tests)
    login = _login_uid(entry.path)
    if login is not None and login == (own_login if own_login is not None else uid):
        return True
    try:
        return entry.stat(follow_symlinks=False).st_uid == uid
    except OSError:
        return False  # exited meanwhile


def _is_camera_broker(proc_dir: str) -> bool:
    comm = _read_text(os.path.join(proc_dir, "comm")).strip()
    return comm.startswith(_CAMERA_BROKERS)


def _maps_video_device(proc_dir: str) -> bool:
    """Streaming V4L2 clients mmap the device's buffers; idle monitors do not."""
    return "/dev/video" in _read_text(os.path.join(proc_dir, "maps"))


def _video_target(target: str, devices: set[str] | None) -> bool:
    path = target.split(" (deleted)", 1)[0]
    if devices is None:
        return path.startswith("/dev/video")
    return path in devices


def _as_xid(handle: Any) -> int | None:
    if isinstance(handle, bool) or not isinstance(handle, int) or handle <= 0:
        return None
    return handle


def _is_connection_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) or type(exc).__name__ in {
        "ConnectionClosedError",
        "DisplayConnectionError",
    }


# ---------------------------------------------------------------------------

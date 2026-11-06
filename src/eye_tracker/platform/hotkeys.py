"""System-wide keyboard shortcuts.

The module has two halves:

* **Parsing and formatting** (:func:`parse_hotkey`, :func:`format_hotkey`) are pure
  Python and work everywhere. Hotkeys are stored in the settings file as short,
  case-insensitive strings such as ``"ctrl+alt+p"``.
* **Registration** is done by a :class:`HotkeyManager`. :func:`create_hotkey_manager`
  picks the native implementation for the running system:

  ===========  ==========================================================================
  Windows      ``RegisterHotKey`` on a dedicated message-loop thread
  macOS        Carbon ``RegisterEventHotKey`` (no permission prompt needed); the
               events are delivered by the Cocoa run loop that Qt already spins
  Linux/X11    passive key grabs through python-xlib on a dedicated thread
  Wayland      unsupported: bind the ``ctl`` commands in the desktop's own keyboard
               settings instead (the manager's note names them as this copy is run)
  ===========  ==========================================================================

A global hotkey swallows its key system-wide, so combinations that the user types
text with are refused (:meth:`HotkeyManager.layout_conflict`): on Windows AltGr
arrives as Ctrl+Alt (Ctrl+Alt+C is 'ć' on a Polish keyboard), on macOS Option
alone types characters, and on X11 a keyboard-layout switch such as Alt+Shift
makes every hotkey containing both keys impossible to press. Keys follow the
keyboard layout on every system, and :meth:`HotkeyManager.last_error` explains
any refusal in a user-presentable way.

Callbacks may be invoked on *any* thread (the hotkey thread on Windows/X11, the
main thread on macOS). They must return quickly and marshal work to the Qt main
thread themselves, for example by emitting a queued signal.

Only the registered combinations are ever observed. Nothing here installs a
keyboard hook or sees any other key press.
"""

from __future__ import annotations

import logging
import os
import sys

from .hotkeys_base import (
    KEYS,
    MODIFIERS,
    Hotkey,
    HotkeyCallback,
    HotkeyManager,
    format_hotkey,
    parse_hotkey,
)
from .hotkeys_macos import MacHotkeyManager
from .hotkeys_windows import WindowsHotkeyManager
from .hotkeys_x11 import X11HotkeyManager

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- factory
def _is_wayland_session() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(
        os.environ.get("WAYLAND_DISPLAY")
    )


#: The ``ctl`` commands to bind where global hotkeys are unavailable.
_CTL_SHORTCUT_ACTIONS = ("toggle", "privacy-toggle", "calibrate")


def _ctl_commands() -> str:
    """``'… ctl toggle', '… ctl privacy-toggle' or '… ctl calibrate'``, spelled as
    this copy is run: the release packages put no ``eye-tracker`` command on the
    PATH (an AppImage is called by its file name, the macOS app has
    ``eye-tracker-cli`` inside it), and a desktop shortcut bound to a command
    that does not exist fails without a word. Built when a manager is created,
    because a ``--config-dir`` profile becomes part of the command."""
    from ..command_text import cli_command_text  # naming only; no runtime dependency

    commands = [f"'{cli_command_text('ctl', action)}'" for action in _CTL_SHORTCUT_ACTIONS]
    return f"{', '.join(commands[:-1])} or {commands[-1]}"


def _wayland_note() -> str:
    return (
        "Wayland does not allow applications to register global hotkeys. Bind "
        f"{_ctl_commands()} to shortcuts in your desktop's keyboard settings."
    )


def _xwayland_note() -> str:
    return (
        "Running under Wayland: hotkeys are grabbed through XWayland and may only fire "
        "while an X11 window has focus. For reliable shortcuts bind "
        f"{_ctl_commands()} in your desktop's keyboard settings."
    )


def create_hotkey_manager() -> HotkeyManager:
    """Return the best hotkey manager for this system (never raises)."""
    try:
        if sys.platform == "win32":
            return WindowsHotkeyManager()
        if sys.platform == "darwin":
            return MacHotkeyManager()
        if not os.environ.get("DISPLAY"):
            if _is_wayland_session():
                return HotkeyManager(note=_wayland_note())
            return HotkeyManager(note="No X11 display is available for global hotkeys.")
        try:
            import Xlib  # noqa: F401  (availability probe only)
        except ImportError:
            return HotkeyManager(note="python-xlib is not installed; global hotkeys are off.")
        note = _xwayland_note() if _is_wayland_session() else None
        return X11HotkeyManager(note=note)
    except Exception as exc:  # defensive: hotkeys are optional
        log.warning("Global hotkeys unavailable: %s", exc)
        return HotkeyManager(note=f"Global hotkeys unavailable: {exc}")


__all__ = [
    "KEYS",
    "MODIFIERS",
    "Hotkey",
    "HotkeyCallback",
    "HotkeyManager",
    "MacHotkeyManager",
    "WindowsHotkeyManager",
    "X11HotkeyManager",
    "create_hotkey_manager",
    "format_hotkey",
    "parse_hotkey",
]

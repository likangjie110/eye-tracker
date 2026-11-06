"""Start at login ("autostart") on Windows, macOS and Linux.

Each OS uses its native per-user mechanism; none needs administrator rights:

* **Windows**: a ``HKCU\\...\\CurrentVersion\\Run`` value named ``EyeTracker``
  (the same value the installer's "Start at login" task writes).
* **macOS**: a LaunchAgent plist in ``~/Library/LaunchAgents``. It is only
  written, never loaded with ``launchctl``: launchd picks it up at next login,
  which is exactly when it is needed. A switch-off that launchd records in its
  own database (``launchctl disable``, possibly the Login Items switch in
  System Settings) leaves the plist untouched; it is read back with
  ``launchctl print-disabled`` and cleared by :func:`enable` with
  ``launchctl enable``, which does not load the agent either.
* **Linux**: an XDG autostart entry, ``$XDG_CONFIG_HOME/autostart/eye-tracker.desktop``.

Profiles
--------
Every function takes ``config_dir``: the ``--config-dir`` profile the entry is
for. ``None`` means the profile of the running process (its ``--config-dir``,
if it was started with one). A non-default profile's entry carries
``--config-dir <dir>`` in its command, so the login launch opens the same
settings and calibration and is recognised as the same instance.

There is still only one entry per user: the camera and the cursor can serve
one tracker, so turning start at login on in one profile replaces another
profile's entry, and :func:`status` reports such an entry as
:attr:`Status.OTHER_PROFILE` rather than as enabled.

Stale entries
-------------
An entry records the path of the program that wrote it. That path can go away:
a replaced AppImage, a moved portable folder, a deleted virtual environment, or
a macOS app that ran from its disk image or from Gatekeeper's App Translocation
mount. :func:`status` reports those as :attr:`Status.STALE`, :func:`enable`
refuses to register a macOS app from such a location, and :func:`refresh`
(called by the app at startup) re-points a stale entry at the running copy.

Filesystem locations and registry access go through small module-level helpers
(``_mac_plist_path``, ``_linux_desktop_path``, ``_reg_read``/``_reg_write_str``/
``_reg_delete``) so tests can redirect them.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from .. import paths
from . import autostart_linux, autostart_macos, autostart_windows
from .autostart_common import (
    _GUI_EXECUTABLE_NAMES,
    _TRANSIENT_LOCATION_MESSAGE,
    BACKGROUND_FLAG,
    CONFIG_DIR_FLAG,
    RUN_KEY,
    RUN_VALUE_NAME,
    AutostartError,
    _Entry,
    _unlink,
    log,
)


class Status(StrEnum):
    """State of the login entry, as seen from one ``--config-dir`` profile."""

    #: The entry starts this profile, and its program exists.
    ENABLED = "enabled"
    #: No entry, or one that was switched off (Task Manager, GNOME Tweaks, launchd).
    DISABLED = "disabled"
    #: The entry starts this profile, but its program is gone or sits in a
    #: temporary location (macOS disk image, App Translocation).
    STALE = "stale"
    #: The entry starts another ``--config-dir`` profile.
    OTHER_PROFILE = "other-profile"


def is_supported() -> bool:
    """Whether this OS has an autostart implementation."""
    return _system() in {"windows", "macos", "linux"}


def launch_command(background: bool = True, config_dir: Path | None = None) -> list[str]:
    """Command line that starts this installation of the app.

    * frozen build inside an AppImage: the ``.AppImage`` file (the mounted
      bundle path changes on every run);
    * other frozen builds: the windowed executable (inside the ``.app`` bundle
      on macOS);
    * source/pip installs: ``python -m eye_tracker``, using ``pythonw.exe`` on
      Windows so no console window opens at login.

    A non-default profile (``config_dir``, see the module docs) adds
    ``--config-dir <dir>``. ``background`` appends ``--background`` (no
    first-run wizard or startup notifications), which is what a login launch
    wants.
    """
    command = _base_command()
    profile = _profile_dir(config_dir)
    if profile is not None:
        command += [CONFIG_DIR_FLAG, str(profile)]
    if background:
        command.append(BACKGROUND_FLAG)
    return command


def registered_command() -> list[str] | None:
    """The command of the login entry, split into arguments; ``None`` without one.

    Includes entries that were switched off in Task Manager or the desktop's
    settings. Never raises.
    """
    try:
        entry = _read_entry()
    except Exception:
        log.warning("Could not read the start-at-login entry", exc_info=True)
        return None
    return list(entry.command) if entry is not None and entry.command else None


def status(config_dir: Path | None = None) -> Status:
    """State of the login entry for the ``config_dir`` profile. Never raises."""
    try:
        entry = _read_entry()
    except Exception:
        log.warning("Could not read the start-at-login state", exc_info=True)
        return Status.DISABLED
    if entry is None or not entry.active or not entry.command:
        return Status.DISABLED
    if not _same_path(_command_profile(entry.command), _profile_dir(config_dir)):
        return Status.OTHER_PROFILE
    if not _program_available(entry.command[0]):
        return Status.STALE
    return Status.ENABLED


def is_enabled(config_dir: Path | None = None) -> bool:
    """Whether the app is registered to start the ``config_dir`` profile at login.

    ``False`` for an entry whose program no longer exists or that starts
    another profile (see :func:`status`). Never raises.
    """
    return status(config_dir) is Status.ENABLED


def enable(background: bool = True, config_dir: Path | None = None) -> None:
    """Register the app to start the ``config_dir`` profile at login.

    Replaces any previous entry, including another profile's. Raises
    :class:`AutostartError` on failure, on an unsupported OS, or on macOS when
    the app runs from its disk image or a translocated copy.
    """
    system = _system()
    try:
        command = launch_command(background, config_dir)
        if _is_transient_location(command[0]):
            raise AutostartError(_TRANSIENT_LOCATION_MESSAGE)
        if system == "windows":
            autostart_windows._win_enable(command)
        elif system == "macos":
            autostart_macos._mac_enable(command)
        elif system == "linux":
            autostart_linux._linux_enable(command)
        else:
            raise AutostartError(f"Start at login is not supported on {sys.platform}")
    except AutostartError:
        raise
    except Exception as exc:
        raise AutostartError(f"Could not enable start at login: {exc}") from exc
    log.info("Start at login enabled (%s)", location())


def disable(config_dir: Path | None = None) -> None:
    """Remove the login entry of the ``config_dir`` profile. A missing entry is not an error.

    An entry that starts another profile is left alone (and logged): it is not
    this profile's to remove. Raises :class:`AutostartError` when an existing
    entry cannot be removed.
    """
    system = _system()
    if system not in {"windows", "macos", "linux"}:
        return
    try:
        entry = _read_entry()
    except Exception:
        entry = None  # unreadable: removing it is the only sensible thing to do
    if entry is not None and entry.command:
        owner = _command_profile(entry.command)
        if not _same_path(owner, _profile_dir(config_dir)):
            log.info("Start at login starts another profile (%s); left alone", owner or "default")
            return
    try:
        if system == "windows":
            autostart_windows._win_disable()
        elif system == "macos":
            _unlink(autostart_macos._mac_plist_path())
        else:
            _unlink(autostart_linux._linux_desktop_path())
    except Exception as exc:
        raise AutostartError(f"Could not disable start at login: {exc}") from exc
    log.info("Start at login disabled")


def refresh(config_dir: Path | None = None) -> bool:
    """Point this profile's login entry at the running copy of the app, if it moved.

    Meant for app startup. The entry is rewritten when it is active, starts the
    ``config_dir`` profile and records a different command than
    :func:`launch_command` gives now, and

    * its program is gone or in a temporary location (a replaced AppImage, a
      moved folder, a macOS app first run from its disk image), or
    * this is a packaged build, which then replaces what the entry started
      (e.g. an older AppImage, or a source checkout's interpreter).

    It is never pointed at a temporary location, and a working entry is not
    taken over by a source run (a developer's checkout). With several packaged
    copies of one profile, the one started last wins. The ``--background``
    choice of the entry is kept. Returns ``True`` when the entry was rewritten.
    Never raises.
    """
    try:
        entry = _read_entry()
        if entry is None or not entry.active or not entry.command:
            return False
        profile = _profile_dir(config_dir)
        if not _same_path(_command_profile(entry.command), profile):
            return False
        background = BACKGROUND_FLAG in entry.command[1:]
        wanted = launch_command(background, profile)
        if _same_command(entry.command, wanted) or _is_transient_location(wanted[0]):
            return False
        if _program_available(entry.command[0]) and not paths.is_frozen():
            return False
        enable(background, profile)
        log.info("Start at login updated: %s (was %s)", wanted[0], entry.command[0])
    except Exception:
        log.warning("Could not update the start-at-login entry", exc_info=True)
        return False
    return True


def location() -> str | None:
    """Human-readable location of the login entry (for diagnostics)."""
    system = _system()
    if system == "windows":
        return f"HKCU\\{RUN_KEY}\\{RUN_VALUE_NAME}"
    if system == "macos":
        return str(autostart_macos._mac_plist_path())
    if system == "linux":
        return str(autostart_linux._linux_desktop_path())
    return None


# -------------------------------------------------------------------- command
def _system(platform: str | None = None) -> str:
    """``"windows"``, ``"macos"``, ``"linux"`` or ``"unsupported"`` (tests patch this)."""
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        return "windows"
    if platform == "darwin":
        return "macos"
    if platform.startswith("linux"):
        return "linux"
    return "unsupported"


def _base_command() -> list[str]:
    if paths.is_frozen():
        # Only trusted when frozen: APPIMAGE is inherited by every child of an
        # AppImage (a terminal emulator AppImage, for instance).
        appimage = os.environ.get("APPIMAGE", "")
        if _system() == "linux" and appimage and Path(appimage).is_file():
            return [appimage]
        return [str(_frozen_gui_executable())]
    return [_source_interpreter(), "-m", "eye_tracker"]


def _frozen_gui_executable() -> Path:
    """The windowed executable, even when called from a console one.

    Registering a console build would open a terminal window at every login.
    The console builds are the ``*-cli`` executables and, on Windows, every
    other executable not named like the windowed one: the installer adds
    ``eye-tracker.exe`` (a copy of ``eye-tracker-cli.exe``) for the
    ``eye-tracker`` command on the PATH, so ``eye-tracker autostart enable``
    runs as that.
    """
    exe = Path(sys.executable)
    gui_names = {name.lower() for name in _GUI_EXECUTABLE_NAMES}
    console = exe.stem.lower().endswith("-cli") or (
        _system() == "windows" and exe.name.lower() not in gui_names
    )
    if console:
        for name in _GUI_EXECUTABLE_NAMES:
            candidate = exe.with_name(name)
            if candidate.name.lower() != exe.name.lower() and candidate.is_file():
                return candidate
    return exe


def _source_interpreter() -> str:
    exe = Path(sys.executable)
    if _system() == "windows" and exe.name.lower() == "python.exe":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return str(exe)


def _active_profile() -> Path | None:
    """The ``--config-dir`` of this process; ``None`` for the default profile.

    ``paths`` keeps the override resolved (see ``paths.set_base_override``).
    """
    return paths.base_override()


def _profile_dir(config_dir: Path | str | None) -> Path | None:
    """The profile an entry is for: ``config_dir`` resolved like ``paths`` does,
    or the running process's profile when ``None``."""
    if config_dir is None:
        return _active_profile()
    return Path(config_dir).expanduser().resolve()


def _command_profile(command: Sequence[str]) -> str | None:
    """The ``--config-dir`` value in a command line, ``None`` for the default profile."""
    args = list(command[1:])
    for index, arg in enumerate(args):
        if arg == CONFIG_DIR_FLAG and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(CONFIG_DIR_FLAG + "="):
            return arg.partition("=")[2]
    return None


def _without_profile(command: Sequence[str]) -> list[str]:
    """The arguments of ``command`` (after the program) other than ``--config-dir``."""
    rest: list[str] = []
    args = iter(command[1:])
    for arg in args:
        if arg == CONFIG_DIR_FLAG:
            next(args, None)
        elif not arg.startswith(CONFIG_DIR_FLAG + "="):
            rest.append(arg)
    return rest


def _path_key(path: str | Path) -> str:
    """Comparable form of a path; case-insensitive for Windows entries."""
    text = os.path.normpath(os.path.expanduser(str(path)))
    return text.casefold() if _system() == "windows" else text


def _same_path(a: str | Path | None, b: str | Path | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return _path_key(a) == _path_key(b)


def _same_command(recorded: Sequence[str], wanted: Sequence[str]) -> bool:
    """Whether two commands start the same program with the same profile and options."""
    return (
        bool(recorded)
        and bool(wanted)
        and _same_path(recorded[0], wanted[0])
        and _same_path(_command_profile(recorded), _command_profile(wanted))
        and _without_profile(recorded) == _without_profile(wanted)
    )


def _program_available(program: str) -> bool:
    """Whether an entry's program will still be there at the next login."""
    if not program or _is_transient_location(program):
        return False
    if _system() == "windows":
        program = os.path.expandvars(program)  # a hand-made REG_EXPAND_SZ entry
    if os.path.isabs(program):
        return os.path.exists(program)
    return shutil.which(program) is not None


def _is_transient_location(program: str) -> bool:
    """Whether ``program`` runs from a place that will be gone at the next login (macOS).

    Gatekeeper's App Translocation runs a quarantined app from a random mount
    under ``/private/var/folders/.../AppTranslocation/``, and an app opened
    straight from its disk image lives on a read-only ``/Volumes/`` mount that
    disappears when the image is ejected. (Apps on an external drive also live
    under ``/Volumes/``, but on a writable volume.)
    """
    if _system() != "macos":
        return False
    if "/AppTranslocation/" in program:
        return True
    return program.startswith("/Volumes/") and _is_read_only_volume(program)


def _is_read_only_volume(path: str) -> bool:
    """Whether ``path`` is on a read-only filesystem (a mounted disk image)."""
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        return False
    try:
        flags = statvfs(path).f_flag
    except OSError:
        return False
    return bool(flags & getattr(os, "ST_RDONLY", 1))


def _read_entry() -> _Entry | None:
    """The login entry of the current OS, ``None`` when there is none."""
    system = _system()
    if system == "windows":
        return autostart_windows._win_entry()
    if system == "macos":
        return autostart_macos._mac_entry()
    if system == "linux":
        return autostart_linux._linux_entry()
    return None

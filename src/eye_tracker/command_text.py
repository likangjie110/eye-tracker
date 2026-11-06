"""Command lines shown to users: the CLI, the tray app and profile arguments.

These helpers are pure naming logic: given the running interpreter or frozen executable, they
produce the command a user should type. They live outside ``cli.py`` so that the platform layer,
the UI, the app and the IPC server can show a command without importing the command-line module
(which imports the app, the diagnostics and the IPC client). That dependency was one of the
module cycles tracked as TD-ARCH-002.

The output is covered by ``tests/test_cli.py`` and is intentional: hints must name a command that
exists for this installation, including a ``--config-dir`` profile.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from . import APP_NAME, APP_SLUG, paths

FROZEN_CLI_NAME = "eye-tracker-cli"

#: Windowed executables next to :data:`FROZEN_CLI_NAME` (Windows, macOS).
_FROZEN_GUI_NAMES = ("EyeTracker", APP_NAME)


def cli_command(*args: str) -> list[str]:
    """The command line that runs this installation's command-line interface.

    Hints such as "bind ``… ctl toggle`` to a desktop shortcut" or "run ``…
    doctor``" must name a command that exists here. Only source installs and
    the Windows installer (its ``eye-tracker.exe``, for terminals opened after
    the installation, unless its PATH option was unticked) put an
    ``eye-tracker`` command on the PATH. The result, followed by ``args``, is

    * inside an AppImage: the ``.AppImage`` file (AppRun passes the arguments
      on; the mounted bundle path changes on every run);
    * the Windows installer's copy, when this process's PATH finds its
      ``eye-tracker.exe``: ``eye-tracker``;
    * other frozen builds: the console executable of this copy
      (``eye-tracker-cli.exe`` next to ``EyeTracker.exe``, ``eye-tracker-cli``
      inside the macOS app, the one executable of the Linux bundle);
    * source and pip installs: ``eye-tracker`` when that command on the PATH is
      this installation's, else its script's full path, else
      ``python -m eye_tracker`` with this interpreter.

    A ``--config-dir`` profile is passed on (``--config-dir DIR`` follows the
    program): each profile is a separate instance with its own command socket,
    so ``ctl`` without it would talk to the default profile.
    """
    return [*_cli_base(), *_profile_args(), *args]


def cli_command_text(*args: str) -> str:
    """:func:`cli_command` as the user would type it (quoted for this OS's shell)."""
    return format_command(cli_command(*args))


def app_command() -> list[str]:
    """The command line that starts the tray app of this installation.

    Like :func:`cli_command`, except that the Windows and macOS builds name
    their windowed executable: a console one (``eye-tracker-cli``, or the
    installer's ``eye-tracker.exe``) would tie the app to the terminal it was
    started from.
    """
    base = _cli_base()
    if paths.is_frozen() and _appimage() is None:
        exe = Path(sys.executable)
        for name in _FROZEN_GUI_NAMES:
            windowed = exe.with_name(name + exe.suffix)
            if windowed.is_file():
                base = [str(windowed)]
                break
    return [*base, *_profile_args()]


def format_command(parts: Sequence[str]) -> str:
    """Render a command as the user would type it on this OS."""
    if os.name == "nt":
        return subprocess.list2cmdline(list(parts))
    return shlex.join(parts)


def program_name() -> str:
    """How ``--help`` names the program: :func:`cli_command` without directories.

    ``eye-tracker`` from source and on the PATH set by the Windows installer,
    ``eye-tracker-cli.exe`` or the ``.AppImage`` file name in the other packages.
    """
    base = _cli_base()
    if len(base) > 1:  # python -m eye_tracker
        return format_command([Path(base[0]).stem, *base[1:]])
    name = Path(base[0]).name
    if not paths.is_frozen() and Path(name).stem.lower() == APP_SLUG:
        return APP_SLUG  # the script, with or without .exe
    return name


def _profile_args() -> list[str]:
    override = paths.base_override()
    return ["--config-dir", str(override)] if override is not None else []


def _cli_base() -> list[str]:
    """The program part of :func:`cli_command` (no profile, no arguments)."""
    if paths.is_frozen():
        appimage = _appimage()
        if appimage is not None:
            return [appimage]
        exe = Path(sys.executable)
        if sys.platform == "win32" and _installer_command_on_path(exe):
            return [APP_SLUG]
        if exe.stem.lower() != FROZEN_CLI_NAME:
            # The windowed executable has no console: its output would be lost.
            console = exe.with_name(FROZEN_CLI_NAME + exe.suffix)
            if console.is_file():
                return [str(console)]
        return [str(exe)]
    return _source_cli_base()


def _appimage() -> str | None:
    """The ``.AppImage`` file this frozen Linux build runs from, if any.

    Only trusted when frozen: ``APPIMAGE`` is inherited by every child of an
    AppImage (a terminal emulator AppImage, for instance).
    """
    appimage = os.environ.get("APPIMAGE", "")
    if sys.platform.startswith("linux") and appimage and os.path.isfile(appimage):
        return appimage
    return None


def _installer_command_on_path(exe: Path) -> bool:
    """Whether ``eye-tracker`` on this process's PATH is the Windows installer's
    ``eye-tracker.exe`` of this very copy (next to ``exe``).

    A process started before the installer changed the PATH (the app launched
    from its last page) does not see the entry yet and keeps the full path.
    """
    found = shutil.which(APP_SLUG)
    return found is not None and _same_file(found, exe.with_name(APP_SLUG + ".exe"))


def _source_cli_base() -> list[str]:
    """The ``eye-tracker`` script of a source or pip install (see :func:`cli_command`)."""
    if not sys.executable:  # an embedded interpreter: nothing better to name
        return [APP_SLUG]
    interpreter = Path(sys.executable)
    # Virtual environments and pip keep console scripts next to the interpreter.
    script = interpreter.with_name(APP_SLUG + (".exe" if sys.platform == "win32" else ""))
    have_script = script.is_file()
    on_path = shutil.which(APP_SLUG)
    if on_path is not None and (not have_script or _same_file(on_path, script)):
        return [APP_SLUG]
    if have_script:
        return [str(script)]
    if sys.platform == "win32" and interpreter.name.lower() == "pythonw.exe":
        # pythonw has no console: the command's output would be lost.
        console = interpreter.with_name("python.exe")
        if console.is_file():
            interpreter = console
    return [str(interpreter), "-m", "eye_tracker"]


def _same_file(a: str | Path, b: str | Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False

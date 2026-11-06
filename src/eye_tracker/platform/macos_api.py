"""macOS bindings and pure helpers for the platform layer (phase 4 / B2).

Constants, the ctypes/pyobjc indirections tests replace, and the pure helpers that turn
CoreGraphics / Accessibility results into plain values. ``macos_platform.py`` holds the policy;
``macos.py`` stays the import point used by the platform factory.

Everything imports on every OS: pyobjc is imported lazily inside the methods that need it.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import math
import os
import platform as _stdlib_platform
import plistlib
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .. import __version__
from ..types import Rect

log = logging.getLogger(__name__)


#: Evaluated once; tests flip it to exercise macOS-only code paths elsewhere.
_IS_MACOS = sys.platform == "darwin"

LOGIN_FRAMEWORK = "/System/Library/PrivateFrameworks/login.framework/Versions/Current/login"
AVFOUNDATION_FRAMEWORK = "/System/Library/Frameworks/AVFoundation.framework"

_PERMISSION_URLS = {
    "accessibility": (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    ),
    "camera": "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
}

# Framework constants, used when a pyobjc build does not export the symbol.
_CG_EVENT_SOURCE_STATE_COMBINED = 0  # kCGEventSourceStateCombinedSessionState
_CG_ANY_INPUT_EVENT_TYPE = 0xFFFFFFFF  # kCGAnyInputEventType, i.e. (CGEventType)~0
_CG_EVENT_KEY_DOWN = 10  # kCGEventKeyDown
_CG_WINDOW_LIST_ON_SCREEN_ONLY = 1 << 0
_CG_WINDOW_LIST_EXCLUDE_DESKTOP = 1 << 4
_CG_NULL_WINDOW_ID = 0
_AX_VALUE_CGPOINT = 1  # kAXValueCGPointType
_AX_VALUE_CGSIZE = 2  # kAXValueCGSizeType
_AX_SUCCESS = 0
_AX_ERROR_CANNOT_COMPLETE = -25204  # kAXErrorCannotComplete: the app did not answer in time
_NS_ACTIVATE_IGNORING_OTHER_APPS = 1 << 1
_NS_ACTIVATION_POLICY_ACCESSORY = 1
#: NSActivityUserInitiatedAllowingIdleSystemSleep: no App Nap and no timer
#: coalescing, while the system and the displays may still sleep when idle.
_NS_ACTIVITY_USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP = 0x00EFFFFF
_ACTIVITY_REASON = "Eye tracking follows the camera in real time"
_AV_MEDIA_TYPE_VIDEO = "vide"  # value of AVMediaTypeVideo
_AV_STATUS = {1: False, 2: False, 3: True}  # restricted, denied, authorized (0 = not asked)

#: Default AX messaging timeout is 6 s; a hung app must not freeze our UI thread.
_AX_TIMEOUT_S = 0.5
#: Tighter timeout for the frontmost app's focused window, which is polled.
_AX_APP_TIMEOUT_S = 0.25
#: How long an app that did not answer an AX request is left alone.
_AX_UNRESPONSIVE_S = 5.0
_TRUST_TTL_S = 5.0
_TOOL_TIMEOUT_S = 5.0
#: An AX frame and a window-list frame this close (points) are the same window.
_FRAME_TOLERANCE = 2
#: Remembers which build Accessibility was granted to (see ``accessibility_status``).
_ACCESSIBILITY_STATE_FILE = "macos-accessibility.json"
#: Name suffix of the bundle's command-line executable (``eye-tracker-cli``).
_CLI_SUFFIX = "-cli"

_AX_POINT_RE = re.compile(r"x:\s*(-?[\d.]+)\s+y:\s*(-?[\d.]+)")
_AX_SIZE_RE = re.compile(r"w:\s*(-?[\d.]+)\s+h:\s*(-?[\d.]+)")


# ---------------------------------------------------------------------------
# indirections (tests replace these)
# ---------------------------------------------------------------------------
def _load_library(path: str) -> Any:
    return ctypes.CDLL(path)


def _tool(name: str) -> str | None:
    """Absolute path of a system tool (GUI apps may start with a minimal PATH)."""
    found = shutil.which(name)
    if found:
        return found
    for directory in ("/usr/bin", "/usr/sbin", "/bin"):
        candidate = f"{directory}/{name}"
        if os.path.exists(candidate):
            return candidate
    return None


def _run_ok(argv: Sequence[str], timeout: float = _TOOL_TIMEOUT_S) -> bool:
    exe = _tool(argv[0])
    if exe is None:
        return False
    try:
        result = subprocess.run(
            [exe, *argv[1:]],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("%s failed: %s", argv[0], exc)
        return False
    if result.returncode != 0:
        log.debug("%s exited with %s", argv[0], result.returncode)
    return result.returncode == 0


def _reap(proc: Any) -> None:
    with contextlib.suppress(Exception):
        proc.wait(timeout=30)


def _macos_major_version() -> int:
    """Major macOS version (e.g. 14); 0 when unknown or not on macOS."""
    try:
        return int((_stdlib_platform.mac_ver()[0] or "0").split(".")[0])
    except (ValueError, IndexError):
        return 0


def _bundle_main_executable(executable: Path) -> Path | None:
    """The main executable of the app bundle ``executable`` belongs to.

    The bundle holds two programs side by side in ``<App>.app/Contents/MacOS``
    (see ``eye-tracker.spec``): the app itself, named by ``CFBundleExecutable``
    in ``Contents/Info.plist``, and the ``eye-tracker-cli`` command. ``None``
    when ``executable`` is not inside a bundle or the plist cannot be read.
    """
    macos_dir = executable.parent
    contents = macos_dir.parent
    if macos_dir.name != "MacOS" or contents.name != "Contents":
        return None
    try:
        with open(contents / "Info.plist", "rb") as fh:
            info = plistlib.load(fh)
    except Exception as exc:  # OSError, InvalidFileException, an XML parser error ...
        log.debug("Cannot read the bundle's Info.plist: %s", exc)
        return None
    name = info.get("CFBundleExecutable") if isinstance(info, dict) else None
    if not isinstance(name, str) or not name or Path(name).name != name:
        return None
    return macos_dir / name


def _packaged_executables() -> tuple[Path, Path] | None:
    """``(this process's executable, the app's main executable)``; ``None`` from source."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        executable = Path(sys.executable).resolve()
    except OSError:
        return None
    return executable, _bundle_main_executable(executable) or executable


def _build_fingerprint() -> str | None:
    """Identity of this packaged build; ``None`` when running from source.

    An ad-hoc signed app's Accessibility grant is tied to its code hash, which
    changes with every build - and so do the version, size and modification
    time of the bundle's main executable. That is the app's executable even
    in ``eye-tracker-cli``: both programs must name the same build.
    """
    executables = _packaged_executables()
    if executables is None:
        return None
    try:
        stat = executables[1].stat()
    except OSError:
        return None
    return f"{__version__}:{stat.st_size}:{stat.st_mtime_ns}"


def _runs_helper_executable() -> bool:
    """Whether this process is a packaged build's ``eye-tracker-cli``, not the app.

    macOS attributes the privacy permissions of such a process to whatever
    started it (the terminal, for a command typed there), so
    ``AXIsProcessTrusted()`` says nothing about the app's own grant.
    """
    executables = _packaged_executables()
    if executables is None:
        return False
    executable, main = executables
    return executable != main or executable.name.endswith(_CLI_SUFFIX)


# ---------------------------------------------------------------------------
# pure helpers (unit-tested with fakes on any OS)
# ---------------------------------------------------------------------------
def _err_value(result: Any) -> tuple[int, Any]:
    """Split pyobjc's ``(error, value)`` result of an out-parameter call."""
    if isinstance(result, tuple) and len(result) == 2:
        err, value = result
        try:
            return int(err or 0), value
        except (TypeError, ValueError):
            return -1, None
    return _AX_SUCCESS, result


def _split_handle(handle: Any) -> tuple[int | None, Any]:
    if isinstance(handle, tuple) and len(handle) == 2:
        pid, element = handle
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            return pid, element
    return None, None


def _session_locked_from(info: Mapping[str, Any] | None) -> bool | None:
    """Interpret ``CGSessionCopyCurrentDictionary()``."""
    if info is None:
        return None  # no window-server session (e.g. ssh)
    try:
        if bool(info.get("CGSSessionScreenIsLocked", False)):
            return True
        # Fast user switching: another user owns the screen. Treated as locked so
        # the camera is released while nobody can see our session.
        on_console = info.get("kCGSSessionOnConsoleKey")
        return on_console is not None and not bool(on_console)
    except Exception:
        return None


def _pair_from_struct(value: Any, names: tuple[str, str]) -> tuple[float, float] | None:
    """(x, y) or (width, height) from a CGPoint/CGSize-like object or a 2-sequence."""
    try:
        if hasattr(value, names[0]) and hasattr(value, names[1]):
            return float(getattr(value, names[0])), float(getattr(value, names[1]))
        if isinstance(value, Sequence) and not isinstance(value, str) and len(value) == 2:
            return float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    return None


def _pair_from_repr(text: str, size: bool) -> tuple[float, float] | None:
    """Parse an AXValue description such as ``{value = x:24.0 y:38.0 type = …}``."""
    match = (_AX_SIZE_RE if size else _AX_POINT_RE).search(text)
    if match is None:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except ValueError:
        return None


def _rect_from(origin: tuple[float, float] | None, size: tuple[float, float] | None) -> Rect | None:
    if origin is None or size is None:
        return None
    values = (*origin, *size)
    if not all(math.isfinite(v) for v in values):
        return None
    x, y, w, h = (round(v) for v in values)
    if w <= 0 or h <= 0:
        return None
    return Rect(x, y, w, h)


def _cg_bounds(info: Mapping[str, Any]) -> Rect | None:
    bounds = info.get("kCGWindowBounds")
    if not bounds:
        return None
    try:
        origin = (float(bounds["X"]), float(bounds["Y"]))
        size = (float(bounds["Width"]), float(bounds["Height"]))
    except (KeyError, TypeError, ValueError):
        return None
    return _rect_from(origin, size)


def _cg_user_windows(
    windows: Iterable[Mapping[str, Any]], own_pid: int
) -> Iterator[tuple[int, Rect]]:
    """(pid, bounds) of ordinary app windows, front to back.

    Layer 0 is the normal window level; the menu bar, Dock, status items and our
    own always-on-top helper windows live on other layers or belong to us.
    """
    for info in windows:
        try:
            if int(info.get("kCGWindowLayer", 0)) != 0:
                continue
            if float(info.get("kCGWindowAlpha", 1.0)) <= 0.0:
                continue
            pid = int(info.get("kCGWindowOwnerPID", 0))
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid == own_pid:
            continue
        rect = _cg_bounds(info)
        if rect is None or rect.w < 2 or rect.h < 2:
            continue
        yield pid, rect


def _cg_window_at(
    windows: Iterable[Mapping[str, Any]], x: float, y: float, own_pid: int
) -> tuple[int, Rect] | None:
    for pid, rect in _cg_user_windows(windows, own_pid):
        if rect.contains(x, y):
            return pid, rect
    return None


def _cg_front_rect(windows: Iterable[Mapping[str, Any]], pid: int, own_pid: int) -> Rect | None:
    for owner, rect in _cg_user_windows(windows, own_pid):
        if owner == pid:
            return rect
    return None


def _frames_match(a: Rect, b: Rect, tolerance: int = _FRAME_TOLERANCE) -> bool:
    return (
        abs(a.x - b.x) <= tolerance
        and abs(a.y - b.y) <= tolerance
        and abs(a.w - b.w) <= tolerance
        and abs(a.h - b.h) <= tolerance
    )


def _ax_attr_err(ax: Any, element: Any, name: str) -> tuple[int, Any]:
    """``(AXError, value)`` of an attribute; the value is ``None`` on error."""
    err, value = _err_value(ax.AXUIElementCopyAttributeValue(element, name, None))
    return err, (value if err == _AX_SUCCESS else None)


def _ax_attr(ax: Any, element: Any, name: str) -> Any:
    return _ax_attr_err(ax, element, name)[1]


def _ax_set(ax: Any, element: Any, name: str, value: Any) -> bool:
    try:
        return int(ax.AXUIElementSetAttributeValue(element, name, value) or 0) == _AX_SUCCESS
    except Exception as exc:
        log.debug("AX set %s failed: %s", name, exc)
        return False


def _ax_perform(ax: Any, element: Any, action: str) -> bool:
    try:
        return int(ax.AXUIElementPerformAction(element, action) or 0) == _AX_SUCCESS
    except Exception as exc:
        log.debug("AX action %s failed: %s", action, exc)
        return False


def _ax_pair(ax: Any, value: Any, size: bool) -> tuple[float, float] | None:
    """Unpack an AXValue holding a CGPoint (``size=False``) or a CGSize."""
    if value is None:
        return None
    names = ("width", "height") if size else ("x", "y")
    value_type = (
        getattr(ax, "kAXValueCGSizeType", _AX_VALUE_CGSIZE)
        if size
        else getattr(ax, "kAXValueCGPointType", _AX_VALUE_CGPOINT)
    )
    try:
        ok, struct = _ok_value(ax.AXValueGetValue(value, value_type, None))
        if ok and struct is not None:
            pair = _pair_from_struct(struct, names)
            if pair is not None:
                return pair
    except Exception as exc:
        log.debug("AXValueGetValue failed: %s", exc)
    # Some pyobjc versions bridge AXValue objects already, or cannot unpack them;
    # their description always carries the numbers.
    return _pair_from_struct(value, names) or _pair_from_repr(str(value), size)


def _ok_value(result: Any) -> tuple[bool, Any]:
    if isinstance(result, tuple) and len(result) == 2:
        return bool(result[0]), result[1]
    return True, result


def _ax_rect_checked(ax: Any, element: Any) -> tuple[int, Rect | None]:
    """``(AXError, frame)`` of a window; the error lets callers spot hung apps."""
    if ax is None or element is None:
        return _AX_SUCCESS, None
    try:
        err, position = _ax_attr_err(ax, element, "AXPosition")
        if err != _AX_SUCCESS:
            # Closed, or its app is hung: asking for AXSize would wait all over again.
            return err, None
        err, size = _ax_attr_err(ax, element, "AXSize")
        if err != _AX_SUCCESS:
            return err, None
        origin = _ax_pair(ax, position, size=False)
        dimensions = _ax_pair(ax, size, size=True)
    except Exception as exc:
        log.debug("AX geometry failed: %s", exc)
        return _AX_SUCCESS, None
    return _AX_SUCCESS, _rect_from(origin, dimensions)


def _ax_rect(ax: Any, element: Any) -> Rect | None:
    return _ax_rect_checked(ax, element)[1]


def _ax_window_of(ax: Any, element: Any) -> Any:
    """The window that contains an arbitrary UI element."""
    if _ax_attr(ax, element, "AXRole") == "AXWindow":
        return element
    window = _ax_attr(ax, element, "AXWindow")
    if window is not None:
        return window
    current = element
    for _ in range(32):  # bounded: a broken hierarchy must not loop forever
        current = _ax_attr(ax, current, "AXParent")
        if current is None:
            return None
        role = _ax_attr(ax, current, "AXRole")
        if role == "AXWindow":
            return current
        if role == "AXApplication":
            return None
    return None


# ---------------------------------------------------------------------------
# platform
# ---------------------------------------------------------------------------

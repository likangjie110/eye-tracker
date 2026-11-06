"""The macOS platform policy (phase 4 / B2).

``MacPlatform``: focus, cursor, monitors, lock/sleep, permissions and the camera-in-use checks.
The ctypes/pyobjc bindings and pure helpers live in ``macos_api.py``; every public method degrades
to ``None``/``False`` instead of raising when a framework symbol is missing.
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import json
import logging
import math
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from ..types import AppIdentity, Rect, WindowRef
from .base import PlatformServices
from .macos_api import (
    _ACCESSIBILITY_STATE_FILE,
    _ACTIVITY_REASON,
    _AV_MEDIA_TYPE_VIDEO,
    _AV_STATUS,
    _AX_APP_TIMEOUT_S,
    _AX_ERROR_CANNOT_COMPLETE,
    _AX_SUCCESS,
    _AX_TIMEOUT_S,
    _AX_UNRESPONSIVE_S,
    _CG_ANY_INPUT_EVENT_TYPE,
    _CG_EVENT_KEY_DOWN,
    _CG_EVENT_SOURCE_STATE_COMBINED,
    _CG_NULL_WINDOW_ID,
    _CG_WINDOW_LIST_EXCLUDE_DESKTOP,
    _CG_WINDOW_LIST_ON_SCREEN_ONLY,
    _IS_MACOS,
    _NS_ACTIVATE_IGNORING_OTHER_APPS,
    _NS_ACTIVATION_POLICY_ACCESSORY,
    _NS_ACTIVITY_USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP,
    _PERMISSION_URLS,
    _TRUST_TTL_S,
    AVFOUNDATION_FRAMEWORK,
    LOGIN_FRAMEWORK,
    _ax_attr,
    _ax_attr_err,
    _ax_perform,
    _ax_rect,
    _ax_rect_checked,
    _ax_set,
    _ax_window_of,
    _build_fingerprint,
    _cg_front_rect,
    _cg_user_windows,
    _cg_window_at,
    _err_value,
    _frames_match,
    _load_library,
    _macos_major_version,
    _reap,
    _run_ok,
    _runs_helper_executable,
    _session_locked_from,
    _split_handle,
    _tool,
)

log = logging.getLogger(__name__)


class MacPlatform(PlatformServices):
    """macOS 12+ implementation of :class:`PlatformServices`.

    ``clock`` and ``state_path`` exist for tests; production code uses the
    defaults (``state_path``: the file remembering which build was trusted).
    """

    name: ClassVar[str] = "macos"

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        state_path: Path | None = None,
    ) -> None:
        self._clock = clock
        self._modules: dict[str, Any] = {}
        self._trust_cache: tuple[float, bool] | None = None
        self._ax_ready = False
        self._ax_lock = threading.Lock()
        #: pid -> clock time until which AX requests to that app are skipped.
        self._ax_unresponsive: dict[int, float] = {}
        self._activity: Any = None  # NSProcessInfo activity token (App Nap opt-out)
        self._activity_lock = threading.Lock()
        self._state_path = state_path
        self._trusted_build: str | None = None  # last build known to be trusted (cached)
        self._trusted_build_loaded = False
        self._build_id: str | None = None  # see _fingerprint
        self._fingerprint_known = False
        self._helper: bool | None = None  # see _is_helper
        self._warned_untrusted = False

    def _mod(self, name: str) -> Any:
        """Import a pyobjc module once; ``None`` when unavailable."""
        if name not in self._modules:
            try:
                module: Any = importlib.import_module(name)
            except Exception as exc:
                log.debug("%s unavailable: %s", name, exc)
                module = None
            self._modules.setdefault(name, module)
        return self._modules[name]

    def _ax(self) -> Any:
        """ApplicationServices, with a short global AX messaging timeout set once."""
        ax = self._mod("ApplicationServices")
        if ax is None:
            return None
        with self._ax_lock:
            if not self._ax_ready:
                self._ax_ready = True
                try:
                    # Setting the timeout on the system-wide element applies globally.
                    ax.AXUIElementSetMessagingTimeout(
                        ax.AXUIElementCreateSystemWide(), _AX_TIMEOUT_S
                    )
                except Exception as exc:
                    log.debug("AXUIElementSetMessagingTimeout failed: %s", exc)
        return ax

    def _ax_trusted(self) -> bool:
        now = self._clock()
        cached = self._trust_cache
        if cached is not None and now - cached[0] < _TRUST_TTL_S:
            return cached[1]
        trusted = bool(self._accessibility_permission())
        self._trust_cache = (now, trusted)
        return trusted

    def _ax_responsive(self, pid: int) -> bool:
        """``False`` while an app that recently timed out an AX request is left alone."""
        until = self._ax_unresponsive.get(pid)
        if until is None:
            return True
        if self._clock() >= until:
            self._ax_unresponsive.pop(pid, None)
            return True
        return False

    def _note_ax_error(self, pid: int, err: int) -> None:
        if err == _AX_ERROR_CANNOT_COMPLETE:
            if pid not in self._ax_unresponsive:
                log.debug("App %s does not answer Accessibility requests; skipping it", pid)
            self._ax_unresponsive[pid] = self._clock() + _AX_UNRESPONSIVE_S

    # -------------------------------------------------------------- lifecycle
    def prepare_process(self) -> None:
        """Nothing to do: Qt already uses point coordinates, like Quartz and AX.

        The Dock icon is hidden by ``LSUIElement`` in the app bundle and, when
        running from source, by :meth:`set_accessory_app` after QApplication exists.
        """

    def set_accessory_app(self) -> bool:
        """Run as a menu-bar ("accessory") app: no Dock icon, no app menu, no App Nap.

        A menu-bar app without visible windows is exactly what App Nap throttles,
        so this also calls :meth:`set_background_activity`. Must be called on the
        main thread after the QApplication was created. Returns whether the
        activation policy was set.
        """
        self.set_background_activity(True)
        appkit = self._mod("AppKit")
        if appkit is None:
            return False
        try:
            policy = getattr(
                appkit, "NSApplicationActivationPolicyAccessory", _NS_ACTIVATION_POLICY_ACCESSORY
            )
            return bool(appkit.NSApplication.sharedApplication().setActivationPolicy_(policy))
        except Exception as exc:
            log.debug("setActivationPolicy failed: %s", exc)
            return False

    def set_background_activity(self, active: bool) -> bool:
        """Opt out of App Nap while ``active`` (``True``), or allow it again.

        Without this, macOS naps a window-less menu-bar app: its timers (the
        controller tick, the walk-away countdown) and the camera loop get
        delayed by up to seconds. The activity is "user initiated, allowing idle
        system sleep": the Mac and its displays still sleep when idle. The
        controller may end it while tracking is paused to save power. Returns
        whether the requested state is in effect.
        """
        with self._activity_lock:
            if active == (self._activity is not None):
                return True
            foundation = self._mod("Foundation")
            if foundation is None:
                return False
            try:
                info = foundation.NSProcessInfo.processInfo()
                if active:
                    options = getattr(
                        foundation,
                        "NSActivityUserInitiatedAllowingIdleSystemSleep",
                        _NS_ACTIVITY_USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP,
                    )
                    # The token must stay referenced: the activity ends with it.
                    self._activity = info.beginActivityWithOptions_reason_(
                        options, _ACTIVITY_REASON
                    )
                    log.debug("App Nap disabled")
                    return self._activity is not None
                info.endActivity_(self._activity)
                self._activity = None
                log.debug("App Nap allowed again")
                return True
            except Exception as exc:
                log.debug("NSProcessInfo activity change failed: %s", exc)
                return False

    def capabilities(self) -> dict[str, bool]:
        quartz = self._mod("Quartz") is not None
        focus = self._mod("AppKit") is not None and self._mod("ApplicationServices") is not None
        return {
            "lock": _IS_MACOS,
            "display_off": _IS_MACOS and _tool("pmset") is not None,
            "wake_display": _IS_MACOS and _tool("caffeinate") is not None,
            "input_idle": quartz,
            "key_idle": quartz,
            "session_locked": quartz,
            "focus": focus,
            "cursor": True,
            "camera_in_use": False,
            "hotkeys": _IS_MACOS,
            "panes": focus,
        }

    # ---------------------------------------------------------- session/power
    def lock_screen(self) -> bool:
        """Lock immediately (like Ctrl+Cmd+Q); fall back to display sleep."""
        if not _IS_MACOS:
            return False
        if self._sac_lock():
            log.info("Screen locked")
            return True
        if _run_ok(["pmset", "displaysleepnow"]):
            log.info(
                "Screen lock API unavailable; displays put to sleep instead (this locks "
                "when 'Require password immediately after sleep' is enabled)"
            )
            return True
        log.warning("Could not lock the screen")
        return False

    @staticmethod
    def _sac_lock() -> bool:
        """``SACLockScreenImmediate`` from the private login framework."""
        try:
            lib = _load_library(LOGIN_FRAMEWORK)
            func = lib.SACLockScreenImmediate
            func.restype = ctypes.c_int
            func.argtypes = []
            status = int(func())
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            log.debug("SACLockScreenImmediate unavailable: %s", exc)
            return False
        if status != 0:
            log.debug("SACLockScreenImmediate returned %s", status)
        return status == 0

    def display_off(self) -> bool:
        return _IS_MACOS and _run_ok(["pmset", "displaysleepnow"])

    def wake_display(self) -> bool:
        """Declare user activity for 2 s (``caffeinate -u``), which wakes the displays."""
        if not _IS_MACOS:
            return False
        exe = _tool("caffeinate")
        if exe is None:
            return False
        try:
            proc = subprocess.Popen(
                [exe, "-u", "-t", "2"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            log.debug("caffeinate failed: %s", exc)
            return False
        # Non-blocking for the caller, but the child must still be reaped.
        threading.Thread(target=_reap, args=(proc,), name="caffeinate-reaper", daemon=True).start()
        return True

    def is_session_locked(self) -> bool | None:
        quartz = self._mod("Quartz")
        if quartz is None:
            return None
        try:
            info = quartz.CGSessionCopyCurrentDictionary()
        except Exception as exc:
            log.debug("CGSessionCopyCurrentDictionary failed: %s", exc)
            return None
        return _session_locked_from(info)

    # ------------------------------------------------------------------ input
    def seconds_since_input(self) -> float | None:
        return self._seconds_since("kCGAnyInputEventType", _CG_ANY_INPUT_EVENT_TYPE)

    def seconds_since_key_input(self) -> float | None:
        """Time since the last key press; only a timestamp is read, never the key."""
        return self._seconds_since("kCGEventKeyDown", _CG_EVENT_KEY_DOWN)

    def _seconds_since(self, event_name: str, event_default: int) -> float | None:
        quartz = self._mod("Quartz")
        if quartz is None:
            return None
        try:
            state = getattr(
                quartz, "kCGEventSourceStateCombinedSessionState", _CG_EVENT_SOURCE_STATE_COMBINED
            )
            event_type = getattr(quartz, event_name, event_default)
            value = float(quartz.CGEventSourceSecondsSinceLastEventType(state, event_type))
        except Exception as exc:
            log.debug("CGEventSourceSecondsSinceLastEventType failed: %s", exc)
            return None
        return value if math.isfinite(value) and value >= 0.0 else None

    def cursor_position_reliable(self) -> bool:
        """Quartz reports the pointer everywhere, over every app's windows."""
        return True

    def move_cursor(self, x: int, y: int) -> bool | None:
        quartz = self._mod("Quartz")
        if quartz is None:
            return None  # let the caller fall back to QCursor
        try:
            err = quartz.CGWarpMouseCursorPosition((float(x), float(y)))
            # A warp suppresses local mouse events for ~0.25 s; re-associating the
            # mouse ends that, so the user can take over immediately.
            quartz.CGAssociateMouseAndMouseCursorPosition(True)
        except Exception as exc:
            log.debug("CGWarpMouseCursorPosition failed: %s", exc)
            return None
        return int(err or 0) == 0

    # ---------------------------------------------------------------- windows
    def foreground_window(self) -> WindowRef | None:
        appkit = self._mod("AppKit")
        if appkit is None:
            return None
        try:
            app = appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
            pid = int(app.processIdentifier()) if app is not None else 0
        except Exception as exc:
            log.debug("frontmostApplication failed: %s", exc)
            return None
        if pid <= 0 or pid == os.getpid():
            return None
        window = None
        if self._ax_trusted() and self._ax_responsive(pid):
            window = self._focused_ax_window(pid)
        rect = None
        if window is not None:
            err, rect = _ax_rect_checked(self._ax(), window)
            self._note_ax_error(pid, err)
        if rect is None:
            rect = self._cg_front_rect(pid)
        return WindowRef(handle=(pid, window), pid=pid, rect=rect)

    def _focused_ax_window(self, pid: int) -> Any:
        ax = self._ax()
        if ax is None:
            return None
        try:
            app = ax.AXUIElementCreateApplication(pid)
            # Polled twice a second: a busy app must not hold the UI thread for
            # the full global timeout on every poll.
            with contextlib.suppress(Exception):
                ax.AXUIElementSetMessagingTimeout(app, _AX_APP_TIMEOUT_S)
            err, window = _ax_attr_err(ax, app, "AXFocusedWindow")
        except Exception as exc:
            log.debug("AXFocusedWindow failed: %s", exc)
            return None
        self._note_ax_error(pid, err)
        return window

    def window_at(self, x: int, y: int) -> WindowRef | None:
        """Frontmost ordinary window under a point (excluding our own).

        The on-screen window list is consulted first: it needs no permission,
        skips our click-through overlay and cannot hang on a busy app. The AX
        element is then looked up for window-level focus when permitted.
        """
        windows = self._cg_windows()
        if windows is None:
            return self._ax_window_at(x, y)
        hit = _cg_window_at(windows, x, y, os.getpid())
        if hit is None:
            return None
        pid, rect = hit
        window = None
        if self._ax_trusted() and self._ax_responsive(pid):
            window = self._ax_window_containing(pid, x, y, rect)
        if window is not None:
            rect = _ax_rect(self._ax(), window) or rect
        return WindowRef(handle=(pid, window), pid=pid, rect=rect)

    def _cg_windows(self) -> list[Mapping[str, Any]] | None:
        """On-screen windows, front to back; ``None`` when the list is unavailable."""
        quartz = self._mod("Quartz")
        if quartz is None:
            return None
        try:
            options = getattr(
                quartz, "kCGWindowListOptionOnScreenOnly", _CG_WINDOW_LIST_ON_SCREEN_ONLY
            ) | getattr(
                quartz, "kCGWindowListExcludeDesktopElements", _CG_WINDOW_LIST_EXCLUDE_DESKTOP
            )
            info = quartz.CGWindowListCopyWindowInfo(
                options, getattr(quartz, "kCGNullWindowID", _CG_NULL_WINDOW_ID)
            )
        except Exception as exc:
            log.debug("CGWindowListCopyWindowInfo failed: %s", exc)
            return None
        return None if info is None else list(info)

    def _cg_front_rect(self, pid: int) -> Rect | None:
        windows = self._cg_windows()
        return _cg_front_rect(windows, pid, os.getpid()) if windows else None

    def _ax_window_containing(self, pid: int, x: int, y: int, hint: Rect) -> Any:
        """The app's AX window under the point, preferring the one matching ``hint``."""
        ax = self._ax()
        if ax is None:
            return None
        try:
            err, windows = _ax_attr_err(ax, ax.AXUIElementCreateApplication(pid), "AXWindows")
            self._note_ax_error(pid, err)
            windows = windows or []
            fallback = None
            for window in windows:  # AXWindows is ordered front to back
                if _ax_attr(ax, window, "AXMinimized"):
                    continue
                rect = _ax_rect(ax, window)
                if rect is None or not rect.contains(x, y):
                    continue
                if rect == hint:
                    return window
                if fallback is None:
                    fallback = window
            return fallback
        except Exception as exc:
            log.debug("AXWindows lookup failed: %s", exc)
            return None

    def _ax_window_at(self, x: int, y: int) -> WindowRef | None:
        """Hit-test through Accessibility (used when the window list is unavailable)."""
        ax = self._ax()
        if ax is None or not self._ax_trusted():
            return None
        try:
            system = ax.AXUIElementCreateSystemWide()
            err, element = _err_value(
                ax.AXUIElementCopyElementAtPosition(system, float(x), float(y), None)
            )
            if err != _AX_SUCCESS or element is None:
                return None
            window = _ax_window_of(ax, element)
            if window is None:
                return None
            err, pid = _err_value(ax.AXUIElementGetPid(window, None))
        except Exception as exc:
            log.debug("AX hit test failed: %s", exc)
            return None
        if err != _AX_SUCCESS or not pid or int(pid) == os.getpid():
            return None
        return WindowRef(handle=(int(pid), window), pid=int(pid), rect=_ax_rect(ax, window))

    def activate_window(self, ref: WindowRef) -> bool:
        """Bring the app forward and raise the window (no synthetic click).

        ``activateWithOptions_`` alone is no longer enough on macOS 14+
        (cooperative activation ignores requests from inactive apps while still
        reporting success), so the Accessibility route (``AXFrontmost`` +
        ``AXRaise``) is used when permitted. Without Accessibility on macOS 14+
        the request is still made but reported as failed: it has no effect for
        a menu-bar app.
        """
        pid, window = _split_handle(ref.handle)
        if pid is None:
            return False
        trusted = self._ax_trusted()
        if not trusted:
            self._warn_untrusted_once()
        # Every AX call to a hung app would block for the full timeout.
        ax = self._ax() if trusted and self._ax_responsive(pid) else None
        if ax is not None and window is not None:
            try:
                err, minimized = _ax_attr_err(ax, window, "AXMinimized")
            except Exception:
                err, minimized = _AX_SUCCESS, False
            self._note_ax_error(pid, err)
            if minimized:
                return False  # never un-minimise behind the user's back
            if err == _AX_ERROR_CANNOT_COMPLETE:
                ax = None
            else:
                _ax_set(ax, window, "AXMain", True)
        activated = False
        appkit = self._mod("AppKit")
        if appkit is not None:
            try:
                app = appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if app is not None and not app.isTerminated():
                    options = getattr(
                        appkit,
                        "NSApplicationActivateIgnoringOtherApps",
                        _NS_ACTIVATE_IGNORING_OTHER_APPS,
                    )
                    activated = bool(app.activateWithOptions_(options))
            except Exception as exc:
                log.debug("activateWithOptions failed: %s", exc)
        if ax is not None:
            try:
                app_element = ax.AXUIElementCreateApplication(pid)
            except Exception as exc:
                log.debug("AXUIElementCreateApplication failed: %s", exc)
            else:
                activated = _ax_set(ax, app_element, "AXFrontmost", True) or activated
            if window is not None:
                _ax_perform(ax, window, "AXRaise")
        if not trusted and _macos_major_version() >= 14:
            return False
        return activated

    def is_window_valid(self, ref: WindowRef) -> bool:
        """Running, not hidden or minimised, and on a Space that is shown right now.

        Accessibility keeps answering for windows on other Spaces, other Stage
        Manager stages or behind another app's full-screen Space, with frames
        inside the same display. Activating such a window would make macOS
        switch that display back to its Space, so the on-screen window list
        decides. Windows of an app that does not answer AX requests are not
        valid targets either, for now.
        """
        pid, window = _split_handle(ref.handle)
        appkit = self._mod("AppKit")
        if pid is None or appkit is None:
            return False
        try:
            app = appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is None or app.isTerminated() or app.isHidden():
                return False
        except Exception as exc:
            log.debug("NSRunningApplication lookup failed: %s", exc)
            return False
        if window is None:
            return self._on_screen(pid, None)
        if not self._ax_responsive(pid):
            return False
        ax = self._ax()
        if ax is None:
            return False
        try:
            err, minimized = _ax_attr_err(ax, window, "AXMinimized")
        except Exception:
            return False
        self._note_ax_error(pid, err)
        if minimized or err == _AX_ERROR_CANNOT_COMPLETE:
            return False
        # A closed window's element answers every query with an error.
        err, rect = _ax_rect_checked(ax, window)
        self._note_ax_error(pid, err)
        if rect is None:
            return False
        return self._on_screen(pid, rect)

    def _on_screen(self, pid: int, frame: Rect | None) -> bool:
        """Whether ``pid`` shows a window (with about this frame) on a visible Space."""
        windows = self._cg_windows()
        if windows is None:
            return True  # cannot tell: keep the previous behaviour
        return any(
            owner == pid and (frame is None or _frames_match(bounds, frame))
            for owner, bounds in _cg_user_windows(windows, os.getpid())
        )

    def window_rect(self, ref: WindowRef) -> Rect | None:
        pid, window = _split_handle(ref.handle)
        if pid is None:
            return None
        if window is not None and self._ax_responsive(pid):
            err, rect = _ax_rect_checked(self._ax(), window)
            self._note_ax_error(pid, err)
            if err != _AX_ERROR_CANNOT_COMPLETE:
                return rect
        # Application-level handle, or a hung app: the window list still knows
        # where its front window is.
        return self._cg_front_rect(pid)

    def window_client_rect(self, ref: WindowRef) -> Rect | None:
        """The window frame: Accessibility has no portable content-area attribute,
        and the title bar's height depends on the app (none in full screen)."""
        return self.window_rect(ref)

    def window_app(self, ref: WindowRef) -> AppIdentity | None:
        pid, _window = _split_handle(ref.handle)
        pid = pid if pid is not None else ref.pid
        if pid is None or pid <= 0:
            return None
        name = self._app_process_name(pid)
        if name is None:
            return None
        bundle = ""
        appkit = self._mod("AppKit")
        if appkit is not None:
            try:
                app = appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                bundle = str(app.bundleIdentifier() or "") if app is not None else ""
            except Exception as exc:
                log.debug("bundleIdentifier failed: %s", exc)
        return AppIdentity(process=name, app_id=bundle)

    def same_window(self, a: WindowRef | None, b: WindowRef | None) -> bool:
        if a is None or b is None:
            return False
        pid_a, win_a = _split_handle(a.handle)
        pid_b, win_b = _split_handle(b.handle)
        if pid_a is None or pid_a != pid_b:
            return False
        if win_a is None and win_b is None:
            return True  # application-level handles (no Accessibility permission)
        if win_a is None or win_b is None:
            return False
        try:
            cf = self._mod("CoreFoundation")
            if cf is not None:
                return bool(cf.CFEqual(win_a, win_b))
            return bool(win_a == win_b)
        except Exception:
            return False

    # ------------------------------------------------------------ permissions
    def permissions(self) -> dict[str, bool | None]:
        """Camera and Accessibility permission (``None``: unknown or not decided).

        In ``eye-tracker-cli`` Accessibility is ``None`` whatever macOS says: it
        judged whatever started the command (see :meth:`_is_helper`), usually the
        terminal, not the app, so ``doctor`` must neither call the app's grant
        missing nor report a trusted terminal's grant as the app's.
        """
        accessibility = None if self._is_helper() else self._accessibility_permission()
        return {"camera": self._camera_permission(), "accessibility": accessibility}

    def accessibility_status(self) -> str:
        """Accessibility permission, in more detail than :meth:`permissions`.

        * ``"granted"``: keyboard focus can follow the gaze.
        * ``"missing"``: never granted (or revoked); ``request_permission`` asks.
        * ``"stale"``: not trusted, but a *previous build* of the app was.
          Release builds are signed ad hoc, so each update has a new code
          identity; System Settings keeps showing the old entry as enabled while
          it no longer applies. The fix: remove Eye Tracker from Privacy &
          Security › Accessibility with "−", then add the app again.
        * ``"unknown"``: the Accessibility API is unavailable, or this is
          ``eye-tracker-cli``, whose answer from macOS belongs to whatever
          started it (usually the terminal), not to the app; see :meth:`_is_helper`.

        Only packaged builds can be told apart this way; a source checkout
        (whose permission belongs to the terminal or Python) reports "missing".
        """
        if self._is_helper():
            return "unknown"
        trusted = self._accessibility_permission()
        if trusted is None:
            return "unknown"
        self._trust_cache = (self._clock(), trusted)
        if trusted:
            return "granted"
        current = self._fingerprint()
        if current is None:
            return "missing"
        previous = self._load_trusted_build()
        return "stale" if previous is not None and previous != current else "missing"

    def _fingerprint(self) -> str | None:
        """This build's :func:`_build_fingerprint`, computed once per process."""
        if not self._fingerprint_known:
            self._fingerprint_known = True
            self._build_id = _build_fingerprint()
        return self._build_id

    def _is_helper(self) -> bool:
        """Whether this process is ``eye-tracker-cli`` (cached :func:`_runs_helper_executable`).

        macOS attributes its Accessibility trust to the process that started
        it - usually the terminal - so a trusted terminal must not be recorded
        as a trusted app build, and an untrusted one says nothing about the app.
        """
        if self._helper is None:
            self._helper = _runs_helper_executable()
        return self._helper

    def _accessibility_permission(self) -> bool | None:
        """Whether *this process* may use the Accessibility API (``AXIsProcessTrusted``)."""
        ax = self._ax()
        if ax is None:
            return None
        try:
            trusted = bool(ax.AXIsProcessTrusted())
        except Exception as exc:
            log.debug("AXIsProcessTrusted failed: %s", exc)
            return None
        if trusted and not self._is_helper():
            self._remember_trusted_build()
        return trusted

    def _warn_untrusted_once(self) -> None:
        if self._warned_untrusted:
            return
        self._warned_untrusted = True
        if self.accessibility_status() == "stale":
            log.warning(
                "Accessibility access was granted to an earlier version of Eye Tracker and "
                "does not apply to this one: keyboard focus cannot follow your gaze. In System "
                "Settings › Privacy & Security › Accessibility, remove Eye Tracker with '−' "
                "and add it again."
            )
        else:
            log.warning(
                "Accessibility permission is missing: keyboard focus cannot follow your gaze "
                "(System Settings › Privacy & Security › Accessibility)."
            )

    def _state_file(self) -> Path:
        if self._state_path is None:
            from .. import paths

            self._state_path = paths.data_dir() / _ACCESSIBILITY_STATE_FILE
        return self._state_path

    def _load_trusted_build(self) -> str | None:
        if not self._trusted_build_loaded:
            self._trusted_build_loaded = True
            try:
                data = json.loads(self._state_file().read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.debug("No record of an earlier Accessibility grant: %s", exc)
                data = None
            value = data.get("trusted_build") if isinstance(data, dict) else None
            self._trusted_build = value if isinstance(value, str) else None
        return self._trusted_build

    def _remember_trusted_build(self) -> None:
        """Record this build as trusted, so a later update can recognise a stale grant."""
        current = self._fingerprint()
        if current is None or self._load_trusted_build() == current:
            return
        path = self._state_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".tmp")
            partial.write_text(json.dumps({"trusted_build": current}), encoding="utf-8")
            os.replace(partial, path)
        except OSError as exc:
            log.debug("Could not record the Accessibility grant: %s", exc)
            return
        self._trusted_build = current

    def _capture_device(self) -> tuple[Any, Any]:
        """``(AVCaptureDevice class, AVMediaTypeVideo)`` or ``(None, None)``.

        The AVFoundation pyobjc wrapper is optional; without it the class is
        looked up in the Objective-C runtime after loading the framework.
        """
        av = self._mod("AVFoundation")
        if av is not None:
            return av.AVCaptureDevice, getattr(av, "AVMediaTypeVideo", _AV_MEDIA_TYPE_VIDEO)
        objc = self._mod("objc")
        if objc is None:
            return None, None
        try:
            objc.loadBundle(
                "AVFoundation", {}, bundle_path=AVFOUNDATION_FRAMEWORK, scan_classes=False
            )
            return objc.lookUpClass("AVCaptureDevice"), _AV_MEDIA_TYPE_VIDEO
        except Exception as exc:
            log.debug("AVCaptureDevice unavailable: %s", exc)
            return None, None

    def _camera_permission(self) -> bool | None:
        device, media_type = self._capture_device()
        if device is None:
            return None
        try:
            status = int(device.authorizationStatusForMediaType_(media_type))
        except Exception as exc:
            log.debug("authorizationStatusForMediaType failed: %s", exc)
            return None
        return _AV_STATUS.get(status)

    def request_permission(self, name: str) -> None:
        try:
            if name == "accessibility":
                ax = self._ax()
                if ax is None:
                    return
                key = getattr(ax, "kAXTrustedCheckOptionPrompt", "AXTrustedCheckOptionPrompt")
                ax.AXIsProcessTrustedWithOptions({key: True})
                self._trust_cache = None
            elif name == "camera":
                # Needs block metadata, i.e. the AVFoundation wrapper. Without it the
                # prompt still appears the first time OpenCV opens the camera.
                av = self._mod("AVFoundation")
                if av is not None:
                    av.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                        getattr(av, "AVMediaTypeVideo", _AV_MEDIA_TYPE_VIDEO),
                        lambda granted: log.info("Camera access granted: %s", bool(granted)),
                    )
        except Exception as exc:
            log.debug("request_permission(%s) failed: %s", name, exc)

    def open_permission_settings(self, name: str) -> bool:
        url = _PERMISSION_URLS.get(name)
        if url is None or not _IS_MACOS:
            return False
        return _run_ok(["open", url])

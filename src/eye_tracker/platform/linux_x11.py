"""X11 idle time (libXss) and windows/DPMS (python-xlib).

Split out of ``platform/linux.py`` (phase 4 / B2).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from ..types import Rect, WindowRef
from .linux_common import (
    _DPMS_MODE_OFF,
    _DPMS_MODE_ON,
    _EWMH_SOURCE_PAGER,
    _RETRY_AFTER_FAILURE_S,
    _SKIPPED_WINDOW_TYPES,
    _T,
    _X_ANY_PROPERTY_TYPE,
    _X_CURRENT_TIME,
    _X_IS_VIEWABLE,
    _X_MOTION_NOTIFY,
    _X_SUBSTRUCTURE_NOTIFY_MASK,
    _X_SUBSTRUCTURE_REDIRECT_MASK,
)
from .linux_desktop import (
    _as_xid,
    _is_connection_error,
)

log = logging.getLogger(__name__)


# X11 idle time (libXss via ctypes)
# ---------------------------------------------------------------------------
class _XScreenSaverInfo(ctypes.Structure):
    """``XScreenSaverInfo`` from ``<X11/extensions/scrnsaver.h>``."""

    _fields_ = (
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("til_or_since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),
        ("eventMask", ctypes.c_ulong),
    )


def _load_first(sonames: Iterable[str], short_name: str) -> Any:
    for soname in sonames:
        try:
            return ctypes.CDLL(soname)
        except OSError:
            continue
    found = ctypes.util.find_library(short_name)
    if not found:
        raise OSError(f"lib{short_name} not found")
    return ctypes.CDLL(found)


class _XssIdle:
    """Milliseconds since the last input, from the MIT-SCREEN-SAVER extension.

    One private ``Display`` connection is opened lazily and reused; the lock
    serialises access because Xlib connections are not thread-safe unless
    ``XInitThreads`` ran before anything else touched Xlib.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._handles: tuple[Any, Any, int, int, Any] | None = None
        self._retry_at = 0.0

    def available(self) -> bool:
        with self._lock:
            return self._ensure()

    def idle_ms(self) -> int | None:
        with self._lock:
            if not self._ensure() or self._handles is None:
                return None
            _xlib, xss, display, root, info = self._handles
            try:
                if not xss.XScreenSaverQueryInfo(display, root, info):
                    return None
                return int(info.contents.idle)
            except Exception as exc:
                log.debug("XScreenSaverQueryInfo failed: %s", exc)
                return None

    def _ensure(self) -> bool:
        if self._handles is not None:
            return True
        now = self._clock()
        if now < self._retry_at:
            return False
        self._handles = self._open()
        if self._handles is None:
            self._retry_at = now + _RETRY_AFTER_FAILURE_S
            return False
        return True

    @staticmethod
    def _open() -> tuple[Any, Any, int, int, Any] | None:
        try:
            xlib = _load_first(("libX11.so.6", "libX11.so"), "X11")
            xss = _load_first(("libXss.so.1", "libXss.so"), "Xss")
            xlib.XOpenDisplay.restype = ctypes.c_void_p
            xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            xlib.XDefaultRootWindow.restype = ctypes.c_ulong
            xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            xlib.XCloseDisplay.restype = ctypes.c_int
            xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
            xss.XScreenSaverQueryExtension.restype = ctypes.c_int
            xss.XScreenSaverQueryExtension.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
            ]
            xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XScreenSaverInfo)
            xss.XScreenSaverAllocInfo.argtypes = []
            xss.XScreenSaverQueryInfo.restype = ctypes.c_int
            xss.XScreenSaverQueryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(_XScreenSaverInfo),
            ]
        except (OSError, AttributeError) as exc:
            log.debug("libXss unavailable: %s", exc)
            return None
        display = xlib.XOpenDisplay(None)
        if not display:
            log.debug("XOpenDisplay failed")
            return None
        event_base, error_base = ctypes.c_int(), ctypes.c_int()
        # Querying info on a server without the extension would raise an X error,
        # and Xlib's default error handler terminates the process.
        if not xss.XScreenSaverQueryExtension(
            display, ctypes.byref(event_base), ctypes.byref(error_base)
        ):
            log.debug("X server lacks the MIT-SCREEN-SAVER extension")
            xlib.XCloseDisplay(display)
            return None
        info = xss.XScreenSaverAllocInfo()
        if not info:
            xlib.XCloseDisplay(display)
            return None
        root = int(xlib.XDefaultRootWindow(display))
        return (xlib, xss, display, root, info)


# ---------------------------------------------------------------------------
# X11 windows and DPMS (python-xlib)
# ---------------------------------------------------------------------------
def _default_display_factory() -> Any:
    from Xlib import display as xdisplay

    display = xdisplay.Display()
    # Errors of requests without a reply (e.g. an event sent to a window that
    # just closed) are printed to stderr by python-xlib unless handled.
    display.set_error_handler(_log_x_error)
    return display


def _log_x_error(error: Any, request: Any) -> None:
    log.debug("X11 error: %s", error)


class _EwmhClient:
    """Top-level windows (EWMH hints of the window manager) and DPMS of the X server.

    Uses its own ``Display`` connection (never Qt's) guarded by a lock, so any
    thread may call it. ``display_factory`` exists for tests.
    """

    def __init__(
        self,
        display_factory: Callable[[], Any] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        own_pid: int | None = None,
    ) -> None:
        self._factory = display_factory or _default_display_factory
        self._clock = clock
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._lock = threading.RLock()
        self._display: Any = None
        self._root: Any = None
        self._atoms: dict[str, int] = {}
        self._retry_at = 0.0

    # ------------------------------------------------------------- public API
    def foreground(self) -> WindowRef | None:
        def query() -> WindowRef | None:
            active = self._prop(self._root, "_NET_ACTIVE_WINDOW")
            wid = _as_xid(int(active[0])) if active else None
            if wid is None:
                return None
            win = self._window(wid)
            if not self._eligible(win):
                return None
            pid = self._pid(win)
            if pid == self._own_pid:
                return None
            return WindowRef(handle=wid, pid=pid, rect=self._frame_rect(win))

        return self._call(query, None)

    def window_at(self, x: int, y: int) -> WindowRef | None:
        def query() -> WindowRef | None:
            stack = self._prop(self._root, "_NET_CLIENT_LIST_STACKING")
            if not stack:
                stack = self._prop(self._root, "_NET_CLIENT_LIST") or []
            for raw in reversed(stack):  # the stacking list is bottom-to-top
                wid = _as_xid(int(raw))
                if wid is None:
                    continue
                try:
                    win = self._window(wid)
                    rect = self._frame_rect(win)
                    if rect is None or not rect.contains(x, y) or not self._eligible(win):
                        continue
                    pid = self._pid(win)
                except Exception as exc:
                    if _is_connection_error(exc):
                        raise
                    continue  # the window vanished between listing and querying it
                if pid == self._own_pid:
                    continue  # our overlay / dialogs: look at what is underneath
                return WindowRef(handle=wid, pid=pid, rect=rect)
            return None

        return self._call(query, None)

    def rect(self, wid: int) -> Rect | None:
        return self._call(lambda: self._frame_rect(self._window(wid)), None)

    def client_rect(self, wid: int) -> Rect | None:
        return self._call(lambda: self._client_rect(self._window(wid)), None)

    def app_class(self, wid: int) -> tuple[int | None, str] | None:
        """``(pid, WM_CLASS class)`` of a window; ``None`` when X is unreachable."""

        def query() -> tuple[int | None, str]:
            win = self._window(wid)
            wm_class = win.get_wm_class()
            name = str(wm_class[1]) if wm_class and len(wm_class) > 1 else ""
            return self._pid(win), name

        return self._call(query, None)

    def is_valid(self, wid: int) -> bool:
        return bool(self._call(lambda: self._eligible(self._window(wid)), False))

    def activate(self, wid: int) -> bool:
        def send() -> bool:
            win = self._window(wid)
            event = self._client_message(
                win,
                self._atom("_NET_ACTIVE_WINDOW"),
                [_EWMH_SOURCE_PAGER, _X_CURRENT_TIME, 0, 0, 0],
            )
            self._root.send_event(
                event, event_mask=_X_SUBSTRUCTURE_REDIRECT_MASK | _X_SUBSTRUCTURE_NOTIFY_MASK
            )
            self._display.flush()
            return True

        return bool(self._call(send, False))

    def nudge_pointer(self) -> bool:
        """Wake the displays with a 1 px XTest pointer move there and back."""

        def nudge() -> bool:
            if not self._display.has_extension("XTEST"):
                return False
            from Xlib.ext import xtest

            for dx in (1, -1):
                xtest.fake_input(self._display, _X_MOTION_NOTIFY, detail=1, x=dx, y=0)
            self._display.sync()
            return True

        return bool(self._call(nudge, False))

    def dpms_status(self) -> tuple[bool, bool] | None:
        """``(capable, enabled)`` of the server's DPMS; ``None`` when X is unreachable."""

        def query() -> tuple[bool, bool]:
            if not self._display.has_extension("DPMS"):
                return False, False
            if not bool(getattr(self._display.dpms_capable(), "capable", False)):
                return False, False
            return True, bool(getattr(self._display.dpms_info(), "state", False))

        return self._call(query, None)

    def dpms_monitors_on(self) -> bool | None:
        """Whether the monitors are powered on (DPMS power level "On")."""

        def query() -> bool:
            level = getattr(self._display.dpms_info(), "power_level", _DPMS_MODE_ON)
            return int(level) == _DPMS_MODE_ON

        return self._call(query, None)

    def dpms_force(self, off: bool) -> bool:
        """Force the monitors off or on; DPMS must be enabled for this."""

        def force() -> bool:
            self._display.dpms_force_level(_DPMS_MODE_OFF if off else _DPMS_MODE_ON)
            self._display.sync()
            return True

        return bool(self._call(force, False))

    def dpms_set_enabled(self, enabled: bool) -> bool:
        def apply() -> bool:
            if enabled:
                self._display.dpms_enable()
            else:
                self._display.dpms_disable()
            self._display.sync()
            return True

        return bool(self._call(apply, False))

    # -------------------------------------------------------------- internals
    def _call(self, fn: Callable[[], _T], default: _T) -> _T:
        with self._lock:
            if not self._connect():
                return default
            try:
                return fn()
            except Exception as exc:
                log.debug("X11 request failed: %s", exc)
                if _is_connection_error(exc):
                    self._drop()
                return default

    def _connect(self) -> bool:
        if self._display is not None:
            return True
        now = self._clock()
        if now < self._retry_at:
            return False
        try:
            display = self._factory()
            root = display.screen().root
        except Exception as exc:
            log.debug("Cannot open an X11 connection: %s", exc)
            self._retry_at = now + _RETRY_AFTER_FAILURE_S
            return False
        self._display, self._root = display, root
        self._atoms.clear()
        return True

    def _drop(self) -> None:
        display, self._display, self._root = self._display, None, None
        self._retry_at = self._clock() + 1.0
        try:
            if display is not None:
                display.close()
        except Exception:
            pass

    def _atom(self, name: str) -> int:
        atom = self._atoms.get(name)
        if atom is None:
            atom = int(self._display.intern_atom(name))
            self._atoms[name] = atom
        return atom

    def _window(self, wid: int) -> Any:
        return self._display.create_resource_object("window", int(wid))

    def _prop(self, win: Any, name: str) -> list[int] | None:
        prop = win.get_full_property(self._atom(name), _X_ANY_PROPERTY_TYPE)
        value = getattr(prop, "value", None) if prop is not None else None
        if value is None:
            return None
        try:
            return [int(v) for v in value]
        except (TypeError, ValueError):
            return None

    def _pid(self, win: Any) -> int | None:
        values = self._prop(win, "_NET_WM_PID")
        return int(values[0]) if values else None

    def _eligible(self, win: Any) -> bool:
        """Mapped on the current desktop, not minimised, not part of the shell."""
        # Windows on other workspaces are unmapped by the WM (their client window
        # is then "unviewable"), so this also filters other desktops.
        if getattr(win.get_attributes(), "map_state", None) != _X_IS_VIEWABLE:
            return False
        state = self._prop(win, "_NET_WM_STATE") or []
        if self._atom("_NET_WM_STATE_HIDDEN") in state:
            return False
        types = self._prop(win, "_NET_WM_WINDOW_TYPE") or []
        skipped = {self._atom(name) for name in _SKIPPED_WINDOW_TYPES}
        return not any(t in skipped for t in types)

    def _frame_rect(self, win: Any) -> Rect | None:
        """Visible outer rectangle: client area + WM decorations - CSD shadows."""
        geometry = win.get_geometry()
        # Asking the root window to translate the client's origin yields root
        # (= global) coordinates, whatever the reparenting depth.
        origin = self._root.translate_coords(win, 0, 0)
        x, y = int(origin.x), int(origin.y)
        w, h = int(geometry.width), int(geometry.height)
        frame = self._prop(win, "_NET_FRAME_EXTENTS")  # left, right, top, bottom
        if frame and len(frame) >= 4:
            left, right, top, bottom = frame[:4]
            x, y, w, h = x - left, y - top, w + left + right, h + top + bottom
        shadow = self._prop(win, "_GTK_FRAME_EXTENTS")  # invisible CSD shadow margins
        if shadow and len(shadow) >= 4:
            left, right, top, bottom = shadow[:4]
            x, y, w, h = x + left, y + top, w - left - right, h - top - bottom
        if w <= 0 or h <= 0:
            return None
        return Rect(x, y, w, h)

    def _client_rect(self, win: Any) -> Rect | None:
        """The client window without WM decorations and without CSD shadows."""
        geometry = win.get_geometry()
        origin = self._root.translate_coords(win, 0, 0)
        x, y = int(origin.x), int(origin.y)
        w, h = int(geometry.width), int(geometry.height)
        shadow = self._prop(win, "_GTK_FRAME_EXTENTS")
        if shadow and len(shadow) >= 4:
            left, right, top, bottom = shadow[:4]
            x, y, w, h = x + left, y + top, w - left - right, h - top - bottom
        if w <= 0 or h <= 0:
            return None
        return Rect(x, y, w, h)

    def _client_message(self, win: Any, atom: int, data: list[int]) -> Any:
        from Xlib.protocol import event

        return event.ClientMessage(window=win, client_type=atom, data=(32, data))


# ---------------------------------------------------------------------------

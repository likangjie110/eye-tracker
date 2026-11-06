"""Monitor, screen and cursor half of :mod:`eye_tracker.engine.controller` (TD-CPLX-001).

Monitor layout, Qt screen-change plumbing, the automatic switch itself and the cursor/window
notifications live here as a mixin, so ``controller.py`` keeps the pipeline. The mixin only
calls back into the host controller; it must never import ``controller`` at runtime
(TYPE_CHECKING only).
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import math
from typing import TYPE_CHECKING, Any

from PySide6.QtGui import QGuiApplication

from ..types import Monitor, Rect, TrackingState, WindowRef, monitor_at, nearest_monitor
from .controller_support import CursorLike, _layout_key, _same_monitor
from .window_memory import WindowMemory, choose_cursor_target

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtCore import QTimer

    from ..config import Settings
    from ..gaze.learning import DriftMonitor
    from ..platform.base import PlatformServices
    from .decision import SwitchDecider
    from .input_state import InputTracker

log = logging.getLogger("eye_tracker.engine.controller")

#: How often the session lock state is checked.
LOCK_POLL_S = 2.0

#: Moving the mouse back to the previous monitor this soon after an automatic
#: switch marks that switch as wrong (drift evidence); a switch left alone this
#: long counts as correct.
WRONG_SWITCH_S = 2.0

#: Consecutive refused pointer moves after which switching pauses (backing off
#: from ``WARP_BACKOFF_S``, doubling up to ``WARP_BACKOFF_MAX_S``) instead of
#: retrying after every cooldown.
WARP_REFUSAL_LIMIT = 3

WARP_BACKOFF_S = 60.0

WARP_BACKOFF_MAX_S = 1800.0

#: Consecutive failed window activations after which a missing Accessibility
#: permission (macOS) is reported.
ACTIVATION_FAILURE_LIMIT = 3


#: Screen hot-plug events come in bursts (and displays drop out during sleep);
#: the layout is re-read once they have settled.
SCREEN_DEBOUNCE_MS = 1000


class _ScreensHost:
    """State and callbacks the mixin expects from the host ``Controller``."""

    _settings: Settings
    _platform: PlatformServices
    _monitors: list[Monitor]
    _monitors_provider: Callable[[], list[Monitor]]
    _state: TrackingState
    _started: bool
    _closed: bool
    _decider: SwitchDecider
    _pane_decider: Any
    _pane_sigma_cache: Any
    _min_side: float
    _memory: WindowMemory
    _input: InputTracker
    _drift: DriftMonitor
    _cursor: CursorLike
    _cursor_reliable: bool
    _assumed_monitor: int | None
    _last_switch: tuple[float, int | None, int] | None
    _switch_count: int
    _activation_failures: int
    _warp_refusals: int
    _warp_suspensions: int
    _switching_suspended_until: float
    _cursor_warning_shown: bool
    _next_lock_check: float
    _session_locked: bool
    _screens_connected: bool
    _screen_timer: QTimer
    notify: Any
    switched: Any

    if TYPE_CHECKING:

        def _check_accessibility(self) -> None: ...
        def _check_session_lock(self, now: float) -> None: ...
        def _follow_pane_window(self, ref: WindowRef, now: float) -> None: ...
        def _forget_pane_window(self) -> None: ...
        def _panes_wanted(self) -> bool: ...
        def _reset_tracking(self) -> None: ...
        def _update_state(self) -> None: ...
        def _validate_calibration(self, *, announce: bool) -> None: ...


class ControllerScreensMixin(_ScreensHost):
    """Monitor layout, screen changes and the cursor switch (mixed into ``Controller``)."""

    def monitors(self) -> list[Monitor]:
        """The current monitor layout."""
        if not self._monitors and not self._started:
            return self._read_monitors()
        return list(self._monitors)

    def refresh_monitors(self) -> None:
        """Re-read the monitor layout (called automatically after screen changes)."""
        if self._closed:
            return
        monitors = self._read_monitors()
        if not monitors:
            log.debug("No monitors reported; keeping the previous layout")
            return
        if _layout_key(monitors) == _layout_key(self._monitors):
            self._monitors = monitors  # names or scale factors may have changed
            return
        log.info(
            "Monitor layout changed: %s",
            ", ".join(f"{m.index}:{m.rect.w}x{m.rect.h}@{m.rect.x},{m.rect.y}" for m in monitors),
        )
        self._set_monitors(monitors)
        self._memory.clear()
        self._reset_tracking()
        if self._started:
            self._validate_calibration(announce=True)
            self._update_state()

    def _current_monitor(self, cursor: tuple[int, int] | None) -> int | None:
        """Index of the monitor the pointer is on (``None`` if unknown)."""
        if not self._cursor_reliable:
            return self._assumed_monitor
        monitor = monitor_at(self._monitors, cursor[0], cursor[1]) if cursor else None
        return monitor.index if monitor is not None else None

    def _monitor_index_at(self, x: float, y: float) -> int | None:
        monitor = monitor_at(self._monitors, x, y)
        return monitor.index if monitor is not None else None

    def _switch_to(
        self,
        target: int,
        gaze: tuple[float, float] | None,
        cursor: tuple[int, int] | None,
        current: int | None,
        now: float,
    ) -> None:
        monitor = next((m for m in self._monitors if m.index == target), None)
        if monitor is None:
            return
        sw = self._settings.switching
        if self._cursor_reliable and current is not None and cursor is not None:
            self._memory.record_cursor(current, cursor)

        window = self._remembered_window(monitor) if sw.focus_window else None
        x, y = choose_cursor_target(
            sw.cursor_target,
            monitor,
            self._memory,
            gaze,
            window.rect if window is not None else None,
        )
        # Warp first, then activate: input injected by activation fallbacks must
        # not be mistaken for the user typing (see InputTracker).
        self._input.note_programmatic_move((x, y), now)
        if not self._move_cursor(x, y, now):
            # Nothing moved, so this was no switch: no focus change, no count. The
            # decider armed its cooldown when it fired, so it retries after that.
            return
        if sw.focus_window:
            if window is None:
                found = self._platform_call("window_at", x, y)
                window = found if isinstance(found, WindowRef) else None
            if window is not None:
                self._activate(monitor, window)

        self._decider.notify_switched(now, target)
        self._pane_decider.reset()  # another monitor: another pane context
        self._assumed_monitor = target
        if self._last_switch is not None:
            self._drift.record_switch(True)  # the previous switch was kept until now
        # Without a reliable pointer position an undone switch cannot be seen.
        self._last_switch = (now, current, target) if self._cursor_reliable else None
        self._switch_count += 1
        log.debug("Switched to monitor %d (cursor %d, %d)", target, x, y)
        self.switched.emit(target)

    def _activate(self, monitor: Monitor, window: WindowRef) -> None:
        if self._platform_call("activate_window", window, default=False):
            self._memory.record_window(monitor.index, window)
            self._activation_failures = 0
            return
        log.debug("Could not activate the window on monitor %d", monitor.index)
        self._activation_failures += 1
        if self._activation_failures >= ACTIVATION_FAILURE_LIMIT:
            # Activation also fails for ordinary reasons (Windows' foreground
            # lock, a window that closed); only a missing permission is reported.
            self._activation_failures = 0
            self._check_accessibility()

    def _remembered_window(self, monitor: Monitor) -> WindowRef | None:
        """The last window used on ``monitor`` if it still exists and is still there."""
        ref = self._memory.last_window(monitor.index)
        if ref is None:
            return None
        if not self._platform_call("is_window_valid", ref, default=False):
            self._memory.forget_window(monitor.index)
            return None
        rect = self._platform_call("window_rect", ref)
        if not isinstance(rect, Rect):
            rect = ref.rect
        if rect is None:
            return ref
        if not monitor.rect.contains(*rect.center):
            self._memory.forget_window(monitor.index)  # moved to another monitor
            return None
        return ref if rect == ref.rect else dataclasses.replace(ref, rect=rect)

    def _move_cursor(self, x: int, y: int, now: float) -> bool:
        """Warp the pointer; False if the system refused.

        Refusals in a row pause switching (backing off) instead of retrying after
        every cooldown, and the user is told once.
        """
        try:
            result = self._cursor.set_pos(int(x), int(y))
        except Exception:
            log.warning("Moving the cursor failed", exc_info=True)
            result = False
        if result is None or bool(result):
            self._warp_refusals = 0
            self._warp_suspensions = 0
            self._switching_suspended_until = -math.inf
            return True
        # Windows refuses pointer moves as soon as the secure desktop (lock screen,
        # UAC prompt, Ctrl+Alt+Del) has the input, up to LOCK_POLL_S before the
        # regular poll notices. Ask now: a refusal caused by a lock says nothing
        # about the desktop, so it neither counts towards the backoff nor tells
        # the user that the cursor cannot be moved.
        self._next_lock_check = now + LOCK_POLL_S
        self._check_session_lock(now)
        if self._session_locked:
            log.info("The system refused to move the pointer: the session is locked")
            return False
        self._warp_refusals += 1
        log.info("The system refused to move the pointer (%d in a row)", self._warp_refusals)
        if self._warp_refusals >= WARP_REFUSAL_LIMIT:
            self._warp_suspensions += 1
            delay = min(
                WARP_BACKOFF_MAX_S, WARP_BACKOFF_S * 2 ** min(self._warp_suspensions - 1, 16)
            )
            self._switching_suspended_until = now + delay
            log.warning("Pointer moves keep failing; switching paused for %.0f s", delay)
            if not self._cursor_warning_shown:
                self._cursor_warning_shown = True
                self._notify(
                    "Cannot move the cursor",
                    "The system keeps refusing to move the mouse pointer, so switching "
                    "monitors is paused for now. On Wayland, install ydotool (see the "
                    "documentation).",
                )
        return False

    def _settle_last_switch(self, now: float) -> None:
        """A switch the user left alone for WRONG_SWITCH_S was a correct one.

        Recording those as well keeps the drift statistics honest: with adaptive
        learning off, undone switches would otherwise be the only events.
        """
        last = self._last_switch
        if last is not None and now - last[0] > WRONG_SWITCH_S:
            self._last_switch = None
            self._drift.record_switch(True)

    def _record_foreground_window(self, now: float) -> None:
        if self._state is not TrackingState.TRACKING:
            return
        remember = self._settings.switching.focus_window
        panes = self._panes_wanted()
        if not (remember or panes):
            return
        ref = self._platform_call("foreground_window")
        if not isinstance(ref, WindowRef) or ref.rect is None or not self._monitors:
            if panes:
                self._forget_pane_window()
            return
        if remember:
            cx, cy = ref.rect.center
            monitor = (
                monitor_at(self._monitors, cx, cy) or nearest_monitor(self._monitors, cx, cy)[0]
            )
            self._memory.record_window(monitor.index, ref)
        if panes:
            self._follow_pane_window(ref, now)

    def _connect_screens(self) -> None:
        app = QGuiApplication.instance()
        if not isinstance(app, QGuiApplication) or self._screens_connected:
            return
        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._schedule_monitor_refresh)
        app.primaryScreenChanged.connect(self._schedule_monitor_refresh)
        for screen in QGuiApplication.screens():
            screen.geometryChanged.connect(self._schedule_monitor_refresh)
        self._screens_connected = True

    def _disconnect_screens(self) -> None:
        app = QGuiApplication.instance()
        if not self._screens_connected or not isinstance(app, QGuiApplication):
            return
        self._screens_connected = False
        with contextlib.suppress(RuntimeError, TypeError):
            app.screenAdded.disconnect(self._on_screen_added)
            app.screenRemoved.disconnect(self._schedule_monitor_refresh)
            app.primaryScreenChanged.disconnect(self._schedule_monitor_refresh)

    def _on_screen_added(self, screen: Any) -> None:
        with contextlib.suppress(RuntimeError, AttributeError):
            screen.geometryChanged.connect(self._schedule_monitor_refresh)
        self._schedule_monitor_refresh()

    def _schedule_monitor_refresh(self, *_args: object) -> None:
        if not self._closed:
            self._screen_timer.start()

    def _read_monitors(self) -> list[Monitor]:
        try:
            return list(self._monitors_provider())
        except Exception:
            log.warning("Reading the monitor layout failed", exc_info=True)
            return []

    def _set_monitors(self, monitors: list[Monitor]) -> None:
        before = self._monitors
        assumed = next((m for m in before if m.index == self._assumed_monitor), None)
        self._monitors = list(monitors)
        self._decider.set_monitors(self._monitors)
        self._pane_decider.reset()
        self._pane_sigma_cache = None
        sides = [min(m.rect.w, m.rect.h) for m in self._monitors if m.rect.w > 0 and m.rect.h > 0]
        self._min_side = float(min(sides)) if sides else 1000.0
        if self._cursor_reliable:
            return
        if assumed is not None:
            # Monitors are numbered in the order Qt lists the screens, so removing
            # a screen (or changing the primary) renumbers the others: follow the
            # physical monitor the pointer was moved to, not its old number.
            match = _same_monitor(assumed, before, self._monitors)
            self._assumed_monitor = match.index if match is not None else None
        if not any(m.index == self._assumed_monitor for m in self._monitors):
            # A stale position is still the best guess until the first switch.
            cursor = self._cursor_pos()
            monitor = monitor_at(self._monitors, *cursor) if cursor else None
            self._assumed_monitor = monitor.index if monitor is not None else None

    def _cursor_pos(self) -> tuple[int, int] | None:
        try:
            x, y = self._cursor.pos()
        except Exception:
            log.debug("Reading the cursor position failed", exc_info=True)
            return None
        return (int(x), int(y))

    def _platform_call(self, name: str, *args: Any, default: Any = None) -> Any:
        """Call a platform method; the contract says they never raise, but be safe.

        A method the object does not have (a partial test double) returns
        ``default``.
        """
        method = getattr(self._platform, name, None)
        if method is None:
            return default
        try:
            return method(*args)
        except Exception:
            log.debug("Platform call %s failed", name, exc_info=True)
            return default

    def _notify(self, title: str, message: str, *, force: bool = False) -> None:
        """Emit ``notify``. Informational messages respect the notifications setting;
        ``force`` is for actions the user chose to be notified about."""
        log.info("Notification: %s — %s", title, message)
        if force or self._settings.general.notifications:
            self.notify.emit(title, message)

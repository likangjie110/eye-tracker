"""Tick/render and lifecycle halves of :mod:`eye_tracker.ui.calibration_window` (TD-CPLX-005).

The collector/tick/render plumbing and the surface/window lifecycle of ``CalibrationWindow`` live
here; the result/save/retry half is in :mod:`eye_tracker.ui.calibration_result` and the window keeps
construction, public properties, input dispatch and fitting. The mixins only call back into the
host window; this module must never import ``calibration_window`` at runtime (TYPE_CHECKING only).

The state/timing constants are defined here because every half uses them;
``calibration_window`` imports and re-exports the public ones, so
``eye_tracker.ui.calibration_window.STATE_*`` and friends keep working.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

from ..gaze.calibration import (
    EVENT_FINISHED,
    EVENT_RETRY,
    EVENT_TARGET,
    PHASE_SETTLE,
    CalibrationCollector,
    make_plan,
)
from ..gaze.store import CalibrationData
from .calibration_model import (
    _MUTED,
    _TEXT,
    _WARNING,
    _DotState,
    _ease_in_out,
    _ease_out_back,
    _monitor_labels,
)
from .calibration_surface import _Surface

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QColor, QScreen

    from ..gaze.calibration import CalibrationReport, CalibrationTarget
    from ..gaze.model import GazeModel
    from ..types import Monitor
    from .calibration_window import CalibrationWindow

# The window's logger: the class split must not move the log records.
log = logging.getLogger("eye_tracker.ui.calibration_window")

# States of the calibration window.
STATE_IDLE = "idle"  # constructed, not started
STATE_INTRO = "intro"  # instructions shown, waiting for Space
STATE_RUNNING = "running"  # dots are being shown
STATE_FITTING = "fitting"  # computing the model
STATE_RESULT = "result"  # report shown, waiting for Save / Retry / Cancel
STATE_ERROR = "error"  # not enough data (or another problem); Retry / Cancel
STATE_CLOSED = "closed"  # finished (saved or cancelled)

#: Pause automatically when no face was seen for this long.
NO_FACE_PAUSE_S = 1.5
#: Without any observation for this long the camera is reported as missing.
NO_CAMERA_S = 2.0
#: Close the calibration after this long without a key press while it waits for
#: the user (instructions, result, error, paused with Space). Walk-away locking
#: is suspended while the window is open.
IDLE_TIMEOUT_S = 120.0
#: Close the calibration after the dots were paused this long for lack of a face.
NO_FACE_TIMEOUT_S = 60.0

TICK_MS = 30  # animation / collector tick while running
IDLE_TICK_MS = 250  # intro: only the face indicator needs refreshing
#: The inactivity check also runs on screens that stop the animation timer.
WATCHDOG_MS = 1000

# Share of the settle phase used to glide from the previous dot (same monitor)
# or to pop in (new monitor). Moving targets are easier to follow than jumps.
GLIDE_SHARE = 0.45
POP_SHARE = 0.3


class _CalibrationHost:
    """State and host callbacks the flow mixins use, declared for the type checker.

    ``CalibrationWindow.__init__`` creates the state; the methods listed under
    ``TYPE_CHECKING`` are implemented by the window or by a sibling mixin.
    """

    # State owned (or created) by CalibrationWindow.__init__.
    _state: str
    _controller: object
    _clock: Callable[[], float]
    _points_per_monitor: int
    _timing: tuple[float, float, int, int]
    _margin: float
    _monitors: list[Monitor]
    _labels: dict[int, str]
    _surfaces: list[_Surface]
    _collector: CalibrationCollector | None
    _plan: list[CalibrationTarget]
    _model: GazeModel | None
    _report: CalibrationReport | None
    _data: CalibrationData | None
    _error: str
    _prev_target: CalibrationTarget | None
    _last_target: CalibrationTarget | None
    _retrying: bool
    _manual_pause: bool
    _auto_pause: bool
    _pause_started: float | None
    _paused_total: float
    _last_obs_at: float | None
    _last_face_at: float | None
    _face_grace_until: float
    _connected: bool
    _last_input_at: float
    _no_face_since: float | None
    _watched_screens: list[QScreen]
    _timer: QTimer
    _watchdog: QTimer

    if TYPE_CHECKING:
        # Defined on the QObject window; Any keeps the Signal descriptor out of the
        # non-QObject mixin while still telling the type checker the attribute exists.
        finished: Any

        @property
        def is_active(self) -> bool: ...

        @property
        def paused(self) -> bool: ...

        def begin(self) -> None: ...
        def cancel(self) -> None: ...
        def tick(self) -> None: ...
        def _set_state(self, state: str) -> None: ...
        def _on_observation(self, obs: object) -> None: ...
        def _drop_fit(self) -> None: ...
        def _enter_fitting(self) -> None: ...
        def _tick_fitting(self) -> None: ...
        def _show_intro(self) -> None: ...
        def _refresh_face_chip(self) -> None: ...
        def _finish(self, data: CalibrationData | None) -> None: ...
        def _focus_primary(self) -> None: ...
        def _on_screens_changed(self, *_args: object) -> None: ...
        def _screen_for(self, monitor: Monitor) -> QScreen | None: ...
        def _camera_device(self) -> str: ...
        def _current_monitors(self) -> list[Monitor]: ...
        def _layout_changed(self) -> bool: ...
        def _notify(self, title: str, message: str) -> None: ...


class CalibrationTickMixin(_CalibrationHost):
    """Collector, timer, pause bookkeeping and dot rendering; mixed into ``CalibrationWindow``."""

    def toggle_pause(self) -> None:
        """Pause or resume the dots (Space while running)."""
        if self._state != STATE_RUNNING:
            return
        now = self._clock()
        self._last_input_at = now
        self._manual_pause = not self._manual_pause
        self._update_pause(now)
        self._render()

    def tick(self) -> None:
        """Advance the sequence to the current clock time and redraw.

        Called by a timer every 30 ms while dots are shown; tests call it directly
        after advancing a fake clock.
        """
        if self.check_idle():
            return
        if self._state == STATE_INTRO:
            self._refresh_face_chip()
        elif self._state == STATE_RUNNING:
            self._tick_running()
        elif self._state == STATE_FITTING:
            self._tick_fitting()

    def check_idle(self) -> bool:
        """Close an abandoned calibration. Returns ``True`` if it was closed.

        Runs every :data:`WATCHDOG_MS` (and on every :meth:`tick`). A calibration
        waiting for a key for :data:`IDLE_TIMEOUT_S`, or with its dots paused
        for lack of a face for :data:`NO_FACE_TIMEOUT_S`, is cancelled so that
        walk-away detection and the normal camera rate resume.
        """
        if not self.is_active:
            return False
        now = self._clock()
        waiting = self._state in (STATE_INTRO, STATE_RESULT, STATE_ERROR) or (
            self._state == STATE_RUNNING and self._manual_pause
        )
        if waiting and now - self._last_input_at >= IDLE_TIMEOUT_S:
            reason = f"Nothing was pressed for {IDLE_TIMEOUT_S / 60:g} minutes."
        elif (
            self._state == STATE_RUNNING
            and self._auto_pause
            and self._no_face_since is not None
            and now - self._no_face_since >= NO_FACE_TIMEOUT_S
        ):
            reason = f"No face was seen for {NO_FACE_TIMEOUT_S:g} seconds."
        else:
            return False
        log.info("Calibration closed after inactivity (%s)", self._state)
        self._notify(
            "Calibration closed",
            f"{reason} Choose “Calibrate now…” in the tray menu to start again.",
        )
        self.cancel()
        return True

    def _tick_running(self) -> None:
        collector = self._collector
        if collector is None:
            return
        now = self._clock()
        face_missing = now >= self._face_grace_until and (
            self._last_face_at is None or now - self._last_face_at > NO_FACE_PAUSE_S
        )
        if face_missing != self._auto_pause:
            self._auto_pause = face_missing
            self._no_face_since = now if face_missing else None
            log.debug("Calibration %s", "paused: no face" if face_missing else "resumed")
            self._update_pause(now)
        for event in collector.update(self._collector_time(now)):
            if event == EVENT_TARGET:
                self._prev_target = self._last_target
                self._last_target = collector.current
                self._retrying = False
            elif event == EVENT_RETRY:
                self._retrying = True
            elif event == EVENT_FINISHED:
                self._enter_fitting()
                return
        self._render()

    def _collector_time(self, now: float) -> float:
        """Wall time minus paused time: the collector's clock stands still while paused."""
        paused = self._paused_total
        if self._pause_started is not None:
            paused += now - self._pause_started
        return now - paused

    def _update_pause(self, now: float) -> None:
        if self.paused and self._pause_started is None:
            self._pause_started = now
        elif not self.paused and self._pause_started is not None:
            self._paused_total += now - self._pause_started
            self._pause_started = None

    def _hint(self, now: float) -> tuple[str, QColor]:
        if self._state != STATE_RUNNING:
            return "", _MUTED
        if self._manual_pause:
            return "Paused — press Space to continue", _TEXT
        if self._auto_pause:
            if self._last_obs_at is None or now - self._last_obs_at > NO_CAMERA_S:
                return "Waiting for the camera…", _WARNING
            return "Can't see your face — check the camera", _WARNING
        if self._retrying:
            return "Keep looking at the dot…", _WARNING
        return "", _MUTED

    def _dot_state(self) -> _DotState | None:
        collector = self._collector
        target = collector.current if collector is not None else None
        if collector is None or target is None:
            return None
        t = collector.phase_progress
        x, y, pop = target.x, target.y, 1.0
        ring: float | None = None
        arc: float | None = None
        if collector.phase == PHASE_SETTLE:
            prev = self._prev_target
            if prev is not None and prev.monitor_index == target.monitor_index:
                g = _ease_in_out(t / GLIDE_SHARE)
                x = prev.x + (target.x - prev.x) * g
                y = prev.y + (target.y - prev.y) * g
            else:
                pop = _ease_out_back(t / POP_SHARE)
            ring = t
        else:
            arc = t
        return _DotState(x, y, target.x, target.y, pop, ring, arc, self._retrying, self.paused)

    def _render(self) -> None:
        if self._state != STATE_RUNNING or self._collector is None:
            return
        collector = self._collector
        dot = self._dot_state()
        target = collector.current
        hint, color = self._hint(self._clock())
        total = len(self._plan)
        index = min(collector.current_index + 1, total)
        text = f"Dot {index} of {total}"
        keys = "Space  pause    ·    R  restart    ·    Esc  cancel"
        for surface in self._surfaces:
            active = target is not None and surface.monitor.index == target.monitor_index
            surface.set_running(
                dot,
                active=active,
                progress=collector.progress,
                progress_text=text,
                hint=hint,
                hint_color=color,
                keys_text=keys,
            )


class CalibrationLifecycleMixin(_CalibrationHost):
    """Surface opening/closing and screen watching; mixed into ``CalibrationWindow``."""

    def start(self) -> bool:
        """Open the surfaces on every monitor and show the instructions.

        Returns ``False`` (and emits ``finished(False)``) when there is no monitor.
        Calling it while a calibration is open just raises the windows.
        """
        if self.is_active:
            # Asked for again (tray, hotkey, second launch): the user is here.
            self._last_input_at = self._clock()
            self._focus_primary()
            return True
        monitors = self._current_monitors()
        if not monitors:
            log.warning("Calibration needs at least one monitor")
            self._state = STATE_CLOSED
            self.finished.emit(False)
            return False
        self._monitors = monitors
        self._labels = _monitor_labels(monitors)
        primary = next((m for m in monitors if m.primary), monitors[0])
        ordered = [primary, *(m for m in monitors if m is not primary)]
        owner = cast("CalibrationWindow", self)
        self._surfaces = [_Surface(owner, m, self._screen_for(m)) for m in ordered]

        signal = getattr(self._controller, "observation", None)
        if signal is not None:
            signal.connect(self._on_observation)
            self._connected = True
        self._watch_screens()
        try:
            self._controller.begin_calibration()  # type: ignore[attr-defined]
        except Exception:
            log.exception("controller.begin_calibration failed")

        log.info("Calibration opened on %d monitor(s)", len(monitors))
        self._last_input_at = self._clock()
        self._set_state(STATE_INTRO)
        self._show_intro()
        for surface in self._surfaces:
            surface.present()
        self._focus_primary()
        self._timer.start(IDLE_TICK_MS)
        self._watchdog.start()
        return True

    def show(self) -> bool:
        """Alias of :meth:`start`."""
        return self.start()

    def begin(self) -> None:
        """Start (or restart) showing the dots."""
        if self._state in (STATE_IDLE, STATE_CLOSED):
            return
        # The controller reports a new layout up to a second after Qt does (it
        # debounces hot-plugs), so a change may have slipped past the screen
        # signals: dots on the old geometry could never be saved.
        if self._layout_changed():
            self._on_screens_changed()
            return
        settle_s, collect_s, min_samples, max_retries = self._timing
        self._plan = make_plan(self._monitors, self._points_per_monitor, self._margin)
        self._collector = CalibrationCollector(
            self._plan,
            settle_s=settle_s,
            collect_s=collect_s,
            min_samples=min_samples,
            max_retries=max_retries,
        )
        now = self._clock()
        self._last_input_at = now
        self._no_face_since = None
        self._prev_target = self._last_target = None
        self._retrying = False
        self._manual_pause = self._auto_pause = False
        self._pause_started = None
        self._paused_total = 0.0
        self._face_grace_until = now + NO_FACE_PAUSE_S
        self._model = self._report = None
        self._error = ""
        self._drop_fit()
        self._collector.start(now)
        self._set_state(STATE_RUNNING)
        for surface in self._surfaces:
            surface.setCursor(Qt.CursorShape.BlankCursor)
        self._timer.start(TICK_MS)
        self.tick()

    def _on_screens_changed(self, *_args: object) -> None:
        if not self.is_active:
            return
        log.info("Monitor layout changed; calibration cancelled")
        self._notify("Calibration cancelled", "The monitor layout changed. Please calibrate again.")
        self.cancel()

    def _finish(self, data: CalibrationData | None) -> None:
        if self._state == STATE_CLOSED:
            return
        self._set_state(STATE_CLOSED)
        self._timer.stop()
        self._watchdog.stop()
        self._drop_fit()
        if self._connected:
            with contextlib.suppress(RuntimeError, TypeError, AttributeError):
                self._controller.observation.disconnect(self._on_observation)  # type: ignore[attr-defined]
            self._connected = False
        self._unwatch_screens()
        try:
            self._controller.finish_calibration(data)  # type: ignore[attr-defined]
        except Exception:
            log.exception("controller.finish_calibration failed")
        for surface in self._surfaces:
            surface.closing = True
            surface.close()
            surface.deleteLater()
        self._surfaces = []
        self.finished.emit(data is not None)

    def _watch_screens(self) -> None:
        """Cancel on any display change: added, removed, resized, moved or a new primary."""
        app = QGuiApplication.instance()
        if not isinstance(app, QGuiApplication):
            return
        app.screenAdded.connect(self._on_screens_changed)
        app.screenRemoved.connect(self._on_screens_changed)
        app.primaryScreenChanged.connect(self._on_screens_changed)
        # A screen added later cancels the calibration itself, so the screens
        # present now are all whose geometry matters.
        self._watched_screens = list(QGuiApplication.screens())
        for screen in self._watched_screens:
            screen.geometryChanged.connect(self._on_screens_changed)

    def _unwatch_screens(self) -> None:
        app = QGuiApplication.instance()
        if isinstance(app, QGuiApplication):
            for sig in (app.screenAdded, app.screenRemoved, app.primaryScreenChanged):
                with contextlib.suppress(RuntimeError, TypeError):
                    sig.disconnect(self._on_screens_changed)
        screens, self._watched_screens = self._watched_screens, []
        for screen in screens:
            # A removed screen's QScreen may already be gone.
            with contextlib.suppress(RuntimeError, TypeError):
                screen.geometryChanged.disconnect(self._on_screens_changed)

    def _focus_primary(self) -> None:
        if not self._surfaces:
            return
        primary = self._surfaces[0]
        primary.raise_()
        primary.activateWindow()
        primary.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

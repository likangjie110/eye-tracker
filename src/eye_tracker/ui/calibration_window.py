"""Full-screen calibration: one surface per monitor, an animated dot and a quality report.

Flow (keyboard driven, mouse optional)::

    intro ──Space──▶ running ──(all dots done)──▶ fitting ──▶ result ──Enter──▶ saved
      │                │  Space pauses                          │  R retries
      └──── Esc cancels at any point ◀──────────────────────────┘

Each monitor gets its own frameless, full-screen, stay-on-top surface. The
:class:`~eye_tracker.gaze.calibration.CalibrationCollector` decides which dot is
shown; the surface of that dot's monitor draws it while the other surfaces point
towards it. Observations arrive through ``Controller.observation``.

Time comes from an injectable clock and all progress is derived from the
collector, so the whole sequence can be driven deterministically in tests by
advancing a fake clock and calling :meth:`CalibrationWindow.tick`.

When the user's face disappears for more than :data:`NO_FACE_PAUSE_S` the
sequence pauses by itself (the collector runs on a clock that stands still
while paused) and resumes as soon as the face is back, so a user who glances at
the keyboard does not lose dots.

While the window is open the controller is in ``CALIBRATING``: walk-away
detection and the shoulder guard are suspended and the camera runs at the
calibration rate. An abandoned window must not keep it that way, so the
calibration closes itself after :data:`IDLE_TIMEOUT_S` without a key press on
the instructions, result or error screen (or while paused with Space), and after
:data:`NO_FACE_TIMEOUT_S` of dots paused because nobody is in front of the camera.

A monitor that is added, removed, resized or moved cancels the calibration
(with a notification): dots recorded on the old geometry could never be saved.

Fitting the model (:func:`~eye_tracker.gaze.calibration.evaluate`) takes about
0.1 s, longer on slow machines. It runs on a short-lived background thread whose
result the UI tick polls, so the windows stay responsive (Esc works, the spinner
turns) meanwhile. ``fit_in_thread=False`` fits synchronously on the tick after
the "Calculating" screen was shown, which keeps tests single-threaded.
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import Future

from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QScreen,
)
from PySide6.QtWidgets import (
    QWidget,
)

from ..config import Settings
from ..gaze.calibration import (
    EVENT_FINISHED,
    EVENT_RETRY,
    EVENT_TARGET,
    PHASE_SETTLE,
    CalibrationCollector,
    CalibrationReport,
    CalibrationTarget,
    evaluate,
    make_plan,
)
from ..gaze.model import GazeModel
from ..gaze.store import CalibrationData
from ..types import Monitor, Observation, layout_signature
from .calibration_model import (
    _DANGER,
    _MUTED,
    _SUCCESS,
    _TEXT,
    _WARNING,
    GRADE_COLORS,
    FitResult,
    _css,
    _DotState,
    _ease_in_out,
    _ease_out_back,
    _fit_features,
    _monitor_labels,
    _spatial_order,
    _start_fit,
    controller_gaze_features,
)
from .calibration_surface import (
    _Surface,
)
from .util import screen_for_monitor

log = logging.getLogger(__name__)

__all__ = [
    "IDLE_TIMEOUT_S",
    "NO_FACE_PAUSE_S",
    "NO_FACE_TIMEOUT_S",
    "STATE_CLOSED",
    "STATE_ERROR",
    "STATE_FITTING",
    "STATE_IDLE",
    "STATE_INTRO",
    "STATE_RESULT",
    "STATE_RUNNING",
    "CalibrationWindow",
    "controller_gaze_features",
]

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

# ============================================================ small helpers


# ================================================================== controller
# ================================================================== controller
class CalibrationWindow(QObject):
    """Runs a calibration on every monitor and hands the result to the controller.

    Args:
        controller: The app controller (``monitors()``, ``backend_info()``,
            ``begin_calibration()``, ``finish_calibration(data)``, the
            ``observation`` and ``notify`` signals; optionally ``settings`` for
            the camera the calibration belongs to and ``gaze_feature_indices()``,
            see :func:`controller_gaze_features`).
        parent: Optional QObject parent.
        clock: Monotonic clock in seconds (inject a fake one in tests).
        points_per_monitor: Dots per monitor (1, 5 or a square number, see
            :func:`~eye_tracker.gaze.calibration.make_plan`).
        settle_s, collect_s, min_samples, max_retries: Collector timing, see
            :class:`~eye_tracker.gaze.calibration.CalibrationCollector`.
        margin: Distance of the outer dots from the screen edges (fraction).
        fit_in_thread: Fit the model on a background thread (default) instead
            of synchronously on the next tick.

    Signals:
        finished(bool): ``True`` after a calibration was saved, ``False`` when cancelled.
        state_changed(str): One of the ``STATE_*`` constants.

    Keys: Space starts / pauses, Enter saves, R retries, Esc cancels.
    """

    finished = Signal(bool)
    state_changed = Signal(str)

    def __init__(
        self,
        controller: object,
        parent: QObject | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        points_per_monitor: int = 9,
        settle_s: float = 0.8,
        collect_s: float = 1.0,
        min_samples: int = 5,
        max_retries: int = 1,
        margin: float = 0.1,
        fit_in_thread: bool = True,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._clock = clock
        self._points_per_monitor = points_per_monitor
        self._timing = (settle_s, collect_s, min_samples, max_retries)
        self._margin = margin
        self._fit_in_thread = fit_in_thread
        self._fit_future: Future[FitResult] | None = None
        self._fit_started = 0.0

        self._state = STATE_IDLE
        self._monitors: list[Monitor] = []
        self._labels: dict[int, str] = {}
        self._surfaces: list[_Surface] = []
        self._collector: CalibrationCollector | None = None
        self._plan: list[CalibrationTarget] = []
        self._model: GazeModel | None = None
        self._report: CalibrationReport | None = None
        self._data: CalibrationData | None = None
        self._error = ""

        self._prev_target: CalibrationTarget | None = None
        self._last_target: CalibrationTarget | None = None
        self._retrying = False
        self._manual_pause = False
        self._auto_pause = False
        self._pause_started: float | None = None
        self._paused_total = 0.0
        self._last_obs_at: float | None = None
        self._last_face_at: float | None = None
        self._face_grace_until = -math.inf
        self._connected = False
        #: When the user last pressed a key (or button) here; drives the idle timeout.
        self._last_input_at = 0.0
        #: When the dots were paused for lack of a face (``None`` while not).
        self._no_face_since: float | None = None
        #: Gaze-direction feature indices passed to the fit (see controller_gaze_features).
        self._nonlinear: tuple[int, ...] | None = None
        #: Screens whose geometryChanged is connected (disconnected in _finish).
        self._watched_screens: list[QScreen] = []

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self.tick)
        # Separate from the animation timer, which the result and error screens stop.
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_MS)
        self._watchdog.timeout.connect(self.check_idle)

    # ============================================================== properties
    @property
    def state(self) -> str:
        return self._state

    @property
    def is_active(self) -> bool:
        """True from :meth:`start` until the calibration is saved or cancelled."""
        return self._state not in (STATE_IDLE, STATE_CLOSED)

    @property
    def report(self) -> CalibrationReport | None:
        return self._report

    @property
    def model(self) -> GazeModel | None:
        return self._model

    @property
    def data(self) -> CalibrationData | None:
        """The saved calibration (after :meth:`save`)."""
        return self._data

    @property
    def error(self) -> str:
        return self._error

    @property
    def paused(self) -> bool:
        return self._manual_pause or self._auto_pause

    @property
    def current_target(self) -> CalibrationTarget | None:
        return self._collector.current if self._collector is not None else None

    @property
    def progress(self) -> float:
        return self._collector.progress if self._collector is not None else 0.0

    @property
    def plan(self) -> list[CalibrationTarget]:
        return list(self._plan)

    @property
    def monitors(self) -> list[Monitor]:
        return list(self._monitors)

    def surfaces(self) -> list[QWidget]:
        """The per-monitor windows (primary first)."""
        return list(self._surfaces)

    def hint(self) -> str:
        """The hint currently shown under the dot ("" when none)."""
        return self._hint(self._clock())[0]

    # ============================================================== lifecycle
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
        self._surfaces = [_Surface(self, m, self._screen_for(m)) for m in ordered]

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

    def retry(self) -> None:
        """Discard the collected dots and run the sequence again."""
        log.info("Calibration restarted")
        self.begin()

    def toggle_pause(self) -> None:
        """Pause or resume the dots (Space while running)."""
        if self._state != STATE_RUNNING:
            return
        now = self._clock()
        self._last_input_at = now
        self._manual_pause = not self._manual_pause
        self._update_pause(now)
        self._render()

    def save(self) -> bool:
        """Hand the calibration to the controller (Enter on the result screen)."""
        if self._state != STATE_RESULT or self._model is None or self._report is None:
            return False
        collector = self._collector
        assert collector is not None
        if self._layout_changed():
            # Retrying would plan on the same stale layout: close, and let the
            # user start over on the new one.
            self._on_screens_changed()
            return False
        try:
            backend, feature_version = self._controller.backend_info()  # type: ignore[attr-defined]
        except Exception:
            log.exception("controller.backend_info failed")
            backend = feature_version = ""
        if not backend or not feature_version:
            # Without them the calibration could never be matched to the backend later.
            self._show_error("The vision backend is not ready. Please try again.")
            return False
        data = CalibrationData(
            backend=str(backend),
            feature_version=str(feature_version),
            layout_signature=layout_signature(self._monitors),
            monitors=list(self._monitors),
            samples=collector.samples,
            implicit_samples=[],
            model=self._model,
            report=self._report.to_dict(),
            # The calibration only fits the camera it was recorded with (its
            # position and field of view shape every feature).
            camera=self._camera_device(),
            frame_size=collector.frame_size,
        )
        self._data = data
        log.info("Calibration saved: %s", self._report.summary())
        self._finish(data)
        return True

    def cancel(self) -> None:
        """Close without saving (Esc)."""
        if self._state in (STATE_IDLE, STATE_CLOSED):
            return
        log.info("Calibration cancelled")
        self._finish(None)

    close = cancel

    # ============================================================== input
    def handle_key(self, key: int) -> bool:
        """Keyboard control shared by every surface. Returns True if handled."""
        # Any key proves someone is at the keyboard (restarts the idle timeout).
        self._last_input_at = self._clock()
        K = Qt.Key
        enter = key in (K.Key_Return.value, K.Key_Enter.value)
        if key == K.Key_Escape.value:
            self.cancel()
            return True
        if self._state == STATE_INTRO and (enter or key == K.Key_Space.value):
            self.begin()
            return True
        if self._state == STATE_RUNNING:
            if key == K.Key_Space.value:
                self.toggle_pause()
                return True
            if key == K.Key_R.value:
                self.retry()
                return True
        if self._state == STATE_RESULT:
            if enter:
                self.save()
                return True
            if key == K.Key_R.value:
                self.retry()
                return True
        if self._state == STATE_ERROR and (enter or key in (K.Key_R.value, K.Key_Space.value)):
            self.retry()
            return True
        return False

    def _on_observation(self, obs: object) -> None:
        if not isinstance(obs, Observation) or self._state == STATE_CLOSED:
            return
        now = self._clock()
        # Any observation proves the camera works; a motion-gate copy (skipped)
        # repeats the last analysed frame, whose face is still in an unchanged image.
        self._last_obs_at = now
        if obs.face_count > 0:
            self._last_face_at = now
        if self._state == STATE_RUNNING:
            # The controller turns the motion gate off while calibrating, but a
            # skipped copy may still predate the current dot: never record one.
            if not obs.skipped and not self.paused and self._collector is not None:
                self._collector.add(obs)
        elif self._state == STATE_INTRO:
            self._refresh_face_chip()

    def _on_screens_changed(self, *_args: object) -> None:
        if not self.is_active:
            return
        log.info("Monitor layout changed; calibration cancelled")
        self._notify("Calibration cancelled", "The monitor layout changed. Please calibrate again.")
        self.cancel()

    # ============================================================== ticking
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

    # ============================================================== fitting
    def _enter_fitting(self) -> None:
        self._set_state(STATE_FITTING)
        self._fit_started = self._clock()
        for surface in self._surfaces:
            surface.unsetCursor()
            card = surface.show_card()
            card.clear()
            if surface is self._surfaces[0]:
                card.add_spinner()
                card.add_label("Calculating your calibration…", size=16.0, bold=True)
                card.add_label("This takes a moment.", muted=True)
            else:
                card.add_label("Almost done…", muted=True)
            surface.place_card()
        collector = self._collector
        samples = collector.samples if collector is not None else []
        self._nonlinear = _fit_features(controller_gaze_features(self._controller), samples)
        if self._fit_in_thread:
            self._fit_future = _start_fit(samples, self._monitors, self._nonlinear)
        # Without a thread the fit runs on the next tick, so the "Calculating"
        # screen is painted first; with one, ticks animate the spinner and poll.
        self._timer.start(TICK_MS)

    def _tick_fitting(self) -> None:
        now = self._clock()
        for surface in self._surfaces:
            if surface.card.spinner is not None:
                surface.card.spinner.set_time(now - self._fit_started)
        future = self._fit_future
        if future is None:
            collector = self._collector
            samples = collector.samples if collector is not None else []
            try:
                result = evaluate(samples, self._monitors, nonlinear=self._nonlinear)
            except Exception as exc:
                self._fit_failed(exc)
            else:
                self._fit_succeeded(*result)
            return
        if not future.done():
            return
        self._fit_future = None
        error = future.exception()
        if error is not None:
            self._fit_failed(error)
        else:
            self._fit_succeeded(*future.result())

    def _fit_succeeded(self, model: GazeModel, report: CalibrationReport) -> None:
        self._timer.stop()
        self._model, self._report = model, report
        self._last_input_at = self._clock()  # the idle timeout counts from the result
        self._set_state(STATE_RESULT)
        self._show_result(report)

    def _fit_failed(self, error: BaseException) -> None:
        collector = self._collector
        if isinstance(error, ValueError):
            # evaluate() raises ValueError when there is not enough data.
            log.info("Calibration could not be fitted: %s", error)
            skipped = len(collector.skipped_points) if collector is not None else 0
            self._show_error(
                f"Your face was not clearly visible for {skipped} of {len(self._plan)} dots."
                if skipped
                else "Not enough usable data was collected."
            )
            return
        # A numerical failure must not leave full-screen windows without a way out.
        log.error("Calibration fitting failed", exc_info=error)
        self._show_error(
            f"Something went wrong while fitting the calibration: {error}",
            badge="Calibration failed",
        )

    def _drop_fit(self) -> None:
        """Forget a running fit; its thread finishes on its own and the result is ignored."""
        if self._fit_future is not None:
            self._fit_future.cancel()
            self._fit_future = None

    # ============================================================== screens
    def _show_intro(self) -> None:
        n_points = self._points_per_monitor * len(self._monitors)
        settle_s, collect_s, _, _ = self._timing
        seconds = max(5, round(n_points * (settle_s + collect_s) / 5) * 5)
        screens = "screen" if len(self._monitors) == 1 else f"{len(self._monitors)} screens"
        for surface in self._surfaces:
            card = surface.show_card()
            card.clear()
            card.add_label("Let's calibrate", size=22.0, bold=True)
            card.add_label(
                "Look at each dot as it appears. Turn your head naturally — don't hold it still.",
                size=13.0,
            )
            card.add_label(
                f"{n_points} dots on {'one' if len(self._monitors) == 1 else 'your'} {screens}"
                f" · about {seconds} seconds",
                muted=True,
            )
            card.add_spacing(6)
            card.add_label("", name="face", size=10.5)
            card.add_spacing(4)
            card.add_keys([("Space", "start"), ("Esc", "cancel")])
            surface.place_card()
        self._refresh_face_chip()

    def _refresh_face_chip(self) -> None:
        now = self._clock()
        if self._last_obs_at is None or now - self._last_obs_at > NO_CAMERA_S:
            text, color = "●  Waiting for the camera…", _MUTED
        elif self._last_face_at is not None and now - self._last_face_at <= 1.0:
            text, color = "●  Face detected — ready", _SUCCESS
        else:
            text, color = "●  Looking for your face…", _WARNING
        for surface in self._surfaces:
            label = surface.card.labels.get("face")
            if label is not None and label.text() != text:
                label.setText(text)
                label.setStyleSheet(f"color: {_css(color)};")

    def _show_result(self, report: CalibrationReport) -> None:
        collector = self._collector
        skipped = len(collector.skipped_points) if collector is not None else 0
        multi = len(self._monitors) > 1
        for surface in self._surfaces:
            surface.unsetCursor()
            card = surface.show_card()
            card.clear()
            if surface is not self._surfaces[0]:
                card.add_label("Your results are on the main screen.", muted=True, size=12.0)
                surface.place_card()
                continue
            card.add_badge(report.grade, GRADE_COLORS.get(report.grade, _TEXT))
            if multi:
                pct = math.floor(report.monitor_accuracy * 100 + 1e-9)
                card.add_label(f"{pct}%", name="headline", size=40.0, bold=True)
                card.add_label("of glances landed on the right screen", muted=True)
            else:
                error = report.mean_error_px
                value = f"{error:.0f} px" if math.isfinite(error) else "—"
                card.add_label(value, name="headline", size=40.0, bold=True)
                card.add_label("average distance from the dots", muted=True)
            card.add_spacing(6)
            if multi and report.per_monitor_accuracy:
                # Left to right, like the dots and the "Screen n" labels.
                order = {m.index: n for n, m in enumerate(_spatial_order(self._monitors))}
                rows = [
                    (self._labels.get(index, f"Screen {index + 1}"), value)
                    for index, value in sorted(
                        report.per_monitor_accuracy.items(),
                        key=lambda kv: (order.get(kv[0], len(order)), kv[0]),
                    )
                ]
                card.add_bars(rows)
                card.add_spacing(4)
            details = f"{report.n_samples} samples from {report.n_points} dots"
            if math.isfinite(report.mean_error_px) and multi:
                details = f"Mean error {report.mean_error_px:.0f} px · " + details
            if skipped:
                details += f" · {skipped} skipped"
            card.add_label(details, name="details", muted=True, size=10.0)
            uncovered = self._uncovered_labels(report)
            if uncovered:
                # Every dot of these screens was skipped: switching to them cannot work.
                verb = "was" if len(uncovered) == 1 else "were"
                card.add_label(
                    f"{' and '.join(uncovered)} {verb} not calibrated — make sure the camera "
                    "sees your face when you look there, then press R to try again.",
                    name="tip",
                    size=10.5,
                    color=_WARNING,
                )
            elif report.grade in ("fair", "poor"):
                tip = (
                    "Tip: sit where you usually sit, make sure your face is evenly lit and "
                    "turn your head a little towards each dot. Press R to try again."
                )
                card.add_label(tip, name="tip", size=10.5, color=_WARNING)
            else:
                card.add_label(
                    "Stored only on this computer. It keeps improving as you use the mouse.",
                    name="tip",
                    size=10.0,
                    muted=True,
                )
            card.add_spacing(8)
            card.add_buttons(
                [
                    ("save", "Save   ⏎", self.save, True),
                    ("retry", "Retry   R", self.retry, False),
                    ("cancel", "Cancel   Esc", self.cancel, False),
                ]
            )
            surface.place_card()
        self._focus_primary()

    def _uncovered_labels(self, report: CalibrationReport) -> list[str]:
        """Labels of the monitors the report has no dots for, left to right."""
        uncovered = set(getattr(report, "uncovered_monitors", None) or ())
        return [
            self._labels.get(m.index, f"Screen {m.index + 1}")
            for m in _spatial_order(self._monitors)
            if m.index in uncovered
        ]

    def _show_error(self, message: str, *, badge: str = "Not enough data") -> None:
        self._timer.stop()
        self._error = message
        self._last_input_at = self._clock()  # the idle timeout counts from here
        self._set_state(STATE_ERROR)
        for surface in self._surfaces:
            surface.unsetCursor()
            card = surface.show_card()
            card.clear()
            if surface is not self._surfaces[0]:
                card.add_label("See the main screen.", muted=True, size=12.0)
                surface.place_card()
                continue
            card.add_badge(badge, _DANGER)
            card.add_label("Let's try that again", size=20.0, bold=True)
            card.add_label(message, name="message", size=12.0)
            card.add_label(
                "Check that the camera sees your whole face, that the room is not too dark "
                "and that nothing else is using the camera.",
                muted=True,
                size=10.5,
            )
            card.add_spacing(8)
            card.add_buttons(
                [
                    ("retry", "Try again   R", self.retry, True),
                    ("cancel", "Cancel   Esc", self.cancel, False),
                ]
            )
            surface.place_card()
        self._focus_primary()

    # ============================================================== internals
    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

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

    def _current_monitors(self) -> list[Monitor]:
        try:
            return list(self._controller.monitors())  # type: ignore[attr-defined]
        except Exception:
            log.exception("Could not list monitors for calibration")
            return []

    def _layout_changed(self) -> bool:
        """Whether the controller's monitor layout differs from the one being calibrated."""
        current = self._current_monitors()
        return bool(current) and layout_signature(current) != layout_signature(self._monitors)

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

    def _notify(self, title: str, message: str) -> None:
        """Tell the user through the controller's ``notify`` signal (shown by the tray)."""
        notify = getattr(self._controller, "notify", None)
        if notify is None:
            return
        try:
            notify.emit(title, message)
        except Exception:
            log.debug("Could not emit notify", exc_info=True)

    def _camera_device(self) -> str:
        """The camera setting the calibration is recorded with (``""`` if unknown)."""
        settings = getattr(self._controller, "settings", None)
        if callable(settings):
            try:
                settings = settings()
            except Exception:
                settings = None
        if isinstance(settings, Settings):
            return str(settings.camera.device).strip()
        return ""

    def _focus_primary(self) -> None:
        if not self._surfaces:
            return
        primary = self._surfaces[0]
        primary.raise_()
        primary.activateWindow()
        primary.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    @staticmethod
    def _screen_for(monitor: Monitor) -> QScreen | None:
        try:
            screen = screen_for_monitor(monitor)
        except Exception:
            log.debug("screen_for_monitor failed", exc_info=True)
            screen = None
        if screen is None:
            screen = (
                QGuiApplication.screenAt(
                    QRect(monitor.rect.x, monitor.rect.y, monitor.rect.w, monitor.rect.h).center()
                )
                or QGuiApplication.primaryScreen()
            )
        return screen

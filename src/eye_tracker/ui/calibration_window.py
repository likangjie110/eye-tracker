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

The result/save/retry handling lives in :mod:`eye_tracker.ui.calibration_result`,
the collector/tick/render plumbing and the surface/window lifecycle in
:mod:`eye_tracker.ui.calibration_flow`; both are mixed in below. This module keeps
construction, the public properties, input dispatch and fitting.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

from ..config import Settings
from ..gaze.calibration import (
    CalibrationCollector,
    CalibrationReport,
    CalibrationTarget,
    evaluate,
)
from ..gaze.model import GazeModel
from ..gaze.store import CalibrationData
from ..types import Monitor, Observation, layout_signature
from .calibration_flow import (
    GLIDE_SHARE,  # noqa: F401 - module-level constant kept importable here
    IDLE_TICK_MS,  # noqa: F401 - module-level constant kept importable here
    IDLE_TIMEOUT_S,
    NO_CAMERA_S,
    NO_FACE_PAUSE_S,
    NO_FACE_TIMEOUT_S,
    POP_SHARE,  # noqa: F401 - module-level constant kept importable here
    STATE_CLOSED,
    STATE_ERROR,
    STATE_FITTING,
    STATE_IDLE,
    STATE_INTRO,
    STATE_RESULT,
    STATE_RUNNING,
    TICK_MS,
    WATCHDOG_MS,
    CalibrationLifecycleMixin,
    CalibrationTickMixin,
)
from .calibration_model import (
    _MUTED,
    _SUCCESS,
    _WARNING,
    FitResult,
    _css,
    _fit_features,
    _start_fit,
    controller_gaze_features,
)
from .calibration_result import CalibrationResultMixin
from .util import screen_for_monitor

if TYPE_CHECKING:
    from PySide6.QtGui import QScreen

    from .calibration_surface import _Surface

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


# ================================================================== controller
class CalibrationWindow(
    CalibrationResultMixin,
    CalibrationTickMixin,
    CalibrationLifecycleMixin,
    QObject,
):
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

    # ============================================================== intro screen

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

    # ============================================================== internals

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

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

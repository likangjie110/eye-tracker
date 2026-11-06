"""Calibration/learning half of :mod:`eye_tracker.engine.controller` (TD-CPLX-001).

Calibration loading and matching, implicit learning, refits, drift detection and the
"calibration needed" reminders live here as a mixin, so ``controller.py`` keeps the gaze pipeline.
The mixin only calls back into the host controller; it must never import ``controller`` at runtime
(TYPE_CHECKING only).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import numpy as np

from .. import paths
from ..gaze.learning import DriftMonitor, ImplicitLearner, plausible_label, refit_model
from ..gaze.store import CalibrationData, CalibrationLibrary
from ..types import nearest_monitor
from .calibration_select import match_profile, with_camera_identity
from .controller_screens import SCREEN_DEBOUNCE_MS

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtCore import QTimer

    from ..config import Settings
    from ..gaze.model import GazeModel
    from ..types import Monitor, Observation

log = logging.getLogger("eye_tracker.engine.controller")

#: Minimum time between two "accuracy dropped" notifications.
DRIFT_ALERT_COOLDOWN_S = 1800.0

#: A deferred "calibration needed" announcement is delivered once the user could
#: be interrupted for this long and no monitor change is pending: displays that
#: drop out during sleep come back unchanged and must not prompt.
ANNOUNCE_SETTLE_S = 2 * SCREEN_DEBOUNCE_MS / 1000.0

#: When the user comes back (from away, a locked session or privacy mode) to a
#: calibration that is still unusable, they are reminded, at most this often.
CALIBRATION_REMINDER_S = 1800.0


class _CalibrationHost:
    """State and callbacks the calibration mixin expects from the host ``Controller``."""

    _settings: Settings
    _clock: Callable[[], float]
    _monitors: list[Monitor]
    _calibration: CalibrationData | None
    _calibration_reason: str
    _model: GazeModel | None
    _library: CalibrationLibrary
    _learner: ImplicitLearner
    _drift: DriftMonitor
    _implicit_dirty: bool
    _feature_mismatches: int
    _announce_pending: str | None
    _interruptible_since: float | None
    _last_announce: float
    _frame_size: tuple[int, int] | None
    _screen_timer: QTimer
    calibration_required: Any

    if TYPE_CHECKING:

        def _current_backend(self) -> tuple[str, str] | None: ...
        def _may_interrupt(self) -> bool: ...
        def _monitor_index_at(self, x: float, y: float) -> int | None: ...
        def _notify(self, title: str, message: str, *, force: bool = False) -> None: ...
        @staticmethod
        def _predict_point(
            model: GazeModel, features: np.ndarray
        ) -> tuple[float, float] | None: ...
        def _reset_tracking(self) -> None: ...


class ControllerCalibrationMixin(_CalibrationHost):
    """Calibration, implicit learning and refits (mixed into ``Controller``)."""

    def _learn(self, obs: Observation, now: float) -> None:
        cal, model = self._calibration, self._model
        if cal is None or model is None or self._learner.max_samples <= 0:
            return
        sample = self._learner.on_observation(obs, now, self._monitor_index_at)
        if sample is None:
            return
        predicted = self._predict_point(model, sample.features)
        predicted_monitor = (
            nearest_monitor(self._monitors, *predicted)[0].index
            if predicted is not None and self._monitors
            else None
        )
        # The comparison is drift evidence either way, also for a rejected label.
        self._drift.record(predicted_monitor, sample.monitor_index)
        self._check_drift(now)
        if predicted is not None and not plausible_label(predicted, sample, self._monitors):
            # The pointer was parked while the user read elsewhere: not a label.
            self._learner.discard_last()
            log.debug("Ignored an implausible learning sample at %.0f, %.0f", sample.x, sample.y)
            return
        self._implicit_dirty = True
        if self._learner.should_refit():
            self._refit(cal, model)

    def _refit(self, cal: CalibrationData, model: GazeModel) -> None:
        self._learner.mark_refit()
        learned = self._learner.samples
        try:
            refined = refit_model(cal.samples, learned, model, monitors=cal.monitors)
        except (ValueError, np.linalg.LinAlgError) as exc:
            log.warning("Could not refine the gaze model: %s", exc)
            return
        cal.model = refined
        cal.implicit_samples = learned
        self._model = refined
        self._save_calibration(cal, quiet=True)
        log.info("Gaze model refined with %d learned samples", len(learned))

    def _refit_after_trim(self) -> None:
        """Refit the calibration after learned samples were dropped (smaller capacity)."""
        cal = self._calibration
        if cal is None or not cal.model.is_fitted:
            return
        learned = self._learner.samples
        try:
            refined = refit_model(cal.samples, learned, cal.model, monitors=cal.monitors)
        except (ValueError, np.linalg.LinAlgError) as exc:
            log.warning("Could not refit the gaze model: %s", exc)
            return
        cal.model = refined
        cal.implicit_samples = learned
        if self._model is not None:
            self._model = refined
        self._save_calibration(cal, quiet=True)
        log.info("Gaze model refitted with the %d learned samples kept", len(learned))

    def _check_drift(self, now: float) -> None:
        if self._settings.learning.drift_alerts and self._drift.should_alert(
            now, DRIFT_ALERT_COOLDOWN_S
        ):
            self._notify(
                "Accuracy dropped",
                "Accuracy dropped — recalibrate? Choose Calibrate… in the tray menu.",
            )

    def _load_calibration(self) -> None:
        self._library = CalibrationLibrary.load(paths.calibration_file())
        self._use_profile(self._library.latest, save=False)
        if self._calibration is None:
            self._calibration_reason = "not calibrated yet"

    def _use_profile(self, data: CalibrationData | None, *, save: bool) -> None:
        """Make ``data`` the calibration in use, with its own learned samples.

        The samples learned for the previous profile are kept in that profile.
        ``save`` also marks ``data`` as the most recently used and writes the file.
        """
        if data is self._calibration:
            return
        self._flush_learned()
        self._calibration = data
        self._model = None
        self._learner.clear()
        if data is not None and data.implicit_samples:
            self._learner.load(data.implicit_samples)
        self._implicit_dirty = False
        self._drift.reset()
        self._feature_mismatches = 0
        changed = False
        if data is not None and len(self._learner.samples) < len(data.implicit_samples):
            # The learning capacity was lowered after this profile learned (while
            # another profile was in use, or in the settings file): load() kept
            # only what fits, but the stored model was fitted with all of them.
            changed = self._refit_trimmed_profile(data)
        if save and data is not None and self._library.mark_used(data):
            changed = True
        if changed:
            self._write_library(quiet=True)

    def _refit_trimmed_profile(self, data: CalibrationData) -> bool:
        """Refit ``data`` with the learned samples the learner kept.

        Otherwise the dropped samples would keep steering the gaze (with a
        capacity of 0, "no learned influence" would not hold) and stay in the
        file. Returns whether ``data`` changed (and should be saved); on failure
        the profile is left as it was.
        """
        kept = self._learner.samples
        if data.model.is_fitted:
            try:
                data.model = refit_model(data.samples, kept, data.model, monitors=data.monitors)
            except (ValueError, np.linalg.LinAlgError) as exc:
                log.warning(
                    "Could not refit the gaze model after trimming learned samples: %s", exc
                )
                return False
        data.implicit_samples = kept
        log.info(
            "Learned samples trimmed to the capacity of %d; gaze model refitted with %d",
            self._learner.max_samples,
            len(kept),
        )
        return True

    def _flush_learned(self) -> None:
        """Copy unsaved learned samples into the calibration they belong to."""
        if self._implicit_dirty and self._calibration is not None:
            self._calibration.implicit_samples = self._learner.samples

    def _with_camera_identity(self, data: CalibrationData) -> CalibrationData:
        return with_camera_identity(
            data,
            camera_device=str(self._settings.camera.device),
            frame_size=self._frame_size,
        )

    def _save_calibration(self, data: CalibrationData, *, quiet: bool) -> bool:
        """Store ``data`` as the most recently used profile and write the file."""
        self._flush_learned()
        self._library.put(data)
        return self._write_library(quiet=quiet)

    def _write_library(self, *, quiet: bool) -> bool:
        try:
            self._library.save(paths.calibration_file())
        except (OSError, ValueError) as exc:
            log.error("Could not save the calibration: %s", exc)
            if not quiet:
                self._notify("Calibration not saved", str(exc), force=True)
            return False
        self._implicit_dirty = False
        return True

    def _validate_calibration(self, *, announce: bool) -> None:
        """Decide which calibration fits the backend, monitors and camera in use.

        The profile in use is kept while it fits; otherwise the best saved profile
        for the current setup is taken (and learned samples follow it). Without
        one the calibration is unusable and, if it was usable and ``announce``,
        ``calibration_required`` is emitted (now or once the user is back).
        """
        was_usable = self._model is not None
        if not self._monitors and self._calibration is not None:
            return  # nothing to compare with; keep the current verdict
        data, reason = self._match_profile()
        if data is not None:
            switched = data is not self._calibration
            if switched:
                log.info(
                    "Using the calibration of %s for this setup",
                    data.created_at or "an earlier session",
                )
                self._use_profile(data, save=True)
            if not was_usable or switched:
                log.info("Calibration is usable")
                self._reset_tracking()
            self._model = data.model
            self._calibration_reason = ""
            self._announce_pending = None
            self._interruptible_since = None
            return
        self._model = None
        self._calibration_reason = reason
        if was_usable:
            log.warning("Calibration no longer usable: %s", reason)
            if announce:
                self._announce_calibration(reason)

    def _match_profile(self) -> tuple[CalibrationData | None, str]:
        return match_profile(
            self._library,
            self._calibration,
            backend_info=self._current_backend(),
            camera=str(self._settings.camera.device),
            frame_size=self._frame_size,
            monitors=self._monitors,
        )

    def _invalidate_calibration(self, reason: str) -> None:
        was_usable = self._model is not None
        self._model = None
        self._calibration_reason = reason
        if was_usable:
            self._announce_calibration(reason)

    def _announce_calibration(self, reason: str) -> None:
        """Emit ``calibration_required`` now, or once the user can be interrupted."""
        if self._may_interrupt():
            self._emit_calibration_required(reason)
        else:
            log.debug("Calibration announcement deferred: %s", reason)
            self._announce_pending = reason
            self._interruptible_since = None

    def _remind_calibration(self, now: float) -> None:
        """The user is back (from away, a lock or privacy mode): remind them of a
        calibration that is still unusable, unless that was said recently.

        Also when there never was one: otherwise a setup that starts at login and
        was never calibrated would never be told why nothing switches.
        """
        if (
            self._model is not None
            or self._announce_pending is not None
            or now - self._last_announce < CALIBRATION_REMINDER_S
        ):
            return
        self._announce_pending = self._calibration_reason
        self._interruptible_since = None

    def _deliver_calibration_notice(self, now: float) -> None:
        """Deliver a deferred announcement once the user has been interruptible for
        ANNOUNCE_SETTLE_S with no monitor change pending."""
        if self._announce_pending is None:
            return
        if self._model is not None:
            self._announce_pending = None  # usable again: nothing to say
            self._interruptible_since = None
            return
        if not self._may_interrupt() or self._screen_timer.isActive():
            self._interruptible_since = None
            return
        if self._interruptible_since is None:
            self._interruptible_since = now
            return
        if now - self._interruptible_since >= ANNOUNCE_SETTLE_S:
            self._emit_calibration_required(self._calibration_reason or self._announce_pending)

    def _emit_calibration_required(self, reason: str) -> None:
        self._announce_pending = None
        self._interruptible_since = None
        self._last_announce = self._clock()
        self.calibration_required.emit(reason)

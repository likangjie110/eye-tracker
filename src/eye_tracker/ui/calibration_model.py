"""Drawing helpers, colour constants and the fit worker for the calibration window.

Extracted from ``ui/calibration_window.py`` (phase 4 / B3). The window, the surface and the
small widgets import from here; nothing here imports the window.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from concurrent.futures import Future
from dataclasses import dataclass

from PySide6.QtGui import QColor

from ..gaze.calibration import (
    CalibrationReport,
    CalibrationSample,
    evaluate,
)
from ..gaze.model import GazeModel, gaze_feature_indices
from ..types import Monitor
from . import util

log = logging.getLogger(__name__)


_ACCENT = QColor(util.ACCENT)
_ACCENT_2 = QColor(util.ACCENT_2)
_TEXT = QColor(util.TEXT)
_MUTED = QColor(util.TEXT_MUTED)
_SUCCESS = QColor(util.SUCCESS)
_WARNING = QColor(util.WARNING)
_DANGER = QColor(util.DANGER)
_BG_INNER = QColor(22, 26, 46, 235)  # 92 % opacity
_BG_OUTER = QColor(7, 9, 16, 235)

GRADE_COLORS: dict[str, QColor] = {
    "excellent": _SUCCESS,
    "good": _ACCENT_2,
    "fair": _WARNING,
    "poor": _DANGER,
}


# ============================================================ small helpers
def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - _clamp01(t)) ** 3


def _ease_in_out(t: float) -> float:
    t = _clamp01(t)
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def _ease_out_back(t: float) -> float:
    t = _clamp01(t)
    c1 = 1.70158
    return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _rgba(color: QColor, alpha: float) -> QColor:
    out = QColor(color)
    out.setAlphaF(_clamp01(alpha) * color.alphaF())
    return out


def _css(color: QColor) -> str:
    return color.name(QColor.NameFormat.HexRgb)


def _grade_color(value: float) -> QColor:
    if value >= 0.97:
        return _SUCCESS
    if value >= 0.9:
        return _ACCENT_2
    if value >= 0.75:
        return _WARNING
    return _DANGER


def _spatial_order(monitors: Sequence[Monitor]) -> list[Monitor]:
    """Monitors left to right, then top to bottom (the order the dots visit them)."""
    return sorted(monitors, key=lambda m: (m.rect.x, m.rect.y, m.index))


def _monitor_labels(monitors: Sequence[Monitor]) -> dict[int, str]:
    """Human labels numbered left to right ("Screen 1 · main")."""
    labels: dict[int, str] = {}
    for n, monitor in enumerate(_spatial_order(monitors), start=1):
        text = f"Screen {n}"
        if monitor.primary:
            text += " · main"
        labels[monitor.index] = text
    return labels


FitResult = tuple[GazeModel, CalibrationReport]


def controller_gaze_features(controller: object) -> tuple[int, ...] | None:
    """Indices of the vision backend's gaze-direction features (head rotation, eyes).

    They are what :func:`~eye_tracker.gaze.calibration.evaluate` takes as
    ``nonlinear``: the model gets polynomial terms only for them, so a changed
    posture (head position) does not bend the fit. Asks the controller
    (``gaze_feature_indices()``) and otherwise derives them from the class of the
    backend ``backend_info()`` names. ``None`` when neither is known, which means
    every feature is treated alike (the behaviour of older calibrations).
    """
    getter = getattr(controller, "gaze_feature_indices", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            log.debug("controller.gaze_feature_indices() failed", exc_info=True)
        else:
            return tuple(int(i) for i in value) if value is not None else None
    try:
        from ..vision.backends import backend_class

        name = str(controller.backend_info()[0])  # type: ignore[attr-defined]
        cls = backend_class(name) if name else None
    except Exception:
        log.debug("No backend class for the calibration", exc_info=True)
        return None
    if cls is None:
        return None
    return gaze_feature_indices(cls.feature_names, cls.gaze_features)


def _fit_features(
    indices: tuple[int, ...] | None, samples: Sequence[CalibrationSample]
) -> tuple[int, ...] | None:
    """``indices``, or ``None`` when they do not fit the recorded feature vectors.

    The backend may have changed between the start of the calibration and the fit;
    a stale index must degrade the model (every feature nonlinear), not fail it.
    """
    if not indices or not samples:
        return indices or None
    n_features = len(samples[0].features)
    if max(indices) >= n_features:
        log.warning(
            "Gaze feature indices %s do not fit %d features; fitting without them",
            indices,
            n_features,
        )
        return None
    return indices


def _start_fit(
    samples: Sequence[CalibrationSample],
    monitors: Sequence[Monitor],
    nonlinear: tuple[int, ...] | None = None,
) -> Future[FitResult]:
    """Run :func:`evaluate` on a daemon thread; the returned future is polled by the UI.

    Polling (rather than a cross-thread signal) means the thread never touches a
    Qt object, so closing the calibration while it runs is always safe: the
    result is simply dropped.
    """
    future: Future[FitResult] = Future()
    samples, monitors = list(samples), list(monitors)

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(evaluate(samples, monitors, nonlinear=nonlinear))
        except BaseException as exc:  # delivered to the UI thread, never lost
            future.set_exception(exc)

    threading.Thread(target=run, name="eye-tracker-calibration-fit", daemon=True).start()
    return future


@dataclass(frozen=True)
class _DotState:
    """What the active surface draws for the current target."""

    x: float  # global coordinates of the dot
    y: float
    target_x: float  # global coordinates of the target (dot's destination)
    target_y: float
    pop: float  # 0..1 appear animation
    ring: float | None  # 0..1 settle progress (shrinking ring)
    arc: float | None  # 0..1 collect progress (filling arc)
    retry: bool
    paused: bool

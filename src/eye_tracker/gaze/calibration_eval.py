"""Calibration evaluation: fit the final model and estimate how well it will work.

Extracted from :mod:`eye_tracker.gaze.calibration` (phase 4 / B5). That module re-exports
everything here, so ``eye_tracker.gaze.calibration.evaluate`` and the other names keep working.

The evaluation works on a plain sequence of samples labelled with the points the user looked at
and never touches a clock, a camera or a screen. ``CalibrationSample`` is defined next to the
collector that records it and is imported here for type hints only, so the dependency between
the two modules stays one-way (``calibration`` -> ``calibration_eval``).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from ..types import Monitor, nearest_monitor, virtual_bounds
from .model import DEFAULT_ALPHAS, SUPPORTED_DEGREES, GazeModel, lopo_predictions, select_model

if TYPE_CHECKING:
    from .calibration import CalibrationSample

log = logging.getLogger(__name__)

#: Minimum monitor accuracy for each grade, best first.
GRADE_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("excellent", 0.97),
    ("good", 0.90),
    ("fair", 0.75),
)

MIN_SAMPLES = 10
MIN_POINTS = 3

#: Percentile of the absolute held-out error per axis that is reported as a
#: monitor's gaze error (:attr:`CalibrationReport.per_monitor_error_px`). The 75th
#: covers most looks without being dominated by the few dots the face was half
#: lost on.
AXIS_ERROR_PERCENTILE = 75.0


# --------------------------------------------------------------------- samples
def samples_to_arrays(
    samples: Iterable[CalibrationSample],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Stack samples into ``X (n, d)``, targets ``Y (n, 2)`` and ``weights (n,)``."""
    items = list(samples)
    if not items:
        raise ValueError("no samples")
    X = np.vstack([np.asarray(s.features, dtype=np.float64).reshape(1, -1) for s in items])
    Y = np.array([(s.x, s.y) for s in items], dtype=np.float64)
    W = np.array([s.weight for s in items], dtype=np.float64)
    return X, Y, W


# ---------------------------------------------------------------------- report
@dataclass
class CalibrationReport:
    """Quality of a calibration, estimated by leave-one-point-out cross-validation."""

    #: Fraction of samples whose held-out prediction is nearest to the right monitor.
    monitor_accuracy: float
    mean_error_px: float
    median_error_px: float
    per_monitor_accuracy: dict[int, float]
    n_samples: int
    n_points: int
    alpha: float
    grade: str
    #: Polynomial degree chosen for the model (see ``gaze.model``).
    degree: int = 2
    #: Monitors without a single surviving calibration dot (usually the face was
    #: not found while looking at them). They appear in ``per_monitor_accuracy``
    #: with 0.0 and cap the grade at "fair".
    uncovered_monitors: list[int] = field(default_factory=list)
    #: Gaze error per monitor and axis, ``{monitor: (x_px, y_px)}``: the
    #: :data:`AXIS_ERROR_PERCENTILE` th percentile of the absolute held-out error
    #: of that monitor's samples. Split panes are only told apart where they are
    #: large compared with it. Empty in reports saved before it existed (see
    #: :func:`eye_tracker.gaze.store.axis_error`) and for uncovered monitors.
    per_monitor_error_px: dict[int, tuple[float, float]] = field(default_factory=dict)

    def summary(self) -> str:
        """One human-readable line, e.g. ``"Excellent — 99% monitor accuracy, 180 samples"``."""
        # Floor, so "100%" is only ever shown for a perfect result.
        pct = math.floor(self.monitor_accuracy * 100.0 + 1e-9)
        text = f"{self.grade.capitalize()} — {pct}% monitor accuracy, {self.n_samples} samples"
        if math.isfinite(self.mean_error_px):
            text += f", mean error {self.mean_error_px:.0f} px"
        if self.uncovered_monitors:
            n = len(self.uncovered_monitors)
            text += f"; {n} screen{'s' if n > 1 else ''} not calibrated"
        return text

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict (monitor keys become strings, non-finite numbers ``None``)."""
        out = asdict(self)
        out["per_monitor_accuracy"] = {str(k): v for k, v in self.per_monitor_accuracy.items()}
        out["per_monitor_error_px"] = {
            str(k): [_json_number(float(v)) for v in xy]
            for k, xy in self.per_monitor_error_px.items()
        }
        return {k: _json_number(v) for k, v in out.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CalibrationReport:
        """Inverse of :meth:`to_dict`. Raises ``ValueError`` on malformed input."""
        try:
            per_monitor = {
                int(k): float(v) for k, v in dict(data.get("per_monitor_accuracy") or {}).items()
            }
            return cls(
                monitor_accuracy=_float(data["monitor_accuracy"]),
                mean_error_px=_float(data["mean_error_px"]),
                median_error_px=_float(data["median_error_px"]),
                per_monitor_accuracy=per_monitor,
                n_samples=int(data["n_samples"]),
                n_points=int(data["n_points"]),
                alpha=_float(data["alpha"]),
                grade=str(data["grade"]),
                degree=int(data.get("degree", 2)),
                uncovered_monitors=[int(i) for i in data.get("uncovered_monitors") or []],
                per_monitor_error_px=axis_errors_from_dict(data.get("per_monitor_error_px")),
            )
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise ValueError(f"invalid calibration report: {exc}") from exc


# ---------------------------------------------------------------- per-axis error
def axis_errors_from_dict(value: Any) -> dict[int, tuple[float, float]]:
    """``per_monitor_error_px`` as stored (``{"0": [x, y]}``); absent or null gives ``{}``.

    Entries that are not two finite, non-negative numbers are dropped: a damaged
    entry only means "unknown", which callers estimate otherwise. A value that is
    not an object at all raises ``ValueError``, like the rest of a malformed report.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("per_monitor_error_px must be an object")
    out: dict[int, tuple[float, float]] = {}
    for key, pair in value.items():
        if not isinstance(pair, list | tuple) or len(pair) != 2:
            continue
        try:
            ex, ey = float(pair[0]), float(pair[1])
            index = int(key)
        except (TypeError, ValueError, OverflowError):
            continue
        if all(math.isfinite(v) and v >= 0.0 for v in (ex, ey)):
            out[index] = (ex, ey)
    return dict(sorted(out.items()))


def per_axis_errors(
    predictions: np.ndarray, targets: np.ndarray, monitor_indices: Sequence[int]
) -> dict[int, tuple[float, float]]:
    """``{monitor: (x_px, y_px)}``: the :data:`AXIS_ERROR_PERCENTILE` th percentile of
    the absolute error on each axis over each monitor's samples.

    ``predictions`` and ``targets`` are ``(n, 2)`` arrays (held-out predictions and
    the points looked at); ``monitor_indices`` gives each row's monitor. Rows with
    a non-finite prediction are ignored; a monitor without a finite row is left out.
    """
    preds = np.asarray(predictions, dtype=np.float64).reshape(-1, 2)
    truth = np.asarray(targets, dtype=np.float64).reshape(-1, 2)
    owners = np.asarray([int(i) for i in monitor_indices], dtype=np.int64)
    if not preds.shape[0] == truth.shape[0] == owners.shape[0]:
        raise ValueError("predictions, targets and monitor indices differ in length")
    err = np.abs(preds - truth)
    finite = np.isfinite(err).all(axis=1)
    out: dict[int, tuple[float, float]] = {}
    for idx in sorted(set(owners.tolist())):
        rows = err[(owners == idx) & finite]
        if rows.shape[0] == 0:
            continue
        ex, ey = np.percentile(rows, AXIS_ERROR_PERCENTILE, axis=0)
        out[int(idx)] = (float(ex), float(ey))
    return out


def grade_for(monitor_accuracy: float) -> str:
    """``"excellent"`` (>= 0.97), ``"good"`` (>= 0.9), ``"fair"`` (>= 0.75) or ``"poor"``."""
    for name, threshold in GRADE_THRESHOLDS:
        if monitor_accuracy >= threshold:
            return name
    return "poor"


# ------------------------------------------------------------------ evaluation
def evaluate(
    samples: Sequence[CalibrationSample],
    monitors: Sequence[Monitor],
    degree: int | None = None,
    *,
    nonlinear: Sequence[int] | None = None,
) -> tuple[GazeModel, CalibrationReport]:
    """Fit the final model and estimate how well it will work.

    Hyper-parameters are chosen by leave-one-point-out cross-validation (every
    fold hides all samples of one calibration dot): ``alpha`` always, and the
    polynomial degree too unless ``degree`` is given. The report is computed from
    the held-out predictions of the chosen settings, so it reflects accuracy on
    gaze points the model has not seen. The returned model is fitted on all samples.

    ``nonlinear`` should be the backend's gaze-direction feature indices (see
    ``gaze.model.gaze_feature_indices``): only they get polynomial terms, which
    keeps the model accurate when the user's posture changes. ``None`` expands
    every feature (the behaviour before backends declared them).

    A monitor without any calibration dot (possible with three or more monitors)
    is reported with 0 % accuracy in ``per_monitor_accuracy``, listed in
    ``uncovered_monitors`` and caps the grade at "fair".

    Raises ``ValueError("not enough calibration data")`` with fewer than 10
    samples, fewer than 3 points, or points on fewer than two monitors (one on a
    single-monitor desk).
    """
    monitor_list = list(monitors)
    if not monitor_list:
        raise ValueError("no monitors")
    known = {m.index for m in monitor_list}
    usable = [s for s in samples if s.monitor_index in known]
    if len(usable) < len(samples):
        log.warning("Ignoring %d samples of unknown monitors", len(samples) - len(usable))

    point_ids = np.array([s.point_id for s in usable])
    covered = {s.monitor_index for s in usable}
    if (
        len(usable) < MIN_SAMPLES
        or np.unique(point_ids).shape[0] < MIN_POINTS
        or len(covered) < min(2, len(monitor_list))
    ):
        raise ValueError("not enough calibration data")

    X, Y, W = samples_to_arrays(usable)
    bounds = virtual_bounds(monitor_list)
    degrees = SUPPORTED_DEGREES if degree is None else (degree,)
    selection = select_model(
        X,
        Y,
        point_ids,
        alphas=DEFAULT_ALPHAS,
        degrees=degrees,
        bounds=bounds,
        weights=W,
        nonlinear=nonlinear,
    )
    preds = selection.predictions
    if preds is None:  # every candidate diverged; still report honest held-out errors
        preds = lopo_predictions(
            X,
            Y,
            point_ids,
            selection.alpha,
            degree=selection.degree,
            bounds=bounds,
            weights=W,
            nonlinear=nonlinear,
        )

    truth = np.array([s.monitor_index for s in usable])
    predicted = [_predicted_monitor(monitor_list, p) for p in preds]
    correct = np.array([p == t for p, t in zip(predicted, truth.tolist(), strict=True)])
    errors = np.hypot(preds[:, 0] - Y[:, 0], preds[:, 1] - Y[:, 1])
    finite = errors[np.isfinite(errors)]

    # A monitor without dots was never calibrated: the model only extrapolates
    # there, and gaze at it is often where the face is lost. An "Excellent"
    # grade computed over the other monitors would hide that.
    uncovered = sorted(known - covered)
    per_monitor = {int(idx): float(np.mean(correct[truth == idx])) for idx in sorted(covered)}
    per_monitor.update({int(idx): 0.0 for idx in uncovered})
    grade = grade_for(float(np.mean(correct)))
    if uncovered and grade in ("excellent", "good"):
        grade = "fair"
    if uncovered:
        log.warning("Calibration has no points on monitor(s) %s", uncovered)

    report = CalibrationReport(
        monitor_accuracy=float(np.mean(correct)),
        mean_error_px=float(np.mean(finite)) if finite.size else math.nan,
        median_error_px=float(np.median(finite)) if finite.size else math.nan,
        per_monitor_accuracy=dict(sorted(per_monitor.items())),
        n_samples=len(usable),
        n_points=int(np.unique(point_ids).shape[0]),
        alpha=selection.alpha,
        grade=grade,
        degree=selection.degree,
        uncovered_monitors=[int(i) for i in uncovered],
        per_monitor_error_px=per_axis_errors(preds, Y, truth.tolist()),
    )
    model = GazeModel(degree=selection.degree, alpha=selection.alpha, nonlinear=nonlinear).fit(
        X, Y, W, bounds, regions=[m.rect for m in monitor_list]
    )
    log.info(
        "Calibration evaluated: %s (degree %d, alpha %g)",
        report.summary(),
        report.degree,
        report.alpha,
    )
    return model, report


def _predicted_monitor(monitors: Sequence[Monitor], point: np.ndarray) -> int | None:
    px, py = float(point[0]), float(point[1])
    if not (math.isfinite(px) and math.isfinite(py)):
        return None
    return nearest_monitor(monitors, px, py)[0].index


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _float(value: Any) -> float:
    return math.nan if value is None else float(value)


def _json_number(value: Any) -> Any:
    """JSON has no NaN/Infinity; represent them as null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value

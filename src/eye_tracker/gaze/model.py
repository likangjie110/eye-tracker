"""The ridge regression gaze model and its persistence (phase 4 / B5).

The constants, the away region and the linear gaze estimate live in ``gaze/model_common.py``;
alpha/degree selection and LOPO validation live in ``gaze/model_selection.py``. This module keeps
:class:`GazeModel` and re-exports the public names it always exposed.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..types import Monitor, Rect, nearest_monitor
from .model_common import (
    CLIP_MARGIN,
    DEFAULT_ALPHAS,
    LOOK_AWAY_ALPHA,
    LOOK_AWAY_EXCESS,
    LOOK_AWAY_MARGIN,
    MODEL_KIND,
    STD_FLOOR,
    SUPPORTED_DEGREES,
    AwayRegion,
    _away_region,
)
from .model_selection import (
    ModelSelection,
    _as_indices,
    _as_matrix,
    _as_targets,
    _as_weights,
    _check_bounds,
    _check_indices,
    _denormalise,
    _design,
    _index_list,
    _n_terms,
    _normalise,
    _solve_ridge,
    _standardisation,
    _vector,
    lopo_predictions,
    select_alpha,
    select_model,
)

__all__ = [
    "DEFAULT_ALPHAS",
    "LOOK_AWAY_EXCESS",
    "LOOK_AWAY_MARGIN",
    "AwayRegion",
    "GazeModel",
    "ModelSelection",
    "gaze_feature_indices",
    "lopo_predictions",
    "select_alpha",
    "select_model",
]

log = logging.getLogger(__name__)


class GazeModel:
    """Maps a feature vector to a gaze point on the virtual desktop (ridge regression on
    standardised features with optional polynomial terms, see the module docstring).

    ``nonlinear`` lists the indices of the features that get quadratic and cubic
    terms (the backend's gaze-direction features, see :func:`gaze_feature_indices`);
    the others enter linearly. ``None`` expands every feature. Indices are sorted
    and de-duplicated; they are checked against the feature count when fitting.
    """

    def __init__(
        self,
        degree: int = 2,
        alpha: float = 1.0,
        nonlinear: Sequence[int] | None = None,
    ) -> None:
        if degree not in SUPPORTED_DEGREES:
            raise ValueError(f"degree must be one of {SUPPORTED_DEGREES}, got {degree!r}")
        alpha = float(alpha)
        if not math.isfinite(alpha) or alpha < 0.0:
            raise ValueError(f"alpha must be a finite number >= 0, got {alpha!r}")
        self.degree = degree
        self.alpha = alpha
        self._nonlinear = _as_indices(nonlinear)
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None
        self._lo: np.ndarray | None = None
        self._hi: np.ndarray | None = None
        self._coef: np.ndarray | None = None  # (n_terms, 2), in normalised target space
        # (1 + d, 2): the linear estimate behind looks_away (None: saved without it).
        self._linear_coef: np.ndarray | None = None
        # Where that estimate places each calibrated monitor (empty: fitted without
        # the monitors, or saved before the regions existed).
        self._away_regions: tuple[AwayRegion, ...] = ()
        self._bounds: Rect | None = None

    # ---------------------------------------------------------------- properties
    @property
    def is_fitted(self) -> bool:
        return self._coef is not None

    @property
    def n_features(self) -> int:
        """Length of the feature vectors the model was fitted on (0 if unfitted)."""
        return 0 if self._mean is None else int(self._mean.shape[0])

    @property
    def nonlinear(self) -> tuple[int, ...] | None:
        """Indices of the features with polynomial terms (``None``: all of them)."""
        return self._nonlinear

    @property
    def bounds(self) -> Rect | None:
        """Virtual-desktop rectangle used to normalise targets (``None`` if unfitted)."""
        return self._bounds

    @property
    def away_regions(self) -> tuple[AwayRegion, ...]:
        """Where the linear look-away estimate places each monitor passed to
        :meth:`fit` as ``regions`` (see :class:`AwayRegion`)."""
        return self._away_regions

    def __repr__(self) -> str:
        state = f"fitted, {self.n_features} features" if self.is_fitted else "unfitted"
        nonlinear = "" if self._nonlinear is None else f", nonlinear={list(self._nonlinear)}"
        return f"GazeModel(degree={self.degree}, alpha={self.alpha:g}{nonlinear}, {state})"

    # ---------------------------------------------------------------- fitting
    def fit(
        self,
        X: np.ndarray,
        Y: np.ndarray,
        weights: np.ndarray | None = None,
        bounds: Rect | None = None,
        *,
        regions: Sequence[Rect] | None = None,
    ) -> GazeModel:
        """Fit the model and return ``self``.

        ``X`` is ``(n, d)`` features, ``Y`` is ``(n, 2)`` targets in global pixels and
        ``weights`` optional non-negative per-sample weights (e.g. 0.5 for samples
        learned from mouse use). ``bounds`` should be the virtual desktop; when
        omitted the bounding box of ``Y`` is used. ``regions`` are the monitor
        rectangles the samples were taken on; for each one, where the linear
        look-away estimate places it is learned (:attr:`away_regions`).
        """
        X_arr = _as_matrix(X, "X")
        Y_arr = _as_targets(Y, X_arr.shape[0])
        w = _as_weights(weights, X_arr.shape[0])
        bounds = _check_bounds(bounds, Y_arr)
        _check_indices(self._nonlinear, X_arr.shape[1])

        mean, std = _standardisation(X_arr)
        A = _design((X_arr - mean) / std, self.degree, self._nonlinear)
        AtW = A.T * w
        lhs, rhs = AtW @ A, AtW @ _normalise(Y_arr, bounds)
        self._coef = _solve_ridge(lhs, rhs, self.alpha)
        # Every design starts with the linear columns [1, z], so the linear fit
        # behind looks_away is the leading block of the same normal equations.
        k = 1 + X_arr.shape[1]
        self._linear_coef = _solve_ridge(lhs[:k, :k], rhs[:k], LOOK_AWAY_ALPHA)
        self._mean, self._std = mean, std
        self._lo, self._hi = X_arr.min(axis=0), X_arr.max(axis=0)
        self._bounds = bounds
        # The training samples lie within the calibrated range, so their linear
        # estimate needs no clipping: it is the leading block of the design.
        estimates = _denormalise(A[:, :k] @ self._linear_coef, bounds)
        self._away_regions = tuple(
            _away_region(rect, Y_arr, estimates, w) for rect in (regions or ())
        )
        return self

    # ---------------------------------------------------------------- prediction
    def predict(self, x: np.ndarray) -> np.ndarray:
        """Predict gaze point(s) in global pixels: ``(d,) -> (2,)`` or ``(n, d) -> (n, 2)``."""
        if self._coef is None or self._mean is None or self._std is None or self._bounds is None:
            raise RuntimeError("GazeModel is not fitted")
        arr, X_arr = self._features(x)
        if self._lo is not None and self._hi is not None:
            span = self._hi - self._lo
            X_arr = np.clip(X_arr, self._lo - CLIP_MARGIN * span, self._hi + CLIP_MARGIN * span)
        Z = (X_arr - self._mean) / self._std
        Y = _denormalise(_design(Z, self.degree, self._nonlinear) @ self._coef, self._bounds)
        return Y[0] if arr.ndim == 1 else Y

    # ---------------------------------------------------------------- range checks
    def extrapolation(self, x: np.ndarray) -> np.ndarray:
        """How far each feature lies outside the calibrated range, in units of that range.

        ``0`` inside ``[lo, hi]`` (the minimum and maximum seen when fitting),
        ``0.25`` a quarter of the range below ``lo`` or above ``hi``, and so on;
        ``(d,) -> (d,)`` or ``(n, d) -> (n, d)``. All zeros for a model loaded
        from a file written before the range was stored, where it is unknown.
        """
        if self._mean is None:
            raise RuntimeError("GazeModel is not fitted")
        arr, X_arr = self._features(x)
        if self._lo is None or self._hi is None:
            excess = np.zeros_like(X_arr)
        else:
            span = np.maximum(self._hi - self._lo, STD_FLOOR)
            excess = np.maximum(np.maximum(self._lo - X_arr, X_arr - self._hi), 0.0) / span
        return excess[0] if arr.ndim == 1 else excess

    @property
    def has_linear_estimate(self) -> bool:
        """Whether :meth:`looks_away` can use the combined gaze direction (models
        fitted by this version; older files fall back to the per-feature test)."""
        return self._linear_coef is not None

    def looks_away(
        self,
        x: np.ndarray,
        features: Sequence[int] | None = None,
        threshold: float = LOOK_AWAY_EXCESS,
        *,
        monitors: Sequence[Monitor] | None = None,
        margin: float = LOOK_AWAY_MARGIN,
    ) -> bool:
        """Whether the feature vector ``x`` ``(d,)`` shows gaze away from every monitor.

        With ``monitors`` (the layout the model was calibrated for) and a model
        that has the linear estimate (:attr:`has_linear_estimate`): True when
        that estimate of the gaze point lies more than ``margin`` times the
        nearest monitor's diagonal outside it (see :data:`LOOK_AWAY_MARGIN` and
        the module docstring), and more than ``margin`` times the diagonal of
        every :attr:`away_regions` entry of those monitors outside that region.
        The gaze-direction ``features`` (indices; default :attr:`nonlinear`)
        extrapolate freely; the others are clipped to their calibrated range,
        since leaning back is not looking away.

        Otherwise the fallback: True when one of the gaze-direction features
        lies more than ``threshold`` of its calibrated range outside that range
        (see :data:`LOOK_AWAY_EXCESS`).

        Either way this is False when the gaze-direction features are unknown
        (``features`` and :attr:`nonlinear` both ``None``, as for models saved
        before ``nonlinear`` existed; pass the backend's indices for those), and
        for non-finite features: a broken frame never reads as "looking away".
        """
        indices = self._nonlinear if features is None else _as_indices(features)
        arr = np.asarray(x, dtype=np.float64)
        if arr.ndim != 1:
            raise ValueError(f"expected one feature vector, got shape {arr.shape}")
        if monitors and self._linear_coef is not None:
            _, X_arr = self._features(arr)
            if not indices:
                return False
            _check_indices(indices, X_arr.shape[1])
            point = self._linear_estimate(X_arr[0], indices)
            if point is None:
                return False
            monitor, distance = nearest_monitor(monitors, *point)
            if distance <= margin * monitor.rect.diagonal:
                return False
            # Regions of other layouts (a profile used elsewhere) do not apply.
            rects = {m.rect for m in monitors}
            return not any(
                region.distance_outside(*point) <= margin * region.diagonal
                for region in self._away_regions
                if region.rect in rects
            )
        excess = self.extrapolation(arr)
        if not indices:
            return False
        _check_indices(indices, excess.shape[0])
        # NaN compares False, so a broken feature never reads as "looking away".
        return bool(np.max(excess[list(indices)]) > threshold)

    def _linear_estimate(
        self, x: np.ndarray, gaze_indices: tuple[int, ...]
    ) -> tuple[float, float] | None:
        """The linear gaze estimate for ``x`` ``(d,)`` in global pixels, with only the
        features other than ``gaze_indices`` clipped (``None`` if not finite)."""
        assert self._linear_coef is not None
        assert self._mean is not None
        assert self._std is not None
        assert self._bounds is not None
        if self._lo is not None and self._hi is not None:
            span = self._hi - self._lo
            clipped = np.clip(x, self._lo - CLIP_MARGIN * span, self._hi + CLIP_MARGIN * span)
            free = np.zeros(x.shape[0], dtype=bool)
            free[list(gaze_indices)] = True
            x = np.where(free, x, clipped)
        z = (x - self._mean) / self._std
        px, py = _denormalise(np.concatenate(([1.0], z)) @ self._linear_coef, self._bounds)
        if not (math.isfinite(px) and math.isfinite(py)):
            return None
        return (float(px), float(py))

    def _features(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """``(input as an array, input as an (n, d) matrix)``, with the length checked."""
        assert self._mean is not None
        arr = np.asarray(x, dtype=np.float64)
        X_arr = arr.reshape(1, -1) if arr.ndim == 1 else arr
        if X_arr.ndim != 2 or X_arr.shape[1] != self._mean.shape[0]:
            raise ValueError(
                f"expected feature vector(s) of length {self._mean.shape[0]}, got shape {arr.shape}"
            )
        return arr, X_arr

    # ---------------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation (plain lists and numbers)."""
        out: dict[str, Any] = {"kind": MODEL_KIND, "degree": self.degree, "alpha": self.alpha}
        if self._nonlinear is not None:
            # Absent means "every feature", which is how older files are read.
            out["nonlinear"] = list(self._nonlinear)
        if (
            self._coef is not None
            and self._bounds is not None
            and self._mean is not None
            and self._std is not None
            and self._lo is not None
            and self._hi is not None
        ):
            out.update(
                {
                    "mean": self._mean.tolist(),
                    "std": self._std.tolist(),
                    "lo": self._lo.tolist(),
                    "hi": self._hi.tolist(),
                    "coef": self._coef.tolist(),
                    "bounds": self._bounds.to_list(),
                }
            )
            if self._linear_coef is not None:
                # Absent in older files, which then use the per-feature test.
                out["linear_coef"] = self._linear_coef.tolist()
            if self._away_regions:
                # Absent in older files: the monitor rectangles alone are used.
                out["away_regions"] = [region.to_dict() for region in self._away_regions]
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GazeModel:
        """Inverse of :meth:`to_dict`. Raises ``ValueError`` on malformed input."""
        if not isinstance(d, dict):
            raise ValueError("model must be an object")
        kind = d.get("kind", MODEL_KIND)
        if kind != MODEL_KIND:
            raise ValueError(f"unsupported model kind {kind!r}")
        model = _model_parameters(d)
        if "coef" not in d:
            return model  # an unfitted model was saved
        arrays = _load_model_arrays(d)
        _validate_model_arrays(model, arrays)
        model._mean, model._std, model._lo, model._hi = (
            arrays.mean,
            arrays.std,
            arrays.lo,
            arrays.hi,
        )
        model._coef, model._bounds, model._linear_coef = (
            arrays.coef,
            arrays.bounds,
            arrays.linear,
        )
        model._away_regions = arrays.regions
        return model


@dataclass(slots=True)
class _ModelArrays:
    """The fitted arrays of a saved model, before validation."""

    mean: np.ndarray
    std: np.ndarray
    coef: np.ndarray
    bounds: Rect
    lo: np.ndarray | None
    hi: np.ndarray | None
    linear: np.ndarray | None
    regions: tuple[AwayRegion, ...]


def _model_parameters(d: dict[str, Any]) -> GazeModel:
    """Build the (possibly unfitted) model from its scalar parameters."""
    try:
        return GazeModel(
            degree=int(d.get("degree", 2)),
            alpha=float(d.get("alpha", 1.0)),
            nonlinear=_index_list(d.get("nonlinear")),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid model parameters: {exc}") from exc


def _load_model_arrays(d: dict[str, Any]) -> _ModelArrays:
    """Read the fitted arrays, mapping missing/malformed values to ``invalid model data``."""
    try:
        mean = _vector(d["mean"], "mean")
        std = _vector(d["std"], "std")
        coef = np.asarray(d["coef"], dtype=np.float64)
        bounds = Rect.from_list(d["bounds"])
        lo = _vector(d["lo"], "lo") if "lo" in d else None
        hi = _vector(d["hi"], "hi") if "hi" in d else None
        linear = (
            np.asarray(d["linear_coef"], dtype=np.float64)
            if d.get("linear_coef") is not None
            else None
        )
        raw_regions = d.get("away_regions") or []
        if not isinstance(raw_regions, list):
            raise TypeError("away_regions must be a list")
        regions = tuple(AwayRegion.from_dict(r) for r in raw_regions)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        # OverflowError: int(inf) for a bounds value such as 1e400 or Infinity.
        raise ValueError(f"invalid model data: {exc}") from exc
    return _ModelArrays(mean, std, coef, bounds, lo, hi, linear, regions)


def _validate_model_arrays(model: GazeModel, arrays: _ModelArrays) -> None:
    """Check the array shapes, ranges and finiteness of a saved model."""
    n = arrays.mean.shape[0]
    if n == 0 or arrays.std.shape != (n,):
        raise ValueError("model arrays have inconsistent shapes")
    _check_indices(model.nonlinear, n)
    if arrays.coef.shape != (_n_terms(n, model.degree, model.nonlinear), 2):
        raise ValueError("model arrays have inconsistent shapes")
    _validate_clip_range(arrays, n)
    _validate_linear_coef(arrays, n)
    _validate_finite(arrays)
    if np.any(arrays.std <= 0.0) or arrays.bounds.w <= 0 or arrays.bounds.h <= 0:
        raise ValueError("model scale parameters must be positive")


def _validate_clip_range(arrays: _ModelArrays, n: int) -> None:
    if (arrays.lo is None) != (arrays.hi is None):
        raise ValueError("model clip range is incomplete")
    if arrays.lo is not None and arrays.hi is not None:
        bad_shape = arrays.lo.shape != (n,) or arrays.hi.shape != (n,)
        if bad_shape or np.any(arrays.hi < arrays.lo):
            raise ValueError("model clip range is inconsistent")


def _validate_linear_coef(arrays: _ModelArrays, n: int) -> None:
    if arrays.linear is not None and arrays.linear.shape != (1 + n, 2):
        raise ValueError("model arrays have inconsistent shapes")


def _validate_finite(arrays: _ModelArrays) -> None:
    checked = [arrays.mean, arrays.std, arrays.coef]
    if arrays.lo is not None and arrays.hi is not None:
        checked += [arrays.lo, arrays.hi]
    if arrays.linear is not None:
        checked.append(arrays.linear)
    if not all(np.all(np.isfinite(a)) for a in checked):
        raise ValueError("model contains non-finite numbers")


# ------------------------------------------------------------ model selection
def gaze_feature_indices(
    feature_names: Sequence[str], gaze_features: Sequence[str]
) -> tuple[int, ...] | None:
    """Indices of ``gaze_features`` within ``feature_names`` (a backend's
    ``VisionBackend.feature_names`` and ``gaze_features``).

    The result is what :class:`GazeModel`, :func:`select_model` and
    ``calibration.evaluate`` take as ``nonlinear`` and :meth:`GazeModel.looks_away`
    as ``features``. ``None`` (every feature is nonlinear, gaze direction unknown)
    when the backend declares no gaze features. Names missing from
    ``feature_names`` are ignored with a warning rather than raised on, so a
    backend declaration error degrades the model instead of stopping tracking.
    """
    names = list(feature_names)
    indices = []
    for name in gaze_features:
        if name in names:
            indices.append(names.index(name))
        else:
            log.warning("Gaze feature %r is not one of the features %s; ignored", name, names)
    return tuple(sorted(set(indices))) or None

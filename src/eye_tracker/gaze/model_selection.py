"""Alpha/degree selection and leave-one-pose-out validation for :class:`GazeModel`.

Extracted from ``gaze/model.py`` (phase 4 / B5); the ridge model and its serialization stay there.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..types import Rect
from .model_common import (
    _PREFER_SIMPLER,
    DEFAULT_ALPHAS,
    STD_FLOOR,
    SUPPORTED_DEGREES,
    log,
)


def select_alpha(
    X: np.ndarray,
    Y: np.ndarray,
    groups: np.ndarray,
    alphas: Sequence[float] = DEFAULT_ALPHAS,
    degree: int = 2,
    bounds: Rect | None = None,
    *,
    weights: np.ndarray | None = None,
    nonlinear: Sequence[int] | None = None,
) -> float:
    """Pick the ridge strength with the lowest leave-one-group-out mean pixel error.

    ``groups`` holds one id per sample (the calibration point id), so every fold
    predicts a point the model has never seen. That mirrors real use far better
    than per-sample cross-validation, where near-duplicate frames of the same point
    leak into the training set. Returns the middle alpha when fewer than two groups
    exist. Near-ties go to the larger alpha (the smoother model). ``nonlinear`` is
    passed to the candidate models (see :class:`GazeModel`).
    """
    selection = select_model(
        X,
        Y,
        groups,
        alphas=alphas,
        degrees=(degree,),
        bounds=bounds,
        weights=weights,
        nonlinear=nonlinear,
    )
    return selection.alpha


@dataclass(frozen=True, eq=False, slots=True)
class ModelSelection:
    """Outcome of :func:`select_model`."""

    degree: int
    alpha: float
    #: Mean leave-one-group-out error of the winner (``nan`` without cross-validation).
    mean_error_px: float
    #: Leave-one-group-out predictions ``(n, 2)`` of the winner (``None`` without CV).
    predictions: np.ndarray | None = field(default=None, repr=False)


def select_model(
    X: np.ndarray,
    Y: np.ndarray,
    groups: np.ndarray,
    *,
    alphas: Sequence[float] = DEFAULT_ALPHAS,
    degrees: Sequence[int] = SUPPORTED_DEGREES,
    bounds: Rect | None = None,
    weights: np.ndarray | None = None,
    nonlinear: Sequence[int] | None = None,
) -> ModelSelection:
    """Choose ``degree`` and ``alpha`` jointly by leave-one-group-out cross-validation.

    Simpler candidates win near-ties (lower degree first, then larger alpha). With
    fewer than two groups no cross-validation is possible and the lowest listed
    degree (at most 2) with the middle alpha is returned. ``nonlinear`` selects
    the features with polynomial terms, as for :class:`GazeModel`; build the final
    model with the same value.
    """
    alpha_list = sorted({float(a) for a in alphas}, reverse=True)
    degree_list = sorted({int(d) for d in degrees})
    if not alpha_list or not degree_list:
        raise ValueError("alphas and degrees must not be empty")
    for a in alpha_list:
        if not math.isfinite(a) or a < 0.0:
            raise ValueError(f"alpha must be a finite number >= 0, got {a!r}")
    for deg in degree_list:
        if deg not in SUPPORTED_DEGREES:
            raise ValueError(f"degree must be one of {SUPPORTED_DEGREES}, got {deg!r}")

    X_arr = _as_matrix(X, "X")
    Y_arr = _as_targets(Y, X_arr.shape[0])
    g = _as_groups(groups, X_arr.shape[0])
    w = _as_weights(weights, X_arr.shape[0])
    indices = _as_indices(nonlinear)
    _check_indices(indices, X_arr.shape[1])
    # One normalisation for every fold, so alpha means the same in each of them
    # and in the final fit.
    bounds = _check_bounds(bounds, Y_arr)
    middle_alpha = sorted(alpha_list)[len(alpha_list) // 2]

    if np.unique(g).shape[0] < 2:
        degree = min(degree_list[0], 2)
        log.debug("select_model: fewer than two groups; degree=%d alpha=%g", degree, middle_alpha)
        return ModelSelection(degree, middle_alpha, math.nan, None)

    best: ModelSelection | None = None
    for degree in degree_list:
        folds = _LopoFolds(X_arr, Y_arr, g, w, degree=degree, bounds=bounds, nonlinear=indices)
        for alpha in alpha_list:
            preds = folds.predict(alpha)
            err = _mean_error(preds, Y_arr)
            log.debug("select_model: degree=%d alpha=%g -> %.1f px", degree, alpha, err)
            if not math.isfinite(err):
                continue
            if best is None or err < best.mean_error_px * (1.0 - _PREFER_SIMPLER):
                best = ModelSelection(degree, alpha, err, preds)
    if best is None:
        log.warning("select_model: every candidate diverged; using the defaults")
        return ModelSelection(min(degree_list[0], 2), middle_alpha, math.nan, None)
    return best


def lopo_predictions(
    X: np.ndarray,
    Y: np.ndarray,
    groups: np.ndarray,
    alpha: float,
    *,
    degree: int = 2,
    bounds: Rect | None = None,
    weights: np.ndarray | None = None,
    nonlinear: Sequence[int] | None = None,
) -> np.ndarray:
    """Leave-one-group-out predictions ``(n, 2)`` in global pixels.

    Each group is predicted by a model trained on all other groups. Rows of a
    group whose complement carries no weight are ``nan``. Needs two groups.
    """
    X_arr = _as_matrix(X, "X")
    Y_arr = _as_targets(Y, X_arr.shape[0])
    g = _as_groups(groups, X_arr.shape[0])
    w = _as_weights(weights, X_arr.shape[0])
    indices = _as_indices(nonlinear)
    _check_indices(indices, X_arr.shape[1])
    if np.unique(g).shape[0] < 2:
        raise ValueError("leave-one-group-out needs at least two groups")
    if degree not in SUPPORTED_DEGREES:
        raise ValueError(f"degree must be one of {SUPPORTED_DEGREES}, got {degree!r}")
    folds = _LopoFolds(
        X_arr, Y_arr, g, w, degree=degree, bounds=_check_bounds(bounds, Y_arr), nonlinear=indices
    )
    return folds.predict(alpha)


class _LopoFolds:
    """Leave-one-group-out cross-validation that costs little more than one fit.

    The normal equations of a fold are the full ones minus the held-out group's
    contribution (``AᵀWA - A_gᵀW_gA_g``), so the design matrix is built once and
    each (fold, alpha) pair needs only a small ``p x p`` solve. Standardisation
    uses all samples; the resulting leakage of one mean/std estimate into the
    folds is negligible compared with the per-point effects being measured.
    Held-out rows are not clipped to the fold's feature range the way
    :meth:`GazeModel.predict` would; that only matters for a held-out point far
    outside every other point, where the estimate is then somewhat pessimistic.
    """

    def __init__(
        self,
        X: np.ndarray,
        Y: np.ndarray,
        groups: np.ndarray,
        w: np.ndarray,
        *,
        degree: int,
        bounds: Rect,
        nonlinear: tuple[int, ...] | None = None,
    ) -> None:
        mean, std = _standardisation(X)
        A = _design((X - mean) / std, degree, nonlinear)
        Yn = _normalise(Y, bounds)
        AtW = A.T * w
        lhs, rhs = AtW @ A, AtW @ Yn
        total_w = float(w.sum())
        self._bounds = bounds
        self._n = X.shape[0]
        self._folds: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, bool]] = []
        for gid in np.unique(groups):
            mask = groups == gid
            Ag, wg = A[mask], w[mask]
            AgtW = Ag.T * wg
            trainable = total_w - float(wg.sum()) > 1e-12
            self._folds.append((mask, lhs - AgtW @ Ag, rhs - AgtW @ Yn[mask], Ag, trainable))

    def predict(self, alpha: float) -> np.ndarray:
        preds = np.full((self._n, 2), np.nan)
        for mask, lhs, rhs, Ag, trainable in self._folds:
            if trainable:
                preds[mask] = Ag @ _solve_ridge(lhs, rhs, alpha)
        return _denormalise(preds, self._bounds)


# --------------------------------------------------------------------- internals
def _n_terms(n_features: int, degree: int, nonlinear: tuple[int, ...] | None = None) -> int:
    k = n_features if nonlinear is None else len(nonlinear)
    n = 1 + n_features
    if degree >= 2:
        n += k * (k + 1) // 2
    if degree >= 3:
        n += k
    return n


@functools.lru_cache(maxsize=8)
def _pair_indices(d: int) -> tuple[np.ndarray, np.ndarray]:
    iu, ju = np.triu_indices(d)
    return iu, ju


def _design(Z: np.ndarray, degree: int, nonlinear: tuple[int, ...] | None = None) -> np.ndarray:
    n = Z.shape[0]
    cols = [np.ones((n, 1)), Z]
    if degree >= 2:
        # A list index selects columns; a tuple would index dimensions.
        Zn = Z if nonlinear is None else Z[:, list(nonlinear)]
        k = Zn.shape[1]
        if k:
            iu, ju = _pair_indices(k)
            cols.append(Zn[:, iu] * Zn[:, ju])
            if degree >= 3:
                cols.append(Zn**3)
    return np.hstack(cols)


def _standardisation(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return X.mean(axis=0), np.maximum(X.std(axis=0), STD_FLOOR)


def _solve_ridge(lhs: np.ndarray, rhs: np.ndarray, alpha: float) -> np.ndarray:
    """Solve ``(lhs + αI')β = rhs`` where ``I'`` is the identity without the intercept."""
    system = lhs.copy()
    idx = np.arange(1, system.shape[0])
    system[idx, idx] += alpha
    try:
        coef = np.linalg.solve(system, rhs)
        if np.all(np.isfinite(coef)):
            return coef
    except np.linalg.LinAlgError:
        pass
    # Singular system (alpha == 0 with collinear or too few samples): the
    # minimum-norm solution is the most conservative choice.
    log.debug("ridge system singular; falling back to least squares")
    return np.linalg.lstsq(system, rhs, rcond=None)[0]


def _mean_error(preds: np.ndarray, Y: np.ndarray) -> float:
    err = np.hypot(preds[:, 0] - Y[:, 0], preds[:, 1] - Y[:, 1])
    if np.all(np.isnan(err)):
        return math.nan
    return float(np.nanmean(err))


def _normalise(Y: np.ndarray, bounds: Rect) -> np.ndarray:
    return (Y - (bounds.x, bounds.y)) / (bounds.w, bounds.h)


def _denormalise(Yn: np.ndarray, bounds: Rect) -> np.ndarray:
    return Yn * (bounds.w, bounds.h) + (bounds.x, bounds.y)


def _check_bounds(bounds: Rect | None, Y: np.ndarray) -> Rect:
    if bounds is None:
        x0, y0 = np.floor(Y.min(axis=0))
        x1, y1 = np.ceil(Y.max(axis=0))
        return Rect(int(x0), int(y0), max(1, int(x1 - x0)), max(1, int(y1 - y0)))
    if bounds.w <= 0 or bounds.h <= 0:
        raise ValueError(f"bounds must have a positive size, got {bounds}")
    return bounds


def _as_matrix(X: np.ndarray, name: str) -> np.ndarray:
    arr = np.asarray(X, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[0] == 0 or arr.shape[1] == 0:
        raise ValueError(f"{name} must be a non-empty (n, d) array, got shape {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} contains non-finite values")
    return arr


def _as_targets(Y: np.ndarray, n: int) -> np.ndarray:
    arr = np.asarray(Y, dtype=np.float64)
    if arr.shape != (n, 2):
        raise ValueError(f"Y must have shape ({n}, 2), got {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError("Y contains non-finite values")
    return arr


def _as_weights(weights: np.ndarray | None, n: int) -> np.ndarray:
    if weights is None:
        return np.ones(n)
    w = np.asarray(weights, dtype=np.float64).reshape(-1)
    if w.shape != (n,):
        raise ValueError(f"weights must have shape ({n},), got {w.shape}")
    if not np.all(np.isfinite(w)) or np.any(w < 0) or not np.any(w > 0):
        raise ValueError("weights must be finite, non-negative and not all zero")
    return w


def _as_groups(groups: np.ndarray, n: int) -> np.ndarray:
    g = np.asarray(groups).reshape(-1)
    if g.shape != (n,):
        raise ValueError(f"groups must have shape ({n},), got {g.shape}")
    return g


def _vector(values: Any, name: str) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be a list of numbers")
    return arr


def _as_indices(indices: Sequence[int] | None) -> tuple[int, ...] | None:
    """Sorted, de-duplicated, non-negative feature indices (``None`` passes through)."""
    if indices is None:
        return None
    out = set()
    for i in indices:
        # bool is an int, and numpy integers are not; accept the latter only.
        if isinstance(i, bool) or not isinstance(i, int | np.integer):
            raise ValueError(f"feature indices must be integers, got {i!r}")
        if i < 0:
            raise ValueError(f"feature indices must be >= 0, got {i!r}")
        out.add(int(i))
    return tuple(sorted(out))


def _check_indices(indices: tuple[int, ...] | None, n_features: int) -> None:
    if indices and indices[-1] >= n_features:
        raise ValueError(f"feature index {indices[-1]} is out of range for {n_features} features")


def _index_list(value: Any) -> tuple[int, ...] | None:
    """``nonlinear`` as read from JSON: absent/null or a list of integers."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("nonlinear must be a list of feature indices")
    return _as_indices(value)

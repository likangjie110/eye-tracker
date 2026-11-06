"""Constants, the away-region model and the linear gaze estimate (phase 4 / B5).

Shared by ``gaze/model.py`` (the ridge model) and ``gaze/model_selection.py`` (alpha/degree
selection and leave-one-pose-out validation). Extracted from ``gaze/model.py`` so neither module
has to import the other.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..types import Rect

log = logging.getLogger(__name__)


#: Identifier written by :meth:`GazeModel.to_dict`; lets future model types coexist.
MODEL_KIND = "ridge-poly"

#: Standard deviations below this are treated as this value (constant features).
STD_FLOOR = 1e-6

#: Features are clipped to the training range widened by this fraction of the
#: range on each side. Calibration targets stop short of the screen edges, so a
#: generous margin is needed to still reach the edges and corners.
CLIP_MARGIN = 0.5

DEFAULT_ALPHAS: tuple[float, ...] = (0.01, 0.1, 1.0, 10.0, 100.0)
SUPPORTED_DEGREES: tuple[int, ...] = (1, 2, 3)

#: A frame looks away from every monitor when the linear gaze estimate (see the
#: module docstring) lies more than this fraction of the nearest monitor's
#: diagonal outside that monitor: about 12 cm beyond the edge of a 24" screen.
#: In synthetic tests (two, three and stacked monitors; posture changes of
#: 5-12 cm, and users who turn the head 25 % more or less than while
#: calibrating) at most 0.1 % of on-screen frames, edges and corners included,
#: exceed it; the worst case tried - nodding 40 % more while sitting 5 cm
#: higher - flags up to 5 % of the frames on the taskbar, where the per-feature
#: test flagged up to 80 %. Over 90 % of the frames of gaze 30 cm below the
#: screens and practically all at 40-80 cm (a phone or papers on the desk)
#: exceed it. Treated as "no gaze", every such frame restarts the switch dwell,
#: so even partial detection keeps a glance at the phone from switching monitors.
LOOK_AWAY_MARGIN = 0.2

#: Ridge strength of that linear estimate (standardised features, targets
#: normalised to the desktop). Stronger than the gaze model's usual choice:
#: head and eye features are nearly collinear during a calibration, and an
#: even split between them keeps the estimate on the screens when the user
#: later moves the head more or less, at a negligible cost in slope.
LOOK_AWAY_ALPHA = 10.0

#: Fallback for models saved without the linear estimate: a frame looks away
#: when one of its gaze-direction features lies more than this fraction of its
#: calibrated range beyond that range. Calibration dots stop 10 % short of the
#: screen edges, so gaze at an edge lies about 0.125 of the range beyond it.
#: This test is weaker (about half of the frames of gaze 40 cm below the
#: screens) and flags on-screen gaze once the user moves the head more or less
#: than while calibrating; recalibrating, or a refit by adaptive learning,
#: replaces it with the linear estimate.
LOOK_AWAY_EXCESS = 0.25

#: An :class:`AwayRegion` is learned from a monitor's samples only when their
#: targets (at least three distinct ones) span at least this fraction of the
#: monitor in both directions; otherwise the corners would be extrapolated from
#: too narrow a base.
REGION_MIN_SPAN = 0.25

#: ...and only when the image of the monitor is at most this many times its
#: diagonal: a larger one is a fit gone wrong, not a density difference (4K
#: next to 1080p scales by about 1.3).
REGION_MAX_SCALE = 3.0

# A more complex candidate (higher degree, smaller alpha) must beat the simpler
# one by this relative margin in cross-validation to be chosen.
_PREFER_SIMPLER = 1e-3


@dataclass(frozen=True, slots=True)
class AwayRegion:
    """Where the linear look-away estimate places one calibrated monitor.

    ``rect`` is the monitor as calibrated. ``x0, y0, x1, y1`` bound both the
    rectangle and its image under the linear estimate (an affine fit through
    the monitor's samples); ``diagonal`` is the larger of the two diagonals.
    Gaze within ``margin * diagonal`` of the box does not look away (see
    :meth:`GazeModel.looks_away`).
    """

    rect: Rect
    x0: float
    y0: float
    x1: float
    y1: float
    diagonal: float

    @classmethod
    def of(cls, rect: Rect) -> AwayRegion:
        """The region of a monitor whose image is unknown: the monitor itself."""
        return cls(rect, rect.x, rect.y, rect.right, rect.bottom, rect.diagonal)

    def distance_outside(self, px: float, py: float) -> float:
        """Euclidean distance from the point to the box (0 when inside)."""
        dx = max(self.x0 - px, 0.0, px - self.x1)
        dy = max(self.y0 - py, 0.0, py - self.y1)
        return math.hypot(dx, dy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rect": self.rect.to_list(),
            "box": [self.x0, self.y0, self.x1, self.y1],
            "diagonal": self.diagonal,
        }

    @classmethod
    def from_dict(cls, d: Any) -> AwayRegion:
        """Inverse of :meth:`to_dict`. Raises ``ValueError`` on malformed input."""
        if not isinstance(d, dict):
            raise ValueError("away region must be an object")
        try:
            rect = Rect.from_list(d["rect"])
            x0, y0, x1, y1 = (float(v) for v in d["box"])
            diagonal = float(d["diagonal"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid away region: {exc}") from exc
        values = (x0, y0, x1, y1, diagonal)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("away region contains non-finite numbers")
        if rect.w <= 0 or rect.h <= 0 or x1 < x0 or y1 < y0 or diagonal <= 0.0:
            raise ValueError("away region is inconsistent")
        return cls(rect, x0, y0, x1, y1, diagonal)


def _away_region(rect: Rect, Y: np.ndarray, estimates: np.ndarray, w: np.ndarray) -> AwayRegion:
    """Where the linear look-away estimate places the monitor ``rect``.

    ``Y`` are the training targets, ``estimates`` their linear estimates and ``w``
    the sample weights. The estimates of the samples on ``rect`` are fitted as an
    affine function of their targets (weighted least squares), which maps the
    corners of ``rect`` to its image. The region bounds the rectangle and that
    image, so it is never smaller than the monitor itself; without enough
    samples to place the corners reliably it is the monitor itself.
    """
    region = AwayRegion.of(rect)
    on_rect = (
        (Y[:, 0] >= rect.x)
        & (Y[:, 0] < rect.right)
        & (Y[:, 1] >= rect.y)
        & (Y[:, 1] < rect.bottom)
        & (w > 0.0)
    )
    targets = Y[on_rect]
    if np.unique(targets, axis=0).shape[0] < 3:
        return region
    span = targets.max(axis=0) - targets.min(axis=0)
    if span[0] < REGION_MIN_SPAN * rect.w or span[1] < REGION_MIN_SPAN * rect.h:
        return region
    root_w = np.sqrt(w[on_rect])[:, None]
    design = np.column_stack([targets, np.ones(targets.shape[0])])
    affine, _, rank, _ = np.linalg.lstsq(design * root_w, estimates[on_rect] * root_w, rcond=None)
    if rank < 3:
        return region
    corners = np.array(
        [
            [rect.x, rect.y, 1.0],
            [rect.right, rect.y, 1.0],
            [rect.right, rect.bottom, 1.0],
            [rect.x, rect.bottom, 1.0],
        ]
    )
    image = corners @ affine
    if not np.all(np.isfinite(image)):
        return region
    diagonal = max(float(np.hypot(*(image[2] - image[0]))), float(np.hypot(*(image[3] - image[1]))))
    if diagonal > REGION_MAX_SCALE * rect.diagonal:
        log.debug("Ignoring an implausible look-away region for %s", rect)
        return region
    x0, y0 = image.min(axis=0)
    x1, y1 = image.max(axis=0)
    return AwayRegion(
        rect,
        min(float(rect.x), float(x0)),
        min(float(rect.y), float(y0)),
        max(float(rect.right), float(x1)),
        max(float(rect.bottom), float(y1)),
        max(rect.diagonal, diagonal),
    )

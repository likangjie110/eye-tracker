"""Detection and drawing helpers for the face-mesh backend.

``_Drawing`` holds what the annotator needs from the last processed frame; the
``_det_*`` helpers convert YuNet rows into boxes, sizes, centres and primary
scores; ``_in_crop`` and ``_sigmoid`` support the seed and presence checks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .facemesh_geometry import Roi, primary_score

Box = tuple[float, float, float, float]


@dataclass(slots=True)
class _Drawing:
    """What ``annotate`` needs from the last processed frame."""

    timestamp: float
    points: np.ndarray | None  # (478, 2) normalised, primary face
    others: list[Box]  # normalised boxes of the other faces
    rvec: np.ndarray | None
    tvec: np.ndarray | None
    camera: np.ndarray | None


def _det_box(face: np.ndarray, width: int, height: int) -> Box:
    """Clipped, normalised ``(x, y, w, h)`` of a YuNet row in frame pixels."""
    x, y, w, h = (float(v) for v in face[:4])
    x0, y0 = max(x / width, 0.0), max(y / height, 0.0)
    x1, y1 = min((x + w) / width, 1.0), min((y + h) / height, 1.0)
    return (x0, y0, max(x1 - x0, 0.0), max(y1 - y0, 0.0))


def _det_side(face: np.ndarray) -> float:
    return max(float(face[2]), float(face[3]))


def _det_centre(face: np.ndarray) -> tuple[float, float]:
    return float(face[0] + face[2] / 2.0), float(face[1] + face[3] / 2.0)


def _det_score(face: np.ndarray, width: int, height: int) -> float:
    return primary_score(_det_side(face), *_det_centre(face), width, height)


def _in_crop(face: np.ndarray, roi: Roi) -> bool:
    """True when the detection's centre lies inside the (unrotated) crop square."""
    cx, cy = _det_centre(face)
    half = roi.side / 2.0
    return abs(cx - roi.cx) < half and abs(cy - roi.cy) < half


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)

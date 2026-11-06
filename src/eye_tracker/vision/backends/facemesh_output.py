"""Observation building and preview drawing for the face-mesh backend.

Mixed into :class:`~.facemesh_backend.FaceMeshBackend`: turns the other detected
faces into normalised boxes, builds the :class:`~eye_tracker.types.Observation`
for a frame without landmarks and draws the preview overlay. The mixin only
declares the state it uses and never imports the backend at runtime.
"""

from __future__ import annotations

import math
import time

import cv2
import numpy as np

from ...types import Observation
from .facemesh_detection import Box, _det_box, _det_score, _Drawing
from .facemesh_geometry import Roi
from .overlay import (
    COLOR_BOX,
    COLOR_EYE,
    COLOR_IRIS,
    COLOR_OTHER,
    COLOR_POSE,
    draw_box,
    draw_label,
    line_thickness,
    observation_label,
    to_bgr,
)

#: A face whose landmark box comes this close (normalised) to the image edge is
#: probably cut off, so its landmarks are less trustworthy.
BORDER_MARGIN = 0.01

#: Preview arrow showing where the face points: 5 cm out of the face (the pose
#: model frame's -z axis).
_POSE_ARROW_CM = np.array((0.0, 0.0, -5.0))

_EYE_A_CONTOUR = (33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246)
_EYE_B_CONTOUR = (362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398)
_IRIS_A = (468, 469, 470, 471, 472)
_IRIS_B = (473, 474, 475, 476, 477)


class OutputMixin:
    """Observation and preview half of the face-mesh backend."""

    # State owned by ``FaceMeshBackend.__init__``; declared here so the methods
    # below type-check without importing the backend (there is no runtime cycle).
    _max_faces: int
    _roi: Roi | None
    _guard_others: list[Box]
    _last: _Drawing | None
    _landmarks: np.ndarray | None
    _nose_tip: np.ndarray

    # ------------------------------------------------------------- observation
    def _other_faces(
        self, counted: list[np.ndarray] | None, primary_px: np.ndarray, width: int, height: int
    ) -> list[Box]:
        """Faces besides the primary one, as normalised boxes.

        A fresh guard count (``counted``, from this very frame) is split into the
        primary face and the others, and the others are kept until the next
        count. They are matched against the primary face only here, where both
        come from the same frame: matching stale detections against a later
        face position would count the user twice after a quick head movement.
        """
        if self._max_faces < 2:
            return []
        if counted is None:
            return self._guard_others
        x0, y0 = primary_px.min(axis=0)
        x1, y1 = primary_px.max(axis=0)
        others = []
        for face in counted:
            cx = float(face[0] + face[2] / 2.0)
            cy = float(face[1] + face[3] / 2.0)
            if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                others.append(_det_box(face, width, height))
        self._guard_others = others
        return others

    def _without_landmarks(
        self,
        detections: list[np.ndarray],
        timestamp: float,
        width: int,
        height: int,
        started: float,
    ) -> Observation:
        self._roi = None
        self._landmarks = None
        # Best first, in the order the faces were tried as the primary face.
        faces = sorted(detections, key=lambda d: _det_score(d, width, height), reverse=True)
        boxes = [_det_box(f, width, height) for f in faces]
        self._last = _Drawing(timestamp, None, boxes[1:], None, None, None)
        # YuNet sees a face the landmark network rejects (a profile, heavy
        # blur): someone is there, but nothing usable for gaze.
        return Observation(
            timestamp=timestamp,
            face_count=min(len(faces), self._max_faces),
            face_box=boxes[0] if boxes else None,
            inference_ms=(time.perf_counter() - started) * 1e3,
            frame_size=(width, height),
        )

    def annotate(self, frame_bgr: np.ndarray, observation: Observation) -> np.ndarray:
        out = to_bgr(frame_bgr)
        thickness = line_thickness(out)
        last = self._last
        if last is not None and last.timestamp == observation.timestamp:
            if last.points is not None:
                self._draw_face(out, last, thickness)
            for box in last.others:
                draw_box(out, box, COLOR_OTHER, thickness)
        if observation.face_box is not None:
            draw_box(out, observation.face_box, COLOR_BOX, thickness)
        draw_label(out, observation_label(observation))
        return out

    def _draw_face(self, out: np.ndarray, last: _Drawing, thickness: int) -> None:
        assert last.points is not None
        h, w = out.shape[:2]
        pixels = last.points * (w, h)
        pts = np.round(pixels).astype(np.int32)
        for contour in (_EYE_A_CONTOUR, _EYE_B_CONTOUR):
            cv2.polylines(out, [pts[list(contour)]], True, COLOR_EYE, thickness, cv2.LINE_AA)
        for iris in (_IRIS_A, _IRIS_B):
            centre = pixels[iris[0]]
            ring = pixels[list(iris[1:])]
            radius = float(np.mean(np.hypot(*(ring - centre).T)))
            cx, cy = (round(float(v)) for v in centre)
            cv2.circle(out, (cx, cy), max(1, round(radius)), COLOR_IRIS, thickness, cv2.LINE_AA)
            cv2.circle(out, (cx, cy), max(1, thickness), COLOR_IRIS, -1, cv2.LINE_AA)
        if last.rvec is not None and last.tvec is not None and last.camera is not None:
            # Where the nose points: from the tip, 5 cm out of the face.
            tip = self._nose_tip
            ends, _ = cv2.projectPoints(
                np.array([tip, tip + _POSE_ARROW_CM]), last.rvec, last.tvec, last.camera, None
            )
            ax, ay, bx, by = (float(v) for v in ends.ravel()[:4])
            if all(math.isfinite(v) for v in (ax, ay, bx, by)):
                cv2.line(
                    out,
                    (round(ax), round(ay)),
                    (round(bx), round(by)),
                    COLOR_POSE,
                    thickness + 1,
                    cv2.LINE_AA,
                )

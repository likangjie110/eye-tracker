"""Frame analysis, observation building and preview drawing for the face-mesh backend.

Mixed into :class:`~.facemesh_backend.FaceMeshBackend`: runs the per-frame analysis pipeline
(guard count, tracked-face primary check, re-acquisition and settling), turns the other detected
faces into normalised boxes, builds the :class:`~eye_tracker.types.Observation` for a frame with or
without landmarks and draws the preview overlay. The mixin only declares the state and host methods
it uses and never imports the backend at runtime.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

import cv2
import numpy as np

from ...types import Observation
from .facemesh_detection import Box, _det_box, _det_score, _Drawing
from .facemesh_geometry import PoseEstimator, Roi, roi_from_landmarks
from .facemesh_landmarks import (
    CONFIDENT_LOGIT,
    DETECT_WIDTH,
    GUARD_PERIOD_S,
    PRIMARY_CHECK_S,
    PRIMARY_RECHECK_S,
)
from .overlay import (
    COLOR_BOX,
    COLOR_EYE,
    COLOR_IRIS,
    COLOR_OTHER,
    COLOR_POSE,
    as_bgr,
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
    """Frame analysis, observation and preview half of the face-mesh backend."""

    # State owned by ``FaceMeshBackend.__init__``; declared here so the methods
    # below type-check without importing the backend (there is no runtime cycle).
    _closed: bool
    _net: cv2.dnn.Net | None
    _frame_size: tuple[int, int] | None
    _max_faces: int
    _roi: Roi | None
    _guard_others: list[Box]
    _last: _Drawing | None
    _landmarks: np.ndarray | None
    _nose_tip: np.ndarray
    _pose: PoseEstimator
    _guard_ts: float
    _unreadable: Roi | None

    if TYPE_CHECKING:
        # Implemented by LandmarkMixin (landmark helpers) or FaceMeshBackend.
        def _forget_tracking(self) -> None: ...
        def _guard_count(self, frame: np.ndarray, timestamp: float) -> list[np.ndarray]: ...
        def _detect(self, frame: np.ndarray, input_width: int) -> list[np.ndarray]: ...
        def _infer(self, frame: np.ndarray, roi: Roi) -> tuple[np.ndarray, float] | None: ...
        def _primary_check_due(self, timestamp: float) -> bool: ...
        def _schedule_primary_check(self, timestamp: float, period: float) -> None: ...
        @staticmethod
        def _primary_candidate(
            roi: Roi, detections: list[np.ndarray], width: int, height: int
        ) -> Roi | None: ...
        def _note_settled(self, settled: bool) -> None: ...
        def _acquire(
            self,
            frame: np.ndarray,
            detections: list[np.ndarray],
            width: int,
            height: int,
            timestamp: float,
            lost: Roi | None = None,
        ) -> tuple[np.ndarray, Roi, float] | None: ...
        def _settle(
            self,
            frame: np.ndarray,
            points: np.ndarray,
            used: Roi,
            logit: float,
            detections: list[np.ndarray] | None,
            *,
            reseed: bool,
        ) -> tuple[np.ndarray, bool, float]: ...
        def _with_landmarks(
            self,
            points: np.ndarray,
            counted: list[np.ndarray] | None,
            timestamp: float,
            width: int,
            height: int,
            started: float,
        ) -> Observation: ...

    # -------------------------------------------------------------- analysis
    def process(self, frame_bgr: np.ndarray, timestamp: float) -> Observation:
        if self._closed or self._net is None:
            raise RuntimeError("FaceMeshBackend is closed")
        started = time.perf_counter()
        frame = as_bgr(frame_bgr)
        height, width = frame.shape[:2]
        if (width, height) != self._frame_size:
            self._frame_size = (width, height)
            self._forget_tracking()
        guard = self._max_faces >= 2

        # YuNet rows found in this very frame, if any search ran (reused below so
        # that no frame runs the detector twice at the same resolution).
        detections: list[np.ndarray] | None = None
        # Shoulder guard: a fresh face count on this frame, if one is due.
        counted: list[np.ndarray] | None = None
        if guard and not 0.0 <= timestamp - self._guard_ts < GUARD_PERIOD_S:
            counted = detections = self._guard_count(frame, timestamp)

        # Landmarks, the crop they came from and the pass's face-presence logit.
        roi = self._roi
        tracking = roi is not None
        found: tuple[np.ndarray, Roi, float] | None = None
        if roi is not None:
            found, detections = self._tracked_face(
                frame, timestamp, width, height, roi, detections, guard
            )
        if found is None:
            # Lost (or never had) the face: find one. With the guard on, the
            # search doubles as a fresh face count.
            tracking = False
            self._pose.reset()
            detections, counted, found = self._search_face(
                frame, timestamp, width, height, roi, detections, counted, guard
            )
        if found is None:
            self._note_settled(True)
            return self._without_landmarks(detections or [], timestamp, width, height, started)
        points, settled, logit = self._settle(frame, *found, detections, reseed=tracking)
        # A doubtful fit (a hand coming over the face) may not survive the next
        # pass on the very same picture, so the motion gate must not repeat it.
        self._note_settled(settled and logit >= CONFIDENT_LOGIT)
        roi = roi_from_landmarks(points)
        self._roi = roi if roi.valid(width, height) else None
        return self._with_landmarks(points, counted, timestamp, width, height, started)

    def _tracked_face(
        self,
        frame: np.ndarray,
        timestamp: float,
        width: int,
        height: int,
        roi: Roi,
        detections: list[np.ndarray] | None,
        guard: bool,
    ) -> tuple[tuple[np.ndarray, Roi, float] | None, list[np.ndarray] | None]:
        """Analyse the tracked crop; a clearly stronger face elsewhere may take over."""
        # With the guard on, its counts double as primary-face checks; this
        # only stays false for a while after a stronger face was rejected.
        check = self._primary_check_due(timestamp)
        if check and detections is None and not guard:
            detections = self._detect(frame, DETECT_WIDTH)
            self._schedule_primary_check(timestamp, PRIMARY_CHECK_S)
        candidate = None
        if check and detections is not None:
            candidate = self._primary_candidate(roi, detections, width, height)
        found = None
        if candidate is not None:
            # A clearly stronger face elsewhere (the user came back): it takes
            # over if the network reads it confidently; otherwise (a profile,
            # a hand over it, half out of the picture) the tracked face stays
            # rather than being lost to it, and the next try waits.
            result = self._infer(frame, candidate)
            if result is not None and result[1] >= CONFIDENT_LOGIT:
                self._pose.reset()
                found = (result[0], candidate, result[1])
            else:
                self._schedule_primary_check(timestamp, PRIMARY_CHECK_S)
        if found is None:
            result = self._infer(frame, roi)
            if result is not None:
                found = (result[0], roi, result[1])
        return found, detections

    def _search_face(
        self,
        frame: np.ndarray,
        timestamp: float,
        width: int,
        height: int,
        roi: Roi | None,
        detections: list[np.ndarray] | None,
        counted: list[np.ndarray] | None,
        guard: bool,
    ) -> tuple[list[np.ndarray], list[np.ndarray] | None, tuple[np.ndarray, Roi, float] | None]:
        """Find a face afresh; with the guard on the search also counts faces."""
        if detections is None:
            if guard:
                detections = counted = self._guard_count(frame, timestamp)
            else:
                detections = self._detect(frame, DETECT_WIDTH)
        if not guard:
            # The search chose the primary face; look again soon, the user
            # may be on the way into the picture.
            self._schedule_primary_check(timestamp, PRIMARY_RECHECK_S)
        # The face tracked until now (lost on this very frame) or the one
        # that was already unreadable before.
        lost = roi if roi is not None else self._unreadable
        found = self._acquire(frame, detections, width, height, timestamp, lost)
        return detections, counted, found

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

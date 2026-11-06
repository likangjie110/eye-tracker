"""Landmark tracking half of the face-mesh backend (phase 4 / method split).

Extracted from :class:`~.facemesh_backend.FaceMeshBackend` so the frame loop and
the three network-heavy steps stay in the backend: the frame-to-frame tracking
state, the inference/detection helpers, the rolled-seed retry and the
primary-face candidate live here. The mixin only declares the state it uses and
calls back into the host class; it must never import
:mod:`~.facemesh_backend` at runtime.
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from .facemesh_detection import Box, _det_score, _in_crop, _sigmoid
from .facemesh_geometry import (
    _NUM_LANDMARKS,
    INPUT_SIZE,
    ROI_SCALE,
    PoseEstimator,
    Roi,
    primary_score,
    roi_from_detection,
    roi_from_landmarks,
)
from .facemesh_model import _OUTPUT_NAMES
from .lite_backend import create_yunet

#: Face-presence probability below which the tracked face counts as lost.
PRESENCE_THRESHOLD = 0.5

#: YuNet input width for (re-)acquiring the face.
DETECT_WIDTH = 320

#: YuNet input width while the shoulder guard counts faces. Measured with a face
#: photo scaled into a 640x480 frame, YuNet finds faces from about 20 px wide at
#: 320 px input, 14 px at 480 and 12 px at 640. With a typical 65-78° webcam a
#: face is roughly 60-75 px divided by its distance in metres wide at 640 px, so
#: 480 px reaches onlookers about 4 m away (320 px: about 2.5-3 m) for half the
#: cost of 640 px (about 13 ms against 26 ms on one core).
GUARD_DETECT_WIDTH = 480

#: Minimum seconds between shoulder-guard face counts; the count is reused in
#: between (the guard itself waits seconds before reacting).
GUARD_PERIOD_S = 0.5

#: YuNet detections below this confidence are ignored (and not counted as faces).
DETECTION_SCORE = 0.6

#: A face elsewhere in the image whose primary score (see :func:`primary_score`)
#: is this much higher than the tracked face's takes over as the primary face.
PRIMARY_SWITCH_RATIO = 1.3

#: With the shoulder guard off, YuNet looks for a better primary face this often
#: (seconds) while a face is tracked, so a face acquired while the user was away
#: (a poster, a colleague) does not keep the role once the user is back. One
#: 320 px detection costs about 7 ms, i.e. about 0.5 % of one core at this period.
#: A stronger face that the network then rejects is not tried again for this
#: long either, also with the guard on (which detects every GUARD_PERIOD_S): an
#: unreadable face in view (a profile, a hand over it) would otherwise cost an
#: extra inference at every count, doubling the inferences at 2 fps.
PRIMARY_CHECK_S = 1.5
#: ... and this soon after a face was (re-)acquired, when the user may still be
#: on the way into the picture.
PRIMARY_RECHECK_S = 0.5

#: A landmark pass whose presence logit is below this is a partial fit at best,
#: although it clears the presence threshold. Measured on a face photo: a hand
#: over the eyes scored 3.3-3.8, over the mouth 2.8-3.2 (the next pass rejected
#: it), a face half out of the frame 0.4-4.1, three quarters in it 6.4-7.8, the
#: first, wrongly rolled pass on a face rolled by 110° 3.2; faces seen in full
#: scored 10-24 (10.3 heavily blurred, 11-12 with a hand on the chin), and 11-22
#: as a stronger face coming into the picture. Such a fit does not take the
#: primary role from the tracked face (one took it for good: it kept clearing
#: the presence threshold), and it does not count as settled (the motion gate
#: repeated one, 5° off, for seconds, although the next pass rejected it).
CONFIDENT_LOGIT = 8.0

#: YuNet's box side divided by the side of the 468-point landmark box, for the
#: same face (measured 1.10-1.18 at 320 and 480 px input). Converts a tracked
#: face's size to YuNet's scale when YuNet itself did not report it.
YUNET_TO_MESH_SIDE = 1.13

#: When the best detection fails the landmark network (a profile, a hand over the
#: face), up to this many detections are tried in all ...
MAX_SEED_CANDIDATES = 3
#: ... but only those scoring at least this share of the best one. A face much
#: smaller than the user's is a background face, and the user turning away must
#: not hand it the gaze control. Nor does any other face get it while the best
#: detection is the face tracked until it became unreadable (see
#: ``FaceMeshBackend._acquire``).
SEED_CANDIDATE_MIN_RATIO = 0.5

#: YuNet's eye points hardly follow a rolled head at 320 px (a 55° roll reads as
#: about 5°), and the landmark network rejects crops more than about 40° off, or
#: worse, fits them with a partly rolled face. A seed that fails, or passes with
#: a presence logit below ROLL_RETRY_BELOW_LOGIT, is therefore also tried rotated
#: by this angle either way, and the most face-like crop wins ...
ROLL_RETRY_DEG = 45.0
#: ... (aligned faces score a logit of about 12-22, 9 when heavily blurred; such
#: partial fits about 2-8). A turned crop scoring below this itself counts only
#: if the crop its landmarks ask for then reads confidently: a face the network
#: rejects upright (a hand over the mouth) passed turned by 45° with a logit of
#: 2-4 and a fit 30-45° off in roll and yaw, and a crop 135° off an upright face
#: with 2.4, from where the passes converged on an upside-down fit. Correctly
#: turned crops of photos rolled by 45-70° scored 11-30 ...
ROLL_RETRY_BELOW_LOGIT = 10.0
#: ... at most this often (seconds) when it does not help, so a face the network
#: can never read (a profile) does not cost two extra inferences on every frame.
ROLL_RETRY_PERIOD_S = 2.0

#: After this many analysed frames in a row whose crop did not settle, the
#: result counts as settled anyway. A crop catching up with a jump settles
#: within a frame or two; one that keeps shifting slightly on a still picture is
#: jitter, and the motion gate must be able to hold it to keep the CPU use low.
MAX_UNSETTLED_FRAMES = 3


class LandmarkMixin:
    """Landmark acquisition, settling and detection for the face-mesh backend."""

    # State owned by ``FaceMeshBackend.__init__``; declared here so the methods
    # below type-check without importing the backend (there is no runtime cycle).
    _net: cv2.dnn.Net | None
    _detector: cv2.FaceDetectorYN | None
    _guard_detector: cv2.FaceDetectorYN | None
    _detector_sizes: dict[int, tuple[int, int]]
    _detector_model: Path | None
    _pose: PoseEstimator
    _roi: Roi | None
    _settled: bool
    _unsettled_frames: int
    _guard_others: list[Box]
    _guard_ts: float
    _primary_ts: float
    _primary_period: float
    _roll_retry_ts: float
    _unreadable: Roi | None

    # ----------------------------------------------------------- tracking state
    def _forget_tracking(self) -> None:
        """Drop everything carried from frame to frame except the blink baseline."""
        self._roi = None
        self._settled = True
        self._unsettled_frames = 0
        self._guard_others = []
        self._guard_ts = -math.inf
        self._primary_ts = -math.inf
        self._primary_period = PRIMARY_CHECK_S
        self._roll_retry_ts = -math.inf
        self._unreadable = None
        self._pose.reset()

    def _note_settled(self, settled: bool) -> None:
        self._unsettled_frames = 0 if settled else self._unsettled_frames + 1
        self._settled = settled or self._unsettled_frames >= MAX_UNSETTLED_FRAMES

    def _primary_check_due(self, timestamp: float) -> bool:
        # Written like the guard's check so that a clock jumping backwards (a
        # new timestamp source) counts as due rather than as "checked recently".
        return not 0.0 <= timestamp - self._primary_ts < self._primary_period

    def _schedule_primary_check(self, timestamp: float, period: float) -> None:
        self._primary_ts = timestamp
        self._primary_period = period

    def _infer(self, frame: np.ndarray, roi: Roi) -> tuple[np.ndarray, float] | None:
        """Landmarks (frame pixels) and face-presence logit for a crop, or ``None``.

        ``None`` when the presence probability is below
        :data:`PRESENCE_THRESHOLD`. The logit is returned rather than the
        probability because it keeps ranking crops where the probability has
        long saturated at 1.0 (see ``_acquire``).
        """
        assert self._net is not None
        m = roi.transform()
        crop = cv2.warpAffine(
            frame,
            m,
            (INPUT_SIZE, INPUT_SIZE),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
        )
        blob = cv2.dnn.blobFromImage(crop, 1.0 / 255.0, (INPUT_SIZE, INPUT_SIZE), swapRB=True)
        self._net.setInput(blob)
        raw, flag = self._net.forward(_OUTPUT_NAMES)
        logit = float(flag.ravel()[0])
        if not _sigmoid(logit) >= PRESENCE_THRESHOLD:  # also rejects NaN
            return None
        crop_points = raw.reshape(-1, 3)[:_NUM_LANDMARKS, :2].astype(np.float64)
        inverse = cv2.invertAffineTransform(m)
        points = crop_points @ inverse[:, :2].T + inverse[:, 2]
        if not np.all(np.isfinite(points)):
            return None
        return points, logit

    def _detect(self, frame: np.ndarray, input_width: int) -> list[np.ndarray]:
        """YuNet detections (rows in frame pixels) with score >= DETECTION_SCORE."""
        height, width = frame.shape[:2]
        detector: cv2.FaceDetectorYN | None = self._detector
        if input_width == GUARD_DETECT_WIDTH:
            if self._guard_detector is None:
                self._guard_detector = create_yunet(self._detector_model, GUARD_DETECT_WIDTH)
            detector = self._guard_detector
        assert detector is not None
        scale = min(1.0, input_width / width)
        image = frame
        if scale < 1.0:
            size = (input_width, max(1, round(height * scale)))
            image = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        size = (image.shape[1], image.shape[0])
        if self._detector_sizes.get(input_width) != size:
            detector.setInputSize(size)
            self._detector_sizes[input_width] = size
        _, rows = detector.detect(image)
        if rows is None:
            return []
        faces = []
        for row in rows:
            if float(row[14]) >= DETECTION_SCORE:
                face = row.astype(np.float64)
                face[:14] /= scale
                faces.append(face)
        return faces

    def _guard_count(self, frame: np.ndarray, timestamp: float) -> list[np.ndarray]:
        """Detect every face at the guard's resolution and note when."""
        faces = self._detect(frame, GUARD_DETECT_WIDTH)
        self._guard_ts = timestamp
        return faces

    def _better_rolled(
        self, frame: np.ndarray, seed: Roi, logit_to_beat: float
    ) -> tuple[np.ndarray, Roi, float] | None:
        """``seed`` rolled by :data:`ROLL_RETRY_DEG` either way, if that looks more like a face.

        Both rotations are run; the one with the higher presence logit is
        returned (landmarks, crop, logit) when it beats ``logit_to_beat`` (the
        unrotated seed's). Passing the presence threshold is not enough: a crop
        50-100° off can pass with a confident, wrongly rolled fit, but measured
        on rolled photos it scores a logit of about 2-14 against 18-28 for the
        right orientation.

        A winner below :data:`ROLL_RETRY_BELOW_LOGIT` is doubtful too: a face
        turned further than the retry turned the crop (still about 45° off), or
        no face at all. A face the network rejects upright (a hand over the
        mouth) passed turned with a logit of 2-4 and a fit 30-45° off in roll
        and yaw. One more pass on the crop its landmarks ask for tells them
        apart: the former then reads confidently, the latter was rejected.
        """
        best: tuple[np.ndarray, Roi, float] | None = None
        for turn in (ROLL_RETRY_DEG, -ROLL_RETRY_DEG):
            crop = Roi(seed.cx, seed.cy, seed.side, seed.angle + turn)
            result = self._infer(frame, crop)
            if result is not None and result[1] > logit_to_beat:
                best, logit_to_beat = (result[0], crop, result[1]), result[1]
        if best is None or best[2] >= ROLL_RETRY_BELOW_LOGIT:
            return best
        height, width = frame.shape[:2]
        wanted = roi_from_landmarks(best[0])
        follow = self._infer(frame, wanted) if wanted.valid(width, height) else None
        if follow is None or follow[1] < ROLL_RETRY_BELOW_LOGIT:
            return None
        return follow[0], wanted, follow[1]

    @staticmethod
    def _primary_candidate(
        roi: Roi, detections: list[np.ndarray], width: int, height: int
    ) -> Roi | None:
        """Crop of a clearly stronger face elsewhere than the tracked ``roi``, if any.

        Faces are compared by :func:`primary_score` and must beat the tracked
        face by :data:`PRIMARY_SWITCH_RATIO`. Like is compared with like: the
        tracked face's size is YuNet's box for it when this frame's detections
        include it, and otherwise its landmark box converted to YuNet's scale
        (:data:`YUNET_TO_MESH_SIDE`). Comparing YuNet's box (about 13 % larger)
        with the landmark box would let a face only 1.15x the user's take over.
        """
        others = [face for face in detections if not _in_crop(face, roi)]
        if not others:
            return None
        tracked = [face for face in detections if _in_crop(face, roi)]
        if tracked:
            tracked_score = max(_det_score(face, width, height) for face in tracked)
        else:
            mesh_side = roi.side / ROI_SCALE
            tracked_score = primary_score(
                mesh_side * YUNET_TO_MESH_SIDE, roi.cx, roi.cy, width, height
            )
        best = max(others, key=lambda d: _det_score(d, width, height))
        if _det_score(best, width, height) <= PRIMARY_SWITCH_RATIO * tracked_score:
            return None
        seed = roi_from_detection(best)
        return seed if seed.valid(width, height) else None

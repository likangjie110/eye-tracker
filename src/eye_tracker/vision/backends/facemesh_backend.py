"""Face mesh backend: 478 face landmarks (with irises) and head pose via OpenCV DNN.

This is the accurate default. It runs MediaPipe's face landmark network (the
``face_landmarks_detector.tflite`` member of the Face Landmarker bundle,
Apache-2.0, see ``models/NOTICE.md``) with OpenCV's DNN module. The MediaPipe
runtime itself is deliberately not used: it contains a usage-logging client
that uploads to Google servers, and this app never touches the network.

Each analysed frame costs about one landmark inference (about 12 ms on one core
of a current desktop CPU, 10 ms of it in the network; a frame without a face
costs one YuNet detection, about 7 ms):

1. **Region of interest.** A square crop, rotated so that the eye line is
   horizontal, is tracked from the previous frame's landmarks (MediaPipe's rule:
   landmark bounding box, 1.5x its longer side). When there is no previous face,
   when the network's face-presence score drops below 0.5 or when the crop
   degenerates, YuNet (the lite backend's detector) finds faces and the best
   one (largest, then most central) seeds a new crop. When the network rejects
   that seed or accepts it only half-heartedly, it is also tried rotated
   (YuNet misjudges strong head roll) and the most face-like crop wins if the
   network is confident about it; when nothing works, comparable runners-up
   are tried, unless the unreadable face is the one tracked until then (the
   user's hand over it, say).
2. **Landmarks.** The 256x256 crop goes through the network; its 478 points
   (468 mesh points plus five per iris) are mapped back into the frame. When
   the face moved noticeably since the crop was chosen, the landmarks are
   biased towards the old position, so the network runs again on the crop
   they ask for (and after a large jump on a crop from a fresh YuNet detection)
   until the crop sits on the face: a still or slowly moving face costs one
   inference, a posture shift between two frames two to four (see
   ``FaceMeshBackend._settle``).
3. **Head pose.** ``cv2.solvePnP`` fits MediaPipe's canonical face mesh to the
   landmarks MediaPipe itself uses for rigid fitting (its Procrustes basis),
   with approximate intrinsics (focal length = longer frame side, principal
   point = frame centre) and the previous pose as the initial guess.
4. **Eyes.** Iris position inside each eye and lid opening (blinks).

With the shoulder guard on (``max_faces >= 2``) YuNet additionally counts faces
at :data:`GUARD_DETECT_WIDTH` pixels at most every :data:`GUARD_PERIOD_S`
seconds, which finds onlookers several metres away. With the guard off it still
looks every :data:`PRIMARY_CHECK_S` seconds (one 320 px detection, about 0.5 %
of one core): either way, a clearly stronger face elsewhere in the picture
takes over as the primary face if the network reads it confidently, so that a
poster or a colleague acquired while the user was away does not keep the gaze
control once the user is back.

Feature vector (:attr:`FaceMeshBackend.feature_names`):

* ``yaw``, ``pitch``, ``roll`` in degrees. Yaw is positive when the nose points
  towards the right edge of the camera image, pitch when it points towards the
  bottom edge (head lowered), roll when the head is tilted counter-clockwise in
  the image. Mirroring the image flips the sign of yaw and roll.
* ``tx``, ``ty``, ``tz``: position of the head centre relative to the camera in
  centimetres (x right, y down, z away from the camera), only as accurate as the
  assumed focal length.
* ``iris_h``, ``iris_v``: see :class:`EyeMetrics`.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import ClassVar

import cv2
import numpy as np

from ... import paths
from ...types import Observation
from ..threads import limit_opencv_threads
from .base import BackendUnavailable, VisionBackend
from .face_geometry import GeometryError, load_face_geometry

# The helpers moved to the three modules below; imported here so that
# ``facemesh_backend`` stays the import point for callers and tests (some of the
# private names are only used by tests, hence the re-export noqa).
from .facemesh_detection import (  # noqa: F401 - re-exported
    Box,
    _det_box,
    _det_centre,
    _det_score,
    _det_side,
    _Drawing,
    _in_crop,
    _sigmoid,
)
from .facemesh_geometry import (  # noqa: F401 - re-exported
    _EYE_LINE,
    _MESH_POINTS,
    _MIN_EYE_WIDTH_PX,
    _NUM_LANDMARKS,
    BLINK_BASELINE_MIN_SAMPLES,
    BLINK_BASELINE_RATIO,
    BLINK_BASELINE_WINDOW,
    BLINK_MIN_OPENNESS,
    BLINK_OPENNESS,
    CATCH_UP_MIN_LOGIT,
    EYE_A,
    EYE_B,
    INPUT_SIZE,
    MIN_ROI_SIDE,
    PRIMARY_CENTRE_WEIGHT,
    ROI_CATCH_UP_SHIFT,
    ROI_SCALE,
    ROI_SETTLE_SCALE,
    ROI_SETTLE_SHIFT,
    BlinkDetector,
    EyeMetrics,
    HeadPose,
    PoseEstimator,
    Roi,
    _eye_line_angle,
    camera_matrix,
    crop_caught_up,
    crop_settled,
    eye_metrics,
    pose_from_rotation,
    primary_score,
    roi_from_detection,
    roi_from_landmarks,
)
from .facemesh_model import (  # noqa: F401 - re-exported
    _OUTPUT_NAMES,
    _SELF_TEST_FLAG,
    _SELF_TEST_FLAG_TOLERANCE,
    _SELF_TEST_IRIS,
    _SELF_TEST_MEAN,
    _SELF_TEST_PX_TOLERANCE,
    _check_landmark_net,
    _read_tflite,
    _windows_short_path,
    load_landmark_net,
    self_test_blob,
)
from .lite_backend import create_yunet
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

MODEL_FILE = "face_landmarks_detector.tflite"
GEOMETRY_FILE = "geometry_pipeline_metadata_landmarks.binarypb"

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

#: A face whose landmark box comes this close (normalised) to the image edge is
#: probably cut off, so its landmarks are less trustworthy.
BORDER_MARGIN = 0.01
MAX_FACES_LIMIT = 5

_NOSE_TIP = 4
#: Preview arrow showing where the face points: 5 cm out of the face (the pose
#: model frame's -z axis).
_POSE_ARROW_CM = np.array((0.0, 0.0, -5.0))

_EYE_A_CONTOUR = (33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246)
_EYE_B_CONTOUR = (362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398)
_IRIS_A = (468, 469, 470, 471, 472)
_IRIS_B = (473, 474, 475, 476, 477)


class FaceMeshBackend(VisionBackend):
    """Head pose and iris features from MediaPipe's face landmark network.

    Creating an instance caps OpenCV's process-wide thread pool (see
    :mod:`eye_tracker.vision.threads`).

    Args:
        model_path: Landmark network (``face_landmarks_detector.tflite``).
        max_faces: Faces to report; 2 or more enables shoulder-guard counting.
        geometry_path: Canonical face geometry (``.binarypb``).
        detector_path: YuNet face detector (``.onnx``).

    Raises:
        BackendUnavailable: OpenCV lacks the needed modules or a model cannot be
            loaded.
    """

    name: ClassVar[str] = "facemesh"
    feature_names: ClassVar[tuple[str, ...]] = (
        "yaw",
        "pitch",
        "roll",
        "tx",
        "ty",
        "tz",
        "iris_h",
        "iris_v",
    )
    feature_version: ClassVar[str] = "facemesh-pose-iris-1"
    gaze_features: ClassVar[tuple[str, ...]] = ("yaw", "pitch", "iris_h", "iris_v")

    def __init__(
        self,
        model_path: Path | None = None,
        max_faces: int = 1,
        *,
        geometry_path: Path | None = None,
        detector_path: Path | None = None,
    ) -> None:
        if not hasattr(cv2, "dnn") or not hasattr(cv2.dnn, "readNetFromTFLite"):
            raise BackendUnavailable(
                f"OpenCV {cv2.__version__} cannot read TFLite models (4.10 or newer is required)"
            )
        limit_opencv_threads()
        try:
            geometry = load_face_geometry(
                Path(geometry_path) if geometry_path else paths.model_path(GEOMETRY_FILE)
            )
        except GeometryError as exc:
            raise BackendUnavailable(
                f"Face geometry unavailable: {exc} (run scripts/fetch_models.py)"
            ) from exc
        self._net: cv2.dnn.Net | None = load_landmark_net(
            Path(model_path) if model_path else paths.model_path(MODEL_FILE)
        )
        self._detector_model = Path(detector_path) if detector_path else None
        self._detector: cv2.FaceDetectorYN | None = create_yunet(self._detector_model, DETECT_WIDTH)
        self._guard_detector: cv2.FaceDetectorYN | None = None
        self._detector_sizes: dict[int, tuple[int, int]] = {}
        self._pose = PoseEstimator(geometry)
        # Nose tip in the pose model frame, for the preview's direction line.
        self._nose_tip = geometry.vertices[_NOSE_TIP] * (1.0, -1.0, -1.0)
        self._blink = BlinkDetector()
        self._max_faces = _clamp_faces(max_faces)
        self._roi: Roi | None = None
        self._settled = True
        self._unsettled_frames = 0
        self._frame_size: tuple[int, int] | None = None
        # Other faces (normalised boxes) from the latest shoulder-guard count,
        # reused until the next count is due.
        self._guard_others: list[Box] = []
        self._guard_ts = -math.inf
        # Primary-face check with the guard off: time of the last one and the
        # period until the next.
        self._primary_ts = -math.inf
        self._primary_period = PRIMARY_CHECK_S
        # Last time rotated seeds were tried in vain (see ROLL_RETRY_PERIOD_S).
        self._roll_retry_ts = -math.inf
        # Where the face tracked until it became unreadable is (see _acquire).
        self._unreadable: Roi | None = None
        self._last: _Drawing | None = None
        self._landmarks: np.ndarray | None = None
        self._closed = False

    # ----------------------------------------------------------- lifecycle
    def set_max_faces(self, n: int) -> None:
        n = _clamp_faces(n)
        if n == self._max_faces:
            return
        self._max_faces = n
        # Count again on the next frame with the new setting.
        self._guard_others = []
        self._guard_ts = -math.inf

    @property
    def max_faces(self) -> int:
        """Most faces reported in ``face_count``."""
        return self._max_faces

    @property
    def settled(self) -> bool:
        """``False`` while the tracking crop is still catching up with a moved face.

        A frame re-runs the network within itself only after a large jump (see
        ``_settle``); after a moderate move, or a jump that a few passes could
        not quite follow, the crop the landmarks ask for is still off by more
        than :data:`ROI_SETTLE_SHIFT` / :data:`ROI_SETTLE_SCALE` and the next
        frame continues from it. The remaining bias is small, but the motion
        gate must not repeat it for seconds. Nor a doubtful fit (a presence
        logit below :data:`CONFIDENT_LOGIT`), which the next pass may reject.
        ``True`` again after :data:`MAX_UNSETTLED_FRAMES` unsettled frames in a
        row.
        """
        return self._settled

    def reset(self) -> None:
        """Forget the tracked face; the next frame looks for faces afresh.

        The vision worker calls this when the camera was released (pause,
        privacy mode, a lock) or replaced: the picture may have changed
        completely meanwhile, and a crop left over from before could latch onto
        whatever face is there now. The blink baseline is kept, it belongs to
        the user rather than to a camera session.
        """
        self._forget_tracking()
        self._last = None
        self._landmarks = None

    @property
    def landmarks(self) -> np.ndarray | None:
        """Normalised ``(478, 2)`` landmarks of the last frame's primary face."""
        return None if self._landmarks is None else self._landmarks.copy()

    @property
    def blink_threshold(self) -> float:
        """Current eye-openness threshold for blinks (diagnostics)."""
        return self._blink.threshold

    def close(self) -> None:
        self._closed = True
        self._net = None
        self._detector = None
        self._guard_detector = None
        self._forget_tracking()
        self._last = None
        self._landmarks = None
        self._blink.reset()

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

    # ------------------------------------------------------------ analysis
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
        found: tuple[np.ndarray, Roi, float] | None = None
        roi = self._roi
        tracking = roi is not None
        if roi is not None:
            # With the guard on, its counts double as primary-face checks; this
            # only stays false for a while after a stronger face was rejected.
            check = self._primary_check_due(timestamp)
            if check and detections is None and not guard:
                detections = self._detect(frame, DETECT_WIDTH)
                self._schedule_primary_check(timestamp, PRIMARY_CHECK_S)
            candidate = None
            if check and detections is not None:
                candidate = self._primary_candidate(roi, detections, width, height)
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
        if found is None:
            # Lost (or never had) the face: find one. With the guard on, the
            # search doubles as a fresh face count.
            tracking = False
            self._pose.reset()
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

    def _acquire(
        self,
        frame: np.ndarray,
        detections: list[np.ndarray],
        width: int,
        height: int,
        timestamp: float,
        lost: Roi | None = None,
    ) -> tuple[np.ndarray, Roi, float] | None:
        """Seed the landmark network from YuNet detections; the first that works wins.

        Candidates go best first (largest, then most central: see
        :func:`primary_score`). When the network rejects the best one or
        accepts it only with a low presence logit (:data:`ROLL_RETRY_BELOW_LOGIT`),
        it is also tried rotated by :data:`ROLL_RETRY_DEG` either way (YuNet
        misjudges strong head roll) and the most face-like crop wins. When it
        still fails, comparable runners-up are tried (see
        :data:`SEED_CANDIDATE_MIN_RATIO`), unless the best detection lies in
        ``lost``, the crop of the face that was tracked until it became
        unreadable: that is the user drinking, a hand over the face, turned
        away, and a poster or a colleague in the background must not take the
        gaze control meanwhile. Such a face is followed from frame to frame
        until a face is read again or it is no longer the best detection. A
        frame where the first seed works well costs one inference, as before.
        Returns the landmarks, their crop and its presence logit.
        """
        if not detections:
            # Nothing in view: whether the user is still there is unknown, so
            # what is remembered of them is kept.
            return None
        ranked = sorted(detections, key=lambda d: _det_score(d, width, height), reverse=True)
        best_score = _det_score(ranked[0], width, height)
        holding = lost is not None and _in_crop(ranked[0], lost)
        self._unreadable = None
        for index, face in enumerate(ranked[:MAX_SEED_CANDIDATES]):
            if index and (
                holding or _det_score(face, width, height) < SEED_CANDIDATE_MIN_RATIO * best_score
            ):
                break
            seed = roi_from_detection(face)
            if not seed.valid(width, height):
                continue
            result = self._infer(frame, seed)
            doubtful = result is None or result[1] < ROLL_RETRY_BELOW_LOGIT
            if (
                index == 0
                and doubtful
                and not 0.0 <= timestamp - self._roll_retry_ts < ROLL_RETRY_PERIOD_S
            ):
                rolled = self._better_rolled(
                    frame, seed, -math.inf if result is None else result[1]
                )
                if rolled is not None:
                    return rolled
                # Turning the crop did not help: the face is not merely rolled.
                self._roll_retry_ts = timestamp
            if result is not None:
                return result[0], seed, result[1]
        if holding:
            self._unreadable = roi_from_detection(ranked[0])
        return None

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

    def _settle(
        self,
        frame: np.ndarray,
        points: np.ndarray,
        used: Roi,
        logit: float,
        detections: list[np.ndarray] | None,
        *,
        reseed: bool,
    ) -> tuple[np.ndarray, bool, float]:
        """Re-run the network until its crop sits on the face.

        Returns the landmarks, whether their crop settled and the presence
        logit of the pass that produced them.

        ``points`` came from the crop ``used`` with the presence logit ``logit``.
        One pass on a crop chosen from the previous frame follows a face that
        moved by up to about 10 % of the crop almost completely, but after a
        larger jump (a posture shift between two frames at the idle frame
        rates) the landmarks stay biased towards the old position, upward jumps
        especially, while the network may still report a face. So:

        1. If the crop the landmarks ask for is close to ``used`` (within
           :data:`ROI_SETTLE_SHIFT`, or within :data:`ROI_CATCH_UP_SHIFT` with a
           confident presence logit, see :func:`crop_caught_up`), they are final
           for this frame. This is the steady state, also for a steadily moving
           head: no extra cost.
        2. Otherwise one more pass on that crop, which suffices for moderate
           jumps (about 10 ms).
        3. Still not caught up, or that crop was rejected: a large jump, which
           the network follows only a fraction of the way per pass. With
           ``reseed`` (tracking), YuNet finds the face independently of the old
           crop (the detection nearest the wanted crop, about 7 ms unless this
           frame already ran one) and the network runs on its crop and once more
           on the crop its landmarks ask for. Without (the face was just found
           by YuNet), one more plain pass.

        Each step keeps the previous landmarks when the network rejects the new
        crop, and a rejected crop is not tried twice. The crop counts as
        settled when the last pass's crop matched within the stricter
        :data:`ROI_SETTLE_SHIFT` (the result as a whole, see :attr:`settled`,
        only when that pass was also confident, see :data:`CONFIDENT_LOGIT`).
        """
        height, width = frame.shape[:2]
        wanted = roi_from_landmarks(points)
        if crop_caught_up(used, wanted, logit) or not wanted.valid(width, height):
            return points, crop_settled(used, wanted), logit
        second = self._infer(frame, wanted)
        if second is not None:
            points, used, logit = second[0], wanted, second[1]
            wanted = roi_from_landmarks(points)
            if crop_caught_up(used, wanted, logit) or not wanted.valid(width, height):
                return points, crop_settled(used, wanted), logit
        elif not reseed:
            return points, False, logit
        # Whether `wanted` still deserves a pass (the network just rejected it if not).
        last_pass = second is not None
        if reseed:
            if detections is None:
                detections = self._detect(frame, DETECT_WIDTH)
            nearby = [face for face in detections if _in_crop(face, wanted)]
            if nearby:
                face = min(
                    nearby,
                    key=lambda d: math.hypot(
                        _det_centre(d)[0] - wanted.cx, _det_centre(d)[1] - wanted.cy
                    ),
                )
                seed = roi_from_detection(face)
                third = self._infer(frame, seed) if seed.valid(width, height) else None
                if third is not None:
                    points, used, logit = third[0], seed, third[1]
                    wanted = roi_from_landmarks(points)
                    last_pass = True
        if last_pass and wanted.valid(width, height):
            last = self._infer(frame, wanted)
            if last is not None:
                points, used, logit = last[0], wanted, last[1]
        return points, crop_settled(used, roi_from_landmarks(points)), logit

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

    def _with_landmarks(
        self,
        points: np.ndarray,
        counted: list[np.ndarray] | None,
        timestamp: float,
        width: int,
        height: int,
        started: float,
    ) -> Observation:
        mesh = points[:_MESH_POINTS]
        x0, y0 = mesh.min(axis=0) / (width, height)
        x1, y1 = mesh.max(axis=0) / (width, height)
        face_box = (
            max(float(x0), 0.0),
            max(float(y0), 0.0),
            max(min(float(x1), 1.0) - max(float(x0), 0.0), 0.0),
            max(min(float(y1), 1.0) - max(float(y0), 0.0), 0.0),
        )
        others = self._other_faces(counted, mesh, width, height)
        eyes = eye_metrics(points)
        pose = self._pose.estimate(points, width, height)

        features: np.ndarray | None = None
        quality = 0.0
        blink = False
        if eyes is not None and pose is not None:
            vector = np.array(
                [
                    pose.yaw,
                    pose.pitch,
                    pose.roll,
                    pose.tx,
                    pose.ty,
                    pose.tz,
                    eyes.iris_h,
                    eyes.iris_v,
                ],
                dtype=np.float64,
            )
            if np.all(np.isfinite(vector)):
                features = vector
                touches_border = (
                    x0 <= BORDER_MARGIN
                    or y0 <= BORDER_MARGIN
                    or x1 >= 1.0 - BORDER_MARGIN
                    or y1 >= 1.0 - BORDER_MARGIN
                )
                quality = 0.5 if touches_border else 1.0
                blink = self._blink.update(eyes.openness)

        normalised = points / (width, height)
        self._landmarks = normalised
        self._last = _Drawing(
            timestamp,
            normalised,
            others,
            None if self._pose.rvec is None else self._pose.rvec.copy(),
            None if self._pose.tvec is None else self._pose.tvec.copy(),
            self._pose.camera.copy(),
        )
        return Observation(
            timestamp=timestamp,
            face_count=min(1 + len(others), self._max_faces),
            features=features,
            quality=quality,
            blink=blink,
            head_yaw=pose.yaw if pose is not None else None,
            head_pitch=pose.pitch if pose is not None else None,
            face_box=face_box,
            inference_ms=(time.perf_counter() - started) * 1e3,
            frame_size=(width, height),
        )

    # ------------------------------------------------------------- preview
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


def _clamp_faces(n: int) -> int:
    return max(1, min(int(n), MAX_FACES_LIMIT))

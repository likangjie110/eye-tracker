"""Pure geometry helpers for the face-mesh backend.

Eye metrics, blink detection, the rotated tracking crop, primary-face scoring
and head pose are functions of landmark pixels only; they are unit-tested
without the network. Nothing here imports :mod:`~.facemesh_backend`, so the
landmark network is never pulled in just to compute a pose.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from .face_geometry import FaceGeometry

log = logging.getLogger(__name__)


#: Side of the square network input in pixels.
INPUT_SIZE = 256

#: Crop side relative to the longer side of the face (MediaPipe uses 1.5).
ROI_SCALE = 1.5

#: A crop smaller than this (frame pixels) is too coarse to track.
MIN_ROI_SIDE = 24.0

#: How much less a face at the image corner counts than the same face at the
#: centre when choosing the primary face (the user usually sits in front of the
#: camera; posters, TVs and passers-by are usually off to the side). Mild on
#: purpose: size dominates, centrality decides between similar faces.
PRIMARY_CENTRE_WEIGHT = 0.3

#: A crop counts as settled on the face when the landmarks it produced ask for a
#: crop whose centre is at most this share of its side away ...
ROI_SETTLE_SHIFT = 0.04

#: ... and whose side differs by at most this share (still-face jitter stays
#: below about 0.01 for both).
ROI_SETTLE_SCALE = 0.035

#: A crop whose centre is further off than this share of its side (or whose side
#: is off by more than ROI_SETTLE_SCALE) is re-run within the same frame. It
#: pulls the landmarks towards where the face was: by up to 10° of pitch and 0.1
#: of iris travel after a 5-8 cm posture shift between two frames, and upward
#: jumps show up mostly as a changed side, because the network follows them only
#: a fraction of the way. Between the two shift limits the bias stays below
#: about 1.5° and the next frame removes it (the motion gate then does not hold
#: the observation, see ``VisionBackend.settled``), so a steadily moving head
#: costs no extra inference.
ROI_CATCH_UP_SHIFT = 0.08

#: ... provided that the network is sure it sees a face in the crop (a presence
#: logit of at least this). Far behind a large jump, an upward one especially,
#: the network follows the face only a fraction of the way per pass, so a crop
#: can ask for a shift below ROI_CATCH_UP_SHIFT while its landmarks are still
#: 35-45 px (12-20° of pitch) off. Measured on drawn faces after such jumps,
#: those crops scored a logit of 4-6, crops in the same band that were on the
#: face (landmarks within 1.5 px) 19-23; for photos see ROLL_RETRY_BELOW_LOGIT.
#: Without this, which of the two a jump ended on depended on pixel-level details
#: (a 1 px shift of the face, sensor noise, the CPU's floating-point rounding).
CATCH_UP_MIN_LOGIT = 10.0

#: Mean eye openness (lid gap / eye width) below which the eyes always count as
#: closed. Open eyes measure roughly 0.2-0.35.
BLINK_OPENNESS = 0.12

#: Once the user's own open-eye baseline is known, the blink threshold becomes
#: ``BLINK_BASELINE_RATIO * baseline``, clamped to [BLINK_MIN_OPENNESS,
#: BLINK_OPENNESS]: narrow eyes or a downward gaze never read as a blink.
BLINK_MIN_OPENNESS = 0.08
BLINK_BASELINE_RATIO = 0.5
#: Open-eye samples needed before the baseline is trusted, and how many are kept.
BLINK_BASELINE_MIN_SAMPLES = 30
BLINK_BASELINE_WINDOW = 90

_NUM_LANDMARKS = 478
_MESH_POINTS = 468
_MIN_EYE_WIDTH_PX = 1.5

# Per eye: (first corner, second corner, iris centre, upper lid, lower lid).
# Landmarks are numbered anatomically, and in a camera image the subject's
# right eye (33/133) appears on the left. Taking 33 -> 133 for one eye and
# 362 -> 263 for the other makes both corner axes point towards image-right, so
# the iris coordinate moves in the same direction in both eyes and can be
# averaged.
EYE_A = (33, 133, 468, 159, 145)
EYE_B = (362, 263, 473, 386, 374)

#: Outer eye corners on the image-left and image-right side: the eye line.
_EYE_LINE = (33, 263)

# ----------------------------------------------------------------------------
# Pure helpers (unit-tested without the network)


@dataclass(frozen=True, slots=True)
class EyeMetrics:
    """Iris position and lid opening, averaged over both eyes.

    ``iris_h`` is the iris centre projected onto the corner-to-corner axis
    (0 at the image-left corner, 1 at the image-right corner, ~0.5 looking
    straight). ``iris_v`` is the signed distance from that axis in eye widths
    (positive = towards the bottom of the image). ``openness`` is the lid gap in
    eye widths.
    """

    iris_h: float
    iris_v: float
    openness: float


def eye_metrics(points_px: np.ndarray) -> EyeMetrics | None:
    """Compute :class:`EyeMetrics` from landmark positions in pixels.

    ``points_px`` has shape ``(478, 2)`` (or more rows / columns). Pixel units
    matter: normalised landmark coordinates are anisotropic for non-square
    images. Returns ``None`` when the eyes are degenerate (too small).
    """
    if points_px.shape[0] < _NUM_LANDMARKS:
        return None
    h_values: list[float] = []
    v_values: list[float] = []
    openness: list[float] = []
    for c0, c1, iris, lid_top, lid_bottom in (EYE_A, EYE_B):
        a = points_px[c0, :2]
        axis = points_px[c1, :2] - a
        width = float(math.hypot(axis[0], axis[1]))
        if not width >= _MIN_EYE_WIDTH_PX:  # also rejects NaN
            return None
        u = axis / width
        normal = np.array([-u[1], u[0]])  # image-down for an upright face
        rel = points_px[iris, :2] - a
        h_values.append(float(rel @ u) / width)
        v_values.append(float(rel @ normal) / width)
        lid = points_px[lid_top, :2] - points_px[lid_bottom, :2]
        openness.append(float(math.hypot(lid[0], lid[1])) / width)
    return EyeMetrics(
        iris_h=sum(h_values) / 2.0,
        iris_v=sum(v_values) / 2.0,
        openness=sum(openness) / 2.0,
    )


class BlinkDetector:
    """Decides whether the eyes are closed, relative to the user's own eyes.

    A fixed openness threshold misfires for narrow eyes and for a downward gaze
    (the lids close in when looking at a monitor below the camera). The detector
    keeps a rolling median of open-eye readings and, once it has enough of them,
    flags a blink below half of that baseline. The threshold never rises above
    the absolute :data:`BLINK_OPENNESS`, so wide eyes behave as before.
    """

    def __init__(
        self,
        absolute: float = BLINK_OPENNESS,
        minimum: float = BLINK_MIN_OPENNESS,
        ratio: float = BLINK_BASELINE_RATIO,
        min_samples: int = BLINK_BASELINE_MIN_SAMPLES,
        window: int = BLINK_BASELINE_WINDOW,
    ) -> None:
        self.absolute = float(absolute)
        self.minimum = float(minimum)
        self.ratio = float(ratio)
        self.min_samples = max(1, int(min_samples))
        self._history: deque[float] = deque(maxlen=max(self.min_samples, int(window)))

    @property
    def threshold(self) -> float:
        """Current openness threshold below which the eyes count as closed."""
        if len(self._history) < self.min_samples:
            return self.absolute
        baseline = float(np.median(np.fromiter(self._history, dtype=np.float64)))
        return min(self.absolute, max(self.minimum, self.ratio * baseline))

    def update(self, openness: float) -> bool:
        """Classify one reading (``True`` = closed) and learn from open eyes.

        Until the baseline exists every finite reading is learnt, not only those
        above the absolute threshold: eyes that stay below it (narrow eyes, a
        monitor below the camera) would otherwise never teach the detector
        anything and read as closed forever. The median shrugs off the few blinks
        among them. Afterwards only open eyes are learnt, so a long stretch of
        closed eyes cannot drag the baseline down.
        """
        if not math.isfinite(openness):
            return False
        closed = openness < self.threshold
        if not closed or len(self._history) < self.min_samples:
            self._history.append(openness)
        return closed

    def reset(self) -> None:
        self._history.clear()


@dataclass(frozen=True, slots=True)
class Roi:
    """A square, rotated crop of the frame that the landmark network sees.

    ``(cx, cy)`` is the centre and ``side`` the side length in frame pixels;
    ``angle`` is the direction of the eye line in degrees (image coordinates,
    y down), which the crop rotates to horizontal.
    """

    cx: float
    cy: float
    side: float
    angle: float

    def transform(self) -> np.ndarray:
        """2x3 affine matrix mapping frame pixels to network-input pixels."""
        # Positive angles rotate counter-clockwise on screen, which levels an eye
        # line that descends towards image-right by that angle.
        m = cv2.getRotationMatrix2D((self.cx, self.cy), self.angle, INPUT_SIZE / self.side)
        m[0, 2] += INPUT_SIZE / 2.0 - self.cx
        m[1, 2] += INPUT_SIZE / 2.0 - self.cy
        return m

    def valid(self, width: int, height: int) -> bool:
        """True when the crop is finite, large enough and centred inside the frame."""
        return (
            all(math.isfinite(v) for v in (self.cx, self.cy, self.side, self.angle))
            and self.side >= MIN_ROI_SIDE
            and 0.0 <= self.cx < width
            and 0.0 <= self.cy < height
        )


def _eye_line_angle(left: np.ndarray, right: np.ndarray) -> float:
    return math.degrees(math.atan2(float(right[1] - left[1]), float(right[0] - left[0])))


def roi_from_detection(face: np.ndarray) -> Roi:
    """Crop for a YuNet detection row (pixels: box, right eye, left eye, ...)."""
    x, y, w, h = (float(v) for v in face[:4])
    # YuNet's "right eye" is the one on the image left.
    angle = _eye_line_angle(face[4:6], face[6:8])
    return Roi(x + w / 2.0, y + h / 2.0, max(w, h) * ROI_SCALE, angle)


def roi_from_landmarks(points_px: np.ndarray) -> Roi:
    """Crop for tracking a face into the next frame (MediaPipe's rule)."""
    mesh = points_px[:_MESH_POINTS, :2]
    x0, y0 = mesh.min(axis=0)
    x1, y1 = mesh.max(axis=0)
    angle = _eye_line_angle(points_px[_EYE_LINE[0]], points_px[_EYE_LINE[1]])
    side = max(float(x1 - x0), float(y1 - y0)) * ROI_SCALE
    return Roi(float(x0 + x1) / 2.0, float(y0 + y1) / 2.0, side, angle)


def crop_settled(
    used: Roi, wanted: Roi, shift: float = ROI_SETTLE_SHIFT, scale: float = ROI_SETTLE_SCALE
) -> bool:
    """True when ``wanted`` (the crop the landmarks ask for) matches ``used``.

    Landmarks from a crop that is centred on the face are trustworthy; when the
    face sits off-centre in the crop (it moved since the crop was chosen) they
    are pulled towards the crop centre, and the crop they ask for is off by a
    noticeable share of its side. ``shift`` and ``scale`` are the tolerated
    centre offset and side change, as shares of ``used.side`` (see
    :data:`ROI_SETTLE_SHIFT`).
    """
    if not (used.side > 0.0 and all(math.isfinite(v) for v in (wanted.cx, wanted.cy, wanted.side))):
        return False
    offset = math.hypot(wanted.cx - used.cx, wanted.cy - used.cy) / used.side
    return offset <= shift and abs(wanted.side / used.side - 1.0) <= scale


def crop_caught_up(used: Roi, wanted: Roi, logit: float) -> bool:
    """True when a frame's landmark passes may stop at the crop ``used``.

    That is when it is settled on the face (:func:`crop_settled`), or when it is
    catching up with a moving face (``wanted`` within :data:`ROI_CATCH_UP_SHIFT`)
    and the network is confident that it sees a face in it (``logit``, the
    presence logit of the pass on ``used``, of at least
    :data:`CATCH_UP_MIN_LOGIT`). See ``FaceMeshBackend._settle``.
    """
    if crop_settled(used, wanted):
        return True
    return logit >= CATCH_UP_MIN_LOGIT and crop_settled(used, wanted, shift=ROI_CATCH_UP_SHIFT)


def primary_score(side: float, cx: float, cy: float, width: int, height: int) -> float:
    """How strongly a face of ``side`` pixels centred at ``(cx, cy)`` claims to be the user.

    The face side, reduced by up to :data:`PRIMARY_CENTRE_WEIGHT` the further the
    face is from the image centre (0 at the centre, the full weight at a corner).
    """
    half_w, half_h = width / 2.0, height / 2.0
    offset = math.hypot((cx - half_w) / half_w, (cy - half_h) / half_h) / math.sqrt(2.0)
    return side * (1.0 - PRIMARY_CENTRE_WEIGHT * min(offset, 1.0))


@dataclass(frozen=True, slots=True)
class HeadPose:
    """Head rotation (degrees) and position (centimetres), see the module docs."""

    yaw: float
    pitch: float
    roll: float
    tx: float
    ty: float
    tz: float


def pose_from_rotation(rotation: np.ndarray, translation: np.ndarray | Sequence[float]) -> HeadPose:
    """:class:`HeadPose` from a model-to-camera rotation matrix and translation.

    The model frame is the canonical face flipped to OpenCV's camera convention
    (x right, y down, z away from the camera), so a face looking straight into
    the camera has the identity rotation.
    """
    r = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    nose = -r[:, 2]  # the face's forward direction, towards the camera when frontal
    yaw = math.degrees(math.atan2(nose[0], -nose[2]))
    pitch = math.degrees(math.atan2(nose[1], math.hypot(nose[0], nose[2])))
    # Direction of the model x axis in the image; image y points down, so a
    # counter-clockwise tilt lifts its right end (negative y).
    roll = math.degrees(math.atan2(-r[1, 0], r[0, 0]))
    tx, ty, tz = (float(v) for v in np.asarray(translation, dtype=np.float64).ravel()[:3])
    return HeadPose(yaw, pitch, roll, tx, ty, tz)


def camera_matrix(width: int, height: int) -> np.ndarray:
    """Approximate pinhole intrinsics: focal length = longer side, centred."""
    f = float(max(width, height))
    return np.array([[f, 0.0, width / 2.0], [0.0, f, height / 2.0], [0.0, 0.0, 1.0]])


class PoseEstimator:
    """Head pose by fitting the canonical face mesh to the landmarks."""

    def __init__(self, geometry: FaceGeometry) -> None:
        ids = np.array(geometry.basis_ids, dtype=np.intp)
        self._ids = ids
        # Canonical frame (x right, y up, z towards the camera) -> OpenCV-style
        # object frame (x right, y down, z away from the camera).
        self._object = np.ascontiguousarray(geometry.vertices[ids] * (1.0, -1.0, -1.0))
        self._rvec: np.ndarray | None = None
        self._tvec: np.ndarray | None = None
        self._size: tuple[int, int] | None = None
        self.camera: np.ndarray = camera_matrix(1, 1)

    @property
    def rvec(self) -> np.ndarray | None:
        return self._rvec

    @property
    def tvec(self) -> np.ndarray | None:
        return self._tvec

    def reset(self) -> None:
        """Forget the previous pose (a new face or a new frame size)."""
        self._rvec = None
        self._tvec = None

    def estimate(self, points_px: np.ndarray, width: int, height: int) -> HeadPose | None:
        """Pose for ``(478, 2)`` landmark pixels in a ``width`` x ``height`` frame."""
        if (width, height) != self._size:
            self._size = (width, height)
            self.camera = camera_matrix(width, height)
            self.reset()
        image = np.ascontiguousarray(points_px[self._ids, :2], dtype=np.float64)
        if not np.all(np.isfinite(image)):
            self.reset()
            return None
        solution = None
        if self._rvec is not None and self._tvec is not None:
            solution = self._solve(image, cv2.SOLVEPNP_ITERATIVE, self._rvec, self._tvec)
        if solution is None:
            # A global solver for a fresh face (or when the tracked solution
            # drifted into an implausible one).
            solution = self._solve(image, cv2.SOLVEPNP_SQPNP)
        if solution is None:
            self.reset()
            return None
        self._rvec, self._tvec = solution
        rotation, _ = cv2.Rodrigues(self._rvec)
        return pose_from_rotation(rotation, self._tvec)

    def _solve(
        self,
        image: np.ndarray,
        method: int,
        rvec: np.ndarray | None = None,
        tvec: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray] | None:
        guess = rvec is not None and tvec is not None
        try:
            ok, r, t = cv2.solvePnP(
                self._object,
                image,
                self.camera,
                None,
                rvec.copy() if rvec is not None else None,
                tvec.copy() if tvec is not None else None,
                useExtrinsicGuess=guess,
                flags=method,
            )
        except cv2.error:
            log.debug("solvePnP failed", exc_info=True)
            return None
        if not ok or not np.all(np.isfinite(r)) or not np.all(np.isfinite(t)):
            return None
        rotation, _ = cv2.Rodrigues(r)
        # In front of the camera and facing it (nose towards the lens).
        if float(t.ravel()[2]) <= 0.0 or float(rotation[2, 2]) <= 0.0:
            return None
        return r.reshape(3, 1), t.reshape(3, 1)

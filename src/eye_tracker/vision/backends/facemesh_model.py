"""Loading and self-testing the MediaPipe face landmark TFLite network.

The network runs through OpenCV's DNN module (see :mod:`~.facemesh_backend`).
Every load is checked against one fixed input with recorded reference outputs,
so a runtime that computes the wrong tensors becomes a clean
``BackendUnavailable`` instead of garbage landmarks.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np

from .base import BackendUnavailable
from .facemesh_geometry import _NUM_LANDMARKS, EYE_A, INPUT_SIZE

log = logging.getLogger(__name__)


_OUTPUT_NAMES = ["Identity", "Identity_1"]  # landmarks, face-presence logit

# ----------------------------------------------------------------------------
# Model loading


def _read_tflite(model: str | np.ndarray) -> cv2.dnn.Net:
    # OpenCV 5 defaults to its new graph engine, which cannot deliver this
    # network's named outputs correctly; the classic engine is exact. OpenCV 4.x
    # only has the classic engine (and no `engine` argument).
    if hasattr(cv2.dnn, "ENGINE_CLASSIC"):
        return cv2.dnn.readNetFromTFLite(model, engine=cv2.dnn.ENGINE_CLASSIC)
    return cv2.dnn.readNetFromTFLite(model)


def _windows_short_path(path: Path) -> str | None:
    """The 8.3 alias of ``path`` if Windows provides a pure-ASCII one."""
    if sys.platform != "win32":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    get_short = ctypes.windll.kernel32.GetShortPathNameW  # type: ignore[attr-defined]
    length = int(get_short(str(path), buffer, len(buffer)))
    if 0 < length < len(buffer) and buffer.value.isascii():
        return str(buffer.value)
    return None


def self_test_blob() -> np.ndarray:
    """The fixed input of the load-time self-test: a different gradient per channel.

    Asymmetric on purpose, so that a runtime that mixes up the tensor layout or
    the channel order produces visibly different landmarks.
    """
    ramp = np.linspace(0.0, 1.0, INPUT_SIZE, dtype=np.float32)
    xx, yy = np.meshgrid(ramp, ramp)
    return np.stack([xx, yy, (xx + yy) / 2.0])[None]


#: Reference outputs for :func:`self_test_blob`, recorded with OpenCV 5.0's
#: classic engine (MediaPipe's own runtime agrees to within 0.005 of the frame
#: on real faces): presence logit, mean landmark (x, y) and iris 468 (x, y) in
#: network-input pixels.
_SELF_TEST_FLAG = -8.30
_SELF_TEST_MEAN = (127.64, 116.29)
_SELF_TEST_IRIS = (95.10, 85.45)
_SELF_TEST_PX_TOLERANCE = 2.0
_SELF_TEST_FLAG_TOLERANCE = 1.5


def _check_landmark_net(net: cv2.dnn.Net) -> None:
    """Make sure the network runs and computes what it should.

    Some OpenCV engines load the model but return the wrong tensors (OpenCV
    5.0's new engine, for example); comparing one fixed input against reference
    values turns such a runtime into a clean "unavailable" instead of garbage
    landmarks.

    Raises:
        ValueError: The outputs have the wrong shape or values.
    """
    net.setInput(self_test_blob())
    landmarks, flag = net.forward(_OUTPUT_NAMES)
    if landmarks.size != _NUM_LANDMARKS * 3 or flag.size != 1:
        raise ValueError(
            f"unexpected output shapes {landmarks.shape} and {flag.shape} (not the landmark model)"
        )
    points = landmarks.reshape(-1, 3)[:, :2].astype(np.float64)
    mean = points.mean(axis=0)
    iris = points[EYE_A[2]]
    logit = float(flag.ravel()[0])
    worst = max(
        float(np.abs(mean - _SELF_TEST_MEAN).max()), float(np.abs(iris - _SELF_TEST_IRIS).max())
    )
    if not (
        worst <= _SELF_TEST_PX_TOLERANCE
        and abs(logit - _SELF_TEST_FLAG) <= _SELF_TEST_FLAG_TOLERANCE
    ):
        raise ValueError(
            f"the model computes wrong values (landmarks off by {worst:.1f} px, "
            f"presence logit {logit:.2f} instead of {_SELF_TEST_FLAG:.2f})"
        )


def load_landmark_net(path: Path) -> cv2.dnn.Net:
    """Load and self-test the landmark network.

    OpenCV opens file paths with the ANSI code page on Windows, so non-ASCII
    paths fail there; in-memory loading avoids that, but OpenCV 5.0 ignores the
    engine choice for in-memory models (and its default engine cannot run this
    network). The loaders are therefore tried in order: the path itself (unless
    it is non-ASCII on Windows), the in-memory model, and the Windows 8.3 short
    path. Each result must pass :func:`_check_landmark_net`.

    Raises:
        BackendUnavailable: The model is missing or cannot be run correctly.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BackendUnavailable(
            f"Face landmark model not found at {path} (run scripts/fetch_models.py)"
        ) from exc
    loaders: list[tuple[str, Callable[[], cv2.dnn.Net]]] = []
    text = str(path)
    windows_unsafe = sys.platform == "win32" and not text.isascii()
    if not windows_unsafe:
        loaders.append(("path", lambda: _read_tflite(text)))
    loaders.append(("memory", lambda: _read_tflite(np.frombuffer(data, dtype=np.uint8))))
    if windows_unsafe:
        short = _windows_short_path(path)
        if short is not None:
            loaders.append(("short path", lambda: _read_tflite(short)))
    errors: list[str] = []
    for label, load in loaders:
        try:
            net = load()
            _check_landmark_net(net)
        except (cv2.error, ValueError, TypeError) as exc:
            detail = str(exc).strip().splitlines()[-1] if str(exc).strip() else type(exc).__name__
            errors.append(f"{label}: {detail}")
            log.debug("Loading the landmark model via %s failed", label, exc_info=True)
            continue
        return net
    hint = (
        " (moving the app to a folder with an ASCII-only path may help)" if windows_unsafe else ""
    )
    raise BackendUnavailable(
        f"OpenCV {cv2.__version__} cannot run the face landmark model{hint}: " + "; ".join(errors)
    )

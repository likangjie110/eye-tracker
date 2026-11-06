"""Low-level OpenCV capture helpers shared by the frame sources.

Capture API selection, FOURCC (pixel format) helpers, capture-property writes,
frame validation and image decoding live here, so that
:mod:`eye_tracker.vision.camera` can concentrate on the :class:`Camera` and
:class:`FileSource` lifecycles. Nothing here opens a device or writes image data
on import.

The dependency runs in one direction only: this module must not import
:mod:`eye_tracker.vision.camera`, which keeps the import graph acyclic.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

#: File suffixes treated as still images by ``FileSource``.
IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".pgm", ".ppm", ".pbm"}
)

_API_BY_NAME: dict[str, int] = {
    "any": cv2.CAP_ANY,
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "avfoundation": cv2.CAP_AVFOUNDATION,
    "v4l2": cv2.CAP_V4L2,
}


def api_preference(api: str) -> int:
    """Map a capture API name from the settings to an OpenCV ``CAP_*`` constant.

    ``"auto"`` picks the API that opens fastest and supports MJPG on each OS:
    DirectShow on Windows (Media Foundation can take many seconds to open),
    AVFoundation on macOS and V4L2 on Linux.
    """
    key = api.strip().lower()
    if key == "auto":
        if sys.platform == "win32":
            return cv2.CAP_DSHOW
        if sys.platform == "darwin":
            return cv2.CAP_AVFOUNDATION
        if sys.platform.startswith("linux"):
            return cv2.CAP_V4L2
        return cv2.CAP_ANY
    try:
        return _API_BY_NAME[key]
    except KeyError:
        choices = ", ".join(["auto", *_API_BY_NAME])
        raise ValueError(f"Unknown camera API {api!r}; expected one of {choices}") from None


def _fourcc(code: str) -> int:
    # Computed by hand: the equivalent OpenCV helper lives on the video writer
    # class, which this package deliberately never references.
    a, b, c, d = (ord(ch) for ch in code)
    return a | (b << 8) | (c << 16) | (d << 24)


def _fourcc_name(cap: cv2.VideoCapture) -> str:
    """The pixel format the driver reports, e.g. ``"MJPG"`` or ``"YUY2"`` (diagnostics)."""
    try:
        code = int(cap.get(cv2.CAP_PROP_FOURCC))
    except (cv2.error, ValueError, OverflowError):
        return "?"
    text = "".join(chr((code >> shift) & 0xFF) for shift in (0, 8, 16, 24))
    return text if text.isprintable() and text.strip() else f"0x{code:08x}"


def _backend_name(cap: cv2.VideoCapture) -> str:
    try:
        return str(cap.getBackendName())
    except cv2.error:
        return "?"


def _try_set(cap: cv2.VideoCapture, prop: int, value: float) -> None:
    # Drivers silently ignore properties they do not support; some raise instead.
    with contextlib.suppress(cv2.error):
        cap.set(prop, value)


def _valid_frame(frame: np.ndarray | None) -> bool:
    return frame is not None and frame.size > 0


def _imread(path: Path) -> np.ndarray | None:
    # cv2.imread cannot open non-ASCII paths on Windows; decoding bytes read by
    # Python works everywhere.
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError as exc:
        log.debug("Could not read %s: %s", path, exc)
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


_DEV_VIDEO = re.compile(r"^/dev/video(\d+)$")


class CameraError(RuntimeError):
    """A frame source could not be created (for example a missing video file)."""


@dataclass
class CameraInfo:
    """A camera found by :func:`list_cameras`."""

    index: int
    name: str
    width: int
    height: int


def device_index(device: str) -> int | None:
    """The camera index a ``camera.device`` setting selects right now, or ``None``.

    ``"0"``, ``"1"``, … and, on Linux, ``/dev/videoN`` or a stable link to one
    (``/dev/v4l/by-id/…``, resolved now). ``None`` for a video or image file and
    for a device path that does not lead to a camera. Never raises.
    """
    spec = device.strip()
    if spec.isdigit():
        return int(spec)
    if sys.platform.startswith("linux") and spec.startswith("/dev/") and not os.path.isfile(spec):
        try:
            return _v4l2_index(spec)
        except (CameraError, OSError, ValueError):
            return None
    return None


def _resolve_link(path: str) -> str:
    """``os.path.realpath`` (replaceable in tests)."""
    return os.path.realpath(path)


def _v4l2_index(spec: str) -> int:
    """Camera index of a Linux device path: ``/dev/videoN`` or a link to one.

    Raises:
        CameraError: The path is missing or not a V4L2 capture node.
    """
    match = _DEV_VIDEO.match(spec)
    if match is None:
        target = _resolve_link(spec)
        match = _DEV_VIDEO.match(target)
        if match is None:
            if not os.path.exists(spec):
                raise CameraError(f"Video device not found: {spec} (is the camera plugged in?)")
            raise CameraError(f"Not a V4L2 video device: {spec} (resolves to {target})")
    return int(match.group(1))


def list_cameras(
    max_index: int = 4, api: str = "auto", skip: Collection[int] = ()
) -> list[CameraInfo]:
    """Probe camera indices ``0 … max_index - 1`` and return those that deliver frames.

    Each camera is opened briefly (its activity light may flash). This can take
    several seconds; call it from a background thread. A camera currently held
    by another application may be missing.

    ``skip`` lists indices that must not be probed, above all the camera this
    app's own worker is using: on DirectShow, probing an open camera "succeeds"
    and releasing the probe then stops the device under the worker.
    """
    pref = api_preference(api)
    excluded = {int(i) for i in skip}
    found: list[CameraInfo] = []
    for index in range(max(0, max_index)):
        if index in excluded:
            continue
        cap: cv2.VideoCapture | None = None
        try:
            cap = cv2.VideoCapture(index, pref)
            if not cap.isOpened():
                continue
            ok, frame = cap.read()
            if not ok or not _valid_frame(frame):
                log.debug("Camera %d opened but returned no frame", index)
                continue
            h, w = frame.shape[:2]
            found.append(CameraInfo(index=index, name=f"Camera {index}", width=w, height=h))
        except cv2.error as exc:
            log.debug("Probing camera %d failed: %s", index, exc)
        finally:
            if cap is not None:
                cap.release()
    return found

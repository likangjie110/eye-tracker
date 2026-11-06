"""Frame sources: webcams through OpenCV and video/image files for tests and demos.

Every source hands out BGR ``uint8`` frames that live only in memory. Nothing in
this module writes image data anywhere.

Sources are used from the vision worker thread. ``open()`` may block for a few
seconds (some Windows camera drivers are slow to start) but never raises;
failures are reported through ``last_error``.

Offline guarantee: :func:`open_source` and :class:`FileSource` refuse URLs and
other protocol specifications, so only local files are ever opened, and video
files are read by OpenCV's FFmpeg backend alone. FFmpeg only lets a local file
reference local data (its protocol whitelist for ``file`` inputs is
``file,crypto,data``), so a playlist or SDP file cannot pull a network stream
either; other backends (GStreamer in distribution builds of OpenCV, Media
Foundation) are never asked, because they would follow such references.
"""

from __future__ import annotations

import logging
import math
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

import cv2
import numpy as np

from .camera_devices import (
    IMAGE_SUFFIXES,
    CameraError,
    CameraInfo,
    _backend_name,
    _fourcc,
    _fourcc_name,
    _imread,
    _try_set,
    _v4l2_index,
    _valid_frame,
    api_preference,
    device_index,
    list_cameras,
)

__all__ = [
    "Camera",
    "CameraError",
    "CameraInfo",
    "FileSource",
    "FrameSource",
    "api_preference",
    "device_index",
    "list_cameras",
    "open_source",
]

log = logging.getLogger(__name__)

# A camera that fails this many reads in a row, or keeps failing for this long,
# is considered gone (unplugged, taken over by another application or its
# driver crashed). The time bound matters for drivers whose reads block for
# seconds before failing (Media Foundation, V4L2 select timeouts).
_MAX_READ_FAILURES = 5
_MAX_FAILURE_S = 3.0
# Reads attempted while opening; the first frames of some cameras are empty.
_WARMUP_READS = 5
# YUY2 bandwidth (bytes/s) above which DirectShow is asked for MJPG: USB 2
# cameras cannot deliver more uncompressed, while below it the uncompressed
# format is cheaper to handle than DirectShow's MJPEG decoder.
_DSHOW_MJPG_ABOVE_BYTES_S = 20e6

_API_BY_NAME: dict[str, int] = {
    "any": cv2.CAP_ANY,
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "avfoundation": cv2.CAP_AVFOUNDATION,
    "v4l2": cv2.CAP_V4L2,
}

# What FFmpeg would open as a protocol rather than a file (see its
# url_find_protocol): "<scheme>:…" where the scheme is letters, digits, "+", "-"
# or "." ("rtsp://…", "http:…", "tcp:…", "concat:…"), except a single drive
# letter ("C:\…"); "subfile,…:" wraps another protocol; and "://" anywhere
# catches nested URLs.
_URL_LIKE = re.compile(r"^(?:[A-Za-z0-9+.\-]{2,}:|subfile,)|://", re.IGNORECASE)

#: OpenCV's FFmpeg backend reads this environment variable when a capture opens.
_FFMPEG_OPTIONS = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
#: Keep FFmpeg on local protocols: a local playlist/container must not pull a URL.
_FFMPEG_PROTOCOLS = "protocol_whitelist;file,crypto,data"


def _restrict_ffmpeg_protocols() -> None:
    """Append FFmpeg's file-only protocol whitelist to the capture options (TD-SEC-012)."""
    current = os.environ.get(_FFMPEG_OPTIONS, "")
    if "protocol_whitelist" in current:
        return
    os.environ[_FFMPEG_OPTIONS] = f"{current}|{_FFMPEG_PROTOCOLS}" if current else _FFMPEG_PROTOCOLS


if sys.platform.startswith("linux"):
    # V4L2 waits up to 10 s (the default) for each frame of a stalled camera,
    # during which the worker cannot release it. OpenCV reads this on the first
    # capture, so setting it at import time is early enough.
    os.environ.setdefault("OPENCV_VIDEOIO_V4L_SELECT_TIMEOUT", "2")


@runtime_checkable
class FrameSource(Protocol):
    """Anything that produces BGR frames for the vision worker."""

    def open(self) -> bool:
        """Start capturing. Returns ``False`` (and sets ``last_error``) on failure."""
        ...

    def read(self) -> np.ndarray | None:
        """Return the most recent BGR frame, or ``None`` if none is available."""
        ...

    def release(self) -> None:
        """Stop capturing and free the device. Safe to call more than once."""
        ...

    @property
    def is_open(self) -> bool: ...

    @property
    def last_error(self) -> str | None: ...


class Camera(FrameSource):
    """A webcam opened through ``cv2.VideoCapture``.

    Args:
        index: Device index (``0`` is the default camera).
        width, height: Requested capture size. Small sizes keep USB bandwidth and
            decoding cost low; the backends downscale further anyway.
        api: Capture API name, see :func:`api_preference`.
        fps: Requested camera frame rate. Some drivers convert every captured
            frame, so asking for fewer frames saves CPU even between reads.
        flush: Buffered frames to drop before a read that follows a pause, so
            that a slow reader never analyses a stale picture.
    """

    def __init__(
        self,
        index: int,
        width: int = 640,
        height: int = 480,
        api: str = "auto",
        fps: int = 15,
        flush: int = 1,
    ) -> None:
        if index < 0:
            raise ValueError(f"camera index must be >= 0, got {index}")
        self.index = int(index)
        self.width = int(width)
        self.height = int(height)
        self.api = api
        self.fps = max(1, int(fps))
        self.flush = max(0, int(flush))
        #: The settings string this camera was configured with (``"0"``,
        #: ``"/dev/v4l/by-id/…"``); identifies the camera for calibrations.
        self.device = str(self.index)
        self._api_pref = api_preference(api)
        self._cap: cv2.VideoCapture | None = None
        self._error: str | None = None
        self._failures = 0
        self._fail_since: float | None = None
        self._last_read = -math.inf
        self._frame_size: tuple[int, int] | None = None

    # ------------------------------------------------------------------ info
    @property
    def name(self) -> str:
        return f"Camera {self.index}"

    @property
    def is_open(self) -> bool:
        return self._cap is not None

    @property
    def last_error(self) -> str | None:
        return self._error

    @property
    def frame_size(self) -> tuple[int, int] | None:
        """``(width, height)`` of the delivered frames once the camera is open."""
        return self._frame_size

    # ------------------------------------------------------------- lifecycle
    def open(self) -> bool:
        if self._cap is not None:
            return True
        self._failures = 0
        self._fail_since = None
        started = time.monotonic()
        # The OS privacy switches (Windows "Let desktop apps access your camera",
        # macOS camera access) make opening fail exactly like a busy camera.
        unavailable = (
            f"{self.name} could not be opened — it may be in use by another app "
            "or blocked by the system's camera privacy settings"
        )
        try:
            cap = cv2.VideoCapture(self.index, self._api_pref)
        except Exception as exc:  # open() must never raise; drivers fail in odd ways
            log.debug("VideoCapture(%d) raised: %s", self.index, exc)
            self._error = unavailable
            return False
        if not cap.isOpened():
            cap.release()
            self._error = unavailable
            log.debug("%s did not open (%.0f ms)", self.name, (time.monotonic() - started) * 1e3)
            return False

        if self._api_pref == cv2.CAP_DSHOW:
            self._configure_dshow(cap)
        else:
            # FOURCC first: on V4L2 changing the pixel format afterwards can reset
            # the frame size. MJPG lets USB 2 cameras deliver 640x480 at full
            # rate; cameras without MJPG simply keep their default format.
            _try_set(cap, cv2.CAP_PROP_FOURCC, _fourcc("MJPG"))
            _try_set(cap, cv2.CAP_PROP_FRAME_WIDTH, self.width)
            _try_set(cap, cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            _try_set(cap, cv2.CAP_PROP_FPS, self.fps)
        _try_set(cap, cv2.CAP_PROP_BUFFERSIZE, 1)

        # Some drivers report success on open but never deliver a frame when another
        # application holds the device, so only a real frame counts as success.
        frame: np.ndarray | None = None
        for _ in range(_WARMUP_READS):
            try:
                ok, frame = cap.read()
            except Exception as exc:
                log.debug("%s warm-up read raised: %s", self.name, exc)
                ok, frame = False, None
            if ok and _valid_frame(frame):
                break
            frame = None
        if frame is None:
            cap.release()
            self._error = (
                f"{self.name} opened but delivers no frames — it may be in use by another "
                "app or blocked by the system's camera privacy settings"
            )
            log.debug("%s delivered no frames", self.name)
            return False

        self._cap = cap
        self._error = None
        self._last_read = time.monotonic()
        self._frame_size = (int(frame.shape[1]), int(frame.shape[0]))
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "%s opened via %s at %dx%d (%s) in %.0f ms",
                self.name,
                _backend_name(cap),
                self._frame_size[0],
                self._frame_size[1],
                _fourcc_name(cap),
                (time.monotonic() - started) * 1e3,
            )
        return True

    def _configure_dshow(self, cap: cv2.VideoCapture) -> None:
        """Apply size, rate and format with as few DirectShow graph rebuilds as possible.

        OpenCV's DirectShow backend rebuilds the capture graph for every format
        change, and a size or rate change renegotiates the pixel format from
        scratch (dropping a previously chosen MJPG). So only values that differ
        are set, size before rate, and the MJPG request comes last (DirectShow
        keeps the current size and rate when changing the format) and only when
        uncompressed frames would exceed USB 2 bandwidth.
        """

        def current(prop: int) -> float:
            try:
                return float(cap.get(prop))
            except cv2.error:
                return 0.0

        size = (round(current(cv2.CAP_PROP_FRAME_WIDTH)), round(current(cv2.CAP_PROP_FRAME_HEIGHT)))
        if size != (self.width, self.height):
            _try_set(cap, cv2.CAP_PROP_FRAME_WIDTH, self.width)
            _try_set(cap, cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if round(current(cv2.CAP_PROP_FPS)) != self.fps:
            _try_set(cap, cv2.CAP_PROP_FPS, self.fps)
        if self.width * self.height * 2 * self.fps > _DSHOW_MJPG_ABOVE_BYTES_S:
            _try_set(cap, cv2.CAP_PROP_FOURCC, _fourcc("MJPG"))

    def read(self) -> np.ndarray | None:
        cap = self._cap
        if cap is None:
            return None
        frame: np.ndarray | None = None
        ok = False
        try:
            # Drop buffered frames only after a pause longer than ~1.5 frame periods.
            # When reading at camera speed nothing stale can be queued, and grabbing
            # anyway would block for an extra frame period and halve the frame rate.
            # Never while failing: a stalled driver would block once per grab.
            flushed = True
            paused = time.monotonic() - self._last_read > 1.5 / self.fps
            if self.flush and paused and self._failures == 0:
                for _ in range(self.flush):
                    if not cap.grab():
                        flushed = False
                        break
            if flushed:
                ok, frame = cap.read()
        except Exception as exc:
            log.debug("%s read raised: %s", self.name, exc)
        now = time.monotonic()
        self._last_read = now

        if not ok or not _valid_frame(frame):
            self._failures += 1
            if self._fail_since is None:
                self._fail_since = now
            if self._failures >= _MAX_READ_FAILURES or now - self._fail_since >= _MAX_FAILURE_S:
                self._error = (
                    f"{self.name} stopped delivering frames — was it unplugged or taken "
                    "by another app?"
                )
                log.debug("%s stopped delivering frames; closing it", self.name)
                self.release()
            return None
        self._failures = 0
        self._fail_since = None
        return frame

    def release(self) -> None:
        cap, self._cap = self._cap, None
        self._failures = 0
        self._fail_since = None
        if cap is not None:
            try:
                cap.release()
            except cv2.error as exc:
                log.debug("%s release raised: %s", self.name, exc)
            log.debug("%s released", self.name)

    def __repr__(self) -> str:
        return f"Camera(index={self.index}, api={self.api!r}, open={self.is_open})"


class FileSource(FrameSource):
    """Frames from a video file (looping forever) or a still image.

    Intended for tests, demos and benchmarks without a camera.

    Args:
        path: Video or image file.
        width, height: Optional bounding box. Larger frames are scaled down to fit
            inside it (aspect ratio preserved, never enlarged), mimicking the
            capture size a camera would deliver.
        realtime: For videos, advance through the file at its native frame rate
            (like a live camera: slow readers skip frames). When ``False`` every
            ``read()`` returns the next frame.
    """

    def __init__(
        self,
        path: str | Path,
        width: int | None = None,
        height: int | None = None,
        *,
        realtime: bool = False,
    ) -> None:
        self.path = Path(path)
        self.width = width
        self.height = height
        self.realtime = realtime
        #: The settings string this source was configured with.
        self.device = str(self.path)
        self._image: np.ndarray | None = None
        self._cap: cv2.VideoCapture | None = None
        self._error: str | None = None
        self._video_fps = 30.0
        self._last_frame: np.ndarray | None = None
        self._t_prev: float | None = None
        self._debt = 0.0
        # Replaceable for deterministic tests of realtime playback.
        self._now: Callable[[], float] = time.monotonic

    @property
    def is_open(self) -> bool:
        return self._image is not None or self._cap is not None

    @property
    def last_error(self) -> str | None:
        return self._error

    @property
    def is_image(self) -> bool:
        return self.path.suffix.lower() in IMAGE_SUFFIXES

    def open(self) -> bool:
        if self.is_open:
            return True
        if _URL_LIKE.search(str(self.path)):
            # FFmpeg would treat "rtsp:…", "tcp:…" and the like as network URLs.
            self._error = f"Not a local file: {self.path} (network sources are not supported)"
            return False
        if not self.path.is_file():
            self._error = f"Video source not found: {self.path}"
            return False
        if self.is_image:
            image = _imread(self.path)
            if image is None:
                self._error = f"Could not decode image {self.path.name}"
                return False
            self._image = self._fit(image)
            self._error = None
            return True

        cap = self._open_capture()
        if cap is None:
            self._error = f"Could not open video {self.path.name}"
            return False
        ok, frame = cap.read()
        cap.release()
        if not ok or not _valid_frame(frame):
            self._error = f"Video {self.path.name} contains no readable frames"
            return False
        # Re-open instead of seeking back: seeking is unreliable for some codecs.
        self._cap = self._open_capture()
        if self._cap is None:
            self._error = f"Could not open video {self.path.name}"
            return False
        fps = float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0)
        self._video_fps = fps if math.isfinite(fps) and 1.0 <= fps <= 240.0 else 30.0
        self._last_frame = None
        self._t_prev = None
        self._debt = 0.0
        self._error = None
        return True

    def read(self) -> np.ndarray | None:
        if self._image is not None:
            # A copy, so a consumer drawing on the frame cannot alter later reads.
            return self._image.copy()
        if self._cap is None:
            return None
        frame = self._read_realtime() if self.realtime else self._step(decode=True)
        if frame is None:
            return None
        return self._fit(frame)

    def release(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()
        self._image = None
        self._last_frame = None

    # --------------------------------------------------------------- helpers
    def _open_capture(self) -> cv2.VideoCapture | None:
        _restrict_ffmpeg_protocols()
        try:
            # FFmpeg only: the whitelist above keeps a local file from
            # referencing network data (see the module docs).
            cap = cv2.VideoCapture(str(self.path), cv2.CAP_FFMPEG)
        except cv2.error as exc:
            log.debug("VideoCapture(%s) raised: %s", self.path.name, exc)
            return None
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def _step(self, *, decode: bool) -> np.ndarray | None:
        """Advance one frame, looping at the end. Returns the frame if decoded."""
        for attempt in range(2):
            cap = self._cap
            if cap is None:
                return None
            if decode:
                ok, frame = cap.read()
                if ok and _valid_frame(frame):
                    self._last_frame = frame
                    return frame
            elif cap.grab():
                return None
            if attempt == 0:
                # End of the file: start over.
                cap.release()
                self._cap = self._open_capture()
        self._error = f"Video {self.path.name} stopped delivering frames"
        self.release()
        return None

    def _read_realtime(self) -> np.ndarray | None:
        now = self._now()
        if self._t_prev is None or self._last_frame is None:
            self._t_prev = now
            self._debt = 0.0
            return self._step(decode=True)
        self._debt += (now - self._t_prev) * self._video_fps
        self._t_prev = now
        steps = int(self._debt)
        self._debt -= steps
        if steps <= 0:
            return self._last_frame.copy()
        # After a long pause jump ahead a little instead of decoding a backlog.
        steps = min(steps, 30)
        for _ in range(steps - 1):
            self._step(decode=False)
            if self._cap is None:
                return None
        return self._step(decode=True)

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        scale = 1.0
        if self.width:
            scale = min(scale, self.width / w)
        if self.height:
            scale = min(scale, self.height / h)
        if scale >= 1.0:
            return frame
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)

    def __repr__(self) -> str:
        return f"FileSource({self.path.name!r}, open={self.is_open})"


def open_source(
    device: str,
    width: int,
    height: int,
    api: str,
    *,
    fps: int = 15,
    realtime: bool = True,
) -> FrameSource:
    """Create (but do not open) the frame source described by a settings string.

    ``"0"``, ``"1"``, … select a camera by index. On Linux ``/dev/videoN`` and
    stable links to it (``/dev/v4l/by-id/…``, ``/dev/v4l/by-path/…``) are
    accepted too; links are resolved on every call, so a camera that was
    re-plugged under another number is still found. Anything else is a path to
    a local video or image file. URLs and other protocols are refused: Eye
    Tracker never opens network streams.

    Raises:
        CameraError: The file does not exist, is a URL, or the settings are invalid.
    """
    spec = device.strip()
    if not spec:
        raise CameraError("No camera configured")
    if _URL_LIKE.search(spec):
        raise CameraError(
            f"Network or protocol video sources are not supported ({spec!r}); "
            "use a camera index or a local file"
        )
    index: int | None = None
    if spec.isdigit():
        index = int(spec)
    elif sys.platform.startswith("linux") and spec.startswith("/dev/") and not os.path.isfile(spec):
        index = _v4l2_index(spec)
    if index is not None:
        try:
            source = Camera(index, width, height, api, fps=fps)
        except ValueError as exc:
            raise CameraError(str(exc)) from exc
        source.device = spec
        return source

    path = Path(spec).expanduser()
    if not path.is_file():
        raise CameraError(f"Video source not found: {path}")
    file_source = FileSource(path, width, height, realtime=realtime)
    file_source.device = spec
    return file_source

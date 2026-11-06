"""Resource/lifecycle half of the vision worker (phase 4 / file-level split).

Extracted from ``vision/worker.py`` so the worker class keeps the frame loop and statistics
while camera/backend acquisition, teardown, idling and back-off waits live here. The mixin only
calls back into the host class; it must never import ``worker`` at runtime (TYPE_CHECKING only).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from .backends.base import BackendUnavailable
from .threads import limit_opencv_threads  # noqa: F401 - keeps one OpenCV cap import point

if TYPE_CHECKING:
    from .backends.base import VisionBackend
    from .camera import FrameSource
    from .worker import BackendFactory, SourceFactory, _Config

log = logging.getLogger(__name__)

#: Upper bound for an interval set through the controller (also imported by worker.py).
_MAX_INTERVAL_S = 60.0


class WorkerLifecycleMixin:
    """Camera/backend lifecycle; mixed into :class:`~eye_tracker.vision.worker.VisionWorker`."""

    # State owned (or created) by the host worker.
    _source: FrameSource | None
    _backend: VisionBackend | None
    _source_factory: SourceFactory
    _backend_factory: BackendFactory
    _backend_info: tuple[str, str] | None
    _applied_max_faces: int | None
    _backend_failures: int
    _backend_recreations: int
    _backoff_step: int
    _control_gen: int
    _stop_requested: bool
    _active: bool
    _slot: float | None
    _clock: Any
    _interval: float
    _dt_ewma: float | None
    _last_frame_t: float | None
    _cond: Any
    _stats_lock: Any
    _stats: Any
    _counters: dict[str, int]
    _gate: Any
    _last_obs: Any
    _pending_backend: BackendFactory | None
    _log_times: dict[str, float]
    BACKOFF_S: tuple[float, ...]
    IDLE_BACKEND_CLOSE_S: float

    if TYPE_CHECKING:

        def _has_pending_locked(self) -> bool: ...
        def _set_error(self, message: str) -> None: ...
        def _clear_error(self) -> None: ...
        def _publish_stats(self, *, force: bool = False) -> None: ...
        def _log_throttled(self, key: str, message: str, *, exc: bool = False) -> None: ...

    def _create_backend(self, cfg: _Config) -> bool:
        try:
            backend = self._backend_factory()
        except Exception as exc:
            message = f"Vision backend unavailable: {exc}"
            # A missing runtime or model is expected on some systems; anything else
            # is a bug worth a traceback.
            self._log_throttled("backend", message, exc=not isinstance(exc, BackendUnavailable))
            self._set_error(message)
            # Nothing can be analysed, so keep the camera (and its light) off.
            self._release_source()
            return False
        self._backend = backend
        self._backend_failures = 0
        with self._cond:
            # Built by a factory that reconfigure() replaced meanwhile: it is
            # closed before use, and its identity must not pass for the new one's.
            if self._pending_backend is None:
                self._backend_info = (backend.name, backend.feature_version)
        self._applied_max_faces = None
        self._apply_config(cfg)
        with self._stats_lock:
            self._stats.extra["backend"] = backend.name
            self._stats.extra["feature_version"] = backend.feature_version
        log.debug("Vision backend %s created", backend.name)
        return True

    def _close_backend(self) -> None:
        backend, self._backend = self._backend, None
        self._applied_max_faces = None
        if backend is None:
            return
        try:
            backend.close()
        except Exception:
            log.debug("Closing the vision backend failed", exc_info=True)
        # A new backend may produce different features; never reuse old results.
        self._reset_tracking()

    def _ensure_source(self) -> bool:
        if self._source is not None and self._source.is_open:
            return True
        self._release_source()
        try:
            source = self._source_factory()
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            self._log_throttled("open", f"Camera unavailable: {message}")
            self._set_error(message)
            return False
        self._source = source
        try:
            opened = source.open()
            error = source.last_error
        except Exception as exc:
            log.debug("Frame source open() raised", exc_info=True)
            opened = False
            error = f"Camera could not be opened: {exc}"
        if not opened:
            message = error or "Camera could not be opened"
            self._log_throttled("open", message)
            self._set_error(message)
            self._release_source()
            return False
        device = getattr(source, "device", None)
        with self._stats_lock:
            self._counters["camera_opens"] += 1
            self._stats.camera_open = True
            self._stats.extra["device"] = None if device is None else str(device)
        if self._backend_recreations == 0:
            self._clear_error()
        self._publish_stats(force=True)
        return True

    def _release_source(self) -> None:
        source, self._source = self._source, None
        if source is None:
            return
        try:
            source.release()
        except Exception:
            log.debug("Releasing the frame source failed", exc_info=True)
        with self._stats_lock:
            was_open = self._stats.camera_open
            self._stats.camera_open = False
            self._stats.fps = 0.0
            self._dt_ewma = None
            self._last_frame_t = None
        if was_open:
            self._publish_stats(force=True)

    def _apply_reconfigure(
        self, source_factory: SourceFactory | None, backend_factory: BackendFactory | None
    ) -> None:
        if source_factory is not None:
            self._release_source()
            self._source_factory = source_factory
        if backend_factory is not None:
            self._close_backend()
            self._backend_factory = backend_factory
            # A different backend gets a clean slate.
            self._backend_recreations = 0
            with self._stats_lock:
                self._stats.extra["backend_failing"] = False
        self._reset_tracking()
        self._backoff_step = 0
        self._log_times.clear()
        self._clear_error()

    def _apply_config(self, cfg: _Config) -> None:
        self._gate.threshold = cfg.gate_threshold
        if self._backend is not None and cfg.max_faces != self._applied_max_faces:
            try:
                self._backend.set_max_faces(cfg.max_faces)
            except Exception:
                log.warning("Vision backend rejected max_faces=%d", cfg.max_faces, exc_info=True)
            self._applied_max_faces = cfg.max_faces

    def _reset_tracking(self) -> None:
        """Forget the last frame: the next one is analysed afresh by gate and backend.

        Called whenever the camera was released or replaced. The backend (kept
        for up to :attr:`IDLE_BACKEND_CLOSE_S`) must forget the face it tracked
        too: after a pause the picture can be a different one, and a crop left
        over from before would latch onto whatever face is there now.
        """
        self._gate.reset()
        self._last_obs = None
        backend = self._backend
        if backend is not None:
            try:
                backend.reset()
            except Exception:
                self._log_throttled("reset", "Vision backend failed to reset", exc=True)

    def _go_idle(self) -> None:
        """Release the camera and block until activated, reconfigured or stopped."""
        self._release_source()
        self._reset_tracking()
        self._backoff_step = 0
        self._slot = None
        close_at = time.monotonic() + self.IDLE_BACKEND_CLOSE_S
        with self._cond:
            while not self._stop_requested and not self._active and not self._has_pending_locked():
                if self._backend is None:
                    self._cond.wait()
                    continue
                remaining = close_at - time.monotonic()
                if remaining > 0:
                    self._cond.wait(remaining)
                    continue
                # Inactive for a long time (privacy mode, locked session): free the
                # backend's memory. Closing it under the lock is fine, it is fast.
                log.debug("Vision worker idle; closing the backend to free memory")
                self._close_backend()

    def _wait_next(self, slot: float) -> float:
        """Sleep until ``slot + interval`` and return that time.

        Setters wake the wait early so a shortened interval applies at once;
        ``stop()``, ``set_active(False)`` and ``reconfigure()`` end it.
        """
        with self._cond:
            due = slot + self._interval
            while not self._stop_requested and self._active and not self._has_pending_locked():
                due = slot + self._interval
                remaining = due - self._clock()
                if remaining <= 0:
                    break
                if not self._cond.wait(min(remaining, _MAX_INTERVAL_S)):
                    # Timed out. Not re-checking the clock keeps the loop moving
                    # even with a clock that does not advance.
                    break
            return due

    def _backoff_wait(self) -> None:
        delay = self.BACKOFF_S[min(self._backoff_step, len(self.BACKOFF_S) - 1)]
        self._backoff_step += 1
        deadline = time.monotonic() + delay
        with self._cond:
            generation = self._control_gen
            while not self._stop_requested and self._control_gen == generation:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._cond.wait(remaining)

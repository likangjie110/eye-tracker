"""Support seams for :mod:`eye_tracker.engine.controller`.

The pointer/worker protocols, the Qt pointer, monitor helpers and the small factories that the
controller passes into the vision worker. They are module-level because tests replace them
individually (``controller._backend_factory``); keeping them here shrinks ``controller.py`` without
changing the controller's behaviour or those seams.

Nothing here imports the controller: the dependency is one-way.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
from PySide6.QtGui import QCursor, QGuiApplication

from .. import paths
from ..config import atomic_write_text
from ..gaze.model import gaze_feature_indices
from ..types import (
    Monitor,
    Observation,
    Rect,
    WorkerStats,
    monitor_at,
)
from .scheduler import PROFILES

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..config import Settings
    from ..platform.base import PlatformServices
    from ..vision.backends.base import VisionBackend
    from ..vision.camera import FrameSource

    SourceFactory = Callable[[], FrameSource]
    BackendFactory = Callable[[], VisionBackend]
    ObservationCallback = Callable[[Observation], None]
    StatsCallback = Callable[[WorkerStats], None]
    PreviewCallback = Callable[[np.ndarray], None]
    WorkerFactory = Callable[
        [SourceFactory, BackendFactory, ObservationCallback, StatsCallback, PreviewCallback],
        "WorkerLike",
    ]


class CursorLike(Protocol):
    """Mouse pointer access (injectable for tests)."""

    def pos(self) -> tuple[int, int]:
        """Current pointer position in global coordinates."""
        ...

    def set_pos(self, x: int, y: int) -> bool | None:
        """Warp the pointer. ``False`` means the system refused."""
        ...


class WorkerLike(Protocol):
    """The part of :class:`~eye_tracker.vision.worker.VisionWorker` the controller uses."""

    def start(self) -> None: ...
    def stop(self, timeout: float = 3.0) -> None: ...
    def set_interval(self, seconds: float) -> None: ...
    def set_active(self, active: bool) -> None: ...
    def set_max_faces(self, n: int) -> None: ...
    def set_motion_gate(self, enabled: bool, threshold: float) -> None: ...
    def set_preview(self, enabled: bool) -> None: ...
    def reconfigure(
        self,
        source_factory: SourceFactory | None = None,
        backend_factory: BackendFactory | None = None,
    ) -> None: ...

    @property
    def stats(self) -> WorkerStats: ...

    @property
    def backend_info(self) -> tuple[str, str] | None: ...


class QtCursor:
    """Default :class:`CursorLike`: the platform layer moves the pointer, Qt is the fallback.

    ``PlatformServices.move_cursor`` returns ``None`` where Qt's ``QCursor.setPos``
    is the right tool (X11), ``True``/``False`` where it did the work itself.
    """

    def __init__(self, platform: PlatformServices) -> None:
        self._platform = platform

    def pos(self) -> tuple[int, int]:
        point = QCursor.pos()
        return (point.x(), point.y())

    def set_pos(self, x: int, y: int) -> bool | None:
        try:
            result = self._platform.move_cursor(int(x), int(y))
        except Exception:
            log.debug("Platform move_cursor failed; using Qt", exc_info=True)
            result = None
        if result is None:
            QCursor.setPos(int(x), int(y))
            return True
        return bool(result)


def qt_monitors() -> list[Monitor]:
    """The monitors Qt knows about, indexed in ``QGuiApplication.screens()`` order.

    Returns an empty list when no ``QGuiApplication`` exists.
    """
    app = QGuiApplication.instance()
    if not isinstance(app, QGuiApplication):
        return []
    primary = QGuiApplication.primaryScreen()
    monitors: list[Monitor] = []
    for screen in QGuiApplication.screens():
        geometry = screen.geometry()
        if geometry.width() <= 0 or geometry.height() <= 0:
            continue
        index = len(monitors)
        monitors.append(
            Monitor(
                index=index,
                name=screen.name() or _fallback_screen_name(index),
                rect=Rect(geometry.x(), geometry.y(), geometry.width(), geometry.height()),
                primary=primary is not None and screen == primary,
                scale=float(screen.devicePixelRatio()),
            )
        )
    return monitors


def _fallback_screen_name(index: int) -> str:
    """The name :func:`qt_monitors` gives a screen that has none (by position)."""
    return f"Screen {index + 1}"


def _same_monitor(old: Monitor, before: list[Monitor], after: list[Monitor]) -> Monitor | None:
    """The monitor of the layout ``after`` that is most likely the physical
    monitor ``old`` of the layout ``before``.

    Indices follow Qt's screen order and say nothing once a screen was added or
    removed. A screen's name (its connector or device name) follows the monitor
    even when the desktop is laid out anew, so a name that is unique in both
    layouts decides first - unless it is merely a positional fallback name.
    Then the same rectangle, then whichever monitor now covers the old centre.
    """
    exact = next((m for m in after if m.name == old.name and m.rect == old.rect), None)
    if exact is not None:
        return exact
    named = [m for m in after if m.name == old.name]
    if (
        len(named) == 1
        and sum(m.name == old.name for m in before) == 1
        and old.name != _fallback_screen_name(old.index)
    ):
        return named[0]
    same_rect = next((m for m in after if m.rect == old.rect), None)
    if same_rect is not None:
        return same_rect
    return monitor_at(after, *old.rect.center)


def _make_vision_worker(
    source_factory: SourceFactory,
    backend_factory: BackendFactory,
    on_observation: ObservationCallback,
    on_stats: StatsCallback,
    on_preview: PreviewCallback,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> WorkerLike:
    # Imported lazily: the vision stack pulls in OpenCV, which tests with a fake
    # worker (and `eye-tracker ctl`) never need.
    from ..vision.worker import VisionWorker

    return VisionWorker(
        source_factory, backend_factory, on_observation, on_stats, on_preview, clock
    )


def _camera_fps(settings: Settings) -> int:
    """Capture rate to request: 15 fps unless the profile analyses faster than that.

    A lower capture rate lets the camera expose longer in dim light (less noise,
    steadier landmarks), and 15 fps covers every mode of eco and balanced except
    calibration, which works fine with 15 samples per second.
    """
    rates = PROFILES.get(settings.performance.profile, PROFILES["balanced"])
    return 30 if rates["active"] > 15 else 15


def _source_factory(settings: Settings) -> SourceFactory:
    cam = settings.camera
    device, width, height, api = cam.device, cam.width, cam.height, cam.api
    fps = _camera_fps(settings)

    def factory() -> FrameSource:
        from ..vision.camera import open_source

        return open_source(device, width, height, api, fps=fps)

    return factory


def _backend_factory(settings: Settings, max_faces: int) -> BackendFactory:
    name = settings.general.backend

    def factory() -> VisionBackend:
        from ..vision.backends import create_backend

        return create_backend(name, max_faces)

    return factory


def _backend_gaze_indices(backend: object) -> tuple[int, ...] | None:
    """Indices of a backend's (class's or instance's) gaze-direction features."""
    try:
        return gaze_feature_indices(
            tuple(getattr(backend, "feature_names", ())),
            tuple(getattr(backend, "gaze_features", ())),
        )
    except Exception:
        log.debug("Cannot read the gaze features of %r", backend, exc_info=True)
        return None


@functools.cache
def _named_backend_gaze_indices(name: str) -> tuple[int, ...] | None:
    """:func:`_backend_gaze_indices` for a backend known only by name (``None`` if unknown)."""
    try:
        from ..vision.backends import backend_class

        cls = backend_class(name)
    except Exception:
        return None
    return _backend_gaze_indices(cls)


@dataclasses.dataclass(frozen=True, slots=True)
class _BuiltBackend:
    """A backend the worker built through one of our factories."""

    #: Which factory built it (see ``Controller._make_backend_factory``).
    generation: int
    #: ``(name, feature_version)``.
    info: tuple[str, str]
    #: Indices of its gaze-direction features (``None``: not declared).
    gaze_indices: tuple[int, ...] | None


def _saved_privacy_mode() -> bool:
    """Whether privacy mode was on when the app last ran (``paths.state_file()``).

    An unreadable file counts as "off": it is only ever written by
    :func:`_save_privacy_mode`, so damage means it never held a choice.
    """
    try:
        data = json.loads(paths.state_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        log.warning("Could not read the remembered privacy mode: %s", exc)
        return False
    return isinstance(data, dict) and data.get("privacy_mode") is True


def _save_privacy_mode(enabled: bool) -> None:
    """Remember privacy mode for the next start (never raises)."""
    try:
        atomic_write_text(paths.state_file(), json.dumps({"privacy_mode": enabled}) + "\n")
    except OSError as exc:
        log.warning("Could not remember privacy mode for the next start: %s", exc)


def _layout_key(monitors: list[Monitor]) -> tuple[tuple[int, int, int, int, int], ...]:
    return tuple((m.index, m.rect.x, m.rect.y, m.rect.w, m.rect.h) for m in monitors)


def _pane_providers(settings: Settings) -> tuple[bool, bool, bool, bool]:
    """The provider switches of the split-pane settings."""
    p = settings.panes
    return (p.tmux, p.wezterm, p.windows_terminal, p.desktop_apps)


def _trace_id(value: Any) -> str | int | None:
    """A pane id as the trace stores it."""
    if value is None or isinstance(value, int):
        return value
    return str(value)


def _same_handle(a: Any, b: Any) -> bool:
    if a is b:
        return True
    try:
        return bool(a == b)
    except Exception:
        return False


def _finite(value: float, digits: int = 2) -> float | None:
    return round(float(value), digits) if math.isfinite(value) else None


def _known_size(size: tuple[int, int] | None) -> tuple[int, int] | None:
    """``size`` as ``(w, h)`` ints if both are positive, else ``None`` (unknown)."""
    if size is None:
        return None
    try:
        w, h = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (w, h) if w > 0 and h > 0 else None

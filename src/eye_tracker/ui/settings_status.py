"""Controller/status helpers for the settings dialog (phase 4 / B3).

Reads the controller's live status (stats, cameras, diagnostics), formats it and provides the
camera probe used by the dialog. Extracted from ``ui/settings_dialog.py``; the dialog imports and
re-exports these names, so its public surface is unchanged.

Split out so the formatting/probing logic can be tested without constructing the dialog.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Collection
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal

from .. import paths
from ..command_text import cli_command_text
from ..config import Settings
from ..platform.base import PlatformServices
from ..platform.hotkeys import HotkeyManager
from ..types import TrackingState

log = logging.getLogger(__name__)


def settings_from_controller(controller: object) -> Settings:
    """A private copy of the controller's current settings.

    Uses ``controller.settings`` (attribute, property or method). Falls back to the
    settings file on disk, which the controller keeps up to date, rather than to
    defaults: applying defaults would silently reset the user's configuration.
    """
    value = getattr(controller, "settings", None)
    if callable(value):
        try:
            value = value()
        except Exception:
            log.debug("controller.settings() failed", exc_info=True)
            value = None
    if isinstance(value, Settings):
        return value.copy()
    log.warning("Controller exposes no settings; reading %s", paths.settings_file())
    return Settings.load(paths.settings_file())


def platform_from_controller(controller: object) -> PlatformServices:
    """The controller's platform services, or the process-wide singleton."""
    value = getattr(controller, "platform", None)
    if isinstance(value, PlatformServices):
        return value
    from ..platform import get_platform

    return get_platform()


def _capabilities(platform: PlatformServices) -> dict[str, bool]:
    try:
        return dict(platform.capabilities())
    except Exception:
        log.debug("capabilities() failed", exc_info=True)
        return {}


def format_stats(stats: object) -> str:
    """One line summarising ``Controller.stats_changed`` data.

    Example: ``"Tracking · 4.0 fps (target 4) · 9.5 ms/frame · 38 % skipped · CPU 0.6 %"``.
    Unknown or missing keys are left out, so this never raises.
    """
    if not isinstance(stats, dict):
        return ""
    parts: list[str] = []
    state = stats.get("state")
    label = stats.get("state_label")
    if isinstance(label, str) and label:
        parts.append(label)
    elif isinstance(state, TrackingState):
        parts.append(state.label)
    elif isinstance(state, str) and state:
        try:
            parts.append(TrackingState(state).label)
        except ValueError:
            parts.append(state)

    def number(key: str) -> float | None:
        value = stats.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(value) else None

    fps, target = number("fps"), number("target_fps")
    if fps is not None:
        text = f"{fps:.1f} fps"
        if target:
            text += f" (target {target:g})"
        parts.append(text)
    inference = number("inference_ms")
    if inference:
        parts.append(f"{inference:.1f} ms/frame")
    skip = number("skip_ratio")
    if skip is not None:
        parts.append(f"{skip * 100:.0f} % skipped")
    cpu = number("cpu_percent")
    if cpu is not None:
        parts.append(f"CPU {cpu:.1f} %")
    backend = stats.get("backend")
    if isinstance(backend, str) and backend:
        parts.append(backend)
    return " · ".join(parts)


def probe_cameras(max_index: int = 4, api: str = "auto", skip: Collection[int] = ()) -> list[Any]:
    """List cameras that deliver frames (runs on a worker thread; tests patch this).

    Indices in ``skip`` are never opened: the camera the tracker is using must
    not be probed, since closing the probe would tear down the tracker's capture
    of the same device on DirectShow.
    """
    from ..vision.camera import list_cameras

    return list(list_cameras(max_index, api, skip=skip))


def camera_in_use(controller: object) -> set[int]:
    """The camera index the controller has applied, as a ``skip`` set for probing.

    Uses the *applied* settings, not a value being edited in a dialog: that is
    the device the vision worker may hold open. On Linux a ``/dev/videoN`` path
    or a stable ``/dev/v4l/by-id/…`` link counts as the index it leads to.
    Empty for a video file.
    """
    device = str(settings_from_controller(controller).camera.device).strip()
    if device.isdigit():
        return {int(device)}
    if not device.startswith("/dev/"):
        return set()
    # Imported only for device paths: the camera module loads OpenCV.
    from ..vision.camera import device_index

    index = device_index(device)
    return {index} if index is not None else set()


def load_diagnostics_text(controller: object | None = None) -> str:
    """The ``doctor`` report as text (tests patch this).

    Imported lazily: the report module is only needed when the page is opened.
    Without a camera probe, so no camera light flashes. The controller's hotkey
    manager, when there is one, tells the report which hotkeys the OS accepted.
    """
    try:
        from .. import diagnostics

        collect = diagnostics.collect_report
        render = diagnostics.format_report
    except (ImportError, AttributeError) as exc:
        log.warning("Diagnostics module unavailable: %s", exc)
        return (
            "The diagnostics report is not available in this build.\n"
            f"Run '{cli_command_text('doctor')}' in a terminal instead."
        )
    manager = getattr(controller, "hotkey_manager", None)
    return str(
        render(
            collect(False, hotkey_manager=manager if isinstance(manager, HotkeyManager) else None)
        )
    )


# =================================================================== camera probe
class CameraProbe(QObject):
    """Runs :func:`probe_cameras` on a background thread.

    Opening camera devices can take seconds per index on Windows, which must not
    freeze the UI. ``finished`` delivers ``list[CameraInfo]`` on the main thread.
    """

    finished = Signal(object)
    _done = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, api: str = "auto", max_index: int = 4, skip: Collection[int] = ()) -> bool:
        """Start probing (never opening the indices in ``skip``).

        Returns ``False`` if a probe is already running.
        """
        if self.running:
            return False
        self._thread = threading.Thread(
            target=self._run,
            args=(api, max_index, frozenset(skip)),
            name="eye-tracker-camera-probe",
            daemon=True,
        )
        self._thread.start()
        return True

    def _run(self, api: str, max_index: int, skip: frozenset[int]) -> None:
        try:
            cameras = probe_cameras(max_index, api, skip)
        except Exception:
            log.warning("Camera detection failed", exc_info=True)
            cameras = []
        try:
            self._done.emit(cameras)
        except RuntimeError:
            # The dialog (and this object) was closed while probing.
            log.debug("Camera probe finished after its owner was deleted")

    def _on_done(self, cameras: object) -> None:
        self._thread = None
        self.finished.emit(cameras)

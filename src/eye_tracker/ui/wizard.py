"""First-run assistant: what the app does, camera check, permissions, walk-away action.

Pages::

    Welcome → Camera → Permissions (macOS only) → Walk away → Finish

The camera choice is applied immediately so the live preview and the "face
detected" indicator reflect the selected device (and reverted on Cancel). All
other choices are applied when the user presses Finish, together with
``general.first_run_done = True``. If "Calibrate now" is ticked,
:attr:`FirstRunWizard.calibration_requested` is emitted after the wizard closed.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import (
    QGuiApplication,
)
from PySide6.QtWidgets import (
    QWidget,
    QWizard,
)

from .. import APP_NAME
from ..config import Settings
from ..platform import autostart
from ..platform.base import PlatformServices
from ..platform.hotkeys import format_hotkey, parse_hotkey
from ..types import Observation, TrackingState
from .icons import app_icon
from .settings_dialog import (
    CameraProbe,
    camera_in_use,
    platform_from_controller,
    settings_from_controller,
)
from .util import DANGER, SUCCESS, WARNING, controller_state, ui_scale
from .wizard_pages import (
    CAMERA_PRIVACY_HELP,
    PRESENCE_CHOICES,
    _CameraPage,
    _FinishPage,
    _PermissionsPage,
    _PresencePage,
    _WelcomePage,
    frame_to_image,
)

log = logging.getLogger(__name__)

__all__ = [
    "CAMERA_PRIVACY_HELP",
    "PAGE_CAMERA",
    "PAGE_FINISH",
    "PAGE_PERMISSIONS",
    "PAGE_PRESENCE",
    "PAGE_WELCOME",
    "PRESENCE_CHOICES",
    "FirstRunWizard",
    "frame_to_image",
]

PAGE_WELCOME = 0
PAGE_CAMERA = 1
PAGE_PERMISSIONS = 2
PAGE_PRESENCE = 3
PAGE_FINISH = 4

# How long a face counts as "detected" after the last observation that had one.
_FACE_FRESH_S = 1.0
_CAMERA_STALE_S = 2.5


# ====================================================================== wizard
class FirstRunWizard(QWizard):
    """First-run assistant.

    Args:
        controller: The app controller (``settings``, ``apply_settings``,
            ``set_preview``, ``observation``/``preview_frame``/``state_changed``
            signals, ``platform``).
        parent: Optional parent widget.
        show_permissions: Show the macOS permissions page. ``None`` (default)
            shows it on macOS only.
        clock: Monotonic clock used for the face indicator (tests inject one).

    Signals:
        calibration_requested: Emitted after Finish when "Calibrate now" was ticked.
    """

    calibration_requested = Signal()

    def __init__(
        self,
        controller: object,
        parent: QWidget | None = None,
        *,
        show_permissions: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._clock = clock
        self._platform: PlatformServices = platform_from_controller(controller)
        try:
            self.capabilities: dict[str, bool] = dict(self._platform.capabilities())
        except Exception:
            self.capabilities = {}
        self._show_permissions = (
            sys.platform == "darwin" if show_permissions is None else bool(show_permissions)
        )
        self.scale = ui_scale(self.screen() or QGuiApplication.primaryScreen())
        settings = settings_from_controller(controller)
        self._original_device = settings.camera.device
        self._device = settings.camera.device
        self._preview_on = False
        self._last_obs_at: float | None = None
        self._last_face_at: float | None = None
        self._tracking_state = controller_state(controller)
        self._wants_calibration = False
        self._autostart_error: str | None = None

        self.setWindowTitle(f"{APP_NAME} setup")
        self.setWindowIcon(app_icon())
        self.setWizardStyle(
            QWizard.WizardStyle.MacStyle
            if sys.platform == "darwin"
            else QWizard.WizardStyle.ModernStyle
        )
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setPixmap(QWizard.WizardPixmap.LogoPixmap, app_icon().pixmap(round(48 * self.scale)))
        self.setButtonText(QWizard.WizardButton.FinishButton, "Finish")
        self.setMinimumSize(round(640 * self.scale), round(560 * self.scale))

        self.welcome_page = _WelcomePage(self)
        self.camera_page = _CameraPage(self)
        self.permissions_page = _PermissionsPage(self)
        self.presence_page = _PresencePage(self)
        self.finish_page = _FinishPage(self)
        self.setPage(PAGE_WELCOME, self.welcome_page)
        self.setPage(PAGE_CAMERA, self.camera_page)
        self.setPage(PAGE_PERMISSIONS, self.permissions_page)
        self.setPage(PAGE_PRESENCE, self.presence_page)
        self.setPage(PAGE_FINISH, self.finish_page)
        self.setStartId(PAGE_WELCOME)

        self._probe = CameraProbe(self)
        self._probe.finished.connect(self._on_cameras_detected)
        self.camera_page.detect.clicked.connect(self.detect_cameras)
        self.camera_page.device.activated.connect(self._on_device_chosen)
        self._fill_devices([])
        self._load_presence(settings)
        self._load_finish(settings)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._refresh_status)
        self.currentIdChanged.connect(self._on_page_changed)
        for name in ("observation", "preview_frame", "state_changed"):
            signal = getattr(controller, name, None)
            if signal is not None:
                slot = {
                    "observation": self._on_observation,
                    "preview_frame": self._on_preview_frame,
                    "state_changed": self._on_state_changed,
                }[name]
                signal.connect(slot)
        self._refresh_status()
        self._refresh_permissions()

    # ============================================================== public API
    def nextId(self) -> int:
        """Skip the permissions page where it does not apply."""
        if self.currentId() == PAGE_CAMERA and not self._show_permissions:
            return PAGE_PRESENCE
        return super().nextId()

    @property
    def wants_calibration(self) -> bool:
        """Whether the user asked to calibrate right after the wizard (set on Finish)."""
        return self._wants_calibration

    @property
    def autostart_error(self) -> str | None:
        return self._autostart_error

    def chosen_presence(self) -> str:
        """The selected walk-away choice (a ``PRESENCE_CHOICES`` value)."""
        for value, button in self.presence_page.buttons.items():
            if button.isChecked():
                return value
        return "lock"

    def set_presence_choice(self, value: str) -> None:
        button = self.presence_page.buttons.get(value)
        if button is not None:
            button.setChecked(True)

    def final_settings(self) -> Settings:
        """The settings Finish applies (current controller settings plus the choices)."""
        settings = settings_from_controller(self._controller)
        settings.camera.device = self._device
        choice = self.chosen_presence()
        if choice == "off":
            settings.presence.enabled = False
        else:
            settings.presence.enabled = True
            settings.presence.action = choice
            settings.presence.away_timeout_s = int(self.presence_page.timeout.value())
        settings.general.first_run_done = True
        return settings

    def detect_cameras(self) -> None:
        """Probe for cameras in the background and refresh the device list."""
        settings = settings_from_controller(self._controller)
        # The camera behind the live preview is not probed (see camera_in_use).
        if self._probe.start(settings.camera.api, skip=camera_in_use(self._controller)):
            self.camera_page.detect.setEnabled(False)
            self.camera_page.detect.setText("Detecting…")

    def select_device(self, device: str) -> None:
        """Switch to another camera (applied immediately)."""
        device = device.strip()
        if not device or device == self._device:
            return
        self._device = device
        self._fill_devices(self._known_cameras())
        self._apply_device(device)

    def request_permission(self, name: str) -> None:
        try:
            self._platform.request_permission(name)
        except Exception:
            log.warning("Requesting the %s permission failed", name, exc_info=True)
        self._refresh_permissions()

    def open_permission_settings(self, name: str) -> bool:
        """Open the OS settings page for a permission. Returns whether one opened."""
        try:
            return bool(self._platform.open_permission_settings(name))
        except Exception:
            log.warning("Opening the %s settings failed", name, exc_info=True)
            return False

    def open_camera_privacy_settings(self) -> None:
        """The camera page's button: the OS page, or the steps where there is none."""
        if not self.open_permission_settings("camera"):
            # No settings page to open (Linux): the help text says what to do.
            self.camera_page.help.setVisible(True)
            self.camera_page.help.setStyleSheet(f"color: {WARNING};")

    # ============================================================== QWizard
    def accept(self) -> None:
        settings = self.final_settings()
        try:
            self._controller.apply_settings(settings)  # type: ignore[attr-defined]
        except Exception:
            log.exception("Applying the first-run settings failed")
        self._apply_autostart(self.finish_page.autostart.isChecked())
        self._wants_calibration = self.finish_page.calibrate.isChecked()
        self._stop_preview()
        super().accept()
        log.info("First-run setup finished (calibrate now: %s)", self._wants_calibration)
        if self._wants_calibration:
            self.calibration_requested.emit()

    def reject(self) -> None:
        self._stop_preview()
        if self._device != self._original_device:
            # The camera choice was applied live; Cancel restores the previous one.
            self._device = self._original_device
            self._apply_device(self._original_device)
        super().reject()

    # ============================================================== pages
    def _monitor_count(self) -> int | None:
        """How many monitors the controller sees (``None`` if it cannot say)."""
        monitors = getattr(self._controller, "monitors", None)
        try:
            return len(monitors()) if callable(monitors) else None
        except Exception:
            log.debug("monitors() failed", exc_info=True)
            return None

    def _load_presence(self, settings: Settings) -> None:
        page = self.presence_page
        choice = settings.presence.action if settings.presence.enabled else "off"
        if choice == "none":
            choice = "off"
        (page.buttons.get(choice) or page.buttons["lock"]).setChecked(True)
        page.timeout.setValue(int(settings.presence.away_timeout_s))
        warning = int(settings.presence.warning_s)
        # Like CountdownToast.hint_text: input cancels the countdown only where the
        # settings count it and the system reports it (not on Wayland outside GNOME).
        if settings.presence.require_input_idle and self.capabilities.get("input_idle", True):
            cancel = "move the mouse or look at the camera"
        else:
            cancel = "look at the camera"
        page.countdown.setText(
            f"A {warning}-second countdown comes first: {cancel} to cancel it."
            if warning
            else "The action happens without a countdown."
        )

    def _load_finish(self, settings: Settings) -> None:
        page = self.finish_page
        try:
            supported = bool(autostart.is_supported())
            enabled = bool(autostart.is_enabled()) if supported else False
        except Exception:
            supported, enabled = False, False
        page.autostart.setChecked(enabled)
        page.autostart.setEnabled(supported)
        # A calibration only serves switching: with one monitor it would change
        # nothing, so it is neither ticked nor recommended there.
        single = self._monitor_count() == 1
        page.calibrate.setChecked(not single and settings.switching.enabled)
        page.one_monitor.setVisible(single)
        rows = []
        labels = {
            "toggle_tracking": "Pause / resume",
            "toggle_privacy": "Privacy mode",
            "recalibrate": "Recalibrate",
        }
        for field, label in labels.items():
            text = getattr(settings.hotkeys, field, "")
            try:
                shown = format_hotkey(parse_hotkey(text))
            except ValueError:
                continue
            rows.append(f"{label}: <b>{shown}</b>")
        if settings.hotkeys.enabled and rows and self.capabilities.get("hotkeys", True):
            page.shortcuts.setText("Shortcuts — " + " · ".join(rows))
        else:
            page.shortcuts.setText("")

    def _on_page_changed(self, page_id: int) -> None:
        if page_id == PAGE_CAMERA:
            self._start_preview()
            self._status_timer.start()
        else:
            self._stop_preview()
            self._status_timer.stop()
        if page_id == PAGE_PERMISSIONS:
            self._refresh_permissions()
            self._status_timer.start()

    # ============================================================== camera
    def _start_preview(self) -> None:
        if self._preview_on:
            return
        self._preview_on = True
        # One owner among others (the preview window): see Controller.set_preview.
        self._call_controller("set_preview", True, self)

    def _stop_preview(self) -> None:
        if not self._preview_on:
            return
        self._preview_on = False
        self._call_controller("set_preview", False, self)
        self.camera_page.preview.clear()

    def _on_preview_frame(self, frame: object) -> None:
        if self._preview_on:
            self.camera_page.preview.set_frame(frame)

    def _on_observation(self, obs: object) -> None:
        if not isinstance(obs, Observation):
            return
        now = self._clock()
        self._last_obs_at = now
        if self._tracking_state == TrackingState.CAMERA_ERROR:
            # Frames arrive again: the controller clears the error with its next
            # state change, but the indicator need not wait for it.
            self._tracking_state = TrackingState.STARTING
        if obs.face_count > 0:
            self._last_face_at = now
        if self.currentId() == PAGE_CAMERA:
            self._refresh_status()

    def _on_state_changed(self, state: object) -> None:
        if isinstance(state, TrackingState):
            self._tracking_state = state
        self._refresh_status()

    def face_status(self) -> str:
        """What the camera indicator shows.

        ``"face"`` (a face was seen within the last second), ``"no_face"``,
        ``"waiting"`` (no frames yet), ``"error"`` (the camera cannot be opened),
        ``"blocked"`` (it cannot be opened and the OS reports that camera access
        is denied), ``"off"`` (paused or privacy mode) or ``"busy"`` (another app
        has the camera).
        """
        state = self._tracking_state
        if state == TrackingState.CAMERA_ERROR:
            return "blocked" if self._camera_permission() is False else "error"
        if state == TrackingState.YIELDED:
            return "busy"
        if not state.camera_active:
            return "off"
        now = self._clock()
        if self._last_obs_at is None or now - self._last_obs_at > _CAMERA_STALE_S:
            return "waiting"
        if self._last_face_at is not None and now - self._last_face_at <= _FACE_FRESH_S:
            return "face"
        return "no_face"

    def _refresh_status(self) -> None:
        status = self.face_status()
        text, color = {
            "face": ("✓  Face detected", SUCCESS),
            "no_face": ("✗  No face detected — sit in front of the camera", WARNING),
            "waiting": ("Starting the camera…", None),
            "error": (
                "✗  The camera could not be opened — another app may be using it, or the "
                "system's privacy settings may block it",
                DANGER,
            ),
            "blocked": ("✗  Camera access is blocked in the system's privacy settings", DANGER),
            "off": ("The camera is off — tracking is paused or in privacy mode", WARNING),
            "busy": ("The camera is in use by another app — close it to continue", WARNING),
        }[status]
        page = self.camera_page
        label = page.status
        if label.text() != text:
            label.setText(text)
            label.setStyleSheet(f"color: {color};" if color else "")
        failed = status in ("error", "blocked")
        page.help.setVisible(failed)
        page.privacy_settings.setVisible(failed)
        if self.currentId() == PAGE_PERMISSIONS:
            self._refresh_permissions()

    def _camera_permission(self) -> bool | None:
        """The OS camera permission (``None``: unknown or not applicable)."""
        try:
            value = dict(self._platform.permissions()).get("camera")
        except Exception:
            log.debug("permissions() failed", exc_info=True)
            return None
        return value if isinstance(value, bool) else None

    def _known_cameras(self) -> list[str]:
        combo = self.camera_page.device
        return [str(combo.itemData(i)) for i in range(combo.count())]

    def _fill_devices(self, devices: list[Any]) -> None:
        combo = self.camera_page.device
        combo.blockSignals(True)
        try:
            combo.clear()
            seen: set[str] = set()
            for item in devices:
                if isinstance(item, str):
                    value, label = item, (f"Camera {item}" if item.isdigit() else item)
                else:
                    index = getattr(item, "index", None)
                    if index is None:
                        continue
                    value = str(index)
                    w, h = getattr(item, "width", 0), getattr(item, "height", 0)
                    label = getattr(item, "name", f"Camera {index}")
                    if w and h:
                        label += f" — {w}×{h}"
                if value in seen:
                    continue
                seen.add(value)
                combo.addItem(label, value)
            if self._device not in seen:
                label = f"Camera {self._device}" if self._device.isdigit() else self._device
                combo.addItem(label, self._device)
            combo.setCurrentIndex(combo.findData(self._device))
        finally:
            combo.blockSignals(False)

    def _on_cameras_detected(self, cameras: object) -> None:
        page = self.camera_page
        page.detect.setEnabled(True)
        page.detect.setText("Detect cameras")
        self._fill_devices(list(cameras) if isinstance(cameras, list) else [])

    def _on_device_chosen(self, index: int) -> None:
        value = self.camera_page.device.itemData(index)
        if isinstance(value, str):
            self.select_device(value)

    def _apply_device(self, device: str) -> None:
        settings = settings_from_controller(self._controller)
        if settings.camera.device == device:
            return
        settings.camera.device = device
        self._last_obs_at = self._last_face_at = None
        self.camera_page.preview.clear("Switching camera…")
        try:
            self._controller.apply_settings(settings)  # type: ignore[attr-defined]
        except Exception:
            log.exception("Switching the camera failed")
        self._refresh_status()

    # ============================================================== permissions
    def _refresh_permissions(self) -> None:
        try:
            perms = dict(self._platform.permissions())
        except Exception:
            perms = {}
        stale = self._accessibility_status() == "stale"
        for name, label in self.permissions_page.status.items():
            value = perms.get(name)
            if value is True:
                text, color = "✓ Allowed", SUCCESS
            elif name == "accessibility" and stale:
                # Granted to an earlier build: the toggle looks on but no longer applies.
                text = "✗ Granted to an earlier version — remove it with “−” and add it again"
                color = DANGER
            elif value is False:
                text, color = "✗ Not allowed yet", DANGER
            else:
                text, color = "Not decided yet", None
            if label.text() != text:
                label.setText(text)
                label.setStyleSheet(f"color: {color};" if color else "")
            # Nothing left to ask for once granted.
            self.permissions_page.allow[name].setEnabled(value is not True)

    def _accessibility_status(self) -> str:
        """``platform.accessibility_status()`` ("granted", "missing", "stale", "unknown").

        Only macOS implements it; elsewhere, or when it fails, ``"unknown"``.
        """
        try:
            return str(self._platform.accessibility_status())
        except Exception:
            log.debug("accessibility_status() failed", exc_info=True)
            return "unknown"

    # ============================================================== misc
    def _apply_autostart(self, wanted: bool) -> None:
        if not self.finish_page.autostart.isEnabled():
            return  # unsupported here: nothing to change
        try:
            if wanted == bool(autostart.is_enabled()):
                return
            if wanted:
                autostart.enable()
            else:
                autostart.disable()
        except autostart.AutostartError as exc:
            log.warning("Changing start at login failed: %s", exc)
            self._autostart_error = str(exc)
            notify = getattr(self._controller, "notify", None)
            if notify is not None:
                try:
                    notify.emit("Start at login", str(exc))
                except Exception:
                    log.debug("Could not emit notify", exc_info=True)

    def _call_controller(self, name: str, *args: object) -> None:
        method = getattr(self._controller, name, None)
        if method is None:
            return
        try:
            method(*args)
        except Exception:
            log.warning("controller.%s failed", name, exc_info=True)

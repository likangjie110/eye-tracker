"""Window opening and UI-event half of the tray application (phase 4 / method split).

Extracted from :class:`~eye_tracker.app.EyeTrackerApp`: showing and raising the
settings, calibration, preview, About, update and setup windows, the startup
window sequence and the slots that react to tray and UI requests. The mixin only
declares the state it uses and calls back into the host app class; it must never
import :mod:`eye_tracker.app` at runtime.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QObject, QTimer

from .app_startup import (
    _OFFERED_CALIBRATIONS,
    _QUIT_DELAY_MS,
    EXPLICIT_CALIBRATION_REASONS,
    PROMPT_CALIBRATE,
    PROMPT_CLICK_WINDOW_S,
    PROMPT_PERMISSION,
    PROMPT_SETUP,
    PROMPT_UPDATE,
    _present,
)

if TYPE_CHECKING:
    from .app_startup import AppOptions
    from .engine.controller import Controller
    from .ui.about import AboutDialog
    from .ui.calibration_window import CalibrationWindow
    from .ui.preview import PreviewWindow
    from .ui.settings_dialog import SettingsDialog
    from .ui.update_dialog import UpdateDialog
    from .ui.wizard import FirstRunWizard
    from .update.service import UpdateService

log = logging.getLogger(__name__)


class AppWindowsMixin:
    """Window lifecycle and UI-request slots for :class:`~eye_tracker.app.EyeTrackerApp`."""

    # State owned by ``EyeTrackerApp.__init__``; declared here so the methods
    # below type-check without importing the app module (there is no runtime cycle).
    _closed: bool
    _controller: Controller | None
    _options: AppOptions
    _clock: Callable[[], float]
    _settings_dialog: SettingsDialog | None
    _calibration: CalibrationWindow | None
    _preview: PreviewWindow | None
    _about: AboutDialog | None
    _wizard: FirstRunWizard | None
    _updates: UpdateService | None
    _update_dialog: UpdateDialog | None
    _prompt: tuple[str, float] | None
    _prompt_permission: str | None

    if TYPE_CHECKING:

        def _raise_calibration(self) -> bool: ...
        def _setup_pending(self) -> bool: ...
        def _popup_tray_menu(self) -> bool: ...
        def _suggest_calibration(self, reason: str) -> None: ...
        def _refresh_autostart(self) -> None: ...
        def _remind_privacy_mode(self) -> None: ...
        def _after_quiet_period(self, callback: Callable[[], None]) -> None: ...
        def _prompt_setup(self) -> None: ...
        def _open_permission_settings(self, name: str) -> None: ...
        def quit(self) -> None: ...

    # ------------------------------------------------------------- window openers
    def present(self) -> None:
        """Answer a second launch or ``ctl show``: bring up whatever needs attention.

        An open calibration, wizard or settings window is raised. If the first-run
        setup was never finished (e.g. the app was only ever started at login),
        the setup assistant opens. Otherwise the tray menu pops up next to the
        tray icon; without a system tray (e.g. GNOME without an AppIndicator
        extension) the settings window opens instead.
        """
        if self._closed:
            return
        if self._raise_calibration():
            return
        for window in (self._wizard, self._settings_dialog):
            if window is not None and window.isVisible():
                _present(window)
                return
        if self._setup_pending():
            self.open_wizard()
            return
        if not self._popup_tray_menu():
            self.open_settings()

    def open_settings(self) -> None:
        """Show the settings window (a fresh one unless it is already open)."""
        if self._closed or self._controller is None or self._raise_calibration():
            return
        if self._wizard is not None and self._wizard.isVisible():
            _present(self._wizard)
            return
        dialog = self._settings_dialog
        if dialog is None or not dialog.isVisible():
            from .ui.settings_dialog import SettingsDialog

            if dialog is not None:
                dialog.deleteLater()
            dialog = SettingsDialog(self._controller)
            dialog.calibration_requested.connect(self._on_settings_calibrate)
            dialog.setup_requested.connect(self._on_settings_setup)
            dialog.finished.connect(functools.partial(self._on_settings_closed, dialog))
            self._settings_dialog = dialog
        _present(dialog)

    def open_calibration(self, reason: str = "user") -> None:
        """Open the calibration on every monitor, or raise it if it is already open."""
        if self._closed or self._controller is None:
            return
        window = self._calibration
        if window is not None and window.is_active:
            window.start()  # raises the existing windows
            return
        from .ui.calibration_window import CalibrationWindow

        log.info("Opening the calibration (%s)", reason)
        self._prompt = None
        window = CalibrationWindow(self._controller, cast(QObject, self))
        window.finished.connect(functools.partial(self._on_calibration_finished, window, reason))
        # Assigned before start(): start() emits finished(False) at once when
        # there is no monitor, and the slot clears this reference.
        self._calibration = window
        window.start()

    def open_preview(self) -> None:
        """Show the camera preview (one instance; closing only hides it)."""
        if self._closed or self._controller is None or self._raise_calibration():
            return
        if self._preview is None:
            from .ui.preview import PreviewWindow

            self._preview = PreviewWindow(self._controller)
        _present(self._preview)

    def open_about(self) -> None:
        """Show the About dialog."""
        if self._closed or self._raise_calibration():
            return
        if self._about is None or not self._about.isVisible():
            from .ui.about import AboutDialog

            if self._about is not None:
                self._about.deleteLater()
            self._about = AboutDialog()
        _present(self._about)

    def open_update(self) -> None:
        """Show the update window (it looks for an update unless one is known)."""
        updates = self._updates
        if self._closed or updates is None or self._raise_calibration():
            return
        if self._update_dialog is None:
            from .ui.update_dialog import UpdateDialog

            self._update_dialog = UpdateDialog(updates)
        self._update_dialog.present()

    def open_wizard(self) -> None:
        """Show the first-run wizard."""
        if self._closed or self._controller is None or self._raise_calibration():
            return
        if self._wizard is not None:
            _present(self._wizard)
            return
        from .ui.wizard import FirstRunWizard

        wizard = FirstRunWizard(self._controller)
        wizard.calibration_requested.connect(self._on_wizard_calibrate)
        wizard.finished.connect(functools.partial(self._on_wizard_finished, wizard))
        self._wizard = wizard
        _present(wizard)

    # ------------------------------------------------------------------ lifecycle

    def _after_start(self) -> None:
        controller = self._controller
        if self._closed or controller is None:
            return
        self._refresh_autostart()
        self._remind_privacy_mode()
        setup_offered = False
        if self._setup_pending():
            if self._options.background:
                # A login start stays quiet; offer the setup once that is over.
                self._after_quiet_period(self._prompt_setup)
                setup_offered = True  # the setup offers the calibration itself
            else:
                try:
                    self.open_wizard()
                except Exception:
                    log.exception("The first-run wizard could not be opened")
                else:
                    return  # the wizard offers the calibration itself
        if self._options.calibrate:
            self.open_calibration("cli")
        elif not setup_offered:
            # A login start too: the notice waits for the end of its quiet period
            # (see _suggest_calibration). Skipping it there meant a setup that was
            # never calibrated and starts at login was never told why nothing
            # switches.
            self._suggest_calibration(controller.calibration_reason)

    # ---------------------------------------------------------------------- slots

    def _on_calibration_required(self, reason: str) -> None:
        if reason in EXPLICIT_CALIBRATION_REASONS:
            self.open_calibration(reason)
        else:
            self._suggest_calibration(reason)

    def _on_tray_calibrate(self) -> None:
        self.open_calibration("tray")

    def _on_settings_calibrate(self) -> None:
        self.open_calibration("settings")

    def _on_settings_setup(self) -> None:
        # Opened right away rather than "at next start": waiting meant storing
        # first_run_done=False, which also switches the walk-away lock to a
        # notification until the assistant is finished (see the controller).
        self.open_wizard()

    def _on_wizard_calibrate(self) -> None:
        self.open_calibration("wizard")

    def _on_calibration_finished(self, window: CalibrationWindow, reason: str, saved: bool) -> None:
        if self._calibration is window:
            self._calibration = None
        window.deleteLater()
        log.info("Calibration %s", "saved" if saved else "closed without saving")
        controller = self._controller
        if not saved and reason in _OFFERED_CALIBRATIONS and controller is not None:
            # Offered rather than asked for (setup assistant, --calibrate): whoever
            # put it off learns what that means, and that a click brings it back.
            self._suggest_calibration(controller.calibration_reason)

    def _on_settings_closed(self, dialog: SettingsDialog, _result: int) -> None:
        if self._settings_dialog is dialog:
            self._settings_dialog = None
        dialog.deleteLater()

    def _on_wizard_finished(self, wizard: FirstRunWizard, _result: int) -> None:
        if self._wizard is wizard:
            self._wizard = None
        wizard.deleteLater()
        controller = self._controller
        if self._closed or controller is None or wizard.wants_calibration:
            return
        # Finished (or cancelled) without calibrating: say why switching is off.
        self._suggest_calibration(controller.calibration_reason)

    def _on_ui_requested(self, command: str) -> None:
        if command == "show":
            self.present()
        elif command == "settings":
            self.open_settings()
        elif command == "quit":
            QTimer.singleShot(_QUIT_DELAY_MS, cast(QObject, self), self.quit)
        else:
            log.debug("Ignoring unknown UI request %r", command)

    def _on_message_clicked(self) -> None:
        prompt, self._prompt = self._prompt, None
        permission, self._prompt_permission = self._prompt_permission, None
        controller = self._controller
        if prompt is None or controller is None:
            return
        kind, shown_at = prompt
        if self._clock() - shown_at > PROMPT_CLICK_WINDOW_S:
            return
        if kind == PROMPT_SETUP and self._setup_pending():
            self.open_wizard()
        elif kind == PROMPT_CALIBRATE and not controller.is_calibrated:
            self.open_calibration("notification")
        elif kind == PROMPT_PERMISSION and permission:
            self._open_permission_settings(permission)
        elif kind == PROMPT_UPDATE:
            self.open_update()

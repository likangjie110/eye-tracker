"""The tray application: the window manager behind the startup sequence.

:func:`run_app` is what ``eye-tracker`` (``run``) executes. The startup wiring
lives in :mod:`eye_tracker.app_startup` (:func:`~eye_tracker.app_startup.build_app`):
it creates everything, checks the single instance and returns an
:class:`AppContext`, whose :meth:`~AppContext.exec` runs the Qt event loop so tests
can build the whole app without entering it. This module is the public import
point for all of it.

The startup order (see :mod:`eye_tracker.app_startup`):

1. logging (console only for now), so native libraries are quietened before
   they load;
2. ``PlatformServices.prepare_process`` before the ``QApplication`` exists
   (DPI awareness and Qt environment variables), then the ``QApplication``;
3. the single-instance check: whoever takes the :class:`~eye_tracker.ipc.InstanceLock`
   is the instance. Otherwise (or if an older version answers) the running app
   is asked to show itself (or to calibrate; a login start only checks that it
   answers) and this process exits without touching the log file or the
   settings. An instance that is shutting down no longer answers but still
   holds the lock for a moment: the launch waits for it to exit and then
   becomes the instance;
4. file logging, settings, the command socket, the
   :class:`~eye_tracker.engine.controller.Controller` and the UI; once the
   event loop runs, the login item is pointed at this copy if it moved.

A process started at login (``--background``) stays quiet for
:data:`~eye_tracker.app_startup.STARTUP_QUIET_S`. If the first-run setup was never
finished, it then offers the setup assistant with a notification, and a second
launch (or ``ctl show``) opens the assistant.

:class:`EyeTrackerApp` owns the controller and every window. The windows talk to
the controller themselves (the tray, the gaze overlay, the countdown toast and
the privacy curtain follow its signals on their own); this class opens windows
on request (tray menu, IPC, hotkeys) and keeps at most one of each.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QWidget

from . import APP_NAME, paths
from .app_startup import (
    _ABSENT_STATES,
    _AFTER_QUIET_MS,
    _BUSY_MARKER,
    _HANDOVER_RETRY_MS,
    _OFFERED_CALIBRATIONS,
    _PERMISSION_NOTICE_S,
    _QUIT_DELAY_MS,
    EXPLICIT_CALIBRATION_REASONS,
    HANDOVER_WAIT_MS,
    PROMPT_CALIBRATE,
    PROMPT_CLICK_WINDOW_S,
    PROMPT_PERMISSION,
    PROMPT_SETUP,
    PROMPT_UPDATE,
    SIGNAL_POLL_MS,
    STARTUP_QUIET_S,
    AppContext,
    AppOptions,
    _apply_overrides,
    _configure_qt_app,
    _controller_factory,
    _create_app,
    _dispose_widget,
    _get_setting,
    _handover_request,
    _is_busy,
    _isatty,
    _noop,
    _present,
    _quit_on_signals,
    _set_setting,
    _show_error,
    _wait_for_handover,
    _wait_ms,
    _warn_startup_options_ignored,
)
from .app_startup import (
    build_app as _build_app,
)
from .app_updates import AppUpdatesMixin
from .app_windows import AppWindowsMixin
from .command_text import cli_command_text
from .config import Settings
from .logging_setup import set_level
from .platform.base import PlatformServices
from .types import TrackingState

if TYPE_CHECKING:
    from .app_startup import ControllerFactory
    from .engine.controller import Controller
    from .ui.about import AboutDialog
    from .ui.calibration_window import CalibrationWindow
    from .ui.countdown import CountdownToast
    from .ui.curtain import PrivacyCurtain
    from .ui.overlay import GazeOverlay
    from .ui.preview import PreviewWindow
    from .ui.settings_dialog import SettingsDialog
    from .ui.tray import TrayIcon
    from .ui.update_dialog import UpdateDialog
    from .ui.wizard import FirstRunWizard
    from .update.service import UpdateService

log = logging.getLogger(__name__)


__all__ = [
    "EXPLICIT_CALIBRATION_REASONS",
    "HANDOVER_WAIT_MS",
    "PROMPT_CALIBRATE",
    "PROMPT_CLICK_WINDOW_S",
    "PROMPT_PERMISSION",
    "PROMPT_SETUP",
    "PROMPT_UPDATE",
    "SIGNAL_POLL_MS",
    "STARTUP_QUIET_S",
    "_ABSENT_STATES",
    "_AFTER_QUIET_MS",
    "_BUSY_MARKER",
    "_HANDOVER_RETRY_MS",
    "_OFFERED_CALIBRATIONS",
    "_PERMISSION_NOTICE_S",
    "_QUIT_DELAY_MS",
    "AppContext",
    "AppOptions",
    "EyeTrackerApp",
    "_apply_overrides",
    "_configure_qt_app",
    "_controller_factory",
    "_create_app",
    "_dispose_widget",
    "_get_setting",
    "_handover_request",
    "_is_busy",
    "_isatty",
    "_noop",
    "_present",
    "_quit_on_signals",
    "_set_setting",
    "_show_error",
    "_wait_for_handover",
    "_wait_ms",
    "_warn_startup_options_ignored",
    "build_app",
    "cli_command_text",
    "run_app",
]


# ============================================================================ run
def run_app(args: argparse.Namespace) -> int:
    """Start the tray app and run the Qt event loop. Returns the exit code.

    Honours ``args.background``, ``args.calibrate``, ``args.camera``,
    ``args.backend``, ``args.config_dir`` and ``args.log_level``; missing
    attributes mean "not given".
    """
    return build_app(args).exec()


def build_app(
    args: argparse.Namespace,
    *,
    platform: PlatformServices | None = None,
    controller_factory: ControllerFactory | None = None,
    controller_kwargs: Mapping[str, Any] | None = None,
) -> AppContext:
    """Create the ``QApplication`` (unless one exists), check for a running
    instance, then create and start the controller, the command socket and the UI.

    The startup order and the arguments are documented on
    :func:`eye_tracker.app_startup.build_app`; the app class is bound here so the
    startup module does not have to import this one.
    """
    return _build_app(
        args,
        platform=platform,
        controller_factory=controller_factory,
        controller_kwargs=controller_kwargs,
        app_factory=EyeTrackerApp,
    )


class EyeTrackerApp(AppWindowsMixin, AppUpdatesMixin, QObject):
    """Owns the controller, the tray icon and the windows of the running app.

    Args:
        qt_app: The ``QApplication``.
        settings: Settings to start with (command-line overrides applied).
        services: OS integration.
        options: Startup switches.
        controller_factory: ``(settings, services) -> Controller``; defaults to
            :class:`~eye_tracker.engine.controller.Controller`.
        session_overrides: ``{"section.field": (saved value, session value)}`` for
            settings overridden on the command line. The session value stays out
            of the settings file when other settings are saved.
        clock: Monotonic time source (notification click window).
    """

    def __init__(
        self,
        qt_app: QApplication,
        settings: Settings,
        services: PlatformServices,
        options: AppOptions | None = None,
        *,
        controller_factory: ControllerFactory | None = None,
        session_overrides: Mapping[str, tuple[str, str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._qt_app = qt_app
        self._settings = settings
        self._services = services
        self._options = options or AppOptions()
        self._controller_factory = controller_factory
        self._overrides = dict(session_overrides or {})
        self._clock = clock
        self._controller: Controller | None = None
        self._started = False
        self._closed = False
        self._quit_hooked = False

        self._tray: TrayIcon | None = None
        self._overlay: GazeOverlay | None = None
        self._countdown: CountdownToast | None = None
        self._curtain: PrivacyCurtain | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._calibration: CalibrationWindow | None = None
        self._preview: PreviewWindow | None = None
        self._about: AboutDialog | None = None
        self._wizard: FirstRunWizard | None = None
        self._updates: UpdateService | None = None
        self._update_dialog: UpdateDialog | None = None
        #: Reasons announced with a "calibration needed" notification since the
        #: calibration was last usable (cleared when it is usable again).
        self._announced: set[str] = set()
        #: The clickable notification on screen: (``PROMPT_*`` kind, when shown).
        self._prompt: tuple[str, float] | None = None
        #: The permission a ``PROMPT_PERMISSION`` notification is about.
        self._prompt_permission: str | None = None
        #: When the controller's latest notification was shown (see
        #: _on_permission_needed); ``None`` once used or when it was not shown.
        self._last_notice_at: float | None = None
        #: The controller's latest notification, shown or not: (title, message, when).
        self._last_notice: tuple[str, str, float] | None = None
        #: A reason that could not be announced while notifications were muted.
        self._deferred_reason: str | None = None
        #: A permission notice swallowed by the startup mute: (name, title, message).
        self._deferred_permission: tuple[str, str, str] | None = None

    # ---------------------------------------------------------------- accessors
    @property
    def controller(self) -> Controller | None:
        return self._controller

    @property
    def tray(self) -> TrayIcon | None:
        return self._tray

    @property
    def overlay(self) -> GazeOverlay | None:
        return self._overlay

    @property
    def countdown(self) -> CountdownToast | None:
        return self._countdown

    @property
    def curtain(self) -> PrivacyCurtain | None:
        return self._curtain

    @property
    def settings_dialog(self) -> SettingsDialog | None:
        return self._settings_dialog

    @property
    def calibration_window(self) -> CalibrationWindow | None:
        return self._calibration

    @property
    def preview(self) -> PreviewWindow | None:
        return self._preview

    @property
    def wizard(self) -> FirstRunWizard | None:
        return self._wizard

    @property
    def closed(self) -> bool:
        return self._closed

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Create the controller and the UI, start tracking and open startup windows."""
        if self._started or self._closed:
            return
        self._started = True
        controller = self._make_controller()
        self._controller = controller
        controller.calibration_required.connect(self._on_calibration_required)
        controller.settings_changed.connect(self._on_settings_changed)
        controller.ui_requested.connect(self._on_ui_requested)
        controller.state_changed.connect(self._on_state_changed)

        from .ui.countdown import CountdownToast
        from .ui.curtain import PrivacyCurtain
        from .ui.overlay import GazeOverlay
        from .ui.tray import TrayIcon

        # These follow the controller's signals by themselves: the toast
        # away_warning/away_cancelled, the curtain guard_changed, the overlay
        # gaze_changed and settings.ui.show_gaze_overlay.
        tray = TrayIcon(controller, self)
        self._tray = tray
        self._overlay = GazeOverlay(controller, self)
        self._countdown = CountdownToast(controller)
        curtain = PrivacyCurtain(controller, self)
        self._curtain = curtain
        # Connected after the tray: any other notification replaces ours.
        controller.notify.connect(self._on_controller_notify)

        tray.open_settings.connect(self.open_settings)
        tray.open_calibration.connect(self._on_tray_calibrate)
        tray.open_preview.connect(self.open_preview)
        tray.open_about.connect(self.open_about)
        tray.open_update.connect(self.open_update)
        tray.quit_requested.connect(self.quit)
        tray.tray.messageClicked.connect(self._on_message_clicked)
        # Esc or the button on the curtain: switching resumes (the guard stays on).
        curtain.dismissed.connect(controller.dismiss_curtain)
        controller.permission_needed.connect(self._on_permission_needed)
        if self._options.background:
            tray.mute_notifications(STARTUP_QUIET_S)
        tray.show()
        self._qt_app.aboutToQuit.connect(self.shutdown)
        self._quit_hooked = True

        controller.start()
        self._start_updates(tray)
        # Windows open once the event loop runs, so the tray is already in place.
        QTimer.singleShot(0, self, self._after_start)

    def shutdown(self) -> None:
        """Close every window, then stop tracking (releases the camera). Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._quit_hooked:
            self._quit_hooked = False
            with contextlib.suppress(RuntimeError, TypeError):
                self._qt_app.aboutToQuit.disconnect(self.shutdown)
        # Windows first, while the controller still runs: an open calibration
        # reports its cancellation, the preview turns the preview stream off.
        calibration, self._calibration = self._calibration, None
        if calibration is not None:
            with contextlib.suppress(Exception):
                calibration.cancel()
            calibration.deleteLater()
        if self._updates is not None:
            self._updates.shutdown()
        widgets: list[QWidget | None] = [
            self._wizard,
            self._settings_dialog,
            self._about,
            self._preview,
            self._countdown,
            self._update_dialog,
        ]
        self._wizard = self._settings_dialog = self._about = self._preview = None
        self._countdown = self._update_dialog = None
        for widget in widgets:
            if widget is not None:
                _dispose_widget(widget)
        for helper in (self._overlay, self._curtain):
            if helper is not None:
                with contextlib.suppress(Exception):
                    helper.close()
        if self._controller is not None:
            try:
                self._controller.shutdown()
            except Exception:
                log.exception("Controller shutdown failed")
        if self._tray is not None:
            with contextlib.suppress(Exception):
                self._tray.dispose()

    def quit(self) -> None:
        """Leave the event loop (shutdown follows via ``aboutToQuit``)."""
        log.info("Quit requested")
        self._qt_app.quit()

    # ----------------------------------------------------------------- commands
    def handle_command(self, command: str) -> str:
        """IPC entry point: :meth:`Controller.handle_command` executes it (UI
        commands come back through ``ui_requested``)."""
        controller = self._controller
        if controller is None or self._closed:
            # A second launch that gets this waits for the instance (_is_busy).
            return f"error: {APP_NAME} {_BUSY_MARKER}; try again"
        return str(controller.handle_command(command))

    # ---------------------------------------------------------------------- slots

    def _on_settings_changed(self, settings: object) -> None:
        if not isinstance(settings, Settings):
            return
        self._settings = settings
        if not self._options.log_level_locked:
            set_level(settings.general.log_level)
        if self._updates is not None:
            self._updates.set_auto(settings.updates.check)
        self._save_without_overrides(settings)

    def _on_state_changed(self, state: object) -> None:
        controller = self._controller
        if not self._announced or controller is None:
            return
        if controller.is_calibrated or state in _ABSENT_STATES:
            # Usable again (recalibrated, or back at a known desk): the next time
            # it stops being usable deserves a notification, even for a reason
            # announced before (docking at a new desk is "the monitor layout
            # changed" every time). And after being away, locked or private,
            # the controller reminds the user of a calibration that is still
            # unusable, with the same reason as before: that must be shown too.
            self._announced.clear()

    def _on_controller_notify(self, title: str, message: str) -> None:
        # The tray shows one message at a time; ours has been replaced.
        self._prompt = None
        self._prompt_permission = None
        tray = self._tray
        now = self._clock()
        shown = tray is not None and tray.muted_for <= 0
        self._last_notice_at = now if shown else None
        self._last_notice = (str(title), str(message), now)

    def _on_permission_needed(self, name: str) -> None:
        """Make the notification that explains a missing permission clickable.

        The controller emits ``permission_needed`` right after that ``notify``
        (or without one when notifications are off, and then there is nothing to
        click). Clicking opens the OS privacy settings for ``name``.

        The controller says it only once, so a notice swallowed by the quiet
        period of a login start (typically the Accessibility check at startup)
        is shown once that period is over, if the permission is still missing.
        """
        shown_at, self._last_notice_at = self._last_notice_at, None
        notice, self._last_notice = self._last_notice, None
        now = self._clock()
        if shown_at is not None and now - shown_at <= _PERMISSION_NOTICE_S:
            self._prompt = (PROMPT_PERMISSION, now)
            self._prompt_permission = str(name)
            return
        tray = self._tray
        if (
            notice is None
            or now - notice[2] > _PERMISSION_NOTICE_S
            or tray is None
            or tray.muted_for <= 0
        ):
            return  # nothing was said (notifications off), so nothing to repeat
        if self._deferred_permission is None:
            self._after_quiet_period(self._announce_deferred_permission)
        # One slot: a click can only open the settings of the notice shown last.
        self._deferred_permission = (str(name), notice[0], notice[1])

    def _announce_deferred_permission(self) -> None:
        deferred, self._deferred_permission = self._deferred_permission, None
        tray = self._tray
        if deferred is None or self._closed or tray is None:
            return
        name, title, message = deferred
        if not self._permission_still_needed(name):
            return
        # The controller applied the notifications setting when it sent it.
        if tray.notify(title, message, force=True):
            self._prompt = (PROMPT_PERMISSION, self._clock())
            self._prompt_permission = name

    def _permission_still_needed(self, name: str) -> bool:
        """Whether permission ``name`` still blocks something (see _on_permission_needed)."""
        controller = self._controller
        if controller is None:
            return False
        if name == "camera":
            return controller.state is TrackingState.CAMERA_ERROR
        if name == "accessibility":
            try:
                return str(self._services.accessibility_status()) in ("missing", "stale")
            except Exception:
                log.debug("accessibility_status() failed", exc_info=True)
        return True

    # ------------------------------------------------------------------ helpers
    def _make_controller(self) -> Controller:
        if self._controller_factory is not None:
            return self._controller_factory(self._settings, self._services)
        from .engine.controller import Controller

        return Controller(self._settings, self._services)

    def _popup_tray_menu(self) -> bool:
        tray = self._tray
        if tray is None or not tray.available or not tray.tray.isVisible():
            return False
        geometry = tray.tray.geometry()
        # The icon may sit in an overflow area without a geometry.
        point = geometry.center() if geometry.isValid() and geometry.width() > 0 else None
        tray.menu.popup(point if point is not None else QCursor.pos())
        tray.menu.activateWindow()
        return True

    def _suggest_calibration(self, reason: str) -> None:
        """Tell the user that switching needs a calibration.

        Once per reason until the calibration is usable again. A notification
        that could not be shown does not count: one suppressed by the quiet
        period after a login start is shown when that period ends.
        """
        controller = self._controller
        tray = self._tray
        if controller is None or tray is None or controller.is_calibrated:
            return
        if self._calibration is not None and self._calibration.is_active:
            return
        if len(controller.monitors()) < 2 or reason in self._announced:
            return  # with one monitor there is nothing to switch between
        if not controller.settings.switching.enabled:
            return  # switching is off on purpose: a calibration changes nothing
        if getattr(controller, "privacy", False):
            # The camera is off on purpose; the controller reminds the user once
            # privacy mode ends (_remind_calibration).
            return
        detail = f" ({reason})" if reason else ""
        shown = tray.notify(
            "Calibration needed",
            f"Switching monitors is off until you calibrate{detail}. "
            "Click here or choose “Calibrate now…” in the tray menu.",
        )
        if shown:
            self._announced.add(reason)
            self._prompt = (PROMPT_CALIBRATE, self._clock())
        elif tray.muted_for > 0:
            # Swallowed by the quiet period of a login start: say it afterwards.
            if self._deferred_reason is None:
                self._after_quiet_period(self._announce_deferred)
            self._deferred_reason = reason

    def _announce_deferred(self) -> None:
        reason, self._deferred_reason = self._deferred_reason, None
        controller = self._controller
        if reason is not None and not self._closed and controller is not None:
            # The reason may be stale by now; the current one is what matters.
            self._suggest_calibration(controller.calibration_reason or reason)

    def _remind_privacy_mode(self) -> None:
        """Say that privacy mode is still on from the previous run.

        It is remembered across restarts so that the camera never comes back on
        by itself (``privacy.remember_privacy_mode``); this tells the user why
        nothing switches and walk-away detection is off. A notice swallowed by
        the quiet period of a login start is shown once that period is over.
        """
        controller, tray = self._controller, self._tray
        if self._closed or controller is None or tray is None:
            return
        if not getattr(controller, "privacy_restored", False):
            return
        shown = tray.notify(
            "Privacy mode is still on",
            "The camera stays off, as you left it, so monitor switching and walk-away "
            "detection are paused. Turn privacy mode off in the tray menu.",
        )
        if not shown and tray.muted_for > 0:
            self._after_quiet_period(self._remind_privacy_mode)

    def _prompt_setup(self) -> None:
        """After a quiet login start: offer the first-run setup that never happened."""
        tray = self._tray
        if self._closed or tray is None or not self._setup_pending():
            return
        if self._wizard is not None or (
            self._calibration is not None and self._calibration.is_active
        ):
            return
        shown = tray.notify(
            f"Finish setting up {APP_NAME}",
            "Check the camera and choose what happens when you walk away. "
            "Click here to open the setup assistant.",
        )
        if shown:
            self._prompt = (PROMPT_SETUP, self._clock())

    def _after_quiet_period(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` once the startup notification mute is over."""
        tray = self._tray
        delay = tray.muted_for if tray is not None else 0.0
        QTimer.singleShot(int(delay * 1000) + _AFTER_QUIET_MS, self, callback)

    def _setup_pending(self) -> bool:
        """The first-run setup was never finished.

        Nothing in the UI sets ``first_run_done`` back to ``False``: running the
        assistant again from the settings opens it at once (see
        :meth:`_on_settings_setup`), so the walk-away lock stays in force.
        """
        controller = self._controller
        return controller is not None and not controller.settings.general.first_run_done

    def _raise_calibration(self) -> bool:
        """Raise an open calibration instead of opening another window. Returns
        whether one was open.

        Every other window would open underneath its topmost full-screen
        surfaces and take the keyboard from it (Space, R and Esc would stop
        working with no visible reason).
        """
        calibration = self._calibration
        if calibration is None or not calibration.is_active:
            return False
        calibration.start()  # only raises and refocuses the existing surfaces
        return True

    def _open_permission_settings(self, name: str) -> None:
        """Open the OS privacy settings page for permission ``name``. Never raises."""
        try:
            opened = self._services.open_permission_settings(name)
        except Exception:
            log.warning("Could not open the %s privacy settings", name, exc_info=True)
            return
        if not opened:
            log.info("No privacy settings page to open for %s here", name)

    def _refresh_autostart(self) -> None:
        """Point the login item at this copy of the app if it moved (e.g. an
        update to another folder, a replaced AppImage). Never raises."""
        try:
            from .platform import autostart

            if autostart.is_supported():
                autostart.refresh()
        except Exception:
            log.warning("Could not check the start-at-login entry", exc_info=True)

    def _save_without_overrides(self, settings: Settings) -> None:
        """The controller saved ``settings``; keep command-line overrides out of the file."""
        restore = {
            key: saved
            for key, (saved, session) in self._overrides.items()
            if _get_setting(settings, key) == session and saved != session
        }
        if not restore:
            return
        on_disk = settings.copy()
        for key, value in restore.items():
            _set_setting(on_disk, key, value)
        try:
            on_disk.save(paths.settings_file())
        except OSError as exc:
            log.warning("Could not save the settings: %s", exc)

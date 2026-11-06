"""Startup wiring for :mod:`eye_tracker.app`: create the app, or hand the launch over.

The public entry point is :func:`build_app`. It prepares logging and the ``QApplication``, checks
the single-instance lock (asking a running instance to show itself or to calibrate, waiting for one
that is still shutting down), then creates and starts the controller, the command socket and the
UI. It returns an :class:`AppContext`; :meth:`AppContext.exec` runs the Qt event loop and
:meth:`AppContext.shutdown` closes everything again.

These helpers live here, next to the small command-line, signal and Qt utilities they use, so
``app.py`` keeps only :class:`~eye_tracker.app.EyeTrackerApp` and stays the public import point.
The dependency is one-way (``app`` -> ``app_startup``): the app class is passed in as
``app_factory`` instead of being imported here, which would close a module cycle.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from . import APP_AUTHOR, APP_NAME, APP_SLUG, __version__, ipc, paths
from .command_text import cli_command_text
from .config import Settings
from .logging_setup import install_qt_message_handler, set_level, setup_logging
from .platform import get_platform
from .platform.base import PlatformServices
from .types import TrackingState

if TYPE_CHECKING:
    from .app import EyeTrackerApp
    from .engine.controller import Controller

    AppFactory = Callable[..., EyeTrackerApp]
    ControllerFactory = Callable[[Settings, PlatformServices], Controller]

# The app's logger: the code moved here keeps its log records on ``eye_tracker.app``.
log = logging.getLogger("eye_tracker.app")


#: ``calibration_required`` reasons that are explicit user requests (the
#: recalibrate hotkey, ``eye-tracker ctl calibrate``): the calibration opens at
#: once. Any other reason (a changed monitor layout, another vision backend) only
#: produces a notification that opens it when clicked: covering every screen
#: unannounced could hide a dialog the user is working with, such as the
#: operating system's "keep these display settings?" countdown.
EXPLICIT_CALIBRATION_REASONS = frozenset({"hotkey", "ipc", "user"})
#: Calibrations the app offered rather than the user asked for (``open_calibration``
#: reasons): closing one without saving brings the "Calibration needed" notice.
_OFFERED_CALIBRATIONS = frozenset({"wizard", "cli"})
#: With ``--background`` (login start) notifications stay quiet this long.
STARTUP_QUIET_S = 20.0
#: Clicking a "calibration needed" or "finish setup" notification later than
#: this does nothing.
PROMPT_CLICK_WINDOW_S = 600.0
#: Kinds of notifications that do something when clicked: open the calibration,
#: the setup assistant, the OS privacy settings of a missing permission, or the
#: update window.
PROMPT_CALIBRATE = "calibrate"
PROMPT_SETUP = "setup"
PROMPT_PERMISSION = "permission"
PROMPT_UPDATE = "update"
#: A ``permission_needed`` belongs to the controller notification emitted this
#: recently (the controller emits both back to back).
_PERMISSION_NOTICE_S = 1.0
#: States the user is absent in. Entering one forgets which calibration reasons
#: were announced, so the controller's reminder on their return is shown.
_ABSENT_STATES = frozenset({TrackingState.AWAY, TrackingState.LOCKED, TrackingState.PRIVACY})
#: How often Python gets control during the event loop, so Ctrl+C is handled.
SIGNAL_POLL_MS = 500
#: Delay before quitting on an IPC ``quit``, so the reply reaches the client.
_QUIT_DELAY_MS = 100
#: Margin after the startup mute ends before a deferred notification is shown.
_AFTER_QUIET_MS = 250
#: Settings that ``--camera`` / ``--backend`` override for one session.
_OVERRIDABLE = {"camera": "camera.device", "backend": "general.backend"}
#: Options that only a new instance applies: (``argparse`` attribute, option).
_STARTUP_ONLY_OPTIONS = (
    ("trace", "--trace"),
    ("camera", "--camera"),
    ("backend", "--backend"),
)
#: How long a launch keeps waiting for an instance that holds the lock but does
#: not answer, after :data:`~eye_tracker.ipc.STARTUP_WAIT_MS`: one that is shutting
#: down (its event loop has stopped while it releases the camera, which can take
#: a few seconds) exits within this time, and the launch then takes its place.
HANDOVER_WAIT_MS = 10_000
#: Reply timeout for each retry during :data:`HANDOVER_WAIT_MS`.
_HANDOVER_REPLY_MS = 500
#: Pause between two retries during :data:`HANDOVER_WAIT_MS`.
_HANDOVER_RETRY_MS = 250
#: Part of the reply of an instance that cannot take commands yet or any more.
_BUSY_MARKER = "is starting or shutting down"


@dataclass(frozen=True, slots=True)
class AppOptions:
    """Command-line switches that change how the app starts."""

    #: Started at login: no first-run wizard, no startup notifications, and an
    #: instance that is already running is not asked to show itself.
    background: bool = False
    #: Open the calibration right after start.
    calibrate: bool = False
    #: ``--log-level`` was given, so the settings' level is ignored.
    log_level_locked: bool = False


@dataclass(slots=True)
class AppContext:
    """What :func:`build_app` created. :meth:`exec` runs it.

    ``exit_code`` is set when there is nothing to run: the request was handed to
    an instance that is already running, or startup failed.
    """

    qt_app: QApplication
    app: EyeTrackerApp | None = None
    server: ipc.InstanceServer | None = None
    exit_code: int | None = None

    @property
    def controller(self) -> Controller | None:
        return self.app.controller if self.app is not None else None

    def exec(self) -> int:
        """Run the event loop until the app quits, then shut down. Returns the exit code."""
        if self.app is None or self.exit_code is not None:
            return self.exit_code if self.exit_code is not None else 0
        with _quit_on_signals(self.app.quit):
            code = int(self.qt_app.exec())
        self.shutdown()
        log.info("%s exited (code %s)", APP_NAME, code)
        return code

    def shutdown(self) -> None:
        """Stop accepting commands, close every window and release the camera. Idempotent."""
        if self.server is not None:
            self.server.close()
        if self.app is not None:
            self.app.shutdown()


# ============================================================================ run
def build_app(
    args: argparse.Namespace,
    *,
    platform: PlatformServices | None = None,
    controller_factory: ControllerFactory | None = None,
    controller_kwargs: Mapping[str, Any] | None = None,
    app_factory: AppFactory,
) -> AppContext:
    """Create the ``QApplication`` (unless one exists), check for a running
    instance, then create and start the controller, the command socket and the UI.

    Args:
        args: Parsed command line (see :func:`eye_tracker.app.run_app`).
        platform: OS integration; defaults to :func:`~eye_tracker.platform.get_platform`.
        controller_factory: ``(settings, platform) -> Controller``; defaults to
            :class:`~eye_tracker.engine.controller.Controller`.
        controller_kwargs: Extra keyword arguments for the default controller
            (its test seams: ``worker_factory``, ``monitors_provider``, ``cursor``,
            ``hotkey_manager_factory``, ``clock``).
        app_factory: The app class, passed by :mod:`eye_tracker.app`, which owns it;
            importing it here would close the import cycle.
    """
    options = AppOptions(
        background=bool(getattr(args, "background", False)),
        calibrate=bool(getattr(args, "calibrate", False)),
        log_level_locked=bool(getattr(args, "log_level", None)),
    )
    config_dir = getattr(args, "config_dir", None)
    if config_dir:
        paths.set_base_override(Path(config_dir))
    level = getattr(args, "log_level", None) or "INFO"
    console = _isatty(sys.stderr)
    # The log file belongs to the running instance; a process that only hands a
    # request over to it must not write (or rotate) it, so file logging starts
    # after the single-instance check.
    setup_logging(level, console=console, log_to_file=False)
    services = platform if platform is not None else get_platform()
    qt_app = _make_qt_app(services)
    claimed = _claim_instance(qt_app, args, options)
    if isinstance(claimed, AppContext):
        return claimed
    return _start_app(
        qt_app,
        args,
        services,
        options,
        claimed,
        level=level,
        console=console,
        controller_factory=controller_factory,
        controller_kwargs=controller_kwargs,
        app_factory=app_factory,
    )


# ---------------------------------------------------------------- Qt application
def _make_qt_app(services: PlatformServices) -> QApplication:
    """Create the ``QApplication`` (or reuse an existing one) and configure it."""
    existing = QApplication.instance()
    if existing is None:
        # Must happen before the QApplication exists (DPI awareness, Qt env vars).
        services.prepare_process()
        qt_app = QApplication([sys.argv[0] if sys.argv and sys.argv[0] else APP_SLUG])
    elif isinstance(existing, QApplication):
        qt_app = existing
    else:
        raise RuntimeError("A non-GUI Qt application already exists; cannot start the tray app")
    _configure_qt_app(qt_app)
    # macOS: no Dock icon or application menu for a tray app. The .app bundle's
    # LSUIElement does this too; this covers running from source.
    accessory = getattr(services, "set_accessory_app", None)
    if callable(accessory):
        try:
            accessory()
        except Exception:
            log.debug("set_accessory_app failed", exc_info=True)
    return qt_app


# ------------------------------------------------------------- single instance
def _claim_instance(
    qt_app: QApplication, args: argparse.Namespace, options: AppOptions
) -> ipc.InstanceLock | AppContext:
    """Become the instance, hand the request to a running one, or return its exit.

    ``build_app`` gets either the lock this process now holds or the
    :class:`AppContext` of a launch that has nothing left to do.
    """
    # Single instance. Taking the lock is atomic, so of two launches racing each
    # other exactly one becomes the instance; the other hands its request over
    # (waiting for the winner to start listening) and leaves.
    request = _handover_request(options)
    lock = ipc.InstanceLock()
    owner = lock.acquire()
    try:
        # The owner asks too: an instance of a version without the lock answers.
        # A launch that lost the lock waits for the winner to start listening.
        reply = ipc.send_command(request, wait_ms=0 if owner else ipc.STARTUP_WAIT_MS)
        if not owner and (reply is None or _is_busy(reply)):
            owner, reply = _wait_for_handover(lock, request)
    except BaseException:
        lock.release()  # never keep a later launch out after failing here
        raise
    if reply is not None and not _is_busy(reply):
        lock.release()
        if reply.startswith("error"):
            log.warning("The running instance refused %r: %s", request, reply)
            return AppContext(qt_app, exit_code=1)
        log.info("%s is already running; asked it to %s", APP_NAME, request)
        _warn_startup_options_ignored(args)
        return AppContext(qt_app, exit_code=0)
    if not owner or reply is not None:
        # Nobody answered in time, or only "starting or shutting down" (from an
        # older version without the lock, when this process holds it).
        lock.release()
        log.error("Another instance (lock %s) does not answer; exiting", lock.path)
        _show_error(qt_app, f"{APP_NAME} is already running but does not respond.")
        return AppContext(qt_app, exit_code=1)
    return lock


# ------------------------------------------------------------------ the instance
def _start_app(
    qt_app: QApplication,
    args: argparse.Namespace,
    services: PlatformServices,
    options: AppOptions,
    lock: ipc.InstanceLock,
    *,
    level: str,
    console: bool,
    controller_factory: ControllerFactory | None,
    controller_kwargs: Mapping[str, Any] | None,
    app_factory: AppFactory,
) -> AppContext:
    """Create and start the app and the command socket; own the lock from here on."""
    app: EyeTrackerApp | None = None
    try:
        app = _create_app(
            qt_app,
            args,
            services,
            options,
            level=level,
            console=console,
            controller_factory=controller_factory,
            controller_kwargs=controller_kwargs,
            app_factory=app_factory,
        )
        # From here on the server owns the lock and releases it in close().
        server = ipc.InstanceServer(app.handle_command, app, lock=lock)
    except BaseException:
        lock.release()  # never keep a later launch out after failing here
        if app is not None:
            app.shutdown()
        raise
    if not server.listen():
        if server.another_instance_running:
            # Only possible where files cannot be locked: another instance
            # claimed the socket after our check, or it hangs.
            log.error("Another instance is running but does not answer; exiting")
            _show_error(qt_app, f"{APP_NAME} is already running but does not respond.")
            server.close()
            app.shutdown()
            return AppContext(qt_app, exit_code=1)
        log.warning("Command socket unavailable; '%s' will not work", cli_command_text("ctl"))

    try:
        app.start()
    except Exception as exc:
        log.exception("%s could not start", APP_NAME)
        server.close()
        app.shutdown()
        _show_error(
            qt_app,
            f"{APP_NAME} could not start:\n\n{exc}\n\n"
            f"Details are in the log file:\n{paths.log_file()}",
        )
        return AppContext(qt_app, exit_code=1)
    return AppContext(qt_app, app, server)


def _create_app(
    qt_app: QApplication,
    args: argparse.Namespace,
    services: PlatformServices,
    options: AppOptions,
    *,
    level: str,
    console: bool,
    controller_factory: ControllerFactory | None,
    controller_kwargs: Mapping[str, Any] | None,
    app_factory: AppFactory,
) -> EyeTrackerApp:
    """File logging, settings (with command-line overrides) and the app object.

    Runs once this process is known to be the instance, so it may write the log
    file and read the settings.
    """
    setup_logging(level, console=console)
    install_qt_message_handler()
    settings = Settings.load(paths.settings_file())
    if not options.log_level_locked:
        set_level(settings.general.log_level)
    overrides = _apply_overrides(settings, args)
    log.info(
        "%s %s starting (Python %s, %s)",
        APP_NAME,
        __version__,
        sys.version.split()[0],
        sys.platform,
    )

    trace = getattr(args, "trace", None)
    if trace:
        controller_kwargs = {**dict(controller_kwargs or {}), "trace_path": trace}
    if controller_factory is None and controller_kwargs:
        controller_factory = _controller_factory(dict(controller_kwargs))
    return app_factory(
        qt_app,
        settings,
        services,
        options,
        controller_factory=controller_factory,
        session_overrides=overrides,
    )


def _controller_factory(kwargs: dict[str, Any]) -> ControllerFactory:
    def factory(settings: Settings, services: PlatformServices) -> Controller:
        from .engine.controller import Controller

        return Controller(settings, services, **kwargs)

    return factory


# ======================================================================= helpers
def _handover_request(options: AppOptions) -> str:
    """What a launch asks an instance that is already running to do.

    A login start (``--background``) must not pop anything up there (the tray
    menu, the setup assistant or the settings take the keyboard from whatever
    the user is typing in), so it only asks for ``status``, which just proves
    that the instance answers.
    """
    if options.calibrate:
        return "calibrate"
    return "status" if options.background else "show"


def _is_busy(reply: str) -> bool:
    """Whether ``reply`` says the instance cannot take commands yet or any more."""
    return reply.startswith("error") and _BUSY_MARKER in reply


def _wait_for_handover(lock: ipc.InstanceLock, request: str) -> tuple[bool, str | None]:
    """Wait for an instance that holds ``lock`` but did not answer ``request``.

    Such an instance is usually shutting down: ``aboutToQuit`` stops the camera
    with the event loop already stopped, while the lock (and the socket, which
    still accepts connections) are released only afterwards. Giving up at once
    would leave nothing running once it has exited: the classic "quit from the
    tray and start again right away". So until :data:`HANDOVER_WAIT_MS` has
    passed, this takes the lock as soon as it is free, or hands ``request``
    over if the instance answers after all.

    Returns ``(owner, reply)``: ``(True, None)`` when this process now holds
    the lock (it becomes the instance); otherwise the last reply (``None``, or
    a "starting or shutting down" error) or the first real answer.
    """
    log.info("The running instance does not answer; waiting for it (it may be shutting down)")
    deadline = time.monotonic() + HANDOVER_WAIT_MS / 1000.0
    reply: str | None = None
    while True:
        if lock.acquire():
            log.info("The previous instance has exited; starting")
            return True, None
        reply = ipc.send_command(request, timeout_ms=_HANDOVER_REPLY_MS)
        if reply is not None and not _is_busy(reply):
            return False, reply
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return False, reply
        _wait_ms(min(_HANDOVER_RETRY_MS, remaining_ms))


def _wait_ms(ms: int) -> None:
    """Wait ``ms`` while still processing timers and socket events (not user input)."""
    loop = QEventLoop()
    QTimer.singleShot(max(1, int(ms)), loop.quit)
    loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)


def _warn_startup_options_ignored(args: argparse.Namespace) -> None:
    """Say which options had no effect because the request went to a running instance.

    ``--trace``, ``--camera`` and ``--backend`` are applied when an instance
    starts; a launch that only hands a request over cannot change the running
    one, and silently dropping them left users waiting for a trace file that
    never appeared.
    """
    ignored = [
        option for attribute, option in _STARTUP_ONLY_OPTIONS if getattr(args, attribute, None)
    ]
    if not ignored:
        return
    log.warning(
        "%s is already running, so %s %s not applied: %s only when %s starts. Quit it "
        "first (tray menu > Quit %s, or '%s'), then run the command again.",
        APP_NAME,
        ", ".join(ignored),
        "was" if len(ignored) == 1 else "were",
        "it applies" if len(ignored) == 1 else "they apply",
        APP_NAME,
        APP_NAME,
        cli_command_text("ctl", "quit"),
    )


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> dict[str, tuple[str, str]]:
    """Apply ``--camera`` / ``--backend`` for this session.

    Returns ``{"section.field": (saved value, session value)}``.
    """
    overrides: dict[str, tuple[str, str]] = {}
    for option, key in _OVERRIDABLE.items():
        value = getattr(args, option, None)
        if not value:
            continue
        value = str(value).strip()
        saved = str(_get_setting(settings, key))
        _set_setting(settings, key, value)
        overrides[key] = (saved, value)
        # Camera values can be file paths; keep them out of the log.
        shown = value if option == "backend" else "(from the command line)"
        log.info("Setting %s overridden for this session: %s", key, shown)
    return overrides


def _get_setting(settings: Settings, key: str) -> Any:
    section, _, name = key.partition(".")
    return getattr(getattr(settings, section), name)


def _set_setting(settings: Settings, key: str, value: Any) -> None:
    section, _, name = key.partition(".")
    setattr(getattr(settings, section), name, value)


def _configure_qt_app(qt_app: QApplication) -> None:
    qt_app.setApplicationName(APP_NAME)
    qt_app.setApplicationDisplayName(APP_NAME)
    qt_app.setApplicationVersion(__version__)
    qt_app.setOrganizationName(APP_AUTHOR)
    # Wayland app id / .desktop association (packaging/linux/eye-tracker.desktop).
    qt_app.setDesktopFileName(APP_SLUG)
    # A tray app keeps running when its last window closes.
    qt_app.setQuitOnLastWindowClosed(False)
    try:
        from .ui.icons import app_icon

        qt_app.setWindowIcon(app_icon())
    except Exception:
        log.warning("Could not create the application icon", exc_info=True)


def _present(widget: QWidget) -> None:
    if widget.isMinimized():
        widget.setWindowState(widget.windowState() & ~Qt.WindowState.WindowMinimized)
    widget.show()
    widget.raise_()
    widget.activateWindow()


def _dispose_widget(widget: QWidget) -> None:
    with contextlib.suppress(Exception):
        widget.close()
    with contextlib.suppress(RuntimeError):
        widget.deleteLater()


def _noop() -> None:
    """Timer slot that only hands control to the Python interpreter."""


@contextlib.contextmanager
def _quit_on_signals(request_quit: Callable[[], None]) -> Iterator[None]:
    """Quit cleanly on Ctrl+C (and SIGTERM outside Windows) while the event loop runs.

    Python runs signal handlers only between bytecodes, which never happens while
    Qt's C++ event loop waits. A timer that calls a Python no-op hands control
    back regularly so the handler gets a chance to run.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    wanted: list[signal.Signals] = [signal.SIGINT]
    if sys.platform != "win32":
        wanted.append(signal.SIGTERM)
    previous: dict[signal.Signals, Any] = {}

    def handler(signum: int, _frame: object) -> None:
        log.info("Received signal %s; quitting", signum)
        QTimer.singleShot(0, request_quit)

    for sig in wanted:
        try:
            if signal.getsignal(sig) is signal.SIG_IGN:
                continue  # e.g. started in the background by a shell, or with nohup
            previous[sig] = signal.signal(sig, handler)
        except (OSError, ValueError):
            log.debug("Cannot handle signal %s", sig, exc_info=True)
    ticker = QTimer()
    ticker.setInterval(SIGNAL_POLL_MS)
    ticker.timeout.connect(_noop)
    ticker.start()
    try:
        yield
    finally:
        ticker.stop()
        for sig, old in previous.items():
            with contextlib.suppress(OSError, ValueError, TypeError):
                signal.signal(sig, old)


def _show_error(qt_app: QApplication, text: str) -> None:
    """Tell a user without a console that startup failed (the log has details)."""
    if qt_app.platformName() in ("offscreen", "minimal"):
        return
    QMessageBox.critical(None, APP_NAME, text)


def _isatty(stream: Any) -> bool:
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False

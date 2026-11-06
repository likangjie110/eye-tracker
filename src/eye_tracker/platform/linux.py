"""Linux integration for X11 and Wayland sessions.

Design
------
Linux has no single desktop API, so every feature is a short list of
strategies tried in order, gated by what the session actually offers:

* **X11** gets the full feature set in-process: idle time from the
  MIT-SCREEN-SAVER extension (``libXss`` through ``ctypes``), window focus
  through EWMH and display power through the DPMS extension (both with
  ``python-xlib``), cursor warping through Qt.
* **Wayland** forbids global window inspection and pointer warping by design.
  The app still runs (Qt prefers XWayland so calibration windows can be
  positioned), presence and screen locking work through logind and
  desktop-specific tools, the cursor moves through the compositor's own IPC
  (sway, Hyprland) or ``ydotool``, and window focus is reported as unsupported.

Queries that are polled (idle time, lock state) go through D-Bus in-process
(QtDBus) once a Qt application exists, and fall back to ``gdbus``/``loginctl``.
Desktop tools are run with ``subprocess`` - never through a shell - and a short
timeout. Everything polled is cached so a busy tick never turns into a stream
of process launches, and the camera-in-use scan runs on a background thread.

Lock detection
--------------
logind's ``LockedHint`` is only set by lockers that report it (GNOME Shell,
KDE, light-locker ...). Tiling-WM lockers (swaylock, i3lock via xss-lock,
hyprlock, slock ...) never do, so a running locker process of this user's
login counts as "locked" as well (setuid lockers such as slock included, see
:meth:`LinuxPlatform._locker_running`), and ``loginctl lock-session`` only
counts as a successful lock when something reacted to it (see
:meth:`LinuxPlatform.lock_screen`). Tiling compositors that pose as GNOME or
KDE through ``XDG_CURRENT_DESKTOP`` are recognised by their IPC sockets.

Camera use by other apps
------------------------
A background thread looks for processes holding a physical ``/dev/video*``
node (``/proc/<pid>/fd``). With fanotify (unprivileged since Linux 5.13) it is
woken by every open and close of a camera node and can tell our own from other
processes'. V4L2 lets only one process stream, so another app that opens the
camera read-write while we stream and closes it again at once (a browser
joining a call gets EBUSY) was refused; the camera is then reported busy for a
grace period so that app can retry. With inotify only
(no process attribution) holders are still found as soon as they open the
device; without either, the scan is polled. Apps that capture through
PipeWire's camera portal never open the node themselves and cannot be seen;
``pause_for_apps`` covers those.

Cursor on Wayland
-----------------
``ydotool`` (1.x, with ``ydotoold`` running) moves the pointer through a
virtual uinput mouse. Its "absolute" move is emulated with relative motion,
so the compositor's pointer acceleration distorts it unless the acceleration
profile is flat. The injected motion also resets GNOME's idle timer; such
resets are recognised and not reported as user input.

The module imports on every OS (the test suite imports it on Windows and
macOS): ``python-xlib``, QtDBus and the X libraries are loaded lazily, and
every public method degrades to ``None``/``False`` instead of raising.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import ClassVar

from ..types import AppIdentity, Rect, WindowRef
from .base import PlatformServices
from .linux_camera import (
    _CameraNotifier,
    _CameraWatcher,
    _open_camera_notifier,
)
from .linux_common import (
    _ACTION_TIMEOUT_S,
    _DBUS_PROPERTIES,
    _DPMS_WATCH_INTERVAL_S,
    _DPMS_WATCH_MAX_S,
    _IDLE_QUERY_TIMEOUT_S,
    _IDLE_TRANSIENT_RETRY_S,
    _IDLE_TTL_S,
    _INJECTION_WINDOW_S,
    _LOCK_CONFIRM_POLL_S,
    _LOCK_CONFIRM_S,
    _LOCKED_HINT_TTL_S,
    _LOCKER_COMMS,
    _LOGIN1,
    _LOGIN1_MANAGER,
    _LOGIN1_PATH,
    _LOGIN1_SESSION,
    _MUTTER_IDLE,
    _MUTTER_IDLE_PATH,
    _MUTTER_IDLE_SERVICE,
    _QUERY_TIMEOUT_S,
    _RETRY_AFTER_FAILURE_S,
    _SESSION_ID_RE,
    _SESSION_RETRY_S,
    _VIDEO_NODE_RE,
    DEV_ROOT,
    PROC_ROOT,
    SYS_ROOT,
    _readlink,
)
from .linux_dbus import (
    _QtDBus,
)
from .linux_desktop import (
    _as_xid,
    _belongs_to_user,
    _desktop_handles_logind_lock,
    _display_power_commands,
    _getuid,
    _is_camera_broker,
    _is_gnome_env,
    _is_missing_service,
    _is_wayland_env,
    _lock_commands,
    _lock_method_label,
    _locked_hint_authoritative,
    _login_uid,
    _maps_video_device,
    _module_available,
    _parse_gdbus_uint,
    _parse_locked_hint,
    _parse_xset_dpms,
    _pointer_command_ok,
    _read_text,
    _video_target,
    _xcb_plugin_usable,
    _ydotool_socket_present,
)
from .linux_x11 import (
    _EwmhClient,
    _XssIdle,
)

__all__ = ["DEV_ROOT", "PROC_ROOT", "SYS_ROOT", "LinuxPlatform"]


log = logging.getLogger(__name__)


class LinuxPlatform(PlatformServices):
    """Linux implementation of :class:`PlatformServices` (X11 and Wayland).

    The keyword arguments exist for tests; production code uses the defaults.
    ``background_threads=False`` keeps all work on the calling thread.
    """

    name: ClassVar[str] = "linux"

    def __init__(
        self,
        *,
        proc_root: str | os.PathLike[str] = PROC_ROOT,
        dev_root: str | os.PathLike[str] = DEV_ROOT,
        sys_root: str | os.PathLike[str] = SYS_ROOT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        background_threads: bool = True,
        camera_notifier_factory: Callable[[Sequence[str]], _CameraNotifier | None] = (
            _open_camera_notifier
        ),
    ) -> None:
        self._proc_root = Path(proc_root)
        self._dev_root = Path(dev_root)
        self._sys_root = Path(sys_root)
        self._clock = clock
        self._sleep = sleep
        self._background = background_threads
        #: Variables set by ``prepare_process`` -> their original value (None = unset),
        #: restored for child processes (see ``_child_env``).
        self._env_overrides: dict[str, str | None] = {}
        self._xss = _XssIdle(clock)
        self._ewmh = _EwmhClient(clock=clock)
        self._dbus = _QtDBus(clock)

        self._session_lock = threading.Lock()
        self._resolved_session: str | None = None
        self._session_retry_at = -math.inf
        self._session_paths: dict[str, str] = {}  # logind session id -> D-Bus object path

        self._locked_lock = threading.Lock()
        self._locked_cache: tuple[float, bool | None] | None = None

        self._idle_lock = threading.Lock()
        self._last_input_at: float | None = None  # clock time of the last user input
        self._idle_checked_at = -math.inf
        self._idle_retry_at = -math.inf
        self._idle_ever_ok = False
        self._injected: tuple[float, float] | None = None  # our last ydotool move

        self._pointer_lock = threading.Lock()
        self._ydotool_cli: str | None = None  # "1" (usable CLI), "0" (0.1.x), "" (unknown)
        self._ydotool_probed_at = -math.inf
        self._ydotool_warned = False

        self._dpms_lock = threading.Lock()
        self._dpms_restore: str | None = None  # "xlib" / "xset": DPMS was off before
        self._dpms_watch: threading.Thread | None = None

        self._camera = _CameraWatcher(
            devices=self._physical_video_devices,
            scan=self._scan_camera_users,
            own_use=self._holds_camera_ourselves,
            clock=clock,
            threaded=background_threads,
            notifier_factory=camera_notifier_factory,
        )

    # ---------------------------------------------------------------- session
    @property
    def is_wayland(self) -> bool:
        return _is_wayland_env(os.environ)

    def _x11_session(self) -> bool:
        """A real X11 session (XWayland does not count: it only sees X11 clients)."""
        return not self.is_wayland and bool(os.environ.get("DISPLAY"))

    # -------------------------------------------------------------- lifecycle
    def prepare_process(self) -> None:
        """Unscaled Qt, and XWayland instead of native Wayland when possible.

        Native Wayland clients cannot position windows or read global
        coordinates, which calibration windows and the gaze overlay rely on.
        """
        if "QT_ENABLE_HIGHDPI_SCALING" not in os.environ:
            self._set_env("QT_ENABLE_HIGHDPI_SCALING", "0")
        if not (self.is_wayland and os.environ.get("DISPLAY")):
            return
        if os.environ.get("QT_QPA_PLATFORM"):
            return  # the user chose a platform plugin explicitly
        if _xcb_plugin_usable():
            # A list, not just "xcb": if the xcb plugin still cannot load (another
            # xcb-util library missing) Qt falls back to native Wayland instead of
            # aborting at start-up.
            self._set_env("QT_QPA_PLATFORM", "xcb;wayland")
            log.info("Wayland session: preferring XWayland (QT_QPA_PLATFORM=xcb;wayland)")
        else:
            log.warning(
                "Wayland session without libxcb-cursor0: running as a native Wayland client; "
                "calibration windows may be misplaced. Install libxcb-cursor0 to fix this."
            )

    def _set_env(self, key: str, value: str) -> None:
        self._env_overrides.setdefault(key, os.environ.get(key))
        os.environ[key] = value

    def _child_env(self) -> dict[str, str]:
        """Environment for desktop tools: as the user's session, not as ours."""
        env = dict(os.environ)
        # kscreen-doctor & co. are Qt programs: an inherited QT_QPA_PLATFORM=xcb would
        # push them onto XWayland, where e.g. DPMS control does not exist.
        for key, original in self._env_overrides.items():
            if original is None:
                env.pop(key, None)
            else:
                env[key] = original
        # PyInstaller (and AppImage runtimes) point LD_LIBRARY_PATH at bundled
        # libraries, which can break system binaries linked against newer ones.
        original_path = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original_path is not None:
            if original_path:
                env["LD_LIBRARY_PATH"] = original_path
            else:
                env.pop("LD_LIBRARY_PATH", None)
        elif getattr(sys, "frozen", False):
            env.pop("LD_LIBRARY_PATH", None)
        return env

    def capabilities(self) -> dict[str, bool]:
        try:
            env = os.environ
            x11 = self._x11_session()
            has_display = bool(env.get("DISPLAY"))
            xlib = _module_available("Xlib")
            gnome_idle = _is_gnome_env(env) and shutil.which("gdbus") is not None
            x11_dpms = False
            if x11:
                status = self._ewmh.dpms_status()  # None without python-xlib
                x11_dpms = status[0] if status is not None else shutil.which("xset") is not None
            return {
                "lock": any(_available(cmd) for cmd in _lock_commands(None)),
                "display_off": x11_dpms
                or any(_available(c) for c in _display_power_commands(env, False)),
                "wake_display": x11_dpms
                or any(_available(c) for c in _display_power_commands(env, True))
                or (x11 and xlib),
                "input_idle": (x11 and self._xss.available()) or gnome_idle,
                "key_idle": False,
                "session_locked": shutil.which("loginctl") is not None or self._proc_root.is_dir(),
                "focus": x11 and xlib,
                "cursor": (not self.is_wayland)
                or next(self._wayland_pointer_commands(0, 0), None) is not None,
                "camera_in_use": self._proc_root.is_dir(),
                # Same rule as platform.hotkeys: X11 key grabs (also through XWayland).
                "hotkeys": has_display and xlib,
                # Needs the focused window and its geometry, like "focus".
                "panes": x11 and xlib,
            }
        except Exception:
            log.debug("capability probe failed", exc_info=True)
            return super().capabilities()

    # ----------------------------------------------------------- subprocesses
    def _run(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
        """Run a desktop tool; ``None`` when it is missing, hangs or cannot start."""
        exe = shutil.which(argv[0])
        if exe is None:
            return None
        try:
            return subprocess.run(
                [exe, *argv[1:]],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self._child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.debug("%s timed out after %.1f s", argv[0], timeout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.debug("%s failed to run: %s", argv[0], exc)
        return None

    def _first_success(self, commands: Iterable[Sequence[str]], timeout: float) -> str | None:
        """Run commands in order until one exits with 0; return its name."""
        for argv in commands:
            result = self._run(argv, timeout)
            if result is None:
                continue
            if result.returncode == 0:
                return " ".join(argv[:2])
            log.debug(
                "%s exited with %s: %s",
                " ".join(argv[:2]),
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
        return None

    # -------------------------------------------------------------- lock screen
    def lock_screen(self) -> bool:
        """Lock the session with the first locker that demonstrably reacts.

        ``loginctl lock-session`` exits with 0 whether or not anything listens
        to logind's ``Lock`` signal (bare i3/sway without xss-lock or a swayidle
        ``lock`` hook). Outside desktops known to listen, it only counts once
        ``LockedHint`` is set or a locker process appeared; otherwise the other
        lockers are tried, and ``False`` tells the caller nothing locked.

        On GNOME and KDE, whose lock screens always set ``LockedHint``, every
        locker's success is checked that way: GNOME Shell ignores lock requests
        when locking is disabled by policy (``disable-lock-screen``), while
        ``loginctl`` and the D-Bus calls still report success. Only a hint that
        cannot be read at all falls back to trusting the desktop.
        """
        try:
            used = self._lock_with_tools()
        except Exception:
            log.debug("lock_screen failed", exc_info=True)
            used = None
        if used is None:
            log.warning("Could not lock the screen: no screen locker responded")
            return False
        log.info("Screen locked via %s", used)
        with self._locked_lock:
            self._locked_cache = None  # the next poll must see the new state
        return True

    def lock_methods(self) -> list[str]:
        """The screen-lock tools installed here, in the order :meth:`lock_screen` tries them.

        Shown by ``doctor``. Being installed is not proof that a tool locks this
        session: ``loginctl lock-session`` exists on every systemd system, but on
        a bare i3 or sway session nothing listens to logind's ``Lock`` signal.
        Whether one works is only known when it is tried; :meth:`lock_screen`
        checks that and reports failure. Never raises.
        """
        try:
            handled = _desktop_handles_logind_lock(os.environ)
            methods: list[str] = []
            for argv in _lock_commands(None):
                if shutil.which(argv[0]) is None:
                    continue
                label = _lock_method_label(argv)
                if argv[0] == "loginctl" and not handled:
                    label += " (works only if a locker such as xss-lock listens to logind)"
                methods.append(label)
            return methods
        except Exception:
            log.debug("Listing the lock tools failed", exc_info=True)
            return []

    def _lock_with_tools(self) -> str | None:
        session = self._session_id()
        # Where LockedHint is authoritative, any locker's "success" can be checked.
        verify_all = session is not None and _locked_hint_authoritative(os.environ)
        # A desktop with a lock screen of its own: the display manager's lock
        # (dm-tool switches to the greeter) would come on top of it.
        own_locker = _desktop_handles_logind_lock(os.environ)
        unconfirmed: str | None = None  # a locker ran, but the screen did not lock in time
        for argv in _lock_commands(session):
            if shutil.which(argv[0]) is None:
                continue
            if unconfirmed is not None and self._lock_evident(session):
                return unconfirmed  # a slow locker reacted after all: do not stack another
            if unconfirmed is not None and own_locker and argv[0] == "dm-tool":
                # The desktop's locker said it locked but nothing shows it (or it
                # refused by policy): a greeter on top would make the user unlock
                # twice, or lock them again right after they unlocked.
                log.debug("Not stacking dm-tool lock on the desktop's own lock screen")
                continue
            result = self._run(argv, _ACTION_TIMEOUT_S)
            if result is None:
                continue
            name = " ".join(argv[:2])
            if result.returncode != 0:
                log.debug(
                    "%s exited with %s: %s",
                    name,
                    result.returncode,
                    (result.stderr or "").strip()[:200],
                )
                continue
            if argv[0] == "loginctl":
                confirmed = self._logind_lock_took_effect(session)
            else:
                confirmed = not verify_all or self._lock_confirmed(session, trust_unknown=True)
            if not confirmed:
                log.info(
                    "%s: the screen did not lock within %.0f s; trying other lockers",
                    name,
                    _LOCK_CONFIRM_S,
                )
                if unconfirmed is None:
                    unconfirmed = name
                continue
            return name
        # The last attempt may have been answered just after its wait.
        if unconfirmed is not None and self._lock_evident(session):
            return unconfirmed
        return None

    def _logind_lock_took_effect(self, session: str | None) -> bool:
        """Whether something handled logind's ``Lock`` signal (see :meth:`lock_screen`)."""
        env = os.environ
        if not _desktop_handles_logind_lock(env):
            return self._lock_confirmed(session, trust_unknown=False)
        if session is not None and _locked_hint_authoritative(env):
            return self._lock_confirmed(session, trust_unknown=True)
        # Cinnamon, MATE, Xfce ... listen to logind, but not all of their lock
        # screens report the hint: nothing reliable to check.
        return True

    def _lock_confirmed(self, session: str | None, *, trust_unknown: bool) -> bool:
        """Wait up to ``_LOCK_CONFIRM_S`` for the lock to show (``LockedHint`` or a locker).

        ``trust_unknown``: count it as locked when the hint could not be read
        at all (no evidence either way) and no locker process appeared.
        """
        deadline = self._clock() + _LOCK_CONFIRM_S
        hint_known = False
        while True:
            if session is not None:
                hint = self._logind_locked_hint(session)
                if hint is True:
                    return True
                hint_known = hint_known or hint is not None
            if self._locker_running():
                return True
            if self._clock() >= deadline:
                return trust_unknown and not hint_known
            self._sleep(_LOCK_CONFIRM_POLL_S)

    def _lock_evident(self, session: str | None) -> bool:
        """Whether the session shows as locked right now (locker process or hint).

        The process scan comes first: without QtDBus the hint costs a
        ``loginctl`` launch.
        """
        if self._locker_running():
            return True
        return session is not None and self._logind_locked_hint(session) is True

    # ------------------------------------------------------------- lock state
    def is_session_locked(self) -> bool | None:
        now = self._clock()
        with self._locked_lock:
            cached = self._locked_cache
            if cached is not None and now - cached[0] < _LOCKED_HINT_TTL_S:
                return cached[1]
            try:
                value = self._query_locked_state()
            except Exception:
                log.debug("lock state query failed", exc_info=True)
                value = None
            self._locked_cache = (now, value)
            return value

    def _query_locked_state(self) -> bool | None:
        """logind's ``LockedHint``, or a running locker that does not report to it."""
        session = self._session_id()
        hint = self._logind_locked_hint(session) if session is not None else None
        if hint is True:
            return True
        if hint is False and _locked_hint_authoritative(os.environ):
            return False  # GNOME / KDE always set the hint: no need to scan processes
        locker = self._locker_running()
        if locker:
            return True
        if hint is not None:
            return hint
        # logind did not answer. Without a session to ask, a locker process is the
        # only evidence there is; with one, a failed query means "unknown" rather
        # than "unlocked", so a real lock screen is never mistaken for a return.
        return locker if session is None else None

    def _logind_locked_hint(self, session: str) -> bool | None:
        answered, value = self._locked_hint_dbus(session)
        if answered:
            return value
        result = self._run(
            ["loginctl", "show-session", session, "-p", "LockedHint", "--value"],
            _QUERY_TIMEOUT_S,
        )
        if result is None or result.returncode != 0:
            return None
        return _parse_locked_hint(result.stdout)

    def _locked_hint_dbus(self, session: str) -> tuple[bool, bool | None]:
        """``(answered, LockedHint)`` from logind over D-Bus, without a process launch."""
        with self._session_lock:
            path = self._session_paths.get(session)
        if path is None:
            reply = self._dbus.call(
                system=True,
                service=_LOGIN1,
                path=_LOGIN1_PATH,
                interface=_LOGIN1_MANAGER,
                method="GetSession",
                args=(session,),
            )
            if reply is None:
                return False, None
            if not reply.ok:
                log.debug("logind GetSession(%s) failed: %s", session, reply.error)
                return True, None
            path = reply.values[0] if reply.values else None
            if not isinstance(path, str) or not path.startswith("/"):
                log.debug("Unexpected GetSession reply %r; using loginctl", reply.values)
                return False, None
            with self._session_lock:
                self._session_paths[session] = path
        reply = self._dbus.call(
            system=True,
            service=_LOGIN1,
            path=path,
            interface=_DBUS_PROPERTIES,
            method="Get",
            args=(_LOGIN1_SESSION, "LockedHint"),
        )
        if reply is None:
            return False, None
        if reply.ok:
            value = reply.values[0] if reply.values else None
            if isinstance(value, bool):
                return True, value
            log.debug("Unexpected LockedHint reply %r; using loginctl", reply.values)
            return False, None
        log.debug("LockedHint of session %s unavailable: %s", session, reply.error)
        with self._session_lock:
            self._session_paths.pop(session, None)  # the session may have ended
        return True, None

    def _locker_running(self) -> bool | None:
        """Whether a screen locker of this user's login runs; ``None`` without ``/proc``.

        Every process is looked at, not only those owned by this user: slock
        (and other lockers installed setuid root, e.g. swaylock built for
        shadow passwords, xlock) run as another user or have dropped privileges
        in a way that makes their ``/proc`` entry root's. ``comm`` is readable
        for every process, and so is ``loginuid``, which the audit subsystem
        sets at login and which no setuid changes: a locker counts as ours when
        we own it or when its login UID is ours (see :func:`_belongs_to_user`).
        (Not the audit *session* id: an app started by the systemd user manager
        has a different one than the graphical session its locker runs in.)
        Without login UIDs (no audit support) only the owner tells, as before.
        """
        root = str(self._proc_root)
        if not os.path.isdir(root):
            return None
        own_login = _login_uid(os.path.join(root, "self"))
        uid = _getuid()
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.name.isdigit():
                        continue
                    comm = _read_text(os.path.join(entry.path, "comm")).strip()
                    if comm in _LOCKER_COMMS and _belongs_to_user(entry, own_login, uid):
                        return True
        except OSError:
            return None
        return False

    def _session_id(self) -> str | None:
        """The logind session of this desktop.

        ``XDG_SESSION_ID`` is missing when the app is started by a systemd user
        unit; the user's graphical ("Display") session is used then. A failed
        lookup (logind still busy at login) is retried every 30 s.
        """
        env_id = os.environ.get("XDG_SESSION_ID", "").strip()
        if env_id:
            return env_id if _SESSION_ID_RE.fullmatch(env_id) else None
        with self._session_lock:
            if self._resolved_session:
                return self._resolved_session
            now = self._clock()
            if now < self._session_retry_at:
                return None
            found = self._lookup_display_session()
            if found:
                self._resolved_session = found
                return found
            self._session_retry_at = now + _SESSION_RETRY_S
            return None

    def _lookup_display_session(self) -> str:
        uid = _getuid()
        if uid is None:
            return ""
        result = self._run(
            ["loginctl", "show-user", str(uid), "-p", "Display", "--value"],
            _QUERY_TIMEOUT_S,
        )
        if result is None or result.returncode != 0:
            return ""
        lines = result.stdout.strip().splitlines()
        candidate = lines[0].strip() if lines else ""
        if "=" in candidate:
            candidate = candidate.split("=", 1)[1].strip()
        return candidate if _SESSION_ID_RE.fullmatch(candidate) else ""

    # ----------------------------------------------------------- display power
    def display_off(self) -> bool:
        try:
            if self._x11_session() and self._x11_dpms(off=True):
                log.debug("Displays turned off via X11 DPMS")
                return True
            used = self._first_success(
                _display_power_commands(os.environ, on=False), _ACTION_TIMEOUT_S
            )
        except Exception:
            log.debug("display_off failed", exc_info=True)
            return False
        if used is None:
            log.debug("No display power control available for this session")
            return False
        log.debug("Displays turned off via %s", used)
        return True

    def wake_display(self) -> bool:
        try:
            if self._x11_session() and self._x11_dpms(off=False):
                return True
            used = self._first_success(
                _display_power_commands(os.environ, on=True), _ACTION_TIMEOUT_S
            )
            if used is not None:
                return True
            # Any input wakes DPMS-blanked X11 screens; XTest synthesises some.
            return self._x11_session() and self._ewmh.nudge_pointer()
        except Exception:
            log.debug("wake_display failed", exc_info=True)
            return False

    def _x11_dpms(self, off: bool) -> bool:
        """Monitor power through the X server's DPMS extension.

        Forcing a power level requires DPMS to be enabled. A user who disabled it
        (``xset -dpms``: kiosks, presentations) gets it disabled again once the
        monitors are back on, and a server without DPMS (Xvnc, xrdp, many VMs)
        reports failure instead of pretending, so the caller can tell the user.
        """
        status = self._ewmh.dpms_status()
        if status is None:
            return self._xset_dpms(off)
        capable, enabled = status
        if not capable:
            log.debug("The X server has no usable DPMS")
            return False
        start_watch = False
        with self._dpms_lock:
            if not off:
                ok = self._ewmh.dpms_force(off=False)
                self._restore_dpms_locked()
                return ok
            if not enabled:
                if not self._ewmh.dpms_set_enabled(True):
                    return False
                self._dpms_restore = "xlib"
            ok = self._ewmh.dpms_force(off=True)
            if not ok:
                self._restore_dpms_locked()
            start_watch = ok and self._dpms_restore == "xlib"
        if start_watch and self._background:
            self._start_dpms_watch()
        return ok

    def _xset_dpms(self, off: bool) -> bool:
        """``xset`` fallback when python-xlib is unavailable."""
        if shutil.which("xset") is None:
            return False
        query = self._run(["xset", "q"], _QUERY_TIMEOUT_S)
        state = _parse_xset_dpms(query.stdout) if query and query.returncode == 0 else None
        if state is not None and not state[0]:
            # xset would print a complaint and still exit with 0.
            log.debug("The X server has no usable DPMS")
            return False
        with self._dpms_lock:
            if off and state is not None and not state[1]:
                self._dpms_restore = "xset"  # "xset dpms force" enables DPMS as a side effect
            result = self._run(["xset", "dpms", "force", "off" if off else "on"], _ACTION_TIMEOUT_S)
            ok = result is not None and result.returncode == 0
            if not off or not ok:
                self._restore_dpms_locked()
        return ok

    def _restore_dpms_locked(self) -> None:
        mode, self._dpms_restore = self._dpms_restore, None
        if mode == "xlib":
            self._ewmh.dpms_set_enabled(False)
        elif mode == "xset":
            self._run(["xset", "-dpms"], _ACTION_TIMEOUT_S)

    def _restore_dpms_if_awake(self) -> bool:
        """Disable DPMS again once the monitors are on; ``True`` when nothing is left to do."""
        with self._dpms_lock:
            if self._dpms_restore != "xlib":
                return True
            if self._ewmh.dpms_monitors_on() is not True:
                return False
            log.debug("Monitors woke up; disabling DPMS again as the user had it")
            self._restore_dpms_locked()
            return True

    def _start_dpms_watch(self) -> None:
        # Input wakes the monitors without us (e.g. wake_on_return is off), and
        # DPMS must not stay enabled with the server's blanking timeouts then.
        with self._dpms_lock:
            if self._dpms_watch is not None and self._dpms_watch.is_alive():
                return
            self._dpms_watch = threading.Thread(
                target=self._watch_dpms_wake, name="dpms-restore", daemon=True
            )
            self._dpms_watch.start()

    def _watch_dpms_wake(self) -> None:
        deadline = time.monotonic() + _DPMS_WATCH_MAX_S
        while time.monotonic() < deadline:
            time.sleep(_DPMS_WATCH_INTERVAL_S)
            try:
                if self._restore_dpms_if_awake():
                    return
            except Exception:
                log.debug("DPMS watch failed", exc_info=True)
                return

    # ------------------------------------------------------------------ input
    def seconds_since_input(self) -> float | None:
        try:
            if self._x11_session():
                ms = self._xss.idle_ms()
                if ms is not None:
                    return ms / 1000.0
            return self._mutter_idle_s()
        except Exception:
            log.debug("seconds_since_input failed", exc_info=True)
            return None

    def _mutter_idle_s(self) -> float | None:
        """Idle time from GNOME's Mutter (the only source on GNOME Wayland).

        Results are cached for a short while and extrapolated: returning a stale
        value unchanged would move the implied "last input" timestamp forward and
        look like fresh (keyboard) activity. An idle reset caused by our own
        ``ydotool`` motion is not user input and keeps the previous timestamp.
        """
        now = self._clock()
        with self._idle_lock:
            last = self._last_input_at
            if last is not None and now - self._idle_checked_at < _IDLE_TTL_S:
                return max(0.0, now - last)
            if now < self._idle_retry_at:
                return None
            ms, missing = self._query_mutter_idle_ms()
            if ms is None:
                self._last_input_at = None
                # Back off for long only where the service does not exist (KDE,
                # sway ...); a hiccup of a working gnome-shell is retried soon.
                permanent = missing or not self._idle_ever_ok
                delay = _RETRY_AFTER_FAILURE_S if permanent else _IDLE_TRANSIENT_RETRY_S
                self._idle_retry_at = now + delay
                log.debug("Mutter idle monitor unavailable; retrying in %.0f s", delay)
                return None
            self._idle_ever_ok = True
            self._idle_checked_at = now
            observed = now - ms / 1000.0
            current = last if last is not None and self._injected_input_at(observed) else observed
            self._last_input_at = current
            return max(0.0, now - current)

    def _injected_input_at(self, when: float) -> bool:
        injected = self._injected
        if injected is None:
            return False
        started, finished = injected
        return started - 0.05 <= when <= finished + _INJECTION_WINDOW_S

    def _query_mutter_idle_ms(self) -> tuple[int | None, bool]:
        """``(idle ms, service missing)``: in-process D-Bus first, ``gdbus`` otherwise."""
        reply = self._dbus.call(
            system=False,
            service=_MUTTER_IDLE_SERVICE,
            path=_MUTTER_IDLE_PATH,
            interface=_MUTTER_IDLE_SERVICE,
            method="GetIdletime",
        )
        if reply is not None and not reply.ok:
            return None, _is_missing_service(reply.error)
        if reply is not None:
            value = reply.values[0] if reply.values else None
            if isinstance(value, int) and not isinstance(value, bool):
                return max(0, value), False
            # An answer QtDBus did not convert as expected: never let that hide
            # the idle time for good - use gdbus instead.
            log.debug("Unexpected GetIdletime reply %r; using gdbus", reply.values)
        result = self._run(_MUTTER_IDLE, _IDLE_QUERY_TIMEOUT_S)
        if result is None:
            # Missing gdbus is permanent; a timeout is a busy gnome-shell.
            return None, shutil.which("gdbus") is None
        if result.returncode == 0:
            ms = _parse_gdbus_uint(result.stdout)
            if ms is not None:
                return ms, False
        return None, _is_missing_service(f"{result.stdout}\n{result.stderr}")

    # ----------------------------------------------------------------- cursor
    def cursor_position_reliable(self) -> bool:
        """``False`` on Wayland: XWayland only sees the pointer over X11 windows."""
        return not self.is_wayland

    def move_cursor(self, x: int, y: int) -> bool | None:
        """X11: ``None`` (Qt warps the pointer). Wayland: compositor IPC or ``ydotool``.

        sway and Hyprland place the pointer exactly through their IPC. ``ydotool``
        emulates absolute moves with relative motion, which pointer acceleration
        distorts unless its virtual device uses a flat profile; that cannot be
        checked from here.
        """
        if not self.is_wayland:
            return None
        x, y = int(x), int(y)
        for argv in self._wayland_pointer_commands(x, y):
            started = self._clock()
            result = self._run(argv, _QUERY_TIMEOUT_S)
            if result is None or not _pointer_command_ok(argv, result):
                log.debug("%s could not move the pointer", argv[0])
                continue
            if argv[0] == "ydotool":
                # uinput motion resets the compositor's idle timer like real input.
                with self._idle_lock:
                    self._injected = (started, self._clock())
            return True
        return False

    def _wayland_pointer_commands(self, x: int, y: int) -> Iterator[list[str]]:
        """Pointer-warp commands usable in this Wayland session, most exact first.

        A generator: ydotool is only probed when the compositor's IPC failed.
        """
        env = os.environ
        if env.get("SWAYSOCK") and shutil.which("swaymsg"):
            # "-" is the current seat (sway >= 1.5); older versions need a name.
            for seat in ("-", "seat0"):
                yield ["swaymsg", "seat", seat, "cursor", "set", str(x), str(y)]
        if env.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
            yield ["hyprctl", "dispatch", "movecursor", str(x), str(y)]
        if self._ydotool_usable():
            yield ["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)]

    def _ydotool_usable(self) -> bool:
        """ydotool 1.x (``--absolute``) with its daemon running."""
        if shutil.which("ydotool") is None:
            return False
        with self._pointer_lock:
            now = self._clock()
            if not self._ydotool_cli and now >= self._ydotool_probed_at + _RETRY_AFTER_FAILURE_S:
                self._ydotool_probed_at = now
                result = self._run(["ydotool", "mousemove", "--help"], _QUERY_TIMEOUT_S)
                text = f"{result.stdout}\n{result.stderr}" if result is not None else ""
                if "--absolute" in text:
                    self._ydotool_cli = "1"
                elif result is not None:
                    self._ydotool_cli = "0"
            cli = self._ydotool_cli
            if cli == "0" and not self._ydotool_warned:
                self._ydotool_warned = True
                log.warning(
                    "ydotool is too old to move the pointer (0.1.x); install ydotool 1.x "
                    "and run ydotoold"
                )
        if cli != "1":
            return False
        if not _ydotool_socket_present(os.environ):
            log.debug("ydotool is installed but ydotoold is not running")
            return False
        return True

    # ---------------------------------------------------------------- windows
    def foreground_window(self) -> WindowRef | None:
        if not self._x11_session():
            return None
        return self._ewmh.foreground()

    def window_at(self, x: int, y: int) -> WindowRef | None:
        if not self._x11_session():
            return None
        return self._ewmh.window_at(int(x), int(y))

    def activate_window(self, ref: WindowRef) -> bool:
        wid = _as_xid(ref.handle)
        if wid is None or not self._x11_session():
            return False
        if not self._ewmh.is_valid(wid):
            return False  # minimised or gone: never un-minimise behind the user's back
        return self._ewmh.activate(wid)

    def is_window_valid(self, ref: WindowRef) -> bool:
        wid = _as_xid(ref.handle)
        if wid is None or not self._x11_session():
            return False
        return self._ewmh.is_valid(wid)

    def window_rect(self, ref: WindowRef) -> Rect | None:
        wid = _as_xid(ref.handle)
        if wid is None or not self._x11_session():
            return None
        return self._ewmh.rect(wid)

    def window_client_rect(self, ref: WindowRef) -> Rect | None:
        wid = _as_xid(ref.handle)
        if wid is None or not self._x11_session():
            return None
        return self._ewmh.client_rect(wid)

    def window_app(self, ref: WindowRef) -> AppIdentity | None:
        wid = _as_xid(ref.handle)
        if wid is None or not self._x11_session():
            return None
        found = self._ewmh.app_class(wid)
        if found is None:
            return None
        pid, wm_class = found
        pid = pid if pid is not None else ref.pid
        if ref.pid is not None and pid != ref.pid:
            return None  # the id now belongs to another client's window
        name = self._app_process_name(pid)
        if name is None:
            return None
        return AppIdentity(process=name, app_id=wm_class)

    # ----------------------------------------------------------------- camera
    def camera_in_use_by_other_app(self) -> bool | None:
        """Whether another process holds a physical camera, or was refused one just now.

        Never scans on the calling thread (see :class:`_CameraWatcher`); ``None``
        until the first background scan finished.
        """
        return self._camera.query()

    def _user_processes(self) -> list[tuple[int, str]]:
        """``(pid, /proc/<pid>)`` of this user's processes.

        Other users' file descriptors are unreadable anyway; skipping them by
        owner is much cheaper than failing on each one.
        """
        uid = _getuid()
        found: list[tuple[int, str]] = []
        with os.scandir(self._proc_root) as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                if uid is not None:
                    try:
                        if entry.stat(follow_symlinks=False).st_uid != uid:
                            continue
                    except OSError:
                        continue  # exited meanwhile
                found.append((int(entry.name), entry.path))
        return found

    def _scan_camera_users(self, devices: set[str] | None) -> bool | None:
        if not self._proc_root.is_dir():
            return None
        if devices is not None and not devices:
            return False  # no camera to compete for
        own_pid = os.getpid()
        for pid, path in self._user_processes():
            if pid != own_pid and self._holds_video_device(path, devices):
                log.debug("Camera in use by pid %s", pid)
                return True
        return False

    def _holds_camera_ourselves(self, devices: set[str] | None) -> bool:
        """Whether this process has a physical camera node open."""
        if devices is not None and not devices:
            return False
        fd_dir = self._proc_root / str(os.getpid()) / "fd"
        try:
            with os.scandir(fd_dir) as fds:
                for fd in fds:
                    try:
                        target = _readlink(fd.path)
                    except OSError:
                        continue
                    if _video_target(target, devices):
                        return True
        except OSError:
            return False
        return False

    def _holds_video_device(self, proc_dir: str, devices: set[str] | None) -> bool:
        try:
            with os.scandir(os.path.join(proc_dir, "fd")) as fds:
                for fd in fds:
                    try:
                        target = _readlink(fd.path)
                    except OSError:
                        continue
                    if not _video_target(target, devices):
                        continue
                    # PipeWire keeps camera nodes open just to monitor them; it only
                    # competes for the camera while it streams to some client.
                    if _is_camera_broker(proc_dir):
                        return _maps_video_device(proc_dir)
                    return True
        except OSError:
            # Other users' processes (permission denied) or a process that exited.
            return False
        return False

    def _physical_video_devices(self) -> set[str] | None:
        """``/dev/videoN`` paths backed by hardware; ``None`` when unknown."""
        try:
            names = [p.name for p in self._dev_root.iterdir() if _VIDEO_NODE_RE.fullmatch(p.name)]
        except OSError:
            return None
        v4l = self._sys_root / "class" / "video4linux"
        if not v4l.is_dir():
            return {f"/dev/{name}" for name in names}
        # Loopback devices (OBS virtual camera, v4l2loopback) have no parent
        # "device" link; apps reading them never block our physical camera.
        return {
            f"/dev/{name}"
            for name in names
            if not (v4l / name).exists() or (v4l / name / "device").exists()
        }

    # ------------------------------------------------------------ permissions
    def permissions(self) -> dict[str, bool | None]:
        return {"camera": self._camera_access(), "accessibility": None}

    def _camera_access(self) -> bool | None:
        """Whether this user may open a camera node (``video`` group / logind ACL)."""
        try:
            nodes = [p for p in self._dev_root.iterdir() if _VIDEO_NODE_RE.fullmatch(p.name)]
        except OSError:
            return None
        if not nodes:
            return None
        return any(os.access(node, os.R_OK | os.W_OK) for node in nodes)


def _available(argv: Sequence[str]) -> bool:
    return shutil.which(argv[0]) is not None

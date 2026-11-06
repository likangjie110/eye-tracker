"""The Windows platform policy (phase 4 / B2).

Window selection, keyboard-focus acquisition despite the foreground lock, monitor power and the
camera-in-use checks. The Win32 binding lives in ``windows_api.py``; this module holds the policy
and the camera-usage helpers, and degrades to ``None``/``False`` instead of raising.
"""

from __future__ import annotations

import logging
import ntpath
import os
import sys
import threading

from .. import APP_ID
from ..types import AppIdentity, Rect, WindowRef
from .base import PlatformServices
from .windows_api import (
    _BOOT_TIME_SLACK,
    _MAX_Z_WALK,
    _NON_PACKAGED,
    _SESSION_START_SLACK,
    _SETTINGS_URIS,
    _SHELL_CLASSES,
    _TRANSIENT_CLASSES,
    _WEBCAM_KEY,
    MONITOR_POWER_OFF,
    SC_MONITORPOWER,
    WM_SYSCOMMAND,
    WS_EX_NOACTIVATE,
    WS_EX_TRANSPARENT,
    _active_camera_users,
    _best_effort,
    _CameraUser,
    _consent_value,
    _hwnd_of,
    _norm_path,
    _open_uri,
    _Process,
    _real_path,
    _Registry,
    _unix_to_filetime,
    _Win32,
    _WinRegistry,
)

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ platform
_KIND_OWN = "own"
_KIND_SHELL = "shell"
_KIND_TRANSIENT = "transient"
_KIND_NORMAL = "normal"


class WindowsPlatform(PlatformServices):
    """Windows 10/11 implementation of :class:`PlatformServices`.

    ``api`` and ``registry`` exist for tests; production code uses the defaults.
    """

    name = "windows"

    def __init__(self, api: _Win32 | None = None, registry: _Registry | None = None) -> None:
        self._api_obj = api
        self._api_error: str | None = None
        self._api_lock = threading.Lock()
        self._registry: _Registry = registry if registry is not None else _WinRegistry()
        self._pid = os.getpid()
        self._own_paths: frozenset[str] | None = None
        self._own_package_name: str | None = None

    @property
    def _api(self) -> _Win32:
        api = self._api_obj
        if api is None:
            with self._api_lock:
                if self._api_obj is None:
                    if self._api_error is not None:
                        raise OSError(self._api_error)
                    try:
                        self._api_obj = _Win32()
                    except OSError as exc:
                        # Remember the failure: callers poll several times a second.
                        self._api_error = str(exc)
                        log.warning("Windows integration unavailable: %s", exc)
                        raise
                api = self._api_obj
        return api

    # ---------------------------------------------------------------- lifecycle
    def prepare_process(self) -> None:
        """Per-Monitor-V2 DPI awareness + unscaled Qt, so Qt coordinates are physical pixels."""
        current = os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
        if current != "0":
            log.warning(
                "QT_ENABLE_HIGHDPI_SCALING=%s is set in the environment; cursor and window "
                "positions may be off on scaled displays",
                current,
            )
        try:
            api = self._api
            mode = api.enable_per_monitor_dpi()
            log.debug("DPI awareness: %s", mode)
            # Groups taskbar entries and notifications under the app, not python.exe.
            if not api.set_app_user_model_id(APP_ID):
                log.debug("Could not set the AppUserModelID")
        except Exception:
            log.debug("prepare_process failed", exc_info=True)

    def capabilities(self) -> dict[str, bool]:
        caps = super().capabilities()
        caps.update(
            lock=True,
            display_off=True,
            wake_display=True,
            input_idle=True,
            key_idle=False,
            session_locked=True,
            focus=True,
            cursor=True,
            camera_in_use=True,
            hotkeys=True,
            panes=True,
        )
        return caps

    # ------------------------------------------------------------ session/power
    @_best_effort(False)
    def lock_screen(self) -> bool:
        if self._api.lock_workstation():
            return True
        log.warning("LockWorkStation failed (locking may be disabled by policy)")
        return False

    @_best_effort(False)
    def display_off(self) -> bool:
        """Power the monitors down through ``DefWindowProc`` of a window of our own.

        Any top-level window's ``DefWindowProc`` handles ``SC_MONITORPOWER``, so
        a throw-away hidden window on the calling thread is enough, and sending
        to it is a plain function call. Broadcasting instead would queue the
        request in every window of the system: a suspended or debugger-paused
        app would run it whenever it next pumps messages, possibly long after
        the user came back, blacking the screens out again.
        """
        api = self._api
        hwnd = api.create_hidden_window()
        if hwnd is None:
            log.debug("Could not create a window for SC_MONITORPOWER; broadcasting instead")
            return api.post_broadcast(WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_POWER_OFF)
        try:
            api.send_message(hwnd, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_POWER_OFF)
        finally:
            api.destroy_window(hwnd)
        return True

    @_best_effort(False)
    def wake_display(self) -> bool:
        api = self._api
        api.set_display_required()
        before = api.cursor_pos()
        ok = api.nudge_mouse()
        if before is not None:
            # Pointer acceleration can turn +1/-1 into an uneven pair; put it back exactly.
            api.set_cursor_pos(*before)
        return ok

    @_best_effort(None)
    def is_session_locked(self) -> bool | None:
        name = self._api.input_desktop_name()
        if name is None:
            return True  # the input desktop is the secure (Winlogon) desktop
        if not name:
            return None
        return name.lower() != "default"

    # -------------------------------------------------------------------- input
    @_best_effort(None)
    def seconds_since_input(self) -> float | None:
        ms = self._api.idle_ms()
        return None if ms is None else ms / 1000.0

    @_best_effort(None)
    def move_cursor(self, x: int, y: int) -> bool | None:
        api = self._api
        # A second SetCursorPos corrects the rare first move that lands off
        # target when crossing between monitors with different scale factors.
        for _attempt in range(2):
            if not api.set_cursor_pos(x, y):
                return False
            if api.cursor_pos() == (int(x), int(y)):
                return True
        return True

    # ------------------------------------------------------------------ windows
    def _classify(self, hwnd: int) -> tuple[str, int]:
        _tid, pid = self._api.thread_process(hwnd)
        if pid == self._pid:
            return _KIND_OWN, pid
        cls = self._api.class_name(hwnd)
        if cls in _SHELL_CLASSES:
            return _KIND_SHELL, pid
        if cls in _TRANSIENT_CLASSES:
            return _KIND_TRANSIENT, pid
        return _KIND_NORMAL, pid

    def _ref(self, hwnd: int, pid: int) -> WindowRef:
        return WindowRef(handle=int(hwnd), pid=pid or None, rect=self._api.frame_rect(hwnd))

    @_best_effort(None)
    def foreground_window(self) -> WindowRef | None:
        hwnd = self._api.foreground()
        if not hwnd:
            return None
        root = self._api.root(hwnd)
        kind, pid = self._classify(root)
        return self._ref(root, pid) if kind == _KIND_NORMAL else None

    @_best_effort(None)
    def window_at(self, x: int, y: int) -> WindowRef | None:
        api = self._api
        hit = api.window_from_point(x, y)
        if not hit:
            return None
        root = api.root(hit)
        kind, pid = self._classify(root)
        if kind == _KIND_NORMAL:
            return self._ref(root, pid)
        if kind == _KIND_SHELL:
            return None  # desktop or taskbar: no window there
        # One of our own windows or a transient shell surface is on top; the
        # user's window is the next suitable one below it.
        return self._window_below(root, x, y)

    def _window_below(self, start: int, x: int, y: int) -> WindowRef | None:
        api = self._api
        hwnd = api.next_window(start)
        for _step in range(_MAX_Z_WALK):
            if not hwnd:
                return None
            if self._covers_point(hwnd, x, y):
                kind, pid = self._classify(hwnd)
                if kind == _KIND_NORMAL:
                    return self._ref(hwnd, pid)
                if kind == _KIND_SHELL:
                    return None
            hwnd = api.next_window(hwnd)
        return None

    def _covers_point(self, hwnd: int, x: int, y: int) -> bool:
        api = self._api
        if not api.is_visible(hwnd) or api.is_iconic(hwnd) or api.is_cloaked(hwnd):
            return False
        if api.ex_style(hwnd) & (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE):
            return False  # click-through overlays and non-activatable tool windows
        rect = api.frame_rect(hwnd)
        return rect is not None and rect.contains(x, y)

    @_best_effort(False)
    def activate_window(self, ref: WindowRef) -> bool:
        """Give ``ref`` the foreground despite Windows' foreground lock.

        Strategies are tried from least to most intrusive; the first two
        inject no input at all, so the OS idle timer (which drives presence
        and the typing heuristic) is untouched in the common case.
        """
        api = self._api
        hwnd = _hwnd_of(ref)
        if hwnd is None or not api.is_window(hwnd) or api.is_iconic(hwnd):
            return False
        if api.foreground() == hwnd:
            return True
        if api.is_hung(hwnd):
            # An app that stopped pumping messages cannot take focus anyway, and
            # waiting on it would freeze our UI thread. (Windows only reports a
            # window as hung after ~5 s; bring_to_top never waits regardless.)
            log.debug("Not activating window %#x: it is not responding", hwnd)
            return False
        api.allow_set_foreground(os.getpid())  # only this app, never ASFW_ANY (TD-SEC-011)

        if self._activate_attached(hwnd):
            return True
        if api.send_empty_mouse_input() and self._try_foreground(hwnd):
            return True
        if api.tap_alt() and self._try_foreground(hwnd):
            return True
        return api.foreground() == hwnd

    def _try_foreground(self, hwnd: int) -> bool:
        api = self._api
        # Raise only after a granted switch: raising a window that did not get
        # focus would show it on top while keystrokes still go elsewhere.
        if api.set_foreground(hwnd):
            api.bring_to_top(hwnd)
        return api.foreground() == hwnd

    def _activate_attached(self, hwnd: int) -> bool:
        """Share the foreground thread's input state while switching.

        While attached, our thread counts as part of the foreground queue, which
        is what ``SetForegroundWindow`` checks. Skipped for hung windows: sharing
        input state with a thread that does not pump messages can stall ours.
        """
        api = self._api
        current = api.current_thread_id()
        foreground = api.foreground()
        fg_thread = api.thread_process(foreground)[0] if foreground else 0
        if not fg_thread or fg_thread == current:
            return self._try_foreground(hwnd)
        if foreground and api.is_hung(foreground):
            return False
        if not api.attach_thread_input(current, fg_thread, True):
            return False
        try:
            return self._try_foreground(hwnd)
        finally:
            api.attach_thread_input(current, fg_thread, False)

    @_best_effort(False)
    def is_window_valid(self, ref: WindowRef) -> bool:
        api = self._api
        hwnd = _hwnd_of(ref)
        if hwnd is None or not api.is_window(hwnd):
            return False
        if ref.pid is not None and api.thread_process(hwnd)[1] != ref.pid:
            return False  # the handle was recycled for another process's window
        return api.is_visible(hwnd) and not api.is_iconic(hwnd) and not api.is_cloaked(hwnd)

    @_best_effort(None)
    def window_rect(self, ref: WindowRef) -> Rect | None:
        hwnd = _hwnd_of(ref)
        if hwnd is None or not self._api.is_window(hwnd):
            return None
        return self._api.frame_rect(hwnd)

    @_best_effort(None)
    def window_client_rect(self, ref: WindowRef) -> Rect | None:
        hwnd = _hwnd_of(ref)
        if hwnd is None or not self._api.is_window(hwnd):
            return None
        return self._api.client_rect(hwnd)

    @_best_effort(None)
    def window_app(self, ref: WindowRef) -> AppIdentity | None:
        api = self._api
        hwnd = _hwnd_of(ref)
        if hwnd is None or not api.is_window(hwnd):
            return None
        pid = api.thread_process(hwnd)[1]
        if not pid or (ref.pid is not None and pid != ref.pid):
            return None  # gone, or the handle was recycled for another process's window
        name = self._app_process_name(pid)
        if name is None:
            return None
        return AppIdentity(process=name, app_id=api.class_name(hwnd))

    # ------------------------------------------------------------------- camera
    def _own_executables(self) -> frozenset[str]:
        if self._own_paths is None:
            candidates: list[str | None] = [sys.executable, getattr(sys, "_base_executable", None)]
            try:
                # A venv's python.exe is only a launcher; the camera is opened by
                # the interpreter it starts, which is this process's image.
                candidates.append(self._api.process_image_path())
            except Exception:
                log.debug("Could not read the process image path", exc_info=True)
            # Started from a packaged (MSIX) app, such as a terminal inside
            # another app, this process sees redirected paths (``AppData\Roaming``)
            # while the consent store records the real file under
            # ``AppData\Local\Packages\<family>\LocalCache``. Resolving the path
            # through the file system gives that real location.
            candidates += [_real_path(p) for p in candidates if p]
            self._own_paths = frozenset(_norm_path(p) for p in candidates if p)
        return self._own_paths

    def _own_package(self) -> str:
        """Lower-cased package family name of this process; ``""`` without package identity."""
        if self._own_package_name is None:
            name: str | None = None
            try:
                name = self._api.current_package_family_name()
            except Exception:
                log.debug("Could not read the package identity", exc_info=True)
            self._own_package_name = (name or "").lower()
        return self._own_package_name

    def _is_own_camera_user(self, user: _CameraUser) -> bool:
        if user.packaged:
            # With package identity (e.g. the Microsoft Store Python) our own
            # session is recorded under the package family name, not a path.
            own = self._own_package()
            return bool(own) and user.app.lower() == own
        return _norm_path(user.executable) in self._own_executables()

    @_best_effort(None)
    def camera_in_use_by_other_app(self) -> bool | None:
        users = _active_camera_users(self._registry)
        others = [user for user in users if not self._is_own_camera_user(user)]
        if not others:
            return False
        # An app that crashed, or a machine that lost power, mid-session never
        # records the stop time, and the registry keeps that open session across
        # reboots. Sessions opened before this boot are certainly over.
        boot = self._boot_filetime()
        if boot is not None:
            # Generous slack: the boot time is derived from the current clock,
            # which NTP may have moved since. The process check below is exact.
            others = [u for u in others if u.start >= boot - _BOOT_TIME_SLACK]
            if not others:
                return False
        processes = self._processes()
        if not processes:
            return True  # cannot tell live sessions from stale ones: trust the registry
        return any(self._session_is_live(user, processes) for user in others)

    def _session_is_live(self, user: _CameraUser, processes: list[_Process]) -> bool:
        """Whether a process that can own ``user``'s session is still running.

        The process must match the entry (full executable path, or package
        family) and must have been started before the session was: a copy of
        the app launched after a crash never touched the camera. The cheap
        identity checks come first, so the creation time is only read for the
        handful of processes that can be the app at all.
        """
        api = self._api
        if user.packaged:
            family = user.app.lower()
            return any(
                (api.process_package_family_name(p.pid) or "").lower() == family
                and self._started_before(p.pid, user.start)
                for p in processes
            )
        target = _norm_path(user.executable)
        name = ntpath.basename(target)
        for process in processes:
            if process.name != name or not self._started_before(process.pid, user.start):
                continue
            exe = self._process_exe(process.pid)
            # A path we may not read (an elevated process) gets the benefit of the doubt.
            if exe is None or _norm_path(exe) == target:
                return True
        return False

    def _started_before(self, pid: int, session_start: int) -> bool:
        """Whether ``pid`` may have opened a camera session that began at ``session_start``.

        An unreadable creation time gets the benefit of the doubt.
        """
        created = self._process_created(pid)
        return created is None or created <= session_start + _SESSION_START_SLACK

    def _processes(self) -> list[_Process] | None:
        """Running processes (pid and name); ``None`` if they cannot be listed.

        Only the name is requested: it is cheap for every process, whereas
        asking psutil for the creation time as well costs a full system
        snapshot per process of another account (see :class:`_Process`).
        """
        try:
            import psutil
        except Exception:
            return None
        processes: list[_Process] = []
        try:
            for proc in psutil.process_iter(["name"]):
                name = proc.info.get("name")
                if name:
                    processes.append(_Process(pid=int(proc.pid), name=str(name).lower()))
        except Exception:
            log.debug("Could not list processes", exc_info=True)
            return None
        return processes

    @staticmethod
    def _process_created(pid: int) -> int | None:
        """Creation time of ``pid`` as a ``FILETIME`` tick count; ``None`` when unreadable."""
        try:
            import psutil

            created = psutil.Process(pid).create_time()
        except Exception:
            return None
        return _unix_to_filetime(created) if isinstance(created, (int, float)) else None

    @staticmethod
    def _process_exe(pid: int) -> str | None:
        """Full executable path of ``pid``; ``None`` when it is gone or not readable."""
        try:
            import psutil

            exe = psutil.Process(pid).exe()
        except Exception:
            return None
        return str(exe) if exe else None

    @staticmethod
    def _boot_filetime() -> int | None:
        """System boot time as a ``FILETIME`` tick count.

        With Fast Startup this is the last full boot (resuming from hibernation
        keeps the uptime), so it is early rather than late: safe as a filter.
        """
        try:
            import psutil

            return _unix_to_filetime(psutil.boot_time())
        except Exception:
            return None

    # -------------------------------------------------------------- permissions
    def permissions(self) -> dict[str, bool | None]:
        return {"camera": self._camera_permission(), "accessibility": None}

    def _camera_permission(self) -> bool | None:
        """``False`` when a Windows privacy switch blocks desktop apps from the camera."""
        try:
            device = _consent_value(self._registry.values("HKLM", _WEBCAM_KEY))
            user = _consent_value(self._registry.values("HKCU", _WEBCAM_KEY))
            desktop = _consent_value(
                self._registry.values("HKCU", f"{_WEBCAM_KEY}\\{_NON_PACKAGED}")
            )
        except Exception:
            log.debug("Could not read camera consent", exc_info=True)
            return None
        if "deny" in (device, user, desktop):
            return False
        if user == "allow":
            return True
        return None

    def open_permission_settings(self, name: str) -> bool:
        uri = _SETTINGS_URIS.get(name)
        return _open_uri(uri) if uri else False

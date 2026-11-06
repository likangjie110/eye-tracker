"""Camera-node notifications (fanotify/inotify) and the camera watcher thread.

Split out of ``platform/linux.py`` (phase 4 / B2).
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import math
import os
import select
import struct
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple, Protocol

log = logging.getLogger(__name__)


# camera-node notifications (fanotify / inotify via ctypes)
# ---------------------------------------------------------------------------
class _CameraEvent(NamedTuple):
    """An open and/or close of a watched camera node."""

    opened: bool
    #: Whether another process did it; ``None`` when the notifier cannot tell.
    foreign: bool | None
    #: A descriptor opened for writing was closed. Capture opens the node
    #: read-write, device listings (browsers enumerating cameras, ``v4l2-ctl``
    #: queries) mostly read-only - so this marks a capture attempt ending.
    wrote: bool = False


class _CameraNotifier(Protocol):
    """Blocking source of :class:`_CameraEvent` for a set of device nodes."""

    kind: str

    @property
    def alive(self) -> bool:
        """False once the notifier failed for good (the watcher then polls)."""
        ...

    def wait(self, timeout: float) -> list[_CameraEvent]: ...

    def close(self) -> None: ...


# <linux/fanotify.h>, <sys/inotify.h>, <fcntl.h>
_FAN_CLOEXEC = 0x0000_0001
_FAN_NONBLOCK = 0x0000_0002
_FAN_REPORT_FID = 0x0000_0200
_FAN_MARK_ADD = 0x0000_0001
_FAN_CLOSE_WRITE = 0x0000_0008
_FAN_CLOSE_NOWRITE = 0x0000_0010
_FAN_OPEN = 0x0000_0020
_FAN_Q_OVERFLOW = 0x0000_4000
_FANOTIFY_METADATA_VERSION = 3
_IN_CLOSE_WRITE = 0x0000_0008
_IN_CLOSE_NOWRITE = 0x0000_0010
_IN_OPEN = 0x0000_0020
_AT_FDCWD = -100
_POLLIN = 0x0001  # <poll.h>; ``select.POLLIN`` does not exist on Windows, where tests also run
#: struct fanotify_event_metadata: event_len, vers, reserved, metadata_len, mask, fd, pid.
_FAN_EVENT = struct.Struct("=IBBHQii")
#: struct inotify_event without its trailing name: wd, mask, cookie, len.
_IN_EVENT = struct.Struct("=iIII")


def _parse_fanotify(data: bytes, own_pid: int) -> list[_CameraEvent]:
    """Events from a ``read`` of a fanotify descriptor.

    For an unprivileged listener the kernel reports the pid only for events
    caused by the listener's own process (0 otherwise), which is exactly the
    own-vs-foreign distinction needed here.
    """
    events: list[_CameraEvent] = []
    offset = 0
    while offset + _FAN_EVENT.size <= len(data):
        length, version, _reserved, _meta, mask, _fd, pid = _FAN_EVENT.unpack_from(data, offset)
        if length < _FAN_EVENT.size:
            break  # malformed: never loop forever
        offset += length
        # FAN_REPORT_FID groups never receive file descriptors (fd == FAN_NOFD).
        # Quick open/close pairs of one process may arrive merged in one event.
        if version != _FANOTIFY_METADATA_VERSION or mask & _FAN_Q_OVERFLOW:
            events.append(_CameraEvent(opened=False, foreign=None))
        elif mask & (_FAN_OPEN | _FAN_CLOSE_WRITE | _FAN_CLOSE_NOWRITE):
            events.append(
                _CameraEvent(
                    opened=bool(mask & _FAN_OPEN),
                    foreign=pid != own_pid,
                    wrote=bool(mask & _FAN_CLOSE_WRITE),
                )
            )
    return events


def _parse_inotify(data: bytes) -> list[_CameraEvent]:
    """Events from a ``read`` of an inotify descriptor (no process information)."""
    events: list[_CameraEvent] = []
    offset = 0
    while offset + _IN_EVENT.size <= len(data):
        _wd, mask, _cookie, name_len = _IN_EVENT.unpack_from(data, offset)
        offset += _IN_EVENT.size + name_len
        events.append(
            _CameraEvent(
                opened=bool(mask & _IN_OPEN), foreign=None, wrote=bool(mask & _IN_CLOSE_WRITE)
            )
        )
    return events


def _libc() -> Any:
    return ctypes.CDLL(None, use_errno=True)


def _os_error(what: str) -> OSError:
    err = ctypes.get_errno()
    return OSError(err, f"{what}: {os.strerror(err)}")


class _FdNotifier:
    """Reads events from a non-blocking notification descriptor."""

    kind = "notifier"

    def __init__(self, fd: int) -> None:
        self._fd = fd

    @property
    def alive(self) -> bool:
        """False once the descriptor failed and was closed (see :meth:`wait`)."""
        return self._fd >= 0

    def wait(self, timeout: float) -> list[_CameraEvent]:
        """Events that arrive within ``timeout`` seconds (possibly none).

        A failing descriptor is closed and reported once as a forced rescan;
        :attr:`alive` is then ``False`` so the watcher can fall back to polling.
        """
        if self._fd < 0:
            time.sleep(max(0.0, timeout))
            return []
        try:
            if not self._readable(timeout):
                return []
            data = os.read(self._fd, 16384)
        except BlockingIOError:
            return []
        except (OSError, ValueError) as exc:
            log.info("Camera %s stopped working (%s); polling the cameras instead", self.kind, exc)
            self.close()
            return [_CameraEvent(opened=False, foreign=None)]  # force a rescan
        return self._parse(data)

    def _readable(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the descriptor to have data.

        ``poll``, not ``select``: ``select`` cannot watch descriptors numbered
        ``FD_SETSIZE`` (1024) or higher, which a busy process may hand out.
        """
        make_poller = getattr(select, "poll", None)  # absent on Windows (tests)
        if make_poller is None:
            ready, _, _ = select.select([self._fd], [], [], max(0.0, timeout))
            return bool(ready)
        poller = make_poller()
        poller.register(self._fd, _POLLIN)
        return bool(poller.poll(max(0, math.ceil(timeout * 1000))))

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _parse(self, data: bytes) -> list[_CameraEvent]:
        raise NotImplementedError


class _FanotifyNotifier(_FdNotifier):
    """Unprivileged fanotify (Linux >= 5.13): opens with own-vs-foreign attribution."""

    kind = "fanotify"

    def __init__(self, paths: Sequence[str]) -> None:
        libc = _libc()
        init = libc.fanotify_init
        init.argtypes = [ctypes.c_uint, ctypes.c_uint]
        init.restype = ctypes.c_int
        mark = libc.fanotify_mark
        mark.argtypes = [
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_char_p,
        ]
        mark.restype = ctypes.c_int
        # Unprivileged groups must report file handles instead of descriptors.
        fd = int(init(_FAN_CLOEXEC | _FAN_NONBLOCK | _FAN_REPORT_FID, os.O_RDONLY))
        if fd < 0:
            raise _os_error("fanotify_init")
        marked = 0
        for path in paths:
            mask = _FAN_OPEN | _FAN_CLOSE_WRITE | _FAN_CLOSE_NOWRITE
            if int(mark(fd, _FAN_MARK_ADD, mask, _AT_FDCWD, os.fsencode(path))) == 0:
                marked += 1
            else:
                log.debug("fanotify cannot watch %s: %s", path, _os_error("fanotify_mark"))
        if not marked:
            os.close(fd)
            raise OSError("fanotify could not watch any camera node")
        super().__init__(fd)
        self._own_pid = os.getpid()

    def _parse(self, data: bytes) -> list[_CameraEvent]:
        return _parse_fanotify(data, self._own_pid)


class _InotifyNotifier(_FdNotifier):
    """inotify: opens and closes of the nodes, without knowing who did them."""

    kind = "inotify"

    def __init__(self, paths: Sequence[str]) -> None:
        libc = _libc()
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        add = libc.inotify_add_watch
        add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add.restype = ctypes.c_int
        flags = getattr(os, "O_NONBLOCK", 0o4000) | getattr(os, "O_CLOEXEC", 0o2000000)
        fd = int(init(flags))
        if fd < 0:
            raise _os_error("inotify_init1")
        watched = 0
        for path in paths:
            mask = _IN_OPEN | _IN_CLOSE_WRITE | _IN_CLOSE_NOWRITE
            if int(add(fd, os.fsencode(path), mask)) >= 0:
                watched += 1
        if not watched:
            os.close(fd)
            raise OSError("inotify could not watch any camera node")
        super().__init__(fd)

    def _parse(self, data: bytes) -> list[_CameraEvent]:
        return _parse_inotify(data)


def _open_camera_notifier(paths: Sequence[str]) -> _CameraNotifier | None:
    """The best available notifier for these device nodes, or ``None`` (poll)."""
    if not sys.platform.startswith("linux") or not paths:
        return None
    for cls in (_FanotifyNotifier, _InotifyNotifier):
        try:
            notifier = cls(paths)
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            log.debug("%s unavailable for camera nodes: %s", cls.kind, exc)
            continue
        log.debug("Watching %s with %s", ", ".join(paths), cls.kind)
        return notifier
    return None


# ---------------------------------------------------------------------------
# camera watcher
# ---------------------------------------------------------------------------
class _CameraWatcher:
    """Whether another process uses - or just tried to open - a physical camera.

    :meth:`query` never runs the ``/proc`` scan on the caller's thread: a
    daemon thread keeps the answer fresh while somebody keeps asking and ends
    itself ``idle_stop_s`` after the last question. It rescans when notified of
    an open or close of a camera node (plus a slow safety rescan) or, without
    notifications (or once the notifier failed), every ``poll_s``. The first
    question after the thread (re)started briefly waits for its first scan.
    ``threaded=False`` scans inline at most every ``poll_s`` instead (tests).

    **Refused opens.** When a notifier attributes to another process the close
    of a read-write descriptor of a camera node while we hold a camera
    ourselves, that app just tried to start the camera and failed (V4L2 has one
    streamer; capture opens read-write, device listings read-only). The camera
    is then reported busy for ``grace_s`` so the controller releases it and the
    app can retry, and for as long as the app then holds it. Some apps also
    probe devices read-write, so when such a period ends without the app
    taking the camera it counts as a false alarm; from the second consecutive
    false alarm on, further attempts are ignored for a cooldown that doubles
    each time (``cooldown_s`` .. ``max_cooldown_s``), which bounds the cost of
    an app that keeps probing the cameras in the background.
    """

    poll_s = 3.0
    safety_rescan_s = 30.0
    idle_stop_s = 10.0
    debounce_s = 0.1
    grace_s = 30.0
    cooldown_s = 60.0
    max_cooldown_s = 1800.0
    #: Upper bound for one wait, so stop requests and idleness are noticed.
    max_wait_s = 1.0
    #: How long the first query after a (re)start waits for a fresh scan. A
    #: ``/proc`` scan takes milliseconds; this only bounds a pathological one.
    first_scan_wait_s = 0.5

    def __init__(
        self,
        *,
        devices: Callable[[], set[str] | None],
        scan: Callable[[set[str] | None], bool | None],
        own_use: Callable[[set[str] | None], bool],
        clock: Callable[[], float],
        threaded: bool,
        notifier_factory: Callable[[Sequence[str]], _CameraNotifier | None],
    ) -> None:
        self._devices = devices
        self._scan = scan
        self._own_use = own_use
        self._clock = clock
        self._threaded = threaded
        self._notifier_factory = notifier_factory
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        #: Set by every finished scan; replaced by a fresh one when the thread starts.
        self._scanned = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._last_query = -math.inf
        self._next_scan_at = -math.inf
        self._holders: bool | None = None
        self._contention_until: float | None = None
        self._contention_confirmed = False
        self._false_alarms = 0
        self._cooldown_until = -math.inf

    # ------------------------------------------------------------- public API
    def query(self) -> bool | None:
        """Latest answer; ``None`` when no scan could tell yet.

        The first question after the thread ended (nobody asked for
        ``idle_stop_s``, e.g. while tracking was paused or the screen locked)
        waits up to ``first_scan_wait_s`` for the new thread's first scan: the
        answer from before the pause may be hours old, and the controller asks
        exactly then to decide whether the camera may be reopened.
        """
        now = self._clock()
        started = False
        with self._lock:
            self._last_query = now
            due = now >= self._next_scan_at
            if self._threaded:
                started = self._ensure_thread_locked()
            scanned = self._scanned
        if started:
            scanned.wait(self.first_scan_wait_s)
        elif due and not self._threaded:
            self.rescan()
        with self._lock:
            return self._state_locked(self._clock())

    def rescan(self) -> None:
        """Scan ``/proc`` for other holders now (on the calling thread)."""
        self._rescan_with(self._devices())

    def handle_events(self, events: Sequence[_CameraEvent]) -> None:
        """React to opens/closes of camera nodes: rescan soon, spot refused opens."""
        if not events:
            return
        # A refused capture attempt: another process opened the node read-write
        # and closed it again (after EBUSY). An app that keeps it open is found by
        # the rescan instead; read-only opens are device listings.
        if any(event.foreign and event.wrote for event in events):
            try:
                ours = self._own_use(self._devices())
            except Exception:
                log.debug("own camera check failed", exc_info=True)
                ours = False
            if ours:
                with self._lock:
                    self._begin_contention_locked(self._clock())
        with self._lock:
            self._next_scan_at = min(self._next_scan_at, self._clock() + self.debounce_s)

    def stop(self, timeout: float = 2.0) -> None:
        """End the background thread (tests; production threads end when idle)."""
        with self._lock:
            self._stopping = True
            thread = self._thread
        self._wakeup.set()
        if thread is not None:
            thread.join(timeout)

    # -------------------------------------------------------------- internals
    def _ensure_thread_locked(self) -> bool:
        """Start the watcher thread unless it runs; ``True`` when it was started."""
        if self._thread is not None or self._stopping:
            return False
        # Nothing watched the nodes since the previous thread ended: the new one
        # must set up its notifier and rescan at once, not at the old deadline,
        # and the old answer no longer counts (``None`` until that scan is done).
        self._next_scan_at = -math.inf
        self._holders = None
        self._scanned = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(self._scanned,), name="camera-watch", daemon=True
        )
        self._thread.start()
        return True

    def _run(self, scanned: threading.Event) -> None:
        """The watcher thread; ``scanned`` is the event its first scan sets."""
        notifier: _CameraNotifier | None = None
        watched: frozenset[str] | None = None
        try:
            while True:
                now = self._clock()
                with self._lock:
                    if self._stopping or now - self._last_query > self.idle_stop_s:
                        # Cleared under the lock so a concurrent query starts a new thread.
                        if self._thread is threading.current_thread():
                            self._thread = None
                        return
                    due = self._next_scan_at
                if now >= due:
                    devices = self._devices()
                    key = frozenset(devices) if devices else None
                    if key != watched:  # first run, or cameras (un)plugged
                        if notifier is not None:
                            notifier.close()
                        watched = key
                        notifier = self._notifier_factory(sorted(key)) if key else None
                    self._rescan_with(devices)
                    interval = self.safety_rescan_s if notifier is not None else self.poll_s
                    with self._lock:
                        self._next_scan_at = self._clock() + interval
                    continue
                timeout = min(due - now, self.max_wait_s)
                if notifier is None:
                    self._wakeup.wait(timeout)
                    continue
                self.handle_events(notifier.wait(timeout))
                if not notifier.alive:
                    # Poll from now on (poll_s, not the slow safety rescan). The
                    # device set stays "watched", so a notifier that keeps failing
                    # is not re-created in a loop; the next device change or
                    # thread restart tries again.
                    notifier.close()
                    notifier = None
                    with self._lock:
                        self._next_scan_at = min(self._next_scan_at, self._clock() + self.poll_s)
        except Exception:
            log.debug("camera watcher stopped by an error", exc_info=True)
        finally:
            if notifier is not None:
                notifier.close()
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
            # A thread that died before its first scan must not keep a query
            # waiting for it. (Its own event: a successor has a new one.)
            scanned.set()

    def _rescan_with(self, devices: set[str] | None) -> None:
        try:
            holders = self._scan(devices)
        except Exception:
            log.debug("camera scan failed", exc_info=True)
            holders = None
        now = self._clock()
        with self._lock:
            self._holders = holders
            self._next_scan_at = max(self._next_scan_at, now + self.poll_s)
            if holders and self._contention_until is not None:
                self._contention_confirmed = True
            self._scanned.set()

    def _state_locked(self, now: float) -> bool | None:
        self._settle_contention_locked(now)
        if self._contention_until is not None:
            return True
        return self._holders

    def _begin_contention_locked(self, now: float) -> None:
        if now < self._cooldown_until:
            log.debug("Another app opened the camera; ignored (recent false alarms)")
            return
        if self._contention_until is None:
            log.info(
                "Another app tried to open the camera while tracking uses it; "
                "releasing it for %.0f s so that app can start",
                self.grace_s,
            )
            self._contention_confirmed = False
        self._contention_until = now + self.grace_s

    def _settle_contention_locked(self, now: float) -> None:
        until = self._contention_until
        if until is None or now < until:
            return
        self._contention_until = None
        if self._contention_confirmed or self._holders:
            self._false_alarms = 0
            return
        self._false_alarms += 1
        if self._false_alarms >= 2:
            cooldown = min(self.cooldown_s * 2 ** (self._false_alarms - 2), self.max_cooldown_s)
            self._cooldown_until = now + cooldown
            log.info(
                "No app took the camera after it was released; ignoring camera probes for %.0f s",
                cooldown,
            )
        else:
            log.info("No app took the camera after it was released; tracking resumes")


# ---------------------------------------------------------------------------
# platform
# ---------------------------------------------------------------------------

"""QtDBus in-process property calls and reply unwrapping.

Split out of ``platform/linux.py`` (phase 4 / B2).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple

from .linux_common import (
    _DBUS_TIMEOUT_MS,
    _RETRY_AFTER_FAILURE_S,
)

log = logging.getLogger(__name__)


# D-Bus (QtDBus, in-process)
# ---------------------------------------------------------------------------
class _DBusReply(NamedTuple):
    """Result of a D-Bus method call."""

    values: tuple[Any, ...]
    #: D-Bus error name; empty on success.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _dbus_unwrap(value: Any) -> Any:
    """Plain Python values from QtDBus wrappers (variants, object paths)."""
    variant = getattr(value, "variant", None)
    if callable(variant):
        return _dbus_unwrap(variant())
    path = getattr(value, "path", None)
    if callable(path) and type(value).__name__ == "QDBusObjectPath":
        return str(path())
    return value


class _QtDBus:
    """Blocking D-Bus method calls through QtDBus, without launching processes.

    Polled queries (idle time, lock state) would otherwise start ``gdbus`` or
    ``loginctl`` several times a second. :meth:`call` returns ``None`` when
    QtDBus, a Qt application or the bus is unavailable; callers then fall back
    to the command-line tools. Any thread may call it.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._modules: tuple[Any, Any, Any, Any] | None = None
        self._import_failed = False
        self._bus_retry_at: dict[bool, float] = {}

    def call(
        self,
        *,
        system: bool,
        service: str,
        path: str,
        interface: str,
        method: str,
        args: Sequence[Any] = (),
        timeout_ms: int = _DBUS_TIMEOUT_MS,
    ) -> _DBusReply | None:
        modules = self._load()
        if modules is None:
            return None
        app_class, qdbus, connection_class, message_class = modules
        try:
            # A connection made before the QCoreApplication exists "may misbehave"
            # (Qt's words); the command-line fallback covers that short window.
            if app_class.instance() is None:
                return None
            now = self._clock()
            with self._lock:
                if now < self._bus_retry_at.get(system, -math.inf):
                    return None
            bus = connection_class.systemBus() if system else connection_class.sessionBus()
            if not bus.isConnected():
                with self._lock:
                    self._bus_retry_at[system] = now + _RETRY_AFTER_FAILURE_S
                log.debug("D-Bus %s bus unavailable", "system" if system else "session")
                return None
            message = message_class.createMethodCall(service, path, interface, method)
            if args:
                message.setArguments(list(args))
            reply = bus.call(message, qdbus.CallMode.Block, int(timeout_ms))
            if reply.type() == message_class.MessageType.ReplyMessage:
                return _DBusReply(tuple(_dbus_unwrap(v) for v in reply.arguments()))
            return _DBusReply((), str(reply.errorName() or "org.freedesktop.DBus.Error.Failed"))
        except Exception as exc:
            log.debug("D-Bus call %s.%s failed: %s", interface, method, exc)
            return None

    def _load(self) -> tuple[Any, Any, Any, Any] | None:
        with self._lock:
            if self._modules is None and not self._import_failed:
                try:
                    from PySide6.QtCore import QCoreApplication
                    from PySide6.QtDBus import QDBus, QDBusConnection, QDBusMessage
                except Exception as exc:
                    log.debug("QtDBus unavailable: %s", exc)
                    self._import_failed = True
                else:
                    self._modules = (QCoreApplication, QDBus, QDBusConnection, QDBusMessage)
            return self._modules


# ---------------------------------------------------------------------------

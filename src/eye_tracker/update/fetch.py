"""The rules for what the update check may fetch, on top of a bare transport.

The transport (:mod:`.winhttp`) only moves bytes. Everything that decides *what*
may be fetched lives here, in plain Python that is tested without a network:

* HTTPS only, port 443, no credentials in the address, and only GitHub's own
  hosts (:data:`ALLOWED_HOSTS`): the API, the release pages and the hosts that
  serve release files;
* redirects are followed here, one at a time, and every hop is checked against
  the same rules (the transport never follows one by itself);
* a size limit per request, checked against the announced length and while the
  body arrives;
* nothing is sent but the address and an ``Accept`` header: no cookies, no
  identifier, no system information.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

#: The hosts a request, or a redirect of one, may go to.
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
MAX_REDIRECTS = 5
CHUNK_BYTES = 64 * 1024
#: A whole download must finish within this time, and must average at least
#: :data:`MIN_RATE_BYTES_S` over :data:`RATE_WINDOW_S`; otherwise it is stalled.
MAX_DOWNLOAD_S = 30 * 60.0
MIN_RATE_BYTES_S = 8 * 1024
RATE_WINDOW_S = 30.0
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_HTTPS = re.compile(r"^https://([A-Za-z0-9.-]+)(/[^\s#]*)?$")


class NetworkError(OSError):
    """A request that failed or was refused (the message is meant for the user)."""


class Cancelled(Exception):
    """The user cancelled a download."""


class Response(Protocol):
    """An open HTTP response (see :class:`~eye_tracker.update.winhttp.WinHttpTransport`)."""

    status: int
    location: str | None
    content_length: int | None

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


class Transport(Protocol):
    def open(self, host: str, path: str, accept: str | None) -> Response:
        """Send one GET to ``https://host/path`` without following redirects."""
        ...

    def close(self) -> None: ...


def supported() -> bool:
    """Whether this platform has a transport (Windows, for now)."""
    return sys.platform == "win32"


def default_transport() -> Transport:
    if not supported():
        raise NetworkError("Update checks are not available on this platform yet.")
    from .winhttp import WinHttpTransport

    return WinHttpTransport()


def split_url(url: str) -> tuple[str, str]:
    """``(host, path)`` of an allowed address; :class:`NetworkError` for any other."""
    match = _HTTPS.match(url) if isinstance(url, str) else None
    if match is None:
        raise NetworkError("Refused an address that is not a plain https address.")
    host = match[1].lower()
    if host not in ALLOWED_HOSTS:
        raise NetworkError(f"Refused an address on {host}: not a GitHub host.")
    return host, match[2] or "/"


def resolve_location(base_host: str, location: str) -> str:
    """The absolute address a ``Location`` header names (a path stays on the host)."""
    if location.startswith("/") and not location.startswith("//"):
        return f"https://{base_host}{location}"
    return location


class Fetcher:
    """Fetches allowed addresses through a transport, with the limits above."""

    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport
        self._owned = transport is None

    def close(self) -> None:
        if self._transport is not None and self._owned:
            self._transport.close()
            self._transport = None

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ public
    def get(self, url: str, *, max_bytes: int, accept: str | None = None) -> bytes:
        """The body of ``url`` (at most ``max_bytes``)."""
        response = self._open(url, accept, max_bytes)
        try:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise NetworkError("The answer is larger than expected.")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            response.close()

    def download(
        self,
        url: str,
        dest: Path,
        *,
        max_bytes: int,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> int:
        """Write ``url`` to ``dest`` and return the size. ``progress(done, total)`` is
        called as data arrives (``total`` is 0 when the length is unknown);
        ``cancelled()`` is polled between chunks (raises :class:`Cancelled`).

        A download that exceeds :data:`MAX_DOWNLOAD_S`, or stays below
        :data:`MIN_RATE_BYTES_S` over a :data:`RATE_WINDOW_S` window, fails as
        stalled instead of hanging in ``DOWNLOADING`` forever.
        """

        def attempt() -> int:
            response = self._open(url, None, max_bytes)
            total = response.content_length or 0
            done = 0
            started = window_start = clock()
            window_done = 0
            try:
                with dest.open("wb") as out:
                    while True:
                        if cancelled is not None and cancelled():
                            raise Cancelled
                        chunk = response.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        done += len(chunk)
                        now = clock()
                        if now - started > MAX_DOWNLOAD_S:
                            raise NetworkError("The download took too long.")
                        if now - window_start >= RATE_WINDOW_S:
                            rate = (done - window_done) / (now - window_start)
                            if rate < MIN_RATE_BYTES_S:
                                raise NetworkError("The download is too slow.")
                            window_start, window_done = now, done
                        if done > max_bytes:
                            raise NetworkError("The download is larger than expected.")
                        out.write(chunk)
                        if progress is not None:
                            progress(done, total)
            finally:
                response.close()
            return done

        return attempt()

    # --------------------------------------------------------------- internals
    def _open(self, url: str, accept: str | None, max_bytes: int) -> Response:
        if self._transport is None:
            self._transport = default_transport()
        for _hop in range(MAX_REDIRECTS + 1):
            host, path = split_url(url)
            response = self._transport.open(host, path, accept)
            if response.status == 200:
                length = response.content_length
                if length is not None and length > max_bytes:
                    response.close()
                    raise NetworkError("The file is larger than expected.")
                return response
            status, location = response.status, response.location
            response.close()
            if status in _REDIRECTS and location:
                url = resolve_location(host, location)
                continue
            raise NetworkError(f"GitHub answered with HTTP {status}.")
        raise NetworkError("Too many redirects.")

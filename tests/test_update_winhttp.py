"""Behaviour tests for the only networking code (``update/winhttp.py``).

The module is WinHTTP through ctypes, so the tests replace ``winhttp._winhttp()`` with a fake API:
the call sequence, the options (redirects off, secure flag, timeouts), the header parsing, the
chunked read and every failure path are pinned here without Windows or a network.

This closes the phase-1 finding that the one network transport had no behaviour tests at all.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import pytest

from eye_tracker.update import winhttp
from eye_tracker.update.fetch import NetworkError

SESSION = 101
CONNECTION = 202
REQUEST = 303
FLAG_NUMBER = 0x20000000


class FakeWinHTTP:
    """A fake WinHTTP library that records calls and can fail at any step."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.closed: list[int] = []
        self.fail_open = False
        self.fail_timeouts = False
        self.fail_option = False
        self.fail_connect = False
        self.fail_open_request = False
        self.fail_send = False
        self.fail_receive = False
        self.fail_query = False
        self.fail_read = False
        self.status = 200
        self.content_length = 42
        self.location: str | None = None
        self.chunks: list[bytes] = [b"hello", b" world"]

    def _call(self, name: str, *args: object) -> None:
        self.calls.append((name, args))

    def WinHttpOpen(self, *_args: object) -> int:
        self._call("Open", *_args)
        return 0 if self.fail_open else SESSION

    def WinHttpSetTimeouts(self, *_args: object) -> bool:
        self._call("SetTimeouts", *_args)
        return not self.fail_timeouts

    def WinHttpConnect(self, *_args: object) -> int:
        self._call("Connect", *_args)
        return 0 if self.fail_connect else CONNECTION

    def WinHttpOpenRequest(self, *_args: object) -> int:
        self._call("OpenRequest", *_args)
        return 0 if self.fail_open_request else REQUEST

    def WinHttpSetOption(self, request: int, option: int, value: object, size: int) -> bool:
        self._call("SetOption", request, option, value, size)
        return not self.fail_option

    def WinHttpSendRequest(self, *_args: object) -> bool:
        self._call("SendRequest", *_args)
        return not self.fail_send

    def WinHttpReceiveResponse(self, *_args: object) -> bool:
        self._call("ReceiveResponse", *_args)
        return not self.fail_receive

    def WinHttpQueryHeaders(
        self,
        request: int,
        level: int,
        name: object,
        buffer: object,
        size: object,
        index: object,
    ) -> bool:
        self._call("QueryHeaders", request, level)
        if self.fail_query:
            return False
        if level & FLAG_NUMBER:
            value = (
                self.status
                if (level & 0xFFFF) == winhttp._QUERY_STATUS_CODE
                else (self.content_length)
            )
            ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value = int(value)
            return True
        if self.location is None:
            return False
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_wchar * 2048)).contents.value = self.location
        return True

    def WinHttpReadData(self, request: int, buffer: object, size: int, read: object) -> bool:
        self._call("ReadData", request, size)
        if self.fail_read:
            return False
        data = self.chunks.pop(0) if self.chunks else b""
        ctypes.memmove(buffer, data, len(data))
        ctypes.cast(read, ctypes.POINTER(wintypes.DWORD)).contents.value = len(data)
        return True

    def WinHttpCloseHandle(self, handle: int) -> bool:
        self.closed.append(handle)
        return True

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def args(self, name: str) -> list[object]:
        return [args for call, args in self.calls if call == name]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeWinHTTP:
    fake = FakeWinHTTP()
    monkeypatch.setattr(winhttp, "_winhttp", lambda: fake)
    return fake


def test_transport_opens_a_session_with_system_proxy_and_timeouts(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    assert api.names()[:2] == ["Open", "SetTimeouts"]
    open_args = api.args("Open")[0]
    assert open_args[0] == winhttp.USER_AGENT
    assert open_args[1] == winhttp._ACCESS_TYPE_AUTOMATIC_PROXY
    assert api.args("SetTimeouts")[0][1:] == winhttp._TIMEOUTS == (10_000, 10_000, 15_000, 30_000)
    transport.close()
    assert api.closed == [SESSION]


def test_open_sends_one_secure_get_for_the_path(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    response = transport.open("api.github.com", "/repos/x/releases/latest", "application/json")
    names = api.names()
    assert names.index("Connect") < names.index("OpenRequest") < names.index("SendRequest")
    connect = api.args("Connect")[0]
    assert connect[1:] == ("api.github.com", winhttp._PORT, 0)
    request = api.args("OpenRequest")[0]
    assert request[1] == "GET"
    assert request[2] == "/repos/x/releases/latest"
    assert request[6] == winhttp._FLAG_SECURE
    sent = api.args("SendRequest")[0]
    assert sent[1] == "Accept: application/json\r\n"
    assert sent[2] == 0xFFFFFFFF  # WinHTTP computes the length of the header block
    assert response.status == 200
    assert response.content_length == 42


def test_redirects_are_disabled_with_the_never_policy(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    transport.open("api.github.com", "/", None)
    option = api.args("SetOption")[0]
    assert option[1] == winhttp._OPTION_REDIRECT_POLICY
    policy = ctypes.cast(option[2], ctypes.POINTER(wintypes.DWORD)).contents.value
    assert policy == winhttp._REDIRECT_POLICY_NEVER
    assert option[3] == ctypes.sizeof(wintypes.DWORD)


def test_open_without_accept_sends_no_headers(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    transport.open("api.github.com", "/", None)
    sent = api.args("SendRequest")[0]
    assert sent[1] is None
    assert sent[2] == 0


def test_connect_failure_raises(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    api.fail_connect = True
    with pytest.raises(NetworkError, match=r"Could not reach api\.github\.com"):
        transport.open("api.github.com", "/", None)


def test_open_request_failure_closes_the_connection(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    api.fail_open_request = True
    with pytest.raises(NetworkError):
        transport.open("api.github.com", "/", None)
    assert api.closed == [CONNECTION]


def test_send_failure_closes_both_handles(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    api.fail_send = True
    with pytest.raises(NetworkError, match=r"Could not reach api\.github\.com"):
        transport.open("api.github.com", "/", None)
    assert api.closed == [REQUEST, CONNECTION]


def test_receive_failure_closes_both_handles(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    api.fail_receive = True
    with pytest.raises(NetworkError):
        transport.open("api.github.com", "/", None)
    assert api.closed == [REQUEST, CONNECTION]


def test_missing_headers_report_defaults(api: FakeWinHTTP) -> None:
    api.fail_query = True
    transport = winhttp.WinHttpTransport()
    response = transport.open("api.github.com", "/", None)
    assert response.status == 0
    assert response.content_length is None
    assert response.location is None


def test_location_is_read_when_present(api: FakeWinHTTP) -> None:
    api.location = "https://github.com/next"
    transport = winhttp.WinHttpTransport()
    response = transport.open("api.github.com", "/", None)
    assert response.location == "https://github.com/next"


def test_read_returns_successive_chunks(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    response = transport.open("api.github.com", "/", None)
    assert response.read(16) == b"hello"
    assert response.read(16) == b" world"
    assert response.read(16) == b""


def test_read_failure_raises(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    response = transport.open("api.github.com", "/", None)
    api.fail_read = True
    with pytest.raises(NetworkError, match="The connection was interrupted"):
        response.read(4)


def test_response_close_is_idempotent(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    response = transport.open("api.github.com", "/", None)
    response.close()
    response.close()
    assert api.closed == [REQUEST, CONNECTION]


def test_transport_close_is_idempotent(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    transport.close()
    transport.close()
    assert api.closed == [SESSION]


def test_timeout_failure_closes_the_session(api: FakeWinHTTP) -> None:
    api.fail_timeouts = True
    with pytest.raises(NetworkError, match="Could not set connection timeouts"):
        winhttp.WinHttpTransport()
    assert api.closed == [SESSION]


def test_redirect_policy_failure_closes_both_handles(api: FakeWinHTTP) -> None:
    transport = winhttp.WinHttpTransport()
    api.fail_option = True
    with pytest.raises(NetworkError, match="Could not disable redirects"):
        transport.open("api.github.com", "/", None)
    assert api.closed == [REQUEST, CONNECTION]


def test_session_failure_raises(api: FakeWinHTTP) -> None:
    api.fail_open = True
    with pytest.raises(NetworkError, match="Could not start a connection"):
        winhttp.WinHttpTransport()


def test_user_agent_names_the_app_and_version() -> None:
    from eye_tracker import REPO_URL, __version__

    assert f"EyeTracker/{__version__} (+{REPO_URL})" == winhttp.USER_AGENT

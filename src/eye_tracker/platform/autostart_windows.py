"""Windows start-at-login: the HKCU Run value and the Task Manager startup approval.

Split out of ``platform/autostart.py`` (phase 4 / B2), verbatim except for imports.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

from .autostart_common import (
    RUN_KEY,
    RUN_VALUE_NAME,
    STARTUP_APPROVED_KEY,
    AutostartError,
    _Entry,
)

log = logging.getLogger(__name__)


# -------------------------------------------------------------------- Windows
def _win_command_line(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(command))


def _win_split(command_line: str) -> list[str]:
    """Split a command line the way ``CommandLineToArgvW`` and the C runtime do.

    The program name ends at the next space, or at the closing quote when it is
    quoted; backslashes mean nothing there. For the other arguments, ``2n``
    backslashes before a quote become ``n`` and the quote delimits, ``2n+1``
    become ``n`` and a literal quote, and ``""`` inside quotes is a literal
    quote. This is the inverse of ``subprocess.list2cmdline``.
    """
    text = command_line.lstrip(" \t")
    if not text:
        return []
    if text.startswith('"'):
        end = text.find('"', 1)
        end = len(text) if end < 0 else end
        args = [text[1:end]]
        index = end + 1
    else:
        end = len(text)
        for position, char in enumerate(text):
            if char in " \t":
                end = position
                break
        args = [text[:end]]
        index = end
    current: list[str] = []
    in_quotes = started = False
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            run = index
            while run < length and text[run] == "\\":
                run += 1
            count = run - index
            if run < length and text[run] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    index = run + 1
                else:
                    index = run  # the quote is a delimiter, handled next
            else:
                current.append("\\" * count)
                index = run
            started = True
            continue
        if char == '"':
            if in_quotes and index + 1 < length and text[index + 1] == '"':
                current.append('"')
                index += 2
                continue
            in_quotes = not in_quotes
            started = True
        elif char in " \t" and not in_quotes:
            if started:
                args.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
        index += 1
    if started:
        args.append("".join(current))
    return args


def _win_entry() -> _Entry | None:
    value = _reg_read(RUN_KEY, RUN_VALUE_NAME)
    if not isinstance(value, str) or not value.strip():
        return None
    return _Entry(_win_split(value), active=not _win_disabled_in_task_manager())


def _win_disabled_in_task_manager() -> bool:
    """Task Manager keeps the Run value but flags it; the first byte is odd when disabled."""
    data = _reg_read(STARTUP_APPROVED_KEY, RUN_VALUE_NAME)
    return isinstance(data, (bytes, bytearray)) and len(data) > 0 and data[0] & 1 == 1


def _win_enable(command: Sequence[str]) -> None:
    _reg_write_str(RUN_KEY, RUN_VALUE_NAME, _win_command_line(command))
    # Clearing Task Manager's "disabled" flag makes the new entry effective;
    # otherwise the toggle in our UI would silently do nothing.
    _reg_delete(STARTUP_APPROVED_KEY, RUN_VALUE_NAME)


def _win_disable() -> None:
    _reg_delete(RUN_KEY, RUN_VALUE_NAME)
    _reg_delete(STARTUP_APPROVED_KEY, RUN_VALUE_NAME)


def _reg_read(key_path: str, name: str) -> Any | None:
    """Value ``name`` under ``HKCU\\key_path``, ``None`` when missing."""
    if sys.platform != "win32":
        raise AutostartError("the Windows registry is not available on this OS")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    return value


def _reg_write_str(key_path: str, name: str, value: str) -> None:
    """Write a ``REG_SZ`` value under ``HKCU\\key_path`` (creating the key)."""
    if sys.platform != "win32":
        raise AutostartError("the Windows registry is not available on this OS")
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _reg_delete(key_path: str, name: str) -> None:
    """Delete value ``name`` under ``HKCU\\key_path``; missing is fine."""
    if sys.platform != "win32":
        raise AutostartError("the Windows registry is not available on this OS")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass

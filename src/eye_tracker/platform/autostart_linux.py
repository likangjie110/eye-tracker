"""Linux start-at-login: the XDG autostart desktop entry.

Split out of ``platform/autostart.py`` (phase 4 / B2), verbatim except for imports.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path

from .. import APP_NAME, APP_SLUG
from .autostart_common import (
    _DESKTOP_QUOTED_ESCAPES,
    _DESKTOP_RESERVED,
    _DESKTOP_STRING_ESCAPES,
    DESKTOP_FILE_NAME,
    _Entry,
    _write_text,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------- Linux
def _linux_desktop_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "")
    # The XDG spec says relative values are invalid and must be ignored.
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".config"
    return root / "autostart" / DESKTOP_FILE_NAME


def _desktop_exec_arg(arg: str) -> str:
    """Quote one argument following the Desktop Entry ``Exec`` rules.

    Only double quotes are recognised (``shlex.quote``'s single quotes are
    not), and ``%`` must be doubled because it introduces field codes.
    """
    if arg and not any(ch in _DESKTOP_RESERVED for ch in arg):
        quoted = arg
    else:
        escaped = "".join(f"\\{ch}" if ch in _DESKTOP_QUOTED_ESCAPES else ch for ch in arg)
        quoted = f'"{escaped}"'
    return quoted.replace("%", "%%")


def _desktop_string_escape(value: str) -> str:
    """Escaping of the key file's ``string`` type, applied on top of ``Exec`` quoting."""
    return (
        value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    )


def _desktop_exec(command: Sequence[str]) -> str:
    return _desktop_string_escape(" ".join(_desktop_exec_arg(arg) for arg in command))


def _desktop_exec_split(value: str) -> list[str]:
    """Split an ``Exec`` value into arguments (the inverse of :func:`_desktop_exec`).

    Undoes the key-file string escapes, then the ``Exec`` quoting (only double
    quotes; ``\\"``, ``\\```, ``\\$`` and ``\\\\`` inside them), then ``%%``.
    """
    unescaped: list[str] = []
    chars = iter(value)
    for char in chars:
        if char == "\\":
            following = next(chars, "")
            unescaped.append(_DESKTOP_STRING_ESCAPES.get(following, "\\" + following))
        else:
            unescaped.append(char)
    text = "".join(unescaped)
    args: list[str] = []
    current: list[str] = []
    in_quotes = started = False
    index = 0
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_quotes:
            if char == "\\" and following and following in _DESKTOP_QUOTED_ESCAPES:
                current.append(following)
                index += 1
            elif char == '"':
                in_quotes = False
            else:
                current.append(char)
        elif char == '"':
            in_quotes = started = True
        elif char in " \t":
            if started:
                args.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
        index += 1
    if started:
        args.append("".join(current))
    return [arg.replace("%%", "%") for arg in args]


def _desktop_entry(command: Sequence[str]) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.5",
        f"Name={APP_NAME}",
        "Comment=Moves the mouse cursor to the monitor you are looking at",
        f"Exec={_desktop_exec(command)}",
        f"Icon={APP_SLUG}",
        "Terminal=false",
        "Hidden=false",
        "NoDisplay=false",
        "X-GNOME-Autostart-enabled=true",
        # Give the panel a moment so the tray icon has somewhere to go.
        "X-GNOME-Autostart-Delay=3",
    ]
    return "\n".join(lines) + "\n"


def _parse_desktop_entry(text: str) -> dict[str, str]:
    """Keys of the ``[Desktop Entry]`` group (values unescaped only as far as needed here)."""
    entry: dict[str, str] = {}
    group = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            continue
        if group == "Desktop Entry" and "=" in line:
            key, _, value = line.partition("=")
            entry.setdefault(key.strip(), value.strip())
    return entry


def _linux_enable(command: Sequence[str]) -> None:
    _write_text(_linux_desktop_path(), _desktop_entry(command))


def _linux_entry() -> _Entry | None:
    path = _linux_desktop_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    entry = _parse_desktop_entry(text)
    # Desktop environments "turn off" an autostart entry by editing these keys
    # (GNOME Tweaks, KDE System Settings) rather than deleting the file.
    active = (
        entry.get("Hidden", "false").lower() != "true"
        and entry.get("X-GNOME-Autostart-enabled", "true").lower() != "false"
    )
    exec_value = entry.get("Exec", "")
    return _Entry(_desktop_exec_split(exec_value) if exec_value else [], active=active)

"""The hotkey editor widget and its key/mouse translation (phase 4 / B3).

Extracted from ``ui/settings_dialog.py`` so the shortcut recorder can be exercised on its own.
The dialog imports and re-exports ``HotkeyEdit`` and the preview helpers.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QFocusEvent, QHideEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import QLineEdit, QWidget

from ..platform.hotkeys import Hotkey, format_hotkey, parse_hotkey
from .util import DANGER

log = logging.getLogger(__name__)


# =================================================================== hotkey editor
_MODIFIER_KEYS = frozenset(
    k.value
    for k in (
        Qt.Key.Key_Control,
        Qt.Key.Key_Shift,
        Qt.Key.Key_Alt,
        Qt.Key.Key_AltGr,
        Qt.Key.Key_Meta,
        Qt.Key.Key_Super_L,
        Qt.Key.Key_Super_R,
        Qt.Key.Key_Hyper_L,
        Qt.Key.Key_Hyper_R,
    )
)


def _build_key_table() -> dict[int, str]:
    K = Qt.Key
    table: dict[int, str] = {
        K.Key_Space.value: "space",
        K.Key_Return.value: "enter",
        K.Key_Enter.value: "enter",
        K.Key_Tab.value: "tab",
        K.Key_Backtab.value: "tab",
        K.Key_Escape.value: "escape",
        K.Key_Backspace.value: "backspace",
        K.Key_Insert.value: "insert",
        K.Key_Delete.value: "delete",
        K.Key_Home.value: "home",
        K.Key_End.value: "end",
        K.Key_PageUp.value: "pageup",
        K.Key_PageDown.value: "pagedown",
        K.Key_Left.value: "left",
        K.Key_Right.value: "right",
        K.Key_Up.value: "up",
        K.Key_Down.value: "down",
        K.Key_Minus.value: "minus",
        K.Key_Equal.value: "equal",
        K.Key_BracketLeft.value: "bracketleft",
        K.Key_BracketRight.value: "bracketright",
        K.Key_Backslash.value: "backslash",
        K.Key_Semicolon.value: "semicolon",
        K.Key_Apostrophe.value: "quote",
        K.Key_QuoteLeft.value: "grave",
        K.Key_Comma.value: "comma",
        K.Key_Period.value: "period",
        K.Key_Slash.value: "slash",
    }
    for i in range(26):
        table[K.Key_A.value + i] = chr(ord("a") + i)
    for i in range(10):
        table[K.Key_0.value + i] = str(i)
    for i in range(24):
        table[K.Key_F1.value + i] = f"f{i + 1}"
    return table


_KEY_NAMES = _build_key_table()

# With Shift held Qt reports the shifted symbol ("!" for Shift+1). Global hotkeys are
# bound to physical keys, so map back to the unshifted key (US layout; on Windows
# letters and digits come from the layout-independent virtual key instead).
_SHIFTED_US: dict[int, str] = {
    Qt.Key.Key_Exclam.value: "1",
    Qt.Key.Key_At.value: "2",
    Qt.Key.Key_NumberSign.value: "3",
    Qt.Key.Key_Dollar.value: "4",
    Qt.Key.Key_Percent.value: "5",
    Qt.Key.Key_AsciiCircum.value: "6",
    Qt.Key.Key_Ampersand.value: "7",
    Qt.Key.Key_Asterisk.value: "8",
    Qt.Key.Key_ParenLeft.value: "9",
    Qt.Key.Key_ParenRight.value: "0",
    Qt.Key.Key_Underscore.value: "minus",
    Qt.Key.Key_Plus.value: "equal",
    Qt.Key.Key_BraceLeft.value: "bracketleft",
    Qt.Key.Key_BraceRight.value: "bracketright",
    Qt.Key.Key_Bar.value: "backslash",
    Qt.Key.Key_Colon.value: "semicolon",
    Qt.Key.Key_QuoteDbl.value: "quote",
    Qt.Key.Key_AsciiTilde.value: "grave",
    Qt.Key.Key_Less.value: "comma",
    Qt.Key.Key_Greater.value: "period",
    Qt.Key.Key_Question.value: "slash",
}


def _modifier_names(mods: Qt.KeyboardModifier, macos: bool) -> list[str]:
    """Canonical modifier names in ctrl, alt, shift, meta order.

    On macOS Qt reports the Command key as ``ControlModifier`` and the Control key
    as ``MetaModifier``; our names follow the physical keys (Command = meta).
    """
    KM = Qt.KeyboardModifier
    ctrl_flag, meta_flag = (
        (KM.MetaModifier, KM.ControlModifier)
        if macos
        else (
            KM.ControlModifier,
            KM.MetaModifier,
        )
    )
    names: list[str] = []
    if mods & ctrl_flag:
        names.append("ctrl")
    if mods & KM.AltModifier:
        names.append("alt")
    if mods & KM.ShiftModifier:
        names.append("shift")
    if mods & meta_flag:
        names.append("meta")
    return names


def modifier_preview(names: Sequence[str], macos: bool | None = None) -> str:
    """Held modifiers in the notation of :func:`format_hotkey`, e.g. ``"Ctrl+Win+…"``.

    Shown while a shortcut is being recorded. Built with ``format_hotkey`` so
    the preview and the finished shortcut name the keys alike (Win on Windows,
    Super on Linux, ⌃⌥⇧⌘ on macOS). Empty when nothing is held.
    """
    if not names:
        return ""
    if macos is None:
        macos = sys.platform == "darwin"
    # format_hotkey needs a key: F1 is valid with any modifier; drop its label.
    text = format_hotkey(Hotkey(frozenset(names), "f1"), macos=macos)
    prefix = text.removesuffix("F1")
    if prefix == text:  # unexpected label: fall back to the plain names
        prefix = "+".join(name.capitalize() for name in names) + "+"
    return prefix + "…"


def hotkey_text_from_event(event: QKeyEvent, macos: bool | None = None) -> str | None:
    """Settings-style hotkey text (``"ctrl+alt+p"``) for a key press.

    Returns ``None`` for a bare modifier press. The result is *not* validated: an
    unsupported key or a missing modifier yields text that :func:`parse_hotkey`
    rejects with a helpful message.
    """
    if macos is None:
        macos = sys.platform == "darwin"
    key = int(event.key())
    if key in _MODIFIER_KEYS or key == 0:
        return None
    mods = event.modifiers()
    names = _modifier_names(mods, macos)
    name: str | None = None
    if mods & Qt.KeyboardModifier.KeypadModifier and key not in (
        Qt.Key.Key_Return.value,
        Qt.Key.Key_Enter.value,
    ):
        # Keypad keys have their own virtual keys; "ctrl+alt+1" would never fire from
        # the keypad, so do not pretend it would.
        name = "keypad" + (QKeySequence(key).toString().lower() or str(key))
    elif sys.platform == "win32":
        vk = int(event.nativeVirtualKey())
        if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
            name = chr(vk).lower()
    if name is None:
        name = _KEY_NAMES.get(key)
    if name is None and "shift" in names:
        name = _SHIFTED_US.get(key)
    if name is None:
        name = QKeySequence(key).toString(QKeySequence.SequenceFormat.PortableText).lower()
        name = name or f"key{key}"
    return "+".join([*names, name])


class HotkeyEdit(QLineEdit):
    """Records a global shortcut: click it, then press the key combination.

    Esc (without modifiers) cancels recording and Backspace/Delete clears the
    shortcut. :meth:`hotkey` returns the canonical settings text (``"ctrl+alt+p"``)
    when the combination is valid, otherwise the raw attempt, so the owner can
    show why it was rejected.
    """

    hotkey_changed = Signal(str)
    #: True while the field waits for a key combination.
    recording_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setPlaceholderText("Not set")
        self.setToolTip("Click, then press the shortcut. Backspace clears it, Esc cancels.")
        self._value = ""
        self._recording = False
        self._invalid = False

    # --------------------------------------------------------------- value
    def hotkey(self) -> str:
        """Canonical hotkey text, ``""`` when unset, or the raw text if invalid."""
        return self._value

    def set_hotkey(self, text: str) -> None:
        """Set the shortcut (canonicalised when valid). Emits ``hotkey_changed``."""
        text = (text or "").strip()
        try:
            value = str(parse_hotkey(text)) if text else ""
        except ValueError:
            value = text
        changed = value != self._value
        self._value = value
        self._refresh()
        if changed:
            self.hotkey_changed.emit(value)

    def set_invalid(self, invalid: bool) -> None:
        """Mark the field as rejected (red frame)."""
        if invalid != self._invalid:
            self._invalid = invalid
            self.setStyleSheet(f"border: 1px solid {DANGER};" if invalid else "")

    @property
    def recording(self) -> bool:
        return self._recording

    def display_text(self) -> str:
        """What the field shows for the current value (platform notation)."""
        if not self._value:
            return ""
        try:
            return format_hotkey(parse_hotkey(self._value))
        except ValueError:
            return self._value

    # --------------------------------------------------------------- events
    def event(self, event: QEvent) -> bool:
        # While recording, claim every key combination before the dialog's own
        # shortcuts (button mnemonics, Esc/Enter defaults) can act on it.
        if event.type() == QEvent.Type.ShortcutOverride and self._recording:
            event.accept()
            return True
        return super().event(event)

    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        self._set_recording(True)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        self._set_recording(False)

    def hideEvent(self, event: QHideEvent) -> None:
        # A dialog closed while recording never delivers focusOut to the field.
        super().hideEvent(event)
        self._set_recording(False)

    def _set_recording(self, recording: bool) -> None:
        if recording == self._recording:
            return
        self._recording = recording
        self._refresh()
        self.recording_changed.emit(recording)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = int(event.key())
        bare = not _modifier_names(event.modifiers(), sys.platform == "darwin")
        if bare and key == Qt.Key.Key_Escape.value:
            self._refresh()
            self.clearFocus()
            return
        if bare and key in (Qt.Key.Key_Backspace.value, Qt.Key.Key_Delete.value):
            self.set_hotkey("")
            return
        text = hotkey_text_from_event(event)
        if text is None:
            # Only modifiers so far: show them as a live preview.
            held = _modifier_names(event.modifiers(), sys.platform == "darwin")
            self.setText(modifier_preview(held))
            return
        self.set_hotkey(text)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if int(event.key()) in _MODIFIER_KEYS:
            held = _modifier_names(event.modifiers(), sys.platform == "darwin")
            if not held:
                self._refresh()
            return
        super().keyReleaseEvent(event)

    def _refresh(self) -> None:
        self.setPlaceholderText("Press a shortcut…" if self._recording else "Not set")
        self.setText(self.display_text())


# ======================================================================= bindings
@dataclass
class _Binding:
    """Connects one settings key to a widget.

    ``snapshot`` captures the raw widget state; while it equals the state right
    after :meth:`load`, the originally loaded value is returned unchanged.
    """

    key: str
    widget: QWidget
    read: Callable[[], Any]
    write: Callable[[Any], None]
    snapshot: Callable[[], Any]
    loaded: Any = None
    loaded_snapshot: Any = None

    def load(self, value: Any) -> None:
        self.write(value)
        self.loaded = value
        self.loaded_snapshot = self.snapshot()

    @property
    def modified(self) -> bool:
        return bool(self.snapshot() != self.loaded_snapshot)

    def value(self) -> Any:
        return self.read() if self.modified else self.loaded

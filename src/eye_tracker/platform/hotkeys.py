"""System-wide keyboard shortcuts.

The module has two halves:

* **Parsing and formatting** (:func:`parse_hotkey`, :func:`format_hotkey`) are pure
  Python and work everywhere. Hotkeys are stored in the settings file as short,
  case-insensitive strings such as ``"ctrl+alt+p"``.
* **Registration** is done by a :class:`HotkeyManager`. :func:`create_hotkey_manager`
  picks the native implementation for the running system:

  ===========  ==========================================================================
  Windows      ``RegisterHotKey`` on a dedicated message-loop thread
  macOS        Carbon ``RegisterEventHotKey`` (no permission prompt needed); the
               events are delivered by the Cocoa run loop that Qt already spins
  Linux/X11    passive key grabs through python-xlib on a dedicated thread
  Wayland      unsupported: bind the ``ctl`` commands in the desktop's own keyboard
               settings instead (the manager's note names them as this copy is run)
  ===========  ==========================================================================

A global hotkey swallows its key system-wide, so combinations that the user types
text with are refused (:meth:`HotkeyManager.layout_conflict`): on Windows AltGr
arrives as Ctrl+Alt (Ctrl+Alt+C is 'ć' on a Polish keyboard), on macOS Option
alone types characters, and on X11 a keyboard-layout switch such as Alt+Shift
makes every hotkey containing both keys impossible to press. Keys follow the
keyboard layout on every system, and :meth:`HotkeyManager.last_error` explains
any refusal in a user-presentable way.

Callbacks may be invoked on *any* thread (the hotkey thread on Windows/X11, the
main thread on macOS). They must return quickly and marshal work to the Qt main
thread themselves, for example by emitting a queued signal.

Only the registered combinations are ever observed. Nothing here installs a
keyboard hook or sees any other key press.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import sys
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, ClassVar, Protocol

log = logging.getLogger(__name__)

# The shared model, parsers and manager bases live in hotkeys_base; re-export the public
# names so importers of eye_tracker.platform.hotkeys keep working.
from .hotkeys_base import (  # noqa: E402  (defined before: keeps the module self-contained)
    _DIGITS,
    _LETTERS,
    _PUNCTUATION,
    KEYS,
    MODIFIERS,
    Hotkey,
    HotkeyCallback,
    HotkeyManager,
    _as_hotkey,
    _Binding,
    _describe_typed,
    _key_label,
    _NativeHotkeyManager,
    _ThreadedHotkeyManager,
    format_hotkey,
    parse_hotkey,
)

# ------------------------------------------------------------------------- Windows
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000
_WM_QUIT = 0x0012
_WM_HOTKEY = 0x0312
_WM_USER = 0x0400
_WM_APP_CALL = 0x8000 + 0x2E7  # WM_APP + private offset
_PM_NOREMOVE = 0x0000
_ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_WIN_MODIFIERS = {"ctrl": _MOD_CONTROL, "alt": _MOD_ALT, "shift": _MOD_SHIFT, "meta": _MOD_WIN}

_WIN_VK: dict[str, int] = {
    **{ch: ord(ch.upper()) for ch in _LETTERS},
    **{d: ord(d) for d in _DIGITS},
    **{f"f{n}": 0x6F + n for n in range(1, 25)},  # VK_F1 = 0x70 … VK_F24 = 0x87
    "space": 0x20,
    "enter": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    # OEM keys, US layout; other layouts are resolved with VkKeyScanW first.
    "semicolon": 0xBA,
    "equal": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
    "slash": 0xBF,
    "grave": 0xC0,
    "bracketleft": 0xDB,
    "backslash": 0xDC,
    "bracketright": 0xDD,
    "quote": 0xDE,
}


def _win_modifiers(modifiers: Iterable[str]) -> int:
    mask = 0
    for m in modifiers:
        mask |= _WIN_MODIFIERS[m]
    return mask


def _win_virtual_key(
    key: str,
    vk_scan: Callable[[str], int] | None = None,
    vk_to_scan: Callable[[int], int] | None = None,
) -> int | None:
    """Virtual-key code for ``key``, or ``None`` if no key of the layout produces it.

    Punctuation lives on different keys in different layouts (on a Turkish Q
    keyboard "." is where "/" is on a US one), so the active layout is asked first
    with ``vk_scan`` (``VkKeyScanW``). A character that the layout only types with
    Shift or AltGr (German '/', Turkish '[') has no key of its own: the user asked
    for the unshifted key, and the US virtual key would be a different key (Turkish
    'ğ' for '[') or no key at all, so ``None`` is returned. The US table is only a
    fallback for characters the layout lacks entirely (Cyrillic, Greek), where the
    US position is the sensible guess, and even then only if a physical key produces
    that virtual key (``vk_to_scan``, i.e. ``MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)``,
    is non-zero).
    """
    char = _PUNCTUATION.get(key)
    if char is None:
        return _WIN_VK.get(key)
    if vk_scan is not None:
        try:
            result = int(vk_scan(char))
        except Exception:
            result = -1
        if result != -1:
            if (result >> 8) & 0xFF:
                log.debug("%r needs Shift or AltGr on the current keyboard layout", char)
                return None
            return result & 0xFF
    vk = _WIN_VK[key]
    if vk_to_scan is not None:
        try:
            if not vk_to_scan(vk):
                return None
        except Exception:
            log.debug("MapVirtualKey(%#x) failed", vk, exc_info=True)
    return vk


class _Win32Api:
    """Private ctypes prototypes (own WinDLL instances so other modules' argtypes are
    never clobbered)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.MSG = wintypes.MSG
        self.byref = ctypes.byref
        self.get_last_error = ctypes.get_last_error  # type: ignore[attr-defined]
        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

        self.RegisterHotKey = user32.RegisterHotKey
        self.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.RegisterHotKey.restype = wintypes.BOOL
        self.UnregisterHotKey = user32.UnregisterHotKey
        self.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.UnregisterHotKey.restype = wintypes.BOOL
        self.GetMessageW = user32.GetMessageW
        self.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.GetMessageW.restype = wintypes.BOOL
        self.PeekMessageW = user32.PeekMessageW
        self.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.PeekMessageW.restype = wintypes.BOOL
        self.PostThreadMessageW = user32.PostThreadMessageW
        self.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self.PostThreadMessageW.restype = wintypes.BOOL
        self.VkKeyScanW = user32.VkKeyScanW
        self.VkKeyScanW.argtypes = [wintypes.WCHAR]
        self.VkKeyScanW.restype = ctypes.c_short
        self.MapVirtualKeyW = user32.MapVirtualKeyW
        self.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self.MapVirtualKeyW.restype = wintypes.UINT
        # Read-only keyboard-layout queries for the AltGr check. None of them loads or
        # activates a layout; ToUnicodeEx is always called with flag 0x4 so that it
        # leaves the keyboard state (including a pending dead key) untouched.
        self.GetKeyboardLayoutList = user32.GetKeyboardLayoutList
        self.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(wintypes.HKL)]
        self.GetKeyboardLayoutList.restype = ctypes.c_int
        self.MapVirtualKeyExW = user32.MapVirtualKeyExW
        self.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.HKL]
        self.MapVirtualKeyExW.restype = wintypes.UINT
        self.ToUnicodeEx = user32.ToUnicodeEx
        self.ToUnicodeEx.argtypes = [
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_ubyte),
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.HKL,
        ]
        self.ToUnicodeEx.restype = ctypes.c_int
        self.GetLocaleInfoW = kernel32.GetLocaleInfoW
        self.GetLocaleInfoW.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self.GetLocaleInfoW.restype = ctypes.c_int
        self.GetCurrentThreadId = kernel32.GetCurrentThreadId
        self.GetCurrentThreadId.argtypes = []
        self.GetCurrentThreadId.restype = wintypes.DWORD
        self.HKL = wintypes.HKL
        self.c_ubyte = ctypes.c_ubyte
        self.create_unicode_buffer = ctypes.create_unicode_buffer


@functools.cache
def _win32() -> _Win32Api:
    return _Win32Api()


_MAPVK_VK_TO_VSC = 0
_TOUNICODE_KEEP_STATE = 0x4  # Windows 10 1607+: do not change the keyboard state
_LOCALE_SENGLISHDISPLAYNAME = 0x72
_VK_SHIFT, _VK_CONTROL, _VK_MENU = 0x10, 0x11, 0x12
_VK_LSHIFT, _VK_LCONTROL, _VK_RMENU = 0xA0, 0xA2, 0xA5
_WIN_KEYBOARD_LAYOUTS_KEY = r"SYSTEM\CurrentControlSet\Control\Keyboard Layouts"


class _WinLayoutProbe(Protocol):
    """Read-only questions about the installed Windows keyboard layouts."""

    def layouts(self) -> Sequence[int]:
        """Handles (HKLs) of every keyboard layout the user has installed."""
        ...

    def altgr_text(self, vk: int, shift: bool, hkl: int) -> tuple[str, bool] | None:
        """What Ctrl+Alt(+Shift)+``vk`` types on layout ``hkl``: ``(text, is_dead_key)``."""
        ...

    def layout_name(self, hkl: int) -> str:
        """Display name of layout ``hkl`` such as ``"Polish (Programmers)"``."""
        ...


def _win_altgr_conflict(hotkey: Hotkey, vk: int, probe: _WinLayoutProbe) -> str | None:
    """Why a Ctrl+Alt hotkey would eat a character typed with AltGr, or ``None``.

    Windows reports AltGr as LCtrl+RAlt, and ``RegisterHotKey`` cannot tell left
    from right modifiers, so a Ctrl+Alt(+Shift) hotkey fires on AltGr(+Shift) as
    well and the character never reaches the application (Ctrl+Alt+T is AltGr+T,
    '₺', on Turkish Q). Hotkeys are matched by virtual key whatever layout is active
    later, so every installed layout is checked. With the Win key held AltGr cannot
    be meant: no Windows layout uses the Win key as a character modifier.
    """
    mods = hotkey.modifiers
    if not {"ctrl", "alt"} <= mods or "meta" in mods:
        return None
    shift = "shift" in mods
    for hkl in probe.layouts():
        typed = probe.altgr_text(vk, shift, hkl)
        if typed is None:
            continue
        text, dead = typed
        what = f"the dead key {_describe_typed(text)}" if dead else _describe_typed(text)
        altgr = "+".join(["AltGr", *(["Shift"] if shift else []), _key_label(hotkey.key)])
        # "is AltGr+, and types" rather than "is AltGr+,, which types": a punctuation
        # key label must not run into the sentence's own punctuation.
        return (
            f"{format_hotkey(hotkey, macos=False)} is {altgr} and types {what} on the "
            f"{probe.layout_name(hkl)} keyboard layout"
        )
    return None


def _win_layout_text(hkl: int) -> str | None:
    """The registry's "Layout Text" of keyboard layout ``hkl`` (e.g. ``"Turkish Q"``).

    The high word of an HKL is either the low word of the layout id (KLID
    ``0000xxxx``), an IME KLID (``Exxxxxxx``) or ``Fnnn`` where ``nnn`` is the
    layout's "Layout Id" value (variants such as Polish (214) or US-Dvorak).
    Only reads the registry.
    """
    if sys.platform != "win32":  # also tells mypy that winreg exists below
        return None
    import winreg

    hkl &= 0xFFFFFFFF
    device = hkl >> 16
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WIN_KEYBOARD_LAYOUTS_KEY) as layouts:
            if device & 0xF000 == 0xF000:
                wanted = device & 0x0FFF
                index = 0
                while True:
                    try:
                        klid = winreg.EnumKey(layouts, index)
                    except OSError:
                        return None
                    index += 1
                    with (
                        contextlib.suppress(OSError, ValueError),
                        winreg.OpenKey(layouts, klid) as key,
                    ):
                        if int(str(winreg.QueryValueEx(key, "Layout Id")[0]), 16) == wanted:
                            return str(winreg.QueryValueEx(key, "Layout Text")[0])
            klid = f"{hkl:08X}" if device & 0xF000 == 0xE000 else f"{device:08X}"
            with winreg.OpenKey(layouts, klid) as key:
                return str(winreg.QueryValueEx(key, "Layout Text")[0]) or None
    except OSError:
        return None


class _Win32LayoutProbe:
    """:class:`_WinLayoutProbe` backed by user32 (safe to use from any thread)."""

    def __init__(self, api: _Win32Api) -> None:
        self._api = api

    def layouts(self) -> list[int]:
        api = self._api
        count = api.GetKeyboardLayoutList(0, None)
        if count <= 0:
            return []
        handles = (api.HKL * count)()
        count = api.GetKeyboardLayoutList(count, handles)
        return [int(h or 0) for h in handles[: max(count, 0)]]

    def altgr_text(self, vk: int, shift: bool, hkl: int) -> tuple[str, bool] | None:
        api = self._api
        scan = api.MapVirtualKeyExW(vk, _MAPVK_VK_TO_VSC, hkl)
        if not scan:
            return None  # no key produces this virtual key on that layout
        state = (api.c_ubyte * 256)()
        for vk_mod in (_VK_CONTROL, _VK_LCONTROL, _VK_MENU, _VK_RMENU):
            state[vk_mod] = 0x80
        if shift:
            state[_VK_SHIFT] = state[_VK_LSHIFT] = 0x80
        buf = api.create_unicode_buffer(8)
        count = api.ToUnicodeEx(vk, scan, state, buf, len(buf), _TOUNICODE_KEEP_STATE, hkl)
        if count == 0:
            return None
        dead = count < 0
        raw = buf[:1] if dead else buf[: min(count, len(buf))]
        # Control characters are not "typed text" (Ctrl+Alt+Enter may report "\r").
        text = "".join(ch for ch in raw if unicodedata.category(ch) != "Cc")
        return (text, dead) if text else None

    def layout_name(self, hkl: int) -> str:
        name = _win_layout_text(hkl)
        if name:
            return name
        buf = self._api.create_unicode_buffer(128)
        if self._api.GetLocaleInfoW(hkl & 0xFFFF, _LOCALE_SENGLISHDISPLAYNAME, buf, len(buf)):
            return buf.value
        return f"0x{hkl & 0xFFFFFFFF:08X}"


class WindowsHotkeyManager(_ThreadedHotkeyManager):
    """``RegisterHotKey`` with ``MOD_NOREPEAT`` on a dedicated message-loop thread.

    Hotkeys registered with a ``NULL`` window belong to the registering thread, so
    registrations are posted to that thread (``PostThreadMessageW`` with a private
    ``WM_APP`` message) and executed there. ``GetMessageW`` blocks, so the thread
    costs nothing while idle.

    Ctrl+Alt(+Shift) combinations that type a character with AltGr on any
    installed keyboard layout are refused (see :func:`_win_altgr_conflict`);
    ``layout_probe`` replaces the user32-backed layout queries in tests.
    """

    name: ClassVar[str] = "win32"

    def __init__(self, *, layout_probe: _WinLayoutProbe | None = None) -> None:
        super().__init__()
        self._thread_id = 0
        self._os_ids: set[int] = set()  # touched only on the hotkey thread
        self._layout_probe = layout_probe

    def _thread_setup(self) -> bool:
        api = _win32()
        self._thread_id = int(api.GetCurrentThreadId())
        # PostThreadMessageW fails until the thread owns a message queue; any
        # Peek/GetMessage call creates it, so do that before announcing readiness.
        msg = api.MSG()
        api.PeekMessageW(api.byref(msg), None, _WM_USER, _WM_USER, _PM_NOREMOVE)
        return True

    def _thread_loop(self) -> None:
        api = _win32()
        msg = api.MSG()
        while not self._stopping.is_set():
            result = api.GetMessageW(api.byref(msg), None, 0, 0)
            if result == 0:  # WM_QUIT
                break
            if result == -1:
                log.error("GetMessageW failed (error %s)", api.get_last_error())
                break
            if msg.message == _WM_HOTKEY:
                self._dispatch(int(msg.wParam))
            elif msg.message == _WM_APP_CALL:
                self._run_pending_calls()

    def _thread_teardown(self) -> None:
        api = _win32()
        for hotkey_id in list(self._os_ids):
            api.UnregisterHotKey(None, hotkey_id)
        self._os_ids.clear()
        # Thread ids are recycled; never post to this one again.
        self._thread_id = 0

    def _post(self, message: int) -> bool:
        if not self._thread_id:
            return False
        return bool(_win32().PostThreadMessageW(self._thread_id, message, 0, 0))

    def _wake(self) -> bool:
        return self._post(_WM_APP_CALL)

    def _wake_for_stop(self) -> None:
        self._post(_WM_QUIT)

    def _native_register(self, binding: _Binding) -> bool:
        return bool(self._call_on_thread(lambda: self._register_on_thread(binding), False))

    def _native_unregister(self, binding: _Binding) -> None:
        self._call_on_thread(lambda: self._unregister_on_thread(binding.id), None)

    def layout_conflict(self, hotkey: Hotkey | str) -> str | None:
        hotkey = _as_hotkey(hotkey)
        try:
            vk = self._virtual_key(hotkey.key)
            return None if vk is None else _win_altgr_conflict(hotkey, vk, self._probe())
        except Exception:  # advisory only: never break a settings dialog
            log.debug("Keyboard layout check for %s failed", hotkey, exc_info=True)
            return None

    def _probe(self) -> _WinLayoutProbe:
        if self._layout_probe is None:
            self._layout_probe = _Win32LayoutProbe(_win32())
        return self._layout_probe

    @staticmethod
    def _virtual_key(key: str) -> int | None:
        if key not in _PUNCTUATION:
            return _win_virtual_key(key)  # layout independent: no need to load user32
        api = _win32()
        return _win_virtual_key(
            key, api.VkKeyScanW, lambda vk: int(api.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC))
        )

    def _register_on_thread(self, binding: _Binding) -> bool:
        hotkey = binding.hotkey
        label = format_hotkey(hotkey, macos=False)
        vk = self._virtual_key(hotkey.key)
        if vk is None:
            char = _PUNCTUATION.get(hotkey.key, hotkey.key)
            return self._reject(
                binding,
                f"{label}: no key types {_describe_typed(char)} without Shift or AltGr on "
                "the current keyboard layout",
            )
        try:
            conflict = _win_altgr_conflict(hotkey, vk, self._probe())
        except Exception:  # the check must never make hotkeys unusable
            log.warning("Could not check %s against the keyboard layouts", label, exc_info=True)
            conflict = None
        if conflict is not None:
            return self._reject(binding, conflict)
        api = _win32()
        mods = _win_modifiers(hotkey.modifiers) | _MOD_NOREPEAT
        if not api.RegisterHotKey(None, binding.id, mods, vk):
            error = api.get_last_error()
            if error == _ERROR_HOTKEY_ALREADY_REGISTERED:
                return self._reject(binding, f"{label} is already in use by another application")
            return self._reject(binding, f"RegisterHotKey({label}) failed (error {error})")
        self._os_ids.add(binding.id)
        return True

    def _unregister_on_thread(self, hotkey_id: int) -> None:
        if hotkey_id in self._os_ids:
            _win32().UnregisterHotKey(None, hotkey_id)
            self._os_ids.discard(hotkey_id)


# --------------------------------------------------------------------------- macOS
_MAC_CMD_KEY = 0x0100
_MAC_SHIFT_KEY = 0x0200
_MAC_OPTION_KEY = 0x0800
_MAC_CONTROL_KEY = 0x1000
_MAC_MODIFIERS = {
    "ctrl": _MAC_CONTROL_KEY,
    "alt": _MAC_OPTION_KEY,
    "shift": _MAC_SHIFT_KEY,
    "meta": _MAC_CMD_KEY,
}
_NO_ERR = 0
_EVENT_NOT_HANDLED_ERR = -9874
_EVENT_HOTKEY_EXISTS_ERR = -9878
_EVENT_HOTKEY_PRESSED = 5
# kEventHotKeyExclusive: without it Carbon lets several applications register the
# same combination and one press fires all of them, so a combination another app
# owns could never be reported. With it, registering fails with
# eventHotKeyExistsErr while any other registration of the combination exists
# (reported as "already in use by another application"), and while we hold it,
# registrations that other apps make later stay silent until we release it. The
# defaults (⌃⌥⌘T/P/C) are therefore chosen to be nobody else's default.
_EVENT_HOTKEY_EXCLUSIVE = 1 << 0
_UC_KEY_ACTION_DOWN = 0
_UC_KEY_TRANSLATE_NO_DEAD_KEYS = 1 << 0  # kUCKeyTranslateNoDeadKeysMask
_CF_NOTIFICATION_DELIVER_IMMEDIATELY = 4
# Key codes of the character keys whose meaning follows the keyboard layout: the
# main block (0x00-0x32, which includes the ISO section key 0x0A) plus the JIS Yen
# and underscore keys. Keypad keys are left out on purpose: they are separate keys.
_MAC_CHARACTER_KEYCODES: tuple[int, ...] = (*range(0x00, 0x33), 0x5D, 0x5E)


def _fourcc(code: str) -> int:
    """Four-character Carbon ``OSType`` as an integer (``'keyb'`` → 0x6B657962)."""
    raw = code.encode("mac_roman")
    if len(raw) != 4:
        raise ValueError(f"four-char code must have 4 bytes: {code!r}")
    return int.from_bytes(raw, "big")


_EVENT_CLASS_KEYBOARD = _fourcc("keyb")
_EVENT_PARAM_DIRECT_OBJECT = _fourcc("----")
_TYPE_EVENT_HOTKEY_ID = _fourcc("hkid")
_HOTKEY_SIGNATURE = _fourcc("EyTk")

# kVK_* virtual key codes from HIToolbox/Events.h. They identify physical key
# positions on an ANSI (US) keyboard; macOS has no F21–F24.
_MAC_KEYCODES: dict[str, int] = {
    "a": 0x00,
    "s": 0x01,
    "d": 0x02,
    "f": 0x03,
    "h": 0x04,
    "g": 0x05,
    "z": 0x06,
    "x": 0x07,
    "c": 0x08,
    "v": 0x09,
    "b": 0x0B,
    "q": 0x0C,
    "w": 0x0D,
    "e": 0x0E,
    "r": 0x0F,
    "y": 0x10,
    "t": 0x11,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "6": 0x16,
    "5": 0x17,
    "equal": 0x18,
    "9": 0x19,
    "7": 0x1A,
    "minus": 0x1B,
    "8": 0x1C,
    "0": 0x1D,
    "bracketright": 0x1E,
    "o": 0x1F,
    "u": 0x20,
    "bracketleft": 0x21,
    "i": 0x22,
    "p": 0x23,
    "enter": 0x24,
    "l": 0x25,
    "j": 0x26,
    "quote": 0x27,
    "k": 0x28,
    "semicolon": 0x29,
    "backslash": 0x2A,
    "comma": 0x2B,
    "slash": 0x2C,
    "n": 0x2D,
    "m": 0x2E,
    "period": 0x2F,
    "tab": 0x30,
    "space": 0x31,
    "grave": 0x32,
    "backspace": 0x33,  # kVK_Delete
    "escape": 0x35,
    "f17": 0x40,
    "f18": 0x4F,
    "f19": 0x50,
    "f20": 0x5A,
    "f5": 0x60,
    "f6": 0x61,
    "f7": 0x62,
    "f3": 0x63,
    "f8": 0x64,
    "f9": 0x65,
    "f11": 0x67,
    "f13": 0x69,
    "f16": 0x6A,
    "f14": 0x6B,
    "f10": 0x6D,
    "f12": 0x6F,
    "f15": 0x71,
    "insert": 0x72,  # kVK_Help
    "home": 0x73,
    "pageup": 0x74,
    "delete": 0x75,  # kVK_ForwardDelete
    "f4": 0x76,
    "end": 0x77,
    "f2": 0x78,
    "pagedown": 0x79,
    "f1": 0x7A,
    "left": 0x7B,
    "right": 0x7C,
    "down": 0x7D,
    "up": 0x7E,
}


def _mac_modifiers(modifiers: Iterable[str]) -> int:
    mask = 0
    for m in modifiers:
        mask |= _MAC_MODIFIERS[m]
    return mask


# Character → key code of letters, digits and punctuation at their US (ANSI) position.
_MAC_ANSI_CHARS: dict[str, int] = {
    _PUNCTUATION.get(key, key): code
    for key, code in _MAC_KEYCODES.items()
    if key in _PUNCTUATION or len(key) == 1
}


def _mac_char_keymap(chars: Mapping[int, str]) -> dict[str, int]:
    """Invert ``{keycode: typed text}`` into ``{character: keycode}``.

    Only single, non-blank characters are kept (lower-cased). If several keys type
    the same character, the key at that character's US position wins, otherwise
    the lowest key code.
    """
    keymap: dict[str, int] = {}
    for code in sorted(chars):
        text = chars[code].lower()
        if len(text) != 1 or text.isspace():
            continue
        if text not in keymap or _MAC_ANSI_CHARS.get(text) == code:
            keymap[text] = code
    return keymap


def _mac_keycode(key: str, keymap: Mapping[str, int] | None) -> int | None:
    """Key code for canonical ``key`` on the layout described by ``keymap``.

    ``keymap`` (from :func:`_mac_char_keymap`) makes letters, digits and punctuation
    follow the layout, so ⌃⌥A is the key labelled A on AZERTY and Dvorak too.
    Letters and digits that the layout does not type without modifiers (Cyrillic
    letters, the AZERTY number row) keep their US position, which is where their
    label is; punctuation that needs Shift/Option on this layout has no key of its
    own and yields ``None``. Without a ``keymap`` the US positions are used.
    """
    char = _PUNCTUATION.get(key, key if len(key) == 1 else None)
    if char is not None and keymap is not None:
        code = keymap.get(char)
        if code is not None:
            return code
        if key in _PUNCTUATION:
            return None
    return _MAC_KEYCODES.get(key)


def _cf_global(lib: Any, name: str) -> Any:
    """The exported ``CFStringRef`` constant ``name`` of framework ``lib``."""
    import ctypes

    return ctypes.c_void_p.in_dll(lib, name).value


class _Carbon:
    """ctypes binding of the handful of Carbon (HIToolbox) hot-key functions, plus the
    Text Input Sources / ``UCKeyTranslate`` calls that read the keyboard layout.

    Every method returns plain Python values so :class:`MacHotkeyManager` can be
    exercised with a fake on any OS. The layout functions are loaded on first use;
    if they are missing, hotkeys still work at the US key positions.
    """

    PATH = "/System/Library/Frameworks/Carbon.framework/Carbon"
    CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    CORE_SERVICES = "/System/Library/Frameworks/CoreServices.framework/CoreServices"

    def __init__(self) -> None:
        import ctypes

        self._ctypes = ctypes
        lib = ctypes.CDLL(self.PATH)
        u32 = ctypes.c_uint32
        vp = ctypes.c_void_p

        class EventTypeSpec(ctypes.Structure):
            _fields_: ClassVar = [("eventClass", u32), ("eventKind", u32)]

        class EventHotKeyID(ctypes.Structure):
            _fields_: ClassVar = [("signature", u32), ("id", u32)]

        self._EventTypeSpec = EventTypeSpec
        self._EventHotKeyID = EventHotKeyID
        # OSStatus (*EventHandlerProcPtr)(EventHandlerCallRef, EventRef, void *userData)
        self._HandlerProc = ctypes.CFUNCTYPE(ctypes.c_int32, vp, vp, vp)
        # void (*CFNotificationCallback)(center, observer, name, object, userInfo)
        self._NotificationProc = ctypes.CFUNCTYPE(None, vp, vp, vp, vp, vp)

        lib.GetApplicationEventTarget.argtypes = []
        lib.GetApplicationEventTarget.restype = vp
        lib.InstallEventHandler.argtypes = [
            vp,
            self._HandlerProc,
            ctypes.c_size_t,  # ItemCount
            ctypes.POINTER(EventTypeSpec),
            vp,
            ctypes.POINTER(vp),
        ]
        lib.InstallEventHandler.restype = ctypes.c_int32
        lib.RemoveEventHandler.argtypes = [vp]
        lib.RemoveEventHandler.restype = ctypes.c_int32
        lib.RegisterEventHotKey.argtypes = [u32, u32, EventHotKeyID, vp, u32, ctypes.POINTER(vp)]
        lib.RegisterEventHotKey.restype = ctypes.c_int32
        lib.UnregisterEventHotKey.argtypes = [vp]
        lib.UnregisterEventHotKey.restype = ctypes.c_int32
        lib.GetEventParameter.argtypes = [
            vp,
            u32,
            u32,
            ctypes.POINTER(u32),
            ctypes.c_size_t,  # ByteCount
            ctypes.POINTER(ctypes.c_size_t),
            vp,
        ]
        lib.GetEventParameter.restype = ctypes.c_int32
        self._lib = lib
        # The C side only holds raw function pointers: without these references the
        # trampolines would be garbage-collected and the next call would crash.
        self._proc: Any = None
        self._layout_proc: Any = None
        self._layout_observer: tuple[Any, Any] | None = None
        self._layout_api: bool | None = None  # None: not loaded yet
        self._cf: Any = None
        self._cs: Any = None
        self._layout_data_key: Any = None
        self._layout_changed_name: Any = None

    def install_handler(self, handler: Callable[[Any], int]) -> tuple[int, Any]:
        ctypes = self._ctypes

        def trampoline(_call_ref: Any, event: Any, _user_data: Any) -> int:
            try:
                return int(handler(event))
            except Exception:  # an exception must never unwind into Carbon
                log.exception("Hotkey event handler failed")
                return _EVENT_NOT_HANDLED_ERR

        proc = self._HandlerProc(trampoline)
        spec = self._EventTypeSpec(_EVENT_CLASS_KEYBOARD, _EVENT_HOTKEY_PRESSED)
        ref = ctypes.c_void_p()
        status = self._lib.InstallEventHandler(
            self._lib.GetApplicationEventTarget(),
            proc,
            1,
            ctypes.byref(spec),
            None,
            ctypes.byref(ref),
        )
        if status == _NO_ERR:
            self._proc = proc
        return int(status), ref

    def remove_handler(self, ref: Any) -> int:
        status = int(self._lib.RemoveEventHandler(ref))
        self._proc = None
        return status

    def register_hotkey(
        self, keycode: int, modifiers: int, signature: int, hotkey_id: int
    ) -> tuple[int, Any]:
        ctypes = self._ctypes
        ref = ctypes.c_void_p()
        status = self._lib.RegisterEventHotKey(
            keycode,
            modifiers,
            self._EventHotKeyID(signature, hotkey_id),
            self._lib.GetApplicationEventTarget(),
            _EVENT_HOTKEY_EXCLUSIVE,
            ctypes.byref(ref),
        )
        return int(status), ref

    def unregister_hotkey(self, ref: Any) -> int:
        return int(self._lib.UnregisterEventHotKey(ref))

    def hotkey_id(self, event: Any) -> tuple[int, int, int]:
        """``(status, signature, id)`` of the hot key that produced ``event``."""
        ctypes = self._ctypes
        hk = self._EventHotKeyID()
        status = self._lib.GetEventParameter(
            event,
            _EVENT_PARAM_DIRECT_OBJECT,
            _TYPE_EVENT_HOTKEY_ID,
            None,
            ctypes.sizeof(hk),
            None,
            ctypes.byref(hk),
        )
        return int(status), int(hk.signature), int(hk.id)

    # ------------------------------------------------------------ keyboard layout
    def _load_layout_api(self) -> bool:
        if self._layout_api is None:
            try:
                self._bind_layout_api()
                self._layout_api = True
            except (OSError, AttributeError, TypeError, ValueError) as exc:
                log.info("Keyboard layout API unavailable (%s); hotkeys use US key positions", exc)
                self._layout_api = False
        return self._layout_api

    def _bind_layout_api(self) -> None:
        ctypes = self._ctypes
        vp = ctypes.c_void_p
        lib = self._lib
        cf = ctypes.CDLL(self.CORE_FOUNDATION)
        cs = ctypes.CDLL(self.CORE_SERVICES)
        for copy in (
            lib.TISCopyCurrentKeyboardLayoutInputSource,
            lib.TISCopyCurrentASCIICapableKeyboardLayoutInputSource,
        ):
            copy.argtypes = []
            copy.restype = vp
        lib.TISGetInputSourceProperty.argtypes = [vp, vp]
        lib.TISGetInputSourceProperty.restype = vp
        lib.LMGetKbdType.argtypes = []
        lib.LMGetKbdType.restype = ctypes.c_uint8
        cf.CFDataGetBytePtr.argtypes = [vp]
        cf.CFDataGetBytePtr.restype = vp
        cf.CFRelease.argtypes = [vp]
        cf.CFRelease.restype = None
        cf.CFNotificationCenterGetDistributedCenter.argtypes = []
        cf.CFNotificationCenterGetDistributedCenter.restype = vp
        cf.CFNotificationCenterAddObserver.argtypes = [
            vp,
            vp,
            self._NotificationProc,
            vp,
            vp,
            ctypes.c_long,  # CFNotificationSuspensionBehavior (CFIndex)
        ]
        cf.CFNotificationCenterAddObserver.restype = None
        cf.CFNotificationCenterRemoveObserver.argtypes = [vp, vp, vp, vp]
        cf.CFNotificationCenterRemoveObserver.restype = None
        cs.UCKeyTranslate.argtypes = [
            vp,  # const UCKeyboardLayout *
            ctypes.c_uint16,  # virtualKeyCode
            ctypes.c_uint16,  # keyAction
            ctypes.c_uint32,  # modifierKeyState
            ctypes.c_uint32,  # keyboardType
            ctypes.c_uint32,  # OptionBits
            ctypes.POINTER(ctypes.c_uint32),  # deadKeyState
            ctypes.c_ulong,  # UniCharCount maxStringLength
            ctypes.POINTER(ctypes.c_ulong),  # UniCharCount *actualStringLength
            ctypes.POINTER(ctypes.c_uint16),  # UniChar unicodeString[]
        ]
        cs.UCKeyTranslate.restype = ctypes.c_int32
        self._layout_data_key = _cf_global(lib, "kTISPropertyUnicodeKeyLayoutData")
        self._layout_changed_name = _cf_global(lib, "kTISNotifySelectedKeyboardInputSourceChanged")
        self._cf, self._cs = cf, cs

    def layout_characters(
        self, modifiers: int = 0, *, ascii_capable: bool = False
    ) -> dict[int, str] | None:
        """What each character key types on the current keyboard layout.

        ``modifiers`` are Carbon modifier masks (``shiftKey``, ``optionKey``).
        ``ascii_capable`` reads the ASCII-capable layout that macOS uses for
        shortcuts while a Cyrillic layout or an input method is selected. Returns
        ``{keycode: text}`` (dead keys give their spacing character), or ``None``
        if the layout cannot be read. Main thread only, like all TIS calls.
        """
        if not self._load_layout_api():
            return None
        ctypes = self._ctypes
        lib = self._lib
        copy = (
            lib.TISCopyCurrentASCIICapableKeyboardLayoutInputSource
            if ascii_capable
            else lib.TISCopyCurrentKeyboardLayoutInputSource
        )
        source = copy()
        if not source:
            return None
        try:
            data = lib.TISGetInputSourceProperty(source, self._layout_data_key)
            layout = self._cf.CFDataGetBytePtr(data) if data else None
            if not layout:
                return None  # input methods have no Unicode key layout
            kbd_type = int(lib.LMGetKbdType())
            state = (modifiers >> 8) & 0xFF
            dead = ctypes.c_uint32()
            length = ctypes.c_ulong()
            buf = (ctypes.c_uint16 * 4)()
            chars: dict[int, str] = {}
            for keycode in _MAC_CHARACTER_KEYCODES:
                dead.value = 0
                length.value = 0
                status = self._cs.UCKeyTranslate(
                    layout,
                    keycode,
                    _UC_KEY_ACTION_DOWN,
                    state,
                    kbd_type,
                    _UC_KEY_TRANSLATE_NO_DEAD_KEYS,
                    ctypes.byref(dead),
                    len(buf),
                    ctypes.byref(length),
                    buf,
                )
                count = min(int(length.value), len(buf))
                if status != _NO_ERR or count <= 0:
                    continue
                text = bytes(buf)[: 2 * count].decode("utf-16-le", "replace")
                text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc")
                if text:
                    chars[keycode] = text
            return chars
        finally:
            self._cf.CFRelease(source)

    def observe_layout_changes(self, callback: Callable[[], None]) -> bool:
        """Call ``callback`` whenever the user selects another keyboard layout.

        The distributed notification is delivered by the main run loop (which Qt
        spins), so ``callback`` runs on the main thread. Returns success.
        """
        if self._layout_observer is not None:
            return True
        if not self._load_layout_api():
            return False
        ctypes = self._ctypes

        def trampoline(_center: Any, _observer: Any, _name: Any, _obj: Any, _info: Any) -> None:
            try:
                callback()
            except Exception:  # an exception must never unwind into CoreFoundation
                log.exception("Keyboard layout change handler failed")

        center = self._cf.CFNotificationCenterGetDistributedCenter()
        if not center:
            return False
        proc = self._NotificationProc(trampoline)
        observer = ctypes.c_void_p(id(self))  # any unique non-NULL token
        self._cf.CFNotificationCenterAddObserver(
            center,
            observer,
            proc,
            self._layout_changed_name,
            None,
            _CF_NOTIFICATION_DELIVER_IMMEDIATELY,
        )
        self._layout_proc = proc
        self._layout_observer = (center, observer)
        return True

    def stop_observing_layout_changes(self) -> None:
        if self._layout_observer is None:
            return
        center, observer = self._layout_observer
        self._cf.CFNotificationCenterRemoveObserver(
            center, observer, self._layout_changed_name, None
        )
        self._layout_observer = None
        self._layout_proc = None


class MacHotkeyManager(_NativeHotkeyManager):
    """Carbon ``RegisterEventHotKey`` hotkeys.

    Unlike an event tap this needs neither Accessibility nor Input Monitoring
    permission. Carbon is not thread-safe, so every call must come from the main
    thread (calls from other threads are refused with a warning); callbacks run on
    the main thread, dispatched by the Cocoa run loop Qt is already running.

    Carbon matches virtual key codes, which are physical key positions. Letters,
    digits and punctuation are therefore looked up in the current keyboard layout
    (see :func:`_mac_keycode`), so ⌃⌥A is the key labelled A on AZERTY and Dvorak
    as it is on Windows and X11, and the registrations move when the user switches
    layouts. Hotkeys are registered exclusively, so a combination that another
    application already owns is reported as unavailable, and Option(+Shift)
    combinations that type a character are refused.
    """

    name: ClassVar[str] = "carbon"

    def __init__(self, carbon: Any = None) -> None:
        super().__init__()
        self._carbon = carbon  # injectable for tests; created lazily otherwise
        self._handler_ref: Any = None
        self._refs: dict[int, Any] = {}
        self._keycodes: dict[int, int] = {}  # binding id → key code it is registered at
        self._keymap: dict[str, int] | None = None  # character → key code (current layout)
        self._observing = False

    @staticmethod
    def _on_main_thread(what: str) -> bool:
        if threading.current_thread() is threading.main_thread():
            return True
        log.warning("macOS hotkeys must be %s from the main thread", what)
        return False

    def _load_carbon(self) -> bool:
        if self._carbon is None:
            try:
                self._carbon = _Carbon()
            except (OSError, AttributeError) as exc:
                log.warning("Carbon hot-key API unavailable: %s", exc)
                self.note = "The Carbon hot-key API could not be loaded."
                return False
        return True

    def _backend_start(self) -> bool:
        if self._handler_ref is not None:
            return True
        if not self._on_main_thread("registered") or not self._load_carbon():
            return False
        status, ref = self._carbon.install_handler(self._on_event)
        if status != _NO_ERR:
            log.warning("InstallEventHandler failed (OSStatus %s)", status)
            return False
        self._handler_ref = ref
        self._keymap = self._read_keymap()
        try:
            self._observing = bool(self._carbon.observe_layout_changes(self._on_layout_changed))
        except Exception:
            log.debug("Cannot follow keyboard layout changes", exc_info=True)
        return True

    def _backend_stop(self) -> None:
        if self._handler_ref is None or not self._on_main_thread("stopped"):
            return
        if self._observing:
            try:
                self._carbon.stop_observing_layout_changes()
            except Exception:
                log.debug("Removing the keyboard layout observer failed", exc_info=True)
            self._observing = False
        for ref in self._refs.values():  # normally empty: stop() unregisters first
            self._carbon.unregister_hotkey(ref)
        self._refs.clear()
        self._keycodes.clear()
        self._carbon.remove_handler(self._handler_ref)
        self._handler_ref = None
        self._keymap = None

    def layout_conflict(self, hotkey: Hotkey | str) -> str | None:
        hotkey = _as_hotkey(hotkey)
        if (
            not _mac_option_types(hotkey)
            or threading.current_thread() is not threading.main_thread()
        ):
            return None
        try:
            if not self._load_carbon():
                return None
            keymap = self._keymap if self._handler_ref is not None else self._read_keymap()
            keycode = _mac_keycode(hotkey.key, keymap)
            return None if keycode is None else self._option_conflict(hotkey, keycode)
        except Exception:  # advisory only: never break a settings dialog
            log.debug("Keyboard layout check for %s failed", hotkey, exc_info=True)
            return None

    def _read_keymap(self) -> dict[str, int] | None:
        """Character → key code of the current layout, completed by the ASCII-capable one."""
        try:
            current = self._carbon.layout_characters(0)
            ascii_capable = self._carbon.layout_characters(0, ascii_capable=True)
        except Exception:
            log.debug("Reading the keyboard layout failed", exc_info=True)
            return None
        if current is None and ascii_capable is None:
            return None
        keymap = _mac_char_keymap(ascii_capable or {})
        keymap.update(_mac_char_keymap(current or {}))
        return keymap

    def _option_conflict(self, hotkey: Hotkey, keycode: int) -> str | None:
        """Why an Option(+Shift) hotkey would eat a character typed with Option, or ``None``."""
        if not _mac_option_types(hotkey):
            return None
        chars = self._carbon.layout_characters(_mac_modifiers(hotkey.modifiers))
        text = (chars or {}).get(keycode)
        if not text:
            return None
        return (
            f"{format_hotkey(hotkey, macos=True)} types {_describe_typed(text)} on the "
            "current keyboard layout"
        )

    def _native_register(self, binding: _Binding) -> bool:
        if not self._on_main_thread("registered"):
            return self._reject(binding, "macOS hotkeys can only be registered on the main thread")
        return self._register_carbon(binding)

    def _register_carbon(self, binding: _Binding) -> bool:
        hotkey = binding.hotkey
        label = format_hotkey(hotkey, macos=True)
        keycode = _mac_keycode(hotkey.key, self._keymap)
        if keycode is None:
            if hotkey.key in _PUNCTUATION:
                return self._reject(
                    binding,
                    f"{label}: no key types {_describe_typed(_PUNCTUATION[hotkey.key])} "
                    "without modifiers on the current keyboard layout",
                )
            return self._reject(binding, f"{label}: Mac keyboards have no {_key_label(hotkey.key)}")
        try:
            conflict = self._option_conflict(hotkey, keycode)
        except Exception:  # the check must never make hotkeys unusable
            log.warning("Could not check %s against the keyboard layout", label, exc_info=True)
            conflict = None
        if conflict is not None:
            return self._reject(binding, conflict)
        status, ref = self._carbon.register_hotkey(
            keycode, _mac_modifiers(hotkey.modifiers), _HOTKEY_SIGNATURE, binding.id
        )
        if status != _NO_ERR:
            if status == _EVENT_HOTKEY_EXISTS_ERR:
                return self._reject(binding, f"{label} is already in use by another application")
            return self._reject(binding, f"RegisterEventHotKey({label}) failed (OSStatus {status})")
        self._refs[binding.id] = ref
        self._keycodes[binding.id] = keycode
        return True

    def _native_unregister(self, binding: _Binding) -> None:
        ref = self._refs.pop(binding.id, None)
        self._keycodes.pop(binding.id, None)
        if ref is not None and self._on_main_thread("unregistered"):
            status = self._carbon.unregister_hotkey(ref)
            if status != _NO_ERR:
                log.debug("UnregisterEventHotKey failed (OSStatus %s)", status)

    def _on_layout_changed(self) -> None:
        """Move every registration to the key that types its character on the new layout.

        Bindings that cannot be registered on the new layout stay known (inactive)
        and are retried on the next layout change.
        """
        if self._handler_ref is None or not self._on_main_thread("updated"):
            return
        with self._api_lock:
            self._keymap = self._read_keymap()
            with self._table_lock:
                bindings = list(self._bindings.values())
            for binding in bindings:
                keycode = _mac_keycode(binding.hotkey.key, self._keymap)
                if binding.id in self._refs and keycode == self._keycodes.get(binding.id):
                    continue
                ref = self._refs.pop(binding.id, None)
                self._keycodes.pop(binding.id, None)
                if ref is not None:
                    self._carbon.unregister_hotkey(ref)
                self._set_active(binding, self._register_carbon(binding))

    def _on_event(self, event: Any) -> int:
        status, signature, hotkey_id = self._carbon.hotkey_id(event)
        if status != _NO_ERR or signature != _HOTKEY_SIGNATURE:
            return _EVENT_NOT_HANDLED_ERR  # someone else's hot key: let it propagate
        self._dispatch(hotkey_id)
        return _NO_ERR


def _mac_option_types(hotkey: Hotkey) -> bool:
    """Whether ``hotkey`` uses Option without Control/Command: the macOS typing layer."""
    return "alt" in hotkey.modifiers and not hotkey.modifiers & {"ctrl", "meta"}


# The X11 backend lives in its own module; the factory picks it lazily below.
from .hotkeys_x11 import X11HotkeyManager  # noqa: E402  (avoids the import cycle)


# --------------------------------------------------------------------------- factory
def _is_wayland_session() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(
        os.environ.get("WAYLAND_DISPLAY")
    )


#: The ``ctl`` commands to bind where global hotkeys are unavailable.
_CTL_SHORTCUT_ACTIONS = ("toggle", "privacy-toggle", "calibrate")


def _ctl_commands() -> str:
    """``'… ctl toggle', '… ctl privacy-toggle' or '… ctl calibrate'``, spelled as
    this copy is run: the release packages put no ``eye-tracker`` command on the
    PATH (an AppImage is called by its file name, the macOS app has
    ``eye-tracker-cli`` inside it), and a desktop shortcut bound to a command
    that does not exist fails without a word. Built when a manager is created,
    because a ``--config-dir`` profile becomes part of the command."""
    from ..command_text import cli_command_text  # naming only; no runtime dependency

    commands = [f"'{cli_command_text('ctl', action)}'" for action in _CTL_SHORTCUT_ACTIONS]
    return f"{', '.join(commands[:-1])} or {commands[-1]}"


def _wayland_note() -> str:
    return (
        "Wayland does not allow applications to register global hotkeys. Bind "
        f"{_ctl_commands()} to shortcuts in your desktop's keyboard settings."
    )


def _xwayland_note() -> str:
    return (
        "Running under Wayland: hotkeys are grabbed through XWayland and may only fire "
        "while an X11 window has focus. For reliable shortcuts bind "
        f"{_ctl_commands()} in your desktop's keyboard settings."
    )


def create_hotkey_manager() -> HotkeyManager:
    """Return the best hotkey manager for this system (never raises)."""
    try:
        if sys.platform == "win32":
            return WindowsHotkeyManager()
        if sys.platform == "darwin":
            return MacHotkeyManager()
        if not os.environ.get("DISPLAY"):
            if _is_wayland_session():
                return HotkeyManager(note=_wayland_note())
            return HotkeyManager(note="No X11 display is available for global hotkeys.")
        try:
            import Xlib  # noqa: F401  (availability probe only)
        except ImportError:
            return HotkeyManager(note="python-xlib is not installed; global hotkeys are off.")
        note = _xwayland_note() if _is_wayland_session() else None
        return X11HotkeyManager(note=note)
    except Exception as exc:  # defensive: hotkeys are optional
        log.warning("Global hotkeys unavailable: %s", exc)
        return HotkeyManager(note=f"Global hotkeys unavailable: {exc}")


__all__ = [
    "KEYS",
    "MODIFIERS",
    "Hotkey",
    "HotkeyCallback",
    "HotkeyManager",
    "MacHotkeyManager",
    "WindowsHotkeyManager",
    "X11HotkeyManager",
    "create_hotkey_manager",
    "format_hotkey",
    "parse_hotkey",
]

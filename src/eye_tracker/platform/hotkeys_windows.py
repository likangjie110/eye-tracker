"""The Windows global-hotkey backend.

Split out of ``platform/hotkeys.py`` (phase 4 / B2): RegisterHotKey on a dedicated
message-loop thread, plus the keyboard-layout probes that refuse combinations which would
type something else (AltGr arrives as Ctrl+Alt). It imports ``hotkeys_base`` only and is safe
to import on every OS; the Win32 libraries are imported inside the methods that need them."""

from __future__ import annotations

import contextlib
import functools
import logging
import sys
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from typing import ClassVar, Protocol

from .hotkeys_base import (
    _DIGITS,
    _LETTERS,
    _PUNCTUATION,
    Hotkey,
    _as_hotkey,
    _Binding,
    _describe_typed,
    _key_label,
    _ThreadedHotkeyManager,
    format_hotkey,
)

log = logging.getLogger(__name__)


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

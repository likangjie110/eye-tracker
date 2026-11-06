"""The macOS global-hotkey backend.

Split out of ``platform/hotkeys.py`` (phase 4 / B2): Carbon RegisterEventHotKey on the main
run loop, plus the Option-as-typing and keyboard-layout handling. It imports ``hotkeys_base``
only and is safe to import on every OS; pyobjc is imported inside the methods that need it."""

from __future__ import annotations

import logging
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from typing import Any, ClassVar

from .hotkeys_base import (
    _PUNCTUATION,
    Hotkey,
    _as_hotkey,
    _Binding,
    _describe_typed,
    _key_label,
    _NativeHotkeyManager,
    format_hotkey,
)

log = logging.getLogger(__name__)


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

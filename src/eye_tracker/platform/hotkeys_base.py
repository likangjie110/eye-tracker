"""Shared model, parser and manager bases for the global-hotkey backends.

Split out of ``platform/hotkeys.py`` (phase 4 / B2). The platform modules (X11 today; Windows and
macOS next) import from here, and ``hotkeys.py`` re-exports the public names; this module imports
no other hotkey module, so the facade/backend cycle cannot come back.

The parsing/formatting half is pure Python; the manager bases contain the bookkeeping shared by
the native implementations (binding table, thread hand-off, layout-conflict policy).
"""

from __future__ import annotations

import logging
import queue
import re
import sys
import threading
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

log = logging.getLogger(__name__)


HotkeyCallback = Callable[[], None]

MODIFIERS: tuple[str, ...] = ("ctrl", "alt", "shift", "meta")
"""Canonical modifier names. ``meta`` is the Windows key, Command on macOS and Super on Linux."""

_LETTERS: tuple[str, ...] = tuple("abcdefghijklmnopqrstuvwxyz")
_DIGITS: tuple[str, ...] = tuple("0123456789")
_FKEYS = tuple(f"f{n}" for n in range(1, 25))
_NAMED_KEYS = (
    "space",
    "enter",
    "tab",
    "escape",
    "backspace",
    "insert",
    "delete",
    "home",
    "end",
    "pageup",
    "pagedown",
    "left",
    "right",
    "up",
    "down",
)
# Canonical punctuation names follow the X11 keysym names; the value is the character
# the key produces on a US layout.
_PUNCTUATION: dict[str, str] = {
    "minus": "-",
    "equal": "=",
    "bracketleft": "[",
    "bracketright": "]",
    "backslash": "\\",
    "semicolon": ";",
    "quote": "'",
    "grave": "`",
    "comma": ",",
    "period": ".",
    "slash": "/",
}

KEYS: tuple[str, ...] = _LETTERS + _DIGITS + _FKEYS + _NAMED_KEYS + tuple(_PUNCTUATION)
"""Every key name accepted by :class:`Hotkey` (canonical, lower case)."""

_KEY_SET = frozenset(KEYS)

# Keys that produce text or drive text editing. Grabbing them with Shift as the only
# modifier would make it impossible, system-wide, to type capitals, use Shift+Tab or
# extend a selection (Shift+arrows/Home/End/PgUp/PgDn, which also scroll terminals).
_TYPING_KEYS = frozenset(
    _LETTERS
    + _DIGITS
    + tuple(_PUNCTUATION)
    + ("space", "enter", "tab", "backspace", "delete", "insert")
    + ("left", "right", "up", "down", "home", "end", "pageup", "pagedown")
)


def _invert(groups: dict[str, tuple[str, ...]]) -> dict[str, str]:
    return {alias: canonical for canonical, aliases in groups.items() for alias in aliases}


_MODIFIER_ALIASES: dict[str, str] = _invert(
    {
        "ctrl": ("ctrl", "control", "ctl", "strg", "⌃"),
        "alt": ("alt", "option", "opt", "⌥"),
        "shift": ("shift", "⇧"),
        "meta": ("meta", "cmd", "command", "win", "windows", "super", "⌘"),
    }
)
_MODIFIER_SYMBOLS = frozenset("⌃⌥⇧⌘")

_KEY_ALIASES: dict[str, str] = _invert(
    {
        # Qt portable names, common spellings and the macOS menu glyphs (so that
        # format_hotkey(..., macos=True) round-trips through parse_hotkey).
        "escape": ("esc", "⎋"),
        "enter": ("return", "↩", "⏎", "↵"),
        "tab": ("⇥",),
        "space": ("spacebar", "␣"),
        "backspace": ("bksp", "⌫"),
        "insert": ("ins", "help"),
        "delete": ("del", "forwarddelete", "⌦"),
        "home": ("↖",),
        "end": ("↘",),
        "pageup": ("pgup", "prior", "⇞"),
        "pagedown": ("pgdn", "pgdown", "next", "⇟"),
        "left": ("arrowleft", "leftarrow", "←"),
        "right": ("arrowright", "rightarrow", "→"),
        "up": ("arrowup", "uparrow", "↑"),
        "down": ("arrowdown", "downarrow", "↓"),
        "minus": ("dash", "hyphen"),
        "equal": ("equals",),
        "bracketleft": ("leftbracket", "lbracket"),
        "bracketright": ("rightbracket", "rbracket"),
        "quote": ("apostrophe",),
        "grave": ("backquote", "backtick"),
        "period": ("dot", "fullstop"),
        "slash": ("forwardslash",),
    }
)
_KEY_ALIASES.update({char: name for name, char in _PUNCTUATION.items()})

_DISPLAY_NAMES: dict[str, str] = {
    "space": "Space",
    "enter": "Enter",
    "tab": "Tab",
    "escape": "Esc",
    "backspace": "Backspace",
    "insert": "Insert",
    "delete": "Delete",
    "home": "Home",
    "end": "End",
    "pageup": "PgUp",
    "pagedown": "PgDn",
    "left": "Left",
    "right": "Right",
    "up": "Up",
    "down": "Down",
    **_PUNCTUATION,
}
_MAC_DISPLAY_NAMES: dict[str, str] = {
    **_DISPLAY_NAMES,
    "enter": "↩",
    "tab": "⇥",
    "escape": "⎋",
    "backspace": "⌫",
    "insert": "Help",
    "delete": "⌦",
    "home": "↖",
    "end": "↘",
    "pageup": "⇞",
    "pagedown": "⇟",
    "left": "←",
    "right": "→",
    "up": "↑",
    "down": "↓",
}
_MAC_MODIFIER_SYMBOLS = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "meta": "⌘"}

_SEPARATORS = re.compile(r"[\s_\-]+")


# --------------------------------------------------------------------------- model
@dataclass(frozen=True)
class Hotkey:
    """A key plus at least one modifier.

    ``modifiers`` is a subset of :data:`MODIFIERS`; ``key`` is one of :data:`KEYS`.
    ``str(hotkey)`` gives the canonical settings form, e.g. ``"ctrl+alt+p"``.
    Construction validates both and raises :class:`ValueError` otherwise.
    """

    modifiers: frozenset[str]
    key: str

    def __post_init__(self) -> None:
        # Accept any iterable of modifier names but always store a frozenset so that
        # equality and hashing do not depend on how the object was built.
        mods = frozenset(self.modifiers)
        object.__setattr__(self, "modifiers", mods)
        unknown = sorted(mods.difference(MODIFIERS))
        if unknown:
            raise ValueError(f"unknown modifier(s): {', '.join(unknown)}")
        if self.key not in _KEY_SET:
            raise ValueError(f"unsupported key: {self.key!r}")
        if not mods:
            raise ValueError("a global hotkey needs at least one modifier (Ctrl, Alt, Shift, Meta)")
        if mods == {"shift"} and self.key in _TYPING_KEYS:
            raise ValueError(
                "Shift alone is not enough for a typing or text-navigation key; "
                "add Ctrl, Alt or Meta"
            )

    @property
    def ordered_modifiers(self) -> tuple[str, ...]:
        """Modifiers in canonical order (ctrl, alt, shift, meta)."""
        return tuple(m for m in MODIFIERS if m in self.modifiers)

    def __str__(self) -> str:
        return "+".join((*self.ordered_modifiers, self.key))


def _normalise_token(token: str) -> str:
    token = token.strip().lower()
    # Single characters are keys in their own right ("-" is the minus key), so only
    # multi-character words have their inner separators removed ("Page Up" → "pageup").
    if len(token) > 1:
        token = _SEPARATORS.sub("", token)
    return token


def _split_tokens(text: str) -> list[str]:
    """Split ``"Ctrl + Alt+P"`` or ``"⌃⌥P"`` into raw tokens (last one is the key)."""
    tokens: list[str] = []
    for raw in text.split("+"):
        part = raw.strip()
        peeled = False
        # macOS style glyphs may be glued to each other and to the key: "⌃⌥⇧P".
        while len(part) > 1 and part[0] in _MODIFIER_SYMBOLS:
            tokens.append(part[0])
            part = part[1:].strip()
            peeled = True
        if part or not peeled:
            tokens.append(part)
    return tokens


def parse_hotkey(text: str) -> Hotkey:
    """Parse a user-facing hotkey string.

    Accepts ``"Ctrl+Alt+P"``, ``"ctrl + alt + p"``, ``"cmd+shift+g"`` and macOS glyph
    notation such as ``"⌃⌥P"``. Matching is case-insensitive and tolerant of spaces.
    Aliases: ``cmd``/``command``/``win``/``windows``/``super`` → ``meta``,
    ``option``/``opt`` → ``alt``, ``control``/``ctl`` → ``ctrl``; key aliases include
    ``esc``, ``return``, ``del``, ``ins``, ``pgup``/``pgdn`` and punctuation characters.

    Note for Qt callers: ``QKeySequence.toString(PortableText)`` on macOS calls the
    Command key ``Ctrl`` and the Control key ``Meta``; swap them before parsing.

    Raises:
        ValueError: if the text is empty, uses an unknown modifier or key, has no
            modifier, or consists of modifiers only.
    """
    if not isinstance(text, str):
        raise ValueError(f"hotkey must be a string, not {type(text).__name__}")
    stripped = text.strip()
    if not stripped:
        raise ValueError("hotkey is empty")
    if stripped == "+" or (stripped.endswith("+") and stripped[:-1].rstrip().endswith("+")):
        # "Ctrl++": '+' is Shift+'=' on most layouts, so it has no key of its own.
        raise ValueError(f"the '+' key cannot be used in hotkey {text!r}; use '=' instead")
    tokens = _split_tokens(stripped)
    *mod_tokens, key_token = tokens

    modifiers: set[str] = set()
    for raw in mod_tokens:
        token = _normalise_token(raw)
        if not token:
            raise ValueError(f"malformed hotkey {text!r}: empty part")
        modifier = _MODIFIER_ALIASES.get(token)
        if modifier is None:
            raise ValueError(f"unknown modifier {raw.strip()!r} in hotkey {text!r}")
        modifiers.add(modifier)

    key = _normalise_token(key_token)
    if not key:
        raise ValueError(f"malformed hotkey {text!r}: missing key")
    if key in _MODIFIER_ALIASES:
        raise ValueError(f"hotkey {text!r} needs a key in addition to its modifiers")
    key = _KEY_ALIASES.get(key, key)
    if key not in _KEY_SET:
        raise ValueError(f"unsupported key {key_token.strip()!r} in hotkey {text!r}")
    if not modifiers:
        raise ValueError(f"hotkey {text!r} needs at least one modifier (Ctrl, Alt, Shift or Meta)")
    return Hotkey(frozenset(modifiers), key)


def format_hotkey(hk: Hotkey, macos: bool | None = None) -> str:
    """Human-readable label: ``"Ctrl+Alt+P"`` (Windows/Linux) or ``"⌃⌥P"`` (macOS).

    ``macos=None`` follows the running system. The meta key is shown as ``Win`` on
    Windows and ``Super`` elsewhere. The result is always accepted by
    :func:`parse_hotkey`.
    """
    if macos is None:
        macos = sys.platform == "darwin"
    if macos:
        key = _key_label(hk.key, macos=True)
        return "".join(_MAC_MODIFIER_SYMBOLS[m] for m in hk.ordered_modifiers) + key
    return "+".join([*_modifier_labels(hk.modifiers), _key_label(hk.key)])


def _modifier_labels(modifiers: Iterable[str]) -> list[str]:
    """Windows/Linux labels of ``modifiers`` in canonical order (``["Ctrl", "Alt"]``)."""
    meta = "Win" if sys.platform == "win32" else "Super"
    labels = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "meta": meta}
    wanted = set(modifiers)
    return [labels[m] for m in MODIFIERS if m in wanted]


def _key_label(key: str, macos: bool = False) -> str:
    """Display name of a canonical key (``"pageup"`` → ``"PgUp"``, ``"t"`` → ``"T"``)."""
    return (_MAC_DISPLAY_NAMES if macos else _DISPLAY_NAMES).get(key, key.upper())


def _describe_typed(text: str) -> str:
    """``text`` (what a key types) quoted for a user-facing message.

    Visible characters are quoted as they are (``'ć'``). Blank and invisible ones,
    such as the no-break space that Option+Space types on a Mac, and lone combining
    marks (which would sit on the quote sign) are named instead (``no-break space
    (U+00A0)``): ``repr()`` would show Python escapes like ``'\\xa0'``, which mean
    nothing to users.
    """
    text = unicodedata.normalize("NFC", text)  # 'a' + combining acute → 'á'
    if text and all(_is_visible(ch) for ch in text):
        quote = '"' if "'" in text else "'"
        return f"{quote}{text}{quote}"
    names = []
    for ch in text:
        name = unicodedata.name(ch, "")
        code = f"U+{ord(ch):04X}"
        names.append(f"{name.lower()} ({code})" if name else code)
    return " + ".join(names) or "nothing"


def _is_visible(ch: str) -> bool:
    """Whether ``ch`` shows up as a glyph of its own when printed between quotes."""
    return ch.isprintable() and not ch.isspace() and not unicodedata.category(ch).startswith("M")


def _as_hotkey(hotkey: Hotkey | str) -> Hotkey:
    """``hotkey`` itself, or the parsed string (raises ``ValueError`` if invalid)."""
    return parse_hotkey(hotkey) if isinstance(hotkey, str) else hotkey


# ------------------------------------------------------------------------- managers
class HotkeyManager:
    """Registers global hotkeys. This base class is the *unsupported* no-op.

    Native implementations share these semantics:

    * :meth:`register` starts the backend on demand and returns whether the OS
      accepted the combination (``False`` if another application owns it, or if
      it would swallow a character the user types, see :meth:`layout_conflict`).
      Re-registering a name replaces its previous hotkey; registering a
      combination that another name in this manager already uses returns ``False``.
      :meth:`last_error` says why a registration failed.
    * :meth:`start` is optional (it pre-warms the backend). :meth:`stop` releases
      every hotkey, ends the backend and is idempotent; a later :meth:`register`
      starts it again.
    * Callbacks run on an arbitrary thread; exceptions they raise are logged.
    """

    supported: ClassVar[bool] = False
    name: ClassVar[str] = "none"

    def __init__(self, note: str | None = None) -> None:
        #: Human-readable remark on availability (why hotkeys are unavailable or
        #: limited here); ``None`` when fully supported. Shown by the UI and doctor.
        self.note = note
        # name → why its last registration failed or its hotkey stopped working.
        self._errors: dict[str, str] = {}

    def register(self, name: str, hotkey: Hotkey | str, callback: HotkeyCallback) -> bool:
        """Bind ``hotkey`` to ``callback`` under ``name``. Returns success."""
        log.debug("Global hotkeys unsupported here; %r not registered", name)
        self._errors[name] = self.note or "global hotkeys are not supported on this system"
        return False

    def unregister(self, name: str) -> bool:
        """Release the hotkey registered under ``name``. Returns whether one existed."""
        return False

    def unregister_all(self) -> None:
        """Release every hotkey (the backend keeps running)."""

    @property
    def registered(self) -> dict[str, Hotkey]:
        """Registrations that are currently active, by name.

        A hotkey that stopped working (for example after a keyboard layout change
        removed its key) is left out until it works again; see :meth:`last_error`.
        """
        return {}

    def last_error(self, name: str) -> str | None:
        """Why the latest :meth:`register` of ``name`` failed, or why it stopped working.

        The text is a short, user-presentable sentence such as ``"Ctrl+Alt+C is
        AltGr+C and types 'ć' on the Polish (Programmers) keyboard layout"``.
        ``None`` if the hotkey is active or ``name`` was never registered.
        """
        return self._errors.get(name)

    def layout_conflict(self, hotkey: Hotkey | str) -> str | None:
        """Why ``hotkey`` clashes with how the user types, or ``None``.

        On Windows, Ctrl+Alt is AltGr: a global Ctrl+Alt+C would eat every 'ć' typed
        on a Polish keyboard. On macOS, Option (+Shift) alone types characters. On
        X11, a keyboard-layout switch such as Alt+Shift (XKB option
        ``grp:alt_shift_toggle``) means a hotkey containing both keys can never be
        pressed: the layout changes instead and the rest of the chord reaches other
        applications (likewise when an XKB option makes Win or Alt a layout or AltGr
        key). The native managers check this on every installed (Windows),
        the current (macOS) or the configured (X11) keyboard layout and refuse such
        registrations; this method lets settings UIs and diagnostics warn before
        registering. It never changes the keyboard state or the active layout.

        Raises:
            ValueError: if ``hotkey`` is a string that is not a valid hotkey.
        """
        _as_hotkey(hotkey)
        return None

    def start(self) -> None:
        """Start the backend early (optional; :meth:`register` starts it on demand)."""

    def stop(self) -> None:
        """Release every hotkey and stop the backend. Safe to call repeatedly."""


@dataclass(eq=False)
class _Binding:
    id: int
    name: str
    hotkey: Hotkey
    callback: HotkeyCallback
    #: Set by a native hook that refused the binding only because of the current
    #: keyboard layout or its options: the binding is kept (inactive) and tried
    #: again when the layout changes, like one whose key vanished later.
    retry_on_layout_change: bool = False


class _NativeHotkeyManager(HotkeyManager):
    """Bookkeeping shared by the native managers.

    Subclasses implement the ``_backend_*`` and ``_native_*`` hooks. Two locks keep
    dispatch deadlock-free: ``_api_lock`` serialises public calls (and may be held
    while waiting for a backend thread), ``_table_lock`` only ever guards short
    dictionary operations and is the only lock taken when a hotkey fires.

    A binding whose OS registration is lost later (an X11 or macOS keyboard layout
    change removed its key) stays in ``_bindings`` so that the next layout change
    can restore it, but it is marked inactive: :attr:`registered` leaves it out and
    :meth:`last_error` explains why. So does a binding that a native hook refused
    only because of the current layout (``_Binding.retry_on_layout_change``):
    :meth:`register` still returns ``False`` for it.
    """

    supported: ClassVar[bool] = True
    _MAX_ID: ClassVar[int] = 0xBFFF  # RegisterHotKey accepts application ids 0..0xBFFF

    def __init__(self, note: str | None = None) -> None:
        super().__init__(note)
        self._api_lock = threading.RLock()
        self._table_lock = threading.Lock()
        self._bindings: dict[str, _Binding] = {}
        self._by_id: dict[int, _Binding] = {}
        self._inactive: set[int] = set()  # ids of bindings whose OS registration was lost
        self._last_id = 0

    # ------------------------------------------------------------------ public
    def register(self, name: str, hotkey: Hotkey | str, callback: HotkeyCallback) -> bool:
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._table_lock:
            self._errors.pop(name, None)
        if isinstance(hotkey, str):
            try:
                hotkey = parse_hotkey(hotkey)
            except ValueError as exc:
                log.warning("Hotkey for %r not registered: %s", name, exc)
                self._set_error(name, str(exc))
                return False
        label = format_hotkey(hotkey)
        with self._api_lock:
            existing = self._bindings.get(name)
            if (
                existing is not None
                and existing.hotkey == hotkey
                and existing.id not in self._inactive
            ):
                with self._table_lock:
                    existing.callback = callback
                return True
            clash = next(
                (b for b in self._bindings.values() if b.hotkey == hotkey and b.name != name),
                None,
            )
            if clash is not None:
                log.warning("Hotkey %s for %r is already bound to %r", label, name, clash.name)
                self._set_error(name, f"{label} is already used for {clash.name}")
                return False
            if existing is not None:
                self._remove(existing)
            if not self._backend_start():
                log.warning("Hotkey %s for %r not registered: backend unavailable", label, name)
                self._set_error(name, self.note or "the global hotkey service is unavailable")
                return False
            binding = _Binding(self._allocate_id(), name, hotkey, callback)
            # Publish before the OS registration so a press that arrives immediately
            # afterwards can already be dispatched.
            with self._table_lock:
                self._by_id[binding.id] = binding
            ok = False
            try:
                ok = bool(self._native_register(binding))
            except Exception:
                log.exception("Registering hotkey %s for %r failed", label, name)
            with self._table_lock:
                if ok:
                    self._bindings[name] = binding
                    self._errors.pop(name, None)
                elif binding.retry_on_layout_change:
                    # Kept, inactive, for the layout change that makes it work.
                    self._bindings[name] = binding
                    self._inactive.add(binding.id)
                    self._errors.setdefault(name, f"{label} could not be registered")
                else:
                    self._by_id.pop(binding.id, None)
                    self._errors.setdefault(name, f"{label} could not be registered")
            if ok:
                log.info("Registered hotkey %s for %s", label, name)
            return ok

    def unregister(self, name: str) -> bool:
        with self._api_lock:
            binding = self._bindings.get(name)
            if binding is None:
                return False
            self._remove(binding)
            return True

    def unregister_all(self) -> None:
        with self._api_lock:
            for binding in list(self._bindings.values()):
                self._remove(binding)

    @property
    def registered(self) -> dict[str, Hotkey]:
        with self._table_lock:
            return {
                name: b.hotkey for name, b in self._bindings.items() if b.id not in self._inactive
            }

    def last_error(self, name: str) -> str | None:
        with self._table_lock:
            return self._errors.get(name)

    def start(self) -> None:
        with self._api_lock:
            if not self._backend_start():
                log.warning("Global hotkey backend %s could not start", self.name)

    def stop(self) -> None:
        with self._api_lock:
            for binding in list(self._bindings.values()):
                self._remove(binding)
            try:
                self._backend_stop()
            except Exception:
                log.exception("Stopping the hotkey backend failed")

    # --------------------------------------------------------------- internals
    def _allocate_id(self) -> int:
        with self._table_lock:
            for _ in range(self._MAX_ID):
                self._last_id = self._last_id % self._MAX_ID + 1
                if self._last_id not in self._by_id:
                    return self._last_id
        raise RuntimeError("no free hotkey id")  # pragma: no cover - 49k live hotkeys

    def _remove(self, binding: _Binding) -> None:
        # Forget the binding first so that a press racing with the unregistration is
        # simply ignored instead of calling a callback the owner already dropped.
        with self._table_lock:
            self._bindings.pop(binding.name, None)
            self._by_id.pop(binding.id, None)
            self._inactive.discard(binding.id)
        try:
            self._native_unregister(binding)
        except Exception:
            log.exception("Unregistering hotkey %s failed", format_hotkey(binding.hotkey))

    def _set_error(self, name: str, message: str) -> None:
        with self._table_lock:
            self._errors[name] = message

    def _reject(self, binding: _Binding, message: str) -> bool:
        """Log why the OS did not take ``binding``, remember it for :meth:`last_error`.

        Called by the native hooks (on whichever thread they run); always returns
        ``False`` so it can end a hook with ``return self._reject(...)``.
        """
        log.warning("Hotkey for %r not registered: %s", binding.name, message)
        self._set_error(binding.name, message)
        return False

    def _set_active(self, binding: _Binding, active: bool) -> None:
        """Record whether a registered binding currently holds its OS registration."""
        with self._table_lock:
            if active:
                self._inactive.discard(binding.id)
                self._errors.pop(binding.name, None)
            elif binding.id in self._by_id:
                self._inactive.add(binding.id)

    def _dispatch(self, binding_id: int) -> None:
        with self._table_lock:
            binding = self._by_id.get(binding_id)
            callback = binding.callback if binding is not None else None
        if binding is None or callback is None:
            log.debug("Ignoring event for unknown hotkey id %s", binding_id)
            return
        log.debug("Hotkey %s pressed (%s)", format_hotkey(binding.hotkey), binding.name)
        try:
            callback()
        except Exception:
            log.exception("Hotkey callback for %r raised", binding.name)

    # ------------------------------------------------------------------- hooks
    def _backend_start(self) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def _backend_stop(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _native_register(self, binding: _Binding) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def _native_unregister(self, binding: _Binding) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


class _PendingCall:
    """A function to run on the backend thread, with a result the caller waits for."""

    __slots__ = ("_lock", "_state", "done", "error", "fn", "result")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self._lock = threading.Lock()
        self._state = "pending"  # → "running" → "finished", or "cancelled"

    def run(self) -> None:
        with self._lock:
            if self._state != "pending":
                return
            self._state = "running"
        try:
            self.result = self.fn()
        except Exception as exc:
            self.error = exc
        finally:
            with self._lock:
                self._state = "finished"
            self.done.set()

    def cancel(self) -> bool:
        """Prevent a call that has not started yet. Returns ``True`` if it was prevented."""
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "cancelled"
        self.done.set()
        return True

    @property
    def finished(self) -> bool:
        return self._state == "finished"


class _ThreadedHotkeyManager(_NativeHotkeyManager):
    """A native manager whose OS registrations live on one dedicated thread.

    Windows hotkeys are bound to the registering thread's message queue and an
    Xlib ``Display`` must not be shared between threads, so every OS call is
    executed on the backend thread via :meth:`_call_on_thread`.
    """

    _CALL_TIMEOUT_S: ClassVar[float] = 2.0
    _JOIN_TIMEOUT_S: ClassVar[float] = 2.0

    def __init__(self, note: str | None = None) -> None:
        super().__init__(note)
        self._thread: threading.Thread | None = None
        self._calls: queue.SimpleQueue[_PendingCall] = queue.SimpleQueue()
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._start_ok = False

    # ------------------------------------------------------------- lifecycle
    def _backend_start(self) -> bool:
        thread = self._thread
        if thread is not None and thread.is_alive():
            if not self._stopping.is_set():
                return self._start_ok
            # A stop() issued from inside a callback leaves the thread finishing its
            # current message; never run two backend threads at once.
            if thread is threading.current_thread():
                return False
            thread.join(self._JOIN_TIMEOUT_S)
            if thread.is_alive():
                log.warning("Previous hotkey thread is still running")
                return False
        self._stopping.clear()
        self._ready.clear()
        self._start_ok = False
        if not self._before_thread_start():
            return False
        thread = threading.Thread(
            target=self._thread_main, name=f"eye-tracker-hotkeys-{self.name}", daemon=True
        )
        self._thread = thread
        thread.start()
        if not self._ready.wait(self._CALL_TIMEOUT_S):
            log.warning("Hotkey thread did not become ready in time")
            return False
        return self._start_ok

    def _backend_stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stopping.set()
        self._wake_for_stop()
        if thread is not threading.current_thread():
            thread.join(self._JOIN_TIMEOUT_S)
            if thread.is_alive():
                log.warning("Hotkey thread did not stop within %.1f s", self._JOIN_TIMEOUT_S)
        if not thread.is_alive():
            self._thread = None

    def _thread_main(self) -> None:
        try:
            self._start_ok = bool(self._thread_setup())
        except Exception:
            log.exception("Hotkey backend %s failed to initialise", self.name)
            self._start_ok = False
        self._ready.set()
        try:
            if self._start_ok:
                self._thread_loop()
        except Exception:
            log.exception("Hotkey thread (%s) crashed", self.name)
        finally:
            try:
                self._thread_teardown()
            except Exception:
                log.exception("Hotkey backend %s cleanup failed", self.name)
            self._cancel_pending_calls()

    # ------------------------------------------------------------ cross-thread
    def _call_on_thread(self, fn: Callable[[], Any], default: Any) -> Any:
        thread = self._thread
        if thread is None or not thread.is_alive():
            return default
        if threading.current_thread() is thread:
            return fn()
        if self._stopping.is_set():
            return default
        call = _PendingCall(fn)
        self._calls.put(call)
        if not self._wake():
            call.cancel()
            return default
        if not call.done.wait(self._CALL_TIMEOUT_S) and not call.cancel():
            # Already running: give the native call a little longer to finish.
            call.done.wait(self._CALL_TIMEOUT_S)
        if not call.finished:
            log.warning("Hotkey thread did not answer in time")
            return default
        if call.error is not None:
            log.error("Hotkey operation failed on the hotkey thread", exc_info=call.error)
            return default
        return call.result

    def _run_pending_calls(self) -> None:
        while True:
            try:
                call = self._calls.get_nowait()
            except queue.Empty:
                return
            call.run()

    def _cancel_pending_calls(self) -> None:
        while True:
            try:
                call = self._calls.get_nowait()
            except queue.Empty:
                return
            call.cancel()

    # ------------------------------------------------------------------- hooks
    def _before_thread_start(self) -> bool:
        """Prepare resources the thread needs (runs on the caller's thread)."""
        return True

    def _thread_setup(self) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def _thread_loop(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _thread_teardown(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _wake(self) -> bool:  # pragma: no cover - abstract
        """Make the thread run :meth:`_run_pending_calls` soon. Returns success."""
        raise NotImplementedError

    def _wake_for_stop(self) -> None:
        self._wake()

"""The X11 global-hotkey backend.

Split out of ``platform/hotkeys.py`` (phase 4 / B2) so the largest backend owns its own module.
The facade re-exports every name here; ``create_hotkey_manager`` still decides when it is used.
The python-xlib imports stay inside functions, so importing this module on Windows or macOS is
harmless.

The constants from ``X.h`` below are duplicated so the layout-conflict logic is testable without
python-xlib.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from array import array
from collections.abc import Callable, Iterable
from typing import Any, ClassVar, NamedTuple

from .hotkeys_base import (
    _DIGITS,
    _LETTERS,
    Hotkey,
    _as_hotkey,
    _Binding,
    _key_label,
    _modifier_labels,
    _ThreadedHotkeyManager,
    format_hotkey,
)

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------- X11
# Values from X.h, duplicated so the pure logic below is testable without python-xlib.
_X_SHIFT_MASK = 1 << 0
_X_LOCK_MASK = 1 << 1
_X_CONTROL_MASK = 1 << 2
_X_MOD1_MASK = 1 << 3  # Alt
_X_MOD2_MASK = 1 << 4  # NumLock on virtually every keymap
_X_MOD4_MASK = 1 << 6  # Super
_X_KEY_PRESS = 2
_X_KEY_RELEASE = 3
_X_PROPERTY_NOTIFY = 28
_X_MAPPING_NOTIFY = 34
_X_MAPPING_MODIFIER = 0
_X_MAPPING_KEYBOARD = 1
_X_GRAB_MODE_ASYNC = 1
_X_PROPERTY_CHANGE_MASK = 1 << 22
_X_ANY_PROPERTY_TYPE = 0
# Root window property in which setxkbmap and the desktops record the XKB rules
# they applied: "rules\0model\0layout\0variant\0options" (options comma-separated).
_XKB_RULES_NAMES = "_XKB_RULES_NAMES"

#: :attr:`X11HotkeyManager.note` while another program grabs a Super key on its
#: own (see ``X11HotkeyManager._check_super_alone``).
_SUPER_TAKEN_NOTE = (
    "Another program uses the Super key on its own (Xfce opens its menu with it), so "
    "shortcuts with Super work only when Ctrl or Alt is pressed before Super."
)


class _XkbKeyOption(NamedTuple):
    """How an XKB option repurposes keys of a hotkey (see :data:`_X_KEY_OPTIONS`)."""

    #: The modifiers that trigger it when held together.
    modifiers: frozenset[str]
    #: The key pressed with them, or ``None`` when the modifiers alone trigger it.
    key: str | None
    #: What it does, completing "…, which <effect>" in the user-facing message.
    effect: str


_SWITCHES_LAYOUT = "switches the keyboard layout"
_ACTS_AS_ALTGR = "acts as AltGr"
_ACTS_AS_LEVEL5 = "acts as a level-5 key"
_WIN_AS_CTRL = "turns the Win keys into Ctrl"
_WIN_AS_ALT = "turns the Win keys into Alt"


def _xkb_option(effect: str, *modifiers: str, key: str | None = None) -> _XkbKeyOption:
    """Table entry: ``modifiers`` (+ ``key``) do ``effect`` instead of their usual job."""
    return _XkbKeyOption(frozenset(modifiers), key, effect)


#: XKB options (``setxkbmap -option``, or a desktop's keyboard settings) that take
#: modifiers of a hotkey for something else, read from xkeyboard-config's
#: ``symbols/group``, ``symbols/level3`` and ``rules/base``. A grab of such a
#: hotkey succeeds, yet pressing it does not work as intended, so it is refused
#: with the reason given here:
#:
#: * Pair toggles: with ``grp:alt_shift_toggle`` the second key pressed of Alt and
#:   Shift sends ``ISO_Next_Group`` instead of its modifier, in either order (Shift
#:   is ``PC_ALT_LEVEL2`` [Shift_L, ISO_Next_Group], Alt ``TWO_LEVEL`` [NoSymbol,
#:   ISO_Next_Group]); Ctrl+Shift, Ctrl+Alt and Ctrl+Win toggles work alike. The
#:   chord cannot be formed: the layout changes and the rest reaches other
#:   applications (Ctrl+Alt+Shift+T arrives as Ctrl+Alt+T, which opens a terminal).
#: * Single-key switches (``grp:lalt_toggle``, ``grp:lwin_switch``, the Win and Alt
#:   ``lv3`` switches, …) turn the (left) key into a layout or AltGr key, so it no
#:   longer adds its modifier at all. So do the layout *selectors*
#:   (``grp:win_menu_select``, ``grp:ctrl_select``; ``grp:win_menu_switch`` and
#:   ``grp:lctrl_rctrl_switch`` are their older names, still accepted), the
#:   level-5 Win keys (``lv5:lwin_switch_lock``) and ``altwin:ctrl_win`` /
#:   ``altwin:alt_win``, which make both Win keys Ctrl or Alt: no Super is left.
#: * Space toggles: the hotkey would fire, but every press also switches the layout.
#:
#: Right-hand-only variants (``grp:ralt_rshift_toggle``, ``grp:rctrl_rshift_toggle``,
#: ``grp:rctrl_ralt_toggle``, ``grp:toggle``, ``grp:rwin_*``, ``lv3:ralt_*``) and
#: both-keys-of-a-kind toggles (``grp:alts_toggle``, ``grp:ctrls_toggle``,
#: ``grp:shifts_toggle``) leave the left-hand keys that chords are pressed with
#: working, so they are not listed.
_X_KEY_OPTIONS: dict[str, _XkbKeyOption] = {
    # Pair toggles: the chord cannot be formed.
    "grp:alt_shift_toggle": _xkb_option(_SWITCHES_LAYOUT, "alt", "shift"),
    "grp:alt_shift_toggle_bidir": _xkb_option(_SWITCHES_LAYOUT, "alt", "shift"),
    "grp:lalt_lshift_toggle": _xkb_option(_SWITCHES_LAYOUT, "alt", "shift"),
    "grp:ctrl_shift_toggle": _xkb_option(_SWITCHES_LAYOUT, "ctrl", "shift"),
    "grp:ctrl_shift_toggle_bidir": _xkb_option(_SWITCHES_LAYOUT, "ctrl", "shift"),
    "grp:lctrl_lshift_toggle": _xkb_option(_SWITCHES_LAYOUT, "ctrl", "shift"),
    "grp:ctrl_alt_toggle": _xkb_option(_SWITCHES_LAYOUT, "ctrl", "alt"),
    "grp:ctrl_alt_toggle_bidir": _xkb_option(_SWITCHES_LAYOUT, "ctrl", "alt"),
    "grp:lctrl_lalt_toggle": _xkb_option(_SWITCHES_LAYOUT, "ctrl", "alt"),
    "grp:lctrl_lwin_toggle": _xkb_option(_SWITCHES_LAYOUT, "ctrl", "meta"),
    "grp:lctrl_lwin_rctrl_menu": _xkb_option(_SWITCHES_LAYOUT, "ctrl", "meta"),
    # Single (left) keys that stop being modifiers.
    "grp:lalt_toggle": _xkb_option(_SWITCHES_LAYOUT, "alt"),
    "grp:lswitch": _xkb_option(_SWITCHES_LAYOUT, "alt"),
    "grp:lctrl_toggle": _xkb_option(_SWITCHES_LAYOUT, "ctrl"),
    "grp:lshift_toggle": _xkb_option(_SWITCHES_LAYOUT, "shift"),
    "grp:lwin_toggle": _xkb_option(_SWITCHES_LAYOUT, "meta"),
    "grp:lwin_switch": _xkb_option(_SWITCHES_LAYOUT, "meta"),
    "grp:win_switch": _xkb_option(_SWITCHES_LAYOUT, "meta"),
    "grp:win_menu_select": _xkb_option(_SWITCHES_LAYOUT, "meta"),
    "grp:win_menu_switch": _xkb_option(_SWITCHES_LAYOUT, "meta"),
    "grp:ctrl_select": _xkb_option(_SWITCHES_LAYOUT, "ctrl"),
    "grp:lctrl_rctrl_switch": _xkb_option(_SWITCHES_LAYOUT, "ctrl"),
    "lv3:lalt_switch": _xkb_option(_ACTS_AS_ALTGR, "alt"),
    "lv3:alt_switch": _xkb_option(_ACTS_AS_ALTGR, "alt"),
    "lv3:lwin_switch": _xkb_option(_ACTS_AS_ALTGR, "meta"),
    "lv3:win_switch": _xkb_option(_ACTS_AS_ALTGR, "meta"),
    "lv5:lwin_switch_lock": _xkb_option(_ACTS_AS_LEVEL5, "meta"),
    "lv5:lwin_switch_lock_cancel": _xkb_option(_ACTS_AS_LEVEL5, "meta"),
    "altwin:ctrl_win": _xkb_option(_WIN_AS_CTRL, "meta"),
    "altwin:alt_win": _xkb_option(_WIN_AS_ALT, "meta"),
    # Space toggles: every press of the hotkey would also switch the layout.
    "grp:alt_space_toggle": _xkb_option(_SWITCHES_LAYOUT, "alt", key="space"),
    "grp:ctrl_space_toggle": _xkb_option(_SWITCHES_LAYOUT, "ctrl", key="space"),
    "grp:win_space_toggle": _xkb_option(_SWITCHES_LAYOUT, "meta", key="space"),
}

_X_MODIFIERS = {
    "ctrl": _X_CONTROL_MASK,
    "alt": _X_MOD1_MASK,
    "shift": _X_SHIFT_MASK,
    "meta": _X_MOD4_MASK,
}
_X_RELEVANT_MASK = _X_SHIFT_MASK | _X_CONTROL_MASK | _X_MOD1_MASK | _X_MOD4_MASK

_X_KEYSYM_NAMES: dict[str, str] = {
    **{ch: ch for ch in _LETTERS + _DIGITS},
    **{f"f{n}": f"F{n}" for n in range(1, 25)},
    "space": "space",
    "enter": "Return",
    "tab": "Tab",
    "escape": "Escape",
    "backspace": "BackSpace",
    "insert": "Insert",
    "delete": "Delete",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "left": "Left",
    "right": "Right",
    "up": "Up",
    "down": "Down",
    "minus": "minus",
    "equal": "equal",
    "bracketleft": "bracketleft",
    "bracketright": "bracketright",
    "backslash": "backslash",
    "semicolon": "semicolon",
    "quote": "apostrophe",
    "grave": "grave",
    "comma": "comma",
    "period": "period",
    "slash": "slash",
}


def _x11_modifiers(modifiers: Iterable[str]) -> int:
    mask = 0
    for m in modifiers:
        mask |= _X_MODIFIERS[m]
    return mask


def _x11_lock_variants(numlock_mask: int) -> tuple[int, ...]:
    """Modifier masks to grab in addition to the real ones.

    A passive grab matches the modifier state exactly, so without these variants a
    hotkey would silently stop working whenever Caps Lock or Num Lock is on.
    """
    variants: list[int] = []
    for mask in (0, _X_LOCK_MASK, numlock_mask, _X_LOCK_MASK | numlock_mask):
        if mask not in variants:
            variants.append(mask)
    return tuple(variants)


def _x11_parse_xkb_options(value: Any) -> tuple[str, ...]:
    """The XKB options recorded in an ``_XKB_RULES_NAMES`` property value.

    The value is five NUL-separated fields (rules, model, layout, variant,
    options); python-xlib returns it as ``bytes`` (``str`` or an ``array`` from
    older versions). Anything malformed yields no options.
    """
    if isinstance(value, str):
        raw = value
    elif isinstance(value, (bytes, bytearray, memoryview, array)):
        raw = bytes(value).decode("latin-1")  # ICCCM STRING; option names are ASCII
    else:
        return ()
    fields = raw.split("\0")
    if len(fields) < 5:
        return ()
    return tuple(option.strip() for option in fields[4].split(",") if option.strip())


def _x11_read_xkb_options(root: Any, atom: int) -> tuple[str, ...]:
    """The XKB options of the X server (read-only ``GetProperty`` on the root window)."""
    prop = root.get_full_property(atom, _X_ANY_PROPERTY_TYPE)
    return _x11_parse_xkb_options(getattr(prop, "value", None))


def _x11_layout_switch_conflict(hotkey: Hotkey, options: Iterable[str]) -> str | None:
    """Why keys of ``hotkey`` are taken by one of the XKB ``options``, or ``None``.

    See :data:`_X_KEY_OPTIONS`; the first matching option is named, for example
    ``"Ctrl+Alt+Shift+T includes Alt+Shift, which switches the keyboard layout (XKB
    option grp:alt_shift_toggle)"``.
    """
    for option in options:
        taken = _X_KEY_OPTIONS.get(option)
        if taken is None:
            continue
        if not taken.modifiers <= hotkey.modifiers or taken.key not in (None, hotkey.key):
            continue
        label = format_hotkey(hotkey, macos=False)
        chord = "+".join(
            [*_modifier_labels(taken.modifiers), *([_key_label(taken.key)] if taken.key else [])]
        )
        # "Alt+Space switches …" rather than "Alt+Space includes Alt+Space, which …".
        subject = label if chord == label else f"{label} includes {chord}, which"
        return f"{subject} {taken.effect} (XKB option {option})"
    return None


def _x11_string_to_keysym(name: str) -> int:
    from Xlib import XK

    return int(XK.string_to_keysym(name))


def _x11_open_display(display_name: str | None) -> Any:
    from Xlib import display as xdisplay

    return xdisplay.Display(display_name)


class _XErrorCatcher:
    """``onerror`` handler for python-xlib requests (same protocol as ``Xlib.error.CatchError``).

    Returning a true value tells python-xlib the error was handled, so a failed grab
    (``BadAccess``: another client owns the combination) is recorded here instead of
    being reported asynchronously on stderr.
    """

    def __init__(self) -> None:
        self.error: Any = None

    def __call__(self, error: Any, request: Any) -> int:
        if self.error is None:
            self.error = error
        return 1


class X11HotkeyManager(_ThreadedHotkeyManager):
    """Passive key grabs on the X11 root window, served by a python-xlib thread.

    The thread owns its own ``Display`` connection. Its loop blocks in ``select`` on
    that connection and a wake-up pipe, without a timeout: every cross-thread call
    and :meth:`stop` write to the pipe after queuing their work, and events that
    python-xlib already buffered are drained before waiting. The thread therefore
    never wakes up while no key is pressed, and :meth:`stop` still returns promptly.

    After a keyboard mapping change every binding is grabbed again at its new
    keycode; one whose key vanished (``setxkbmap ru``) stays known, is reported
    as inactive and is grabbed again by the next mapping change that restores it.
    A hotkey refused when it was registered, because its key was missing or a
    layout switch took its keys, is kept and retried the same way.

    A Super key that another client grabs on its own (Xubuntu opens its menu
    with it) takes the whole keyboard while it is held, so a chord pressed
    Super first never reaches these grabs. That cannot be fixed from here; it
    is detected and explained in :attr:`note`.

    A hotkey that contains a configured keyboard-layout switch (Alt+Shift with
    ``grp:alt_shift_toggle``, see :data:`_X_KEY_OPTIONS`) is refused, because
    ``XGrabKey`` would accept it although it can never be pressed. The options are
    read from the root window's ``_XKB_RULES_NAMES`` and followed through
    ``PropertyNotify`` (root properties change on focus changes at most, which the
    loop ignores after comparing one atom) and mapping changes.
    """

    name: ClassVar[str] = "x11"
    _SELECT_TIMEOUT_S: ClassVar[float | None] = None  # block until an event or a wake-up

    def __init__(
        self,
        display_name: str | None = None,
        *,
        note: str | None = None,
        display_factory: Callable[[str | None], Any] | None = None,
    ) -> None:
        super().__init__(note)
        # The note this manager was created with; the Super-key remark is added to it.
        self._base_note = note
        self._display_name = display_name
        self._display_factory = display_factory or _x11_open_display
        self._display: Any = None
        self._root: Any = None
        self._numlock_mask = _X_MOD2_MASK
        self._pipe_lock = threading.Lock()
        self._wake_r = -1
        self._wake_w = -1
        # XKB options of the server, written by the hotkey thread only (None while it
        # is not running); an immutable tuple, so other threads may read it as is.
        self._xkb_options: tuple[str, ...] | None = None
        # Only touched on the hotkey thread:
        self._rules_atom = 0  # the _XKB_RULES_NAMES atom (0: options not followed)
        self._super_taken = False  # another client grabs Super alone (_check_super_alone)
        self._grabs: dict[int, tuple[int, int]] = {}  # binding id → (keycode, modifier mask)
        self._lookup: dict[tuple[int, int], int] = {}  # (keycode, modifier mask) → binding id
        self._held: set[int] = set()
        self._last_release: dict[int, int] = {}

    # ------------------------------------------------------------- lifecycle
    def _before_thread_start(self) -> bool:
        with self._pipe_lock:
            if self._wake_r < 0:
                try:
                    read_fd, write_fd = os.pipe()
                except OSError as exc:
                    log.warning("Cannot create the hotkey wake-up pipe: %s", exc)
                    return False
                try:
                    os.set_blocking(read_fd, False)
                    os.set_blocking(write_fd, False)
                except (OSError, AttributeError) as exc:  # not selectable here (not POSIX)
                    os.close(read_fd)
                    os.close(write_fd)
                    log.warning("Cannot configure the hotkey wake-up pipe: %s", exc)
                    return False
                self._wake_r, self._wake_w = read_fd, write_fd
        return True

    def _thread_setup(self) -> bool:
        try:
            display = self._display_factory(self._display_name)
        except Exception as exc:
            log.warning("Cannot connect to the X server for hotkeys: %s", exc)
            self.note = f"Could not connect to the X server: {exc}"
            return False
        display.set_error_handler(self._on_x_error)
        self._display = display
        self._root = display.screen().root
        self._numlock_mask = self._find_numlock_mask()
        self._follow_xkb_options()
        self._check_super_alone()
        return True

    def _thread_loop(self) -> None:
        import select

        display = self._display
        while not self._stopping.is_set():
            self._run_pending_calls()
            # python-xlib may already have buffered events (e.g. read during a sync),
            # which select() would not report, so drain before waiting.
            while display.pending_events():
                self._handle_event(display.next_event())
            try:
                readable, _, _ = select.select(
                    [display.fileno(), self._wake_r], [], [], self._SELECT_TIMEOUT_S
                )
            except (OSError, ValueError) as exc:
                log.warning("Hotkey thread stopping: %s", exc)
                break
            if self._wake_r in readable:
                self._drain_wake_pipe()

    def _thread_teardown(self) -> None:
        display = self._display
        if display is not None:
            try:
                for binding_id in list(self._grabs):
                    self._ungrab(binding_id)
                display.sync()
            except Exception:
                log.debug("Ungrabbing hotkeys failed", exc_info=True)
            try:
                display.close()
            except Exception:
                log.debug("Closing the X display failed", exc_info=True)
        self._display = None
        self._root = None
        self._rules_atom = 0
        self._xkb_options = None
        if self._super_taken:  # checked again when the thread starts again
            self._super_taken = False
            self.note = self._base_note
        self._grabs.clear()
        self._lookup.clear()
        self._held.clear()
        self._last_release.clear()
        with self._pipe_lock:
            for fd in (self._wake_r, self._wake_w):
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            self._wake_r = self._wake_w = -1

    def _wake(self) -> bool:
        with self._pipe_lock:
            if self._wake_w < 0:
                return False
            try:
                os.write(self._wake_w, b"\0")
            except BlockingIOError:
                return True  # pipe full: the thread is due to wake up anyway
            except OSError:
                return False
            return True

    def _drain_wake_pipe(self) -> None:
        # Non-blocking: read until empty (BlockingIOError is an OSError).
        with contextlib.suppress(OSError):
            while os.read(self._wake_r, 4096):
                pass

    # --------------------------------------------------------- registrations
    def _native_register(self, binding: _Binding) -> bool:
        return bool(self._call_on_thread(lambda: self._grab_binding(binding), False))

    def _native_unregister(self, binding: _Binding) -> None:
        self._call_on_thread(lambda: self._ungrab_binding(binding.id), None)

    def layout_conflict(self, hotkey: Hotkey | str) -> str | None:
        hotkey = _as_hotkey(hotkey)
        try:
            return _x11_layout_switch_conflict(hotkey, self._current_xkb_options())
        except Exception:  # advisory only: never break a settings dialog
            log.debug("Keyboard layout check for %s failed", hotkey, exc_info=True)
            return None

    def _current_xkb_options(self) -> tuple[str, ...]:
        """The server's XKB options, from any thread.

        The hotkey thread keeps them up to date while it runs; otherwise (``doctor``,
        a settings dialog before the first registration) a short-lived connection
        reads them.
        """
        options = self._xkb_options
        if options is not None:
            return options
        display = self._display_factory(self._display_name)
        try:
            atom = int(display.intern_atom(_XKB_RULES_NAMES, only_if_exists=True))
            return _x11_read_xkb_options(display.screen().root, atom) if atom else ()
        finally:
            display.close()

    def _follow_xkb_options(self) -> None:
        """Read the XKB options and ask for ``PropertyNotify`` when they change.

        Hotkey thread only. Desktops may apply the user's layout switch after this
        app started (at login), and ``setxkbmap`` writes the property only after
        the keymap change that sends ``MappingNotify``, so the property itself is
        watched. Failing that, options are simply not checked.
        """
        try:
            self._rules_atom = int(self._display.intern_atom(_XKB_RULES_NAMES))
            self._root.change_attributes(event_mask=_X_PROPERTY_CHANGE_MASK)
        except Exception:
            log.debug("Cannot follow the XKB layout options", exc_info=True)
            self._rules_atom = 0
        self._refresh_xkb_options()

    def _refresh_xkb_options(self) -> bool:
        """Re-read the XKB options (hotkey thread only); returns whether they changed."""
        old = self._xkb_options
        new: tuple[str, ...] = old or ()
        if self._rules_atom:
            try:
                new = _x11_read_xkb_options(self._root, self._rules_atom)
            except Exception:  # keep what was known: never flip-flop on a failed read
                log.debug("Reading the XKB layout options failed", exc_info=True)
        self._xkb_options = new
        if new != old:
            log.debug("XKB options: %s", ",".join(new) or "(none)")
        return new != old

    def _keycode_for(self, key: str) -> int:
        keysym = _x11_string_to_keysym(_X_KEYSYM_NAMES[key])
        if not keysym:
            return 0
        return int(self._display.keysym_to_keycode(keysym))

    def _find_numlock_mask(self) -> int:
        try:
            keycode = int(self._display.keysym_to_keycode(_x11_string_to_keysym("Num_Lock")))
            if keycode:
                for index, keycodes in enumerate(self._display.get_modifier_mapping()):
                    if keycode in list(keycodes):
                        return 1 << index
        except Exception:
            log.debug("Could not determine the NumLock modifier", exc_info=True)
        return _X_MOD2_MASK

    def _grab_binding(self, binding: _Binding) -> bool:
        label = format_hotkey(binding.hotkey, macos=False)
        # XGrabKey accepts a chord that the layout switch makes impossible to press;
        # without this check the hotkey would silently never fire.
        switch = _x11_layout_switch_conflict(binding.hotkey, self._xkb_options or ())
        if switch is not None:
            # Grabbed as soon as the user moves the switch elsewhere (_regrab_all).
            binding.retry_on_layout_change = True
            return self._reject(binding, switch)
        keycode = self._keycode_for(binding.hotkey.key)
        if not keycode:
            binding.retry_on_layout_change = True
            return self._reject(
                binding, f"the key of {label} is not on the current keyboard layout"
            )
        mods = _x11_modifiers(binding.hotkey.modifiers)
        if not self._grab(keycode, mods):
            return self._reject(binding, f"{label} is already in use by another application")
        self._grabs[binding.id] = (keycode, mods)
        self._lookup[(keycode, mods)] = binding.id
        return True

    def _ungrab_binding(self, binding_id: int) -> None:
        self._ungrab(binding_id)
        self._display.flush()

    def _grab(self, keycode: int, mods: int) -> bool:
        catcher = _XErrorCatcher()
        for variant in _x11_lock_variants(self._numlock_mask):
            self._root.grab_key(
                keycode,
                mods | variant,
                True,
                _X_GRAB_MODE_ASYNC,
                _X_GRAB_MODE_ASYNC,
                onerror=catcher,
            )
        # Grab errors arrive asynchronously; a round trip guarantees they were seen.
        self._display.sync()
        if catcher.error is None:
            return True
        # Release whichever variants did succeed so nothing half-registered lingers.
        quiet = _XErrorCatcher()
        for variant in _x11_lock_variants(self._numlock_mask):
            self._root.ungrab_key(keycode, mods | variant, onerror=quiet)
        self._display.sync()
        return False

    def _check_super_alone(self) -> None:
        """Note whether another client grabs a Super key on its own (hotkey thread only).

        Xubuntu binds Super_L and Super_R without modifiers to its menu, and
        xfsettingsd grabs them asynchronously and acts on the release. X
        activates a passive grab only while the keyboard is not grabbed, so with
        Super pressed first the whole keyboard belongs to that client until
        Super is released, and Ctrl+Alt+Super+T never reaches this manager's
        grab (Ctrl or Alt first works). Such a grab is found by asking for the
        same one, which X refuses with BadAccess; a grab that is granted is
        released at once. The result is explained in :attr:`note`.
        """
        taken = False
        try:
            for name in ("Super_L", "Super_R"):
                keysym = _x11_string_to_keysym(name)
                keycode = int(self._display.keysym_to_keycode(keysym)) if keysym else 0
                if keycode and not self._grab(keycode, 0):
                    taken = True
                    break
                if keycode:
                    quiet = _XErrorCatcher()
                    for variant in _x11_lock_variants(self._numlock_mask):
                        self._root.ungrab_key(keycode, variant, onerror=quiet)
                    self._display.sync()
        except Exception:
            log.debug("Checking the Super key grabs failed", exc_info=True)
            return
        if taken != self._super_taken:
            self._super_taken = taken
            if taken:
                log.warning("Another program grabs the Super key on its own: %s", _SUPER_TAKEN_NOTE)
        parts = [part for part in (self._base_note, _SUPER_TAKEN_NOTE if taken else None) if part]
        self.note = " ".join(parts) or None

    def _ungrab(self, binding_id: int) -> None:
        grab = self._grabs.pop(binding_id, None)
        self._held.discard(binding_id)
        self._last_release.pop(binding_id, None)
        if grab is None:
            return
        self._lookup.pop(grab, None)
        keycode, mods = grab
        quiet = _XErrorCatcher()
        for variant in _x11_lock_variants(self._numlock_mask):
            self._root.ungrab_key(keycode, mods | variant, onerror=quiet)

    def _regrab_all(self) -> None:
        """Re-establish grabs after a keyboard layout or modifier mapping change.

        The wanted bindings come from the live table, not from ``_grabs``: a binding
        whose grab failed on an earlier change (its key was missing) must be retried
        now that the key may be back. A registration in flight on this thread has
        already grabbed but is not yet in ``_bindings``, so grabbed ``_by_id``
        entries are included too. Bindings being removed have already left both
        tables, so they are not grabbed again.
        """
        with self._table_lock:
            wanted = {b.id: b for b in self._bindings.values()}
            wanted.update({i: b for i, b in self._by_id.items() if i in self._grabs})
        for binding_id in list(self._grabs):
            self._ungrab(binding_id)
        self._numlock_mask = self._find_numlock_mask()
        self._check_super_alone()  # the Super keys may have new keycodes
        for binding in wanted.values():
            ok = self._grab_binding(binding)  # logs why it failed
            if not ok:
                log.info("Hotkey %r is retried on the next keyboard mapping change", binding.name)
            self._set_active(binding, ok)

    # ------------------------------------------------------------------ events
    def _handle_event(self, event: Any) -> None:
        kind = event.type
        if kind == _X_MAPPING_NOTIFY:
            if event.request in (_X_MAPPING_KEYBOARD, _X_MAPPING_MODIFIER):
                self._display.refresh_keyboard_mapping(event)
                self._refresh_xkb_options()
                self._regrab_all()
            return
        if kind == _X_PROPERTY_NOTIFY:
            # Any root property change arrives here (focus changes, window lists);
            # only a change of the XKB options matters.
            if (
                self._rules_atom
                and int(getattr(event, "atom", 0)) == self._rules_atom
                and self._refresh_xkb_options()
            ):
                self._regrab_all()
            return
        if kind == _X_KEY_RELEASE:
            # A release carries the modifiers held just before it, and users often
            # lift Ctrl/Alt before the key: match releases by keycode alone, or the
            # binding would stay "held" and swallow every later press.
            keycode = int(event.detail)
            for grabbed_id, (grab_code, _mods) in self._grabs.items():
                if grab_code == keycode:
                    self._held.discard(grabbed_id)
                    self._last_release[grabbed_id] = int(event.time)
            return
        if kind != _X_KEY_PRESS:
            return
        binding_id = self._lookup.get((int(event.detail), int(event.state) & _X_RELEVANT_MASK))
        if binding_id is None:
            return
        # X auto-repeat sends Release+Press pairs with identical timestamps while a
        # key is held; fire once per physical press (the MOD_NOREPEAT equivalent).
        last_release = self._last_release.get(binding_id)
        if binding_id in self._held:
            return
        if last_release is not None and (int(event.time) - last_release) & 0xFFFFFFFF <= 1:
            return
        self._held.add(binding_id)
        self._dispatch(binding_id)

    @staticmethod
    def _on_x_error(error: Any, request: Any = None) -> None:
        log.debug("X error in hotkey connection: %s", error)

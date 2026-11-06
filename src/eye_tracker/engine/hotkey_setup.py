"""Register the configured global hotkeys with the platform manager.

Extracted from the Controller (phase 4 / B1). The function takes everything it needs as
arguments and returns the announced-failure set, so the registration rules can be tested
without a Controller, a QApplication or a real platform. The Controller keeps the callback
(``_on_hotkey``) and the user-facing confirmation; this module only owns "configured ->
registered or explained".

The failure set is what keeps a combination that keeps failing from producing a notification on
every settings apply: a pair is announced once, and forgotten as soon as it registers.
"""

from __future__ import annotations

import contextlib
import functools
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Settings
    from ..platform.hotkeys import HotkeyManager

log = logging.getLogger(__name__)

#: Hotkey setting names (``Settings.hotkeys.<name>``) in registration order.
HOTKEY_ACTIONS = ("toggle_tracking", "toggle_privacy", "recalibrate")


def register_hotkeys(
    manager: HotkeyManager,
    settings: Settings,
    announced: set[tuple[str, str]],
    *,
    suspended: bool,
    on_hotkey: Callable[[str], None],
    notify: Callable[[str, str], None],
) -> set[tuple[str, str]]:
    """(Re)register the configured hotkeys; returns the updated announced-failure set.

    ``suspended`` is the controller's "a mode change is in progress" latch: nothing is
    touched then. A hotkey that could not be registered is announced once per
    ``(action, combination)``; the next call with the same failure is silent.
    """
    if suspended:
        return announced
    hotkeys = settings.hotkeys
    try:
        manager.unregister_all()
    except Exception:
        log.debug("Releasing hotkeys failed", exc_info=True)
    if not hotkeys.enabled:
        # Turning them on again announces every failure afresh.
        with contextlib.suppress(Exception):
            manager.stop()
        return set()
    if not manager.supported:
        if manager.note:
            log.info("Global hotkeys unavailable: %s", manager.note)
        return announced
    failed: list[str] = []
    failing: set[tuple[str, str]] = set()
    for name in HOTKEY_ACTIONS:
        combo = str(getattr(hotkeys, name, "") or "").strip()
        if not combo:
            continue
        try:
            ok = manager.register(name, combo, functools.partial(on_hotkey, name))
        except Exception:
            log.warning("Registering hotkey %s failed", combo, exc_info=True)
            ok = False
        if ok:
            continue
        key = (name, combo.lower())
        failing.add(key)
        if key in announced:
            log.debug("Hotkey %s for %s is still unavailable (already announced)", combo, name)
            continue
        reason = hotkey_error(manager, name)
        failed.append(
            reason or f"{combo} could not be registered (invalid, or used by another app)"
        )
    if failed:
        notify("Hotkey unavailable", "; ".join(failed) + ". Choose another in Settings → Hotkeys.")
    return failing


def hotkey_error(manager: HotkeyManager, name: str) -> str | None:
    """The manager's explanation of why ``name`` is not registered, if it has one."""
    try:
        reason = manager.last_error(name)
    except Exception:
        log.debug("Reading the hotkey error failed", exc_info=True)
        return None
    if not reason:
        return None
    return str(reason).strip().rstrip(".") or None

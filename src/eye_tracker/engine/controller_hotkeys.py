"""Global-hotkey half of :mod:`eye_tracker.engine.controller` (TD-CPLX-001).

The hotkey manager, the pause/privacy mode toggles those shortcuts drive, the shortcut
validation and the permission notice live here as a mixin, so ``controller.py`` keeps the
pipeline. The mixin only calls back into the host controller; it must never import
``controller`` at runtime (TYPE_CHECKING only).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .controller_policy import may_interrupt
from .controller_support import _save_privacy_mode
from .hotkey_setup import HOTKEY_ACTIONS, hotkey_error, register_hotkeys

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..config import Settings
    from ..platform.hotkeys import HotkeyManager
    from ..types import Monitor, TrackingState

    HotkeyManagerFactory = Callable[[], HotkeyManager]

log = logging.getLogger("eye_tracker.engine.controller")


class _HotkeyHost:
    """State and callbacks the mixin expects from the host ``Controller``."""

    _settings: Settings
    _state: TrackingState
    _monitors: list[Monitor]
    _hotkeys: HotkeyManager | None
    _hotkey_factory: HotkeyManagerFactory | None
    _hotkeys_suspended: bool
    _hotkey_pressed: Any
    _announced_hotkey_failures: set[tuple[str, str]]
    _paused: bool
    _privacy: bool
    _privacy_before_calibration: bool
    _privacy_restored: bool
    _calibrating: bool
    _displays_off_only: bool
    _accessibility_notice_shown: bool
    _closed: bool
    calibration_required: Any
    permission_needed: Any

    if TYPE_CHECKING:

        def _calibration_useful(self) -> bool: ...
        def _notify(self, title: str, message: str, *, force: bool = False) -> None: ...
        def _on_main_thread(self) -> bool: ...
        def _platform_call(self, name: str, *args: Any, default: Any = None) -> Any: ...
        def _update_state(self) -> None: ...


class ControllerHotkeysMixin(_HotkeyHost):
    """Global hotkeys and the mode changes they trigger (mixed into ``Controller``)."""

    @property
    def hotkey_manager(self) -> HotkeyManager | None:
        """The global hotkey manager (``None`` before :meth:`start`)."""
        return self._hotkeys

    def suspend_hotkeys(self, suspended: bool) -> None:
        """Release (``True``) or restore (``False``) the global hotkeys.

        The settings dialog suspends them while it records a new shortcut: the OS
        delivers a registered combination to us instead of the focused window, so
        without this the user could not re-record a shortcut that is in use.
        """
        suspended = bool(suspended)
        if suspended == self._hotkeys_suspended:
            return
        self._hotkeys_suspended = suspended
        if self._hotkeys is None or self._closed:
            return
        if suspended:
            try:
                self._hotkeys.unregister_all()
            except Exception:
                log.debug("Releasing hotkeys failed", exc_info=True)
        else:
            self._register_hotkeys()

    def pause(self) -> None:
        """Pause tracking; the camera is released."""
        if not self._paused:
            self._paused = True
            log.info("Tracking paused")
            self._update_state()

    def resume(self) -> None:
        """Resume after :meth:`pause` (privacy mode, if on, stays on)."""
        if self._paused:
            self._paused = False
            log.info("Tracking resumed")
            self._update_state()

    def toggle_pause(self) -> None:
        if self._paused:
            self.resume()
        else:
            self.pause()

    def set_privacy(self, enabled: bool) -> None:
        """Privacy mode: the camera is fully released (its light goes off).

        The choice is remembered for the next start (``privacy.remember_privacy_mode``).
        """
        enabled = bool(enabled)
        # An explicit choice made during a calibration is what applies afterwards.
        self._privacy_before_calibration = False
        if enabled == self._privacy:
            return
        self._privacy = enabled
        self._privacy_restored = False
        log.info("Privacy mode %s", "on" if enabled else "off")
        _save_privacy_mode(enabled)
        self._update_state()

    def toggle_privacy(self) -> None:
        self.set_privacy(not self._privacy)

    def _hotkey_status(self) -> dict[str, Any]:
        """The global hotkeys as the OS took them: ``{"registered": [names],
        "errors": {name: why it is not registered}}``.

        Only this process knows: ``eye-tracker doctor`` reads it through
        ``status`` (it cannot register anything itself without taking the
        combinations away from the running app). Empty while hotkeys are off or
        unsupported here, where nothing is registered on purpose.
        """
        manager = self._hotkeys
        hk = self._settings.hotkeys
        if manager is None or not hk.enabled or not manager.supported:
            return {"registered": [], "errors": {}}
        configured = [name for name in HOTKEY_ACTIONS if str(getattr(hk, name, "") or "").strip()]
        if self._hotkeys_suspended:
            # Released on purpose; they come back when the settings dialog is done.
            released = "released while a shortcut is being recorded in Settings → Hotkeys"
            return {"registered": [], "errors": dict.fromkeys(configured, released)}
        try:
            registered = sorted(str(name) for name in manager.registered)
        except Exception:
            log.debug("Reading the registered hotkeys failed", exc_info=True)
            registered = []
        errors: dict[str, str] = {}
        for name in configured:
            if name in registered:
                continue
            reason = hotkey_error(manager, name)
            if reason:
                errors[name] = reason
        status: dict[str, Any] = {"registered": registered, "errors": errors}
        note = getattr(manager, "note", None)
        if isinstance(note, str) and note.strip():
            # A limitation the running manager found (another program grabbing
            # the Super key on its own): doctor shows it.
            status["note"] = note.strip()
        return status

    def _setup_hotkeys(self) -> None:
        if self._hotkeys is None:
            factory = self._hotkey_factory
            if factory is None:
                from ..platform.hotkeys import create_hotkey_manager

                factory = create_hotkey_manager
            try:
                self._hotkeys = factory()
            except Exception:
                log.warning("Global hotkeys unavailable", exc_info=True)
                return
        self._register_hotkeys()

    def _register_hotkeys(self) -> None:
        manager = self._hotkeys
        if manager is None:
            return
        self._announced_hotkey_failures = register_hotkeys(
            manager,
            self._settings,
            self._announced_hotkey_failures,
            suspended=self._hotkeys_suspended,
            on_hotkey=self._on_hotkey,
            notify=self._notify,
        )

    def _on_hotkey(self, name: str) -> None:  # hotkey thread (or the main thread on macOS)
        if self._closed:
            return
        if self._on_main_thread():
            self._handle_hotkey(name)
        else:
            self._hotkey_pressed.emit(name)

    def _handle_hotkey(self, name: str) -> None:
        if self._closed:
            return
        if self._hotkeys_suspended:
            log.debug("Hotkey %s ignored while hotkeys are suspended", name)
            return
        log.info("Hotkey: %s", name)
        before = (self._paused, self._privacy)
        if name == "toggle_tracking":
            self.toggle_pause()
            self._confirm_mode_change(before)
        elif name == "toggle_privacy":
            self.toggle_privacy()
            self._confirm_mode_change(before)
        elif name == "recalibrate":
            self.calibration_required.emit("hotkey")

    def _confirm_mode_change(self, before: tuple[bool, bool]) -> None:
        """Confirm a pause or privacy change made by a hotkey or ``ctl`` command.

        Both are toggles, and the tray icon often sits in a hidden overflow area,
        so without a word the user cannot tell which way a press went, and
        pausing also stops walk-away detection. ``before`` is ``(paused,
        privacy)`` before the change. The tray menu needs no confirmation: its
        check marks show the result.
        """
        paused, privacy = self._paused, self._privacy
        was_paused, was_private = before
        stopped = (
            "Switching monitors and walk-away detection pause"
            if self._settings.presence.enabled
            else "Switching monitors pauses"
        )
        if self._state.camera_active:
            back_on = "The camera is on again."
        else:  # e.g. the session is locked, or another app has the camera
            back_on = f"The camera stays off for now ({self._state.label})."
        if privacy != was_private:
            if privacy:
                title = "Privacy mode on"
                message = (
                    f"The camera is off. {stopped} until you turn privacy mode "
                    f"off{self._hotkey_hint('toggle_privacy')}."
                )
            else:
                title = "Privacy mode off"
                message = "Tracking is still paused: the camera stays off." if paused else back_on
        elif paused != was_paused:
            if paused:
                title = "Tracking paused"
                message = (
                    f"The camera is released. {stopped} until you "
                    f"resume{self._hotkey_hint('toggle_tracking')}."
                )
            else:
                title = "Tracking resumed"
                message = "Privacy mode is still on: the camera stays off." if privacy else back_on
        else:
            return
        self._notify(title, message)

    def _hotkey_hint(self, name: str) -> str:
        """``" (Ctrl+Alt+Win+T)"`` for a registered hotkey ``name``, else ``""``."""
        from ..platform.hotkeys import Hotkey, format_hotkey

        manager = self._hotkeys
        try:
            hotkey = manager.registered.get(name) if manager is not None else None
        except Exception:
            log.debug("Reading the registered hotkeys failed", exc_info=True)
            return ""
        return f" ({format_hotkey(hotkey)})" if isinstance(hotkey, Hotkey) else ""

    def _may_interrupt(self) -> bool:
        return may_interrupt(
            calibration_useful=self._calibration_useful(),
            calibrating=self._calibrating,
            displays_off_only=self._displays_off_only,
            state=self._state,
        )

    def _check_accessibility(self) -> None:
        """Tell the user (once) when focus cannot follow the gaze for lack of the
        macOS Accessibility permission, or because it belongs to an older build."""
        sw = self._settings.switching
        if (
            self._accessibility_notice_shown
            or not (sw.enabled and sw.focus_window)
            or len(self._monitors) < 2
        ):
            return
        status = self._platform_call("accessibility_status")
        if status not in ("missing", "stale"):
            return
        self._accessibility_notice_shown = True
        if status == "stale":
            message = (
                "Keyboard focus cannot follow your gaze: the Accessibility permission "
                "belongs to a previous version of Eye Tracker. In System Settings › Privacy "
                "& Security › Accessibility, remove Eye Tracker with “−” and add it again."
            )
        else:
            message = (
                "Keyboard focus cannot follow your gaze until Eye Tracker may control "
                "your computer: System Settings › Privacy & Security › Accessibility."
            )
        self._notify("Accessibility access needed", message, force=True)
        self.permission_needed.emit("accessibility")

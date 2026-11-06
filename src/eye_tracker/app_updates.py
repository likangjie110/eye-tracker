"""Update-service half of the tray application (phase 4 / method split).

Extracted from :class:`~eye_tracker.app.EyeTrackerApp`: creating the update
service and turning its state and announcement signals into tray state and the
one-off "update available" notification. The mixin only declares the state it
uses and calls back into the host app class; it must never import
:mod:`eye_tracker.app` at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QObject

from . import APP_NAME
from .app_startup import PROMPT_UPDATE

if TYPE_CHECKING:
    from .config import Settings
    from .ui.tray import TrayIcon
    from .update.service import UpdateService


class AppUpdatesMixin:
    """Update-service wiring for :class:`~eye_tracker.app.EyeTrackerApp`."""

    # State owned by ``EyeTrackerApp.__init__``; declared here so the methods
    # below type-check without importing the app module (there is no runtime cycle).
    _updates: UpdateService | None
    _tray: TrayIcon | None
    _settings: Settings
    _prompt: tuple[str, float] | None
    _clock: Callable[[], float]

    def _start_updates(self, tray: TrayIcon) -> None:
        """Create the update service (it looks for updates only if the user turned
        that on, or asks) and connect it to the tray."""
        from .update.service import UpdateService

        updates = UpdateService(cast(QObject, self))
        self._updates = updates
        updates.state_changed.connect(self._on_update_state)
        updates.announce.connect(self._on_update_announced)
        updates.start(self._settings.updates.check)

    def _on_update_state(self, state: object) -> None:
        from .update.service import Phase, UpdateState

        tray = self._tray
        if tray is None or not isinstance(state, UpdateState):
            return
        release = state.release
        if state.phase is Phase.UP_TO_DATE:
            tray.set_update_available(None)
        elif release is not None and state.phase in (
            Phase.AVAILABLE,
            Phase.DOWNLOADING,
            Phase.INSTALLING,
            Phase.FAILED,
        ):
            tray.set_update_available(release.version)

    def _on_update_announced(self, release: object) -> None:
        """A newer version was found by the daily check: say so once."""
        tray = self._tray
        version = getattr(release, "version", None)
        if tray is None or not version:
            return
        shown = tray.notify(
            "Update available",
            f"{APP_NAME} {version} is available. Click here to update.",
        )
        if shown:
            self._prompt = (PROMPT_UPDATE, self._clock())

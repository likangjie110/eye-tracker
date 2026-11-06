"""Split-pane half of :mod:`eye_tracker.engine.controller` (TD-CPLX-001).

Pane support, the pane worker, window following and the per-pane cursor memory live here as a
mixin, so ``controller.py`` keeps the gaze pipeline. The mixin only calls back into the host
controller; it must never import ``controller`` at runtime (TYPE_CHECKING only).
"""

from __future__ import annotations

import dataclasses
import logging
import math
from typing import TYPE_CHECKING, Any

from ..gaze.store import CalibrationData, axis_error
from ..panes.decider import FALLBACK_GAZE_ERROR_PX, PaneDecision, eligible_panes
from ..panes.providers.command import window_key
from ..panes.registry import PaneRegistry, default_providers, is_denied
from ..panes.types import Pane, PaneSnapshot
from ..panes.worker import DetectResult, FocusResult, PaneWorker
from ..types import AppIdentity, Monitor, Rect, WindowRef, monitor_at, nearest_monitor
from .controller_support import _same_handle

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..config import Settings
    from .decision import Decision, SwitchDecider
    from .input_state import InputTracker
    from .panes.decider import PaneDecider

    PaneRegistryFactory = Callable[[Settings], PaneRegistry]
    PaneWorkerFactory = Callable[[PaneRegistry], PaneWorker]

log = logging.getLogger("eye_tracker.engine.controller")

#: While the gaze is inside the focused terminal window its panes are asked for
#: again this often (at the next window poll), so the layout the pane decider
#: sees is never much older than its freshness limit (1.5 s).
PANE_REFRESH_S = 1.0

#: How long :meth:`Controller.shutdown` waits for a pane provider call in progress.
PANE_JOIN_TIMEOUT_S = 2.0

#: Windows whose pane cursor positions are remembered (least recently used dropped).
PANE_CURSOR_WINDOWS = 16


class _PaneHost:
    """State and callbacks the pane mixin expects from the host ``Controller``."""

    _settings: Settings
    _clock: Callable[[], float]
    _closed: bool
    _monitors: list[Monitor]
    _cursor_reliable: bool
    _last_gaze: tuple[float, float] | None
    _decider: SwitchDecider
    _input: InputTracker
    _calibration: CalibrationData | None
    _pane_decider: PaneDecider
    _pane_registry_factory: PaneRegistryFactory
    _pane_worker_factory: PaneWorkerFactory
    _pane_worker: PaneWorker | None
    _pane_capable: bool | None
    _pane_window: WindowRef | None
    _pane_app: AppIdentity | None
    _pane_snapshot: PaneSnapshot | None
    _pane_requested_at: float
    _pane_last_request: float
    _pane_valid_from: float
    _pane_sigma_cache: tuple[CalibrationData, int, Rect, tuple[float, float] | None] | None
    _pane_sigma_used: tuple[float, float] | None
    _pane_switches: int
    _pane_cursor: dict[object, tuple[WindowRef, dict[Any, tuple[int, int]]]]

    if TYPE_CHECKING:

        def _cursor_pos(self) -> tuple[int, int] | None: ...
        def _move_cursor(self, x: int, y: int, now: float) -> bool: ...
        def _platform_call(self, name: str, *args: Any, default: Any = None) -> Any: ...


class ControllerPanesMixin(_PaneHost):
    """Split-pane support and the pane cursor (mixed into ``Controller``)."""

    def _pane_status(self) -> dict[str, Any]:
        """Split-pane focus at a glance: never titles, commands or paths."""
        snapshot = self._pane_snapshot
        window = self._pane_window
        sigma = self._pane_sigma_used
        if sigma is None and window is not None and window.rect is not None:
            sigma = self._pane_sigma(window.rect)
        worker = self._pane_worker
        fallback = (FALLBACK_GAZE_ERROR_PX, FALLBACK_GAZE_ERROR_PX)
        return {
            "enabled": bool(self._settings.panes.enabled),
            "supported": self._panes_supported(),
            "provider": snapshot.provider if snapshot is not None else None,
            "panes": len(snapshot.panes) if snapshot is not None else 0,
            "eligible": eligible_panes(snapshot, sigma or fallback, self._pane_decider.config),
            "switches": self._pane_switches,
            "sigma_px": None if sigma is None else [round(sigma[0], 1), round(sigma[1], 1)],
            "failed_providers": sorted(worker.disabled_providers) if worker is not None else [],
        }

    def _panes_supported(self) -> bool:
        """The platform's ``panes`` capability (asked once)."""
        if self._pane_capable is None:
            caps = self._platform_call("capabilities", default={})
            self._pane_capable = bool(isinstance(caps, dict) and caps.get("panes", False))
        return self._pane_capable

    def _panes_wanted(self) -> bool:
        s = self._settings
        return bool(s.panes.enabled and s.switching.enabled and self._panes_supported())

    def _default_pane_registry(self, settings: Settings) -> PaneRegistry:
        return PaneRegistry(
            default_providers(settings.panes, client_rect=self._pane_client_rect),
            desktop_apps=settings.panes.desktop_apps,
        )

    def _pane_client_rect(self, ref: WindowRef) -> Rect | None:  # pane worker thread
        rect = self._platform_call("window_client_rect", ref)
        return rect if isinstance(rect, Rect) else None

    def _ensure_pane_worker(self) -> PaneWorker | None:
        if self._pane_worker is None and self._panes_wanted() and not self._closed:
            try:
                worker = self._pane_worker_factory(self._pane_registry_factory(self._settings))
                worker.detected.connect(self._on_pane_detected)
                worker.focused.connect(self._on_pane_focused)
                worker.start()
            except Exception:
                log.warning("Split-pane focus could not start", exc_info=True)
                return None
            self._pane_worker = worker
            # A new worker numbers its requests from 1 again.
            self._pane_last_request = 0
            self._pane_valid_from = 0
            log.info("Split-pane focus is on (experimental)")
        return self._pane_worker

    def _stop_pane_worker(self, *, wait: bool) -> None:
        """Stop the pane worker. Only :meth:`shutdown` waits for a provider call
        in progress; a settings change must not block the GUI thread, and a
        stopped worker delivers nothing more."""
        worker, self._pane_worker = self._pane_worker, None
        if worker is None:
            return
        try:
            if wait:
                worker.stop(PANE_JOIN_TIMEOUT_S)
            else:
                worker.request_stop()
        except Exception:
            log.debug("Stopping the pane worker failed", exc_info=True)

    def _reconfigure_panes(self, *, providers_changed: bool) -> None:
        """Split-pane settings changed: start, stop or re-provision the worker."""
        if not self._panes_wanted():
            if self._pane_worker is not None:
                log.info("Split-pane focus is off")
            self._stop_pane_worker(wait=False)
            self._forget_pane_window()
            self._pane_decider.reset()
            return
        worker = self._pane_worker
        if worker is not None and providers_changed:
            worker.set_registry(self._pane_registry_factory(self._settings))
            self._pane_snapshot = None
            self._pane_requested_at = -math.inf
            self._pane_valid_from = self._pane_last_request + 1

    def _forget_pane_window(self) -> None:
        self._pane_window = None
        self._pane_app = None
        self._pane_snapshot = None
        self._pane_requested_at = -math.inf
        self._pane_valid_from = self._pane_last_request + 1

    def _follow_pane_window(self, ref: WindowRef, now: float) -> None:
        """Track the focused window and ask for its panes when useful.

        A new window is asked about at once; the window being followed again
        every :data:`PANE_REFRESH_S` while the gaze is inside it. Windows of
        denied applications (see ``panes.registry``) are never asked about.
        """
        current = self._pane_window
        if (
            current is None
            or current.pid != ref.pid  # a handle reused by another process's window
            or not self._platform_call("same_window", current, ref, default=False)
        ):
            self._pane_window = ref
            self._pane_snapshot = None
            self._pane_requested_at = -math.inf
            self._pane_valid_from = self._pane_last_request + 1
            self._prune_pane_cursors()
            app = self._platform_call("window_app", ref)
            self._pane_app = app if isinstance(app, AppIdentity) else None
            fresh = True
        else:
            # Keep the handle object the snapshot belongs to; only the frame moves.
            if ref.rect != current.rect:
                self._pane_window = dataclasses.replace(current, rect=ref.rect)
            fresh = False
        app = self._pane_app
        if app is None or is_denied(app, desktop_apps=self._settings.panes.desktop_apps):
            return
        if not fresh:
            gaze = self._last_gaze
            rect = ref.rect
            inside = gaze is not None and rect is not None and rect.contains(*gaze)
            if not inside or now - self._pane_requested_at < PANE_REFRESH_S:
                return
        worker = self._ensure_pane_worker()
        window = self._pane_window
        if worker is None or window is None:
            return
        self._pane_requested_at = now
        self._pane_last_request = worker.request_detect(window, app)

    def _on_pane_detected(self, result: DetectResult) -> None:
        window = self._pane_window
        if self._closed or window is None or not _same_handle(result.window_handle, window.handle):
            return  # the user moved on to another window meanwhile
        if result.request_id < self._pane_valid_from:
            return  # asked before a window change or with the providers since replaced
        self._pane_snapshot = result.snapshot
        if result.snapshot is not None:
            # Panes that closed take their remembered cursor position with them.
            entry = self._pane_cursor.get(window_key(window))
            if entry is not None:
                ids = {p.id for p in result.snapshot.panes}
                for gone in [pid for pid in entry[1] if pid not in ids]:
                    del entry[1][gone]

    def _on_pane_focused(self, result: FocusResult) -> None:
        if self._closed:
            return
        if not result.ok:
            log.debug("Could not focus pane %s", result.pane_id)
            return
        self._pane_switches += 1
        snapshot = self._pane_snapshot
        if snapshot is not None and _same_handle(snapshot.window_handle, result.window_handle):
            pane = snapshot.pane(result.pane_id)
            self._pane_snapshot = snapshot.with_focus(result.pane_id)
            if pane is not None and self._settings.panes.move_cursor:
                self._move_cursor_into_pane(pane)
        self._pane_requested_at = -math.inf  # confirm the new layout at the next poll

    def _move_cursor_into_pane(self, pane: Pane) -> None:
        """After a pane got the keyboard focus: the cursor follows, to where the
        user last left it in that pane (if still inside it), else its centre.

        The same path as a monitor switch: the warp is announced to the input
        tracker first (so it is not taken for mouse use) and refusals count
        towards the warp backoff. Nothing happens if the cursor is already in
        the pane.
        """
        window = self._pane_window
        if window is None:
            return
        rect = pane.rect
        cursor = self._cursor_pos()
        if self._cursor_reliable and cursor is not None and rect.contains(*cursor):
            return
        entry = self._pane_cursor.get(window_key(window))
        spot = entry[1].get(pane.id) if entry is not None else None
        if spot is None or not rect.contains(*spot):
            spot = rect.clamp(*rect.center)
        now = self._clock()
        self._input.note_programmatic_move(spot, now)
        if self._move_cursor(spot[0], spot[1], now):
            log.debug("Cursor moved into pane %s", pane.id)

    def _remember_pane_cursor(self, pos: tuple[int, int]) -> None:
        """Record ``pos`` for the pane of the followed window that contains it."""
        window = self._pane_window
        snapshot = self._pane_snapshot
        if (
            window is None
            or snapshot is None
            or not self._settings.panes.move_cursor
            or not _same_handle(snapshot.window_handle, window.handle)
        ):
            return
        pane = next((p for p in snapshot.panes if p.rect.contains(*pos)), None)
        if pane is None:
            return
        key = window_key(window)
        entry = self._pane_cursor.pop(key, None)  # re-inserted: most recently used last
        spots = entry[1] if entry is not None else {}
        spots[pane.id] = (int(pos[0]), int(pos[1]))
        self._pane_cursor[key] = (window, spots)
        while len(self._pane_cursor) > PANE_CURSOR_WINDOWS:
            self._pane_cursor.pop(next(iter(self._pane_cursor)))

    def _prune_pane_cursors(self) -> None:
        """Forget the cursor positions of windows that are gone."""
        for key, (ref, _spots) in list(self._pane_cursor.items()):
            if not self._platform_call("is_window_valid", ref, default=False):
                del self._pane_cursor[key]

    def _pane_sigma(self, rect: Rect) -> tuple[float, float] | None:
        """The calibration's gaze error on the monitor showing ``rect`` (cached)."""
        cal = self._calibration
        if cal is None or not self._monitors:
            return None
        cx, cy = rect.center
        monitor = monitor_at(self._monitors, cx, cy) or nearest_monitor(self._monitors, cx, cy)[0]
        cached = self._pane_sigma_cache
        if (
            cached is not None
            and cached[0] is cal
            and cached[1] == monitor.index
            and cached[2] == monitor.rect
        ):
            return cached[3]
        sigma = axis_error(cal, monitor)
        self._pane_sigma_cache = (cal, monitor.index, monitor.rect, sigma)
        return sigma

    def _pane_step(
        self,
        now: float,
        gaze: tuple[float, float] | None,
        decision: Decision,
        enabled: bool,
    ) -> PaneDecision:
        """Move the keyboard focus between split panes (only while the monitor
        decider is content). Never activates a window; the cursor follows only once
        the focus call succeeded (:meth:`_on_pane_focused`)."""
        s = self._settings.panes
        worker = self._pane_worker
        window = self._pane_window
        active = (
            enabled
            and s.enabled
            and worker is not None
            and window is not None
            and decision.target is None
            and decision.reason == "same"
            and now - self._decider.last_switch_time >= s.after_monitor_switch_ms / 1000.0
        )
        rect = window.rect if window is not None else None
        sigma = self._pane_sigma(rect) if active and rect is not None else None
        if active:
            self._pane_sigma_used = sigma
        pane = self._pane_decider.update(
            now,
            gaze,
            rect,
            self._pane_snapshot,
            sigma,
            self._input.last_mouse_activity,
            self._input.last_key_activity,
            enabled=active,
        )
        if pane.target is not None and worker is not None and window is not None:
            worker.request_focus(window, pane.target)
        return pane

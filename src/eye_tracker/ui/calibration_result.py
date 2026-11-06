"""Result/save/retry half of :mod:`eye_tracker.ui.calibration_window` (TD-CPLX-005).

The result and error screens, saving, cancelling and retrying live here; the tick/render and
lifecycle halves are in :mod:`eye_tracker.ui.calibration_flow`. The mixin only calls back into the
host window; this module must never import ``calibration_window`` at runtime (TYPE_CHECKING only),
and it imports ``calibration_flow`` one way for the shared host declaration and constants.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from ..gaze.store import CalibrationData
from ..types import layout_signature
from .calibration_flow import (
    STATE_CLOSED,
    STATE_ERROR,
    STATE_IDLE,
    STATE_RESULT,
    _CalibrationHost,
)
from .calibration_model import (
    _DANGER,
    _TEXT,
    _WARNING,
    GRADE_COLORS,
    _spatial_order,
)

if TYPE_CHECKING:
    from ..gaze.calibration import CalibrationReport
    from .calibration_widgets import _Card

# The window's logger: the class split must not move the log records.
log = logging.getLogger("eye_tracker.ui.calibration_window")


class CalibrationResultMixin(_CalibrationHost):
    """Result screen, saving and retrying; mixed into ``CalibrationWindow``."""

    def retry(self) -> None:
        """Discard the collected dots and run the sequence again."""
        log.info("Calibration restarted")
        self.begin()

    def save(self) -> bool:
        """Hand the calibration to the controller (Enter on the result screen)."""
        if self._state != STATE_RESULT or self._model is None or self._report is None:
            return False
        collector = self._collector
        assert collector is not None
        if self._layout_changed():
            # Retrying would plan on the same stale layout: close, and let the
            # user start over on the new one.
            self._on_screens_changed()
            return False
        try:
            backend, feature_version = self._controller.backend_info()  # type: ignore[attr-defined]
        except Exception:
            log.exception("controller.backend_info failed")
            backend = feature_version = ""
        if not backend or not feature_version:
            # Without them the calibration could never be matched to the backend later.
            self._show_error("The vision backend is not ready. Please try again.")
            return False
        data = CalibrationData(
            backend=str(backend),
            feature_version=str(feature_version),
            layout_signature=layout_signature(self._monitors),
            monitors=list(self._monitors),
            samples=collector.samples,
            implicit_samples=[],
            model=self._model,
            report=self._report.to_dict(),
            # The calibration only fits the camera it was recorded with (its
            # position and field of view shape every feature).
            camera=self._camera_device(),
            frame_size=collector.frame_size,
        )
        self._data = data
        log.info("Calibration saved: %s", self._report.summary())
        self._finish(data)
        return True

    def cancel(self) -> None:
        """Close without saving (Esc)."""
        if self._state in (STATE_IDLE, STATE_CLOSED):
            return
        log.info("Calibration cancelled")
        self._finish(None)

    close = cancel

    def _show_result(self, report: CalibrationReport) -> None:
        collector = self._collector
        skipped = len(collector.skipped_points) if collector is not None else 0
        multi = len(self._monitors) > 1
        for surface in self._surfaces:
            surface.unsetCursor()
            card = surface.show_card()
            card.clear()
            if surface is not self._surfaces[0]:
                card.add_label("Your results are on the main screen.", muted=True, size=12.0)
            else:
                self._fill_result_card(card, report, multi=multi, skipped=skipped)
            surface.place_card()
        self._focus_primary()

    def _fill_result_card(
        self, card: _Card, report: CalibrationReport, *, multi: bool, skipped: int
    ) -> None:
        card.add_badge(report.grade, GRADE_COLORS.get(report.grade, _TEXT))
        if multi:
            pct = math.floor(report.monitor_accuracy * 100 + 1e-9)
            card.add_label(f"{pct}%", name="headline", size=40.0, bold=True)
            card.add_label("of glances landed on the right screen", muted=True)
        else:
            error = report.mean_error_px
            value = f"{error:.0f} px" if math.isfinite(error) else "—"
            card.add_label(value, name="headline", size=40.0, bold=True)
            card.add_label("average distance from the dots", muted=True)
        card.add_spacing(6)
        if multi and report.per_monitor_accuracy:
            # Left to right, like the dots and the "Screen n" labels.
            order = {m.index: n for n, m in enumerate(_spatial_order(self._monitors))}
            rows = [
                (self._labels.get(index, f"Screen {index + 1}"), value)
                for index, value in sorted(
                    report.per_monitor_accuracy.items(),
                    key=lambda kv: (order.get(kv[0], len(order)), kv[0]),
                )
            ]
            card.add_bars(rows)
            card.add_spacing(4)
        details = f"{report.n_samples} samples from {report.n_points} dots"
        if math.isfinite(report.mean_error_px) and multi:
            details = f"Mean error {report.mean_error_px:.0f} px · " + details
        if skipped:
            details += f" · {skipped} skipped"
        card.add_label(details, name="details", muted=True, size=10.0)
        self._add_result_tip(card, report)
        card.add_spacing(8)
        card.add_buttons(
            [
                ("save", "Save   ⏎", self.save, True),
                ("retry", "Retry   R", self.retry, False),
                ("cancel", "Cancel   Esc", self.cancel, False),
            ]
        )

    def _add_result_tip(self, card: _Card, report: CalibrationReport) -> None:
        uncovered = self._uncovered_labels(report)
        if uncovered:
            # Every dot of these screens was skipped: switching to them cannot work.
            verb = "was" if len(uncovered) == 1 else "were"
            card.add_label(
                f"{' and '.join(uncovered)} {verb} not calibrated — make sure the camera "
                "sees your face when you look there, then press R to try again.",
                name="tip",
                size=10.5,
                color=_WARNING,
            )
        elif report.grade in ("fair", "poor"):
            tip = (
                "Tip: sit where you usually sit, make sure your face is evenly lit and "
                "turn your head a little towards each dot. Press R to try again."
            )
            card.add_label(tip, name="tip", size=10.5, color=_WARNING)
        else:
            card.add_label(
                "Stored only on this computer. It keeps improving as you use the mouse.",
                name="tip",
                size=10.0,
                muted=True,
            )

    def _uncovered_labels(self, report: CalibrationReport) -> list[str]:
        """Labels of the monitors the report has no dots for, left to right."""
        uncovered = set(getattr(report, "uncovered_monitors", None) or ())
        return [
            self._labels.get(m.index, f"Screen {m.index + 1}")
            for m in _spatial_order(self._monitors)
            if m.index in uncovered
        ]

    def _show_error(self, message: str, *, badge: str = "Not enough data") -> None:
        self._timer.stop()
        self._error = message
        self._last_input_at = self._clock()  # the idle timeout counts from here
        self._set_state(STATE_ERROR)
        for surface in self._surfaces:
            surface.unsetCursor()
            card = surface.show_card()
            card.clear()
            if surface is not self._surfaces[0]:
                card.add_label("See the main screen.", muted=True, size=12.0)
                surface.place_card()
                continue
            card.add_badge(badge, _DANGER)
            card.add_label("Let's try that again", size=20.0, bold=True)
            card.add_label(message, name="message", size=12.0)
            card.add_label(
                "Check that the camera sees your whole face, that the room is not too dark "
                "and that nothing else is using the camera.",
                muted=True,
                size=10.5,
            )
            card.add_spacing(8)
            card.add_buttons(
                [
                    ("retry", "Try again   R", self.retry, True),
                    ("cancel", "Cancel   Esc", self.cancel, False),
                ]
            )
            surface.place_card()
        self._focus_primary()

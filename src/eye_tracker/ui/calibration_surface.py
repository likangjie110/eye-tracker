"""The calibration surface: one full-screen window per monitor (phase 4 / B3).

Draws the intro text, the moving dot and the result cards; the ``CalibrationWindow`` decides what
state it is in.
"""

from __future__ import annotations

import logging
import math
import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QConicalGradient,
    QFont,
    QKeyEvent,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QRadialGradient,
    QResizeEvent,
    QScreen,
)
from PySide6.QtWidgets import QWidget

from ..types import Monitor
from .calibration_model import (
    _ACCENT,
    _ACCENT_2,
    _BG_INNER,
    _BG_OUTER,
    _MUTED,
    _TEXT,
    _WARNING,
    _clamp01,
    _DotState,
    _ease_out_cubic,
    _rgba,
)
from .calibration_widgets import _Card
from .util import ui_scale

if TYPE_CHECKING:
    from .calibration_window import CalibrationWindow

log = logging.getLogger(__name__)


# ================================================================== surface
class _Surface(QWidget):
    """One monitor's full-screen calibration window."""

    MODE_CARD = "card"
    MODE_RUNNING = "running"

    def __init__(self, owner: CalibrationWindow, monitor: Monitor, screen: QScreen | None) -> None:
        super().__init__(
            None,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self._owner = owner
        self.monitor = monitor
        self.qscreen = screen
        self.scale = ui_scale(screen) if screen is not None else 1.0
        self.setWindowTitle("Eye Tracker — calibration")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(False)
        self.mode = self.MODE_CARD
        self.active = False
        self.dot: _DotState | None = None
        self.progress = 0.0
        self.progress_text = ""
        self.hint = ""
        self.hint_color = _WARNING
        self.keys_text = ""
        self.closing = False
        self.card = _Card(self, self.scale)
        self.card.hide()

    # -------------------------------------------------------------- showing
    def present(self) -> None:
        rect = self.monitor.rect
        if self.qscreen is not None:
            self.setScreen(self.qscreen)
        self.setGeometry(QRect(rect.x, rect.y, rect.w, rect.h))
        if sys.platform == "darwin":
            # A full-screen window gets its own Space on macOS (with an animation and
            # a black backdrop on the other displays); a borderless screen-sized
            # window stays on the current Space.
            self.show()
        else:
            self.showFullScreen()

    # -------------------------------------------------------------- card mode
    def show_card(self) -> _Card:
        self.mode = self.MODE_CARD
        self.card.show()
        self.place_card()
        self.update()
        return self.card

    def place_card(self) -> None:
        s = self.scale
        width = max(round(280 * s), min(round(600 * s), self.width() - round(48 * s)))
        height = self.card.preferred_height(width)
        x = (self.width() - width) // 2
        y = max(round(24 * s), int((self.height() - height) / 2 - self.height() * 0.04))
        self.card.setGeometry(x, y, width, height)

    # -------------------------------------------------------------- running mode
    def set_running(
        self,
        dot: _DotState | None,
        *,
        active: bool,
        progress: float,
        progress_text: str,
        hint: str,
        hint_color: QColor,
        keys_text: str,
    ) -> None:
        """Update the running display, repainting only what changed."""
        full = self.mode != self.MODE_RUNNING or active != self.active
        if not active and (self.dot is None) != (dot is None):
            full = True
        if (
            not active
            and dot is not None
            and self.dot is not None
            and (abs(dot.x - self.dot.x) > 0.5 or abs(dot.y - self.dot.y) > 0.5)
        ):
            full = True  # the pointer towards the dot moved
        old_dot_rect = self._dot_rect(self.dot) if self.active else None
        footer_changed = (
            abs(progress - self.progress) > 1e-4
            or progress_text != self.progress_text
            or hint != self.hint
            or keys_text != self.keys_text
        )
        self.mode = self.MODE_RUNNING
        self.card.hide()
        self.active, self.dot = active, dot
        self.progress, self.progress_text = progress, progress_text
        self.hint, self.hint_color, self.keys_text = hint, hint_color, keys_text
        if full:
            self.update()
            return
        if active:
            new_rect = self._dot_rect(dot)
            if old_dot_rect is not None and new_rect is not None:
                self.update(old_dot_rect.united(new_rect))
            elif new_rect is not None:
                self.update(new_rect)
            elif old_dot_rect is not None:
                self.update(old_dot_rect)
        if footer_changed:
            self.update(self._footer_rect())

    # -------------------------------------------------------------- events
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._owner.handle_key(int(event.key())):
            super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.closing:
            # Closed by the window manager (Alt+F4, …): treat as cancel.
            self._owner.cancel()
        event.accept()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.mode == self.MODE_CARD:
            self.place_card()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        # Replace (not blend) the translucent background of the repainted region.
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        bg = QRadialGradient(QPointF(rect.center()), max(rect.width(), rect.height()) * 0.75)
        bg.setColorAt(0.0, _BG_INNER)
        bg.setColorAt(1.0, _BG_OUTER)
        p.fillRect(event.rect(), QBrush(bg))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if self.mode == self.MODE_RUNNING:
            if self.active and self.dot is not None:
                self._paint_dot(p, self.dot)
            elif self.dot is not None:
                self._paint_pointer(p, self.dot)
            self._paint_footer(p)
        p.end()

    # -------------------------------------------------------------- geometry
    def _local(self, x: float, y: float) -> QPointF:
        return self.mapFromGlobal(QPointF(x, y))

    def _dot_extent(self) -> float:
        s = self.scale
        return max(66 * s, 11 * s * 3.4) + 6 * s

    def _dot_rect(self, dot: _DotState | None) -> QRect | None:
        if dot is None:
            return None
        c = self._local(dot.x, dot.y)
        e = self._dot_extent()
        return QRectF(c.x() - e, c.y() - e, 2 * e, 2 * e).toAlignedRect()

    def _footer_geometry(self) -> tuple[float, float, float, float]:
        s = self.scale
        width = min(self.width() * 0.36, 520 * s)
        height = max(3.0, 4 * s)
        x0 = (self.width() - width) / 2
        y0 = self.height() - 72 * s
        return x0, y0, width, height

    def _footer_rect(self) -> QRect:
        s = self.scale
        top = int(self.height() - 72 * s - 96 * s)
        return QRect(0, top, self.width(), self.height() - top)

    # -------------------------------------------------------------- painting
    def _paint_dot(self, p: QPainter, dot: _DotState) -> None:
        s = self.scale
        c = self._local(dot.x, dot.y)
        r = 11 * s * max(0.0, dot.pop)
        base_a = QColor(107, 114, 128) if dot.paused else _ACCENT
        base_b = QColor(156, 163, 175) if dot.paused else _ACCENT_2

        # Soft glow.
        if r > 0:
            glow = QRadialGradient(c, r * 3.4)
            glow.setColorAt(0.0, _rgba(base_a, 0.45))
            glow.setColorAt(0.45, _rgba(base_b, 0.14))
            glow.setColorAt(1.0, _rgba(base_b, 0.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            p.drawEllipse(c, r * 3.4, r * 3.4)

        ring_end = 11 * s + 8 * s
        if dot.ring is not None:
            # Shrinking ring: tells the eyes where to go and when collection starts.
            t = _ease_out_cubic(dot.ring)
            radius = 64 * s + (ring_end - 64 * s) * t
            color = _WARNING if dot.retry else _TEXT
            pen = QPen(_rgba(color, 0.25 + 0.6 * min(1.0, dot.ring / 0.15)), max(1.5, 2 * s))
            if dot.paused:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, radius, radius)
        if dot.arc is not None:
            # Collection progress arc, clockwise from 12 o'clock.
            box = QRectF(c.x() - ring_end, c.y() - ring_end, 2 * ring_end, 2 * ring_end)
            width = max(2.0, 3 * s)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 38), width))
            p.drawEllipse(box)
            if dot.arc > 0:
                if dot.retry:
                    brush = QBrush(_WARNING)
                else:
                    grad = QConicalGradient(c, 90.0)
                    grad.setColorAt(0.0, base_b)
                    grad.setColorAt(1.0, base_a)
                    brush = QBrush(grad)
                pen = QPen(brush, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawArc(box, 90 * 16, -round(360 * 16 * _clamp01(dot.arc)))

        if r > 0:
            disc = QLinearGradient(c.x() - r, c.y() - r, c.x() + r, c.y() + r)
            disc.setColorAt(0.0, base_a)
            disc.setColorAt(1.0, base_b)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(disc))
            p.drawEllipse(c, r, r)
            # A crisp centre point gives the eyes something precise to fixate.
            p.setBrush(QColor(255, 255, 255, 240))
            core = max(1.5, 2.6 * s * dot.pop)
            p.drawEllipse(c, core, core)

    def _paint_pointer(self, p: QPainter, dot: _DotState) -> None:
        s = self.scale
        rect = self.rect()
        center = QPointF(rect.center())
        target = self._local(dot.target_x, dot.target_y)
        dx, dy = target.x() - center.x(), target.y() - center.y()
        dist = math.hypot(dx, dy)
        if dist >= 1.0:
            ux, uy = dx / dist, dy / dist
            reach = min(rect.width(), rect.height()) * 0.28
            tip = QPointF(center.x() + ux * reach, center.y() + uy * reach)
            angle = math.atan2(uy, ux)
            size = 26 * s
            path = QPainterPath()
            for n, sign in enumerate((-1, 1)):
                a = angle + math.pi + sign * math.radians(40)
                end = QPointF(tip.x() + size * math.cos(a), tip.y() + size * math.sin(a))
                if n == 0:
                    path.moveTo(end)
                    path.lineTo(tip)
                else:
                    path.lineTo(end)
            pen = QPen(_rgba(_ACCENT_2, 0.85), max(3.0, 5 * s))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        font = QFont(self.font())
        font.setPointSizeF(13.0)
        p.setFont(font)
        p.setPen(_MUTED)
        text_rect = QRectF(0, center.y() - 20 * s, rect.width(), 40 * s)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, "Follow the dot on the other screen")

    def _paint_footer(self, p: QPainter) -> None:
        s = self.scale
        x0, y0, width, height = self._footer_geometry()
        radius = height / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 30))
        p.drawRoundedRect(QRectF(x0, y0, width, height), radius, radius)
        fill = width * _clamp01(self.progress)
        if fill > 0:
            grad = QLinearGradient(x0, y0, x0 + width, y0)
            grad.setColorAt(0.0, _ACCENT)
            grad.setColorAt(1.0, _ACCENT_2)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(x0, y0, max(height, fill), height), radius, radius)

        font = QFont(self.font())
        font.setPointSizeF(10.0)
        p.setFont(font)
        p.setPen(_MUTED)
        full_w = float(self.width())
        if self.progress_text:
            p.drawText(
                QRectF(0, y0 - 30 * s, full_w, 22 * s),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                self.progress_text,
            )
        if self.keys_text:
            font.setPointSizeF(9.0)
            p.setFont(font)
            p.drawText(
                QRectF(0, y0 + 14 * s, full_w, 22 * s),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                self.keys_text,
            )
        if self.hint and self.active:
            font.setPointSizeF(14.0)
            font.setWeight(QFont.Weight.DemiBold)
            p.setFont(font)
            p.setPen(self.hint_color)
            p.drawText(
                QRectF(0, y0 - 90 * s, full_w, 44 * s),
                Qt.AlignmentFlag.AlignCenter,
                self.hint,
            )

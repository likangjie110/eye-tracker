"""Small widgets painted by the calibration window (phase 4 / B3).

``_Bar``, ``_Spinner``, ``_Card`` and the layout helper. They import the colour constants and
drawing helpers from ``calibration_model``; the window owns them.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QFont,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .calibration_model import (
    _ACCENT,
    _ACCENT_2,
    _MUTED,
    _TEXT,
    _clamp01,
    _css,
    _grade_color,
    _rgba,
)

log = logging.getLogger(__name__)


# ================================================================== card
class _Bar(QWidget):
    """A thin rounded progress bar used for per-monitor accuracy."""

    def __init__(self, value: float, color: QColor, scale: float, parent: QWidget | None = None):
        super().__init__(parent)
        self._value = _clamp01(value)
        self._color = color
        self.setFixedHeight(max(4, round(6 * scale)))
        self.setMinimumWidth(round(120 * scale))

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        rect = QRectF(self.rect())
        radius = rect.height() / 2
        p.setBrush(QColor(255, 255, 255, 28))
        p.drawRoundedRect(rect, radius, radius)
        if self._value > 0:
            fill = QRectF(
                rect.x(), rect.y(), max(rect.height(), rect.width() * self._value), rect.height()
            )
            p.setBrush(self._color)
            p.drawRoundedRect(fill, radius, radius)
        p.end()


class _Spinner(QWidget):
    """A rotating arc shown while the model is being fitted."""

    def __init__(self, scale: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        size = max(16, round(34 * scale))
        self.setFixedSize(size, size)
        self._scale = scale
        self._angle = 0.0

    def set_time(self, seconds: float) -> None:
        """Rotate to the position for ``seconds`` (one turn every 0.9 s)."""
        angle = (seconds / 0.9 * 360.0) % 360.0
        if abs(angle - self._angle) > 0.5:
            self._angle = angle
            self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = max(2.0, 3 * self._scale)
        box = QRectF(self.rect()).adjusted(width, width, -width, -width)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 36), width))
        p.drawEllipse(box)
        grad = QConicalGradient(box.center(), -self._angle)
        grad.setColorAt(0.0, _ACCENT_2)
        grad.setColorAt(0.3, _rgba(_ACCENT, 0.0))
        pen = QPen(QBrush(grad), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        # Qt angles are counter-clockwise in 1/16 degree; spin clockwise.
        p.drawArc(box, round(-self._angle * 16), 110 * 16)
        p.end()


class _Card(QFrame):
    """Centred content panel (instructions, results) built from simple parts."""

    def __init__(self, parent: QWidget, scale: float) -> None:
        super().__init__(parent)
        self.setObjectName("calibrationCard")
        self._scale = scale
        s = scale
        self.setStyleSheet(
            f"""
            QFrame#calibrationCard {{
                background: rgba(255, 255, 255, 0.045);
                border: 1px solid rgba(255, 255, 255, 0.09);
                border-radius: {round(22 * s)}px;
            }}
            QLabel {{ color: {_css(_TEXT)}; background: transparent; border: none; }}
            QLabel[muted="true"] {{ color: {_css(_MUTED)}; }}
            QLabel[keycap="true"] {{
                color: {_css(_TEXT)};
                border: 1px solid rgba(255, 255, 255, 0.28);
                border-bottom-width: 2px;
                border-radius: {round(6 * s)}px;
                padding: {round(2 * s)}px {round(9 * s)}px;
                font-weight: 600;
            }}
            QPushButton {{
                color: {_css(_TEXT)};
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.14);
                border-radius: {round(10 * s)}px;
                padding: {round(9 * s)}px {round(20 * s)}px;
                font-size: 11pt;
            }}
            QPushButton:hover {{ background: rgba(255, 255, 255, 0.15); }}
            QPushButton:pressed {{ background: rgba(255, 255, 255, 0.05); }}
            QPushButton[primary="true"] {{
                color: white;
                border: none;
                font-weight: 600;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {_css(_ACCENT)}, stop:1 {_css(_ACCENT_2)});
            }}
            QPushButton[primary="true"]:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #7C7FF5, stop:1 #4FDDF1);
            }}
            """
        )
        self._layout = QVBoxLayout(self)
        m = round(36 * s)
        self._layout.setContentsMargins(m, round(32 * s), m, round(30 * s))
        self._layout.setSpacing(round(12 * s))
        self.buttons: dict[str, QPushButton] = {}
        self.labels: dict[str, QLabel] = {}
        self.spinner: _Spinner | None = None

    # -------------------------------------------------------------- content
    def clear(self) -> None:
        self.buttons.clear()
        self.labels.clear()
        self.spinner = None
        _delete_layout(self._layout, keep=True)

    def add_label(
        self,
        text: str,
        *,
        name: str | None = None,
        size: float = 11.0,
        bold: bool = False,
        muted: bool = False,
        color: QColor | None = None,
        align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignHCenter,
    ) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAlignment(align)
        label.setTextFormat(Qt.TextFormat.PlainText)
        font = QFont(label.font())
        font.setPointSizeF(size)
        if bold:
            font.setWeight(QFont.Weight.DemiBold)
        label.setFont(font)
        if muted:
            label.setProperty("muted", True)
        if color is not None:
            label.setStyleSheet(f"color: {_css(color)};")
        self._layout.addWidget(label)
        if name:
            self.labels[name] = label
        return label

    def add_badge(self, text: str, color: QColor) -> QLabel:
        s = self._scale
        badge = QLabel(text.upper())
        font = QFont(badge.font())
        font.setPointSizeF(9.0)
        font.setWeight(QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        badge.setFont(font)
        badge.setStyleSheet(
            f"color: {_css(color)}; background: rgba({color.red()}, {color.green()},"
            f" {color.blue()}, 0.16); border: 1px solid rgba({color.red()}, {color.green()},"
            f" {color.blue()}, 0.45); border-radius: {round(11 * s)}px;"
            f" padding: {round(3 * s)}px {round(12 * s)}px;"
        )
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(badge)
        row.addStretch(1)
        self._layout.addLayout(row)
        self.labels["badge"] = badge
        return badge

    def add_keys(self, items: Sequence[tuple[str, str]]) -> None:
        row = QHBoxLayout()
        row.setSpacing(round(8 * self._scale))
        row.addStretch(1)
        for n, (key, action) in enumerate(items):
            if n:
                dot = QLabel("·")
                dot.setProperty("muted", True)
                row.addSpacing(round(6 * self._scale))
                row.addWidget(dot)
                row.addSpacing(round(6 * self._scale))
            cap = QLabel(key)
            cap.setProperty("keycap", True)
            text = QLabel(action)
            text.setProperty("muted", True)
            row.addWidget(cap)
            row.addWidget(text)
        row.addStretch(1)
        self._layout.addLayout(row)

    def add_bars(self, rows: Sequence[tuple[str, float]]) -> None:
        s = self._scale
        for label_text, value in rows:
            row = QHBoxLayout()
            row.setSpacing(round(12 * s))
            label = QLabel(label_text)
            label.setMinimumWidth(round(130 * s))
            label.setProperty("muted", True)
            pct = QLabel(f"{math.floor(value * 100 + 1e-9)}%")
            pct.setMinimumWidth(round(44 * s))
            pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(label)
            row.addWidget(_Bar(value, _grade_color(value), s), 1)
            row.addWidget(pct)
            self._layout.addLayout(row)

    def add_buttons(self, buttons: Sequence[tuple[str, str, Callable[[], object], bool]]) -> None:
        """``(name, text, callback, primary)`` for each button, left to right."""
        row = QHBoxLayout()
        row.setSpacing(round(10 * self._scale))
        row.addStretch(1)
        for name, text, callback, primary in buttons:
            button = QPushButton(text)
            # Keys are handled by the surface (Enter / R / Esc); focusable buttons
            # would swallow Space and Enter.
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if primary:
                button.setProperty("primary", True)
            button.clicked.connect(lambda _=False, cb=callback: cb())
            row.addWidget(button)
            self.buttons[name] = button
        row.addStretch(1)
        self._layout.addLayout(row)

    def add_spinner(self) -> _Spinner:
        spinner = _Spinner(self._scale)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(spinner)
        row.addStretch(1)
        self._layout.addLayout(row)
        self.spinner = spinner
        return spinner

    def add_spacing(self, points: float) -> None:
        self._layout.addSpacing(round(points * self._scale))

    def preferred_height(self, width: int) -> int:
        """Height the content needs at ``width`` (all current content counted)."""
        self._show_new_children()
        layout = self._layout
        layout.invalidate()
        if layout.hasHeightForWidth():
            return int(layout.totalHeightForWidth(width))
        return int(layout.totalSizeHint().height())

    def _show_new_children(self) -> None:
        # Widgets added to an already visible card stay hidden until a queued show
        # event, and layouts ignore hidden widgets, so the card would be measured
        # without them and end up too small. Show them now. Removed widgets were
        # hidden explicitly (and are awaiting deletion), so they stay hidden.
        for child in self.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
            if child.isHidden() and not child.testAttribute(
                Qt.WidgetAttribute.WA_WState_ExplicitShowHide
            ):
                child.show()


def _delete_layout(layout: QLayout, *, keep: bool = False) -> None:
    """Remove (and schedule deletion of) everything in ``layout``; ``keep`` spares it."""
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        child = item.layout()
        if widget is not None:
            widget.hide()
            widget.deleteLater()
        elif child is not None:
            _delete_layout(child)
    if not keep:
        layout.deleteLater()

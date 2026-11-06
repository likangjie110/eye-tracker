"""The page widgets of the first-run assistant (phase 4 / B3).

``_Illustration``, the page classes and the small formatting helpers. ``FirstRunWizard`` (in
``wizard.py``) composes them; the wizard imports and re-exports the helpers its tests use.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QWizardPage,
)

from .. import APP_NAME
from ..config import describe_settings
from .preview import bgr_to_qimage
from .util import ACCENT, ACCENT_2, SUCCESS

log = logging.getLogger(__name__)

#: Walk-away choices: (value, title, description). ``"off"`` disables presence detection.
PRESENCE_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("lock", "Lock the computer", "Recommended. Unlock as usual when you are back."),
    (
        "lock_and_display_off",
        "Lock and turn the displays off",
        "Also saves power while you are away.",
    ),
    ("display_off", "Turn the displays off", "They come back when you return. Does not lock."),
    ("notify", "Only show a notification", "Nothing else happens."),
    ("off", "Do nothing", "Walk-away detection stays off. You can turn it on later."),
)

_CAPABILITY_FOR = {
    "lock": ("lock",),
    "lock_and_display_off": ("lock", "display_off"),
    "display_off": ("display_off",),
}


if TYPE_CHECKING:
    from .wizard import FirstRunWizard


#: Where to allow camera access, by ``sys.platform`` (Linux: every other Unix).
CAMERA_PRIVACY_HELP: dict[str, str] = {
    "win32": (
        "If Windows blocks the camera: Settings › Privacy & security › Camera, turn on "
        "“Camera access” and “Let desktop apps access your camera”."
    ),
    "darwin": (
        f"If macOS blocks the camera: System Settings › Privacy & Security › Camera, "
        f"allow {APP_NAME}."
    ),
    "linux": (
        "If your account may not use the camera: add it to the “video” group "
        "(sudo usermod -aG video $USER), then log out and back in."
    ),
}


def _meta(key: str) -> dict[str, Any]:
    return next((row for row in describe_settings() if row["key"] == key), {})


def _muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return label


def frame_to_image(frame: object) -> QImage | None:
    """A QImage that owns a copy of a ``uint8`` camera frame; ``None`` if not an image."""
    if not isinstance(frame, np.ndarray):
        return None
    try:
        return bgr_to_qimage(frame)
    except ValueError:
        return None


# ================================================================ small widgets
class _Illustration(QWidget):
    """Two monitors and a cursor hopping to the one being looked at."""

    def __init__(self, scale: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scale = scale
        self.setFixedHeight(round(120 * scale))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event: QPaintEvent) -> None:
        s = self._scale
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = self.palette().color(QPalette.ColorRole.WindowText)
        w = self.width()
        mon_w, mon_h, gap = 150 * s, 88 * s, 22 * s
        x0 = (w - 2 * mon_w - gap) / 2
        y0 = 6 * s
        for n in range(2):
            rect = QRectF(x0 + n * (mon_w + gap), y0, mon_w, mon_h)
            p.setPen(QPen(_alpha(text, 0.55), max(1.5, 2 * s)))
            p.setBrush(_alpha(text, 0.05) if n == 0 else _alpha(QColor(ACCENT), 0.12))
            p.drawRoundedRect(rect, 8 * s, 8 * s)
            stand = QRectF(rect.center().x() - 16 * s, rect.bottom() + 5 * s, 32 * s, 4 * s)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_alpha(text, 0.35))
            p.drawRoundedRect(stand, 2 * s, 2 * s)
        # Gaze arc from the left monitor to the right one.
        start = QPointF(x0 + mon_w * 0.55, y0 + mon_h * 0.45)
        end = QPointF(x0 + mon_w + gap + mon_w * 0.45, y0 + mon_h * 0.45)
        path = QPainterPath(start)
        path.quadTo(QPointF((start.x() + end.x()) / 2, y0 - 2 * s), end)
        grad = QLinearGradient(start, end)
        grad.setColorAt(0, QColor(ACCENT))
        grad.setColorAt(1, QColor(ACCENT_2))
        pen = QPen(QBrush(grad), max(2.0, 3 * s), Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        # Cursor arrow at the destination.
        cx, cy = end.x() + 4 * s, end.y() + 4 * s
        arrow = QPainterPath(QPointF(cx, cy))
        for dx, dy in ((0, 22), (6, 16.5), (10.5, 25), (14, 23.5), (9.5, 15), (17, 15)):
            arrow.lineTo(QPointF(cx + dx * s, cy + dy * s))
        arrow.closeSubpath()
        p.setPen(QPen(QColor(20, 20, 30), max(1.0, 1.2 * s)))
        p.setBrush(QColor(255, 255, 255))
        p.drawPath(arrow)
        p.end()


def _alpha(color: QColor, alpha: float) -> QColor:
    out = QColor(color)
    out.setAlphaF(alpha)
    return out


class _PreviewView(QWidget):
    """Camera preview with rounded corners; frames are kept in memory only."""

    def __init__(self, scale: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scale = scale
        self._image: QImage | None = None
        self._placeholder = "Camera preview"
        self.setMinimumSize(round(320 * scale), round(200 * scale))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_frame(self, frame: object) -> None:
        image = frame_to_image(frame)
        if image is not None:
            self._image = image
            self.update()

    def clear(self, placeholder: str = "Camera preview") -> None:
        self._image = None
        self._placeholder = placeholder
        self.update()

    @property
    def has_image(self) -> bool:
        return self._image is not None

    def paintEvent(self, _event: QPaintEvent) -> None:
        s = self._scale
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = 12 * s
        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        p.setClipPath(clip)
        p.fillRect(rect, QColor(12, 14, 22))
        if self._image is not None and not self._image.isNull():
            size = self._image.size().scaled(
                rect.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio
            )
            target = QRectF(
                rect.x() + (rect.width() - size.width()) / 2,
                rect.y() + (rect.height() - size.height()) / 2,
                size.width(),
                size.height(),
            )
            p.drawImage(target, self._image)
        else:
            p.setPen(QColor(156, 163, 175))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._placeholder)
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, radius, radius)
        p.end()


# ======================================================================= pages
class _WelcomePage(QWizardPage):
    def __init__(self, wizard: FirstRunWizard) -> None:
        super().__init__()
        s = wizard.scale
        self.setTitle(f"Welcome to {APP_NAME}")
        self.setSubTitle("Look at a monitor and your cursor — and keyboard focus — follow.")
        layout = QVBoxLayout(self)
        layout.setSpacing(round(12 * s))
        layout.addWidget(_Illustration(s))
        intro = QLabel(
            f"{APP_NAME} uses your webcam to notice which screen you are looking at and moves "
            "the mouse cursor there, so you can read on one monitor and start typing on the "
            "other without reaching for the mouse. It can also lock the computer when you "
            "walk away."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        promises = QFrame()
        promises.setObjectName("promises")
        promises.setStyleSheet(
            "QFrame#promises { background: rgba(99, 102, 241, 0.08);"
            " border: 1px solid rgba(99, 102, 241, 0.30);"
            f" border-radius: {round(10 * s)}px; }}"
        )
        grid = QVBoxLayout(promises)
        grid.setContentsMargins(round(16 * s), round(12 * s), round(16 * s), round(12 * s))
        grid.setSpacing(round(6 * s))
        for text in (
            "Runs entirely on this computer — no account, no internet connection.",
            "Camera frames are analysed in memory and never saved or sent anywhere.",
            "Privacy mode switches the camera off completely, from the tray or a hotkey.",
        ):
            row = QLabel(f"<span style='color:{SUCCESS}'>✓</span>&nbsp;&nbsp;{text}")
            row.setTextFormat(Qt.TextFormat.RichText)
            row.setWordWrap(True)
            grid.addWidget(row)
        layout.addWidget(promises)
        layout.addStretch(1)


class _CameraPage(QWizardPage):
    def __init__(self, wizard: FirstRunWizard) -> None:
        super().__init__()
        self._wizard = wizard
        s = wizard.scale
        self.setTitle("Camera")
        self.setSubTitle("Sit as you usually do and check that your face is detected.")
        layout = QVBoxLayout(self)
        layout.setSpacing(round(10 * s))

        row = QHBoxLayout()
        row.addWidget(QLabel("Camera:"))
        self.device = QComboBox()
        self.device.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self.device, 1)
        self.detect = QPushButton("Detect cameras")
        self.detect.setToolTip("Look for connected cameras (each may briefly turn on).")
        row.addWidget(self.detect)
        layout.addLayout(row)

        self.preview = _PreviewView(s)
        layout.addWidget(self.preview, 1)

        self.status = QLabel()
        font = QFont(self.status.font())
        font.setPointSizeF(font.pointSizeF() * 1.15)
        font.setWeight(QFont.Weight.DemiBold)
        self.status.setFont(font)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        # Shown only when the camera cannot be opened: the OS privacy switch is
        # the usual culprit, and not something "try another camera" would fix.
        self.help = _muted(CAMERA_PRIVACY_HELP.get(sys.platform, CAMERA_PRIVACY_HELP["linux"]))
        self.help.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.help.setVisible(False)
        layout.addWidget(self.help)
        row = QHBoxLayout()
        row.addStretch(1)
        self.privacy_settings = QPushButton("Open camera privacy settings")
        self.privacy_settings.clicked.connect(wizard.open_camera_privacy_settings)
        self.privacy_settings.setVisible(False)
        row.addWidget(self.privacy_settings)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(
            _muted(
                "The preview is shown only here and is never saved. The camera light may "
                "stay on while Eye Tracker is running; privacy mode turns it off."
            )
        )


class _PermissionsPage(QWizardPage):
    def __init__(self, wizard: FirstRunWizard) -> None:
        super().__init__()
        self._wizard = wizard
        s = wizard.scale
        self.setTitle("Permissions")
        self.setSubTitle("macOS asks you to allow a few things once.")
        layout = QVBoxLayout(self)
        layout.setSpacing(round(14 * s))
        grid = QGridLayout()
        grid.setHorizontalSpacing(round(12 * s))
        grid.setVerticalSpacing(round(6 * s))
        self.status: dict[str, QLabel] = {}
        self.allow: dict[str, QPushButton] = {}
        rows = (
            (
                "camera",
                "Camera",
                "Needed to see where you are looking.",
            ),
            (
                "accessibility",
                "Accessibility",
                "Lets Eye Tracker give keyboard focus to the window on the screen you look "
                "at. Without it only the cursor moves.",
            ),
        )
        for n, (name, title, text) in enumerate(rows):
            heading = QLabel(f"<b>{title}</b>")
            grid.addWidget(heading, n * 2, 0)
            status = QLabel()
            status.setWordWrap(True)
            self.status[name] = status
            grid.addWidget(status, n * 2, 1)
            allow = QPushButton("Allow…")
            allow.clicked.connect(lambda _=False, p=name: wizard.request_permission(p))
            self.allow[name] = allow
            grid.addWidget(allow, n * 2, 2)
            settings = QPushButton("Open Settings")
            settings.clicked.connect(lambda _=False, p=name: wizard.open_permission_settings(p))
            grid.addWidget(settings, n * 2, 3)
            grid.addWidget(_muted(text), n * 2 + 1, 0, 1, 4)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        layout.addWidget(
            _muted("You can change these later in System Settings → Privacy & Security.")
        )
        layout.addStretch(1)


class _PresencePage(QWizardPage):
    def __init__(self, wizard: FirstRunWizard) -> None:
        super().__init__()
        s = wizard.scale
        self.setTitle("When you walk away")
        self.setSubTitle("Eye Tracker notices when you leave. What should happen?")
        layout = QVBoxLayout(self)
        layout.setSpacing(round(4 * s))
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QRadioButton] = {}
        caps = wizard.capabilities
        for value, title, description in PRESENCE_CHOICES:
            button = QRadioButton(title)
            if any(not caps.get(c, True) for c in _CAPABILITY_FOR.get(value, ())):
                button.setText(f"{title} (may not work on this system)")
            self.group.addButton(button)
            self.buttons[value] = button
            layout.addWidget(button)
            if description:
                hint = _muted(description)
                hint.setContentsMargins(round(26 * s), 0, 0, round(6 * s))
                layout.addWidget(hint)
        layout.addSpacing(round(10 * s))
        row = QHBoxLayout()
        row.addWidget(QLabel("After"))
        self.timeout = QSpinBox()
        meta = _meta("presence.away_timeout_s")
        self.timeout.setRange(int(meta.get("lo") or 5), int(meta.get("hi") or 3600))
        self.timeout.setSingleStep(5)
        self.timeout.setSuffix(" seconds")
        self.timeout.setToolTip(str(meta.get("doc") or ""))
        row.addWidget(self.timeout)
        row.addWidget(QLabel("without a face or any input"))
        row.addStretch(1)
        layout.addLayout(row)
        self.countdown = _muted("")
        layout.addWidget(self.countdown)
        layout.addStretch(1)
        for button in self.buttons.values():
            button.toggled.connect(self._update_enabled)

    def _update_enabled(self) -> None:
        off = self.buttons["off"].isChecked()
        self.timeout.setEnabled(not off)


class _FinishPage(QWizardPage):
    def __init__(self, wizard: FirstRunWizard) -> None:
        super().__init__()
        s = wizard.scale
        self.setTitle("You're all set")
        self.setSubTitle(_finish_subtitle())
        layout = QVBoxLayout(self)
        layout.setSpacing(round(10 * s))
        self.calibrate = QCheckBox("Calibrate now (recommended, about 30 seconds)")
        self.calibrate.setChecked(True)
        self.calibrate.setToolTip(
            "Switching needs a short calibration: you look at a few dots on each screen."
        )
        layout.addWidget(self.calibrate)
        #: Shown instead of a recommendation when there is nothing to switch between.
        self.one_monitor = _muted(
            "With one monitor there is nothing to switch between, so no calibration is "
            "needed: walk-away detection, privacy mode and the shoulder guard work as they "
            "are. Connect a second monitor and calibrate from the tray menu."
        )
        self.one_monitor.setVisible(False)
        layout.addWidget(self.one_monitor)
        self.autostart = QCheckBox(f"Start {APP_NAME} when I log in")
        layout.addWidget(self.autostart)
        layout.addSpacing(round(10 * s))
        self.shortcuts = QLabel()
        self.shortcuts.setTextFormat(Qt.TextFormat.RichText)
        self.shortcuts.setWordWrap(True)
        layout.addWidget(self.shortcuts)
        layout.addStretch(1)
        layout.addWidget(_muted("Everything can be changed later under Settings."))


def _finish_subtitle() -> str:
    """Where the app lives from now on, and how its icon is used there."""
    if sys.platform == "darwin":
        return f"{APP_NAME} keeps running in the menu bar: click the eye icon for its menu."
    if sys.platform == "win32":
        # Windows 11 puts new tray icons in the hidden overflow area.
        return (
            f"{APP_NAME} keeps running in the system tray: click the eye icon for its menu. "
            "If you don't see it, click ^ on the taskbar."
        )
    return (
        f"{APP_NAME} keeps running in the system tray: right-click the eye icon for its "
        "menu, double-click it for the settings."
    )

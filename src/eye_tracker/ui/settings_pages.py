"""Page-building and per-page sync halves of :mod:`eye_tracker.ui.settings_dialog`.

Extracted from ``ui/settings_dialog.py`` (TD-CPLX-002): the generic page/binding builders and the
sync/apply halves of the Hotkeys, Camera and start-at-login pages. The mixins only call back into
the host dialog; this module must never import ``settings_dialog`` at runtime (TYPE_CHECKING only).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME
from ..config import describe_settings
from ..platform import autostart
from ..platform.hotkeys import parse_hotkey
from . import util
from .hotkey_edit import _Binding
from .settings_status import camera_in_use
from .util import DANGER

if TYPE_CHECKING:
    from PySide6.QtWidgets import QPushButton

    from .hotkey_edit import HotkeyEdit
    from .settings_status import CameraProbe

# The dialog's logger: the class split must not move the log records.
log = logging.getLogger("eye_tracker.ui.settings_dialog")

HOTKEY_ACTIONS: tuple[tuple[str, str], ...] = (
    ("toggle_tracking", "Pause / resume tracking"),
    ("toggle_privacy", "Privacy mode (camera off)"),
    ("recalibrate", "Recalibrate"),
)


_AUTOSTART_NOTES: dict[str, str] = {
    "stale": (
        f"The login item starts a copy of {APP_NAME} that no longer exists or will be gone "
        "after a restart. Tick the box and apply to point it at this copy."
    ),
    "other-profile": (
        f"The login item starts {APP_NAME} with another settings folder (--config-dir). "
        "Tick the box and apply to start this one instead."
    ),
}


_META: dict[str, dict[str, Any]] = {row["key"]: row for row in describe_settings()}


def _doc(key: str) -> str:
    return str(_META.get(key, {}).get("doc") or "")


@contextlib.contextmanager
def _blocked(widget: QObject) -> Iterator[None]:
    """Block a widget's signals for the duration of the ``with`` block."""
    previous = widget.blockSignals(True)
    try:
        yield
    finally:
        widget.blockSignals(previous)


class SettingsPageMixin:
    """The generic page, group, hint and binding builders for every settings page."""

    # The dialog state the mixins read or write (created by SettingsDialog.__init__/builders).
    _scale: float
    _bindings: dict[str, _Binding]
    _controller: object
    _hotkey_edits: dict[str, HotkeyEdit]
    _hotkey_errors: dict[str, str]
    _conflicts: dict[str, str | None]
    _hotkeys_suspended: bool
    _status_shows_hotkey_error: bool
    _hotkey_error: QLabel
    _hotkey_warning: QLabel
    _device: QComboBox
    _detect_button: QPushButton
    _camera_note: QLabel
    _backend_label: QLabel
    _camera_probe: CameraProbe
    _probe_skip: set[int]
    _autostart: QCheckBox
    _autostart_initial: bool
    _autostart_note: QLabel

    if TYPE_CHECKING:

        def _on_changed(self, *_args: object) -> None: ...
        def _set_status(self, text: str, color: str | None) -> None: ...

    def _page(self, title: str, subtitle: str) -> tuple[QScrollArea, QVBoxLayout]:
        s = self._scale
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(int(24 * s), int(20 * s), int(24 * s), int(16 * s))
        layout.setSpacing(int(14 * s))
        heading = QLabel(title)
        font = heading.font()
        font.setPointSizeF(font.pointSizeF() * 1.45)
        font.setWeight(font.Weight.DemiBold)
        heading.setFont(font)
        layout.addWidget(heading)
        sub = self._hint(subtitle)
        layout.addWidget(sub)
        scroll.setWidget(inner)
        return scroll, layout

    def _group(self, layout: QVBoxLayout, title: str) -> QFormLayout:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(int(16 * self._scale))
        form.setVerticalSpacing(int(8 * self._scale))
        layout.addWidget(box)
        return form

    def _hint(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _banner(self, text: str) -> QFrame:
        s = self._scale
        frame = QFrame()
        frame.setObjectName("banner")
        frame.setStyleSheet(
            "QFrame#banner { background: rgba(99, 102, 241, 0.10);"
            " border: 1px solid rgba(99, 102, 241, 0.35);"
            f" border-radius: {int(8 * s)}px; }}"
        )
        row = QHBoxLayout(frame)
        row.setContentsMargins(int(14 * s), int(10 * s), int(14 * s), int(10 * s))
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        # Banners can name commands to paste into the desktop's shortcut settings.
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(label, 1)
        return frame

    @staticmethod
    def _row_label(text: str, tooltip: str) -> QLabel:
        label = QLabel(text)
        label.setToolTip(tooltip)
        return label

    def _register(self, binding: _Binding, signal: Any) -> _Binding:
        tooltip = _doc(binding.key)
        if tooltip and not binding.widget.toolTip():
            binding.widget.setToolTip(tooltip)
        self._bindings[binding.key] = binding
        signal.connect(self._on_changed)
        return binding

    def _check(self, form: QFormLayout, key: str, text: str) -> QCheckBox:
        box = QCheckBox(text)

        def read() -> bool:
            return box.isChecked()

        def write(value: Any) -> None:
            box.setChecked(bool(value))

        self._register(_Binding(key, box, read, write, box.isChecked), box.toggled)
        form.addRow(box)
        return box

    def _combo(
        self,
        form: QFormLayout | None,
        key: str,
        label: str,
        options: Sequence[tuple[str, str]],
    ) -> QComboBox:
        combo = QComboBox()
        for value, text in options:
            combo.addItem(text, value)
        # Settings may contain a value unknown to this UI version; keep it selectable.
        for value in _META.get(key, {}).get("choices") or ():
            if combo.findData(value) < 0:
                combo.addItem(value, value)

        def read() -> Any:
            return combo.currentData()

        def write(value: Any) -> None:
            index = combo.findData(value)
            if index < 0:
                combo.addItem(str(value), value)
                index = combo.count() - 1
            combo.setCurrentIndex(index)

        self._register(
            _Binding(key, combo, read, write, combo.currentIndex), combo.currentIndexChanged
        )
        if form is not None:
            form.addRow(self._row_label(label, _doc(key)), combo)
        return combo

    def _int(
        self,
        form: QFormLayout | None,
        key: str,
        label: str,
        *,
        suffix: str = "",
        step: int = 1,
    ) -> QSpinBox:
        spin = QSpinBox()
        meta = _META.get(key, {})
        lo, hi = meta.get("lo"), meta.get("hi")
        spin.setRange(int(lo) if lo is not None else 0, int(hi) if hi is not None else 1_000_000)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        spin.setAccelerated(True)

        def write(value: Any) -> None:
            spin.setValue(int(value))

        self._register(_Binding(key, spin, spin.value, write, spin.value), spin.valueChanged)
        if form is not None:
            form.addRow(self._row_label(label, _doc(key)), spin)
        return spin

    def _float(
        self,
        form: QFormLayout | None,
        key: str,
        label: str,
        *,
        factor: float = 1.0,
        decimals: int = 1,
        step: float = 0.1,
        suffix: str = "",
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        meta = _META.get(key, {})
        lo, hi = meta.get("lo"), meta.get("hi")
        spin.setDecimals(decimals)
        spin.setRange(
            float(lo) * factor if lo is not None else 0.0,
            float(hi) * factor if hi is not None else 1e9,
        )
        spin.setSingleStep(step)
        spin.setSuffix(suffix)

        def read() -> float:
            return round(spin.value() / factor, decimals + 4)

        def write(value: Any) -> None:
            spin.setValue(float(value) * factor)

        self._register(_Binding(key, spin, read, write, spin.value), spin.valueChanged)
        if form is not None:
            form.addRow(self._row_label(label, _doc(key)), spin)
        return spin

    def _slider(
        self,
        form: QFormLayout,
        key: str,
        label: str,
        *,
        left: str,
        right: str,
    ) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setPageStep(10)
        value_label = QLabel()
        value_label.setMinimumWidth(int(40 * self._scale))

        def show(v: int) -> None:
            value_label.setText(f"{v} %")

        slider.valueChanged.connect(show)

        def read() -> float:
            return slider.value() / 100.0

        def write(value: Any) -> None:
            slider.setValue(round(float(value) * 100))
            show(slider.value())

        self._register(_Binding(key, slider, read, write, slider.value), slider.valueChanged)
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        low = self._hint(left)
        low.setWordWrap(False)
        high = self._hint(right)
        high.setWordWrap(False)
        lay.addWidget(low)
        lay.addWidget(slider, 1)
        lay.addWidget(high)
        lay.addWidget(value_label)
        form.addRow(self._row_label(label, _doc(key)), row)
        return slider

    def _lines(self, form: QFormLayout, key: str, label: str, placeholder: str) -> QPlainTextEdit:
        edit = QPlainTextEdit()
        edit.setPlaceholderText(placeholder)
        edit.setTabChangesFocus(True)
        edit.setFixedHeight(int(84 * self._scale))

        def read() -> list[str]:
            return [line.strip() for line in edit.toPlainText().splitlines() if line.strip()]

        def write(value: Any) -> None:
            edit.setPlainText("\n".join(str(v) for v in (value or [])))

        self._register(_Binding(key, edit, read, write, edit.toPlainText), edit.textChanged)
        form.addRow(self._row_label(label, _doc(key)), edit)
        return edit

    @staticmethod
    def _depends(master: QCheckBox, widgets: Sequence[QWidget]) -> None:
        def update(checked: bool) -> None:
            for widget in widgets:
                widget.setEnabled(checked)

        master.toggled.connect(update)
        update(master.isChecked())


class HotkeySyncMixin(SettingsPageMixin):
    """Recording, validation and suspension of the global shortcuts."""

    def _on_hotkey_recording(self, recording: bool) -> None:
        # Registered global hotkeys are swallowed by the OS before they reach this
        # dialog, so they are released while a shortcut is being recorded.
        if recording:
            self._suspend_hotkeys(True)
            return
        # Moving from one shortcut field to the next leaves the first (False)
        # just before entering the second (True). Resuming at once would
        # re-register every hotkey (repeating "Hotkey unavailable" for one that
        # fails) only to release them again, so it waits until focus settled.
        QTimer.singleShot(0, cast(QObject, self), self._resume_hotkeys_if_idle)

    def _resume_hotkeys_if_idle(self) -> None:
        """Give the hotkeys back unless a shortcut field is recording again."""
        if not any(edit.recording for edit in self._hotkey_edits.values()):
            self._resume_hotkeys()

    def _resume_hotkeys(self) -> None:
        """Give the hotkeys back now if this dialog released them."""
        self._suspend_hotkeys(False)

    def _suspend_hotkeys(self, suspended: bool) -> None:
        if suspended == self._hotkeys_suspended:
            return
        self._hotkeys_suspended = suspended
        suspend = getattr(self._controller, "suspend_hotkeys", None)
        if callable(suspend):
            try:
                suspend(suspended)
            except Exception:
                log.debug("Suspending hotkeys failed", exc_info=True)

    def _validate_hotkeys(self) -> None:
        errors: dict[str, str] = {}
        warnings: dict[str, str] = {}
        seen: dict[str, str] = {}
        labels = dict(HOTKEY_ACTIONS)
        enabled = self._bindings.get("hotkeys.enabled")
        # With global shortcuts off nothing is registered, and the fields cannot
        # be edited: a bad stored value must not block every other change.
        active = enabled is None or bool(enabled.read())
        for action, edit in self._hotkey_edits.items():
            text = edit.hotkey()
            if not text or not active:
                continue
            try:
                canonical = str(parse_hotkey(text))
            except ValueError as exc:
                errors[action] = f"{labels[action]}: {exc}"
                continue
            if canonical in seen:
                errors[action] = f"{labels[action]}: same shortcut as {labels[seen[canonical]]}"
                continue
            seen[canonical] = action
            conflict = self._layout_conflict(canonical)
            if conflict:
                warnings[action] = f"{labels[action]}: {conflict}. Choose another shortcut."
        self._hotkey_errors = errors
        for action, edit in self._hotkey_edits.items():
            edit.set_invalid(action in errors or action in warnings)
        self._hotkey_error.setText("\n".join(errors.values()))
        self._hotkey_error.setVisible(bool(errors))
        self._hotkey_warning.setText("\n".join(warnings.values()))
        self._hotkey_warning.setVisible(bool(warnings))

    def _layout_conflict(self, hotkey: str) -> str | None:
        """Why ``hotkey`` would swallow a character on an installed keyboard layout.

        Asks the controller's hotkey manager (``layout_conflict``, read-only);
        answers are cached for the life of the dialog, since the check reads
        every installed layout.
        """
        if hotkey not in self._conflicts:
            manager = getattr(self._controller, "hotkey_manager", None)
            check = getattr(manager, "layout_conflict", None)
            result: str | None = None
            if callable(check):
                try:
                    value = check(hotkey)
                    result = str(value) if value else None
                except Exception:
                    log.debug("layout_conflict(%s) failed", hotkey, exc_info=True)
            self._conflicts[hotkey] = result
        return self._conflicts[hotkey]

    def _show_hotkey_error(self) -> None:
        """Put the first shortcut error in the footer (cleared once all are valid)."""
        self._set_status(
            next(iter(self._hotkey_errors.values()), "Fix the highlighted shortcut first."),
            DANGER,
        )
        self._status_shows_hotkey_error = True


class CameraSyncMixin(SettingsPageMixin):
    """Camera probing, the device list and the live statistics of the camera page."""

    def detect_cameras(self) -> None:
        """Probe connected cameras in the background and fill the device list."""
        api = self._bindings["camera.api"].value() or "auto"
        self._probe_skip = camera_in_use(self._controller)
        if self._camera_probe.start(str(api), skip=self._probe_skip):
            self._detect_button.setEnabled(False)
            self._detect_button.setText("Detecting…")

    def _on_cameras_detected(self, cameras: object) -> None:
        self._detect_button.setEnabled(True)
        self._detect_button.setText("Detect cameras")
        current = self._device_value()
        found = list(cameras) if isinstance(cameras, list) else []
        with _blocked(self._device):
            self._device.clear()
            for info in found:
                index = getattr(info, "index", None)
                if index is None:
                    continue
                w, h = getattr(info, "width", 0), getattr(info, "height", 0)
                size = f" — {w}×{h}" if w and h else ""
                self._device.addItem(
                    f"{getattr(info, 'name', f'Camera {index}')}{size}", str(index)
                )
            for index in sorted(self._probe_skip):
                # Not probed, but it exists: keep it selectable.
                if self._device.findData(str(index)) < 0:
                    self._device.addItem(f"Camera {index} (in use)", str(index))
        self._select_device(current)
        other = "other " if self._probe_skip else ""
        parts = [f"Found {len(found)} {other}camera(s)." if found else f"No {other}camera found."]
        if self._probe_skip:
            parts.append("The camera in use is not probed.")
        self._camera_note.setText(" ".join(parts))
        self._camera_note.setVisible(True)
        self._on_changed()

    def _device_value(self) -> str:
        text = self._device.currentText().strip()
        index = self._device.currentIndex()
        if index >= 0 and self._device.itemText(index) == self._device.currentText():
            data = self._device.itemData(index)
            if isinstance(data, str) and data:
                return data
        return text

    def _select_device(self, value: str) -> None:
        with _blocked(self._device):
            index = self._device.findData(value)
            if index < 0:
                label = f"Camera {value}" if value.isdigit() else value
                self._device.addItem(label, value)
                index = self._device.count() - 1
            self._device.setCurrentIndex(index)
        # currentTextChanged was blocked; recompute the dirty state explicitly.
        self._on_changed()

    def _browse_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            cast(QWidget, self),
            "Choose a video or image",
            "",
            "Video or image (*.mp4 *.avi *.mkv *.mov *.webm *.png *.jpg *.jpeg *.bmp)"
            ";;All files (*)",
        )
        if path:
            self._select_device(path)

    def _available_backends(self) -> list[str] | None:
        try:
            from ..vision.backends import available_backends

            return list(available_backends())
        except Exception:
            log.debug("available_backends failed", exc_info=True)
            return None

    def _backend_info(self) -> tuple[str, str] | None:
        getter = getattr(self._controller, "backend_info", None)
        if getter is None:
            return None
        try:
            name, version = getter()
            return str(name), str(version)
        except Exception:
            return None

    def _refresh_backend_label(self) -> None:
        info = self._backend_info()
        self._backend_label.setText(f"Backend in use: {info[0]} ({info[1]})" if info else "")
        self._backend_label.setVisible(bool(info))


class AutostartSyncMixin(SettingsPageMixin):
    """Start-at-login state and the explanation under its checkbox."""

    @staticmethod
    def _autostart_supported() -> bool:
        try:
            return bool(autostart.is_supported())
        except Exception:
            return False

    @staticmethod
    def _read_autostart() -> bool:
        try:
            return bool(autostart.is_enabled())
        except Exception:
            log.debug("autostart.is_enabled failed", exc_info=True)
            return False

    def _apply_autostart(self) -> bool:
        wanted = self._autostart.isChecked()
        if wanted == self._autostart_initial:
            return True
        try:
            if wanted:
                autostart.enable()
            else:
                autostart.disable()
        except autostart.AutostartError as exc:
            # _autostart_initial keeps the OS state, so the change stays pending
            # (Apply enabled) and the next OK/Apply tries again.
            log.warning("Changing start at login failed: %s", exc)
            self._set_status(str(exc), DANGER)
            return False
        self._autostart_initial = wanted
        self._refresh_autostart_note()
        return True

    def _refresh_autostart_note(self) -> None:
        """Explain an unchecked box whose login item exists but does not start this copy."""
        status = util.autostart_status(autostart) if self._autostart_supported() else ""
        text = _AUTOSTART_NOTES.get(status, "")
        self._autostart_note.setText(text)
        self._autostart_note.setVisible(bool(text))

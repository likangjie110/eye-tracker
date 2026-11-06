"""Settings dialog: every option of :class:`~eye_tracker.config.Settings`, grouped by task.

The dialog edits a *copy* of the controller's settings. Nothing changes until
the user presses OK or Apply, which hands the copy to
``Controller.apply_settings``. Each widget is bound to one settings key through a
small binding object that remembers what was loaded, so

* a value the user did not touch is written back exactly as it was loaded (no
  float drift from spin-box rounding), and
* settings changed elsewhere while the dialog is open (tray menu, IPC) refresh
  the untouched widgets without discarding the user's pending edits.

Tooltips come from the field documentation in :func:`eye_tracker.config.describe_settings`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QFontDatabase,
    QGuiApplication,
    QHideEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, paths
from ..command_text import cli_command_text
from ..config import Settings
from ..update.fetch import supported as update_supported  # noqa: F401 - re-exported seam
from .hotkey_edit import (
    HotkeyEdit,
    _Binding,
    hotkey_text_from_event,
    modifier_preview,
)
from .icons import app_icon
from .settings_builders import (
    BACKEND_LABELS,
    _build_camera_capture,
    _build_camera_device,
    _build_camera_live,
    _build_camera_performance,
    _build_general_advanced,
    _build_general_calibration,
    _build_general_startup,
    _build_general_updates,
    _build_hotkeys_editors,
    _build_hotkeys_notes,
    _build_presence_privacy,
    _build_presence_walk_away,
    _build_switching_advanced,
    _build_switching_controls,
    _build_switching_options,
)
from .settings_pages import (
    HOTKEY_ACTIONS,
    AutostartSyncMixin,
    CameraSyncMixin,
    HotkeySyncMixin,
)
from .settings_status import (
    CameraProbe,
    _capabilities,
    camera_in_use,
    format_stats,
    load_diagnostics_text,
    platform_from_controller,
    probe_cameras,
    settings_from_controller,
)
from .util import ACCENT, DANGER, ui_scale

log = logging.getLogger(__name__)

__all__ = [
    "BACKEND_LABELS",
    "HOTKEY_ACTIONS",
    "CameraProbe",
    "HotkeyEdit",
    "SettingsDialog",
    "camera_in_use",
    "format_stats",
    "hotkey_text_from_event",
    "load_diagnostics_text",
    "modifier_preview",
    "platform_from_controller",
    "probe_cameras",
    "settings_from_controller",
]


# ============================================================ controller access


def _get(settings: Settings, key: str) -> Any:
    section, name = key.split(".", 1)
    return getattr(getattr(settings, section), name)


def _set(settings: Settings, key: str, value: Any) -> None:
    section, name = key.split(".", 1)
    setattr(getattr(settings, section), name, value)


class SettingsDialog(
    HotkeySyncMixin,
    CameraSyncMixin,
    AutostartSyncMixin,
    QDialog,
):
    """All user settings, grouped into pages with a sidebar.

    Args:
        controller: The app's ``Controller`` (``settings``, ``apply_settings``,
            ``stats_changed``, ``settings_changed``, ``backend_info``, ``calibration``,
            ``platform``).
        parent: Optional parent widget.

    Signals:
        calibration_requested: The user pressed "Recalibrate…".
        setup_requested: The user pressed "Run setup assistant…" (the app opens
            the first-run assistant right away).
        applied: Settings were handed to the controller (argument: the new ``Settings``).
    """

    calibration_requested = Signal()
    setup_requested = Signal()
    applied = Signal(object)

    PAGES: tuple[tuple[str, str, str], ...] = (
        ("general", "General", "Startup, notifications and the vision backend."),
        ("switching", "Switching", "When and how the cursor follows your gaze."),
        (
            "presence",
            "Presence & privacy",
            "Walk-away lock, camera privacy and the shoulder guard.",
        ),
        ("camera", "Camera & performance", "Capture device, frame rate and CPU use."),
        ("hotkeys", "Hotkeys", "Global shortcuts that work in every application."),
        ("diagnostics", "Diagnostics", "System report for bug reports and troubleshooting."),
    )

    # Widgets created by the module-level page builders below (declared so those builders
    # can assign them and the methods above can read them).
    _nav: QListWidget
    _stack: QStackedWidget
    _defaults_button: QPushButton
    _status: QLabel
    _buttons: QDialogButtonBox
    _calibration_status: QLabel
    _action_note: QLabel

    def __init__(self, controller: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._platform = platform_from_controller(controller)
        self._caps = _capabilities(self._platform)
        self._base = settings_from_controller(controller)
        self._bindings: dict[str, _Binding] = {}
        self._hotkey_edits: dict[str, HotkeyEdit] = {}
        self._hotkey_errors: dict[str, str] = {}
        #: layout_conflict() answers by canonical hotkey text.
        self._conflicts: dict[str, str | None] = {}
        #: The footer shows a hotkey error (cleared once the shortcuts are valid).
        self._status_shows_hotkey_error = False
        self._pages: dict[str, int] = {}
        self._diagnostics_loaded = False
        self._loading = False
        #: This dialog asked the controller to release the global hotkeys.
        self._hotkeys_suspended = False
        self._scale = ui_scale(self.screen() or QGuiApplication.primaryScreen())

        self.setWindowTitle(f"{APP_NAME} Settings")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(int(760 * self._scale), int(540 * self._scale))
        self.setSizeGripEnabled(True)

        self._camera_probe = CameraProbe(self)
        self._camera_probe.finished.connect(self._on_cameras_detected)
        #: Camera indices left out of the running probe (the one in use).
        self._probe_skip: set[int] = set()

        self._build()
        self._autostart_initial = self._read_autostart()
        self._autostart.setChecked(self._autostart_initial)
        self._refresh_autostart_note()
        self._load(self._base)

        self._connect_controller_signal("stats_changed", self._on_stats)
        self._connect_controller_signal("settings_changed", self._on_settings_changed)
        self._connect_controller_signal("state_changed", self._on_state_changed)
        self._refresh_calibration_status()
        self.show_page("general")

    # ============================================================== public API
    def settings(self) -> Settings:
        """The settings as currently shown (a new object; unexposed fields preserved)."""
        out = self._base.copy()
        for key, binding in self._bindings.items():
            _set(out, key, binding.value())
        return out

    def is_modified(self) -> bool:
        """Whether anything differs from the last loaded/applied state."""
        return self._autostart.isChecked() != self._autostart_initial or any(
            b.modified for b in self._bindings.values()
        )

    def is_valid(self) -> bool:
        return not self._hotkey_errors

    def bound_keys(self) -> list[str]:
        """Every ``section.field`` key that has a widget."""
        return list(self._bindings)

    def widget_for(self, key: str) -> QWidget:
        """The editor widget bound to ``key`` (for tests and focus handling)."""
        return self._bindings[key].widget

    def hotkey_edit(self, action: str) -> HotkeyEdit:
        return self._hotkey_edits[action]

    def hotkey_errors(self) -> dict[str, str]:
        return dict(self._hotkey_errors)

    def stats_text(self) -> str:
        return self._stats_label.text()

    def show_page(self, key: str) -> None:
        """Switch to a page by key (``"general"``, ``"hotkeys"``, …)."""
        row = self._pages.get(key)
        if row is not None:
            self._nav.setCurrentRow(row)

    def current_page(self) -> str:
        row = self._nav.currentRow()
        return next((k for k, r in self._pages.items() if r == row), "")

    def apply(self) -> bool:
        """Validate and hand the settings to the controller. Returns ``False`` if invalid."""
        if not self.is_valid():
            self._show_hotkey_error()
            return False
        new = self.settings()
        if new.to_dict() != self._base.to_dict():
            try:
                self._controller.apply_settings(new)  # type: ignore[attr-defined]
            except Exception as exc:
                log.exception("Applying settings failed")
                self._set_status(f"Could not apply the settings: {exc}", DANGER)
                return False
            self.applied.emit(new)
        # Reload from the controller so values it adjusted (clamped) are shown and the
        # new state becomes the baseline for "modified".
        current = new
        if hasattr(self._controller, "settings"):
            current = settings_from_controller(self._controller)
        autostart_ok = self._apply_autostart()
        self._load(current)
        if autostart_ok:
            self._set_status("", None)
        return autostart_ok

    def restore_defaults(self) -> None:
        """Show default values (still to be applied).

        Settings without a widget, such as the first-run flag, keep their value.
        """
        defaults = Settings()
        for key, binding in self._bindings.items():
            binding.write(_get(defaults, key))
        self._on_changed()

    # ============================================================== QDialog
    def accept(self) -> None:
        if self.apply():
            super().accept()

    def hideEvent(self, event: QHideEvent) -> None:
        # Closed or hidden while a shortcut field had focus: give the hotkeys
        # back now. The deferred resume of _on_hotkey_recording would never run
        # once the dialog is deleted.
        super().hideEvent(event)
        self._resume_hotkeys()

    # ============================================================== building
    def _build(self) -> None:
        s = self._scale
        root, body = _build_dialog_layout(self)
        self._nav = _build_dialog_navigation(self, body, s)
        self._stack, self._pages = _build_dialog_pages(self, body, self._nav)
        self._defaults_button, self._status, self._buttons = _build_dialog_footer(self, root, s)

    # -------------------------------------------------------------- pages
    def _build_general(self, layout: QVBoxLayout) -> None:
        self._autostart, self._autostart_note = _build_general_startup(self, layout)
        _build_general_updates(self, layout)
        self._calibration_status = _build_general_calibration(self, layout)
        _build_general_advanced(self, layout)

    def _build_switching(self, layout: QVBoxLayout) -> None:
        enabled, target, focus = _build_switching_options(self, layout)
        controls = _build_switching_controls(self, layout)
        self._depends(enabled, [target, focus, *controls])
        _build_switching_advanced(self, layout)

    def _build_presence(self, layout: QVBoxLayout) -> None:
        self._action_note = _build_presence_walk_away(self, layout)
        _build_presence_privacy(self, layout)

    def _build_camera(self, layout: QVBoxLayout) -> None:
        form, self._device, self._detect_button = _build_camera_device(self, layout)
        self._camera_note = _build_camera_capture(self, form)
        _build_camera_performance(self, layout)
        self._stats_label, self._backend_label = _build_camera_live(self, layout)
        self._refresh_backend_label()

    def _build_hotkeys(self, layout: QVBoxLayout) -> None:
        form, enabled, edits = _build_hotkeys_editors(self, layout)
        self._hotkey_error, self._hotkey_warning = _build_hotkeys_notes(
            self, layout, form, enabled, edits, cli_command_text
        )

    def _build_diagnostics(self, layout: QVBoxLayout) -> None:
        self._diagnostics = QPlainTextEdit()
        self._diagnostics.setReadOnly(True)
        self._diagnostics.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._diagnostics.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._diagnostics.setMinimumHeight(int(300 * self._scale))
        self._diagnostics.setPlaceholderText("Collecting…")
        layout.addWidget(self._diagnostics, 1)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_diagnostics)
        copy = QPushButton("Copy report")
        copy.setToolTip("Copy the report to paste into a bug report.")
        copy.clicked.connect(self._copy_diagnostics)
        logs = QPushButton("Open log folder")
        logs.clicked.connect(self._open_log_folder)
        row.addWidget(refresh)
        row.addWidget(copy)
        row.addStretch(1)
        row.addWidget(logs)
        layout.addLayout(row)
        layout.addWidget(self._hint("The report contains no images and nothing is sent anywhere."))

    # ============================================================== loading
    def _load(self, settings: Settings) -> None:
        self._loading = True
        try:
            self._base = settings.copy()
            for key, binding in self._bindings.items():
                binding.load(_get(settings, key))
            # The start-at-login baseline is not reset here: it is what the OS
            # has (read at start, updated only by a successful change), so a
            # change that failed stays pending and OK/Apply retry it.
        finally:
            self._loading = False
        self._on_changed()

    # ============================================================== slots
    def _on_changed(self, *_args: object) -> None:
        if self._loading:
            return
        self._validate_hotkeys()
        self._update_action_note()
        valid = self.is_valid()
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        apply_button = self._buttons.button(QDialogButtonBox.StandardButton.Apply)
        if ok is not None:
            ok.setEnabled(valid)
        if apply_button is not None:
            apply_button.setEnabled(valid and self.is_modified())
        # The error label is on the Hotkeys page; the footer says why OK is off
        # whichever page is shown.
        if not valid:
            self._show_hotkey_error()
        elif self._status_shows_hotkey_error:
            self._set_status("", None)

    def _on_button(self, button: QAbstractButton) -> None:
        if self._buttons.buttonRole(button) == QDialogButtonBox.ButtonRole.ApplyRole:
            self.apply()

    def _on_page_changed(self, row: int) -> None:
        self._stack.setCurrentIndex(row)
        if row == self._pages.get("diagnostics") and not self._diagnostics_loaded:
            self.refresh_diagnostics()

    def _on_stats(self, stats: object) -> None:
        text = format_stats(stats)
        if text:
            self._stats_label.setText(text)

    def _on_state_changed(self, _state: object) -> None:
        # Calibration saved or invalidated (e.g. new monitor layout).
        self._refresh_calibration_status()

    def _on_settings_changed(self, settings: object) -> None:
        """Settings changed elsewhere: refresh the widgets the user has not touched."""
        if not isinstance(settings, Settings):
            return
        self._loading = True
        try:
            self._base = settings.copy()
            for key, binding in self._bindings.items():
                if not binding.modified:
                    binding.load(_get(settings, key))
        finally:
            self._loading = False
        self._on_changed()
        self._refresh_calibration_status()
        self._refresh_backend_label()

    # ============================================================== misc
    def _connect_controller_signal(self, name: str, slot: Callable[[object], None]) -> None:
        signal = getattr(self._controller, name, None)
        if signal is None:
            return
        try:
            signal.connect(slot)
        except (AttributeError, TypeError, RuntimeError):
            log.debug("Controller signal %s not connectable", name, exc_info=True)

    def _set_status(self, text: str, color: str | None) -> None:
        self._status_shows_hotkey_error = False
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {color};" if color else "")

    def _refresh_calibration_status(self) -> None:
        getter = getattr(self._controller, "calibration", None)
        data = None
        if callable(getter):
            try:
                data = getter()
            except Exception:
                log.debug("controller.calibration() failed", exc_info=True)
        if data is None:
            self._calibration_status.setText("Not calibrated yet.")
            return
        grade = getattr(data, "grade", None)
        created = str(getattr(data, "created_at", "") or "")[:10]
        parts = ["Calibrated"]
        if created:
            parts.append(f"on {created}")
        text = " ".join(parts)
        if grade:
            text += f" · quality: {grade}"
        text += "."
        # A stored calibration may no longer fit (new monitor layout, other backend).
        if getattr(self._controller, "is_calibrated", True) is False:
            reason = getattr(self._controller, "calibration_reason", "")
            if isinstance(reason, str) and reason:
                text += f" Needs recalibration: {reason}."
            else:
                text += " Needs recalibration."
        self._calibration_status.setText(text)

    def _update_action_note(self, *_args: object) -> None:
        binding = self._bindings.get("presence.action")
        if binding is None:
            return
        action = binding.read()
        needs = {
            "lock": ["lock"],
            "display_off": ["display_off"],
            "lock_and_display_off": ["lock", "display_off"],
        }.get(str(action), [])
        missing = [n for n in needs if not self._caps.get(n, True)]
        if missing:
            what = " and ".join(
                "locking" if n == "lock" else "turning displays off" for n in missing
            )
            self._action_note.setText(
                f"This system does not support {what}; a notification is shown instead."
            )
        self._action_note.setVisible(bool(missing))

    def refresh_diagnostics(self) -> None:
        """(Re)build the diagnostics report."""
        self._diagnostics_loaded = True
        # The report says which hotkeys are registered. Clicking from a shortcut
        # field to this page has only scheduled giving them back (see
        # _on_hotkey_recording); do it first, or every hotkey would look refused.
        self._resume_hotkeys_if_idle()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            text = load_diagnostics_text(self._controller)
        except Exception as exc:
            log.warning("Diagnostics report failed", exc_info=True)
            text = f"Could not collect the diagnostics report: {exc}"
        finally:
            QGuiApplication.restoreOverrideCursor()
        self._diagnostics.setPlainText(text)

    def diagnostics_text(self) -> str:
        return self._diagnostics.toPlainText()

    def _copy_diagnostics(self) -> None:
        if not self._diagnostics_loaded:
            self.refresh_diagnostics()
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._diagnostics.toPlainText())
            self._set_status("Report copied to the clipboard.", None)

    def _open_log_folder(self) -> None:
        try:
            folder = paths.log_dir()
        except OSError as exc:
            self._set_status(f"Log folder unavailable: {exc}", DANGER)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self._set_status(f"Logs are in {folder}", None)


# ---------------------------------------------------------------- page builders
# The bodies of the page builders above live here so the class stays small; the thin
# methods on SettingsDialog create the widgets in the same order and assign them.


def _build_dialog_layout(dialog: SettingsDialog) -> tuple[QVBoxLayout, QHBoxLayout]:
    root = QVBoxLayout(dialog)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)

    body = QHBoxLayout()
    body.setContentsMargins(0, 0, 0, 0)
    body.setSpacing(0)
    root.addLayout(body, 1)
    return root, body


def _build_dialog_navigation(dialog: SettingsDialog, body: QHBoxLayout, s: float) -> QListWidget:
    nav = QListWidget()
    nav.setObjectName("settingsNav")
    nav.setFrameShape(QFrame.Shape.NoFrame)
    nav.setFixedWidth(int(200 * s))
    nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    nav.setStyleSheet(
        f"""
            QListWidget#settingsNav {{
                padding: {int(10 * s)}px {int(8 * s)}px;
                background: palette(alternate-base);
                outline: 0;
            }}
            QListWidget#settingsNav::item {{
                padding: {int(8 * s)}px {int(10 * s)}px;
                margin: {int(1 * s)}px 0;
                border-radius: {int(6 * s)}px;
            }}
            QListWidget#settingsNav::item:hover:!selected {{
                background: rgba(127, 127, 127, 0.10);
            }}
            QListWidget#settingsNav::item:selected {{
                background: rgba(99, 102, 241, 0.18);
                color: palette(text);
                border-left: {max(2, int(3 * s))}px solid {ACCENT};
            }}
            """
    )
    body.addWidget(nav)
    return nav


def _build_dialog_pages(
    dialog: SettingsDialog, body: QHBoxLayout, nav: QListWidget
) -> tuple[QStackedWidget, dict[str, int]]:
    stack = QStackedWidget()
    body.addWidget(stack, 1)

    pages: dict[str, int] = {}
    builders: dict[str, Callable[[QVBoxLayout], None]] = {
        "general": dialog._build_general,
        "switching": dialog._build_switching,
        "presence": dialog._build_presence,
        "camera": dialog._build_camera,
        "hotkeys": dialog._build_hotkeys,
        "diagnostics": dialog._build_diagnostics,
    }
    for key, title, subtitle in dialog.PAGES:
        page, layout = dialog._page(title, subtitle)
        builders[key](layout)
        layout.addStretch(1)
        pages[key] = stack.addWidget(page)
        nav.addItem(QListWidgetItem(title))
    nav.currentRowChanged.connect(dialog._on_page_changed)
    return stack, pages


def _build_dialog_footer(
    dialog: SettingsDialog, root: QVBoxLayout, s: float
) -> tuple[QPushButton, QLabel, QDialogButtonBox]:
    # Footer: status line + buttons.
    footer = QFrame()
    footer.setFrameShape(QFrame.Shape.NoFrame)
    foot = QHBoxLayout(footer)
    foot.setContentsMargins(int(16 * s), int(10 * s), int(16 * s), int(14 * s))
    defaults_button = QPushButton("Restore defaults")
    defaults_button.setToolTip("Show the default value of every setting (Apply to keep).")
    defaults_button.clicked.connect(dialog.restore_defaults)
    foot.addWidget(defaults_button)
    status = QLabel()
    status.setWordWrap(True)
    foot.addWidget(status, 1)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok
        | QDialogButtonBox.StandardButton.Cancel
        | QDialogButtonBox.StandardButton.Apply
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    buttons.clicked.connect(dialog._on_button)
    foot.addWidget(buttons)
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    root.addWidget(line)
    root.addWidget(footer)
    return defaults_button, status, buttons

"""Page builders for :mod:`eye_tracker.ui.settings_dialog` (TD-CPLX-002).

The six settings pages are built here as plain functions so the dialog class keeps only thin
methods that call them in the original widget order. Nothing here imports ``settings_dialog`` at
runtime: the host dialog is a type hint only (TYPE_CHECKING).
"""

from __future__ import annotations

import html
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME
from .hotkey_edit import HotkeyEdit, _Binding
from .settings_pages import HOTKEY_ACTIONS, _doc
from .util import DANGER, WARNING

if TYPE_CHECKING:
    from .settings_dialog import SettingsDialog


def _hotkey_writer(edit: HotkeyEdit) -> Callable[[Any], None]:
    def write(value: Any) -> None:
        edit.set_hotkey(str(value or ""))

    return write


#: Vision backends offered in the settings (``general.backend`` values), best first.
BACKEND_LABELS: dict[str, str] = {
    "auto": "Automatic (recommended)",
    "facemesh": "Face mesh — head pose + eye direction (most accurate)",
    "lite": "Lite — head pose from face geometry only (lightest)",
}


# ---------------------------------------------------------------- General page
def _build_general_startup(dialog: SettingsDialog, layout: QVBoxLayout) -> tuple[QCheckBox, QLabel]:
    form = dialog._group(layout, "Startup")
    autostart = QCheckBox(f"Start {APP_NAME} when I log in")
    supported = dialog._autostart_supported()
    autostart.setEnabled(supported)
    autostart.setToolTip(
        "Runs quietly in the tray after you log in."
        if supported
        else "Start at login is not supported on this system."
    )
    autostart.toggled.connect(dialog._on_changed)
    form.addRow(autostart)
    autostart_note = dialog._hint("")
    autostart_note.setVisible(False)
    form.addRow(autostart_note)
    dialog._check(form, "general.start_paused", "Start with tracking paused")
    # Opens the assistant now. A "show it at next start" switch would have
    # to clear general.first_run_done, and until the assistant is finished
    # that flag also turns the walk-away lock into a mere notification.
    setup = QPushButton("Run setup assistant…")
    setup.setToolTip(
        "Check the camera and choose what happens when you walk away, step by step. "
        "Cancelling it changes nothing."
    )
    setup.clicked.connect(dialog.setup_requested)
    form.addRow(setup)

    form = dialog._group(layout, "Interface")
    dialog._check(form, "general.notifications", "Show notifications")
    dialog._check(form, "ui.show_gaze_overlay", "Show a dot where I am looking (testing aid)")
    return autostart, autostart_note


def _build_general_updates(
    dialog: SettingsDialog, layout: QVBoxLayout, supported_fn: Callable[[], bool]
) -> None:
    form = dialog._group(layout, "Updates")
    updates = dialog._check(form, "updates.check", "Look for a new version once a day")
    supported = supported_fn()
    updates.setEnabled(supported)
    form.addRow(
        dialog._hint(
            "Off by default: then the app never uses the network. It only tells you "
            "when there is a new version and installs it when you agree. "
            "“Check for updates…” in the tray menu works either way."
            if supported
            else "Checking for updates is not available on this system yet: nothing uses "
            "the network. Take new versions from the releases page."
        )
    )


def _build_general_calibration(dialog: SettingsDialog, layout: QVBoxLayout) -> QLabel:
    form = dialog._group(layout, "Calibration")
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    calibration_status = QLabel()
    calibration_status.setWordWrap(True)
    lay.addWidget(calibration_status, 1)
    recalibrate = QPushButton("Recalibrate…")
    recalibrate.setToolTip("Run the calibration again (about 30 seconds).")
    recalibrate.clicked.connect(dialog.calibration_requested)
    lay.addWidget(recalibrate)
    form.addRow(row)

    return calibration_status


def _build_general_advanced(dialog: SettingsDialog, layout: QVBoxLayout) -> None:
    form = dialog._group(layout, "Advanced")
    available = dialog._available_backends()
    options = []
    for value, label in BACKEND_LABELS.items():
        missing = available is not None and value != "auto" and value not in available
        options.append((value, f"{label} — not available" if missing else label))
    dialog._combo(form, "general.backend", "Vision backend", options)
    form.addRow(
        dialog._hint(
            "Face mesh follows your head and your eyes; Lite follows only the head, "
            "for slow computers. Changing the backend requires a new calibration."
        )
    )
    dialog._combo(
        form,
        "general.log_level",
        "Log level",
        [("DEBUG", "Debug"), ("INFO", "Info"), ("WARNING", "Warning"), ("ERROR", "Error")],
    )


# ---------------------------------------------------------------- Switching page
def _build_switching_options(
    dialog: SettingsDialog, layout: QVBoxLayout
) -> tuple[QCheckBox, QComboBox, QCheckBox]:
    form = dialog._group(layout, "Switching")
    enabled = dialog._check(form, "switching.enabled", "Move the cursor to the monitor I look at")
    target = dialog._combo(
        form,
        "switching.cursor_target",
        "Cursor lands",
        [
            ("last", "Where it was last on that monitor"),
            ("center", "In the centre of the monitor"),
            ("gaze", "Where I am looking (estimated)"),
        ],
    )
    focus = dialog._check(
        form, "switching.focus_window", "Also give keyboard focus to that monitor"
    )
    if not dialog._caps.get("focus", True):
        focus.setText(focus.text() + " (not supported on this system)")

    return enabled, target, focus


def _build_switching_controls(dialog: SettingsDialog, layout: QVBoxLayout) -> list[QWidget]:
    timing = dialog._group(layout, "Timing")
    dwell = dialog._int(timing, "switching.dwell_ms", "Look for at least", suffix=" ms", step=50)
    cooldown = dialog._int(
        timing, "switching.cooldown_ms", "Pause between switches", suffix=" ms", step=100
    )
    mouse = dialog._int(
        timing, "switching.mouse_grace_ms", "Wait after mouse use", suffix=" ms", step=250
    )
    typing = dialog._int(
        timing, "switching.typing_grace_ms", "Wait after typing", suffix=" ms", step=250
    )
    reading = dialog._int(
        timing,
        "switching.reading_grace_ms",
        "… while reading another monitor",
        suffix=" ms",
        step=500,
    )

    tuning = dialog._group(layout, "Sensitivity")
    smoothing = dialog._slider(
        tuning, "switching.smoothing", "Smoothing", left="Responsive", right="Steady"
    )
    hysteresis = dialog._float(
        tuning,
        "switching.hysteresis",
        "Cross into a monitor by",
        factor=100.0,
        decimals=0,
        step=1.0,
        suffix=" % of its size",
    )
    margin = dialog._float(
        tuning,
        "switching.off_screen_margin",
        "Ignore gaze beyond",
        factor=100.0,
        decimals=0,
        step=5.0,
        suffix=" % of the diagonal",
    )
    return [dwell, cooldown, mouse, typing, reading, smoothing, hysteresis, margin]


def _build_switching_advanced(dialog: SettingsDialog, layout: QVBoxLayout) -> None:
    panes = dialog._group(layout, "Split panes (experimental)")
    follow = dialog._check(
        panes, "panes.enabled", "Also move keyboard focus to the split pane I look at"
    )
    if not dialog._caps.get("panes", True):
        follow.setText(follow.text() + " (not supported on this system)")
    move = dialog._check(
        panes, "panes.move_cursor", "Also move the cursor into that pane, where I left it"
    )
    precision = dialog._float(
        panes,
        "panes.precision",
        "Only panes at least",
        decimals=1,
        step=0.5,
        suffix=" × my gaze error",
    )
    panes.addRow(
        dialog._hint(
            "Works with tmux, WezTerm and Windows Terminal panes on the monitor you are on. "
            "Small panes are left alone: how small depends on how accurate your calibration "
            "is."
        )
    )
    desktop = dialog._check(
        panes,
        "panes.desktop_apps",
        "Also switch between side-by-side sessions in the Claude and ChatGPT apps",
    )
    desktop_hint = dialog._hint(
        "Two Claude chats, or a ChatGPT or Codex conversation and its side chat: the one you "
        "look at gets the focus in its message box. Windows only. Asks the app for its "
        "accessibility tree, which can cost the app some CPU and memory."
    )
    panes.addRow(desktop_hint)
    dialog._depends(follow, [move, precision, desktop, desktop_hint])

    learn = dialog._group(layout, "Adaptive accuracy")
    adaptive = dialog._check(learn, "learning.adaptive", "Learn from how I use the mouse")
    max_samples = dialog._int(
        learn, "learning.max_samples", "Keep at most", suffix=" samples", step=50
    )
    dialog._depends(adaptive, [max_samples])
    dialog._check(learn, "learning.drift_alerts", "Suggest recalibrating when accuracy drops")


# ---------------------------------------------------------------- Presence page
def _build_presence_walk_away(dialog: SettingsDialog, layout: QVBoxLayout) -> QLabel:
    layout.addWidget(
        dialog._banner(
            "<b>Everything stays on this computer.</b> Camera frames are analysed in "
            "memory and never saved or sent anywhere. Privacy mode turns the camera off "
            "completely."
        )
    )
    form = dialog._group(layout, "When I walk away")
    enabled = dialog._check(form, "presence.enabled", "React when I leave the computer")
    action = dialog._combo(
        form,
        "presence.action",
        "Then",
        [
            ("lock", "Lock the computer"),
            ("lock_and_display_off", "Lock and turn the displays off"),
            ("display_off", "Turn the displays off"),
            ("notify", "Only show a notification"),
            ("none", "Do nothing"),
        ],
    )
    action_note = dialog._hint("")
    action_note.setVisible(False)
    form.addRow(action_note)
    action.currentIndexChanged.connect(dialog._update_action_note)
    timeout = dialog._int(form, "presence.away_timeout_s", "After", suffix=" s away", step=5)
    warning = dialog._int(form, "presence.warning_s", "Countdown", suffix=" s", step=1)
    idle = dialog._check(
        form, "presence.require_input_idle", "Keyboard or mouse use counts as being here"
    )
    if not dialog._caps.get("input_idle", True):
        # Wayland outside GNOME: only the camera can cancel the countdown.
        idle.setText(idle.text() + " (not supported on this system)")
    wake = dialog._check(form, "presence.wake_on_return", "Turn the displays back on when I return")
    dialog._depends(enabled, [action, timeout, warning, idle, wake])

    return action_note


def _build_presence_privacy(dialog: SettingsDialog, layout: QVBoxLayout) -> None:
    form = dialog._group(layout, "Camera privacy")
    dialog._check(
        form,
        "privacy.remember_privacy_mode",
        "Keep privacy mode on after a restart (the camera stays off)",
    )
    dialog._check(
        form, "privacy.pause_when_locked", "Release the camera while the screen is locked"
    )
    yield_box = dialog._check(
        form, "privacy.yield_camera", "Release the camera while another app uses it"
    )
    if not dialog._caps.get("camera_in_use", True):
        yield_box.setText(yield_box.text() + " (not supported on this system)")
    dialog._lines(
        form,
        "privacy.pause_for_apps",
        "Pause while these apps run",
        "One program per line, e.g.\nzoom.exe\nobs64.exe",
    )

    form = dialog._group(layout, "Shoulder guard")
    guard = dialog._check(
        form, "privacy.shoulder_guard", "React when someone looks over my shoulder"
    )
    guard_action = dialog._combo(
        form,
        "privacy.guard_action",
        "Then",
        [
            ("curtain", "Cover the screens"),
            ("notify", "Only show a notification"),
            ("lock", "Lock the computer"),
        ],
    )
    delay = dialog._float(form, "privacy.guard_delay_s", "After", decimals=1, step=0.5, suffix=" s")
    dialog._depends(guard, [guard_action, delay])


# ---------------------------------------------------------------- Camera page
def _build_camera_device(
    dialog: SettingsDialog, layout: QVBoxLayout
) -> tuple[QFormLayout, QComboBox, QPushButton]:
    form = dialog._group(layout, "Camera")
    device = QComboBox()
    device.setEditable(True)
    device.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    device.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    device_line = device.lineEdit()
    if device_line is not None:
        device_line.setPlaceholderText("Camera number or a video file")

    def read_device() -> str:
        return dialog._device_value()

    def write_device(value: Any) -> None:
        dialog._select_device(str(value))

    dialog._register(
        _Binding("camera.device", device, read_device, write_device, read_device),
        device.currentTextChanged,
    )
    device_row = QWidget()
    lay = QHBoxLayout(device_row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(device, 1)
    detect_button = QPushButton("Detect cameras")
    detect_button.setToolTip("Look for connected cameras (each may briefly turn on).")
    detect_button.clicked.connect(dialog.detect_cameras)
    lay.addWidget(detect_button)
    browse = QToolButton()
    browse.setText("…")
    browse.setToolTip("Use a video or image file instead of a camera (for testing).")
    browse.clicked.connect(dialog._browse_video)
    lay.addWidget(browse)
    form.addRow(dialog._row_label("Device", _doc("camera.device")), device_row)
    return form, device, detect_button


def _build_camera_capture(dialog: SettingsDialog, form: QFormLayout) -> QLabel:
    size_row = QWidget()
    lay = QHBoxLayout(size_row)
    lay.setContentsMargins(0, 0, 0, 0)
    width = dialog._int(None, "camera.width", "", suffix=" px", step=160)
    height = dialog._int(None, "camera.height", "", suffix=" px", step=120)
    lay.addWidget(width, 1)
    lay.addWidget(QLabel("×"))
    lay.addWidget(height, 1)
    form.addRow(dialog._row_label("Resolution", "Requested capture size."), size_row)
    form.addRow(dialog._hint("640 × 480 is plenty: frames are scaled down before analysis."))
    dialog._combo(
        form,
        "camera.api",
        "Capture API",
        [
            ("auto", "Automatic"),
            ("dshow", "DirectShow (Windows)"),
            ("msmf", "Media Foundation (Windows)"),
            ("avfoundation", "AVFoundation (macOS)"),
            ("v4l2", "Video4Linux2 (Linux)"),
            ("any", "Any available"),
        ],
    )
    camera_note = dialog._hint("")
    camera_note.setVisible(False)
    form.addRow(camera_note)
    return camera_note


def _build_camera_performance(dialog: SettingsDialog, layout: QVBoxLayout) -> None:
    form = dialog._group(layout, "Performance")
    options = []
    profile_text = {
        "eco": "Eco — lowest CPU",
        "balanced": "Balanced (recommended)",
        "responsive": "Responsive — fastest switching",
    }
    try:
        from ..engine.scheduler import PROFILES
    except Exception:  # pragma: no cover - engine is part of the package
        PROFILES = {}
    for value in ("eco", "balanced", "responsive"):
        text = profile_text[value]
        fps = PROFILES.get(value, {}).get("active")
        if fps:
            text += f", up to {fps:g} fps"
        options.append((value, text))
    dialog._combo(form, "performance.profile", "Profile", options)
    gate = dialog._check(
        form, "performance.motion_gate", "Skip analysis while the picture is still"
    )
    threshold = dialog._float(
        form, "performance.motion_threshold", "Motion threshold", decimals=1, step=0.5
    )
    dialog._depends(gate, [threshold])


def _build_camera_live(dialog: SettingsDialog, layout: QVBoxLayout) -> tuple[QLabel, QLabel]:
    form = dialog._group(layout, "Live")
    stats_label = QLabel("Waiting for statistics…")
    stats_label.setWordWrap(True)
    stats_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    form.addRow(stats_label)
    backend_label = dialog._hint("")
    form.addRow(backend_label)
    return stats_label, backend_label


# ---------------------------------------------------------------- Hotkeys page
def _build_hotkeys_editors(
    dialog: SettingsDialog, layout: QVBoxLayout
) -> tuple[QFormLayout, QCheckBox, list[QWidget]]:
    form = dialog._group(layout, "Global shortcuts")
    enabled = dialog._check(form, "hotkeys.enabled", "Enable global shortcuts")
    edits: list[QWidget] = []
    for action, label in HOTKEY_ACTIONS:
        key = f"hotkeys.{action}"
        edit = HotkeyEdit()
        edit.setToolTip(f"{_doc(key)}\nClick, then press the shortcut. Backspace clears it.")
        edit.recording_changed.connect(dialog._on_hotkey_recording)
        dialog._hotkey_edits[action] = edit
        dialog._register(
            _Binding(key, edit, edit.hotkey, _hotkey_writer(edit), edit.hotkey),
            edit.hotkey_changed,
        )
        clear = QToolButton()
        clear.setText("Clear")
        clear.setToolTip("Remove this shortcut.")
        clear.clicked.connect(lambda _=False, e=edit: e.set_hotkey(""))
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(edit, 1)
        lay.addWidget(clear)
        form.addRow(dialog._row_label(label, _doc(key)), row)
        edits.append(row)
    return form, enabled, edits


def _build_hotkeys_notes(
    dialog: SettingsDialog,
    layout: QVBoxLayout,
    form: QFormLayout,
    enabled: QCheckBox,
    edits: list[QWidget],
    command_text: Callable[..., str],
) -> tuple[QLabel, QLabel]:
    hotkey_error = QLabel()
    hotkey_error.setWordWrap(True)
    hotkey_error.setStyleSheet(f"color: {DANGER};")
    hotkey_error.setVisible(False)
    form.addRow(hotkey_error)
    # Valid shortcuts the OS will refuse (they type a character, e.g. AltGr).
    hotkey_warning = QLabel()
    hotkey_warning.setWordWrap(True)
    hotkey_warning.setStyleSheet(f"color: {WARNING};")
    hotkey_warning.setVisible(False)
    form.addRow(hotkey_warning)
    dialog._depends(enabled, edits)

    tip = (
        "Shortcuts need at least one of Ctrl, Alt or "
        + ("Cmd" if sys.platform == "darwin" else "Win" if sys.platform == "win32" else "Super")
        + ". Shift alone only works with function keys."
    )
    form.addRow(dialog._hint(tip))
    # The controller's manager knows best (e.g. XWayland limits); before it
    # exists fall back to the platform's capability matrix.
    manager = getattr(dialog._controller, "hotkey_manager", None)
    supported = bool(getattr(manager, "supported", dialog._caps.get("hotkeys", True)))
    note = getattr(manager, "note", None)
    note = note if isinstance(note, str) and note.strip() else None
    if not supported:
        note = note or "Global shortcuts are not available in this desktop session."
        text = html.escape(note)
        # Named as this copy is run (the .AppImage file, eye-tracker-cli, …):
        # the packages put no "eye-tracker" command on the PATH, and a
        # desktop shortcut bound to a missing command fails silently. Added
        # unless the note already names the commands that way.
        if command_text("ctl") not in note:
            commands = " · ".join(
                f"<code>{html.escape(command_text('ctl', action))}</code>"
                for action in ("toggle", "privacy-toggle", "calibrate")
            )
            text += (
                "<br>Bind these commands in your desktop's keyboard settings instead: " + commands
            )
        layout.addWidget(dialog._banner(text))
    elif note:
        layout.addWidget(dialog._banner(html.escape(note)))
    return hotkey_error, hotkey_warning

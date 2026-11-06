"""Rule checks behind the ``doctor`` problem list (TD-CPLX-008).

Extracted from :mod:`eye_tracker.diagnostics`: one small function per report dimension (sections,
libraries, backends, monitors, cameras, platform, hotkeys, autostart, settings) plus the thin
:func:`problems` aggregator. Diagnostics keeps the calibration rule, which needs its redacted
command text, and combines both halves in the same order as before.
"""

from __future__ import annotations

from typing import Any

#: Distributions Eye Tracker never uses that are a problem when installed next to
#: it, with the reason. The MediaPipe runtime is not needed (its models run in
#: OpenCV) and ships a usage logger (telemetry), which the offline rule excludes.
_UNWANTED_DISTRIBUTIONS: dict[str, str] = {
    "mediapipe": "Eye Tracker does not use it, and it contains a usage logger (telemetry)",
}


def _instance_running(report: dict[str, Any]) -> bool:
    return bool((report.get("app") or {}).get("instance_running"))


def problems(report: dict[str, Any]) -> list[str]:
    """Problem messages for every report dimension except calibration."""
    return [
        *_section_error_problems(report),
        *_library_problems(report),
        *_backend_problems(report),
        *_monitor_problems(report),
        *_camera_problems(report),
        *_platform_problems(report),
        *_hotkey_problems(report),
        *_autostart_problems(report),
        *_settings_problems(report),
    ]


def _section_error_problems(report: dict[str, Any]) -> list[str]:
    """Sections that could not be collected at all (only their ``error``)."""
    problems: list[str] = []
    for key, section in report.items():
        if isinstance(section, dict) and "error" in section and len(section) == 1:
            problems.append(f"Could not collect {key} information: {section['error']}")
    return problems


def _library_problems(report: dict[str, Any]) -> list[str]:
    """Packages that must not be installed (the MediaPipe runtime uploads usage data)."""
    libraries = report.get("libraries") or {}
    problems: list[str] = []
    for name, version in (libraries.get("unwanted") or {}).items():
        why = _UNWANTED_DISTRIBUTIONS.get(name, "Eye Tracker does not use it")
        problems.append(
            f"The {name} package ({version}) is installed in this Python environment. "
            f"{why}; uninstall it (pip uninstall {name})."
        )
    return problems


def _backend_problems(report: dict[str, Any]) -> list[str]:
    """An unavailable vision backend, and missing or damaged model files."""
    backends = report.get("backends") or {}
    problems: list[str] = []
    if backends.get("available") == []:
        problems.append(
            "No vision backend is available (it needs OpenCV 4.10 or newer with its DNN "
            "module, and the bundled model files)."
        )
    for filename, model in (backends.get("models") or {}).items():
        users = ", ".join(model.get("used_by") or []) or "vision"
        if not model.get("present"):
            problems.append(f"The model file {filename} ({users} backend) is missing.")
        elif model.get("sha256_ok") is False:
            problems.append(f"The model file {filename} ({users} backend) is damaged (checksum).")
    if backends.get("active_error"):
        problems.append(str(backends["active_error"]))
    return problems


def _monitor_problems(report: dict[str, Any]) -> list[str]:
    """A single-monitor desk cannot switch windows (walk-away and privacy still work)."""
    monitors = report.get("monitors") or {}
    if monitors.get("count") == 1 and not monitors.get("virtual"):
        return [
            "Only one monitor detected: switching needs two or more "
            "(walk-away and privacy features still work)."
        ]
    return []


def _camera_problems(report: dict[str, Any]) -> list[str]:
    """No camera delivered a picture while probing."""
    cameras = report.get("cameras") or {}
    if not (cameras.get("probed") and cameras.get("devices") == []):
        return []
    hint = " (it may be in use by Eye Tracker itself)" if _instance_running(report) else ""
    return [f"No camera delivered a picture{hint}."]


def _platform_problems(report: dict[str, Any]) -> list[str]:
    """OS permissions and session capabilities the app needs."""
    platform_info = report.get("platform") or {}
    permissions = platform_info.get("permissions") or {}
    problems: list[str] = []
    if permissions.get("camera") is False:
        problems.append("Camera access is blocked by the operating system's privacy settings.")
    if platform_info.get("accessibility") == "stale":
        problems.append(
            "Accessibility permission was granted to an earlier version, so keyboard focus "
            "cannot follow your gaze: in System Settings > Privacy & Security > "
            "Accessibility remove Eye Tracker with '-' and add it again."
        )
    elif permissions.get("accessibility") is False:
        problems.append(
            "Accessibility permission is missing: keyboard focus cannot follow your gaze."
        )
    capabilities = platform_info.get("capabilities") or {}
    if capabilities and not capabilities.get("cursor", True):
        problems.append(
            "This session does not allow moving the cursor. On Wayland, sway and Hyprland "
            "work directly; elsewhere install ydotool 1.x and run ydotoold."
        )
    if capabilities and not capabilities.get("lock", True):
        problems.append("Locking the screen is not supported here.")
    return problems


def _hotkey_problems(report: dict[str, Any]) -> list[str]:
    """Global hotkeys that could not be registered or are unusable."""
    hotkeys = report.get("hotkeys") or {}
    problems: list[str] = []
    if hotkeys.get("enabled") and hotkeys.get("note"):
        problems.append(f"Global hotkeys: {hotkeys['note']}")
    for name, error in (hotkeys.get("invalid") or {}).items():
        problems.append(f"Hotkey {name} is invalid: {error}")
    if hotkeys.get("enabled"):
        conflicts = hotkeys.get("layout_conflicts") or {}
        for name, conflict in conflicts.items():
            problems.append(f"Hotkey {name} cannot be used: {conflict}.")
        registration = hotkeys.get("registration")
        for name, result in (registration if isinstance(registration, dict) else {}).items():
            reason = str(result).removeprefix("not registered: ")
            if reason != result and name not in conflicts:
                problems.append(f"Hotkey {name} could not be registered: {reason}.")
    return problems


def _autostart_problems(report: dict[str, Any]) -> list[str]:
    """A start-at-login item that points at a copy that no longer exists."""
    autostart_info = report.get("autostart") or {}
    if autostart_info.get("status") == "stale":
        return [
            "Start at login points to a copy of Eye Tracker that no longer exists; "
            "turn it off and on again."
        ]
    return []


def _settings_problems(report: dict[str, Any]) -> list[str]:
    """The settings file itself does not parse."""
    settings = report.get("settings") or {}
    if settings.get("valid") is False:
        return ["The settings file is not valid JSON; defaults are used."]
    return []

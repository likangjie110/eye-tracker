"""Direct tests for the settings-dialog helpers (phase 5 / P5-3).

The dialog tests exercise these through the widget tree; these pin the pure behaviour directly:
stats formatting, the settings copy, the camera-in-use skip set and the diagnostics text. They
need no dialog and no camera.
"""

from __future__ import annotations

import pytest

from eye_tracker import paths
from eye_tracker.config import Settings
from eye_tracker.platform.base import PlatformServices
from eye_tracker.types import TrackingState
from eye_tracker.ui.settings_status import (
    camera_in_use,
    format_stats,
    load_diagnostics_text,
    platform_from_controller,
    probe_cameras,
    settings_from_controller,
)


class FakeController:
    def __init__(self, settings: Settings | None = None, platform: object | None = None) -> None:
        self._settings = settings
        self.platform = platform
        self.hotkey_manager = None

    @property
    def settings(self) -> Settings | None:
        return self._settings


# ------------------------------------------------------------------ format_stats
def test_format_stats_full_line() -> None:
    stats = {
        "state": TrackingState.TRACKING,
        "fps": 4.04,
        "target_fps": 4,
        "inference_ms": 9.46,
        "skip_ratio": 0.38,
        "cpu_percent": 0.64,
    }
    assert format_stats(stats) == (
        "Tracking · 4.0 fps (target 4) · 9.5 ms/frame · 38 % skipped · CPU 0.6 %"
    )


def test_format_stats_labels_and_backend() -> None:
    assert format_stats({"state": "paused", "backend": "dnn"}) == "Paused · dnn"
    assert format_stats({"state_label": "Custom", "state": "paused"}) == "Custom"
    assert format_stats({"state": "unknown-state"}) == "unknown-state"


def test_format_stats_ignores_non_finite_and_boolean_numbers() -> None:
    stats = {"fps": float("nan"), "inference_ms": True, "skip_ratio": float("inf")}
    assert format_stats(stats) == ""


@pytest.mark.parametrize("value", [None, 42, "stats", []])
def test_format_stats_rejects_non_dicts(value: object) -> None:
    assert format_stats(value) == ""


# ------------------------------------------------------- settings/platform lookup
def test_settings_from_controller_returns_a_private_copy() -> None:
    settings = Settings()
    controller = FakeController(settings)
    copied = settings_from_controller(controller)
    assert copied is not settings
    assert copied == settings


def test_settings_from_controller_accepts_a_method() -> None:
    settings = Settings()

    class WithMethod:
        def settings(self) -> Settings:
            return settings

    assert settings_from_controller(WithMethod()) == settings


def test_settings_from_controller_falls_back_to_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback = Settings()
    calls: list[object] = []

    def load(path: object) -> Settings:
        calls.append(path)
        return fallback

    monkeypatch.setattr(Settings, "load", staticmethod(load))
    monkeypatch.setattr(paths, "settings_file", lambda: "/config/settings.json")
    controller = FakeController(None)
    assert settings_from_controller(controller) is fallback
    assert calls == ["/config/settings.json"]


def test_platform_from_controller_uses_the_controller_or_the_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = PlatformServices()
    assert platform_from_controller(FakeController(platform=platform)) is platform

    sentinel = PlatformServices()
    monkeypatch.setattr("eye_tracker.platform.get_platform", lambda: sentinel)
    assert platform_from_controller(object()) is sentinel


# ------------------------------------------------------------- camera_in_use
def test_camera_in_use_digit_device() -> None:
    settings = Settings()
    settings.camera.device = "2"
    assert camera_in_use(FakeController(settings)) == {2}


@pytest.mark.parametrize("device", ["", "clip.mp4", "rtsp://example/cam"])
def test_camera_in_use_non_device(device: str) -> None:
    settings = Settings()
    settings.camera.device = device
    assert camera_in_use(FakeController(settings)) == set()


def test_camera_in_use_linux_device_path(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    settings.camera.device = "/dev/video4"
    seen: list[str] = []

    def device_index(path: str) -> int | None:
        seen.append(path)
        return 4

    monkeypatch.setattr("eye_tracker.vision.camera.device_index", device_index)
    assert camera_in_use(FakeController(settings)) == {4}
    assert seen == ["/dev/video4"]


def test_camera_in_use_unknown_device_path(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    settings.camera.device = "/dev/does-not-exist"
    monkeypatch.setattr("eye_tracker.vision.camera.device_index", lambda path: None)
    assert camera_in_use(FakeController(settings)) == set()


# ------------------------------------------------------------ probe_cameras
def test_probe_cameras_passes_the_skip_set(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, str, object]] = []

    def list_cameras(max_index: int, api: str, skip: object = ()) -> list[str]:
        seen.append((max_index, api, skip))
        return ["a", "b"]

    monkeypatch.setattr("eye_tracker.vision.camera.list_cameras", list_cameras)
    assert probe_cameras(3, "any", {1}) == ["a", "b"]
    assert seen == [(3, "any", {1})]


# ------------------------------------------------------- diagnostics text
def test_load_diagnostics_text_renders_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from eye_tracker import diagnostics

    monkeypatch.setattr(diagnostics, "collect_report", lambda probe, hotkey_manager=None: "R")
    monkeypatch.setattr(diagnostics, "format_report", lambda report: f"report:{report}")
    assert load_diagnostics_text(FakeController()) == "report:R"

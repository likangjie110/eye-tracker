"""Characterization tests for the controller support seams.

These helpers were module-level functions in ``controller.py`` and tests usually exercised them
through a full Controller. Moving them to ``controller_support.py`` is behaviour-preserving; these
tests pin the behaviour directly so a later refactor cannot change it silently.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from eye_tracker.config import Settings
from eye_tracker.engine.controller_support import (
    _backend_gaze_indices,
    _camera_fps,
    _finite,
    _known_size,
    _layout_key,
    _named_backend_gaze_indices,
    _pane_providers,
    _same_handle,
    _same_monitor,
    _save_privacy_mode,
    _saved_privacy_mode,
    _trace_id,
)
from eye_tracker.types import Monitor, Rect


def monitor(index: int, name: str, rect: Rect) -> Monitor:
    return Monitor(index=index, name=name, rect=rect, primary=index == 0, scale=1.0)


def test_same_monitor_prefers_exact_geometry() -> None:
    old = monitor(0, "DP-1", Rect(0, 0, 1920, 1080))
    same = monitor(0, "DP-1", Rect(0, 0, 1920, 1080))
    assert _same_monitor(old, [old], [same]) is same


def test_same_monitor_follows_a_unique_name_across_a_relayout() -> None:
    old = monitor(0, "DP-1", Rect(0, 0, 1920, 1080))
    moved = monitor(1, "DP-1", Rect(1920, 0, 1920, 1080))
    assert _same_monitor(old, [old], [moved]) is moved


def test_same_monitor_ignores_positional_fallback_names() -> None:
    old = monitor(0, "Screen 1", Rect(0, 0, 1920, 1080))
    other = monitor(0, "Screen 1", Rect(1920, 0, 1920, 1080))
    covering = monitor(0, "HDMI-1", Rect(0, 0, 1920, 1080))
    # The fallback name is not trusted; the rectangle that covers the old centre wins.
    assert _same_monitor(old, [old], [other, covering]) is covering


def test_same_monitor_returns_none_when_nothing_matches() -> None:
    old = monitor(0, "DP-1", Rect(0, 0, 1920, 1080))
    far = monitor(0, "DP-2", Rect(5000, 5000, 800, 600))
    assert _same_monitor(old, [old], [far]) is None


def test_finite_rounds_and_rejects_non_finite() -> None:
    assert _finite(1.23456) == 1.23
    assert _finite(1.239, 1) == 1.2
    assert _finite(float("nan")) is None
    assert _finite(float("inf")) is None


def test_known_size_requires_a_positive_pair() -> None:
    assert _known_size((640, 480)) == (640, 480)
    assert _known_size((0, 480)) is None
    assert _known_size((-1, 2)) is None
    assert _known_size((None, 2)) is None  # type: ignore[arg-type]


def test_layout_key_is_index_and_geometry() -> None:
    monitors = [monitor(0, "A", Rect(0, 0, 100, 50)), monitor(1, "B", Rect(100, 0, 200, 50))]
    assert _layout_key(monitors) == ((0, 0, 0, 100, 50), (1, 100, 0, 200, 50))


def test_trace_id_keeps_ints_and_stringifies_the_rest() -> None:
    assert _trace_id(None) is None
    assert _trace_id(7) == 7
    assert _trace_id("abc") == "abc"
    assert _trace_id(3.5) == "3.5"


def test_same_handle_is_tolerant() -> None:
    assert _same_handle(1, 1) is True
    assert _same_handle(1, 2) is False
    # An array comparison has no single truth value; that must not escape.
    assert _same_handle(np.array([1, 2]), np.array([1, 2])) is False


def test_saved_privacy_mode_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setattr("eye_tracker.paths.state_file", lambda: state)
    assert _saved_privacy_mode() is False
    _save_privacy_mode(True)
    assert _saved_privacy_mode() is True
    _save_privacy_mode(False)
    assert _saved_privacy_mode() is False


def test_saved_privacy_mode_tolerates_damage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    state.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("eye_tracker.paths.state_file", lambda: state)
    assert _saved_privacy_mode() is False


def test_pane_providers_reads_the_settings_switches() -> None:
    settings = Settings()
    settings.panes.tmux = True
    settings.panes.wezterm = False
    settings.panes.windows_terminal = True
    settings.panes.desktop_apps = False
    assert _pane_providers(settings) == (True, False, True, False)


def test_camera_fps_follows_the_scheduler_profile() -> None:
    from eye_tracker.engine.scheduler import PROFILES

    settings = Settings()
    rates = PROFILES.get(settings.performance.profile, PROFILES["balanced"])
    assert _camera_fps(settings) == (30 if rates["active"] > 15 else 15)


def test_named_backend_gaze_indices_unknown_backend() -> None:
    assert _named_backend_gaze_indices("no-such-backend") is None


def test_backend_gaze_indices_reads_a_real_backend_class() -> None:
    from eye_tracker.vision.backends import backend_class

    indices = _backend_gaze_indices(backend_class("lite"))
    assert indices is None or all(isinstance(index, int) for index in indices)

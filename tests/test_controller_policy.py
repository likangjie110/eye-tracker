"""Characterization tests for the pure Controller policies.

These decisions moved out of ``Controller`` in phase 4 / B1. The priority order of
``TrackingState`` is easy to get subtly wrong when flags overlap, so each step of the ladder is
pinned here.
"""

from __future__ import annotations

from typing import TypedDict, cast

import pytest

from eye_tracker.engine.controller_policy import (
    TICK_ACTIVE_MS,
    TICK_IDLE_MS,
    derive_state,
    max_faces,
    may_interrupt,
    tick_interval,
)
from eye_tracker.types import TrackingState


class _StateFlags(TypedDict):
    started: bool
    privacy: bool
    session_locked: bool
    pause_when_locked: bool
    calibrating: bool
    paused: bool
    yield_reason: str | None
    away: bool
    camera_error: bool
    has_model: bool
    calibration_useful: bool


class _StateOverrides(TypedDict, total=False):
    started: bool
    privacy: bool
    session_locked: bool
    pause_when_locked: bool
    calibrating: bool
    paused: bool
    yield_reason: str | None
    away: bool
    camera_error: bool
    has_model: bool
    calibration_useful: bool


class _InterruptArgs(TypedDict):
    calibration_useful: bool
    calibrating: bool
    displays_off_only: bool
    state: TrackingState


BASE: _StateFlags = {
    "started": True,
    "privacy": False,
    "session_locked": False,
    "pause_when_locked": True,
    "calibrating": False,
    "paused": False,
    "yield_reason": None,
    "away": False,
    "camera_error": False,
    "has_model": True,
    "calibration_useful": True,
}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, TrackingState.TRACKING),
        ({"started": False}, TrackingState.STARTING),
        ({"privacy": True, "paused": True}, TrackingState.PRIVACY),
        ({"session_locked": True}, TrackingState.LOCKED),
        ({"session_locked": True, "pause_when_locked": False}, TrackingState.TRACKING),
        ({"calibrating": True, "paused": True}, TrackingState.CALIBRATING),
        ({"paused": True}, TrackingState.PAUSED),
        ({"yield_reason": "camera in use"}, TrackingState.YIELDED),
        ({"away": True}, TrackingState.AWAY),
        ({"camera_error": True}, TrackingState.CAMERA_ERROR),
        ({"has_model": False}, TrackingState.NEEDS_CALIBRATION),
        ({"has_model": False, "calibration_useful": False}, TrackingState.TRACKING),
    ],
)
def test_derive_state_priority(overrides: _StateOverrides, expected: TrackingState) -> None:
    flags = cast(_StateFlags, {**BASE, **overrides})
    assert derive_state(**flags) is expected


def test_derive_state_higher_priority_wins_over_lower() -> None:
    flags: _StateFlags = {
        **BASE,
        "privacy": True,
        "session_locked": True,
        "calibrating": True,
        "paused": True,
        "away": True,
        "camera_error": True,
        "has_model": False,
    }
    assert derive_state(**flags) is TrackingState.PRIVACY


def test_tick_interval_is_fast_for_tracking_and_the_countdown() -> None:
    assert tick_interval(tracking=True, presence_warning=False) == TICK_ACTIVE_MS
    assert tick_interval(tracking=False, presence_warning=True) == TICK_ACTIVE_MS
    assert tick_interval(tracking=False, presence_warning=False) == TICK_IDLE_MS


def test_max_faces_depends_on_the_shoulder_guard() -> None:
    assert max_faces(shoulder_guard=False) == 1
    assert max_faces(shoulder_guard=True) == 2


def test_may_interrupt_needs_a_useful_calibration_and_a_quiet_moment() -> None:
    kwargs: _InterruptArgs = {
        "calibration_useful": True,
        "calibrating": False,
        "displays_off_only": False,
        "state": TrackingState.TRACKING,
    }

    def interrupts(**changes: object) -> bool:
        return may_interrupt(**cast(_InterruptArgs, {**kwargs, **changes}))

    assert interrupts() is True
    assert interrupts(calibration_useful=False) is False
    assert interrupts(calibrating=True) is False
    assert interrupts(displays_off_only=True) is False
    for state in (TrackingState.AWAY, TrackingState.LOCKED, TrackingState.PRIVACY):
        assert interrupts(state=state) is False

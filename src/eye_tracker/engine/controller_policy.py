"""Pure policies the Controller applies to its state.

These left ``Controller`` in phase 4 / B1 because they only read values and return a verdict:
which :class:`~eye_tracker.types.TrackingState` the current flags mean, how fast the housekeeping
timer runs, how many faces the vision backend should track, and whether a calibration prompt may
interrupt the user. They are tested directly here instead of through a full Controller.
"""

from __future__ import annotations

from ..types import TrackingState

#: Housekeeping tick while tracking: the resolution of mouse/typing detection.
TICK_ACTIVE_MS = 100
#: Housekeeping tick in every other state.
TICK_IDLE_MS = 500


def derive_state(
    *,
    started: bool,
    privacy: bool,
    session_locked: bool,
    pause_when_locked: bool,
    calibrating: bool,
    paused: bool,
    yield_reason: str | None,
    away: bool,
    camera_error: bool,
    has_model: bool,
    calibration_useful: bool,
) -> TrackingState:
    """The public state derived from the independent flags, in priority order.

    Deriving instead of transitioning means overlapping causes (paused *and* the session locked,
    say) resolve naturally when one of them ends.
    """
    if not started:
        return TrackingState.STARTING
    if privacy:
        return TrackingState.PRIVACY
    if session_locked and pause_when_locked:
        return TrackingState.LOCKED
    if calibrating:
        return TrackingState.CALIBRATING
    if paused:
        return TrackingState.PAUSED
    if yield_reason:
        return TrackingState.YIELDED
    if away:
        return TrackingState.AWAY
    if camera_error:
        return TrackingState.CAMERA_ERROR
    if not has_model and calibration_useful:
        return TrackingState.NEEDS_CALIBRATION
    return TrackingState.TRACKING


def tick_interval(*, tracking: bool, presence_warning: bool) -> int:
    """The housekeeping timer interval: fast while tracking or during the away countdown."""
    return TICK_ACTIVE_MS if tracking or presence_warning else TICK_IDLE_MS


def max_faces(*, shoulder_guard: bool) -> int:
    """How many faces the vision backend should analyse (the guard needs the second)."""
    return 2 if shoulder_guard else 1


def may_interrupt(
    *,
    calibration_useful: bool,
    calibrating: bool,
    displays_off_only: bool,
    state: TrackingState,
) -> bool:
    """Whether a calibration prompt is appropriate right now.

    Not while the user is away or displays are off (monitors drop out during display sleep and
    come back unchanged), and not where a calibration would not change anything (one monitor, or
    switching turned off; a pending announcement is delivered once that changes).
    """
    return (
        calibration_useful
        and not calibrating
        and not displays_off_only
        and state not in (TrackingState.AWAY, TrackingState.LOCKED, TrackingState.PRIVACY)
    )

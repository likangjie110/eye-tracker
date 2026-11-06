"""Choosing and completing calibration profiles for the current setup.

Extracted from ``Controller`` (phase 4 / B1). The functions are pure with respect to controller
state: they receive the library, the profile in use, the backend identity and the current setup,
and return the profile to use (or the reason there is none). That makes the compatibility rules
testable without a camera or a calibration UI.

``CalibrationData`` is a dataclass, so :func:`with_camera_identity` can return a filled copy
without mutating the stored profile.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from ..types import Monitor, layout_signature

if TYPE_CHECKING:
    from ..gaze.store import CalibrationData, CalibrationLibrary


def calibration_useful(*, monitor_count: int, switching_enabled: bool) -> bool:
    """Whether a calibration would change anything.

    It only serves monitor switching, so not with one monitor or with switching turned off. Then
    presence, privacy mode and the shoulder guard are all there is, and the state must not ask for
    a calibration (warning badge, prompts).
    """
    return monitor_count >= 2 and switching_enabled


def known_size(size: tuple[int, int] | None) -> tuple[int, int] | None:
    """``size`` as a positive integer pair, or ``None`` for unknown/invalid sizes."""
    if size is None:
        return None
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def with_camera_identity(
    data: CalibrationData,
    *,
    camera_device: str,
    frame_size: tuple[int, int] | None,
) -> CalibrationData:
    """``data`` with the camera device and frame size filled in where unknown."""
    changes: dict[str, Any] = {}
    if not data.camera.strip():
        changes["camera"] = str(camera_device)
    if known_size(data.frame_size) is None and frame_size is not None:
        changes["frame_size"] = frame_size
    return dataclasses.replace(data, **changes) if changes else data


def match_profile(
    library: CalibrationLibrary,
    current: CalibrationData | None,
    *,
    backend_info: tuple[str, str] | None,
    camera: str,
    frame_size: tuple[int, int] | None,
    monitors: list[Monitor],
) -> tuple[CalibrationData | None, str]:
    """The profile to use for the current setup, or ``(None, reason)``."""
    if backend_info is None:
        return _match_by_layout(library, current, monitors)
    name, version = backend_info
    reason = "not calibrated yet"
    if current is not None:
        ok, reason = current.is_compatible(
            name, version, monitors, camera=camera, frame_size=frame_size
        )
        if ok:
            return current, ""
    data, why = library.match(name, version, monitors, camera=camera, frame_size=frame_size)
    if data is not None:
        return data, ""
    return None, reason if current is not None else why


def _match_by_layout(
    library: CalibrationLibrary,
    current: CalibrationData | None,
    monitors: list[Monitor],
) -> tuple[CalibrationData | None, str]:
    """Backend unknown (none installed?): the layout is all that can be checked."""
    signature = layout_signature(monitors)

    def fits(profile: CalibrationData) -> bool:
        return profile.model.is_fitted and profile.layout_signature == signature

    if current is not None and fits(current):
        return current, ""
    other = next((profile for profile in library if fits(profile)), None)
    if other is not None:
        return other, ""
    if current is None:
        return None, "not calibrated yet"
    if not current.model.is_fitted:
        return None, "the calibration has no fitted model"
    return None, "the monitor layout changed"

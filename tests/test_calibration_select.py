"""Characterization tests for calibration profile selection.

``match_profile`` decides which saved calibration fits the current backend, monitors, camera and
frame size; ``with_camera_identity`` fills in what older files did not record. Both were Controller
methods; these tests pin the compatibility rules and the fallback reasons without a camera.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from eye_tracker.engine.calibration_select import (
    calibration_useful,
    known_size,
    match_profile,
    with_camera_identity,
)
from eye_tracker.gaze.store import CalibrationData, CalibrationLibrary
from eye_tracker.types import Monitor, Rect, layout_signature

MONITORS = [Monitor(index=0, name="DP-1", rect=Rect(0, 0, 1920, 1080), primary=True, scale=1.0)]


class Profile:
    def __init__(
        self,
        *,
        fitted: bool = True,
        layout: str | None = None,
        compatible: bool = True,
        reason: str = "",
    ) -> None:
        self.model = SimpleNamespace(is_fitted=fitted)
        self.layout_signature = layout if layout is not None else layout_signature(MONITORS)
        self.compatible = compatible
        self.reason = reason
        self.is_compatible = self._is_compatible

    def _is_compatible(self, *_args: object, **_kwargs: object) -> tuple[bool, str]:
        return self.compatible, self.reason


class Library(list):
    def __init__(
        self,
        profiles: list[Profile] | None = None,
        match_result: tuple[object, str] | None = None,
    ) -> None:
        super().__init__(profiles or [])
        self.match_result = match_result or (None, "not calibrated yet")
        self.match_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def match(self, *args: object, **kwargs: object) -> tuple[object, str]:
        self.match_calls.append((args, kwargs))
        return self.match_result


def as_library(library: Library) -> CalibrationLibrary:
    """The fake implements the ``match``/list interface of the real library."""
    return cast(CalibrationLibrary, library)


def as_profile(profile: Profile | None) -> CalibrationData | None:
    """The fake profile implements the fields ``match_profile`` reads."""
    return cast("CalibrationData | None", profile)


def real_calibration(camera: str = "", frame_size: tuple[int, int] = (0, 0)) -> CalibrationData:
    return CalibrationData(
        backend="facemesh",
        feature_version="2",
        layout_signature=layout_signature(MONITORS),
        monitors=MONITORS,
        samples=[],
        implicit_samples=[],
        model=object(),  # type: ignore[arg-type]
        camera=camera,
        frame_size=frame_size,
    )


def test_calibration_useful_needs_two_monitors_and_switching() -> None:
    assert calibration_useful(monitor_count=2, switching_enabled=True) is True
    assert calibration_useful(monitor_count=1, switching_enabled=True) is False
    assert calibration_useful(monitor_count=2, switching_enabled=False) is False


def test_known_size_requires_positive_integers() -> None:
    assert known_size((640, 480)) == (640, 480)
    assert known_size((0, 480)) is None
    assert known_size((640, -1)) is None
    assert known_size(None) is None


def test_with_camera_identity_fills_unknown_fields() -> None:
    data = real_calibration()
    filled = with_camera_identity(data, camera_device="1", frame_size=(1280, 720))
    assert filled.camera == "1"
    assert filled.frame_size == (1280, 720)
    # The original profile is not mutated.
    assert data.camera == ""
    assert data.frame_size == (0, 0)


def test_with_camera_identity_keeps_known_fields() -> None:
    data = real_calibration(camera="0", frame_size=(640, 480))
    assert with_camera_identity(data, camera_device="9", frame_size=(1280, 720)) is data


def test_with_camera_identity_leaves_unknown_size_unknown_without_a_frame_size() -> None:
    data = real_calibration()
    filled = with_camera_identity(data, camera_device="2", frame_size=None)
    assert filled.camera == "2"
    assert filled.frame_size == (0, 0)


def test_compatible_profile_in_use_wins_without_consulting_the_library() -> None:
    current = Profile()
    library = Library([Profile()])
    data, reason = match_profile(
        as_library(library),
        as_profile(current),
        backend_info=("facemesh", "2"),
        camera="0",
        frame_size=(640, 480),
        monitors=MONITORS,
    )
    assert data is current
    assert reason == ""
    assert library.match_calls == []


def test_incompatible_profile_falls_back_to_the_library_match() -> None:
    current = Profile(compatible=False, reason="another backend")
    other = Profile()
    library = Library(match_result=(other, ""))
    data, reason = match_profile(
        as_library(library),
        as_profile(current),
        backend_info=("facemesh", "2"),
        camera="0",
        frame_size=(640, 480),
        monitors=MONITORS,
    )
    assert data is other
    assert reason == ""
    assert library.match_calls == [
        (("facemesh", "2", MONITORS), {"camera": "0", "frame_size": (640, 480)})
    ]


def test_no_match_keeps_the_current_profiles_reason() -> None:
    current = Profile(compatible=False, reason="another backend")
    library = Library(match_result=(None, "library says nothing fits"))
    data, reason = match_profile(
        as_library(library),
        as_profile(current),
        backend_info=("facemesh", "2"),
        camera="0",
        frame_size=(640, 480),
        monitors=MONITORS,
    )
    assert data is None
    assert reason == "another backend"


def test_no_current_profile_and_no_match_returns_the_library_reason() -> None:
    library = Library(match_result=(None, "not calibrated yet"))
    data, reason = match_profile(
        as_library(library),
        None,
        backend_info=("facemesh", "2"),
        camera="0",
        frame_size=(640, 480),
        monitors=MONITORS,
    )
    assert data is None
    assert reason == "not calibrated yet"


def test_unknown_backend_matches_by_layout() -> None:
    current = Profile()
    library = Library()
    data, reason = match_profile(
        as_library(library),
        as_profile(current),
        backend_info=None,
        camera="0",
        frame_size=None,
        monitors=MONITORS,
    )
    assert data is current
    assert reason == ""


def test_unknown_backend_accepts_another_matching_profile() -> None:
    current = Profile(fitted=False, compatible=False)
    other = Profile()
    data, reason = match_profile(
        as_library(Library([other])),
        as_profile(current),
        backend_info=None,
        camera="0",
        frame_size=None,
        monitors=MONITORS,
    )
    assert data is other
    assert reason == ""


def test_unknown_backend_explains_a_changed_layout() -> None:
    current = Profile(layout="a-different-layout", compatible=False)
    data, reason = match_profile(
        as_library(Library()),
        as_profile(current),
        backend_info=None,
        camera="0",
        frame_size=None,
        monitors=MONITORS,
    )
    assert data is None
    assert reason == "the monitor layout changed"


def test_unknown_backend_explains_an_unfitted_model() -> None:
    current = Profile(fitted=False, layout="a-different-layout", compatible=False)
    data, reason = match_profile(
        as_library(Library()),
        as_profile(current),
        backend_info=None,
        camera="0",
        frame_size=None,
        monitors=MONITORS,
    )
    assert data is None
    assert reason == "the calibration has no fitted model"

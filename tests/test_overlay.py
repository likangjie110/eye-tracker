"""Direct tests for the preview drawing helpers (TD-TEST-011)."""

from __future__ import annotations

import cv2
import numpy as np

from eye_tracker.types import Observation
from eye_tracker.vision.backends import overlay


def test_to_bgr_converts_grey_bgra_and_single_channel() -> None:
    grey = np.zeros((4, 5), dtype=np.uint8)
    assert overlay.to_bgr(grey).shape == (4, 5, 3)

    bgra = np.zeros((4, 5, 4), dtype=np.uint8)
    assert overlay.to_bgr(bgra).shape == (4, 5, 3)

    single = np.zeros((4, 5, 1), dtype=np.uint8)
    assert overlay.to_bgr(single).shape == (4, 5, 3)


def test_to_bgr_copies_a_three_channel_frame() -> None:
    frame = np.full((4, 5, 3), 7, dtype=np.uint8)
    out = overlay.to_bgr(frame)
    assert out is not frame
    assert np.array_equal(out, frame)


def test_as_bgr_is_contiguous_and_copies_only_when_needed() -> None:
    frame = np.zeros((4, 5, 3), dtype=np.uint8)
    out = overlay.as_bgr(frame)
    assert out.flags["C_CONTIGUOUS"]
    assert np.shares_memory(out, frame)


def test_draw_label_paints_text_and_changes_pixels() -> None:
    image = np.zeros((80, 320, 3), dtype=np.uint8)
    overlay.draw_label(image, ["hello", "world"])
    assert image.any(), "text must leave pixels behind"


def test_observation_label_describes_no_face() -> None:
    obs = Observation(timestamp=0.0, face_count=0)
    assert overlay.observation_label(obs) == ["no face"]


def test_observation_label_lists_pose_and_extras() -> None:
    obs = Observation(
        timestamp=0.0,
        face_count=2,
        blink=True,
        head_yaw=12.4,
        head_pitch=-3.2,
        features=None,
    )
    assert overlay.observation_label(obs) == [
        "yaw +12  pitch -3",
        "2 faces, eyes closed, unusable",
    ]


def test_draw_box_paints_the_normalised_rectangle() -> None:
    image = np.zeros((100, 100, 3), dtype=np.uint8)
    overlay.draw_box(image, (0.25, 0.25, 0.5, 0.5), (255, 255, 255), 2)
    assert image[25, 25].any()  # top-left corner
    assert not image[0, 0].any()  # outside stays black


def test_line_thickness_scales_with_resolution() -> None:
    assert overlay.line_thickness(np.zeros((40, 320, 3), dtype=np.uint8)) == 1
    assert overlay.line_thickness(np.zeros((720, 1280, 3), dtype=np.uint8)) == 2


def test_overlay_constants_are_bgr_triplets() -> None:
    for color in (
        overlay.COLOR_EYE,
        overlay.COLOR_IRIS,
        overlay.COLOR_BOX,
        overlay.COLOR_OTHER,
        overlay.COLOR_POINT,
        overlay.COLOR_POSE,
    ):
        assert len(color) == 3
        assert all(isinstance(channel, int) and 0 <= channel <= 255 for channel in color)


def test_drawing_on_a_copy_leaves_the_original_untouched() -> None:
    frame = np.zeros((20, 20, 3), dtype=np.uint8)
    copy = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    overlay.draw_label(copy, ["x"])
    assert not frame.any()

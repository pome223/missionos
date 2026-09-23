"""The audit must not turn a partial frustum overlap into absent evidence."""

import numpy as np
import pytest

from scripts.audit_px4_anwm_value import excluded_frustum_planes, green_pixel_count


@pytest.mark.parametrize(
    "center,expected",
    [
        ([5, 0, 0], []),
        ([5, 10, 0], ["left"]),
        ([5, -10, 0], ["right"]),
        ([5, 0, 10], ["top"]),
        ([5, 0, -10], ["bottom"]),
    ],
)
def test_enu_to_optical_frustum_signs(center, expected):
    # Facing ENU east: camera right is -north and camera down is -up.
    pose = np.array([[-1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]])
    intrinsics = np.array([[100, 0, 100], [0, 100, 100], [0, 0, 1]])
    box = {"center_xyz_m": center, "size_xyz_m": [1, 1, 1]}
    assert excluded_frustum_planes(box, pose, intrinsics, 200, 200) == expected


def test_frustum_overlap_is_not_excluded():
    pose = np.array([[-1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]])
    intrinsics = np.array([[100, 0, 100], [0, 100, 100], [0, 0, 1]])
    box = {"center_xyz_m": [5, 5, 0], "size_xyz_m": [1, 1, 1]}
    assert excluded_frustum_planes(box, pose, intrinsics, 200, 200) == []


def test_green_marker_rule_does_not_count_dark_noise_or_magenta():
    rgb = np.array(
        [[[0, 0, 0], [0, 10, 0], [200, 10, 200], [10, 100, 20]]], dtype=np.uint8
    )
    assert green_pixel_count(rgb) == 1

"""Analytic observation-support checks, not model or flight evaluation."""

import numpy as np

from scripts.audit_urban_wam_candidates import model_crop, project_observations


def observations(depth, rgb):
    return {
        "context_depth": np.asarray(depth)[None],
        "context_rgb": np.asarray(rgb, dtype=np.uint8)[None],
        "context_camera_poses": np.eye(4)[None],
        "camera_intrinsics": np.array([[2.0, 0, 1], [0, 2, 1], [0, 0, 1]]),
    }


def test_true_black_pixels_are_observed_and_zero_depth_remains_unknown():
    depth = np.ones((3, 3))
    depth[0, 0] = 0
    image, mask = project_observations(observations(depth, np.zeros((3, 3, 3))), np.eye(4))
    assert not image.any()
    assert mask.sum() == 8 and not mask[0, 0]


def test_points_behind_future_camera_do_not_become_visible():
    target = np.eye(4)
    target[2, 3] = 2
    _, mask = project_observations(observations(np.ones((3, 3)), np.full((3, 3, 3), 255)), target)
    assert not mask.any()


def test_closer_observation_wins_occlusion_independent_of_input_order():
    a = observations(np.ones((3, 3)), np.full((3, 3, 3), 40))
    for key, far in [
        ("context_depth", np.full((1, 3, 3), 2.0)),
        ("context_rgb", np.full((1, 3, 3, 3), 200, dtype=np.uint8)),
        ("context_camera_poses", np.eye(4)[None]),
    ]:
        a[key] = np.concatenate([far, a[key]])
    image, mask = project_observations(a, np.eye(4))
    assert mask.all() and (image == 40).all()


def test_crop_excludes_pixels_outside_model_view():
    support = np.zeros((360, 640), dtype=np.float32)
    support[:, :80] = 1
    assert not model_crop(support).any()

"""A missing depth return cannot become a safe alternative candidate."""

import hashlib
import json

import numpy as np
import pytest

from scripts.screen_px4_aerial_candidates import (
    ALGORITHM,
    SCHEMA,
    depth_corridor_screen,
    segment_intersects_box,
    verify_registration,
    verify_scene,
)


def camera_plane(depth_m=3.0):
    depth = np.full((51, 51), depth_m, dtype=np.float32)
    return (
        depth,
        np.ones(depth.shape, dtype=bool),
        np.array([[40, 0, 25], [0, 40, 25], [0, 0, 1]]),
        np.eye(4),
    )


def test_geometric_oracle_distinguishes_forward_wall_from_left_path():
    lower, upper = [2.525, -1.35, -0.1], [3.475, 1.35, 3.1]
    assert segment_intersects_box([0, 0, 0.25], [5, 0, 0.25], lower, upper)
    assert not segment_intersects_box([0, 0, 0.25], [0, 5, 0.25], lower, upper)


def test_visible_wall_detected_without_world_model():
    result = depth_corridor_screen(*camera_plane(), [0, 0, 0], [0, 0, 5], [0.35, 0.35, 0.1])
    assert result["classification"] == "observed_blocked"
    assert result["observed_surface_points_in_corridor"] > 0


def test_left_candidate_outside_frustum_is_unknown_not_clear():
    result = depth_corridor_screen(*camera_plane(), [0, 0, 0], [-5, 0, 0], [0.35, 0.35, 0.1])
    assert result["classification"] == "unobserved"
    assert result["observed_surface_points_in_corridor"] == 0
    assert result["outside_frustum_or_behind_camera_samples"] > 0
    assert result["absence_of_surface_hits_implies_clear"] is False


def test_no_depth_returns_cannot_establish_free_space():
    depth, _, K, transform = camera_plane()
    depth[:] = np.nan
    result = depth_corridor_screen(
        depth,
        np.zeros(depth.shape, dtype=bool),
        K,
        transform,
        [0, 0, 1],
        [0, 0, 2],
        [0.1, 0.1, 0.1],
    )
    assert result["classification"] == "unobserved"
    assert result["observed_free_samples"] == 0


def test_fully_observed_sampled_corridor_can_be_distinguished_from_unknown():
    result = depth_corridor_screen(*camera_plane(5), [0, 0, 1], [0, 0, 2], [0.1, 0.1, 0.1])
    assert result["classification"] == "observed_clear"
    assert result["observed_free_fraction"] == 1
    assert result["coverage_is_finite_sampling_not_continuous_certification"] is True


def test_missing_registration_mask_cannot_hide_geometry():
    depth, mask, K, transform = camera_plane()
    mask[25, 25] = False
    with pytest.raises(ValueError, match="validity mask"):
        depth_corridor_screen(depth, mask, K, transform, [0, 0, 0], [0, 0, 5], [0.35, 0.35, 0.1])


def test_scene_oracle_requires_observed_pose_agreement(tmp_path):
    scene = {
        "schema_version": "missionos_px4_calibration_scene.v1",
        "flight_outcomes_observed": False,
        "boxes": [{"name": "wall", "center_xyz_m": [3, 0, 1.5], "size_xyz_m": [0.25, 2, 3]}],
    }
    (tmp_path / "scene.json").write_text(json.dumps(scene))
    (tmp_path / "wall.sdf").write_text(
        '<sdf><model name="wall"><static>true</static><pose>3 0 1.5 0 0 0</pose><link><collision><geometry><box><size>0.25 2 3</size></box></geometry></collision></link></model></sdf>'
    )
    capture = {
        "frames": [
            {
                "scene_model_poses": [
                    {"name": "wall", "position": {"x": 3, "z": 1.5}, "orientation": {"w": 1}}
                ]
            }
        ]
    }
    assert verify_scene(capture, tmp_path)[0][0]["name"] == "wall"
    capture["frames"][0]["scene_model_poses"][0]["position"]["x"] = 4
    with pytest.raises(ValueError, match="observed scene pose differs"):
        verify_scene(capture, tmp_path)


@pytest.mark.parametrize(
    ("target", "key", "value"),
    [
        ("capture", "schema_version", "unsupported"),
        ("capture", "source_kind", "public_dataset_replay"),
        ("capture", "hardware_target_allowed", True),
        ("registration", "source_kind", "public_dataset_replay"),
        ("registration", "frozen_replay_only", False),
        ("registration", "live_freshness_established", True),
        ("registration", "model_inference_invoked", True),
        ("registration", "dispatch_authority_created", True),
        ("registration", "physical_execution_invoked", True),
    ],
)
def test_sink_rejects_source_or_authority_tampering_before_reading_pixels(
    tmp_path, target, key, value
):
    capture = {
        "schema_version": "missionos_px4_aerial_camera_probe.v1",
        "source_kind": "actual_px4_gazebo_sensor_capture",
        "hardware_target_allowed": False,
        "arm_command_sent": False,
        "flight_command_sent": False,
        "model_inference_invoked": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }
    registration = {
        "schema_version": SCHEMA,
        "algorithm": ALGORITHM,
        "source_kind": "px4_gazebo_frozen_capture",
        "frozen_replay_only": True,
        "world_frame": "local_ned_from_gazebo_enu",
        "camera_frame": "optical_right_down_forward",
        "invalid_depth_policy": "nan_with_explicit_mask_no_fill",
        "live_freshness_established": False,
        "model_inference_invoked": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }
    (capture if target == "capture" else registration)[key] = value
    capture_bytes = json.dumps(capture).encode()
    registration["capture_sha256"] = hashlib.sha256(capture_bytes).hexdigest()
    (tmp_path / "capture.json").write_bytes(capture_bytes)
    (tmp_path / "registration.json").write_text(json.dumps(registration))
    with pytest.raises(ValueError, match="binding mismatch|diagnostic scope"):
        verify_registration(tmp_path, tmp_path)

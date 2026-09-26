"""Native WAM input isolation and geometry gates, without models or simulators."""

import json
import math

import numpy as np
import pytest

from scripts.ship_anwm import camera_pose, center_baselines, digest, validate, write_json


@pytest.fixture
def native_request(tmp_path):
    fx = 640 / (2 * math.tan(math.pi / 6))
    arrays = {
        "rgb": np.zeros((16, 360, 640, 3), np.uint8),
        "depth": np.full((16, 360, 640), 10, np.float32),
        "poses": np.repeat(np.eye(4)[None], 16, axis=0),
        "intrinsics": np.array([[fx, 0, 320], [0, fx, 180], [0, 0, 1]]),
        "stamps_ns": np.arange(16, dtype=np.int64) * 250_000_000,
    }
    path = tmp_path / "request.json"
    np.savez_compressed(tmp_path / "history.npz", **arrays)
    value = {
        "schema_version": "ship_anwm_request.v1",
        "source_kind": "px4_ship_rgbd_hold",
        "ego_source": "Gazebo_model_pose_simulator_ground_truth",
        "delta_frame": "body_frd_at_observation",
        "num_timesteps": 4,
        "nominal_horizon_s": 1,
        "model_time_alignment_verified": False,
        "future_ground_truth_used_for_forecast": False,
        "dispatch_allowed": False,
        "candidates": [
            {"id": "hold", "delta": [0, 0, 0, 0]},
            {"id": "right_5m", "delta": [0, 5, 0, 0]},
        ],
        "seed": 42,
        "diffusion_steps": 250,
        "history_sha256": digest(tmp_path / "history.npz"),
    }
    write_json(path, value)
    return path, arrays, value


def test_input_isolation(native_request):
    path, arrays, value = native_request
    validate(path)
    arrays["future_rgb"] = arrays["rgb"][-1]
    np.savez_compressed(path.parent / "history.npz", **arrays)
    value["history_sha256"] = digest(path.parent / "history.npz")
    write_json(path, value)
    with pytest.raises(ValueError, match="history only"):
        validate(path)


@pytest.mark.parametrize("mutation", ["clock", "depth", "pose", "hash"])
def test_reject_corrupt_observations(native_request, mutation):
    path, arrays, value = native_request
    if mutation == "clock":
        arrays["stamps_ns"][4] = arrays["stamps_ns"][3]
    elif mutation == "depth":
        arrays["depth"][0, 0, 0] = np.nan
    elif mutation == "pose":
        arrays["poses"][3, 0, 0] = -1
    np.savez_compressed(path.parent / "history.npz", **arrays)
    value["history_sha256"] = (
        "0" * 64 if mutation == "hash" else digest(path.parent / "history.npz")
    )
    write_json(path, value)
    with pytest.raises(ValueError):
        validate(path)


@pytest.mark.parametrize(
    "key,value",
    [("dispatch_allowed", True), ("num_timesteps", 8), ("source_kind", "public_dataset_replay")],
)
def test_reject_scope_changes(native_request, key, value):
    path, _, original = native_request
    original[key] = value
    path.write_text(json.dumps(original))
    with pytest.raises(ValueError, match="frozen contract"):
        validate(path)


def test_optical_pose_enu_ned():
    pose = camera_pose(
        {"vehicle_quaternion_wxyz": [1, 0, 0, 0], "vehicle_position_enu_m": [10, 20, 30]}
    )
    assert np.allclose(pose[:3, 3], [20, 10.25, -30.1])
    assert np.allclose(pose[:3, :3] @ [0, 0, 1], [0, 1, 0])  # optical forward -> east
    assert np.allclose(pose[:3, :3] @ [1, 0, 0], [-1, 0, 0])  # optical right -> south


def test_missing_unused_early_observation_preserves_matched_recent_comparators():
    times = np.arange(16) * 0.25 - 3.75
    centers = list(120 + times)
    centers[0] = None
    result = center_baselines(centers, times, 1, 121)
    assert set(result) == {"velocity_fit", "stopping_fit"}
    assert result["velocity_fit"]["error_px"] == pytest.approx(0, abs=1e-10)


def test_comparators_admit_their_own_recent_window():
    times = np.arange(16) * 0.25 - 3.75
    centers = list(120 + times)
    centers[-8] = None
    assert set(center_baselines(centers, times, 1, 121)) == {"velocity_fit"}
    centers[-1] = None
    assert center_baselines(centers, times, 1, 121) == {}
    assert center_baselines(list(120 + times), times, 1, None) == {}

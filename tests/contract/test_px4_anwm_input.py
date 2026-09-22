"""Portable PX4 model inputs are typed, bound, and do not claim live execution."""

import json

import numpy as np
import pytest

from scripts.aerial_anwm_runtime import (
    CONTEXT_SIZE,
    digest_array,
    digest_file,
    digest_json,
    run,
    target_pose_from_delta,
    validate_request,
)


def bind_archive(request, arrays, base):
    np.savez_compressed(base / "assets.npz", **arrays)
    request["asset_npz_sha256"] = digest_file(base / "assets.npz")
    request["px4_provenance"]["prepared_array_sha256"] = {
        key: digest_array(value) for key, value in arrays.items()
    }


@pytest.fixture
def px4_request(tmp_path):
    poses = np.tile(np.eye(4), (CONTEXT_SIZE, 1, 1))
    poses[:, :3, :3] = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]
    poses[:, 2, 3] = -3.26
    stamps = np.arange(CONTEXT_SIZE, dtype=np.int64) * 250_000_000 + 4_000_000_000
    mask = np.ones((CONTEXT_SIZE, 16, 16), dtype=bool)
    mask[:, 0] = False
    registered = np.where(mask, np.float32(4), np.float32(np.nan))
    arrays = {
        "context_rgb": np.zeros((CONTEXT_SIZE, 16, 16, 3), dtype=np.uint8),
        "context_depth": np.where(mask, registered, np.float32(0)),
        "context_valid_mask": mask,
        "context_camera_poses": poses,
        "context_simulation_time_ns": stamps,
        "camera_intrinsics": np.array([[8, 0, 8], [0, 8, 8], [0, 0, 1]]),
        "goal_rgb": np.zeros((16, 16, 3), dtype=np.uint8),
    }
    candidates = []
    for name, delta in (("left_5m", [0.0, -5.0, 0.0, 0.0]), ("right_5m", [0.0, 5.0, 0.0, 0.0])):
        candidate = {
            "candidate_id": name,
            "delta_local_m_rad": delta,
            "target_camera_pose": target_pose_from_delta(np, poses[-1], np.array(delta)).tolist(),
            "horizon_seconds": 1.0,
        }
        candidate["candidate_sha256"] = digest_json(candidate)
        candidates.append(candidate)
    history = {
        "role": "historical_observation",
        "capture_scope": "px4_sitl_airborne_observation",
        "capture_sha256": "a" * 64,
        "registration_sha256": "b" * 64,
        "capture_completed_at": "2026-09-22T00:00:00+00:00",
        "observed_at": "2026-09-22T00:00:00+00:00",
        "observed_at_unix_s": 1790035200.0,
        "flight_session_status_sha256": "c" * 64,
        "source_sdf_sha256": {"camera": "d" * 64, "airframe": "e" * 64},
        "last_camera_optical_to_local_ned": poses[-1].tolist(),
        **{
            key: ["f" * 64] * 16
            for key in (
                "frame_npz_sha256",
                "frame_rgb_raw_sha256",
                "frame_depth_raw_sha256",
                "frame_calibration_sha256",
            )
        },
    }
    goal = {
        "schema_version": "missionos_aerial_goal_reference.v1",
        "role": "declared_goal_reference",
        "source_kind": "actual_gazebo_static_camera",
        "reference_manifest_sha256": "b" * 64,
        "rgb_sha256": "c" * 64,
        "source_message_sha256": "d" * 64,
        "camera_sdf_sha256": "e" * 64,
        "width": 16,
        "height": 16,
        "intrinsics": arrays["camera_intrinsics"].tolist(),
        "optical_to_local_ned": poses[-1].tolist(),
        "simulation_time_ns": 1_000_000_000,
        "scene_geometry_sha256": "a" * 64,
        "flight_outcome_observed": False,
        "future_ground_truth_used_for_forecast": False,
    }
    request = {
        "schema_version": "aerial_anwm_request.v1",
        "source_kind": "px4_gazebo_frozen_capture",
        "request_id": "synthetic-px4-validation-only",
        "assets_npz": "assets.npz",
        "delta_frame": "body_frd_at_observation",
        "num_timesteps": 4,
        "frame_interval_seconds": 0.25,
        "frame_interval_source": "measured_Gazebo_simulation_timestamps",
        "physical_frame_timing_verified": False,
        "simulation_frame_timing_verified": True,
        "horizon_seconds_nominal": True,
        "model_time_alignment_verified": False,
        "source_timing": {"simulation_time_ns": stamps.tolist()},
        "candidates": candidates,
        "candidate_plans_sha256": digest_json(candidates),
        "px4_provenance": {
            "history": history,
            "goal_reference": goal,
            "scene_geometry_sha256": "a" * 64,
            "session_id": "fixture-session-no-real-flight",
            "control_hold_kind": "px4_offboard_hover",
            "current_vehicle_local_ned_m": [0, 0, -3],
            "current_vehicle_gazebo_enu_m": [0, 0, 3],
            "yaw_ned_rad": 0.0,
            "registered_depth_sha256": digest_array(registered),
            "source_assets_reverified": True,
            "transport_field_decode_independently_verified": False,
        },
    }
    bind_archive(request, arrays, tmp_path)
    return request, arrays, tmp_path


def test_typed_px4_validate_only_retains_masks_and_source(px4_request):
    request, arrays, base = px4_request
    result = run(request, base, base / "unused", validate_only=True)
    manifest = result["input_manifest"]
    assert manifest["source_kind"] == "px4_gazebo_frozen_capture"
    assert "public_provenance" not in manifest
    assert manifest["simulation_frame_timing_verified"] is True
    assert manifest["physical_frame_timing_verified"] is False
    assert manifest["model_time_alignment_verified"] is False
    assert manifest["live_freshness_established"] is False
    assert "runtime_invocation_evidence" not in result
    assert np.all(arrays["context_depth"][~arrays["context_valid_mask"]] == 0)
    assert str(base) not in json.dumps(manifest)


def test_original_observation_timestamp_survives_manifest_cleaning(px4_request):
    request, _, base = px4_request
    history = request["px4_provenance"]["history"]
    history["capture_completed_at"] = "2026-09-22T00:01:00+00:00"
    result = run(request, base, base / "unused", validate_only=True)
    cleaned = result["input_manifest"]["px4_provenance"]["history"]
    assert cleaned["observed_at"] == history["observed_at"]
    assert cleaned["observed_at_unix_s"] == history["observed_at_unix_s"]
    assert cleaned["capture_completed_at"] != cleaned["observed_at"]


@pytest.mark.parametrize("field,value", [
    ("observed_at", "2026-09-22T00:01:00+00:00"),
    ("observed_at", "2026-09-22T00:00:00"),
    ("capture_completed_at", "2026-09-21T23:59:59+00:00"),
])
def test_contradictory_source_times_are_rejected(px4_request, field, value):
    request, _, base = px4_request
    request["px4_provenance"]["history"][field] = value
    with pytest.raises(ValueError, match="observation|timestamp"):
        validate_request(request, base)


def test_preparation_uses_earliest_source_receipt_on_last_historical_frame():
    from scripts.prepare_px4_anwm_input import last_frame_observation_time

    capture = {
        "capture_completed_at": "2026-09-22T00:10:00+00:00",
        "frames": [{"received_at": {"rgb": "2026-09-21T23:59:59+00:00"}},
                   {"received_at": {key: f"2026-09-22T00:00:0{i}+00:00"
                                    for i, key in enumerate(("pose", "rgb", "depth", "rgb_info", "depth_info"))}}],
    }
    assert last_frame_observation_time(capture) == ("2026-09-22T00:00:00+00:00", 1790035200.0)


@pytest.mark.parametrize("defect", ["missing_source", "missing_timezone"])
def test_preparation_requires_complete_dated_source_receipts(defect):
    from scripts.prepare_px4_anwm_input import last_frame_observation_time

    received = dict.fromkeys(("pose", "rgb", "depth", "rgb_info", "depth_info"),
                             "2026-09-22T00:00:00+00:00")
    if defect == "missing_source":
        received.pop("depth")
    else:
        received["depth"] = "2026-09-22T00:00:00"
    with pytest.raises(ValueError, match="source receipt|timezone"):
        last_frame_observation_time({"frames": [{"received_at": received}]})


def test_irregular_or_repeated_px4_frames_rejected(px4_request):
    request, _, base = px4_request
    request["source_timing"]["simulation_time_ns"][4] += 10_000_000
    with pytest.raises(ValueError, match="cadence"):
        validate_request(request, base)


def test_px4_future_outcome_array_rejected(px4_request):
    request, arrays, base = px4_request
    arrays["future_rgb"] = arrays["context_rgb"][-1]
    bind_archive(request, arrays, base)
    with pytest.raises(ValueError, match="only historical"):
        validate_request(request, base)


def test_unknown_depth_cannot_be_replaced_by_far_plane(px4_request):
    request, arrays, base = px4_request
    arrays["context_depth"][~arrays["context_valid_mask"]] = 19.1
    bind_archive(request, arrays, base)
    with pytest.raises(ValueError, match="validity mask"):
        validate_request(request, base)


def test_known_depth_cannot_change_through_zero_conversion(px4_request):
    request, arrays, base = px4_request
    arrays["context_depth"][arrays["context_valid_mask"]] = 8
    bind_archive(request, arrays, base)
    with pytest.raises(ValueError, match="survive sentinel"):
        validate_request(request, base)


@pytest.mark.parametrize(
    "key,value",
    [
        ("role", "observed_candidate_outcome"),
        ("scene_geometry_sha256", "f" * 64),
        ("future_ground_truth_used_for_forecast", True),
    ],
)
def test_goal_is_reference_not_outcome(px4_request, key, value):
    request, _, base = px4_request
    request["px4_provenance"]["goal_reference"][key] = value
    with pytest.raises(ValueError, match="goal"):
        validate_request(request, base)


def test_candidate_plan_receipt_cannot_be_replaced(px4_request):
    request, _, base = px4_request
    request["candidate_plans_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="plans hash"):
        validate_request(request, base)


def test_asset_tampering_does_not_pass_portable_receipt(px4_request):
    request, arrays, base = px4_request
    arrays["context_rgb"][0, 0, 0] = 200
    np.savez_compressed(base / "assets.npz", **arrays)
    with pytest.raises(ValueError, match="prepared arrays"):
        validate_request(request, base)


@pytest.fixture
def goal_reference_fixture(tmp_path):
    from PIL import Image

    Image.fromarray(np.zeros((16, 16, 3), dtype=np.uint8)).save(tmp_path / "goal.png")
    (tmp_path / "goal-rgb.pb").write_bytes(b"synthetic-wire-fixture-not-simulator-evidence")
    (tmp_path / "camera.sdf").write_text(
        '<sdf><model name="aerial_goal_reference"><static>true</static>'
        '<pose>1 5 3 0 0 0</pose><link name="goal_camera_link"><sensor type="camera"><camera>'
        "<horizontal_fov>1.5707963267948966</horizontal_fov>"
        "<image><width>16</width><height>16</height></image>"
        "</camera></sensor></link></model></sdf>"
    )
    K = np.array([[8, 0, 8], [0, 8, 8], [0, 0, 1]])
    reference = {
        "schema_version": "missionos_aerial_goal_reference.v1",
        "role": "declared_goal_reference",
        "source_kind": "actual_gazebo_static_camera",
        "scene_geometry_sha256": "a" * 64,
        "rgb_file": "goal.png",
        "rgb_sha256": digest_file(tmp_path / "goal.png"),
        "camera_sdf_file": "camera.sdf",
        "camera_sdf_sha256": digest_file(tmp_path / "camera.sdf"),
        "source_message_sha256": digest_file(tmp_path / "goal-rgb.pb"),
        "width": 16,
        "height": 16,
        "intrinsics": K.tolist(),
        "optical_to_local_ned": [[-1, 0, 0, 5], [0, 0, 1, 1], [0, 1, 0, -3], [0, 0, 0, 1]],
        "observed_reference_pose": {"name": "aerial_goal_reference",
                                    "position": {"x": 1, "y": 5, "z": 3}, "orientation": {"w": 1}},
        "simulation_time_ns": 1,
        "future_outcome_image": False,
        "candidate_outcome_observed": False,
        "flight_command_sent": False,
    }
    path = tmp_path / "goal.json"
    path.write_text(json.dumps(reference))
    return path, reference, K


def test_goal_reference_camera_calibration_is_bound_to_sdf(goal_reference_fixture):
    from scripts.prepare_px4_anwm_input import load_goal_reference

    path, reference, K = goal_reference_fixture
    tmp_path = path.parent
    image, provenance = load_goal_reference(path, (16, 16, 3), K, "a" * 64)
    assert image.shape == (16, 16, 3)
    assert provenance["flight_outcome_observed"] is False
    # Updating both SDF bytes and its hash still cannot hide a calibration mismatch.
    sdf = (tmp_path / "camera.sdf").read_text().replace("1.5707963267948966", "1.0")
    (tmp_path / "camera.sdf").write_text(sdf)
    reference["camera_sdf_sha256"] = digest_file(tmp_path / "camera.sdf")
    path.write_text(json.dumps(reference))
    with pytest.raises(ValueError, match="SDF camera calibration"):
        load_goal_reference(path, (16, 16, 3), K, "a" * 64)


@pytest.mark.parametrize("defect", ["flip_goal", "rotate_goal", "move_observed", "rotate_observed",
                                  "missing_observed", "change_model", "change_link", "change_sensor",
                                  "relative_pose", "dynamic_model"])
def test_goal_pose_cannot_drift_from_bound_geometry(goal_reference_fixture, defect):
    from scripts.prepare_px4_anwm_input import load_goal_reference

    path, reference, K = goal_reference_fixture
    sdf_path = path.parent / "camera.sdf"
    if defect == "flip_goal":
        reference["optical_to_local_ned"][0][3] = -5
    elif defect == "rotate_goal":
        reference["optical_to_local_ned"] = np.eye(4).tolist()
    elif defect == "move_observed":
        reference["observed_reference_pose"]["position"]["y"] = -5
    elif defect == "rotate_observed":
        reference["observed_reference_pose"]["orientation"] = {"z": 1}
    elif defect == "missing_observed":
        reference.pop("observed_reference_pose")
    else:
        sdf = sdf_path.read_text()
        replacements = {
            "change_model": ("1 5 3 0 0 0", "1 -5 3 0 0 0"),
            "change_link": ('<link name="goal_camera_link">', '<link name="goal_camera_link"><pose>0 1 0 0 0 0</pose>'),
            "change_sensor": ('<sensor type="camera">', '<sensor type="camera"><pose>1 0 0 0 0 0</pose>'),
            "relative_pose": ('<pose>', '<pose relative_to="world">'),
            "dynamic_model": ("<static>true</static>", "<static>false</static>"),
        }
        sdf_path.write_text(sdf.replace(*replacements[defect]))
        reference["camera_sdf_sha256"] = digest_file(sdf_path)
    path.write_text(json.dumps(reference))
    with pytest.raises(ValueError, match="goal|SDF"):
        load_goal_reference(path, (16, 16, 3), K, "a" * 64)


def test_goal_pose_composes_model_link_sensor_and_optical_frames(goal_reference_fixture):
    from scripts.prepare_px4_anwm_input import load_goal_reference

    path, reference, K = goal_reference_fixture
    sdf_path = path.parent / "camera.sdf"
    sdf = sdf_path.read_text().replace('<link name="goal_camera_link">',
        '<link name="goal_camera_link"><pose>1 0 0 0 0 1.5707963267948966</pose>')
    sdf = sdf.replace('<sensor type="camera">', '<sensor type="camera"><pose>0 2 0 0 0 0</pose>')
    sdf_path.write_text(sdf)
    reference["camera_sdf_sha256"] = digest_file(sdf_path)
    reference["optical_to_local_ned"] = [[0, 0, 1, 5], [1, 0, 0, 0], [0, 1, 0, -3], [0, 0, 0, 1]]
    path.write_text(json.dumps(reference))
    _, accepted = load_goal_reference(path, (16, 16, 3), K, "a" * 64)
    assert accepted["optical_to_local_ned"] == reference["optical_to_local_ned"]


@pytest.mark.parametrize("flag", [
    "future_outcome_image", "candidate_outcome_observed", "flight_outcome_observed",
    "future_ground_truth_used_for_forecast", "flight_command_sent",
    "physical_execution_observed", "physical_execution_invoked",
])
@pytest.mark.parametrize("value", [True, 1, "false", None])
def test_goal_source_cannot_sanitize_contradictory_outcome_flags(tmp_path, flag, value):
    from scripts.prepare_px4_anwm_input import load_goal_reference

    reference = {
        "schema_version": "missionos_aerial_goal_reference.v1",
        "role": "declared_goal_reference",
        "source_kind": "actual_gazebo_static_camera",
        "scene_geometry_sha256": "a" * 64,
        flag: value,
    }
    path = tmp_path / "goal.json"
    path.write_text(json.dumps(reference))
    # No asset exists: a contradictory declaration must fail before it can be
    # cleaned or admitted as a benign goal reference.
    with pytest.raises(ValueError, match="contradicts its reference-only role"):
        load_goal_reference(path, (16, 16, 3), np.eye(3), "a" * 64)

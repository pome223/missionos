"""Retained authorization plus independent swept-airframe static geometry tests."""

import hashlib
import json

import numpy as np
import pytest

from missionos_core.prediction import prediction_digest
from scripts.build_px4_aerial_execution_policy import build_policy
from scripts.probe_px4_aerial_camera import box_sdf
from scripts.aerial_anwm_runtime import target_pose_from_delta
from test_px4_aerial_wam_jev import MODEL, trial  # noqa: F401 - shared synthetic model-receipt fixture


def write_json(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def policy_inputs(trial, tmp_path):  # noqa: F811 - pytest injects the imported shared fixture
    result, _, _ = trial
    # Match the actual scene: aircraft forward is ENU +X, hence local NED yaw
    # pi/2; five-metre lateral paths run along ENU +/-Y, not through the X wall.
    provenance = result["input_manifest"]["px4_provenance"]
    provenance["yaw_ned_rad"] = np.pi / 2
    camera = np.array([[-1., 0., 0., 0.], [0., 0., 1., .2], [0., 1., 0., -3.], [0., 0., 0., 1.]])
    provenance["history"]["last_camera_optical_to_local_ned"] = camera.tolist()
    for candidate, output in zip(result["input_manifest"]["candidates"], result["candidates"]):
        candidate["target_camera_pose"] = target_pose_from_delta(np, camera, np.array(candidate["delta_local_m_rad"])).tolist()
        candidate["candidate_sha256"] = prediction_digest({k: v for k, v in candidate.items() if k != "candidate_sha256"})
        output["candidate_sha256"] = candidate["candidate_sha256"]
    result["input_manifest"]["candidate_plans_sha256"] = prediction_digest(result["input_manifest"]["candidates"])
    result["runtime_invocation_evidence"]["forecasts_sha256"] = prediction_digest(result["candidates"])
    box = {"name": "fixture_wall", "center_xyz_m": [3.0, 0.0, 3.0],
           "size_xyz_m": [0.25, 2.0, 6.0], "rgba": [1, 0, 0, 1]}
    config = {"schema_version": "missionos_px4_aerial_flight_policy.v1", "execution_scope": "px4_sitl",
              "hardware_target": False, "candidate_ids": ["left_5m", "right_5m"],
              "session_id": "synthetic-session", "approved_instruction_ref": "fixture:retained-user-instruction"}
    authorization = {"instruction_ref": config["approved_instruction_ref"],
                     "text": "Synthetic retained instruction for a simulator-only contract test.",
                     "authorized_at_unix_s": 90.0, "hardware_allowed": False}
    write_json(tmp_path / "user-authorization.json", authorization)

    def bind_scene(boxes):
        records, entities = [], []
        for item in boxes:
            sdf = box_sdf(item).encode()
            (tmp_path / (item["name"] + ".sdf")).write_bytes(sdf)
            records.append({"name": item["name"], "center_xyz_m": list(map(float, item["center_xyz_m"])),
                            "size_xyz_m": list(map(float, item["size_xyz_m"])),
                            "sdf_sha256": hashlib.sha256(sdf).hexdigest()})
            entities.append({"name": item["name"], "position": dict(zip("xyz", item["center_xyz_m"])),
                             "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}})
        digest = prediction_digest(records)
        config.update(scene_sha256=digest, scene_entities=entities)
        write_json(tmp_path / "config.json", config)
        write_json(tmp_path / "scene.json", {"schema_version": "missionos_px4_calibration_scene.v1",
                                              "boxes": boxes, "scene_geometry_sha256": digest})
        provenance = result["input_manifest"]["px4_provenance"]
        provenance["scene_geometry_sha256"] = digest
        provenance["goal_reference"]["scene_geometry_sha256"] = digest
        result["input_manifest_sha256"] = prediction_digest(result["input_manifest"])
        result["runtime_invocation_evidence"]["input_manifest_sha256"] = result["input_manifest_sha256"]
    bind_scene([box])
    return result, tmp_path, box, config, authorization, bind_scene


def build(fixture):
    return build_policy(fixture[0], fixture[1], now=150.0, expected_model_sha256=MODEL)


def test_actual_sdf_geometry_clearance_concretizes_both_candidates(policy_inputs):
    policy = build(policy_inputs)
    assert policy["allowed_candidates"][0]["target_local_ned_m"] == pytest.approx([5.0, 0.0, -3.0])
    assert policy["allowed_candidates"][1]["target_local_ned_m"] == pytest.approx([-5.0, 0.0, -3.0])
    assert all(c["independent_sdf_path_clear"] is True for c in policy["allowed_candidates"])
    screens = policy["geometry_verification"]["candidate_screens"]
    assert all(s["margin_m_per_axis"] == 0.6 and not s["intersected_collision_boxes"] for s in screens)
    assert all(s["airframe_start_enu_m"] == [0.0, 0.0, 3.0] for s in screens)
    assert policy["authorization"]["expires_at_unix_s"] == 7350.0
    assert policy["authorization"]["instruction_sha256"] == hashlib.sha256(policy_inputs[4]["text"].encode()).hexdigest()
    assert policy["authorization"]["instruction_ref"] == policy_inputs[3]["approved_instruction_ref"]
    assert policy["approval_generated"] is False
    assert policy["dispatch_invoked"] is False
    assert policy["geometry_verification"]["flight_outcome_observed"] is False


@pytest.mark.parametrize("center,size", [
    ([0.0, 2.5, 3.0], [0.25, 0.25, 0.25]),  # Centerline intersects.
    ([0.65, 2.5, 3.0], [0.2, 0.25, 0.25]),  # Centerline clear, airframe margin intersects.
    ([0.0, 5.5, 3.0], [0.2, 0.2, 0.2]),  # Beyond endpoint, but margin intersects.
    ([0.0, 2.5, 3.55], [0.2, 0.2, 0.2]),  # Vertical margin intersects.
])
def test_collision_on_either_swept_airframe_path_prevents_policy(policy_inputs, center, size):
    _, _, box, _, _, bind = policy_inputs
    box.update(center_xyz_m=center, size_xyz_m=size)
    bind([box])
    with pytest.raises(ValueError, match="airframe_path_intersects_sdf"):
        build(policy_inputs)


def test_thin_obstacle_between_start_and_target_is_not_missed_by_endpoint_checks(policy_inputs):
    _, _, box, _, _, bind = policy_inputs
    box.update(center_xyz_m=[0.0, -2.173, 3.0], size_xyz_m=[0.01, 0.001, 0.01])
    bind([box])
    with pytest.raises(ValueError, match="airframe_path_intersects_sdf"):
        build(policy_inputs)


@pytest.mark.parametrize("defect", ["missing_authorization", "different_instruction", "hardware_authorization",
                                    "future_authorization", "different_session", "unbound_sdf", "unknown_entity"])
def test_missing_or_changed_source_evidence_cannot_generate_permission(policy_inputs, defect):
    _, directory, box, config, authorization, _ = policy_inputs
    if defect == "missing_authorization":
        (directory / "user-authorization.json").unlink()
    elif defect == "different_instruction":
        authorization["instruction_ref"] = "fixture:other-instruction"
        write_json(directory / "user-authorization.json", authorization)
    elif defect == "hardware_authorization":
        authorization["hardware_allowed"] = True
        write_json(directory / "user-authorization.json", authorization)
    elif defect == "future_authorization":
        authorization["authorized_at_unix_s"] = 151.0
        write_json(directory / "user-authorization.json", authorization)
    elif defect == "different_session":
        config["session_id"] = "another-session"
        write_json(directory / "config.json", config)
    elif defect == "unbound_sdf":
        path = directory / (box["name"] + ".sdf")
        path.write_text(path.read_text().replace("3.0 0.0 3.0", "9.0 0.0 3.0"))
    elif defect == "unknown_entity":
        config["scene_entities"].append({"name": "unreviewed_obstacle"})
        write_json(directory / "config.json", config)
    with pytest.raises((ValueError, FileNotFoundError)):
        build(policy_inputs)

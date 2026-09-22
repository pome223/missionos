"""Bound actual PX4 SITL image-goal evidence to one already authorized candidate.

This host-side evaluator never sends MAVLink or creates human approval. It
requires a caller-supplied user-instruction policy and fresh hover telemetry
after the Jev judgment. The separate executor must validate and sign the short
lived command immediately before use. No hardware scope or fallback is allowed.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

import numpy as np

from missionos_core.prediction import (
    OptionForecast, PredictionBinding, PredictionOption, PredictionRegistry,
    PredictionRequest, prediction_digest, validate_forecast_metrics,
)
from src.intelligence.jev_assurance import JevAssuranceJudge
from src.intelligence.mission_assurance_agent import MissionAssuranceAgent, MissionSituation
from src.intelligence.prediction_evidence import capture_prediction_evidence, receive_prediction_evidence
from scripts.aerial_anwm_runtime import target_pose_from_delta, validate_model_identity

MODEL_SHA256 = "bdd149cac6ec002ba7dc4ad99ec6f9eb02cd6d4f05320195cf174737b13b0bc2"
SOURCE_KIND = "px4_gazebo_frozen_capture"
IDS = ("left_5m", "right_5m")
MAX_INPUT_AGE = 180.0
MAX_TELEMETRY_AGE = 2.0


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _sha(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "invalid_sha256")
    return value


def _number(value):
    _require(type(value) in (int, float) and math.isfinite(value), "invalid_number")
    return value


def _vector(value, length=3):
    _require(isinstance(value, list) and len(value) == length, "invalid_vector")
    return [_number(v) for v in value]


def _distance(left, right):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def _timestamp(value):
    _require(isinstance(value, str), "capture_timestamp_required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None, "capture_timestamp_timezone_required")
    return parsed.timestamp()


def _goal_state(output):
    state = {"goal_compatibility": {
        "schema_version": "missionos_goal_compatibility.v1", "metric_id": "anwm_goal_image_mse",
        "raw_cost": output["goal_mse"], "lower_is_better": True, "risk_assessed": False,
    }, "predicted_image_sha256": _sha(output["predicted_image_sha256"]),
        "source_kind": "recorded_model_inference", "horizon_seconds_nominal": True,
        "collision_risk_assessed": False}
    validate_forecast_metrics(None, state)
    return state


def validate_result(result, *, expected_model_sha256=MODEL_SHA256):
    """Check receipt bindings; hashes identify supplied records, not authenticity."""
    _sha(expected_model_sha256)
    _require(result.get("schema_version") == "aerial_anwm_result.v1", "result_schema_mismatch")
    _require(result.get("score_is_calibrated_risk") is False, "goal_cost_is_not_collision_risk")
    manifest = result["input_manifest"]
    _require(manifest.get("schema_version") == "missionos_aerial_anwm_input.v1"
             and manifest.get("source_kind") == SOURCE_KIND, "px4_capture_source_required")
    _require(result["input_manifest_sha256"] == prediction_digest(manifest), "input_hash_mismatch")
    _sha(manifest["asset_npz_sha256"])
    _require(type(manifest.get("context_size")) is int and manifest["context_size"] == 16,
             "sixteen_real_history_frames_required")
    _require(manifest.get("delta_frame") == "body_frd_at_observation"
             and manifest.get("future_ground_truth_used_for_forecast") is False
             and manifest.get("goal_image_used_for_scoring_only") is True
             and manifest.get("physical_frame_timing_verified") is False
             and manifest.get("horizon_seconds_nominal") is True
             and manifest.get("simulation_frame_timing_verified") is True
             and manifest.get("model_time_alignment_verified") is False, "invalid_prediction_claims")
    _require(type(manifest.get("num_timesteps")) is int and manifest["num_timesteps"] == 4
             and type(manifest.get("seed")) is int and 0 <= manifest["seed"] < 2**32
             and type(manifest.get("diffusion_steps")) is int and 1 <= manifest["diffusion_steps"] <= 250,
             "invalid_model_configuration")
    timing = manifest["source_timing"]
    stamps = timing["simulation_time_ns"]
    _require(isinstance(stamps, list) and len(stamps) == 16
             and all(type(s) is int and s >= 0 for s in stamps), "invalid_simulation_timestamps")
    _require(all(abs((b - a) / 1e9 - 0.25) <= 0.004000001 for a, b in zip(stamps, stamps[1:])),
             "history_sampling_mismatch")
    intervals = [(b - a) / 1e9 for a, b in zip(stamps, stamps[1:])]
    _require(timing.get("metadata_semantics") == "Gazebo_simulation_nanoseconds"
             and _vector(timing["measured_frame_intervals_seconds"], 15) == intervals
             and _number(timing["expected_interval_seconds"]) == 0.25
             and abs(_number(timing["maximum_interval_error_seconds"])
                     - max(abs(v - 0.25) for v in intervals)) < 1e-9,
             "source_timing_receipt_mismatch")
    provenance = manifest["px4_provenance"]
    _require(provenance.get("control_hold_kind") == "px4_offboard_hover"
             and provenance.get("source_assets_reverified") is True,
             "actual_px4_hover_required")
    _sha(provenance["scene_geometry_sha256"])
    _vector(provenance["current_vehicle_local_ned_m"])
    _number(provenance["yaw_ned_rad"])
    history = provenance["history"]
    _require(history.get("role") == "historical_observation"
             and history.get("capture_scope") == "px4_sitl_airborne_observation", "airborne_history_required")
    _sha(history["capture_sha256"])
    _sha(history["registration_sha256"])
    _sha(history["flight_session_status_sha256"])
    for field in ("frame_npz_sha256", "frame_rgb_raw_sha256", "frame_depth_raw_sha256", "frame_calibration_sha256"):
        _require(isinstance(history.get(field), list) and len(history[field]) == 16, "history_artifact_binding_missing")
        for value in history[field]:
            _sha(value)
    captured_at = _timestamp(history["observed_at"])
    _require(abs(_number(history["observed_at_unix_s"]) - captured_at) <= 1e-6
             and _timestamp(history["capture_completed_at"]) >= captured_at,
             "original_source_observation_time_mismatch")
    camera_pose = np.asarray(history["last_camera_optical_to_local_ned"], dtype=float)
    _require(camera_pose.shape == (4, 4) and np.isfinite(camera_pose).all()
             and np.allclose(camera_pose[3], [0, 0, 0, 1])
             and np.allclose(camera_pose[:3, :3].T @ camera_pose[:3, :3], np.eye(3), atol=1e-6)
             and np.isclose(np.linalg.det(camera_pose[:3, :3]), 1, atol=1e-6), "invalid_source_camera_pose")
    camera_yaw = math.atan2(camera_pose[1, 2], camera_pose[0, 2])
    _require(abs(math.remainder(camera_yaw - provenance["yaw_ned_rad"], 2 * math.pi)) <= 0.05
             and _distance(camera_pose[:3, 3].tolist(), provenance["current_vehicle_local_ned_m"]) <= 0.5,
             "camera_and_vehicle_frames_disagree")
    goal = provenance["goal_reference"]
    _require(goal.get("schema_version") == "missionos_aerial_goal_reference.v1"
             and goal.get("role") == "declared_goal_reference"
             and goal.get("source_kind") == "actual_gazebo_static_camera"
             and goal.get("flight_outcome_observed") is False
             and goal.get("future_ground_truth_used_for_forecast") is False
             and goal.get("reference_manifest_sha256") != history["capture_sha256"]
             and goal.get("scene_geometry_sha256") == provenance["scene_geometry_sha256"],
             "declared_independent_goal_required")
    _sha(goal["reference_manifest_sha256"])
    _sha(goal["rgb_sha256"])
    candidates = manifest["candidates"]
    _require(isinstance(candidates, list) and [c["candidate_id"] for c in candidates] == list(IDS),
             "fixed_lateral_candidates_required")
    _require(manifest["candidate_plans_sha256"] == prediction_digest(candidates), "candidate_plans_hash_mismatch")
    for candidate, right in zip(candidates, (-5.0, 5.0)):
        body = {k: v for k, v in candidate.items() if k != "candidate_sha256"}
        _require(set(body) == {"candidate_id", "delta_local_m_rad", "target_camera_pose", "horizon_seconds"}
                 and candidate["candidate_sha256"] == prediction_digest(body), "candidate_hash_mismatch")
        _require(_vector(candidate["delta_local_m_rad"], 4) == [0.0, right, 0.0, 0.0]
                 and _number(candidate["horizon_seconds"]) == 1.0, "candidate_motion_not_authorized")
        pose = candidate["target_camera_pose"]
        _require(isinstance(pose, list) and len(pose) == 4, "invalid_camera_pose")
        for row in pose:
            _vector(row, 4)
        _require(np.allclose(np.asarray(pose), target_pose_from_delta(
            np, camera_pose, np.asarray(candidate["delta_local_m_rad"])), atol=1e-6, rtol=0),
            "candidate_camera_action_mismatch")
    outputs = result["candidates"]
    _require(isinstance(outputs, list) and len(outputs) == 2
             and {c["candidate_id"] for c in outputs} == set(IDS), "forecast_candidates_mismatch")
    by_id = {c["candidate_id"]: c for c in outputs}
    for candidate in candidates:
        output = by_id[candidate["candidate_id"]]
        _require(output["candidate_sha256"] == candidate["candidate_sha256"], "forecast_candidate_hash_mismatch")
        _goal_state(output)
        _require(_number(output["projection_goal_mse"]) >= 0, "invalid_projection_cost")
    model, invocation = result["model"], result["runtime_invocation_evidence"]
    validate_model_identity(model, expected_model_sha256)
    _require(invocation.get("schema_version") == "runtime_invocation_evidence.v1"
             and invocation.get("fixture_invocation") is False
             and invocation.get("model_execution_verified") is True
             and type(invocation.get("actual_model_calls")) is int and invocation["actual_model_calls"] == 2
             and invocation.get("execution_scope") == "px4_gazebo_frozen_candidate_forecast"
             and invocation.get("source_kind") == SOURCE_KIND
             and invocation.get("physical_execution_observed") is False
             and invocation.get("physical_frame_timing_verified") is False
             and invocation.get("horizon_seconds_nominal") is True
             and type(invocation.get("context_size")) is int and invocation["context_size"] == 16,
             "actual_model_invocation_required")
    _require(invocation.get("input_manifest_sha256") == result["input_manifest_sha256"]
             and invocation.get("model_sha256") == expected_model_sha256
             and invocation.get("forecasts_sha256") == prediction_digest(outputs)
             and invocation.get("source_timing") == timing
             and type(invocation.get("seed")) is int and invocation["seed"] == manifest["seed"]
             and type(invocation.get("sampling_steps")) is int
             and invocation["sampling_steps"] == manifest["diffusion_steps"], "runtime_receipt_binding_mismatch")
    return manifest, by_id, captured_at


def _policy_targets(manifest, policy, now):
    _require(policy.get("schema_version") == "missionos_px4_aerial_execution_policy.v1"
             and policy.get("execution_scope") == "px4_sitl"
             and policy.get("static_scene") is True, "bounded_sitl_policy_required")
    provenance = manifest["px4_provenance"]
    _require(isinstance(policy.get("session_id"), str)
             and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", policy["session_id"])
             and policy["session_id"] == provenance["session_id"]
             and policy["scene_sha256"] == provenance["scene_geometry_sha256"], "policy_source_binding_mismatch")
    authorization = policy["authorization"]
    _require(authorization.get("kind") == "explicit_user_instruction"
             and isinstance(authorization.get("instruction_ref"), str)
             and bool(authorization["instruction_ref"].strip())
             and authorization.get("hardware_allowed") is False,
             "existing_user_instruction_authorization_required")
    _sha(authorization["instruction_sha256"])
    _require(_number(authorization["authorized_at_unix_s"]) <= now
             <= _number(authorization["expires_at_unix_s"]), "authorization_expired_or_future")
    origin, yaw = provenance["current_vehicle_local_ned_m"], provenance["yaw_ned_rad"]
    allowed = policy["allowed_candidates"]
    _require(isinstance(allowed, list) and len(allowed) == 2
             and {c["candidate_id"] for c in allowed} == set(IDS), "policy_candidates_mismatch")
    targets = {}
    for candidate, right in zip(manifest["candidates"], (-5.0, 5.0)):
        supplied = next(c for c in allowed if c["candidate_id"] == candidate["candidate_id"])
        target = [origin[0] - math.sin(yaw) * right, origin[1] + math.cos(yaw) * right, origin[2]]
        _require(supplied["candidate_sha256"] == candidate["candidate_sha256"]
                 and supplied.get("independent_sdf_path_clear") is True
                 and _distance(_vector(supplied["target_local_ned_m"]), target) <= 1e-5,
                 "independent_geometry_or_target_binding_failed")
        targets[candidate["candidate_id"]] = target
    fence = policy["geofence"]
    for axis in ("north", "east"):
        low, high = _number(fence[f"{axis}_min_m"]), _number(fence[f"{axis}_max_m"])
        _require(-6 <= low < high <= 6, "geofence_exceeds_sitl_boundary")
    low, high = _number(fence["altitude_min_m"]), _number(fence["altitude_max_m"])
    _require(2.8 <= low < high <= 3.2, "altitude_exceeds_sitl_boundary")
    for target in [origin, *targets.values()]:
        _require(fence["north_min_m"] <= target[0] <= fence["north_max_m"]
                 and fence["east_min_m"] <= target[1] <= fence["east_max_m"]
                 and low <= -target[2] <= high, "candidate_outside_geofence")
    return targets


def _revalidate(manifest, policy, telemetry, captured_at, now):
    targets = _policy_targets(manifest, policy, now)
    _require(0 <= now - captured_at <= MAX_INPUT_AGE, "original_model_input_stale_or_future")
    _require(telemetry.get("session_id") == policy["session_id"]
             and telemetry.get("phase") == "holding"
             and telemetry.get("hardware_target") is False
             and telemetry.get("scene_static_verified") is True
             and telemetry.get("armed") is True
             and type(telemetry.get("px4_main_mode")) is int and telemetry["px4_main_mode"] == 6
             and telemetry.get("scene_sha256") == policy["scene_sha256"], "current_session_or_scene_mismatch")
    _require(0 <= now - _number(telemetry["observed_at_unix_s"]) <= MAX_TELEMETRY_AGE,
             "current_telemetry_stale_or_future")
    provenance = manifest["px4_provenance"]
    _require(_distance(_vector(telemetry["local_ned_pose_m"]), provenance["current_vehicle_local_ned_m"]) <= 0.2,
             "hover_moved_since_model_observation")
    delta = math.remainder(_number(telemetry["yaw_ned_rad"]) - provenance["yaw_ned_rad"], 2 * math.pi)
    _require(abs(delta) <= 0.05, "hover_yaw_changed_since_model_observation")
    _require(_distance(_vector(telemetry["local_ned_velocity_mps"]), [0, 0, 0]) <= 0.2, "hover_not_stationary")
    return targets


def evaluate(result, policy, *, judge, telemetry_reader, now_fn=time.time, expected_model_sha256=MODEL_SHA256):
    # Freeze inputs before any remote judgment; later source reads cannot renew
    # the timestamps or change the model's already evaluated candidate set.
    result, policy = json.loads(json.dumps([result, policy], allow_nan=False))
    manifest, outputs, captured_at = validate_result(result, expected_model_sha256=expected_model_sha256)
    model_identity_sha256 = prediction_digest(validate_model_identity(result["model"], expected_model_sha256))
    _revalidate(manifest, policy, telemetry_reader(), captured_at, now_fn())
    binding = PredictionBinding("EmbodiedCity/ANWM", expected_model_sha256,
                                "missionos.px4.sitl.image_goal_trial.v1", prediction_digest(policy),
                                "px4_gazebo_authorized_hover_trial.v1", manifest["schema_version"])
    request = PredictionRequest("px4_aerial_" + result["input_manifest_sha256"],
                                "captured_" + manifest["px4_provenance"]["history"]["capture_sha256"],
                                captured_at, binding,
                                {"input_manifest_sha256": result["input_manifest_sha256"],
                                 "model_identity_sha256": model_identity_sha256,
                                 "candidate_plans_sha256": manifest["candidate_plans_sha256"]},
                                tuple(PredictionOption(c["candidate_id"], c["horizon_seconds"],
                                                       {"candidate_sha256": c["candidate_sha256"]})
                                      for c in manifest["candidates"]))

    class Predictor:
        def __init__(self):
            self.binding = binding
            self.request_sha256 = request.digest()

        def predict(self, current):
            _require(current.digest() == self.request_sha256, "recorded_input_changed")
            return tuple(OptionForecast(o.option_id, o.horizon_seconds, None, _goal_state(outputs[o.option_id]))
                         for o in current.options)

    registry = PredictionRegistry()
    registry.register(Predictor())
    forecast = registry.forecast(request, now=now_fn(), max_age_seconds=MAX_INPUT_AGE)
    envelope = capture_prediction_evidence(request, forecast, execution_id=policy["session_id"],
                                          state_revision=result["input_manifest_sha256"],
                                          source_ref="px4_capture:" + manifest["px4_provenance"]["history"]["capture_sha256"])
    situation = MissionSituation(
        situation_id="px4_candidate_" + result["input_manifest_sha256"],
        observed_at=manifest["px4_provenance"]["history"]["observed_at"],
        mission_contract={"prediction_contract": binding.mission_contract,
                          "objective": "Select image-goal compatibility between two already specified lateral candidates.",
                          "response_mapping": {"continue": "Prefer left_5m for image-goal compatibility only.",
                                               "replan": "Prefer right_5m for image-goal compatibility only.",
                                               "operator_escalation": "Evidence is inadequate or contradictory; dispatch neither candidate."}},
        progress={}, observations={"source_kind": SOURCE_KIND, "candidate_ids": list(IDS)},
        constraints={"prediction_context": envelope["context"], "execution_scope": "px4_sitl",
                     "collision_risk_assessed": False, "horizon_seconds_nominal": True,
                     "model_time_alignment_verified": False,
                     "claim_limits": "Image similarity is not collision risk, flight feasibility, approval, or observed completion. Independent rules and existing user authorization govern any later dispatch."},
        uncertainty={}, source_refs=(envelope["context"]["source_ref"],),
        source_schema_version=manifest["schema_version"], input_digest=request.digest(),
        execution_scope="px4_sitl", allowed_response_kinds=("continue", "replan", "operator_escalation"),
    )
    situation, admission = receive_prediction_evidence(situation, envelope, now=now_fn(), max_age_seconds=MAX_INPUT_AGE)
    _require(admission["status"] == "adopted", "model_evidence_not_admitted")
    proposal = MissionAssuranceAgent(judge).evaluate(situation).to_dict()
    _require(proposal["judgment_status"] == "proposal_guardrail_passed"
             and proposal["proposed_response_kind"] in ("continue", "replan")
             and proposal["parameters"] == {}
             and proposal["model_inference_invoked"] is True, "jev_primary_failed_or_escalated")
    jev_receipt = proposal["model_invocation_evidence"]
    _require(jev_receipt.get("provider") == "typesafe"
             and jev_receipt.get("invocation_kind") == "decision_api"
             and jev_receipt.get("review_signal") == "bounded"
             and jev_receipt.get("dispatch_authority_created") is False, "jev_primary_receipt_required")
    now = now_fn()
    telemetry = telemetry_reader()
    targets = _revalidate(manifest, policy, telemetry, captured_at, now)
    candidate_id = "left_5m" if proposal["proposed_response_kind"] == "continue" else "right_5m"
    candidate = next(c for c in manifest["candidates"] if c["candidate_id"] == candidate_id)
    command = {"schema_version": "missionos_px4_aerial_candidate_command.v1",
               "session_id": policy["session_id"], "command_id": uuid.uuid4().hex,
               "action": "fly_candidate", "candidate_id": candidate_id,
               "candidate_sha256": candidate["candidate_sha256"], "target_local_ned_m": targets[candidate_id],
               "scene_sha256": policy["scene_sha256"],
               "model_receipt_sha256": prediction_digest(result["runtime_invocation_evidence"]),
               "jev_receipt_sha256": prediction_digest(jev_receipt),
               "approval_receipt_sha256": prediction_digest(policy["authorization"]),
               "approved_instruction_ref": policy["authorization"]["instruction_ref"],
               "issued_at_unix_s": now, "expires_at_unix_s": now + 5.0}
    return {"schema_version": "missionos_px4_aerial_wam_jev_evaluation.v1",
            "request": asdict(request), "forecast": forecast, "admission": admission,
            "jev_proposal": proposal,
            "rule_receipt": {"schema_version": "missionos_px4_aerial_rule_evaluation.v1", "decision": "permit",
                             "policy_sha256": prediction_digest(policy), "telemetry_sha256": prediction_digest(telemetry),
                             "input_manifest_sha256": result["input_manifest_sha256"], "evaluated_at_unix_s": now,
                             "model_identity_sha256": model_identity_sha256,
                             "authorization_basis": "provided_explicit_user_instruction", "approval_generated": False,
                             "collision_risk_from_model_used": False, "dispatch_invoked": False},
            "command": command, "command_sha256": prediction_digest(command),
            "model_rerun_during_admission": False, "physical_execution_invoked": False,
            "completion_claimed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--telemetry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--command-output", type=Path, required=True)
    parser.add_argument("--secret-project", required=True)
    parser.add_argument("--jev-secret-name", default="jev-api-key")
    args = parser.parse_args()
    _require(not args.output.exists() and not args.command_output.exists(), "output_files_must_be_new")
    result, policy = json.loads(args.result.read_text()), json.loads(args.policy.read_text())
    manifest, _, captured_at = validate_result(result)
    def read_telemetry():
        return json.loads(args.telemetry.read_text())
    _revalidate(manifest, policy, read_telemetry(), captured_at, time.time())
    secret = subprocess.run(["gcloud", "secrets", "versions", "access", "latest", "--project",
                             args.secret_project, "--secret", args.jev_secret_name],
                            check=True, capture_output=True, text=True).stdout.strip()
    _require(bool(secret), "empty_jev_secret")
    os.environ["TYPESAFE_API_KEY"] = secret
    output = evaluate(result, policy, judge=JevAssuranceJudge(), telemetry_reader=read_telemetry)
    for path, value in ((args.output, output), (args.command_output, output["command"])):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as handle:
            handle.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"candidate_id": output["command"]["candidate_id"], "rule_decision": "permit",
                      "dispatch_invoked": False, "expires_at_unix_s": output["command"]["expires_at_unix_s"]}))


if __name__ == "__main__":
    main()

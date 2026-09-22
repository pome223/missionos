"""Bounded dispatch decisions with synthetic receipts and fixture Jev transport.

No aircraft, real model, API, Secret Manager, or user approval is invoked here.
"""

from copy import deepcopy

import numpy as np
import pytest

from missionos_core.prediction import prediction_digest
from scripts.aerial_anwm_runtime import MODEL_REVISION, UPSTREAM_REVISION, VAE_REVISION, target_pose_from_delta
from scripts.evaluate_px4_aerial_wam_jev import evaluate, validate_result
from src.intelligence.jev_assurance import JevAssuranceJudge

MODEL = prediction_digest("fixture-only checkpoint")
SCENE = prediction_digest("fixture-only independently screened scene")


@pytest.fixture
def trial():
    origin = np.eye(4)
    origin[:3, :3] = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]
    origin[:3, 3] = [0.2, 0.0, -3.0]
    candidates = []
    for identifier, right in (("left_5m", -5.0), ("right_5m", 5.0)):
        candidate = {"candidate_id": identifier, "delta_local_m_rad": [0.0, right, 0.0, 0.0],
                     "target_camera_pose": target_pose_from_delta(np, origin, np.array([0, right, 0, 0])).tolist(),
                     "horizon_seconds": 1.0}
        candidate["candidate_sha256"] = prediction_digest(candidate)
        candidates.append(candidate)
    artifact_hashes = [prediction_digest(["fixture", i]) for i in range(16)]
    timing = {"simulation_time_ns": list(range(0, 16 * 250_000_000, 250_000_000)),
              "metadata_semantics": "Gazebo_simulation_nanoseconds", "expected_interval_seconds": 0.25,
              "measured_frame_intervals_seconds": [0.25] * 15, "maximum_interval_error_seconds": 0.0}
    manifest = {
        "schema_version": "missionos_aerial_anwm_input.v1", "source_kind": "px4_gazebo_frozen_capture",
        "asset_npz_sha256": prediction_digest("fixture-only NPZ"), "context_size": 16,
        "delta_frame": "body_frd_at_observation", "future_ground_truth_used_for_forecast": False,
        "goal_image_used_for_scoring_only": True, "physical_frame_timing_verified": False,
        "horizon_seconds_nominal": True, "simulation_frame_timing_verified": True,
        "model_time_alignment_verified": False, "num_timesteps": 4, "seed": 42, "diffusion_steps": 10,
        "source_timing": timing, "candidates": candidates, "candidate_plans_sha256": prediction_digest(candidates),
        "px4_provenance": {
            "session_id": "synthetic-session", "control_hold_kind": "px4_offboard_hover",
            "scene_geometry_sha256": SCENE, "current_vehicle_local_ned_m": [0.0, 0.0, -3.0],
            "yaw_ned_rad": 0.0, "source_assets_reverified": True,
            "history": {"role": "historical_observation", "capture_scope": "px4_sitl_airborne_observation",
                        "capture_sha256": prediction_digest("fixture-only capture"),
                        "registration_sha256": prediction_digest("fixture-only registration"),
                        "flight_session_status_sha256": prediction_digest("fixture-only source status"),
                        "capture_completed_at": "1970-01-01T00:01:40+00:00",
                        "observed_at": "1970-01-01T00:01:40+00:00", "observed_at_unix_s": 100.0,
                        "last_camera_optical_to_local_ned": origin.tolist(),
                        **{key: artifact_hashes[:] for key in ("frame_npz_sha256", "frame_rgb_raw_sha256",
                                                             "frame_depth_raw_sha256", "frame_calibration_sha256")}},
            "goal_reference": {"schema_version": "missionos_aerial_goal_reference.v1",
                               "role": "declared_goal_reference", "source_kind": "actual_gazebo_static_camera",
                               "future_ground_truth_used_for_forecast": False,
                               "flight_outcome_observed": False, "scene_geometry_sha256": SCENE,
                               "reference_manifest_sha256": prediction_digest("fixture-only goal manifest"),
                               "rgb_sha256": prediction_digest("fixture-only goal RGB")},
        },
    }
    outputs = [{"candidate_id": c["candidate_id"], "candidate_sha256": c["candidate_sha256"],
                "goal_mse": cost, "projection_goal_mse": 0.1,
                "predicted_image_sha256": prediction_digest([c["candidate_id"], "fixture prediction"])}
               for c, cost in zip(candidates, [0.3, 0.05])]
    result = {"schema_version": "aerial_anwm_result.v1", "input_manifest": manifest,
              "input_manifest_sha256": prediction_digest(manifest), "score_is_calibrated_risk": False,
              "model": {"model_id": "EmbodiedCity/ANWM", "checkpoint_sha256": MODEL, "context_size": 16,
                        "model_revision": MODEL_REVISION, "upstream_revision": UPSTREAM_REVISION,
                        "vae_repository": "stabilityai/sd-vae-ft-ema", "vae_revision": VAE_REVISION},
              "candidates": outputs, "runtime_invocation_evidence": {
                  "schema_version": "runtime_invocation_evidence.v1", "fixture_invocation": False,
                  "model_execution_verified": True, "actual_model_calls": 2,
                  "execution_scope": "px4_gazebo_frozen_candidate_forecast", "source_kind": "px4_gazebo_frozen_capture",
                  "physical_execution_observed": False, "physical_frame_timing_verified": False,
                  "horizon_seconds_nominal": True, "context_size": 16,
                  "input_manifest_sha256": prediction_digest(manifest), "model_sha256": MODEL,
                  "forecasts_sha256": prediction_digest(outputs), "source_timing": deepcopy(timing),
                  "seed": 42, "sampling_steps": 10}}
    policy = {"schema_version": "missionos_px4_aerial_execution_policy.v1", "execution_scope": "px4_sitl",
              "static_scene": True, "session_id": "synthetic-session", "scene_sha256": SCENE,
              "authorization": {"kind": "explicit_user_instruction", "instruction_ref": "fixture:synthetic-authorization",
                                "instruction_sha256": prediction_digest("fixture only, not an actual approval"),
                                "hardware_allowed": False, "authorized_at_unix_s": 90.0, "expires_at_unix_s": 300.0},
              "allowed_candidates": [{"candidate_id": c["candidate_id"], "candidate_sha256": c["candidate_sha256"],
                                      "target_local_ned_m": [0.0, c["delta_local_m_rad"][1], -3.0],
                                      "independent_sdf_path_clear": True} for c in candidates],
              "geofence": {"north_min_m": -6, "north_max_m": 6, "east_min_m": -6, "east_max_m": 6,
                           "altitude_min_m": 2.8, "altitude_max_m": 3.2}}
    telemetry = {"session_id": "synthetic-session", "phase": "holding", "hardware_target": False,
                 "scene_static_verified": True, "armed": True, "px4_main_mode": 6,
                 "scene_sha256": SCENE, "observed_at_unix_s": 149.5, "local_ned_pose_m": [0.0, 0.0, -3.0],
                 "local_ned_velocity_mps": [0.0, 0.0, 0.0], "yaw_ned_rad": 0.0}
    return result, policy, telemetry


def fixture_judge(choice="replan", *, mutate=None, prompts=None, review="bounded"):
    def transport(payload):
        if prompts is not None:
            prompts.append(deepcopy(payload))
        if mutate:
            mutate()
        return {"model": "fixture-jev-no-api-call", "answers": {
            "response": {"type": "choice", "choice": choice, "confidence": 1.0,
                         "probabilities": {kind: float(kind == choice) for kind in ("continue", "replan", "operator_escalation")}},
            "review": {"type": "choice", "choice": review}}}
    return JevAssuranceJudge(transport=transport)


def call(trial, *, judge=None, now_fn=lambda: 150.0):
    result, policy, telemetry = trial
    return evaluate(result, policy, judge=judge or fixture_judge(), telemetry_reader=lambda: deepcopy(telemetry),
                    now_fn=now_fn, expected_model_sha256=MODEL)


def test_bounded_command_uses_existing_authorization_and_exact_vehicle_target(trial):
    prompts = []
    output = call(trial, judge=fixture_judge(prompts=prompts))
    command = output["command"]
    assert command["candidate_id"] == "right_5m"
    assert command["target_local_ned_m"] == [0.0, 5.0, -3.0]  # Not the camera's +0.2 m offset.
    assert command["expires_at_unix_s"] - command["issued_at_unix_s"] == 5.0
    assert command["approval_receipt_sha256"] == prediction_digest(trial[1]["authorization"])
    assert command["model_receipt_sha256"] == prediction_digest(trial[0]["runtime_invocation_evidence"])
    assert output["command_sha256"] == prediction_digest(command)
    assert output["request"]["state"]["model_identity_sha256"] == prediction_digest(trial[0]["model"])
    assert output["rule_receipt"]["model_identity_sha256"] == prediction_digest(trial[0]["model"])
    assert output["admission"]["status"] == "adopted"
    assert output["rule_receipt"]["approval_generated"] is False
    assert output["rule_receipt"]["dispatch_invoked"] is False
    assert output["physical_execution_invoked"] is False
    assert output["completion_claimed"] is False
    assert all(c["risk_score"] is None for c in output["forecast"]["forecasts"])
    prompt = prompts[0]["state"]["mission_situation"]
    assert prompt["constraints"]["collision_risk_assessed"] is False
    assert "input_manifest" not in json_text(prompt)
    assert "fixture:synthetic-authorization" not in json_text(prompt)


def json_text(value):
    import json
    return json.dumps(value)


@pytest.mark.parametrize("mutation", [
    lambda t: t[1]["authorization"].update(kind="generated_by_model"),
    lambda t: t[1]["authorization"].update(hardware_allowed=True),
    lambda t: t[1]["authorization"].update(expires_at_unix_s=149),
    lambda t: t[1].update(execution_scope="hardware"),
    lambda t: t[1].update(static_scene=False),
    lambda t: t[1]["allowed_candidates"][0].update(independent_sdf_path_clear=False),
    lambda t: t[1]["allowed_candidates"][0].update(target_local_ned_m=[0, -4, -3]),
    lambda t: t[1]["geofence"].update(east_max_m=100),
    lambda t: t[2].update(scene_sha256="e" * 64),
    lambda t: t[2].update(session_id="other-session"),
    lambda t: t[2].update(phase="moving"),
    lambda t: t[2].update(scene_static_verified=False),
    lambda t: t[2].update(armed=False),
    lambda t: t[2].update(px4_main_mode=4),
    lambda t: t[2].update(observed_at_unix_s=147),
    lambda t: t[2].update(local_ned_pose_m=[0.3, 0, -3]),
    lambda t: t[2].update(local_ned_velocity_mps=[0, 0, 0.21]),
    lambda t: t[2].update(yaw_ned_rad=0.06),
])
def test_policy_or_current_state_rejection_happens_before_jev(trial, mutation):
    mutation(trial)
    prompts = []
    with pytest.raises(ValueError):
        call(trial, judge=fixture_judge(prompts=prompts))
    assert prompts == []


@pytest.mark.parametrize("field,value", [("local_ned_pose_m", [0.3, 0, -3]), ("observed_at_unix_s", 147),
                                         ("scene_sha256", "e" * 64), ("yaw_ned_rad", 0.06)])
def test_live_state_is_revalidated_after_jev_instead_of_reusing_prejudgment_snapshot(trial, field, value):
    judge = fixture_judge(mutate=lambda: trial[2].update({field: value}))
    with pytest.raises(ValueError):
        call(trial, judge=judge)


def test_later_telemetry_does_not_renew_original_model_observation(trial):
    clock = [150.0]
    def delay():
        clock[0] = 281.0
        trial[2]["observed_at_unix_s"] = 281.0
    with pytest.raises(ValueError, match="original_model_input_stale"):
        call(trial, judge=fixture_judge(mutate=delay), now_fn=lambda: clock[0])


def test_late_capture_serialization_does_not_make_old_observation_fresh(trial):
    result, _, telemetry = trial
    result["input_manifest"]["px4_provenance"]["history"]["capture_completed_at"] = "1970-01-01T00:04:41+00:00"
    result["input_manifest_sha256"] = prediction_digest(result["input_manifest"])
    result["runtime_invocation_evidence"]["input_manifest_sha256"] = result["input_manifest_sha256"]
    telemetry["observed_at_unix_s"] = 281.0
    prompts = []
    with pytest.raises(ValueError, match="original_model_input_stale"):
        call(trial, judge=fixture_judge(prompts=prompts), now_fn=lambda: 281.0)
    assert prompts == []


@pytest.mark.parametrize("choice", ["operator_escalation", "invented_action"])
def test_primary_jev_failure_or_escalation_has_no_fallback_command(trial, choice):
    with pytest.raises(ValueError, match="jev_primary_failed_or_escalated"):
        call(trial, judge=fixture_judge(choice=choice))


def test_jev_review_signal_does_not_create_a_dispatch_command(trial):
    with pytest.raises(ValueError, match="jev_primary_receipt_required"):
        call(trial, judge=fixture_judge(review="review"))


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(input_manifest_sha256="e" * 64),
    lambda r: r["candidates"][0].update(goal_mse=0.001),
    lambda r: r["model"].update(checkpoint_sha256="e" * 64),
    lambda r: r["runtime_invocation_evidence"].update(actual_model_calls=0),
    lambda r: r["runtime_invocation_evidence"].update(source_kind="public_dataset_replay"),
])
def test_altered_model_receipt_rejected(trial, mutation):
    mutation(trial[0])
    with pytest.raises(ValueError):
        validate_result(trial[0], expected_model_sha256=MODEL)


@pytest.mark.parametrize("field", ["model_revision", "upstream_revision", "vae_revision", "vae_repository"])
@pytest.mark.parametrize("missing", [False, True])
def test_full_model_identity_cannot_change_with_unchanged_invocation_receipt(trial, field, missing):
    result = trial[0]
    receipt = deepcopy(result["runtime_invocation_evidence"])
    if missing:
        result["model"].pop(field)
    else:
        result["model"][field] = "other/model" if field == "vae_repository" else "0" * 40
    with pytest.raises(ValueError, match="model_identity_mismatch"):
        validate_result(result, expected_model_sha256=MODEL)
    assert result["runtime_invocation_evidence"] == receipt


@pytest.mark.parametrize("defect", ["camera_yaw", "vehicle_origin", "goal_is_future_outcome", "goal_is_history"])
def test_source_semantic_contradictions_are_rejected_even_after_rehashing(trial, defect):
    result = trial[0]
    provenance = result["input_manifest"]["px4_provenance"]
    if defect == "camera_yaw":
        provenance["yaw_ned_rad"] = 0.2
    elif defect == "vehicle_origin":
        provenance["current_vehicle_local_ned_m"][0] = 10.0
    elif defect == "goal_is_future_outcome":
        provenance["goal_reference"]["future_ground_truth_used_for_forecast"] = True
    elif defect == "goal_is_history":
        provenance["goal_reference"]["reference_manifest_sha256"] = provenance["history"]["capture_sha256"]
    result["input_manifest_sha256"] = prediction_digest(result["input_manifest"])
    result["runtime_invocation_evidence"]["input_manifest_sha256"] = result["input_manifest_sha256"]
    with pytest.raises(ValueError):
        validate_result(result, expected_model_sha256=MODEL)

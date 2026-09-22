"""Synthetic receipt parsing and fixture Jev transport; no model/API execution.

Receipt fields below deliberately simulate the producer contract. They are not
runtime evidence: checkpoint/image hashes and all forecast costs are fixtures.
"""

from copy import deepcopy
from dataclasses import replace

import pytest

from missionos_core.prediction import (
    PredictionBinding,
    PredictionOption,
    PredictionRequest,
    prediction_digest,
)
from scripts.evaluate_aerial_wam_jev import RecordedAerialPredictor, evaluate, validate_result
from src.intelligence.jev_assurance import JevAssuranceJudge
from src.intelligence.prediction_evidence import AUTHORITY_FLAGS

FIXTURE_MODEL_HASH = prediction_digest("unit-test-only synthetic checkpoint")


def result_fixture():
    candidates = []
    for identifier, distance in (("forward", 1.0), ("alternative", 0.0)):
        candidate = {
            "candidate_id": identifier,
            "delta_local_m_rad": [distance, 0.0, 0.0, 0.0],
            "target_camera_pose": [
                [1.0, 0.0, 0.0, distance],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ],
            "horizon_seconds": 1.0,
        }
        candidate["candidate_sha256"] = prediction_digest(candidate)
        candidates.append(candidate)
    manifest = {
        "schema_version": "missionos_aerial_anwm_input.v1",
        "request_id": "unit-test-fixture",
        "source_kind": "public_dataset_replay",
        "source_dataset": "EmbodiedCity/ANWM-Dataset",
        "source_revision": "f" * 40,
        "source_trajectory": "fixture-trajectory",
        "public_provenance": {
            "dataset_repository": "EmbodiedCity/ANWM-Dataset",
            "dataset_revision": "f" * 40,
            "trajectory": "fixture-trajectory",
            "context_frame_indices": list(range(16)),
            "goal_frame_index": 19,
        },
        "asset_npz_sha256": prediction_digest("fixture assets"),
        "delta_frame": "body_frd_at_observation",
        "context_size": 16,
        "num_timesteps": 4,
        "frame_interval_seconds": 0.25,
        "frame_interval_source": "ANWM public benchmark 4 fps",
        "physical_frame_timing_verified": False,
        "horizon_seconds_nominal": True,
        "source_timing": {
            "metadata_field": "timestamps",
            "metadata_semantics": "source_csv_row_indices",
            "source_row_indices": list(range(11, 31)),
            "nominal_input_fps": 4,
            "nominal_fps_source": "upstream infer.py --input_fps default",
            "model_time_basis": "frame_index_offset_divided_by_128",
        },
        "candidates": candidates,
        "seed": 42,
        "diffusion_steps": 10,
        "goal_image_used_for_scoring_only": True,
        "future_ground_truth_used_for_forecast": False,
    }
    input_hash = prediction_digest(manifest)
    outputs = [
        {
            "candidate_id": candidate["candidate_id"],
            "candidate_sha256": candidate["candidate_sha256"],
            "goal_mse": goal_cost,
            "projection_goal_mse": baseline_cost,
            "predicted_image": candidate["candidate_id"] + ".png",
            "predicted_image_sha256": prediction_digest([candidate["candidate_id"], "fixture-prediction"]),
            "projection_image": candidate["candidate_id"] + "-projection.png",
            "projection_image_sha256": prediction_digest([candidate["candidate_id"], "fixture-projection"]),
            "elapsed_seconds": 0.1,
        }
        for candidate, goal_cost, baseline_cost in zip(candidates, [0.8, 0.05], [0.02, 0.4])
    ]
    return {
        "schema_version": "aerial_anwm_result.v1",
        "input_manifest": manifest,
        "input_manifest_sha256": input_hash,
        "model": {
            "model_id": "EmbodiedCity/ANWM",
            "checkpoint_sha256": FIXTURE_MODEL_HASH,
            "context_size": 16,
            "model_revision": "fixture-model-revision",
            "upstream_revision": "fixture-code-revision",
            "vae_repository": "stabilityai/sd-vae-ft-ema",
            "vae_revision": "fixture-vae-revision",
        },
        "candidates": outputs,
        "current_frame_goal_mse": 0.2,
        "score_is_calibrated_risk": False,
        "runtime_invocation_evidence": {
            "schema_version": "runtime_invocation_evidence.v1",
            "fixture_invocation": False,
            "model_execution_verified": True,
            "actual_model_calls": 2,
            "context_size": 16,
            "seed": 42,
            "sampling_steps": 10,
            "input_manifest_sha256": input_hash,
            "model_sha256": FIXTURE_MODEL_HASH,
            "forecasts_sha256": prediction_digest(outputs),
            "execution_scope": "public_dataset_offline_candidate_forecast",
            "px4_runtime_invoked": False,
            "physical_execution_observed": False,
            "physical_frame_timing_verified": False,
            "horizon_seconds_nominal": True,
            "source_timing": deepcopy(manifest["source_timing"]),
        },
    }


def test_null_risk_forecasts_admitted_with_learned_and_projection_choices_separate():
    result = result_fixture()
    output = evaluate(result, expected_model_sha256=FIXTURE_MODEL_HASH)
    assert output["admission"]["status"] == "adopted"
    assert output["forecast"]["request_sha256"] == prediction_digest(output["request"])
    assert output["request"]["state"]["input_manifest"] == result["input_manifest"]
    assert output["request"]["state"]["input_manifest"]["physical_frame_timing_verified"] is False
    assert output["request"]["state"]["input_manifest"]["horizon_seconds_nominal"] is True
    assert output["request"]["binding"]["model_sha256"] == FIXTURE_MODEL_HASH
    assert output["comparison"] == {
        "learned_image_goal_choice": "alternative",
        "projection_only_goal_choice": "forward",
        "choice_differs": True,
        "learned_model_superiority_established": False,
    }
    assert output["jev_proposal"] is None
    assert output["model_rerun_during_admission"] is False
    assert output["clock_kind"] == "offline_replay_no_live_freshness"
    for forecast in output["forecast"]["forecasts"]:
        assert forecast["risk_score"] is None
        assert forecast["future_state"]["goal_compatibility"]["risk_assessed"] is False
    assert all(output[key] is False for key in AUTHORITY_FLAGS)


def test_fixture_jev_receives_predictions_without_observed_future_or_flight_authority():
    captured = []

    def fixture_transport(payload):
        captured.append(deepcopy(payload))
        return {
            "model": "fixture-jev-no-api-call",
            "answers": {
                "response": {
                    "type": "choice",
                    "choice": "replan",
                    "confidence": 0.8,
                    "probabilities": {"continue": 0.1, "replan": 0.8, "operator_escalation": 0.1},
                },
                "review": {"type": "choice", "choice": "bounded"},
            },
        }

    output = evaluate(
        result_fixture(), expected_model_sha256=FIXTURE_MODEL_HASH,
        judge=JevAssuranceJudge(transport=fixture_transport),
    )
    assert len(captured) == 1
    situation = captured[0]["state"]["mission_situation"]
    assert situation["observations"]["source_kind"] == "public_dataset_replay"
    assert "future_state" not in situation["observations"]
    assert "collision_probability" not in situation["observations"]
    assert situation["constraints"]["collision_risk_assessed"] is False
    assert situation["constraints"]["execution_allowed"] is False
    assert situation["constraints"]["physical_frame_timing_verified"] is False
    assert situation["constraints"]["horizon_seconds_nominal"] is True
    evidence = situation["uncertainty"]["prediction_evidence"]
    assert evidence["verification_basis"] == "model_inferred"
    assert evidence["receipt"]["feasibility_established"] is False
    assert [f["future_state"]["goal_compatibility"]["raw_cost"] for f in evidence["forecasts"]] == [0.8, 0.05]
    for forecast in evidence["forecasts"]:
        assert forecast["risk_score"] is None
        assert forecast["future_state"]["source_kind"] == "recorded_model_inference"
        assert forecast["future_state"]["live_px4_observation"] is False
    proposal = output["jev_proposal"]
    assert proposal["proposed_response_kind"] == "replan"
    assert proposal["judgment_status"] == "proposal_guardrail_passed"
    assert proposal["parameters"] == {}
    assert proposal["model_invocation_evidence"]["model_id"] == "fixture-jev-no-api-call"
    for key in ("approval_recorded", "dispatch_authority_created", "physical_execution_invoked", "progress_counted"):
        assert proposal[key] is False
    assert all(output[key] is False for key in AUTHORITY_FLAGS)


def test_recorded_predictor_rejects_changed_request_instead_of_reusing_old_forecasts():
    result = result_fixture()
    binding = PredictionBinding("fixture", FIXTURE_MODEL_HASH, "m", "p", "e", "i")
    request = PredictionRequest(
        "request", "observation", 0.0, binding, {"input_manifest": result["input_manifest"]},
        tuple(PredictionOption(c["candidate_id"], c["horizon_seconds"], c) for c in result["input_manifest"]["candidates"]),
    )
    predictor = RecordedAerialPredictor(request, {c["candidate_id"]: c for c in result["candidates"]})
    for changed in (
        replace(request, observation_id="different"),
        replace(request, binding=replace(binding, model_sha256="f" * 64)),
        replace(request, options=tuple(reversed(request.options))),
        replace(request, state={"input_manifest": "changed"}),
    ):
        with pytest.raises(ValueError, match="aerial_recorded_prediction_input_changed"):
            predictor.predict(changed)


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(schema_version="unknown_result.v1"),
    lambda r: r.update(input_manifest_sha256="f" * 64),
    lambda r: r["model"].update(checkpoint_sha256="f" * 64),
    lambda r: r["model"].update(model_id="different-model"),
    lambda r: r["candidates"][0].update(candidate_id="different-candidate"),
    lambda r: r["candidates"][0].update(candidate_sha256="f" * 64),
    lambda r: r["candidates"][0].update(predicted_image_sha256="not-a-hash"),
    lambda r: r["candidates"][0].update(projection_image_sha256="not-a-hash"),
    lambda r: r["candidates"][0].update(goal_mse=float("nan")),
    lambda r: r["candidates"][0].update(goal_mse=0.01),
    lambda r: r["candidates"][0].update(projection_goal_mse=True),
    lambda r: r.update(score_is_calibrated_risk=True),
    lambda r: r["runtime_invocation_evidence"].update(fixture_invocation=True),
    lambda r: r["runtime_invocation_evidence"].update(input_manifest_sha256="f" * 64),
    lambda r: r["runtime_invocation_evidence"].update(model_sha256="f" * 64),
    lambda r: r["runtime_invocation_evidence"].update(forecasts_sha256="f" * 64),
    lambda r: r["runtime_invocation_evidence"].update(model_execution_verified=False),
    lambda r: r["runtime_invocation_evidence"].update(actual_model_calls=0),
    lambda r: r["runtime_invocation_evidence"].update(px4_runtime_invoked=True),
    lambda r: r["runtime_invocation_evidence"].update(physical_execution_observed=True),
    lambda r: r["runtime_invocation_evidence"].update(physical_frame_timing_verified=True),
    lambda r: r["runtime_invocation_evidence"].update(horizon_seconds_nominal=False),
    lambda r: r["runtime_invocation_evidence"]["source_timing"].update(nominal_input_fps=30),
    lambda r: r["runtime_invocation_evidence"].update(seed=43),
    lambda r: r["runtime_invocation_evidence"].update(seed=True),
    lambda r: r["runtime_invocation_evidence"].update(sampling_steps=250),
    lambda r: r["runtime_invocation_evidence"].update(sampling_steps=True),
])
def test_invalid_result_identity_and_claims_rejected_before_judge(mutation):
    result = result_fixture()
    mutation(result)

    class NeverCalledJudge:
        def judge(self, prompt):
            pytest.fail("invalid artifact must be rejected before judging")

    with pytest.raises(ValueError):
        evaluate(result, expected_model_sha256=FIXTURE_MODEL_HASH, judge=NeverCalledJudge())


@pytest.mark.parametrize("key,value", [
    ("schema_version", "unknown_manifest.v1"),
    ("source_kind", "live_px4_observation"),
    ("future_ground_truth_used_for_forecast", True),
    ("goal_image_used_for_scoring_only", False),
    ("physical_frame_timing_verified", True),
    ("physical_frame_timing_verified", 0),
    ("horizon_seconds_nominal", False),
    ("horizon_seconds_nominal", 1),
    ("seed", True),
    ("seed", -1),
    ("seed", 2**32),
    ("diffusion_steps", True),
    ("diffusion_steps", 0),
    ("diffusion_steps", 251),
])
def test_rehashed_manifest_with_wrong_source_or_leaked_future_is_rejected(key, value):
    result = result_fixture()
    result["input_manifest"][key] = value
    if key == "seed":
        result["runtime_invocation_evidence"]["seed"] = value
    if key == "diffusion_steps":
        result["runtime_invocation_evidence"]["sampling_steps"] = value
    result["input_manifest_sha256"] = prediction_digest(result["input_manifest"])
    result["runtime_invocation_evidence"]["input_manifest_sha256"] = result["input_manifest_sha256"]
    with pytest.raises(ValueError):
        validate_result(result, FIXTURE_MODEL_HASH)


@pytest.mark.parametrize("manifest_key,receipt_key", [("seed", "seed"), ("diffusion_steps", "sampling_steps")])
def test_runtime_boolean_does_not_equal_integer_sampling_configuration(manifest_key, receipt_key):
    result = result_fixture()
    result["input_manifest"][manifest_key] = 1
    result["input_manifest_sha256"] = prediction_digest(result["input_manifest"])
    result["runtime_invocation_evidence"].update(
        input_manifest_sha256=result["input_manifest_sha256"], **{receipt_key: True}
    )
    with pytest.raises(ValueError):
        validate_result(result, FIXTURE_MODEL_HASH)


def test_rehashed_candidate_must_retain_its_action_digest():
    result = result_fixture()
    result["input_manifest"]["candidates"][0]["delta_local_m_rad"][0] = 9.0
    result["input_manifest_sha256"] = prediction_digest(result["input_manifest"])
    result["runtime_invocation_evidence"]["input_manifest_sha256"] = result["input_manifest_sha256"]
    with pytest.raises(ValueError):
        validate_result(result, FIXTURE_MODEL_HASH)


@pytest.mark.parametrize("section", ["input_manifest", "model", "runtime_invocation_evidence"])
@pytest.mark.parametrize("context_size", [4, True, None])
def test_released_checkpoint_requires_sixteen_context_frames_in_every_receipt(section, context_size):
    result = result_fixture()
    result[section]["context_size"] = context_size
    if section == "input_manifest":
        result["input_manifest_sha256"] = prediction_digest(result["input_manifest"])
        result["runtime_invocation_evidence"]["input_manifest_sha256"] = result["input_manifest_sha256"]
    with pytest.raises(ValueError):
        validate_result(result, FIXTURE_MODEL_HASH)


@pytest.mark.parametrize("private_field", ["candidate_id", "extra_field"])
def test_private_path_or_unreviewed_candidate_field_cannot_reach_jev(private_field):
    result = result_fixture()
    candidate = result["input_manifest"]["candidates"][0]
    candidate[private_field] = "/private/fixture-only/never-send-to-jev"
    body = {key: value for key, value in candidate.items() if key != "candidate_sha256"}
    candidate["candidate_sha256"] = prediction_digest(body)
    result["candidates"][0]["candidate_id"] = candidate["candidate_id"]
    result["candidates"][0]["candidate_sha256"] = candidate["candidate_sha256"]
    result["input_manifest_sha256"] = prediction_digest(result["input_manifest"])
    result["runtime_invocation_evidence"].update(
        input_manifest_sha256=result["input_manifest_sha256"],
        forecasts_sha256=prediction_digest(result["candidates"]),
    )

    class NeverCalledJudge:
        def judge(self, prompt):
            pytest.fail("private path must be rejected before Jev sees the prompt")

    with pytest.raises(ValueError):
        evaluate(result, expected_model_sha256=FIXTURE_MODEL_HASH, judge=NeverCalledJudge())

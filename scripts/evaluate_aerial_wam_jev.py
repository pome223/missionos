"""Admit a hash-bound, offline ANWM result and optionally ask the real Jev judge.

This is a dataset replay, never a source of PX4 dispatch authority. Image-goal
compatibility is not collision risk. The result is produced separately by
``aerial_anwm_runtime.py``; loading it here does not rerun the world model.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import re
import subprocess

from missionos_core.prediction import (
    OptionForecast,
    PredictionBinding,
    PredictionOption,
    PredictionRegistry,
    PredictionRequest,
    prediction_digest,
)
from src.intelligence.jev_assurance import JevAssuranceJudge
from src.intelligence.mission_assurance_agent import MissionAssuranceAgent, MissionSituation
from src.intelligence.prediction_evidence import (
    AUTHORITY_FLAGS,
    capture_prediction_evidence,
    receive_prediction_evidence,
)
from scripts.aerial_anwm_runtime import validate_model_identity


ANWM_CHECKPOINT_SHA256 = "bdd149cac6ec002ba7dc4ad99ec6f9eb02cd6d4f05320195cf174737b13b0bc2"
POLICY = {
    "scope": "offline_public_aerial_dataset_replay",
    "objective": "Compare image-goal compatibility for two supplied candidate poses.",
    "collision_risk_assessed": False,
    "execution_allowed": False,
}


def _cost(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid_goal_cost")
    return value


def validate_result(result, expected_model_sha256):
    if result.get("schema_version") != "aerial_anwm_result.v1":
        raise ValueError("aerial_result_schema_mismatch")
    if result.get("score_is_calibrated_risk") is not False:
        raise ValueError("aerial_goal_cost_is_not_calibrated_risk")
    validate_model_identity(result.get("model"), expected_model_sha256)
    manifest = result["input_manifest"]
    if manifest.get("schema_version") != "missionos_aerial_anwm_input.v1":
        raise ValueError("aerial_input_schema_mismatch")
    if result["input_manifest_sha256"] != prediction_digest(manifest):
        raise ValueError("aerial_input_manifest_hash_mismatch")
    if manifest["source_kind"] != "public_dataset_replay":
        raise ValueError("aerial_replay_source_required")
    if (
        manifest.get("source_dataset") != "EmbodiedCity/ANWM-Dataset"
        or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_revision", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("asset_npz_sha256", "")))
        or manifest.get("delta_frame") != "body_frd_at_observation"
        or manifest.get("future_ground_truth_used_for_forecast") is not False
        or manifest.get("goal_image_used_for_scoring_only") is not True
        or manifest.get("physical_frame_timing_verified") is not False
        or manifest.get("horizon_seconds_nominal") is not True
        or type(manifest.get("context_size")) is not int
        or manifest["context_size"] != 16
    ):
        raise ValueError("aerial_input_provenance_required")
    if (
        result["model"].get("model_id") != "EmbodiedCity/ANWM"
        or type(result["model"].get("context_size")) is not int
        or result["model"]["context_size"] != 16
    ):
        raise ValueError("aerial_model_identity_mismatch")
    if (
        type(manifest.get("num_timesteps")) is not int
        or not 1 <= manifest["num_timesteps"] <= 128
        or type(manifest.get("frame_interval_seconds")) not in (int, float)
        or manifest["frame_interval_seconds"] != 0.25
    ):
        raise ValueError("invalid_aerial_nominal_timing")
    if result["model"]["checkpoint_sha256"] != expected_model_sha256:
        raise ValueError("aerial_model_hash_mismatch")
    if (
        type(manifest.get("seed")) is not int
        or not 0 <= manifest["seed"] < 2**32
        or type(manifest.get("diffusion_steps")) is not int
        or not 1 <= manifest["diffusion_steps"] <= 250
        or not isinstance(manifest.get("source_timing"), dict)
    ):
        raise ValueError("invalid_aerial_sampling_configuration")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_model_sha256):
        raise ValueError("invalid_expected_model_hash")
    candidates = manifest["candidates"]
    if len(candidates) != 2 or len({x["candidate_id"] for x in candidates}) != 2:
        raise ValueError("aerial_replay_requires_two_distinct_candidates")
    outputs = result["candidates"]
    if len(outputs) != 2 or {x["candidate_id"] for x in outputs} != {
        x["candidate_id"] for x in candidates
    }:
        raise ValueError("aerial_result_candidates_mismatch")
    for candidate in candidates:
        if not isinstance(candidate["candidate_id"], str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,80}", candidate["candidate_id"]
        ):
            raise ValueError("invalid_aerial_candidate_id")
        candidate_body = {k: v for k, v in candidate.items() if k != "candidate_sha256"}
        if (
            set(candidate_body) != {
                "candidate_id", "delta_local_m_rad", "target_camera_pose", "horizon_seconds"
            }
            or candidate.get("candidate_sha256") != prediction_digest(candidate_body)
        ):
            raise ValueError("aerial_candidate_hash_mismatch")
        delta = candidate["delta_local_m_rad"]
        pose = candidate["target_camera_pose"]
        if (
            not isinstance(delta, list) or len(delta) != 4
            or not isinstance(pose, list) or len(pose) != 4
            or any(not isinstance(row, list) or len(row) != 4 for row in pose)
            or any(type(n) not in (int, float) or not math.isfinite(n)
                   for n in [*delta, *(n for row in pose for n in row)])
        ):
            raise ValueError("invalid_aerial_candidate_geometry")
        horizon = candidate["horizon_seconds"]
        if type(horizon) not in (int, float) or not math.isfinite(horizon) or horizon <= 0:
            raise ValueError("invalid_aerial_horizon")
        if not math.isclose(horizon, manifest["num_timesteps"] * 0.25):
            raise ValueError("aerial_nominal_horizon_mismatch")
    for output in outputs:
        candidate = next(x for x in candidates if x["candidate_id"] == output["candidate_id"])
        if output.get("candidate_sha256") != candidate["candidate_sha256"]:
            raise ValueError("aerial_output_candidate_hash_mismatch")
        _cost(output["goal_mse"])
        _cost(output["projection_goal_mse"])
        if not re.fullmatch(r"[0-9a-f]{64}", output["predicted_image_sha256"]):
            raise ValueError("invalid_aerial_prediction_hash")
    invocation = result["runtime_invocation_evidence"]
    if (
        invocation.get("schema_version") != "runtime_invocation_evidence.v1"
        or invocation.get("fixture_invocation") is not False
        or invocation.get("model_execution_verified") is not True
        or type(invocation.get("actual_model_calls")) is not int
        or invocation["actual_model_calls"] != len(candidates)
        or invocation.get("execution_scope") != "public_dataset_offline_candidate_forecast"
        or invocation.get("px4_runtime_invoked") is not False
        or invocation.get("physical_execution_observed") is not False
        or invocation.get("physical_frame_timing_verified") is not False
        or invocation.get("horizon_seconds_nominal") is not True
        or type(invocation.get("context_size")) is not int
        or invocation["context_size"] != 16
    ):
        raise ValueError("learned_aerial_runtime_receipt_required")
    if invocation.get("input_manifest_sha256") != result["input_manifest_sha256"]:
        raise ValueError("aerial_runtime_input_hash_mismatch")
    if invocation.get("model_sha256") != expected_model_sha256:
        raise ValueError("aerial_runtime_model_hash_mismatch")
    if invocation.get("forecasts_sha256") != prediction_digest(outputs):
        raise ValueError("aerial_runtime_forecasts_hash_mismatch")
    if (
        invocation.get("source_timing") != manifest["source_timing"]
        or type(invocation.get("seed")) is not int
        or invocation["seed"] != manifest["seed"]
        or type(invocation.get("sampling_steps")) is not int
        or invocation["sampling_steps"] != manifest["diffusion_steps"]
    ):
        raise ValueError("aerial_runtime_configuration_mismatch")
    return manifest, {x["candidate_id"]: x for x in outputs}


class RecordedAerialPredictor:
    """Replay only the exact inputs of an already completed model invocation."""

    def __init__(self, request, outputs):
        self.binding = request.binding
        self.request_sha256 = request.digest()
        self.outputs = outputs

    def predict(self, request):
        if request.digest() != self.request_sha256:
            raise ValueError("aerial_recorded_prediction_input_changed")
        return tuple(
            OptionForecast(
                option.option_id,
                option.horizon_seconds,
                None,
                {
                    "goal_compatibility": {
                        "schema_version": "missionos_goal_compatibility.v1",
                        "metric_id": "anwm_goal_image_mse",
                        "raw_cost": self.outputs[option.option_id]["goal_mse"],
                        "lower_is_better": True,
                        "risk_assessed": False,
                    },
                    "predicted_image_sha256": self.outputs[option.option_id][
                        "predicted_image_sha256"
                    ],
                    "source_kind": "recorded_model_inference",
                    "live_px4_observation": False,
                    "physical_frame_timing_verified": False,
                    "horizon_seconds_nominal": True,
                },
            )
            for option in request.options
        )


def evaluate(result, *, expected_model_sha256=ANWM_CHECKPOINT_SHA256, judge=None):
    manifest, outputs = validate_result(result, expected_model_sha256)
    digest = prediction_digest(manifest)
    candidates = manifest["candidates"]
    binding = PredictionBinding(
        "EmbodiedCity/ANWM",
        expected_model_sha256,
        "missionos.aerial.image_goal.v1",
        prediction_digest(POLICY),
        "airvln_dataset_replay.v1",
        "missionos_aerial_anwm_input.v1",
    )
    request = PredictionRequest(
        "aerial_replay_" + digest,
        "aerial_input_" + digest,
        0.0,
        binding,
        {"input_manifest": manifest, "clock_kind": "offline_replay_no_live_freshness",
         "model_identity_sha256": prediction_digest(validate_model_identity(result["model"], expected_model_sha256))},
        tuple(
            PredictionOption(x["candidate_id"], x["horizon_seconds"], dict(x))
            for x in candidates
        ),
    )
    registry = PredictionRegistry()
    registry.register(RecordedAerialPredictor(request, outputs))
    forecast = registry.forecast(request, now=0.0)
    envelope = capture_prediction_evidence(
        request,
        forecast,
        execution_id="aerial_replay_" + digest,
        state_revision=digest,
        source_ref="public_dataset_replay:" + digest,
    )
    first, second = [x["candidate_id"] for x in candidates]
    situation = MissionSituation(
        situation_id="aerial_goal_replay_" + digest,
        observed_at="1970-01-01T00:00:00+00:00",
        mission_contract={
            "prediction_contract": binding.mission_contract,
            "objective": POLICY["objective"],
            "response_mapping": {
                "continue": f"Prefer the already specified candidate {first} for image-goal compatibility only.",
                "replan": f"Prefer the already specified alternative {second} for image-goal compatibility only.",
                "operator_escalation": "Image-goal comparison evidence is missing or contradictory.",
            },
        },
        progress={"task_id": "offline_aerial_goal_comparison"},
        observations={
            "source_kind": "public_dataset_replay",
            "candidate_ids": [first, second],
            "metric_semantics": "Mean squared RGB difference to a supplied goal image; lower is better. This evaluates visual goal compatibility only.",
        },
        constraints={
            "prediction_context": envelope["context"],
            **POLICY,
            "physical_frame_timing_verified": False,
            "horizon_seconds_nominal": True,
            "time_basis": f"{manifest['num_timesteps']} source frame steps, interpreted at the upstream nominal 4 fps. Source timestamps are frame indices; physical duration is unverified.",
            "claim_limits": "No collision assessment, live observation, feasible flight, approval, dispatch, or mission completion is established. Do not infer them from image similarity.",
        },
        uncertainty={},
        source_refs=(envelope["context"]["source_ref"],),
        source_schema_version="missionos_aerial_anwm_input.v1",
        input_digest=digest,
        execution_scope="fixture",
        allowed_response_kinds=("continue", "replan", "operator_escalation"),
    )
    admitted, admission = receive_prediction_evidence(
        situation, envelope, now=0.0, max_age_seconds=1.0
    )
    if admission["status"] != "adopted":
        raise ValueError("aerial_replay_prediction_not_admitted")
    proposal = MissionAssuranceAgent(judge).evaluate(admitted).to_dict() if judge else None
    learned_choice = min(outputs, key=lambda key: outputs[key]["goal_mse"])
    baseline_choice = min(outputs, key=lambda key: outputs[key]["projection_goal_mse"])
    return {
        "schema_version": "missionos_aerial_wam_jev_replay.v1",
        "scope": "offline_public_dataset_replay",
        "request": asdict(request),
        "forecast": forecast,
        "admission": admission,
        "recorded_model_invocation": result["runtime_invocation_evidence"],
        "model_rerun_during_admission": False,
        "clock_kind": "offline_replay_no_live_freshness",
        "comparison": {
            "learned_image_goal_choice": learned_choice,
            "projection_only_goal_choice": baseline_choice,
            "choice_differs": learned_choice != baseline_choice,
            "learned_model_superiority_established": False,
        },
        "jev_proposal": proposal,
        "jev_mode": "primary" if judge else "off",
        "approval_status": "not_requested_replay_only",
        **{key: False for key in AUTHORITY_FLAGS},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-model-sha256", default=ANWM_CHECKPOINT_SHA256)
    parser.add_argument("--jev-mode", choices=("off", "primary"), default="off")
    parser.add_argument("--secret-project")
    parser.add_argument("--jev-secret-name", default="jev-api-key")
    args = parser.parse_args()
    result = json.loads(args.result.read_text())
    # Reject malformed/foreign results before any Secret Manager or API access.
    validate_result(result, args.expected_model_sha256)
    judge = None
    if args.jev_mode == "primary":
        if args.secret_project:
            secret = subprocess.run(
                ["gcloud", "secrets", "versions", "access", "latest", "--project",
                 args.secret_project, "--secret", args.jev_secret_name],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            if not secret:
                raise ValueError("empty_jev_secret")
            os.environ["TYPESAFE_API_KEY"] = secret
        judge = JevAssuranceJudge()
    output = evaluate(result, expected_model_sha256=args.expected_model_sha256, judge=judge)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"scope": output["scope"], "admission": output["admission"]["status"],
                      "jev_mode": output["jev_mode"], "authority_created": False}))


if __name__ == "__main__":
    main()

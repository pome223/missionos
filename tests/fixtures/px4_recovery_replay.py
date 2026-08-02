from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from src.runtime.px4_gazebo_route.recovery_intent_compiler import (
    build_runtime_recovery_intent,
    compile_runtime_recovery_intent,
    verify_runtime_recovery_outcome,
    verify_runtime_recovery_reachability,
)


def _policy() -> dict:
    return {
        "policy_ref": "fixture_px4_recovery_replay_policy",
        "max_recovery_duration_s": 75.0,
        "max_recovery_horizontal_speed_mps": 10.0,
        "max_recovery_vertical_speed_mps": 3.0,
        "max_reroute_target_abs_m": 5000.0,
        "reachability_duration_margin_factor": 1.25,
        "reachability_setup_seconds": 5.0,
        "wind_uncertainty_floor_mps": 1.0,
    }


def _telemetry(*, x: float) -> dict:
    return {
        "position": {
            "local_x_m": x,
            "local_y_m": 0.0,
            "altitude_above_home_m": 30.0,
        },
        "wind": {"speed_mps": 1.5},
        "telemetry": {"stale": False},
        "battery": {
            "endurance_projection": {"projected_insufficient_for_route": False}
        },
    }


def _canonical_sha256(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _epoch(*, index: int, target_x: float, target_y: float) -> tuple[dict, dict, dict]:
    observed_at = f"2026-07-20T00:0{index}:00+00:00"
    parameters = {
        "target_x_m": target_x,
        "target_y_m": target_y,
        "target_altitude_m": 45.0,
    }
    intent = build_runtime_recovery_intent(
        agent_output={
            "strategy": "local_avoidance",
            "selected_bounded_action": "avoid_obstacle",
            "proposed_parameters": parameters,
            "intent_constraints": {
                "avoidance_side": "right",
                "minimum_clearance_m": 30.0,
                "maximum_duration_s": 75.0,
            },
            "requires_human_approval": True,
        },
        observed_at=observed_at,
        decision_signature=f"fixture_decision_epoch_{index}",
    )
    compilation = compile_runtime_recovery_intent(
        intent=intent,
        candidate={
            "selected_bounded_action": "avoid_obstacle",
            "proposed_parameters": parameters,
            "basis": {
                "avoidance_side": "right",
                "minimum_lateral_clearance_m": 32.0,
            },
            "source_refs": ["fixture.telemetry", "fixture.obstacle"],
        },
        recovery_policy=_policy(),
    )
    reachability = verify_runtime_recovery_reachability(
        compilation=compilation,
        telemetry_snapshot=_telemetry(x=target_x - 60.0),
        recovery_policy=_policy(),
    )
    proposal_id = f"runtime_recovery_proposal_fixture_{index}"
    approval_id = f"runtime_recovery_maneuver_approval_fixture_{index}"
    proposal = {
        "schema_version": "missionos_runtime_recovery_proposal_evidence.v2",
        "proposal_id": proposal_id,
        "task_id": "task_private_fixture_source",
        "proposal_status": "dispatch_authority_bound",
        "observed_at": observed_at,
        "valid_until": f"2026-07-20T00:0{index}:59+00:00",
        "sample_index": index * 10,
        "decision_signature_version": "semantic_v2",
        "recovery_decision_signature": f"fixture_decision_epoch_{index}",
        "proposal_origin": {
            "schema_version": "missionos_runtime_recovery_proposal_origin.v1",
            "origin_kind": "hosted_llm",
            "provider": "openai_compatible_deepseek",
            "model_id": "deepseek-v4-flash",
            "contains_prompt_or_response_text": False,
        },
        "proposal_origin_sha256": f"fixture_origin_{index}",
        "proposal_source": "hosted_runtime_recovery_judgment",
        "hosted_model_invoked_for_proposal": True,
        "hosted_model_judgment_used_for_proposal": True,
        "recovery_intent": intent,
        "intent_compilation": compilation,
        "reachability_verification": reachability,
        "claimed_by_approval_ref": approval_id,
        "dispatch_status": "queued_for_active_runner",
        "dispatch_authority_created": True,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    approval = {
        "schema_version": "missionos_runtime_recovery_maneuver_approval.v1",
        "approval_id": approval_id,
        "task_id": "task_private_fixture_source",
        "operator_approval_performed": True,
        "approved_recovery_action": "avoid_obstacle",
        "approved_parameters": parameters,
        "operator_surface": "missionos_runtime_recovery",
        "explicit_recovery_dispatch_approval": True,
        "approval_free_recovery_dispatch_allowed": False,
        "delivery_completion_claimed": False,
        "progress_counted": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "approved_at": f"2026-07-20T00:0{index}:10+00:00",
    }
    receipt_payload = {
        "schema_version": "missionos_runtime_recovery_dispatch_receipt.v1",
        "task_id": "task_private_fixture_source",
        "dispatch_status": "queued_for_active_runner",
        "recovery_action": "avoid_obstacle",
        "recovery_parameters": parameters,
        "operator_approved": True,
        "explicit_recovery_dispatch_approval": True,
        "maneuver_approval": approval,
        "active_runner_request_queued": True,
        "blocked_reasons": [],
        "proposal_revalidation": {
            "schema_version": "missionos_runtime_recovery_proposal_revalidation.v1",
            "validation_status": "valid",
            "proposal_id": proposal_id,
            "action_matches": True,
            "parameters_match": True,
            "intent_compiler_contract_required": True,
            "recovery_intent_id": intent["recovery_intent_id"],
            "recovery_compilation_id": compilation["recovery_compilation_id"],
            "stored_recovery_reachability_id": reachability[
                "recovery_reachability_id"
            ],
            "telemetry_fresh": True,
            "reasons": [],
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
        "dispatch_authority_created": True,
        "delivery_completion_claimed": False,
        "progress_counted": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "observed_at": f"2026-07-20T00:0{index}:10+00:00",
    }
    receipt_hash = _canonical_sha256(receipt_payload)
    receipt = {
        **receipt_payload,
        "dispatch_receipt_sha256": receipt_hash,
        "dispatch_receipt_id": f"runtime_recovery_dispatch_receipt_{receipt_hash[:12]}",
    }
    resume = {
        "verification_status": "verified",
        "resume_auto_authorized": True,
        "route_rejoin_verified": True,
    }
    outcome = verify_runtime_recovery_outcome(
        action="avoid_obstacle",
        recovery_observation={
            "command_ack_observed": True,
            "assist_attempted": True,
            "target_reached": True,
            "resume_status": "resumed_auto_mission",
            "resume_safety_verification": resume,
        },
        dispatch_authority_created=True,
    )
    attempt = {
        "schema_version": "missionos_runtime_recovery_attempt_evidence.v1",
        "attempt_id": f"runtime_recovery_attempt_fixture_{index}",
        "task_id": "task_private_fixture_source",
        "source_proposal_id": proposal_id,
        "proposal_origin_sha256": f"fixture_origin_{index}",
        "observed_at": f"2026-07-20T00:0{index}:30+00:00",
        "sample_index": index * 10 + 5,
        "attempt_status": "succeeded",
        "recovery_action": "avoid_obstacle",
        "recovery_parameters": parameters,
        "position": {
            "local_x_m": target_x,
            "local_y_m": target_y,
            "local_z_m": -45.0,
            "altitude_above_home_m": 45.0,
        },
        "command_ack_observed": True,
        "assist_attempted": True,
        "assist_status": "target_reached",
        "target_reached": True,
        "target_distance_m": 1.2,
        "resume_status": "resumed_auto_mission",
        "resume_auto_attempted": True,
        "resume_safety_verification": resume,
        "outcome_verification": outcome,
        "outcome_verification_id": outcome["recovery_outcome_verification_id"],
        "outcome_verification_sha256": outcome[
            "recovery_outcome_verification_sha256"
        ],
        "dispatch_authority_created": True,
        "simulator_execution_observed": True,
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    return proposal, receipt, attempt


def build_fixture_task() -> dict:
    proposal_1, receipt_1, attempt_1 = _epoch(index=1, target_x=300.0, target_y=50.0)
    proposal_2, receipt_2, attempt_2 = _epoch(index=2, target_x=430.0, target_y=50.0)
    return {
        "task_id": "task_private_fixture_source",
        "owner_session_id": "private-session-not-for-publication",
        "kind": "px4_gazebo_mission_designer_sitl_execution_request",
        "status": "completed",
        "artifacts": {
            "private_debug": {
                "artifact_dir": "/Users/private/operator/evidence",
                "api_key": "sk-never-publish-fixture",
            },
            "missionos_runtime_recovery_proposals": {
                proposal_1["proposal_id"]: proposal_1,
                proposal_2["proposal_id"]: proposal_2,
            },
            "missionos_runtime_recovery_dispatch_receipts": {
                receipt_1["dispatch_receipt_id"]: receipt_1,
                receipt_2["dispatch_receipt_id"]: receipt_2,
            },
            "missionos_runtime_recovery_dispatch_receipt": receipt_2,
            "missionos_runtime_recovery_attempts": {
                attempt_1["attempt_id"]: attempt_1,
                attempt_2["attempt_id"]: attempt_2,
            },
            "missionos_auto_mission_runtime_replay": {
                "schema_version": "missionos_auto_mission_runtime_replay.v1",
                "flight_path_trace_path": "/Users/private/operator/pose.jsonl",
                "raw_sample_count": 4,
                "flight_path_profile": [
                    {
                        "sample_index": 0,
                        "phase": "route",
                        "latitude_deg": 35.6812,
                        "longitude_deg": 139.7671,
                        "local_x_m": 0.0,
                        "local_y_m": 0.0,
                        "local_z_m": -30.0,
                        "relative_alt_m": 30.0,
                        "elapsed_s": 0.0,
                        "battery_remaining_percent": 100.0,
                    },
                    {
                        "sample_index": 1,
                        "phase": "recovery",
                        "latitude_deg": 35.682,
                        "longitude_deg": 139.768,
                        "local_x_m": 300.0,
                        "local_y_m": 50.0,
                        "local_z_m": -45.0,
                        "relative_alt_m": 45.0,
                        "elapsed_s": 60.0,
                        "battery_remaining_percent": 90.0,
                    },
                    {
                        "sample_index": 2,
                        "phase": "recovery",
                        "latitude_deg": 35.683,
                        "longitude_deg": 139.769,
                        "local_x_m": 430.0,
                        "local_y_m": 50.0,
                        "local_z_m": -45.0,
                        "relative_alt_m": 45.0,
                        "elapsed_s": 120.0,
                        "battery_remaining_percent": 82.0,
                    },
                    {
                        "sample_index": 3,
                        "phase": "return",
                        "latitude_deg": 35.6812,
                        "longitude_deg": 139.7671,
                        "local_x_m": 0.0,
                        "local_y_m": 0.0,
                        "local_z_m": 0.0,
                        "relative_alt_m": 0.0,
                        "elapsed_s": 240.0,
                        "battery_remaining_percent": 70.0,
                    },
                ],
            },
            "missionos_auto_mission_runtime_monitor_summary": {
                "return_progress_observed": True,
                "landed": True,
            },
            "missionos_auto_mission_waypoint_gate_summary": {
                "route_completed_claimed": True,
            },
            "missionos_auto_mission_dropoff_gate_summary": {
                "dropoff_verified": True,
            },
            "missionos_auto_mission_sitl_delivery_gate_summary": {
                "sitl_delivery_claimed": False,
            },
            "missionos_auto_mission_payload_release_sim_gate_summary": {
                "payload_release_observed_sim": True,
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_fixture_task(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

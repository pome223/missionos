#!/usr/bin/env python3
"""Runtime smoke for PX4/Gazebo fleet-memory Part 2 feedback loop."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os

from src.runtime.px4_gazebo_delivery_mission_control import (
    PX4GazeboDeliveryMissionFailureType,
    PX4GazeboDeliveryMissionPhase,
    build_px4_gazebo_delivery_mission_contract,
    run_px4_gazebo_delivery_mission_v1,
)
from src.runtime.px4_gazebo_fleet_memory import (
    run_px4_gazebo_fleet_memory_feedback_simulation,
)

OPT_IN_ENV = "RUN_PX4_GAZEBO_FLEET_MEMORY_PART2_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo fleet memory Part 2 smoke."
        )


def main() -> int:
    _require_opt_in()
    contract = build_px4_gazebo_delivery_mission_contract(
        route_plan_refs=(
            "px4_gazebo_pickup_dropoff_route_plan:pickup_to_waypoint",
            "px4_gazebo_pickup_dropoff_route_plan:waypoint_alpha_to_bravo",
            "px4_gazebo_pickup_dropoff_route_plan:waypoint_to_dropoff",
        ),
        waypoint_refs=(
            "gazebo_waypoint:alpha",
            "gazebo_waypoint:bravo",
            "gazebo_waypoint:charlie",
        ),
        now=NOW,
    )
    happy = run_px4_gazebo_delivery_mission_v1(
        mission_contract=contract,
        route_dispatch_refs=(
            "px4_gazebo_route_command_dispatch_result:leg_pickup_to_waypoint",
            "px4_gazebo_route_command_dispatch_result:leg_waypoint_alpha_to_bravo",
            "px4_gazebo_route_command_dispatch_result:leg_waypoint_to_dropoff",
        ),
        route_completion_gate_refs=(
            "px4_gazebo_route_delivery_completion_gate:leg_pickup_to_waypoint",
            "px4_gazebo_route_delivery_completion_gate:leg_waypoint_alpha_to_bravo",
            "px4_gazebo_route_delivery_completion_gate:leg_waypoint_to_dropoff",
        ),
        now=NOW,
    )
    blocked = run_px4_gazebo_delivery_mission_v1(
        mission_contract=contract,
        failure_phase=PX4GazeboDeliveryMissionPhase.DELIVERY_ROUTE,
        failure_type=PX4GazeboDeliveryMissionFailureType.POSE_DEVIATION,
        now=NOW,
    )
    artifacts = run_px4_gazebo_fleet_memory_feedback_simulation(
        happy_runner_result=happy["runner_result"],
        happy_replay_timeline=happy["replay_timeline"],
        blocked_runner_result=blocked["runner_result"],
        blocked_replay_timeline=blocked["replay_timeline"],
        mission_contract_ref=(
            f"px4_gazebo_delivery_mission_contract:{contract.mission_contract_id}"
        ),
        now=NOW,
    )
    summary = {
        "schema_version": "px4_gazebo_fleet_memory_part2_smoke.v1",
        "trajectory_summary_schema_version": artifacts["happy_summary"].schema_version,
        "route_segment_memory_schema_version": artifacts[
            "route_segment_memory"
        ].schema_version,
        "delivery_zone_memory_count": len(artifacts["delivery_zone_memories"]),
        "fleet_memory_snapshot_schema_version": artifacts[
            "fleet_memory_snapshot"
        ].schema_version,
        "feedback_candidate_status": artifacts[
            "feedback_candidate"
        ].candidate_status.value,
        "blocked_promotion_status": artifacts[
            "blocked_promotion_gate"
        ].promotion_status.value,
        "promoted_promotion_status": artifacts[
            "promoted_promotion_gate"
        ].promotion_status.value,
        "memory_informed_plan_schema_version": artifacts[
            "memory_informed_plan"
        ].schema_version,
        "memory_informed_plan_promotion_status": artifacts[
            "memory_informed_plan"
        ].promotion_status,
        "memory_informed_plan_operator_approval_performed": artifacts[
            "memory_informed_plan"
        ].operator_approval_performed,
        "memory_informed_plan_promoted_memory_refs": list(
            artifacts["memory_informed_plan"].promoted_memory_refs
        ),
        "memory_used_for_planning_only": artifacts[
            "memory_informed_plan"
        ].memory_used_for_planning_only,
        "memory_decision_trace": list(
            artifacts["memory_informed_plan"].memory_decision_trace
        ),
        "lead_drone_observation_schema_version": artifacts[
            "lead_drone_observation"
        ].schema_version,
        "followup_feedback_schema_version": artifacts[
            "followup_mission_feedback"
        ].schema_version,
        "fleet_learning_replay_case_ids": [
            case.case_id for case in artifacts["fleet_learning_replay"].cases
        ],
        "fleet_learning_corpus_coverage_labels": list(
            artifacts["fleet_learning_corpus"].required_coverage_labels
        ),
        "part2_finalization_schema_version": artifacts[
            "part2_finalization"
        ].schema_version,
        "part2_finalization_status": artifacts[
            "part2_finalization"
        ].finalization_status,
        "part2_layer_labels": list(artifacts["part2_finalization"].part2_layer_labels),
        "negative_case_labels": list(
            artifacts["part2_finalization"].negative_case_labels
        ),
        "memory_use_scope": artifacts["part2_finalization"].memory_use_scope,
        "memory_direct_command_authority_allowed": artifacts[
            "part2_finalization"
        ].memory_direct_command_authority_allowed,
        "memory_grants_dispatch_authority": artifacts[
            "part2_finalization"
        ].memory_grants_dispatch_authority,
        "approval_free_dispatch_allowed": artifacts[
            "part2_finalization"
        ].approval_free_dispatch_allowed,
        "approval_free_stronger_execution_allowed": artifacts[
            "part2_finalization"
        ].approval_free_stronger_execution_allowed,
        "hardware_target_allowed": artifacts[
            "part2_finalization"
        ].hardware_target_allowed,
        "physical_execution_invoked": artifacts[
            "part2_finalization"
        ].physical_execution_invoked,
        "px4_mission_upload_allowed": artifacts[
            "part2_finalization"
        ].px4_mission_upload_allowed,
        "unbounded_setpoint_stream_allowed": artifacts[
            "part2_finalization"
        ].unbounded_setpoint_stream_allowed,
        "arbitrary_gazebo_mutation_allowed": artifacts[
            "part2_finalization"
        ].arbitrary_gazebo_mutation_allowed,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    assert summary["feedback_candidate_status"] == "proposed"
    assert summary["blocked_promotion_status"] == "blocked"
    assert summary["promoted_promotion_status"] == "promoted"
    assert summary["memory_informed_plan_promotion_status"] == "promoted"
    assert summary["memory_informed_plan_operator_approval_performed"] is True
    assert summary["memory_informed_plan_promoted_memory_refs"]
    assert summary["memory_used_for_planning_only"] is True
    assert "memory_used_for_planning_not_dispatch" in summary["memory_decision_trace"]
    assert "stale_ignored" in summary["fleet_learning_replay_case_ids"]
    assert "contradictory_blocked" in summary["fleet_learning_replay_case_ids"]
    assert "outlier_not_adopted" in summary["fleet_learning_replay_case_ids"]
    assert "unsafe_rejected" in summary["fleet_learning_replay_case_ids"]
    assert "memory_not_authority" in summary["fleet_learning_corpus_coverage_labels"]
    assert summary["part2_finalization_status"] == "completed"
    assert "trajectory_summary" in summary["part2_layer_labels"]
    assert "memory_informed_planning" in summary["part2_layer_labels"]
    assert "fleet_learning_corpus" in summary["part2_layer_labels"]
    assert "stale_ignored" in summary["negative_case_labels"]
    assert "contradictory_blocked" in summary["negative_case_labels"]
    assert "outlier_not_adopted" in summary["negative_case_labels"]
    assert "unsafe_rejected" in summary["negative_case_labels"]
    assert summary["memory_use_scope"] == "planning_gates_risk_scoring_only"
    assert summary["memory_direct_command_authority_allowed"] is False
    assert summary["memory_grants_dispatch_authority"] is False
    assert summary["approval_free_dispatch_allowed"] is False
    assert summary["approval_free_stronger_execution_allowed"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["px4_mission_upload_allowed"] is False
    assert summary["unbounded_setpoint_stream_allowed"] is False
    assert summary["arbitrary_gazebo_mutation_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

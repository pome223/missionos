"""Fleet-memory and redacted-review contracts migrated from smoke wrappers."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.runtime.px4_gazebo_delivery_mission_control import (
    PX4GazeboDeliveryMissionFailureType,
    PX4GazeboDeliveryMissionPhase,
    build_px4_gazebo_delivery_mission_contract,
    run_px4_gazebo_delivery_mission_v1,
)
from src.runtime.px4_gazebo_fleet_memory import (
    run_px4_gazebo_fleet_memory_feedback_simulation,
)
from src.runtime.px4_gazebo_mission_review import (
    run_px4_gazebo_mission_control_review_report,
    write_px4_gazebo_mission_review_archive,
)


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def fleet_memory_artifacts():
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
    fleet_memory = run_px4_gazebo_fleet_memory_feedback_simulation(
        happy_runner_result=happy["runner_result"],
        happy_replay_timeline=happy["replay_timeline"],
        blocked_runner_result=blocked["runner_result"],
        blocked_replay_timeline=blocked["replay_timeline"],
        mission_contract_ref=(
            f"px4_gazebo_delivery_mission_contract:{contract.mission_contract_id}"
        ),
        now=NOW,
    )
    return happy, fleet_memory


def test_fleet_memory_stays_planning_only(fleet_memory_artifacts) -> None:
    _, artifacts = fleet_memory_artifacts
    plan = artifacts["memory_informed_plan"]
    finalization = artifacts["part2_finalization"]
    replay_case_ids = {case.case_id for case in artifacts["fleet_learning_replay"].cases}

    assert artifacts["feedback_candidate"].candidate_status.value == "proposed"
    assert artifacts["blocked_promotion_gate"].promotion_status.value == "blocked"
    assert artifacts["promoted_promotion_gate"].promotion_status.value == "promoted"
    assert plan.promotion_status == "promoted"
    assert plan.operator_approval_performed is True
    assert plan.promoted_memory_refs
    assert plan.memory_used_for_planning_only is True
    assert "memory_used_for_planning_not_dispatch" in plan.memory_decision_trace
    assert {
        "stale_ignored",
        "contradictory_blocked",
        "outlier_not_adopted",
        "unsafe_rejected",
    } <= replay_case_ids
    assert "memory_not_authority" in (
        artifacts["fleet_learning_corpus"].required_coverage_labels
    )
    assert finalization.finalization_status == "completed"
    assert finalization.memory_use_scope == "planning_gates_risk_scoring_only"
    assert finalization.memory_direct_command_authority_allowed is False
    assert finalization.memory_grants_dispatch_authority is False
    assert finalization.approval_free_dispatch_allowed is False
    assert finalization.approval_free_stronger_execution_allowed is False
    assert finalization.hardware_target_allowed is False
    assert finalization.physical_execution_invoked is False
    assert finalization.px4_mission_upload_allowed is False
    assert finalization.unbounded_setpoint_stream_allowed is False
    assert finalization.arbitrary_gazebo_mutation_allowed is False


def test_review_archive_is_redacted_and_does_not_grant_authority(
    fleet_memory_artifacts,
    tmp_path: Path,
) -> None:
    happy, fleet_memory = fleet_memory_artifacts
    artifacts = run_px4_gazebo_mission_control_review_report(
        runner_result=happy["runner_result"],
        replay_timeline=happy["replay_timeline"],
        fleet_memory_artifacts=fleet_memory,
        now=NOW,
    )
    report = artifacts["evidence_report"]
    replay_index = artifacts["replay_index"]
    safety = artifacts["safety_boundary_summary"]
    provenance = artifacts["fleet_memory_provenance_summary"]
    archive = write_px4_gazebo_mission_review_archive(
        output_dir=tmp_path / "review",
        report=report,
        replay_index=replay_index,
        safety_boundary_summary=safety,
        fleet_memory_provenance=provenance,
    )

    assert report.final_status == "completed"
    assert "all_required_phases_observed" in report.why_completed_or_blocked
    assert replay_index.event_count > 0
    assert len(report.evidence_chain) >= 4
    assert report.fleet_memory_provenance_ref is not None
    assert "Replay Timeline" in artifacts["redacted_html"]
    assert all(Path(path).exists() for path in archive.values())
    assert report.raw_logs_included is False
    assert report.sqlite_included is False
    assert report.full_telemetry_included is False
    assert report.reproduction_steps_included is False
    assert report.runtime_script_names_included is False
    assert report.transport_details_included is False
    assert report.low_level_command_details_included is False
    assert safety.hardware_target_allowed is False
    assert safety.physical_execution_invoked is False
    assert safety.px4_mission_upload_allowed is False
    assert safety.unbounded_setpoint_stream_allowed is False
    assert provenance.memory_direct_command_authority_allowed is False
    assert provenance.memory_grants_dispatch_authority is False
    assert provenance.memory_used_for_planning_only is True

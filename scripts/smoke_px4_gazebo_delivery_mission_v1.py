#!/usr/bin/env python3
"""Runtime smoke for PX4/Gazebo multi-phase delivery mission runner v1."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import tempfile

from src.runtime.px4_gazebo_delivery_mission_control import (
    DEFAULT_MISSION_PHASE_SEQUENCE,
    PX4GazeboDeliveryMissionFailureType,
    PX4GazeboDeliveryMissionPhase,
    attach_px4_gazebo_delivery_mission_v1_task,
    build_px4_gazebo_delivery_mission_contract,
    build_px4_gazebo_delivery_mission_golden_corpus,
    run_px4_gazebo_delivery_mission_v1,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_PX4_GAZEBO_DELIVERY_MISSION_V1_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo delivery mission v1 smoke."
        )


def _contract():
    return build_px4_gazebo_delivery_mission_contract(
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


def main() -> int:
    _require_opt_in()
    contract = _contract()
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
    failure = run_px4_gazebo_delivery_mission_v1(
        mission_contract=contract,
        failure_phase=PX4GazeboDeliveryMissionPhase.DELIVERY_ROUTE,
        failure_type=PX4GazeboDeliveryMissionFailureType.POSE_DEVIATION,
        now=NOW,
    )
    corpus = build_px4_gazebo_delivery_mission_golden_corpus(
        happy_runner_result=happy["runner_result"],
        failure_runner_result=failure["runner_result"],
        now=NOW,
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TaskStore(f"{tmpdir}/tasks.db")
        happy_task = store.create(
            kind="px4_gazebo_delivery_mission_runner_v1",
            title="PX4/Gazebo multi-phase mission happy path",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        failure_task = store.create(
            kind="px4_gazebo_delivery_mission_runner_v1",
            title="PX4/Gazebo multi-phase mission failure branch",
            status="running",
        )
        happy_updated = attach_px4_gazebo_delivery_mission_v1_task(
            happy_task["task_id"],
            mission_artifacts=happy,
            task_store_factory=lambda: store,
        )
        failure_updated = attach_px4_gazebo_delivery_mission_v1_task(
            failure_task["task_id"],
            mission_artifacts=failure,
            task_store_factory=lambda: store,
        )
    summary = {
        "schema_version": "px4_gazebo_delivery_mission_v1_smoke.v1",
        "contract_schema_version": contract.schema_version,
        "runner_schema_version": happy["runner_result"].schema_version,
        "phase_gate_evaluation_schema_version": happy["phase_gate_evaluations"][
            0
        ].schema_version,
        "recovery_decision_schema_version": failure["recovery_decisions"][
            0
        ].schema_version,
        "replay_schema_version": happy["replay_timeline"].schema_version,
        "golden_corpus_schema_version": corpus.schema_version,
        "happy_task_status": happy_updated["status"],
        "happy_final_status": happy["runner_result"].final_status.value,
        "happy_observed_phase_count": len(happy["runner_result"].observed_phases),
        "happy_waypoint_count": happy["runner_result"].waypoint_count,
        "happy_route_segment_count": happy["runner_result"].route_segment_count,
        "happy_dropoff_landing_error_m": happy["runner_result"].dropoff_landing_error_m,
        "happy_health_snapshot_count": len(happy["health_snapshots"]),
        "happy_phase_gate_verdicts": [
            item.verdict.value for item in happy["phase_gate_evaluations"]
        ],
        "happy_replay_event_count": len(happy["replay_timeline"].events),
        "failure_task_status": failure_updated["status"],
        "failure_final_status": failure["runner_result"].final_status.value,
        "failure_blocked_phase": failure["runner_result"].blocked_phase.value,
        "failure_blocked_reasons": list(failure["runner_result"].blocked_reasons),
        "failure_phase_gate_verdicts": [
            item.verdict.value for item in failure["phase_gate_evaluations"]
        ],
        "failure_recovery_decision_actions": [
            item.recovery_action.value for item in failure["recovery_decisions"]
        ],
        "policy_matrix_cell_count": len(happy["recovery_policy_matrix"].entries),
        "policy_matrix_expected_cell_count": len(DEFAULT_MISSION_PHASE_SEQUENCE)
        * len(PX4GazeboDeliveryMissionFailureType),
        "golden_corpus_case_count": len(corpus.case_ids),
        "golden_corpus_coverage_labels": list(corpus.required_coverage_labels),
        "mission_runner_api_version": happy["runner_result"].mission_runner_api_version,
        "hardware_target_allowed": happy["runner_result"].hardware_target_allowed,
        "physical_execution_invoked": happy["runner_result"].physical_execution_invoked,
        "px4_mission_upload_allowed": happy["runner_result"].px4_mission_upload_allowed,
        "unbounded_setpoint_stream_allowed": happy[
            "runner_result"
        ].unbounded_setpoint_stream_allowed,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    assert summary["happy_task_status"] == "completed"
    assert summary["happy_final_status"] == "completed"
    assert summary["happy_observed_phase_count"] == 10
    assert summary["happy_waypoint_count"] >= 3
    assert summary["happy_route_segment_count"] >= 3
    assert summary["happy_dropoff_landing_error_m"] <= 0.5
    assert summary["happy_health_snapshot_count"] == 20
    assert summary["failure_task_status"] == "blocked"
    assert summary["failure_final_status"] == "blocked"
    assert summary["failure_blocked_phase"] == "delivery_route"
    assert "mission_pose_deviation" in summary["failure_blocked_reasons"]
    assert set(summary["happy_phase_gate_verdicts"]) == {"pass"}
    assert "abort" in summary["failure_phase_gate_verdicts"]
    assert "land" in summary["failure_recovery_decision_actions"]
    assert (
        summary["policy_matrix_cell_count"]
        == summary["policy_matrix_expected_cell_count"]
    )
    assert summary["golden_corpus_case_count"] == 2
    assert "multi_waypoint_happy_path" in summary["golden_corpus_coverage_labels"]
    assert "failure_branching" in summary["golden_corpus_coverage_labels"]
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

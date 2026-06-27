#!/usr/bin/env python3
"""Part 1 finalization smoke for PX4/Gazebo multi-phase mission control."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os

from src.runtime.px4_gazebo_delivery_mission_control import (
    DEFAULT_MISSION_PHASE_SEQUENCE,
    PX4GazeboDeliveryMissionFailureType,
    PX4GazeboDeliveryMissionPhase,
    build_px4_gazebo_delivery_mission_contract,
    prepare_px4_gazebo_delivery_mission_v1,
    run_px4_gazebo_delivery_mission_v1,
)

OPT_IN_ENV = "RUN_PX4_GAZEBO_MISSION_PART1_FINALIZATION_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo mission Part 1 finalization smoke."
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
    prepared = prepare_px4_gazebo_delivery_mission_v1(
        mission_contract=contract,
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
    failures = {
        failure_type.value: run_px4_gazebo_delivery_mission_v1(
            mission_contract=contract,
            failure_phase=phase,
            failure_type=failure_type,
            now=NOW,
        )["runner_result"].final_status.value
        for failure_type, phase in {
            PX4GazeboDeliveryMissionFailureType.POSE_DEVIATION: (
                PX4GazeboDeliveryMissionPhase.DELIVERY_ROUTE
            ),
            PX4GazeboDeliveryMissionFailureType.BATTERY_LOW: (
                PX4GazeboDeliveryMissionPhase.DELIVERY_ROUTE
            ),
            PX4GazeboDeliveryMissionFailureType.LINK_LOSS: (
                PX4GazeboDeliveryMissionPhase.WAYPOINT_ROUTE
            ),
            PX4GazeboDeliveryMissionFailureType.GATE_BLOCKED: (
                PX4GazeboDeliveryMissionPhase.DROPOFF_APPROACH
            ),
            PX4GazeboDeliveryMissionFailureType.ACK_TIMEOUT: (
                PX4GazeboDeliveryMissionPhase.TAKEOFF
            ),
        }.items()
    }
    runner = happy["runner_result"]
    summary = {
        "schema_version": "px4_gazebo_mission_part1_finalization_smoke.v1",
        "contract_refs_complete": contract.contract_refs_complete,
        "prepared_run_schema_version": prepared["prepared_run"].schema_version,
        "happy_final_status": runner.final_status.value,
        "happy_waypoint_count": runner.waypoint_count,
        "happy_route_segment_count": runner.route_segment_count,
        "happy_dropoff_landing_error_m": runner.dropoff_landing_error_m,
        "health_snapshot_count": len(happy["health_snapshots"]),
        "phase_gate_evaluation_count": len(happy["phase_gate_evaluations"]),
        "phase_transition_event_count": len(happy["phase_transition_events"]),
        "inspection_schema_version": happy["mission_inspection"].schema_version,
        "policy_matrix_cell_count": len(happy["recovery_policy_matrix"].entries),
        "policy_matrix_expected_cell_count": len(DEFAULT_MISSION_PHASE_SEQUENCE)
        * len(PX4GazeboDeliveryMissionFailureType),
        "failure_statuses": failures,
        "hardware_target_allowed": runner.hardware_target_allowed,
        "physical_execution_invoked": runner.physical_execution_invoked,
        "px4_mission_upload_allowed": runner.px4_mission_upload_allowed,
        "unbounded_setpoint_stream_allowed": runner.unbounded_setpoint_stream_allowed,
        "memory_direct_command_authority_allowed": (
            runner.memory_direct_command_authority_allowed
        ),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    assert summary["contract_refs_complete"] is True
    assert summary["happy_final_status"] == "completed"
    assert summary["happy_waypoint_count"] >= 3
    assert summary["happy_route_segment_count"] >= 3
    assert summary["happy_dropoff_landing_error_m"] <= 0.5
    assert set(summary["failure_statuses"].values()) == {"blocked"}
    assert (
        summary["policy_matrix_cell_count"]
        == summary["policy_matrix_expected_cell_count"]
    )
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["px4_mission_upload_allowed"] is False
    assert summary["unbounded_setpoint_stream_allowed"] is False
    assert summary["memory_direct_command_authority_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

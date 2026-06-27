#!/usr/bin/env python3
"""Curate sanitized PX4/Gazebo multi-phase mission golden fixtures."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from src.runtime.px4_gazebo_delivery_mission_control import (
    PX4GazeboDeliveryMissionFailureType,
    PX4GazeboDeliveryMissionPhase,
    build_px4_gazebo_delivery_mission_contract,
    run_px4_gazebo_delivery_mission_v1,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
DEFAULT_OUTPUT_DIR = Path("tests/golden/px4_gazebo_mission")

CASES: dict[
    str,
    tuple[
        PX4GazeboDeliveryMissionPhase | None, PX4GazeboDeliveryMissionFailureType | None
    ],
] = {
    "multi_waypoint_happy_v1": (None, None),
    "failure_pose_deviation_v1": (
        PX4GazeboDeliveryMissionPhase.DELIVERY_ROUTE,
        PX4GazeboDeliveryMissionFailureType.POSE_DEVIATION,
    ),
    "failure_battery_low_v1": (
        PX4GazeboDeliveryMissionPhase.DELIVERY_ROUTE,
        PX4GazeboDeliveryMissionFailureType.BATTERY_LOW,
    ),
    "failure_link_loss_v1": (
        PX4GazeboDeliveryMissionPhase.WAYPOINT_ROUTE,
        PX4GazeboDeliveryMissionFailureType.LINK_LOSS,
    ),
    "failure_gate_blocked_v1": (
        PX4GazeboDeliveryMissionPhase.DROPOFF_APPROACH,
        PX4GazeboDeliveryMissionFailureType.GATE_BLOCKED,
    ),
    "failure_ack_timeout_v1": (
        PX4GazeboDeliveryMissionPhase.TAKEOFF,
        PX4GazeboDeliveryMissionFailureType.ACK_TIMEOUT,
    ),
}


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


def _run_case(
    failure_phase: PX4GazeboDeliveryMissionPhase | None,
    failure_type: PX4GazeboDeliveryMissionFailureType | None,
) -> dict[str, Any]:
    return run_px4_gazebo_delivery_mission_v1(
        mission_contract=_contract(),
        failure_phase=failure_phase,
        failure_type=failure_type,
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


def _summary(case_id: str, artifacts: dict[str, Any]) -> dict[str, Any]:
    runner = artifacts["runner_result"]
    replay = artifacts["replay_timeline"]
    decisions = artifacts["recovery_decisions"]
    return {
        "case_id": case_id,
        "schema_version": "px4_gazebo_mission_golden_summary.v1",
        "runner_schema_version": runner.schema_version,
        "replay_schema_version": replay.schema_version,
        "phase_gate_evaluation_schema_version": artifacts["phase_gate_evaluations"][
            0
        ].schema_version,
        "recovery_decision_schema_version": (
            decisions[0].schema_version if decisions else None
        ),
        "final_status": runner.final_status.value,
        "blocked_phase": (
            None if runner.blocked_phase is None else runner.blocked_phase.value
        ),
        "blocked_reasons": list(runner.blocked_reasons),
        "observed_phase_count": len(runner.observed_phases),
        "missing_phase_count": len(runner.missing_phases),
        "health_snapshot_count": len(artifacts["health_snapshots"]),
        "phase_gate_evaluation_count": len(artifacts["phase_gate_evaluations"]),
        "phase_transition_event_count": len(artifacts["phase_transition_events"]),
        "recovery_decision_count": len(decisions),
        "recovery_decision_actions": [
            decision.recovery_action.value for decision in decisions
        ],
        "phase_gate_verdicts": [
            item.verdict.value for item in artifacts["phase_gate_evaluations"]
        ],
        "waypoint_count": runner.waypoint_count,
        "route_segment_count": runner.route_segment_count,
        "dropoff_landing_error_m": runner.dropoff_landing_error_m,
        "hardware_target_allowed": runner.hardware_target_allowed,
        "physical_execution_invoked": runner.physical_execution_invoked,
        "px4_mission_upload_allowed": runner.px4_mission_upload_allowed,
        "unbounded_setpoint_stream_allowed": runner.unbounded_setpoint_stream_allowed,
        "memory_direct_command_authority_allowed": (
            runner.memory_direct_command_authority_allowed
        ),
    }


def _replay(artifacts: dict[str, Any]) -> dict[str, Any]:
    replay = artifacts["replay_timeline"]

    def _sanitized_event(event: Any) -> dict[str, Any]:
        artifact_schema = event.artifact_ref.split(":", 1)[0]
        return {
            "sequence": event.sequence,
            "t_relative_seconds": event.t_relative_seconds,
            "phase": event.phase.value,
            "event_type": event.event_type,
            "artifact_ref_present": bool(event.artifact_ref),
            "artifact_schema": artifact_schema,
            "artifact_ref_shape": f"{artifact_schema}:<sanitized>",
        }

    return {
        "schema_version": replay.schema_version,
        "final_status": replay.final_status.value,
        "event_count": len(replay.events),
        "events": [_sanitized_event(event) for event in replay.events],
    }


def _invariants(case_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "exact": {
            "final_status": summary["final_status"],
            "blocked_phase": summary["blocked_phase"],
            "blocked_reasons": summary["blocked_reasons"],
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "px4_mission_upload_allowed": False,
            "unbounded_setpoint_stream_allowed": False,
            "memory_direct_command_authority_allowed": False,
        },
        "expected_counts": {
            "waypoint_count_min": 3,
            "route_segment_count_min": 3,
            "phase_gate_evaluation_count_min": 1,
            "health_snapshot_count_min": 2,
        },
        "tolerances": {
            "dropoff_landing_error_m_max": 0.5,
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main() -> int:
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for case_id, (failure_phase, failure_type) in CASES.items():
        artifacts = _run_case(failure_phase, failure_type)
        case_dir = DEFAULT_OUTPUT_DIR / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        summary = _summary(case_id, artifacts)
        _write_json(case_dir / "summary.golden.json", summary)
        _write_json(case_dir / "replay_timeline.golden.json", _replay(artifacts))
        _write_json(
            case_dir / "expected_invariants.json", _invariants(case_id, summary)
        )
    print(
        json.dumps(
            {
                "schema_version": "px4_gazebo_mission_golden_curation.v1",
                "case_count": len(CASES),
                "case_ids": sorted(CASES),
                "output_dir": str(DEFAULT_OUTPUT_DIR),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

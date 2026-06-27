#!/usr/bin/env python3
"""Runtime smoke for simulated delivery runner v0."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.px4_gazebo_telemetry import sanitize_px4_gazebo_telemetry_sample
from src.runtime.simulated_delivery_runner import (
    create_and_run_simulated_delivery_task_v0,
)
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _contract():
    return build_delivery_mission_contract(
        mission_id="runner-v0-smoke",
        pickup_location={
            "location_id": "pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "dropoff-pad-b",
            "latitude": 35.689487,
            "longitude": 139.691706,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:30:00Z",
        },
        package_constraints={"package_id": "pkg-runner-smoke", "max_weight_kg": 1.2},
        geofence_constraints={"allowed_regions": ["sim-delivery-corridor"]},
        weather_constraints={
            "max_wind_speed_mps": 6.0,
            "max_precipitation_mm_per_hour": 0.0,
            "min_visibility_m": 1500.0,
        },
        battery_policy={
            "minimum_takeoff_percent": 80,
            "return_to_home_percent": 35,
            "reserve_landing_percent": 25,
        },
        landing_zone_policy={
            "min_clear_radius_m": 3.0,
            "max_slope_degrees": 5.0,
            "accepted_surface_kinds": ["marked_pad"],
        },
        telemetry_requirements={
            "required_measurements": [
                "position",
                "battery_percent",
                "vehicle_health",
                "weather_snapshot",
            ],
            "max_freshness_seconds": 2.0,
        },
        now=NOW,
    )


def _telemetry():
    return sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": "runner-v0-smoke",
            "source": {
                "source_kind": "gz_sim_harmonic_stdout_log",
                "source_id": "gz-sim-runner-v0-smoke",
                "vehicle_id": "vehicle-runner-v0-smoke",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "35.681236,139.767125,16.0",
                "battery_percent": 88.0,
                "vehicle_health": "nominal",
                "weather_snapshot": "clear",
                "pickup_reached": True,
                "dropoff_reached": True,
                "route_progress_percent": 100.0,
            },
        }
    )


def main() -> int:
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        updated = create_and_run_simulated_delivery_task_v0(
            delivery_mission_contract=_contract(),
            sanitized_telemetry=_telemetry(),
            title="Simulated delivery runner smoke",
            owner_session_id="runner-smoke-session",
            owner_user_id="runner-smoke-user",
            now=NOW,
            task_store_factory=lambda: store,
        )
        timeline = store.query_timeline(updated["task_id"])

    artifacts = updated["artifacts"]
    result = artifacts["simulated_delivery_runner_result"]
    summary = {
        "task_id": updated["task_id"],
        "task_status": updated["status"],
        "task_ended": updated["ended_at"] is not None,
        "runner_result_created": "simulated_delivery_runner_result" in artifacts,
        "final_task_status": result["final_task_status"],
        "telemetry_window_created": "gazebo_delivery_telemetry_window" in artifacts,
        "hil_evidence_created": "hil_telemetry_evidence" in artifacts,
        "hil_review_created": "hil_telemetry_review" in artifacts,
        "policy_review_created": "delivery_mission_policy_review" in artifacts,
        "scorecard_created": "delivery_mission_scorecard" in artifacts,
        "gate_created": "delivery_mission_gate_result" in artifacts,
        "episode_created": "simulated_delivery_episode" in artifacts,
        "progress_review_created": "delivery_progress_review" in artifacts,
        "recovery_decision_created": "delivery_recovery_decision" in artifacts,
        "progress_status": artifacts["delivery_progress_review"]["status"],
        "recovery_primary_action": artifacts["delivery_recovery_decision"][
            "primary_action"
        ],
        "status_changed_event": any(
            event["event_type"] == "status_changed" for event in timeline["events"]
        ),
        "approval_promotion_reuse_created": any(
            key in artifacts
            for key in ("approval", "promotion_package", "reuse_plan", "runtime_reuse")
        ),
        "live_execution_allowed": result["live_execution_allowed"],
        "physical_execution_invoked": result["physical_execution_invoked"],
        "command_payload_allowed": result["command_payload_allowed"],
        "dispatch_implementation_present": result["dispatch_implementation_present"],
        "gazebo_entity_mutation_allowed": result["gazebo_entity_mutation_allowed"],
        "ros_dispatch_allowed": result["ros_dispatch_allowed"],
        "mavlink_dispatch_allowed": result["mavlink_dispatch_allowed"],
        "actuator_execution_allowed": result["actuator_execution_allowed"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    assert summary["task_status"] == "completed"
    assert summary["task_ended"] is True
    assert summary["runner_result_created"] is True
    assert summary["status_changed_event"] is True
    assert summary["approval_promotion_reuse_created"] is False
    assert summary["live_execution_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["command_payload_allowed"] is False
    assert summary["gazebo_entity_mutation_allowed"] is False
    assert summary["ros_dispatch_allowed"] is False
    assert summary["mavlink_dispatch_allowed"] is False
    assert summary["actuator_execution_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

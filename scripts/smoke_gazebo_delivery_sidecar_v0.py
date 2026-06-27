from __future__ import annotations

from datetime import datetime, timezone
import json
import tempfile
from pathlib import Path

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.gazebo_delivery_scenario import build_gazebo_delivery_scenario
from src.runtime.gazebo_delivery_sidecar_v0 import (
    create_and_run_gazebo_delivery_sidecar_v0_task,
)
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _contract():
    return build_delivery_mission_contract(
        mission_id="gazebo-delivery-sidecar-v0-smoke",
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
        package_constraints={"package_id": "pkg-sidecar-v0-smoke", "max_weight_kg": 1.2},
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


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TaskStore(str(Path(tmpdir) / "tasks.db"))
        contract = _contract()
        scenario = build_gazebo_delivery_scenario(
            delivery_mission_contract=contract,
            now=NOW,
        )
        updated = create_and_run_gazebo_delivery_sidecar_v0_task(
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            title="Gazebo delivery sidecar v0 smoke",
            owner_session_id="session-sidecar-v0-smoke",
            owner_user_id="user-sidecar-v0-smoke",
            now=NOW,
            task_store_factory=lambda: store,
        )
        artifacts = updated["artifacts"]
        result = artifacts["simulated_delivery_runner_result"]
        sidecar_sequence = artifacts["gazebo_delivery_sidecar_v0_sequence"]
        timeline = store.query_timeline(updated["task_id"])
        status_changed = any(
            event["event_type"] == "status_changed"
            and event.get("status") == "completed"
            for event in timeline["events"]
        )
        print(
            json.dumps(
                {
                    "task_id": updated["task_id"],
                    "task_status": updated["status"],
                    "final_task_status": result["final_task_status"],
                    "sidecar_sequence_created": bool(sidecar_sequence),
                    "sidecar_phase_count": len(sidecar_sequence),
                    "sidecar_final_phase": sidecar_sequence[-1]["phase"],
                    "telemetry_window_created": "gazebo_delivery_telemetry_window"
                    in artifacts,
                    "hil_evidence_created": "hil_telemetry_evidence" in artifacts,
                    "hil_review_created": "hil_telemetry_review" in artifacts,
                    "delivery_gate_created": "delivery_mission_gate_result" in artifacts,
                    "progress_status": artifacts["delivery_progress_review"]["status"],
                    "recovery_primary_action": artifacts[
                        "delivery_recovery_decision"
                    ]["primary_action"],
                    "status_changed_event": status_changed,
                    "approval_promotion_reuse_created": any(
                        key in artifacts
                        for key in (
                            "approval",
                            "promotion_package",
                            "reuse_plan",
                            "runtime_reuse",
                        )
                    ),
                    "live_execution_allowed": result["live_execution_allowed"],
                    "physical_execution_invoked": result["physical_execution_invoked"],
                    "command_payload_allowed": result["command_payload_allowed"],
                    "gazebo_entity_mutation_allowed": result[
                        "gazebo_entity_mutation_allowed"
                    ],
                    "ros_dispatch_allowed": result["ros_dispatch_allowed"],
                    "mavlink_dispatch_allowed": result["mavlink_dispatch_allowed"],
                    "actuator_execution_allowed": result["actuator_execution_allowed"],
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()

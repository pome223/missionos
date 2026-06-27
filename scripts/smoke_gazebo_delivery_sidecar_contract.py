#!/usr/bin/env python3
"""Runtime smoke for the Gazebo delivery sidecar contract artifact."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from tempfile import TemporaryDirectory

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.gazebo_delivery_scenario import build_gazebo_delivery_scenario
from src.runtime.gazebo_delivery_sidecar_contract import (
    attach_gazebo_delivery_sidecar_contract,
    build_gazebo_delivery_sidecar_contract,
    validate_gazebo_delivery_sidecar_contract,
)
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _contract():
    return build_delivery_mission_contract(
        mission_id="gazebo-delivery-sidecar-smoke-001",
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
        package_constraints={"package_id": "pkg-sidecar-smoke", "max_weight_kg": 1.2},
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


def main() -> int:
    delivery_contract = _contract()
    scenario = build_gazebo_delivery_scenario(
        delivery_mission_contract=delivery_contract,
        now=NOW,
    )
    sidecar_contract = build_gazebo_delivery_sidecar_contract(
        delivery_mission_contract=delivery_contract,
        gazebo_delivery_scenario=scenario,
        now=NOW,
    )
    validated = validate_gazebo_delivery_sidecar_contract(sidecar_contract)

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="Gazebo delivery sidecar contract smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        artifacts = attach_gazebo_delivery_sidecar_contract(
            task["task_id"],
            delivery_mission_contract=delivery_contract,
            gazebo_delivery_scenario=scenario,
            now=NOW,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])
        assert stored is not None
        assert stored["status"] == "running"
        assert stored["artifacts"]["existing"] == {"kept": True}
        assert "approval" not in stored["artifacts"]
        assert "promotion_package" not in stored["artifacts"]
        assert "reuse_plan" not in stored["artifacts"]
        assert "runtime_reuse" not in stored["artifacts"]

    sidecar_artifact = artifacts["gazebo_delivery_sidecar_contract"]
    summary = {
        "sidecar_contract_attached": "gazebo_delivery_sidecar_contract" in artifacts,
        "sidecar_returns_artifacts_only": sidecar_artifact[
            "sidecar_returns_artifacts_only"
        ],
        "mission_os_validates_returned_artifacts": sidecar_artifact[
            "mission_os_validates_returned_artifacts"
        ],
        "accepted_simulation_requests": sidecar_artifact[
            "accepted_simulation_requests"
        ],
        "returned_artifact_schemas": sidecar_artifact["returned_artifact_schemas"],
        "task_status_preserved": stored["status"] == "running",
        "existing_artifacts_retained": stored["artifacts"]["existing"]["kept"],
        "approval_promotion_reuse_created": any(
            key in stored["artifacts"]
            for key in ("approval", "promotion_package", "reuse_plan", "runtime_reuse")
        ),
        "simulation_only": validated.simulation_only,
        "live_execution_allowed": validated.live_execution_allowed,
        "physical_execution_invoked": validated.physical_execution_invoked,
        "command_payload_allowed": validated.command_payload_allowed,
        "gazebo_entity_mutation_allowed": validated.gazebo_entity_mutation_allowed,
        "ros_dispatch_allowed": validated.ros_dispatch_allowed,
        "mavlink_dispatch_allowed": validated.mavlink_dispatch_allowed,
        "actuator_execution_allowed": validated.actuator_execution_allowed,
    }
    assert summary["sidecar_contract_attached"] is True
    assert summary["sidecar_returns_artifacts_only"] is True
    assert summary["mission_os_validates_returned_artifacts"] is True
    assert summary["approval_promotion_reuse_created"] is False
    assert summary["simulation_only"] is True
    assert summary["live_execution_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["command_payload_allowed"] is False
    assert summary["gazebo_entity_mutation_allowed"] is False
    assert summary["ros_dispatch_allowed"] is False
    assert summary["mavlink_dispatch_allowed"] is False
    assert summary["actuator_execution_allowed"] is False
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

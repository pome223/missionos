"""Runtime smoke for Gazebo delivery scenario artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.gazebo_delivery_scenario import (
    GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION,
    attach_gazebo_delivery_scenario,
)
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _contract():
    return build_delivery_mission_contract(
        mission_id="gazebo-delivery-scenario-smoke",
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
        package_constraints={"package_id": "pkg-scenario-smoke", "max_weight_kg": 1.2},
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
    with TemporaryDirectory() as tmpdir:
        store = TaskStore(f"{tmpdir}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="Gazebo delivery scenario smoke",
            status="running",
            artifacts={"existing": {"schema_version": "existing.v1"}},
        )
        artifacts = attach_gazebo_delivery_scenario(
            task["task_id"],
            delivery_mission_contract=_contract(),
            world_ref="worlds/delivery_minimal.sdf",
            now=NOW,
            task_store_factory=lambda: store,
        )
        reloaded = store.get(task["task_id"])

    assert reloaded is not None
    scenario = artifacts["gazebo_delivery_scenario"]
    result = {
        "schema_version": scenario["schema_version"],
        "simulator_kind": scenario["simulator_kind"],
        "world_ref": scenario["world_ref"],
        "pickup_pad_ref": scenario["pickup_pad"]["delivery_location_ref"],
        "dropoff_pad_ref": scenario["dropoff_pad"]["delivery_location_ref"],
        "route_id": scenario["route"]["route_id"],
        "task_status_preserved": reloaded["status"] == "running",
        "existing_artifacts_retained": "existing" in reloaded["artifacts"],
        "scenario_attached": "gazebo_delivery_scenario" in reloaded["artifacts"],
        "approval_promotion_reuse_created": any(
            key in reloaded["artifacts"]
            for key in ("approval", "promotion_package", "runtime_reuse")
        ),
        "simulation_only": scenario["simulation_only"],
        "command_payload_allowed": scenario["command_payload_allowed"],
        "dispatch_implementation_present": scenario["dispatch_implementation_present"],
        "gazebo_entity_mutation_allowed": scenario["gazebo_entity_mutation_allowed"],
        "live_execution_allowed": scenario["live_execution_allowed"],
        "physical_execution_invoked": scenario["physical_execution_invoked"],
        "ros_dispatch_allowed": scenario["ros_dispatch_allowed"],
        "mavlink_dispatch_allowed": scenario["mavlink_dispatch_allowed"],
        "actuator_execution_allowed": scenario["actuator_execution_allowed"],
    }
    print(json.dumps(result, indent=2, sort_keys=True))

    assert result["schema_version"] == GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    assert result["simulator_kind"] == "gazebo_sim"
    assert result["world_ref"] == "worlds/delivery_minimal.sdf"
    assert result["task_status_preserved"] is True
    assert result["existing_artifacts_retained"] is True
    assert result["scenario_attached"] is True
    assert result["approval_promotion_reuse_created"] is False
    assert result["simulation_only"] is True
    assert result["command_payload_allowed"] is False
    assert result["dispatch_implementation_present"] is False
    assert result["gazebo_entity_mutation_allowed"] is False
    assert result["live_execution_allowed"] is False
    assert result["physical_execution_invoked"] is False
    assert result["ros_dispatch_allowed"] is False
    assert result["mavlink_dispatch_allowed"] is False
    assert result["actuator_execution_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

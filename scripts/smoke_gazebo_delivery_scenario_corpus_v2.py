#!/usr/bin/env python3
"""Runtime smoke for Gazebo delivery scenario variants and corpus v2."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.delivery_progress_review import (
    DELIVERY_PROGRESS_BUCKET_LANDING_ZONE_UNAVAILABLE,
    DELIVERY_PROGRESS_BUCKET_ROUTE_GEOFENCE_VIOLATION,
)
from src.runtime.gazebo_delivery_scenario import (
    GAZEBO_DELIVERY_SCENARIO_VARIANTS,
    build_gazebo_delivery_scenario_variant,
)
from src.runtime.px4_gazebo_telemetry import sanitize_px4_gazebo_telemetry_sample
from src.runtime.simulated_delivery_runner import run_simulated_delivery_task_v0
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _contract(case_id: str = "scenario-corpus-v2"):
    return build_delivery_mission_contract(
        mission_id=f"gazebo-delivery-{case_id}",
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
        package_constraints={"package_id": f"pkg-{case_id}", "max_weight_kg": 1.2},
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


def _telemetry(
    case_id: str,
    *,
    route_geofence_violation: bool = False,
    landing_zone_available: bool = True,
):
    return sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": f"scenario-corpus-v2-{case_id}",
            "source": {
                "source_kind": "gz_sim_delivery_entity_state_pose",
                "source_id": "gz-sim-scenario-corpus-v2",
                "vehicle_id": f"vehicle-{case_id}",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "25.1,0.0,0.2",
                "battery_percent": 88.0,
                "vehicle_health": "nominal",
                "weather_snapshot": "clear",
                "pickup_reached": True,
                "dropoff_reached": True,
                "route_progress_percent": 100.0,
                "route_geofence_violation": route_geofence_violation,
                "landing_zone_available": landing_zone_available,
            },
        }
    )


def _run_case(
    store: TaskStore,
    *,
    case_id: str,
    variant: str,
    route_geofence_violation: bool = False,
    landing_zone_available: bool = True,
) -> dict:
    contract = _contract(case_id)
    scenario = build_gazebo_delivery_scenario_variant(
        delivery_mission_contract=contract,
        variant=variant,
        now=NOW,
    )
    task = store.create(
        kind="simulated_delivery_runner",
        title=f"Gazebo delivery scenario corpus v2: {case_id}",
        status="running",
        artifacts={"existing": {"case_id": case_id, "kept": True}},
    )
    updated = run_simulated_delivery_task_v0(
        task["task_id"],
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        sanitized_telemetry=_telemetry(
            case_id,
            route_geofence_violation=route_geofence_violation,
            landing_zone_available=landing_zone_available,
        ),
        now=NOW,
        task_store_factory=lambda: store,
    )
    result = updated["artifacts"]["simulated_delivery_runner_result"]
    return {
        "task_status": updated["status"],
        "scenario_id": scenario.scenario_id,
        "scenario_variant": scenario.metadata["scenario_variant"],
        "expected_outcome": scenario.metadata["expected_outcome"],
        "blocked_reasons": result["blocked_reasons"],
        "progress_status": updated["artifacts"]["delivery_progress_review"]["status"],
        "recovery_primary_action": updated["artifacts"]["delivery_recovery_decision"][
            "primary_action"
        ],
        "existing_artifact_kept": updated["artifacts"]["existing"]["kept"],
        "live_execution_allowed": result["live_execution_allowed"],
        "physical_execution_invoked": result["physical_execution_invoked"],
        "command_payload_allowed": result["command_payload_allowed"],
        "ros_dispatch_allowed": result["ros_dispatch_allowed"],
        "mavlink_dispatch_allowed": result["mavlink_dispatch_allowed"],
        "actuator_execution_allowed": result["actuator_execution_allowed"],
    }


def main() -> int:
    contract = _contract("variant-determinism")
    first_variants = {
        variant: build_gazebo_delivery_scenario_variant(
            delivery_mission_contract=contract,
            variant=variant,
            now=NOW,
        )
        for variant in GAZEBO_DELIVERY_SCENARIO_VARIANTS
    }
    second_variants = {
        variant: build_gazebo_delivery_scenario_variant(
            delivery_mission_contract=contract,
            variant=variant,
            now=NOW,
        )
        for variant in GAZEBO_DELIVERY_SCENARIO_VARIANTS
    }
    assert {
        variant: scenario.scenario_id for variant, scenario in first_variants.items()
    } == {
        variant: scenario.scenario_id for variant, scenario in second_variants.items()
    }
    assert len({scenario.scenario_id for scenario in first_variants.values()}) == len(
        GAZEBO_DELIVERY_SCENARIO_VARIANTS
    )
    for scenario in first_variants.values():
        assert scenario.metadata["opt_in_only"] is True
        assert scenario.metadata["headless_compatible"] is True
        assert scenario.metadata["requires_gui"] is False
        assert scenario.metadata["command_control_ports_exposed"] is False
        assert scenario.command_payload_allowed is False
        assert scenario.ros_dispatch_allowed is False
        assert scenario.mavlink_dispatch_allowed is False
        assert scenario.actuator_execution_allowed is False
        assert scenario.live_execution_allowed is False
        assert scenario.physical_execution_invoked is False

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        nominal = _run_case(store, case_id="nominal", variant="nominal_delivery")
        route = _run_case(
            store,
            case_id="route-geofence",
            variant="blocked_route_geofence",
            route_geofence_violation=True,
        )
        landing = _run_case(
            store,
            case_id="landing-zone",
            variant="landing_zone_unavailable",
            landing_zone_available=False,
        )

    summary = {
        "scenario_variant_count": len(GAZEBO_DELIVERY_SCENARIO_VARIANTS),
        "scenario_ids_deterministic": True,
        "scenario_ids_distinct": True,
        "nominal_task_status": nominal["task_status"],
        "route_task_status": route["task_status"],
        "landing_task_status": landing["task_status"],
        "route_geofence_violation_blocked": (
            DELIVERY_PROGRESS_BUCKET_ROUTE_GEOFENCE_VIOLATION
            in route["blocked_reasons"]
        ),
        "landing_zone_unavailable_blocked": (
            DELIVERY_PROGRESS_BUCKET_LANDING_ZONE_UNAVAILABLE
            in landing["blocked_reasons"]
        ),
        "route_recovery_primary_action": route["recovery_primary_action"],
        "landing_recovery_primary_action": landing["recovery_primary_action"],
        "existing_artifacts_retained": all(
            item["existing_artifact_kept"] for item in (nominal, route, landing)
        ),
        "any_live_execution_allowed": any(
            item["live_execution_allowed"] for item in (nominal, route, landing)
        ),
        "any_physical_execution_invoked": any(
            item["physical_execution_invoked"] for item in (nominal, route, landing)
        ),
        "any_command_payload_allowed": any(
            item["command_payload_allowed"] for item in (nominal, route, landing)
        ),
        "any_ros_dispatch_allowed": any(
            item["ros_dispatch_allowed"] for item in (nominal, route, landing)
        ),
        "any_mavlink_dispatch_allowed": any(
            item["mavlink_dispatch_allowed"] for item in (nominal, route, landing)
        ),
        "any_actuator_execution_allowed": any(
            item["actuator_execution_allowed"] for item in (nominal, route, landing)
        ),
    }

    assert summary["nominal_task_status"] == "completed"
    assert summary["route_task_status"] == "blocked"
    assert summary["landing_task_status"] == "blocked"
    assert summary["route_geofence_violation_blocked"] is True
    assert summary["landing_zone_unavailable_blocked"] is True
    assert summary["route_recovery_primary_action"] == "operator_escalation_required"
    assert summary["landing_recovery_primary_action"] == "operator_escalation_required"
    assert summary["existing_artifacts_retained"] is True
    assert summary["any_live_execution_allowed"] is False
    assert summary["any_physical_execution_invoked"] is False
    assert summary["any_command_payload_allowed"] is False
    assert summary["any_ros_dispatch_allowed"] is False
    assert summary["any_mavlink_dispatch_allowed"] is False
    assert summary["any_actuator_execution_allowed"] is False

    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

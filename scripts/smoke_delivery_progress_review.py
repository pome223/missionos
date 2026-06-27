#!/usr/bin/env python3
"""Runtime smoke for delivery_progress_review.v1.

This smoke exercises the real TaskStore attach path for a simulated delivery
progress review. It builds the existing delivery contract, Gazebo scenario,
policy review, scorecard/gate, and simulated episode artifacts, then attaches a
read-only progress review for an in-progress simulated delivery.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from tempfile import TemporaryDirectory

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.delivery_mission_gate import build_delivery_mission_gate_artifacts
from src.runtime.delivery_mission_policy_review import (
    build_delivery_mission_policy_review,
)
from src.runtime.delivery_progress_review import attach_delivery_progress_review
from src.runtime.gazebo_delivery_scenario import build_gazebo_delivery_scenario
from src.runtime.px4_gazebo_telemetry import (
    build_px4_gazebo_hil_review_gate_smoke,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.simulated_delivery_episode import build_simulated_delivery_episode
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _contract():
    return build_delivery_mission_contract(
        mission_id="delivery-progress-smoke-001",
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
        package_constraints={"package_id": "pkg-progress-smoke", "max_weight_kg": 1.2},
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
            "sample_id": "delivery-progress-smoke",
            "source": {
                "source_kind": "gz_sim_harmonic_stdout_log",
                "source_id": "gz-sim-delivery-world",
                "vehicle_id": "vehicle-delivery-progress-smoke",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "35.681236,139.767125,16.0",
                "battery_percent": 80.0,
                "vehicle_health": "nominal",
                "weather_snapshot": "clear",
                "pickup_reached": True,
                "dropoff_reached": False,
                "route_progress_percent": 42.5,
            },
        }
    )


def main() -> int:
    contract = _contract()
    scenario = build_gazebo_delivery_scenario(
        delivery_mission_contract=contract,
        now=NOW,
    )
    telemetry = _telemetry()
    hil_review = build_px4_gazebo_hil_review_gate_smoke(
        telemetry,
        freshness_threshold_seconds=10.0,
        now=NOW,
    )["hil_telemetry_review"]
    policy_review = build_delivery_mission_policy_review(
        delivery_mission_contract=contract,
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_review,
        now=NOW,
    )
    gate_artifacts = build_delivery_mission_gate_artifacts(
        delivery_mission_contract=contract,
        delivery_mission_policy_review=policy_review,
        now=NOW,
    )
    episode = build_simulated_delivery_episode(
        delivery_mission_contract=contract,
        delivery_mission_policy_review=policy_review,
        delivery_mission_scorecard=gate_artifacts["delivery_mission_scorecard"],
        delivery_mission_gate_result=gate_artifacts["delivery_mission_gate_result"],
        now=NOW,
    )

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="Delivery progress review smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        artifacts = attach_delivery_progress_review(
            task["task_id"],
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            simulated_delivery_episode=episode,
            sanitized_telemetry=telemetry,
            hil_telemetry_review=hil_review,
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

    review = artifacts["delivery_progress_review"]
    summary = {
        "progress_review_attached": "delivery_progress_review" in artifacts,
        "schema_version": review["schema_version"],
        "status": review["status"],
        "passed": review["passed"],
        "pickup_reached": review["pickup_reached"],
        "dropoff_reached": review["dropoff_reached"],
        "route_progress_percent": review["route_progress_percent"],
        "completion_criteria_met": review["completion_criteria_met"],
        "task_status_preserved": stored["status"] == "running",
        "existing_artifacts_retained": stored["artifacts"]["existing"]["kept"],
        "approval_promotion_reuse_created": any(
            key in stored["artifacts"]
            for key in ("approval", "promotion_package", "reuse_plan", "runtime_reuse")
        ),
        "live_execution_allowed": review["live_execution_allowed"],
        "physical_execution_invoked": review["physical_execution_invoked"],
        "command_payload_allowed": review["command_payload_allowed"],
        "gazebo_entity_mutation_allowed": review["gazebo_entity_mutation_allowed"],
    }
    assert summary["status"] == "in_progress"
    assert summary["pickup_reached"] is True
    assert summary["route_progress_percent"] == 42.5
    assert summary["approval_promotion_reuse_created"] is False
    assert summary["live_execution_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["command_payload_allowed"] is False
    assert summary["gazebo_entity_mutation_allowed"] is False
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

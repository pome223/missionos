"""Runtime smoke for simulated delivery episode artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.delivery_mission_gate import build_delivery_mission_gate_artifacts
from src.runtime.delivery_mission_policy_review import (
    DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED,
    build_delivery_mission_policy_review,
)
from src.runtime.px4_gazebo_telemetry import (
    build_px4_gazebo_hil_review_gate_smoke,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
    attach_simulated_delivery_episode,
)
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _contract():
    return build_delivery_mission_contract(
        mission_id="simulated-delivery-smoke",
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
        package_constraints={"package_id": "pkg-episode-smoke", "max_weight_kg": 1.2},
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
    telemetry = sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": "simulated-delivery-smoke",
            "source": {
                "source_kind": "px4_sih_stdout_log",
                "source_id": "px4-sih-simulated-delivery",
                "vehicle_id": "vehicle-delivery-episode",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "35.681236,139.767125,16.0",
                "battery_percent": 30.0,
                "vehicle_health": "nominal",
                "weather_snapshot": "clear",
            },
        }
    )
    hil_artifacts = build_px4_gazebo_hil_review_gate_smoke(
        telemetry,
        freshness_threshold_seconds=10.0,
        now=NOW,
    )
    policy_review = build_delivery_mission_policy_review(
        delivery_mission_contract=_contract(),
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_artifacts["hil_telemetry_review"],
        now=NOW,
    )
    gate_artifacts = build_delivery_mission_gate_artifacts(
        delivery_mission_contract=_contract(),
        delivery_mission_policy_review=policy_review,
        now=NOW,
    )

    with TemporaryDirectory() as tmpdir:
        store = TaskStore(f"{tmpdir}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="Simulated delivery episode smoke",
            status="running",
            artifacts={"existing": {"schema_version": "existing.v1"}},
        )
        artifacts = attach_simulated_delivery_episode(
            task["task_id"],
            delivery_mission_contract=_contract(),
            delivery_mission_policy_review=policy_review,
            delivery_mission_scorecard=gate_artifacts["delivery_mission_scorecard"],
            delivery_mission_gate_result=gate_artifacts["delivery_mission_gate_result"],
            now=NOW,
            task_store_factory=lambda: store,
        )
        reloaded = store.get(task["task_id"])

    assert reloaded is not None
    episode = artifacts["simulated_delivery_episode"]
    result = {
        "schema_version": episode["schema_version"],
        "phase": episode["phase"],
        "final_status": episode["final_status"],
        "passed": episode["passed"],
        "warning_reasons": episode["warning_reasons"],
        "return_to_home_recommended": episode["return_to_home_recommended"],
        "abort_recommended": episode["abort_recommended"],
        "task_status_preserved": reloaded["status"] == "running",
        "existing_artifacts_retained": "existing" in reloaded["artifacts"],
        "episode_attached": "simulated_delivery_episode" in reloaded["artifacts"],
        "approval_promotion_reuse_created": any(
            key in reloaded["artifacts"]
            for key in ("approval", "promotion_package", "runtime_reuse")
        ),
        "live_execution_allowed": episode["live_execution_allowed"],
        "physical_execution_invoked": episode["physical_execution_invoked"],
        "command_payload_allowed": episode["command_payload_allowed"],
        "ros_dispatch_allowed": episode["ros_dispatch_allowed"],
        "mavlink_dispatch_allowed": episode["mavlink_dispatch_allowed"],
        "actuator_execution_allowed": episode["actuator_execution_allowed"],
    }
    print(json.dumps(result, indent=2, sort_keys=True))

    assert result["schema_version"] == SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    assert result["phase"] == "preflight_review"
    assert result["final_status"] == "ready_with_warnings"
    assert result["passed"] is True
    assert (
        DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED
        in result["warning_reasons"]
    )
    assert result["return_to_home_recommended"] is True
    assert result["abort_recommended"] is False
    assert result["task_status_preserved"] is True
    assert result["existing_artifacts_retained"] is True
    assert result["episode_attached"] is True
    assert result["approval_promotion_reuse_created"] is False
    assert result["live_execution_allowed"] is False
    assert result["physical_execution_invoked"] is False
    assert result["command_payload_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

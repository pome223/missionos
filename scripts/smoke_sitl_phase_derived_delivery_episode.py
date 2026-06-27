"""Runtime smoke for SITL phase-derived delivery episode artifacts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.px4_gazebo_sitl_telemetry_run import (
    build_px4_gazebo_sitl_telemetry_run,
)
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
    attach_simulated_delivery_episode_from_sitl_telemetry_run,
)
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
VALID_LOGS = "\n".join(
    (
        "PX4_SIM_MODEL: gz_x500",
        "INFO  [gz_bridge] world: default, model: x500_0",
        "INFO  [gz_bridge] Gazebo world is ready",
        "INFO  [px4] Startup script returned successfully",
    )
)
VALID_POSE_SAMPLES = (
    {"x": 0.0, "y": 0.0, "z": -0.01},
    {"x": 0.0, "y": 0.0, "z": -0.01},
    {"x": 0.0, "y": 0.0, "z": -0.01},
)


def _contract():
    return build_delivery_mission_contract(
        mission_id="sitl-phase-derived-smoke",
        pickup_location={
            "location_id": "pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "summit-dropoff-pad",
            "latitude": 35.689487,
            "longitude": 139.691706,
            "altitude_m": 3000.0,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:45:00Z",
        },
        package_constraints={"package_id": "pkg-sitl-phase", "max_weight_kg": 5.0},
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
        metadata={
            "sitl_phase_geometry": {
                "home_xy_m": [0.0, 0.0],
                "dropoff_xy_m": [30.0, 0.0],
                "takeoff_altitude_m": 1.0,
                "staged_ascent_altitude_m": 2.0,
                "dropoff_approach_radius_m": 3.0,
                "summit_approach_radius_m": 8.0,
            }
        },
        now=NOW,
    )


def main() -> int:
    sitl_run, source_artifacts = build_px4_gazebo_sitl_telemetry_run(
        log_text=VALID_LOGS,
        pose_samples=VALID_POSE_SAMPLES,
        mavlink_frame_count=42,
        mavlink_heartbeat_count=5,
        mavlink_observation_window_seconds=5.5,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=6),
        max_duration_seconds=30.0,
        source_id="actual-px4-gazebo-sitl-phase-derived-smoke",
    )
    with TemporaryDirectory() as tmpdir:
        store = TaskStore(f"{tmpdir}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="SITL phase-derived delivery episode smoke",
            status="running",
            artifacts={"existing": {"schema_version": "existing.v1"}},
        )
        artifacts = attach_simulated_delivery_episode_from_sitl_telemetry_run(
            task_id=task["task_id"],
            delivery_mission_contract=_contract(),
            sitl_telemetry_run=sitl_run,
            sanitized_telemetry=source_artifacts["px4_gazebo_sanitized_telemetry"],
            hil_telemetry_review=source_artifacts["hil_telemetry_review"],
            autonomy_gate_result=source_artifacts["autonomy_gate_result"],
            now=NOW,
            task_store_factory=lambda: store,
        )
        reloaded = store.get(task["task_id"])

    assert reloaded is not None
    episode = artifacts["simulated_delivery_episode"]
    trace = artifacts["delivery_replay_trace"]
    result = {
        "schema_version": episode["schema_version"],
        "phase": episode["phase"],
        "phase_history": episode["phase_history"],
        "final_status": episode["final_status"],
        "passed": episode["passed"],
        "sitl_telemetry_run_ref": episode["sitl_telemetry_run_ref"],
        "trace_sitl_telemetry_run_ref": trace["sitl_telemetry_run_ref"],
        "dropoff_verified": episode["dropoff_verified"],
        "task_status_preserved": reloaded["status"] == "running",
        "existing_artifacts_retained": "existing" in reloaded["artifacts"],
        "episode_attached": "simulated_delivery_episode" in reloaded["artifacts"],
        "approval_promotion_reuse_created": any(
            key in reloaded["artifacts"]
            for key in ("approval", "promotion_package", "runtime_reuse")
        ),
        "gazebo_execution_invoked_by_episode": episode[
            "gazebo_execution_invoked_by_episode"
        ],
        "physical_execution_invoked": episode["physical_execution_invoked"],
        "mavlink_dispatch_allowed": episode["mavlink_dispatch_allowed"],
        "px4_mission_upload_allowed": episode["px4_mission_upload_allowed"],
    }
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(result, sort_keys=True))

    assert result["schema_version"] == SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    assert result["phase"] == "preflight"
    assert result["phase_history"] == ["preflight"]
    assert result["final_status"] == "ready_for_simulation"
    assert result["passed"] is True
    assert result["dropoff_verified"] is False
    assert result["sitl_telemetry_run_ref"] == result["trace_sitl_telemetry_run_ref"]
    assert result["task_status_preserved"] is True
    assert result["existing_artifacts_retained"] is True
    assert result["episode_attached"] is True
    assert result["approval_promotion_reuse_created"] is False
    assert result["gazebo_execution_invoked_by_episode"] is False
    assert result["physical_execution_invoked"] is False
    assert result["mavlink_dispatch_allowed"] is False
    assert result["px4_mission_upload_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

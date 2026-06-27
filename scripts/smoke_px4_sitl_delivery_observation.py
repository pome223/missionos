#!/usr/bin/env python3
"""Runtime smoke for PX4 SITL telemetry-only delivery observation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.px4_gazebo_delivery_world_profile import (
    build_px4_gazebo_delivery_world_profile,
)
from src.runtime.px4_sitl_delivery_observation import (
    PX4_SITL_DELIVERY_OBSERVATION_SCHEMA_VERSION,
    attach_px4_sitl_delivery_observation,
)
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
PX4_SITL_LOGS = "\n".join(
    [
        "INFO  [px4] startup script: /bin/sh etc/init.d-posix/rcS 0",
        "INFO  [init] found model autostart file as SYS_AUTOSTART=10040",
        "INFO  [init] SIH simulator",
        "INFO  [simulator_sih] Simulation loop with 250 Hz",
        "INFO  [logger] logger started (mode=all)",
        "INFO  [px4] Startup script returned successfully",
    ]
)


def main() -> int:
    profile = build_px4_gazebo_delivery_world_profile(now=NOW)
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_sitl_delivery_observation",
            title="PX4 SITL delivery observation smoke",
            status="running",
            artifacts={"existing": {"case_id": "observation", "kept": True}},
        )
        artifacts = attach_px4_sitl_delivery_observation(
            task["task_id"],
            log_text=PX4_SITL_LOGS,
            captured_at=NOW,
            profile=profile,
            task_store_factory=lambda: store,
        )
        reloaded = store.get(task["task_id"])

    assert reloaded is not None
    observation = artifacts["px4_sitl_delivery_observation"]
    summary = {
        "task_status": reloaded["status"],
        "schema_version": observation["schema_version"],
        "profile_ref": observation["profile_ref"],
        "telemetry_ref": observation["telemetry_ref"],
        "source_kind": observation["source_kind"],
        "source_id": observation["source_id"],
        "vehicle_id": observation["vehicle_id"],
        "delivery_vehicle_ref": observation["delivery_vehicle_ref"],
        "measurement_keys": observation["measurement_keys"],
        "px4_sitl_started": observation["measurements"]["px4_sitl_started"],
        "existing_artifacts_retained": reloaded["artifacts"]["existing"]["kept"],
        "observation_attached": "px4_sitl_delivery_observation"
        in reloaded["artifacts"],
        "simulation_only": observation["simulation_only"],
        "telemetry_only": observation["telemetry_only"],
        "read_only": observation["read_only"],
        "command_surface_present": observation["command_surface_present"],
        "command_payload_allowed": observation["command_payload_allowed"],
        "dispatch_implementation_present": observation[
            "dispatch_implementation_present"
        ],
        "ros_dispatch_allowed": observation["ros_dispatch_allowed"],
        "mavlink_dispatch_allowed": observation["mavlink_dispatch_allowed"],
        "px4_mission_upload_allowed": observation["px4_mission_upload_allowed"],
        "gazebo_entity_mutation_allowed": observation["gazebo_entity_mutation_allowed"],
        "hardware_target_allowed": observation["hardware_target_allowed"],
        "live_execution_allowed": observation["live_execution_allowed"],
        "physical_execution_invoked": observation["physical_execution_invoked"],
        "actuator_execution_allowed": observation["actuator_execution_allowed"],
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    assert summary["schema_version"] == PX4_SITL_DELIVERY_OBSERVATION_SCHEMA_VERSION
    assert summary["task_status"] == "running"
    assert summary["observation_attached"] is True
    assert summary["existing_artifacts_retained"] is True
    assert summary["px4_sitl_started"] is True
    assert summary["simulation_only"] is True
    assert summary["telemetry_only"] is True
    assert summary["read_only"] is True
    assert summary["command_surface_present"] is False
    assert summary["command_payload_allowed"] is False
    assert summary["dispatch_implementation_present"] is False
    assert summary["ros_dispatch_allowed"] is False
    assert summary["mavlink_dispatch_allowed"] is False
    assert summary["px4_mission_upload_allowed"] is False
    assert summary["gazebo_entity_mutation_allowed"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["live_execution_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["actuator_execution_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

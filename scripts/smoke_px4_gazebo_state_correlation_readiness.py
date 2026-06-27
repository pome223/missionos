#!/usr/bin/env python3
"""Runtime smoke for read-only PX4/Gazebo state correlation readiness."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import tempfile

from src.runtime.px4_gazebo_delivery_world_profile import (
    build_px4_gazebo_delivery_world_profile,
)
from src.runtime.px4_gazebo_state_correlation import (
    DEFAULT_GAZEBO_DELIVERY_ENTITY_NAME,
    attach_px4_gazebo_delivery_state_readiness_artifacts,
)
from src.runtime.px4_sitl_delivery_observation import (
    build_px4_sitl_delivery_observation_from_logs,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_PX4_GAZEBO_STATE_CORRELATION_SMOKE"
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


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo state correlation smoke."
        )


def main() -> None:
    _require_opt_in()
    profile = build_px4_gazebo_delivery_world_profile(now=NOW)
    observation = build_px4_sitl_delivery_observation_from_logs(
        PX4_SITL_LOGS,
        captured_at=NOW,
        profile=profile,
    )
    gazebo_pose = {
        "entity_name": DEFAULT_GAZEBO_DELIVERY_ENTITY_NAME,
        "x": -10.0,
        "y": 0.0,
        "z": 0.05,
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TaskStore(f"{tmpdir}/tasks.db")
        task = store.create(
            kind="px4_gazebo_delivery_state_readiness",
            title="PX4/Gazebo state correlation readiness smoke",
            status="running",
            artifacts={
                "px4_gazebo_delivery_world_profile": profile.model_dump(mode="json"),
                "px4_sitl_delivery_observation": observation.model_dump(mode="json"),
            },
        )
        artifacts = attach_px4_gazebo_delivery_state_readiness_artifacts(
            task["task_id"],
            profile=profile,
            observation=observation,
            gazebo_pose=gazebo_pose,
            checked_at=NOW,
            task_store_factory=lambda: store,
        )
        updated = store.get(task["task_id"])
        assert updated is not None
        correlation = artifacts["px4_gazebo_delivery_state_correlation"]
        readiness = artifacts["px4_sitl_delivery_readiness_diagnostics"]
        assert updated["status"] == "running"
        assert correlation["state_correlation_status"] == "ready"
        assert readiness["readiness_status"] == "ready"
        assert correlation["mavlink_dispatch_allowed"] is False
        assert readiness["ros_dispatch_allowed"] is False
        assert readiness["hardware_target_allowed"] is False
        assert readiness["physical_execution_invoked"] is False
        print(
            json.dumps(
                {
                    "task_id": task["task_id"],
                    "task_status": updated["status"],
                    "correlation_schema": correlation["schema_version"],
                    "state_correlation_status": correlation["state_correlation_status"],
                    "coupled_motion_confirmed": correlation["coupled_motion_confirmed"],
                    "readiness_schema": readiness["schema_version"],
                    "readiness_status": readiness["readiness_status"],
                    "delivery_vehicle_ref": correlation["delivery_vehicle_ref"],
                    "gazebo_entity_name": correlation["gazebo_entity_name"],
                    "gazebo_pose_observed": correlation["gazebo_pose_observed"],
                    "px4_sitl_started": correlation["px4_sitl_started"],
                    "command_surface_present": correlation["command_surface_present"],
                    "mavlink_dispatch_allowed": correlation["mavlink_dispatch_allowed"],
                    "ros_dispatch_allowed": readiness["ros_dispatch_allowed"],
                    "hardware_target_allowed": readiness["hardware_target_allowed"],
                    "physical_execution_invoked": readiness[
                        "physical_execution_invoked"
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()

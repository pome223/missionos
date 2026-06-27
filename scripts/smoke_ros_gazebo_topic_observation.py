#!/usr/bin/env python3
"""Runtime smoke for ROS/Gazebo observation-only topic adapter diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import tempfile

from src.runtime.px4_gazebo_delivery_world_profile import (
    build_px4_gazebo_delivery_world_profile,
)
from src.runtime.px4_gazebo_state_correlation import (
    build_px4_gazebo_delivery_state_correlation,
    build_px4_sitl_delivery_readiness_diagnostics,
)
from src.runtime.px4_sitl_delivery_observation import (
    build_px4_sitl_delivery_observation_from_logs,
)
from src.runtime.ros_gazebo_topic_observation import (
    attach_ros_gazebo_topic_observation_artifacts,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_ROS_GAZEBO_TOPIC_OBSERVATION_SMOKE"
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
            f"Set {OPT_IN_ENV}=1 to run the ROS/Gazebo topic observation smoke."
        )


def main() -> None:
    _require_opt_in()
    profile = build_px4_gazebo_delivery_world_profile(now=NOW)
    observation = build_px4_sitl_delivery_observation_from_logs(
        PX4_SITL_LOGS,
        captured_at=NOW,
        profile=profile,
    )
    correlation = build_px4_gazebo_delivery_state_correlation(
        profile=profile,
        observation=observation,
        gazebo_pose={
            "entity_name": "delivery_vehicle_state",
            "x": -10.0,
            "y": 0.0,
            "z": 0.05,
        },
        observed_at=NOW,
    )
    readiness = build_px4_sitl_delivery_readiness_diagnostics(
        state_correlation=correlation,
        checked_at=NOW,
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TaskStore(f"{tmpdir}/tasks.db")
        task = store.create(
            kind="ros_gazebo_topic_observation",
            title="ROS/Gazebo topic observation smoke",
            status="running",
            artifacts={
                "px4_sitl_delivery_readiness_diagnostics": readiness.model_dump(
                    mode="json"
                ),
            },
        )
        artifacts = attach_ros_gazebo_topic_observation_artifacts(
            task["task_id"],
            readiness_diagnostics=readiness,
            checked_at=NOW,
            task_store_factory=lambda: store,
        )
        updated = store.get(task["task_id"])
        assert updated is not None
        adapter = artifacts["ros_gazebo_delivery_topic_observation_adapter"]
        diagnostics = artifacts["px4_gazebo_command_surface_diagnostics"]
        assert updated["status"] == "running"
        assert adapter["topic_observation_mode"] == "read_only_topic_refs"
        assert diagnostics["command_surface_status"] == "closed"
        assert adapter["ros_action_dispatch_allowed"] is False
        assert diagnostics["mavlink_dispatch_allowed"] is False
        assert diagnostics["hardware_target_allowed"] is False
        assert diagnostics["physical_execution_invoked"] is False
        print(
            json.dumps(
                {
                    "task_id": task["task_id"],
                    "task_status": updated["status"],
                    "adapter_schema": adapter["schema_version"],
                    "observed_topic_refs": adapter["observed_topic_refs"],
                    "topic_observation_mode": adapter["topic_observation_mode"],
                    "diagnostics_schema": diagnostics["schema_version"],
                    "command_surface_status": diagnostics["command_surface_status"],
                    "rejected_topic_refs": diagnostics["rejected_topic_refs"],
                    "socket_opened": diagnostics["socket_opened"],
                    "mavlink_frame_sent": diagnostics["mavlink_frame_sent"],
                    "ros_action_dispatch_allowed": diagnostics[
                        "ros_action_dispatch_allowed"
                    ],
                    "ros_dispatch_allowed": diagnostics["ros_dispatch_allowed"],
                    "mavlink_dispatch_allowed": diagnostics["mavlink_dispatch_allowed"],
                    "gazebo_entity_mutation_allowed": diagnostics[
                        "gazebo_entity_mutation_allowed"
                    ],
                    "hardware_target_allowed": diagnostics["hardware_target_allowed"],
                    "physical_execution_invoked": diagnostics[
                        "physical_execution_invoked"
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Opt-in actual PX4 + Gazebo SITL telemetry-only smoke for #408.

This smoke starts the real `px4io/px4-sitl-gazebo:latest` stack with the
`gz_x500` model, observes Gazebo pose samples and PX4 MAVLink HEARTBEAT frames
for at least five seconds, and attaches a telemetry-only HIL/gate artifact chain
to a temporary task. It does not upload missions, send MAVLink commands, publish
ROS actions, send setpoints, execute actuators, or mutate Gazebo entities.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from src.runtime.gz_sim_log_collector import parse_gz_sim_entity_pose
from src.runtime.px4_gazebo_sitl_telemetry_run import (
    PX4_GAZEBO_SITL_TELEMETRY_RUN_SCHEMA_VERSION,
    attach_px4_gazebo_sitl_telemetry_run_artifacts,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_PX4_GAZEBO_SITL_TELEMETRY_RUN_SMOKE"
ROOT_DIR = Path(__file__).resolve().parents[1]
CONTAINER_NAME = "boiled-claw-px4-gazebo-sitl-telemetry-run-smoke"
PX4_GAZEBO_IMAGE = os.getenv(
    "PX4_GAZEBO_SITL_TELEMETRY_IMAGE",
    "px4io/px4-sitl-gazebo:latest",
)
PX4_MODEL = "gz_x500"
GAZEBO_WORLD = "default"
MAVLINK_PX4_PORT = 14602
MAVLINK_OBSERVER_PORT = 14652


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the actual PX4/Gazebo SITL telemetry smoke."
        )


def _run(
    command: list[str],
    *,
    check: bool = True,
    input_text: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT_DIR,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def _start_container() -> None:
    _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "--add-host",
            "host.docker.internal:host-gateway",
            "-e",
            f"PX4_SIM_MODEL={PX4_MODEL}",
            "-e",
            f"PX4_GZ_WORLD={GAZEBO_WORLD}",
            "-e",
            "HEADLESS=1",
            "-e",
            "PX4_GZ_NO_FOLLOW=1",
            PX4_GAZEBO_IMAGE,
            "-d",
        ],
        timeout=240,
    )
    _wait_for_startup()


def _stop_container() -> None:
    _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)


def _logs(tail: str = "320") -> str:
    return _run(["docker", "logs", "--tail", tail, CONTAINER_NAME], check=False).stdout


def _wait_for_startup(timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last_logs = ""
    while time.monotonic() < deadline:
        logs = _logs()
        if (
            "Gazebo world is ready" in logs
            and "gz_bridge] world: default, model: x500_0" in logs
            and "Startup script returned successfully" in logs
        ):
            return
        last_logs = logs
        time.sleep(1)
    raise RuntimeError(
        "timed out waiting for actual PX4/Gazebo SITL startup: " + last_logs[-800:]
    )


def _pose_sample() -> dict[str, float]:
    sample = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            "timeout 5 gz topic -e -t /world/default/pose/info -n 1",
        ],
        timeout=10,
    ).stdout
    pose = parse_gz_sim_entity_pose(sample, entity_name="x500_0")
    return {key: float(pose[key]) for key in ("x", "y", "z")}


def _collect_pose_samples(*, sample_count: int, interval_seconds: float) -> list[dict]:
    samples: list[dict[str, Any]] = []
    for index in range(sample_count):
        pose = _pose_sample()
        samples.append(
            {
                **pose,
                "battery_remaining_pct": 96.0,
                "flight_mode": "standby",
                "mission_state": "idle",
                "gps_fix": True,
                "ekf_status": "nominal",
                "link_quality": "nominal",
            }
        )
        if index < sample_count - 1:
            time.sleep(interval_seconds)
    return samples


def _observe_mavlink_heartbeat(*, window_seconds: float) -> dict[str, Any]:
    script = f"""
import json
import socket
import subprocess
import time

subprocess.run(
    [
        "/opt/px4-gazebo/bin/px4-mavlink",
        "start",
        "-u",
        "{MAVLINK_PX4_PORT}",
        "-r",
        "400000",
        "-t",
        "127.0.0.1",
        "-o",
        "{MAVLINK_OBSERVER_PORT}",
        "-m",
        "onboard",
    ],
    check=False,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
frames = []
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.settimeout(1)
    sock.bind(("127.0.0.1", {MAVLINK_OBSERVER_PORT}))
    deadline = time.monotonic() + {window_seconds!r}
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        if not data or data[0] not in (0xFD, 0xFE):
            continue
        if data[0] == 0xFD and len(data) >= 10:
            msg_id = data[7] | (data[8] << 8) | (data[9] << 16)
        elif data[0] == 0xFE and len(data) >= 6:
            msg_id = data[5]
        else:
            msg_id = None
        frames.append({{"msg_id": msg_id, "length": len(data)}})
print(json.dumps({{
    "observation_window_seconds": {window_seconds!r},
    "frame_count": len(frames),
    "heartbeat_count": sum(1 for frame in frames if frame["msg_id"] == 0),
    "first_msg_ids": [frame["msg_id"] for frame in frames[:8]],
}}, sort_keys=True))
"""
    result = _run(
        ["docker", "exec", "-i", CONTAINER_NAME, "python3", "-"],
        input_text=script,
        timeout=int(window_seconds) + 15,
    )
    if not result.stdout.strip():
        raise RuntimeError(
            "PX4 MAVLink HEARTBEAT observer produced no JSON output: "
            + result.stderr[-500:]
        )
    return json.loads(result.stdout.strip())


def _inspect_container() -> dict[str, Any]:
    result = _run(
        ["docker", "inspect", CONTAINER_NAME, "--format", "{{json .}}"],
        timeout=20,
    )
    return json.loads(result.stdout)


def _exercise(*, sample_count: int, interval_seconds: float) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    pose_samples = _collect_pose_samples(
        sample_count=sample_count,
        interval_seconds=interval_seconds,
    )
    mavlink = _observe_mavlink_heartbeat(window_seconds=5.5)
    finished_at = datetime.now(timezone.utc)
    log_text = _logs(tail="420")
    inspect_data = _inspect_container()
    state = inspect_data["State"]
    config = inspect_data["Config"]

    metadata = {
        "container_id": inspect_data["Id"][:12],
        "container_started_at": state["StartedAt"],
        "px4_gazebo_image": config["Image"],
        "px4_model": PX4_MODEL,
        "gazebo_world": GAZEBO_WORLD,
        "mavlink_observation_mode": "internal_container_receive_only",
        "mavlink_command_sent": False,
        "mission_upload_performed": False,
        "setpoint_stream_performed": False,
    }

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_gazebo_sitl_telemetry_run_smoke",
            title="Actual PX4/Gazebo SITL telemetry-only smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        artifacts = attach_px4_gazebo_sitl_telemetry_run_artifacts(
            task_id=task["task_id"],
            log_text=log_text,
            pose_samples=pose_samples,
            mavlink_frame_count=int(mavlink["frame_count"]),
            mavlink_heartbeat_count=int(mavlink["heartbeat_count"]),
            mavlink_observation_window_seconds=float(
                mavlink["observation_window_seconds"]
            ),
            started_at=started_at,
            finished_at=finished_at,
            max_duration_seconds=90.0,
            source_id=CONTAINER_NAME,
            px4_image_ref=PX4_GAZEBO_IMAGE,
            gazebo_image_ref=PX4_GAZEBO_IMAGE,
            px4_model=PX4_MODEL,
            gazebo_world=GAZEBO_WORLD,
            metadata=metadata,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])
        assert stored is not None

    run = artifacts["px4_gazebo_sitl_telemetry_run"]
    assert run["schema_version"] == PX4_GAZEBO_SITL_TELEMETRY_RUN_SCHEMA_VERSION
    assert run["px4_gazebo_sitl_started"] is True
    assert run["mavlink_heartbeat_observed"] is True
    assert run["mavlink_heartbeat_count"] >= 1
    assert run["gazebo_pose_sample_count"] >= 2
    assert run["vehicle_takeoff_observed"] is False
    assert run["external_dispatch_performed"] is False
    assert run["mavlink_command_sent"] is False
    assert run["mavlink_dispatch_performed"] is False
    assert run["physical_execution_invoked"] is False
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"]["kept"] is True

    return {
        "container_name": CONTAINER_NAME,
        "image": PX4_GAZEBO_IMAGE,
        "schema_version": run["schema_version"],
        "source_kind": run["source_kind"],
        "source_id": run["source_id"],
        "px4_gazebo_sitl_started": run["px4_gazebo_sitl_started"],
        "telemetry_collected": run["telemetry_collected"],
        "mavlink_heartbeat_observed": run["mavlink_heartbeat_observed"],
        "mavlink_heartbeat_count": run["mavlink_heartbeat_count"],
        "mavlink_frame_count": run["mavlink_frame_count"],
        "mavlink_observation_window_seconds": run["mavlink_observation_window_seconds"],
        "gazebo_pose_sample_count": run["gazebo_pose_sample_count"],
        "vehicle_spawn_marker_observed": run["vehicle_spawn_marker_observed"],
        "vehicle_takeoff_observed": run["vehicle_takeoff_observed"],
        "hil_evidence_created": run["hil_evidence_created"],
        "gate_created": run["gate_created"],
        "gate_passed": artifacts["autonomy_gate_result"]["passed"],
        "task_status": stored["status"],
        "existing_artifact_kept": stored["artifacts"]["existing"]["kept"],
        "external_dispatch_performed": run["external_dispatch_performed"],
        "mavlink_command_sent": run["mavlink_command_sent"],
        "mavlink_dispatch_performed": run["mavlink_dispatch_performed"],
        "ros_dispatch_performed": run["ros_dispatch_performed"],
        "actuator_execution_performed": run["actuator_execution_performed"],
        "px4_mission_upload_performed": run["px4_mission_upload_performed"],
        "gazebo_entity_mutation_performed": run["gazebo_entity_mutation_performed"],
        "hardware_target_allowed": run["hardware_target_allowed"],
        "physical_execution_invoked": run["physical_execution_invoked"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--sample-count", type=int, default=6)
    parser.add_argument("--sample-interval-seconds", type=float, default=1.0)
    args = parser.parse_args()

    _require_opt_in()
    _start_container()
    try:
        summary = _exercise(
            sample_count=args.sample_count,
            interval_seconds=args.sample_interval_seconds,
        )
        print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
        print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    finally:
        if not args.keep_running:
            _stop_container()
    return 0


if __name__ == "__main__":
    sys.exit(main())

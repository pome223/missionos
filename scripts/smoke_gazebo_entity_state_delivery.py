#!/usr/bin/env python3
"""Opt-in Gazebo entity state-driven delivery smoke.

This starts the real `gz sim` delivery state world and observes the moving
`delivery_vehicle_state` entity through read-only Gazebo pose topics. Mission OS
uses those observed poses to derive pickup/enroute/dropoff/completed telemetry
and lets runner v0 make the terminal task decision. It does not mutate Gazebo,
use ROS/MAVLink, upload PX4 missions, send setpoints, execute actuators, or
perform live/physical execution.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICE_NAME = "boiled-claw-gz-sim-delivery-entity-state"
PROFILE = "gz-sim-delivery-entity-state"
OPT_IN_ENV = "RUN_GAZEBO_ENTITY_STATE_DELIVERY_SMOKE"
POSE_TOPIC = "/world/delivery_state_driven/pose/info"
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the Gazebo entity-state delivery smoke."
        )


def _run_command(
    command: list[str],
    *,
    capture: bool = False,
    timeout: int | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=ROOT_DIR,
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def _compose(*args: str, capture: bool = False, timeout: int | None = None):
    return _run_command(
        ["docker", "compose", "--profile", PROFILE, *args],
        capture=capture,
        timeout=timeout,
    )


def _logs() -> str:
    result = _compose("logs", "--no-color", "--tail", "320", SERVICE_NAME, capture=True)
    return result.stdout


def _wait_for_delivery_state_world(*, timeout_seconds: float = 120.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_logs = ""
    while time.monotonic() < deadline:
        logs = _logs()
        clean = _strip_ansi(logs)
        if (
            "Gazebo Sim Server v" in clean
            and "Loading SDF world file" in clean
            and "/worlds/delivery_state_driven.sdf" in clean
            and (
                "/world/delivery_state_driven/state" in clean
                or "/world/delivery_state_driven/scene/info" in clean
            )
        ):
            return logs
        last_logs = logs
        time.sleep(2)
    raise RuntimeError(
        "Gazebo entity-state delivery world logs did not appear: "
        + last_logs[-500:]
    )


def _inspect_service() -> dict:
    result = _run_command(
        ["docker", "inspect", SERVICE_NAME, "--format", "{{json .}}"],
        capture=True,
    )
    return json.loads(result.stdout)


def _read_pose_topic() -> str:
    result = _compose(
        "exec",
        "-T",
        SERVICE_NAME,
        "sh",
        "-lc",
        f"timeout 8 gz topic -e -t {POSE_TOPIC} -n 1",
        capture=True,
        timeout=12,
    )
    return result.stdout


def _collect_pose_samples(*, timeout_seconds: float = 60.0) -> list[str]:
    from src.runtime.gz_sim_log_collector import (
        delivery_phases_from_entity_poses,
        parse_gz_sim_entity_pose,
    )

    deadline = time.monotonic() + timeout_seconds
    samples: list[str] = []
    parsed: list[dict[str, float | str]] = []
    required_phases = {"pickup", "enroute", "dropoff", "completed"}
    while time.monotonic() < deadline:
        try:
            sample = _read_pose_topic()
            pose = parse_gz_sim_entity_pose(sample)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError):
            time.sleep(1)
            continue
        samples.append(sample)
        parsed.append(pose)
        observed_phases = set(delivery_phases_from_entity_poses(parsed))
        if len(parsed) >= 4 and required_phases.issubset(observed_phases):
            delta_x = float(parsed[-1]["x"]) - float(parsed[0]["x"])
            if delta_x >= 0.25 and float(parsed[-1]["x"]) >= 25.0:
                return samples
        time.sleep(1)
    raise RuntimeError(
        "Gazebo entity pose did not observe pickup/enroute/dropoff/completed "
        "before timeout"
    )


def _provenance(inspect_data: dict, *, started_at: str, finished_at: str) -> dict:
    host_config = inspect_data["HostConfig"]
    config = inspect_data["Config"]
    state = inspect_data["State"]
    env = {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for item in config.get("Env") or []
        if "=" in item
    }
    return {
        "compose_profile": PROFILE,
        "compose_service": SERVICE_NAME,
        "world_name": "delivery_state_driven",
        "world_sdf_path": "/worlds/delivery_state_driven.sdf",
        "delivery_world_ref": "simulators/gazebo/worlds/delivery_state_driven.sdf",
        "observed_entity_name": "delivery_vehicle_state",
        "pose_topic": POSE_TOPIC,
        "container_id": inspect_data["Id"][:12],
        "container_started_at": state["StartedAt"],
        "collector_started_at": started_at,
        "collector_finished_at": finished_at,
        "source_image": config["Image"],
        "image_tag": config["Image"].split(":")[-1] if ":" in config["Image"] else "",
        "network_mode": host_config["NetworkMode"],
        "port_bindings": host_config["PortBindings"],
        "read_only_rootfs": host_config["ReadonlyRootfs"],
        "privileged": host_config["Privileged"],
        "cap_drop": host_config.get("CapDrop") or [],
        "tmpfs": host_config.get("Tmpfs") or {},
        "home": env.get("HOME", ""),
        "xdg_cache_home": env.get("XDG_CACHE_HOME", ""),
        "xdg_config_home": env.get("XDG_CONFIG_HOME", ""),
        "gz_log_path": env.get("GZ_LOG_PATH", ""),
        "gazebo_invocation_args": list(config.get("Cmd") or []),
        "actual_gazebo_entity_state_delivery": True,
    }


def _contract(now: datetime):
    from src.runtime.delivery_mission_contract import build_delivery_mission_contract

    return build_delivery_mission_contract(
        mission_id="gazebo-entity-state-delivery-smoke-001",
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
        package_constraints={"package_id": "pkg-gazebo-entity-state", "max_weight_kg": 1.2},
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
        now=now,
    )


def _exercise_gazebo_entity_state_delivery() -> dict:
    from src.runtime.gazebo_delivery_scenario import build_gazebo_delivery_scenario
    from src.runtime.gz_sim_log_collector import (
        collect_gz_sim_delivery_entity_state_sanitized,
    )
    from src.runtime.simulated_delivery_runner import run_simulated_delivery_task_v0
    from src.runtime.task_store import TaskStore

    captured_at = datetime.now(timezone.utc)
    collector_started_at = captured_at.isoformat()
    logs = _wait_for_delivery_state_world()
    pose_samples = _collect_pose_samples()
    collector_finished_at = datetime.now(timezone.utc).isoformat()
    inspect_data = _inspect_service()
    host_config = inspect_data["HostConfig"]
    state = inspect_data["State"]
    provenance = _provenance(
        inspect_data,
        started_at=collector_started_at,
        finished_at=collector_finished_at,
    )
    sanitized = collect_gz_sim_delivery_entity_state_sanitized(
        logs,
        pose_samples,
        captured_at=captured_at,
        provenance=provenance,
    )
    contract = _contract(captured_at)
    scenario = build_gazebo_delivery_scenario(
        delivery_mission_contract=contract,
        now=captured_at,
    )

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="gazebo_entity_state_delivery_smoke",
            title="Gazebo entity-state delivery smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        updated = run_simulated_delivery_task_v0(
            task["task_id"],
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            sanitized_telemetry=sanitized,
            now=captured_at,
            task_store_factory=lambda: store,
        )
        artifacts = updated["artifacts"]
        timeline = store.query_timeline(task["task_id"])

    return {
        "service": SERVICE_NAME,
        "profile": PROFILE,
        "gazebo_process_running": state["Running"],
        "delivery_world_loaded": sanitized.metadata["delivery_world_loaded"],
        "actual_gz_sim_process_started": sanitized.metadata[
            "actual_gz_sim_process_started"
        ],
        "delivery_progress_source": sanitized.metadata["delivery_progress_source"],
        "gazebo_entity_state_observed": sanitized.metadata[
            "gazebo_entity_state_observed"
        ],
        "gazebo_entity_motion_observed": sanitized.metadata[
            "gazebo_entity_motion_observed"
        ],
        "mission_os_gazebo_mutation_allowed": sanitized.metadata[
            "mission_os_gazebo_mutation_allowed"
        ],
        "observed_entity_name": sanitized.metadata["observed_entity_name"],
        "entity_pose_sample_count": sanitized.metadata["entity_pose_sample_count"],
        "entity_motion_delta_x_m": sanitized.metadata["entity_motion_delta_x_m"],
        "observed_delivery_phases": sanitized.metadata["observed_delivery_phases"],
        "observed_delivery_phase_count": sanitized.measurements[
            "observed_delivery_phase_count"
        ],
        "latest_delivery_progress_phase": sanitized.metadata[
            "latest_delivery_progress_phase"
        ],
        "pickup_reached": sanitized.measurements["pickup_reached"],
        "dropoff_reached": sanitized.measurements["dropoff_reached"],
        "route_progress_percent": sanitized.measurements["route_progress_percent"],
        "task_status": updated["status"],
        "final_task_status": artifacts["simulated_delivery_runner_result"][
            "final_task_status"
        ],
        "telemetry_window_created": "gazebo_delivery_telemetry_window" in artifacts,
        "hil_evidence_created": "hil_telemetry_evidence" in artifacts,
        "hil_review_created": "hil_telemetry_review" in artifacts,
        "delivery_gate_created": "delivery_mission_gate_result" in artifacts,
        "delivery_gate_passed": artifacts["delivery_mission_gate_result"]["passed"],
        "progress_status": artifacts["delivery_progress_review"]["status"],
        "recovery_primary_action": artifacts["delivery_recovery_decision"][
            "primary_action"
        ],
        "status_changed_event": any(
            event["event_type"] == "status_changed"
            and event.get("status") == "completed"
            for event in timeline["events"]
        ),
        "existing_artifact_kept": artifacts["existing"]["kept"],
        "approval_promotion_reuse_created": any(
            key in artifacts
            for key in ("approval", "promotion_package", "reuse_plan", "runtime_reuse")
        ),
        "network_mode": host_config["NetworkMode"],
        "port_bindings": host_config["PortBindings"],
        "read_only_rootfs": host_config["ReadonlyRootfs"],
        "privileged": host_config["Privileged"],
        "cap_drop": host_config.get("CapDrop") or [],
        "live_execution_allowed": artifacts["simulated_delivery_runner_result"][
            "live_execution_allowed"
        ],
        "physical_execution_invoked": artifacts["simulated_delivery_runner_result"][
            "physical_execution_invoked"
        ],
        "command_payload_allowed": artifacts["simulated_delivery_runner_result"][
            "command_payload_allowed"
        ],
        "gazebo_entity_mutation_allowed": artifacts["simulated_delivery_runner_result"][
            "gazebo_entity_mutation_allowed"
        ],
        "ros_dispatch_allowed": artifacts["simulated_delivery_runner_result"][
            "ros_dispatch_allowed"
        ],
        "mavlink_dispatch_allowed": artifacts["simulated_delivery_runner_result"][
            "mavlink_dispatch_allowed"
        ],
        "actuator_execution_allowed": artifacts["simulated_delivery_runner_result"][
            "actuator_execution_allowed"
        ],
    }


def _stop_service() -> None:
    subprocess.run(
        ["docker", "compose", "--profile", PROFILE, "stop", SERVICE_NAME],
        cwd=ROOT_DIR,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["docker", "compose", "--profile", PROFILE, "rm", "-f", SERVICE_NAME],
        cwd=ROOT_DIR,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    _require_opt_in()
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()

    _stop_service()
    try:
        _compose("up", "-d", "--build", SERVICE_NAME, timeout=240)
        summary = _exercise_gazebo_entity_state_delivery()
        print(json.dumps(summary, indent=2, sort_keys=True))
    finally:
        if not args.keep_running:
            _stop_service()


if __name__ == "__main__":
    main()

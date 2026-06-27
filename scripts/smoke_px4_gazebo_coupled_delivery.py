#!/usr/bin/env python3
"""Opt-in smoke for actual PX4/Gazebo coupled delivery motion."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import subprocess
import time
from tempfile import TemporaryDirectory
from typing import Any, Callable

from src.runtime.gz_sim_log_collector import parse_gz_sim_entity_pose
from src.runtime.px4_gazebo_coupled_delivery import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_TAKEOFF,
    build_px4_gazebo_coupled_command_allowlist,
    build_px4_gazebo_coupled_command_approval,
    build_px4_gazebo_coupled_delivery_phase_evidence,
    run_px4_gazebo_coupled_delivery_task,
    validate_px4_gazebo_coupled_command_dispatch,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_PX4_GAZEBO_COUPLED_DELIVERY_SMOKE"
CONTAINER_NAME = "boiled-claw-px4-gazebo-coupled-delivery-smoke"
PX4_GAZEBO_IMAGE = os.getenv(
    "PX4_GAZEBO_COUPLED_DELIVERY_IMAGE",
    "px4io/px4-sitl-gazebo:latest",
)
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

MAVLINK_COMMAND_HELPER = r"""
import math
import socket
import struct
import sys
import time

MAVLINK2_MAGIC = 0xFD
MAVLINK_MSG_ID_HEARTBEAT = 0
MAVLINK_MSG_ID_COMMAND_LONG = 76
MAV_TYPE_GCS = 6
MAV_AUTOPILOT_INVALID = 8
MAV_STATE_ACTIVE = 4
MAVLINK_VERSION = 3
CRC_EXTRA = {0: 50, 76: 152}
COMMANDS = {
    "arm": (400, [1, 0, 0, 0, 0, 0, 0]),
    "takeoff": (22, [0, 0, 0, 0, math.nan, math.nan, 2.5]),
    "land": (21, [0, 0, 0, 0, math.nan, math.nan, 0]),
}


def _accumulate(byte, crc):
    tmp = byte ^ (crc & 0xFF)
    tmp = (tmp ^ (tmp << 4)) & 0xFF
    return ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF


def _crc(data, extra):
    crc = 0xFFFF
    for byte in data:
        crc = _accumulate(byte, crc)
    return _accumulate(extra, crc)


def _frame(msg_id, payload, sequence):
    header = bytes(
        [
            len(payload),
            0,
            0,
            sequence,
            255,
            190,
            msg_id & 0xFF,
            (msg_id >> 8) & 0xFF,
            (msg_id >> 16) & 0xFF,
        ]
    )
    return (
        bytes([MAVLINK2_MAGIC])
        + header
        + payload
        + struct.pack("<H", _crc(header + payload, CRC_EXTRA[msg_id]))
    )


def _heartbeat(sequence):
    payload = struct.pack(
        "<IBBBBB",
        0,
        MAV_TYPE_GCS,
        MAV_AUTOPILOT_INVALID,
        0,
        MAV_STATE_ACTIVE,
        MAVLINK_VERSION,
    )
    return _frame(MAVLINK_MSG_ID_HEARTBEAT, payload, sequence)


def _command_long(command_id, params, sequence):
    payload = struct.pack(
        "<fffffffHBBB",
        *[float(item) for item in params],
        int(command_id),
        1,
        1,
        0,
    )
    return _frame(MAVLINK_MSG_ID_COMMAND_LONG, payload, sequence)


command_name = sys.argv[1]
if command_name not in COMMANDS:
    raise SystemExit(f"unsupported command: {command_name}")

command_id, params = COMMANDS[command_name]
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 14550))
    remote = ("127.0.0.1", 18570)
    for sequence in range(3):
        sock.sendto(_heartbeat(sequence), remote)
        time.sleep(0.1)
    sock.sendto(_command_long(command_id, params, 10), remote)

print(
    {
        "mavlink_command_name": command_name,
        "mavlink_command_id": command_id,
        "mavlink_frame_sent_to_px4": True,
    }
)
"""


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo coupled delivery smoke."
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
            "-e",
            "PX4_SIM_MODEL=gz_x500",
            "-e",
            "PX4_GZ_WORLD=default",
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


def _logs(tail: str = "240") -> str:
    return _run(["docker", "logs", "--tail", tail, CONTAINER_NAME], check=False).stdout


def _wait_for_startup(timeout: float = 80.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = _logs()
        if (
            "Gazebo world is ready" in logs
            and "gz_bridge] world: default, model: x500_0" in logs
            and "Startup script returned successfully" in logs
        ):
            return
        time.sleep(1)
    raise RuntimeError("timed out waiting for PX4/Gazebo coupled startup")


def _wait_for_px4_home(timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "home set" in _logs():
            return
        time.sleep(1)
    raise RuntimeError("timed out waiting for PX4 home set before coupled command")


def _pose_sample() -> tuple[str, float]:
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
    return sample, float(pose["z"])


COMMAND_IDS = {
    "arm": MAV_CMD_COMPONENT_ARM_DISARM,
    "takeoff": MAV_CMD_NAV_TAKEOFF,
    "land": MAV_CMD_NAV_LAND,
}


def _send_mavlink_command(
    command_name: str,
    *,
    approval: Any,
    allowlist: Any,
) -> None:
    validate_px4_gazebo_coupled_command_dispatch(
        approval=approval,
        allowlist=allowlist,
        command_id=COMMAND_IDS[command_name],
    )
    _run(
        ["docker", "exec", "-i", CONTAINER_NAME, "python3", "-", command_name],
        input_text=MAVLINK_COMMAND_HELPER,
        timeout=15,
    )


def _send_until_z(
    command_names: list[str],
    predicate: Callable[[float, list[float]], bool],
    *,
    approval: Any,
    allowlist: Any,
    timeout: float,
    resend_interval: float = 5.0,
) -> tuple[str, float, list[float]]:
    deadline = time.monotonic() + timeout
    samples: list[float] = []
    last_sent_at = 0.0
    last_sample = ""
    last_z = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now - last_sent_at >= resend_interval:
            for command_name in command_names:
                _send_mavlink_command(
                    command_name,
                    approval=approval,
                    allowlist=allowlist,
                )
            last_sent_at = now
        last_sample, last_z = _pose_sample()
        samples.append(last_z)
        if predicate(last_z, samples):
            return last_sample, last_z, samples
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for Gazebo z predicate; samples={samples}")


def _wait_for_z(
    predicate: Callable[[float, list[float]], bool],
    *,
    timeout: float = 45.0,
) -> tuple[str, float, list[float]]:
    deadline = time.monotonic() + timeout
    samples: list[float] = []
    last_sample = ""
    last_z = 0.0
    while time.monotonic() < deadline:
        last_sample, last_z = _pose_sample()
        samples.append(last_z)
        if predicate(last_z, samples):
            return last_sample, last_z, samples
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for Gazebo z predicate; samples={samples}")


def _marker_subset(logs: str, markers: list[str]) -> list[str]:
    return [marker for marker in markers if marker in logs]


def _wait_for_markers(markers: list[str], *, timeout: float = 30.0) -> list[str]:
    deadline = time.monotonic() + timeout
    accepted: list[str] = []
    while time.monotonic() < deadline:
        accepted = _marker_subset(_logs(), markers)
        if accepted == markers:
            return accepted
        time.sleep(1)
    return accepted


def main() -> int:
    _require_opt_in()
    _start_container()
    try:
        _wait_for_px4_home()
        with TemporaryDirectory() as tmp:
            store = TaskStore(f"{tmp}/tasks.db")
            task = store.create(
                kind="px4_gazebo_coupled_delivery",
                title="PX4/Gazebo coupled delivery smoke",
                status="running",
                artifacts={
                    "existing": {"case_id": "actual-px4-gazebo-coupled", "kept": True}
                },
            )
            approval = build_px4_gazebo_coupled_command_approval(
                operator_approval_performed=True,
                now=NOW,
            )
            allowlist = build_px4_gazebo_coupled_command_allowlist(
                approval=approval,
                now=NOW,
            )
            persisted = store.update(
                task["task_id"],
                artifacts={
                    "px4_gazebo_coupled_command_approval": approval.model_dump(
                        mode="json"
                    ),
                    "px4_gazebo_coupled_command_allowlist": allowlist.model_dump(
                        mode="json"
                    ),
                },
            )
            assert persisted is not None
            reloaded = store.get(task["task_id"])
            assert reloaded is not None
            assert "px4_gazebo_coupled_command_approval" in reloaded["artifacts"]
            assert "px4_gazebo_coupled_command_allowlist" in reloaded["artifacts"]

            _pickup_sample, pickup_z = _pose_sample()
            _enroute_sample, enroute_z, climb_samples = _send_until_z(
                ["arm", "takeoff"],
                lambda z, _samples: z >= 1.0,
                approval=approval,
                allowlist=allowlist,
                timeout=70.0,
            )
            peak_z = max([pickup_z, enroute_z, *climb_samples])
            _send_mavlink_command("land", approval=approval, allowlist=allowlist)
            _dropoff_sample, dropoff_z, descent_samples = _wait_for_z(
                lambda z, samples: len(samples) >= 2
                and max(samples) >= 1.0
                and 0.2 < z < max(samples) - 0.25,
                timeout=60.0,
            )
            peak_z = max([peak_z, *descent_samples])
            _completed_sample, completed_z, landing_samples = _wait_for_z(
                lambda z, _samples: z <= 0.15,
                timeout=80.0,
            )
            peak_z = max([peak_z, *landing_samples])
            required_markers = [
                "Armed by external command",
                "Takeoff detected",
                "Landing detected",
                "Disarmed by landing",
            ]
            accepted_markers = _wait_for_markers(required_markers)
            if accepted_markers != required_markers:
                raise RuntimeError(
                    "missing PX4 coupled command markers: "
                    f"{set(required_markers) - set(accepted_markers)}"
                )

            evidence = [
                build_px4_gazebo_coupled_delivery_phase_evidence(
                    mission_phase="pickup",
                    px4_container_image=PX4_GAZEBO_IMAGE,
                    approval=approval,
                    allowlist=allowlist,
                    mavlink_command_names=["MAV_CMD_COMPONENT_ARM_DISARM"],
                    mavlink_command_ids=[400],
                    px4_acceptance_log_markers=["Armed by external command"],
                    gazebo_pose_before_z_m=pickup_z,
                    gazebo_pose_after_z_m=pickup_z,
                    gazebo_pose_peak_z_m=peak_z,
                    now=NOW,
                ),
                build_px4_gazebo_coupled_delivery_phase_evidence(
                    mission_phase="enroute",
                    px4_container_image=PX4_GAZEBO_IMAGE,
                    approval=approval,
                    allowlist=allowlist,
                    mavlink_command_names=["MAV_CMD_NAV_TAKEOFF"],
                    mavlink_command_ids=[22],
                    px4_acceptance_log_markers=["Takeoff detected"],
                    gazebo_pose_before_z_m=pickup_z,
                    gazebo_pose_after_z_m=enroute_z,
                    gazebo_pose_peak_z_m=peak_z,
                    now=NOW,
                ),
                build_px4_gazebo_coupled_delivery_phase_evidence(
                    mission_phase="dropoff",
                    px4_container_image=PX4_GAZEBO_IMAGE,
                    approval=approval,
                    allowlist=allowlist,
                    mavlink_command_names=["MAV_CMD_NAV_LAND"],
                    mavlink_command_ids=[21],
                    px4_acceptance_log_markers=["Landing detected"],
                    gazebo_pose_before_z_m=peak_z,
                    gazebo_pose_after_z_m=dropoff_z,
                    gazebo_pose_peak_z_m=peak_z,
                    now=NOW,
                ),
                build_px4_gazebo_coupled_delivery_phase_evidence(
                    mission_phase="completed",
                    px4_container_image=PX4_GAZEBO_IMAGE,
                    approval=approval,
                    allowlist=allowlist,
                    mavlink_command_names=["MAV_CMD_NAV_LAND"],
                    mavlink_command_ids=[21],
                    px4_acceptance_log_markers=["Disarmed by landing"],
                    gazebo_pose_before_z_m=peak_z,
                    gazebo_pose_after_z_m=completed_z,
                    gazebo_pose_peak_z_m=peak_z,
                    now=NOW,
                ),
            ]
            updated = run_px4_gazebo_coupled_delivery_task(
                task["task_id"],
                phase_evidence=evidence,
                now=NOW,
                task_store_factory=lambda: store,
            )

        runner = updated["artifacts"]["px4_gazebo_coupled_delivery_runner_result"]
        summary = {
            "task_status": updated["status"],
            "existing_artifacts_retained": updated["artifacts"]["existing"]["kept"],
            "phase_evidence_count": len(
                updated["artifacts"]["px4_gazebo_coupled_delivery_phase_evidence"]
            ),
            "observed_delivery_phases": runner["observed_delivery_phases"],
            "final_status": runner["final_status"],
            "completion_basis": runner["completion_basis"],
            "completion_mode": runner["completion_mode"],
            "actual_px4_sitl_container_started": runner[
                "actual_px4_sitl_container_started"
            ],
            "actual_gazebo_world_started": runner["actual_gazebo_world_started"],
            "actual_gz_bridge_started": runner["actual_gz_bridge_started"],
            "delivery_phase_command_executed": runner[
                "delivery_phase_command_executed"
            ],
            "simulation_actuator_effect_observed": runner[
                "simulation_actuator_effect_observed"
            ],
            "px4_gazebo_coupled_motion_observed": runner[
                "px4_gazebo_coupled_motion_observed"
            ],
            "simulation_mavlink_dispatch_allowed": runner[
                "simulation_mavlink_dispatch_allowed"
            ],
            "simulation_actuator_effect_allowed": runner[
                "simulation_actuator_effect_allowed"
            ],
            "physical_actuator_execution_allowed": runner[
                "physical_actuator_execution_allowed"
            ],
            "hardware_target_allowed": runner["hardware_target_allowed"],
            "physical_execution_invoked": runner["physical_execution_invoked"],
            "px4_mission_upload_allowed": runner["px4_mission_upload_allowed"],
            "unbounded_setpoint_stream_allowed": runner[
                "unbounded_setpoint_stream_allowed"
            ],
            "operator_approval_ref": updated["artifacts"][
                "px4_gazebo_coupled_delivery_phase_evidence"
            ][0]["operator_approval_ref"],
            "bounded_allowlist_ref": updated["artifacts"][
                "px4_gazebo_coupled_delivery_phase_evidence"
            ][0]["bounded_allowlist_ref"],
            "approval_artifact_schema": updated["artifacts"][
                "px4_gazebo_coupled_command_approval"
            ]["schema_version"],
            "allowlist_artifact_schema": updated["artifacts"][
                "px4_gazebo_coupled_command_allowlist"
            ]["schema_version"],
            "pickup_z_m": pickup_z,
            "enroute_z_m": enroute_z,
            "dropoff_z_m": dropoff_z,
            "completed_z_m": completed_z,
            "peak_z_m": peak_z,
            "px4_acceptance_log_markers": accepted_markers,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        assert summary["task_status"] == "completed"
        assert summary["existing_artifacts_retained"] is True
        assert summary["phase_evidence_count"] == 4
        assert summary["observed_delivery_phases"] == [
            "pickup",
            "enroute",
            "dropoff",
            "completed",
        ]
        assert (
            summary["completion_basis"]
            == "actual_px4_mavlink_command_and_gazebo_coupled_motion"
        )
        assert summary["completion_mode"] == "coupled_command_driven_delivery_completed"
        assert summary["actual_px4_sitl_container_started"] is True
        assert summary["actual_gazebo_world_started"] is True
        assert summary["actual_gz_bridge_started"] is True
        assert summary["delivery_phase_command_executed"] is True
        assert summary["simulation_actuator_effect_observed"] is True
        assert summary["px4_gazebo_coupled_motion_observed"] is True
        assert summary["simulation_mavlink_dispatch_allowed"] is True
        assert summary["simulation_actuator_effect_allowed"] is True
        assert summary["physical_actuator_execution_allowed"] is False
        assert summary["hardware_target_allowed"] is False
        assert summary["physical_execution_invoked"] is False
        assert summary["px4_mission_upload_allowed"] is False
        assert summary["unbounded_setpoint_stream_allowed"] is False
        assert summary["operator_approval_ref"]
        assert summary["bounded_allowlist_ref"]
        assert float(summary["peak_z_m"]) >= 1.0
        assert float(summary["completed_z_m"]) <= 0.15
        return 0
    finally:
        _stop_container()


if __name__ == "__main__":
    raise SystemExit(main())

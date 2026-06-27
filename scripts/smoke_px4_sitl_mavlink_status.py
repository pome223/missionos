#!/usr/bin/env python3
"""Opt-in runtime smoke against an actual PX4 SITL MAVLink endpoint."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from tempfile import TemporaryDirectory

from src.runtime.px4_real_mavlink_transport import (
    MAVLINK_MSG_ID_HEARTBEAT,
    PX4SITLMAVLinkStatusSmoke,
    decode_mavlink2_header,
    encode_mavlink2_heartbeat,
    encode_mavlink2_request_message,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_PX4_SITL_MAVLINK_STATUS_SMOKE"
CONTAINER_NAME = "boiled-claw-px4-sitl-mavlink-status-smoke"
PX4_IMAGE = "px4io/px4-sitl:latest"
LOCAL_OBSERVATION_PORT = 14650
PX4_MAVLINK_PORT = 14600
PX4_MAVLINK_REMOTE_HOST = os.getenv("PX4_SITL_MAVLINK_REMOTE_HOST", "192.168.65.254")


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4 SITL MAVLink status smoke."
        )


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def _start_px4_container() -> None:
    _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)
    _run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--network",
            "host",
            "--name",
            CONTAINER_NAME,
            "-e",
            "PX4_SIM_MODEL=sihsim_quadx",
            PX4_IMAGE,
            "-d",
        ]
    )
    time.sleep(4)
    _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            (
                f"/opt/px4/bin/px4-mavlink start -u {PX4_MAVLINK_PORT} "
                f"-r 400000 -t {PX4_MAVLINK_REMOTE_HOST} "
                f"-o {LOCAL_OBSERVATION_PORT} -m onboard"
            ),
        ]
    )


def _stop_px4_container() -> None:
    _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)


def _receive_px4_frame(
    sock: socket.socket, timeout: float = 20.0
) -> tuple[dict, tuple[str, int]]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
            return decode_mavlink2_header(data), (str(addr[0]), int(addr[1]))
        except socket.timeout as exc:
            last_error = exc
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"timed out waiting for PX4 MAVLink frame: {last_error}")


def main() -> int:
    _require_opt_in()
    _start_px4_container()
    try:
        with TemporaryDirectory() as tmp:
            store = TaskStore(f"{tmp}/tasks.db")
            task = store.create(
                kind="px4_sitl_mavlink_status_smoke",
                title="PX4 SITL MAVLink status smoke",
                status="running",
                artifacts={"existing": {"case_id": "actual-px4-sitl", "kept": True}},
            )
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1.0)
                sock.bind(("0.0.0.0", LOCAL_OBSERVATION_PORT))
                time.sleep(1)
                first_frame, px4_addr = _receive_px4_frame(sock)
                sock.sendto(
                    encode_mavlink2_heartbeat(sequence=201),
                    px4_addr,
                )
                sock.sendto(
                    encode_mavlink2_request_message(
                        requested_message_id=MAVLINK_MSG_ID_HEARTBEAT,
                        target_system=1,
                        target_component=1,
                        sequence=202,
                    ),
                    px4_addr,
                )
                followup_frames = []
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline and len(followup_frames) < 2:
                    try:
                        frame, _addr = _receive_px4_frame(sock, timeout=1.0)
                    except RuntimeError:
                        continue
                    followup_frames.append(frame)

            logs = _run(["docker", "logs", "--tail", "80", CONTAINER_NAME]).stdout
            artifact = PX4SITLMAVLinkStatusSmoke(
                px4_startup_confirmed="Startup script returned successfully" in logs,
                mavlink_frame_received_from_px4=True,
                mavlink_frame_sent_to_px4=True,
                heartbeat_frame_sent_to_px4=True,
                request_message_frame_sent_to_px4=True,
                px4_source_addr=px4_addr,
                px4_mavlink_remote_host=PX4_MAVLINK_REMOTE_HOST,
                first_received_msg_id=first_frame["msg_id"],
                followup_msg_ids=[frame["msg_id"] for frame in followup_frames],
                metadata={
                    "issue": 328,
                    "parent_epic": 307,
                    "smoke_only_diagnostic": True,
                },
            )
            updated = store.update(
                task["task_id"],
                artifacts={
                    "px4_sitl_mavlink_status_smoke": artifact.model_dump(mode="json")
                },
            )
        assert updated is not None
        artifact = updated["artifacts"]["px4_sitl_mavlink_status_smoke"]
        summary = {
            "task_status": updated["status"],
            "existing_artifacts_retained": updated["artifacts"]["existing"]["kept"],
            "actual_px4_sitl_container_started": artifact[
                "actual_px4_sitl_container_started"
            ],
            "px4_startup_confirmed": artifact["px4_startup_confirmed"],
            "mavlink_socket_opened": artifact["mavlink_socket_opened"],
            "mavlink_frame_received_from_px4": artifact[
                "mavlink_frame_received_from_px4"
            ],
            "mavlink_frame_sent_to_px4": artifact["mavlink_frame_sent_to_px4"],
            "heartbeat_frame_sent_to_px4": artifact["heartbeat_frame_sent_to_px4"],
            "request_message_frame_sent_to_px4": artifact[
                "request_message_frame_sent_to_px4"
            ],
            "delivery_phase_command_executed": artifact[
                "delivery_phase_command_executed"
            ],
            "first_received_msg_id": artifact["first_received_msg_id"],
            "px4_mavlink_remote_host": artifact["px4_mavlink_remote_host"],
            "followup_msg_ids": artifact["followup_msg_ids"],
            "hardware_target_allowed": artifact["hardware_target_allowed"],
            "physical_execution_invoked": artifact["physical_execution_invoked"],
        }
        print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
        assert summary["task_status"] == "running"
        assert summary["existing_artifacts_retained"] is True
        assert summary["actual_px4_sitl_container_started"] is True
        assert summary["px4_startup_confirmed"] is True
        assert summary["mavlink_socket_opened"] is True
        assert summary["mavlink_frame_received_from_px4"] is True
        assert summary["mavlink_frame_sent_to_px4"] is True
        assert summary["heartbeat_frame_sent_to_px4"] is True
        assert summary["request_message_frame_sent_to_px4"] is True
        assert summary["delivery_phase_command_executed"] is False
        assert isinstance(summary["first_received_msg_id"], int)
        assert summary["followup_msg_ids"]
        assert summary["hardware_target_allowed"] is False
        assert summary["physical_execution_invoked"] is False
        return 0
    finally:
        _stop_px4_container()


if __name__ == "__main__":
    raise SystemExit(main())

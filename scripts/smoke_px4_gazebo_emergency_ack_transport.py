#!/usr/bin/env python3
"""Opt-in smoke for actual PX4/Gazebo emergency ACK return-path transport."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path

from src.runtime.px4_gazebo_emergency_dispatcher import (
    MAV_CMD_NAV_LAND,
    build_px4_gazebo_emergency_ack_transport_diagnostic,
)

OPT_IN_ENV = "RUN_PX4_GAZEBO_EMERGENCY_ACK_TRANSPORT_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

HELPER = r"""
import json
import math
import socket
import struct
import time

MAVLINK2_MAGIC = 0xFD
MAVLINK1_MAGIC = 0xFE
MAVLINK_MSG_ID_HEARTBEAT = 0
MAVLINK_MSG_ID_COMMAND_LONG = 76
MAVLINK_MSG_ID_COMMAND_ACK = 77
MAV_TYPE_GCS = 6
MAV_AUTOPILOT_INVALID = 8
MAV_STATE_ACTIVE = 4
MAVLINK_VERSION = 3
CRC_EXTRA = {0: 50, 76: 152}
ACK_RESULT_NAMES = {0: "ACCEPTED", 1: "TEMPORARILY_REJECTED", 2: "DENIED", 3: "UNSUPPORTED", 4: "FAILED"}


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
            sequence & 0xFF,
            255,
            190,
            msg_id & 0xFF,
            (msg_id >> 8) & 0xFF,
            (msg_id >> 16) & 0xFF,
        ]
    )
    checksum = _crc(header + payload, CRC_EXTRA[msg_id])
    return bytes([MAVLINK2_MAGIC]) + header + payload + struct.pack("<H", checksum)


def _heartbeat(sequence):
    payload = struct.pack(
        "<IBBBBB",
        0,
        MAV_TYPE_GCS,
        MAV_AUTOPILOT_INVALID,
        0,
        0,
        MAV_STATE_ACTIVE,
    ) + bytes([MAVLINK_VERSION])
    return _frame(MAVLINK_MSG_ID_HEARTBEAT, payload, sequence)


def _command_long(command_id, sequence):
    params = (0.0, 0.0, 0.0, 0.0, math.nan, math.nan, 0.0)
    payload = struct.pack("<fffffffHBBB", *params, int(command_id), 1, 1, 0)
    return _frame(MAVLINK_MSG_ID_COMMAND_LONG, payload, sequence)


def _decode_frame(data):
    if len(data) < 8:
        return None
    if data[0] == MAVLINK1_MAGIC:
        payload_len = data[1]
        msg_id = data[5]
        payload = data[6 : 6 + payload_len]
        return {"msg_id": msg_id, "payload": payload}
    if len(data) < 12 or data[0] != MAVLINK2_MAGIC:
        return None
    payload_len = data[1]
    msg_id = data[7] | (data[8] << 8) | (data[9] << 16)
    payload = data[10 : 10 + payload_len]
    return {"msg_id": msg_id, "payload": payload}


def _wait_command_ack(sock, command_id, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    sock.settimeout(0.2)
    while time.monotonic() < deadline:
        try:
            data, _addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        decoded = _decode_frame(data)
        if decoded is None or decoded["msg_id"] != MAVLINK_MSG_ID_COMMAND_ACK:
            continue
        payload = decoded["payload"]
        if len(payload) < 10:
            continue
        ack_command_id, result_code, _progress, _param2, _target_system, _target_component = struct.unpack("<HBBiBB", payload[:10])
        if int(ack_command_id) != int(command_id):
            continue
        return {
            "observed": True,
            "result_code": int(result_code),
            "result_name": ACK_RESULT_NAMES.get(int(result_code), "UNKNOWN"),
        }
    return {"observed": False, "result_code": None, "result_name": None}


command_id = 21
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 14651))
    remote = ("127.0.0.1", 14601)
    sequence = 0
    for _ in range(5):
        sock.sendto(_heartbeat(sequence), remote)
        sequence += 1
        time.sleep(0.1)
    sock.sendto(_command_long(command_id, sequence), remote)
    ack = _wait_command_ack(sock, command_id, 5.0)
    print(
        json.dumps(
            {
                "command_id": command_id,
                "frame_sent": True,
                "ack_observed": bool(ack["observed"]),
                "ack_result_code": ack["result_code"],
                "ack_result_name": ack["result_name"],
                "local_bind_port": 14651,
                "px4_endpoint_port": 14601,
            },
            sort_keys=True,
        )
    )
"""


def _horizontal_smoke_module():
    path = Path(__file__).with_name("smoke_px4_gazebo_horizontal_route_delivery.py")
    spec = importlib.util.spec_from_file_location("horizontal_route_smoke", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load horizontal route smoke module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo emergency ACK transport smoke."
        )


def main() -> int:
    _require_opt_in()
    horizontal = _horizontal_smoke_module()
    horizontal._start_container()
    try:
        horizontal._wait_for_px4_home()
        result = horizontal._run(
            [
                "docker",
                "exec",
                "-i",
                horizontal.CONTAINER_NAME,
                "python3",
                "-",
            ],
            input_text=HELPER,
            timeout=20,
        )
        observed = json.loads(result.stdout.strip())
        logs = horizontal._all_logs()
        diagnostic = build_px4_gazebo_emergency_ack_transport_diagnostic(
            transport_mode="container_local_emergency_mavlink",
            command_id=observed["command_id"],
            command_name="MAV_CMD_NAV_LAND",
            frame_sent=observed["frame_sent"],
            command_ack_observed=observed["ack_observed"],
            command_ack_result_code=observed["ack_result_code"],
            command_ack_result_name=observed["ack_result_name"],
            px4_state_observed="Landing" in logs or "Landing at" in logs,
            px4_state_label="px4_landing_log_observed",
            now=NOW,
        )
        summary = {
            "schema_version": "px4_gazebo_emergency_ack_transport_smoke.v1",
            "diagnostic_schema_version": diagnostic.schema_version,
            "diagnostic_id": diagnostic.diagnostic_id,
            "actual_px4_gazebo_container_started": True,
            "transport_mode": diagnostic.transport_mode,
            "support_status": diagnostic.support_status.value,
            "command_id": observed["command_id"],
            "command_name": "MAV_CMD_NAV_LAND",
            "frame_sent": observed["frame_sent"],
            "command_ack_observed": observed["ack_observed"],
            "command_ack_result_code": observed["ack_result_code"],
            "command_ack_result_name": observed["ack_result_name"],
            "ack_complete_transport_supported": diagnostic.ack_complete_transport_supported,
            "px4_log_landing_observed": diagnostic.px4_state_observed,
            "px4_state_label": diagnostic.px4_state_label,
            "completion_basis": diagnostic.completion_basis,
            "hardware_target_allowed": diagnostic.hardware_target_allowed,
            "physical_execution_invoked": diagnostic.physical_execution_invoked,
            "px4_mission_upload_allowed": diagnostic.px4_mission_upload_allowed,
            "unbounded_setpoint_stream_allowed": diagnostic.unbounded_setpoint_stream_allowed,
            "observed_at": diagnostic.observed_at.isoformat(),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        assert summary["command_id"] == MAV_CMD_NAV_LAND
        assert summary["frame_sent"] is True
        assert summary["ack_complete_transport_supported"] is False
        assert summary["support_status"] == "ack_unavailable_state_observed"
        assert summary["command_ack_observed"] is False
        assert summary["command_ack_result_code"] is None
        assert summary["command_ack_result_name"] is None
        assert summary["px4_log_landing_observed"] is True
        assert summary["completion_basis"] == "state_observed_after_dispatch_timeout"
        assert summary["hardware_target_allowed"] is False
        assert summary["physical_execution_invoked"] is False
        return 0
    finally:
        horizontal._stop_container()


if __name__ == "__main__":
    raise SystemExit(main())

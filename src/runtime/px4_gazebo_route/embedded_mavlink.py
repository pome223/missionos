"""Embedded MAVLink helper programs used by the PX4/Gazebo route runtime.

These programs execute inside the opt-in simulator container. Keeping their
source isolated prevents transport mechanics from being coupled to route
authority, orchestration, and verification logic.
"""

from __future__ import annotations

MAVLINK_ROUTE_HELPER = r"""
import json
import math
import socket
import struct
import sys
import time

MAVLINK2_MAGIC = 0xFD
MAVLINK1_MAGIC = 0xFE
MAVLINK_MSG_ID_HEARTBEAT = 0
MAVLINK_MSG_ID_COMMAND_LONG = 76
MAVLINK_MSG_ID_COMMAND_ACK = 77
MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED = 84
MAV_TYPE_GCS = 6
MAV_AUTOPILOT_INVALID = 8
MAV_STATE_ACTIVE = 4
MAVLINK_VERSION = 3
MAV_FRAME_LOCAL_NED = 1
CRC_EXTRA = {0: 50, 76: 152, 84: 143}
COMMANDS = {
    "arm": (400, [1, 0, 0, 0, 0, 0, 0]),
    "takeoff": (22, [0, 0, 0, 0, math.nan, math.nan, 2.5]),
    "land": (21, [0, 0, 0, 0, math.nan, math.nan, 0]),
    "offboard": (176, [1, 6, 0, 0, 0, 0, 0]),
}
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
    return bytes([MAVLINK2_MAGIC]) + header + payload + struct.pack(
        "<H", _crc(header + payload, CRC_EXTRA[msg_id])
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
        ack_command_id, result_code, _progress, _param2, _target_system, _target_component = struct.unpack(
            "<HBBiBB", payload[:10]
        )
        if int(ack_command_id) != int(command_id):
            continue
        return {
            "observed": True,
            "result_code": int(result_code),
            "result_name": ACK_RESULT_NAMES.get(int(result_code), "UNKNOWN"),
        }
    return {"observed": False, "result_code": None, "result_name": None}


def _setpoint_local_ned(x, y, z, sequence, vx=0.0, vy=0.0, vz=0.0):
    type_mask_position_only = 0b0000110111111000
    type_mask_position_velocity = 0b0000110111000000
    type_mask = (
        type_mask_position_velocity
        if any(abs(float(value)) > 0.0 for value in (vx, vy, vz))
        else type_mask_position_only
    )
    payload = struct.pack(
        "<IfffffffffffHBBB",
        0,
        float(x),
        float(y),
        float(z),
        float(vx),
        float(vy),
        float(vz),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        type_mask,
        1,
        1,
        MAV_FRAME_LOCAL_NED,
    )
    return _frame(MAVLINK_MSG_ID_SET_POSITION_TARGET_LOCAL_NED, payload, sequence)


def _feed_forward_scale(elapsed_seconds, duration_seconds, ramp_start_fraction, ramp_end_fraction):
    if duration_seconds <= 0.0:
        return 0.0
    progress = max(0.0, min(1.0, elapsed_seconds / duration_seconds))
    ramp_start_fraction = max(0.0, min(1.0, ramp_start_fraction))
    ramp_end_fraction = max(ramp_start_fraction, min(1.0, ramp_end_fraction))
    if progress <= ramp_start_fraction:
        return 1.0
    if progress >= ramp_end_fraction:
        return 0.0
    span = ramp_end_fraction - ramp_start_fraction
    if span <= 0.0:
        return 0.0
    return 1.0 - ((progress - ramp_start_fraction) / span)


mode = sys.argv[1]
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 14650))
    remote = ("127.0.0.1", 14600)
    sequence = 0
    setpoint_frames_sent = 0
    if mode in COMMANDS:
        for _ in range(3):
            sock.sendto(_heartbeat(sequence), remote)
            sequence += 1
            time.sleep(0.1)
        command_id, params = COMMANDS[mode]
        sock.sendto(_command_long(command_id, params, sequence), remote)
        ack = _wait_command_ack(sock, command_id, 5.0)
        print(
            json.dumps(
                {
                    "mode": mode,
                    "command_id": command_id,
                    "sent": True,
                    "command_ack_required": True,
                    "command_ack_timeout_seconds": 5.0,
                    "command_ack_observed": bool(ack["observed"]),
                    "command_ack_result_code": ack["result_code"],
                    "command_ack_result_name": ack["result_name"],
                    "blocked_reasons": []
                    if ack["observed"] is True and ack["result_code"] == 0
                    else [f"{mode}_command_ack_not_accepted"],
                }
            )
        )
    elif mode == "route":
        target_x = float(sys.argv[2])
        target_y = float(sys.argv[3])
        target_z = float(sys.argv[4])
        duration_seconds = float(sys.argv[5])
        feed_forward_vx = float(sys.argv[6]) if len(sys.argv) > 6 else 0.0
        feed_forward_vy = float(sys.argv[7]) if len(sys.argv) > 7 else 0.0
        ramp_start_fraction = float(sys.argv[8]) if len(sys.argv) > 8 else 0.65
        ramp_end_fraction = float(sys.argv[9]) if len(sys.argv) > 9 else 0.9
        feed_forward_scale_samples = []
        for _ in range(40):
            sock.sendto(_heartbeat(sequence), remote)
            sequence += 1
            sock.sendto(
                _setpoint_local_ned(
                    target_x,
                    target_y,
                    target_z,
                    sequence,
                    feed_forward_vx,
                    feed_forward_vy,
                ),
                remote,
            )
            sequence += 1
            setpoint_frames_sent += 1
            time.sleep(0.05)
        sock.sendto(_command_long(*COMMANDS["offboard"], sequence), remote)
        sequence += 1
        ack = _wait_command_ack(sock, COMMANDS["offboard"][0], 5.0)
        if ack["observed"] is not True or ack["result_code"] != 0:
            print(
                json.dumps(
                    {
                        "mode": mode,
                        "sent": False,
                        "offboard_mode_switch_allowed": True,
                        "offboard_mode_switch_command_id": 176,
                        "offboard_mode_switch_frame_sent": True,
                        "offboard_mode_switch_ack_required": True,
                        "offboard_mode_switch_ack_command_id": 176,
                        "offboard_mode_switch_ack_timeout_seconds": 5.0,
                        "offboard_mode_switch_ack_observed": bool(ack["observed"]),
                        "offboard_mode_switch_ack_result_code": ack["result_code"],
                        "offboard_mode_switch_ack_result_name": ack["result_name"],
                        "setpoint_frames_sent": 0,
                        "setpoint_stream_duration_seconds": 0.0,
                        "blocked_reasons": ["blocked_offboard_ack_missing"],
                    }
                )
            )
            raise SystemExit(0)
        route_started_at = time.monotonic()
        deadline = route_started_at + duration_seconds
        while time.monotonic() < deadline:
            elapsed = time.monotonic() - route_started_at
            scale = _feed_forward_scale(
                elapsed,
                duration_seconds,
                ramp_start_fraction,
                ramp_end_fraction,
            )
            feed_forward_scale_samples.append(scale)
            sock.sendto(_heartbeat(sequence), remote)
            sequence += 1
            sock.sendto(
                _setpoint_local_ned(
                    target_x,
                    target_y,
                    target_z,
                    sequence,
                    feed_forward_vx * scale,
                    feed_forward_vy * scale,
                ),
                remote,
            )
            sequence += 1
            setpoint_frames_sent += 1
            time.sleep(0.05)
        print(
            json.dumps(
                {
                    "mode": mode,
                    "sent": True,
                    "offboard_mode_switch_allowed": True,
                    "offboard_mode_switch_command_id": 176,
                    "offboard_mode_switch_frame_sent": True,
                    "offboard_mode_switch_ack_required": True,
                    "offboard_mode_switch_ack_command_id": 176,
                    "offboard_mode_switch_ack_timeout_seconds": 5.0,
                    "offboard_mode_switch_ack_observed": True,
                    "offboard_mode_switch_ack_result_code": ack["result_code"],
                    "offboard_mode_switch_ack_result_name": ack["result_name"],
                    "setpoint_frames_sent": setpoint_frames_sent,
                    "setpoint_stream_duration_seconds": duration_seconds,
                    "feed_forward_velocity_x_mps": feed_forward_vx,
                    "feed_forward_velocity_y_mps": feed_forward_vy,
                    "feed_forward_phase_schedule": "full_then_linear_ramp_down",
                    "feed_forward_ramp_start_fraction": ramp_start_fraction,
                    "feed_forward_ramp_end_fraction": ramp_end_fraction,
                    "feed_forward_scale_min": min(feed_forward_scale_samples)
                    if feed_forward_scale_samples
                    else None,
                    "feed_forward_scale_max": max(feed_forward_scale_samples)
                    if feed_forward_scale_samples
                    else None,
                    "feed_forward_scale_sample_count": len(feed_forward_scale_samples),
                    "blocked_reasons": [],
                }
            )
        )
    else:
        raise SystemExit(f"unsupported mode: {mode}")
"""

MAVLINK_HEARTBEAT_OBSERVER_HELPER = r"""
import json
import socket
import sys
import time

MAVLINK2_MAGIC = 0xFD
MAVLINK1_MAGIC = 0xFE
MAVLINK_MSG_ID_HEARTBEAT = 0


def _decode_msg_id(data):
    if len(data) < 8:
        return None
    if data[0] == MAVLINK1_MAGIC:
        return int(data[5])
    if len(data) >= 12 and data[0] == MAVLINK2_MAGIC:
        return int(data[7] | (data[8] << 8) | (data[9] << 16))
    return None


duration_seconds = float(sys.argv[1])
gap_threshold_seconds = float(sys.argv[2])
observed_at = time.time()
heartbeat_times = []
packet_count = 0
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 14650))
    sock.settimeout(0.2)
    deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        try:
            data, _addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        packet_count += 1
        if _decode_msg_id(data) == MAVLINK_MSG_ID_HEARTBEAT:
            heartbeat_times.append(time.monotonic())

intervals = [
    heartbeat_times[index] - heartbeat_times[index - 1]
    for index in range(1, len(heartbeat_times))
]
max_interval = max(intervals) if intervals else 0.0
gap_count = sum(1 for value in intervals if value > gap_threshold_seconds)
print(
    json.dumps(
        {
            "observer_status": "completed",
            "source": "udp://127.0.0.1:14650",
            "duration_seconds": duration_seconds,
            "gap_threshold_seconds": gap_threshold_seconds,
            "packet_count": packet_count,
            "heartbeat_count": len(heartbeat_times),
            "heartbeat_intervals_seconds": intervals,
            "max_heartbeat_interval_seconds": max_interval,
            "heartbeat_gap_count": gap_count,
            "heartbeat_gap_observed": bool(gap_count),
            "observer_sent_packets": False,
            "packet_drop_performed": False,
            "observed_at_epoch_seconds": observed_at,
        },
        sort_keys=True,
    )
)
"""

MAVLINK_LINK_LOSS_APPLICATOR_HELPER = r"""
import json
import socket
import subprocess
import sys
import time

MAVLINK2_MAGIC = 0xFD
MAVLINK1_MAGIC = 0xFE
MAVLINK_MSG_ID_HEARTBEAT = 0


def _decode_msg_id(data):
    if len(data) < 8:
        return None
    if data[0] == MAVLINK1_MAGIC:
        return int(data[5])
    if len(data) >= 12 and data[0] == MAVLINK2_MAGIC:
        return int(data[7] | (data[8] << 8) | (data[9] << 16))
    return None


def _run(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-500:],
        "stderr_tail": result.stderr[-500:],
    }


def _host_ip():
    result = subprocess.run(
        "getent ahostsv4 host.docker.internal | awk '{print $1; exit}'",
        capture_output=True,
        shell=True,
        text=True,
        timeout=5,
    )
    value = result.stdout.strip()
    return value or "127.0.0.1"


duration_seconds = float(sys.argv[1])
gap_threshold_seconds = float(sys.argv[2])
route_px4_port = sys.argv[3]
route_local_port = sys.argv[4]
emergency_px4_port = sys.argv[5]
emergency_local_port = sys.argv[6]
restart_emergency = sys.argv[7].strip().lower() in ("1", "true", "yes", "on")
observed_at = time.time()
heartbeat_times = []
warmup_heartbeat_count = 0
interruption_heartbeat_count = 0
post_restart_heartbeat_count = 0
packet_count = 0
commands = []
stop_started_at = None
restart_started_at = None
restart_completed_at = None
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", int(route_local_port)))
    sock.settimeout(0.2)
    warmup_deadline = time.monotonic() + 1.0
    while time.monotonic() < warmup_deadline:
        try:
            data, _addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        packet_count += 1
        if _decode_msg_id(data) == MAVLINK_MSG_ID_HEARTBEAT:
            heartbeat_times.append(time.monotonic())
            warmup_heartbeat_count += 1
    stop_started_at = time.time()
    commands.append(_run(["/opt/px4-gazebo/bin/px4-mavlink", "stop-all"]))
    silence_deadline = time.monotonic() + duration_seconds
    while time.monotonic() < silence_deadline:
        try:
            data, _addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        packet_count += 1
        if _decode_msg_id(data) == MAVLINK_MSG_ID_HEARTBEAT:
            heartbeat_times.append(time.monotonic())
            interruption_heartbeat_count += 1
    restart_started_at = time.time()
    commands.append(
        _run(
            [
                "/opt/px4-gazebo/bin/px4-mavlink",
                "start",
                "-u",
                route_px4_port,
                "-r",
                "400000",
                "-t",
                "127.0.0.1",
                "-o",
                route_local_port,
                "-m",
                "onboard",
            ]
        )
    )
    if restart_emergency:
        commands.append(
            _run(
                [
                    "/opt/px4-gazebo/bin/px4-mavlink",
                    "start",
                    "-u",
                    emergency_px4_port,
                    "-r",
                    "400000",
                    "-t",
                    _host_ip(),
                    "-o",
                    emergency_local_port,
                    "-m",
                    "onboard",
                ]
            )
        )
    restart_completed_at = time.time()
    post_deadline = time.monotonic() + 3.0
    while time.monotonic() < post_deadline:
        try:
            data, _addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        packet_count += 1
        if _decode_msg_id(data) == MAVLINK_MSG_ID_HEARTBEAT:
            heartbeat_times.append(time.monotonic())
            post_restart_heartbeat_count += 1

intervals = [
    heartbeat_times[index] - heartbeat_times[index - 1]
    for index in range(1, len(heartbeat_times))
]
max_interval = max(intervals) if intervals else 0.0
gap_count = sum(1 for value in intervals if value > gap_threshold_seconds)
restart_returncodes = [item["returncode"] for item in commands[1:]]
print(
    json.dumps(
        {
            "applicator_status": (
                "completed"
                if commands and commands[0]["returncode"] == 0 and all(code == 0 for code in restart_returncodes)
                else "failed"
            ),
            "source": f"udp://127.0.0.1:{route_local_port}",
            "duration_seconds": duration_seconds,
            "gap_threshold_seconds": gap_threshold_seconds,
            "packet_count": packet_count,
            "heartbeat_count": len(heartbeat_times),
            "warmup_heartbeat_count": warmup_heartbeat_count,
            "interruption_heartbeat_count": interruption_heartbeat_count,
            "post_restart_heartbeat_count": post_restart_heartbeat_count,
            "baseline_heartbeat_observed": bool(warmup_heartbeat_count),
            "post_restart_heartbeat_observed": bool(post_restart_heartbeat_count),
            "heartbeat_intervals_seconds": intervals,
            "max_heartbeat_interval_seconds": max_interval,
            "heartbeat_gap_count": gap_count,
            "heartbeat_gap_observed": bool(gap_count),
            "endpoint_stop_performed": bool(commands and commands[0]["returncode"] == 0),
            "endpoint_restart_performed": all(code == 0 for code in restart_returncodes),
            "emergency_endpoint_restart_requested": restart_emergency,
            "stop_started_at_epoch_seconds": stop_started_at,
            "restart_started_at_epoch_seconds": restart_started_at,
            "restart_completed_at_epoch_seconds": restart_completed_at,
            "commands": commands,
            "observer_sent_packets": False,
            "packet_drop_performed": False,
            "rf_link_loss_claimed": False,
            "vehicle_failsafe_claimed": False,
            "observed_at_epoch_seconds": observed_at,
        },
        sort_keys=True,
    )
)
"""

__all__ = [
    "MAVLINK_HEARTBEAT_OBSERVER_HELPER",
    "MAVLINK_LINK_LOSS_APPLICATOR_HELPER",
    "MAVLINK_ROUTE_HELPER",
]

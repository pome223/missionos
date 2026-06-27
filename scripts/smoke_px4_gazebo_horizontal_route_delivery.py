#!/usr/bin/env python3
"""Opt-in smoke for actual PX4/Gazebo horizontal pickup-to-dropoff route."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping
import xml.etree.ElementTree as ET

from scripts import smoke_px4_gazebo_collision_contact_event as contact_event_smoke
from scripts import smoke_px4_gazebo_sitl_mission_upload as mission_upload_smoke
from src.runtime.gz_sim_log_collector import parse_gz_sim_entity_pose
from src.runtime.px4_gazebo_coupled_delivery import (
    build_px4_gazebo_coupled_command_allowlist,
    build_px4_gazebo_coupled_command_approval,
    validate_px4_gazebo_coupled_command_dispatch,
)
from src.runtime.px4_gazebo_emergency_dispatcher import (
    build_px4_gazebo_emergency_command_allowlist,
    build_px4_gazebo_emergency_command_approval,
    run_px4_gazebo_emergency_command_dispatch,
)
from src.runtime.px4_gazebo_route_delivery import (
    build_px4_gazebo_route_delivery_completion_gate,
    run_px4_gazebo_route_delivery_task,
)
from src.runtime.px4_gazebo_route_dispatcher import (
    ROUTE_SETPOINT_STREAM_MAX_DURATION_SECONDS,
    ROUTE_SETPOINT_STREAM_MAX_FRAMES,
    build_px4_gazebo_route_command_allowlist,
    build_px4_gazebo_route_command_dispatch_result_from_observed_stream,
    build_px4_gazebo_route_deviation_abort,
    build_px4_gazebo_route_progress_evidence,
    build_px4_gazebo_route_recovery_completion,
    derive_px4_gazebo_route_target_ned,
)
from src.runtime.px4_gazebo_route_plan import (
    ROUTE_ON_DEVIATION_ACTIONS,
    build_px4_gazebo_pickup_dropoff_route_plan,
)
from src.runtime.missionos_sitl_dispatch_runtime import (
    MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND_ENV,
    WIND_COMPENSATED_ROUTE_ENV,
    WIND_COMPENSATION_METHOD_ENV,
    WIND_COMPENSATION_SOURCE_RESPONSE_ENV,
    WIND_FEED_FORWARD_MPS_ENV,
    WIND_FEED_FORWARD_RAMP_END_FRACTION_ENV,
    WIND_FEED_FORWARD_RAMP_START_FRACTION_ENV,
    WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV,
    WIND_PREEMPTIVE_OFFSET_M_ENV,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE"
ARTIFACT_ROOT_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT"
PREUPLOAD_MISSION_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_PREUPLOAD_MISSION"
SKIP_EMERGENCY_MAVLINK_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_SKIP_EMERGENCY_MAVLINK"
PAYLOAD_RELEASE_MODEL_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_PAYLOAD_RELEASE_MODEL"
WIND_MEAN_MPS_ENV = "MISSION_DESIGNER_REALISM_WIND_MEAN_MPS"
WIND_DIRECTION_DEG_ENV = "MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG"
WIND_GUST_MPS_ENV = "MISSION_DESIGNER_REALISM_WIND_GUST_MPS"
WIND_VARIANCE_ENV = "MISSION_DESIGNER_REALISM_WIND_VARIANCE"
TEMPERATURE_C_ENV = "MISSION_DESIGNER_REALISM_TEMPERATURE_C"
PRESSURE_HPA_ENV = "MISSION_DESIGNER_REALISM_PRESSURE_HPA"
THERMAL_BATTERY_DRAIN_FACTOR_ENV = (
    "MISSION_DESIGNER_REALISM_THERMAL_BATTERY_DRAIN_FACTOR"
)
THERMAL_MOTOR_DERATE_FACTOR_ENV = (
    "MISSION_DESIGNER_REALISM_THERMAL_MOTOR_DERATE_FACTOR"
)
PAYLOAD_MASS_KG_ENV = "MISSION_DESIGNER_REALISM_PAYLOAD_MASS_KG"
BATTERY_SCENARIO_ENV = "MISSION_DESIGNER_REALISM_BATTERY_SCENARIO"
BATTERY_REMAINING_PERCENT_ENV = "MISSION_DESIGNER_REALISM_BATTERY_REMAINING_PERCENT"
SENSOR_FAILURE_COMPONENT_ENV = "MISSION_DESIGNER_REALISM_SENSOR_FAILURE_COMPONENT"
SENSOR_FAILURE_TYPE_ENV = "MISSION_DESIGNER_REALISM_SENSOR_FAILURE_TYPE"
LANDING_ZONE_BLOCKED_ENV = "MISSION_DESIGNER_REALISM_LANDING_ZONE_BLOCKED"
VISIBILITY_MODE_ENV = "MISSION_DESIGNER_REALISM_VISIBILITY_MODE"
NO_FLY_ZONE_MARKER_ENV = "MISSION_DESIGNER_REALISM_NO_FLY_ZONE_MARKER"
TRAFFIC_CONFLICT_MARKER_ENV = "MISSION_DESIGNER_REALISM_TRAFFIC_CONFLICT_MARKER"
ALTERNATE_LANDING_MARKER_ENV = "MISSION_DESIGNER_REALISM_ALTERNATE_LANDING_MARKER"
RTH_BEHAVIOR_ENV = "MISSION_DESIGNER_REALISM_RTH_BEHAVIOR"
MOVING_ACTOR_MARKER_ENV = "MISSION_DESIGNER_REALISM_MOVING_ACTOR_MARKER"
COLLISION_OBSTACLE_ENV = "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE"
COLLISION_OBSTACLE_CONTACT_TOPIC_ENV = (
    "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_CONTACT_TOPIC"
)
COLLISION_OBSTACLE_START_X_ENV = "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_START_X_M"
COLLISION_OBSTACLE_START_Y_ENV = "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_START_Y_M"
COLLISION_OBSTACLE_END_X_ENV = "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_END_X_M"
COLLISION_OBSTACLE_END_Y_ENV = "MISSION_DESIGNER_REALISM_COLLISION_OBSTACLE_END_Y_M"
MULTI_DRONE_CONFLICT_PROBE_ENV = "MISSION_DESIGNER_REALISM_MULTI_DRONE_CONFLICT_PROBE"
TELEMETRY_DROPOUT_MODE_ENV = "MISSION_DESIGNER_REALISM_TELEMETRY_DROPOUT_MODE"
MAVLINK_LINK_DEGRADATION_MODE_ENV = (
    "MISSION_DESIGNER_REALISM_MAVLINK_LINK_DEGRADATION_MODE"
)
TERRAIN_WORLD_SDF_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SDF"
TERRAIN_WORLD_SHA256_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SHA256"
TERRAIN_WORLD_SOURCE_REF_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_WORLD_SOURCE_REF"
TERRAIN_PROVIDER_STATUS_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_PROVIDER_STATUS"
TERRAIN_SAMPLING_MODE_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_SAMPLING_MODE"
TERRAIN_VERTICAL_REFERENCE_ENV = (
    "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_VERTICAL_REFERENCE"
)
TERRAIN_COLLISION_MODE_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_TERRAIN_COLLISION_MODE"
PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX = (
    "payload_feasibility_advisory:mission_designer_payload_mass"
)
PAYLOAD_RECOVERY_ACTION_REF = "payload_recovery_action:mission_designer_payload_mass"
CONTAINER_NAME = "boiled-claw-px4-gazebo-horizontal-route-smoke"
ROUTE_MAVLINK_LOCAL_PORT = 14650
ROUTE_MAVLINK_PX4_PORT = 14600
EMERGENCY_MAVLINK_LOCAL_PORT = 14651
EMERGENCY_MAVLINK_PX4_PORT = 14601
PX4_GAZEBO_IMAGE = os.getenv(
    "PX4_GAZEBO_HORIZONTAL_ROUTE_IMAGE",
    "px4io/px4-sitl-gazebo:latest",
)
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_NAV_LAND = 21
PREUPLOAD_SUMMARY: dict[str, Any] | None = None
PAYLOAD_RELEASE_SUMMARY: dict[str, Any] | None = None
WIND_REALISM_SUMMARY: dict[str, Any] | None = None
THERMAL_WEATHER_REALISM_SUMMARY: dict[str, Any] | None = None
VEHICLE_REALISM_SUMMARY: dict[str, Any] | None = None
BATTERY_REALISM_SUMMARY: dict[str, Any] | None = None
SENSOR_REALISM_SUMMARY: dict[str, Any] | None = None
WORLD_REALISM_SUMMARY: dict[str, Any] | None = None
VISIBILITY_REALISM_SUMMARY: dict[str, Any] | None = None
OPERATIONAL_REALISM_SUMMARY: dict[str, Any] | None = None
MOVING_ACTOR_LINEAR_MOTION_SUMMARY: dict[str, Any] | None = None
MOVING_ACTOR_POSE_SUMMARY: dict[str, Any] | None = None
MOVING_ACTOR_PROXIMITY_SUMMARY: dict[str, Any] | None = None
COLLISION_OBSTACLE_SUMMARY: dict[str, Any] | None = None
ROUTE_BLOCKING_CANDIDATE_SUMMARY: dict[str, Any] | None = None
HORIZONTAL_CONTACT_TOPIC_SUMMARY: dict[str, Any] | None = None
OPERATIONAL_INCIDENT_REPORT_SUMMARY: dict[str, Any] | None = None
TRAFFIC_CONFLICT_VERIFICATION_SUMMARY: dict[str, Any] | None = None
ROUTE_BLOCKING_VERIFICATION_SUMMARY: dict[str, Any] | None = None
ALTERNATE_LANDING_CANDIDATE_SUMMARY: dict[str, Any] | None = None
ALTERNATE_LANDING_EXECUTION_SUMMARY: dict[str, Any] | None = None
RTH_BEHAVIOR_SUMMARY: dict[str, Any] | None = None
ALTERNATE_MISSION_UPLOAD_SUMMARY: dict[str, Any] | None = None
TELEMETRY_REALISM_SUMMARY: dict[str, Any] | None = None
MAVLINK_LINK_REALISM_SUMMARY: dict[str, Any] | None = None
TERRAIN_WORLD_REALISM_SUMMARY: dict[str, Any] | None = None
LIVE_POSE_TRACE_PATH: Path | None = None
TELEMETRY_DROPOUT_EVENTS: list[dict[str, Any]] = []
TELEMETRY_OBSERVER_SAMPLE_EVENTS: list[dict[str, Any]] = []
BATTERY_STATUS_SAMPLE_INTERVAL_SECONDS = 5.0
BATTERY_STATUS_SAMPLE_TIMEOUT_SECONDS = 1
_LAST_BATTERY_STATUS_SAMPLE_AT = 0.0
_LAST_BATTERY_STATUS_SAMPLE: dict[str, Any] = {
    "battery_status_observed": False,
    "battery_state_source": "px4-listener:battery_status_not_observed",
}
PAYLOAD_MODEL_CONTAINER_PATH = "/tmp/boiled-claw-payload-release-models"
PAYLOAD_DETACH_TOPIC = "/model/x500_0/delivery_payload/detach"
COLLISION_OBSTACLE_CONTACT_TOPIC = "/mission_designer/collision_obstacle/contacts"
MATERIALIZED_APPLICATION_STATUSES = {"applied", "applied_with_approximations"}


def _application_status_is_materialized(status: Any) -> bool:
    return status in MATERIALIZED_APPLICATION_STATUSES


def _float_env(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return float(default)


def _form2a_wind_compensation_request() -> dict[str, Any]:
    selected_response = os.getenv(MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND_ENV, "")
    compensated_route = os.getenv(WIND_COMPENSATED_ROUTE_ENV) == "1"
    method = os.getenv(WIND_COMPENSATION_METHOD_ENV, "static_target_offset")
    offset_m = _float_env(WIND_PREEMPTIVE_OFFSET_M_ENV, 0.0)
    direction_deg = _float_env(WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV, 90.0)
    feed_forward_mps = _float_env(WIND_FEED_FORWARD_MPS_ENV, 0.0)
    ramp_start_fraction = _float_env(WIND_FEED_FORWARD_RAMP_START_FRACTION_ENV, 0.65)
    ramp_end_fraction = _float_env(WIND_FEED_FORWARD_RAMP_END_FRACTION_ENV, 0.9)
    ramp_start_fraction = min(max(ramp_start_fraction, 0.0), 1.0)
    ramp_end_fraction = min(max(ramp_end_fraction, ramp_start_fraction), 1.0)
    offset_enabled = bool(
        compensated_route and method == "static_target_offset" and offset_m > 0.0
    )
    feed_forward_enabled = bool(
        compensated_route
        and method == "mid_route_velocity_feed_forward"
        and feed_forward_mps > 0.0
    )
    return {
        "schema_version": "missionos_form2a_wind_compensation_request.v1",
        "selected_response_kind": selected_response,
        "compensation_method": method,
        "compensated_route_requested": compensated_route,
        "preemptive_offset_m": offset_m,
        "preemptive_offset_direction_deg": direction_deg,
        "preemptive_offset_direction_convention": "opposite_wind_vector_xy",
        "feed_forward_mps": feed_forward_mps,
        "feed_forward_direction_deg": direction_deg,
        "feed_forward_direction_convention": "opposite_wind_vector_xy",
        "feed_forward_phase_schedule": "full_then_linear_ramp_down",
        "feed_forward_ramp_start_fraction": ramp_start_fraction,
        "feed_forward_ramp_end_fraction": ramp_end_fraction,
        "source_response_kind": os.getenv(WIND_COMPENSATION_SOURCE_RESPONSE_ENV, ""),
        "route_geometry_compensation_applied": offset_enabled,
        "velocity_feed_forward_applied": feed_forward_enabled,
        "progress_counted": False,
        "drone_physics_affected": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
    }


def _form2a_wind_compensation_xy_offset(
    request: Mapping[str, Any],
) -> tuple[float, float]:
    if request.get("route_geometry_compensation_applied") is not True:
        return (0.0, 0.0)
    offset_m = float(request.get("preemptive_offset_m") or 0.0)
    direction_rad = math.radians(float(request.get("preemptive_offset_direction_deg") or 0.0))
    wind_unit_x = math.sin(direction_rad)
    wind_unit_y = math.cos(direction_rad)
    return (-offset_m * wind_unit_x, -offset_m * wind_unit_y)


def _form2a_wind_feed_forward_xy_mps(
    request: Mapping[str, Any],
) -> tuple[float, float]:
    if request.get("velocity_feed_forward_applied") is not True:
        return (0.0, 0.0)
    feed_forward_mps = float(request.get("feed_forward_mps") or 0.0)
    direction_rad = math.radians(float(request.get("feed_forward_direction_deg") or 0.0))
    wind_unit_x = math.sin(direction_rad)
    wind_unit_y = math.cos(direction_rad)
    return (-feed_forward_mps * wind_unit_x, -feed_forward_mps * wind_unit_y)


def _form2a_wind_feed_forward_scale(
    *,
    elapsed_seconds: float,
    duration_seconds: float,
    ramp_start_fraction: float,
    ramp_end_fraction: float,
) -> float:
    if duration_seconds <= 0.0:
        return 0.0
    progress = min(max(elapsed_seconds / duration_seconds, 0.0), 1.0)
    ramp_start_fraction = min(max(ramp_start_fraction, 0.0), 1.0)
    ramp_end_fraction = min(max(ramp_end_fraction, ramp_start_fraction), 1.0)
    if progress <= ramp_start_fraction:
        return 1.0
    if progress >= ramp_end_fraction:
        return 0.0
    ramp_span = ramp_end_fraction - ramp_start_fraction
    if ramp_span <= 0.0:
        return 0.0
    return 1.0 - ((progress - ramp_start_fraction) / ramp_span)


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


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo horizontal route smoke."
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


def _payload_model_sdf_patch() -> str:
    return """
    <plugin filename="gz-sim-detachable-joint-system"
            name="gz::sim::systems::DetachableJoint">
      <parent_link>base_link</parent_link>
      <child_model>delivery_payload</child_model>
      <child_link>payload_link</child_link>
      <detach_topic>/model/x500_0/delivery_payload/detach</detach_topic>
      <attach_topic>/model/x500_0/delivery_payload/attach</attach_topic>
      <output_topic>/model/x500_0/delivery_payload/state</output_topic>
    </plugin>
"""


def _payload_world_sdf_patch(*, payload_mass_kg: float) -> str:
    return f"""
    <model name="delivery_payload">
      <pose>0 0 0.04 0 0 0</pose>
      <static>false</static>
      <link name="payload_link">
        <inertial>
          <mass>{payload_mass_kg:.6f}</mass>
          <inertia>
            <ixx>0.0001</ixx><ixy>0</ixy><ixz>0</ixz>
            <iyy>0.0001</iyy><iyz>0</iyz><izz>0.0001</izz>
          </inertia>
        </inertial>
        <collision name="payload_collision">
          <geometry><box><size>0.12 0.12 0.08</size></box></geometry>
        </collision>
        <visual name="payload_visual">
          <geometry><box><size>0.12 0.12 0.08</size></box></geometry>
          <material><diffuse>0.1 0.5 1.0 1</diffuse></material>
        </visual>
      </link>
    </model>
"""


def _landing_zone_blocked_world_sdf_patch() -> str:
    return """
    <model name="mission_designer_landing_zone_blocked_marker">
      <pose>5.0 5.0 0.025 0 0 0</pose>
      <static>true</static>
      <link name="marker_link">
        <visual name="blocked_marker_visual">
          <geometry><box><size>0.9 0.9 0.05</size></box></geometry>
          <material><diffuse>1.0 0.12 0.08 0.65</diffuse></material>
        </visual>
      </link>
    </model>
"""


VISIBILITY_FOG_RENDER_MARKER_ID = "mission_designer_visibility_fog_render_marker"
VISIBILITY_FOG_RENDER_TYPE = "linear"
VISIBILITY_FOG_RENDER_DENSITY = "0.35"
VISIBILITY_FOG_RENDER_COLOR = "0.72 0.76 0.78 1"
VISIBILITY_FOG_RENDER_START_M = "0.0"
VISIBILITY_FOG_RENDER_END_M = "25.0"


def _visibility_fog_render_marker_sdf_patch() -> str:
    return f"""
      <!-- {VISIBILITY_FOG_RENDER_MARKER_ID} -->
      <fog>
        <type>{VISIBILITY_FOG_RENDER_TYPE}</type>
        <color>{VISIBILITY_FOG_RENDER_COLOR}</color>
        <density>{VISIBILITY_FOG_RENDER_DENSITY}</density>
        <start>{VISIBILITY_FOG_RENDER_START_M}</start>
        <end>{VISIBILITY_FOG_RENDER_END_M}</end>
      </fog>
"""


def _inject_visibility_fog_render_marker(world_text: str) -> str:
    if VISIBILITY_FOG_RENDER_MARKER_ID in world_text:
        return world_text
    fog_patch = _visibility_fog_render_marker_sdf_patch()
    if "</scene>" in world_text:
        return world_text.replace("</scene>", fog_patch + "    </scene>", 1)
    return world_text.replace(
        "  </world>\n</sdf>",
        f"    <scene>{fog_patch}    </scene>\n  </world>\n</sdf>",
    )


def _visibility_marker_fog_element(world_text: str) -> ET.Element | None:
    marker_index = world_text.find(VISIBILITY_FOG_RENDER_MARKER_ID)
    if marker_index < 0:
        return None
    fog_start = world_text.find("<fog", marker_index)
    if fog_start < 0:
        return None
    scene_end = world_text.find("</scene>", marker_index)
    if scene_end >= 0 and fog_start > scene_end:
        return None
    fog_end = world_text.find("</fog>", fog_start)
    if fog_end < 0:
        return None
    fog_fragment = world_text[fog_start : fog_end + len("</fog>")]
    try:
        return ET.fromstring(fog_fragment)
    except ET.ParseError:
        return None


def _no_fly_zone_world_sdf_patch() -> str:
    return """
    <model name="mission_designer_no_fly_zone_marker">
      <pose>2.5 2.5 1.0 0 0 0</pose>
      <static>true</static>
      <link name="no_fly_zone_marker_link">
        <visual name="no_fly_zone_marker_visual">
          <geometry><cylinder><radius>1.25</radius><length>2.0</length></cylinder></geometry>
          <material><diffuse>1.0 0.0 0.0 0.22</diffuse></material>
          <transparency>0.78</transparency>
        </visual>
      </link>
    </model>
"""


def _traffic_conflict_world_sdf_patch() -> str:
    return """
    <model name="mission_designer_traffic_conflict_marker">
      <pose>3.6 2.9 0.25 0 0 0.785398</pose>
      <static>true</static>
      <link name="traffic_conflict_marker_link">
        <visual name="traffic_conflict_marker_visual">
          <geometry><box><size>0.8 0.35 0.5</size></box></geometry>
          <material><diffuse>1.0 0.62 0.0 0.48</diffuse></material>
          <transparency>0.52</transparency>
        </visual>
      </link>
    </model>
"""


def _alternate_landing_world_sdf_patch() -> str:
    return """
    <model name="mission_designer_alternate_landing_marker">
      <pose>-2.0 3.5 0.03 0 0 0</pose>
      <static>true</static>
      <link name="alternate_landing_marker_link">
        <visual name="alternate_landing_marker_visual">
          <geometry><cylinder><radius>0.65</radius><length>0.06</length></cylinder></geometry>
          <material><diffuse>0.1 0.72 1.0 0.42</diffuse></material>
          <transparency>0.58</transparency>
        </visual>
      </link>
    </model>
"""


def _moving_actor_world_sdf_patch() -> str:
    return """
    <model name="mission_designer_moving_actor_marker">
      <pose>1.2 -0.7 0.25 0 0 0</pose>
      <link name="moving_actor_marker_link">
        <gravity>false</gravity>
        <inertial>
          <mass>1.0</mass>
          <inertia>
            <ixx>0.1</ixx>
            <iyy>0.1</iyy>
            <izz>0.1</izz>
          </inertia>
        </inertial>
        <visual name="moving_actor_marker_visual">
          <geometry><box><size>0.35 0.35 0.5</size></box></geometry>
          <material><diffuse>0.95 0.15 0.65 0.58</diffuse></material>
          <transparency>0.42</transparency>
        </visual>
      </link>
      <plugin filename="gz-sim-trajectory-follower-system"
              name="gz::sim::systems::TrajectoryFollower">
        <link_name>moving_actor_marker_link</link_name>
        <loop>true</loop>
        <force>10</force>
        <torque>10</torque>
        <waypoints>
          <waypoint>1.2 -0.7</waypoint>
          <waypoint>4.2 3.2</waypoint>
        </waypoints>
      </plugin>
    </model>
"""


def _moving_actor_waypoint_motion_spec() -> dict[str, Any]:
    start_xy = [1.2, -0.7]
    end_xy = [4.2, 3.2]
    loop_seconds = 6.0
    distance_m = math.hypot(end_xy[0] - start_xy[0], end_xy[1] - start_xy[1])
    return {
        "mode": "linear_waypoint_motion",
        "actor_id": "mission_designer_moving_actor_marker",
        "frame": "gazebo_world_local",
        "start_xy_m": start_xy,
        "end_xy_m": end_xy,
        "loop_seconds": loop_seconds,
        "nominal_profile_velocity_mps": distance_m / loop_seconds,
    }


def _moving_actor_waypoint_trajectory_definition_sha256() -> str:
    return hashlib.sha256(
        json.dumps(
            _moving_actor_waypoint_motion_spec(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _collision_obstacle_world_sdf_patch(*, contact_topic_enabled: bool = True) -> str:
    motion = _collision_obstacle_motion_spec()
    start_x, start_y = motion["start_xy_m"]
    end_x, end_y = motion["end_xy_m"]
    contact_system = (
        """
    <plugin name="gz::sim::systems::Contact" filename="gz-sim-contact-system"/>
    <plugin name="gz::sim::systems::Sensors" filename="gz-sim-sensors-system">
      <render_engine>ogre2</render_engine>
    </plugin>
"""
        if contact_topic_enabled
        else ""
    )
    contact_sensor = (
        f"""
        <sensor name="collision_obstacle_contact_sensor" type="contact">
          <always_on>true</always_on>
          <update_rate>20</update_rate>
          <topic>{COLLISION_OBSTACLE_CONTACT_TOPIC}</topic>
          <contact>
            <collision>collision_obstacle_collision</collision>
          </contact>
        </sensor>
"""
        if contact_topic_enabled
        else ""
    )
    return f"""
{contact_system.rstrip()}
    <model name="mission_designer_collision_obstacle">
      <pose>{start_x} {start_y} 0.3 0 0 0</pose>
      <link name="collision_obstacle_link">
        <gravity>false</gravity>
        <inertial>
          <mass>3.0</mass>
          <inertia>
            <ixx>0.2</ixx>
            <iyy>0.2</iyy>
            <izz>0.2</izz>
          </inertia>
        </inertial>
        <collision name="collision_obstacle_collision">
          <geometry><box><size>0.8 0.8 0.6</size></box></geometry>
        </collision>
{contact_sensor.rstrip()}
        <visual name="collision_obstacle_visual">
          <geometry><box><size>0.8 0.8 0.6</size></box></geometry>
          <material><diffuse>0.95 0.25 0.15 0.82</diffuse></material>
          <transparency>0.18</transparency>
        </visual>
      </link>
      <plugin filename="gz-sim-trajectory-follower-system"
              name="gz::sim::systems::TrajectoryFollower">
        <link_name>collision_obstacle_link</link_name>
        <loop>true</loop>
        <force>20</force>
        <torque>20</torque>
        <waypoints>
          <waypoint>{start_x} {start_y}</waypoint>
          <waypoint>{end_x} {end_y}</waypoint>
        </waypoints>
      </plugin>
    </model>
"""


def _wind_effects_world_sdf_patch(*, wind_x_mps: float, wind_y_mps: float) -> str:
    return f"""
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics">
    </plugin>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands">
    </plugin>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster">
    </plugin>
    <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu">
    </plugin>
    <plugin filename="gz-sim-air-pressure-system" name="gz::sim::systems::AirPressure">
    </plugin>
    <plugin filename="gz-sim-air-speed-system" name="gz::sim::systems::AirSpeed">
    </plugin>
    <wind>
      <linear_velocity>{wind_x_mps} {wind_y_mps} 0</linear_velocity>
    </wind>
    <plugin filename="gz-sim-apply-link-wrench-system" name="gz::sim::systems::ApplyLinkWrench">
    </plugin>
    <plugin filename="gz-sim-navsat-system" name="gz::sim::systems::NavSat">
    </plugin>
    <plugin filename="gz-sim-magnetometer-system" name="gz::sim::systems::Magnetometer">
    </plugin>
    <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors">
      <render_engine>ogre2</render_engine>
    </plugin>
    <plugin filename="libOpticalFlowSystem.so" name="custom::OpticalFlowSystem">
    </plugin>
    <plugin filename="libGstCameraSystem.so" name="custom::GstCameraSystem">
    </plugin>
    <plugin filename="gz-sim-wind-effects-system" name="gz::sim::systems::WindEffects">
      <force_approximation_scaling_factor>1</force_approximation_scaling_factor>
      <horizontal>
        <magnitude>
          <time_for_rise>1</time_for_rise>
          <sin>
            <amplitude_percent>0.0</amplitude_percent>
            <period>60</period>
          </sin>
          <noise type="gaussian">
            <mean>0</mean>
            <stddev>0</stddev>
          </noise>
        </magnitude>
        <direction>
          <time_for_rise>1</time_for_rise>
          <sin>
            <amplitude>0</amplitude>
            <period>60</period>
          </sin>
          <noise type="gaussian">
            <mean>0</mean>
            <stddev>0</stddev>
          </noise>
        </direction>
      </horizontal>
      <vertical>
        <noise type="gaussian">
          <mean>0</mean>
          <stddev>0</stddev>
        </noise>
      </vertical>
    </plugin>
"""


def _enable_wind_on_x500_base(model_root: Path) -> dict[str, Any]:
    x500_base_sdf_path = model_root / "x500_base" / "model.sdf"
    if not x500_base_sdf_path.exists():
        return {
            "wind_enabled_on_vehicle_links": False,
            "wind_enabled_link_count": 0,
            "x500_base_sdf_path": str(x500_base_sdf_path),
            "error": "x500_base_model_sdf_missing",
        }
    x500_base_sdf = x500_base_sdf_path.read_text(encoding="utf-8")
    if "<enable_wind>true</enable_wind>" not in x500_base_sdf:
        x500_base_sdf = re.sub(
            r'(<link name="[^"]+">\n)',
            r"\1      <enable_wind>true</enable_wind>\n",
            x500_base_sdf,
        )
        x500_base_sdf_path.write_text(x500_base_sdf, encoding="utf-8")
    wind_enabled_link_count = x500_base_sdf_path.read_text(encoding="utf-8").count(
        "<enable_wind>true</enable_wind>"
    )
    return {
        "wind_enabled_on_vehicle_links": wind_enabled_link_count > 0,
        "wind_enabled_link_count": wind_enabled_link_count,
        "x500_base_sdf_path": str(x500_base_sdf_path),
    }


def _payload_mass_request() -> float | None:
    value = _optional_float_env(PAYLOAD_MASS_KG_ENV)
    if value is None:
        return None
    if value < 0.0 or value > 100.0:
        return None
    return value


def _payload_model_enabled() -> bool:
    return (
        os.getenv(PAYLOAD_RELEASE_MODEL_ENV) == "1"
        or _payload_mass_request() is not None
    )


def _landing_zone_blocked_requested() -> bool:
    value = (os.getenv(LANDING_ZONE_BLOCKED_ENV) or "").strip().lower()
    return value in ("1", "true", "yes", "on", "blocked")


def _visibility_mode_request() -> str | None:
    value = (os.getenv(VISIBILITY_MODE_ENV) or "").strip().lower()
    if not value:
        return None
    return value


def _no_fly_zone_marker_requested() -> bool:
    value = (os.getenv(NO_FLY_ZONE_MARKER_ENV) or "").strip().lower()
    return value in ("1", "true", "yes", "on", "visual", "marker")


def _traffic_conflict_marker_requested() -> bool:
    value = (os.getenv(TRAFFIC_CONFLICT_MARKER_ENV) or "").strip().lower()
    return value in ("1", "true", "yes", "on", "visual", "marker", "vehicle")


def _alternate_landing_marker_requested() -> bool:
    value = (os.getenv(ALTERNATE_LANDING_MARKER_ENV) or "").strip().lower()
    return value in ("1", "true", "yes", "on", "visual", "marker", "alternate")


def _rth_behavior_requested() -> bool:
    value = (os.getenv(RTH_BEHAVIOR_ENV) or "").strip().lower()
    return value in ("1", "true", "yes", "on", "rtl", "rth", "return_to_launch")


def _moving_actor_marker_requested() -> bool:
    value = (os.getenv(MOVING_ACTOR_MARKER_ENV) or "").strip().lower()
    return value in ("1", "true", "yes", "on", "visual", "marker", "actor")


def _collision_obstacle_requested() -> bool:
    value = (os.getenv(COLLISION_OBSTACLE_ENV) or "").strip().lower()
    return value in ("1", "true", "yes", "on", "collision", "obstacle", "moving")


def _collision_obstacle_contact_topic_requested() -> bool:
    value = (os.getenv(COLLISION_OBSTACLE_CONTACT_TOPIC_ENV) or "").strip().lower()
    return value in ("1", "true", "yes", "on", "enabled", "contact", "topic")


def _multi_drone_conflict_probe_requested() -> bool:
    value = (os.getenv(MULTI_DRONE_CONFLICT_PROBE_ENV) or "").strip().lower()
    return value in ("1", "true", "yes", "on", "probe", "multidrone", "multi_drone")


def _telemetry_dropout_mode_request() -> str:
    value = (os.getenv(TELEMETRY_DROPOUT_MODE_ENV) or "").strip().lower()
    aliases = {
        "": "",
        "none": "",
        "off": "",
        "observer": "observer_sample_pause",
        "observer_side_dropout": "observer_sample_pause",
        "observer_pose_gap": "observer_sample_pause",
        "pose_gap": "observer_sample_pause",
        "observer_sample_pause": "observer_sample_pause",
        "sample_pause": "observer_sample_pause",
    }
    return aliases.get(value, value)


def _mavlink_link_degradation_mode_request() -> str:
    value = (os.getenv(MAVLINK_LINK_DEGRADATION_MODE_ENV) or "").strip().lower()
    aliases = {
        "": "",
        "none": "",
        "off": "",
        "heartbeat": "heartbeat_observer",
        "heartbeat_observer": "heartbeat_observer",
        "heartbeat_gap_observer": "heartbeat_observer",
        "mavlink_heartbeat_observer": "heartbeat_observer",
        "bounded_link_loss": "bounded_link_loss",
        "bounded_mavlink_link_loss": "bounded_link_loss",
        "link_loss": "bounded_link_loss",
        "mavlink_link_loss": "bounded_link_loss",
        "link_loss_applicator": "bounded_link_loss",
        "mavlink_link_loss_applicator": "bounded_link_loss",
        "mavlink_link_loss_probe": "link_loss_probe",
        "link_loss_probe": "link_loss_probe",
    }
    return aliases.get(value, value)


def _prepare_payload_model_root(
    run_dir: Path,
    *,
    payload_mass_kg: float,
    payload_enabled: bool,
    wind_effects_enabled: bool,
    landing_zone_blocked: bool,
    visibility_mode: str | None,
    no_fly_zone_marker: bool,
    traffic_conflict_marker: bool,
    alternate_landing_marker: bool,
    moving_actor_marker: bool,
    collision_obstacle: bool,
    collision_obstacle_contact_topic: bool,
    terrain_world_sdf: Path | None,
) -> Path:
    model_root = (run_dir / "payload_release_models").resolve()
    model_root.mkdir(parents=True, exist_ok=True)
    if not (model_root / "x500" / "model.sdf").exists():
        _run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                "-v",
                f"{model_root}:/out",
                PX4_GAZEBO_IMAGE,
                "-lc",
                (
                    "rm -rf /out/x500 /out/x500_base; "
                    "mkdir -p /out/worlds; "
                    "cp -a /opt/px4-gazebo/share/gz/models/x500 /out/x500; "
                    "cp -a /opt/px4-gazebo/share/gz/models/x500_base /out/x500_base; "
                    "cp /opt/px4-gazebo/share/gz/worlds/default.sdf /out/worlds/default.sdf"
                ),
            ],
            timeout=120,
        )
    world_path = model_root / "worlds" / "default.sdf"
    terrain_source_hash = ""
    if terrain_world_sdf is not None:
        if not terrain_world_sdf.exists():
            raise FileNotFoundError(f"terrain world SDF missing: {terrain_world_sdf}")
        terrain_source_hash = hashlib.sha256(terrain_world_sdf.read_bytes()).hexdigest()
        world_text = _inject_terrain_model_into_default_world(
            default_world_text=world_path.read_text(encoding="utf-8"),
            terrain_world_sdf=terrain_world_sdf,
            model_root=model_root,
        )
        world_path.write_text(world_text, encoding="utf-8")
    world_text = world_path.read_text(encoding="utf-8")
    if terrain_source_hash:
        world_text = world_text.replace(
            "<world name=\"default\">",
            (
                "<world name=\"default\">\n"
                "    <!-- mission_designer_terrain_source_sha256:"
                f"{terrain_source_hash} -->"
            ),
            1,
        )
    if terrain_world_sdf is not None:
        terrain_heightmap_root = terrain_world_sdf.parent.parent / "heightmaps"
        if terrain_heightmap_root.exists():
            model_heightmap_root = model_root / "heightmaps"
            if model_heightmap_root.exists():
                shutil.rmtree(model_heightmap_root)
            shutil.copytree(terrain_heightmap_root, model_heightmap_root)
    if payload_enabled:
        sdf_path = model_root / "x500" / "model.sdf"
        sdf_text = sdf_path.read_text(encoding="utf-8")
        if "delivery_payload" not in sdf_text:
            sdf_text = sdf_text.replace(
                "  </model>\n</sdf>",
                _payload_model_sdf_patch() + "  </model>\n</sdf>",
            )
            sdf_path.write_text(sdf_text, encoding="utf-8")
    if payload_enabled and "delivery_payload" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _payload_world_sdf_patch(payload_mass_kg=payload_mass_kg)
            + "  </world>\n</sdf>",
        )
    if wind_effects_enabled:
        wind_requested = _wind_requested_profile()["requested"]
        wind_mean = float(wind_requested["wind_mean_mps"] or 0.0)
        wind_direction = float(wind_requested["wind_direction_deg"] or 0.0)
        wind_x, wind_y = _wind_vector(mean_mps=wind_mean, direction_deg=wind_direction)
        if "gz::sim::systems::WindEffects" not in world_text:
            world_text = world_text.replace(
                "  </world>\n</sdf>",
                _wind_effects_world_sdf_patch(
                    wind_x_mps=wind_x,
                    wind_y_mps=wind_y,
                )
                + "  </world>\n</sdf>",
            )
        _enable_wind_on_x500_base(model_root)
    if (
        landing_zone_blocked
        and "mission_designer_landing_zone_blocked_marker" not in world_text
    ):
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _landing_zone_blocked_world_sdf_patch() + "  </world>\n</sdf>",
        )
    if visibility_mode == "fog" and VISIBILITY_FOG_RENDER_MARKER_ID not in world_text:
        world_text = _inject_visibility_fog_render_marker(world_text)
    if no_fly_zone_marker and "mission_designer_no_fly_zone_marker" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _no_fly_zone_world_sdf_patch() + "  </world>\n</sdf>",
        )
    if (
        traffic_conflict_marker
        and "mission_designer_traffic_conflict_marker" not in world_text
    ):
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _traffic_conflict_world_sdf_patch() + "  </world>\n</sdf>",
        )
    if (
        alternate_landing_marker
        and "mission_designer_alternate_landing_marker" not in world_text
    ):
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _alternate_landing_world_sdf_patch() + "  </world>\n</sdf>",
        )
    if moving_actor_marker and "mission_designer_moving_actor_marker" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _moving_actor_world_sdf_patch() + "  </world>\n</sdf>",
        )
    if collision_obstacle and "mission_designer_collision_obstacle" not in world_text:
        world_text = world_text.replace(
            "  </world>\n</sdf>",
            _collision_obstacle_world_sdf_patch(contact_topic_enabled=False)
            + "  </world>\n</sdf>",
        )
    world_path.write_text(world_text, encoding="utf-8")
    return model_root


def _inject_terrain_model_into_default_world(
    *,
    default_world_text: str,
    terrain_world_sdf: Path,
    model_root: Path,
) -> str:
    terrain_world_text = terrain_world_sdf.read_text(encoding="utf-8")
    match = re.search(
        r'    <model name="digital_twin_heightmap_terrain">.*?\n    </model>',
        terrain_world_text,
        re.S,
    )
    if not match:
        raise RuntimeError("terrain world SDF did not include Digital Twin terrain model")
    terrain_model = match.group(0)
    terrain_model = re.sub(
        r"\n        <collision name=\"terrain_collision\">.*?\n        </collision>",
        "\n        <!-- terrain_collision_removed_for_visual_only_horizontal_route -->",
        terrain_model,
        flags=re.S,
    )
    heightmap_uris = sorted(set(re.findall(r"<uri>([^<]+)</uri>", terrain_model)))
    heightmap_root = model_root / "heightmaps"
    heightmap_root.mkdir(parents=True, exist_ok=True)
    for uri in heightmap_uris:
        source = Path(uri)
        if not source.is_absolute():
            source = Path(__file__).resolve().parents[1] / source
        if not source.exists():
            raise FileNotFoundError(f"terrain heightmap URI missing: {uri}")
        shutil.copy2(source, heightmap_root / source.name)
        terrain_model = terrain_model.replace(uri, f"../heightmaps/{source.name}")
    if "digital_twin_heightmap_terrain" in default_world_text:
        return default_world_text
    return default_world_text.replace("  </world>", terrain_model + "\n  </world>", 1)


def _terrain_world_sdf_request() -> Path | None:
    raw = os.getenv(TERRAIN_WORLD_SDF_ENV, "").strip()
    return Path(raw) if raw else None


def _terrain_world_readback(payload_model_root: Path | None) -> dict[str, Any]:
    requested_path = _terrain_world_sdf_request()
    result: dict[str, Any] = {
        "schema_version": "px4_gazebo_horizontal_route_terrain_world_readback.v1",
        "terrain_world_requested": requested_path is not None,
        "terrain_world_loaded_into_sitl": False,
        "terrain_artifact_used": False,
        "world_artifact_load_mode": "flat_default_world",
        "requested_world_sdf_path": str(requested_path) if requested_path else "",
        "terrain_world_source_ref": os.getenv(TERRAIN_WORLD_SOURCE_REF_ENV, ""),
        "terrain_provider_response_status": os.getenv(TERRAIN_PROVIDER_STATUS_ENV, ""),
        "terrain_sampling_mode": os.getenv(TERRAIN_SAMPLING_MODE_ENV, ""),
        "terrain_vertical_reference": os.getenv(TERRAIN_VERTICAL_REFERENCE_ENV, ""),
        "terrain_collision_mode": os.getenv(TERRAIN_COLLISION_MODE_ENV, ""),
    }
    if requested_path is None:
        return result
    if payload_model_root is None:
        result["error"] = "custom_world_root_missing"
        return result
    world_path = payload_model_root / "worlds" / "default.sdf"
    result["world_sdf_path"] = str(world_path)
    if not world_path.exists():
        result["error"] = "world_sdf_missing"
        return result
    world_text = world_path.read_text(encoding="utf-8")
    observed_sha = hashlib.sha256(world_path.read_bytes()).hexdigest()
    expected_sha = os.getenv(TERRAIN_WORLD_SHA256_ENV, "").strip()
    requested_sha = (
        hashlib.sha256(requested_path.read_bytes()).hexdigest()
        if requested_path.exists()
        else ""
    )
    result.update(
        {
            "world_sdf_sha256": observed_sha,
            "expected_world_sdf_sha256": expected_sha,
            "world_sdf_hash_match": bool(expected_sha and observed_sha == expected_sha),
            "source_world_sdf_sha256": requested_sha,
            "source_world_sdf_hash_match": bool(
                expected_sha and requested_sha == expected_sha
            ),
            "terrain_model_present": "digital_twin_heightmap_terrain" in world_text,
            "terrain_collision_present": '<collision name="terrain_collision"' in world_text,
            "terrain_collision_removed_for_visual_only_runtime": (
                "terrain_collision_removed_for_visual_only_horizontal_route"
                in world_text
            ),
            "terrain_visual_present": "terrain_visual" in world_text,
            "heightmap_file_count": len(list((payload_model_root / "heightmaps").glob("*"))),
            "world_artifact_load_mode": "terrain_injection_into_default_world",
        }
    )
    result["terrain_artifact_used"] = (
        result["terrain_model_present"] is True
        and result["terrain_visual_present"] is True
        and result["heightmap_file_count"] > 0
    )
    result["terrain_world_loaded_into_sitl"] = (
        result["terrain_artifact_used"] is True
        and (
            result["source_world_sdf_hash_match"] is True
            or not expected_sha
        )
    )
    return result


def _terrain_world_loaded_into_sitl() -> bool:
    return bool(
        (TERRAIN_WORLD_REALISM_SUMMARY or {}).get("terrain_world_loaded_into_sitl")
    )


def _terrain_relative_xy_origin(pickup_pose: dict[str, float]) -> tuple[float, float]:
    if not _terrain_world_loaded_into_sitl():
        return (0.0, 0.0)
    return (float(pickup_pose["x"]), float(pickup_pose["y"]))


def _landing_z_threshold(pickup_pose: dict[str, float]) -> float:
    if not _terrain_world_loaded_into_sitl():
        return 0.15
    return float(pickup_pose["z"]) + 0.15


def _start_container(run_dir: Path) -> Path | None:
    global PREUPLOAD_SUMMARY
    payload_model_enabled = _payload_model_enabled()
    landing_zone_blocked = _landing_zone_blocked_requested()
    visibility_mode = _visibility_mode_request()
    no_fly_zone_marker = _no_fly_zone_marker_requested()
    traffic_conflict_marker = _traffic_conflict_marker_requested()
    alternate_landing_marker = _alternate_landing_marker_requested()
    moving_actor_marker = _moving_actor_marker_requested()
    collision_obstacle = _collision_obstacle_requested()
    collision_obstacle_contact_topic = _collision_obstacle_contact_topic_requested()
    wind_effects_enabled = _wind_requested_profile()["requested_present"]
    terrain_world_sdf = _terrain_world_sdf_request()
    payload_mass_kg = _payload_mass_request() or 0.05
    payload_model_root = (
        _prepare_payload_model_root(
            run_dir,
            payload_mass_kg=payload_mass_kg,
            payload_enabled=payload_model_enabled,
            wind_effects_enabled=wind_effects_enabled,
            landing_zone_blocked=landing_zone_blocked,
            visibility_mode=visibility_mode,
            no_fly_zone_marker=no_fly_zone_marker,
            traffic_conflict_marker=traffic_conflict_marker,
            alternate_landing_marker=alternate_landing_marker,
            moving_actor_marker=moving_actor_marker,
            collision_obstacle=collision_obstacle,
            collision_obstacle_contact_topic=collision_obstacle_contact_topic,
            terrain_world_sdf=terrain_world_sdf,
        )
        if payload_model_enabled
        or wind_effects_enabled
        or landing_zone_blocked
        or visibility_mode in ("fog", "smoke")
        or no_fly_zone_marker
        or traffic_conflict_marker
        or alternate_landing_marker
        or moving_actor_marker
        or collision_obstacle
        or terrain_world_sdf is not None
        else None
    )
    extra_args: list[str] = []
    if payload_model_root is not None:
        extra_args.extend(
            [
                "-v",
                f"{payload_model_root}:{PAYLOAD_MODEL_CONTAINER_PATH}:ro",
                "-e",
                f"PX4_GZ_MODELS={PAYLOAD_MODEL_CONTAINER_PATH}",
                "-e",
                (
                    "GZ_SIM_RESOURCE_PATH="
                    f"{PAYLOAD_MODEL_CONTAINER_PATH}:"
                    "/opt/px4-gazebo/share/gz/models"
                ),
                "-e",
                f"PX4_GZ_WORLDS={PAYLOAD_MODEL_CONTAINER_PATH}/worlds",
            ]
        )
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
            "-p",
            f"{EMERGENCY_MAVLINK_PX4_PORT}:{EMERGENCY_MAVLINK_PX4_PORT}/udp",
            "-e",
            "PX4_SIM_MODEL=gz_x500",
            "-e",
            "PX4_GZ_WORLD=default",
            "-e",
            "HEADLESS=1",
            "-e",
            "PX4_GZ_NO_FOLLOW=1",
            *extra_args,
            PX4_GAZEBO_IMAGE,
            "-d",
        ],
        timeout=240,
    )
    _wait_for_startup()
    if os.getenv(PREUPLOAD_MISSION_ENV) == "1":
        mission_upload_smoke.CONTAINER_NAME = CONTAINER_NAME
        PREUPLOAD_SUMMARY = mission_upload_smoke._actual_upload()
        assert PREUPLOAD_SUMMARY["mission_ack_observed"] is True
        assert PREUPLOAD_SUMMARY["mission_ack_type"] == 0
    else:
        PREUPLOAD_SUMMARY = None
    _start_route_ack_mavlink_instance()
    if os.getenv(SKIP_EMERGENCY_MAVLINK_ENV) != "1":
        _start_emergency_mavlink_instance()
    return payload_model_root


def _stop_container() -> None:
    _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)


def _start_route_ack_mavlink_instance() -> None:
    _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            (
                f"/opt/px4-gazebo/bin/px4-mavlink start "
                f"-u {ROUTE_MAVLINK_PX4_PORT} -r 400000 "
                f"-t 127.0.0.1 -o {ROUTE_MAVLINK_LOCAL_PORT} -m onboard"
            ),
        ],
        timeout=20,
    )
    time.sleep(1)


def _start_emergency_mavlink_instance() -> None:
    _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            (
                "HOST_IP=$(getent ahostsv4 host.docker.internal | "
                "awk '{print $1; exit}'); "
                'test -n "$HOST_IP"; '
                f"/opt/px4-gazebo/bin/px4-mavlink start "
                f"-u {EMERGENCY_MAVLINK_PX4_PORT} -r 400000 "
                f'-t "$HOST_IP" -o {EMERGENCY_MAVLINK_LOCAL_PORT} '
                "-m onboard"
            ),
        ],
        timeout=20,
    )
    time.sleep(1)


def _logs(tail: str = "260") -> str:
    return _run(["docker", "logs", "--tail", tail, CONTAINER_NAME], check=False).stdout


def _all_logs() -> str:
    return _run(["docker", "logs", CONTAINER_NAME], check=False).stdout


def _artifact_root() -> Path:
    return Path(os.getenv(ARTIFACT_ROOT_ENV, "output/px4_gazebo_route_runs"))


def _new_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = _artifact_root() / f"horizontal_route_{stamp}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = _artifact_root() / f"horizontal_route_{stamp}_{suffix}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def _optional_float_env(name: str) -> float | None:
    raw = os.getenv(name)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _bounded_float_env(
    name: str,
    *,
    default: float,
    minimum: float = -10.0,
    maximum: float = 10.0,
) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw == "":
        raise ValueError(f"{name} must be a finite float")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite float") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _collision_obstacle_motion_spec() -> dict[str, Any]:
    start_xy = [
        _bounded_float_env(COLLISION_OBSTACLE_START_X_ENV, default=2.1),
        _bounded_float_env(COLLISION_OBSTACLE_START_Y_ENV, default=2.1),
    ]
    end_xy = [
        _bounded_float_env(COLLISION_OBSTACLE_END_X_ENV, default=3.7),
        _bounded_float_env(COLLISION_OBSTACLE_END_Y_ENV, default=3.7),
    ]
    loop_seconds = 6.0
    return {
        "mode": "linear_waypoint_motion",
        "obstacle_id": "mission_designer_collision_obstacle",
        "frame": "gazebo_world_local",
        "start_xy_m": start_xy,
        "end_xy_m": end_xy,
        "loop_seconds": loop_seconds,
    }


def _reset_battery_status_cache() -> None:
    global _LAST_BATTERY_STATUS_SAMPLE_AT, _LAST_BATTERY_STATUS_SAMPLE
    _LAST_BATTERY_STATUS_SAMPLE_AT = 0.0
    _LAST_BATTERY_STATUS_SAMPLE = {
        "battery_status_observed": False,
        "battery_state_source": "px4-listener:battery_status_not_observed",
    }


def _wind_requested_profile() -> dict[str, Any]:
    requested = {
        "wind_mean_mps": _optional_float_env(WIND_MEAN_MPS_ENV),
        "wind_direction_deg": _optional_float_env(WIND_DIRECTION_DEG_ENV),
        "wind_gust_mps": _optional_float_env(WIND_GUST_MPS_ENV),
        "wind_variance": _optional_float_env(WIND_VARIANCE_ENV),
    }
    return {
        "schema_version": "environment_condition_profile.v1",
        "condition_id": "environment_condition_profile:mission_designer_wind_gust",
        "condition_kind": "wind_gust",
        "requested": requested,
        "requested_present": any(value is not None for value in requested.values()),
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def _wind_vector(*, mean_mps: float, direction_deg: float) -> tuple[float, float]:
    radians = math.radians(direction_deg)
    return (
        round(mean_mps * math.sin(radians), 6),
        round(mean_mps * math.cos(radians), 6),
    )


def _thermal_weather_requested_profile() -> dict[str, Any]:
    requested = {
        "temperature_c": _optional_float_env(TEMPERATURE_C_ENV),
        "pressure_hpa": _optional_float_env(PRESSURE_HPA_ENV),
        "thermal_battery_drain_factor": _optional_float_env(
            THERMAL_BATTERY_DRAIN_FACTOR_ENV
        ),
        "thermal_motor_derate_factor": _optional_float_env(
            THERMAL_MOTOR_DERATE_FACTOR_ENV
        ),
    }
    if requested["pressure_hpa"] is not None and (
        requested["pressure_hpa"] < 500.0 or requested["pressure_hpa"] > 1100.0
    ):
        requested["pressure_hpa"] = None
    if requested["thermal_battery_drain_factor"] is not None:
        requested["thermal_battery_drain_factor"] = max(
            0.1,
            min(10.0, float(requested["thermal_battery_drain_factor"])),
        )
    if requested["thermal_motor_derate_factor"] is not None:
        requested["thermal_motor_derate_factor"] = max(
            0.1,
            min(1.0, float(requested["thermal_motor_derate_factor"])),
        )
    return {
        "schema_version": "thermal_weather_condition_profile.v1",
        "condition_id": "thermal_weather_condition_profile:mission_designer_temperature",
        "condition_kind": "thermal_weather",
        "requested": requested,
        "requested_present": any(value is not None for value in requested.values()),
        "source": "mission_designer_coordinate_route",
        "thermal_air_physics_claimed": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def _thermal_battery_drain_factor_from_temperature(
    temperature_c: float | None,
) -> float | None:
    if temperature_c is None:
        return None
    if temperature_c >= 35.0:
        return round(min(2.5, 1.0 + (temperature_c - 25.0) * 0.04), 3)
    if temperature_c <= 0.0:
        return round(min(2.2, 1.0 + abs(temperature_c) * 0.03), 3)
    return 1.0


def _thermal_motor_derate_factor_from_temperature(
    temperature_c: float | None,
) -> float | None:
    if temperature_c is None:
        return None
    if temperature_c >= 40.0:
        return round(max(0.55, 1.0 - (temperature_c - 35.0) * 0.015), 3)
    return 1.0


def _wind_readback_status(
    output: str, *, expected_x: float, expected_y: float
) -> dict[str, Any]:
    publish_status_match = re.search(r"__BC_WIND_PUBLISH_STATUS=(\d+)", output)
    readback_status_match = re.search(r"__BC_WIND_READBACK_STATUS=(\d+)", output)
    wind_message = output.split("__BC_WIND_PUBLISH_STATUS=", 1)[0].strip()
    vector_match = re.search(
        r"linear_velocity\s*\{(?P<body>.*?)\}",
        wind_message,
        flags=re.DOTALL,
    )
    parsed_x = None
    parsed_y = None
    if vector_match:
        body = vector_match.group("body")
        x_match = re.search(r"\bx:\s*([-+0-9.eE]+)", body)
        y_match = re.search(r"\by:\s*([-+0-9.eE]+)", body)
        if x_match:
            parsed_x = float(x_match.group(1))
        if y_match:
            parsed_y = float(y_match.group(1))
        if parsed_x is None:
            parsed_x = 0.0
        if parsed_y is None:
            parsed_y = 0.0
    vector_matches = (
        parsed_x is not None
        and parsed_y is not None
        and math.isclose(parsed_x, expected_x, rel_tol=1e-6, abs_tol=1e-6)
        and math.isclose(parsed_y, expected_y, rel_tol=1e-6, abs_tol=1e-6)
    )
    return {
        "readback_observed": vector_matches,
        "readback_source": "gz_topic_echo",
        "readback_publish_status": (
            int(publish_status_match.group(1)) if publish_status_match else None
        ),
        "readback_status": (
            int(readback_status_match.group(1)) if readback_status_match else None
        ),
        "readback_wind_vector_x_mps": parsed_x,
        "readback_wind_vector_y_mps": parsed_y,
        "readback_message_sha256": (
            hashlib.sha256(wind_message.encode("utf-8")).hexdigest()
            if wind_message
            else None
        ),
    }


def _wind_physics_world_readback(
    payload_model_root: Path | None,
    *,
    expected_x: float,
    expected_y: float,
) -> dict[str, Any]:
    if payload_model_root is None:
        return {
            "wind_effects_world_sdf_readback_observed": False,
            "wind_effects_plugin_materialized": False,
            "wind_world_linear_velocity_matches_requested": False,
            "wind_enabled_on_vehicle_links": False,
            "wind_enabled_link_count": 0,
            "source": "custom_world_not_used",
        }
    world_path = payload_model_root / "worlds" / "default.sdf"
    x500_base_sdf_path = payload_model_root / "x500_base" / "model.sdf"
    result: dict[str, Any] = {
        "wind_effects_world_sdf_readback_observed": False,
        "wind_effects_plugin_materialized": False,
        "wind_world_linear_velocity_matches_requested": False,
        "wind_enabled_on_vehicle_links": False,
        "wind_enabled_link_count": 0,
        "world_sdf_path": str(world_path),
        "x500_base_sdf_path": str(x500_base_sdf_path),
        "source": "gazebo_world_sdf_and_x500_base_sdf",
    }
    if not world_path.exists():
        result["error"] = "world_sdf_missing"
        return result
    try:
        world_text = world_path.read_text(encoding="utf-8")
        root = ET.fromstring(world_text)
    except Exception as exc:
        result["error"] = f"world_sdf_parse_failed:{str(exc)[-200:]}"
        return result
    result["world_sdf_sha256"] = hashlib.sha256(world_path.read_bytes()).hexdigest()
    result["wind_effects_plugin_materialized"] = any(
        plugin.attrib.get("name") == "gz::sim::systems::WindEffects"
        for plugin in root.iter("plugin")
    )
    velocity_text = (root.findtext(".//wind/linear_velocity") or "").strip()
    try:
        parts = [float(part) for part in velocity_text.split()]
    except ValueError:
        parts = []
    if len(parts) >= 2 and math.isfinite(parts[0]) and math.isfinite(parts[1]):
        result["world_wind_vector_x_mps"] = parts[0]
        result["world_wind_vector_y_mps"] = parts[1]
        result["wind_world_linear_velocity_matches_requested"] = (
            abs(parts[0] - expected_x) <= 1e-6 and abs(parts[1] - expected_y) <= 1e-6
        )
    if x500_base_sdf_path.exists():
        x500_base_text = x500_base_sdf_path.read_text(encoding="utf-8")
        result["x500_base_sdf_sha256"] = hashlib.sha256(
            x500_base_sdf_path.read_bytes()
        ).hexdigest()
        result["wind_enabled_link_count"] = x500_base_text.count(
            "<enable_wind>true</enable_wind>"
        )
        result["wind_enabled_on_vehicle_links"] = result["wind_enabled_link_count"] > 0
    result["wind_effects_world_sdf_readback_observed"] = (
        result["wind_effects_plugin_materialized"] is True
        and result["wind_world_linear_velocity_matches_requested"] is True
        and result["wind_enabled_on_vehicle_links"] is True
    )
    return result


def _wind_runtime_gazebo_readback(payload_model_root: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "gazebo_runtime_world_model_readback_observed": False,
        "gazebo_runtime_world_path_observed": False,
        "gazebo_runtime_world_ready_observed": False,
        "gazebo_runtime_model_bridge_observed": False,
        "gazebo_runtime_vehicle_pose_observed": False,
        "source": "docker_logs_and_gz_pose_info",
    }
    if payload_model_root is None:
        result["source"] = "custom_world_not_used"
        return result

    expected_world_path = f"{PAYLOAD_MODEL_CONTAINER_PATH}/worlds/default.sdf"
    result["expected_runtime_world_path"] = expected_world_path
    logs = _logs("400")
    result["gazebo_runtime_world_path_observed"] = (
        f"Starting gazebo with world: {expected_world_path}" in logs
    )
    result["gazebo_runtime_world_ready_observed"] = "Gazebo world is ready" in logs
    result["gazebo_runtime_model_bridge_observed"] = (
        "gz_bridge] world: default, model: x500_0" in logs
    )
    pose_result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            "timeout 5 gz topic -e -t /world/default/pose/info -n 1",
        ],
        check=False,
        timeout=10,
    )
    result["gazebo_runtime_pose_topic_returncode"] = pose_result.returncode
    if pose_result.returncode == 0:
        try:
            pose = parse_gz_sim_entity_pose(pose_result.stdout, entity_name="x500_0")
            result["gazebo_runtime_vehicle_pose_observed"] = True
            result["gazebo_runtime_vehicle_pose"] = {
                key: float(pose[key]) for key in ("x", "y", "z")
            }
        except Exception as exc:
            result["gazebo_runtime_vehicle_pose_error"] = str(exc)[-200:]
    result["gazebo_runtime_world_model_readback_observed"] = (
        result["gazebo_runtime_world_path_observed"] is True
        and result["gazebo_runtime_world_ready_observed"] is True
        and result["gazebo_runtime_model_bridge_observed"] is True
        and result["gazebo_runtime_vehicle_pose_observed"] is True
    )
    return result


def _apply_wind_realism(payload_model_root: Path | None = None) -> dict[str, Any]:
    profile = _wind_requested_profile()
    requested = profile["requested"]
    requested_present = profile["requested_present"]
    wind_mean_capability_status = "not_requested"
    wind_gust_capability_status = "not_requested"
    wind_variance_capability_status = "not_requested"
    application_status = "not_requested"
    observation_status = "not_requested"
    unsupported_reasons: list[str] = []
    approximation_reasons: list[str] = []
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    if requested_present:
        mean = float(requested["wind_mean_mps"] or 0.0)
        direction = float(requested["wind_direction_deg"] or 0.0)
        gust = float(requested["wind_gust_mps"] or mean)
        variance = float(requested["wind_variance"] or 0.0)
        wind_x, wind_y = _wind_vector(mean_mps=mean, direction_deg=direction)
        if (
            requested["wind_gust_mps"] is not None
            or requested["wind_variance"] is not None
        ):
            approximation_reasons.append(
                "gazebo_wind_message_applies_constant_linear_velocity_only"
            )
        message = (
            f"enable_wind: true linear_velocity {{ x: {wind_x} y: {wind_y} z: 0 }}"
        )
        message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
        target_topic = "/world/default/wind"
        result = _run(
            [
                "docker",
                "exec",
                CONTAINER_NAME,
                "sh",
                "-lc",
                (
                    "command -v gz >/dev/null 2>&1 && "
                    "readback_file=$(mktemp) && "
                    "readback_err=$(mktemp) && "
                    f"gz topic -t {shlex.quote(target_topic)} "
                    f"-m gz.msgs.Wind -p {shlex.quote(message)}; "
                    "publish_status=$?; "
                    f"timeout 3 gz topic -e -t {shlex.quote(target_topic)} -n 1 "
                    '>"$readback_file" 2>"$readback_err" & '
                    "reader_pid=$! && "
                    "sleep 1 && "
                    "for _i in 1 2 3 4 5; do "
                    f"gz topic -t {shlex.quote(target_topic)} "
                    f"-m gz.msgs.Wind -p {shlex.quote(message)}; "
                    "candidate_status=$?; "
                    "if [ $candidate_status -eq 0 ]; then publish_status=0; fi; "
                    "sleep 0.2; "
                    "done; "
                    "wait $reader_pid; readback_status=$?; "
                    'cat "$readback_file"; '
                    'printf "\\n__BC_WIND_PUBLISH_STATUS=%s\\n" "$publish_status"; '
                    'printf "__BC_WIND_READBACK_STATUS=%s\\n" "$readback_status"; '
                    'cat "$readback_err" >&2; '
                    'rm -f "$readback_file" "$readback_err"; '
                    "test $publish_status -eq 0"
                ),
            ],
            check=False,
            timeout=10,
        )
        if result.returncode == 0:
            readback = _wind_readback_status(
                result.stdout,
                expected_x=wind_x,
                expected_y=wind_y,
            )
            physics_readback = _wind_physics_world_readback(
                payload_model_root,
                expected_x=wind_x,
                expected_y=wind_y,
            )
            runtime_readback = _wind_runtime_gazebo_readback(payload_model_root)
            terminal_physics_observed = bool(
                physics_readback["wind_effects_world_sdf_readback_observed"]
                and runtime_readback["gazebo_runtime_world_model_readback_observed"]
            )
            if not physics_readback["wind_effects_world_sdf_readback_observed"]:
                unsupported_reasons.append("gazebo_wind_terminal_physics_not_observed")
            if not runtime_readback["gazebo_runtime_world_model_readback_observed"]:
                unsupported_reasons.append(
                    "gazebo_wind_runtime_world_model_not_observed"
                )
            wind_mean_capability_status = (
                "supported" if terminal_physics_observed else "unsupported"
            )
            wind_gust_capability_status = (
                "unsupported"
                if not terminal_physics_observed
                and requested["wind_gust_mps"] is not None
                else (
                    "approximated"
                    if requested["wind_gust_mps"] is not None
                    else "not_requested"
                )
            )
            wind_variance_capability_status = (
                "unsupported"
                if not terminal_physics_observed
                and requested["wind_variance"] is not None
                else (
                    "approximated"
                    if requested["wind_variance"] is not None
                    else "not_requested"
                )
            )
            application_status = (
                "unsupported"
                if not terminal_physics_observed
                else (
                    "applied_with_approximations"
                    if approximation_reasons
                    else "applied"
                )
            )
            observation_status = (
                "applied_config_observed"
                if terminal_physics_observed
                else "unsupported"
            )
            applied = {
                "method": "gz_topic_wind_message",
                "target": target_topic,
                "topic": target_topic,
                "message_type": "gz.msgs.Wind",
                "terminal_physics_method": (
                    "gazebo_wind_effects_world_sdf"
                    if physics_readback["wind_effects_world_sdf_readback_observed"]
                    else "not_observed"
                ),
                "requested_mps": mean,
                "applied_mps": mean,
                "publish_attempt_count": 6,
                "requested_direction_deg": direction,
                "applied_direction_deg": direction,
                "applied_fields": [
                    "wind_mean_mps",
                    "wind_direction_deg",
                ],
                "approximated_fields": [
                    field
                    for field, value in (
                        ("wind_gust_mps", requested["wind_gust_mps"]),
                        ("wind_variance", requested["wind_variance"]),
                    )
                    if value is not None
                ],
                "wind_vector_x_mps": wind_x,
                "wind_vector_y_mps": wind_y,
                "gust_mps": gust,
                "variance": variance,
                "applied_message": message,
                "applied_message_sha256": message_sha256,
                "applied_file_path": physics_readback.get("world_sdf_path"),
                "applied_file_sha256": physics_readback.get("world_sdf_sha256"),
                "wind_effects_plugin_materialized": physics_readback[
                    "wind_effects_plugin_materialized"
                ],
                "wind_enabled_on_vehicle_links": physics_readback[
                    "wind_enabled_on_vehicle_links"
                ],
                "wind_enabled_link_count": physics_readback["wind_enabled_link_count"],
                "wind_world_linear_velocity_matches_requested": physics_readback[
                    "wind_world_linear_velocity_matches_requested"
                ],
                "gazebo_runtime_world_model_readback_observed": runtime_readback[
                    "gazebo_runtime_world_model_readback_observed"
                ],
                "gazebo_runtime_world_path_observed": runtime_readback[
                    "gazebo_runtime_world_path_observed"
                ],
                "gazebo_runtime_world_ready_observed": runtime_readback[
                    "gazebo_runtime_world_ready_observed"
                ],
                "gazebo_runtime_model_bridge_observed": runtime_readback[
                    "gazebo_runtime_model_bridge_observed"
                ],
                "gazebo_runtime_vehicle_pose_observed": runtime_readback[
                    "gazebo_runtime_vehicle_pose_observed"
                ],
                "gazebo_runtime_source": runtime_readback["source"],
                "px4_param_snapshot_ref": None,
                "source": "mission_designer_coordinate_route_env",
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }
            observed = {
                "source": (
                    "gz_topic_echo_readback"
                    if readback["readback_observed"]
                    else "gz_topic_publish_returncode"
                ),
                "observed": terminal_physics_observed,
                "returncode": result.returncode,
                "wind_topic_readback_observed": readback["readback_observed"],
                "wind_topic_readback_status": readback["readback_status"],
                "wind_topic_readback_publish_status": readback[
                    "readback_publish_status"
                ],
                "wind_topic_publish_attempt_count": 6,
                "wind_mean_mps": mean,
                "wind_direction_deg": direction,
                "wind_vector_x_mps": wind_x,
                "wind_vector_y_mps": wind_y,
                "readback_wind_vector_x_mps": readback["readback_wind_vector_x_mps"],
                "readback_wind_vector_y_mps": readback["readback_wind_vector_y_mps"],
                "target_topic": target_topic,
                "message_type": "gz.msgs.Wind",
                "applied_message_sha256": message_sha256,
                "readback_message_sha256": readback["readback_message_sha256"],
                "wind_effects_world_sdf_readback_observed": physics_readback[
                    "wind_effects_world_sdf_readback_observed"
                ],
                "wind_effects_plugin_materialized": physics_readback[
                    "wind_effects_plugin_materialized"
                ],
                "wind_world_linear_velocity_matches_requested": physics_readback[
                    "wind_world_linear_velocity_matches_requested"
                ],
                "wind_enabled_on_vehicle_links": physics_readback[
                    "wind_enabled_on_vehicle_links"
                ],
                "wind_enabled_link_count": physics_readback["wind_enabled_link_count"],
                "world_sdf_sha256": physics_readback.get("world_sdf_sha256"),
                "x500_base_sdf_sha256": physics_readback.get("x500_base_sdf_sha256"),
                "gazebo_runtime_world_model_readback_observed": runtime_readback[
                    "gazebo_runtime_world_model_readback_observed"
                ],
                "gazebo_runtime_world_path_observed": runtime_readback[
                    "gazebo_runtime_world_path_observed"
                ],
                "gazebo_runtime_world_ready_observed": runtime_readback[
                    "gazebo_runtime_world_ready_observed"
                ],
                "gazebo_runtime_model_bridge_observed": runtime_readback[
                    "gazebo_runtime_model_bridge_observed"
                ],
                "gazebo_runtime_vehicle_pose_observed": runtime_readback[
                    "gazebo_runtime_vehicle_pose_observed"
                ],
                "gazebo_runtime_vehicle_pose": runtime_readback.get(
                    "gazebo_runtime_vehicle_pose"
                ),
                "gazebo_runtime_expected_world_path": runtime_readback.get(
                    "expected_runtime_world_path"
                ),
                "gazebo_runtime_source": runtime_readback["source"],
            }
        else:
            wind_mean_capability_status = "unsupported"
            wind_gust_capability_status = "unsupported"
            wind_variance_capability_status = "unsupported"
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("gazebo_wind_topic_publish_failed")
            observed = {
                "source": "gz_topic_publish_returncode",
                "observed": False,
                "returncode": result.returncode,
                "target_topic": target_topic,
                "message_type": "gz.msgs.Wind",
                "attempted_message_sha256": message_sha256,
                "stdout_tail": result.stdout[-500:],
                "stderr_tail": result.stderr[-500:],
            }
    capability = {
        "schema_version": "simulator_capability_matrix.v1",
        "capability_id": "simulator_capability_matrix:mission_designer_wind_gust",
        "wind_mean": wind_mean_capability_status,
        "wind_gust": wind_gust_capability_status,
        "wind_variance": wind_variance_capability_status,
        "support_detection_method": (
            "gz_topic_wind_effects_config_and_runtime_world_model_readback"
            if requested_present
            else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "simulator_condition_application.v1",
        "application_id": "simulator_condition_application:mission_designer_wind_gust",
        "condition_kind": "wind_gust",
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_environment_evidence.v1",
        "evidence_id": "observed_environment_evidence:mission_designer_wind_gust",
        "condition_kind": "wind_gust",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    return {
        "environment_condition_profile": profile,
        "simulator_capability_matrix": capability,
        "simulator_condition_application": application,
        "observed_environment_evidence": evidence,
    }


def _wind_realism_summary_artifacts(*, cleanup_status: str) -> dict[str, Any]:
    return {
        "environment_condition_profile": (WIND_REALISM_SUMMARY or {}).get(
            "environment_condition_profile", {}
        ),
        "simulator_capability_matrix": (WIND_REALISM_SUMMARY or {}).get(
            "simulator_capability_matrix", {}
        ),
        "simulator_condition_application": (WIND_REALISM_SUMMARY or {}).get(
            "simulator_condition_application", {}
        ),
        "observed_environment_evidence": (WIND_REALISM_SUMMARY or {}).get(
            "observed_environment_evidence", {}
        ),
        "scenario_cleanup_receipt": {
            "schema_version": "scenario_cleanup_receipt.v1",
            "cleanup_id": "scenario_cleanup_receipt:horizontal_route_isolated_container",
            "cleanup_scope": "isolated_px4_gazebo_container",
            "cleanup_status": cleanup_status,
            "container_name": CONTAINER_NAME,
            "condition_refs": [
                "environment_condition_profile:mission_designer_wind_gust",
                "thermal_weather_condition_profile:mission_designer_temperature",
                "vehicle_condition_profile:mission_designer_payload_mass",
                "battery_condition_profile:mission_designer_battery_threshold",
                "sensor_condition_profile:mission_designer_sensor_failure",
                "gazebo_world_condition_profile:mission_designer_landing_zone_blocked",
                "visibility_condition_profile:mission_designer_visibility",
                "operational_condition_profile:mission_designer_operational_markers",
                "traffic_conflict_profile:mission_designer_visual_marker",
                "alternate_landing_profile:mission_designer_visual_marker",
                "dynamic_actor_profile:mission_designer_moving_visual_marker",
                "telemetry_degradation_profile:mission_designer_observer_dropout",
                "mavlink_link_degradation_profile:mission_designer_link_probe",
            ],
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _mark_cleanup_observed(run_dir: Path) -> None:
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        return
    summary = json.loads(summary_path.read_text())
    cleanup = dict(summary.get("scenario_cleanup_receipt") or {})
    if not cleanup:
        return
    cleanup["cleanup_status"] = "isolated_container_teardown_observed"
    cleanup["observed_at"] = datetime.now(timezone.utc).isoformat()
    summary["scenario_cleanup_receipt"] = cleanup
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


def _vehicle_payload_mass_realism(
    *,
    payload_model_root: Path | None,
    payload_release_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_mass = _payload_mass_request()
    requested_present = requested_mass is not None
    condition = {
        "schema_version": "vehicle_condition_profile.v1",
        "condition_id": "vehicle_condition_profile:mission_designer_payload_mass",
        "condition_kind": "payload_mass",
        "requested": {
            "payload_mass_kg": requested_mass,
            "payload_mounted": _payload_model_enabled(),
            "release_mechanism": "gazebo_detachable_joint",
        },
        "requested_present": requested_present,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    unsupported_reasons: list[str] = []
    applied: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    observed: dict[str, Any] = {}
    if requested_present:
        world_path = (
            None
            if payload_model_root is None
            else payload_model_root / "worlds" / "default.sdf"
        )
        if world_path is not None and world_path.exists():
            world_text = world_path.read_text(encoding="utf-8")
            applied_mass = None
            for model in ET.fromstring(world_text).iter("model"):
                if model.attrib.get("name") != "delivery_payload":
                    continue
                mass = model.find("./link/inertial/mass")
                if mass is not None and mass.text is not None:
                    applied_mass = float(mass.text)
                break
            world_sha256 = hashlib.sha256(world_text.encode("utf-8")).hexdigest()
            if applied_mass is not None and math.isclose(
                applied_mass,
                float(requested_mass),
                rel_tol=0.0,
                abs_tol=0.000001,
            ):
                application_status = "applied"
                observation_status = "model_sdf_observed"
                world_sdf_hash_match = True
                applied = {
                    "method": "payload_model_sdf_mass",
                    "world_sdf_path": str(world_path),
                    "payload_model": "delivery_payload",
                    "payload_link": "payload_link",
                    "payload_mass_kg": applied_mass,
                    "world_sdf_sha256": world_sha256,
                    "world_sdf_hash_match": world_sdf_hash_match,
                    "model_materialized": True,
                    "payload_mass_materialized": True,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                }
                observed = {
                    "source": "payload_model_world_sdf",
                    "observed": True,
                    "payload_mass_kg": applied_mass,
                    "requested_payload_mass_kg": float(requested_mass),
                    "world_sdf_hash_match": world_sdf_hash_match,
                    "model_materialized": True,
                    "payload_mass_materialized": True,
                    "world_sdf_sha256": world_sha256,
                }
                if (
                    payload_release_summary
                    and payload_release_summary.get("payload_release_observed") is True
                ):
                    observation_status = "model_sdf_and_payload_release_observed"
                    observed["payload_release_observed"] = True
                    observed["payload_release_event_source"] = (
                        payload_release_summary.get("payload_release_event_source")
                    )
                    observed["payload_release_observed_at"] = (
                        payload_release_summary.get("payload_release_observed_at")
                    )
            else:
                application_status = "unsupported"
                observation_status = "unsupported"
                unsupported_reasons.append("payload_mass_not_materialized_in_world_sdf")
                observed = {
                    "source": "payload_model_world_sdf",
                    "observed": False,
                    "requested_payload_mass_kg": float(requested_mass),
                    "payload_mass_kg": applied_mass,
                    "world_sdf_hash_match": False,
                    "model_materialized": applied_mass is not None,
                    "payload_mass_materialized": False,
                    "world_sdf_sha256": world_sha256,
                }
        else:
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("payload_model_world_sdf_missing")
    capability_status = (
        "supported"
        if application_status == "applied"
        else "unsupported" if unsupported_reasons else "not_requested"
    )
    capability = {
        "schema_version": "simulator_capability_matrix.v1",
        "capability_id": "simulator_capability_matrix:mission_designer_payload_mass",
        "payload_mass": capability_status,
        "support_detection_method": (
            "payload_model_world_sdf" if requested_present else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
    }
    application = {
        "schema_version": "simulator_condition_application.v1",
        "application_id": "simulator_condition_application:mission_designer_payload_mass",
        "condition_kind": "payload_mass",
        "application_status": application_status,
        "requested_condition_ref": condition["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_vehicle_condition_evidence.v1",
        "evidence_id": "observed_vehicle_condition_evidence:mission_designer_payload_mass",
        "condition_kind": "payload_mass",
        "observation_status": observation_status,
        "requested_condition_ref": condition["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "payload_release_does_not_verify_dropoff": True,
        "delivery_completion_claimed": False,
    }
    return {
        "vehicle_condition_profile": condition,
        "payload_simulator_capability_matrix": capability,
        "payload_simulator_condition_application": application,
        "observed_vehicle_condition_evidence": evidence,
    }


def _battery_requested_profile() -> dict[str, Any]:
    scenario = (os.getenv(BATTERY_SCENARIO_ENV) or "").strip().lower()
    requested_remaining = _optional_float_env(BATTERY_REMAINING_PERCENT_ENV)
    if requested_remaining is not None and (
        requested_remaining < 0.0 or requested_remaining > 100.0
    ):
        requested_remaining = None
    if not scenario and requested_remaining is not None:
        scenario = "battery_critical" if requested_remaining <= 10.0 else "battery_low"
    if scenario not in ("", "battery_low", "battery_critical"):
        scenario = "unsupported"
    return {
        "schema_version": "battery_condition_profile.v1",
        "condition_id": "battery_condition_profile:mission_designer_battery_threshold",
        "condition_kind": "battery_threshold",
        "requested": {
            "battery_scenario": scenario or None,
            "requested_remaining_percent": requested_remaining,
            "requested_warning_level": (
                2
                if scenario == "battery_critical"
                else 1 if scenario == "battery_low" else None
            ),
        },
        "requested_present": bool(scenario or requested_remaining is not None),
        "source": "mission_designer_coordinate_route",
        "requested_remaining_does_not_spoof_px4_battery_status": True,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def _px4_param_show(param_name: str) -> dict[str, Any]:
    result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            f"/opt/px4-gazebo/bin/px4-param show {param_name}",
        ],
        check=False,
        timeout=5,
    )
    output = (result.stdout + result.stderr).strip()
    value = _listener_field(output, param_name)
    if value is None:
        match = re.search(
            rf"\b{re.escape(param_name)}(?:\s+\[[^\]]+\])?\s*:\s*(-?\d+(?:\.\d+)?)",
            output,
        )
        value = float(match.group(1)) if match else None
    if value is None:
        value = _listener_field(output, "value")
    return {
        "param": param_name,
        "returncode": result.returncode,
        "value": value,
        "output_tail": output[-500:],
    }


def _px4_param_set(param_name: str, value: float) -> dict[str, Any]:
    result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            f"/opt/px4-gazebo/bin/px4-param set {param_name} {value:.6f}",
        ],
        check=False,
        timeout=5,
    )
    return {
        "param": param_name,
        "requested_value": value,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-500:],
        "stderr_tail": result.stderr[-500:],
    }


def _px4_param_set_applied(result: Mapping[str, Any]) -> bool:
    output = (
        f"{result.get('stdout_tail') or ''}\n{result.get('stderr_tail') or ''}".lower()
    )
    return result.get("returncode") == 0 and "not found" not in output


def _px4_param_value_matches(
    snapshot: Mapping[str, Any],
    expected_value: float,
    *,
    abs_tol: float = 1e-4,
) -> bool:
    if snapshot.get("returncode") != 0:
        return False
    value = snapshot.get("value")
    return value is not None and math.isclose(
        float(value),
        float(expected_value),
        abs_tol=abs_tol,
    )


def _thermal_weather_realism() -> dict[str, Any]:
    profile = _thermal_weather_requested_profile()
    requested = profile["requested"]
    requested_present = profile["requested_present"]
    unsupported_reasons: list[str] = []
    approximation_reasons: list[str] = []
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    thermal_capability_status = "not_requested"
    battery_drain_status = "not_requested"
    motor_derate_status = "not_requested"
    if requested_present:
        temperature_c = requested.get("temperature_c")
        pressure_hpa = requested.get("pressure_hpa")
        explicit_battery_factor = requested.get("thermal_battery_drain_factor")
        explicit_motor_factor = requested.get("thermal_motor_derate_factor")
        thermal_effect_requested = any(
            value is not None
            for value in (
                temperature_c,
                explicit_battery_factor,
                explicit_motor_factor,
            )
        )
        if pressure_hpa is not None:
            approximation_reasons.append(
                "pressure_hpa_recorded_for_context_not_air_physics"
            )
        if not thermal_effect_requested:
            application_status = "unsupported"
            observation_status = "unsupported"
            thermal_capability_status = "unsupported"
            unsupported_reasons.append(
                "thermal_battery_or_motor_condition_not_requested"
            )
            if pressure_hpa is not None:
                unsupported_reasons.append(
                    "pressure_physics_not_supported_by_bounded_sitl_model"
                )
            capability = {
                "schema_version": "simulator_capability_matrix.v1",
                "capability_id": (
                    "simulator_capability_matrix:mission_designer_thermal_weather"
                ),
                "thermal_weather": thermal_capability_status,
                "battery_drain_temperature_effect": battery_drain_status,
                "motor_derate_temperature_effect": motor_derate_status,
                "air_temperature_physics": "not_claimed",
                "pressure_physics": "not_claimed",
                "support_detection_method": (
                    "px4_param_set_readback_and_battery_status_listener"
                ),
                "unsupported_reasons": unsupported_reasons,
                "approximation_reasons": approximation_reasons,
            }
            application = {
                "schema_version": "simulator_condition_application.v1",
                "application_id": (
                    "simulator_condition_application:mission_designer_thermal_weather"
                ),
                "condition_kind": "thermal_weather",
                "application_status": application_status,
                "requested_condition_ref": profile["condition_id"],
                "applied": applied,
                "unsupported_reasons": unsupported_reasons,
                "approximation_reasons": approximation_reasons,
                "simulator_only": True,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            }
            evidence = {
                "schema_version": "observed_environment_evidence.v1",
                "evidence_id": (
                    "observed_environment_evidence:mission_designer_thermal_weather"
                ),
                "condition_kind": "thermal_weather",
                "observation_status": observation_status,
                "requested_condition_ref": profile["condition_id"],
                "application_ref": application["application_id"],
                "observed": observed,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "delivery_completion_claimed": False,
            }
            return {
                "thermal_weather_condition_profile": profile,
                "thermal_weather_simulator_capability_matrix": capability,
                "thermal_weather_simulator_condition_application": application,
                "observed_thermal_weather_evidence": evidence,
            }
        battery_factor = (
            float(explicit_battery_factor)
            if explicit_battery_factor is not None
            else _thermal_battery_drain_factor_from_temperature(temperature_c)
        )
        motor_factor = (
            float(explicit_motor_factor)
            if explicit_motor_factor is not None
            else _thermal_motor_derate_factor_from_temperature(temperature_c)
        )
        if battery_factor is None:
            battery_factor = 1.0
        if motor_factor is None:
            motor_factor = 1.0
        if explicit_battery_factor is None and temperature_c is not None:
            approximation_reasons.append(
                "temperature_to_battery_drain_factor_uses_bounded_sitl_model"
            )
        if explicit_motor_factor is None and temperature_c is not None:
            approximation_reasons.append(
                "temperature_to_motor_derate_factor_uses_bounded_sitl_model"
            )
        before_params = {
            "SIM_BAT_MIN_PCT": _px4_param_show("SIM_BAT_MIN_PCT"),
            "SIM_BAT_DRAIN": _px4_param_show("SIM_BAT_DRAIN"),
            "MPC_THR_MAX": _px4_param_show("MPC_THR_MAX"),
        }
        before_drain = before_params["SIM_BAT_DRAIN"].get("value")
        try:
            before_drain_seconds = float(before_drain)
        except (TypeError, ValueError):
            before_drain_seconds = 1800.0
        effective_drain_seconds = max(
            60.0,
            round(before_drain_seconds / max(float(battery_factor), 0.1), 3),
        )
        effective_motor_derate = max(0.1, min(1.0, float(motor_factor)))
        set_results = [
            _px4_param_set("SIM_BAT_MIN_PCT", 5.0),
            _px4_param_set("SIM_BAT_DRAIN", effective_drain_seconds),
        ]
        if effective_motor_derate < 0.999:
            set_results.append(_px4_param_set("MPC_THR_MAX", effective_motor_derate))
        applied_params = {
            item["param"]: item["requested_value"] for item in set_results
        }
        after_params = {
            "SIM_BAT_MIN_PCT": _px4_param_show("SIM_BAT_MIN_PCT"),
            "SIM_BAT_DRAIN": _px4_param_show("SIM_BAT_DRAIN"),
            "MPC_THR_MAX": _px4_param_show("MPC_THR_MAX"),
        }
        param_readback = {
            name: _px4_param_value_matches(after_params.get(name) or {}, value)
            for name, value in applied_params.items()
        }
        params_set = all(_px4_param_set_applied(item) for item in set_results)
        params_read_back = bool(param_readback) and all(param_readback.values())
        _reset_battery_status_cache()
        time.sleep(1)
        battery_sample = _battery_status_sample()
        if params_set and params_read_back:
            application_status = "applied_with_approximations"
            observation_status = "thermal_condition_param_readback_observed"
            thermal_capability_status = "supported"
            battery_drain_status = "supported"
            motor_derate_status = (
                "supported" if effective_motor_derate < 0.999 else "not_materialized"
            )
        else:
            application_status = "unsupported"
            observation_status = "unsupported"
            thermal_capability_status = "unsupported"
            battery_drain_status = "unsupported"
            motor_derate_status = (
                "unsupported" if effective_motor_derate < 0.999 else "not_materialized"
            )
            if not params_set:
                unsupported_reasons.append("px4_thermal_param_set_failed")
            if not params_read_back:
                unsupported_reasons.append("px4_thermal_param_readback_mismatch")
        applied = {
            "method": "px4_runtime_param_thermal_battery_motor_model",
            "target": "px4_runtime_params",
            "temperature_c": temperature_c,
            "pressure_hpa": pressure_hpa,
            "thermal_battery_drain_factor": battery_factor,
            "thermal_motor_derate_factor": effective_motor_derate,
            "baseline_sim_bat_drain_seconds": before_drain_seconds,
            "effective_sim_bat_drain_seconds": effective_drain_seconds,
            "applied_params": applied_params,
            "before_params": before_params,
            "after_params": after_params,
            "param_readback_matches_requested": param_readback,
            "thermal_air_physics_claimed": False,
            "motor_derate_param_materialized": effective_motor_derate < 0.999,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }
        observed = {
            "source": "px4-param-readback-and-battery-status-listener",
            "observed": params_set and params_read_back,
            "temperature_c": temperature_c,
            "pressure_hpa": pressure_hpa,
            "thermal_battery_drain_factor": battery_factor,
            "thermal_motor_derate_factor": effective_motor_derate,
            "thermal_air_physics_claimed": False,
            "battery_status": battery_sample,
            "battery_status_observed": battery_sample.get("battery_status_observed")
            is True,
            "param_readback_matches_requested": param_readback,
        }
    capability = {
        "schema_version": "simulator_capability_matrix.v1",
        "capability_id": "simulator_capability_matrix:mission_designer_thermal_weather",
        "thermal_weather": thermal_capability_status,
        "battery_drain_temperature_effect": battery_drain_status,
        "motor_derate_temperature_effect": motor_derate_status,
        "air_temperature_physics": "not_claimed",
        "pressure_physics": "not_claimed",
        "support_detection_method": (
            "px4_param_set_readback_and_battery_status_listener"
            if requested_present
            else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "simulator_condition_application.v1",
        "application_id": "simulator_condition_application:mission_designer_thermal_weather",
        "condition_kind": "thermal_weather",
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_environment_evidence.v1",
        "evidence_id": "observed_environment_evidence:mission_designer_thermal_weather",
        "condition_kind": "thermal_weather",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    return {
        "thermal_weather_condition_profile": profile,
        "thermal_weather_simulator_capability_matrix": capability,
        "thermal_weather_simulator_condition_application": application,
        "observed_thermal_weather_evidence": evidence,
    }


def _battery_realism() -> dict[str, Any]:
    profile = _battery_requested_profile()
    requested = profile["requested"]
    requested_present = profile["requested_present"]
    scenario = requested.get("battery_scenario")
    unsupported_reasons: list[str] = []
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    capability_status = "not_requested"
    before_params: dict[str, Any] = {}
    set_results: list[dict[str, Any]] = []
    if requested_present:
        if scenario not in ("battery_low", "battery_critical"):
            unsupported_reasons.append("battery_scenario_unsupported")
            application_status = "unsupported"
            observation_status = "unsupported"
            capability_status = "unsupported"
        else:
            _reset_battery_status_cache()
            before_sample = _battery_status_sample()
            observed_remaining_percent = before_sample.get("battery_remaining_percent")
            requested_remaining = requested.get("requested_remaining_percent")
            threshold_percent = (
                float(observed_remaining_percent) + 5.0
                if observed_remaining_percent is not None
                else float(requested_remaining or 20.0) + 35.0
            )
            threshold = max(0.01, min(0.99, threshold_percent / 100.0))
            before_params = {
                "BAT_LOW_THR": _px4_param_show("BAT_LOW_THR"),
                "BAT_CRIT_THR": _px4_param_show("BAT_CRIT_THR"),
            }
            set_results.append(_px4_param_set("BAT_LOW_THR", threshold))
            if scenario == "battery_critical":
                set_results.append(_px4_param_set("BAT_CRIT_THR", threshold))
            applied_params = {
                item["param"]: item["requested_value"] for item in set_results
            }
            after_params = {
                "BAT_LOW_THR": _px4_param_show("BAT_LOW_THR"),
                "BAT_CRIT_THR": _px4_param_show("BAT_CRIT_THR"),
            }
            param_readback = {
                name: _px4_param_value_matches(after_params.get(name) or {}, value)
                for name, value in applied_params.items()
            }
            params_set = all(_px4_param_set_applied(item) for item in set_results)
            params_read_back = bool(param_readback) and all(param_readback.values())
            if params_set and params_read_back:
                application_status = "applied_with_approximations"
                capability_status = "supported"
                applied = {
                    "method": "px4_runtime_param_threshold_override",
                    "target": "px4_runtime_params",
                    "applied_params": applied_params,
                    "before_params": before_params,
                    "after_params": after_params,
                    "param_readback_matches_requested": param_readback,
                    "requested_remaining_percent": requested_remaining,
                    "requested_remaining_does_not_spoof_px4_battery_status": True,
                    "battery_remaining_target_materialized": False,
                    "battery_remaining_target_commitment": (
                        "not_materialized_as_px4_battery_status_remaining"
                    ),
                    "battery_warning_threshold_materialized": True,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                }
            else:
                application_status = "unsupported"
                capability_status = "unsupported"
                observation_status = "unsupported"
                if not params_set:
                    unsupported_reasons.append("px4_battery_param_set_failed")
                if not params_read_back:
                    unsupported_reasons.append("px4_battery_param_readback_mismatch")
            _reset_battery_status_cache()
            time.sleep(1)
            after_sample = _battery_status_sample()
            observed_remaining = after_sample.get("battery_remaining_percent")
            observed_remaining_matches_requested = (
                requested_remaining is not None
                and observed_remaining is not None
                and math.isclose(
                    float(observed_remaining),
                    float(requested_remaining),
                    abs_tol=0.5,
                )
            )
            observed = {
                "source": "px4-listener:battery_status",
                "observed": after_sample.get("battery_status_observed") is True,
                "battery_status": after_sample,
                "requested_remaining_percent": requested_remaining,
                "observed_remaining_percent": observed_remaining,
                "observed_remaining_matches_requested": observed_remaining_matches_requested,
                "observed_warning": after_sample.get("battery_warning"),
                "requested_remaining_does_not_spoof_px4_battery_status": True,
                "battery_remaining_target_materialized": False,
                "battery_remaining_target_commitment": (
                    "not_materialized_as_px4_battery_status_remaining"
                ),
                "battery_warning_threshold_materialized": _application_status_is_materialized(
                    application_status
                ),
            }
            expected_warning = requested.get("requested_warning_level")
            observed["failsafe_behavior_status"] = (
                "not_requested_for_battery_low_warning"
                if scenario == "battery_low"
                else "unsupported_without_dedicated_critical_battery_recovery_smoke"
            )
            if (
                _application_status_is_materialized(application_status)
                and after_sample.get("battery_status_observed") is True
            ):
                observation_status = "battery_status_observed"
                if expected_warning is not None and (
                    after_sample.get("battery_warning") is None
                    or int(after_sample.get("battery_warning") or 0)
                    < int(expected_warning)
                ):
                    observation_status = "battery_status_observed_warning_not_reached"
                    unsupported_reasons.append(
                        "px4_battery_warning_threshold_not_observed"
                    )
            elif _application_status_is_materialized(application_status):
                observation_status = "battery_status_not_observed"
                unsupported_reasons.append("px4_battery_status_not_observed")
    capability = {
        "schema_version": "simulator_capability_matrix.v1",
        "capability_id": "simulator_capability_matrix:mission_designer_battery_threshold",
        "battery_threshold": capability_status,
        "battery_failsafe_behavior": (
            "not_requested"
            if scenario == "battery_low"
            else "unsupported" if scenario == "battery_critical" else capability_status
        ),
        "support_detection_method": (
            "px4_param_set_and_battery_status_listener"
            if requested_present
            else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": (
            [
                "requested_remaining_percent_is_warning_threshold_input_not_px4_remaining_target"
            ]
            if requested_present and scenario in ("battery_low", "battery_critical")
            else []
        ),
    }
    application = {
        "schema_version": "simulator_condition_application.v1",
        "application_id": "simulator_condition_application:mission_designer_battery_threshold",
        "condition_kind": "battery_threshold",
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": capability["approximation_reasons"],
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_vehicle_condition_evidence.v1",
        "evidence_id": "observed_vehicle_condition_evidence:mission_designer_battery_threshold",
        "condition_kind": "battery_threshold",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "requested_remaining_does_not_spoof_px4_battery_status": True,
        "delivery_completion_claimed": False,
    }
    return {
        "battery_condition_profile": profile,
        "battery_simulator_capability_matrix": capability,
        "battery_simulator_condition_application": application,
        "observed_battery_condition_evidence": evidence,
    }


def _latest_trace_battery_status() -> dict[str, Any] | None:
    if LIVE_POSE_TRACE_PATH is None or not LIVE_POSE_TRACE_PATH.exists():
        return None
    rows = LIVE_POSE_TRACE_PATH.read_text().splitlines()
    for row in reversed(rows):
        try:
            payload = json.loads(row)
        except json.JSONDecodeError:
            continue
        battery_status = payload.get("battery_status")
        if (
            isinstance(battery_status, dict)
            and battery_status.get("battery_status_observed") is True
        ):
            return battery_status
    return None


def _refresh_battery_realism_observation_from_trace() -> None:
    if not BATTERY_REALISM_SUMMARY:
        return
    profile = BATTERY_REALISM_SUMMARY.get("battery_condition_profile") or {}
    requested = profile.get("requested") or {}
    if profile.get("requested_present") is not True:
        return
    latest = _latest_trace_battery_status()
    if not latest:
        return
    application = (
        BATTERY_REALISM_SUMMARY.get("battery_simulator_condition_application") or {}
    )
    application_status = application.get("application_status")
    expected_warning = requested.get("requested_warning_level")
    observed_warning = latest.get("battery_warning")
    warning_reached = _application_status_is_materialized(application_status) and (
        expected_warning is None
        or (
            observed_warning is not None
            and int(observed_warning) >= int(expected_warning)
        )
    )
    evidence = BATTERY_REALISM_SUMMARY.get("observed_battery_condition_evidence") or {}
    observed = dict(evidence.get("observed") or {})
    observed.update(
        {
            "source": "px4-listener:battery_status",
            "observed": True,
            "battery_status": latest,
            "requested_remaining_percent": requested.get("requested_remaining_percent"),
            "observed_remaining_percent": latest.get("battery_remaining_percent"),
            "observed_remaining_matches_requested": (
                requested.get("requested_remaining_percent") is not None
                and latest.get("battery_remaining_percent") is not None
                and math.isclose(
                    float(latest.get("battery_remaining_percent")),
                    float(requested.get("requested_remaining_percent")),
                    abs_tol=0.5,
                )
            ),
            "observed_warning": observed_warning,
            "requested_remaining_does_not_spoof_px4_battery_status": True,
            "battery_remaining_target_materialized": False,
            "battery_remaining_target_commitment": (
                "not_materialized_as_px4_battery_status_remaining"
            ),
            "battery_warning_threshold_materialized": _application_status_is_materialized(
                application_status
            ),
            "failsafe_behavior_status": (
                "not_requested_for_battery_low_warning"
                if requested.get("battery_scenario") == "battery_low"
                else "unsupported_without_dedicated_critical_battery_recovery_smoke"
            ),
        }
    )
    evidence["observed"] = observed
    if _application_status_is_materialized(application_status):
        evidence["observation_status"] = (
            "battery_status_observed"
            if warning_reached
            else "battery_status_observed_warning_not_reached"
        )
    else:
        evidence["observation_status"] = (
            evidence.get("observation_status") or "unsupported"
        )
    evidence["observed_at"] = datetime.now(timezone.utc).isoformat()
    BATTERY_REALISM_SUMMARY["observed_battery_condition_evidence"] = evidence
    if warning_reached:
        for key in (
            "battery_simulator_capability_matrix",
            "battery_simulator_condition_application",
        ):
            record = dict(BATTERY_REALISM_SUMMARY.get(key) or {})
            record["unsupported_reasons"] = [
                reason
                for reason in record.get("unsupported_reasons", [])
                if reason != "px4_battery_warning_threshold_not_observed"
            ]
            BATTERY_REALISM_SUMMARY[key] = record


def _sensor_failure_requested_profile() -> dict[str, Any]:
    component = (os.getenv(SENSOR_FAILURE_COMPONENT_ENV) or "").strip().lower()
    failure_type = (os.getenv(SENSOR_FAILURE_TYPE_ENV) or "").strip().lower()
    if not component and failure_type:
        component = "gps"
    requested_present = bool(component or failure_type)
    supported_components = {"gps"}
    supported_failure_types = {"off"}
    validation_reasons: list[str] = []
    if component and component not in supported_components:
        validation_reasons.append("sensor_component_not_in_this_vertical_slice")
    if failure_type and failure_type not in supported_failure_types:
        validation_reasons.append("sensor_failure_type_not_in_this_vertical_slice")
    return {
        "schema_version": "sensor_condition_profile.v1",
        "condition_id": "sensor_condition_profile:mission_designer_sensor_failure",
        "condition_kind": "sensor_failure",
        "requested": {
            "sensor_component": component or None,
            "failure_type": failure_type or None,
            "reset_failure_type": "ok" if component else None,
        },
        "requested_present": requested_present,
        "validation_reasons": validation_reasons,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }


def _sensor_gps_sample(*, timeout_seconds: int = 2) -> dict[str, Any]:
    result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            f"timeout {timeout_seconds} /opt/px4-gazebo/bin/px4-listener sensor_gps 1",
        ],
        check=False,
        timeout=timeout_seconds + 2,
    )
    output = (result.stdout + result.stderr).strip()
    observed = result.returncode == 0 and bool(output)
    return {
        "sensor_gps_observed": observed,
        "source": "px4-listener:sensor_gps",
        "returncode": result.returncode,
        "timestamp": _listener_field(output, "timestamp"),
        "satellites_used": _listener_field(output, "satellites_used"),
        "fix_type": _listener_field(output, "fix_type"),
        "output_tail": output[-500:],
    }


def _px4_failure_injection_command(
    component: str,
    failure_type: str,
) -> dict[str, Any]:
    result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            f"/opt/px4-gazebo/bin/px4-failure {component} {failure_type}",
        ],
        check=False,
        timeout=5,
    )
    combined = (result.stdout + result.stderr).strip()
    return {
        "component": component,
        "failure_type": failure_type,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-500:],
        "stderr_tail": result.stderr[-500:],
        "unsupported_message_observed": "unsupported" in combined.lower(),
    }


def _sensor_failure_realism() -> dict[str, Any]:
    profile = _sensor_failure_requested_profile()
    requested = profile["requested"]
    requested_present = profile["requested_present"]
    validation_reasons = list(profile.get("validation_reasons") or [])
    unsupported_reasons: list[str] = list(validation_reasons)
    component = requested.get("sensor_component")
    failure_type = requested.get("failure_type")
    capability_status = "not_requested"
    application_status = "not_requested"
    observation_status = "not_requested"
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    reset: dict[str, Any] = {}
    if requested_present and not validation_reasons and component and failure_type:
        before_sample = _sensor_gps_sample()
        block_result = _px4_param_set("SIM_GZ_EN_GPS", 0.0)
        applied = {
            "method": "px4_sim_gz_en_gps_param",
            "block_param_result": block_result,
            "component": component,
            "failure_type": failure_type,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }
        block_param_applied = _px4_param_set_applied(block_result)
        if block_param_applied:
            time.sleep(1)
            after_sample = _sensor_gps_sample()
            baseline_observed = before_sample["sensor_gps_observed"] is True
            gps_stopped = (
                baseline_observed
                and failure_type == "off"
                and after_sample["sensor_gps_observed"] is False
            )
            capability_status = "supported" if gps_stopped else "unsupported"
            application_status = "applied" if gps_stopped else "unsupported"
            observation_status = (
                "sensor_failure_effect_observed"
                if gps_stopped
                else "sensor_failure_command_observed_effect_unconfirmed"
            )
            if not baseline_observed:
                unsupported_reasons.append("sensor_gps_baseline_not_observed")
                observation_status = "sensor_failure_baseline_not_observed"
            elif not gps_stopped:
                unsupported_reasons.append("sensor_failure_effect_not_observed")
            observed = {
                "source": "px4-listener:sensor_gps",
                "requested_sensor_component": component,
                "requested_failure_type": failure_type,
                "before_sensor_sample": before_sample,
                "after_sensor_sample": after_sample,
                "block_param_observed": block_param_applied,
                "baseline_sensor_gps_observed": baseline_observed,
                "sensor_failure_effect_observed": gps_stopped,
                "gps_sample_lost_after_injection": gps_stopped,
                "estimator_degradation_observed": False,
                "sensor_failure_does_not_verify_failsafe": True,
            }
        else:
            capability_status = "unsupported"
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("sim_gz_en_gps_param_failed_or_unsupported")
            observed = {
                "source": "px4-param:SIM_GZ_EN_GPS",
                "requested_sensor_component": component,
                "requested_failure_type": failure_type,
                "before_sensor_sample": before_sample,
                "block_param_observed": False,
                "sensor_failure_effect_observed": False,
                "gps_sample_lost_after_injection": False,
                "estimator_degradation_observed": False,
            }
        reset = _px4_param_set("SIM_GZ_EN_GPS", 1.0)
        reset_applied = _px4_param_set_applied(reset)
        reset["reset_observed"] = reset_applied
        if not reset_applied:
            capability_status = "unsupported"
            application_status = "unsupported"
            observation_status = "sensor_failure_cleanup_failed"
            unsupported_reasons.append("sensor_failure_cleanup_failed")
            observed["sensor_failure_effect_observed"] = False
            observed["gps_sample_lost_after_injection"] = False
            observed["cleanup_reset_observed"] = False
        else:
            observed["cleanup_reset_observed"] = True
    elif requested_present:
        capability_status = "unsupported"
        application_status = "unsupported"
        observation_status = "unsupported"
    capability = {
        "schema_version": "sensor_simulator_capability_matrix.v1",
        "capability_id": "sensor_simulator_capability_matrix:mission_designer_sensor_failure",
        "sensor_failure": capability_status,
        "support_detection_method": (
            "px4_param_set_sim_gz_en_gps_and_sensor_gps_readback"
            if requested_present
            else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
    }
    application = {
        "schema_version": "sensor_failure_injection_application.v1",
        "application_id": "sensor_failure_injection_application:mission_designer_sensor_failure",
        "condition_kind": "sensor_failure",
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "reset": reset,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_sensor_condition_evidence.v1",
        "evidence_id": "observed_sensor_condition_evidence:mission_designer_sensor_failure",
        "condition_kind": "sensor_failure",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    return {
        "sensor_condition_profile": profile,
        "sensor_simulator_capability_matrix": capability,
        "sensor_failure_injection_application": application,
        "observed_sensor_condition_evidence": evidence,
    }


def _landing_zone_blocked_realism(
    *,
    payload_model_root: Path | None,
) -> dict[str, Any]:
    requested = _landing_zone_blocked_requested()
    profile = {
        "schema_version": "gazebo_world_condition_profile.v1",
        "condition_id": "gazebo_world_condition_profile:mission_designer_landing_zone_blocked",
        "condition_kind": "landing_zone_blocked_marker",
        "requested": {
            "landing_zone_blocked": requested,
            "dropoff_frame": "gazebo_world_local",
            "marker_pose_xyz_m": [5.0, 5.0, 0.025],
            "collision_enabled": False,
            "visual_only": True,
        },
        "requested_present": requested,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    unsupported_reasons: list[str] = []
    approximation_reasons: list[str] = []
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    if requested:
        world_path = (
            None
            if payload_model_root is None
            else payload_model_root / "worlds" / "default.sdf"
        )
        if world_path is None or not world_path.exists():
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("gazebo_world_sdf_missing")
        else:
            world_text = world_path.read_text(encoding="utf-8")
            world_sha256 = hashlib.sha256(world_text.encode("utf-8")).hexdigest()
            marker_present = False
            marker_visual_present = False
            marker_collision_present = False
            for model in ET.fromstring(world_text).iter("model"):
                if (
                    model.attrib.get("name")
                    != "mission_designer_landing_zone_blocked_marker"
                ):
                    continue
                marker_present = True
                marker_visual_present = model.find(".//visual") is not None
                marker_collision_present = model.find(".//collision") is not None
                break
            if (
                marker_present
                and marker_visual_present
                and not marker_collision_present
            ):
                application_status = "applied"
                observation_status = "world_sdf_marker_observed"
                approximation_reasons.append(
                    "visual_only_marker_not_collision_or_landing_zone_verifier"
                )
                applied = {
                    "method": "gazebo_world_sdf_visual_marker",
                    "world_sdf_path": str(world_path),
                    "model_name": "mission_designer_landing_zone_blocked_marker",
                    "marker_pose_xyz_m": [5.0, 5.0, 0.025],
                    "collision_enabled": False,
                    "world_sdf_sha256": world_sha256,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                }
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": True,
                    "model_name": "mission_designer_landing_zone_blocked_marker",
                    "visual_present": marker_visual_present,
                    "collision_present": marker_collision_present,
                    "world_sdf_sha256": world_sha256,
                    "landing_zone_blocked_does_not_verify_dropoff": True,
                }
            else:
                application_status = "unsupported"
                observation_status = "unsupported"
                unsupported_reasons.append(
                    "landing_zone_blocked_marker_not_materialized"
                )
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": False,
                    "world_sdf_sha256": world_sha256,
                }
    capability_status = (
        "supported_visual_only"
        if application_status == "applied"
        else "unsupported" if unsupported_reasons else "not_requested"
    )
    capability = {
        "schema_version": "gazebo_world_capability_matrix.v1",
        "capability_id": "gazebo_world_capability_matrix:mission_designer_landing_zone_blocked",
        "landing_zone_blocked_marker": capability_status,
        "collision_enabled": False,
        "support_detection_method": (
            "gazebo_world_sdf_marker_presence" if requested else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "gazebo_world_application.v1",
        "application_id": "gazebo_world_application:mission_designer_landing_zone_blocked",
        "condition_kind": "landing_zone_blocked_marker",
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    obstacle_manifest = {
        "schema_version": "obstacle_manifest.v1",
        "manifest_id": "obstacle_manifest:mission_designer_landing_zone_blocked",
        "obstacles": (
            [
                {
                    "obstacle_id": "mission_designer_landing_zone_blocked_marker",
                    "kind": "landing_zone_blocked_marker",
                    "source": "gazebo_world_sdf",
                    "frame": "gazebo_world_local",
                    "visual_only": True,
                    "collision_enabled": False,
                    "sensor_visible_claimed": False,
                }
            ]
            if requested
            else []
        ),
        "delivery_completion_claimed": False,
    }
    evidence = {
        "schema_version": "observed_world_condition_evidence.v1",
        "evidence_id": "observed_world_condition_evidence:mission_designer_landing_zone_blocked",
        "condition_kind": "landing_zone_blocked_marker",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    return {
        "gazebo_world_condition_profile": profile,
        "gazebo_world_capability_matrix": capability,
        "gazebo_world_application": application,
        "obstacle_manifest": obstacle_manifest,
        "observed_world_condition_evidence": evidence,
    }


def _visibility_realism(
    *,
    payload_model_root: Path | None,
) -> dict[str, Any]:
    mode = _visibility_mode_request()
    requested_present = bool(mode)
    unsupported_reasons: list[str] = []
    approximation_reasons: list[str] = []
    if mode and mode not in ("fog", "smoke"):
        unsupported_reasons.append("visibility_mode_not_supported")
    if mode == "smoke":
        unsupported_reasons.append("smoke_visibility_mode_deferred_to_particle_slice")
    profile = {
        "schema_version": "visibility_condition_profile.v1",
        "condition_id": "visibility_condition_profile:mission_designer_visibility",
        "condition_kind": "visibility_fog_render_marker",
        "requested": {
            "visibility_mode": mode if mode in ("fog", "smoke") else None,
            "fog_mode_requested": mode == "fog",
            "smoke_mode_requested": mode == "smoke",
            "render_only_marker_requested": mode == "fog",
            "smoke_deferred_to_followup_pr": mode == "smoke",
        },
        "requested_present": requested_present,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    if mode == "fog":
        world_path = (
            None
            if payload_model_root is None
            else payload_model_root / "worlds" / "default.sdf"
        )
        if world_path is None or not world_path.exists():
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("gazebo_world_sdf_missing")
        else:
            world_text = world_path.read_text(encoding="utf-8")
            world_sha256 = hashlib.sha256(world_text.encode("utf-8")).hexdigest()
            marker_comment_present = VISIBILITY_FOG_RENDER_MARKER_ID in world_text
            fog_element_present = False
            fog_type_matches = False
            fog_density_matches = False
            fog_color_matches = False
            fog_start_matches = False
            fog_end_matches = False
            try:
                ET.fromstring(world_text)
                fog = _visibility_marker_fog_element(world_text)
                if fog is not None:
                    fog_element_present = True
                    type_text = (fog.findtext("type") or "").strip()
                    density_text = (fog.findtext("density") or "").strip()
                    color_text = (fog.findtext("color") or "").strip()
                    start_text = (fog.findtext("start") or "").strip()
                    end_text = (fog.findtext("end") or "").strip()
                    fog_type_matches = type_text == VISIBILITY_FOG_RENDER_TYPE
                    fog_density_matches = density_text == VISIBILITY_FOG_RENDER_DENSITY
                    fog_color_matches = color_text == VISIBILITY_FOG_RENDER_COLOR
                    fog_start_matches = start_text == VISIBILITY_FOG_RENDER_START_M
                    fog_end_matches = end_text == VISIBILITY_FOG_RENDER_END_M
            except ET.ParseError:
                unsupported_reasons.append("visibility_world_sdf_parse_failed")
            fog_render_marker_materialized = bool(
                marker_comment_present
                and fog_element_present
                and fog_type_matches
                and fog_density_matches
                and fog_color_matches
                and fog_start_matches
                and fog_end_matches
            )
            if fog_render_marker_materialized:
                application_status = "applied_with_approximations"
                observation_status = "world_sdf_fog_render_marker_observed"
                approximation_reasons.append(
                    "scene_fog_render_marker_not_visibility_meters_or_sensor_effect"
                )
                applied = {
                    "method": "gazebo_world_sdf_scene_fog_render_marker",
                    "world_sdf_path": str(world_path),
                    "visibility_mode": mode,
                    "fog_render_marker_id": VISIBILITY_FOG_RENDER_MARKER_ID,
                    "fog_type_requested": VISIBILITY_FOG_RENDER_TYPE,
                    "fog_density_requested": VISIBILITY_FOG_RENDER_DENSITY,
                    "fog_color_requested": VISIBILITY_FOG_RENDER_COLOR,
                    "fog_start_m_requested": VISIBILITY_FOG_RENDER_START_M,
                    "fog_end_m_requested": VISIBILITY_FOG_RENDER_END_M,
                    "visibility_fog_render_marker_materialized": True,
                    "visibility_meters_target_materialized": False,
                    "observed_fog_render_matches_requested": True,
                    "gazebo_world_sdf_mutated": True,
                    "publisher_state_mutated": False,
                    "mission_upload_path_mutated": False,
                    "mission_progress_mutated": False,
                    "world_sdf_sha256": world_sha256,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                }
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": True,
                    "fog_render_marker_id": VISIBILITY_FOG_RENDER_MARKER_ID,
                    "fog_element_present": fog_element_present,
                    "fog_type_matches_requested": fog_type_matches,
                    "fog_density_matches_requested": fog_density_matches,
                    "fog_color_matches_requested": fog_color_matches,
                    "fog_start_matches_requested": fog_start_matches,
                    "fog_end_matches_requested": fog_end_matches,
                    "visibility_fog_render_marker_materialized": True,
                    "visibility_meters_target_materialized": False,
                    "observed_fog_render_matches_requested": True,
                    "traffic_conflict_verified": False,
                    "route_blocking_verified": False,
                    "incident_verified": False,
                    "world_sdf_sha256": world_sha256,
                }
            else:
                application_status = "unsupported"
                observation_status = "unsupported"
                unsupported_reasons.append(
                    "visibility_fog_render_marker_not_materialized"
                )
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": False,
                    "fog_render_marker_id": VISIBILITY_FOG_RENDER_MARKER_ID,
                    "marker_comment_present": marker_comment_present,
                    "fog_element_present": fog_element_present,
                    "fog_type_matches_requested": fog_type_matches,
                    "fog_density_matches_requested": fog_density_matches,
                    "fog_color_matches_requested": fog_color_matches,
                    "fog_start_matches_requested": fog_start_matches,
                    "fog_end_matches_requested": fog_end_matches,
                    "visibility_fog_render_marker_materialized": False,
                    "visibility_meters_target_materialized": False,
                    "observed_fog_render_matches_requested": False,
                    "world_sdf_sha256": world_sha256,
                }
    elif requested_present:
        application_status = "unsupported"
        observation_status = "unsupported"
    capability_status = (
        "supported_render_only"
        if application_status == "applied_with_approximations"
        else "unsupported" if unsupported_reasons else "not_requested"
    )
    capability = {
        "schema_version": "visibility_capability_matrix.v1",
        "capability_id": "visibility_capability_matrix:mission_designer_visibility",
        "fog_render_marker": capability_status,
        "smoke_render_marker": "deferred_to_followup_pr",
        "visibility_meters_target": "not_materialized",
        "support_detection_method": (
            "gazebo_world_sdf_scene_fog_presence"
            if requested_present
            else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "visibility_application.v1",
        "application_id": "visibility_application:mission_designer_visibility",
        "condition_kind": "visibility_fog_render_marker",
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_visibility_condition_evidence.v1",
        "evidence_id": "observed_visibility_condition_evidence:mission_designer_visibility",
        "condition_kind": "visibility_fog_render_marker",
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verified": False,
        "delivery_completion_claimed": False,
    }
    return {
        "visibility_condition_profile": profile,
        "visibility_capability_matrix": capability,
        "visibility_application": application,
        "observed_visibility_condition_evidence": evidence,
    }


def _operational_no_fly_zone_realism(
    *,
    payload_model_root: Path | None,
) -> dict[str, Any]:
    no_fly_zone_requested = _no_fly_zone_marker_requested()
    traffic_conflict_requested = _traffic_conflict_marker_requested()
    alternate_landing_requested = _alternate_landing_marker_requested()
    rth_behavior_requested = _rth_behavior_requested()
    moving_actor_requested = _moving_actor_marker_requested()
    collision_obstacle_requested = _collision_obstacle_requested()
    collision_contact_topic_requested = _collision_obstacle_contact_topic_requested()
    multi_drone_conflict_probe_requested = _multi_drone_conflict_probe_requested()
    requested = (
        no_fly_zone_requested
        or traffic_conflict_requested
        or alternate_landing_requested
        or rth_behavior_requested
        or moving_actor_requested
        or collision_obstacle_requested
        or multi_drone_conflict_probe_requested
    )
    unsupported_reasons: list[str] = []
    approximation_reasons: list[str] = []
    visual_marker_requested = (
        no_fly_zone_requested
        or traffic_conflict_requested
        or alternate_landing_requested
        or moving_actor_requested
        or collision_obstacle_requested
    )
    profile = {
        "schema_version": "operational_condition_profile.v1",
        "condition_id": "operational_condition_profile:mission_designer_operational_markers",
        "condition_kind": (
            "moving_visual_actor_marker"
            if moving_actor_requested
            and not no_fly_zone_requested
            and not traffic_conflict_requested
            and not alternate_landing_requested
            and not collision_obstacle_requested
            else (
                "collision_enabled_moving_obstacle"
                if collision_obstacle_requested
                and not no_fly_zone_requested
                and not traffic_conflict_requested
                and not alternate_landing_requested
                and not moving_actor_requested
                and not multi_drone_conflict_probe_requested
                else (
                    "alternate_landing_visual_marker"
                    if alternate_landing_requested
                    and not no_fly_zone_requested
                    and not traffic_conflict_requested
                    and not moving_actor_requested
                    and not collision_obstacle_requested
                    and not rth_behavior_requested
                    else (
                        "traffic_conflict_visual_marker"
                        if traffic_conflict_requested
                        and not no_fly_zone_requested
                        and not alternate_landing_requested
                        and not moving_actor_requested
                        and not collision_obstacle_requested
                        and not multi_drone_conflict_probe_requested
                        else (
                            "multi_drone_conflict_support_detection"
                            if multi_drone_conflict_probe_requested
                            and not no_fly_zone_requested
                            and not traffic_conflict_requested
                            and not alternate_landing_requested
                            and not moving_actor_requested
                            and not collision_obstacle_requested
                            else (
                                "operational_visual_markers"
                                if (
                                    (
                                        traffic_conflict_requested
                                        and no_fly_zone_requested
                                    )
                                    or (
                                        alternate_landing_requested
                                        and (
                                            no_fly_zone_requested
                                            or traffic_conflict_requested
                                        )
                                    )
                                    or rth_behavior_requested
                                    or (
                                        moving_actor_requested
                                        and (
                                            no_fly_zone_requested
                                            or traffic_conflict_requested
                                            or alternate_landing_requested
                                        )
                                    )
                                    or collision_obstacle_requested
                                    or (
                                        multi_drone_conflict_probe_requested
                                        and (
                                            no_fly_zone_requested
                                            or traffic_conflict_requested
                                            or alternate_landing_requested
                                            or moving_actor_requested
                                            or collision_obstacle_requested
                                        )
                                    )
                                )
                                else "no_fly_zone_visual_marker"
                            )
                        )
                    )
                )
            )
        ),
        "requested": {
            "no_fly_zone_marker": no_fly_zone_requested,
            "traffic_conflict_marker": traffic_conflict_requested,
            "alternate_landing_marker": alternate_landing_requested,
            "return_to_home_behavior": rth_behavior_requested,
            "moving_actor_marker": moving_actor_requested,
            "collision_obstacle": collision_obstacle_requested,
            "multi_drone_conflict_probe": multi_drone_conflict_probe_requested,
            "frame": "gazebo_world_local",
            "no_fly_zone_center_xy_m": [2.5, 2.5],
            "no_fly_zone_radius_m": 1.25,
            "traffic_conflict_xy_m": [3.6, 2.9],
            "alternate_landing_xy_m": [-2.0, 3.5],
            "moving_actor_start_xy_m": [1.2, -0.7],
            "moving_actor_end_xy_m": [4.2, 3.2],
            "moving_actor_loop_seconds": 6.0,
            "moving_actor_mode": "linear_waypoint_motion",
            "moving_actor_nominal_profile_velocity_mps": (
                _moving_actor_waypoint_motion_spec()["nominal_profile_velocity_mps"]
            ),
            "collision_obstacle_start_xy_m": _collision_obstacle_motion_spec()[
                "start_xy_m"
            ],
            "collision_obstacle_end_xy_m": _collision_obstacle_motion_spec()[
                "end_xy_m"
            ],
            "collision_obstacle_loop_seconds": _collision_obstacle_motion_spec()[
                "loop_seconds"
            ],
            "enforcement_enabled": False,
            "traffic_motion_enabled": False,
            "moving_actor_sdf_scripted_motion_enabled": (
                True if moving_actor_requested else False
            ),
            "collision_enabled": False,
            "collision_obstacle_collision_enabled": collision_obstacle_requested,
            "collision_obstacle_contact_topic_enabled": (
                collision_obstacle_requested and collision_contact_topic_requested
            ),
            "sensor_visible_claimed": False,
            "incident_claimed": False,
            "route_blocking_enabled": False,
            "alternate_landing_behavior_enabled": False,
            "return_to_home_behavior_enabled": rth_behavior_requested,
            "multi_vehicle_enabled": False,
            "multi_drone_conflict_verifier_enabled": False,
            "explicit_vehicle_ids": [],
            "visual_only": visual_marker_requested,
        },
        "requested_present": requested,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    observation_status = "not_requested"
    if requested:
        if multi_drone_conflict_probe_requested:
            unsupported_reasons.extend(
                [
                    "multi_drone_support_not_implemented",
                    "multi_drone_probe_not_traffic_conflict_verifier",
                ]
            )
        world_path = (
            None
            if payload_model_root is None
            else payload_model_root / "worlds" / "default.sdf"
        )
        if multi_drone_conflict_probe_requested and not visual_marker_requested:
            application_status = "unsupported"
            observation_status = "unsupported"
            observed = {
                "source": "operational_support_detection",
                "observed": False,
                "multi_drone_conflict_probe_requested": True,
                "multi_vehicle_enabled": False,
                "explicit_vehicle_ids_observed": [],
                "multi_drone_conflict_verified": False,
                "route_blocking_observed": False,
                "incident_observed": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            }
        elif world_path is None or not world_path.exists():
            application_status = "unsupported"
            observation_status = "unsupported"
            unsupported_reasons.append("gazebo_world_sdf_missing")
        else:
            world_text = world_path.read_text(encoding="utf-8")
            world_sha256 = hashlib.sha256(world_text.encode("utf-8")).hexdigest()
            no_fly_marker_present = False
            no_fly_marker_visual_present = False
            no_fly_marker_collision_present = False
            traffic_marker_present = False
            traffic_marker_visual_present = False
            traffic_marker_collision_present = False
            alternate_marker_present = False
            alternate_marker_visual_present = False
            alternate_marker_collision_present = False
            moving_actor_present = False
            moving_actor_script_present = False
            moving_actor_visual_present = False
            moving_actor_collision_present = False
            collision_obstacle_present = False
            collision_obstacle_visual_present = False
            collision_obstacle_collision_present = False
            collision_obstacle_script_present = False
            collision_obstacle_contact_sensor_present = False
            for model in ET.fromstring(world_text).iter("model"):
                model_name = model.attrib.get("name")
                if model_name == "mission_designer_no_fly_zone_marker":
                    no_fly_marker_present = True
                    no_fly_marker_visual_present = model.find(".//visual") is not None
                    no_fly_marker_collision_present = (
                        model.find(".//collision") is not None
                    )
                if model_name == "mission_designer_traffic_conflict_marker":
                    traffic_marker_present = True
                    traffic_marker_visual_present = model.find(".//visual") is not None
                    traffic_marker_collision_present = (
                        model.find(".//collision") is not None
                    )
                if model_name == "mission_designer_alternate_landing_marker":
                    alternate_marker_present = True
                    alternate_marker_visual_present = (
                        model.find(".//visual") is not None
                    )
                    alternate_marker_collision_present = (
                        model.find(".//collision") is not None
                    )
                if model_name == "mission_designer_moving_actor_marker":
                    moving_actor_present = True
                    moving_actor_visual_present = model.find(".//visual") is not None
                    moving_actor_script_present = (
                        model.find(
                            ".//plugin[@name='gz::sim::systems::TrajectoryFollower']"
                        )
                        is not None
                    )
                    moving_actor_collision_present = (
                        model.find(".//collision") is not None
                    )
                if model_name == "mission_designer_collision_obstacle":
                    collision_obstacle_present = True
                    collision_obstacle_visual_present = (
                        model.find(".//visual") is not None
                    )
                    collision_obstacle_collision_present = (
                        model.find(".//collision") is not None
                    )
                    collision_obstacle_contact_sensor_present = (
                        model.find(".//sensor[@type='contact']") is not None
                    )
                    collision_obstacle_script_present = (
                        model.find(
                            ".//plugin[@name='gz::sim::systems::TrajectoryFollower']"
                        )
                        is not None
                    )
            for actor in ET.fromstring(world_text).iter("actor"):
                actor_name = actor.attrib.get("name")
                if actor_name == "mission_designer_moving_actor_marker":
                    moving_actor_present = True
                    moving_actor_script_present = (
                        actor.find(".//script/trajectory") is not None
                    )
                    moving_actor_collision_present = (
                        actor.find(".//collision") is not None
                    )
            no_fly_ok = not no_fly_zone_requested or (
                no_fly_marker_present
                and no_fly_marker_visual_present
                and not no_fly_marker_collision_present
            )
            traffic_ok = not traffic_conflict_requested or (
                traffic_marker_present
                and traffic_marker_visual_present
                and not traffic_marker_collision_present
            )
            alternate_ok = not alternate_landing_requested or (
                alternate_marker_present
                and alternate_marker_visual_present
                and not alternate_marker_collision_present
            )
            moving_actor_ok = not moving_actor_requested or (
                moving_actor_present
                and moving_actor_visual_present
                and moving_actor_script_present
                and not moving_actor_collision_present
            )
            collision_obstacle_ok = not collision_obstacle_requested or (
                collision_obstacle_present
                and collision_obstacle_visual_present
                and collision_obstacle_collision_present
                and (
                    collision_obstacle_contact_sensor_present
                    if collision_contact_topic_requested
                    else True
                )
                and collision_obstacle_script_present
            )
            if (
                no_fly_ok
                and traffic_ok
                and alternate_ok
                and moving_actor_ok
                and collision_obstacle_ok
            ):
                application_status = "applied_with_approximations"
                observation_status = "world_sdf_operational_markers_observed"
                if no_fly_zone_requested:
                    approximation_reasons.append(
                        "visual_only_marker_not_geofence_enforcement"
                    )
                if traffic_conflict_requested:
                    approximation_reasons.append(
                        "visual_only_marker_not_dynamic_traffic_or_collision"
                    )
                if alternate_landing_requested:
                    approximation_reasons.append(
                        "visual_only_marker_not_alternate_landing_or_rth_behavior"
                    )
                if moving_actor_requested:
                    approximation_reasons.append(
                        "visual_only_actor_not_collision_sensor_visible_incident_or_route_blocking"
                    )
                if collision_obstacle_requested:
                    approximation_reasons.append(
                        "collision_obstacle_not_traffic_conflict_route_blocking_incident_or_gate"
                    )
                applied = {
                    "method": "gazebo_world_sdf_operational_visual_markers",
                    "world_sdf_path": str(world_path),
                    "model_names": [
                        *(
                            ["mission_designer_no_fly_zone_marker"]
                            if no_fly_zone_requested
                            else []
                        ),
                        *(
                            ["mission_designer_traffic_conflict_marker"]
                            if traffic_conflict_requested
                            else []
                        ),
                        *(
                            ["mission_designer_alternate_landing_marker"]
                            if alternate_landing_requested
                            else []
                        ),
                        *(
                            ["mission_designer_moving_actor_marker"]
                            if moving_actor_requested
                            else []
                        ),
                        *(
                            ["mission_designer_collision_obstacle"]
                            if collision_obstacle_requested
                            else []
                        ),
                    ],
                    "no_fly_zone_center_xy_m": [2.5, 2.5],
                    "no_fly_zone_radius_m": 1.25,
                    "traffic_conflict_xy_m": [3.6, 2.9],
                    "alternate_landing_xy_m": [-2.0, 3.5],
                    "moving_actor_start_xy_m": [1.2, -0.7],
                    "moving_actor_end_xy_m": [4.2, 3.2],
                    "moving_actor_loop_seconds": 6.0,
                    "moving_actor_mode": "linear_waypoint_motion",
                    "moving_actor_nominal_profile_velocity_mps": (
                        _moving_actor_waypoint_motion_spec()[
                            "nominal_profile_velocity_mps"
                        ]
                    ),
                    "moving_actor_trajectory_definition_sha256": (
                        _moving_actor_waypoint_trajectory_definition_sha256()
                    ),
                    "collision_obstacle_start_xy_m": (
                        _collision_obstacle_motion_spec()["start_xy_m"]
                    ),
                    "collision_obstacle_end_xy_m": (
                        _collision_obstacle_motion_spec()["end_xy_m"]
                    ),
                    "collision_obstacle_loop_seconds": (
                        _collision_obstacle_motion_spec()["loop_seconds"]
                    ),
                    "enforcement_enabled": False,
                    "traffic_motion_enabled": False,
                    "moving_actor_scripted_motion_enabled": moving_actor_requested,
                    "collision_enabled": collision_obstacle_requested,
                    "collision_obstacle_enabled": collision_obstacle_requested,
                    "collision_obstacle_contact_sensor_enabled": (
                        collision_obstacle_requested
                    ),
                    "collision_obstacle_contact_topic": (
                        COLLISION_OBSTACLE_CONTACT_TOPIC
                        if collision_obstacle_requested
                        else ""
                    ),
                    "sensor_visible_claimed": False,
                    "incident_claimed": False,
                    "route_blocking_enabled": False,
                    "alternate_landing_behavior_enabled": False,
                    "return_to_home_behavior_enabled": False,
                    "multi_vehicle_enabled": False,
                    "multi_drone_conflict_verifier_enabled": False,
                    "explicit_vehicle_ids": [],
                    "world_sdf_sha256": world_sha256,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                }
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": True,
                    "no_fly_zone_model_name": (
                        "mission_designer_no_fly_zone_marker"
                        if no_fly_zone_requested
                        else ""
                    ),
                    "traffic_conflict_model_name": (
                        "mission_designer_traffic_conflict_marker"
                        if traffic_conflict_requested
                        else ""
                    ),
                    "alternate_landing_model_name": (
                        "mission_designer_alternate_landing_marker"
                        if alternate_landing_requested
                        else ""
                    ),
                    "moving_actor_name": (
                        "mission_designer_moving_actor_marker"
                        if moving_actor_requested
                        else ""
                    ),
                    "collision_obstacle_name": (
                        "mission_designer_collision_obstacle"
                        if collision_obstacle_requested
                        else ""
                    ),
                    "no_fly_zone_visual_present": no_fly_marker_visual_present,
                    "traffic_conflict_visual_present": traffic_marker_visual_present,
                    "alternate_landing_visual_present": alternate_marker_visual_present,
                    "moving_actor_script_present": False,
                    "moving_actor_sdf_motion_present": moving_actor_script_present,
                    "moving_actor_trajectory_follower_present": moving_actor_script_present,
                    "moving_actor_visual_present": moving_actor_visual_present,
                    "collision_present": (
                        no_fly_marker_collision_present
                        or traffic_marker_collision_present
                        or alternate_marker_collision_present
                        or moving_actor_collision_present
                        or collision_obstacle_collision_present
                    ),
                    "collision_obstacle_visual_present": collision_obstacle_visual_present,
                    "collision_obstacle_collision_present": collision_obstacle_collision_present,
                    "collision_obstacle_contact_sensor_present": (
                        collision_obstacle_contact_sensor_present
                    ),
                    "collision_obstacle_contact_topic": (
                        COLLISION_OBSTACLE_CONTACT_TOPIC
                        if collision_obstacle_contact_sensor_present
                        else ""
                    ),
                    "collision_obstacle_trajectory_follower_present": collision_obstacle_script_present,
                    "geofence_enforcement_observed": False,
                    "dynamic_traffic_observed": False,
                    "moving_actor_sdf_script_observed": False,
                    "moving_actor_sdf_motion_observed": moving_actor_script_present,
                    "moving_actor_trajectory_follower_observed": moving_actor_script_present,
                    "moving_actor_collision_observed": False,
                    "moving_actor_sensor_evidence_observed": False,
                    "moving_actor_incident_observed": False,
                    "moving_actor_route_blocking_observed": False,
                    "collision_obstacle_route_blocking_observed": False,
                    "collision_obstacle_incident_observed": False,
                    "collision_obstacle_contact_observed": False,
                    "traffic_conflict_sensor_evidence_observed": False,
                    "alternate_landing_behavior_observed": False,
                    "return_to_home_behavior_observed": False,
                    "multi_drone_conflict_probe_observed": False,
                    "multi_vehicle_observed": False,
                    "explicit_vehicle_ids_observed": [],
                    "multi_drone_conflict_verified": False,
                    "world_sdf_sha256": world_sha256,
                }
            else:
                application_status = "unsupported"
                observation_status = "unsupported"
                if not no_fly_ok:
                    unsupported_reasons.append("no_fly_zone_marker_not_materialized")
                if not traffic_ok:
                    unsupported_reasons.append(
                        "traffic_conflict_marker_not_materialized"
                    )
                if not alternate_ok:
                    unsupported_reasons.append(
                        "alternate_landing_marker_not_materialized"
                    )
                if not moving_actor_ok:
                    unsupported_reasons.append("moving_actor_marker_not_materialized")
                if not collision_obstacle_ok:
                    unsupported_reasons.append("collision_obstacle_not_materialized")
                observed = {
                    "source": "gazebo_world_sdf",
                    "observed": False,
                    "world_sdf_sha256": world_sha256,
                }
    capability_status = (
        "supported_visual_only"
        if application_status == "applied_with_approximations"
        else "unsupported" if unsupported_reasons else "not_requested"
    )
    geofence = {
        "schema_version": "geofence_condition_profile.v1",
        "geofence_id": "geofence_condition_profile:mission_designer_no_fly_zone",
        "geofences": (
            [
                {
                    "geofence_id": "mission_designer_no_fly_zone_marker",
                    "frame": "gazebo_world_local",
                    "center_xy_m": [2.5, 2.5],
                    "radius_m": 1.25,
                    "visual_only": True,
                    "enforcement_enabled": False,
                }
            ]
            if no_fly_zone_requested
            else []
        ),
        "delivery_completion_claimed": False,
    }
    traffic_conflict = {
        "schema_version": "traffic_conflict_profile.v1",
        "traffic_conflict_id": "traffic_conflict_profile:mission_designer_visual_marker",
        "conflicts": (
            [
                {
                    "conflict_id": "mission_designer_traffic_conflict_marker",
                    "frame": "gazebo_world_local",
                    "position_xy_m": [3.6, 2.9],
                    "visual_only": True,
                    "dynamic_motion_enabled": False,
                    "collision_enabled": False,
                    "sensor_visible_claimed": False,
                }
            ]
            if traffic_conflict_requested
            else []
        ),
        "delivery_completion_claimed": False,
    }
    alternate_landing = {
        "schema_version": "alternate_landing_profile.v1",
        "alternate_landing_id": "alternate_landing_profile:mission_designer_visual_marker",
        "candidates": (
            [
                {
                    "candidate_id": "mission_designer_alternate_landing_marker",
                    "frame": "gazebo_world_local",
                    "position_xy_m": [-2.0, 3.5],
                    "visual_only": True,
                    "alternate_landing_behavior_enabled": False,
                    "return_to_home_behavior_enabled": False,
                    "landing_zone_verified": False,
                    "collision_enabled": False,
                }
            ]
            if alternate_landing_requested
            else []
        ),
        "delivery_completion_claimed": False,
    }
    dynamic_actor = {
        "schema_version": "dynamic_actor_profile.v1",
        "dynamic_actor_id": "dynamic_actor_profile:mission_designer_moving_visual_marker",
        "actors": (
            [
                {
                    "actor_id": "mission_designer_moving_actor_marker",
                    "sdf_entity_type": "model",
                    "frame": "gazebo_world_local",
                    "start_xy_m": [1.2, -0.7],
                    "end_xy_m": [4.2, 3.2],
                    "loop_seconds": 6.0,
                    "mode": "linear_waypoint_motion",
                    "nominal_profile_velocity_mps": (
                        _moving_actor_waypoint_motion_spec()[
                            "nominal_profile_velocity_mps"
                        ]
                    ),
                    "trajectory_definition_sha256": (
                        _moving_actor_waypoint_trajectory_definition_sha256()
                    ),
                    "visual_only": True,
                    "sdf_scripted_motion_enabled": True,
                    "trajectory_follower_plugin_enabled": True,
                    "gravity_enabled": False,
                    "collision_enabled": False,
                    "sensor_visible_claimed": False,
                    "route_blocking_enabled": False,
                    "incident_claimed": False,
                }
            ]
            if moving_actor_requested
            else []
        ),
        "delivery_completion_claimed": False,
    }
    collision_motion = _collision_obstacle_motion_spec()
    collision_obstacle = {
        "schema_version": "collision_obstacle_profile.v1",
        "obstacle_id": "collision_obstacle_profile:mission_designer_collision_obstacle",
        "condition_kind": "collision_enabled_moving_obstacle",
        "requested_present": collision_obstacle_requested,
        "obstacles": (
            [
                {
                    "obstacle_id": "mission_designer_collision_obstacle",
                    "sdf_entity_type": "model",
                    "frame": "gazebo_world_local",
                    "start_xy_m": collision_motion["start_xy_m"],
                    "end_xy_m": collision_motion["end_xy_m"],
                    "loop_seconds": collision_motion["loop_seconds"],
                    "mode": collision_motion["mode"],
                    "visual_only": False,
                    "collision_enabled": True,
                    "contact_sensor_enabled": collision_contact_topic_requested,
                    "contact_topic": (
                        COLLISION_OBSTACLE_CONTACT_TOPIC
                        if collision_contact_topic_requested
                        else ""
                    ),
                    "trajectory_follower_plugin_enabled": True,
                    "sensor_visible_claimed": False,
                    "route_blocking_enabled": False,
                    "incident_claimed": False,
                    "traffic_conflict_verifier": False,
                }
            ]
            if collision_obstacle_requested
            else []
        ),
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }
    multi_vehicle_frame_contract = {
        "schema_version": "multi_vehicle_frame_contract.v1",
        "contract_id": "multi_vehicle_frame_contract:mission_designer_primary_vehicle_only",
        "condition_kind": "multi_drone_conflict_support_detection",
        "requested_present": multi_drone_conflict_probe_requested,
        "frame": "gazebo_world_local",
        "primary_vehicle_id": "x500_0",
        "primary_vehicle_frame": "gazebo_world_local",
        "additional_vehicle_ids": [],
        "additional_vehicle_frames": [],
        "multi_vehicle_enabled": False,
        "conflict_verifier_enabled": False,
        "traffic_conflict_verified": False,
        "route_blocking_enabled": False,
        "incident_claimed": False,
        "unsupported_until_explicit_vehicle_ids_and_observer": True,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    capability = {
        "schema_version": "operational_capability_matrix.v1",
        "capability_id": "operational_capability_matrix:mission_designer_operational_markers",
        "no_fly_zone_marker": (
            capability_status if no_fly_zone_requested else "not_requested"
        ),
        "traffic_conflict_marker": (
            capability_status if traffic_conflict_requested else "not_requested"
        ),
        "alternate_landing_marker": (
            capability_status if alternate_landing_requested else "not_requested"
        ),
        "moving_actor_marker": (
            capability_status if moving_actor_requested else "not_requested"
        ),
        "collision_obstacle": (
            "supported_collision_geometry"
            if collision_obstacle_requested
            and application_status == "applied_with_approximations"
            else "unsupported" if collision_obstacle_requested else "not_requested"
        ),
        "geofence_enforcement": "unsupported",
        "dynamic_traffic_motion": "unsupported",
        "moving_actor_collision": "unsupported",
        "moving_actor_sensor_visibility": "unsupported",
        "moving_actor_incident_evidence": "unsupported",
        "moving_actor_route_blocking": "unsupported",
        "traffic_collision": "unsupported",
        "traffic_sensor_visibility": "unsupported",
        "collision_obstacle_contact_evidence": (
            "supported_contact_topic_observer"
            if collision_obstacle_requested
            and collision_contact_topic_requested
            and application_status == "applied_with_approximations"
            else (
                "unsupported"
                if collision_obstacle_requested and collision_contact_topic_requested
                else (
                    "not_requested" if collision_obstacle_requested else "not_requested"
                )
            )
        ),
        "collision_obstacle_route_blocking": "unsupported",
        "collision_obstacle_incident_evidence": "unsupported",
        "multi_drone_conflict_probe": (
            "unsupported" if multi_drone_conflict_probe_requested else "not_requested"
        ),
        "multi_vehicle_simulation": "unsupported",
        "multi_drone_conflict_verifier": "unsupported",
        "explicit_vehicle_frame_binding": "unsupported",
        "alternate_landing_behavior": "unsupported",
        "return_to_home_behavior": (
            "supported_sitl_only" if rth_behavior_requested else "not_requested"
        ),
        "alternate_landing_zone_verification": "unsupported",
        "support_detection_method": (
            "gazebo_world_sdf_marker_presence" if requested else "not_requested"
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
    }
    application = {
        "schema_version": "operational_application.v1",
        "application_id": "operational_application:mission_designer_operational_markers",
        "condition_kind": profile["condition_kind"],
        "application_status": application_status,
        "requested_condition_ref": profile["condition_id"],
        "applied": applied,
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": approximation_reasons,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_operational_condition_evidence.v1",
        "evidence_id": "observed_operational_condition_evidence:mission_designer_operational_markers",
        "condition_kind": profile["condition_kind"],
        "observation_status": observation_status,
        "requested_condition_ref": profile["condition_id"],
        "application_ref": application["application_id"],
        "observed": observed,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    return {
        "operational_condition_profile": profile,
        "geofence_condition_profile": geofence,
        "traffic_conflict_profile": traffic_conflict,
        "alternate_landing_profile": alternate_landing,
        "dynamic_actor_profile": dynamic_actor,
        "collision_obstacle_profile": collision_obstacle,
        "multi_vehicle_frame_contract": multi_vehicle_frame_contract,
        "operational_capability_matrix": capability,
        "operational_application": application,
        "observed_operational_condition_evidence": evidence,
    }


def _vehicle_realism_summary_artifacts() -> dict[str, Any]:
    return {
        "vehicle_condition_profile": (VEHICLE_REALISM_SUMMARY or {}).get(
            "vehicle_condition_profile", {}
        ),
        "payload_simulator_capability_matrix": (VEHICLE_REALISM_SUMMARY or {}).get(
            "payload_simulator_capability_matrix", {}
        ),
        "payload_simulator_condition_application": (VEHICLE_REALISM_SUMMARY or {}).get(
            "payload_simulator_condition_application", {}
        ),
        "observed_vehicle_condition_evidence": (VEHICLE_REALISM_SUMMARY or {}).get(
            "observed_vehicle_condition_evidence", {}
        ),
        "battery_condition_profile": (BATTERY_REALISM_SUMMARY or {}).get(
            "battery_condition_profile", {}
        ),
        "battery_simulator_capability_matrix": (BATTERY_REALISM_SUMMARY or {}).get(
            "battery_simulator_capability_matrix", {}
        ),
        "battery_simulator_condition_application": (BATTERY_REALISM_SUMMARY or {}).get(
            "battery_simulator_condition_application", {}
        ),
        "observed_battery_condition_evidence": (BATTERY_REALISM_SUMMARY or {}).get(
            "observed_battery_condition_evidence", {}
        ),
        "thermal_weather_condition_profile": (
            THERMAL_WEATHER_REALISM_SUMMARY or {}
        ).get("thermal_weather_condition_profile", {}),
        "thermal_weather_simulator_capability_matrix": (
            THERMAL_WEATHER_REALISM_SUMMARY or {}
        ).get("thermal_weather_simulator_capability_matrix", {}),
        "thermal_weather_simulator_condition_application": (
            THERMAL_WEATHER_REALISM_SUMMARY or {}
        ).get("thermal_weather_simulator_condition_application", {}),
        "observed_thermal_weather_evidence": (
            THERMAL_WEATHER_REALISM_SUMMARY or {}
        ).get("observed_thermal_weather_evidence", {}),
        "sensor_condition_profile": (SENSOR_REALISM_SUMMARY or {}).get(
            "sensor_condition_profile", {}
        ),
        "sensor_simulator_capability_matrix": (SENSOR_REALISM_SUMMARY or {}).get(
            "sensor_simulator_capability_matrix", {}
        ),
        "sensor_failure_injection_application": (SENSOR_REALISM_SUMMARY or {}).get(
            "sensor_failure_injection_application", {}
        ),
        "observed_sensor_condition_evidence": (SENSOR_REALISM_SUMMARY or {}).get(
            "observed_sensor_condition_evidence", {}
        ),
        "gazebo_world_condition_profile": (WORLD_REALISM_SUMMARY or {}).get(
            "gazebo_world_condition_profile", {}
        ),
        "gazebo_world_capability_matrix": (WORLD_REALISM_SUMMARY or {}).get(
            "gazebo_world_capability_matrix", {}
        ),
        "gazebo_world_application": (WORLD_REALISM_SUMMARY or {}).get(
            "gazebo_world_application", {}
        ),
        "obstacle_manifest": (WORLD_REALISM_SUMMARY or {}).get("obstacle_manifest", {}),
        "observed_world_condition_evidence": (WORLD_REALISM_SUMMARY or {}).get(
            "observed_world_condition_evidence", {}
        ),
        "visibility_condition_profile": (VISIBILITY_REALISM_SUMMARY or {}).get(
            "visibility_condition_profile", {}
        ),
        "visibility_capability_matrix": (VISIBILITY_REALISM_SUMMARY or {}).get(
            "visibility_capability_matrix", {}
        ),
        "visibility_application": (VISIBILITY_REALISM_SUMMARY or {}).get(
            "visibility_application", {}
        ),
        "observed_visibility_condition_evidence": (
            VISIBILITY_REALISM_SUMMARY or {}
        ).get("observed_visibility_condition_evidence", {}),
        "operational_condition_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "operational_condition_profile", {}
        ),
        "geofence_condition_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "geofence_condition_profile", {}
        ),
        "traffic_conflict_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "traffic_conflict_profile", {}
        ),
        "alternate_landing_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "alternate_landing_profile", {}
        ),
        "dynamic_actor_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "dynamic_actor_profile", {}
        ),
        "collision_obstacle_profile": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "collision_obstacle_profile", {}
        ),
        "gazebo_route_corridor_obstacle_spawn_application": (
            _gazebo_route_corridor_obstacle_spawn_application_realism()
        ).get("gazebo_route_corridor_obstacle_spawn_application", {}),
        "multi_vehicle_frame_contract": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "multi_vehicle_frame_contract", {}
        ),
        "moving_actor_pose_observation": (MOVING_ACTOR_POSE_SUMMARY or {}).get(
            "moving_actor_pose_observation", {}
        ),
        "moving_actor_waypoint_motion_application": (
            MOVING_ACTOR_LINEAR_MOTION_SUMMARY or {}
        ).get("moving_actor_waypoint_motion_application", {}),
        "moving_actor_proximity_evidence": (MOVING_ACTOR_PROXIMITY_SUMMARY or {}).get(
            "moving_actor_proximity_evidence", {}
        ),
        "collision_obstacle_evidence": (COLLISION_OBSTACLE_SUMMARY or {}).get(
            "collision_obstacle_evidence", {}
        ),
        "route_blocking_candidate_evidence": (
            ROUTE_BLOCKING_CANDIDATE_SUMMARY or {}
        ).get("route_blocking_candidate_evidence", {}),
        "horizontal_route_contact_topic_integration": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_contact_topic_integration", {}),
        "horizontal_route_contact_event_incident_evidence": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_contact_event_incident_evidence", {}),
        "horizontal_route_contact_operational_incident_report": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_contact_operational_incident_report", {}),
        "horizontal_route_contact_scoped_verifier_candidate": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_contact_scoped_verifier_candidate", {}),
        "horizontal_route_contact_incident_verification": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_contact_incident_verification", {}),
        "horizontal_route_incident_informed_traffic_conflict_verification": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_incident_informed_traffic_conflict_verification", {}),
        "horizontal_route_incident_informed_route_blocking_verification": (
            HORIZONTAL_CONTACT_TOPIC_SUMMARY or {}
        ).get("horizontal_route_incident_informed_route_blocking_verification", {}),
        "operational_incident_report": (OPERATIONAL_INCIDENT_REPORT_SUMMARY or {}).get(
            "operational_incident_report", {}
        ),
        "traffic_conflict_verification": (
            TRAFFIC_CONFLICT_VERIFICATION_SUMMARY or {}
        ).get("traffic_conflict_verification", {}),
        "route_blocking_verification": (ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}).get(
            "route_blocking_verification", {}
        ),
        "alternate_landing_candidate_evidence": (
            ALTERNATE_LANDING_CANDIDATE_SUMMARY or {}
        ).get("alternate_landing_candidate_evidence", {}),
        "alternate_landing_execution_request": (
            ALTERNATE_LANDING_EXECUTION_SUMMARY or {}
        ).get("alternate_landing_execution_request", {}),
        "alternate_mission_upload_request": (
            ALTERNATE_MISSION_UPLOAD_SUMMARY or {}
        ).get("alternate_mission_upload_request", {}),
        "alternate_mission_upload_receipt": (
            ALTERNATE_MISSION_UPLOAD_SUMMARY or {}
        ).get("alternate_mission_upload_receipt", {}),
        "alternate_route_behavior_observation": (
            ALTERNATE_MISSION_UPLOAD_SUMMARY or {}
        ).get("alternate_route_behavior_observation", {}),
        "alternate_route_command_dispatch": (
            ALTERNATE_MISSION_UPLOAD_SUMMARY or {}
        ).get("alternate_route_command_dispatch", {}),
        "alternate_route_execution_evidence": (
            ALTERNATE_MISSION_UPLOAD_SUMMARY or {}
        ).get("alternate_route_execution_evidence", {}),
        "alternate_landing_command_dispatch": (
            ALTERNATE_LANDING_EXECUTION_SUMMARY or {}
        ).get("alternate_landing_command_dispatch", {}),
        "alternate_landing_behavior_observation": (
            ALTERNATE_LANDING_EXECUTION_SUMMARY or {}
        ).get("alternate_landing_behavior_observation", {}),
        "alternate_landing_outcome": (ALTERNATE_LANDING_EXECUTION_SUMMARY or {}).get(
            "alternate_landing_outcome", {}
        ),
        "rth_execution_request": (RTH_BEHAVIOR_SUMMARY or {}).get(
            "rth_execution_request", {}
        ),
        "rth_command_dispatch": (RTH_BEHAVIOR_SUMMARY or {}).get(
            "rth_command_dispatch", {}
        ),
        "rth_behavior_observation": (RTH_BEHAVIOR_SUMMARY or {}).get(
            "rth_behavior_observation", {}
        ),
        "rth_outcome": (RTH_BEHAVIOR_SUMMARY or {}).get("rth_outcome", {}),
        "operational_capability_matrix": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "operational_capability_matrix", {}
        ),
        "operational_application": (OPERATIONAL_REALISM_SUMMARY or {}).get(
            "operational_application", {}
        ),
        "observed_operational_condition_evidence": (
            OPERATIONAL_REALISM_SUMMARY or {}
        ).get("observed_operational_condition_evidence", {}),
        "telemetry_degradation_profile": (TELEMETRY_REALISM_SUMMARY or {}).get(
            "telemetry_degradation_profile", {}
        ),
        "telemetry_degradation_application": (TELEMETRY_REALISM_SUMMARY or {}).get(
            "telemetry_degradation_application", {}
        ),
        "observed_telemetry_gap_evidence": (TELEMETRY_REALISM_SUMMARY or {}).get(
            "observed_telemetry_gap_evidence", {}
        ),
        "telemetry_freshness_report": (TELEMETRY_REALISM_SUMMARY or {}).get(
            "telemetry_freshness_report", {}
        ),
        "mavlink_link_degradation_profile": (MAVLINK_LINK_REALISM_SUMMARY or {}).get(
            "mavlink_link_degradation_profile", {}
        ),
        "mavlink_link_degradation_capability_matrix": (
            MAVLINK_LINK_REALISM_SUMMARY or {}
        ).get("mavlink_link_degradation_capability_matrix", {}),
        "mavlink_link_degradation_application": (
            MAVLINK_LINK_REALISM_SUMMARY or {}
        ).get("mavlink_link_degradation_application", {}),
        "observed_mavlink_gap_evidence": (MAVLINK_LINK_REALISM_SUMMARY or {}).get(
            "observed_mavlink_gap_evidence", {}
        ),
        "terrain_world_readback": TERRAIN_WORLD_REALISM_SUMMARY or {},
    }


def _gazebo_route_corridor_obstacle_spawn_application_realism() -> dict[str, Any]:
    profile = (OPERATIONAL_REALISM_SUMMARY or {}).get("collision_obstacle_profile", {})
    application = (OPERATIONAL_REALISM_SUMMARY or {}).get("operational_application", {})
    evidence = (OPERATIONAL_REALISM_SUMMARY or {}).get(
        "observed_operational_condition_evidence", {}
    )
    requested = _collision_obstacle_requested()
    profile_obstacles = profile.get("obstacles") if isinstance(profile, dict) else []
    obstacle = (
        profile_obstacles[0]
        if isinstance(profile_obstacles, list) and profile_obstacles
        else {}
    )
    fallback_motion = _collision_obstacle_motion_spec()
    applied = application.get("applied") if isinstance(application, dict) else {}
    observed = evidence.get("observed") if isinstance(evidence, dict) else {}
    model_names = applied.get("model_names") if isinstance(applied, dict) else []
    applied_world_sdf_path = str(applied.get("world_sdf_path", ""))
    applied_world_sdf_sha256 = str(applied.get("world_sdf_sha256", ""))
    observed_world_sdf_sha256 = str(observed.get("world_sdf_sha256", ""))
    world_sdf_hash_match = bool(
        applied_world_sdf_path
        and applied_world_sdf_sha256
        and observed_world_sdf_sha256
        and applied_world_sdf_sha256 == observed_world_sdf_sha256
    )
    model_materialized = (
        requested
        and isinstance(model_names, list)
        and "mission_designer_collision_obstacle" in model_names
        and world_sdf_hash_match
        and observed.get("collision_obstacle_name")
        == "mission_designer_collision_obstacle"
        and bool(observed.get("collision_obstacle_collision_present"))
        and bool(observed.get("collision_obstacle_trajectory_follower_present"))
    )
    unsupported_reasons: list[str] = []
    if not requested:
        application_status = "not_requested"
    elif model_materialized:
        application_status = "applied"
    else:
        application_status = "unsupported"
        unsupported_reasons.append("gazebo_collision_obstacle_model_not_materialized")
        if not applied_world_sdf_path:
            unsupported_reasons.append("world_sdf_path_missing")
        if not world_sdf_hash_match:
            unsupported_reasons.append("world_sdf_hash_mismatch_or_missing")
    return {
        "gazebo_route_corridor_obstacle_spawn_application": {
            "schema_version": "gazebo_route_corridor_obstacle_spawn_application.v1",
            "application_id": (
                "gazebo_route_corridor_obstacle_spawn_application:"
                "mission_designer_collision_obstacle"
            ),
            "condition_kind": "gazebo_route_corridor_collision_obstacle_spawn",
            "application_status": application_status,
            "observation_status": application_status,
            "requested_present": requested,
            "requested": {
                "source": "mission_designer_coordinate_route",
                "obstacle_id": "mission_designer_collision_obstacle",
                "frame": "gazebo_world_local",
                "start_xy_m": obstacle.get("start_xy_m")
                or fallback_motion["start_xy_m"],
                "end_xy_m": obstacle.get("end_xy_m") or fallback_motion["end_xy_m"],
                "collision_enabled": requested,
                "trajectory_follower_requested": requested,
            },
            "applied": {
                "method": (
                    "gazebo_world_sdf_model_injection_before_sitl_start"
                    if model_materialized
                    else ""
                ),
                "world_sdf_path": applied_world_sdf_path,
                "world_sdf_sha256": applied_world_sdf_sha256,
                "model_name": (
                    "mission_designer_collision_obstacle" if model_materialized else ""
                ),
                "collision_name": (
                    "collision_obstacle_collision" if model_materialized else ""
                ),
                "trajectory_follower_plugin_enabled": bool(
                    observed.get("collision_obstacle_trajectory_follower_present")
                ),
                "contact_sensor_enabled": bool(
                    observed.get("collision_obstacle_contact_sensor_present")
                ),
                "contact_topic": observed.get("collision_obstacle_contact_topic", ""),
            },
            "observed": {
                "source": "gazebo_world_sdf",
                "observed": model_materialized,
                "world_sdf_hash_match": world_sdf_hash_match,
                "model_materialized": bool(
                    observed.get("collision_obstacle_name")
                    == "mission_designer_collision_obstacle"
                ),
                "collision_geometry_materialized": bool(
                    observed.get("collision_obstacle_collision_present")
                ),
                "trajectory_follower_materialized": bool(
                    observed.get("collision_obstacle_trajectory_follower_present")
                ),
                "world_sdf_sha256": observed_world_sdf_sha256,
                "route_blocking_verified": False,
                "traffic_conflict_verified": False,
                "incident_verified": False,
                "auto_gate": False,
                "task_status_mutated": False,
                "gate_status_mutated": False,
                "delivery_completion_claimed": False,
            },
            "unsupported_reasons": unsupported_reasons,
            "simulator_applicator": True,
            "verifier": False,
            "behavior_reactor": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def _route_corridor_obstacle_application_source_check(
    spawn_application: dict[str, Any],
) -> tuple[bool, list[str]]:
    observed = spawn_application.get("observed") or {}
    reasons: list[str] = []
    if (
        spawn_application.get("schema_version")
        != "gazebo_route_corridor_obstacle_spawn_application.v1"
    ):
        reasons.append("gazebo_route_corridor_obstacle_spawn_schema_missing")
    if (
        spawn_application.get("application_id")
        != "gazebo_route_corridor_obstacle_spawn_application:mission_designer_collision_obstacle"
    ):
        reasons.append("gazebo_route_corridor_obstacle_spawn_ref_missing")
    if spawn_application.get("application_status") != "applied":
        reasons.append("gazebo_route_corridor_obstacle_spawn_not_applied")
    if observed.get("observed") is not True:
        reasons.append("gazebo_route_corridor_obstacle_spawn_not_observed")
    if observed.get("world_sdf_hash_match") is not True:
        reasons.append("gazebo_route_corridor_obstacle_world_sdf_hash_not_verified")
    if observed.get("model_materialized") is not True:
        reasons.append("gazebo_route_corridor_obstacle_model_not_materialized")
    if observed.get("collision_geometry_materialized") is not True:
        reasons.append("gazebo_route_corridor_obstacle_collision_not_materialized")
    if observed.get("trajectory_follower_materialized") is not True:
        reasons.append("gazebo_route_corridor_obstacle_motion_not_materialized")
    return not reasons, reasons


def _telemetry_observer_dropout_realism() -> dict[str, Any]:
    mode = _telemetry_dropout_mode_request()
    requested_present = bool(mode)
    supported = mode == "observer_sample_pause"
    unsupported_reasons = (
        []
        if (not requested_present or supported)
        else ["telemetry_dropout_mode_not_supported"]
    )
    gap_events = list(TELEMETRY_DROPOUT_EVENTS)
    sample_events = list(TELEMETRY_OBSERVER_SAMPLE_EVENTS)
    gap_durations = [
        float(event.get("gap_duration_seconds") or 0.0) for event in gap_events
    ]
    max_gap_seconds = max(gap_durations) if gap_durations else 0.0
    gap_count = len(gap_events)
    first_pause_index = min(
        (
            int(event.get("sample_index"))
            for event in gap_events
            if event.get("sample_index") is not None
        ),
        default=None,
    )
    observed_sample_indexes = [
        int(event.get("sample_index"))
        for event in sample_events
        if event.get("event") == "observer_sample_observed"
        and event.get("sample_index") is not None
    ]
    baseline_observer_sample_observed = (
        requested_present
        and supported
        and first_pause_index is not None
        and any(index < first_pause_index for index in observed_sample_indexes)
    )
    observer_sample_pause_performed = (
        requested_present and supported and bool(gap_events)
    )
    observer_sample_gap_observed = observer_sample_pause_performed and gap_count > 0
    post_pause_observer_sample_observed = (
        requested_present
        and supported
        and first_pause_index is not None
        and any(index > first_pause_index for index in observed_sample_indexes)
    )
    observer_sample_pause_observed = (
        baseline_observer_sample_observed
        and observer_sample_pause_performed
        and observer_sample_gap_observed
        and post_pause_observer_sample_observed
    )
    if requested_present and supported:
        if not baseline_observer_sample_observed:
            unsupported_reasons.append(
                "telemetry_observer_baseline_sample_not_observed"
            )
        if not observer_sample_pause_performed:
            unsupported_reasons.append("telemetry_observer_sample_pause_not_performed")
        if not observer_sample_gap_observed:
            unsupported_reasons.append("telemetry_observer_sample_gap_not_observed")
        if not post_pause_observer_sample_observed:
            unsupported_reasons.append(
                "telemetry_observer_post_pause_sample_not_observed"
            )
    application_status = (
        "applied"
        if observer_sample_pause_observed
        else "unsupported" if requested_present else "not_requested"
    )
    observation_status = (
        "observer_sample_pause_gap_observed"
        if observer_sample_pause_observed
        else (
            "observer_sample_pause_gap_not_observed"
            if requested_present and supported
            else "unsupported" if requested_present else "not_requested"
        )
    )
    requested_condition_ref = (
        "telemetry_degradation_profile:mission_designer_observer_dropout"
    )
    application_ref = (
        "telemetry_degradation_application:mission_designer_observer_dropout"
    )
    profile = {
        "schema_version": "telemetry_degradation_profile.v1",
        "condition_id": requested_condition_ref,
        "condition_kind": "observer_side_telemetry_dropout",
        "requested": {
            "telemetry_dropout_mode": mode or None,
            "affected_streams": ["pose_samples"] if requested_present else [],
            "observer_side_only": True,
            "publisher_transport_loss_claimed": False,
            "vehicle_recovery_behavior_claimed": False,
            "mission_failure_claimed": False,
        },
        "requested_present": requested_present,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    application = {
        "schema_version": "telemetry_degradation_application.v1",
        "application_id": application_ref,
        "condition_kind": "observer_side_telemetry_dropout",
        "application_status": application_status,
        "requested_condition_ref": requested_condition_ref,
        "applied": (
            {
                "method": "mission_os_observer_pose_sample_pause",
                "mode": "observer_sample_pause",
                "affected_streams": ["pose_samples"],
                "gap_event_count": gap_count,
                "baseline_observer_sample_observed": baseline_observer_sample_observed,
                "observer_sample_pause_performed": observer_sample_pause_performed,
                "observer_sample_gap_observed": observer_sample_gap_observed,
                "post_pause_observer_sample_observed": post_pause_observer_sample_observed,
                "publisher_state_mutated": False,
                "mission_upload_path_mutated": False,
                "mission_progress_mutated": False,
                "publisher_transport_loss_claimed": False,
                "vehicle_recovery_behavior_claimed": False,
                "mission_failure_claimed": False,
                "px4_command_path_mutated": False,
                "gazebo_command_path_mutated": False,
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }
            if observer_sample_pause_observed
            else {}
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": (
            ["observer_sample_pause_only_consumer_side"]
            if requested_present and supported
            else []
        ),
        "observer_process_mutated": False,
        "publisher_state_mutated": False,
        "mission_upload_path_mutated": False,
        "mission_progress_mutated": False,
        "publisher_transport_loss_claimed": False,
        "vehicle_recovery_behavior_claimed": False,
        "mission_failure_claimed": False,
        "simulator_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_telemetry_gap_evidence.v1",
        "evidence_id": "observed_telemetry_gap_evidence:mission_designer_observer_dropout",
        "condition_kind": "observer_side_telemetry_dropout",
        "observation_status": observation_status,
        "requested_condition_ref": requested_condition_ref,
        "application_ref": application_ref,
        "observed": {
            "max_gap_seconds": max_gap_seconds,
            "gap_count": gap_count,
            "missing_sample_count": sum(
                int(event.get("missing_sample_count") or 0) for event in gap_events
            ),
            "affected_streams": ["pose_samples"] if requested_present else [],
            "gap_events": gap_events,
            "sample_events": sample_events,
            "baseline_observer_sample_observed": baseline_observer_sample_observed,
            "observer_sample_pause_performed": observer_sample_pause_performed,
            "observer_sample_gap_observed": observer_sample_gap_observed,
            "post_pause_observer_sample_observed": post_pause_observer_sample_observed,
            "publisher_state_mutated": False,
            "mission_upload_path_mutated": False,
            "mission_progress_mutated": False,
            "publisher_transport_loss_observed": False,
            "vehicle_recovery_behavior_observed": False,
            "mission_failure_claimed": False,
        },
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    freshness = {
        "schema_version": "telemetry_freshness_report.v1",
        "report_id": "telemetry_freshness_report:mission_designer_observer_dropout",
        "condition_kind": "observer_side_telemetry_dropout",
        "freshness_status": (
            "gap_observed"
            if gap_count
            else (
                "no_gap_observed"
                if requested_present and supported
                else "unsupported" if requested_present else "not_requested"
            )
        ),
        "max_gap_seconds": max_gap_seconds,
        "gap_count": gap_count,
        "affected_streams": ["pose_samples"] if requested_present else [],
        "stale_telemetry_is_not_current_telemetry": True,
        "observer_dropout_does_not_claim_publisher_transport_loss": True,
        "observer_dropout_does_not_fail_task_status": True,
        "delivery_completion_claimed": False,
    }
    return {
        "telemetry_degradation_profile": profile,
        "telemetry_degradation_application": application,
        "observed_telemetry_gap_evidence": evidence,
        "telemetry_freshness_report": freshness,
    }


def _mavlink_link_degradation_realism() -> dict[str, Any]:
    mode = _mavlink_link_degradation_mode_request()
    requested_present = bool(mode)
    known_request = mode in (
        "",
        "bounded_link_loss",
        "link_loss_probe",
        "heartbeat_observer",
    )
    unsupported_reasons: list[str] = []
    if requested_present and not known_request:
        unsupported_reasons.append("mavlink_link_degradation_mode_not_supported")
    elif mode == "link_loss_probe":
        unsupported_reasons.extend(
            [
                "safe_mavlink_link_loss_applicator_not_implemented",
                "observer_dropout_not_a_mavlink_link_loss_proxy",
            ]
        )
    heartbeat_observation: dict[str, Any] = {}
    link_loss_application: dict[str, Any] = {}
    if mode == "heartbeat_observer":
        heartbeat_observation = _observe_mavlink_heartbeat_gap()
    elif mode == "bounded_link_loss":
        link_loss_application = _apply_bounded_mavlink_link_loss()
    bounded_stop_restart_observed = (
        mode == "bounded_link_loss"
        and link_loss_application.get("applicator_status") == "completed"
        and link_loss_application.get("endpoint_stop_performed") is True
        and link_loss_application.get("endpoint_restart_performed") is True
    )
    bounded_baseline_observed = (
        mode == "bounded_link_loss"
        and link_loss_application.get("baseline_heartbeat_observed") is True
    )
    bounded_gap_observed = (
        mode == "bounded_link_loss"
        and link_loss_application.get("heartbeat_gap_observed") is True
    )
    bounded_restart_observed = (
        mode == "bounded_link_loss"
        and link_loss_application.get("post_restart_heartbeat_observed") is True
    )
    bounded_endpoint_interruption_observed = (
        bounded_stop_restart_observed
        and bounded_baseline_observed
        and bounded_gap_observed
        and bounded_restart_observed
    )
    if mode == "bounded_link_loss":
        if not bounded_stop_restart_observed:
            unsupported_reasons.append("mavlink_endpoint_stop_restart_not_observed")
        if not bounded_baseline_observed:
            unsupported_reasons.append("mavlink_baseline_heartbeat_not_observed")
        if not bounded_gap_observed:
            unsupported_reasons.append("mavlink_heartbeat_gap_not_observed")
        if not bounded_restart_observed:
            unsupported_reasons.append("mavlink_post_restart_heartbeat_not_observed")
    capability_status = (
        "supported_bounded_sitl_applicator"
        if mode == "bounded_link_loss" and bounded_endpoint_interruption_observed
        else (
            "supported_read_only_observer"
            if mode == "heartbeat_observer"
            else "unsupported" if requested_present else "not_requested"
        )
    )
    application_status = (
        "applied"
        if mode == "bounded_link_loss" and bounded_endpoint_interruption_observed
        else (
            "unsupported"
            if mode == "bounded_link_loss"
            else "observed" if mode == "heartbeat_observer" else capability_status
        )
    )
    observation_status = (
        "bounded_link_loss_gap_observed"
        if mode == "bounded_link_loss" and bounded_endpoint_interruption_observed
        else (
            "bounded_link_loss_unsupported"
            if mode == "bounded_link_loss"
            else (
                "heartbeat_gap_observed"
                if heartbeat_observation.get("heartbeat_gap_observed") is True
                else (
                    "heartbeat_observed_no_gap"
                    if mode == "heartbeat_observer"
                    and heartbeat_observation.get("heartbeat_count", 0) > 0
                    else (
                        "heartbeat_not_observed"
                        if mode == "heartbeat_observer"
                        else "unsupported" if requested_present else "not_requested"
                    )
                )
            )
        )
    )
    requested_condition_ref = (
        "mavlink_link_degradation_profile:mission_designer_link_probe"
    )
    application_ref = "mavlink_link_degradation_application:mission_designer_link_probe"
    profile = {
        "schema_version": "mavlink_link_degradation_profile.v1",
        "condition_id": requested_condition_ref,
        "condition_kind": "mavlink_link_degradation",
        "requested": {
            "mavlink_link_degradation_mode": mode or None,
            "requested_link_loss": mode in ("bounded_link_loss", "link_loss_probe"),
            "requested_bounded_link_loss": mode == "bounded_link_loss",
            "requested_heartbeat_observer": mode == "heartbeat_observer",
            "observer_side_dropout_requested": False,
        },
        "requested_present": requested_present,
        "source": "mission_designer_coordinate_route",
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    capability = {
        "schema_version": "mavlink_link_degradation_capability_matrix.v1",
        "capability_id": (
            "mavlink_link_degradation_capability_matrix:mission_designer_link_probe"
        ),
        "mavlink_link_loss": (
            "supported_bounded_sitl_applicator"
            if mode == "bounded_link_loss" and bounded_endpoint_interruption_observed
            else "unsupported" if requested_present else "not_requested"
        ),
        "heartbeat_gap_observer": (
            "supported_read_only_observer"
            if mode == "heartbeat_observer"
            else "not_requested"
        ),
        "support_detection_method": (
            "px4_mavlink_stop_restart_bounded_sitl"
            if mode == "bounded_link_loss"
            else (
                "read_only_udp_heartbeat_observer"
                if mode == "heartbeat_observer"
                else (
                    "mission_designer_allowlist_check_no_safe_link_loss_applicator"
                    if requested_present
                    else "not_requested"
                )
            )
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
    }
    application = {
        "schema_version": "mavlink_link_degradation_application.v1",
        "application_id": application_ref,
        "condition_kind": "mavlink_link_degradation",
        "application_status": application_status,
        "requested_condition_ref": requested_condition_ref,
        "applied": (
            {
                "method": "px4_mavlink_stop_restart_bounded_sitl",
                "scope": "all_px4_mavlink_instances_stop_restart",
                "stop_command": "px4-mavlink stop-all",
                "restart_scope": "route_and_emergency_mavlink_instances",
                "source": link_loss_application.get(
                    "source", f"udp://127.0.0.1:{ROUTE_MAVLINK_LOCAL_PORT}"
                ),
                "duration_seconds": link_loss_application.get("duration_seconds", 0.0),
                "gap_threshold_seconds": link_loss_application.get(
                    "gap_threshold_seconds", 0.0
                ),
                "endpoint_stop_performed": link_loss_application.get(
                    "endpoint_stop_performed"
                )
                is True,
                "endpoint_restart_performed": link_loss_application.get(
                    "endpoint_restart_performed"
                )
                is True,
                "emergency_endpoint_restart_requested": link_loss_application.get(
                    "emergency_endpoint_restart_requested"
                )
                is True,
                "baseline_heartbeat_observed": bounded_baseline_observed,
                "heartbeat_gap_observed": bounded_gap_observed,
                "post_restart_heartbeat_observed": bounded_restart_observed,
                "observer_sent_packets": False,
                "packet_drop_performed": False,
                "rf_link_loss_claimed": False,
                "vehicle_failsafe_claimed": False,
            }
            if mode == "bounded_link_loss"
            else (
                {
                    "method": "read_only_udp_heartbeat_observer",
                    "source": heartbeat_observation.get(
                        "source", "udp://127.0.0.1:14650"
                    ),
                    "duration_seconds": heartbeat_observation.get(
                        "duration_seconds", 0.0
                    ),
                    "gap_threshold_seconds": heartbeat_observation.get(
                        "gap_threshold_seconds", 0.0
                    ),
                    "observer_sent_packets": False,
                    "packet_drop_performed": False,
                }
                if mode == "heartbeat_observer"
                else {}
            )
        ),
        "unsupported_reasons": unsupported_reasons,
        "approximation_reasons": [],
        "simulator_only": True,
        "mavlink_link_loss_claimed": mode == "bounded_link_loss"
        and bounded_endpoint_interruption_observed,
        "bounded_sitl_endpoint_link_loss_claimed": mode == "bounded_link_loss"
        and bounded_endpoint_interruption_observed,
        "rf_link_loss_claimed": False,
        "heartbeat_gap_observer_requested": mode == "heartbeat_observer",
        "px4_command_path_mutated": mode == "bounded_link_loss",
        "px4_mavlink_endpoint_mutated": mode == "bounded_link_loss",
        "gazebo_command_path_mutated": False,
        "mission_upload_path_mutated": mode == "bounded_link_loss",
        "mission_upload_interruption_observed": False,
        "packet_drop_performed": False,
        "observer_dropout_used_as_proxy": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    evidence = {
        "schema_version": "observed_mavlink_gap_evidence.v1",
        "evidence_id": "observed_mavlink_gap_evidence:mission_designer_link_probe",
        "condition_kind": "mavlink_link_degradation",
        "observation_status": observation_status,
        "requested_condition_ref": requested_condition_ref,
        "application_ref": application_ref,
        "observed": {
            "mavlink_link_loss_observed": mode == "bounded_link_loss"
            and bounded_endpoint_interruption_observed,
            "bounded_sitl_endpoint_link_loss_observed": mode == "bounded_link_loss"
            and bounded_endpoint_interruption_observed,
            "rf_link_loss_observed": False,
            "heartbeat_observer_status": (
                heartbeat_observation.get("observer_status")
                if mode == "heartbeat_observer"
                else (
                    link_loss_application.get("applicator_status")
                    if mode == "bounded_link_loss"
                    else None
                )
            ),
            "heartbeat_count": int(
                (
                    heartbeat_observation
                    if mode == "heartbeat_observer"
                    else link_loss_application
                ).get("heartbeat_count")
                or 0
            ),
            "baseline_heartbeat_observed": bounded_baseline_observed,
            "warmup_heartbeat_count": int(
                link_loss_application.get("warmup_heartbeat_count") or 0
            ),
            "interruption_heartbeat_count": int(
                link_loss_application.get("interruption_heartbeat_count") or 0
            ),
            "post_restart_heartbeat_count": int(
                link_loss_application.get("post_restart_heartbeat_count") or 0
            ),
            "post_restart_heartbeat_observed": bounded_restart_observed,
            "heartbeat_gap_observed": (
                heartbeat_observation
                if mode == "heartbeat_observer"
                else link_loss_application
            ).get("heartbeat_gap_observed")
            is True,
            "heartbeat_gap_count": int(
                (
                    heartbeat_observation
                    if mode == "heartbeat_observer"
                    else link_loss_application
                ).get("heartbeat_gap_count")
                or 0
            ),
            "max_heartbeat_interval_seconds": float(
                (
                    heartbeat_observation
                    if mode == "heartbeat_observer"
                    else link_loss_application
                ).get("max_heartbeat_interval_seconds")
                or 0.0
            ),
            "gap_threshold_seconds": float(
                (
                    heartbeat_observation
                    if mode == "heartbeat_observer"
                    else link_loss_application
                ).get("gap_threshold_seconds")
                or 0.0
            ),
            "endpoint_stop_performed": link_loss_application.get(
                "endpoint_stop_performed"
            )
            is True,
            "endpoint_restart_performed": link_loss_application.get(
                "endpoint_restart_performed"
            )
            is True,
            "mission_upload_interruption_observed": False,
            "vehicle_failsafe_observed": False,
            "observer_dropout_used_as_proxy": False,
            "packet_drop_performed": False,
            "px4_mavlink_endpoint_mutated": mode == "bounded_link_loss",
            "source": (
                heartbeat_observation.get("source")
                if mode == "heartbeat_observer"
                else (
                    link_loss_application.get("source")
                    if mode == "bounded_link_loss"
                    else "support_detection_only"
                )
            ),
        },
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "delivery_completion_claimed": False,
    }
    return {
        "mavlink_link_degradation_profile": profile,
        "mavlink_link_degradation_capability_matrix": capability,
        "mavlink_link_degradation_application": application,
        "observed_mavlink_gap_evidence": evidence,
    }


def _listener_field(output: str, field: str) -> float | None:
    match = re.search(rf"\b{re.escape(field)}:\s*(-?\d+(?:\.\d+)?)", output)
    return float(match.group(1)) if match else None


def _listener_bool(output: str, field: str) -> bool | None:
    match = re.search(rf"\b{re.escape(field)}:\s*(True|False)", output)
    if not match:
        return None
    return match.group(1) == "True"


def _battery_status_sample() -> dict[str, Any]:
    global _LAST_BATTERY_STATUS_SAMPLE_AT
    global _LAST_BATTERY_STATUS_SAMPLE

    now = time.monotonic()
    if (
        _LAST_BATTERY_STATUS_SAMPLE_AT > 0
        and now - _LAST_BATTERY_STATUS_SAMPLE_AT
        < BATTERY_STATUS_SAMPLE_INTERVAL_SECONDS
    ):
        return dict(_LAST_BATTERY_STATUS_SAMPLE)

    try:
        result = _run(
            [
                "docker",
                "exec",
                CONTAINER_NAME,
                "/opt/px4-gazebo/bin/px4-listener",
                "battery_status",
                "1",
            ],
            check=False,
            timeout=BATTERY_STATUS_SAMPLE_TIMEOUT_SECONDS,
        )
    except Exception:
        _LAST_BATTERY_STATUS_SAMPLE_AT = now
        _LAST_BATTERY_STATUS_SAMPLE = {
            "battery_status_observed": False,
            "battery_state_source": "px4-listener:battery_status",
        }
        return dict(_LAST_BATTERY_STATUS_SAMPLE)
    output = (result.stdout + result.stderr).strip()
    remaining = _listener_field(output, "remaining")
    warning = _listener_field(output, "warning")
    observed = result.returncode == 0 and bool(output) and remaining is not None
    _LAST_BATTERY_STATUS_SAMPLE_AT = now
    _LAST_BATTERY_STATUS_SAMPLE = {
        "battery_status_observed": observed,
        "battery_state_source": "px4-listener:battery_status",
        "battery_remaining_percent": (
            round(float(remaining) * 100.0, 3) if remaining is not None else None
        ),
        "battery_warning": int(warning) if warning is not None else None,
        "battery_voltage_v": (
            round(float(voltage), 3)
            if (voltage := _listener_field(output, "voltage_v")) is not None
            else None
        ),
        "battery_current_a": (
            round(float(current), 3)
            if (current := _listener_field(output, "current_a")) is not None
            else None
        ),
        "battery_connected": _listener_bool(output, "connected"),
    }
    return dict(_LAST_BATTERY_STATUS_SAMPLE)


def _append_live_pose_row(
    phase: str,
    sample: dict[str, float],
    *,
    sample_index: int | None = None,
) -> None:
    if LIVE_POSE_TRACE_PATH is None:
        return
    telemetry_dropout_mode = _telemetry_dropout_mode_request()
    if (
        telemetry_dropout_mode == "observer_sample_pause"
        and phase == "route"
        and sample_index is not None
        and sample_index > 0
        and sample_index % 5 == 0
    ):
        gap_started_at = datetime.now(timezone.utc).isoformat()
        gap_event = {
            "phase": "telemetry_gap",
            "gap_reason": "observer_sample_pause",
            "gap_started_at": gap_started_at,
            "gap_duration_seconds": 2.0,
            "missing_sample_count": 1,
            "affected_streams": ["pose_samples"],
            "sample_index": sample_index,
            "publisher_state_mutated": False,
            "mission_upload_path_mutated": False,
            "mission_progress_mutated": False,
            "publisher_transport_loss_claimed": False,
            "vehicle_recovery_behavior_claimed": False,
            "mission_failure_claimed": False,
            "delivery_completion_claimed": False,
            "observer_side_only": True,
            "observed_at": gap_started_at,
        }
        TELEMETRY_DROPOUT_EVENTS.append(gap_event)
        TELEMETRY_OBSERVER_SAMPLE_EVENTS.append(
            {
                "event": "observer_sample_pause",
                "sample_index": sample_index,
                "observed_at": gap_started_at,
                "affected_streams": ["pose_samples"],
                "publisher_state_mutated": False,
                "mission_upload_path_mutated": False,
                "mission_progress_mutated": False,
            }
        )
        with LIVE_POSE_TRACE_PATH.open("a") as handle:
            handle.write(json.dumps(gap_event, sort_keys=True) + "\n")
        return
    row: dict[str, Any] = {
        "phase": phase,
        "sample": sample,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    battery_status = _battery_status_sample()
    if battery_status.get("battery_status_observed") is True:
        row["battery_status"] = battery_status
    if sample_index is not None:
        row["sample_index"] = sample_index
    if telemetry_dropout_mode == "observer_sample_pause" and phase == "route":
        TELEMETRY_OBSERVER_SAMPLE_EVENTS.append(
            {
                "event": "observer_sample_observed",
                "sample_index": sample_index,
                "observed_at": row["observed_at"],
                "affected_streams": ["pose_samples"],
                "publisher_state_mutated": False,
                "mission_upload_path_mutated": False,
                "mission_progress_mutated": False,
            }
        )
    with LIVE_POSE_TRACE_PATH.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _pose_rows(
    *,
    pickup_pose: dict[str, float],
    climb_samples: list[dict[str, float]],
    route_pose: dict[str, float],
    completed_pose: dict[str, float],
    landing_samples: list[dict[str, float]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [{"phase": "pickup", "sample": pickup_pose}]
    rows.extend(
        {"phase": "climb", "sample_index": index, "sample": sample}
        for index, sample in enumerate(climb_samples)
    )
    rows.append({"phase": "route", "sample": route_pose})
    rows.extend(
        {"phase": "landing", "sample_index": index, "sample": sample}
        for index, sample in enumerate(landing_samples)
    )
    rows.append({"phase": "completed", "sample": completed_pose})
    return rows


def _pose_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _terminal_pose_record(
    *,
    phase: str,
    pose: Mapping[str, Any] | None,
    sample_index: int | None = None,
    progress_m: float | None = None,
    source: str = "gazebo_pose_sample",
) -> dict[str, Any]:
    observed = bool(pose)
    pose_data = pose or {}
    return {
        "schema_version": "missionos_terminal_pose.v1",
        "phase": phase,
        "observed": observed,
        "source": source if observed else "",
        "sample_index": sample_index,
        "x_m": _pose_float(
            pose_data.get("x", pose_data.get("x_m", pose_data.get("local_x_m")))
        ),
        "y_m": _pose_float(
            pose_data.get("y", pose_data.get("y_m", pose_data.get("local_y_m")))
        ),
        "z_m": _pose_float(
            pose_data.get("z", pose_data.get("z_m", pose_data.get("local_z_m")))
        ),
        "progress_m": progress_m,
    }


def _terminal_pose_summary_fields(
    *,
    route_pose: Mapping[str, Any] | None,
    completed_pose: Mapping[str, Any] | None,
    landing_samples: list[dict[str, float]],
    route_terminal_progress_m: float | None,
    route_terminal_local_ned_pose: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    landing_pose = landing_samples[-1] if landing_samples else None
    return {
        "route_terminal_pose": _terminal_pose_record(
            phase="route",
            pose=route_pose,
            progress_m=route_terminal_progress_m,
        ),
        "route_terminal_local_ned_pose": _terminal_pose_record(
            phase="route",
            pose=route_terminal_local_ned_pose,
            progress_m=route_terminal_progress_m,
            source="px4_local_position_ned",
        ),
        "route_terminal_progress_m": route_terminal_progress_m,
        "landing_terminal_pose": _terminal_pose_record(
            phase="landing",
            pose=landing_pose,
            sample_index=(len(landing_samples) - 1 if landing_samples else None),
            progress_m=route_terminal_progress_m,
        ),
        "completed_terminal_pose": _terminal_pose_record(
            phase="completed",
            pose=completed_pose,
            progress_m=route_terminal_progress_m,
        ),
    }


def _wait_for_startup(timeout: float = 90.0) -> None:
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
    raise RuntimeError("timed out waiting for PX4/Gazebo horizontal route startup")


def _wait_for_px4_home(timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "home set" in _logs():
            return
        time.sleep(1)
    raise RuntimeError("timed out waiting for PX4 home set")


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


def _payload_pose_sample() -> dict[str, float]:
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
    pose = parse_gz_sim_entity_pose(sample, entity_name="delivery_payload")
    return {key: float(pose[key]) for key in ("x", "y", "z")}


def _moving_actor_pose_sample() -> dict[str, float]:
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
    pose = parse_gz_sim_entity_pose(
        sample,
        entity_name="mission_designer_moving_actor_marker",
    )
    return {key: float(pose[key]) for key in ("x", "y", "z")}


def _moving_actor_waypoint_motion_application_realism() -> dict[str, Any]:
    requested = _moving_actor_marker_requested()
    spec = _moving_actor_waypoint_motion_spec()
    trajectory_sha256 = _moving_actor_waypoint_trajectory_definition_sha256()
    operational_application = (OPERATIONAL_REALISM_SUMMARY or {}).get(
        "operational_application",
        {},
    )
    operational_applied = operational_application.get("applied") or {}
    unsupported_reasons: list[str] = []
    applied: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    application_status = "not_requested"
    if requested:
        world_path_text = str(operational_applied.get("world_sdf_path") or "")
        world_path = Path(world_path_text) if world_path_text else None
        if (
            operational_application.get("application_status")
            != "applied_with_approximations"
        ):
            unsupported_reasons.append(
                "moving_actor_operational_application_not_applied"
            )
        if world_path is None or not world_path.exists():
            unsupported_reasons.append("moving_actor_world_sdf_missing")
        if unsupported_reasons:
            application_status = "unsupported"
            observed = {
                "source": "moving_actor_operational_application",
                "observed": False,
                "moving_actor_present": False,
                "moving_actor_trajectory_materialized": False,
                "moving_actor_pose_stream_observed": False,
                "moving_actor_velocity_readback_observed": False,
                "telemetry_publisher_state_mutated": False,
                "mission_upload_path_mutated": False,
                "mission_progress_mutated": False,
                "incident_verified": False,
                "route_blocking_verified": False,
                "traffic_conflict_verified": False,
                "collision_obstacle_observed": False,
                "task_status_mutated": False,
                "gate_status_mutated": False,
                "delivery_completion_claimed": False,
            }
        else:
            assert world_path is not None
            world_text = world_path.read_text(encoding="utf-8")
            world_sha256 = hashlib.sha256(world_text.encode("utf-8")).hexdigest()
            expected_world_sha256 = str(
                operational_applied.get("world_sdf_sha256") or ""
            )
            world_sdf_hash_match = bool(
                expected_world_sha256 and world_sha256 == expected_world_sha256
            )
            actor_present = False
            visual_present = False
            trajectory_follower_present = False
            geometry_present = False
            waypoints: list[list[float]] = []
            try:
                root = ET.fromstring(world_text)
                for model in root.iter("model"):
                    if model.attrib.get("name") != spec["actor_id"]:
                        continue
                    actor_present = True
                    visual_present = model.find(".//visual") is not None
                    geometry_present = model.find(".//visual/geometry") is not None
                    trajectory_follower_present = (
                        model.find(
                            ".//plugin[@name='gz::sim::systems::TrajectoryFollower']"
                        )
                        is not None
                    )
                    for waypoint in model.findall(".//waypoints/waypoint"):
                        parts = (waypoint.text or "").split()
                        if len(parts) >= 2:
                            waypoints.append([float(parts[0]), float(parts[1])])
            except Exception as exc:
                unsupported_reasons.append("moving_actor_world_sdf_parse_failed")
                observed = {"error": str(exc)[-500:]}
            expected_waypoints = [spec["start_xy_m"], spec["end_xy_m"]]
            trajectory_materialized = (
                actor_present
                and visual_present
                and geometry_present
                and trajectory_follower_present
                and len(waypoints) >= 2
                and all(
                    math.isclose(
                        float(waypoints[index][axis]),
                        float(expected_waypoints[index][axis]),
                        abs_tol=1e-6,
                    )
                    for index in range(2)
                    for axis in range(2)
                )
            )
            if not world_sdf_hash_match:
                unsupported_reasons.append("moving_actor_world_sdf_hash_mismatch")
            if not trajectory_materialized:
                unsupported_reasons.append("moving_actor_trajectory_not_materialized")
            if unsupported_reasons:
                application_status = "unsupported"
                observed = {
                    **observed,
                    "source": "gazebo_world_sdf",
                    "observed": False,
                    "moving_actor_present": actor_present,
                    "moving_actor_trajectory_materialized": trajectory_materialized,
                    "moving_actor_pose_stream_observed": False,
                    "moving_actor_velocity_readback_observed": False,
                    "world_sdf_sha256": world_sha256,
                    "expected_world_sdf_sha256": expected_world_sha256,
                    "world_sdf_hash_match": world_sdf_hash_match,
                    "waypoints_observed_xy_m": waypoints,
                    "telemetry_publisher_state_mutated": False,
                    "mission_upload_path_mutated": False,
                    "mission_progress_mutated": False,
                    "incident_verified": False,
                    "route_blocking_verified": False,
                    "traffic_conflict_verified": False,
                    "collision_obstacle_observed": False,
                    "task_status_mutated": False,
                    "gate_status_mutated": False,
                    "delivery_completion_claimed": False,
                }
            else:
                sample_interval_seconds = 2.0
                try:
                    first = _moving_actor_pose_sample()
                    time.sleep(sample_interval_seconds)
                    second = _moving_actor_pose_sample()
                    displacement_xy_m = math.hypot(
                        float(second["x"]) - float(first["x"]),
                        float(second["y"]) - float(first["y"]),
                    )
                    observed_velocity_mps = displacement_xy_m / sample_interval_seconds
                    nominal_velocity_mps = float(spec["nominal_profile_velocity_mps"])
                    motion_observed = displacement_xy_m >= 0.25
                    if not motion_observed:
                        unsupported_reasons.append(
                            "moving_actor_pose_stream_not_observed"
                        )
                    application_status = (
                        "applied_with_approximations"
                        if motion_observed
                        else "unsupported"
                    )
                    applied = {
                        "method": "gazebo_world_sdf_waypoint_motion_actor",
                        "target": "gazebo_world_sdf",
                        "world_sdf_path": str(world_path),
                        "world_sdf_sha256": world_sha256,
                        "trajectory_definition_sha256": trajectory_sha256,
                        "source": "mission_designer_moving_actor_marker",
                        "actor_id": spec["actor_id"],
                        "frame": spec["frame"],
                        "mode": spec["mode"],
                        "start_xy_m": spec["start_xy_m"],
                        "end_xy_m": spec["end_xy_m"],
                        "nominal_profile_velocity_mps": nominal_velocity_mps,
                        "velocity_target_materialized": False,
                        "sample_interval_seconds": sample_interval_seconds,
                        "applied_at": datetime.now(timezone.utc).isoformat(),
                    }
                    observed = {
                        "source": "gz_topic_pose_info_read_only",
                        "topic": "/world/default/pose/info",
                        "observed": motion_observed,
                        "moving_actor_present": actor_present,
                        "moving_actor_trajectory_materialized": trajectory_materialized,
                        "moving_actor_pose_stream_observed": bool(
                            displacement_xy_m >= 0.25
                        ),
                        "moving_actor_velocity_readback_observed": motion_observed,
                        "world_sdf_sha256": world_sha256,
                        "expected_world_sdf_sha256": expected_world_sha256,
                        "world_sdf_hash_match": world_sdf_hash_match,
                        "trajectory_definition_sha256": trajectory_sha256,
                        "waypoints_observed_xy_m": waypoints,
                        "first_pose_xyz_m": [first["x"], first["y"], first["z"]],
                        "second_pose_xyz_m": [second["x"], second["y"], second["z"]],
                        "sample_interval_seconds": sample_interval_seconds,
                        "displacement_xy_m": displacement_xy_m,
                        "nominal_profile_velocity_mps": nominal_velocity_mps,
                        "velocity_target_materialized": False,
                        "observed_velocity_mps": observed_velocity_mps,
                        "velocity_formula": "xy_displacement_m / sample_interval_seconds",
                        "telemetry_publisher_state_mutated": False,
                        "mission_upload_path_mutated": False,
                        "mission_progress_mutated": False,
                        "incident_verified": False,
                        "route_blocking_verified": False,
                        "traffic_conflict_verified": False,
                        "collision_obstacle_observed": False,
                        "task_status_mutated": False,
                        "gate_status_mutated": False,
                        "delivery_completion_claimed": False,
                    }
                except Exception as exc:
                    application_status = "unsupported"
                    unsupported_reasons.append("moving_actor_pose_stream_not_observed")
                    observed = {
                        "source": "gz_topic_pose_info_read_only",
                        "topic": "/world/default/pose/info",
                        "observed": False,
                        "moving_actor_present": actor_present,
                        "moving_actor_trajectory_materialized": trajectory_materialized,
                        "moving_actor_pose_stream_observed": False,
                        "moving_actor_velocity_readback_observed": False,
                        "error": str(exc)[-500:],
                        "world_sdf_sha256": world_sha256,
                        "expected_world_sdf_sha256": expected_world_sha256,
                        "world_sdf_hash_match": world_sdf_hash_match,
                        "trajectory_definition_sha256": trajectory_sha256,
                        "telemetry_publisher_state_mutated": False,
                        "mission_upload_path_mutated": False,
                        "mission_progress_mutated": False,
                        "incident_verified": False,
                        "route_blocking_verified": False,
                        "traffic_conflict_verified": False,
                        "collision_obstacle_observed": False,
                        "task_status_mutated": False,
                        "gate_status_mutated": False,
                        "delivery_completion_claimed": False,
                    }
    return {
        "moving_actor_waypoint_motion_application": {
            "schema_version": "moving_actor_waypoint_motion_application.v1",
            "application_id": (
                "moving_actor_waypoint_motion_application:"
                "mission_designer_moving_actor_marker"
            ),
            "condition_kind": "moving_actor_linear_waypoint_motion",
            "application_status": application_status,
            "requested": {
                "requested_present": requested,
                "mode": spec["mode"],
                "actor_id": spec["actor_id"],
                "frame": spec["frame"],
                "start_xy_m": spec["start_xy_m"],
                "end_xy_m": spec["end_xy_m"],
                "nominal_profile_velocity_mps": spec["nominal_profile_velocity_mps"],
            },
            "applied": applied,
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "approximation_reasons": (
                [
                    "moving_actor_velocity_target_not_materialized_as_guaranteed_runtime_speed"
                ]
                if application_status == "applied_with_approximations"
                else []
            ),
            "simulator_only": True,
            "verifier": False,
            "candidate": False,
            "approval_chain": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def _collision_obstacle_pose_sample() -> dict[str, float]:
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
    pose = parse_gz_sim_entity_pose(
        sample,
        entity_name="mission_designer_collision_obstacle",
    )
    return {key: float(pose[key]) for key in ("x", "y", "z")}


def _collision_obstacle_contact_topic_observation() -> dict[str, Any]:
    topic_list = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            "timeout 5 gz topic -l",
        ],
        check=False,
        timeout=10,
    ).stdout
    topic_lines = [line.strip() for line in topic_list.splitlines() if line.strip()]
    contact_topic_candidates = [
        line
        for line in topic_lines
        if line == COLLISION_OBSTACLE_CONTACT_TOPIC
        or ("mission_designer_collision_obstacle" in line and "contact" in line.lower())
        or "collision_obstacle_contact_sensor" in line
    ]
    selected_topic = (
        COLLISION_OBSTACLE_CONTACT_TOPIC
        if COLLISION_OBSTACLE_CONTACT_TOPIC in contact_topic_candidates
        else (
            contact_topic_candidates[0]
            if contact_topic_candidates
            else COLLISION_OBSTACLE_CONTACT_TOPIC
        )
    )
    topic_advertised = bool(contact_topic_candidates)
    sample_result = _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            ("timeout 2 gz topic -e " f"-t {shlex.quote(selected_topic)} " "-n 1"),
        ],
        check=False,
        timeout=5,
    )
    sample_text = sample_result.stdout.strip()
    contact_event_observed = bool(sample_text)
    return {
        "source": "gz_topic_contact_sensor_read_only",
        "topic": selected_topic,
        "configured_topic": COLLISION_OBSTACLE_CONTACT_TOPIC,
        "candidate_topics": contact_topic_candidates,
        "topic_advertised": topic_advertised,
        "contact_topic_observed": topic_advertised,
        "contact_event_observed": contact_event_observed,
        "contact_sample_observed": contact_event_observed,
        "contact_sample_stdout_tail": sample_text[-500:],
        "contact_sample_returncode": sample_result.returncode,
        "read_only_observer": True,
        "route_blocking_observed": False,
        "incident_observed": False,
        "traffic_conflict_verified": False,
        "task_status_mutated": False,
        "delivery_completion_claimed": False,
    }


def _moving_actor_pose_observation_realism() -> dict[str, Any]:
    requested = _moving_actor_marker_requested()
    observed: dict[str, Any] = {}
    observation_status = "not_requested"
    unsupported_reasons: list[str] = []
    if requested:
        try:
            first = _moving_actor_pose_sample()
            time.sleep(2.0)
            second = _moving_actor_pose_sample()
            displacement_xy_m = math.hypot(
                float(second["x"]) - float(first["x"]),
                float(second["y"]) - float(first["y"]),
            )
            z_range_m = abs(float(second["z"]) - float(first["z"]))
            marker_altitude_reasonable = (
                max(abs(float(first["z"])), abs(float(second["z"]))) <= 5.0
            )
            pose_motion_observed = displacement_xy_m >= 0.25
            observation_status = (
                "pose_motion_observed"
                if pose_motion_observed and marker_altitude_reasonable
                else (
                    "pose_motion_observed_unbounded_altitude"
                    if pose_motion_observed
                    else "pose_sample_observed_without_motion"
                )
            )
            observed = {
                "source": "gz_topic_pose_info_read_only",
                "topic": "/world/default/pose/info",
                "entity_name": "mission_designer_moving_actor_marker",
                "sample_count": 2,
                "first_pose_xyz_m": [first["x"], first["y"], first["z"]],
                "second_pose_xyz_m": [second["x"], second["y"], second["z"]],
                "displacement_xy_m": displacement_xy_m,
                "z_range_m": z_range_m,
                "marker_altitude_reasonable": marker_altitude_reasonable,
                "pose_motion_observed": pose_motion_observed,
                "read_only_observer": True,
                "collision_observed": False,
                "sensor_evidence_observed": False,
                "incident_observed": False,
                "route_blocking_observed": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            }
        except Exception as exc:
            observation_status = "pose_not_observed"
            unsupported_reasons.append("moving_actor_pose_not_observed")
            observed = {
                "source": "gz_topic_pose_info_read_only",
                "topic": "/world/default/pose/info",
                "entity_name": "mission_designer_moving_actor_marker",
                "observed": False,
                "error": str(exc)[-500:],
                "read_only_observer": True,
                "collision_observed": False,
                "sensor_evidence_observed": False,
                "incident_observed": False,
                "route_blocking_observed": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            }
    return {
        "moving_actor_pose_observation": {
            "schema_version": "moving_actor_pose_observation.v1",
            "observation_id": (
                "moving_actor_pose_observation:" "mission_designer_moving_visual_marker"
            ),
            "condition_kind": "moving_visual_actor_marker",
            "observation_status": observation_status,
            "requested_condition_ref": (
                "operational_condition_profile:" "mission_designer_operational_markers"
            ),
            "dynamic_actor_ref": (
                "dynamic_actor_profile:mission_designer_moving_visual_marker"
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "observer_only": True,
            "simulator_only": True,
            "collision_enabled": False,
            "sensor_visible_claimed": False,
            "incident_claimed": False,
            "route_blocking_enabled": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def _collision_obstacle_sdf_placement_readback(
    world_sdf_path: str,
) -> dict[str, Any]:
    if not world_sdf_path:
        return {"observed": False, "error": "world_sdf_path_missing"}
    path = Path(world_sdf_path)
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"observed": False, "error": str(exc)[-500:]}
    model = None
    for candidate in root.iter("model"):
        if candidate.attrib.get("name") == "mission_designer_collision_obstacle":
            model = candidate
            break
    if model is None:
        return {"observed": False, "error": "collision_obstacle_model_missing"}
    pose_xy = None
    pose_text = (model.findtext("pose") or "").strip()
    if pose_text:
        try:
            parts = [float(part) for part in pose_text.split()]
            if len(parts) >= 2 and math.isfinite(parts[0]) and math.isfinite(parts[1]):
                pose_xy = [parts[0], parts[1]]
        except ValueError:
            pose_xy = None
    waypoints: list[list[float]] = []
    for waypoint in model.iter("waypoint"):
        text = (waypoint.text or "").strip()
        try:
            parts = [float(part) for part in text.split()]
        except ValueError:
            continue
        if len(parts) >= 2 and math.isfinite(parts[0]) and math.isfinite(parts[1]):
            waypoints.append([parts[0], parts[1]])
    return {
        "observed": pose_xy is not None and len(waypoints) >= 2,
        "pose_start_xy_m": pose_xy,
        "waypoint_start_xy_m": waypoints[0] if waypoints else None,
        "waypoint_end_xy_m": waypoints[1] if len(waypoints) >= 2 else None,
        "waypoint_count": len(waypoints),
    }


def _xy_pairs_match(
    first: list[float] | None,
    second: list[float] | None,
    *,
    tolerance: float = 1e-6,
) -> bool:
    if first is None or second is None or len(first) != 2 or len(second) != 2:
        return False
    return all(
        abs(float(first[index]) - float(second[index])) <= tolerance
        for index in range(2)
    )


def _collision_obstacle_evidence_realism(
    *,
    route_start_xy_m: tuple[float, float],
    route_dropoff_xy_m: tuple[float, float],
) -> dict[str, Any]:
    requested = _collision_obstacle_requested()
    obstacle_profile = (OPERATIONAL_REALISM_SUMMARY or {}).get(
        "collision_obstacle_profile",
        {},
    )
    obstacle = (
        (obstacle_profile.get("obstacles") or [{}])[0]
        if isinstance(obstacle_profile.get("obstacles"), list)
        and obstacle_profile.get("obstacles")
        else {}
    )
    spawn_application = _gazebo_route_corridor_obstacle_spawn_application_realism().get(
        "gazebo_route_corridor_obstacle_spawn_application",
        {},
    )
    spawn_observed = spawn_application.get("observed") or {}
    simulator_condition_applied, spawn_source_fail_reasons = (
        _route_corridor_obstacle_application_source_check(spawn_application)
    )
    unsupported_reasons: list[str] = []
    if not requested:
        observation_status = "not_requested"
        observed: dict[str, Any] = {}
    elif not simulator_condition_applied:
        observation_status = "collision_obstacle_not_materialized"
        unsupported_reasons.append("gazebo_route_corridor_obstacle_spawn_not_applied")
        unsupported_reasons.extend(spawn_source_fail_reasons)
        observed = {
            "source": "gazebo_route_corridor_obstacle_spawn_application",
            "spawn_application_ref": spawn_application.get("application_id", ""),
            "source_condition_application_verified": False,
            "world_sdf_hash_match": bool(spawn_observed.get("world_sdf_hash_match")),
            "observed": False,
            "simulator_condition_applied": False,
            "collision_geometry_observed": False,
            "route_blocking_observed": False,
            "incident_observed": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    elif not obstacle.get("collision_enabled"):
        observation_status = "collision_obstacle_not_materialized"
        unsupported_reasons.append("collision_geometry_not_materialized")
        observed = {
            "source": "gazebo_route_corridor_obstacle_spawn_application",
            "spawn_application_ref": spawn_application.get("application_id", ""),
            "source_condition_application_verified": False,
            "world_sdf_hash_match": bool(spawn_observed.get("world_sdf_hash_match")),
            "observed": False,
            "simulator_condition_applied": simulator_condition_applied,
            "collision_geometry_observed": False,
            "route_blocking_observed": False,
            "incident_observed": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        try:
            first = _collision_obstacle_pose_sample()
            time.sleep(2.0)
            second = _collision_obstacle_pose_sample()
            xy_samples = [
                [float(first["x"]), float(first["y"])],
                [float(second["x"]), float(second["y"])],
            ]
            fallback_motion = _collision_obstacle_motion_spec()
            configured_start_xy = (
                obstacle.get("start_xy_m") or fallback_motion["start_xy_m"]
            )
            configured_end_xy = obstacle.get("end_xy_m") or fallback_motion["end_xy_m"]
            configured_xy_samples = [
                [
                    float(configured_start_xy[0]),
                    float(configured_start_xy[1]),
                ],
                [
                    float(configured_end_xy[0]),
                    float(configured_end_xy[1]),
                ],
            ]
            sdf_readback = _collision_obstacle_sdf_placement_readback(
                str((spawn_application.get("applied") or {}).get("world_sdf_path", ""))
            )
            sdf_placement_matches_configured = (
                sdf_readback.get("observed") is True
                and _xy_pairs_match(
                    sdf_readback.get("pose_start_xy_m"),
                    configured_xy_samples[0],
                )
                and _xy_pairs_match(
                    sdf_readback.get("waypoint_start_xy_m"),
                    configured_xy_samples[0],
                )
                and _xy_pairs_match(
                    sdf_readback.get("waypoint_end_xy_m"),
                    configured_xy_samples[1],
                )
            )
            if not sdf_placement_matches_configured:
                raise RuntimeError("collision_obstacle_sdf_placement_not_source_bound")
            runtime_route_distances = [
                _point_to_segment_distance_m(
                    (sample[0], sample[1]),
                    route_start_xy_m,
                    route_dropoff_xy_m,
                )
                for sample in xy_samples
            ]
            configured_route_distances = [
                _point_to_segment_distance_m(
                    (sample[0], sample[1]),
                    route_start_xy_m,
                    route_dropoff_xy_m,
                )
                for sample in configured_xy_samples
            ]
            dropoff_distances = [
                math.hypot(
                    sample[0] - route_dropoff_xy_m[0],
                    sample[1] - route_dropoff_xy_m[1],
                )
                for sample in xy_samples
            ]
            displacement_xy_m = math.hypot(
                float(second["x"]) - float(first["x"]),
                float(second["y"]) - float(first["y"]),
            )
            contact_observation = _collision_obstacle_contact_topic_observation()
            observation_status = "collision_obstacle_evidence_observed"
            observed = {
                "source": (
                    "gazebo_route_corridor_obstacle_spawn_application_and_gz_topic_pose_info"
                ),
                "spawn_application_ref": spawn_application.get("application_id", ""),
                "source_condition_application_verified": True,
                "world_sdf_hash_match": True,
                "simulator_condition_applied": simulator_condition_applied,
                "topic": "/world/default/pose/info",
                "entity_name": "mission_designer_collision_obstacle",
                "sample_count": 2,
                "first_pose_xyz_m": [first["x"], first["y"], first["z"]],
                "second_pose_xyz_m": [second["x"], second["y"], second["z"]],
                "displacement_xy_m": displacement_xy_m,
                "collision_geometry_observed": True,
                "trajectory_follower_observed": bool(
                    obstacle.get("trajectory_follower_plugin_enabled")
                ),
                "pose_observed": True,
                "actor_xy_samples_m": xy_samples,
                "configured_xy_samples_m": configured_xy_samples,
                "sdf_placement_readback_observed": bool(sdf_readback.get("observed")),
                "sdf_pose_start_xy_m": sdf_readback.get("pose_start_xy_m"),
                "sdf_waypoint_start_xy_m": sdf_readback.get("waypoint_start_xy_m"),
                "sdf_waypoint_end_xy_m": sdf_readback.get("waypoint_end_xy_m"),
                "sdf_waypoint_count": sdf_readback.get("waypoint_count"),
                "sdf_placement_matches_configured": sdf_placement_matches_configured,
                "route_start_xy_m": list(route_start_xy_m),
                "route_dropoff_xy_m": list(route_dropoff_xy_m),
                "runtime_min_distance_to_route_m": min(runtime_route_distances),
                "configured_min_distance_to_route_m": min(configured_route_distances),
                "min_distance_to_route_m": min(
                    runtime_route_distances + configured_route_distances
                ),
                "min_distance_to_dropoff_m": min(dropoff_distances),
                "contact_topic": COLLISION_OBSTACLE_CONTACT_TOPIC,
                "contact_topic_runtime": contact_observation.get("topic"),
                "contact_topic_candidates": contact_observation.get(
                    "candidate_topics", []
                ),
                "contact_topic_observed": bool(
                    contact_observation.get("contact_topic_observed")
                ),
                "contact_topic_advertised": bool(
                    contact_observation.get("topic_advertised")
                ),
                "contact_event_observed": bool(
                    contact_observation.get("contact_event_observed")
                ),
                "contact_observation_source": contact_observation.get("source"),
                "contact_sample_returncode": contact_observation.get(
                    "contact_sample_returncode"
                ),
                "route_blocking_candidate": False,
                "route_blocking_observed": False,
                "incident_observed": False,
                "traffic_conflict_verified": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            }
        except Exception as exc:
            observation_status = "collision_obstacle_pose_not_observed"
            unsupported_reasons.append("collision_obstacle_pose_not_observed")
            observed = {
                "source": (
                    "gazebo_route_corridor_obstacle_spawn_application_and_gz_topic_pose_info"
                ),
                "spawn_application_ref": spawn_application.get("application_id", ""),
                "source_condition_application_verified": True,
                "world_sdf_hash_match": True,
                "simulator_condition_applied": simulator_condition_applied,
                "topic": "/world/default/pose/info",
                "entity_name": "mission_designer_collision_obstacle",
                "observed": False,
                "error": str(exc)[-500:],
                "collision_geometry_observed": True,
                "route_blocking_observed": False,
                "incident_observed": False,
                "traffic_conflict_verified": False,
                "task_status_mutated": False,
                "delivery_completion_claimed": False,
            }
    return {
        "collision_obstacle_evidence": {
            "schema_version": "collision_obstacle_evidence.v1",
            "evidence_id": (
                "collision_obstacle_evidence:"
                "mission_designer_collision_enabled_obstacle"
            ),
            "condition_kind": "collision_enabled_moving_obstacle",
            "observation_status": observation_status,
            "collision_obstacle_ref": (
                "collision_obstacle_profile:" "mission_designer_collision_obstacle"
            ),
            "gazebo_route_corridor_obstacle_spawn_application_ref": (
                spawn_application.get("application_id", "")
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "simulator_only": True,
            "collision_enabled": requested,
            "sensor_visible_claimed": False,
            "route_blocking_enabled": False,
            "incident_claimed": False,
            "traffic_conflict_verifier": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def _route_blocking_candidate_evidence_realism() -> dict[str, Any]:
    collision_evidence = (COLLISION_OBSTACLE_SUMMARY or {}).get(
        "collision_obstacle_evidence",
        {},
    )
    collision_observed = collision_evidence.get("observed") or {}
    spawn_application = _gazebo_route_corridor_obstacle_spawn_application_realism().get(
        "gazebo_route_corridor_obstacle_spawn_application",
        {},
    )
    spawn_application_verified, spawn_source_fail_reasons = (
        _route_corridor_obstacle_application_source_check(spawn_application)
    )
    requested = _collision_obstacle_requested()
    candidate_threshold_m = 1.25
    unsupported_reasons: list[str] = []
    if not requested:
        observation_status = "not_requested"
        observed: dict[str, Any] = {}
    elif (
        collision_evidence.get("observation_status")
        != "collision_obstacle_evidence_observed"
    ):
        observation_status = "route_blocking_candidate_not_observed"
        unsupported_reasons.append("collision_obstacle_evidence_missing")
        observed = {
            "source": "collision_obstacle_evidence",
            "observed": False,
            "route_blocking_candidate": False,
            "route_blocking_verified": False,
            "incident_report_created": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    elif not spawn_application_verified:
        observation_status = "route_blocking_candidate_not_observed"
        unsupported_reasons.extend(spawn_source_fail_reasons)
        observed = {
            "source": "gazebo_route_corridor_obstacle_spawn_application",
            "observed": False,
            "source_condition_application_ref": spawn_application.get(
                "application_id", ""
            ),
            "source_condition_application_verified": False,
            "world_sdf_hash_match": bool(
                (spawn_application.get("observed") or {}).get("world_sdf_hash_match")
            ),
            "simulator_condition_applied": False,
            "route_blocking_candidate": False,
            "route_blocking_verified": False,
            "incident_report_created": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        min_distance = collision_observed.get("min_distance_to_route_m")
        route_blocking_candidate = (
            isinstance(min_distance, (int, float))
            and float(min_distance) <= candidate_threshold_m
        )
        observation_status = (
            "route_blocking_candidate_observed"
            if route_blocking_candidate
            else "route_clear_candidate_observed"
        )
        observed = {
            "source": "collision_obstacle_evidence",
            "gazebo_route_corridor_obstacle_spawn_application_ref": (
                collision_evidence.get(
                    "gazebo_route_corridor_obstacle_spawn_application_ref", ""
                )
            ),
            "source_condition_application_ref": spawn_application.get(
                "application_id", ""
            ),
            "source_condition_application_verified": True,
            "world_sdf_hash_match": bool(
                (spawn_application.get("observed") or {}).get("world_sdf_hash_match")
            ),
            "simulator_condition_applied": bool(
                collision_observed.get("simulator_condition_applied")
            ),
            "observed": True,
            "candidate_threshold_m": candidate_threshold_m,
            "min_distance_to_route_m": min_distance,
            "min_distance_to_dropoff_m": collision_observed.get(
                "min_distance_to_dropoff_m"
            ),
            "collision_geometry_observed": bool(
                collision_observed.get("collision_geometry_observed")
            ),
            "contact_topic_observed": bool(
                collision_observed.get("contact_topic_observed")
            ),
            "route_blocking_candidate": route_blocking_candidate,
            "route_blocking_verified": False,
            "operator_review_required": route_blocking_candidate,
            "incident_report_created": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "route_blocking_candidate_evidence": {
            "schema_version": "route_blocking_candidate_evidence.v1",
            "evidence_id": (
                "route_blocking_candidate_evidence:"
                "mission_designer_collision_obstacle"
            ),
            "condition_kind": "route_blocking_candidate_from_collision_obstacle",
            "observation_status": observation_status,
            "collision_obstacle_evidence_ref": (
                "collision_obstacle_evidence:"
                "mission_designer_collision_enabled_obstacle"
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "candidate_only": True,
            "route_blocking_verifier": False,
            "incident_report_created": False,
            "traffic_conflict_verifier": False,
            "task_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def _horizontal_route_contact_topic_integration_realism(
    run_dir: Path,
) -> dict[str, Any]:
    requested = _collision_obstacle_contact_topic_requested()
    if not requested:
        return {
            "horizontal_route_contact_topic_integration": {
                "schema_version": "horizontal_route_contact_topic_integration.v1",
                "condition_kind": "horizontal_route_contact_topic_integration",
                "integration_status": "not_requested",
                "requested": False,
                "observed": {},
                "horizontal_route_world_contact_sensor_injected": False,
                "route_execution_mutated": False,
                "task_status_mutated": False,
                "gate_status_mutated": False,
                "delivery_completion_claimed": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            }
        }

    sidecar_summary = contact_event_smoke.run_contact_event_smoke(
        output_root=(run_dir / "contact_topic_sidecar").resolve(),
        docker_image=PX4_GAZEBO_IMAGE,
    )
    artifacts = sidecar_summary.get("artifacts") or {}
    contact_evidence = artifacts.get("collision_contact_event_evidence") or {}
    contact_observed = contact_evidence.get("observed") or {}
    incident_evidence = artifacts.get("contact_event_incident_evidence") or {}
    incident_observed = incident_evidence.get("observed") or {}
    sidecar_verifier_candidate = (
        artifacts.get("contact_event_scoped_verifier_candidate") or {}
    )
    sidecar_verifier_observed = sidecar_verifier_candidate.get("observed") or {}
    sidecar_incident_verification = (
        artifacts.get("contact_event_incident_verification") or {}
    )
    sidecar_incident_verification_observed = (
        sidecar_incident_verification.get("observed") or {}
    )
    sidecar_incident_verified = (
        sidecar_incident_verification.get("schema_version")
        == "contact_event_incident_verification.v1"
        and sidecar_incident_verification.get("verification_scope")
        == "contact_event_incident_only"
        and sidecar_incident_verification.get("verification_status")
        == "incident_verified"
        and sidecar_incident_verification_observed.get("incident_verified") is True
    )
    report = artifacts.get("operational_incident_report") or {}
    report_observed = report.get("observed") or {}
    contact_event_observed = bool(contact_observed.get("contact_event_observed"))
    sidecar_artifact_dir = str(sidecar_summary.get("artifact_dir") or "")
    sidecar_ref_hash = hashlib.sha256(
        f"{run_dir.resolve()}::{sidecar_artifact_dir}".encode("utf-8")
    ).hexdigest()
    horizontal_incident_evidence = {
        "schema_version": "horizontal_route_contact_event_incident_evidence.v1",
        "evidence_id": (
            "horizontal_route_contact_event_incident_evidence:"
            f"{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_contact_topic_incident_candidate",
        "observation_status": incident_evidence.get(
            "observation_status",
            (
                "contact_event_incident_candidate_observed"
                if contact_event_observed
                else "contact_event_incident_not_observed"
            ),
        ),
        "horizontal_route_contact_topic_integration_ref": (
            "horizontal_route_contact_topic_integration:" f"{sidecar_ref_hash[:16]}"
        ),
        "sidecar_contact_event_incident_evidence_ref": incident_evidence.get(
            "evidence_id", ""
        ),
        "sidecar_collision_contact_event_evidence_ref": contact_evidence.get(
            "evidence_id", ""
        ),
        "sidecar_artifact_dir": sidecar_artifact_dir,
        "sidecar_artifact_dir_sha256": sidecar_ref_hash,
        "observed": {
            "source": "horizontal_route_contact_topic_integration",
            "observed": contact_event_observed,
            "contact_topic_observed": bool(
                contact_observed.get("contact_topic_observed")
            ),
            "contact_event_observed": contact_event_observed,
            "contact_event_incident_candidate": bool(
                incident_observed.get("contact_event_incident_candidate")
            ),
            "operator_review_required": bool(
                incident_observed.get("operator_review_required")
                or report_observed.get("operator_review_required")
            ),
            "collision_names": contact_observed.get("collision_names") or [],
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "source_sidecar_contact_event_incident_evidence": incident_evidence,
        "source_sidecar_collision_contact_event_evidence": contact_evidence,
        "candidate_only": True,
        "operator_review_report": True,
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    horizontal_report = {
        "schema_version": "horizontal_route_contact_operational_incident_report.v1",
        "report_id": (
            "horizontal_route_contact_operational_incident_report:"
            f"{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_contact_topic_incident_report",
        "report_status": report.get(
            "report_status",
            (
                "operator_review_required"
                if horizontal_incident_evidence["observed"]["operator_review_required"]
                else "not_observed"
            ),
        ),
        "horizontal_route_contact_event_incident_evidence_ref": (
            horizontal_incident_evidence["evidence_id"]
        ),
        "sidecar_operational_incident_report_ref": report.get("report_id", ""),
        "sidecar_artifact_dir": sidecar_artifact_dir,
        "observed": {
            "source": "horizontal_route_contact_event_incident_evidence",
            "observed": contact_event_observed,
            "operator_review_required": horizontal_incident_evidence["observed"][
                "operator_review_required"
            ],
            "contact_event_incident_candidate": horizontal_incident_evidence[
                "observed"
            ]["contact_event_incident_candidate"],
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "source_sidecar_operational_incident_report": report,
        "operator_review_required": horizontal_incident_evidence["observed"][
            "operator_review_required"
        ],
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    horizontal_verifier_candidate = {
        "schema_version": "horizontal_route_contact_scoped_verifier_candidate.v1",
        "candidate_id": (
            "horizontal_route_contact_scoped_verifier_candidate:"
            f"{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_contact_operator_review_verifier_candidate",
        "candidate_status": (
            "operator_review_candidate" if contact_event_observed else "not_observed"
        ),
        "observation_status": (
            "operator_review_candidate" if contact_event_observed else "not_observed"
        ),
        "horizontal_route_contact_topic_integration_ref": (
            "horizontal_route_contact_topic_integration:" f"{sidecar_ref_hash[:16]}"
        ),
        "horizontal_route_contact_event_incident_evidence_ref": (
            horizontal_incident_evidence["evidence_id"]
        ),
        "sidecar_scoped_verifier_candidate_ref": sidecar_verifier_candidate.get(
            "candidate_id", ""
        ),
        "sidecar_artifact_dir": sidecar_artifact_dir,
        "observed": {
            "source": "horizontal_route_contact_event_incident_evidence",
            "observed": contact_event_observed,
            "contact_event_observed": contact_event_observed,
            "collision_names": contact_observed.get("collision_names") or [],
            "sidecar_scoped_verifier_candidate": bool(
                sidecar_verifier_observed.get("scoped_verifier_candidate")
            ),
            "scoped_verifier_candidate": contact_event_observed,
            "operator_review_required": horizontal_incident_evidence["observed"][
                "operator_review_required"
            ],
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "source_sidecar_scoped_verifier_candidate": sidecar_verifier_candidate,
        "candidate_only": True,
        "operator_review_required": horizontal_incident_evidence["observed"][
            "operator_review_required"
        ],
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    horizontal_incident_verification = {
        "schema_version": "horizontal_route_contact_incident_verification.v1",
        "verification_id": (
            "horizontal_route_contact_incident_verification:" f"{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_contact_scoped_incident_verifier",
        "verification_status": (
            "incident_verified"
            if sidecar_incident_verified
            else "incident_not_verified"
        ),
        "verification_scope": "contact_event_incident_only",
        "horizontal_route_contact_scoped_verifier_candidate_ref": (
            horizontal_verifier_candidate["candidate_id"]
        ),
        "horizontal_route_contact_event_incident_evidence_ref": (
            horizontal_incident_evidence["evidence_id"]
        ),
        "sidecar_contact_event_incident_verification_ref": (
            sidecar_incident_verification.get("verification_id", "")
        ),
        "sidecar_artifact_dir": sidecar_artifact_dir,
        "observed": {
            "source": "horizontal_route_contact_scoped_verifier_candidate",
            "observed": contact_event_observed,
            "contact_event_observed": contact_event_observed,
            "collision_names": contact_observed.get("collision_names") or [],
            "sidecar_incident_verified": bool(
                sidecar_incident_verification_observed.get("incident_verified")
            ),
            "scoped_verifier_candidate": contact_event_observed,
            "operator_review_required": horizontal_incident_evidence["observed"][
                "operator_review_required"
            ],
            "incident_verified": sidecar_incident_verified,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "source_sidecar_contact_event_incident_verification": (
            sidecar_incident_verification
        ),
        "source_verifier_fail_closed_reason": (
            ""
            if sidecar_incident_verified
            else "sidecar_contact_event_incident_verification_not_verified"
        ),
        "operator_review_required": horizontal_incident_evidence["observed"][
            "operator_review_required"
        ],
        "incident_verifier": True,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    horizontal_incident_verification_observed = horizontal_incident_verification[
        "observed"
    ]
    incident_informed_traffic_verified = bool(
        horizontal_incident_verification_observed["incident_verified"]
        and contact_event_observed
        and (contact_observed.get("collision_names") or [])
    )
    horizontal_incident_informed_traffic_conflict_verification = {
        "schema_version": (
            "horizontal_route_incident_informed_traffic_conflict_verification.v1"
        ),
        "verification_id": (
            "horizontal_route_incident_informed_traffic_conflict_verification:"
            f"{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_incident_informed_traffic_conflict",
        "verification_status": (
            "traffic_conflict_verified"
            if incident_informed_traffic_verified
            else "traffic_conflict_not_verified"
        ),
        "verification_scope": "incident_informed_traffic_conflict_only",
        "horizontal_route_contact_incident_verification_ref": (
            horizontal_incident_verification["verification_id"]
        ),
        "horizontal_route_contact_topic_integration_ref": (
            "horizontal_route_contact_topic_integration:" f"{sidecar_ref_hash[:16]}"
        ),
        "observed": {
            "source": "horizontal_route_contact_incident_verification",
            "observed": incident_informed_traffic_verified,
            "incident_verified": horizontal_incident_verification_observed[
                "incident_verified"
            ],
            "contact_event_observed": contact_event_observed,
            "collision_names": contact_observed.get("collision_names") or [],
            "traffic_conflict_verified": incident_informed_traffic_verified,
            "route_blocking_verified": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "dropoff_verified": False,
            "delivery_completion_claimed": False,
        },
        "source_horizontal_route_contact_incident_verification": (
            horizontal_incident_verification
        ),
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": True,
        "route_blocking_candidate": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verifier": False,
        "delivery_verifier": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    route_candidate_evidence = (ROUTE_BLOCKING_CANDIDATE_SUMMARY or {}).get(
        "route_blocking_candidate_evidence",
        {},
    )
    route_candidate_observed = route_candidate_evidence.get("observed") or {}
    incident_traffic_observed = (
        horizontal_incident_informed_traffic_conflict_verification["observed"]
    )
    incident_traffic_source_verified = (
        horizontal_incident_informed_traffic_conflict_verification.get("schema_version")
        == "horizontal_route_incident_informed_traffic_conflict_verification.v1"
        and horizontal_incident_informed_traffic_conflict_verification.get(
            "verification_scope"
        )
        == "incident_informed_traffic_conflict_only"
        and horizontal_incident_informed_traffic_conflict_verification.get(
            "verification_status"
        )
        == "traffic_conflict_verified"
        and incident_traffic_observed.get("traffic_conflict_verified") is True
    )
    route_candidate_source_verified = (
        route_candidate_evidence.get("schema_version")
        == "route_blocking_candidate_evidence.v1"
        and route_candidate_evidence.get("observation_status")
        == "route_blocking_candidate_observed"
        and route_candidate_observed.get("route_blocking_candidate") is True
        and route_candidate_observed.get("source_condition_application_verified")
        is True
        and route_candidate_observed.get("world_sdf_hash_match") is True
        and isinstance(
            route_candidate_observed.get("min_distance_to_route_m"), (int, float)
        )
        and isinstance(
            route_candidate_observed.get("candidate_threshold_m"), (int, float)
        )
    )
    incident_informed_route_blocking_verified = bool(
        incident_traffic_source_verified and route_candidate_source_verified
    )
    route_blocking_fail_closed_reasons: list[str] = []
    if not incident_traffic_source_verified:
        route_blocking_fail_closed_reasons.append(
            "incident_informed_traffic_conflict_not_verified"
        )
    if not route_candidate_source_verified:
        route_blocking_fail_closed_reasons.append(
            "source_bound_route_blocking_candidate_evidence_not_verified"
        )
    horizontal_incident_informed_route_blocking_verification = {
        "schema_version": (
            "horizontal_route_incident_informed_route_blocking_verification.v1"
        ),
        "verification_id": (
            "horizontal_route_incident_informed_route_blocking_verification:"
            f"{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_incident_informed_route_blocking",
        "verification_status": (
            "route_blocking_verified"
            if incident_informed_route_blocking_verified
            else "route_blocking_not_verified"
        ),
        "verification_scope": "incident_informed_route_obstruction_only",
        "horizontal_route_incident_informed_traffic_conflict_verification_ref": (
            horizontal_incident_informed_traffic_conflict_verification[
                "verification_id"
            ]
        ),
        "route_blocking_candidate_evidence_ref": route_candidate_evidence.get(
            "evidence_id", ""
        ),
        "horizontal_route_contact_topic_integration_ref": (
            "horizontal_route_contact_topic_integration:" f"{sidecar_ref_hash[:16]}"
        ),
        "observed": {
            "source": "horizontal_route_incident_informed_traffic_conflict_and_route_candidate",
            "observed": incident_informed_route_blocking_verified,
            "traffic_conflict_verified": incident_traffic_observed.get(
                "traffic_conflict_verified"
            )
            is True,
            "route_blocking_candidate": bool(
                route_candidate_observed.get("route_blocking_candidate")
            ),
            "source_condition_application_ref": route_candidate_observed.get(
                "source_condition_application_ref", ""
            ),
            "source_condition_application_verified": bool(
                route_candidate_observed.get("source_condition_application_verified")
            ),
            "world_sdf_hash_match": bool(
                route_candidate_observed.get("world_sdf_hash_match")
            ),
            "route_blocking_verified": incident_informed_route_blocking_verified,
            "min_distance_to_route_m": route_candidate_observed.get(
                "min_distance_to_route_m"
            ),
            "candidate_threshold_m": route_candidate_observed.get(
                "candidate_threshold_m"
            ),
            "collision_geometry_observed": bool(
                route_candidate_observed.get("collision_geometry_observed")
            ),
            "contact_event_observed": contact_event_observed,
            "collision_names": contact_observed.get("collision_names") or [],
            "operator_review_required": incident_informed_route_blocking_verified,
            "gate_candidate": incident_informed_route_blocking_verified,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "dropoff_verified": False,
            "delivery_completion_claimed": False,
        },
        "source_horizontal_route_incident_informed_traffic_conflict_verification": (
            horizontal_incident_informed_traffic_conflict_verification
        ),
        "source_route_blocking_candidate_evidence": route_candidate_evidence,
        "source_verifier_fail_closed_reasons": route_blocking_fail_closed_reasons,
        "incident_verifier": False,
        "traffic_conflict_verifier": False,
        "route_blocking_verifier": True,
        "gate_candidate_only": True,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verifier": False,
        "delivery_verifier": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    integration_status = (
        "sidecar_contact_event_observed"
        if contact_event_observed
        else "sidecar_contact_event_not_observed"
    )
    return {
        "horizontal_route_contact_topic_integration": {
            "schema_version": "horizontal_route_contact_topic_integration.v1",
            "condition_kind": "horizontal_route_contact_topic_integration",
            "integration_status": integration_status,
            "requested": True,
            "integration_mode": "scoped_sidecar_contact_probe",
            "sidecar_summary_schema_version": sidecar_summary.get("schema_version"),
            "sidecar_status": sidecar_summary.get("status"),
            "sidecar_artifact_dir": sidecar_artifact_dir,
            "sidecar_artifact_dir_sha256": sidecar_ref_hash,
            "horizontal_route_contact_event_incident_evidence_ref": (
                horizontal_incident_evidence["evidence_id"]
            ),
            "horizontal_route_contact_operational_incident_report_ref": (
                horizontal_report["report_id"]
            ),
            "horizontal_route_contact_scoped_verifier_candidate_ref": (
                horizontal_verifier_candidate["candidate_id"]
            ),
            "horizontal_route_contact_incident_verification_ref": (
                horizontal_incident_verification["verification_id"]
            ),
            "horizontal_route_incident_informed_traffic_conflict_verification_ref": (
                horizontal_incident_informed_traffic_conflict_verification[
                    "verification_id"
                ]
            ),
            "horizontal_route_incident_informed_route_blocking_verification_ref": (
                horizontal_incident_informed_route_blocking_verification[
                    "verification_id"
                ]
            ),
            "horizontal_route_world_contact_sensor_injected": False,
            "horizontal_route_px4_home_boundary_protected": True,
            "reason_horizontal_route_world_not_mutated": (
                "direct contact-system injection perturbs PX4/Gazebo startup/home"
            ),
            "observed": {
                "source": "scoped_gazebo_contact_event_sidecar",
                "contact_topic_observed": bool(
                    contact_observed.get("contact_topic_observed")
                ),
                "contact_event_observed": contact_event_observed,
                "collision_names": contact_observed.get("collision_names") or [],
                "contact_event_incident_candidate": bool(
                    incident_observed.get("contact_event_incident_candidate")
                ),
                "operator_review_required": bool(
                    incident_observed.get("operator_review_required")
                    or report_observed.get("operator_review_required")
                ),
                "scoped_verifier_candidate": contact_event_observed,
                "incident_verified": horizontal_incident_verification["observed"][
                    "incident_verified"
                ],
                "incident_informed_traffic_conflict_verified": (
                    horizontal_incident_informed_traffic_conflict_verification[
                        "observed"
                    ]["traffic_conflict_verified"]
                ),
                "incident_informed_route_blocking_verified": (
                    horizontal_incident_informed_route_blocking_verification[
                        "observed"
                    ]["route_blocking_verified"]
                ),
                "route_blocking_verified": False,
                "traffic_conflict_verified": False,
                "task_status_mutated": False,
                "gate_status_mutated": False,
                "delivery_completion_claimed": False,
            },
            "sidecar_collision_contact_event_evidence": contact_evidence,
            "sidecar_contact_event_incident_evidence": incident_evidence,
            "sidecar_contact_event_scoped_verifier_candidate": (
                sidecar_verifier_candidate
            ),
            "sidecar_contact_event_incident_verification": (
                sidecar_incident_verification
            ),
            "sidecar_operational_incident_report": report,
            "route_execution_mutated": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
        "horizontal_route_contact_event_incident_evidence": horizontal_incident_evidence,
        "horizontal_route_contact_operational_incident_report": horizontal_report,
        "horizontal_route_contact_scoped_verifier_candidate": (
            horizontal_verifier_candidate
        ),
        "horizontal_route_contact_incident_verification": (
            horizontal_incident_verification
        ),
        "horizontal_route_incident_informed_traffic_conflict_verification": (
            horizontal_incident_informed_traffic_conflict_verification
        ),
        "horizontal_route_incident_informed_route_blocking_verification": (
            horizontal_incident_informed_route_blocking_verification
        ),
    }


def _refresh_horizontal_contact_topic_summary(run_dir: Path) -> None:
    global HORIZONTAL_CONTACT_TOPIC_SUMMARY
    if HORIZONTAL_CONTACT_TOPIC_SUMMARY is None:
        HORIZONTAL_CONTACT_TOPIC_SUMMARY = (
            _horizontal_route_contact_topic_integration_realism(run_dir)
        )


def _operational_incident_report_realism() -> dict[str, Any]:
    candidate_evidence = (ROUTE_BLOCKING_CANDIDATE_SUMMARY or {}).get(
        "route_blocking_candidate_evidence",
        {},
    )
    candidate_observed = candidate_evidence.get("observed") or {}
    collision_evidence = (COLLISION_OBSTACLE_SUMMARY or {}).get(
        "collision_obstacle_evidence",
        {},
    )
    collision_observed = collision_evidence.get("observed") or {}
    requested = _collision_obstacle_requested()
    unsupported_reasons: list[str] = []
    if not requested:
        report_status = "not_requested"
        observed: dict[str, Any] = {}
    elif not candidate_observed.get("observed"):
        report_status = "operational_incident_report_not_observed"
        unsupported_reasons.append("route_blocking_candidate_evidence_missing")
        observed = {
            "source": "route_blocking_candidate_evidence",
            "observed": False,
            "operator_review_required": False,
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        route_blocking_candidate = bool(
            candidate_observed.get("route_blocking_candidate")
        )
        report_status = (
            "operator_review_required"
            if route_blocking_candidate
            else "no_operational_incident_candidate"
        )
        observed = {
            "source": "route_blocking_candidate_evidence",
            "observed": True,
            "route_blocking_candidate": route_blocking_candidate,
            "candidate_threshold_m": candidate_observed.get("candidate_threshold_m"),
            "min_distance_to_route_m": candidate_observed.get(
                "min_distance_to_route_m"
            ),
            "min_distance_to_dropoff_m": candidate_observed.get(
                "min_distance_to_dropoff_m"
            ),
            "collision_geometry_observed": bool(
                candidate_observed.get("collision_geometry_observed")
            ),
            "source_condition_application_ref": candidate_observed.get(
                "source_condition_application_ref", ""
            ),
            "source_condition_application_verified": bool(
                candidate_observed.get("source_condition_application_verified")
            ),
            "world_sdf_hash_match": bool(
                candidate_observed.get("world_sdf_hash_match")
            ),
            "contact_topic_observed": bool(
                candidate_observed.get("contact_topic_observed")
            ),
            "collision_obstacle_pose_observed": bool(
                collision_observed.get("pose_observed")
            ),
            "operator_review_required": route_blocking_candidate,
            "auto_gate": False,
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "operational_incident_report": {
            "schema_version": "operational_incident_report.v1",
            "report_id": (
                "operational_incident_report:"
                "mission_designer_route_blocking_candidate"
            ),
            "condition_kind": "operator_reviewed_route_blocking_candidate",
            "report_status": report_status,
            "input_evidence_refs": [
                "collision_obstacle_evidence:"
                "mission_designer_collision_enabled_obstacle",
                "route_blocking_candidate_evidence:"
                "mission_designer_collision_obstacle",
            ],
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "operator_review_report": True,
            "auto_gate": False,
            "incident_verifier": False,
            "route_blocking_verifier": False,
            "traffic_conflict_verifier": False,
            "task_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def _traffic_conflict_verification_realism() -> dict[str, Any]:
    incident_report = (OPERATIONAL_INCIDENT_REPORT_SUMMARY or {}).get(
        "operational_incident_report",
        {},
    )
    incident_observed = incident_report.get("observed") or {}
    requested = _collision_obstacle_requested()
    unsupported_reasons: list[str] = []
    if not requested:
        verification_status = "not_requested"
        observed: dict[str, Any] = {}
    elif incident_report.get("report_status") != "operator_review_required":
        verification_status = "traffic_conflict_not_verified"
        unsupported_reasons.append("operator_review_incident_report_missing")
        observed = {
            "source": "operational_incident_report",
            "observed": False,
            "traffic_conflict_verified": False,
            "route_blocking_verified": False,
            "dropoff_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        traffic_conflict_verified = bool(
            incident_observed.get("route_blocking_candidate")
            and incident_observed.get("collision_geometry_observed")
            and incident_observed.get("source_condition_application_verified")
            and incident_observed.get("world_sdf_hash_match")
        )
        verification_status = (
            "traffic_conflict_verified"
            if traffic_conflict_verified
            else "traffic_conflict_not_verified"
        )
        observed = {
            "source": "operational_incident_report",
            "observed": True,
            "verification_scope": "operational_conflict_only",
            "route_blocking_candidate": bool(
                incident_observed.get("route_blocking_candidate")
            ),
            "collision_geometry_observed": bool(
                incident_observed.get("collision_geometry_observed")
            ),
            "source_condition_application_ref": incident_observed.get(
                "source_condition_application_ref", ""
            ),
            "source_condition_application_verified": bool(
                incident_observed.get("source_condition_application_verified")
            ),
            "world_sdf_hash_match": bool(incident_observed.get("world_sdf_hash_match")),
            "contact_topic_observed": bool(
                incident_observed.get("contact_topic_observed")
            ),
            "min_distance_to_route_m": incident_observed.get("min_distance_to_route_m"),
            "candidate_threshold_m": incident_observed.get("candidate_threshold_m"),
            "traffic_conflict_verified": traffic_conflict_verified,
            "operator_review_required": True,
            "route_blocking_verified": False,
            "incident_verified": False,
            "dropoff_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "traffic_conflict_verification": {
            "schema_version": "traffic_conflict_verification.v1",
            "verification_id": (
                "traffic_conflict_verification:" "mission_designer_collision_obstacle"
            ),
            "condition_kind": "scoped_operator_review_traffic_conflict",
            "verification_status": verification_status,
            "operational_incident_report_ref": (
                "operational_incident_report:"
                "mission_designer_route_blocking_candidate"
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "verification_scope": "operational_conflict_only",
            "route_blocking_verifier": False,
            "dropoff_verifier": False,
            "delivery_verifier": False,
            "task_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def _route_blocking_verification_realism() -> dict[str, Any]:
    traffic_verification = (TRAFFIC_CONFLICT_VERIFICATION_SUMMARY or {}).get(
        "traffic_conflict_verification",
        {},
    )
    traffic_observed = traffic_verification.get("observed") or {}
    requested = _collision_obstacle_requested()
    unsupported_reasons: list[str] = []
    if not requested:
        verification_status = "not_requested"
        observed: dict[str, Any] = {}
    elif traffic_verification.get("verification_status") != "traffic_conflict_verified":
        verification_status = "route_blocking_not_verified"
        unsupported_reasons.append("traffic_conflict_verification_missing")
        observed = {
            "source": "traffic_conflict_verification",
            "observed": False,
            "route_blocking_verified": False,
            "gate_candidate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        route_blocking_verified = bool(
            traffic_observed.get("traffic_conflict_verified")
            and traffic_observed.get("route_blocking_candidate")
            and traffic_observed.get("source_condition_application_verified")
            and traffic_observed.get("world_sdf_hash_match")
        )
        verification_status = (
            "route_blocking_verified"
            if route_blocking_verified
            else "route_blocking_not_verified"
        )
        observed = {
            "source": "traffic_conflict_verification",
            "observed": True,
            "verification_scope": "operational_route_obstruction_only",
            "route_blocking_verified": route_blocking_verified,
            "traffic_conflict_verified": bool(
                traffic_observed.get("traffic_conflict_verified")
            ),
            "route_blocking_candidate": bool(
                traffic_observed.get("route_blocking_candidate")
            ),
            "source_condition_application_ref": traffic_observed.get(
                "source_condition_application_ref", ""
            ),
            "source_condition_application_verified": bool(
                traffic_observed.get("source_condition_application_verified")
            ),
            "world_sdf_hash_match": bool(traffic_observed.get("world_sdf_hash_match")),
            "min_distance_to_route_m": traffic_observed.get("min_distance_to_route_m"),
            "candidate_threshold_m": traffic_observed.get("candidate_threshold_m"),
            "gate_candidate": route_blocking_verified,
            "operator_review_required": route_blocking_verified,
            "auto_gate": False,
            "incident_verified": False,
            "dropoff_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "route_blocking_verification": {
            "schema_version": "route_blocking_verification.v1",
            "verification_id": (
                "route_blocking_verification:" "mission_designer_collision_obstacle"
            ),
            "condition_kind": "scoped_operator_review_route_blocking",
            "verification_status": verification_status,
            "traffic_conflict_verification_ref": (
                "traffic_conflict_verification:" "mission_designer_collision_obstacle"
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "verification_scope": "operational_route_obstruction_only",
            "gate_candidate_only": True,
            "auto_gate": False,
            "dropoff_verifier": False,
            "delivery_verifier": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def _alternate_landing_candidate_evidence_realism() -> dict[str, Any]:
    route_blocking_verification = (ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}).get(
        "route_blocking_verification",
        {},
    )
    route_blocking_observed = route_blocking_verification.get("observed") or {}
    alternate_profile = (OPERATIONAL_REALISM_SUMMARY or {}).get(
        "alternate_landing_profile",
        {},
    )
    requested = (
        _collision_obstacle_requested() and _alternate_landing_marker_requested()
    )
    unsupported_reasons: list[str] = []
    if not requested:
        observation_status = "not_requested"
        observed: dict[str, Any] = {}
    elif (
        route_blocking_verification.get("verification_status")
        != "route_blocking_verified"
    ):
        observation_status = "alternate_landing_candidate_not_observed"
        unsupported_reasons.append("route_blocking_verification_missing")
        observed = {
            "source": "route_blocking_verification",
            "observed": False,
            "alternate_landing_candidate": False,
            "px4_route_changed": False,
            "rth_commanded": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        alternate_landing_candidate = bool(
            route_blocking_observed.get("route_blocking_verified")
        )
        candidates = alternate_profile.get("candidates")
        candidate_id = None
        candidate_xy_m = None
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            if isinstance(first, dict):
                candidate_id = first.get("candidate_id")
                candidate_xy_m = first.get("position_xy_m") or first.get("xy_m")
        observation_status = (
            "alternate_landing_candidate_observed"
            if alternate_landing_candidate
            else "alternate_landing_candidate_not_observed"
        )
        observed = {
            "source": "route_blocking_verification",
            "observed": True,
            "alternate_landing_candidate": alternate_landing_candidate,
            "candidate_id": candidate_id,
            "candidate_xy_m": candidate_xy_m,
            "route_blocking_verified": bool(
                route_blocking_observed.get("route_blocking_verified")
            ),
            "traffic_conflict_verified": bool(
                route_blocking_observed.get("traffic_conflict_verified")
            ),
            "gate_candidate": bool(route_blocking_observed.get("gate_candidate")),
            "operator_review_required": alternate_landing_candidate,
            "px4_route_changed": False,
            "rth_commanded": False,
            "land_commanded": False,
            "alternate_landing_behavior_observed": False,
            "task_failed": False,
            "delivery_failed": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "alternate_landing_candidate_evidence": {
            "schema_version": "alternate_landing_candidate_evidence.v1",
            "candidate_evidence_id": (
                "alternate_landing_candidate_evidence:"
                "mission_designer_route_blocking"
            ),
            "condition_kind": "alternate_landing_candidate_from_route_blocking",
            "observation_status": observation_status,
            "requested_present": requested,
            "route_blocking_verification_ref": (
                "route_blocking_verification:" "mission_designer_collision_obstacle"
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "candidate_only": True,
            "px4_behavior_applicator": False,
            "rth_behavior_observer": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def _alternate_landing_execution_realism(
    *,
    emergency_approval: Any | None,
    emergency_allowlist: Any | None,
    emergency_dispatch: Any | None,
    completed_pose: dict[str, float] | None,
    landing_samples: list[dict[str, float]],
) -> dict[str, Any]:
    candidate = (ALTERNATE_LANDING_CANDIDATE_SUMMARY or {}).get(
        "alternate_landing_candidate_evidence",
        {},
    )
    candidate_observed = candidate.get("observed") or {}
    requested = bool(candidate_observed.get("alternate_landing_candidate"))
    dispatch_dump = (
        emergency_dispatch.model_dump(mode="json")
        if emergency_dispatch is not None
        else {}
    )
    approval_ref = (
        f"px4_gazebo_emergency_command_approval:{emergency_approval.approval_id}"
        if emergency_approval is not None
        else ""
    )
    allowlist_ref = (
        f"px4_gazebo_emergency_command_allowlist:{emergency_allowlist.allowlist_id}"
        if emergency_allowlist is not None
        else ""
    )
    dispatch_status = dispatch_dump.get("dispatch_status")
    ack_observed = bool(dispatch_dump.get("command_ack_observed"))
    ack_result_code = dispatch_dump.get("command_ack_result_code")
    command_sent = bool(dispatch_dump.get("recovery_command_sent"))
    landing_observed = (
        completed_pose is not None and float(completed_pose.get("z", 99.0)) <= 0.15
    )
    ack_complete = ack_observed and ack_result_code == 0
    state_observed_after_dispatch_timeout = (
        command_sent
        and dispatch_status == "timeout"
        and not ack_observed
        and landing_observed
    )
    behavior_observed = bool(
        requested
        and command_sent
        and (ack_complete or state_observed_after_dispatch_timeout)
        and landing_observed
    )
    request_status = (
        "approved_for_sitl_alternate_landing" if requested else "not_requested"
    )
    dispatch_observation_status = (
        "alternate_landing_command_ack_observed"
        if command_sent and ack_complete
        else (
            "alternate_landing_state_observed_after_dispatch_timeout"
            if state_observed_after_dispatch_timeout
            else (
                "alternate_landing_command_not_dispatched"
                if not requested
                else "alternate_landing_command_unconfirmed"
            )
        )
    )
    behavior_status = (
        "alternate_landing_behavior_observed"
        if behavior_observed
        else "alternate_landing_behavior_not_observed" if requested else "not_requested"
    )
    final_pose = completed_pose or {}
    return {
        "alternate_landing_execution_request": {
            "schema_version": "alternate_landing_execution_request.v1",
            "request_id": (
                "alternate_landing_execution_request:" "mission_designer_route_blocking"
            ),
            "request_status": request_status,
            "requested_present": requested,
            "candidate_evidence_ref": (
                "alternate_landing_candidate_evidence:"
                "mission_designer_route_blocking"
            ),
            "operator_approval_performed": bool(
                emergency_approval is not None
                and emergency_approval.operator_approval_performed is True
            ),
            "sitl_opt_in": True,
            "approved_action": "land" if requested else "",
            "px4_route_changed": False,
            "rth_commanded": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_landing_command_dispatch": {
            "schema_version": "alternate_landing_command_dispatch.v1",
            "dispatch_id": (
                "alternate_landing_command_dispatch:" "mission_designer_route_blocking"
            ),
            "dispatch_status": dispatch_status or "not_requested",
            "application_status": dispatch_observation_status,
            "approval_ref": approval_ref,
            "allowlist_ref": allowlist_ref,
            "emergency_dispatch_ref": (
                "px4_gazebo_emergency_command_dispatch_result:"
                f"{emergency_dispatch.dispatch_result_id}"
                if emergency_dispatch is not None
                else ""
            ),
            "command_name": dispatch_dump.get("command_name", ""),
            "command_id": dispatch_dump.get("command_id"),
            "command_ack_observed": ack_observed,
            "command_ack_result_code": ack_result_code,
            "command_ack_result_name": dispatch_dump.get("command_ack_result_name"),
            "completion_basis": (
                "ack_observed_and_state_observed"
                if ack_complete and landing_observed
                else (
                    "state_observed_after_dispatch_timeout"
                    if state_observed_after_dispatch_timeout
                    else (
                        "state_not_observed_or_command_unconfirmed"
                        if requested
                        else "not_requested"
                    )
                )
            ),
            "observation_status": dispatch_observation_status,
            "mavlink_dispatch_performed": command_sent,
            "bounded_allowlist_enforced": True,
            "approval_free_dispatch_allowed": False,
            "px4_route_changed": False,
            "rth_commanded": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_landing_behavior_observation": {
            "schema_version": "alternate_landing_behavior_observation.v1",
            "observation_id": (
                "alternate_landing_behavior_observation:"
                "mission_designer_route_blocking"
            ),
            "observation_status": behavior_status,
            "alternate_landing_behavior_observed": behavior_observed,
            "land_commanded": command_sent,
            "rth_commanded": False,
            "command_ack_observed": ack_observed,
            "landing_observed": landing_observed,
            "completion_basis": (
                "ack_observed_and_state_observed"
                if ack_complete and landing_observed
                else (
                    "state_observed_after_dispatch_timeout"
                    if state_observed_after_dispatch_timeout
                    else (
                        "state_not_observed_or_command_unconfirmed"
                        if requested
                        else "not_requested"
                    )
                )
            ),
            "final_pose_xyz_m": (
                [
                    final_pose.get("x"),
                    final_pose.get("y"),
                    final_pose.get("z"),
                ]
                if final_pose
                else []
            ),
            "landing_sample_count": len(landing_samples),
            "px4_route_changed": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_landing_outcome": {
            "schema_version": "alternate_landing_outcome.v1",
            "outcome_id": "alternate_landing_outcome:mission_designer_route_blocking",
            "outcome_status": (
                "alternate_landing_behavior_observed"
                if behavior_observed
                else (
                    "alternate_landing_behavior_pending_or_unconfirmed"
                    if requested
                    else "not_requested"
                )
            ),
            "alternate_landing_behavior_observed": behavior_observed,
            "task_failed": False,
            "delivery_failed": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _alternate_mission_upload_items() -> (
    tuple[tuple[int, int, float, float, float, int, int], ...]
):
    return (
        (0, 22, 35.681236, 139.767125, 15.0, 1, 6),
        (1, 16, 35.681208, 139.767166, 20.0, 0, 6),
        (2, 21, 35.681198, 139.767176, 0.0, 0, 6),
    )


def _execute_alternate_route_rewrite(
    *,
    target_z: float,
    altitude_max_m: float,
    upload_result: dict[str, Any] | None,
    approval: Any,
    route_allowlist: Any,
) -> dict[str, Any]:
    uploaded = bool(
        upload_result
        and upload_result.get("mission_ack_observed") is True
        and int(upload_result.get("mission_ack_type", -1)) == 0
    )
    candidate = (ALTERNATE_LANDING_CANDIDATE_SUMMARY or {}).get(
        "alternate_landing_candidate_evidence",
        {},
    )
    candidate_observed = candidate.get("observed") or {}
    candidate_xy = candidate_observed.get("candidate_xy_m")
    candidate_id = str(candidate_observed.get("candidate_id") or "")
    blocked_reasons: list[str] = []
    if not uploaded:
        blocked_reasons.append("alternate_mission_upload_ack_not_observed")
    if (
        not isinstance(candidate_xy, list)
        or len(candidate_xy) != 2
        or any(value is None for value in candidate_xy)
    ):
        blocked_reasons.append("alternate_landing_candidate_xy_missing")
    approval_ref = f"px4_gazebo_coupled_command_approval:{approval.approval_id}"
    allowlist_ref = f"px4_gazebo_route_command_allowlist:{route_allowlist.allowlist_id}"
    if blocked_reasons:
        return {
            "mode": "alternate_route_rewrite",
            "sent": False,
            "blocked_reasons": blocked_reasons,
            "dispatch_evidence": {
                "schema_version": "alternate_route_command_dispatch.v1",
                "dispatch_id": (
                    "alternate_route_command_dispatch:"
                    "mission_designer_route_blocking"
                ),
                "dispatch_status": "blocked",
                "approval_ref": approval_ref,
                "allowlist_ref": allowlist_ref,
                "candidate_evidence_ref": (
                    "alternate_landing_candidate_evidence:"
                    "mission_designer_route_blocking"
                ),
                "alternate_mission_ack_required": True,
                "alternate_mission_ack_observed": uploaded,
                "blocked_reasons": blocked_reasons,
                "mavlink_message_name": "SET_POSITION_TARGET_LOCAL_NED",
                "mavlink_dispatch_performed": False,
                "bounded_sitl_only": True,
                "approval_free_dispatch_allowed": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
                "delivery_completion_claimed": False,
            },
            "execution_error": "",
            "alternate_route_execution_observed": False,
            "alternate_waypoint_reached_observed": False,
        }
    alternate_observed_waypoint_x = float(candidate_xy[0])
    alternate_observed_waypoint_y = float(candidate_xy[1])
    # The active SITL local setpoint helper maps this alternate-route segment into
    # Gazebo local x/y as observed=(sent_y, sent_x). Record both frames.
    alternate_setpoint_x = alternate_observed_waypoint_y
    alternate_setpoint_y = alternate_observed_waypoint_x
    start_pose = _pose_sample()
    start_distance = math.hypot(
        float(start_pose["x"]) - alternate_observed_waypoint_x,
        float(start_pose["y"]) - alternate_observed_waypoint_y,
    )
    result: dict[str, Any]
    try:
        result = _send_route_with_monitor(
            target_x=alternate_setpoint_x,
            target_y=alternate_setpoint_y,
            target_z=target_z,
            expected_target_x=alternate_observed_waypoint_x,
            expected_target_y=alternate_observed_waypoint_y,
            pickup_pose=start_pose,
            altitude_max_m=altitude_max_m,
            max_pose_deviation_xy_m=8.0,
            max_pose_deviation_z_m=max(10.0, altitude_max_m + 5.0),
            duration_seconds=12.0,
            timeout=22,
            on_deviation=None,
        )
        execution_error = ""
    except Exception as exc:
        result = {
            "mode": "route",
            "sent": False,
            "blocked_reasons": ["alternate_route_rewrite_execution_failed"],
        }
        execution_error = str(exc)[-500:]
    final_pose = _pose_sample()
    _append_live_pose_row("alternate_route_rewrite", final_pose)
    final_distance = math.hypot(
        float(final_pose["x"]) - alternate_observed_waypoint_x,
        float(final_pose["y"]) - alternate_observed_waypoint_y,
    )
    horizontal_progress = max(0.0, start_distance - final_distance)
    waypoint_reached = final_distance <= 3.0
    route_executed = bool(result.get("sent") is True and horizontal_progress >= 1.0)
    return {
        "mode": "alternate_route_rewrite",
        "sent": bool(result.get("sent") is True),
        "blocked_reasons": list(result.get("blocked_reasons", [])),
        "dispatch_evidence": {
            "schema_version": "alternate_route_command_dispatch.v1",
            "dispatch_id": (
                "alternate_route_command_dispatch:" "mission_designer_route_blocking"
            ),
            "dispatch_status": "sent" if result.get("sent") is True else "blocked",
            "approval_ref": approval_ref,
            "allowlist_ref": allowlist_ref,
            "candidate_evidence_ref": (
                "alternate_landing_candidate_evidence:"
                "mission_designer_route_blocking"
            ),
            "candidate_id": candidate_id,
            "alternate_mission_ack_required": True,
            "alternate_mission_ack_observed": uploaded,
            "mavlink_message_name": "SET_POSITION_TARGET_LOCAL_NED",
            "target_frame": "px4_local_ned_setpoint",
            "observed_frame": "gazebo_world_local",
            "sent_setpoint_xy_m": [alternate_setpoint_x, alternate_setpoint_y],
            "observed_waypoint_xy_m": [
                alternate_observed_waypoint_x,
                alternate_observed_waypoint_y,
            ],
            "frame_mapping_basis": "runtime_observed_alternate_route_axis_mapping",
            "mavlink_dispatch_performed": bool(result.get("sent") is True),
            "bounded_sitl_only": True,
            "approval_free_dispatch_allowed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "route_helper_result": result,
        "execution_error": execution_error,
        "sent_setpoint_xy_m": [alternate_setpoint_x, alternate_setpoint_y],
        "observed_waypoint_xy_m": [
            alternate_observed_waypoint_x,
            alternate_observed_waypoint_y,
        ],
        "target_z_m": target_z,
        "start_pose_xyz_m": [
            start_pose.get("x"),
            start_pose.get("y"),
            start_pose.get("z"),
        ],
        "final_pose_xyz_m": [
            final_pose.get("x"),
            final_pose.get("y"),
            final_pose.get("z"),
        ],
        "start_distance_to_alternate_waypoint_m": start_distance,
        "final_distance_to_alternate_waypoint_m": final_distance,
        "horizontal_progress_toward_alternate_waypoint_m": horizontal_progress,
        "alternate_waypoint_reached_observed": waypoint_reached,
        "alternate_route_execution_observed": route_executed and waypoint_reached,
        "completion_basis": (
            "alternate_waypoint_reached_from_pose_progress"
            if route_executed and waypoint_reached
            else (
                "alternate_route_progress_observed_waypoint_pending"
                if route_executed
                else "alternate_route_execution_not_observed"
            )
        ),
        "route_execution_authority": "operator_approved_sitl_only",
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }


def _upload_alternate_landing_mission() -> dict[str, Any]:
    mission_upload_smoke.CONTAINER_NAME = CONTAINER_NAME
    mission_upload_smoke.PX4_MAVLINK_PORT = ROUTE_MAVLINK_PX4_PORT
    mission_upload_smoke.GCS_MAVLINK_PORT = ROUTE_MAVLINK_LOCAL_PORT
    return mission_upload_smoke._actual_upload(
        [
            {
                "seq": int(item[0]),
                "command": int(item[1]),
                "latitude_deg": float(item[2]),
                "longitude_deg": float(item[3]),
                "altitude_m": float(item[4]),
                "current": int(item[5]),
                "frame": int(item[6]),
            }
            for item in _alternate_mission_upload_items()
        ]
    )


def _alternate_mission_upload_realism(
    *,
    upload_result: dict[str, Any] | None,
    alternate_behavior_observation: dict[str, Any],
    alternate_route_execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = (ALTERNATE_LANDING_CANDIDATE_SUMMARY or {}).get(
        "alternate_landing_candidate_evidence",
        {},
    )
    candidate_observed = candidate.get("observed") or {}
    requested = bool(candidate_observed.get("alternate_landing_candidate"))
    uploaded = bool(
        upload_result
        and upload_result.get("mission_ack_observed") is True
        and int(upload_result.get("mission_ack_type", -1)) == 0
    )
    mission_items = (
        [
            {
                "seq": int(item[0]),
                "command": int(item[1]),
                "latitude_deg": float(item[2]),
                "longitude_deg": float(item[3]),
                "altitude_m": float(item[4]),
                "current": int(item[5]),
                "frame": int(item[6]),
            }
            for item in _alternate_mission_upload_items()
        ]
        if requested
        else []
    )
    command_ids = [item["command"] for item in mission_items]
    behavior_observed = bool(
        alternate_behavior_observation.get("alternate_landing_behavior_observed")
    )
    route_execution = alternate_route_execution or {}
    route_dispatch = route_execution.get("dispatch_evidence") or {}
    alternate_route_execution_observed = bool(
        uploaded
        and route_dispatch.get("alternate_mission_ack_observed") is True
        and route_execution.get("alternate_route_execution_observed")
    )
    alternate_waypoint_reached_observed = bool(
        uploaded
        and route_dispatch.get("alternate_mission_ack_observed") is True
        and route_execution.get("alternate_waypoint_reached_observed")
    )
    return {
        "alternate_mission_upload_request": {
            "schema_version": "alternate_mission_upload_request.v1",
            "request_id": (
                "alternate_mission_upload_request:" "mission_designer_route_blocking"
            ),
            "request_status": (
                "approved_for_sitl_alternate_mission_upload"
                if requested
                else "not_requested"
            ),
            "requested_present": requested,
            "candidate_evidence_ref": (
                "alternate_landing_candidate_evidence:"
                "mission_designer_route_blocking"
            ),
            "operator_approval_performed": requested,
            "sitl_opt_in": True,
            "mission_items_source": (
                "alternate_landing_candidate_route_blocking" if requested else ""
            ),
            "mission_item_count": len(mission_items),
            "contains_waypoint_item": 16 in command_ids,
            "contains_land_item": 21 in command_ids,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_mission_upload_receipt": {
            "schema_version": "alternate_mission_upload_receipt.v1",
            "receipt_id": (
                "alternate_mission_upload_receipt:" "mission_designer_route_blocking"
            ),
            "upload_status": (
                "uploaded"
                if uploaded
                else "not_requested" if not requested else "failed_or_unconfirmed"
            ),
            "target_endpoint": f"udp://127.0.0.1:{ROUTE_MAVLINK_PX4_PORT}",
            "mission_items": mission_items,
            "mission_item_count": len(mission_items),
            "mission_request_sequences": (
                list(upload_result.get("mission_request_sequences", []))
                if upload_result
                else []
            ),
            "mission_ack_observed": bool(
                upload_result and upload_result.get("mission_ack_observed") is True
            ),
            "mission_ack_type": (
                upload_result.get("mission_ack_type") if upload_result else None
            ),
            "alternate_mission_uploaded": uploaded,
            "px4_mission_upload_performed": uploaded,
            "mavlink_dispatch_performed": uploaded,
            "bounded_sitl_only": True,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_route_behavior_observation": {
            "schema_version": "alternate_route_behavior_observation.v1",
            "observation_id": (
                "alternate_route_behavior_observation:"
                "mission_designer_route_blocking"
            ),
            "observation_status": (
                "alternate_mission_uploaded_and_landing_observed"
                if uploaded and behavior_observed
                else (
                    "alternate_mission_uploaded_behavior_pending"
                    if uploaded
                    else (
                        "not_requested"
                        if not requested
                        else "alternate_mission_upload_unconfirmed"
                    )
                )
            ),
            "alternate_mission_uploaded": uploaded,
            "alternate_route_execution_observed": alternate_route_execution_observed,
            "alternate_waypoint_reached_observed": alternate_waypoint_reached_observed,
            "alternate_route_execution_ref": (
                "alternate_route_execution_evidence:" "mission_designer_route_blocking"
                if alternate_route_execution_observed
                else ""
            ),
            "alternate_landing_behavior_observed": behavior_observed,
            "behavior_observation_source": (
                "alternate_landing_behavior_observation" if behavior_observed else ""
            ),
            "mission_upload_ack_observed": bool(
                upload_result and upload_result.get("mission_ack_observed") is True
            ),
            "mission_ack_type": (
                upload_result.get("mission_ack_type") if upload_result else None
            ),
            "original_dropoff_verified": False,
            "dropoff_verified": False,
            "delivery_completion_claimed": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "alternate_route_command_dispatch": {
            "schema_version": "alternate_route_command_dispatch.v1",
            "dispatch_id": (
                "alternate_route_command_dispatch:" "mission_designer_route_blocking"
            ),
            **route_dispatch,
            "alternate_mission_uploaded": uploaded,
            "alternate_mission_ack_required": True,
            "alternate_mission_ack_observed": uploaded,
            "dispatch_status": (
                "not_requested"
                if not requested
                else (
                    "blocked"
                    if not route_dispatch
                    else route_dispatch.get("dispatch_status", "blocked")
                )
            ),
            "mavlink_dispatch_performed": bool(
                uploaded and route_dispatch.get("mavlink_dispatch_performed") is True
            ),
            "approval_free_dispatch_allowed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "alternate_route_execution_evidence": {
            "schema_version": "alternate_route_execution_evidence.v1",
            "evidence_id": (
                "alternate_route_execution_evidence:" "mission_designer_route_blocking"
            ),
            "observation_status": (
                "alternate_route_waypoint_reached_observed"
                if alternate_route_execution_observed
                and alternate_waypoint_reached_observed
                else (
                    "alternate_route_progress_observed_waypoint_pending"
                    if alternate_route_execution_observed
                    else (
                        "not_requested"
                        if not requested
                        else "alternate_route_execution_not_observed"
                    )
                )
            ),
            "alternate_mission_uploaded": uploaded,
            "alternate_route_execution_observed": alternate_route_execution_observed,
            "alternate_waypoint_reached_observed": alternate_waypoint_reached_observed,
            "observed": {
                "source": "px4_gazebo_local_pose_after_alternate_route_rewrite",
                "sent_setpoint_xy_m": route_execution.get("sent_setpoint_xy_m", []),
                "observed_waypoint_xy_m": route_execution.get(
                    "observed_waypoint_xy_m", []
                ),
                "target_z_m": route_execution.get("target_z_m"),
                "start_pose_xyz_m": route_execution.get("start_pose_xyz_m", []),
                "final_pose_xyz_m": route_execution.get("final_pose_xyz_m", []),
                "start_distance_to_alternate_waypoint_m": route_execution.get(
                    "start_distance_to_alternate_waypoint_m"
                ),
                "final_distance_to_alternate_waypoint_m": route_execution.get(
                    "final_distance_to_alternate_waypoint_m"
                ),
                "horizontal_progress_toward_alternate_waypoint_m": route_execution.get(
                    "horizontal_progress_toward_alternate_waypoint_m"
                ),
                "completion_basis": route_execution.get("completion_basis", ""),
                "alternate_route_command_dispatch_ref": (
                    "alternate_route_command_dispatch:mission_designer_route_blocking"
                    if route_dispatch
                    else ""
                ),
                "candidate_evidence_ref": (
                    "alternate_landing_candidate_evidence:"
                    "mission_designer_route_blocking"
                ),
                "candidate_id": route_dispatch.get("candidate_id", ""),
                "route_helper_sent": bool(route_execution.get("sent") is True),
                "route_helper_result": route_execution.get("route_helper_result", {}),
                "blocked_reasons": route_execution.get("blocked_reasons", []),
                "execution_error": route_execution.get("execution_error", ""),
                "read_only_observer": False,
                "operator_approved_sitl_only": requested and uploaded,
                "original_dropoff_verified": False,
                "dropoff_verified": False,
                "delivery_completion_claimed": False,
                "auto_gate": False,
                "task_status_mutated": False,
                "gate_status_mutated": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            },
            "alternate_route_execution_is_not_original_dropoff_verification": True,
            "delivery_completion_claimed": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _rth_behavior_execution_realism(
    *,
    emergency_approval: Any | None,
    emergency_allowlist: Any | None,
    emergency_dispatch: Any | None,
    rth_state_observed: bool,
    rth_state_label: str | None,
    rth_pose: dict[str, float] | None,
    rth_samples: list[dict[str, float]],
) -> dict[str, Any]:
    route_blocking = (ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}).get(
        "route_blocking_verification",
        {},
    )
    route_blocking_observed = route_blocking.get("observed") or {}
    requested = bool(
        _rth_behavior_requested()
        and route_blocking_observed.get("route_blocking_verified")
    )
    dispatch_dump = (
        emergency_dispatch.model_dump(mode="json")
        if emergency_dispatch is not None
        else {}
    )
    approval_ref = (
        f"px4_gazebo_emergency_command_approval:{emergency_approval.approval_id}"
        if emergency_approval is not None
        else ""
    )
    allowlist_ref = (
        f"px4_gazebo_emergency_command_allowlist:{emergency_allowlist.allowlist_id}"
        if emergency_allowlist is not None
        else ""
    )
    dispatch_status = dispatch_dump.get("dispatch_status")
    ack_observed = bool(dispatch_dump.get("command_ack_observed"))
    ack_result_code = dispatch_dump.get("command_ack_result_code")
    command_sent = bool(dispatch_dump.get("recovery_command_sent"))
    ack_complete = ack_observed and ack_result_code == 0
    state_observed_after_dispatch_timeout = (
        command_sent
        and dispatch_status == "timeout"
        and not ack_observed
        and rth_state_observed
    )
    behavior_observed = bool(
        requested
        and command_sent
        and (ack_complete or state_observed_after_dispatch_timeout)
        and rth_state_observed
    )
    dispatch_observation_status = (
        "rth_command_ack_observed"
        if command_sent and ack_complete
        else (
            "rth_state_observed_after_dispatch_timeout"
            if state_observed_after_dispatch_timeout
            else (
                "rth_command_not_dispatched"
                if not requested
                else "rth_command_unconfirmed"
            )
        )
    )
    behavior_status = (
        "rth_behavior_observed"
        if behavior_observed
        else "rth_behavior_not_observed" if requested else "not_requested"
    )
    completion_basis = (
        "ack_observed_and_state_observed"
        if ack_complete and rth_state_observed
        else (
            "state_observed_after_dispatch_timeout"
            if state_observed_after_dispatch_timeout
            else (
                "state_not_observed_or_command_unconfirmed"
                if requested
                else "not_requested"
            )
        )
    )
    final_pose = rth_pose or {}
    return {
        "rth_execution_request": {
            "schema_version": "rth_execution_request.v1",
            "request_id": "rth_execution_request:mission_designer_route_blocking",
            "request_status": "approved_for_sitl_rth" if requested else "not_requested",
            "requested_present": requested,
            "route_blocking_verification_ref": (
                "route_blocking_verification:mission_designer_collision_obstacle"
            ),
            "operator_approval_performed": bool(
                emergency_approval is not None
                and emergency_approval.operator_approval_performed is True
            ),
            "sitl_opt_in": True,
            "approved_action": "rtl" if requested else "",
            "px4_route_changed": False,
            "alternate_mission_uploaded": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "rth_command_dispatch": {
            "schema_version": "rth_command_dispatch.v1",
            "dispatch_id": "rth_command_dispatch:mission_designer_route_blocking",
            "dispatch_status": dispatch_status or "not_requested",
            "application_status": dispatch_observation_status,
            "approval_ref": approval_ref,
            "allowlist_ref": allowlist_ref,
            "emergency_dispatch_ref": (
                "px4_gazebo_emergency_command_dispatch_result:"
                f"{emergency_dispatch.dispatch_result_id}"
                if emergency_dispatch is not None
                else ""
            ),
            "command_name": dispatch_dump.get("command_name", ""),
            "command_id": dispatch_dump.get("command_id"),
            "command_ack_observed": ack_observed,
            "command_ack_result_code": ack_result_code,
            "command_ack_result_name": dispatch_dump.get("command_ack_result_name"),
            "completion_basis": completion_basis,
            "mavlink_dispatch_performed": command_sent,
            "bounded_allowlist_enforced": True,
            "approval_free_dispatch_allowed": False,
            "px4_route_changed": False,
            "alternate_mission_uploaded": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "rth_behavior_observation": {
            "schema_version": "rth_behavior_observation.v1",
            "observation_id": (
                "rth_behavior_observation:mission_designer_route_blocking"
            ),
            "observation_status": behavior_status,
            "return_to_home_behavior_observed": behavior_observed,
            "rth_commanded": command_sent,
            "command_ack_observed": ack_observed,
            "completion_basis": completion_basis,
            "rth_state_observed": bool(rth_state_observed),
            "rth_state_label": rth_state_label or "",
            "final_pose_xyz_m": (
                [
                    final_pose.get("x"),
                    final_pose.get("y"),
                    final_pose.get("z"),
                ]
                if final_pose
                else []
            ),
            "rth_sample_count": len(rth_samples),
            "px4_route_changed": False,
            "alternate_mission_uploaded": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
        "rth_outcome": {
            "schema_version": "rth_outcome.v1",
            "outcome_id": "rth_outcome:mission_designer_route_blocking",
            "outcome_status": (
                "rth_behavior_observed"
                if behavior_observed
                else (
                    "rth_behavior_pending_or_unconfirmed"
                    if requested
                    else "not_requested"
                )
            ),
            "return_to_home_behavior_observed": behavior_observed,
            "task_failed": False,
            "delivery_failed": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _point_to_segment_distance_m(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    px, py = point
    sx, sy = start
    ex, ey = end
    dx = ex - sx
    dy = ey - sy
    length_squared = dx * dx + dy * dy
    if length_squared <= 0.0:
        return math.hypot(px - sx, py - sy)
    t = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / length_squared))
    closest_x = sx + t * dx
    closest_y = sy + t * dy
    return math.hypot(px - closest_x, py - closest_y)


def _moving_actor_proximity_evidence_realism(
    *,
    route_start_xy_m: tuple[float, float],
    route_dropoff_xy_m: tuple[float, float],
) -> dict[str, Any]:
    requested = _moving_actor_marker_requested()
    pose_observation = (MOVING_ACTOR_POSE_SUMMARY or {}).get(
        "moving_actor_pose_observation",
        {},
    )
    observed_pose = pose_observation.get("observed") or {}
    pose_samples = [
        observed_pose.get("first_pose_xyz_m"),
        observed_pose.get("second_pose_xyz_m"),
    ]
    actor_xy_samples = [
        [float(sample[0]), float(sample[1])]
        for sample in pose_samples
        if isinstance(sample, list) and len(sample) >= 2
    ]
    advisory_near_route_threshold_m = 2.0
    advisory_near_dropoff_threshold_m = 3.0
    unsupported_reasons: list[str] = []
    if not requested:
        observation_status = "not_requested"
        observed: dict[str, Any] = {}
    elif not actor_xy_samples:
        observation_status = "proximity_not_observed"
        unsupported_reasons.append("moving_actor_pose_samples_missing")
        observed = {
            "source": "moving_actor_pose_observation",
            "observed": False,
            "actor_sample_count": 0,
            "route_blocking_observed": False,
            "incident_observed": False,
            "delivery_completion_claimed": False,
        }
    else:
        route_distances = [
            _point_to_segment_distance_m(
                (sample[0], sample[1]),
                route_start_xy_m,
                route_dropoff_xy_m,
            )
            for sample in actor_xy_samples
        ]
        dropoff_distances = [
            math.hypot(
                sample[0] - route_dropoff_xy_m[0],
                sample[1] - route_dropoff_xy_m[1],
            )
            for sample in actor_xy_samples
        ]
        min_distance_to_route_m = min(route_distances)
        min_distance_to_dropoff_m = min(dropoff_distances)
        advisory_status = (
            "near_route_advisory"
            if min_distance_to_route_m <= advisory_near_route_threshold_m
            else (
                "near_dropoff_advisory"
                if min_distance_to_dropoff_m <= advisory_near_dropoff_threshold_m
                else "clear_advisory"
            )
        )
        observation_status = "proximity_observed"
        observed = {
            "source": "moving_actor_pose_observation",
            "observed": True,
            "actor_sample_count": len(actor_xy_samples),
            "actor_xy_samples_m": actor_xy_samples,
            "route_start_xy_m": list(route_start_xy_m),
            "route_dropoff_xy_m": list(route_dropoff_xy_m),
            "min_distance_to_route_m": min_distance_to_route_m,
            "min_distance_to_dropoff_m": min_distance_to_dropoff_m,
            "advisory_status": advisory_status,
            "advisory_near_route_threshold_m": advisory_near_route_threshold_m,
            "advisory_near_dropoff_threshold_m": advisory_near_dropoff_threshold_m,
            "advisory_only": True,
            "route_blocking_observed": False,
            "incident_observed": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "moving_actor_proximity_evidence": {
            "schema_version": "moving_actor_proximity_evidence.v1",
            "evidence_id": (
                "moving_actor_proximity_evidence:"
                "mission_designer_moving_visual_marker"
            ),
            "condition_kind": "moving_visual_actor_marker",
            "observation_status": observation_status,
            "pose_observation_ref": (
                "moving_actor_pose_observation:" "mission_designer_moving_visual_marker"
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "observer_only": True,
            "simulator_only": True,
            "route_blocking_enabled": False,
            "incident_claimed": False,
            "traffic_conflict_verifier": False,
            "advisory_only": True,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def _trigger_payload_release() -> dict[str, Any] | None:
    if os.getenv(PAYLOAD_RELEASE_MODEL_ENV) != "1":
        return None
    before = _payload_pose_sample()
    observed_at = datetime.now(timezone.utc).isoformat()
    _run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            f"gz topic -t {PAYLOAD_DETACH_TOPIC} -m gz.msgs.Empty -p ''",
        ],
        timeout=10,
    )
    time.sleep(1)
    after = _payload_pose_sample()
    return {
        "payload_release_observed": True,
        "payload_release_event_source": "gazebo_detachable_joint_detach_event",
        "payload_id": "pkg-sitl-dropoff",
        "payload_detach_topic": PAYLOAD_DETACH_TOPIC,
        "payload_pose_before_release": before,
        "payload_release_position_x_m": after["x"],
        "payload_release_position_y_m": after["y"],
        "payload_release_position_z_m": after["z"],
        "payload_release_observed_at": observed_at,
        "gazebo_detachable_joint_release_performed": True,
        "gazebo_detachable_joint_release_observed": True,
        "gazebo_entity_mutation_performed": False,
    }


def _send_helper(mode: str, *args: object, timeout: int = 30) -> dict[str, Any]:
    result = _run(
        ["docker", "exec", "-i", CONTAINER_NAME, "python3", "-", mode, *map(str, args)],
        input_text=MAVLINK_ROUTE_HELPER,
        timeout=timeout,
    )
    return json.loads(result.stdout.strip())


def _observe_mavlink_heartbeat_gap(
    *,
    duration_seconds: float = 3.0,
    gap_threshold_seconds: float = 2.0,
) -> dict[str, Any]:
    result = _run(
        [
            "docker",
            "exec",
            "-i",
            CONTAINER_NAME,
            "python3",
            "-",
            str(duration_seconds),
            str(gap_threshold_seconds),
        ],
        input_text=MAVLINK_HEARTBEAT_OBSERVER_HELPER,
        check=False,
        timeout=int(duration_seconds) + 5,
    )
    if result.returncode != 0:
        return {
            "observer_status": "failed",
            "source": "udp://127.0.0.1:14650",
            "duration_seconds": duration_seconds,
            "gap_threshold_seconds": gap_threshold_seconds,
            "packet_count": 0,
            "heartbeat_count": 0,
            "heartbeat_intervals_seconds": [],
            "max_heartbeat_interval_seconds": 0.0,
            "heartbeat_gap_count": 0,
            "heartbeat_gap_observed": False,
            "observer_sent_packets": False,
            "packet_drop_performed": False,
            "stdout_tail": result.stdout[-500:],
            "stderr_tail": result.stderr[-500:],
        }
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {
            "observer_status": "invalid_output",
            "source": "udp://127.0.0.1:14650",
            "duration_seconds": duration_seconds,
            "gap_threshold_seconds": gap_threshold_seconds,
            "packet_count": 0,
            "heartbeat_count": 0,
            "heartbeat_intervals_seconds": [],
            "max_heartbeat_interval_seconds": 0.0,
            "heartbeat_gap_count": 0,
            "heartbeat_gap_observed": False,
            "observer_sent_packets": False,
            "packet_drop_performed": False,
            "stdout_tail": result.stdout[-500:],
            "stderr_tail": result.stderr[-500:],
        }
    payload["observer_sent_packets"] = False
    payload["packet_drop_performed"] = False
    return payload


def _apply_bounded_mavlink_link_loss(
    *,
    duration_seconds: float = 2.5,
    gap_threshold_seconds: float = 2.0,
) -> dict[str, Any]:
    result = _run(
        [
            "docker",
            "exec",
            "-i",
            CONTAINER_NAME,
            "python3",
            "-",
            str(duration_seconds),
            str(gap_threshold_seconds),
            str(ROUTE_MAVLINK_PX4_PORT),
            str(ROUTE_MAVLINK_LOCAL_PORT),
            str(EMERGENCY_MAVLINK_PX4_PORT),
            str(EMERGENCY_MAVLINK_LOCAL_PORT),
            "0" if os.getenv(SKIP_EMERGENCY_MAVLINK_ENV) == "1" else "1",
        ],
        input_text=MAVLINK_LINK_LOSS_APPLICATOR_HELPER,
        check=False,
        timeout=int(duration_seconds) + 20,
    )
    if result.returncode != 0:
        return {
            "applicator_status": "failed",
            "source": f"udp://127.0.0.1:{ROUTE_MAVLINK_LOCAL_PORT}",
            "duration_seconds": duration_seconds,
            "gap_threshold_seconds": gap_threshold_seconds,
            "packet_count": 0,
            "heartbeat_count": 0,
            "heartbeat_intervals_seconds": [],
            "max_heartbeat_interval_seconds": 0.0,
            "heartbeat_gap_count": 0,
            "heartbeat_gap_observed": False,
            "endpoint_stop_performed": False,
            "endpoint_restart_performed": False,
            "observer_sent_packets": False,
            "packet_drop_performed": False,
            "rf_link_loss_claimed": False,
            "vehicle_failsafe_claimed": False,
            "stdout_tail": result.stdout[-500:],
            "stderr_tail": result.stderr[-500:],
        }
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {
            "applicator_status": "invalid_output",
            "source": f"udp://127.0.0.1:{ROUTE_MAVLINK_LOCAL_PORT}",
            "duration_seconds": duration_seconds,
            "gap_threshold_seconds": gap_threshold_seconds,
            "packet_count": 0,
            "heartbeat_count": 0,
            "heartbeat_intervals_seconds": [],
            "max_heartbeat_interval_seconds": 0.0,
            "heartbeat_gap_count": 0,
            "heartbeat_gap_observed": False,
            "endpoint_stop_performed": False,
            "endpoint_restart_performed": False,
            "observer_sent_packets": False,
            "packet_drop_performed": False,
            "rf_link_loss_claimed": False,
            "vehicle_failsafe_claimed": False,
            "stdout_tail": result.stdout[-500:],
            "stderr_tail": result.stderr[-500:],
        }
    payload["observer_sent_packets"] = False
    payload["packet_drop_performed"] = False
    payload["rf_link_loss_claimed"] = False
    payload["vehicle_failsafe_claimed"] = False
    return payload


def _distance_to_segment_xy(
    *,
    point_xy: tuple[float, float],
    start_xy: tuple[float, float],
    end_xy: tuple[float, float],
) -> float:
    px, py = point_xy
    sx, sy = start_xy
    ex, ey = end_xy
    dx = ex - sx
    dy = ey - sy
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return math.hypot(px - sx, py - sy)
    t = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / length_squared))
    nearest_x = sx + t * dx
    nearest_y = sy + t * dy
    return math.hypot(px - nearest_x, py - nearest_y)


def _send_route_with_monitor(
    *,
    target_x: float,
    target_y: float,
    target_z: float,
    feed_forward_vx_mps: float = 0.0,
    feed_forward_vy_mps: float = 0.0,
    feed_forward_ramp_start_fraction: float = 0.65,
    feed_forward_ramp_end_fraction: float = 0.9,
    expected_target_x: float,
    expected_target_y: float,
    pickup_pose: dict[str, float],
    altitude_max_m: float,
    max_pose_deviation_xy_m: float,
    max_pose_deviation_z_m: float,
    duration_seconds: float,
    timeout: int = 45,
    on_deviation: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    command = [
        "docker",
        "exec",
        "-i",
        CONTAINER_NAME,
        "python3",
        "-",
        "route",
        str(target_x),
        str(target_y),
        str(target_z),
        str(duration_seconds),
        str(feed_forward_vx_mps),
        str(feed_forward_vy_mps),
        str(feed_forward_ramp_start_fraction),
        str(feed_forward_ramp_end_fraction),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write(MAVLINK_ROUTE_HELPER)
    process.stdin.close()
    process.stdin = None
    started_at = time.monotonic()
    deviation_samples: list[dict[str, Any]] = []
    monitor_sample_count = 0
    pickup_xy = (float(pickup_pose["x"]), float(pickup_pose["y"]))
    expected_target_xy = (expected_target_x, expected_target_y)
    while process.poll() is None:
        if time.monotonic() - started_at > timeout:
            process.terminate()
            raise RuntimeError("route helper timed out while monitoring pose")
        sample = _pose_sample()
        _append_live_pose_row("route", sample, sample_index=monitor_sample_count)
        monitor_sample_count += 1
        deviation_xy = _distance_to_segment_xy(
            point_xy=(float(sample["x"]), float(sample["y"])),
            start_xy=pickup_xy,
            end_xy=expected_target_xy,
        )
        deviation_z = abs(float(sample["z"]) - float(altitude_max_m))
        if (
            deviation_xy > max_pose_deviation_xy_m
            or deviation_z > max_pose_deviation_z_m
        ):
            deviation_samples.append(
                {
                    "phase": "route",
                    "sample": sample,
                    "deviation_xy_m": deviation_xy,
                    "deviation_z_m": deviation_z,
                    "threshold_xy_m": max_pose_deviation_xy_m,
                    "threshold_z_m": max_pose_deviation_z_m,
                }
            )
            process.terminate()
            route_stream_stop_reason = "pose_deviation"
            route_stream_forced_kill = False
            try:
                _stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                _stdout, stderr = process.communicate(timeout=5)
                route_stream_stop_reason = "pose_deviation_forced_kill"
                route_stream_forced_kill = True
            recovery_payload = None
            if on_deviation is not None:
                recovery_payload = on_deviation()
            return {
                "mode": "route",
                "sent": False,
                "pose_deviation_aborted": True,
                "deviation_samples": deviation_samples,
                "route_monitor_sample_count": monitor_sample_count,
                "route_stream_terminated_before_recovery_dispatch": True,
                "route_stream_process_returncode": process.returncode,
                "route_stream_stop_reason": route_stream_stop_reason,
                "route_stream_forced_kill": route_stream_forced_kill,
                "feed_forward_velocity_x_mps": feed_forward_vx_mps,
                "feed_forward_velocity_y_mps": feed_forward_vy_mps,
                "feed_forward_phase_schedule": "full_then_linear_ramp_down",
                "feed_forward_ramp_start_fraction": feed_forward_ramp_start_fraction,
                "feed_forward_ramp_end_fraction": feed_forward_ramp_end_fraction,
                "feed_forward_scale_min": None,
                "feed_forward_scale_max": None,
                "feed_forward_scale_sample_count": 0,
                "recovery_payload": recovery_payload,
                "stderr": stderr,
            }
        time.sleep(1)
    stdout, stderr = process.communicate(timeout=5)
    if process.returncode != 0:
        raise RuntimeError(f"route helper failed: {stderr}")
    payload = json.loads(stdout.strip())
    payload["pose_deviation_aborted"] = False
    payload["deviation_samples"] = []
    payload["route_monitor_sample_count"] = monitor_sample_count
    payload["feed_forward_velocity_x_mps"] = float(
        payload.get("feed_forward_velocity_x_mps", feed_forward_vx_mps)
    )
    payload["feed_forward_velocity_y_mps"] = float(
        payload.get("feed_forward_velocity_y_mps", feed_forward_vy_mps)
    )
    payload["feed_forward_phase_schedule"] = payload.get(
        "feed_forward_phase_schedule",
        "full_then_linear_ramp_down",
    )
    payload["feed_forward_ramp_start_fraction"] = float(
        payload.get("feed_forward_ramp_start_fraction", feed_forward_ramp_start_fraction)
    )
    payload["feed_forward_ramp_end_fraction"] = float(
        payload.get("feed_forward_ramp_end_fraction", feed_forward_ramp_end_fraction)
    )
    payload["feed_forward_scale_min"] = payload.get("feed_forward_scale_min")
    payload["feed_forward_scale_max"] = payload.get("feed_forward_scale_max")
    payload["feed_forward_scale_sample_count"] = int(
        payload.get("feed_forward_scale_sample_count") or 0
    )
    return payload


def _send_command(
    command_name: str,
    *,
    approval: Any,
    coupled_allowlist: Any,
) -> None:
    command_id = {
        "arm": MAV_CMD_COMPONENT_ARM_DISARM,
        "takeoff": MAV_CMD_NAV_TAKEOFF,
        "land": MAV_CMD_NAV_LAND,
    }[command_name]
    validate_px4_gazebo_coupled_command_dispatch(
        approval=approval,
        allowlist=coupled_allowlist,
        command_id=command_id,
    )
    result = _send_helper(command_name)
    if (
        result.get("command_ack_observed") is not True
        or result.get("command_ack_result_code") != 0
    ):
        raise RuntimeError(
            f"{command_name}_command_ack_not_accepted: "
            f"{json.dumps(result, sort_keys=True)}"
        )


def _dispatch_emergency_recovery(action: str) -> Any:
    emergency_approval = build_px4_gazebo_emergency_command_approval(
        operator_approval_performed=True,
        approved_recovery_actions=[action],
        now=NOW,
    )
    emergency_allowlist = build_px4_gazebo_emergency_command_allowlist(
        approval=emergency_approval,
        now=NOW,
    )
    emergency_dispatch = run_px4_gazebo_emergency_command_dispatch(
        recovery_action=action,
        approval=emergency_approval,
        allowlist=emergency_allowlist,
        endpoint_port=EMERGENCY_MAVLINK_PX4_PORT,
        local_bind_port=EMERGENCY_MAVLINK_LOCAL_PORT,
        live_mavlink_opt_in=True,
        ack_timeout_seconds=5.0,
        now=NOW,
    )
    return emergency_approval, emergency_allowlist, emergency_dispatch


MULTI_CONDITION_SUPERVISOR_SCOPE = "wind_obstacle_payload_form3_sitl"
WIND_SUPERVISOR_SCOPE = "wind_form3_sitl_only"


def _wind_supervisor_assessment_inputs(
    *,
    selected_bounded_action: str,
    deviation_samples: list[dict[str, Any]],
    supervisor_scope: str = WIND_SUPERVISOR_SCOPE,
    recovery_state_label: str | None = None,
) -> dict[str, Any]:
    wind_profile = _wind_requested_profile().get("requested", {})
    deviation_xy = None
    if deviation_samples:
        deviation_xy = deviation_samples[0].get("deviation_xy_m")
    multi_condition = supervisor_scope == MULTI_CONDITION_SUPERVISOR_SCOPE
    route_blocking = (ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}).get(
        "route_blocking_verification",
        {},
    )
    route_blocking_observed = route_blocking.get("observed") or {}
    route_blocking_active = bool(
        route_blocking.get("verification_status")
        in {"verified", "route_blocking_verified", "blocked"}
        or route_blocking_observed.get("route_blocked") is True
        or route_blocking_observed.get("route_blocking_observed") is True
    )
    payload_application = (VEHICLE_REALISM_SUMMARY or {}).get(
        "payload_simulator_condition_application",
        {},
    )
    payload_advisory = (VEHICLE_REALISM_SUMMARY or {}).get(
        "payload_feasibility_advisory",
        {},
    )
    payload_advisory_active = bool(payload_advisory)
    battery_evidence = (BATTERY_REALISM_SUMMARY or {}).get(
        "observed_battery_condition_evidence",
        {},
    )
    battery_observed = battery_evidence.get("observed") or {}
    battery_warning = battery_observed.get("observed_warning")
    battery_warning_active = False
    if battery_warning is not None:
        try:
            battery_warning_active = int(battery_warning) > 0
        except (TypeError, ValueError):
            battery_warning_active = True
    telemetry_freshness = (TELEMETRY_REALISM_SUMMARY or {}).get(
        "telemetry_freshness_report",
        {},
    )
    telemetry_gap_count = int(telemetry_freshness.get("gap_count") or 0)
    observer_dropout_active = (
        telemetry_freshness.get("freshness_status") == "gap_observed"
        and telemetry_gap_count > 0
    )
    conflicting_risks = []
    if route_blocking_active:
        conflicting_risks.append("route_blocking_active")
    if payload_advisory_active:
        conflicting_risks.append("payload_feasibility_advisory_active")
    if battery_warning_active:
        conflicting_risks.append("battery_warning_active")
    if observer_dropout_active:
        conflicting_risks.append("telemetry_observer_dropout_active")
    secondary_risks = (
        [
            {
                "condition": "route_blocking",
                "risk_state": (
                    "route_blocking_active"
                    if route_blocking_active
                    else "not_active"
                ),
                "silent_continuation_allowed": not route_blocking_active,
                "source_ref": route_blocking.get("verification_id"),
            },
            {
                "condition": "payload_feasibility",
                "risk_state": (
                    "payload_feasibility_advisory_active"
                    if payload_advisory_active
                    else "not_active"
                ),
                "silent_continuation_allowed": not payload_advisory_active,
                "source_ref": payload_application.get("application_id"),
            },
            {
                "condition": "battery_warning",
                "risk_state": (
                    f"warning_{battery_warning}"
                    if battery_warning_active
                    else "nominal_or_unknown"
                ),
                "silent_continuation_allowed": not battery_warning_active,
                "source_ref": battery_evidence.get("evidence_id"),
            },
            {
                "condition": "telemetry_continuity",
                "risk_state": (
                    "observer_dropout_active"
                    if observer_dropout_active
                    else "sufficient_for_recovery_audit"
                ),
                "silent_continuation_allowed": not observer_dropout_active,
                "source_ref": telemetry_freshness.get("report_id"),
            },
        ]
        if multi_condition
        else []
    )
    return {
        "primary_trigger": "wind_drift_exceeded_threshold",
        "assessment_mode": "compound_mission_state_assessment",
        "supervisor_scope": supervisor_scope,
        "condition_priority": [
            "authority_boundary",
            "route_blocking",
            "payload_feasibility",
            "battery_warning",
            "telemetry_continuity",
            "wind_drift",
        ],
        "secondary_risks": secondary_risks,
        "wind": {
            "drift_above_threshold": True,
            "wind_speed_mps": wind_profile.get("wind_mean_mps"),
            "wind_direction_deg": wind_profile.get("wind_direction_deg"),
            "wind_drift_deviation_xy_m": deviation_xy,
            "primary_trigger": True,
        },
        "obstacle": {
            "route_blocking_observed": route_blocking_active,
            "route_blocking_verification_ref": route_blocking.get(
                "verification_id"
            ),
            "verification_status": route_blocking.get("verification_status"),
            "condition_checked": multi_condition,
        },
        "battery": {
            "battery_warning_state": (
                f"warning_{battery_warning}"
                if battery_warning_active
                else "nominal_or_unknown"
            ),
            "battery_evidence_ref": battery_evidence.get("evidence_id"),
            "px4_battery_warning_state_affected": battery_warning_active,
            "condition_checked": True,
        },
        "payload": {
            "payload_feasibility_advisory_active": payload_advisory_active,
            "payload_condition_application_ref": payload_application.get(
                "application_id"
            ),
            "payload_margin_risk": (
                "payload_feasibility_advisory_active"
                if payload_advisory_active
                else "unknown_or_not_active"
            ),
            "condition_checked": multi_condition,
        },
        "route": {
            "route_blocked": route_blocking_active,
            "dropoff_verified": False,
            "delivery_completion_claimed": False,
        },
        "telemetry": {
            "telemetry_continuity": (
                "observer_dropout_active"
                if observer_dropout_active
                else "sufficient_for_recovery_audit"
            ),
            "telemetry_freshness_ref": telemetry_freshness.get("report_id"),
            "observer_dropout_active": observer_dropout_active,
        },
        "recovery_state": {
            "cycle1_recovery_state_label": recovery_state_label,
            "selected_bounded_action": selected_bounded_action,
        },
        "authority": {
            "operator_review_required": True,
            "automatic_dispatch_allowed": False,
            "bounded_action_dispatch_allowed": True,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "conflicting_risks": conflicting_risks,
        "conflict_policy": (
            "operator_review_required_or_form0b_readiness_when_conflict_active"
        ),
        "mission_state_interpretation": (
            "wind_drift_recovery_operator_review_required_due_to_conflicting_risks"
            if conflicting_risks
            else "wind_drift_recovery_required_no_conflicting_blocker_detected"
        ),
    }


def _build_wind_supervisor_cycle(
    *,
    cycle_index: int,
    observation_ref: str,
    response_ref: str,
    selected_bounded_action: str,
    deviation_samples: list[dict[str, Any]],
    dispatch_ref: str | None,
    dispatch_status: str | None,
    approval_ref: str | None,
    outcome_ref: str | None = None,
    outcome_observed: bool = False,
    recovery_state_label: str | None = None,
    pose_z_m: float | None = None,
    supervisor_scope: str = WIND_SUPERVISOR_SCOPE,
) -> dict[str, Any]:
    decision_id = (
        "mission_os_recovery_decision:wind_drift_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_recovery_decision:wind_rtl_state_supervisor_bounded_land"
    )
    request_id = (
        "mission_os_backend_action_request:wind_drift_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_backend_action_request:wind_rtl_state_supervisor_bounded_land"
    )
    receipt_id = (
        "mission_os_backend_action_receipt:wind_drift_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_backend_action_receipt:wind_rtl_state_supervisor_bounded_land"
    )
    outcome_id = (
        "mission_os_recovery_outcome_observation:wind_drift_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_recovery_outcome_observation:wind_rtl_state_supervisor_bounded_land"
    )
    assessment_inputs = _wind_supervisor_assessment_inputs(
        selected_bounded_action=selected_bounded_action,
        deviation_samples=deviation_samples,
        supervisor_scope=supervisor_scope,
        recovery_state_label=recovery_state_label,
    )
    return {
        "cycle_index": cycle_index,
        "decision_ref": decision_id,
        "action_request_ref": request_id,
        "action_receipt_ref": receipt_id,
        "outcome_observation_ref": outcome_id,
        "decision": {
            "schema_version": "mission_os_recovery_decision.v1",
            "decision_id": decision_id,
            "cycle_index": cycle_index,
            "decision_loop_driver": "mission_os_supervisor",
            "supervisor_scope": supervisor_scope,
            "full_gateway_runtime_loop": False,
            "source_observation_ref": observation_ref,
            "mission_response_candidate_ref": response_ref,
            "primary_trigger": "wind_drift_exceeded_threshold",
            "assessment_inputs": assessment_inputs,
            "mission_state_interpretation": assessment_inputs[
                "mission_state_interpretation"
            ],
            "selected_bounded_action": selected_bounded_action,
            "operator_approval_required": True,
            "automatic_dispatch_allowed": False,
            "operator_approved_dispatch_allowed": True,
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "created_dispatch_authority": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "action_request": {
            "schema_version": "mission_os_backend_action_request.v1",
            "request_id": request_id,
            "cycle_index": cycle_index,
            "decision_ref": decision_id,
            "backend_target": "px4_gazebo_sitl",
            "bounded_action": selected_bounded_action,
            "expected_dispatch_ref": dispatch_ref,
            "approval_ref": approval_ref,
            "allowlisted_action": True,
            "operator_approved": True,
            "automatic_dispatch_allowed": False,
            "dispatch_authority_created": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "action_receipt": {
            "schema_version": "mission_os_backend_action_receipt.v1",
            "receipt_id": receipt_id,
            "cycle_index": cycle_index,
            "action_request_ref": request_id,
            "dispatch_ref": dispatch_ref,
            "dispatch_status": dispatch_status,
            "dispatch_observed": str(dispatch_ref or "").startswith(
                "px4_gazebo_emergency_command_dispatch_result:"
            ),
            "backend_target": "px4_gazebo_sitl",
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "outcome_observation": {
            "schema_version": "mission_os_recovery_outcome_observation.v1",
            "observation_id": outcome_id,
            "cycle_index": cycle_index,
            "action_receipt_ref": receipt_id,
            "outcome_observation_ref": outcome_ref,
            "outcome_observed": outcome_observed,
            "state_label": recovery_state_label,
            "pose_z_m": pose_z_m,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _build_wind_supervisor_loop(
    *,
    deviation_samples: list[dict[str, Any]],
    cycle1_dispatch_ref: str | None,
    cycle1_dispatch_status: str | None,
    cycle1_approval_ref: str | None,
    cycle1_outcome_ref: str | None,
    cycle1_outcome_observed: bool,
    cycle1_recovery_state_label: str | None,
    cycle2_dispatch_ref: str | None,
    cycle2_dispatch_status: str | None,
    cycle2_approval_ref: str | None,
    cycle2_outcome_ref: str | None,
    cycle2_outcome_observed: bool,
    cycle2_pose_z_m: float | None,
    supervisor_scope: str = WIND_SUPERVISOR_SCOPE,
) -> dict[str, Any]:
    cycle1 = _build_wind_supervisor_cycle(
        cycle_index=1,
        observation_ref="route_deviation_observation:wind_drift",
        response_ref="mission_response_candidate:wind_drift_bounded_rtl",
        selected_bounded_action="rtl",
        deviation_samples=deviation_samples,
        supervisor_scope=supervisor_scope,
        dispatch_ref=cycle1_dispatch_ref,
        dispatch_status=cycle1_dispatch_status,
        approval_ref=cycle1_approval_ref,
        outcome_ref=cycle1_outcome_ref,
        outcome_observed=cycle1_outcome_observed,
        recovery_state_label=cycle1_recovery_state_label,
    )
    cycle2 = _build_wind_supervisor_cycle(
        cycle_index=2,
        observation_ref=cycle1_outcome_ref or "",
        response_ref="mission_response_candidate:wind_rtl_state_bounded_land",
        selected_bounded_action="land",
        deviation_samples=deviation_samples,
        supervisor_scope=supervisor_scope,
        dispatch_ref=cycle2_dispatch_ref,
        dispatch_status=cycle2_dispatch_status,
        approval_ref=cycle2_approval_ref,
        outcome_ref=cycle2_outcome_ref,
        outcome_observed=cycle2_outcome_observed,
        recovery_state_label=None,
        pose_z_m=cycle2_pose_z_m,
    )
    cycles = [cycle1, cycle2]
    loop_conflicting_risks = sorted(
        {
            risk
            for cycle in cycles
            for risk in (
                (cycle.get("decision") or {})
                .get("assessment_inputs", {})
                .get("conflicting_risks", [])
            )
            if isinstance(risk, str) and risk
        }
    )
    supervisor_loop_claim_supported = bool(
        cycle1_outcome_observed
        and cycle2_outcome_observed
        and not loop_conflicting_risks
    )
    return {
        "schema_version": "mission_os_supervisor_recovery_loop.v1",
        "decision_loop_driver": "mission_os_supervisor",
        "supervisor_scope": supervisor_scope,
        "full_gateway_runtime_loop": False,
        "primary_trigger": "wind_drift_exceeded_threshold",
        "assessment_mode": "compound_mission_state_assessment",
        "secondary_risks": sorted(
            {
                risk["condition"]
                for cycle in cycles
                for risk in (
                    (cycle.get("decision") or {})
                    .get("assessment_inputs", {})
                    .get("secondary_risks", [])
                )
                if isinstance(risk, dict)
            }
        ),
        "cycle_count": 2 if supervisor_loop_claim_supported else 1,
        "observed_cycle_count": (
            2 if cycle1_outcome_observed and cycle2_outcome_observed else 1
        ),
        "supervisor_loop_claim_supported": supervisor_loop_claim_supported,
        "conflicting_risks": loop_conflicting_risks,
        "cycles": cycles,
        "cycle1_supervisor_decision_observed": True,
        "cycle1_backend_action_request_observed": True,
        "cycle1_backend_action_receipt_observed": bool(cycle1_dispatch_ref),
        "cycle1_outcome_observation_observed": cycle1_outcome_observed,
        "cycle2_supervisor_decision_observed": True,
        "cycle2_backend_action_request_observed": True,
        "cycle2_backend_action_receipt_observed": bool(cycle2_dispatch_ref),
        "cycle2_outcome_observation_observed": cycle2_outcome_observed,
        "authority_boundary": {
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "dispatch_authority_created": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _obstacle_supervisor_assessment_inputs(
    *,
    selected_bounded_action: str,
    cycle1_state_label: str | None = None,
) -> dict[str, Any]:
    route_blocking = (ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}).get(
        "route_blocking_verification",
        {},
    )
    route_blocking_observed = route_blocking.get("observed") or {}
    alternate_route = (ALTERNATE_MISSION_UPLOAD_SUMMARY or {}).get(
        "alternate_route_execution_evidence",
        {},
    )
    alternate_route_observed = alternate_route.get("observed") or {}
    battery_evidence = (BATTERY_REALISM_SUMMARY or {}).get(
        "observed_battery_condition_evidence",
        {},
    )
    battery_observed = battery_evidence.get("observed") or {}
    battery_warning = battery_observed.get("observed_warning")
    battery_warning_active = False
    if battery_warning is not None:
        try:
            battery_warning_active = int(battery_warning) > 0
        except (TypeError, ValueError):
            battery_warning_active = True
    telemetry_freshness = (TELEMETRY_REALISM_SUMMARY or {}).get(
        "telemetry_freshness_report",
        {},
    )
    telemetry_gap_count = int(telemetry_freshness.get("gap_count") or 0)
    observer_dropout_active = (
        telemetry_freshness.get("freshness_status") == "gap_observed"
        and telemetry_gap_count > 0
    )
    conflicting_risks = []
    if battery_warning_active:
        conflicting_risks.append("battery_warning_active")
    if observer_dropout_active:
        conflicting_risks.append("telemetry_observer_dropout_active")
    mission_state_interpretation = (
        "obstacle_supervisor_operator_review_required_due_to_conflicting_risks"
        if conflicting_risks
        else "obstacle_alternate_route_completed_no_conflicting_blocker_detected"
    )
    return {
        "primary_trigger": "route_blocking_obstacle_verified",
        "assessment_mode": "compound_mission_state_assessment",
        "obstacle": {
            "route_blocked": bool(
                route_blocking_observed.get("route_blocking_verified")
            ),
            "verification_ref": (
                "route_blocking_verification:mission_designer_collision_obstacle"
            ),
        },
        "alternate_route": {
            "alternate_route_execution_observed": alternate_route.get(
                "alternate_route_execution_observed"
            ),
            "alternate_waypoint_reached_observed": alternate_route.get(
                "alternate_waypoint_reached_observed"
            ),
            "cycle1_state_label": cycle1_state_label,
            "final_distance_to_alternate_waypoint_m": alternate_route_observed.get(
                "final_distance_to_alternate_waypoint_m"
            ),
        },
        "battery": {
            "battery_warning_state": (
                f"warning_{battery_warning}"
                if battery_warning_active
                else "nominal_or_unknown"
            ),
            "px4_battery_warning_state_affected": battery_warning_active,
        },
        "payload": {
            "payload_feasibility_advisory_active": False,
            "payload_margin_risk": "unknown_or_not_active",
        },
        "route": {
            "dropoff_verified": False,
            "delivery_completion_claimed": False,
            "original_dropoff_unverified": True,
        },
        "telemetry": {
            "telemetry_continuity": (
                "observer_dropout_active"
                if observer_dropout_active
                else "sufficient_for_recovery_audit"
            ),
            "observer_dropout_active": observer_dropout_active,
            "gap_count": telemetry_gap_count,
        },
        "recovery_state": {
            "selected_bounded_action": selected_bounded_action,
        },
        "authority": {
            "operator_review_required": True,
            "automatic_dispatch_allowed": False,
            "bounded_action_dispatch_allowed": True,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "conflicting_risks": conflicting_risks,
        "conflict_policy": (
            "operator_review_required_or_form0b_readiness_when_conflict_active"
        ),
        "mission_state_interpretation": mission_state_interpretation,
    }


def _build_obstacle_supervisor_cycle(
    *,
    cycle_index: int,
    observation_ref: str,
    response_ref: str,
    selected_bounded_action: str,
    dispatch_ref: str | None,
    dispatch_status: str | None,
    approval_ref: str | None,
    outcome_ref: str | None = None,
    outcome_observed: bool = False,
    cycle1_state_label: str | None = None,
    pose_z_m: float | None = None,
) -> dict[str, Any]:
    decision_id = (
        "mission_os_recovery_decision:obstacle_supervisor_alternate_route"
        if cycle_index == 1
        else "mission_os_recovery_decision:obstacle_alternate_waypoint_supervisor_bounded_land"
    )
    request_id = (
        "mission_os_backend_action_request:obstacle_supervisor_alternate_route"
        if cycle_index == 1
        else "mission_os_backend_action_request:obstacle_alternate_waypoint_supervisor_bounded_land"
    )
    receipt_id = (
        "mission_os_backend_action_receipt:obstacle_supervisor_alternate_route"
        if cycle_index == 1
        else "mission_os_backend_action_receipt:obstacle_alternate_waypoint_supervisor_bounded_land"
    )
    outcome_id = (
        "mission_os_recovery_outcome_observation:obstacle_supervisor_alternate_route"
        if cycle_index == 1
        else "mission_os_recovery_outcome_observation:obstacle_alternate_waypoint_supervisor_bounded_land"
    )
    assessment_inputs = _obstacle_supervisor_assessment_inputs(
        selected_bounded_action=selected_bounded_action,
        cycle1_state_label=cycle1_state_label,
    )
    dispatch_observed = (
        bool(dispatch_ref)
        if cycle_index == 1
        else str(dispatch_ref or "").startswith(
            "px4_gazebo_emergency_command_dispatch_result:"
        )
    )
    return {
        "cycle_index": cycle_index,
        "decision_ref": decision_id,
        "action_request_ref": request_id,
        "action_receipt_ref": receipt_id,
        "outcome_observation_ref": outcome_id,
        "decision": {
            "schema_version": "mission_os_recovery_decision.v1",
            "decision_id": decision_id,
            "cycle_index": cycle_index,
            "decision_loop_driver": "mission_os_supervisor",
            "supervisor_scope": "obstacle_form3_sitl_only",
            "full_gateway_runtime_loop": False,
            "source_observation_ref": observation_ref,
            "mission_response_candidate_ref": response_ref,
            "primary_trigger": "route_blocking_obstacle_verified",
            "assessment_inputs": assessment_inputs,
            "mission_state_interpretation": assessment_inputs[
                "mission_state_interpretation"
            ],
            "selected_bounded_action": selected_bounded_action,
            "operator_approval_required": True,
            "automatic_dispatch_allowed": False,
            "operator_approved_dispatch_allowed": True,
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "created_dispatch_authority": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "action_request": {
            "schema_version": "mission_os_backend_action_request.v1",
            "request_id": request_id,
            "cycle_index": cycle_index,
            "decision_ref": decision_id,
            "backend_target": "px4_gazebo_sitl",
            "bounded_action": selected_bounded_action,
            "expected_dispatch_ref": dispatch_ref,
            "approval_ref": approval_ref,
            "allowlisted_action": True,
            "operator_approved": True,
            "automatic_dispatch_allowed": False,
            "dispatch_authority_created": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "action_receipt": {
            "schema_version": "mission_os_backend_action_receipt.v1",
            "receipt_id": receipt_id,
            "cycle_index": cycle_index,
            "action_request_ref": request_id,
            "dispatch_ref": dispatch_ref,
            "dispatch_status": dispatch_status,
            "dispatch_observed": dispatch_observed,
            "backend_target": "px4_gazebo_sitl",
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "outcome_observation": {
            "schema_version": "mission_os_recovery_outcome_observation.v1",
            "observation_id": outcome_id,
            "cycle_index": cycle_index,
            "action_receipt_ref": receipt_id,
            "outcome_observation_ref": outcome_ref,
            "outcome_observed": outcome_observed,
            "state_label": cycle1_state_label,
            "pose_z_m": pose_z_m,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _build_obstacle_supervisor_loop() -> dict[str, Any]:
    route_evidence = (ALTERNATE_MISSION_UPLOAD_SUMMARY or {}).get(
        "alternate_route_execution_evidence",
        {},
    )
    route_dispatch = (ALTERNATE_MISSION_UPLOAD_SUMMARY or {}).get(
        "alternate_route_command_dispatch",
        {},
    )
    land_dispatch = (ALTERNATE_LANDING_EXECUTION_SUMMARY or {}).get(
        "alternate_landing_command_dispatch",
        {},
    )
    land_behavior = (ALTERNATE_LANDING_EXECUTION_SUMMARY or {}).get(
        "alternate_landing_behavior_observation",
        {},
    )
    final_pose = land_behavior.get("final_pose_xyz_m") or []
    cycle2_pose_z_m = None
    if isinstance(final_pose, list) and len(final_pose) >= 3:
        try:
            cycle2_pose_z_m = float(final_pose[2])
        except (TypeError, ValueError):
            cycle2_pose_z_m = None
    cycle1_outcome_observed = bool(
        route_evidence.get("alternate_route_execution_observed") is True
        and route_evidence.get("alternate_waypoint_reached_observed") is True
    )
    cycle2_outcome_observed = bool(
        land_behavior.get("alternate_landing_behavior_observed") is True
        and land_behavior.get("landing_observed") is True
    )
    cycle1 = _build_obstacle_supervisor_cycle(
        cycle_index=1,
        observation_ref="route_blocking_verification:mission_designer_collision_obstacle",
        response_ref="mission_response_candidate:obstacle_route_blocking_alternate_route",
        selected_bounded_action="alternate_route",
        dispatch_ref=route_dispatch.get("dispatch_id"),
        dispatch_status=route_dispatch.get("dispatch_status"),
        approval_ref=route_dispatch.get("approval_ref"),
        outcome_ref=route_evidence.get("evidence_id"),
        outcome_observed=cycle1_outcome_observed,
        cycle1_state_label=(
            "alternate_waypoint_reached_observed" if cycle1_outcome_observed else None
        ),
    )
    cycle2 = _build_obstacle_supervisor_cycle(
        cycle_index=2,
        observation_ref=route_evidence.get("evidence_id") or "",
        response_ref="mission_response_candidate:obstacle_alternate_waypoint_bounded_land",
        selected_bounded_action="land",
        dispatch_ref=land_dispatch.get("emergency_dispatch_ref"),
        dispatch_status=land_dispatch.get("dispatch_status"),
        approval_ref=land_dispatch.get("approval_ref"),
        outcome_ref=land_behavior.get("observation_id"),
        outcome_observed=cycle2_outcome_observed,
        pose_z_m=cycle2_pose_z_m,
    )
    cycles = [cycle1, cycle2]
    conflicting_risks = sorted(
        {
            risk
            for cycle in cycles
            for risk in (
                (cycle.get("decision") or {})
                .get("assessment_inputs", {})
                .get("conflicting_risks", [])
            )
        }
    )
    supervisor_loop_claim_supported = bool(
        cycle1_outcome_observed and cycle2_outcome_observed and not conflicting_risks
    )
    return {
        "schema_version": "mission_os_supervisor_recovery_loop.v1",
        "decision_loop_driver": "mission_os_supervisor",
        "supervisor_scope": "obstacle_form3_sitl_only",
        "full_gateway_runtime_loop": False,
        "primary_trigger": "route_blocking_obstacle_verified",
        "assessment_mode": "compound_mission_state_assessment",
        "cycle_count": 2 if supervisor_loop_claim_supported else 1,
        "supervisor_loop_claim_supported": supervisor_loop_claim_supported,
        "conflicting_risks": conflicting_risks,
        "cycles": cycles,
        "cycle1_supervisor_decision_observed": True,
        "cycle1_backend_action_request_observed": True,
        "cycle1_backend_action_receipt_observed": bool(
            route_dispatch.get("dispatch_id")
        ),
        "cycle1_outcome_observation_observed": cycle1_outcome_observed,
        "cycle2_supervisor_decision_observed": True,
        "cycle2_backend_action_request_observed": True,
        "cycle2_backend_action_receipt_observed": bool(
            land_dispatch.get("emergency_dispatch_ref")
        ),
        "cycle2_outcome_observation_observed": cycle2_outcome_observed,
        "authority_boundary": {
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "dispatch_authority_created": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _payload_supervisor_assessment_inputs(
    *,
    selected_bounded_action: str,
    cycle1_state_label: str | None = None,
) -> dict[str, Any]:
    payload_profile = (VEHICLE_REALISM_SUMMARY or {}).get(
        "vehicle_condition_profile", {}
    )
    payload_requested = payload_profile.get("requested") or {}
    battery_evidence = (BATTERY_REALISM_SUMMARY or {}).get(
        "observed_battery_condition_evidence",
        {},
    )
    battery_observed = battery_evidence.get("observed") or {}
    battery_warning = battery_observed.get("observed_warning")
    battery_warning_active = False
    if battery_warning is not None:
        try:
            battery_warning_active = int(battery_warning) > 0
        except (TypeError, ValueError):
            battery_warning_active = True
    telemetry_freshness = (TELEMETRY_REALISM_SUMMARY or {}).get(
        "telemetry_freshness_report",
        {},
    )
    telemetry_gap_count = int(telemetry_freshness.get("gap_count") or 0)
    observer_dropout_active = (
        telemetry_freshness.get("freshness_status") == "gap_observed"
        and telemetry_gap_count > 0
    )
    conflicting_risks = []
    if battery_warning_active:
        conflicting_risks.append("battery_warning_active")
    if observer_dropout_active:
        conflicting_risks.append("telemetry_observer_dropout_active")
    mission_state_interpretation = (
        "payload_supervisor_operator_review_required_due_to_conflicting_risks"
        if conflicting_risks
        else "payload_feasibility_advisory_consumed_no_conflicting_blocker_detected"
    )
    return {
        "primary_trigger": "payload_feasibility_advisory_operator_review_required",
        "assessment_mode": "compound_mission_state_assessment",
        "payload": {
            "payload_feasibility_advisory_active": True,
            "payload_feasibility_advisory_ref": PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX,
            "payload_margin_risk": "payload_feasibility_advisory_active",
            "payload_kg": payload_requested.get("payload_mass_kg"),
        },
        "battery": {
            "battery_warning_state": (
                f"warning_{battery_warning}"
                if battery_warning_active
                else "nominal_or_unknown"
            ),
            "px4_battery_warning_state_affected": battery_warning_active,
        },
        "route": {
            "route_blocked": False,
            "dropoff_verified": False,
            "delivery_completion_claimed": False,
            "original_dropoff_unverified": True,
        },
        "telemetry": {
            "telemetry_continuity": (
                "observer_dropout_active"
                if observer_dropout_active
                else "sufficient_for_recovery_audit"
            ),
            "observer_dropout_active": observer_dropout_active,
            "gap_count": telemetry_gap_count,
        },
        "recovery_state": {
            "cycle1_recovery_state_label": cycle1_state_label,
            "selected_bounded_action": selected_bounded_action,
        },
        "authority": {
            "operator_review_required": True,
            "automatic_dispatch_allowed": False,
            "bounded_action_dispatch_allowed": True,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "conflicting_risks": conflicting_risks,
        "conflict_policy": (
            "operator_review_required_or_form0b_readiness_when_conflict_active"
        ),
        "mission_state_interpretation": mission_state_interpretation,
    }


def _build_payload_supervisor_cycle(
    *,
    cycle_index: int,
    observation_ref: str,
    response_ref: str,
    selected_bounded_action: str,
    dispatch_ref: str | None,
    dispatch_status: str | None,
    approval_ref: str | None,
    outcome_ref: str | None,
    outcome_observed: bool,
    cycle1_state_label: str | None = None,
    outcome_state_label: str | None = None,
    pose_z_m: float | None = None,
) -> dict[str, Any]:
    decision_id = (
        "mission_os_recovery_decision:payload_advisory_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_recovery_decision:payload_rtl_state_supervisor_bounded_land"
    )
    request_id = (
        "mission_os_backend_action_request:payload_advisory_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_backend_action_request:payload_rtl_state_supervisor_bounded_land"
    )
    receipt_id = (
        "mission_os_backend_action_receipt:payload_advisory_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_backend_action_receipt:payload_rtl_state_supervisor_bounded_land"
    )
    outcome_id = (
        "mission_os_recovery_outcome_observation:payload_advisory_supervisor_bounded_rtl"
        if cycle_index == 1
        else "mission_os_recovery_outcome_observation:payload_rtl_state_supervisor_bounded_land"
    )
    assessment_inputs = _payload_supervisor_assessment_inputs(
        selected_bounded_action=selected_bounded_action,
        cycle1_state_label=cycle1_state_label,
    )
    return {
        "cycle_index": cycle_index,
        "decision_ref": decision_id,
        "action_request_ref": request_id,
        "action_receipt_ref": receipt_id,
        "outcome_observation_ref": outcome_id,
        "decision": {
            "schema_version": "mission_os_recovery_decision.v1",
            "decision_id": decision_id,
            "cycle_index": cycle_index,
            "decision_loop_driver": "mission_os_supervisor",
            "supervisor_scope": "payload_form3_sitl_only",
            "full_gateway_runtime_loop": False,
            "source_observation_ref": observation_ref,
            "mission_response_candidate_ref": response_ref,
            "primary_trigger": "payload_feasibility_advisory_operator_review_required",
            "assessment_inputs": assessment_inputs,
            "mission_state_interpretation": assessment_inputs[
                "mission_state_interpretation"
            ],
            "selected_bounded_action": selected_bounded_action,
            "operator_approval_required": True,
            "automatic_dispatch_allowed": False,
            "operator_approved_dispatch_allowed": True,
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "created_dispatch_authority": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "action_request": {
            "schema_version": "mission_os_backend_action_request.v1",
            "request_id": request_id,
            "cycle_index": cycle_index,
            "decision_ref": decision_id,
            "backend_target": "px4_gazebo_sitl",
            "bounded_action": selected_bounded_action,
            "expected_dispatch_ref": dispatch_ref,
            "approval_ref": approval_ref,
            "allowlisted_action": True,
            "operator_approved": True,
            "automatic_dispatch_allowed": False,
            "dispatch_authority_created": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "action_receipt": {
            "schema_version": "mission_os_backend_action_receipt.v1",
            "receipt_id": receipt_id,
            "cycle_index": cycle_index,
            "action_request_ref": request_id,
            "dispatch_ref": dispatch_ref,
            "dispatch_status": dispatch_status,
            "dispatch_observed": str(dispatch_ref or "").startswith(
                "px4_gazebo_emergency_command_dispatch_result:"
            ),
            "backend_target": "px4_gazebo_sitl",
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "outcome_observation": {
            "schema_version": "mission_os_recovery_outcome_observation.v1",
            "observation_id": outcome_id,
            "cycle_index": cycle_index,
            "action_receipt_ref": receipt_id,
            "outcome_observation_ref": outcome_ref,
            "outcome_observed": outcome_observed,
            "state_label": outcome_state_label,
            "pose_z_m": pose_z_m,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _build_payload_supervisor_loop(
    *,
    cycle1_dispatch_ref: str | None,
    cycle1_dispatch_status: str | None,
    cycle1_approval_ref: str | None,
    cycle1_outcome_ref: str | None,
    cycle1_outcome_observed: bool,
    cycle1_recovery_state_label: str | None,
    cycle2_dispatch_ref: str | None,
    cycle2_dispatch_status: str | None,
    cycle2_approval_ref: str | None,
    cycle2_outcome_ref: str | None,
    cycle2_outcome_observed: bool,
    cycle2_state_label: str | None,
    cycle2_pose_z_m: float | None,
) -> dict[str, Any]:
    cycle1 = _build_payload_supervisor_cycle(
        cycle_index=1,
        observation_ref=PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX,
        response_ref="mission_response_candidate:payload_advisory_bounded_rtl",
        selected_bounded_action="rtl",
        dispatch_ref=cycle1_dispatch_ref,
        dispatch_status=cycle1_dispatch_status,
        approval_ref=cycle1_approval_ref,
        outcome_ref=cycle1_outcome_ref,
        outcome_observed=cycle1_outcome_observed,
        cycle1_state_label=cycle1_recovery_state_label,
        outcome_state_label=cycle1_recovery_state_label,
    )
    cycle2 = _build_payload_supervisor_cycle(
        cycle_index=2,
        observation_ref=cycle1_outcome_ref or "",
        response_ref="mission_response_candidate:payload_rtl_state_bounded_land",
        selected_bounded_action="land",
        dispatch_ref=cycle2_dispatch_ref,
        dispatch_status=cycle2_dispatch_status,
        approval_ref=cycle2_approval_ref,
        outcome_ref=cycle2_outcome_ref,
        outcome_observed=cycle2_outcome_observed,
        cycle1_state_label=cycle1_recovery_state_label,
        pose_z_m=cycle2_pose_z_m,
        outcome_state_label=cycle2_state_label,
    )
    cycles = [cycle1, cycle2]
    conflicting_risks = sorted(
        {
            risk
            for cycle in cycles
            for risk in (
                (cycle.get("decision") or {})
                .get("assessment_inputs", {})
                .get("conflicting_risks", [])
            )
        }
    )
    supervisor_loop_claim_supported = bool(
        cycle1_outcome_observed and cycle2_outcome_observed and not conflicting_risks
    )
    return {
        "schema_version": "mission_os_supervisor_recovery_loop.v1",
        "decision_loop_driver": "mission_os_supervisor",
        "supervisor_scope": "payload_form3_sitl_only",
        "full_gateway_runtime_loop": False,
        "primary_trigger": "payload_feasibility_advisory_operator_review_required",
        "assessment_mode": "compound_mission_state_assessment",
        "cycle_count": 2 if supervisor_loop_claim_supported else 1,
        "supervisor_loop_claim_supported": supervisor_loop_claim_supported,
        "conflicting_risks": conflicting_risks,
        "cycles": cycles,
        "cycle1_supervisor_decision_observed": True,
        "cycle1_backend_action_request_observed": True,
        "cycle1_backend_action_receipt_observed": bool(cycle1_dispatch_ref),
        "cycle1_outcome_observation_observed": cycle1_outcome_observed,
        "cycle2_supervisor_decision_observed": True,
        "cycle2_backend_action_request_observed": True,
        "cycle2_backend_action_receipt_observed": bool(cycle2_dispatch_ref),
        "cycle2_outcome_observation_observed": cycle2_outcome_observed,
        "authority_boundary": {
            "ai_judgment_is_gate_verdict": False,
            "ai_judgment_created_dispatch_authority": False,
            "llm_gate_judge_used": False,
            "dispatch_authority_created": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
    }


def _dispatch_alternate_landing_execution() -> Any:
    return _dispatch_emergency_recovery("land")


def _dispatch_rth_behavior_execution() -> Any:
    return _dispatch_emergency_recovery("rtl")


def _payload_advisory_recovery_requested(args: argparse.Namespace) -> bool:
    return args.payload_advisory_recovery_action != "none"


def _validate_payload_advisory_recovery_args(args: argparse.Namespace) -> None:
    if not _payload_advisory_recovery_requested(args):
        if args.mission_os_supervisor_payload_loop:
            raise RuntimeError(
                "payload supervisor loop requires "
                "--payload-advisory-recovery-action rtl and "
                "--post-recovery-action land"
            )
        return
    if not args.payload_feasibility_advisory_ref.startswith(
        PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX
    ):
        raise RuntimeError(
            "payload advisory recovery requires a source-bound "
            f"--payload-feasibility-advisory-ref starting with "
            f"{PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX}"
        )
    if args.mission_os_supervisor_payload_loop and (
        args.payload_advisory_recovery_action != "rtl"
        or args.post_recovery_action != "land"
    ):
        raise RuntimeError(
            "payload supervisor Form 3 requires "
            "--payload-advisory-recovery-action rtl and "
            "--post-recovery-action land"
        )


def _assert_planned_route_stream_budget(*, duration_seconds: float) -> None:
    max_planned_frames = 40 + int(duration_seconds / 0.05) + 2
    if duration_seconds > ROUTE_SETPOINT_STREAM_MAX_DURATION_SECONDS:
        raise RuntimeError("planned route stream duration exceeds allowlist")
    if max_planned_frames > ROUTE_SETPOINT_STREAM_MAX_FRAMES:
        raise RuntimeError("planned route stream frames exceed allowlist")


def _send_until_z(
    command_names: list[str],
    predicate: Callable[[float, list[float]], bool],
    *,
    approval: Any,
    coupled_allowlist: Any,
    timeout: float,
    resend_interval: float = 5.0,
    phase: str = "telemetry",
) -> tuple[dict[str, float], list[dict[str, float]]]:
    deadline = time.monotonic() + timeout
    samples: list[dict[str, float]] = []
    last_sent_at = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now - last_sent_at >= resend_interval:
            for command_name in command_names:
                _send_command(
                    command_name,
                    approval=approval,
                    coupled_allowlist=coupled_allowlist,
                )
            last_sent_at = now
        sample = _pose_sample()
        samples.append(sample)
        _append_live_pose_row(phase, sample, sample_index=len(samples) - 1)
        if predicate(sample["z"], [item["z"] for item in samples]):
            return sample, samples
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for z predicate; samples={samples}")


def _wait_for_z(
    predicate: Callable[[float, list[float]], bool],
    *,
    timeout: float = 60.0,
    phase: str = "telemetry",
) -> tuple[dict[str, float], list[dict[str, float]]]:
    deadline = time.monotonic() + timeout
    samples: list[dict[str, float]] = []
    while time.monotonic() < deadline:
        sample = _pose_sample()
        samples.append(sample)
        _append_live_pose_row(phase, sample, sample_index=len(samples) - 1)
        if predicate(sample["z"], [item["z"] for item in samples]):
            return sample, samples
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for z predicate; samples={samples}")


def _observe_recovery_state(
    *,
    action: str,
    pickup_pose: dict[str, float],
    dispatch_frame_sent: bool,
) -> tuple[bool, str | None, dict[str, float] | None, list[dict[str, float]]]:
    if not dispatch_frame_sent:
        return False, None, None, []
    if action == "land":
        landing_z_threshold = _landing_z_threshold(pickup_pose)
        pose, samples = _wait_for_z(
            lambda z, _samples: z <= landing_z_threshold,
            timeout=80.0,
        )
        return True, None, pose, samples
    if action == "hold":
        samples: list[dict[str, float]] = []
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            samples.append(_pose_sample())
            if "command 17 unsupported" in _logs():
                return (
                    False,
                    "hold_command_unsupported",
                    samples[-1],
                    samples,
                )
            if len(samples) >= 5:
                recent = samples[-5:]
                xy_span = max(
                    math.hypot(
                        item["x"] - recent[0]["x"],
                        item["y"] - recent[0]["y"],
                    )
                    for item in recent
                )
                z_span = max(abs(item["z"] - recent[0]["z"]) for item in recent)
                if xy_span <= 1.0 and z_span <= 0.75:
                    return True, "hold_state_observed", recent[-1], samples
            time.sleep(1)
        return False, None, samples[-1] if samples else None, samples
    if action == "rtl":
        pickup_xy = (float(pickup_pose["x"]), float(pickup_pose["y"]))
        samples = []
        deadline = time.monotonic() + 80.0
        while time.monotonic() < deadline:
            sample = _pose_sample()
            samples.append(sample)
            distance_to_pickup = math.hypot(
                float(sample["x"]) - pickup_xy[0],
                float(sample["y"]) - pickup_xy[1],
            )
            if distance_to_pickup <= 2.0:
                return True, "return_to_launch_state_observed", sample, samples
            time.sleep(1)
        return False, None, samples[-1] if samples else None, samples
    return False, None, None, []


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inject-target-offset-m",
        type=float,
        default=0.0,
        help="Offset the sent route target to exercise pose-deviation aborts.",
    )
    parser.add_argument(
        "--on-deviation-action",
        choices=ROUTE_ON_DEVIATION_ACTIONS,
        default="abort_only",
        help="Action to take after route pose-deviation detection.",
    )
    parser.add_argument(
        "--max-pose-deviation-xy-m",
        type=float,
        default=2.0,
        help=(
            "Horizontal route-deviation threshold for the planned route. "
            "Used by scoped runtime audits such as wind-drift recovery."
        ),
    )
    parser.add_argument(
        "--payload-advisory-recovery-action",
        choices=("none", "land", "rtl", "hold"),
        default="none",
        help=(
            "Operator-approved bounded recovery action to dispatch after the "
            "payload Form 2b advisory is consumed. This is scoped to payload "
            "advisory recovery audits."
        ),
    )
    parser.add_argument(
        "--post-recovery-action",
        choices=("none", "land"),
        default="none",
        help=(
            "Operator-approved bounded action to dispatch after an initial "
            "route-deviation recovery has been observed. This is scoped to "
            "strict Form 3 audits that need a second action-outcome observation."
        ),
    )
    parser.add_argument(
        "--mission-os-supervisor-recovery-loop",
        action="store_true",
        help=(
            "Route wind-drift RTL -> LAND recovery through a scoped Mission OS "
            "supervisor decision loop artifact. This is SITL-only and keeps "
            "hardware/physical authority false."
        ),
    )
    parser.add_argument(
        "--mission-os-supervisor-multi-condition-loop",
        action="store_true",
        help=(
            "Route wind-drift RTL -> LAND recovery through a multi-condition "
            "Mission OS supervisor runtime scope that checks wind, obstacle, "
            "payload, battery, telemetry, recovery state, and authority "
            "dimensions. This is SITL-only, not a full Gateway runtime, and "
            "keeps hardware/physical authority false."
        ),
    )
    parser.add_argument(
        "--mission-os-supervisor-obstacle-loop",
        action="store_true",
        help=(
            "Route obstacle alternate-route -> LAND recovery through a scoped "
            "Mission OS supervisor decision loop artifact. This is SITL-only "
            "and keeps hardware/physical authority false."
        ),
    )
    parser.add_argument(
        "--mission-os-supervisor-payload-loop",
        action="store_true",
        help=(
            "Route payload advisory RTL -> LAND recovery through a scoped "
            "Mission OS supervisor decision loop artifact. This is SITL-only "
            "and keeps hardware/physical authority false."
        ),
    )
    parser.add_argument(
        "--payload-feasibility-advisory-ref",
        default="",
        help=(
            "Source payload_feasibility_advisory.v1 ref consumed by the "
            "payload recovery action."
        ),
    )
    return parser.parse_args()


def main() -> int:
    global LIVE_POSE_TRACE_PATH, PAYLOAD_RELEASE_SUMMARY, WIND_REALISM_SUMMARY, THERMAL_WEATHER_REALISM_SUMMARY, VEHICLE_REALISM_SUMMARY, BATTERY_REALISM_SUMMARY, SENSOR_REALISM_SUMMARY, WORLD_REALISM_SUMMARY, VISIBILITY_REALISM_SUMMARY, OPERATIONAL_REALISM_SUMMARY, MOVING_ACTOR_LINEAR_MOTION_SUMMARY, MOVING_ACTOR_POSE_SUMMARY, MOVING_ACTOR_PROXIMITY_SUMMARY, COLLISION_OBSTACLE_SUMMARY, ROUTE_BLOCKING_CANDIDATE_SUMMARY, HORIZONTAL_CONTACT_TOPIC_SUMMARY, OPERATIONAL_INCIDENT_REPORT_SUMMARY, TRAFFIC_CONFLICT_VERIFICATION_SUMMARY, ROUTE_BLOCKING_VERIFICATION_SUMMARY, ALTERNATE_LANDING_CANDIDATE_SUMMARY, ALTERNATE_LANDING_EXECUTION_SUMMARY, ALTERNATE_MISSION_UPLOAD_SUMMARY, RTH_BEHAVIOR_SUMMARY, TELEMETRY_REALISM_SUMMARY, MAVLINK_LINK_REALISM_SUMMARY, TERRAIN_WORLD_REALISM_SUMMARY, TELEMETRY_DROPOUT_EVENTS, TELEMETRY_OBSERVER_SAMPLE_EVENTS
    args = _parse_args()
    _require_opt_in()
    run_dir = _new_run_dir()
    LIVE_POSE_TRACE_PATH = run_dir / "pose_samples.jsonl"
    LIVE_POSE_TRACE_PATH.write_text("")
    PAYLOAD_RELEASE_SUMMARY = None
    WIND_REALISM_SUMMARY = None
    THERMAL_WEATHER_REALISM_SUMMARY = None
    VEHICLE_REALISM_SUMMARY = None
    BATTERY_REALISM_SUMMARY = None
    SENSOR_REALISM_SUMMARY = None
    WORLD_REALISM_SUMMARY = None
    VISIBILITY_REALISM_SUMMARY = None
    OPERATIONAL_REALISM_SUMMARY = None
    MOVING_ACTOR_LINEAR_MOTION_SUMMARY = None
    MOVING_ACTOR_POSE_SUMMARY = None
    MOVING_ACTOR_PROXIMITY_SUMMARY = None
    COLLISION_OBSTACLE_SUMMARY = None
    ROUTE_BLOCKING_CANDIDATE_SUMMARY = None
    HORIZONTAL_CONTACT_TOPIC_SUMMARY = None
    OPERATIONAL_INCIDENT_REPORT_SUMMARY = None
    TRAFFIC_CONFLICT_VERIFICATION_SUMMARY = None
    ROUTE_BLOCKING_VERIFICATION_SUMMARY = None
    ALTERNATE_LANDING_CANDIDATE_SUMMARY = None
    ALTERNATE_LANDING_EXECUTION_SUMMARY = None
    RTH_BEHAVIOR_SUMMARY = None
    ALTERNATE_MISSION_UPLOAD_SUMMARY = None
    TELEMETRY_REALISM_SUMMARY = None
    MAVLINK_LINK_REALISM_SUMMARY = None
    TERRAIN_WORLD_REALISM_SUMMARY = None
    TELEMETRY_DROPOUT_EVENTS = []
    TELEMETRY_OBSERVER_SAMPLE_EVENTS = []
    _validate_payload_advisory_recovery_args(args)
    payload_model_root = _start_container(run_dir)
    try:
        _wait_for_px4_home()
        TERRAIN_WORLD_REALISM_SUMMARY = _terrain_world_readback(payload_model_root)
        WIND_REALISM_SUMMARY = _apply_wind_realism(payload_model_root)
        THERMAL_WEATHER_REALISM_SUMMARY = _thermal_weather_realism()
        VEHICLE_REALISM_SUMMARY = _vehicle_payload_mass_realism(
            payload_model_root=payload_model_root
        )
        BATTERY_REALISM_SUMMARY = _battery_realism()
        SENSOR_REALISM_SUMMARY = _sensor_failure_realism()
        WORLD_REALISM_SUMMARY = _landing_zone_blocked_realism(
            payload_model_root=payload_model_root
        )
        VISIBILITY_REALISM_SUMMARY = _visibility_realism(
            payload_model_root=payload_model_root
        )
        OPERATIONAL_REALISM_SUMMARY = _operational_no_fly_zone_realism(
            payload_model_root=payload_model_root
        )
        MAVLINK_LINK_REALISM_SUMMARY = _mavlink_link_degradation_realism()
        with TemporaryDirectory() as tmp:
            task_db_path = Path(tmp) / "tasks.db"
            store = TaskStore(str(task_db_path))
            task = store.create(
                kind="px4_gazebo_horizontal_route_delivery",
                title="PX4/Gazebo horizontal route delivery smoke",
                status="running",
                artifacts={
                    "existing": {
                        "case_id": "actual-px4-gazebo-horizontal-route",
                        "kept": True,
                    }
                },
            )
            route = build_px4_gazebo_pickup_dropoff_route_plan(
                pickup_pad_ref="gazebo_pad:pickup",
                dropoff_pad_ref="gazebo_pad:dropoff",
                route_waypoint_refs=["gazebo_waypoint:mid"],
                geofence_polygon=[
                    (-2.0, -2.0),
                    (5.75, -2.0),
                    (5.75, 10.0),
                    (-2.0, 10.0),
                ],
                altitude_min_m=1.0,
                altitude_max_m=2.5,
                min_battery_margin_pct=25.0,
                route_completion_radius_m=0.8,
                max_pose_deviation_xy_m=args.max_pose_deviation_xy_m,
                on_deviation_action=args.on_deviation_action,
                now=NOW,
            )
            approval = build_px4_gazebo_coupled_command_approval(
                operator_approval_performed=True,
                now=NOW,
            )
            coupled_allowlist = build_px4_gazebo_coupled_command_allowlist(
                approval=approval,
                now=NOW,
            )
            route_allowlist = build_px4_gazebo_route_command_allowlist(
                route_plan=route,
                approval=approval,
                now=NOW,
            )
            persisted = store.update(
                task["task_id"],
                artifacts={
                    "px4_gazebo_pickup_dropoff_route_plan": route.model_dump(
                        mode="json"
                    ),
                    "px4_gazebo_coupled_command_approval": approval.model_dump(
                        mode="json"
                    ),
                    "px4_gazebo_coupled_command_allowlist": coupled_allowlist.model_dump(
                        mode="json"
                    ),
                    "px4_gazebo_route_command_allowlist": route_allowlist.model_dump(
                        mode="json"
                    ),
                },
            )
            assert persisted is not None

            preupload_summary = PREUPLOAD_SUMMARY

            pickup_pose = _pose_sample()
            _append_live_pose_row("pickup", pickup_pose)
            _enroute_pose, climb_samples = _send_until_z(
                ["arm", "takeoff"],
                lambda z, _samples: z >= 1.0,
                approval=approval,
                coupled_allowlist=coupled_allowlist,
                timeout=75.0,
                phase="climb",
            )
            if _payload_advisory_recovery_requested(args):
                payload_route_progress_payload = None
                payload_route_pose = None
                payload_pre_recovery_distance_to_pickup_m = None
                payload_route_progress_away_from_pickup_observed = False
                if args.mission_os_supervisor_payload_loop:
                    route_delta_x, route_delta_y, target_z = (
                        derive_px4_gazebo_route_target_ned(route)
                    )
                    route_origin_x, route_origin_y = _terrain_relative_xy_origin(
                        pickup_pose
                    )
                    target_x = route_origin_x + route_delta_x
                    target_y = route_origin_y + route_delta_y
                    _assert_planned_route_stream_budget(duration_seconds=12.0)
                    payload_route_progress_payload = _send_route_with_monitor(
                        target_x=target_x,
                        target_y=target_y,
                        target_z=target_z,
                        expected_target_x=target_x,
                        expected_target_y=target_y,
                        pickup_pose=pickup_pose,
                        altitude_max_m=route.altitude_max_m,
                        max_pose_deviation_xy_m=10.0,
                        max_pose_deviation_z_m=3.0,
                        duration_seconds=12.0,
                        timeout=25,
                    )
                    payload_route_pose = _pose_sample()
                    _append_live_pose_row(
                        "payload_pre_recovery_route", payload_route_pose
                    )
                    payload_pre_recovery_distance_to_pickup_m = math.hypot(
                        float(payload_route_pose["x"]) - float(pickup_pose["x"]),
                        float(payload_route_pose["y"]) - float(pickup_pose["y"]),
                    )
                    payload_route_progress_away_from_pickup_observed = (
                        payload_pre_recovery_distance_to_pickup_m >= 2.5
                    )
                    if not payload_route_progress_away_from_pickup_observed:
                        raise RuntimeError(
                            "payload supervisor Form 3 requires route progress "
                            "away from pickup before bounded RTL"
                        )
                (
                    payload_recovery_approval,
                    payload_recovery_allowlist,
                    payload_recovery_dispatch,
                ) = _dispatch_emergency_recovery(args.payload_advisory_recovery_action)
                (
                    payload_recovery_state_observed,
                    payload_recovery_state_label,
                    payload_recovery_pose,
                    payload_recovery_samples,
                ) = _observe_recovery_state(
                    action=args.payload_advisory_recovery_action,
                    pickup_pose=pickup_pose,
                    dispatch_frame_sent=payload_recovery_dispatch.frame_sent is True,
                )
                payload_recovery_completed = payload_recovery_state_observed
                payload_recovery_distance_to_pickup_m = (
                    None
                    if payload_recovery_pose is None
                    else math.hypot(
                        float(payload_recovery_pose["x"]) - float(pickup_pose["x"]),
                        float(payload_recovery_pose["y"]) - float(pickup_pose["y"]),
                    )
                )
                payload_recovery_approval_ref = (
                    "px4_gazebo_emergency_command_approval:"
                    f"{payload_recovery_approval.approval_id}"
                )
                payload_recovery_dispatch_ref = (
                    "px4_gazebo_emergency_command_dispatch_result:"
                    f"{payload_recovery_dispatch.dispatch_result_id}"
                )
                payload_recovery_outcome_ref = PAYLOAD_RECOVERY_ACTION_REF
                post_recovery_approval = None
                post_recovery_allowlist = None
                post_recovery_dispatch = None
                post_recovery_pose = None
                post_recovery_samples: list[dict[str, float]] = []
                post_recovery_action_taken = None
                post_recovery_dispatch_ref = None
                payload_supervisor_post_recovery_action_ref = None
                payload_supervisor_post_recovery_action = None
                post_recovery_completed = False
                post_recovery_state_observed = False
                post_recovery_state_label = None
                post_recovery_dispatch_status = None
                post_recovery_pose_z_m = None
                mission_os_supervisor_recovery_loop = None
                if (
                    args.mission_os_supervisor_payload_loop
                    and payload_recovery_completed
                    and args.post_recovery_action != "none"
                ):
                    post_recovery_action_taken = args.post_recovery_action
                    (
                        post_recovery_approval,
                        post_recovery_allowlist,
                        post_recovery_dispatch,
                    ) = _dispatch_emergency_recovery(args.post_recovery_action)
                    post_recovery_dispatch_ref = (
                        "px4_gazebo_emergency_command_dispatch_result:"
                        f"{post_recovery_dispatch.dispatch_result_id}"
                    )
                    (
                        post_recovery_state_observed,
                        post_recovery_state_label,
                        post_recovery_pose,
                        post_recovery_samples,
                    ) = _observe_recovery_state(
                        action=args.post_recovery_action,
                        pickup_pose=pickup_pose,
                        dispatch_frame_sent=post_recovery_dispatch.frame_sent is True,
                    )
                    post_recovery_completed = post_recovery_state_observed
                    post_recovery_dispatch_status = (
                        post_recovery_dispatch.dispatch_status
                    )
                    post_recovery_pose_z_m = (
                        None if post_recovery_pose is None else post_recovery_pose["z"]
                    )
                    payload_supervisor_post_recovery_action_ref = (
                        "payload_supervisor_post_recovery_action:"
                        "mission_designer_payload_mass"
                    )
                    payload_supervisor_post_recovery_action = {
                        "schema_version": "payload_supervisor_post_recovery_action.v1",
                        "action_id": payload_supervisor_post_recovery_action_ref,
                        "action_ref": payload_supervisor_post_recovery_action_ref,
                        "condition_kind": "payload_mass_supervisor_form3_recovery",
                        "causal_form": "Form 2a",
                        "form2_subtype": "Form 2a",
                        "trigger_level": "level_2_inferred",
                        "mission_response_kind": "action",
                        "decision_loop_driver": "mission_os_supervisor",
                        "supervisor_scope": "payload_form3_sitl_only",
                        "full_gateway_runtime_loop": False,
                        "source_cycle1_outcome_ref": payload_recovery_outcome_ref,
                        "payload_feasibility_advisory_ref": (
                            args.payload_feasibility_advisory_ref
                        ),
                        "advisory_ref": args.payload_feasibility_advisory_ref,
                        "operator_approval_required": True,
                        "operator_approval_performed": True,
                        "approval_ref": (
                            "px4_gazebo_emergency_command_approval:"
                            f"{post_recovery_approval.approval_id}"
                        ),
                        "dispatch_ref": post_recovery_dispatch_ref,
                        "bounded_action_ref": post_recovery_dispatch_ref,
                        "bounded_action_kind": args.post_recovery_action,
                        "dispatch_status": post_recovery_dispatch.dispatch_status,
                        "command_ack_observed": (
                            post_recovery_dispatch.command_ack_observed
                        ),
                        "command_ack_result_name": (
                            post_recovery_dispatch.command_ack_result_name
                        ),
                        "recovery_state_observed": post_recovery_state_observed,
                        "recovery_state_label": post_recovery_state_label,
                        "recovery_completed": post_recovery_completed,
                        "recovery_pose_z_m": post_recovery_pose_z_m,
                        "automatic_dispatch_suppressed": False,
                        "approval_free_recovery_dispatch_allowed": False,
                        "auto_gate": False,
                        "task_status_mutated": False,
                        "gate_status_mutated": False,
                        "dropoff_verified": False,
                        "delivery_completion_claimed": False,
                        "hardware_target_allowed": False,
                        "physical_execution_invoked": False,
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                    }
                    mission_os_supervisor_recovery_loop = (
                        _build_payload_supervisor_loop(
                            cycle1_dispatch_ref=payload_recovery_dispatch_ref,
                            cycle1_dispatch_status=(
                                payload_recovery_dispatch.dispatch_status
                            ),
                            cycle1_approval_ref=payload_recovery_approval_ref,
                            cycle1_outcome_ref=payload_recovery_outcome_ref,
                            cycle1_outcome_observed=payload_recovery_completed,
                            cycle1_recovery_state_label=payload_recovery_state_label,
                            cycle2_dispatch_ref=post_recovery_dispatch_ref,
                            cycle2_dispatch_status=post_recovery_dispatch_status,
                            cycle2_approval_ref=(
                                "px4_gazebo_emergency_command_approval:"
                                f"{post_recovery_approval.approval_id}"
                            ),
                            cycle2_outcome_ref=(
                                payload_supervisor_post_recovery_action_ref
                            ),
                            cycle2_outcome_observed=post_recovery_completed,
                            cycle2_state_label=post_recovery_state_label,
                            cycle2_pose_z_m=post_recovery_pose_z_m,
                        )
                    )
                final_status = (
                    "payload_supervisor_post_recovery_land_observed"
                    if post_recovery_completed
                    else (
                        "payload_supervisor_post_recovery_unconfirmed"
                        if args.mission_os_supervisor_payload_loop
                        else (
                            f"payload_advisory_recovered_{args.payload_advisory_recovery_action}"
                            if payload_recovery_completed
                            else "payload_advisory_recovery_unconfirmed"
                        )
                    )
                )
                task_status = (
                    "completed"
                    if (
                        post_recovery_completed
                        if args.mission_os_supervisor_payload_loop
                        else payload_recovery_completed
                    )
                    else "blocked"
                )
                payload_recovery_action = {
                    "schema_version": "payload_recovery_action.v1",
                    "action_id": PAYLOAD_RECOVERY_ACTION_REF,
                    "action_ref": PAYLOAD_RECOVERY_ACTION_REF,
                    "condition_kind": "payload_mass_feasibility_recovery",
                    "causal_form": "Form 2a",
                    "form2_subtype": "Form 2a",
                    "trigger_level": "level_2_inferred",
                    "mission_response_kind": "action",
                    "payload_feasibility_advisory_ref": (
                        args.payload_feasibility_advisory_ref
                    ),
                    "advisory_ref": args.payload_feasibility_advisory_ref,
                    "advisory_consumed_by_ref": PAYLOAD_RECOVERY_ACTION_REF,
                    "advisory_lifecycle_state": "reviewed_consumed_by_action_pr",
                    "operator_approval_required": True,
                    "operator_approval_performed": True,
                    "approval_ref": payload_recovery_approval_ref,
                    "dispatch_ref": payload_recovery_dispatch_ref,
                    "bounded_action_ref": payload_recovery_dispatch_ref,
                    "bounded_action_kind": args.payload_advisory_recovery_action,
                    "dispatch_status": payload_recovery_dispatch.dispatch_status,
                    "command_ack_observed": payload_recovery_dispatch.command_ack_observed,
                    "command_ack_result_name": (
                        payload_recovery_dispatch.command_ack_result_name
                    ),
                    "recovery_state_observed": payload_recovery_state_observed,
                    "recovery_state_label": payload_recovery_state_label,
                    "recovery_completed": payload_recovery_completed,
                    "recovery_pose_z_m": (
                        None
                        if payload_recovery_pose is None
                        else payload_recovery_pose["z"]
                    ),
                    "automatic_dispatch_suppressed": False,
                    "approval_free_recovery_dispatch_allowed": False,
                    "auto_gate": False,
                    "task_status_mutated": False,
                    "gate_status_mutated": False,
                    "dropoff_verified": False,
                    "delivery_completion_claimed": False,
                    "hardware_target_allowed": False,
                    "physical_execution_invoked": False,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                }
                updated_payload_recovery = store.update(
                    task["task_id"],
                    status=task_status,
                    artifacts={
                        key: value
                        for key, value in {
                            "px4_gazebo_emergency_command_approval": (
                                payload_recovery_approval.model_dump(mode="json")
                            ),
                            "px4_gazebo_emergency_command_allowlist": (
                                payload_recovery_allowlist.model_dump(mode="json")
                            ),
                            "px4_gazebo_emergency_command_dispatch_result": (
                                payload_recovery_dispatch.model_dump(mode="json")
                            ),
                            "payload_recovery_action": payload_recovery_action,
                            "px4_gazebo_post_recovery_emergency_command_approval": (
                                None
                                if post_recovery_approval is None
                                else post_recovery_approval.model_dump(mode="json")
                            ),
                            "px4_gazebo_post_recovery_emergency_command_allowlist": (
                                None
                                if post_recovery_allowlist is None
                                else post_recovery_allowlist.model_dump(mode="json")
                            ),
                            "px4_gazebo_post_recovery_emergency_command_dispatch_result": (
                                None
                                if post_recovery_dispatch is None
                                else post_recovery_dispatch.model_dump(mode="json")
                            ),
                            "payload_supervisor_post_recovery_action": (
                                payload_supervisor_post_recovery_action
                            ),
                            "mission_os_supervisor_recovery_loop": (
                                mission_os_supervisor_recovery_loop
                            ),
                        }.items()
                        if value is not None
                    },
                )
                assert updated_payload_recovery is not None
                _refresh_battery_realism_observation_from_trace()
                TELEMETRY_REALISM_SUMMARY = _telemetry_observer_dropout_realism()
                VEHICLE_REALISM_SUMMARY = _vehicle_payload_mass_realism(
                    payload_model_root=payload_model_root
                )
                summary = {
                    "artifact_dir": str(run_dir),
                    "task_status": updated_payload_recovery["status"],
                    "existing_artifacts_retained": updated_payload_recovery[
                        "artifacts"
                    ]["existing"]["kept"],
                    "final_status": final_status,
                    "actual_px4_gazebo_horizontal_smoke_observed": True,
                    "dropoff_region_reached": False,
                    "dropoff_verified": False,
                    "delivery_completion_claimed": False,
                    "payload_feasibility_advisory_ref": (
                        args.payload_feasibility_advisory_ref
                    ),
                    "payload_recovery_action_ref": PAYLOAD_RECOVERY_ACTION_REF,
                    "payload_advisory_consumed_by_ref": PAYLOAD_RECOVERY_ACTION_REF,
                    "payload_recovery_action": args.payload_advisory_recovery_action,
                    "payload_recovery_approval_ref": payload_recovery_approval_ref,
                    "payload_recovery_dispatch_ref": payload_recovery_dispatch_ref,
                    "payload_recovery_dispatch_status": (
                        payload_recovery_dispatch.dispatch_status
                    ),
                    "payload_recovery_command_ack_observed": (
                        payload_recovery_dispatch.command_ack_observed
                    ),
                    "payload_recovery_command_ack_result_name": (
                        payload_recovery_dispatch.command_ack_result_name
                    ),
                    "payload_recovery_state_observed": (
                        payload_recovery_state_observed
                    ),
                    "payload_recovery_state_label": payload_recovery_state_label,
                    "payload_recovery_completed": payload_recovery_completed,
                    "payload_recovery_pose_z_m": (
                        None
                        if payload_recovery_pose is None
                        else payload_recovery_pose["z"]
                    ),
                    "payload_route_progress_payload": payload_route_progress_payload,
                    "payload_route_progress_away_from_pickup_observed": (
                        payload_route_progress_away_from_pickup_observed
                    ),
                    "payload_pre_recovery_distance_to_pickup_m": (
                        payload_pre_recovery_distance_to_pickup_m
                    ),
                    "payload_recovery_distance_to_pickup_m": (
                        payload_recovery_distance_to_pickup_m
                    ),
                    "payload_recovery_action_artifact": payload_recovery_action,
                    "post_recovery_action_taken": post_recovery_action_taken,
                    "post_recovery_dispatch_ref": post_recovery_dispatch_ref,
                    "post_recovery_dispatch_status": post_recovery_dispatch_status,
                    "post_recovery_command_ack_observed": (
                        None
                        if post_recovery_dispatch is None
                        else post_recovery_dispatch.command_ack_observed
                    ),
                    "post_recovery_command_ack_result_name": (
                        None
                        if post_recovery_dispatch is None
                        else post_recovery_dispatch.command_ack_result_name
                    ),
                    "post_recovery_state_observed": post_recovery_state_observed,
                    "post_recovery_state_label": post_recovery_state_label,
                    "post_recovery_completed": post_recovery_completed,
                    "post_recovery_pose_z_m": post_recovery_pose_z_m,
                    "payload_supervisor_post_recovery_action_ref": (
                        payload_supervisor_post_recovery_action_ref
                    ),
                    "payload_supervisor_post_recovery_action_artifact": (
                        payload_supervisor_post_recovery_action
                    ),
                    "decision_loop_driver": (
                        "mission_os_supervisor"
                        if mission_os_supervisor_recovery_loop is not None
                        else "scripted_payload_recovery_smoke"
                    ),
                    "supervisor_scope": (
                        "payload_form3_sitl_only"
                        if mission_os_supervisor_recovery_loop is not None
                        else None
                    ),
                    "full_gateway_runtime_loop": False,
                    "supervisor_loop_claim_supported": (
                        None
                        if mission_os_supervisor_recovery_loop is None
                        else mission_os_supervisor_recovery_loop[
                            "supervisor_loop_claim_supported"
                        ]
                    ),
                    "mission_os_supervisor_recovery_loop": (
                        mission_os_supervisor_recovery_loop
                    ),
                    "setpoint_frames_sent": 0,
                    "hardware_target_allowed": False,
                    "physical_execution_invoked": False,
                    "px4_mission_upload_allowed": False,
                    **_wind_realism_summary_artifacts(
                        cleanup_status="teardown_required_after_summary"
                    ),
                    **_vehicle_realism_summary_artifacts(),
                }
                _write_json(run_dir / "summary.json", summary)
                _write_json(
                    run_dir / "mission_artifacts.json",
                    {
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                        "frozen_for_test": False,
                        "artifacts": updated_payload_recovery["artifacts"],
                    },
                )
                recovery_rows = [
                    {
                        "phase": f"payload_recovery_{args.payload_advisory_recovery_action}",
                        "sample_index": index,
                        "sample": sample,
                    }
                    for index, sample in enumerate(payload_recovery_samples)
                ]
                if payload_route_pose is not None:
                    recovery_rows.insert(
                        0,
                        {
                            "phase": "payload_pre_recovery_route",
                            "sample": payload_route_pose,
                        },
                    )
                if payload_recovery_pose is not None:
                    recovery_rows.append(
                        {
                            "phase": "payload_recovery_completed",
                            "sample": payload_recovery_pose,
                        }
                    )
                recovery_rows.extend(
                    {
                        "phase": f"payload_post_recovery_{args.post_recovery_action}",
                        "sample_index": index,
                        "sample": sample,
                    }
                    for index, sample in enumerate(post_recovery_samples)
                )
                if post_recovery_pose is not None:
                    recovery_rows.append(
                        {
                            "phase": "payload_post_recovery_completed",
                            "sample": post_recovery_pose,
                        }
                    )
                _write_jsonl(run_dir / "pose_samples.jsonl", recovery_rows)
                (run_dir / "px4_docker.log").write_text(_all_logs())
                shutil.copy2(task_db_path, run_dir / "tasks.db")
                print(json.dumps(summary, indent=2, sort_keys=True))
                assert summary["delivery_completion_claimed"] is False
                assert summary["hardware_target_allowed"] is False
                assert summary["physical_execution_invoked"] is False
                assert summary["payload_feasibility_advisory_ref"].startswith(
                    PAYLOAD_FEASIBILITY_ADVISORY_REF_PREFIX
                )
                assert summary["payload_advisory_consumed_by_ref"] == (
                    PAYLOAD_RECOVERY_ACTION_REF
                )
                if args.payload_advisory_recovery_action == "land":
                    assert summary["payload_recovery_dispatch_status"] in (
                        "accepted",
                        "timeout",
                    )
                    assert summary["payload_recovery_completed"] is True
                    assert summary["payload_recovery_state_observed"] is True
                    assert float(summary["payload_recovery_pose_z_m"]) <= (
                        _landing_z_threshold(pickup_pose)
                    )
                if args.payload_advisory_recovery_action == "rtl":
                    assert summary["payload_recovery_dispatch_status"] in (
                        "accepted",
                        "timeout",
                    )
                    assert summary["payload_recovery_completed"] is True
                    assert summary["payload_recovery_state_observed"] is True
                    assert (
                        summary["payload_recovery_state_label"]
                        == "return_to_launch_state_observed"
                    )
                if args.mission_os_supervisor_payload_loop:
                    assert summary["decision_loop_driver"] == "mission_os_supervisor"
                    assert summary["supervisor_scope"] == "payload_form3_sitl_only"
                    assert summary["full_gateway_runtime_loop"] is False
                    assert summary["supervisor_loop_claim_supported"] is True
                    assert (
                        summary["payload_route_progress_away_from_pickup_observed"]
                        is True
                    )
                    assert (
                        float(summary["payload_pre_recovery_distance_to_pickup_m"])
                        >= 2.5
                    )
                    assert (
                        float(summary["payload_recovery_distance_to_pickup_m"]) <= 2.0
                    )
                    assert float(
                        summary["payload_recovery_distance_to_pickup_m"]
                    ) < float(summary["payload_pre_recovery_distance_to_pickup_m"])
                    assert summary["post_recovery_action_taken"] == "land"
                    assert summary["post_recovery_dispatch_status"] in (
                        "accepted",
                        "timeout",
                    )
                    assert summary["post_recovery_completed"] is True
                    assert summary["post_recovery_state_observed"] is True
                    assert float(summary["post_recovery_pose_z_m"]) <= (
                        _landing_z_threshold(pickup_pose)
                    )
                return 0

            route_delta_x, route_delta_y, target_z = (
                derive_px4_gazebo_route_target_ned(route)
            )
            route_origin_x, route_origin_y = _terrain_relative_xy_origin(pickup_pose)
            target_x = route_origin_x + route_delta_x
            target_y = route_origin_y + route_delta_y
            form2a_wind_compensation = _form2a_wind_compensation_request()
            compensation_offset_x, compensation_offset_y = (
                _form2a_wind_compensation_xy_offset(form2a_wind_compensation)
            )
            feed_forward_vx_mps, feed_forward_vy_mps = (
                _form2a_wind_feed_forward_xy_mps(form2a_wind_compensation)
            )
            sent_target_x = (
                target_x + float(args.inject_target_offset_m) + compensation_offset_x
            )
            sent_target_y = (
                target_y + float(args.inject_target_offset_m) + compensation_offset_y
            )
            route_duration_seconds = 25.0
            _assert_planned_route_stream_budget(duration_seconds=route_duration_seconds)
            recovery_approval = None
            recovery_allowlist = None
            recovery_dispatch = None

            def _on_deviation() -> dict[str, Any]:
                nonlocal recovery_approval, recovery_allowlist, recovery_dispatch
                if route.on_deviation_action == "abort_only":
                    return {"recovery_action_taken": None}
                (
                    recovery_approval,
                    recovery_allowlist,
                    recovery_dispatch,
                ) = _dispatch_emergency_recovery(route.on_deviation_action)
                return {
                    "recovery_action_taken": route.on_deviation_action,
                    "recovery_dispatch_status": recovery_dispatch.dispatch_status,
                    "recovery_command_ack_observed": (
                        recovery_dispatch.command_ack_observed
                    ),
                    "recovery_command_ack_result_name": (
                        recovery_dispatch.command_ack_result_name
                    ),
                }

            route_send = _send_route_with_monitor(
                target_x=sent_target_x,
                target_y=sent_target_y,
                target_z=target_z,
                feed_forward_vx_mps=feed_forward_vx_mps,
                feed_forward_vy_mps=feed_forward_vy_mps,
                feed_forward_ramp_start_fraction=float(
                    form2a_wind_compensation[
                        "feed_forward_ramp_start_fraction"
                    ]
                ),
                feed_forward_ramp_end_fraction=float(
                    form2a_wind_compensation["feed_forward_ramp_end_fraction"]
                ),
                expected_target_x=target_x,
                expected_target_y=target_y,
                pickup_pose=pickup_pose,
                altitude_max_m=route.altitude_max_m,
                max_pose_deviation_xy_m=route.max_pose_deviation_xy_m,
                max_pose_deviation_z_m=route.max_pose_deviation_z_m,
                duration_seconds=route_duration_seconds,
                timeout=40,
                on_deviation=_on_deviation,
            )
            if route_send.get("pose_deviation_aborted") is True:
                abort = build_px4_gazebo_route_deviation_abort(
                    route_plan=route,
                    route_allowlist=route_allowlist,
                    deviation_samples=route_send["deviation_samples"],
                    route_monitor_sample_count=int(
                        route_send["route_monitor_sample_count"]
                    ),
                    now=NOW,
                )
                recovery_pose = None
                recovery_samples: list[dict[str, float]] = []
                final_status = "aborted_pose_deviation"
                task_status = "blocked"
                recovery_action_taken = None
                recovery_dispatch_ref = None
                recovery_approval_ref = None
                recovery_completed = False
                recovery_ack_complete = False
                recovery_state_observed = False
                recovery_state_label = None
                recovery_completion_basis = None
                recovery_completion = None
                post_recovery_approval = None
                post_recovery_allowlist = None
                post_recovery_dispatch = None
                post_recovery_pose = None
                post_recovery_samples: list[dict[str, float]] = []
                post_recovery_action_taken = None
                post_recovery_dispatch_ref = None
                post_recovery_approval_ref = None
                post_recovery_completion_ref = None
                post_recovery_completed = False
                post_recovery_ack_complete = False
                post_recovery_state_observed = False
                post_recovery_state_label = None
                post_recovery_completion_basis = None
                post_recovery_completion = None
                mission_os_supervisor_recovery_loop = None
                if recovery_dispatch is not None:
                    recovery_action_taken = route.on_deviation_action
                    recovery_dispatch_ref = (
                        "px4_gazebo_emergency_command_dispatch_result:"
                        f"{recovery_dispatch.dispatch_result_id}"
                    )
                    recovery_approval_ref = (
                        None
                        if recovery_approval is None
                        else (
                            "px4_gazebo_emergency_command_approval:"
                            f"{recovery_approval.approval_id}"
                        )
                    )
                    recovery_ack_complete = (
                        recovery_dispatch.dispatch_status == "accepted"
                        and recovery_dispatch.command_ack_observed is True
                    )
                    (
                        recovery_state_observed,
                        recovery_state_label,
                        recovery_pose,
                        recovery_samples,
                    ) = _observe_recovery_state(
                        action=route.on_deviation_action,
                        pickup_pose=pickup_pose,
                        dispatch_frame_sent=recovery_dispatch.frame_sent is True,
                    )
                    recovery_completion = build_px4_gazebo_route_recovery_completion(
                        deviation_abort=abort,
                        emergency_dispatch=recovery_dispatch,
                        recovery_state_observed=recovery_state_observed,
                        recovery_pose_z_m=(
                            None if recovery_pose is None else recovery_pose["z"]
                        ),
                        recovery_state_label=recovery_state_label,
                        now=NOW,
                    )
                    final_status = recovery_completion.final_status
                    recovery_completed = recovery_completion.recovery_completed
                    recovery_completion_basis = (
                        recovery_completion.recovery_completion_basis.value
                    )
                    recovery_ack_complete = recovery_completion.recovery_ack_complete
                    recovery_state_observed = (
                        recovery_completion.recovery_state_observed
                    )
                    task_status = "completed" if recovery_completed else "blocked"
                    if recovery_completed and args.post_recovery_action != "none":
                        post_recovery_action_taken = args.post_recovery_action
                        (
                            post_recovery_approval,
                            post_recovery_allowlist,
                            post_recovery_dispatch,
                        ) = _dispatch_emergency_recovery(args.post_recovery_action)
                        post_recovery_dispatch_ref = (
                            "px4_gazebo_emergency_command_dispatch_result:"
                            f"{post_recovery_dispatch.dispatch_result_id}"
                        )
                        post_recovery_approval_ref = (
                            None
                            if post_recovery_approval is None
                            else (
                                "px4_gazebo_emergency_command_approval:"
                                f"{post_recovery_approval.approval_id}"
                            )
                        )
                        post_recovery_ack_complete = (
                            post_recovery_dispatch.dispatch_status == "accepted"
                            and post_recovery_dispatch.command_ack_observed is True
                        )
                        (
                            post_recovery_state_observed,
                            post_recovery_state_label,
                            post_recovery_pose,
                            post_recovery_samples,
                        ) = _observe_recovery_state(
                            action=args.post_recovery_action,
                            pickup_pose=pickup_pose,
                            dispatch_frame_sent=(
                                post_recovery_dispatch.frame_sent is True
                            ),
                        )
                        post_recovery_completion = (
                            build_px4_gazebo_route_recovery_completion(
                                deviation_abort=abort,
                                emergency_dispatch=post_recovery_dispatch,
                                recovery_state_observed=(post_recovery_state_observed),
                                recovery_pose_z_m=(
                                    None
                                    if post_recovery_pose is None
                                    else post_recovery_pose["z"]
                                ),
                                recovery_state_label=post_recovery_state_label,
                                now=NOW,
                            )
                        )
                        post_recovery_completion_ref = (
                            "px4_gazebo_route_recovery_completion:"
                            f"{post_recovery_completion.recovery_completion_id}"
                        )
                        post_recovery_completed = (
                            post_recovery_completion.recovery_completed
                        )
                        post_recovery_completion_basis = (
                            post_recovery_completion.recovery_completion_basis.value
                        )
                        post_recovery_ack_complete = (
                            post_recovery_completion.recovery_ack_complete
                        )
                        post_recovery_state_observed = (
                            post_recovery_completion.recovery_state_observed
                        )
                        final_status = (
                            f"post_recovery_{post_recovery_completion.final_status}"
                        )
                        task_status = (
                            "completed" if post_recovery_completed else "blocked"
                        )
                    if (
                        args.mission_os_supervisor_recovery_loop
                        or args.mission_os_supervisor_multi_condition_loop
                    ):
                        mission_os_supervisor_recovery_loop = (
                            _build_wind_supervisor_loop(
                                deviation_samples=route_send["deviation_samples"],
                                cycle1_dispatch_ref=recovery_dispatch_ref,
                                cycle1_dispatch_status=(
                                    None
                                    if recovery_dispatch is None
                                    else recovery_dispatch.dispatch_status
                                ),
                                cycle1_approval_ref=recovery_approval_ref,
                                cycle1_outcome_ref=(
                                    None
                                    if recovery_completion is None
                                    else (
                                        "px4_gazebo_route_recovery_completion:"
                                        f"{recovery_completion.recovery_completion_id}"
                                    )
                                ),
                                cycle1_outcome_observed=recovery_state_observed,
                                cycle1_recovery_state_label=recovery_state_label,
                                cycle2_dispatch_ref=post_recovery_dispatch_ref,
                                cycle2_dispatch_status=(
                                    None
                                    if post_recovery_dispatch is None
                                    else post_recovery_dispatch.dispatch_status
                                ),
                                cycle2_approval_ref=post_recovery_approval_ref,
                                cycle2_outcome_ref=post_recovery_completion_ref,
                                cycle2_outcome_observed=(post_recovery_state_observed),
                                cycle2_pose_z_m=(
                                    None
                                    if post_recovery_pose is None
                                    else post_recovery_pose["z"]
                                ),
                                supervisor_scope=(
                                    MULTI_CONDITION_SUPERVISOR_SCOPE
                                    if args.mission_os_supervisor_multi_condition_loop
                                    else WIND_SUPERVISOR_SCOPE
                                ),
                            )
                        )
                updated_abort = store.update(
                    task["task_id"],
                    status=task_status,
                    artifacts={
                        key: value
                        for key, value in {
                            "px4_gazebo_route_deviation_abort": abort.model_dump(
                                mode="json"
                            ),
                            "px4_gazebo_emergency_command_approval": (
                                None
                                if recovery_approval is None
                                else recovery_approval.model_dump(mode="json")
                            ),
                            "px4_gazebo_emergency_command_allowlist": (
                                None
                                if recovery_allowlist is None
                                else recovery_allowlist.model_dump(mode="json")
                            ),
                            "px4_gazebo_emergency_command_dispatch_result": (
                                None
                                if recovery_dispatch is None
                                else recovery_dispatch.model_dump(mode="json")
                            ),
                            "px4_gazebo_route_recovery_completion": (
                                None
                                if recovery_completion is None
                                else recovery_completion.model_dump(mode="json")
                            ),
                            "px4_gazebo_post_recovery_emergency_command_approval": (
                                None
                                if post_recovery_approval is None
                                else post_recovery_approval.model_dump(mode="json")
                            ),
                            "px4_gazebo_post_recovery_emergency_command_allowlist": (
                                None
                                if post_recovery_allowlist is None
                                else post_recovery_allowlist.model_dump(mode="json")
                            ),
                            "px4_gazebo_post_recovery_emergency_command_dispatch_result": (
                                None
                                if post_recovery_dispatch is None
                                else post_recovery_dispatch.model_dump(mode="json")
                            ),
                            "px4_gazebo_post_recovery_completion": (
                                None
                                if post_recovery_completion is None
                                else post_recovery_completion.model_dump(mode="json")
                            ),
                        }.items()
                        if value is not None
                    },
                )
                assert updated_abort is not None
                _refresh_battery_realism_observation_from_trace()
                MOVING_ACTOR_LINEAR_MOTION_SUMMARY = (
                    _moving_actor_waypoint_motion_application_realism()
                )
                MOVING_ACTOR_POSE_SUMMARY = _moving_actor_pose_observation_realism()
                MOVING_ACTOR_PROXIMITY_SUMMARY = (
                    _moving_actor_proximity_evidence_realism(
                        route_start_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                        route_dropoff_xy_m=(target_x, target_y),
                    )
                )
                COLLISION_OBSTACLE_SUMMARY = _collision_obstacle_evidence_realism(
                    route_start_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                    route_dropoff_xy_m=(target_x, target_y),
                )
                ROUTE_BLOCKING_CANDIDATE_SUMMARY = (
                    _route_blocking_candidate_evidence_realism()
                )
                OPERATIONAL_INCIDENT_REPORT_SUMMARY = (
                    _operational_incident_report_realism()
                )
                TRAFFIC_CONFLICT_VERIFICATION_SUMMARY = (
                    _traffic_conflict_verification_realism()
                )
                ROUTE_BLOCKING_VERIFICATION_SUMMARY = (
                    _route_blocking_verification_realism()
                )
                ALTERNATE_LANDING_CANDIDATE_SUMMARY = (
                    _alternate_landing_candidate_evidence_realism()
                )
                TELEMETRY_REALISM_SUMMARY = _telemetry_observer_dropout_realism()
                _refresh_horizontal_contact_topic_summary(run_dir)
                if (
                    recovery_dispatch is not None
                    and (
                        args.mission_os_supervisor_recovery_loop
                        or args.mission_os_supervisor_multi_condition_loop
                    )
                ):
                    mission_os_supervisor_recovery_loop = (
                        _build_wind_supervisor_loop(
                            deviation_samples=route_send["deviation_samples"],
                            cycle1_dispatch_ref=recovery_dispatch_ref,
                            cycle1_dispatch_status=recovery_dispatch.dispatch_status,
                            cycle1_approval_ref=recovery_approval_ref,
                            cycle1_outcome_ref=(
                                None
                                if recovery_completion is None
                                else (
                                    "px4_gazebo_route_recovery_completion:"
                                    f"{recovery_completion.recovery_completion_id}"
                                )
                            ),
                            cycle1_outcome_observed=recovery_state_observed,
                            cycle1_recovery_state_label=recovery_state_label,
                            cycle2_dispatch_ref=post_recovery_dispatch_ref,
                            cycle2_dispatch_status=(
                                None
                                if post_recovery_dispatch is None
                                else post_recovery_dispatch.dispatch_status
                            ),
                            cycle2_approval_ref=post_recovery_approval_ref,
                            cycle2_outcome_ref=post_recovery_completion_ref,
                            cycle2_outcome_observed=(post_recovery_state_observed),
                            cycle2_pose_z_m=(
                                None
                                if post_recovery_pose is None
                                else post_recovery_pose["z"]
                            ),
                            supervisor_scope=(
                                MULTI_CONDITION_SUPERVISOR_SCOPE
                                if args.mission_os_supervisor_multi_condition_loop
                                else WIND_SUPERVISOR_SCOPE
                            ),
                        )
                    )
                summary = {
                    "artifact_dir": str(run_dir),
                    "task_status": updated_abort["status"],
                    "existing_artifacts_retained": updated_abort["artifacts"][
                        "existing"
                    ]["kept"],
                    "final_status": final_status,
                    "actual_px4_gazebo_horizontal_smoke_observed": True,
                    "dropoff_region_reached": False,
                    "dropoff_verified": False,
                    "delivery_completion_claimed": False,
                    "deviation_abort_schema_version": abort.schema_version,
                    "deviation_abort_ref": (
                        f"px4_gazebo_route_deviation_abort:{abort.abort_id}"
                    ),
                    "route_plan_schema_version": route.schema_version,
                    "on_deviation_action": route.on_deviation_action,
                    "pose_deviation_gate_active": True,
                    "pose_deviation_aborted": True,
                    "deviation_samples": route_send["deviation_samples"],
                    "route_monitor_sample_count": route_send[
                        "route_monitor_sample_count"
                    ],
                    "route_stream_terminated_before_recovery_dispatch": route_send[
                        "route_stream_terminated_before_recovery_dispatch"
                    ],
                    "route_stream_process_returncode": route_send[
                        "route_stream_process_returncode"
                    ],
                    "route_stream_stop_reason": route_send["route_stream_stop_reason"],
                    "route_stream_forced_kill": route_send["route_stream_forced_kill"],
                    "recovery_action_taken": recovery_action_taken,
                    "recovery_dispatch_ref": recovery_dispatch_ref,
                    "recovery_approval_ref": recovery_approval_ref,
                    "recovery_completion_ref": (
                        None
                        if recovery_completion is None
                        else (
                            "px4_gazebo_route_recovery_completion:"
                            f"{recovery_completion.recovery_completion_id}"
                        )
                    ),
                    "recovery_completion_schema_version": (
                        None
                        if recovery_completion is None
                        else recovery_completion.schema_version
                    ),
                    "recovery_completed": recovery_completed,
                    "recovery_completion_basis": recovery_completion_basis,
                    "recovery_ack_complete": recovery_ack_complete,
                    "recovery_state_observed": recovery_state_observed,
                    "recovery_state_label": recovery_state_label,
                    "recovery_dispatch_status": (
                        None
                        if recovery_dispatch is None
                        else recovery_dispatch.dispatch_status
                    ),
                    "recovery_command_ack_observed": (
                        None
                        if recovery_dispatch is None
                        else recovery_dispatch.command_ack_observed
                    ),
                    "recovery_command_ack_result_name": (
                        None
                        if recovery_dispatch is None
                        else recovery_dispatch.command_ack_result_name
                    ),
                    "recovery_pose_z_m": (
                        None if recovery_pose is None else recovery_pose["z"]
                    ),
                    "post_recovery_action_taken": post_recovery_action_taken,
                    "post_recovery_dispatch_ref": post_recovery_dispatch_ref,
                    "post_recovery_approval_ref": post_recovery_approval_ref,
                    "post_recovery_completion_ref": post_recovery_completion_ref,
                    "post_recovery_completed": post_recovery_completed,
                    "post_recovery_completion_basis": (post_recovery_completion_basis),
                    "post_recovery_ack_complete": post_recovery_ack_complete,
                    "post_recovery_state_observed": post_recovery_state_observed,
                    "post_recovery_state_label": post_recovery_state_label,
                    "post_recovery_dispatch_status": (
                        None
                        if post_recovery_dispatch is None
                        else post_recovery_dispatch.dispatch_status
                    ),
                    "post_recovery_command_ack_observed": (
                        None
                        if post_recovery_dispatch is None
                        else post_recovery_dispatch.command_ack_observed
                    ),
                    "post_recovery_command_ack_result_name": (
                        None
                        if post_recovery_dispatch is None
                        else post_recovery_dispatch.command_ack_result_name
                    ),
                    "post_recovery_pose_z_m": (
                        None if post_recovery_pose is None else post_recovery_pose["z"]
                    ),
                    "setpoint_frames_sent": 0,
                    "hardware_target_allowed": False,
                    "physical_execution_invoked": False,
                    "px4_mission_upload_allowed": False,
                    "decision_loop_driver": (
                        "mission_os_supervisor"
                        if mission_os_supervisor_recovery_loop is not None
                        else "scripted_horizontal_route_smoke"
                    ),
                    "supervisor_scope": (
                        mission_os_supervisor_recovery_loop.get("supervisor_scope")
                        if mission_os_supervisor_recovery_loop is not None
                        else None
                    ),
                    "full_gateway_runtime_loop": False,
                    "mission_os_supervisor_recovery_loop": (
                        mission_os_supervisor_recovery_loop
                    ),
                    **_wind_realism_summary_artifacts(
                        cleanup_status="teardown_required_after_summary"
                    ),
                    **_vehicle_realism_summary_artifacts(),
                }
                _write_json(run_dir / "summary.json", summary)
                _write_json(
                    run_dir / "mission_artifacts.json",
                    {
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                        "frozen_for_test": False,
                        "artifacts": updated_abort["artifacts"],
                    },
                )
                recovery_rows = [
                    {
                        "phase": f"recovery_{route.on_deviation_action}",
                        "sample_index": index,
                        "sample": sample,
                    }
                    for index, sample in enumerate(recovery_samples)
                ]
                if recovery_pose is not None:
                    recovery_rows.append(
                        {"phase": "recovery_completed", "sample": recovery_pose}
                    )
                recovery_rows.extend(
                    {
                        "phase": f"post_recovery_{args.post_recovery_action}",
                        "sample_index": index,
                        "sample": sample,
                    }
                    for index, sample in enumerate(post_recovery_samples)
                )
                if post_recovery_pose is not None:
                    recovery_rows.append(
                        {
                            "phase": "post_recovery_completed",
                            "sample": post_recovery_pose,
                        }
                    )
                _write_jsonl(run_dir / "pose_samples.jsonl", recovery_rows)
                (run_dir / "px4_docker.log").write_text(_all_logs())
                shutil.copy2(task_db_path, run_dir / "tasks.db")
                print(json.dumps(summary, indent=2, sort_keys=True))
                if route.on_deviation_action == "abort_only":
                    assert summary["final_status"] == "aborted_pose_deviation"
                    assert summary["task_status"] == "blocked"
                if route.on_deviation_action == "land":
                    assert summary["final_status"] in (
                        "recovered_land",
                        "recovered_land_state_observed_ack_timeout",
                    )
                    assert summary["task_status"] == "completed"
                    assert summary["recovery_completed"] is True
                    assert summary["recovery_state_observed"] is True
                    assert summary["route_stream_terminated_before_recovery_dispatch"]
                    assert summary["route_stream_stop_reason"] in (
                        "pose_deviation",
                        "pose_deviation_forced_kill",
                    )
                    assert summary["recovery_dispatch_status"] in (
                        "accepted",
                        "timeout",
                    )
                    if summary["recovery_dispatch_status"] == "timeout":
                        assert summary["recovery_ack_complete"] is False
                        assert (
                            summary["recovery_completion_basis"]
                            == "state_observed_after_dispatch_timeout"
                        )
                    assert float(summary["recovery_pose_z_m"]) <= (
                        _landing_z_threshold(pickup_pose)
                    )
                if route.on_deviation_action == "hold":
                    if summary["recovery_state_label"] == "hold_command_unsupported":
                        assert (
                            summary["final_status"] == "emergency_recovery_unconfirmed"
                        )
                        assert summary["task_status"] == "blocked"
                        assert summary["recovery_completed"] is False
                        assert summary["recovery_state_observed"] is False
                    else:
                        if args.post_recovery_action == "none":
                            assert summary["final_status"] in (
                                "recovered_hold",
                                "recovered_hold_state_observed_ack_timeout",
                            )
                        else:
                            assert summary["final_status"].startswith("post_recovery_")
                        assert summary["task_status"] == "completed"
                        assert summary["recovery_completed"] is True
                        assert summary["recovery_state_observed"] is True
                        assert summary["recovery_state_label"] == "hold_state_observed"
                    if args.post_recovery_action == "land":
                        assert summary["post_recovery_dispatch_status"] in (
                            "accepted",
                            "timeout",
                        )
                        assert summary["post_recovery_completed"] is True
                        assert summary["post_recovery_state_observed"] is True
                        assert float(summary["post_recovery_pose_z_m"]) <= (
                            _landing_z_threshold(pickup_pose)
                        )
                if route.on_deviation_action == "rtl":
                    if args.post_recovery_action == "none":
                        assert summary["final_status"] in (
                            "recovered_rtl",
                            "recovered_rtl_state_observed_ack_timeout",
                        )
                    else:
                        assert summary["final_status"].startswith("post_recovery_")
                    assert summary["task_status"] == "completed"
                    assert summary["recovery_completed"] is True
                    assert summary["recovery_state_observed"] is True
                    assert (
                        summary["recovery_state_label"]
                        == "return_to_launch_state_observed"
                    )
                    if args.post_recovery_action == "land":
                        assert summary["post_recovery_dispatch_status"] in (
                            "accepted",
                            "timeout",
                        )
                        assert summary["post_recovery_completed"] is True
                        assert summary["post_recovery_state_observed"] is True
                        assert float(summary["post_recovery_pose_z_m"]) <= (
                            _landing_z_threshold(pickup_pose)
                        )
                assert summary["existing_artifacts_retained"] is True
                assert summary["deviation_samples"]
                return 0
            assert route_send["offboard_mode_switch_allowed"] is True
            assert route_send["offboard_mode_switch_command_id"] == 176
            assert route_send["offboard_mode_switch_frame_sent"] is True
            assert route_send["offboard_mode_switch_ack_required"] is True
            assert route_send["offboard_mode_switch_ack_command_id"] == 176
            assert route_send["offboard_mode_switch_ack_observed"] is True
            assert route_send["offboard_mode_switch_ack_result_code"] == 0
            route_pose = _pose_sample()
            _append_live_pose_row("route", route_pose)
            alternate_landing_requested = False
            rth_behavior_requested = False
            route_blocking_decision_summaries: dict[str, dict[str, Any]] = {}
            observation_attempts = 8 if _collision_obstacle_requested() else 1
            for observation_attempt in range(1, observation_attempts + 1):
                MOVING_ACTOR_LINEAR_MOTION_SUMMARY = (
                    _moving_actor_waypoint_motion_application_realism()
                )
                MOVING_ACTOR_POSE_SUMMARY = _moving_actor_pose_observation_realism()
                MOVING_ACTOR_PROXIMITY_SUMMARY = (
                    _moving_actor_proximity_evidence_realism(
                        route_start_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                        route_dropoff_xy_m=(target_x, target_y),
                    )
                )
                COLLISION_OBSTACLE_SUMMARY = _collision_obstacle_evidence_realism(
                    route_start_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                    route_dropoff_xy_m=(target_x, target_y),
                )
                ROUTE_BLOCKING_CANDIDATE_SUMMARY = (
                    _route_blocking_candidate_evidence_realism()
                )
                OPERATIONAL_INCIDENT_REPORT_SUMMARY = (
                    _operational_incident_report_realism()
                )
                TRAFFIC_CONFLICT_VERIFICATION_SUMMARY = (
                    _traffic_conflict_verification_realism()
                )
                ROUTE_BLOCKING_VERIFICATION_SUMMARY = (
                    _route_blocking_verification_realism()
                )
                ALTERNATE_LANDING_CANDIDATE_SUMMARY = (
                    _alternate_landing_candidate_evidence_realism()
                )
                alternate_landing_requested = bool(
                    (
                        ALTERNATE_LANDING_CANDIDATE_SUMMARY.get(
                            "alternate_landing_candidate_evidence", {}
                        ).get("observed")
                        or {}
                    ).get("alternate_landing_candidate")
                )
                rth_behavior_requested = bool(
                    _rth_behavior_requested()
                    and (
                        (
                            ROUTE_BLOCKING_VERIFICATION_SUMMARY.get(
                                "route_blocking_verification", {}
                            ).get("observed")
                            or {}
                        ).get("route_blocking_verified")
                    )
                )
                if alternate_landing_requested or rth_behavior_requested:
                    route_blocking_decision_summaries = {
                        "moving_actor_pose": MOVING_ACTOR_POSE_SUMMARY or {},
                        "moving_actor_proximity": (
                            MOVING_ACTOR_PROXIMITY_SUMMARY or {}
                        ),
                        "collision_obstacle": COLLISION_OBSTACLE_SUMMARY or {},
                        "route_blocking_candidate": (
                            ROUTE_BLOCKING_CANDIDATE_SUMMARY or {}
                        ),
                        "operational_incident_report": (
                            OPERATIONAL_INCIDENT_REPORT_SUMMARY or {}
                        ),
                        "traffic_conflict_verification": (
                            TRAFFIC_CONFLICT_VERIFICATION_SUMMARY or {}
                        ),
                        "route_blocking_verification": (
                            ROUTE_BLOCKING_VERIFICATION_SUMMARY or {}
                        ),
                        "alternate_landing_candidate": (
                            ALTERNATE_LANDING_CANDIDATE_SUMMARY or {}
                        ),
                    }
                    break
                if observation_attempt < observation_attempts:
                    _append_live_pose_row(
                        "route_blocking_observation",
                        _pose_sample(),
                        sample_index=observation_attempt - 1,
                    )
                    time.sleep(1)
            alternate_approval = None
            alternate_allowlist = None
            alternate_dispatch = None
            alternate_mission_upload_result = None
            alternate_route_execution_result = None
            rth_approval = None
            rth_allowlist = None
            rth_dispatch = None
            rth_state_observed = False
            rth_state_label = None
            rth_pose = None
            rth_samples: list[dict[str, float]] = []
            landing_phase = "landing"
            if rth_behavior_requested:
                (
                    rth_approval,
                    rth_allowlist,
                    rth_dispatch,
                ) = _dispatch_rth_behavior_execution()
                (
                    rth_state_observed,
                    rth_state_label,
                    rth_pose,
                    rth_samples,
                ) = _observe_recovery_state(
                    action="rtl",
                    pickup_pose=pickup_pose,
                    dispatch_frame_sent=(
                        rth_dispatch is not None and rth_dispatch.frame_sent is True
                    ),
                )
                completed_pose = rth_pose or _pose_sample()
                landing_samples = rth_samples
            elif alternate_landing_requested:
                alternate_mission_upload_result = _upload_alternate_landing_mission()
                alternate_route_execution_result = _execute_alternate_route_rewrite(
                    target_z=target_z,
                    altitude_max_m=route.altitude_max_m,
                    upload_result=alternate_mission_upload_result,
                    approval=approval,
                    route_allowlist=route_allowlist,
                )
                (
                    alternate_approval,
                    alternate_allowlist,
                    alternate_dispatch,
                ) = _dispatch_alternate_landing_execution()
                landing_phase = "alternate_landing"
            else:
                _send_command(
                    "land", approval=approval, coupled_allowlist=coupled_allowlist
                )
            if not rth_behavior_requested:
                landing_z_threshold = _landing_z_threshold(pickup_pose)
                completed_pose, landing_samples = _wait_for_z(
                    lambda z, _samples: z <= landing_z_threshold,
                    timeout=80.0,
                    phase=landing_phase,
                )
            _append_live_pose_row(
                "rth_completed" if rth_behavior_requested else "completed",
                completed_pose,
            )
            PAYLOAD_RELEASE_SUMMARY = (
                None if rth_behavior_requested else _trigger_payload_release()
            )
            VEHICLE_REALISM_SUMMARY = _vehicle_payload_mass_realism(
                payload_model_root=payload_model_root,
                payload_release_summary=PAYLOAD_RELEASE_SUMMARY,
            )
            _refresh_battery_realism_observation_from_trace()
            if route_blocking_decision_summaries:
                MOVING_ACTOR_POSE_SUMMARY = route_blocking_decision_summaries.get(
                    "moving_actor_pose", {}
                )
                MOVING_ACTOR_PROXIMITY_SUMMARY = route_blocking_decision_summaries.get(
                    "moving_actor_proximity", {}
                )
                COLLISION_OBSTACLE_SUMMARY = route_blocking_decision_summaries.get(
                    "collision_obstacle", {}
                )
                ROUTE_BLOCKING_CANDIDATE_SUMMARY = (
                    route_blocking_decision_summaries.get(
                        "route_blocking_candidate", {}
                    )
                )
                OPERATIONAL_INCIDENT_REPORT_SUMMARY = (
                    route_blocking_decision_summaries.get(
                        "operational_incident_report", {}
                    )
                )
                TRAFFIC_CONFLICT_VERIFICATION_SUMMARY = (
                    route_blocking_decision_summaries.get(
                        "traffic_conflict_verification", {}
                    )
                )
                ROUTE_BLOCKING_VERIFICATION_SUMMARY = (
                    route_blocking_decision_summaries.get(
                        "route_blocking_verification", {}
                    )
                )
                ALTERNATE_LANDING_CANDIDATE_SUMMARY = (
                    route_blocking_decision_summaries.get(
                        "alternate_landing_candidate", {}
                    )
                )
            else:
                MOVING_ACTOR_LINEAR_MOTION_SUMMARY = (
                    _moving_actor_waypoint_motion_application_realism()
                )
                MOVING_ACTOR_POSE_SUMMARY = _moving_actor_pose_observation_realism()
                MOVING_ACTOR_PROXIMITY_SUMMARY = (
                    _moving_actor_proximity_evidence_realism(
                        route_start_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                        route_dropoff_xy_m=(target_x, target_y),
                    )
                )
                COLLISION_OBSTACLE_SUMMARY = _collision_obstacle_evidence_realism(
                    route_start_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                    route_dropoff_xy_m=(target_x, target_y),
                )
                ROUTE_BLOCKING_CANDIDATE_SUMMARY = (
                    _route_blocking_candidate_evidence_realism()
                )
                OPERATIONAL_INCIDENT_REPORT_SUMMARY = (
                    _operational_incident_report_realism()
                )
                TRAFFIC_CONFLICT_VERIFICATION_SUMMARY = (
                    _traffic_conflict_verification_realism()
                )
                ROUTE_BLOCKING_VERIFICATION_SUMMARY = (
                    _route_blocking_verification_realism()
                )
                ALTERNATE_LANDING_CANDIDATE_SUMMARY = (
                    _alternate_landing_candidate_evidence_realism()
                )
            ALTERNATE_LANDING_EXECUTION_SUMMARY = _alternate_landing_execution_realism(
                emergency_approval=alternate_approval,
                emergency_allowlist=alternate_allowlist,
                emergency_dispatch=alternate_dispatch,
                completed_pose=completed_pose,
                landing_samples=landing_samples,
            )
            ALTERNATE_MISSION_UPLOAD_SUMMARY = _alternate_mission_upload_realism(
                upload_result=alternate_mission_upload_result,
                alternate_behavior_observation=(
                    ALTERNATE_LANDING_EXECUTION_SUMMARY.get(
                        "alternate_landing_behavior_observation", {}
                    )
                ),
                alternate_route_execution=alternate_route_execution_result,
            )
            TELEMETRY_REALISM_SUMMARY = _telemetry_observer_dropout_realism()
            obstacle_supervisor_recovery_loop = (
                _build_obstacle_supervisor_loop()
                if args.mission_os_supervisor_obstacle_loop
                else None
            )
            RTH_BEHAVIOR_SUMMARY = _rth_behavior_execution_realism(
                emergency_approval=rth_approval,
                emergency_allowlist=rth_allowlist,
                emergency_dispatch=rth_dispatch,
                rth_state_observed=rth_state_observed,
                rth_state_label=rth_state_label,
                rth_pose=rth_pose,
                rth_samples=rth_samples,
            )
            _refresh_horizontal_contact_topic_summary(run_dir)
            dispatch = (
                build_px4_gazebo_route_command_dispatch_result_from_observed_stream(
                    route_plan=route,
                    route_allowlist=route_allowlist,
                    approval=approval,
                    endpoint_port=ROUTE_MAVLINK_PX4_PORT,
                    target_x_m=route_delta_x,
                    target_y_m=route_delta_y,
                    target_z_m=target_z,
                    setpoint_frames_sent=int(route_send["setpoint_frames_sent"]),
                    setpoint_stream_duration_seconds=float(
                        route_send["setpoint_stream_duration_seconds"]
                    ),
                    offboard_mode_switch_frame_sent=bool(
                        route_send["offboard_mode_switch_frame_sent"]
                    ),
                    offboard_mode_switch_ack_observed=bool(
                        route_send["offboard_mode_switch_ack_observed"]
                    ),
                    offboard_mode_switch_ack_result_code=route_send[
                        "offboard_mode_switch_ack_result_code"
                    ],
                    offboard_mode_switch_ack_result_name=route_send[
                        "offboard_mode_switch_ack_result_name"
                    ],
                    offboard_mode_switch_ack_timeout_seconds=float(
                        route_send["offboard_mode_switch_ack_timeout_seconds"]
                    ),
                    now=NOW,
                )
            )
            progress = build_px4_gazebo_route_progress_evidence(
                route_plan=route,
                route_dispatch_result=dispatch,
                pickup_pose_xy_m=(pickup_pose["x"], pickup_pose["y"]),
                observed_pose_xy_m=(completed_pose["x"], completed_pose["y"]),
                deviation_samples=route_send["deviation_samples"],
                now=NOW,
            )
            store.update(
                task["task_id"],
                artifacts={
                    "px4_gazebo_route_command_dispatch_result": dispatch.model_dump(
                        mode="json"
                    ),
                    "px4_gazebo_route_progress_evidence": progress.model_dump(
                        mode="json"
                    ),
                },
            )
            gate = build_px4_gazebo_route_delivery_completion_gate(
                route_plan=route,
                route_dispatch_result=dispatch,
                route_progress_evidence=progress,
                horizontal_route_motion_observed=True,
                px4_telemetry_correlated=True,
                gazebo_pose_correlated=True,
                route_progress_age_seconds=0.0,
                max_route_progress_age_seconds=5.0,
                pose_observed=True,
                expected_vehicle_ref="gazebo_vehicle:x500_0",
                observed_vehicle_ref="gazebo_vehicle:x500_0",
                actual_px4_gazebo_horizontal_smoke_observed=True,
                now=NOW,
            )
            updated = run_px4_gazebo_route_delivery_task(
                task["task_id"],
                completion_gate=gate,
                now=NOW,
                task_store_factory=lambda: store,
            )
            shutil.copy2(task_db_path, run_dir / "tasks.db")

        runner = updated["artifacts"]["px4_gazebo_route_delivery_runner_result"]
        recorded_at = datetime.now(timezone.utc).isoformat()
        delivery_completion_claimed = (
            runner["final_status"] == "completed"
            and gate.dropoff_region_reached
            and not gate.blocked_reasons
        )
        terminal_pose_fields = _terminal_pose_summary_fields(
            route_pose=route_pose,
            completed_pose=completed_pose,
            landing_samples=landing_samples,
            route_terminal_progress_m=gate.horizontal_progress_m,
        )
        summary = {
            "artifact_dir": str(run_dir),
            "recorded_at": recorded_at,
            "frozen_for_test": False,
            "task_status": updated["status"],
            "existing_artifacts_retained": updated["artifacts"]["existing"]["kept"],
            "route_plan_schema_version": route.schema_version,
            "preupload_mission_performed": preupload_summary is not None,
            "preupload_mission_ack_observed": (
                preupload_summary["mission_ack_observed"]
                if preupload_summary
                else False
            ),
            "preupload_mission_ack_type": (
                preupload_summary["mission_ack_type"] if preupload_summary else None
            ),
            "preupload_mission_request_sequences": (
                preupload_summary["mission_request_sequences"]
                if preupload_summary
                else []
            ),
            "route_allowlist_schema_version": route_allowlist.schema_version,
            "dispatch_schema_version": dispatch.schema_version,
            "progress_schema_version": progress.schema_version,
            "completion_gate_schema_version": gate.schema_version,
            "runner_schema_version": runner["schema_version"],
            "final_status": runner["final_status"],
            "actual_px4_gazebo_horizontal_smoke_observed": (
                gate.actual_px4_gazebo_horizontal_smoke_observed
            ),
            "pickup_pose_xy_m": [pickup_pose["x"], pickup_pose["y"]],
            "route_pose_xy_m": [route_pose["x"], route_pose["y"]],
            "completed_pose_xy_m": [completed_pose["x"], completed_pose["y"]],
            "completed_pose_z_m": completed_pose["z"],
            "horizontal_progress_m": gate.horizontal_progress_m,
            "dropoff_region_reached": gate.dropoff_region_reached,
            "delivery_completion_claimed": delivery_completion_claimed,
            **terminal_pose_fields,
            "route_geofence_violation": gate.route_geofence_violation,
            "blocked_reasons": list(gate.blocked_reasons),
            "pose_deviation_gate_active": True,
            "pose_deviation_aborted": False,
            "deviation_samples": list(progress.deviation_samples),
            "route_monitor_sample_count": route_send["route_monitor_sample_count"],
            "setpoint_frames_sent": dispatch.setpoint_frames_sent,
            "setpoint_stream_duration_seconds": dispatch.setpoint_stream_duration_seconds,
            "route_primitive": dispatch.route_primitive,
            "route_target_x_m": dispatch.target_x_m,
            "route_target_y_m": dispatch.target_y_m,
            "route_target_z_m": dispatch.target_z_m,
            "sent_route_target_x_m": sent_target_x,
            "sent_route_target_y_m": sent_target_y,
            "uncompensated_route_target_x_m": target_x,
            "uncompensated_route_target_y_m": target_y,
            "form2a_wind_compensation": form2a_wind_compensation,
            "form2a_wind_compensation_applied": form2a_wind_compensation[
                "route_geometry_compensation_applied"
            ],
            "form2a_wind_preemptive_offset_x_m": compensation_offset_x,
            "form2a_wind_preemptive_offset_y_m": compensation_offset_y,
            "form2a_wind_feed_forward_velocity_x_mps": route_send[
                "feed_forward_velocity_x_mps"
            ],
            "form2a_wind_feed_forward_velocity_y_mps": route_send[
                "feed_forward_velocity_y_mps"
            ],
            "form2a_wind_feed_forward_phase_schedule": route_send[
                "feed_forward_phase_schedule"
            ],
            "form2a_wind_feed_forward_ramp_start_fraction": route_send[
                "feed_forward_ramp_start_fraction"
            ],
            "form2a_wind_feed_forward_ramp_end_fraction": route_send[
                "feed_forward_ramp_end_fraction"
            ],
            "form2a_wind_feed_forward_scale_min": route_send[
                "feed_forward_scale_min"
            ],
            "form2a_wind_feed_forward_scale_max": route_send[
                "feed_forward_scale_max"
            ],
            "form2a_wind_feed_forward_scale_sample_count": route_send[
                "feed_forward_scale_sample_count"
            ],
            "bounded_setpoint_stream_allowed": dispatch.bounded_setpoint_stream_allowed,
            "unbounded_setpoint_stream_allowed": dispatch.unbounded_setpoint_stream_allowed,
            "offboard_mode_switch_allowed": dispatch.offboard_mode_switch_allowed,
            "offboard_mode_switch_command_id": dispatch.offboard_mode_switch_command_id,
            "offboard_mode_switch_frame_sent": dispatch.offboard_mode_switch_frame_sent,
            "offboard_mode_switch_ack_required": (
                dispatch.offboard_mode_switch_ack_required
            ),
            "offboard_mode_switch_ack_command_id": (
                dispatch.offboard_mode_switch_ack_command_id
            ),
            "offboard_mode_switch_ack_timeout_seconds": (
                dispatch.offboard_mode_switch_ack_timeout_seconds
            ),
            "offboard_mode_switch_ack_observed": dispatch.offboard_mode_switch_ack_observed,
            "offboard_mode_switch_ack_result_code": (
                dispatch.offboard_mode_switch_ack_result_code
            ),
            "offboard_mode_switch_ack_result_name": (
                dispatch.offboard_mode_switch_ack_result_name
            ),
            "hardware_target_allowed": gate.hardware_target_allowed,
            "physical_execution_invoked": gate.physical_execution_invoked,
            "px4_mission_upload_allowed": gate.px4_mission_upload_allowed,
            "climb_sample_count": len(climb_samples),
            "landing_sample_count": len(landing_samples),
            "payload_release_observed": bool(
                PAYLOAD_RELEASE_SUMMARY
                and PAYLOAD_RELEASE_SUMMARY["payload_release_observed"]
            ),
            "payload_release_event_source": (
                PAYLOAD_RELEASE_SUMMARY["payload_release_event_source"]
                if PAYLOAD_RELEASE_SUMMARY
                else ""
            ),
            "payload_release_observed_at": (
                PAYLOAD_RELEASE_SUMMARY["payload_release_observed_at"]
                if PAYLOAD_RELEASE_SUMMARY
                else ""
            ),
            "payload_release_position_x_m": (
                PAYLOAD_RELEASE_SUMMARY["payload_release_position_x_m"]
                if PAYLOAD_RELEASE_SUMMARY
                else None
            ),
            "payload_release_position_y_m": (
                PAYLOAD_RELEASE_SUMMARY["payload_release_position_y_m"]
                if PAYLOAD_RELEASE_SUMMARY
                else None
            ),
            "payload_release_position_z_m": (
                PAYLOAD_RELEASE_SUMMARY["payload_release_position_z_m"]
                if PAYLOAD_RELEASE_SUMMARY
                else None
            ),
            "payload_release_summary": PAYLOAD_RELEASE_SUMMARY or {},
            "decision_loop_driver": (
                "mission_os_supervisor"
                if obstacle_supervisor_recovery_loop is not None
                else "scripted_horizontal_route_smoke"
            ),
            "primary_trigger": (
                "route_blocking_obstacle_verified"
                if obstacle_supervisor_recovery_loop is not None
                else None
            ),
            "supervisor_scope": (
                "obstacle_form3_sitl_only"
                if obstacle_supervisor_recovery_loop is not None
                else None
            ),
            "full_gateway_runtime_loop": False,
            "mission_os_supervisor_recovery_loop": obstacle_supervisor_recovery_loop,
            **_wind_realism_summary_artifacts(
                cleanup_status="teardown_required_after_summary"
            ),
            **_vehicle_realism_summary_artifacts(),
        }
        _write_json(run_dir / "summary.json", summary)
        _write_json(
            run_dir / "mission_artifacts.json",
            {
                "recorded_at": recorded_at,
                "frozen_for_test": False,
                "artifacts": updated["artifacts"],
            },
        )
        if LIVE_POSE_TRACE_PATH is None or not LIVE_POSE_TRACE_PATH.read_text().strip():
            _write_jsonl(
                run_dir / "pose_samples.jsonl",
                _pose_rows(
                    pickup_pose=pickup_pose,
                    climb_samples=climb_samples,
                    route_pose=route_pose,
                    completed_pose=completed_pose,
                    landing_samples=landing_samples,
                ),
            )
        (run_dir / "px4_docker.log").write_text(_all_logs())
        print(json.dumps(summary, indent=2, sort_keys=True))
        assert Path(summary["artifact_dir"]).exists()
        assert (Path(summary["artifact_dir"]) / "summary.json").exists()
        assert (Path(summary["artifact_dir"]) / "tasks.db").exists()
        assert (Path(summary["artifact_dir"]) / "px4_docker.log").exists()
        assert (Path(summary["artifact_dir"]) / "pose_samples.jsonl").exists()
        assert (Path(summary["artifact_dir"]) / "mission_artifacts.json").exists()
        alternate_behavior = summary.get("alternate_landing_behavior_observation", {})
        alternate_behavior_observed = (
            alternate_behavior.get("alternate_landing_behavior_observed") is True
        )
        rth_behavior = summary.get("rth_behavior_observation", {})
        rth_behavior_observed = (
            rth_behavior.get("return_to_home_behavior_observed") is True
        )
        route_blocking_observed = (
            summary.get("route_blocking_verification", {})
            .get("observed", {})
            .get("route_blocking_verified")
            is True
        )
        incident_route_blocking_observed = (
            summary.get(
                "horizontal_route_incident_informed_route_blocking_verification", {}
            )
            .get("observed", {})
            .get("route_blocking_verified")
            is True
        )
        assert summary["existing_artifacts_retained"] is True
        if (
            alternate_behavior_observed
            or rth_behavior_observed
            or route_blocking_observed
            or incident_route_blocking_observed
        ):
            assert summary["task_status"] == "blocked"
            assert summary["final_status"] == "blocked"
            assert summary["dropoff_region_reached"] is False
            assert "dropoff_region_not_reached" in summary["blocked_reasons"]
        if alternate_behavior_observed:
            assert (
                summary["alternate_landing_execution_request"]["request_status"]
                == "approved_for_sitl_alternate_landing"
            )
            assert (
                summary["alternate_mission_upload_request"]["request_status"]
                == "approved_for_sitl_alternate_mission_upload"
            )
            assert (
                summary["alternate_mission_upload_request"]["contains_waypoint_item"]
                is True
            )
            assert (
                summary["alternate_mission_upload_request"]["contains_land_item"]
                is True
            )
            assert (
                summary["alternate_mission_upload_receipt"]["upload_status"]
                == "uploaded"
            )
            assert (
                summary["alternate_mission_upload_receipt"]["mission_ack_observed"]
                is True
            )
            assert summary["alternate_mission_upload_receipt"]["mission_ack_type"] == 0
            assert (
                summary["alternate_route_behavior_observation"][
                    "alternate_mission_uploaded"
                ]
                is True
            )
            assert (
                summary["alternate_route_behavior_observation"][
                    "alternate_landing_behavior_observed"
                ]
                is True
            )
            assert (
                summary["alternate_route_behavior_observation"]["dropoff_verified"]
                is False
            )
            assert (
                summary["alternate_route_behavior_observation"][
                    "delivery_completion_claimed"
                ]
                is False
            )
            assert (
                summary["alternate_landing_command_dispatch"][
                    "mavlink_dispatch_performed"
                ]
                is True
            )
            assert alternate_behavior["land_commanded"] is True
            assert alternate_behavior["landing_observed"] is True
            assert alternate_behavior["delivery_completion_claimed"] is False
        elif rth_behavior_observed:
            assert (
                summary["rth_execution_request"]["request_status"]
                == "approved_for_sitl_rth"
            )
            assert summary["rth_command_dispatch"]["mavlink_dispatch_performed"] is True
            assert rth_behavior["rth_commanded"] is True
            assert rth_behavior["rth_state_observed"] is True
            assert rth_behavior["delivery_completion_claimed"] is False
        elif incident_route_blocking_observed:
            assert (
                summary[
                    "horizontal_route_incident_informed_route_blocking_verification"
                ]["verification_status"]
                == "route_blocking_verified"
            )
        elif route_blocking_observed:
            assert (
                summary["route_blocking_verification"]["verification_status"]
                == "route_blocking_verified"
            )
            assert (
                summary["gazebo_route_corridor_obstacle_spawn_application"][
                    "application_status"
                ]
                == "applied"
            )
        else:
            assert summary["task_status"] == "completed"
            assert summary["final_status"] == "completed"
            assert summary["dropoff_region_reached"] is True
            assert summary["blocked_reasons"] == []
        if os.getenv(PREUPLOAD_MISSION_ENV) == "1":
            assert summary["preupload_mission_performed"] is True
            assert summary["preupload_mission_ack_observed"] is True
            assert summary["preupload_mission_ack_type"] == 0
            assert summary["preupload_mission_request_sequences"] == [0, 1, 2, 3]
        if os.getenv(PAYLOAD_RELEASE_MODEL_ENV) == "1" and not rth_behavior_observed:
            assert summary["payload_release_observed"] is True
            assert (
                summary["payload_release_event_source"]
                == "gazebo_detachable_joint_detach_event"
            )
            assert summary["payload_release_position_x_m"] is not None
            assert summary["payload_release_position_y_m"] is not None
            assert summary["payload_release_position_z_m"] is not None
        assert summary["actual_px4_gazebo_horizontal_smoke_observed"] is True
        assert isinstance(summary["delivery_completion_claimed"], bool)
        assert summary["route_terminal_pose"]["phase"] == "route"
        assert summary["route_terminal_pose"]["observed"] is True
        assert summary["landing_terminal_pose"]["phase"] == "landing"
        assert summary["completed_terminal_pose"]["phase"] == "completed"
        assert (
            summary["route_terminal_progress_m"] == summary["horizontal_progress_m"]
        )
        if (
            alternate_behavior_observed
            or rth_behavior_observed
            or route_blocking_observed
            or incident_route_blocking_observed
        ):
            assert summary["horizontal_progress_m"] >= 0.0
        else:
            assert summary["horizontal_progress_m"] >= 5.0
        if alternate_behavior_observed:
            assert summary["route_geofence_violation"] in (False, True)
            if summary["route_geofence_violation"] is True:
                assert "route_geofence_violation" in summary["blocked_reasons"]
                assert summary.get("delivery_completion_claimed") is not True
                assert summary["dropoff_region_reached"] is False
        else:
            assert summary["route_geofence_violation"] is False
        assert summary["pose_deviation_gate_active"] is True
        assert summary["pose_deviation_aborted"] is False
        assert summary["deviation_samples"] == []
        if _collision_obstacle_contact_topic_requested():
            contact_integration = summary["horizontal_route_contact_topic_integration"]
            contact_observed = contact_integration["observed"]
            contact_incident_verification = summary[
                "horizontal_route_contact_incident_verification"
            ]
            contact_incident_verified = contact_incident_verification["observed"]
            assert (
                contact_integration["integration_status"]
                == "sidecar_contact_event_observed"
            )
            assert contact_observed["contact_event_observed"] is True
            assert contact_observed["collision_names"] != []
            assert (
                contact_integration["horizontal_route_world_contact_sensor_injected"]
                is False
            )
            assert (
                contact_integration["horizontal_route_px4_home_boundary_protected"]
                is True
            )
            assert (
                contact_incident_verification["verification_status"]
                == "incident_verified"
            )
            assert contact_incident_verified["incident_verified"] is True
            assert contact_incident_verified["route_blocking_verified"] is False
            assert contact_incident_verified["traffic_conflict_verified"] is False
            assert contact_incident_verified["auto_gate"] is False
            incident_informed_traffic = summary[
                "horizontal_route_incident_informed_traffic_conflict_verification"
            ]
            incident_informed_traffic_observed = incident_informed_traffic["observed"]
            assert (
                incident_informed_traffic["verification_status"]
                == "traffic_conflict_verified"
            )
            assert incident_informed_traffic_observed["incident_verified"] is True
            assert (
                incident_informed_traffic_observed["traffic_conflict_verified"] is True
            )
            assert (
                incident_informed_traffic_observed["route_blocking_verified"] is False
            )
            assert incident_informed_traffic_observed["auto_gate"] is False
            assert contact_observed["task_status_mutated"] is False
            assert contact_observed["delivery_completion_claimed"] is False
            assert contact_incident_verified["task_status_mutated"] is False
            assert contact_incident_verified["delivery_completion_claimed"] is False
            assert incident_informed_traffic_observed["task_status_mutated"] is False
            assert (
                incident_informed_traffic_observed["delivery_completion_claimed"]
                is False
            )
            incident_informed_route_blocking = summary[
                "horizontal_route_incident_informed_route_blocking_verification"
            ]
            incident_route_observed = incident_informed_route_blocking["observed"]
            assert (
                incident_informed_route_blocking["verification_status"]
                == "route_blocking_verified"
            )
            assert incident_route_observed["traffic_conflict_verified"] is True
            assert incident_route_observed["route_blocking_candidate"] is True
            assert incident_route_observed["route_blocking_verified"] is True
            assert incident_route_observed["auto_gate"] is False
            assert incident_route_observed["task_status_mutated"] is False
            assert incident_route_observed["gate_status_mutated"] is False
            assert incident_route_observed["dropoff_verified"] is False
            assert incident_route_observed["delivery_completion_claimed"] is False
        assert summary["route_primitive"] == "bounded_position_setpoint_stream"
        assert summary["bounded_setpoint_stream_allowed"] is True
        assert summary["unbounded_setpoint_stream_allowed"] is False
        assert summary["offboard_mode_switch_allowed"] is True
        assert summary["offboard_mode_switch_command_id"] == 176
        assert summary["offboard_mode_switch_frame_sent"] is True
        assert summary["offboard_mode_switch_ack_required"] is True
        assert summary["offboard_mode_switch_ack_command_id"] == 176
        assert summary["offboard_mode_switch_ack_observed"] is True
        assert summary["offboard_mode_switch_ack_result_code"] == 0
        assert summary["offboard_mode_switch_ack_result_name"] == "ACCEPTED"
        assert summary["route_target_x_m"] == route_delta_x
        assert summary["route_target_y_m"] == route_delta_y
        assert summary["route_target_z_m"] == target_z
        assert summary["hardware_target_allowed"] is False
        assert summary["physical_execution_invoked"] is False
        assert summary["px4_mission_upload_allowed"] is False
        if not rth_behavior_observed:
            assert float(summary["completed_pose_z_m"]) <= (
                _landing_z_threshold(pickup_pose)
            )
        return 0
    finally:
        _stop_container()
        _mark_cleanup_observed(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())

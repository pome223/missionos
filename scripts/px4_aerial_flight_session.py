#!/usr/bin/env python3
"""Opt-in isolated SITL flight session; signed candidate inbox and observed landing.

The user-authorized simulation policy is installed before takeoff. HMAC binds a
host-validated command to this session; it does not turn model output into human
approval. This standalone file runs inside the stock image without pip installs.
Container lifecycle belongs to the caller. No hardware endpoint is configurable.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

OPT_IN = "RUN_PX4_AERIAL_FLIGHT_SESSION"
CONTAINER = "missionos-aerial-flight-e2e"
LABEL = "aerial-wam-jev-sitl"
COMMAND_SCHEMA = "missionos_px4_aerial_candidate_command.v1"
CRC_EXTRA = {0: 50, 30: 39, 32: 185, 33: 104, 76: 152, 77: 143, 84: 143, 245: 130, 253: 83}
MIN_LENGTH = {0: 9, 30: 28, 32: 28, 33: 28, 77: 3, 245: 2, 253: 51}
SIDES = {"left_5m": -5.0, "right_5m": 5.0}
STREAM_RATES = {
    "HEARTBEAT": 10,
    "ATTITUDE": 20,
    "LOCAL_POSITION_NED": 20,
    "GLOBAL_POSITION_INT": 10,
    "EXTENDED_SYS_STATE": 10,
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical(value) + b"\n")
    os.replace(temporary, path)


def sign_command(command, key):
    return {
        "command": command,
        "hmac_sha256": hmac.new(key, canonical(command), hashlib.sha256).hexdigest(),
    }


def _finite_vector(value, size=3):
    if (
        not isinstance(value, list)
        or len(value) != size
        or any(type(x) not in (int, float) or not math.isfinite(x) for x in value)
    ):
        raise ValueError("expected finite numeric vector")
    return [float(x) for x in value]


def validate_command(envelope, config, key, *, now, hover=None, current=None):
    """Verify integrity, policy binding, expiration and fresh hover before motion."""
    if set(envelope) != {"command", "hmac_sha256"}:
        raise ValueError("invalid command envelope")
    command = envelope["command"]
    signature = hmac.new(key, canonical(command), hashlib.sha256).hexdigest()
    if not isinstance(envelope["hmac_sha256"], str) or not hmac.compare_digest(
        signature, envelope["hmac_sha256"]
    ):
        raise ValueError("command signature mismatch")
    if (
        command.get("schema_version") != COMMAND_SCHEMA
        or command.get("session_id") != config["session_id"]
    ):
        raise ValueError("command session mismatch")
    if command.get("approved_instruction_ref") != config["approved_instruction_ref"]:
        raise ValueError("retained approval reference mismatch")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", command.get("command_id", "")):
        raise ValueError("invalid command ID")
    issued, expiry = command.get("issued_at_unix_s"), command.get("expires_at_unix_s")
    if (
        type(issued) not in (int, float)
        or type(expiry) not in (int, float)
        or not all(math.isfinite(x) for x in (issued, expiry))
        or not issued <= now < expiry
        or not 0 < expiry - issued <= 5
    ):
        raise ValueError("expired or invalid command validity window")
    if command.get("action") == "land":
        return command
    if command.get("action") != "fly_candidate" or command.get("candidate_id") not in SIDES:
        raise ValueError("unsupported candidate action")
    for field in (
        "candidate_sha256",
        "scene_sha256",
        "model_receipt_sha256",
        "jev_receipt_sha256",
        "approval_receipt_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", command.get(field, "")):
            raise ValueError("missing evidence hash: " + field)
    if command["scene_sha256"] != config["scene_sha256"]:
        raise ValueError("scene binding mismatch")
    target = _finite_vector(command.get("target_local_ned_m"))
    if max(abs(target[0]), abs(target[1])) > 6 or not -3.2 <= target[2] <= -2.8:
        raise ValueError("target exceeds simulation policy bounds")
    if hover is not None:
        _finite_vector(hover["local_ned_pose_m"])
        _finite_vector([hover["yaw_ned_rad"]], 1)
        yaw = hover["yaw_ned_rad"]
        lateral = SIDES[command["candidate_id"]]
        expected = [
            hover["local_ned_pose_m"][0] - math.sin(yaw) * lateral,
            hover["local_ned_pose_m"][1] + math.cos(yaw) * lateral,
            hover["local_ned_pose_m"][2],
        ]
        if math.dist(target, expected) > 0.25:
            raise ValueError("candidate target disagrees with bounded lateral plan")
    if current is not None:
        if hover is None:
            raise ValueError("hover reference required")
        _finite_vector(current["local_ned_pose_m"])
        _finite_vector(current["local_ned_velocity_mps"])
        _finite_vector([current["yaw_ned_rad"], current["observed_at_unix_s"]], 2)
    if current is not None and (
        current.get("phase") != "holding"
        or current.get("hardware_target") is not False
        or not 0 <= now - current["observed_at_unix_s"] < 2
        or math.dist(current["local_ned_pose_m"], hover["local_ned_pose_m"]) > 0.2
        or math.sqrt(sum(x * x for x in current["local_ned_velocity_mps"])) > 0.2
        or abs(wrap_angle(current["yaw_ned_rad"] - hover["yaw_ned_rad"])) > 0.05
        or current.get("scene_static_verified") is not True
    ):
        raise ValueError("fresh stationary hover and unchanged scene required")
    return command


def wrap_angle(value):
    return (value + math.pi) % (2 * math.pi) - math.pi


def crc(data, extra):
    value = 0xFFFF
    for byte in bytes(data) + bytes([extra]):
        tmp = byte ^ (value & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        value = ((value >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return value


def frame(message_id, payload, sequence, *, system=255, component=190):
    header = bytes(
        [
            len(payload),
            0,
            0,
            sequence & 255,
            system,
            component,
            message_id & 255,
            (message_id >> 8) & 255,
            (message_id >> 16) & 255,
        ]
    )
    return (
        b"\xfd" + header + payload + struct.pack("<H", crc(header + payload, CRC_EXTRA[message_id]))
    )


def decode_datagram(data):
    """Decode concatenated MAVLink1/2 packets; reject bad CRC/source/signatures."""
    offset = 0
    while offset < len(data):
        magic = data[offset]
        header_size = 10 if magic == 253 else 6 if magic == 254 else 0
        if not header_size or len(data) - offset < header_size + 2:
            return
        length = data[offset + 1]
        total = header_size + length + 2
        if magic == 253 and data[offset + 2] != 0:  # No unauthenticated signed/incompat packets.
            return
        if len(data) - offset < total:
            return
        packet = data[offset : offset + total]
        if magic == 253:
            source = packet[5:7]
            message_id = int.from_bytes(packet[7:10], "little")
        else:
            source = packet[3:5]
            message_id = packet[5]
        payload = packet[header_size:-2]
        if (
            source == b"\x01\x01"
            and message_id in MIN_LENGTH
            and struct.unpack("<H", packet[-2:])[0] == crc(packet[1:-2], CRC_EXTRA[message_id])
            and (magic == 253 or len(payload) >= MIN_LENGTH[message_id])
        ):
            # MAVLink2 legitimately truncates trailing zero bytes.
            yield message_id, payload.ljust(MIN_LENGTH[message_id], b"\x00")
        offset += total


def assert_container(inspect, session_dir, *, name=CONTAINER, label=LABEL):
    """Only the caller-owned, isolated x500_depth SITL container is eligible."""
    config, host = inspect["Config"], inspect["HostConfig"]
    env = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
    if (
        inspect.get("Name") != "/" + name
        or not inspect["State"]["Running"]
        or config.get("Labels", {}).get("missionos.scope") != label
        or env.get("PX4_SIM_MODEL") != "gz_x500_depth"
        or host.get("NetworkMode") != "none"
        or host.get("Privileged")
        or host.get("Devices")
        or host.get("PortBindings")
        or host.get("PidMode") == "host"
    ):
        raise ValueError("container is not the authorized isolated x500_depth SITL")
    mounts = [m for m in inspect["Mounts"] if m["Destination"] == "/session"]
    if (
        len(mounts) != 1
        or Path(mounts[0]["Source"]).resolve() != Path(session_dir).resolve()
        or not mounts[0]["RW"]
    ):
        raise ValueError("session mount mismatch")


class FlightSession:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.config = json.loads((self.directory / "config.json").read_text())
        self.key = (self.directory / ".dispatch-key").read_bytes()
        self.phase = "starting"
        self.sequence = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 14650))  # No SO_REUSEADDR: one controller owns the channel.
        self.sock.setblocking(False)
        self.telemetry, self.received, self.acks = {}, {}, []
        self.gz, self.gz_received, self.gz_lock = None, 0.0, threading.Lock()
        self.target, self.target_yaw, self.origin, self.hover = None, None, None, None
        self.offboard = False
        self.stop_requested = False
        self.last_heartbeat = self.last_status = 0.0
        self.started = time.monotonic()
        self.used_commands = set()
        self.outcome = {
            "schema_version": "missionos_px4_aerial_flight_result.v1",
            "session_id": self.config["session_id"],
            "hardware_target": False,
            "takeoff_observed": False,
            "selected_candidate_dispatched": False,
            "selected_candidate_reached": False,
            "landing_observed": False,
            "disarm_observed": False,
            "physical_hardware_execution": False,
        }
        self.events = (self.directory / "events.jsonl").open("x", buffering=1)
        self.trace = (self.directory / "telemetry.jsonl").open("x", buffering=1)
        from gz.msgs10.pose_v_pb2 import Pose_V
        from gz.transport13 import Node

        self.node = Node()
        if not self.node.subscribe(Pose_V, "/world/default/pose/info", self.on_pose):
            raise RuntimeError("Gazebo pose subscription failed")
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: setattr(self, "stop_requested", True))

    def on_pose(self, message):
        poses = {item.name: item for item in message.pose}
        model = poses.get("x500_depth_0")
        if model is None:
            return
        p, q = model.position, model.orientation
        yaw_enu = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        static_ok = True
        for expected in self.config["scene_entities"]:
            actual = poses.get(expected["name"])
            if actual is None:
                static_ok = False
                continue
            for name, fields in (("position", "xyz"), ("orientation", "xyzw")):
                for field_name in fields:
                    if (
                        abs(
                            getattr(getattr(actual, name), field_name)
                            - expected[name].get(field_name, 0.0)
                        )
                        > 1e-5
                    ):
                        static_ok = False
        value = {
            "enu": [p.x, p.y, p.z],
            "ned": [p.y, p.x, -p.z],
            "yaw_ned": wrap_angle(math.pi / 2 - yaw_enu),
            "scene_static_verified": static_ok,
            "simulation_time_ns": message.header.stamp.sec * 1_000_000_000
            + message.header.stamp.nsec,
        }
        with self.gz_lock:
            self.gz, self.gz_received = value, time.monotonic()

    def event(self, kind, **fields):
        self.events.write(
            canonical(
                {
                    "event": kind,
                    "at": datetime.now(UTC).isoformat(),
                    "elapsed_seconds": time.monotonic() - self.started,
                    **fields,
                }
            ).decode()
            + "\n"
        )

    def send(self, message_id, payload):
        self.sock.sendto(frame(message_id, payload, self.sequence), ("127.0.0.1", 14600))
        self.sequence += 1

    def command_frame(self, command_id, params):
        self.send(76, struct.pack("<fffffffHBBB", *params, command_id, 1, 1, 0))

    def pump(self, duration=0.05):
        end = time.monotonic() + duration
        while True:
            now = time.monotonic()
            if now - self.last_heartbeat >= 1:
                self.send(0, struct.pack("<IBBBBB", 0, 6, 8, 0, 4, 3))
                self.last_heartbeat = now
            if self.target is not None and self.phase != "landing":
                # Position and yaw only, at 20 Hz. Actual observation drives completion.
                self.send(
                    84,
                    struct.pack(
                        "<IfffffffffffHBBB",
                        0,
                        *self.target,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        self.target_yaw,
                        0.0,
                        0x9F8,
                        1,
                        1,
                        1,
                    ),
                )
            while True:
                try:
                    data, address = self.sock.recvfrom(65535)
                except BlockingIOError:
                    break
                if address != ("127.0.0.1", 14600):
                    continue
                for message_id, payload in decode_datagram(data):
                    self.received[message_id] = now
                    if message_id == 0:
                        custom, kind, autopilot, mode, state, _ = struct.unpack(
                            "<IBBBBB", payload[:9]
                        )
                        if autopilot != 12 or kind != 2:
                            raise RuntimeError("unexpected autopilot identity")
                        self.telemetry.update(
                            armed=bool(mode & 128), custom_mode=custom, system_state=state
                        )
                    elif message_id == 32:
                        boot, *values = struct.unpack("<Iffffff", payload[:28])
                        self.telemetry.update(
                            px4_pose=values[:3], velocity=values[3:], time_boot_ms=boot
                        )
                    elif message_id == 30:
                        values = struct.unpack("<Iffffff", payload[:28])
                        self.telemetry.update(yaw=values[3], yaw_rate=values[6])
                    elif message_id == 33:
                        self.telemetry["altitude_amsl_m"] = (
                            struct.unpack_from("<i", payload, 12)[0] / 1000
                        )
                    elif message_id == 245:
                        self.telemetry["landed_state"] = payload[1]
                    elif message_id == 77:
                        command_id, result = struct.unpack("<HB", payload[:3])
                        self.acks.append((now, command_id, result))
                    elif message_id == 253:
                        text = payload[1:51].split(b"\x00", 1)[0].decode(errors="replace")
                        self.event("px4_status_text", severity=payload[0], text=text)
            if now - self.last_status >= 0.2:
                status = self.status()
                atomic_json(self.directory / "status.json", status)
                self.trace.write(canonical(status).decode() + "\n")
                self.last_status = now
            if self.phase not in ("holding", "landing"):
                inbox = self.directory / "command.json"
                if inbox.exists():
                    envelope = json.loads(inbox.read_bytes())
                    if envelope.get("command", {}).get("action") == "land":
                        command = validate_command(envelope, self.config, self.key, now=time.time())
                        inbox.rename(
                            self.directory / ("accepted-" + command["command_id"] + ".json")
                        )
                        self.stop_requested = True
                        self.event("authorized_land_requested", command_id=command["command_id"])
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def status(self):
        with self.gz_lock:
            gz, gz_received = self.gz, self.gz_received
        now = time.monotonic()
        ages = [now - self.received.get(key, 0.0) for key in (0, 30, 32)] + [now - gz_received]
        value = {
            "schema_version": "missionos_px4_aerial_flight_status.v1",
            "phase": self.phase,
            "session_id": self.config["session_id"],
            "hardware_target": False,
            "scene_sha256": self.config["scene_sha256"],
            "observed_at_unix_s": time.time() - max(ages),
            "telemetry_age_seconds": max(ages),
            "armed": self.telemetry.get("armed"),
            "landed_state": self.telemetry.get("landed_state"),
            "landed_state_age_seconds": now - self.received.get(245, 0.0),
            "px4_custom_mode": self.telemetry.get("custom_mode"),
            "px4_main_mode": (self.telemetry.get("custom_mode", 0) >> 16) & 255,
            "hold_kind": "px4_offboard_hover",
            "control_hold_kind": "px4_offboard_hover",
            "local_ned_pose_m": gz["ned"] if gz else None,
            "current_vehicle_local_ned_m": gz["ned"] if gz else None,
            "px4_local_ned_pose_m": self.telemetry.get("px4_pose"),
            "local_ned_velocity_mps": self.telemetry.get("velocity"),
            "yaw_ned_rad": gz["yaw_ned"] if gz else None,
            "px4_yaw_ned_rad": self.telemetry.get("yaw"),
            "yaw_rate_rad_s": self.telemetry.get("yaw_rate"),
            "gazebo_pose_enu_m": gz["enu"] if gz else None,
            "pose_simulation_time_ns": gz["simulation_time_ns"] if gz else None,
            "scene_static_verified": gz["scene_static_verified"] if gz else False,
            "hover_reference": self.hover,
        }
        return value

    def command(self, command_id, params, name):
        sent = time.monotonic()
        self.command_frame(command_id, params)
        self.event("command_sent", command_id=command_id, command_name=name)
        while time.monotonic() - sent < 8:
            self.pump()
            relevant = [a for a in self.acks if a[0] >= sent and a[1] == command_id and a[2] != 5]
            if relevant:
                result = relevant[-1][2]
                self.event("command_ack", command_id=command_id, command_name=name, result=result)
                if result != 0:
                    raise RuntimeError(name + " rejected with ACK " + str(result))
                return
        raise RuntimeError(name + " accepted ACK not observed")

    def wait(self, predicate, timeout, description, *, enforce_bounds=True):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            self.pump()
            current = self.status()
            if self.stop_requested and self.phase != "landing":
                raise RuntimeError("land requested by signal")
            if current["telemetry_age_seconds"] < 2:
                if enforce_bounds and self.origin is not None:
                    self.check_bounds(current)
                if predicate(current):
                    return current
        raise RuntimeError("timed out: " + description)

    def check_bounds(self, current):
        point = current["local_ned_pose_m"]
        if (
            max(abs(point[0]), abs(point[1])) > 6
            or not -5 <= point[2] <= 0.5
            or not current["scene_static_verified"]
        ):
            raise RuntimeError("observed simulation geofence or scene constraint violation")
        if abs(wrap_angle(current["yaw_ned_rad"] - current["px4_yaw_ned_rad"])) > 0.15:
            raise RuntimeError("Gazebo and PX4 headings disagree")
        if self.phase in ("holding", "flying_candidate") and (
            current["armed"] is not True or current["px4_main_mode"] != 6
        ):
            raise RuntimeError("armed PX4 OFFBOARD mode is no longer observed")

    def stationary_at(
        self, point, timeout, *, position_tolerance=0.18, speed_limit=0.18, stable_sim_seconds=None
    ):
        since = None
        previous_simulation_ns = None

        def stable(current):
            nonlocal since, previous_simulation_ns
            simulation_ns = current["pose_simulation_time_ns"]
            if previous_simulation_ns is not None and simulation_ns < previous_simulation_ns:
                raise RuntimeError("simulation clock regressed while observing hover")
            previous_simulation_ns = simulation_ns
            ok = (
                math.dist(current["local_ned_pose_m"], point) <= position_tolerance
                and math.sqrt(sum(v * v for v in current["local_ned_velocity_mps"])) <= speed_limit
            )
            clock = simulation_ns / 1e9 if stable_sim_seconds is not None else time.monotonic()
            required = stable_sim_seconds if stable_sim_seconds is not None else 1.5
            since = clock if ok and since is None else since if ok else None
            return since is not None and clock - since >= required

        return self.wait(stable, timeout, "stationary target pose")

    def fly(self):
        self.phase = "preflight"
        for message_id in (0, 30, 32, 33, 245):
            interval = 50000.0 if message_id in (30, 32) else 100000.0
            self.command_frame(511, [float(message_id), interval, 0.0, 0.0, 0.0, 0.0, 0.0])
            self.pump(0.1)
        initial = self.wait(
            lambda s: (
                s["armed"] is False
                and self.telemetry.get("landed_state") == 1
                and "altitude_amsl_m" in self.telemetry
                and time.monotonic() - self.received.get(33, 0.0) < 2
                and s["scene_static_verified"]
            ),
            60,
            "unarmed PX4, estimator and Gazebo readiness",
        )
        self.origin = initial["local_ned_pose_m"]
        if math.hypot(*self.origin[:2]) > 0.3 or abs(self.origin[2]) > 0.2:
            raise RuntimeError("initial pose must be on the ground near world origin")
        self.phase = "arming"
        for attempt in range(4):
            self.pump(3.0)
            try:
                self.command(400, [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], "arm")
                break
            except RuntimeError as exc:
                if "arm rejected with ACK 1" not in str(exc) or attempt == 3:
                    raise
                self.event("preflight_arm_retry", attempt=attempt + 1, reason=str(exc))
        armed = self.wait(lambda s: s["armed"] is True, 10, "armed heartbeat")
        # The unarmed preflight heartbeat can still report a placeholder yaw.
        # Bind OFFBOARD's heading target only after the armed estimator agrees
        # with the observed Gazebo heading under the normal safety bound.
        self.target_yaw = armed["px4_yaw_ned_rad"]
        self.event(
            "armed_heading_target_observed",
            target_yaw_ned_rad=self.target_yaw,
            gazebo_yaw_ned_rad=armed["yaw_ned_rad"],
            pose_simulation_time_ns=armed["pose_simulation_time_ns"],
        )
        self.phase = "taking_off"
        self.command(
            22,
            [0.0, 0.0, 0.0, math.nan, math.nan, math.nan, self.telemetry["altitude_amsl_m"] + 3.0],
            "takeoff",
        )
        self.wait(lambda s: s["local_ned_pose_m"][2] < -1.0, 120, "observed initial climb")
        current = self.status()
        target = [self.origin[0], self.origin[1], -3.0]
        offset = [
            a - b for a, b in zip(current["px4_local_ned_pose_m"], current["local_ned_pose_m"])
        ]
        self.target = [a + b for a, b in zip(target, offset)]
        self.pump(2.0)
        self.command(176, [1.0, 6.0, 0.0, 0.0, 0.0, 0.0, 0.0], "offboard")
        self.wait(lambda s: s["px4_main_mode"] == 6, 10, "OFFBOARD heartbeat mode")
        self.offboard = True
        settled = self.stationary_at(target, 180)
        corrected_offset = [
            a - b for a, b in zip(settled["px4_local_ned_pose_m"], settled["local_ned_pose_m"])
        ]
        self.event(
            "observed_coordinate_alignment",
            previous_offset_ned_m=offset,
            corrected_offset_ned_m=corrected_offset,
            measured_world_position_ned_m=settled["local_ned_pose_m"],
            measured_px4_position_ned_m=settled["px4_local_ned_pose_m"],
            gazebo_simulation_time_ns=settled["pose_simulation_time_ns"],
            model_pose_was_teleported=False,
        )
        self.target = [a + b for a, b in zip(target, corrected_offset)]
        held = self.stationary_at(
            target, 180, position_tolerance=0.05, speed_limit=0.08, stable_sim_seconds=1.0
        )
        self.outcome["takeoff_observed"] = True
        self.hover = {
            "local_ned_pose_m": held["local_ned_pose_m"],
            "yaw_ned_rad": held["yaw_ned_rad"],
            "pose_simulation_time_ns": held["pose_simulation_time_ns"],
            "observed_at_unix_s": held["observed_at_unix_s"],
        }
        self.phase = "holding"
        self.event("hover_observed", **self.hover)
        self.hold_for_command()

    def hold_for_command(self):
        """Default signed lateral command boundary; research subclasses stay separate."""
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            self.pump()
            current = self.status()
            if self.stop_requested:
                return
            if current["telemetry_age_seconds"] >= 2:
                raise RuntimeError("live telemetry became stale during hover")
            self.check_bounds(current)
            inbox = self.directory / "command.json"
            if not inbox.exists():
                continue
            try:
                envelope = json.loads(inbox.read_bytes())
                command = validate_command(
                    envelope,
                    self.config,
                    self.key,
                    now=time.time(),
                    hover=self.hover,
                    current=current,
                )
                if command["command_id"] in self.used_commands:
                    raise ValueError("replayed command ID")
            except (ValueError, KeyError, TypeError) as exc:
                self.event("candidate_command_rejected", reason=str(exc))
                inbox.rename(self.directory / ("rejected-" + uuid.uuid4().hex + ".json"))
                continue
            self.used_commands.add(command["command_id"])
            inbox.rename(self.directory / ("accepted-" + command["command_id"] + ".json"))
            if command["action"] == "land":
                self.event("authorized_land_requested", command_id=command["command_id"])
                return
            self.execute_candidate(command, current)
            return
        self.event("hover_deadline_reached", max_hold_seconds=600)

    def execute_candidate(self, command, current):
        self.phase = "flying_candidate"
        self.outcome.update(
            selected_candidate_dispatched=True,
            candidate_id=command["candidate_id"],
            command_sha256=hashlib.sha256(canonical(command)).hexdigest(),
            evidence_bindings={k: command[k] for k in command if k.endswith("_sha256")},
            commanded_target_local_ned_m=command["target_local_ned_m"],
        )
        self.event("candidate_dispatch", command=command, observed_start=current)
        start = current["local_ned_pose_m"]
        target = command["target_local_ned_m"]
        offset = [a - b for a, b in zip(current["px4_local_ned_pose_m"], start)]
        started = time.monotonic()
        # 1 m/s reference in observed simulation time, independent of wall slowdown.
        distance = math.dist(target, start)
        start_simulation_ns = current["pose_simulation_time_ns"]
        alpha = 0.0
        while alpha < 1.0:
            if time.monotonic() - started > 120:
                raise RuntimeError("candidate simulation-time ramp exceeded wall deadline")
            status = self.status()
            elapsed_simulation_s = (status["pose_simulation_time_ns"] - start_simulation_ns) / 1e9
            if elapsed_simulation_s < 0:
                raise RuntimeError("simulation clock regressed during flight")
            alpha = min(1.0, elapsed_simulation_s / max(distance, 0.001))
            self.target = [a + alpha * (b - a) + o for a, b, o in zip(start, target, offset)]
            self.pump()
            status = self.status()
            if self.stop_requested or status["telemetry_age_seconds"] >= 2:
                raise RuntimeError("candidate motion interrupted or telemetry stale")
            self.check_bounds(status)
        self.target = [a + b for a, b in zip(target, offset)]
        reached = self.stationary_at(target, 45)
        self.outcome.update(
            selected_candidate_reached=True,
            reached_local_ned_pose_m=reached["local_ned_pose_m"],
            measured_displacement_m=math.dist(start, reached["local_ned_pose_m"]),
        )
        self.event("candidate_target_observed", observed=reached)

    def land(self):
        self.phase = "landing"
        self.target = None
        self.command(21, [0.0, 0.0, 0.0, math.nan, math.nan, math.nan, math.nan], "land")
        landed = self.wait(
            lambda s: (
                self.telemetry.get("landed_state") == 1
                and s["landed_state_age_seconds"] < 2
                and abs(s["local_ned_pose_m"][2]) < 0.25
                and math.sqrt(sum(x * x for x in s["local_ned_velocity_mps"])) < 0.2
            ),
            90,
            "PX4 landed state and Gazebo ground pose",
            enforce_bounds=False,
        )
        self.outcome["landing_observed"] = True
        self.event("landing_observed", observed=landed)
        disarmed = self.wait(
            lambda s: s["armed"] is False, 20, "automatic disarmed heartbeat", enforce_bounds=False
        )
        self.outcome.update(
            disarm_observed=True, final_local_ned_pose_m=disarmed["local_ned_pose_m"]
        )
        self.phase = "landed"
        self.event("disarm_observed", observed=disarmed)

    def run(self):
        error = None
        try:
            self.fly()
        except Exception as exc:  # noqa: BLE001 - every failure must enter the landing cleanup.
            error = str(exc)
            self.event("session_error", reason=error)
        finally:
            if self.telemetry.get("armed"):
                try:
                    self.land()
                except Exception as exc:  # noqa: BLE001 - retain landing failures in the terminal receipt.
                    self.event("landing_error", reason=str(exc))
                    error = (error + "; " if error else "") + str(exc)
            self.outcome["error"] = error
            self.outcome["complete"] = bool(
                self.outcome["takeoff_observed"]
                and self.outcome["selected_candidate_reached"]
                and self.outcome["landing_observed"]
                and self.outcome["disarm_observed"]
                and error is None
            )
            if error:
                self.phase = "failed_landed" if self.outcome["disarm_observed"] else "failed"
            atomic_json(self.directory / "status.json", self.status())
            atomic_json(self.directory / "flight-result.json", self.outcome)
            self.events.close()
            self.trace.close()
            self.sock.close()
        return 0 if self.outcome["complete"] else 1


def require_opt_in():
    if os.getenv(OPT_IN) != "1":
        raise ValueError("Set " + OPT_IN + "=1 for the authorized SITL session")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", type=Path, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="mode")
    initialize = sub.add_parser("initialize")
    initialize.add_argument("--session-dir", required=True, type=Path)
    initialize.add_argument("--scene-sha256", required=True)
    initialize.add_argument("--approved-instruction-ref", required=True)
    initialize.add_argument("--scene-entities-file", type=Path)
    for name in ("start", "status", "dispatch", "land"):
        command = sub.add_parser(name)
        command.add_argument("--session-dir", required=True, type=Path)
        if name == "start":
            command.add_argument("--container", default=CONTAINER, choices=[CONTAINER])
        if name == "dispatch":
            command.add_argument("--command-file", required=True, type=Path)
        if name == "land":
            command.add_argument("--reason", default="caller_requested_land")
    args = parser.parse_args(argv)
    if args.mode == "status":
        print((args.session_dir / "status.json").read_text())
        return 0
    require_opt_in()
    if args.controller:
        if (
            os.getenv("RUNS_IN_DOCKER") != "true"
            or os.getenv("PX4_SIM_MODEL") != "gz_x500_depth"
            or args.controller != Path("/session")
            or not Path("/opt/px4-gazebo/bin/px4").is_file()
        ):
            raise ValueError("controller requires the stock isolated SITL image")
        return FlightSession(args.controller).run()
    directory = args.session_dir
    if args.mode == "initialize":
        protected = (
            "config.json",
            ".dispatch-key",
            "status.json",
            "events.jsonl",
            "telemetry.jsonl",
            "command.json",
            "flight-result.json",
            "controller.py",
        )
        if directory.exists() and any((directory / name).exists() for name in protected):
            raise ValueError(
                "existing flight authorization or runtime artifacts must not be overwritten"
            )
        if (
            not re.fullmatch(r"[0-9a-f]{64}", args.scene_sha256)
            or not args.approved_instruction_ref.strip()
        ):
            raise ValueError("scene hash and retained user instruction reference required")
        entities = (
            json.loads(args.scene_entities_file.read_text()) if args.scene_entities_file else []
        )
        if not isinstance(entities, list):
            raise ValueError("scene entities must be a list")
        for entity in entities:
            if not isinstance(entity.get("name"), str) or not entity["name"]:
                raise ValueError("scene entity name required")
            for field, names in (("position", "xyz"), ("orientation", "xyzw")):
                _finite_vector([entity[field].get(key, 0.0) for key in names], len(names))
        directory.mkdir(parents=True, exist_ok=True)
        keypath = directory / ".dispatch-key"
        with keypath.open("xb") as stream:
            os.chmod(keypath, 0o600)
            stream.write(secrets.token_bytes(32))
        atomic_json(
            directory / "config.json",
            {
                "schema_version": "missionos_px4_aerial_flight_policy.v1",
                "session_id": uuid.uuid4().hex,
                "scene_sha256": args.scene_sha256,
                "approved_instruction_ref": args.approved_instruction_ref,
                "execution_scope": "px4_sitl",
                "hardware_target": False,
                "scene_entities": entities,
                "candidate_ids": list(SIDES),
                "max_hold_seconds": 600,
                "takeoff_height_m": 3.0,
                "max_xy_absolute_m": 6.0,
                "ceiling_m": 5.0,
            },
        )
        print(
            canonical(
                {
                    "initialized": True,
                    "session_id": json.loads((directory / "config.json").read_text())["session_id"],
                }
            ).decode()
        )
    elif args.mode == "start":
        metadata = json.loads(subprocess.check_output(["docker", "inspect", args.container]))[0]
        assert_container(metadata, directory)
        if (directory / "events.jsonl").exists():
            raise ValueError("session already started; refusing second controller")
        (directory / "controller.py").write_bytes(Path(__file__).read_bytes())
        subprocess.run(
            [
                "docker",
                "exec",
                args.container,
                "/opt/px4-gazebo/bin/px4-mavlink",
                "start",
                "-u",
                "14600",
                "-o",
                "14650",
                "-t",
                "127.0.0.1",
                "-m",
                "onboard",
                "-r",
                "400000",
            ],
            check=True,
        )
        for stream, rate in STREAM_RATES.items():
            subprocess.run(
                [
                    "docker",
                    "exec",
                    args.container,
                    "/opt/px4-gazebo/bin/px4-mavlink",
                    "stream",
                    "-u",
                    "14600",
                    "-s",
                    stream,
                    "-r",
                    str(rate),
                ],
                check=True,
            )
        subprocess.run(
            [
                "docker",
                "exec",
                "-d",
                "-e",
                OPT_IN + "=1",
                args.container,
                "sh",
                "-c",
                "python3 /session/controller.py --controller /session > /session/controller.log 2>&1",
            ],
            check=True,
        )
        print('{"controller_started":true,"takeoff_observed":false}')
    elif args.mode in ("dispatch", "land"):
        config = json.loads((directory / "config.json").read_text())
        key = (directory / ".dispatch-key").read_bytes()
        status = json.loads((directory / "status.json").read_text())
        command = (
            json.loads(args.command_file.read_bytes())
            if args.mode == "dispatch"
            else {
                "schema_version": COMMAND_SCHEMA,
                "session_id": config["session_id"],
                "command_id": uuid.uuid4().hex,
                "action": "land",
                "reason": args.reason,
                "approved_instruction_ref": config["approved_instruction_ref"],
                "issued_at_unix_s": time.time(),
                "expires_at_unix_s": time.time() + 4.0,
            }
        )
        envelope = sign_command(command, key)
        validate_command(
            envelope,
            config,
            key,
            now=time.time(),
            hover=status.get("hover_reference"),
            current=status,
        )
        inbox = directory / "command.json"
        if inbox.exists():
            raise ValueError("unconsumed command already exists")
        atomic_json(inbox, envelope)
        print(
            canonical(
                {
                    "command_written": True,
                    "command_id": command["command_id"],
                    "execution_observed": False,
                }
            ).decode()
        )
    else:
        parser.error("a command is required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

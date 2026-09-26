"""Loopback-only SITL position/yaw stream. No configurable network endpoint."""

from __future__ import annotations

import json
import math
import socket
import struct
import threading
import time

CRC_EXTRA = {0: 50, 76: 152, 77: 143, 84: 143}
LOCAL = ("127.0.0.1", 14656)
REMOTE = ("127.0.0.1", 14606)
POSITION_YAW_MASK = 0x9F8  # ignore velocity, acceleration and yaw rate; keep xyz/yaw


def crc(data, extra):
    value = 0xFFFF
    for byte in (*data, extra):
        tmp = byte ^ (value & 255)
        tmp = (tmp ^ (tmp << 4)) & 255
        value = ((value >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return value


def frame(mid, payload, sequence, *, system=255, component=191):
    header = bytes(
        [
            len(payload),
            0,
            0,
            sequence & 255,
            system,
            component,
            mid & 255,
            (mid >> 8) & 255,
            (mid >> 16) & 255,
        ]
    )
    return b"\xfd" + header + payload + struct.pack("<H", crc(header + payload, CRC_EXTRA[mid]))


def decode(data):
    """CRC-checked MAVLink2 datagrams, including multiple packed frames."""
    values = []
    while data:
        if len(data) < 12 or data[0] != 253 or data[2] != 0:
            break
        size = data[1] + 12
        if len(data) < size:
            break
        raw, data = data[:size], data[size:]
        mid = raw[7] | raw[8] << 8 | raw[9] << 16
        if (
            mid not in CRC_EXTRA
            or crc(raw[1:-2], CRC_EXTRA[mid]) != struct.unpack("<H", raw[-2:])[0]
        ):
            continue
        values.append(
            {"id": mid, "system": raw[5], "component": raw[6], "payload": raw[10:-2], "raw": raw}
        )
    return values


def setpoint(position, yaw, sequence):
    if len(position) != 3 or not all(math.isfinite(v) for v in [*position, yaw]):
        raise ValueError("nonfinite_setpoint")
    return frame(
        84,
        struct.pack(
            "<IfffffffffffHBBB", 0, *position, 0, 0, 0, 0, 0, 0, yaw, 0, POSITION_YAW_MASK, 1, 1, 1
        ),
        sequence,
    )


def mode_command(mode, sequence):
    if mode not in ("offboard", "loiter"):
        raise ValueError("unsupported_mode")
    params = [1, 6, 0, 0, 0, 0, 0] if mode == "offboard" else [1, 4, 3, 0, 0, 0, 0]
    return frame(76, struct.pack("<fffffffHBBB", *params, 176, 1, 1, 0), sequence)


class PositionStream:
    def __init__(self, root, position, yaw, clock, deadline, permit_hash):
        self.position, self.yaw = list(position), yaw
        self.clock, self.deadline = clock, deadline
        self.permit_hash = permit_hash
        self.refreshed = time.monotonic()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.error = None
        self.sequence = 0
        self.acks = []
        self.log = (root / "vla-transport.jsonl").open("w", buffering=1)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(LOCAL)
        self.sock.setblocking(False)
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def emit(self, raw, kind):
        count = self.sock.sendto(raw, REMOTE)
        if count != len(raw):
            raise RuntimeError("partial_udp_send")
        self.log.write(
            json.dumps(
                {
                    "elapsed_s": self.clock(),
                    "direction": "sent",
                    "kind": kind,
                    "permit_sha256": self.permit_hash,
                    "bytes_sent": count,
                    "frame_hex": raw.hex(),
                }
            )
            + "\n"
        )
        self.sequence += 1

    def start(self):
        self.thread.start()

    def update(self, position, yaw, permit_hash=None):
        with self.lock:
            self.position, self.yaw = list(position), yaw
            self.refreshed = time.monotonic()
            if permit_hash is not None:
                self.permit_hash = permit_hash
        if self.error:
            raise RuntimeError(self.error)

    def command(self, mode):
        with self.lock:
            self.acks.clear()
            self.emit(mode_command(mode, self.sequence), mode)

    def accepted(self):
        with self.lock:
            return any(a["command"] == 176 and a["result"] == 0 for a in self.acks)

    def loop(self):
        try:
            while not self.stop.is_set():
                with self.lock:
                    if self.clock() > self.deadline or time.monotonic() - self.refreshed > 0.8:
                        raise RuntimeError("setpoint_stream_deadline_or_observation_watchdog")
                    self.emit(setpoint(self.position, self.yaw, self.sequence), "position_yaw")
                    if self.sequence % 20 == 0:
                        self.emit(
                            frame(0, struct.pack("<IBBBBB", 0, 6, 8, 0, 4, 3), self.sequence),
                            "heartbeat",
                        )
                    while True:
                        try:
                            data, addr = self.sock.recvfrom(65535)
                        except BlockingIOError:
                            break
                        if addr != REMOTE:
                            continue
                        for item in decode(data):
                            if item["id"] != 77 or (item["system"], item["component"]) != (1, 1):
                                continue
                            payload = item["payload"].ljust(10, b"\0")
                            command, result, _, _, target_system, target_component = struct.unpack(
                                "<HBBiBB", payload[:10]
                            )
                            if (target_system, target_component) != (255, 191):
                                continue
                            self.acks.append({"command": command, "result": result})
                            self.log.write(
                                json.dumps(
                                    {
                                        "elapsed_s": self.clock(),
                                        "direction": "received",
                                        "frame_hex": item["raw"].hex(),
                                    }
                                )
                                + "\n"
                            )
                self.stop.wait(0.05)
        except Exception as exc:
            self.error = str(exc)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.sock.close()
        self.log.close()

from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import threading
from typing import Any

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route.embedded_mavlink import (
    MAVLINK_HEARTBEAT_OBSERVER_HELPER,
    MAVLINK_LINK_LOSS_APPLICATOR_HELPER,
    MAVLINK_ROUTE_HELPER,
)


@pytest.mark.parametrize(
    "source",
    [
        MAVLINK_ROUTE_HELPER,
        MAVLINK_HEARTBEAT_OBSERVER_HELPER,
        MAVLINK_LINK_LOSS_APPLICATOR_HELPER,
    ],
)
def test_embedded_mavlink_programs_remain_valid_python(source: str) -> None:
    compile(source, "<embedded-mavlink-helper>", "exec")


def test_legacy_route_entrypoint_uses_packaged_helper_sources() -> None:
    assert route_entrypoint.MAVLINK_ROUTE_HELPER is MAVLINK_ROUTE_HELPER
    assert (
        route_entrypoint.MAVLINK_HEARTBEAT_OBSERVER_HELPER
        is MAVLINK_HEARTBEAT_OBSERVER_HELPER
    )
    assert (
        route_entrypoint.MAVLINK_LINK_LOSS_APPLICATOR_HELPER
        is MAVLINK_LINK_LOSS_APPLICATOR_HELPER
    )


def _serve_accepted_command_ack(
    sock: socket.socket,
    result: dict[str, Any],
) -> None:
    try:
        sock.settimeout(5.0)
        while True:
            packet, sender = sock.recvfrom(2048)
            if len(packet) < 10 or packet[0] != 0xFD:
                continue
            message_id = packet[7] | (packet[8] << 8) | (packet[9] << 16)
            if message_id != 76:
                continue
            payload_length = packet[1]
            payload = packet[10 : 10 + payload_length]
            command_id = struct.unpack("<H", payload[28:30])[0]
            ack_payload = struct.pack("<HBBiBB", command_id, 0, 100, 0, 1, 1)
            # The helper accepts MAVLink 1 or 2 frames and does not require the
            # checksum bytes to decode COMMAND_ACK evidence.
            ack = bytes([0xFE, len(ack_payload), 0, 1, 1, 77]) + ack_payload + b"\0\0"
            sock.sendto(ack, sender)
            result["command_id"] = command_id
            return
    except BaseException as exc:  # pragma: no cover - surfaced by the caller
        result["error"] = repr(exc)


def test_route_helper_observes_command_ack_over_loopback_udp() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        server_port = server.getsockname()[1]
        result: dict[str, Any] = {}
        thread = threading.Thread(
            target=_serve_accepted_command_ack,
            args=(server, result),
            daemon=True,
        )
        thread.start()

        source = MAVLINK_ROUTE_HELPER.replace(
            'sock.bind(("127.0.0.1", 14650))',
            'sock.bind(("127.0.0.1", 0))',
        ).replace(
            'remote = ("127.0.0.1", 14600)',
            f'remote = ("127.0.0.1", {server_port})',
        )
        completed = subprocess.run(
            [sys.executable, "-c", source, "arm"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        thread.join(timeout=5.0)

    assert completed.returncode == 0, completed.stderr
    assert "error" not in result
    assert result["command_id"] == 400
    summary = json.loads(completed.stdout)
    assert summary == {
        "mode": "arm",
        "command_id": 400,
        "sent": True,
        "command_ack_required": True,
        "command_ack_timeout_seconds": 5.0,
        "command_ack_observed": True,
        "command_ack_result_code": 0,
        "command_ack_result_name": "ACCEPTED",
        "blocked_reasons": [],
    }

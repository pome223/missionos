#!/usr/bin/env python3
"""Runtime smoke for approval-gated PX4/Gazebo emergency dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import socket
import struct
import threading
import time
from typing import Any

from src.runtime.px4_gazebo_emergency_dispatcher import (
    build_px4_gazebo_emergency_command_allowlist,
    build_px4_gazebo_emergency_command_approval,
    run_px4_gazebo_emergency_command_dispatch,
)
from src.runtime.px4_gazebo_route_recovery import PX4GazeboRouteRecoveryAction
from src.runtime.px4_mavlink_ack_state import (
    MAV_RESULT_ACCEPTED,
    encode_mavlink2_command_ack,
)
from src.runtime.px4_real_mavlink_transport import (
    MAVLINK_MSG_ID_COMMAND_LONG,
    decode_mavlink2_frame,
)

OPT_IN_ENV = "RUN_PX4_GAZEBO_EMERGENCY_DISPATCH_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo emergency dispatch smoke."
        )


def _command_id_from_payload(decoded: dict[str, Any]) -> int:
    return int(struct.unpack("<H", decoded["payload"][28:30])[0])


class _FakePX4EmergencyEndpoint:
    def __init__(self) -> None:
        self.received_command_ids: list[int] = []
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.port: int | None = None

    def __enter__(self) -> "_FakePX4EmergencyEndpoint":
        self._thread.start()
        if not self._ready.wait(2):
            raise RuntimeError("fake PX4 emergency endpoint did not start")
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"x", ("127.0.0.1", self.port or 9))
        self._thread.join(timeout=2)

    def _run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(0.2)
            self.port = int(sock.getsockname()[1])
            self._ready.set()
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                if data == b"x":
                    continue
                decoded = decode_mavlink2_frame(data)
                if decoded["msg_id"] != MAVLINK_MSG_ID_COMMAND_LONG:
                    continue
                command_id = _command_id_from_payload(decoded)
                self.received_command_ids.append(command_id)
                sock.sendto(
                    encode_mavlink2_command_ack(
                        command_id=command_id,
                        result_code=MAV_RESULT_ACCEPTED,
                        sequence=90,
                    ),
                    addr,
                )


def main() -> None:
    _require_opt_in()
    approval = build_px4_gazebo_emergency_command_approval(
        operator_approval_performed=True,
        now=NOW,
    )
    allowlist = build_px4_gazebo_emergency_command_allowlist(
        approval=approval,
        now=NOW,
    )
    with _FakePX4EmergencyEndpoint() as endpoint:
        if endpoint.port is None:
            raise RuntimeError("fake PX4 emergency endpoint has no port")
        results = [
            run_px4_gazebo_emergency_command_dispatch(
                recovery_action=action,
                approval=approval,
                allowlist=allowlist,
                endpoint_port=endpoint.port,
                live_mavlink_opt_in=True,
                ack_timeout_seconds=1.0,
                now=NOW,
            )
            for action in (
                PX4GazeboRouteRecoveryAction.HOLD,
                PX4GazeboRouteRecoveryAction.LAND,
                PX4GazeboRouteRecoveryAction.RETURN_TO_LAUNCH,
            )
        ]
        time.sleep(0.05)

    summary = {
        "schema_versions": [item.schema_version for item in results],
        "dispatch_statuses": [item.dispatch_status for item in results],
        "recovery_actions": [item.recovery_action for item in results],
        "command_ids": [item.command_id for item in results],
        "frame_sent": [item.frame_sent for item in results],
        "command_ack_observed": [item.command_ack_observed for item in results],
        "command_ack_result_names": [item.command_ack_result_name for item in results],
        "received_command_ids": endpoint.received_command_ids,
        "hardware_target_allowed": [item.hardware_target_allowed for item in results],
        "physical_execution_invoked": [
            item.physical_execution_invoked for item in results
        ],
        "approval_free_recovery_dispatch_allowed": [
            item.approval_free_recovery_dispatch_allowed for item in results
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()

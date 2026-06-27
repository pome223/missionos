#!/usr/bin/env python3
"""Opt-in smoke for the runtime PX4/Gazebo live MAVLink dispatcher."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import socket
import threading
import time

from src.runtime.px4_gazebo_coupled_delivery import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    build_px4_gazebo_coupled_command_allowlist,
    build_px4_gazebo_coupled_command_approval,
)
from src.runtime.px4_live_mavlink_dispatcher import (
    run_px4_gazebo_live_mavlink_dispatch,
)
from src.runtime.px4_real_mavlink_transport import decode_mavlink2_header

OPT_IN_ENV = "RUN_PX4_LIVE_MAVLINK_DISPATCHER_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class _FakePX4Endpoint:
    def __init__(self) -> None:
        self.received: list[dict[str, object]] = []
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.port: int | None = None

    def __enter__(self) -> "_FakePX4Endpoint":
        self._thread.start()
        if not self._ready.wait(2):
            raise RuntimeError("fake PX4 endpoint did not start")
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"x", ("127.0.0.1", self.port or 9))
        self._thread.join(timeout=2)

    def wait_for_count(self, count: int, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.received) >= count:
                return
            time.sleep(0.01)

    def _run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(0.2)
            self.port = int(sock.getsockname()[1])
            self._ready.set()
            while not self._stop.is_set():
                try:
                    data, _addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                if data == b"x":
                    continue
                self.received.append(decode_mavlink2_header(data))


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4 live MAVLink dispatcher smoke."
        )


def main() -> None:
    _require_opt_in()
    approval = build_px4_gazebo_coupled_command_approval(
        operator_approval_performed=True,
        now=NOW,
    )
    allowlist = build_px4_gazebo_coupled_command_allowlist(
        approval=approval,
        now=NOW,
    )
    with _FakePX4Endpoint() as endpoint:
        if endpoint.port is None:
            raise RuntimeError("fake PX4 endpoint did not publish a port")
        dispatch = run_px4_gazebo_live_mavlink_dispatch(
            approval=approval,
            allowlist=allowlist,
            command_id=MAV_CMD_COMPONENT_ARM_DISARM,
            endpoint_port=endpoint.port,
            live_mavlink_opt_in=True,
            now=NOW,
        )
        endpoint.wait_for_count(4)

    received_msg_ids = [item["msg_id"] for item in endpoint.received]
    if 76 not in received_msg_ids:
        raise RuntimeError("fake PX4 endpoint did not receive COMMAND_LONG")
    summary = {
        "schema_version": dispatch.schema_version,
        "dispatch_mode": dispatch.dispatch_mode,
        "command_name": dispatch.command_name,
        "mavlink_message_id": dispatch.mavlink_message_id,
        "mavlink_frame_sent": dispatch.mavlink_frame_sent,
        "delivery_phase_command_frame_sent": (
            dispatch.delivery_phase_command_frame_sent
        ),
        "delivery_phase_command_ack_observed": (
            dispatch.delivery_phase_command_ack_observed
        ),
        "delivery_phase_command_executed": dispatch.delivery_phase_command_executed,
        "state_transition_observed": dispatch.state_transition_observed,
        "received_msg_ids": received_msg_ids,
        "hardware_target_allowed": dispatch.hardware_target_allowed,
        "physical_execution_invoked": dispatch.physical_execution_invoked,
        "ack_wait_performed": dispatch.ack_wait_performed,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

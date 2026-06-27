#!/usr/bin/env python3
"""Opt-in smoke for ACK/state wait plus bounded route plan artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import socket
import threading

from src.runtime.px4_gazebo_coupled_delivery import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_TAKEOFF,
    build_px4_gazebo_coupled_command_allowlist,
    build_px4_gazebo_coupled_command_approval,
)
from src.runtime.px4_gazebo_route_plan import (
    build_px4_gazebo_pickup_dropoff_route_plan,
)
from src.runtime.px4_mavlink_ack_state import (
    MAV_RESULT_ACCEPTED,
    build_px4_gazebo_runtime_delivery_runner_result,
    encode_mavlink2_command_ack,
    run_px4_gazebo_live_mavlink_dispatch_with_ack,
    wait_for_px4_state_from_logs,
)
from src.runtime.px4_real_mavlink_transport import decode_mavlink2_header

OPT_IN_ENV = "RUN_PX4_MAVLINK_ACK_STATE_ROUTE_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class _FakeAckingPX4Endpoint:
    def __init__(self) -> None:
        self.received: list[dict[str, object]] = []
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.port: int | None = None

    def __enter__(self) -> "_FakeAckingPX4Endpoint":
        self._thread.start()
        if not self._ready.wait(2):
            raise RuntimeError("fake PX4 ACK endpoint did not start")
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
                decoded = decode_mavlink2_header(data)
                self.received.append(decoded)
                if decoded["msg_id"] == 76:
                    command_id = int.from_bytes(data[38:40], "little")
                    sock.sendto(
                        encode_mavlink2_command_ack(
                            command_id=command_id,
                            result_code=MAV_RESULT_ACCEPTED,
                            sequence=len(self.received),
                        ),
                        addr,
                    )


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run the PX4 ACK/state/route smoke.")


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
    dispatches = []
    acks = []
    with _FakeAckingPX4Endpoint() as endpoint:
        if endpoint.port is None:
            raise RuntimeError("fake endpoint did not publish a port")
        for command_id in (
            MAV_CMD_COMPONENT_ARM_DISARM,
            MAV_CMD_NAV_TAKEOFF,
            MAV_CMD_NAV_LAND,
        ):
            dispatch, ack = run_px4_gazebo_live_mavlink_dispatch_with_ack(
                approval=approval,
                allowlist=allowlist,
                command_id=command_id,
                endpoint_port=endpoint.port,
                live_mavlink_opt_in=True,
                now=NOW,
            )
            dispatches.append(dispatch)
            acks.append(ack)
    states = [
        wait_for_px4_state_from_logs(
            expected_state="armed",
            px4_logs="Armed by external command",
            timeout_seconds=1.0,
            now=NOW,
        ),
        wait_for_px4_state_from_logs(
            expected_state="airborne",
            px4_logs="Takeoff detected",
            timeout_seconds=1.0,
            now=NOW,
        ),
        wait_for_px4_state_from_logs(
            expected_state="landing",
            px4_logs="Landing detected",
            timeout_seconds=1.0,
            now=NOW,
        ),
        wait_for_px4_state_from_logs(
            expected_state="landed_disarmed",
            px4_logs="Disarmed by landing",
            timeout_seconds=1.0,
            now=NOW,
        ),
    ]
    runner = build_px4_gazebo_runtime_delivery_runner_result(
        dispatch_results=dispatches,
        command_ack_results=acks,
        state_wait_results=states,
        observed_delivery_phases=["pickup", "enroute", "dropoff", "completed"],
        now=NOW,
    )
    route = build_px4_gazebo_pickup_dropoff_route_plan(
        pickup_pad_ref="gazebo_pad:pickup",
        dropoff_pad_ref="gazebo_pad:dropoff",
        route_waypoint_refs=["gazebo_waypoint:mid"],
        geofence_polygon=[(-2.0, -2.0), (8.0, -2.0), (8.0, 8.0), (-2.0, 8.0)],
        altitude_min_m=1.0,
        altitude_max_m=4.0,
        min_battery_margin_pct=25.0,
        now=NOW,
    )
    summary = {
        "dispatch_schema_version": dispatches[0].schema_version,
        "ack_schema_version": acks[0].schema_version,
        "ack_statuses": [ack.ack_status for ack in acks],
        "ack_result_names": [ack.ack_result_name for ack in acks],
        "required_command_ids": runner.required_command_ids,
        "observed_command_ids": runner.observed_command_ids,
        "missing_command_ids": runner.missing_command_ids,
        "state_wait_statuses": [state.state_wait_status for state in states],
        "runner_schema_version": runner.schema_version,
        "runner_final_status": runner.final_status,
        "route_plan_schema_version": route.schema_version,
        "route_plan_only": route.route_plan_only,
        "route_command_dispatch_allowed": route.route_command_dispatch_allowed,
        "hardware_target_allowed": route.hardware_target_allowed,
        "physical_execution_invoked": route.physical_execution_invoked,
        "received_msg_ids": [item["msg_id"] for item in endpoint.received],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    assert summary["ack_statuses"] == ["accepted", "accepted", "accepted"]
    assert summary["runner_final_status"] == "completed"
    assert summary["missing_command_ids"] == ()
    assert summary["route_plan_only"] is True
    assert summary["route_command_dispatch_allowed"] is False


if __name__ == "__main__":
    main()

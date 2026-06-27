#!/usr/bin/env python3
"""Opt-in smoke for bounded PX4/Gazebo route command dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import socket
import threading
import time

from src.runtime.px4_gazebo_coupled_delivery import (
    build_px4_gazebo_coupled_command_approval,
)
from src.runtime.px4_gazebo_route_dispatcher import (
    build_px4_gazebo_route_command_allowlist,
    build_px4_gazebo_route_progress_evidence,
    run_px4_gazebo_route_command_dispatch,
)
from src.runtime.px4_gazebo_route_plan import (
    build_px4_gazebo_pickup_dropoff_route_plan,
)
from src.runtime.px4_real_mavlink_transport import decode_mavlink2_header

OPT_IN_ENV = "RUN_PX4_GAZEBO_ROUTE_DISPATCHER_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class _FakeRoutePX4Endpoint:
    def __init__(self) -> None:
        self.received: list[dict[str, object]] = []
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.port: int | None = None

    def __enter__(self) -> "_FakeRoutePX4Endpoint":
        self._thread.start()
        if not self._ready.wait(2):
            raise RuntimeError("fake PX4 route endpoint did not start")
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
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run the PX4 route dispatcher smoke.")


def main() -> None:
    _require_opt_in()
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
    approval = build_px4_gazebo_coupled_command_approval(
        operator_approval_performed=True,
        now=NOW,
    )
    allowlist = build_px4_gazebo_route_command_allowlist(
        route_plan=route,
        approval=approval,
        now=NOW,
    )
    with _FakeRoutePX4Endpoint() as endpoint:
        if endpoint.port is None:
            raise RuntimeError("fake route endpoint did not publish a port")
        dispatch = run_px4_gazebo_route_command_dispatch(
            route_plan=route,
            route_allowlist=allowlist,
            approval=approval,
            endpoint_port=endpoint.port,
            live_mavlink_opt_in=True,
            setpoint_frames=3,
            now=NOW,
        )
        endpoint.wait_for_count(4)
    progress = build_px4_gazebo_route_progress_evidence(
        route_plan=route,
        route_dispatch_result=dispatch,
        pickup_pose_xy_m=(0.0, 0.0),
        observed_pose_xy_m=(7.25, 4.0),
        now=NOW,
    )
    summary = {
        "route_plan_schema_version": route.schema_version,
        "allowlist_schema_version": allowlist.schema_version,
        "dispatch_schema_version": dispatch.schema_version,
        "progress_schema_version": progress.schema_version,
        "mavlink_message_id": dispatch.mavlink_message_id,
        "setpoint_frames_sent": dispatch.setpoint_frames_sent,
        "received_msg_ids": [item["msg_id"] for item in endpoint.received],
        "route_command_frame_sent": dispatch.route_command_frame_sent,
        "route_command_ack_applicable": dispatch.route_command_ack_applicable,
        "route_command_ack_observed": dispatch.route_command_ack_observed,
        "horizontal_route_motion_observed": dispatch.horizontal_route_motion_observed,
        "route_state_wait_deferred_to_issue": (
            dispatch.route_state_wait_deferred_to_issue
        ),
        "telemetry_completion_gate_deferred_to_issue": (
            dispatch.telemetry_completion_gate_deferred_to_issue
        ),
        "dropoff_region_reached": progress.dropoff_region_reached,
        "horizontal_progress_m": progress.horizontal_progress_m,
        "route_geofence_violation": progress.route_geofence_violation,
        "route_plan_only": route.route_plan_only,
        "route_command_dispatch_allowed_on_route_plan": (
            route.route_command_dispatch_allowed
        ),
        "px4_mission_upload_allowed": dispatch.px4_mission_upload_allowed,
        "unbounded_setpoint_stream_allowed": dispatch.unbounded_setpoint_stream_allowed,
        "hardware_target_allowed": dispatch.hardware_target_allowed,
        "physical_execution_invoked": dispatch.physical_execution_invoked,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    assert summary["received_msg_ids"] == [0, 84, 84, 84]
    assert summary["route_command_frame_sent"] is True
    assert summary["route_command_ack_applicable"] is False
    assert summary["route_command_ack_observed"] is False
    assert summary["horizontal_route_motion_observed"] is False
    assert summary["dropoff_region_reached"] is True
    assert summary["route_plan_only"] is True
    assert summary["route_command_dispatch_allowed_on_route_plan"] is False
    assert summary["px4_mission_upload_allowed"] is False
    assert summary["unbounded_setpoint_stream_allowed"] is False


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Opt-in smoke for PX4/Gazebo route delivery completion gate artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import socket
import threading
import time
from tempfile import TemporaryDirectory

from src.runtime.px4_gazebo_coupled_delivery import (
    build_px4_gazebo_coupled_command_approval,
)
from src.runtime.px4_gazebo_route_delivery import (
    build_px4_gazebo_route_delivery_completion_gate,
    run_px4_gazebo_route_delivery_task,
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
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_PX4_GAZEBO_ROUTE_DELIVERY_COMPLETION_SMOKE"
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
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo route completion smoke."
        )


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
    gate = build_px4_gazebo_route_delivery_completion_gate(
        route_plan=route,
        route_dispatch_result=dispatch,
        route_progress_evidence=progress,
        horizontal_route_motion_observed=True,
        px4_telemetry_correlated=True,
        gazebo_pose_correlated=True,
        actual_px4_gazebo_horizontal_smoke_observed=False,
        now=NOW,
    )
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_gazebo_route_delivery",
            title="PX4/Gazebo route delivery completion smoke",
            status="running",
            artifacts={"existing": {"case_id": "route-completion", "kept": True}},
        )
        updated = run_px4_gazebo_route_delivery_task(
            task["task_id"],
            completion_gate=gate,
            now=NOW,
            task_store_factory=lambda: store,
        )

    runner = updated["artifacts"]["px4_gazebo_route_delivery_runner_result"]
    summary = {
        "task_status": updated["status"],
        "existing_artifacts_retained": updated["artifacts"]["existing"]["kept"],
        "route_plan_schema_version": route.schema_version,
        "dispatch_schema_version": dispatch.schema_version,
        "progress_schema_version": progress.schema_version,
        "completion_gate_schema_version": gate.schema_version,
        "runner_schema_version": runner["schema_version"],
        "final_status": runner["final_status"],
        "completion_basis": runner["completion_basis"],
        "received_msg_ids": [item["msg_id"] for item in endpoint.received],
        "dropoff_region_reached": gate.dropoff_region_reached,
        "horizontal_progress_m": gate.horizontal_progress_m,
        "horizontal_route_motion_observed": gate.horizontal_route_motion_observed,
        "px4_telemetry_correlated": gate.px4_telemetry_correlated,
        "gazebo_pose_correlated": gate.gazebo_pose_correlated,
        "route_progress_fresh": gate.route_progress_fresh,
        "route_progress_age_seconds": gate.route_progress_age_seconds,
        "max_route_progress_age_seconds": gate.max_route_progress_age_seconds,
        "pose_observed": gate.pose_observed,
        "expected_vehicle_ref": gate.expected_vehicle_ref,
        "observed_vehicle_ref": gate.observed_vehicle_ref,
        "wrong_delivery_vehicle": gate.wrong_delivery_vehicle,
        "actual_px4_gazebo_horizontal_smoke_observed": (
            gate.actual_px4_gazebo_horizontal_smoke_observed
        ),
        "missing_completion_evidence": list(gate.missing_completion_evidence),
        "blocked_reasons": list(gate.blocked_reasons),
        "hardware_target_allowed": gate.hardware_target_allowed,
        "physical_execution_invoked": gate.physical_execution_invoked,
        "px4_mission_upload_allowed": gate.px4_mission_upload_allowed,
        "unbounded_setpoint_stream_allowed": gate.unbounded_setpoint_stream_allowed,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    assert summary["task_status"] == "completed"
    assert summary["existing_artifacts_retained"] is True
    assert summary["received_msg_ids"] == [0, 84, 84, 84]
    assert summary["final_status"] == "completed"
    assert summary["dropoff_region_reached"] is True
    assert summary["horizontal_route_motion_observed"] is True
    assert summary["px4_telemetry_correlated"] is True
    assert summary["gazebo_pose_correlated"] is True
    assert summary["route_progress_fresh"] is True
    assert summary["pose_observed"] is True
    assert summary["wrong_delivery_vehicle"] is False
    assert summary["actual_px4_gazebo_horizontal_smoke_observed"] is False
    assert summary["missing_completion_evidence"] == []
    assert summary["blocked_reasons"] == []
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["px4_mission_upload_allowed"] is False
    assert summary["unbounded_setpoint_stream_allowed"] is False


if __name__ == "__main__":
    main()

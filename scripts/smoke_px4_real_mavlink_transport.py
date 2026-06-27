#!/usr/bin/env python3
"""Runtime smoke for opt-in real MAVLink loopback transport."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import socket
import threading
from tempfile import TemporaryDirectory

from src.runtime.px4_delivery_command_preflight import (
    PX4SimulationCommandKind,
    build_px4_simulation_command_preflight_artifacts,
)
from src.runtime.px4_gazebo_delivery_world_profile import (
    build_px4_gazebo_delivery_world_profile,
)
from src.runtime.px4_real_mavlink_transport import (
    decode_mavlink2_frame,
    encode_mavlink2_heartbeat,
    run_px4_mavlink_heartbeat_status_query,
    run_px4_real_mavlink_dispatch_result,
)
from src.runtime.px4_sitl_delivery_observation import (
    build_px4_sitl_delivery_observation_from_logs,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_PX4_REAL_MAVLINK_TRANSPORT_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
PX4_SITL_LOGS = "\n".join(
    [
        "INFO  [px4] startup script: /bin/sh etc/init.d-posix/rcS 0",
        "INFO  [init] found model autostart file as SYS_AUTOSTART=10040",
        "INFO  [init] SIH simulator",
        "INFO  [simulator_sih] Simulation loop with 250 Hz",
        "INFO  [logger] logger started (mode=all)",
        "INFO  [px4] Startup script returned successfully",
    ]
)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4 real MAVLink transport smoke."
        )


class FakePX4MAVLinkEndpoint:
    def __init__(self) -> None:
        self.received: list[dict] = []
        self.port: int | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "FakePX4MAVLinkEndpoint":
        self._thread.start()
        if not self._ready.wait(2):
            raise RuntimeError("fake MAVLink endpoint did not start")
        return self

    def __exit__(self, *_exc) -> None:
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
                self.received.append(decoded)
                sock.sendto(
                    encode_mavlink2_heartbeat(
                        sequence=len(self.received),
                        system_id=1,
                        component_id=1,
                    ),
                    addr,
                )


def _preflight_artifacts():
    profile = build_px4_gazebo_delivery_world_profile(now=NOW)
    observation = build_px4_sitl_delivery_observation_from_logs(
        PX4_SITL_LOGS,
        captured_at=NOW,
        profile=profile,
    )
    return build_px4_simulation_command_preflight_artifacts(
        profile=profile,
        observation=observation,
        operator_approval_performed=True,
        now=NOW,
    )


def main() -> int:
    _require_opt_in()
    preflight = _preflight_artifacts()
    with TemporaryDirectory() as tmp, FakePX4MAVLinkEndpoint() as endpoint:
        assert endpoint.port is not None
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_real_mavlink_transport",
            title="PX4 real MAVLink transport smoke",
            status="running",
            artifacts={"existing": {"case_id": "real-mavlink", "kept": True}},
        )
        connection, query = run_px4_mavlink_heartbeat_status_query(
            endpoint_port=endpoint.port,
            opt_in=True,
            timeout_seconds=1.0,
            now=NOW,
        )
        dispatch = run_px4_real_mavlink_dispatch_result(
            approval=preflight["px4_simulation_command_approval"],
            allowlist=preflight["px4_simulation_command_allowlist"],
            command_kind=PX4SimulationCommandKind.START_DELIVERY_MISSION,
            endpoint_port=endpoint.port,
            opt_in=True,
            timeout_seconds=1.0,
            now=NOW,
        )
        updated = store.update(
            task["task_id"],
            artifacts={
                "px4_real_mavlink_transport_connection": connection.model_dump(
                    mode="json"
                ),
                "px4_mavlink_heartbeat_status_query": query.model_dump(mode="json"),
                "px4_real_mavlink_dispatch_result": dispatch.model_dump(mode="json"),
            },
        )

    assert updated is not None
    summary = {
        "task_status": updated["status"],
        "existing_artifacts_retained": updated["artifacts"]["existing"]["kept"],
        "connection_schema": connection.schema_version,
        "query_schema": query.schema_version,
        "dispatch_schema": dispatch.schema_version,
        "endpoint_host": connection.endpoint_host,
        "mavlink_socket_opened": dispatch.mavlink_socket_opened,
        "heartbeat_frame_sent": query.mavlink_frame_sent,
        "heartbeat_frame_received": query.mavlink_frame_received,
        "dispatch_frame_sent": dispatch.mavlink_frame_sent,
        "dispatch_frame_received": dispatch.mavlink_frame_received,
        "dispatch_status": dispatch.dispatch_status.value,
        "dispatch_transport_semantics": dispatch.dispatch_transport_semantics,
        "mavlink_command_id": dispatch.mavlink_command_id,
        "mavlink_command_name": dispatch.mavlink_command_name,
        "requested_message_id": dispatch.requested_message_id,
        "requested_message_name": dispatch.requested_message_name,
        "delivery_phase_command_executed": dispatch.delivery_phase_command_executed,
        "fake_endpoint_received_frames": len(endpoint.received),
        "received_msg_ids": [item["msg_id"] for item in endpoint.received],
        "hardware_target_allowed": dispatch.hardware_target_allowed,
        "physical_execution_invoked": dispatch.physical_execution_invoked,
        "raw_mavlink_payload_present": dispatch.raw_mavlink_payload_present,
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    assert summary["task_status"] == "running"
    assert summary["existing_artifacts_retained"] is True
    assert summary["endpoint_host"] == "127.0.0.1"
    assert summary["mavlink_socket_opened"] is True
    assert summary["heartbeat_frame_sent"] is True
    assert summary["heartbeat_frame_received"] is True
    assert summary["dispatch_frame_sent"] is True
    assert summary["dispatch_frame_received"] is True
    assert summary["dispatch_status"] == "sent"
    assert summary["dispatch_transport_semantics"] == "heartbeat_status_query"
    assert summary["mavlink_command_id"] == 512
    assert summary["mavlink_command_name"] == "MAV_CMD_REQUEST_MESSAGE"
    assert summary["requested_message_id"] == 0
    assert summary["requested_message_name"] == "HEARTBEAT"
    assert summary["delivery_phase_command_executed"] is False
    assert summary["fake_endpoint_received_frames"] == 2
    assert summary["received_msg_ids"] == [0, 76]
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["raw_mavlink_payload_present"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

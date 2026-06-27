#!/usr/bin/env python3
"""Runtime smoke for PX4/Gazebo telemetry sidecar failure handling."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import sys
import threading
from tempfile import TemporaryDirectory
from typing import Any

from src.runtime.px4_gazebo_telemetry_sidecar_client import (
    Px4GazeboTelemetrySidecarClientError,
    attach_px4_gazebo_telemetry_sidecar_smoke_artifacts,
)
from src.runtime.task_store import TaskStore


class InvalidTelemetryHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "command_payload_allowed": False,
                    "ros_dispatch_allowed": False,
                    "mavlink_dispatch_allowed": False,
                    "actuator_execution_allowed": False,
                    "live_execution_allowed": False,
                    "physical_execution_invoked": False,
                },
            )
            return
        if self.path.startswith("/telemetry"):
            self._send_json(
                HTTPStatus.OK,
                {
                    "sample_id": "invalid-command-like-telemetry",
                    "source": {
                        "source_kind": "px4_gazebo_telemetry_sidecar",
                        "source_id": "invalid-sidecar",
                        "vehicle_id": "iris-invalid",
                    },
                    "captured_at": "2026-04-30T16:00:00+00:00",
                    "telemetry": {"altitude_m": 1.0},
                    "metadata": {"nested": [{"RosTopic": "/cmd_vel"}]},
                },
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_failure_preserves_task(store: TaskStore, task_id: str) -> None:
    stored = store.get(task_id)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"] == {"existing": {"kept": True}}


def main() -> int:
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        unavailable_task = store.create(
            kind="control_supervisor",
            title="PX4/Gazebo sidecar unavailable smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        unused_port = _free_loopback_port()
        try:
            attach_px4_gazebo_telemetry_sidecar_smoke_artifacts(
                unavailable_task["task_id"],
                base_url=f"http://127.0.0.1:{unused_port}",
                timeout_seconds=0.2,
                task_store_factory=lambda: store,
            )
        except Px4GazeboTelemetrySidecarClientError as exc:
            unavailable_error = str(exc)
        else:  # pragma: no cover - fail path
            raise AssertionError("unavailable sidecar should fail closed")
        _assert_failure_preserves_task(store, unavailable_task["task_id"])

        invalid_task = store.create(
            kind="control_supervisor",
            title="PX4/Gazebo sidecar invalid response smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        port = _free_loopback_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), InvalidTelemetryHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            try:
                attach_px4_gazebo_telemetry_sidecar_smoke_artifacts(
                    invalid_task["task_id"],
                    base_url=f"http://127.0.0.1:{port}",
                    task_store_factory=lambda: store,
                )
            except Px4GazeboTelemetrySidecarClientError as exc:
                invalid_error = str(exc)
            else:  # pragma: no cover - fail path
                raise AssertionError("invalid telemetry should fail closed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        _assert_failure_preserves_task(store, invalid_task["task_id"])

    print(
        json.dumps(
            {
                "unavailable_rejected": "unavailable" in unavailable_error,
                "invalid_response_rejected": "RosTopic" in invalid_error,
                "task_status_preserved": True,
                "existing_artifacts_retained": True,
                "hil_artifacts_persisted_on_failure": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

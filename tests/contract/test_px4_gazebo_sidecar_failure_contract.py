import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
from typing import Any

import pytest

from src.runtime.px4_gazebo_telemetry_sidecar_client import (
    Px4GazeboTelemetrySidecarClientError,
    attach_px4_gazebo_telemetry_sidecar_smoke_artifacts,
)
from src.runtime.task_store import TaskStore


class InvalidTelemetryHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
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
        elif self.path.startswith("/telemetry"):
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
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _new_task(store: TaskStore) -> dict:
    return store.create(
        kind="control_supervisor",
        title="PX4/Gazebo sidecar failure contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )


def _assert_task_preserved(store: TaskStore, task_id: str) -> None:
    stored = store.get(task_id)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"] == {"existing": {"kept": True}}


def test_unavailable_sidecar_fails_closed(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = _new_task(store)

    with pytest.raises(Px4GazeboTelemetrySidecarClientError, match="unavailable"):
        attach_px4_gazebo_telemetry_sidecar_smoke_artifacts(
            task["task_id"],
            base_url=f"http://127.0.0.1:{_free_loopback_port()}",
            timeout_seconds=0.2,
            task_store_factory=lambda: store,
        )

    _assert_task_preserved(store, task["task_id"])


def test_command_like_sidecar_response_fails_closed(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = _new_task(store)
    port = _free_loopback_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), InvalidTelemetryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(Px4GazeboTelemetrySidecarClientError, match="RosTopic"):
            attach_px4_gazebo_telemetry_sidecar_smoke_artifacts(
                task["task_id"],
                base_url=f"http://127.0.0.1:{port}",
                task_store_factory=lambda: store,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    _assert_task_preserved(store, task["task_id"])

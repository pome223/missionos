"""HTTP service wrapper for the mock simulator adapter smoke chain.

This service is intentionally small and dry-run-only. It exposes the existing
mock adapter-backed artifact chain across a process / Docker boundary without
introducing simulator execution, command dispatch, ROS / MAVLink integration,
actuator control, or live physical execution.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from src.runtime.mock_simulator_adapter import build_mock_simulator_adapter_smoke_chain
from src.runtime.simulator_adapter_contract import (
    build_mock_physical_simulator_adapter_contract,
)


MOCK_ADAPTER_SERVICE_SCHEMA_VERSION = "mock_simulator_adapter_service.v1"
MOCK_ADAPTER_RUN_RESULT_SCHEMA_VERSION = "mock_simulator_adapter_run_result.v1"
MOCK_ADAPTER_SERVICE_NAME = "boiled-claw-mock-simulator"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18888


_FORBIDDEN_REQUEST_KEYS = {
    "action",
    "actions",
    "actuator",
    "actuator_execution",
    "actuator_execution_allowed",
    "command",
    "command_payload",
    "command_payload_allowed",
    "dispatch",
    "dispatch_implementation_present",
    "live_execution_allowed",
    "mavlink",
    "mavlink_dispatch",
    "mavlink_dispatch_allowed",
    "motor_command",
    "physical_execution_invoked",
    "ros",
    "ros_dispatch",
    "ros_dispatch_allowed",
    "ros_topic",
    "send_command",
}
_FORBIDDEN_REQUEST_KEYS_NORMALIZED = {
    re.sub(r"[^a-z0-9]", "", key.lower()) for key in _FORBIDDEN_REQUEST_KEYS
}


class MockAdapterServiceError(ValueError):
    """Raised for client-visible mock adapter service request errors."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MockAdapterServiceError("now must be an ISO-8601 string when provided")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MockAdapterServiceError("now must be an ISO-8601 string") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _find_forbidden_request_key(value: Any, *, path: str = "$") -> str | None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_path = f"{path}.{key}"
            if _normalize_key(key) in _FORBIDDEN_REQUEST_KEYS_NORMALIZED:
                return key_path
            found = _find_forbidden_request_key(nested, path=key_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _find_forbidden_request_key(item, path=f"{path}[{index}]")
            if found:
                return found
    return None


def _telemetry_payload_for_case(
    telemetry_case: str,
    *,
    now: datetime,
) -> tuple[dict[str, Any] | None, bool]:
    case = telemetry_case.strip().lower()
    if case in {"", "nominal", "mock_nominal"}:
        return None, False
    if case in {"stale", "stale_telemetry"}:
        return {
            "timestamp": (now - timedelta(seconds=120)).isoformat(),
            "signals": {
                "battery": "ok",
                "localization": "ok",
                "comms": "ok",
                "safety": "nominal",
            },
        }, False
    if case in {"missing", "missing_telemetry"}:
        return {}, False
    if case in {"unsafe", "unsafe_telemetry"}:
        return {
            "timestamp": now.isoformat(),
            "signals": {
                "battery": "low",
                "localization": "ok",
                "comms": "ok",
                "safety": "unsafe",
            },
        }, False
    if case in {"broken_hash", "replay_hash_mismatch"}:
        return None, True
    raise MockAdapterServiceError(
        "telemetry_case must be one of nominal, stale, missing, unsafe, broken_hash"
    )


def build_mock_adapter_health_response(*, now: datetime | None = None) -> dict[str, Any]:
    checked_at = now or _utc_now()
    return {
        "schema_version": MOCK_ADAPTER_SERVICE_SCHEMA_VERSION,
        "service": MOCK_ADAPTER_SERVICE_NAME,
        "status": "ok",
        "checked_at": checked_at.isoformat(),
        "dry_run_only": True,
        "operator_approval_required": True,
        "live_execution_allowed": False,
        "physical_execution_invoked": False,
        "command_payload_allowed": False,
        "ros_dispatch_allowed": False,
        "mavlink_dispatch_allowed": False,
        "actuator_execution_allowed": False,
        "dispatch_implementation_present": False,
    }


def build_mock_adapter_run_response(
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = payload or {}
    forbidden_key = _find_forbidden_request_key(request)
    if forbidden_key:
        raise MockAdapterServiceError(
            f"request contains command-like or dispatch-like key: {forbidden_key}"
        )
    mode = str(request.get("mode", "dry_run_only"))
    if mode != "dry_run_only":
        raise MockAdapterServiceError("mode must be dry_run_only")
    now = _parse_datetime(request.get("now")) or _utc_now()
    scenario_id = str(request.get("scenario_id", "mock_nominal")).strip()
    telemetry_case = str(request.get("telemetry_case", "nominal"))
    telemetry_payload, break_replay_hash = _telemetry_payload_for_case(
        telemetry_case,
        now=now,
    )
    artifacts = build_mock_simulator_adapter_smoke_chain(
        telemetry_payload=telemetry_payload,
        break_replay_hash=break_replay_hash,
        now=now,
    )
    return {
        "schema_version": MOCK_ADAPTER_RUN_RESULT_SCHEMA_VERSION,
        "service": MOCK_ADAPTER_SERVICE_NAME,
        "scenario_id": scenario_id or "mock_nominal",
        "mode": "dry_run_only",
        "created_at": now.isoformat(),
        "operator_approval_required": True,
        "live_execution_allowed": False,
        "physical_execution_invoked": False,
        "command_payload_allowed": False,
        "ros_dispatch_allowed": False,
        "mavlink_dispatch_allowed": False,
        "actuator_execution_allowed": False,
        "dispatch_implementation_present": False,
        "artifacts": artifacts,
    }


class MockAdapterRequestHandler(BaseHTTPRequestHandler):
    """Minimal JSON handler for the mock simulator adapter service."""

    server_version = "BoiledClawMockSimulator/1"

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MockAdapterServiceError("request body must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise MockAdapterServiceError("request body must be a JSON object")
        return parsed

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json(
            status,
            {
                "schema_version": MOCK_ADAPTER_SERVICE_SCHEMA_VERSION,
                "service": MOCK_ADAPTER_SERVICE_NAME,
                "status": "error",
                "error": message,
                "live_execution_allowed": False,
                "physical_execution_invoked": False,
                "dispatch_implementation_present": False,
            },
        )

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, build_mock_adapter_health_response())
            return
        if self.path == "/contract":
            contract = build_mock_physical_simulator_adapter_contract()
            self._send_json(HTTPStatus.OK, contract.model_dump(mode="json"))
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/run":
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        try:
            payload = self._read_json()
            response = build_mock_adapter_run_response(payload)
        except MockAdapterServiceError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._send_json(HTTPStatus.OK, response)

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)


def serve(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, quiet: bool = False) -> None:
    server = ThreadingHTTPServer((host, port), MockAdapterRequestHandler)
    server.quiet = quiet  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    serve(host=args.host, port=args.port, quiet=args.quiet)


if __name__ == "__main__":
    main()

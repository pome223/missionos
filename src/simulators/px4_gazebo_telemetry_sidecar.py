"""Telemetry-only PX4/Gazebo-style sidecar smoke service.

This sidecar is intentionally not PX4 or Gazebo. It is a tiny external process
that emits PX4/Gazebo-style telemetry/log payloads so Mission OS can exercise a
real process and HTTP boundary before any real simulator integration.
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse


SERVICE_NAME = "boiled-claw-px4-gazebo-telemetry-sidecar"
DEFAULT_CAPTURED_AT = "2026-04-30T16:00:00+00:00"


def _telemetry_sample(*, telemetry_case: str = "nominal") -> dict[str, Any]:
    captured_at = (
        "2026-04-30T15:58:00+00:00"
        if telemetry_case == "stale"
        else DEFAULT_CAPTURED_AT
    )
    sample: dict[str, Any] = {
        "sample_id": f"px4_gazebo_sidecar_{telemetry_case}_telemetry",
        "source": {
            "source_kind": "px4_gazebo_telemetry_sidecar",
            "source_id": "px4-gazebo-telemetry-sidecar",
            "vehicle_id": "iris-sidecar-001",
        },
        "captured_at": captured_at,
        "telemetry": {
            "altitude_m": 3.2,
            "battery_remaining_pct": 93,
            "gps_fix": True,
            "heading_deg": 90.0,
            "velocity_mps": 0.0,
        },
        "metadata": {
            "service": SERVICE_NAME,
            "telemetry_sidecar_started": True,
            "telemetry_only": True,
            "read_only": True,
        },
    }
    if telemetry_case == "command_like":
        sample["metadata"] = {
            "service": SERVICE_NAME,
            "nested": [{"RosTopic": "/cmd_vel"}],
        }
    return sample


def _health_payload() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "status": "ok",
        "mode": "telemetry_only_sidecar",
        "px4_started": False,
        "gazebo_started": False,
        "telemetry_sidecar_started": True,
        "command_payload_allowed": False,
        "ros_dispatch_allowed": False,
        "mavlink_dispatch_allowed": False,
        "actuator_execution_allowed": False,
        "live_execution_allowed": False,
        "physical_execution_invoked": False,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "BoiledClawPx4GazeboTelemetrySidecar/1.0"

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, _health_payload())
            return
        if parsed.path == "/telemetry":
            params = parse_qs(parsed.query)
            telemetry_case = params.get("case", ["nominal"])[0]
            if telemetry_case not in {"nominal", "stale", "command_like"}:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": "unsupported telemetry case",
                        "supported_cases": ["nominal", "stale", "command_like"],
                    },
                )
                return
            self._send_json(
                HTTPStatus.OK,
                _telemetry_sample(telemetry_case=telemetry_case),
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "command and mutation endpoints are not exposed"},
        )

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18889)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        json.dumps(
            {
                "service": SERVICE_NAME,
                "status": "starting",
                "host": args.host,
                "port": args.port,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

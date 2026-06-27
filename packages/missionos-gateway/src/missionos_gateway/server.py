"""Small public-safe MissionOS Gateway fixture server.

This server intentionally covers only the first extraction boundary: enough real
HTTP behavior for the migrated CLI to start a Gateway process and exercise
status, conversation, task, timeline, and recovery-shape routes without pulling
private runtime state into this repository.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
import json
import os
import time


FIXTURE_TASK_ID = "task_fixture_delivery"


def _fixture_task(task_id: str = FIXTURE_TASK_ID) -> dict[str, Any]:
    return {
        "task": {
            "task_id": task_id,
            "kind": "missionos_fixture_runtime",
            "title": "Fixture delivery mission",
            "status": "completed",
            "metadata": {
                "missionos_fixture": True,
                "missionos_auto_mission_gui_dispatch_status": "fixture_completed",
                "actual_sitl_flight_evidence_observed": False,
                "physical_execution_invoked": False,
            },
            "artifacts": {
                "mission_designer_coordinate_pair_route": {
                    "takeoff_latitude": 35.681236,
                    "takeoff_longitude": 139.767125,
                    "dropoff_latitude": 35.6984,
                    "dropoff_longitude": 139.773,
                    "payload_weight_kg": 1.0,
                    "wind_speed_mps": 4.0,
                },
                "missionos_auto_mission_runtime_snapshot": {
                    "snapshot_status": "observed",
                    "elapsed_seconds": 0.0,
                    "progress_m": 0.0,
                    "local_x_m": 0.0,
                    "local_y_m": 0.0,
                    "mission_reached_seq": 0,
                    "waypoint_total": 8,
                    "battery_remaining_percent": 100.0,
                    "delivery_completion_claimed": False,
                    "physical_execution_invoked": False,
                    "runtime_progress_observed": False,
                },
                "missionos_auto_mission_runtime_replay": {
                    "actual_sitl_flight_evidence_observed": False,
                    "flight_path_profile": [
                        {
                            "sample_index": 0,
                            "phase": "takeoff",
                            "latitude_deg": 35.681236,
                            "longitude_deg": 139.767125,
                            "relative_alt_m": 0.0,
                        },
                        {
                            "sample_index": 1,
                            "phase": "route",
                            "latitude_deg": 35.686,
                            "longitude_deg": 139.769,
                            "relative_alt_m": 30.0,
                        },
                    ],
                },
                "missionos_auto_mission_gui_dispatch_running_receipt": {
                    "dispatch_status": "fixture_completed",
                    "artifact_root": "examples/fixture_missions",
                },
            },
        }
    }


def _fixture_timeline(task_id: str = FIXTURE_TASK_ID, *, limit: int = 8) -> dict[str, Any]:
    events = [
        {
            "created_at": "2026-06-20T00:00:01Z",
            "event_type": "mission_fixture_created",
            "status": "created",
            "detail": {"task_id": task_id},
        },
        {
            "created_at": "2026-06-20T00:00:02Z",
            "event_type": "mission_fixture_snapshot",
            "status": "fixture_only",
            "detail": {
                "runtime_progress_observed": False,
                "delivery_completion_claimed": False,
                "physical_execution_invoked": False,
            },
        },
    ]
    return {"events": events[-max(limit, 0) :] if limit else []}


class MissionOSFixtureHandler(BaseHTTPRequestHandler):
    server_version = "MissionOSFixtureGateway/0.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send_json(
                {
                    "status": "ok",
                    "session_backend": "fixture",
                    "version": "missionos-gateway-fixture.v1",
                }
            )
            return
        if path == "/missionos/form2a-response-selection":
            self._send_json(
                {
                    "summary_status": "fixture_response_selected",
                    "selected_response_kind": "operator_gated_fixture_mission",
                }
            )
            return
        if path == "/missionos/form2a-operator-review":
            self._send_json(
                {
                    "summary_status": "fixture_review_pending",
                    "human_operator_review": {"review_status": "pending"},
                }
            )
            return
        if path == "/missionos/form2a-action-consumption":
            self._send_json(
                {
                    "summary_status": "fixture_blocked_without_approval",
                    "authority_boundary": {
                        "blocking_reasons": ["human_review_missing"],
                    },
                }
            )
            return
        if path == "/missionos/llm-repair-planner":
            self._send_json(
                {
                    "summary_status": "fixture_not_invoked",
                    "repair_proposal": {"repair_target": "none"},
                }
            )
            return
        if path.startswith("/tasks/"):
            self._handle_task_get(path, query)
            return
        self._send_json({"detail": f"unknown route: {path}"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        payload = self._read_json_body()

        if parsed.path == "/missionos/autonomy-conversation/run":
            instruction = str(payload.get("operator_instruction") or "")
            route_hint = payload.get("missionos_route_hint")
            routed_action = str(route_hint or "fixture_plan")
            self._send_json(
                {
                    "schema_version": "missionos_autonomy_conversation_response.v1",
                    "message": "fixture Gateway handled the instruction",
                    "operator_instruction": instruction,
                    "routed_action": routed_action,
                    "routing_source": "fixture_gateway",
                    "progress_counted": False,
                    "mission_designer": {
                        "mission_designer_context_ref": "fixture_context",
                        "mission_designer_context_sha256": "fixture",
                        "mission_designer_context_session_id": payload.get("session_id"),
                        "summary": {"validation_status": "fixture_ready"},
                    },
                }
            )
            return
        if parsed.path == "/px4-gazebo/mission-scenarios/start-sitl":
            self._send_json(
                {
                    "summary": {
                        "task_id": payload.get("task_id") or FIXTURE_TASK_ID,
                        "startup_status": "fixture_started",
                        "readiness_status": "fixture_ready",
                    }
                }
            )
            return
        if parsed.path == "/px4-gazebo/mission-scenarios/execute-sitl":
            self._send_json(
                {
                    "summary": {
                        "task_id": payload.get("task_id") or FIXTURE_TASK_ID,
                        "task_status": "completed",
                        "upload_status": "fixture_uploaded",
                        "live_flight_status": "fixture_completed_no_live_flight",
                        "dropoff_verified": False,
                        "delivery_completion_claimed": False,
                        "physical_execution_invoked": False,
                    }
                }
            )
            return
        if parsed.path == "/missionos/runtime-recovery-agent/propose-for-task":
            action = str(payload.get("requested_action") or "operator_review")
            parameters = payload.get("requested_parameters")
            parameters = parameters if isinstance(parameters, dict) else {}
            if action == "adjust_altitude":
                proposed_parameters = {
                    "target_altitude_m": float(
                        parameters.get("target_altitude_m") or 40.0
                    )
                }
            elif action == "avoid_obstacle":
                proposed_parameters = {
                    "target_x_m": 30.0,
                    "target_y_m": 30.0,
                    "target_altitude_m": float(
                        parameters.get("target_altitude_m") or 45.0
                    ),
                }
            elif action == "reroute":
                proposed_parameters = {
                    "target_x_m": float(parameters.get("target_x_m") or 80.0),
                    "target_y_m": float(parameters.get("target_y_m") or 30.0),
                }
            else:
                action = "operator_review"
                proposed_parameters = {}
            self._send_json(
                {
                    "schema_version": (
                        "missionos_runtime_recovery_operator_request_proposal.v1"
                    ),
                    "task_id": payload.get("task_id") or FIXTURE_TASK_ID,
                    "operator_instruction": payload.get("operator_instruction") or "",
                    "requested_action": payload.get("requested_action") or "",
                    "requested_parameters": parameters,
                    "proposal_status": "computed" if proposed_parameters else "insufficient_context",
                    "selected_bounded_action": action,
                    "proposed_parameters": proposed_parameters,
                    "dispatch_authority_created": False,
                    "operator_approval_required": True,
                    "physical_execution_invoked": False,
                    "progress_counted": False,
                    "summary": {
                        "task_id": payload.get("task_id") or FIXTURE_TASK_ID,
                        "proposal_status": (
                            "computed" if proposed_parameters else "insufficient_context"
                        ),
                        "selected_bounded_action": action,
                        "proposed_parameters": proposed_parameters,
                        "dispatch_authority_created": False,
                        "operator_approval_required": True,
                        "physical_execution_invoked": False,
                        "progress_counted": False,
                    },
                }
            )
            return
        if parsed.path == "/px4-gazebo/mission-scenarios/recovery-dispatch":
            self._send_json(
                {
                    "response_status": "fixture_dispatched",
                    "summary": {
                        "dispatch_status": "fixture_dispatched",
                        "recovery_action": payload.get("recovery_action"),
                        "recovery_parameters": payload.get("recovery_parameters") or {},
                        "command_ack_result_name": "FIXTURE_ACCEPTED",
                        "delivery_completion_claimed": False,
                        "physical_execution_invoked": False,
                    },
                }
            )
            return
        self._send_json(
            {"detail": f"unknown route: {parsed.path}"},
            status=HTTPStatus.NOT_FOUND,
        )

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_task_get(self, path: str, query: dict[str, list[str]]) -> None:
        parts = [unquote(part) for part in path.strip("/").split("/")]
        task_id = parts[1] if len(parts) >= 2 else FIXTURE_TASK_ID
        if len(parts) >= 3 and parts[2] == "timeline":
            try:
                limit = int((query.get("limit") or ["8"])[0])
            except ValueError:
                limit = 8
            self._send_json(_fixture_timeline(task_id, limit=limit))
            return
        self._send_json(_fixture_task(task_id))

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_web(*, host: str = "127.0.0.1", port: int = 18791) -> None:
    backend = os.getenv("MISSIONOS_GATEWAY_BACKEND", "fixture").strip().lower()
    if backend in {"production", "backend", "full"}:
        run_production_web(host=host, port=port)
        return
    server = ThreadingHTTPServer((host, port), MissionOSFixtureHandler)
    print(f"MissionOS fixture Gateway listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        time.sleep(0)


def run_production_web(*, host: str = "127.0.0.1", port: int = 18791) -> None:
    try:
        from src.gateway.server import create_gateway
    except Exception as exc:  # pragma: no cover - exercised by runtime smoke.
        raise RuntimeError(
            "MissionOS production Gateway backend could not be imported. "
            "Run `pip install -e .` from the repository root and retry."
        ) from exc
    gateway = create_gateway()
    print(f"MissionOS production Gateway listening on http://{host}:{port}", flush=True)
    gateway.run(host=host, port=port)

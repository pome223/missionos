"""Gateway HTTP client and route-level error contracts for MissionOS CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import click
import httpx


# Live SITL start/dispatch (Gazebo flight) can run for 25+ minutes, well past
# the default request timeout. The client must not abandon an approved dispatch
# while the Gateway still owns it.
SITL_DISPATCH_TIMEOUT = 3600.0

CONVERSATION_ROUTE = "/missionos/autonomy-conversation/run"
RECOVERY_DISPATCH_ROUTE = "/px4-gazebo/mission-scenarios/recovery-dispatch"
RECOVERY_AGENT_PROPOSAL_ROUTE = "/missionos/runtime-recovery-agent/propose-for-task"
TURTLEBOT3_RECOVERY_REVISION_ROUTE = "/missionos/turtlebot3/recovery-agent/revise-for-task"
SITL_START_ROUTE = "/px4-gazebo/mission-scenarios/start-sitl"
SITL_EXECUTION_APPROVAL_ROUTE = "/px4-gazebo/mission-scenarios/approve-sitl-execution"
SITL_EXECUTION_ROUTE = "/px4-gazebo/mission-scenarios/execute-sitl"


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _gateway_host_port(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    return parsed.hostname or "127.0.0.1", parsed.port or 18791


def _gateway_start_command(base_url: str) -> str:
    """Render the `web` invocation whose host/port match this gateway URL."""
    host, port = _gateway_host_port(base_url)
    return f"python -m missionos_gateway web --host {host} --port {port}"


def _gateway_unreachable_message(base_url: str) -> str:
    return (
        f"Could not connect to the Gateway: {base_url}\n"
        f"You can start it from the MissionOS CLI:\n"
        f"  missionos gateway start\n"
        f"  missionos gateway start --enable-live-sitl  # SITL dispatch opt-in\n"
        f"  # raw: {_gateway_start_command(base_url)}\n"
        "For temporary chat sessions, use `missionos chat --autostart`. Add "
        "`--enable-live-sitl` only when the session must reach opt-in live "
        "SITL dispatch."
    )


def _format_http_error_detail(
    method: str,
    path: str,
    status_code: int,
    payload: Any,
) -> str:
    if path == SITL_EXECUTION_ROUTE and status_code == 409 and isinstance(payload, dict):
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        reasons = summary.get("blocked_reasons")
        if not isinstance(reasons, list):
            receipt = payload.get("px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt")
            receipt = receipt if isinstance(receipt, dict) else {}
            reasons = (
                receipt.get("blocked_reasons")
                if isinstance(receipt.get("blocked_reasons"), list)
                else []
            )
        reason_text = ", ".join(str(item) for item in reasons) or "live SITL opt-in missing"
        envelope_advisory = payload.get("envelope_violation_advisory")
        envelope_advisory = envelope_advisory if isinstance(envelope_advisory, dict) else {}
        violations = envelope_advisory.get("violations")
        if isinstance(violations, list) and violations:
            violation_details: list[str] = []
            for item in violations:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("violation_kind") or "contract_envelope_violation")
                requested = item.get("requested_value")
                limit_value = item.get("limit_value")
                unit = str(item.get("unit") or "")
                if requested is not None and limit_value is not None:
                    violation_details.append(
                        f"{kind} (requested={requested}{unit}, max={limit_value}{unit})"
                    )
                else:
                    violation_details.append(kind)
            if violation_details:
                reason_text = "; ".join(violation_details)
            return (
                f"{method} {path} failed: HTTP 409: live SITL blocked by Mission Designer "
                f"contract envelope: {reason_text}. Re-plan within the current envelope "
                "or intentionally update the contract before live execution."
            )
        return (
            f"{method} {path} failed: HTTP 409: live SITL blocked: {reason_text}. "
            "Restart the Gateway with "
            "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION=1 and "
            "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_LIVE_FLIGHT=1, then rerun the tutorial."
        )
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return f"{method} {path} failed: HTTP {status_code}: {detail}"
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        bits: list[str] = []
        for key in ("response_status", "dispatch_status", "recovery_action", "task_status"):
            value = summary.get(key) if summary.get(key) not in (None, "") else payload.get(key)
            if value not in (None, ""):
                bits.append(f"{key}={value}")
        reasons = summary.get("blocked_reasons") or payload.get("blocked_reasons")
        if isinstance(reasons, list) and reasons:
            bits.append("blocked_reasons=" + ", ".join(str(item) for item in reasons))
        if bits:
            return f"{method} {path} failed: HTTP {status_code}: " + "; ".join(bits)
    text = str(payload)
    if len(text) > 300:
        text = text[:300] + "…(truncated)"
    return f"{method} {path} failed: HTTP {status_code}: {text}"


@dataclass
class MissionOSGatewayClient:
    """Small authenticated client for the Gateway routes used by the CLI."""

    base_url: str
    timeout: float = 45.0
    api_key: str | None = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        ok_status_codes: set[int] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.api_key and not any(
            key.lower() in {"x-api-key", "authorization"} for key in headers
        ):
            headers["X-API-Key"] = self.api_key
        if headers:
            kwargs["headers"] = headers
        try:
            with httpx.Client(timeout=timeout if timeout is not None else self.timeout) as client:
                response = client.request(method, _join_url(self.base_url, path), **kwargs)
        except httpx.ConnectError as exc:
            raise click.ClickException(_gateway_unreachable_message(self.base_url)) from exc
        except httpx.TimeoutException as exc:
            raise click.ClickException(
                f"{method} {path} timed out while the Gateway may still be working. "
                "Do not repeat an approved dispatch until the durable task state "
                "has been checked."
            ) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text}
        allowed_statuses = ok_status_codes or set()
        if response.status_code >= 400 and response.status_code not in allowed_statuses:
            raise click.ClickException(
                _format_http_error_detail(method, path, response.status_code, payload)
            )
        if not isinstance(payload, dict):
            return {"payload": payload}
        return payload

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def conversation(
        self,
        instruction: str,
        *,
        session_id: str,
        mission_designer_context: dict[str, Any] | None = None,
        coordinate_route: dict[str, Any] | None = None,
        route_hint: str | None = None,
        client_surface: str | None = None,
        robot_profile: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "operator_instruction": instruction,
            "session_id": session_id,
        }
        if mission_designer_context:
            payload["mission_designer_context"] = mission_designer_context
        if coordinate_route:
            payload["coordinate_route"] = coordinate_route
        if route_hint:
            payload["missionos_route_hint"] = route_hint
        if client_surface:
            payload["missionos_client_surface"] = client_surface
        if robot_profile:
            payload["robot_profile"] = robot_profile
        return self._request("POST", CONVERSATION_ROUTE, json=payload)

    def recovery_dispatch(
        self,
        *,
        task_id: str,
        recovery_action: str,
        recovery_parameters: dict[str, Any] | None = None,
        expected_recovery_checkpoint_id: str = "",
        expected_recovery_checkpoint_hash: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": task_id,
            "recovery_action": recovery_action,
            "recovery_parameters": recovery_parameters or {},
            "explicit_recovery_dispatch_approval": True,
        }
        if expected_recovery_checkpoint_id:
            payload["expected_recovery_checkpoint_id"] = expected_recovery_checkpoint_id
        if expected_recovery_checkpoint_hash:
            payload["expected_recovery_checkpoint_hash"] = expected_recovery_checkpoint_hash
        return self._request(
            "POST",
            RECOVERY_DISPATCH_ROUTE,
            timeout=max(self.timeout, SITL_DISPATCH_TIMEOUT),
            ok_status_codes={409},
            json=payload,
        )

    def recovery_agent_propose_for_task(
        self,
        *,
        task_id: str,
        operator_instruction: str,
        requested_action: str,
        requested_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            RECOVERY_AGENT_PROPOSAL_ROUTE,
            ok_status_codes={409},
            json={
                "task_id": task_id,
                "operator_instruction": operator_instruction,
                "requested_action": requested_action,
                "requested_parameters": requested_parameters or {},
            },
        )

    def turtlebot3_recovery_revision(
        self,
        *,
        task_id: str,
        operator_instruction: str,
        expected_recovery_checkpoint_id: str,
        expected_recovery_checkpoint_hash: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            TURTLEBOT3_RECOVERY_REVISION_ROUTE,
            ok_status_codes={409},
            json={
                "task_id": task_id,
                "operator_instruction": operator_instruction,
                "expected_recovery_checkpoint_id": expected_recovery_checkpoint_id,
                "expected_recovery_checkpoint_hash": expected_recovery_checkpoint_hash,
            },
        )

    def execute_sitl(self, *, task_id: str, live_flight_mode: bool) -> dict[str, Any]:
        approval = self._request(
            "POST",
            SITL_EXECUTION_APPROVAL_ROUTE,
            json={"task_id": task_id, "explicit_execution_approval": True},
        )
        approval_artifact = approval.get("execution_operator_approval")
        approval_artifact = approval_artifact if isinstance(approval_artifact, dict) else {}
        execution_approval_id = str(approval_artifact.get("approval_id") or "").strip()
        if not execution_approval_id:
            raise click.ClickException("Gateway did not return a stored SITL execution approval id")
        return self._request(
            "POST",
            SITL_EXECUTION_ROUTE,
            json={
                "task_id": task_id,
                "execution_approval_id": execution_approval_id,
                "live_flight_mode": live_flight_mode,
            },
            timeout=max(self.timeout, SITL_DISPATCH_TIMEOUT),
        )

    def start_sitl(self, *, task_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            SITL_START_ROUTE,
            json={"task_id": task_id},
            timeout=max(self.timeout, SITL_DISPATCH_TIMEOUT),
        )

"""MissionOS operator CLI backed by the Gateway HTTP and WebSocket routes."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlparse
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time

import click
import httpx
import yaml
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .battery_truth import battery_truth_model
from .route_evidence_image import write_mission_route_evidence_artifacts
from .flight_map_html import (
    _json_for_html_script as _json_for_html_script,
    _mission_map_html as _mission_map_html,
)
from .gateway_client import (
    SITL_DISPATCH_TIMEOUT as SITL_DISPATCH_TIMEOUT,
    SITL_EXECUTION_APPROVAL_ROUTE as SITL_EXECUTION_APPROVAL_ROUTE,
    MissionOSGatewayClient,
    _gateway_host_port,
    _gateway_unreachable_message,
    _join_url,
)
from .gateway_process import (
    GATEWAY_PID_RECORD_SCHEMA_VERSION as GATEWAY_PID_RECORD_SCHEMA_VERSION,
    _apply_gateway_llm_env as _apply_gateway_llm_env,
    _build_gateway_pid_record as _build_gateway_pid_record,
    _dotenv_process_values as _dotenv_process_values,
    _gateway_argv as _gateway_argv,
    _gateway_command_signature as _gateway_command_signature,
    _gateway_pid_record_matches_running_process as _gateway_pid_record_matches_running_process,
    _gateway_process_env as _gateway_process_env,
    _llm_backend_default_adk_enabled as _llm_backend_default_adk_enabled,
    _llm_backend_from_env as _llm_backend_from_env,
    _llm_backend_uses_google_credentials as _llm_backend_uses_google_credentials,
    _process_command as _process_command,
    _process_group_id as _process_group_id,
    _process_running as _process_running,
    _process_start_time as _process_start_time,
    _read_gateway_pid as _read_gateway_pid,
    _read_gateway_pid_record as _read_gateway_pid_record,
    _stop_gateway_pid as _stop_gateway_pid,
)
from .indoor_map_html import (
    _mission_indoor_map_html as _mission_indoor_map_html,
)


SITL_EXECUTION_POLL_INTERVAL = 5.0
SITL_EXECUTION_POLL_TIMELINE_LIMIT = 5
ACTIVE_RUNNER_RECOVERY_OBSERVATION_TIMEOUT_SECONDS = 95.0
TURTLEBOT3_CHAT_TASK_STATUS_POLL_INTERVAL = 1.0
TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "recovered", "blocked", "failed", "cancelled", "canceled"}
)
LIVE_SITL_RESPONSE_WAIT_EXCEEDED_MESSAGE = (
    "Execute Live SITL Gateway response exceeded the client wait window; "
    "showing observed task state."
)
TutorialOutcome = str | None

DEFAULT_GATEWAY_URL = "http://127.0.0.1:18791"
DEFAULT_SESSION_ID = "missionos-cli"
DEFAULT_STATE_PATH = "data/missionos_cli_state.json"
DEFAULT_HISTORY_PATH = "data/missionos_cli_history"
DEFAULT_OPERATE_HISTORY_PATH = "data/missionos_operate_history"
DEFAULT_GATEWAY_PID_PATH = Path("data/missionos_gateway.pid")
DEFAULT_GATEWAY_LOG_PATH = Path("data/missionos_gateway.log")
DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION = (
    "TurtleBot3で屋内配送ルートを走って。障害物を避けて、目的地まで届けて。"
)
DEFAULT_TURTLEBOT4_CHAT_INSTRUCTION = (
    "TurtleBot4で屋内配送ルートを走って。障害物を避けて、目的地まで届けて。"
)
DEFAULT_NOVA_CARTER_CHAT_INSTRUCTION = (
    "Nova CarterでIsaac Sim内の短いNav2ルートを走って。"
    "承認、dispatch、ACK、odom evidenceの境界を保って。"
)
TURTLEBOT3_CHAT_TIMEOUT = 600.0
CHAT_COMPANION_TERMINAL_ROOT = Path("data/missionos_chat_companions")
CHAT_COMPANION_TERMINAL_SURFACES = ("operate", "watch", "map")
CHAT_SLASH_COMMANDS = (
    "/status",
    "/approve",
    "/reject",
    "/revision",
    "/run",
    "/repair",
    "/start-sitl",
    "/execute-sitl",
    "/job-status",
    "/map",
    "/land",
    "/rtl",
    "/review-recovery",
    "/approve-recovery",
    "/climb",
    "/speed",
    "/reroute",
    "/avoid",
    "/avoid-obstacle",
    "/back",
    "/help",
    "/clear",
    "/quit",
)
INTENT_INSTRUCTIONS = {
    "approve": "Approve the current MissionOS plan.",
    "reject": "Reject the current MissionOS plan.",
    "revision": "Revise the current MissionOS plan.",
    "run": "Run the current bounded action through the MissionOS execution gate.",
    "repair": "Diagnose the current MissionOS plan and draft a repair.",
}
INTENT_ROUTE_HINTS = {
    "approve": "approve",
    "reject": "reject",
    "revision": "revision",
    "run": "execute",
    "repair": "repair",
}

# Bundled Mt. Fuji delivery coordinate route used by `missionos tutorial`.
# Same values as docs/mission_os/fuji_delivery_route.yaml, embedded so the
# tutorial does not depend on the current working directory.
FUJI_DELIVERY_ROUTE: dict[str, Any] = {
    "takeoff_latitude": 35.3195,
    "takeoff_longitude": 138.7435,
    "dropoff_latitude": 35.3606,
    "dropoff_longitude": 138.7274,
    "dropoff_roof_height_agl_m": 10,
    "payload_weight_kg": 1,
    "wind_speed_mps": 8,
    "wind_direction_deg": 0,
}
TUTORIAL_PLAN_INSTRUCTION = (
    "Plan the Mt. Fuji delivery. Use the Mt. Fuji coordinate route and prepare "
    "it through payload delivery SITL readiness."
)
DEFAULT_TUTORIAL_SESSION_ID = "missionos-cli-tutorial"

console = Console()


def _status_text(value: Any, default: str = "-") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _safe_get(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def make_client(base_url: str, timeout: float) -> MissionOSGatewayClient:
    dotenv_values = _dotenv_process_values()
    api_key = os.getenv("GATEWAY_API_KEY") or dotenv_values.get("GATEWAY_API_KEY")
    return MissionOSGatewayClient(
        base_url=base_url,
        timeout=timeout,
        api_key=api_key or None,
    )


def _gateway_reachable(client: MissionOSGatewayClient) -> bool:
    """Return True when the gateway answers a health probe."""
    try:
        client.health()
    except (click.ClickException, httpx.HTTPError):
        return False
    return True


def _gateway_health_payload(client: MissionOSGatewayClient) -> dict[str, Any]:
    try:
        payload = client.health()
    except (click.ClickException, httpx.HTTPError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _gateway_is_fixture_backend(client: MissionOSGatewayClient) -> bool:
    payload = _gateway_health_payload(client)
    backend = str(payload.get("session_backend") or payload.get("backend") or "").lower()
    version = str(payload.get("version") or "").lower()
    return backend == "fixture" or "fixture" in version


def _spawn_gateway(
    base_url: str,
    *,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    detached: bool = False,
    enable_live_sitl: bool = False,
) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(
        _gateway_argv(base_url),
        stdout=stdout,
        stderr=stderr,
        env=_gateway_process_env(enable_live_sitl=enable_live_sitl),
        start_new_session=detached,
    )


def _terminate_gateway(proc: "subprocess.Popen[bytes]") -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _ensure_gateway(
    client: MissionOSGatewayClient,
    base_url: str,
    *,
    autostart: bool,
    enable_live_sitl: bool = False,
) -> "subprocess.Popen[bytes] | None":
    """Make sure the gateway is reachable before the chat loop starts.

    Returns a spawned gateway process to terminate on exit, or None when an
    already-running gateway is reused. Raises a friendly ClickException with the
    matching start command when the gateway is down and autostart is disabled.
    """
    if _gateway_reachable(client):
        if enable_live_sitl and _gateway_is_fixture_backend(client):
            raise click.ClickException(
                "A fixture Gateway is already running at this URL. Live SITL "
                "requires the production backend. Run "
                "`missionos gateway restart --enable-live-sitl` and then retry."
            )
        if autostart:
            console.print(
                "[yellow]Gateway is already running. --autostart will reuse the "
                f"existing Gateway: {base_url}[/yellow]"
            )
            if enable_live_sitl:
                console.print(
                    "[yellow]The existing Gateway live SITL environment will not "
                    "be changed. To pick up code or env changes, run "
                    "`missionos gateway restart --enable-live-sitl`."
                    "[/yellow]"
                )
        return None
    if not autostart:
        raise click.ClickException(_gateway_unreachable_message(base_url))
    console.print(f"[blue]Autostarting Gateway ({base_url})...[/blue]")
    if enable_live_sitl:
        console.print(
            "[yellow]Live SITL opt-in: "
            "sitl_dispatch_runtime_enabled=true; "
            "live_hardware_target_allowed=false; "
            "physical_execution_invoked=false; "
            "operator_approval_required=true[/yellow]"
        )
    proc = _spawn_gateway(base_url, enable_live_sitl=enable_live_sitl)
    for _ in range(40):  # up to ~20s for the server to come up
        if proc.poll() is not None:
            raise click.ClickException("Gateway autostart failed; the process exited.")
        if _gateway_reachable(client):
            console.print("[green]Gateway is ready.[/green]")
            return proc
        time.sleep(0.5)
    _terminate_gateway(proc)
    raise click.ClickException("Timed out waiting for the Gateway to start.")


def _start_managed_gateway(
    *,
    client: MissionOSGatewayClient,
    base_url: str,
    pid_path: Path,
    log_path: Path,
    wait: bool,
    enable_live_sitl: bool,
) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    try:
        proc = _spawn_gateway(
            base_url,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            detached=True,
            enable_live_sitl=enable_live_sitl,
        )
    finally:
        log_file.close()
    record = _build_gateway_pid_record(
        pid=proc.pid,
        base_url=base_url,
        enable_live_sitl=enable_live_sitl,
    )
    pid_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    console.print(f"[blue]Started Gateway:[/blue] pid={proc.pid} url={base_url}")
    console.print(f"[blue]Log:[/blue] {log_path}")
    if enable_live_sitl:
        console.print(
            "[yellow]Live SITL opt-in: "
            "sitl_dispatch_runtime_enabled=true; "
            "live_hardware_target_allowed=false; "
            "physical_execution_invoked=false; "
            "operator_approval_required=true[/yellow]"
        )
    else:
        console.print(
            "[blue]Gateway mode:[/blue] planning-only "
            "(live SITL/dispatch env is not set)"
        )
    if not wait:
        return
    for _ in range(40):
        if proc.poll() is not None:
            pid_path.unlink(missing_ok=True)
            raise click.ClickException(
                f"Gateway failed to start. Check the log: {log_path}"
            )
        if _gateway_reachable(client):
            console.print("[green]Gateway health: healthy[/green]")
            return
        time.sleep(0.5)
    _stop_gateway_pid(proc.pid)
    pid_path.unlink(missing_ok=True)
    raise click.ClickException(
        f"Gateway health check timed out. Check the log: {log_path}"
    )


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _mission_designer_payload(payload: dict[str, Any]) -> dict[str, Any]:
    mission_designer = payload.get("mission_designer")
    if isinstance(mission_designer, dict) and mission_designer:
        return mission_designer
    operation_result = payload.get("operation_result")
    if isinstance(operation_result, dict):
        return operation_result
    return {}


def _mission_designer_context_ref(payload: dict[str, Any]) -> dict[str, Any]:
    mission_designer = _mission_designer_payload(payload)
    summary = (
        mission_designer.get("summary")
        if isinstance(mission_designer.get("summary"), dict)
        else {}
    )
    context_ref = mission_designer.get("mission_designer_context_ref") or summary.get(
        "mission_designer_context_ref"
    )
    context_sha256 = mission_designer.get(
        "mission_designer_context_sha256"
    ) or summary.get("mission_designer_context_sha256")
    context_session_id = mission_designer.get(
        "mission_designer_context_session_id"
    ) or summary.get("mission_designer_context_session_id")
    if not context_ref or not context_sha256:
        return {}
    return {
        "mission_designer_context_ref": str(context_ref),
        "mission_designer_context_sha256": str(context_sha256),
        "mission_designer_context_session_id": str(context_session_id or ""),
    }


def _mission_designer_sitl_task_id(payload: dict[str, Any]) -> str:
    mission_designer = _mission_designer_payload(payload)
    summary = (
        mission_designer.get("summary")
        if isinstance(mission_designer.get("summary"), dict)
        else {}
    )
    task_id = (
        summary.get("sitl_execution_task_id")
        or summary.get("turtlebot3_home_mission_task_id")
        or summary.get("task_id")
    )
    if task_id:
        return str(task_id)
    task = mission_designer.get("sitl_execution_task")
    if isinstance(task, dict) and task.get("task_id"):
        return str(task["task_id"])
    turtlebot3_task = mission_designer.get("turtlebot3_home_mission_task")
    if isinstance(turtlebot3_task, dict) and turtlebot3_task.get("task_id"):
        return str(turtlebot3_task["task_id"])
    return ""


def _payload_task_id(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    for key in ("task_id", "sitl_execution_task_id"):
        task_id = summary.get(key)
        if task_id:
            return str(task_id)
    task = payload.get("task")
    if isinstance(task, dict) and task.get("task_id"):
        return str(task["task_id"])
    mission_task_id = _mission_designer_sitl_task_id(payload)
    if mission_task_id:
        return mission_task_id
    return ""


def _stored_mission_designer_context(ctx: click.Context, session_id: str) -> dict[str, Any]:
    state = _load_state(ctx.obj["missionos_state_path"])
    context = state.get("mission_designer_context")
    if not isinstance(context, dict):
        return {}
    context_session_id = str(context.get("mission_designer_context_session_id") or "")
    if context_session_id and context_session_id != session_id:
        return {}
    context_gateway_url = str(state.get("missionos_gateway_url") or "")
    current_gateway_url = str(ctx.obj.get("missionos_gateway_url") or "")
    if context_gateway_url and current_gateway_url and context_gateway_url != current_gateway_url:
        return {}
    return dict(context)


def _remember_mission_designer_context(
    ctx: click.Context,
    payload: dict[str, Any],
    *,
    session_id: str,
) -> None:
    context = _mission_designer_context_ref(payload)
    if not context:
        return
    if not context.get("mission_designer_context_session_id"):
        context["mission_designer_context_session_id"] = session_id
    state = _load_state(ctx.obj["missionos_state_path"])
    state["session_id"] = session_id
    state["missionos_gateway_url"] = str(ctx.obj.get("missionos_gateway_url") or "")
    state["mission_designer_context"] = context
    task_id = _mission_designer_sitl_task_id(payload) or _payload_task_id(payload)
    if task_id:
        state["sitl_execution_task_id"] = task_id
    _save_state(ctx.obj["missionos_state_path"], state)


def _remember_sitl_task_id(ctx: click.Context, task_id: str) -> None:
    if not task_id:
        return
    state = _load_state(ctx.obj["missionos_state_path"])
    state["sitl_execution_task_id"] = task_id
    _save_state(ctx.obj["missionos_state_path"], state)


def _remember_sitl_task_id_from_payload(
    ctx: click.Context,
    payload: dict[str, Any] | None,
    *,
    fallback_task_id: str = "",
) -> str:
    task_id = _payload_task_id(payload) or fallback_task_id
    _remember_sitl_task_id(ctx, task_id)
    return task_id


def _stored_sitl_task_id(ctx: click.Context) -> str:
    state = _load_state(ctx.obj["missionos_state_path"])
    return str(state.get("sitl_execution_task_id") or "")


def _load_json_object(raw: str | None, *, label: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{label} must be a JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{label} must be a JSON object")
    return payload


def _load_coordinate_route_file(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(f"could not read {path}: {exc}") from exc
    if file_path.suffix.lower() in {".yaml", ".yml"}:
        try:
            payload = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise click.ClickException(f"{path} must be a YAML object: {exc}") from exc
        if not isinstance(payload, dict):
            raise click.ClickException(f"{path} must be a YAML object")
        return payload
    return _load_json_object(raw, label=path)


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _print_status(payloads: dict[str, dict[str, Any]], *, base_url: str) -> None:
    table = Table(
        title=f"MissionOS Gateway: {base_url}",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Surface", style="cyan")
    table.add_column("Status")
    table.add_column("Key Detail", no_wrap=True)

    health = payloads.get("health", {})
    table.add_row(
        "Gateway",
        _status_text(health.get("status"), "reachable"),
        _status_text(health.get("session_backend") or health.get("version")),
    )

    form2a = payloads.get("form2a", {})
    table.add_row(
        "Plan",
        _status_text(form2a.get("summary_status")),
        _status_text(form2a.get("selected_response_kind")),
    )

    review = payloads.get("review", {})
    table.add_row(
        "Human Review",
        _status_text(review.get("summary_status")),
        _status_text(_safe_get(review, "human_operator_review", "review_status")),
    )

    action = payloads.get("action", {})
    blocking = _safe_get(action, "authority_boundary", "blocking_reasons")
    table.add_row(
        "Execution",
        _status_text(action.get("summary_status")),
        ", ".join(str(item) for item in blocking or []) or "-",
    )

    repair = payloads.get("repair", {})
    table.add_row(
        "Repair",
        _status_text(repair.get("summary_status")),
        _status_text(_safe_get(repair, "repair_proposal", "repair_target")),
    )
    console.print(table)


def _print_conversation_result(payload: dict[str, Any]) -> None:
    message = _status_text(payload.get("message"), "MissionOS handled the instruction.")
    routed_action = _status_text(payload.get("routed_action"))
    routing_source = _status_text(payload.get("routing_source"))
    progress = payload.get("progress_counted")
    lines = [
        f"[bold]MissionOS[/bold]: {message}",
        f"route={routed_action}; source={routing_source}; progress_counted={progress}",
    ]

    operation = payload.get("operation_result")
    payload_split_plan = payload.get("missionos_payload_split_plan")
    if isinstance(operation, dict):
        summary = operation.get("summary") if isinstance(operation.get("summary"), dict) else {}
        status = (
            summary.get("status")
            or operation.get("summary_status")
            or operation.get("response_status")
        )
        if status:
            lines.append(f"operation_status={status}")
        if not isinstance(payload_split_plan, dict) or not payload_split_plan:
            payload_split_plan = operation.get("missionos_payload_split_plan")
        repair = operation.get("repair_proposal")
        if isinstance(repair, dict):
            target = repair.get("repair_target")
            if target:
                lines.append(f"repair_target={_status_text(target)}")
            instruction = repair.get("proposed_operator_instruction")
            if instruction:
                lines.append(f"repair_instruction={_status_text(instruction)}")
            parameters = repair.get("proposed_parameters")
            if isinstance(parameters, dict) and parameters:
                lines.append(
                    "repair_parameters="
                    + ", ".join(f"{key}={value}" for key, value in parameters.items())
                )
        repair_warnings = operation.get("repair_followup_warnings")
        if isinstance(repair_warnings, list):
            for warning in repair_warnings:
                if warning:
                    lines.append(f"repair_warning={_status_text(warning)}")
    if isinstance(payload_split_plan, dict) and payload_split_plan:
        sorties = payload_split_plan.get("sorties")
        payload_values = [
            sortie.get("payload_weight_kg")
            for sortie in (sorties if isinstance(sorties, list) else [])
            if isinstance(sortie, dict)
        ]
        if payload_values:
            min_payload = min(payload_values)
            max_payload = max(payload_values)
            per_sortie = (
                f"{max_payload}kg"
                if min_payload == max_payload
                else f"{min_payload}-{max_payload}kg"
            )
        else:
            per_sortie = "-"
        lines.append(
            "payload_split="
            f"{_status_text(payload_split_plan.get('plan_status'))}; "
            f"requested_total={payload_split_plan.get('requested_payload_weight_kg')}kg; "
            f"sorties={payload_split_plan.get('sortie_count')}; "
            f"per_sortie={per_sortie}; planning_only=True"
        )

    repair_prompt = payload.get("missionos_repair_prompt")
    if isinstance(repair_prompt, dict) and repair_prompt:
        reasons = repair_prompt.get("blocking_reasons")
        if isinstance(reasons, list) and reasons:
            lines.append(
                "repair_prompt=Mission blocked: "
                + ", ".join(str(reason) for reason in reasons)
            )
        prompt_text = repair_prompt.get("operator_prompt")
        if prompt_text:
            lines.append(_status_text(prompt_text))

    form2a = payload.get("form2a_ai_agent")
    if isinstance(form2a, dict):
        selection = form2a.get("selection") if isinstance(form2a.get("selection"), dict) else {}
        review = form2a.get("review") if isinstance(form2a.get("review"), dict) else {}
        action = form2a.get("action") if isinstance(form2a.get("action"), dict) else {}
        details = [
            f"selection={_status_text(selection.get('summary_status'))}",
            f"review={_status_text(review.get('summary_status'))}",
            f"action={_status_text(action.get('summary_status'))}",
        ]
        selected = selection.get("selected_response_kind")
        if selected:
            details.append(f"selected={selected}")
        lines.append("; ".join(details))

    console.print(Panel("\n".join(lines), title="Conversation", border_style="cyan"))


def _wait_for_active_runner_recovery_observation(
    client: MissionOSGatewayClient,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = ACTIVE_RUNNER_RECOVERY_OBSERVATION_TIMEOUT_SECONDS,
    poll_interval: float = 0.5,
) -> dict[str, Any] | None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if summary.get("active_runner_request_queued") is not True:
        return None
    task_id = _payload_task_id(payload)
    if not task_id:
        return None
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_payload: dict[str, Any] | None = None
    recovery_action = str(summary.get("recovery_action") or "").strip().lower()
    maneuver_actions = {"adjust_altitude", "adjust_speed", "reroute", "avoid_obstacle"}
    expected_parameters = (
        summary.get("recovery_parameters")
        if isinstance(summary.get("recovery_parameters"), dict)
        else {}
    )

    def _parameters_match(observed: Any) -> bool:
        if not expected_parameters:
            return True
        if not isinstance(observed, dict):
            return False
        for key, expected_value in expected_parameters.items():
            if key not in observed:
                return False
            observed_value = observed.get(key)
            if isinstance(expected_value, bool) or isinstance(observed_value, bool):
                if bool(expected_value) != bool(observed_value):
                    return False
                continue
            expected_number = _as_float(expected_value)
            observed_number = _as_float(observed_value)
            if expected_number is not None and observed_number is not None:
                if abs(expected_number - observed_number) > 1e-3:
                    return False
                continue
            if str(expected_value) != str(observed_value):
                return False
        return True

    while time.monotonic() <= deadline:
        try:
            task_payload = client.get(f"/tasks/{quote(task_id, safe='')}")
        except click.ClickException:
            return last_payload
        last_payload = task_payload
        snapshot = _task_artifacts(task_payload).get("missionos_auto_mission_runtime_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        outcome = str(snapshot.get("post_abort_outcome_status") or "")
        if outcome and outcome not in {
            "recovery_outcome_pending",
            "return_observation_pending",
            "landing_observation_pending",
        }:
            return task_payload
        if snapshot.get("operator_recovery_command_ack_observed") is False:
            return task_payload
        request_matches = (
            snapshot.get("operator_recovery_request_observed") is True
            and _parameters_match(snapshot.get("operator_recovery_parameters"))
        )
        if recovery_action in maneuver_actions and request_matches and (
            snapshot.get("operator_recovery_assist_status") is not None
            or snapshot.get("operator_recovery_target_reached") is True
            or snapshot.get("operator_recovery_resume_auto_status") is not None
        ):
            return task_payload
        if request_matches:
            last_payload = task_payload
        time.sleep(max(0.1, poll_interval))
    return last_payload


def _recovery_runner_observation_lines(task_payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(task_payload, dict):
        return []
    snapshot = _task_artifacts(task_payload).get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    if not snapshot:
        return []
    observed = _format_flag(
        snapshot.get("operator_recovery_request_observed"),
        default="pending",
    )
    ack = _operator_recovery_ack_text(
        observed=snapshot.get("operator_recovery_command_ack_observed"),
        result=snapshot.get("operator_recovery_command_ack_result"),
    )
    lines = [
        f"runner_observed={observed}; runner_ack={ack}; "
        f"nav_state={_status_text(snapshot.get('nav_state'))}; "
        f"home={_fmt_metres(snapshot.get('distance_to_home_m'))}"
    ]
    parameters = snapshot.get("operator_recovery_parameters")
    if isinstance(parameters, dict) and parameters:
        lines.append(
            "runner_parameters="
            + ", ".join(f"{key}={value}" for key, value in sorted(parameters.items()))
        )
    if snapshot.get("post_abort_tracking") is True:
        lines.append(
            f"tracking={_status_text(snapshot.get('operator_recovery_path'))}; "
            f"landed={_status_text(snapshot.get('landed'))}; "
            f"arming={_status_text(snapshot.get('arming_state'))}; "
            f"post_abort={_format_duration(snapshot.get('post_abort_elapsed_seconds'))}"
        )
        outcome = snapshot.get("post_abort_outcome_status")
        if outcome:
            lines.append(
                f"outcome={_status_text(outcome)}; "
                f"home_delta={_fmt_metres(snapshot.get('post_abort_home_distance_delta_m'))}; "
                f"alt_delta={_fmt_metres(snapshot.get('post_abort_altitude_delta_m'))}"
            )
    if any(
        snapshot.get(key) is not None
        for key in (
            "operator_recovery_assist_attempted",
            "operator_recovery_assist_status",
            "operator_recovery_target_reached",
            "operator_recovery_resume_auto_status",
        )
    ):
        assist_ack = _operator_recovery_ack_text(
            observed=snapshot.get(
                "operator_recovery_assist_offboard_ack_observed"
            ),
            result=snapshot.get("operator_recovery_assist_offboard_ack_result"),
        )
        lines.append(
            "assist="
            f"{_status_text(snapshot.get('operator_recovery_assist_status'))}; "
            f"kind={_status_text(snapshot.get('operator_recovery_assist_kind'))}; "
            f"offboard_ack={assist_ack}; "
            f"offboard_state={_status_text(snapshot.get('operator_recovery_assist_offboard_state_observed'))}; "
            f"nav={_status_text(snapshot.get('operator_recovery_assist_offboard_nav_state'))}; "
            f"setpoints={_status_text(snapshot.get('operator_recovery_assist_setpoint_frames_sent'))}; "
            f"target={_status_text(snapshot.get('operator_recovery_target_reached'))}; "
            f"resume={_status_text(snapshot.get('operator_recovery_resume_auto_status'))}"
        )
        if (
            snapshot.get(
                "operator_recovery_assist_low_altitude_disarm_ack_observed"
            )
            is not None
        ):
            disarm_ack = _operator_recovery_ack_text(
                observed=snapshot.get(
                    "operator_recovery_assist_low_altitude_disarm_ack_observed"
                ),
                result=snapshot.get(
                    "operator_recovery_assist_low_altitude_disarm_ack_result"
                ),
            )
            lines.append(f"assist_disarm_ack={disarm_ack}")
        if (
            snapshot.get(
                "operator_recovery_assist_low_altitude_force_disarm_ack_observed"
            )
            is not None
        ):
            force_disarm_ack = _operator_recovery_ack_text(
                observed=snapshot.get(
                    "operator_recovery_assist_low_altitude_force_disarm_ack_observed"
                ),
                result=snapshot.get(
                    "operator_recovery_assist_low_altitude_force_disarm_ack_result"
                ),
            )
            lines.append(f"assist_force_disarm_ack={force_disarm_ack}")
    return lines


def _print_recovery_result(
    payload: dict[str, Any],
    *,
    task_payload: dict[str, Any] | None = None,
) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    dispatch_status = summary.get("dispatch_status") or payload.get("response_status")
    ack = summary.get("command_ack_result_name") or "-"
    runner_abort = "observed" if summary.get("runner_abort_observed") is True else "not observed yet"
    blocked = summary.get("blocked_reasons") if isinstance(summary.get("blocked_reasons"), list) else []
    active_runner_queued = summary.get("active_runner_request_queued") is True
    lines = [
        f"dispatch_status={_status_text(dispatch_status)}",
        f"recovery_action={_status_text(summary.get('recovery_action'))}",
        f"ACK={ack}; runner_abort={runner_abort}",
        "delivery/progress/physical claim=false",
    ]
    if "recovery_completion_claimed" in summary:
        lines[2] = (
            "recovery_completion_claimed="
            f"{summary.get('recovery_completion_claimed')}; "
            "route_resumed_after_recovery="
            f"{summary.get('route_resumed_after_recovery')}; "
            "route_completed_after_recovery="
            f"{summary.get('route_completed_after_recovery')}"
        )
    recovery_parameters = summary.get("recovery_parameters")
    if isinstance(recovery_parameters, dict) and recovery_parameters:
        parameter_text = ", ".join(
            f"{key}={value}" for key, value in sorted(recovery_parameters.items())
        )
        lines.insert(2, f"recovery_parameters={parameter_text}")
    if active_runner_queued:
        lines.insert(
            2,
            "active_runner_request=queued; polling runner ACK/effect before this panel",
        )
    lines.extend(_recovery_runner_observation_lines(task_payload))
    if blocked:
        lines.append("blocked_reasons=" + ", ".join(str(item) for item in blocked))
    console.print(Panel("\n".join(lines), title="Runtime Recovery", border_style="yellow"))


def _print_sitl_execution_result(payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    blocked = summary.get("blocked_reasons") if isinstance(summary.get("blocked_reasons"), list) else []
    lines = [
        f"task_id={_status_text(summary.get('task_id'))}",
        f"task_status={_status_text(summary.get('task_status'))}",
        f"upload_status={_status_text(summary.get('upload_status'))}",
        f"live_flight_status={_status_text(summary.get('live_flight_status'))}",
        f"dropoff_verified={summary.get('dropoff_verified')}",
        f"delivery_completion_claimed={summary.get('delivery_completion_claimed')}",
        f"physical_execution_invoked={summary.get('physical_execution_invoked')}",
    ]
    if blocked:
        lines.append("blocked_reasons=" + ", ".join(str(item) for item in blocked))
    console.print(Panel("\n".join(lines), title="Execute Live SITL", border_style="green"))


def _print_sitl_start_result(payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    readiness = payload.get("px4_gazebo_sitl_execution_readiness")
    if not isinstance(readiness, dict):
        readiness = {}
    blocked = (
        readiness.get("blocked_reasons")
        if isinstance(readiness.get("blocked_reasons"), list)
        else []
    )
    lines = [
        f"task_id={_status_text(summary.get('task_id'))}",
        f"startup_status={_status_text(summary.get('startup_status'))}",
        f"container={_status_text(summary.get('container_name'))}",
        f"readiness_status={_status_text(summary.get('readiness_status') or readiness.get('readiness_status'))}",
        f"mavlink_endpoint_observed={readiness.get('mavlink_endpoint_observed')}",
        "mission_upload_performed=false",
        "live_flight_runner_invoked=false",
    ]
    if blocked:
        lines.append("blocked_reasons=" + ", ".join(str(item) for item in blocked))
    console.print(Panel("\n".join(lines), title="Start SITL", border_style="blue"))


def _task_artifacts(task_payload: dict[str, Any]) -> dict[str, Any]:
    artifacts = task_payload.get("artifacts")
    if isinstance(artifacts, dict):
        return _artifacts_with_latest_runtime_snapshot(artifacts)
    task = task_payload.get("task")
    if isinstance(task, dict) and isinstance(task.get("artifacts"), dict):
        return _artifacts_with_latest_runtime_snapshot(task["artifacts"])
    return {}


def _artifacts_with_latest_runtime_snapshot(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    if not isinstance(snapshot, dict):
        return artifacts
    latest = _runtime_snapshot_with_latest_file(snapshot)
    if latest is snapshot:
        return artifacts
    updated = dict(artifacts)
    updated["missionos_auto_mission_runtime_snapshot"] = latest
    return updated


def _runtime_snapshot_with_latest_file(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot_path = snapshot.get("running_snapshot_path")
    if not isinstance(snapshot_path, str) or not snapshot_path:
        return snapshot
    path = Path(snapshot_path)
    if path.name != "running_snapshot.json" or not path.exists():
        return snapshot
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return snapshot
    if not isinstance(payload, dict):
        return snapshot
    current_index = _as_float(snapshot.get("sample_index"))
    latest_index = _as_float(payload.get("sample_index"))
    if latest_index is None:
        return snapshot
    if current_index is not None and latest_index < current_index:
        return snapshot
    latest = {**snapshot, **payload}
    latest.setdefault("schema_version", snapshot.get("schema_version"))
    latest["running_snapshot_path"] = snapshot_path
    return latest


def _task_record(task_payload: dict[str, Any]) -> dict[str, Any]:
    task = task_payload.get("task")
    if isinstance(task, dict):
        return task
    return task_payload


def _task_status(task_payload: dict[str, Any]) -> str:
    task = _task_record(task_payload)
    return str(task.get("status") or task.get("task_status") or "")


def _as_float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return None


def _format_duration(seconds: Any) -> str:
    value = _as_float(seconds)
    if value is None:
        return "-"
    total = max(0, int(round(value)))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_distance(meters: Any) -> str:
    value = _as_float(meters)
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return f"{value / 1000:.2f} km"
    return f"{value:.0f} m"


def _format_percent(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.1f}%"


def _battery_display_text(
    *,
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
    diagnostics: bool = True,
) -> str:
    model = battery_truth_model(snapshot=snapshot, artifacts=artifacts)
    text = _format_percent(model.get("display_percent"))
    if text == "-" or not diagnostics:
        return text
    if model.get("status") == "suspect_reset":
        return (
            f"{text} trusted (reported "
            f"{_format_percent(model.get('reported_percent'))}; reset rejected)"
        )
    if model.get("status") == "sample_rejected":
        return f"{text} trusted (latest sample rejected)"
    if model.get("status") == "source_missing":
        return f"{text} (source unverified)"
    return text


def _first_numeric(*values: Any) -> float | None:
    for value in values:
        number = _as_float(value)
        if number is not None:
            return number
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _format_flag(value: Any, *, default: str = "-") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _job_route_distance_m(artifacts: dict[str, Any]) -> float | None:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    route_plan = artifacts.get("digital_twin_route_plan")
    route_plan = route_plan if isinstance(route_plan, dict) else {}
    compilation = artifacts.get("missionos_auto_mission_compilation")
    compilation = compilation if isinstance(compilation, dict) else {}
    return _first_numeric(
        route.get("derived_route_distance_m"),
        route_plan.get("planned_route_distance_m"),
        route_plan.get("requested_distance_m"),
        compilation.get("planned_route_m"),
    )


def _format_mps(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.1f}m/s"


def _format_degrees(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.0f}deg"


def _format_temperature_c(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.1f}C"


def _format_hpa(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.0f}hPa"


def _format_mm_per_hour(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return "-"
    return f"{number:.1f}mm/h"


def _job_weather_condition_text(artifacts: dict[str, Any]) -> str | None:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    keys = (
        "wind_speed_mps",
        "wind_direction_deg",
        "wind_gust_mps",
        "wind_variance",
        "temperature_c",
        "pressure_hpa",
        "precipitation_mm_per_hour",
    )
    if not any(route.get(key) not in (None, "") for key in keys):
        return None
    return (
        "Weather: "
        f"wind={_format_mps(route.get('wind_speed_mps'))}; "
        f"dir={_format_degrees(route.get('wind_direction_deg'))}; "
        f"gust={_format_mps(route.get('wind_gust_mps'))}; "
        f"variance={_status_text(route.get('wind_variance'))}; "
        f"temp={_format_temperature_c(route.get('temperature_c'))}; "
        f"pressure={_format_hpa(route.get('pressure_hpa'))}; "
        f"rain={_format_mm_per_hour(route.get('precipitation_mm_per_hour'))}"
    )


def _job_weather_compact_text(artifacts: dict[str, Any]) -> str | None:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    if not any(
        route.get(key) not in (None, "")
        for key in (
            "wind_speed_mps",
            "wind_gust_mps",
            "temperature_c",
            "precipitation_mm_per_hour",
        )
    ):
        return None
    return (
        f"weather wind={_format_mps(route.get('wind_speed_mps'))} "
        f"gust={_format_mps(route.get('wind_gust_mps'))} "
        f"temp={_format_temperature_c(route.get('temperature_c'))} "
        f"rain={_format_mm_per_hour(route.get('precipitation_mm_per_hour'))}"
    )


def _job_realism_condition_text(artifacts: dict[str, Any]) -> str | None:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    thermal_app = artifacts.get(
        "missionos_auto_thermal_weather_simulator_condition_application"
    )
    if not isinstance(thermal_app, dict):
        thermal_app = artifacts.get("thermal_weather_simulator_condition_application")
    thermal_app = thermal_app if isinstance(thermal_app, dict) else {}
    thermal_evidence = artifacts.get("missionos_auto_observed_thermal_weather_evidence")
    if not isinstance(thermal_evidence, dict):
        thermal_evidence = artifacts.get("observed_thermal_weather_evidence")
    thermal_evidence = thermal_evidence if isinstance(thermal_evidence, dict) else {}
    rain_app = artifacts.get(
        "missionos_auto_rain_weather_simulator_condition_application"
    )
    if not isinstance(rain_app, dict):
        rain_app = artifacts.get("rain_weather_simulator_condition_application")
    rain_app = rain_app if isinstance(rain_app, dict) else {}
    rain_evidence = artifacts.get("missionos_auto_observed_rain_weather_evidence")
    if not isinstance(rain_evidence, dict):
        rain_evidence = artifacts.get("observed_rain_weather_evidence")
    rain_evidence = rain_evidence if isinstance(rain_evidence, dict) else {}
    wind_app = artifacts.get("missionos_auto_simulator_condition_application")
    if not isinstance(wind_app, dict):
        wind_app = artifacts.get("simulator_condition_application")
    wind_app = wind_app if isinstance(wind_app, dict) else {}
    wind_evidence = artifacts.get("missionos_auto_observed_environment_evidence")
    if not isinstance(wind_evidence, dict):
        wind_evidence = artifacts.get("observed_environment_evidence")
    wind_evidence = wind_evidence if isinstance(wind_evidence, dict) else {}
    runtime_snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    runtime_snapshot = runtime_snapshot if isinstance(runtime_snapshot, dict) else {}

    thermal_requested = any(
        route.get(key) not in (None, "")
        for key in (
            "temperature_c",
            "thermal_battery_drain_factor",
            "thermal_motor_derate_factor",
        )
    )
    wind_requested = any(
        route.get(key) not in (None, "")
        for key in ("wind_speed_mps", "wind_gust_mps", "wind_variance")
    )
    gust_requested = route.get("wind_gust_mps") not in (None, "")
    rain_requested = any(
        route.get(key) not in (None, "")
        for key in (
            "precipitation_mm_per_hour",
            "rain_visual_mode",
            "rain_battery_drain_factor",
            "rain_sensor_degradation_factor",
            "rain_landing_risk_factor",
        )
    )
    auto_dispatch = any(
        key in artifacts
        for key in (
            "missionos_auto_mission_gui_dispatch_running_receipt",
            "missionos_auto_mission_gui_dispatch_receipt",
            "missionos_auto_mission_runtime_snapshot",
        )
    )
    if not thermal_requested and not wind_requested and not rain_requested:
        return None

    app_status = _status_text(
        thermal_app.get("application_status"),
        default="pending" if thermal_requested else "not_requested",
    )
    observation_status = _status_text(
        thermal_evidence.get("observation_status"),
        default="pending" if thermal_requested else "not_requested",
    )
    applied = thermal_app.get("applied")
    applied = applied if isinstance(applied, dict) else {}
    parts = [
        f"thermal={app_status}",
        f"thermal_observed={observation_status}",
    ]
    if applied:
        parts.extend(
            [
                f"battery_factor={_status_text(applied.get('thermal_battery_drain_factor'))}",
                f"motor_derate={_status_text(applied.get('thermal_motor_derate_factor'))}",
                f"sim_bat_drain={_status_text(applied.get('effective_sim_bat_drain_seconds'))}s",
            ]
        )
    if rain_requested:
        rain_status = _status_text(
            rain_app.get("application_status"),
            default="pending",
        )
        rain_observation_status = _status_text(
            rain_evidence.get("observation_status"),
            default="pending",
        )
        rain_applied = rain_app.get("applied")
        rain_applied = rain_applied if isinstance(rain_applied, dict) else {}
        parts.extend(
            [
                f"rain={rain_status}",
                f"rain_observed={rain_observation_status}",
            ]
        )
        if rain_applied:
            parts.extend(
                [
                    f"rain_battery_factor={_status_text(rain_applied.get('rain_battery_drain_factor'))}",
                    f"rain_sensor_factor={_status_text(rain_applied.get('rain_sensor_degradation_factor'))}",
                    f"rain_landing_factor={_status_text(rain_applied.get('rain_landing_risk_factor'))}",
                ]
            )
    if wind_requested:
        wind_snapshot_default = "pending"
        if runtime_snapshot.get("wind_mean_pending_reason"):
            wind_snapshot_default = str(runtime_snapshot.get("wind_mean_pending_reason"))
        elif runtime_snapshot.get("wind_mean_started"):
            wind_snapshot_default = "wind_topic_publish_observed"
        elif runtime_snapshot.get("wind_gust_window_start_seconds") is not None:
            wind_snapshot_default = "materialized_gz_wind_window"
        wind_status = _status_text(
            wind_app.get("application_status"),
            default=wind_snapshot_default if auto_dispatch else "pending",
        )
        wind_observation = _status_text(
            wind_evidence.get("observation_status"),
            default=(
                str(runtime_snapshot.get("wind_mean_pending_reason"))
                if runtime_snapshot.get("wind_mean_pending_reason")
                else (
                    "wind_gust_window_running"
                    if runtime_snapshot.get("wind_gust_started")
                    else (
                        "wind_topic_publish_observed"
                        if runtime_snapshot.get("wind_mean_started")
                        else ("pending" if auto_dispatch else "pending")
                    )
                )
            ),
        )
        wind_physics = (
            "materialized_gz_wind"
            if wind_status == "applied_with_approximations"
            else wind_status
        )
        parts.append(f"wind_physics={wind_physics}")
        parts.append(f"wind_observed={wind_observation}")
        if runtime_snapshot.get("wind_mean_pending_reason"):
            parts.append(
                f"wind_pending={_status_text(runtime_snapshot.get('wind_mean_pending_reason'))}"
            )
    if gust_requested:
        gust_physics = (
            "materialized_gz_wind_window"
            if wind_status == "applied_with_approximations"
            else wind_status
        )
        parts.append(
            "gust_physics="
            + gust_physics
        )
        parts.append(f"gust_observed={wind_observation}")
    return "Realism: " + "; ".join(parts)


def _auto_process_status_text(
    *,
    artifacts: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> str | None:
    metadata = metadata if isinstance(metadata, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    receipt = artifacts.get("missionos_auto_mission_gui_dispatch_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    running_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_running_receipt")
    running_receipt = running_receipt if isinstance(running_receipt, dict) else {}
    failed_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_failed_receipt")
    failed_receipt = failed_receipt if isinstance(failed_receipt, dict) else {}
    process_status = _first_present(
        receipt.get("auto_mission_process_status"),
        metadata.get("missionos_auto_mission_process_status"),
        failed_receipt.get("auto_mission_process_status"),
    )
    terminal_gates = _first_present(
        receipt.get("auto_mission_terminal_gates_passed"),
        metadata.get("missionos_auto_mission_terminal_gates_passed"),
    )
    if process_status is None and terminal_gates is None:
        return None
    parts = [f"auto_mission={_status_text(process_status)}"]
    if terminal_gates is not None:
        parts.append(f"terminal_gates={_format_flag(terminal_gates, default='pending')}")
    dispatch_status = _first_present(
        receipt.get("dispatch_status"),
        metadata.get("missionos_auto_mission_gui_dispatch_status"),
        running_receipt.get("dispatch_status"),
    )
    if dispatch_status is not None:
        parts.append(f"dispatch={_status_text(dispatch_status)}")
    monitor_stop = _status_text(snapshot.get("monitor_stop_reason"))
    if monitor_stop != "-":
        parts.append(f"stop={monitor_stop}")
    return "Process: " + "; ".join(parts)


def _progress_bar(percent: float | None, *, width: int = 28) -> str:
    if percent is None:
        return "[" + "-" * width + "]"
    clamped = min(100.0, max(0.0, percent))
    filled = int(round(width * clamped / 100.0))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _job_progress_percent(
    *,
    progress_m: float | None,
    route_distance_m: float | None,
    reached_seq: int | None,
    waypoint_total: int | None,
) -> float | None:
    if progress_m is not None and route_distance_m and route_distance_m > 0:
        return min(100.0, max(0.0, progress_m / route_distance_m * 100.0))
    if reached_seq is not None and waypoint_total and waypoint_total > 0:
        return min(100.0, max(0.0, reached_seq / waypoint_total * 100.0))
    return None


def _job_eta_seconds(
    *,
    elapsed_seconds: float | None,
    progress_m: float | None,
    route_distance_m: float | None,
    monitor_seconds: float | None,
) -> float | None:
    if (
        elapsed_seconds is not None
        and progress_m is not None
        and progress_m > 0
        and route_distance_m is not None
        and route_distance_m > progress_m
    ):
        return elapsed_seconds / progress_m * (route_distance_m - progress_m)
    if monitor_seconds is not None and elapsed_seconds is not None:
        return max(0.0, monitor_seconds - elapsed_seconds)
    return None


def _runtime_recovery_agent_action(artifacts: dict[str, Any]) -> Any:
    agent_bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    agent_bridge = agent_bridge if isinstance(agent_bridge, dict) else {}
    agent_result = agent_bridge.get("runtime_recovery_agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    agent_assessment = agent_result.get("assessment")
    agent_assessment = agent_assessment if isinstance(agent_assessment, dict) else {}
    return _first_present(
        agent_assessment.get("selected_bounded_action"),
        agent_assessment.get("recommended_action"),
        agent_assessment.get("recovery_action"),
    )


def _runtime_recovery_agent_parameters(artifacts: dict[str, Any]) -> dict[str, Any]:
    agent_bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    agent_bridge = agent_bridge if isinstance(agent_bridge, dict) else {}
    agent_result = agent_bridge.get("runtime_recovery_agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    agent_assessment = agent_result.get("assessment")
    agent_assessment = agent_assessment if isinstance(agent_assessment, dict) else {}
    parameters = agent_assessment.get("proposed_parameters")
    return dict(parameters) if isinstance(parameters, dict) else {}


def _runtime_recovery_effective_status(
    agent_result: dict[str, Any],
    agent_bridge: dict[str, Any],
    agent_assessment: dict[str, Any],
) -> str:
    status = _status_text(agent_result.get("runtime_status") or agent_bridge.get("bridge_status"))
    assessment_status = _status_text(agent_assessment.get("assessment_status"), "")
    action = _first_present(
        agent_assessment.get("selected_bounded_action"),
        agent_assessment.get("recommended_action"),
        agent_assessment.get("recovery_action"),
    )
    if assessment_status == "proposal_guardrail_passed" and action:
        return assessment_status
    return status


def _operator_recovery_dispatch_command(action: Any) -> tuple[str, str, str] | None:
    normalized = str(action or "").strip().lower().replace("-", "_")
    if normalized in {"return_to_launch", "return_to_home", "return_home", "rtl"}:
        return ("RTL", "/rtl", "return_to_launch")
    if normalized == "land":
        return ("LAND", "/land", "land")
    return None


def _recovery_parameter_text(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return str(value)
    if math.isfinite(number) and abs(number - round(number)) < 1e-6:
        return str(int(round(number)))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _operator_recovery_cli_command(
    *,
    task_id: str,
    action: Any,
    parameters: dict[str, Any] | None = None,
) -> str | None:
    normalized = str(action or "").strip().lower().replace("-", "_")
    params = parameters if isinstance(parameters, dict) else {}
    if normalized == "adjust_altitude":
        altitude = params.get("target_altitude_m")
        value = _recovery_parameter_text(altitude) if altitude is not None else "<m>"
        return f"missionos climb --task-id {task_id} --altitude-m {value}"
    if normalized == "adjust_speed":
        speed = params.get("target_speed_mps")
        value = _recovery_parameter_text(speed) if speed is not None else "<m/s>"
        return f"missionos speed --task-id {task_id} --speed-mps {value}"
    if normalized in {"reroute", "avoid_obstacle"}:
        x_value = params.get("target_x_m")
        y_value = params.get("target_y_m")
        x_text = _recovery_parameter_text(x_value) if x_value is not None else "<north_m>"
        y_text = _recovery_parameter_text(y_value) if y_value is not None else "<east_m>"
        command = "avoid-obstacle" if normalized == "avoid_obstacle" else "reroute"
        parts = [
            "missionos",
            command,
            "--task-id",
            task_id,
            "--target-x-m",
            x_text,
            "--target-y-m",
            y_text,
        ]
        altitude = params.get("target_altitude_m")
        if altitude is not None:
            parts.extend(["--altitude-m", _recovery_parameter_text(altitude)])
        return " ".join(parts)
    return None


def _operator_recovery_console_command(
    action: Any,
    parameters: dict[str, Any] | None = None,
) -> str | None:
    normalized = str(action or "").strip().lower().replace("-", "_")
    params = parameters if isinstance(parameters, dict) else {}
    if normalized == "adjust_altitude" and params.get("target_altitude_m") is not None:
        return f"climb {_recovery_parameter_text(params['target_altitude_m'])}"
    if normalized == "adjust_speed" and params.get("target_speed_mps") is not None:
        return f"speed {_recovery_parameter_text(params['target_speed_mps'])}"
    if normalized in {"reroute", "avoid_obstacle"}:
        x_value = params.get("target_x_m")
        y_value = params.get("target_y_m")
        if x_value is None or y_value is None:
            return None
        command = "avoid" if normalized == "avoid_obstacle" else "reroute"
        parts = [
            command,
            _recovery_parameter_text(x_value),
            _recovery_parameter_text(y_value),
        ]
        if params.get("target_altitude_m") is not None:
            parts.append(_recovery_parameter_text(params["target_altitude_m"]))
        return " ".join(parts)
    return None


def _operator_recovery_dispatch_hint(
    *,
    task_id: Any,
    action: Any,
    parameters: dict[str, Any] | None = None,
    compact: bool = False,
) -> str | None:
    task_text = _status_text(task_id)
    if task_text == "-":
        return None
    normalized = str(action or "").strip().lower().replace("-", "_")
    parameterized_command = _operator_recovery_cli_command(
        task_id=task_text,
        action=normalized,
        parameters=parameters,
    )
    if parameterized_command:
        if compact:
            return f"operator_action={parameterized_command}"
        return (
            "Operator action available: "
            f"[bold]{parameterized_command}[/bold]; "
            "Gateway validates approval, parameters, and active-runner support."
        )
    command = _operator_recovery_dispatch_command(action)
    if command is None:
        return None
    label, chat_command, recovery_action = command
    chat_text = f"{chat_command} {task_text}"
    if compact:
        return f"operator_action={chat_text}"
    return (
        f"Operator Recovery: {label} can be operator-approved via {chat_text} "
        f"(chat) or missionos recover --task-id {task_text} --action "
        f"{recovery_action}; Gateway validates the live allowlist."
    )


def _operator_recovery_ack_text(*, observed: Any, result: Any) -> str:
    if observed is True:
        if str(result) in {"0", "ACCEPTED", "MAV_RESULT_ACCEPTED"}:
            return "accepted"
        return f"result={_status_text(result)}"
    if observed is False:
        return "not_observed"
    return "pending"


def _operator_recovery_maneuver_evidence_snapshot(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    probe = artifacts.get("missionos_auto_mission_probe_observed")
    probe = probe if isinstance(probe, dict) else {}
    monitor = probe.get("monitor")
    monitor = monitor if isinstance(monitor, dict) else {}
    terminal = monitor.get("terminal_snapshot")
    terminal = terminal if isinstance(terminal, dict) else {}
    action = str(terminal.get("operator_recovery_action") or "").strip().lower()
    if action not in {"adjust_altitude", "adjust_speed", "reroute", "avoid_obstacle"}:
        return {}
    if not any(
        terminal.get(key) is not None
        for key in (
            "operator_recovery_assist_status",
            "operator_recovery_target_reached",
            "operator_recovery_resume_auto_status",
        )
    ):
        return {}
    return terminal


def _operator_recovery_assist_status_text(snapshot: dict[str, Any]) -> str:
    if not any(
        snapshot.get(key) is not None
        for key in (
            "operator_recovery_assist_status",
            "operator_recovery_target_reached",
            "operator_recovery_resume_auto_status",
        )
    ):
        return ""
    return (
        f"assist={_status_text(snapshot.get('operator_recovery_assist_status'))}; "
        f"target={_status_text(snapshot.get('operator_recovery_target_reached'))}; "
        f"resume={_status_text(snapshot.get('operator_recovery_resume_auto_status'))}"
    )


def _operator_recovery_dispatch_status_text(
    *,
    artifacts: dict[str, Any],
    snapshot: dict[str, Any],
    compact: bool = False,
) -> str | None:
    receipt = artifacts.get("missionos_runtime_recovery_dispatch_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    if not receipt and not snapshot.get("operator_recovery_request_observed"):
        return None
    status = _status_text(receipt.get("dispatch_status"))
    action = _status_text(
        receipt.get("recovery_action") or snapshot.get("operator_recovery_action")
    )
    parameters = (
        receipt.get("recovery_parameters")
        or snapshot.get("operator_recovery_parameters")
        or {}
    )
    parameter_text = ""
    if isinstance(parameters, dict) and parameters:
        parameter_text = "; params=" + ",".join(
            f"{key}={value}" for key, value in sorted(parameters.items())
        )
    active_runner = (
        "queued"
        if receipt.get("active_runner_request_queued") is True
        else "not_queued"
        if receipt
        else "observed"
    )
    runner_observed = _format_flag(
        snapshot.get("operator_recovery_request_observed"),
        default="pending",
    )
    ack = _operator_recovery_ack_text(
        observed=snapshot.get("operator_recovery_command_ack_observed"),
        result=snapshot.get("operator_recovery_command_ack_result"),
    )
    tracking_text = ""
    if snapshot.get("post_abort_tracking") is True:
        tracking_text = (
            f"; tracking={_status_text(snapshot.get('operator_recovery_path'))}"
            f"; landed={_status_text(snapshot.get('landed'))}"
            f"; arming={_status_text(snapshot.get('arming_state'))}"
        )
        outcome = snapshot.get("post_abort_outcome_status")
        if outcome:
            tracking_text += f"; outcome={_status_text(outcome)}"
        assist_status = snapshot.get("operator_recovery_assist_status")
        if assist_status:
            tracking_text += f"; assist={_status_text(assist_status)}"
            disarm_ack = _operator_recovery_ack_text(
                observed=snapshot.get(
                    "operator_recovery_assist_low_altitude_disarm_ack_observed"
                ),
                result=snapshot.get(
                    "operator_recovery_assist_low_altitude_disarm_ack_result"
                ),
            )
            if disarm_ack != "-":
                tracking_text += f"; assist_disarm={disarm_ack}"
            force_disarm_ack = _operator_recovery_ack_text(
                observed=snapshot.get(
                    "operator_recovery_assist_low_altitude_force_disarm_ack_observed"
                ),
                result=snapshot.get(
                    "operator_recovery_assist_low_altitude_force_disarm_ack_result"
                ),
            )
            if force_disarm_ack != "-":
                tracking_text += f"; assist_force_disarm={force_disarm_ack}"
    maneuver_snapshot = _operator_recovery_maneuver_evidence_snapshot(artifacts)
    maneuver_text = ""
    if maneuver_snapshot and not snapshot.get("operator_recovery_assist_status"):
        maneuver_assist = _operator_recovery_assist_status_text(maneuver_snapshot)
        if maneuver_assist:
            maneuver_text = (
                f"; maneuver={_status_text(maneuver_snapshot.get('operator_recovery_action'))}; "
                f"{maneuver_assist}"
            )
    if compact:
        return (
            f"operator_dispatch={status}; action={action}; "
            f"active_runner={active_runner}; runner_observed={runner_observed}; "
            f"ack={ack}{parameter_text}{tracking_text}{maneuver_text}"
        )
    return (
        "Operator Dispatch: "
        f"status={status}; action={action}; active_runner={active_runner}; "
        f"runner_observed={runner_observed}; ack={ack}{parameter_text}{tracking_text}{maneuver_text}"
    )


def _job_operator_summary(task_payload: dict[str, Any]) -> list[str]:
    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    dispatch_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_receipt")
    dispatch_receipt = dispatch_receipt if isinstance(dispatch_receipt, dict) else {}
    running_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_running_receipt")
    running_receipt = running_receipt if isinstance(running_receipt, dict) else {}
    failed_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_failed_receipt")
    failed_receipt = failed_receipt if isinstance(failed_receipt, dict) else {}
    replay = artifacts.get("missionos_auto_mission_runtime_replay")
    replay = replay if isinstance(replay, dict) else {}
    dropoff_gate = artifacts.get("missionos_auto_mission_dropoff_gate_summary")
    dropoff_gate = dropoff_gate if isinstance(dropoff_gate, dict) else {}
    sitl_delivery_gate = artifacts.get("missionos_auto_mission_sitl_delivery_gate_summary")
    sitl_delivery_gate = sitl_delivery_gate if isinstance(sitl_delivery_gate, dict) else {}
    runtime_summary = artifacts.get("missionos_auto_mission_runtime_monitor_summary")
    runtime_summary = runtime_summary if isinstance(runtime_summary, dict) else {}
    agent_bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    agent_bridge = agent_bridge if isinstance(agent_bridge, dict) else {}
    agent_result = agent_bridge.get("runtime_recovery_agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    agent_assessment = agent_result.get("assessment")
    agent_assessment = agent_assessment if isinstance(agent_assessment, dict) else {}
    agent_telemetry = agent_bridge.get("telemetry_snapshot")
    agent_telemetry = agent_telemetry if isinstance(agent_telemetry, dict) else {}
    startup = artifacts.get("px4_gazebo_mission_designer_sitl_startup")
    startup = startup if isinstance(startup, dict) else {}
    readiness = startup.get("readiness") if isinstance(startup.get("readiness"), dict) else {}
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}

    task_status = _status_text(task.get("status") or task.get("task_status"))
    dispatch_status = (
        failed_receipt.get("dispatch_status")
        or dispatch_receipt.get("dispatch_status")
        or metadata.get("missionos_auto_mission_gui_dispatch_status")
        or running_receipt.get("dispatch_status")
        or "-"
    )
    progress_m = _as_float(snapshot.get("progress_m"))
    route_distance_m = _job_route_distance_m(artifacts)
    elapsed_seconds = _as_float(snapshot.get("elapsed_seconds"))
    monitor_seconds = _first_numeric(
        dispatch_receipt.get("monitor_seconds"),
        metadata.get("missionos_auto_mission_monitor_seconds"),
        running_receipt.get("monitor_seconds"),
    )
    reached_seq = _as_int(snapshot.get("mission_reached_seq"))
    current_seq = _as_int(snapshot.get("mission_current_seq"))
    waypoint_total = _as_int(snapshot.get("waypoint_total"))
    progress_percent = _job_progress_percent(
        progress_m=progress_m,
        route_distance_m=route_distance_m,
        reached_seq=reached_seq,
        waypoint_total=waypoint_total,
    )
    eta_seconds = _job_eta_seconds(
        elapsed_seconds=elapsed_seconds,
        progress_m=progress_m,
        route_distance_m=route_distance_m,
        monitor_seconds=monitor_seconds,
    )
    progress_text = (
        f"{_format_distance(progress_m)} / {_format_distance(route_distance_m)}"
        if route_distance_m is not None
        else _format_distance(progress_m)
    )
    waypoint_text = (
        f"{_status_text(reached_seq)}/{_status_text(waypoint_total)} reached"
        if waypoint_total is not None
        else _status_text(reached_seq)
    )
    current_text = f"current seq {_status_text(current_seq)}" if current_seq is not None else "-"
    battery_text = _battery_display_text(snapshot=snapshot, artifacts=artifacts)
    terrain_clearance_m = _as_float(snapshot.get("terrain_clearance_m"))
    terrain_clearance_target_m = _as_float(snapshot.get("terrain_clearance_target_m"))
    terrain_clearance_margin_m = _as_float(snapshot.get("terrain_clearance_margin_m"))
    terrain_clearance_status = _status_text(snapshot.get("terrain_clearance_status"))
    monitor_stop = _status_text(snapshot.get("monitor_stop_reason"))
    readiness_text = _status_text(readiness.get("readiness_status"))
    missionos_fixture = metadata.get("missionos_fixture") is True
    actual_sitl_evidence = _first_present(
        metadata.get("actual_sitl_flight_evidence_observed"),
        replay.get("actual_sitl_flight_evidence_observed"),
    )
    dropoff_verified = _first_present(
        metadata.get("dropoff_verified"),
        sitl_delivery_gate.get("dropoff_verified"),
        replay.get("dropoff_verified"),
        dropoff_gate.get("dropoff_verified"),
    )
    sitl_delivery = _first_present(
        metadata.get("sitl_delivery_claimed"),
        sitl_delivery_gate.get("sitl_delivery_claimed"),
        replay.get("sitl_delivery_claimed"),
    )
    delivery_completion = _first_present(
        snapshot.get("delivery_completion_claimed"),
        dispatch_receipt.get("delivery_completion_claimed"),
        metadata.get("delivery_completion_claimed"),
    )
    physical_execution = _first_present(
        snapshot.get("physical_execution_invoked"),
        metadata.get("physical_execution_invoked"),
    )
    recovery_snapshot = runtime_summary.get("recovery_agent_telemetry_snapshot")
    recovery_snapshot = recovery_snapshot if isinstance(recovery_snapshot, dict) else {}
    recovery_detail = recovery_snapshot.get("recovery")
    recovery_detail = recovery_detail if isinstance(recovery_detail, dict) else {}
    recovery_action = _first_present(
        recovery_detail.get("action"),
        runtime_summary.get("recovery_path_taken"),
    )
    recovery_ack = _first_present(
        recovery_detail.get("command_ack_observed"),
        runtime_summary.get("recovery_command_ack_observed"),
    )
    recovery_return_progress = _first_numeric(
        recovery_detail.get("recovery_return_progress_m"),
        runtime_summary.get("recovery_return_progress_m"),
    )
    recovery_final_landing_safe = _first_present(
        recovery_detail.get("final_landing_safe"),
        runtime_summary.get("final_landing_safe"),
    )
    recovery_observation_lost = _first_present(
        recovery_detail.get("observation_lost"),
        runtime_summary.get("recovery_observation_lost"),
    )
    recovery_disarm_observed = recovery_detail.get("recovery_disarm_observed")
    recovery_latest_ground_confirmed = recovery_detail.get(
        "recovery_latest_ground_confirmed"
    )
    force_disarm_no_ground_confirmation = recovery_detail.get(
        "force_disarm_no_ground_confirmation"
    )
    recovery_action_text = str(recovery_action or "").lower()
    snapshot_force_disarm_accepted = (
        snapshot.get("operator_recovery_assist_low_altitude_force_disarm_ack_result")
        == 0
    )
    snapshot_landed = snapshot.get("landed")
    snapshot_maybe_landed = snapshot.get("maybe_landed")
    snapshot_has_ground_signal = snapshot_landed is not None or snapshot_maybe_landed is not None
    snapshot_ground_confirmed = (
        snapshot_landed is True or snapshot_maybe_landed is True
    )
    snapshot_arming_state = _as_int(snapshot.get("arming_state"))
    snapshot_disarmed = (
        snapshot_arming_state is not None and snapshot_arming_state != 2
    )
    snapshot_force_without_ground = bool(
        "land" in recovery_action_text
        and snapshot_force_disarm_accepted
        and snapshot_has_ground_signal
        and not snapshot_ground_confirmed
    )
    if snapshot_force_without_ground:
        recovery_final_landing_safe = False
        force_disarm_no_ground_confirmation = _first_present(
            force_disarm_no_ground_confirmation,
            True,
        )
    if "land" in recovery_action_text and snapshot_has_ground_signal:
        recovery_latest_ground_confirmed = _first_present(
            recovery_latest_ground_confirmed,
            snapshot_ground_confirmed,
        )
    if snapshot_disarmed:
        recovery_disarm_observed = _first_present(
            recovery_disarm_observed,
            True,
        )
    recovery_evidence_path = runtime_summary.get("recovery_agent_evidence_window_path")
    guard_failure_reasons = runtime_summary.get("guard_failure_reasons")
    guard_failure_reasons = (
        guard_failure_reasons if isinstance(guard_failure_reasons, (list, tuple)) else []
    )
    recovery_was_guard_response = (
        runtime_summary.get("guard_abort_requested") is True
        or bool(guard_failure_reasons)
        or monitor_stop.startswith("auto_mission_")
    )
    recovery_label = (
        "Guarded Recovery" if recovery_was_guard_response else "Post-run Return"
    )
    monitor_window_ended = snapshot.get("monitor_window_ended") is True or (
        snapshot.get("snapshot_status") == "monitor_window_ended"
    )
    if (
        actual_sitl_evidence is None
        and progress_m is not None
        and progress_m > 0
        and physical_execution is not False
        and not missionos_fixture
    ):
        actual_sitl_evidence = True
    operator_recovery_hint = None

    if missionos_fixture:
        headline = "Fixture Only: no dispatch or live SITL flight was invoked"
    elif task_status == "running" and monitor_window_ended:
        headline = "Finalizing: AUTO monitor ended; waiting for terminal receipt"
    elif task_status == "running":
        headline = "In Flight: AUTO mission telemetry is still updating"
    elif task_status == "completed" and recovery_was_guard_response:
        headline = "Guarded Recovery Complete: Gateway recorded a terminal result"
    elif task_status == "completed":
        headline = "Complete: Gateway recorded a terminal live SITL result"
    elif task_status == "blocked":
        headline = "Blocked: Gateway stopped the task before completion"
    else:
        headline = f"Status: {task_status}"

    evidence_line = (
        "Evidence: "
        f"actual_sitl_flight={_format_flag(actual_sitl_evidence, default='pending')}; "
        f"dropoff_verified={_format_flag(dropoff_verified, default='pending')}; "
        f"sitl_delivery={_format_flag(sitl_delivery, default='pending')}"
    )
    lines = [
        headline,
        f"Task: {task.get('task_id')}  ({task_status}; dispatch={dispatch_status})",
        "",
        f"Route: {_progress_bar(progress_percent)} {_format_percent(progress_percent)}",
        f"Distance: {progress_text}",
        f"Waypoint: {waypoint_text}  ({current_text})",
        f"Elapsed: {_format_duration(elapsed_seconds)}"
        + (f"  ETA: ~{_format_duration(eta_seconds)}" if eta_seconds is not None else ""),
        f"Battery: {battery_text}",
        f"Altitude: {_operate_altitude_text(snapshot, artifacts)}",
        (
            "Terrain: "
            f"AGL={_format_distance(terrain_clearance_m)}; "
            f"target={_format_distance(terrain_clearance_target_m)}; "
            f"margin={_format_distance(terrain_clearance_margin_m)}; "
            f"status={terrain_clearance_status}"
        )
        if terrain_clearance_m is not None or terrain_clearance_target_m is not None
        else "Terrain: clearance=not_configured",
        f"SITL: startup={_status_text(startup.get('startup_status'))}; readiness={readiness_text}; mavlink={readiness.get('mavlink_endpoint_observed')}",
        "",
        evidence_line,
    ]
    process_status_text = _auto_process_status_text(
        artifacts=artifacts,
        metadata=metadata,
        snapshot=snapshot,
    )
    if process_status_text:
        lines.insert(2, process_status_text)
    operator_dispatch_text = _operator_recovery_dispatch_status_text(
        artifacts=artifacts,
        snapshot=snapshot,
    )
    if operator_dispatch_text:
        lines.insert(3 if process_status_text else 2, operator_dispatch_text)
    weather_condition = _job_weather_condition_text(artifacts)
    if weather_condition:
        sitl_index = next(
            (index for index, line in enumerate(lines) if line.startswith("SITL:")),
            len(lines),
        )
        lines.insert(sitl_index, weather_condition)
    realism_condition = _job_realism_condition_text(artifacts)
    if realism_condition:
        evidence_index = lines.index(evidence_line)
        lines.insert(evidence_index, realism_condition)
    if agent_bridge:
        agent_battery = agent_telemetry.get("battery")
        agent_battery = agent_battery if isinstance(agent_battery, dict) else {}
        endurance = agent_battery.get("endurance_projection")
        endurance = endurance if isinstance(endurance, dict) else {}
        return_home = agent_battery.get("return_home_projection")
        return_home = return_home if isinstance(return_home, dict) else {}
        agent_action = _first_present(
            agent_assessment.get("selected_bounded_action"),
            agent_assessment.get("recommended_action"),
            agent_assessment.get("recovery_action"),
        )
        agent_risk = agent_assessment.get("observed_risk_reasons")
        if isinstance(agent_risk, (list, tuple)):
            agent_risk_text = ",".join(str(item) for item in agent_risk) or "-"
        else:
            agent_risk_text = _status_text(
                agent_risk
                or agent_assessment.get("trigger_reasons")
                or agent_assessment.get("risk_level")
            )
        blocking_reasons = agent_result.get("blocking_reasons")
        if not isinstance(blocking_reasons, (list, tuple)):
            blocking_reasons = agent_assessment.get("blocking_reasons")
        blocking_text = (
            ",".join(str(item) for item in blocking_reasons)
            if isinstance(blocking_reasons, (list, tuple)) and blocking_reasons
            else "-"
        )
        lines.append(
            "Agent Proposal: "
            f"status={_runtime_recovery_effective_status(agent_result, agent_bridge, agent_assessment)}; "
            f"action={_status_text(agent_action)}; "
            f"risk_observed={agent_risk_text}; "
            f"blocked={blocking_text}; "
            "dispatch_authority=False"
        )
        if task_status == "running" and not monitor_window_ended:
            operator_recovery_hint = _operator_recovery_dispatch_hint(
                task_id=task.get("task_id"),
                action=agent_action,
                parameters=agent_assessment.get("proposed_parameters")
                if isinstance(agent_assessment.get("proposed_parameters"), dict)
                else None,
            )
            if operator_recovery_hint:
                lines.append(operator_recovery_hint)
        if endurance and endurance.get("projection_status") == "computed":
            lines.append(
                "Agent Basis: "
                f"burn={_format_percent(endurance.get('battery_burn_percent_per_km'))}/km; "
                f"remaining={_format_distance(endurance.get('remaining_route_m'))}; "
                f"needs={_format_percent(endurance.get('projected_battery_required_percent'))}; "
                f"arrival={_format_percent(endurance.get('projected_arrival_battery_percent'))}; "
                f"reserve_margin={_format_percent(endurance.get('projected_reserve_margin_percent'))}"
            )
        elif endurance:
            # Don't present a route-battery feasibility number we can't trust
            # (e.g. an arrival % higher than the current charge). The RTL basis
            # below is shown separately when it is computable.
            lines.append(
                "Agent Basis: route battery projection unavailable "
                f"({_status_text(endurance.get('projection_status')) or 'insufficient_observation'})"
            )
        if return_home:
            lines.append(
                "Agent RTL Basis: "
                f"home={_format_distance(return_home.get('distance_to_home_m'))}; "
                f"needs={_format_percent(return_home.get('projected_return_battery_required_percent'))}; "
                f"arrival={_format_percent(return_home.get('projected_return_arrival_battery_percent'))}; "
                f"reserve_margin={_format_percent(return_home.get('projected_return_reserve_margin_percent'))}; "
                f"insufficient={_format_flag(return_home.get('projected_insufficient_for_return_home'), default='pending')}"
            )
        agent_route = agent_telemetry.get("route")
        agent_route = agent_route if isinstance(agent_route, dict) else {}
        drift = agent_route.get("drift_projection")
        drift = drift if isinstance(drift, dict) else {}
        if drift:
            lines.append(
                "Agent Drift: "
                f"cross_track={_format_distance(drift.get('deviation_xy_m'))}; "
                f"along_track={_format_distance(drift.get('along_track_m'))}; "
                f"planned={_format_distance(drift.get('planned_route_m'))}"
            )
        terrain = agent_telemetry.get("terrain")
        terrain = terrain if isinstance(terrain, dict) else {}
        if terrain and terrain.get("projection_status") == "computed":
            lines.append(
                "Agent Terrain: "
                f"current_clearance={_format_distance(terrain.get('terrain_clearance_m'))}; "
                f"target={_format_distance(terrain.get('terrain_clearance_target_m'))}; "
                f"current_margin={_format_distance(terrain.get('terrain_clearance_margin_m'))}; "
                f"current_below_min={_format_flag(terrain.get('terrain_clearance_below_minimum'), default='pending')}"
            )
        obstacle = agent_telemetry.get("obstacle")
        obstacle = obstacle if isinstance(obstacle, dict) else {}
        if obstacle:
            lines.append(
                "Agent Obstacle: "
                f"status={_status_text(obstacle.get('projection_status'))}; "
                f"detected={_format_flag(obstacle.get('obstacle_detected'), default='pending')}; "
                f"building_risk={_format_flag(obstacle.get('building_risk_detected'), default='pending')}; "
                f"gazebo_spawned={_format_flag(obstacle.get('gazebo_obstacle_model_spawned'), default='pending')}"
            )
    if recovery_detail or recovery_evidence_path:
        lines.append(
            f"{recovery_label}: "
            f"action={_status_text(recovery_action)}; "
            f"ack={_format_flag(recovery_ack, default='pending')}; "
            f"return={_format_distance(recovery_return_progress)}; "
            f"final_landing_safe={_format_flag(recovery_final_landing_safe, default='pending')}; "
            f"observation_lost={_format_flag(recovery_observation_lost, default='pending')}"
        )
        if (
            recovery_disarm_observed is not None
            or recovery_latest_ground_confirmed is not None
            or force_disarm_no_ground_confirmation is not None
        ):
            lines.append(
                "Recovery Grounding: "
                f"disarm_observed={_format_flag(recovery_disarm_observed, default='pending')}; "
                f"latest_ground_confirmed={_format_flag(recovery_latest_ground_confirmed, default='pending')}; "
                "force_disarm_no_ground_confirmation="
                f"{_format_flag(force_disarm_no_ground_confirmation, default='pending')}"
            )
    lines.append(
        "Claims: "
        f"delivery_completion={_format_flag(delivery_completion, default='False')}; "
        f"physical_execution={_format_flag(physical_execution, default='False')}"
    )
    if monitor_stop != "-":
        lines.append(f"Monitor stop: {monitor_stop}")
    if recovery_evidence_path:
        evidence_label = (
            "Recovery evidence" if recovery_was_guard_response else "Return evidence"
        )
        lines.append(f"{evidence_label}: {recovery_evidence_path}")
    if failed_receipt:
        lines.extend(["", f"Failure: {_status_text(failed_receipt.get('failure_reason'))}"])
    elif task_status == "running":
        if monitor_window_ended:
            next_text = (
                "Next: wait for the Gateway terminal receipt, then rerun `missionos job-status`."
            )
        elif operator_recovery_hint:
            next_text = (
                "Next: use the operator recovery command above only with operator approval, "
                "or wait and rerun `missionos job-status`."
            )
        else:
            next_text = "Next: wait and rerun `missionos job-status`, or use recovery only if the operator intends LAND/RTL."
        lines.extend(["", next_text])
    return lines


def _timeline_events(timeline_payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = timeline_payload.get("events")
    if isinstance(events, list):
        return [event for event in events if isinstance(event, dict)]
    entries = timeline_payload.get("entries")
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict)]
    timeline = timeline_payload.get("timeline")
    if isinstance(timeline, list):
        return [event for event in timeline if isinstance(event, dict)]
    return []


def _timeline_time_text(value: Any) -> str:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds")
    return _status_text(value)


def _timeline_detail_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
    artifacts = changes.get("artifacts") if isinstance(changes.get("artifacts"), dict) else {}
    agent_bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    if isinstance(agent_bridge, dict):
        result = agent_bridge.get("runtime_recovery_agent_result")
        result = result if isinstance(result, dict) else {}
        assessment = result.get("assessment")
        assessment = assessment if isinstance(assessment, dict) else {}
        action = (
            assessment.get("selected_bounded_action")
            or assessment.get("recommended_action")
            or assessment.get("recovery_action")
            or "-"
        )
        risks = assessment.get("observed_risk_reasons") or assessment.get("trigger_reasons")
        risk_text = ",".join(str(item) for item in risks) if isinstance(risks, list) else _status_text(risks)
        return (
            "agent proposal: "
            f"{_status_text(result.get('runtime_status') or agent_bridge.get('bridge_status'))}; "
            f"action={_status_text(action)}; risk_observed={risk_text}"
        )
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    if isinstance(snapshot, dict):
        reached = snapshot.get("mission_reached_seq")
        total = snapshot.get("waypoint_total")
        return (
            f"{_format_duration(snapshot.get('elapsed_seconds'))}; "
            f"{_format_distance(snapshot.get('progress_m'))}; "
            f"wp {_status_text(reached)}/{_status_text(total)}; "
            f"battery {_format_percent(snapshot.get('battery_remaining_percent'))}"
        )
    failed = artifacts.get("missionos_auto_mission_gui_dispatch_failed_receipt")
    if isinstance(failed, dict):
        return "blocked: " + _status_text(failed.get("failure_reason"))
    detail = event.get("detail") or event.get("summary")
    if detail is None:
        detail = payload.get("error") or payload.get("status")
    if isinstance(detail, dict):
        return _status_text(
            detail.get("status")
            or detail.get("after")
            or detail.get("reason")
            or detail.get("message")
            or detail.get("artifact_ref")
        )
    return _status_text(detail)


def _print_job_status(
    task_payload: dict[str, Any],
    timeline_payload: dict[str, Any],
) -> None:
    console.print(
        Panel(
            "\n".join(_job_operator_summary(task_payload)),
            title="MissionOS Job",
            border_style="magenta",
        )
    )
    events = _timeline_events(timeline_payload)
    if not events:
        return
    table = Table(title="Recent Progress", show_header=True, header_style="bold cyan")
    table.add_column("Time", no_wrap=True)
    table.add_column("Event")
    table.add_column("Status")
    table.add_column("What Changed")
    for event in events:
        table.add_row(
            _timeline_time_text(
                event.get("created_at") or event.get("observed_at") or event.get("timestamp")
            ),
            _status_text(event.get("event_type") or event.get("type") or event.get("name")),
            _status_text(event.get("status")),
            _timeline_detail_text(event),
        )
    console.print(table)


def _task_and_timeline(
    client: MissionOSGatewayClient,
    task_id: str,
    *,
    timeline_limit: int = SITL_EXECUTION_POLL_TIMELINE_LIMIT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    encoded_task_id = quote(task_id, safe="")
    task_payload = client.get(f"/tasks/{encoded_task_id}")
    timeline_payload = (
        client.get(f"/tasks/{encoded_task_id}/timeline?limit={timeline_limit}")
        if timeline_limit
        else {"events": []}
    )
    return task_payload, timeline_payload


def _job_progress_status_text(task_payload: dict[str, Any] | None) -> str:
    if not isinstance(task_payload, dict):
        return "Execute Live SITL is running... waiting for Gateway response"
    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    running_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_running_receipt")
    running_receipt = running_receipt if isinstance(running_receipt, dict) else {}
    dispatch_receipt = artifacts.get("missionos_auto_mission_gui_dispatch_receipt")
    dispatch_receipt = dispatch_receipt if isinstance(dispatch_receipt, dict) else {}
    agent_bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    agent_bridge = agent_bridge if isinstance(agent_bridge, dict) else {}
    agent_result = agent_bridge.get("runtime_recovery_agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    agent_assessment = agent_result.get("assessment")
    agent_assessment = agent_assessment if isinstance(agent_assessment, dict) else {}

    status = _status_text(task.get("status") or task.get("task_status"))
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    dispatch_status = _status_text(
        dispatch_receipt.get("dispatch_status")
        or running_receipt.get("dispatch_status")
        or metadata.get("missionos_auto_mission_gui_dispatch_status")
    )
    progress_m = _as_float(snapshot.get("progress_m"))
    route_distance_m = _job_route_distance_m(artifacts)
    reached_seq = _as_int(snapshot.get("mission_reached_seq"))
    waypoint_total = _as_int(snapshot.get("waypoint_total"))
    battery = snapshot.get("battery_remaining_percent")
    terrain_clearance = snapshot.get("terrain_clearance_m")
    elapsed = snapshot.get("elapsed_seconds")
    monitor_ended = snapshot.get("monitor_window_ended") is True or (
        snapshot.get("snapshot_status") == "monitor_window_ended"
    )

    parts = [f"task={_status_text(task.get('task_id'))}", f"status={status}"]
    if dispatch_status != "-":
        parts.append(f"dispatch={dispatch_status}")
    if progress_m is not None:
        if route_distance_m is not None:
            parts.append(f"{_format_distance(progress_m)}/{_format_distance(route_distance_m)}")
        else:
            parts.append(_format_distance(progress_m))
    if reached_seq is not None or waypoint_total is not None:
        parts.append(f"wp {_status_text(reached_seq)}/{_status_text(waypoint_total)}")
    if battery is not None:
        parts.append(f"battery {_format_percent(battery)}")
    if terrain_clearance is not None:
        parts.append(f"terrain_clearance {_format_distance(terrain_clearance)}")
    weather_text = _job_weather_compact_text(artifacts)
    if weather_text:
        parts.append(weather_text)
    if elapsed is not None:
        parts.append(_format_duration(elapsed))
    operator_dispatch_text = _operator_recovery_dispatch_status_text(
        artifacts=artifacts,
        snapshot=snapshot,
        compact=True,
    )
    if operator_dispatch_text:
        parts.append(operator_dispatch_text)
    agent_action = _first_present(
        agent_assessment.get("selected_bounded_action"),
        agent_assessment.get("recommended_action"),
        agent_assessment.get("recovery_action"),
    )
    if agent_action:
        proposal_status = _runtime_recovery_effective_status(
            agent_result,
            agent_bridge,
            agent_assessment,
        )
        agent_risk = agent_assessment.get("observed_risk_reasons")
        if isinstance(agent_risk, (list, tuple)):
            risk_text = ",".join(str(item) for item in agent_risk[:2])
            if len(agent_risk) > 2:
                risk_text += ",..."
        else:
            risk_text = _status_text(
                agent_risk
                or agent_assessment.get("trigger_reasons")
                or agent_assessment.get("risk_level")
            )
        parts.append(
            f"agent_proposal {proposal_status}:{_status_text(agent_action)}"
            + (f" risk={risk_text}" if risk_text != "-" else "")
        )
        if not operator_dispatch_text and not monitor_ended and status == "running":
            recovery_hint = _operator_recovery_dispatch_hint(
                task_id=task.get("task_id"),
                action=agent_action,
                parameters=agent_assessment.get("proposed_parameters")
                if isinstance(agent_assessment.get("proposed_parameters"), dict)
                else None,
                compact=True,
            )
            if recovery_hint:
                parts.append(recovery_hint)
    if monitor_ended and status == "running":
        parts.append("finalizing")
    return "Execute Live SITL is running... " + " · ".join(parts)


def _execute_sitl_with_task_polling(
    client: MissionOSGatewayClient,
    *,
    task_id: str,
    live_flight_mode: bool,
    poll_interval: float = SITL_EXECUTION_POLL_INTERVAL,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Run Execute Live SITL while polling task state.

    Gateway's execute boundary is intentionally authoritative and can run for a
    long AUTO mission. The CLI keeps the HTTP request alive in a worker while the
    foreground renders task state, so a client-side read timeout does not become
    a raw traceback.
    """

    executor = ThreadPoolExecutor(max_workers=1)
    future: Future[dict[str, Any]] = executor.submit(
        client.execute_sitl,
        task_id=task_id,
        live_flight_mode=live_flight_mode,
    )
    last_task_payload: dict[str, Any] | None = None
    last_timeline_payload: dict[str, Any] | None = None
    http_timed_out = False
    try:
        while True:
            if http_timed_out:
                try:
                    last_task_payload, last_timeline_payload = _task_and_timeline(
                        client, task_id
                    )
                except click.ClickException:
                    time.sleep(max(0.01, poll_interval))
                    continue
                if progress_callback:
                    progress_callback(last_task_payload)
                status = _task_status(last_task_payload)
                if status in TERMINAL_TASK_STATUSES:
                    return None, last_task_payload, last_timeline_payload
                time.sleep(max(0.01, poll_interval))
                continue

            try:
                payload = future.result(timeout=max(0.01, poll_interval))
                return payload, last_task_payload, last_timeline_payload
            except FutureTimeout:
                try:
                    last_task_payload, last_timeline_payload = _task_and_timeline(
                        client, task_id
                    )
                except click.ClickException:
                    continue
                if progress_callback:
                    progress_callback(last_task_payload)
                status = _task_status(last_task_payload)
                if status in TERMINAL_TASK_STATUSES:
                    try:
                        payload = future.result(timeout=0.01)
                    except (FutureTimeout, httpx.ReadTimeout):
                        payload = None
                    return payload, last_task_payload, last_timeline_payload
            except httpx.ReadTimeout:
                try:
                    last_task_payload, last_timeline_payload = _task_and_timeline(
                        client, task_id
                    )
                except click.ClickException as exc:
                    raise click.ClickException(
                        "Execute Live SITL Gateway response exceeded the client wait window and task status "
                        f"could not be read: {exc.message}"
                    ) from exc
                if progress_callback:
                    progress_callback(last_task_payload)
                status = _task_status(last_task_payload)
                if status in TERMINAL_TASK_STATUSES:
                    return None, last_task_payload, last_timeline_payload
                http_timed_out = True
    finally:
        executor.shutdown(wait=future.done(), cancel_futures=not future.done())


@click.group(name="missionos")
@click.option("--gateway-url", default=DEFAULT_GATEWAY_URL, show_default=True)
@click.option("--timeout", default=45.0, show_default=True, type=float)
@click.option("--json-output", "json_output", is_flag=True, help="Print raw JSON.")
@click.option(
    "--state-path",
    default=DEFAULT_STATE_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Persist source-bound Mission Designer context between CLI commands.",
)
@click.pass_context
def missionos(
    ctx: click.Context,
    gateway_url: str,
    timeout: float,
    json_output: bool,
    state_path: Path,
) -> None:
    """Operate MissionOS through Gateway-backed CLI boundaries."""
    ctx.obj = ctx.obj or {}
    ctx.obj["missionos_client"] = make_client(gateway_url, timeout)
    ctx.obj["missionos_gateway_url"] = gateway_url
    ctx.obj["missionos_json_output"] = json_output
    ctx.obj["missionos_state_path"] = state_path


@missionos.group("gateway")
def gateway_command() -> None:
    """Start, stop, or inspect the local MissionOS Gateway."""


@gateway_command.command("start")
@click.option(
    "--pid-path",
    default=DEFAULT_GATEWAY_PID_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="PID file for the managed Gateway process.",
)
@click.option(
    "--log-path",
    default=DEFAULT_GATEWAY_LOG_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Log file for the managed Gateway process.",
)
@click.option(
    "--wait/--no-wait",
    default=True,
    show_default=True,
    help="Wait for /health before returning.",
)
@click.option(
    "--enable-live-sitl/--planning-only",
    default=False,
    show_default=True,
    help="Explicitly enable live SITL/dispatch Gateway environment variables.",
)
@click.pass_context
def gateway_start_command(
    ctx: click.Context,
    pid_path: Path,
    log_path: Path,
    wait: bool,
    enable_live_sitl: bool,
) -> None:
    """Start a local Gateway from the MissionOS CLI."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    base_url: str = ctx.obj["missionos_gateway_url"]
    if _gateway_reachable(client):
        if enable_live_sitl and _gateway_is_fixture_backend(client):
            raise click.ClickException(
                "A fixture Gateway is already running at this URL. Live SITL "
                "requires the production backend. Run "
                "`missionos gateway restart --enable-live-sitl` and then retry."
            )
        console.print(f"[green]Gateway is already running:[/green] {base_url}")
        return
    existing_record = _read_gateway_pid_record(pid_path)
    existing_pid = (
        int(existing_record["pid"])
        if existing_record is not None and existing_record.get("pid") is not None
        else None
    )
    if existing_pid is not None and _process_running(existing_pid):
        if _gateway_pid_record_matches_running_process(existing_record or {}):
            raise click.ClickException(
                f"Gateway PID file already points to a running process: {existing_pid}"
            )
        pid_path.unlink(missing_ok=True)
        console.print(
            "[yellow]Discarded a stale Gateway PID file that pointed at another "
            "process. No process was stopped.[/yellow]"
        )
    _start_managed_gateway(
        client=client,
        base_url=base_url,
        pid_path=pid_path,
        log_path=log_path,
        wait=wait,
        enable_live_sitl=enable_live_sitl,
    )


@gateway_command.command("status")
@click.option(
    "--pid-path",
    default=DEFAULT_GATEWAY_PID_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="PID file for the managed Gateway process.",
)
@click.pass_context
def gateway_status_command(ctx: click.Context, pid_path: Path) -> None:
    """Show whether the local Gateway is reachable and managed."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    base_url: str = ctx.obj["missionos_gateway_url"]
    record = _read_gateway_pid_record(pid_path)
    pid = None if record is None else _read_gateway_pid(pid_path)
    reachable = _gateway_reachable(client)
    managed = (
        pid is not None
        and _process_running(pid)
        and _gateway_pid_record_matches_running_process(record or {})
    )
    table = Table(title=f"MissionOS Gateway: {base_url}")
    table.add_column("Check")
    table.add_column("Status")
    table.add_row("HTTP health", "healthy" if reachable else "unreachable")
    table.add_row("Managed PID", str(pid) if managed else "-")
    table.add_row("PID file", str(pid_path) if pid_path.exists() else "-")
    if record is not None:
        table.add_row(
            "Live SITL env",
            "enabled" if record.get("enable_live_sitl") is True else "planning-only",
        )
        table.add_row("Backend", _status_text(record.get("backend"), "fixture"))
        if pid is not None and _process_running(pid) and not managed:
            table.add_row("PID validation", "mismatch/refused")
    console.print(table)


@gateway_command.command("stop")
@click.option(
    "--pid-path",
    default=DEFAULT_GATEWAY_PID_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="PID file for the managed Gateway process.",
)
def gateway_stop_command(pid_path: Path) -> None:
    """Stop a Gateway previously started by `missionos gateway start`."""
    record = _read_gateway_pid_record(pid_path)
    pid = _read_gateway_pid(pid_path)
    if pid is None:
        console.print("[yellow]No managed Gateway PID is recorded.[/yellow]")
        return
    if _process_running(pid) and not _gateway_pid_record_matches_running_process(record or {}):
        pid_path.unlink(missing_ok=True)
        raise click.ClickException(
            f"Gateway PID file did not match a managed MissionOS Gateway: pid={pid}. "
            "Stale PID file was removed; no process was stopped."
        )
    if _stop_gateway_pid(pid):
        pid_path.unlink(missing_ok=True)
        console.print(f"[green]Stopped Gateway:[/green] pid={pid}")
        return
    raise click.ClickException(f"Could not stop Gateway: pid={pid}")


@gateway_command.command("restart")
@click.option(
    "--pid-path",
    default=DEFAULT_GATEWAY_PID_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="PID file for the managed Gateway process.",
)
@click.option(
    "--log-path",
    default=DEFAULT_GATEWAY_LOG_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Log file for the managed Gateway process.",
)
@click.option(
    "--wait/--no-wait",
    default=True,
    show_default=True,
    help="Wait for /health before returning.",
)
@click.option(
    "--enable-live-sitl/--planning-only",
    default=False,
    show_default=True,
    help="Explicitly enable live SITL/dispatch Gateway environment variables.",
)
@click.pass_context
def gateway_restart_command(
    ctx: click.Context,
    pid_path: Path,
    log_path: Path,
    wait: bool,
    enable_live_sitl: bool,
) -> None:
    """Restart a Gateway previously started by `missionos gateway start`."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    base_url: str = ctx.obj["missionos_gateway_url"]
    record = _read_gateway_pid_record(pid_path)
    pid = _read_gateway_pid(pid_path)
    if pid is None:
        if _gateway_reachable(client):
            raise click.ClickException(
                "Gateway is reachable but has no managed MissionOS PID file. "
                "No process was stopped. Use a different --gateway-url or stop "
                "the unmanaged Gateway explicitly before restart."
            )
    elif _process_running(pid):
        if not _gateway_pid_record_matches_running_process(record or {}):
            pid_path.unlink(missing_ok=True)
            raise click.ClickException(
                f"Gateway PID file did not match a managed MissionOS Gateway: pid={pid}. "
                "Stale PID file was removed; no process was stopped."
            )
        if not _stop_gateway_pid(pid):
            raise click.ClickException(f"Could not stop Gateway: pid={pid}")
        pid_path.unlink(missing_ok=True)
        console.print(f"[green]Stopped Gateway:[/green] pid={pid}")
    elif pid_path.exists():
        pid_path.unlink(missing_ok=True)
        console.print("[yellow]Removed PID file for a stopped Gateway.[/yellow]")
    _start_managed_gateway(
        client=client,
        base_url=base_url,
        pid_path=pid_path,
        log_path=log_path,
        wait=wait,
        enable_live_sitl=enable_live_sitl,
    )


@missionos.command("status")
@click.pass_context
def status_command(ctx: click.Context) -> None:
    """Show the current operator surfaces without starting execution."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    payloads = {
        "health": client.health(),
        "form2a": client.get("/missionos/form2a-response-selection"),
        "review": client.get("/missionos/form2a-operator-review"),
        "action": client.get("/missionos/form2a-action-consumption"),
        "repair": client.get("/missionos/llm-repair-planner"),
    }
    if ctx.obj["missionos_json_output"]:
        _print_json(payloads)
        return
    _print_status(payloads, base_url=ctx.obj["missionos_gateway_url"])


@missionos.command("say")
@click.argument("instruction", nargs=-1, required=True)
@click.option("--session-id", default=DEFAULT_SESSION_ID, show_default=True)
@click.option("--route-hint", default="", help="Gateway route hint, e.g. mission_designer_plan.")
@click.option("--coordinate-route-json", default="", help="Coordinate route JSON object.")
@click.option(
    "--coordinate-route-file",
    default="",
    type=click.Path(dir_okay=False),
    help="Path to a coordinate route JSON or YAML object.",
)
@click.pass_context
def say_command(
    ctx: click.Context,
    instruction: tuple[str, ...],
    session_id: str,
    route_hint: str,
    coordinate_route_json: str,
    coordinate_route_file: str,
) -> None:
    """Send a natural-language MissionOS instruction."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    coordinate_route = _load_json_object(
        coordinate_route_json,
        label="--coordinate-route-json",
    ) or _load_coordinate_route_file(coordinate_route_file)
    payload = client.conversation(
        " ".join(instruction),
        session_id=session_id,
        mission_designer_context=_stored_mission_designer_context(ctx, session_id),
        coordinate_route=coordinate_route,
        route_hint=route_hint or None,
    )
    _remember_mission_designer_context(ctx, payload, session_id=session_id)
    if ctx.obj["missionos_json_output"]:
        _print_json(payload)
        return
    _print_conversation_result(payload)


def _intent_command(intent: str):
    @click.option("--session-id", default=DEFAULT_SESSION_ID, show_default=True)
    @click.pass_context
    def _run(ctx: click.Context, session_id: str) -> None:
        client: MissionOSGatewayClient = ctx.obj["missionos_client"]
        payload = client.conversation(
            INTENT_INSTRUCTIONS[intent],
            session_id=session_id,
            mission_designer_context=_stored_mission_designer_context(ctx, session_id),
            route_hint=INTENT_ROUTE_HINTS[intent],
        )
        _remember_mission_designer_context(ctx, payload, session_id=session_id)
        if ctx.obj["missionos_json_output"]:
            _print_json(payload)
            return
        _print_conversation_result(payload)

    return _run


for _intent, _help in {
    "approve": "Record operator approval through MissionOS.",
    "reject": "Record operator rejection through MissionOS.",
    "revision": "Ask MissionOS to revise the current plan.",
    "run": "Run the approved bounded action through execution gates.",
    "repair": "Ask MissionOS to diagnose and draft a repair.",
}.items():
    missionos.add_command(
        click.command(_intent, help=_help)(_intent_command(_intent))
    )


def _parse_recovery_parameters(items: tuple[str, ...] | list[str]) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise click.ClickException(f"recovery parameter must be key=value: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise click.ClickException(f"recovery parameter key is empty: {item}")
        try:
            parameters[key] = float(value)
        except ValueError:
            parameters[key] = value
    return parameters


@missionos.command("clear-state")
@click.pass_context
def clear_state_command(ctx: click.Context) -> None:
    """Forget the stored source-bound Mission Designer context."""
    path: Path = ctx.obj["missionos_state_path"]
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise click.ClickException(f"could not remove {path}: {exc}") from exc
    if ctx.obj["missionos_json_output"]:
        _print_json({"state_cleared": True, "state_path": str(path)})
        return
    console.print(f"Cleared MissionOS CLI state at {path}")


@missionos.command("recover")
@click.option("--task-id", required=True, help="Running AUTO mission task id.")
@click.option(
    "--action",
    "recovery_action",
    required=True,
    help="Operator-approved recovery action; Gateway validates the current allowlist.",
)
@click.option(
    "--param",
    "recovery_params",
    multiple=True,
    help="Recovery parameter as key=value. Repeat for target_altitude_m, target_speed_mps, target_x_m, target_y_m.",
)
@click.pass_context
def recover_command(
    ctx: click.Context,
    task_id: str,
    recovery_action: str,
    recovery_params: tuple[str, ...],
) -> None:
    """Send an operator-approved recovery dispatch."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    payload = client.recovery_dispatch(
        task_id=task_id,
        recovery_action=recovery_action,
        recovery_parameters=_parse_recovery_parameters(recovery_params),
    )
    if ctx.obj["missionos_json_output"]:
        _print_json(payload)
        return
    task_payload = _wait_for_active_runner_recovery_observation(client, payload)
    _print_recovery_result(payload, task_payload=task_payload)


@missionos.command("execute-sitl")
@click.option(
    "--task-id",
    default="",
    help="Prepared SITL execution task id. Defaults to the task stored by `run`.",
)
@click.option(
    "--live-flight/--upload-only",
    default=True,
    show_default=True,
    help="Request the explicit Execute Live SITL boundary.",
)
@click.option(
    "--poll-interval",
    default=SITL_EXECUTION_POLL_INTERVAL,
    show_default=True,
    type=click.FloatRange(0.1, 60.0),
    help="Seconds between task status polls during live SITL execution.",
)
@click.pass_context
def execute_sitl_command(
    ctx: click.Context,
    task_id: str,
    live_flight: bool,
    poll_interval: float,
) -> None:
    """Run the explicit Execute Live SITL boundary."""
    resolved_task_id = task_id or _stored_sitl_task_id(ctx)
    if not resolved_task_id:
        raise click.ClickException(
            "task id is required; run `missionos run` first or pass --task-id"
        )
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    if live_flight:
        with console.status(
            "[red]Execute Live SITL is running... waiting for Gateway response[/red]",
            spinner="dots",
        ) as status:
            payload, task_payload, timeline_payload = _execute_sitl_with_task_polling(
                client,
                task_id=resolved_task_id,
                live_flight_mode=True,
                poll_interval=poll_interval,
                progress_callback=lambda latest: status.update(
                    f"[red]{_job_progress_status_text(latest)}[/red]"
                ),
            )
    else:
        payload = client.execute_sitl(
            task_id=resolved_task_id,
            live_flight_mode=False,
        )
        task_payload = None
        timeline_payload = None
    latest_task_id = _remember_sitl_task_id_from_payload(
        ctx,
        task_payload if task_payload is not None else payload,
        fallback_task_id=resolved_task_id,
    )
    if ctx.obj["missionos_json_output"]:
        _print_json(
            {
                "task_id": latest_task_id,
                "execute_result": payload,
                "task": task_payload,
                "timeline": timeline_payload,
            }
            if live_flight
            else payload
        )
        return
    if payload is None and task_payload is not None and timeline_payload is not None:
        console.print(f"[yellow]{LIVE_SITL_RESPONSE_WAIT_EXCEEDED_MESSAGE}[/yellow]")
        _print_job_status(task_payload, timeline_payload)
        return
    _print_sitl_execution_result(payload)


@missionos.command("start-sitl")
@click.option(
    "--task-id",
    default="",
    help="Prepared SITL execution task id. Defaults to the task stored by `run`.",
)
@click.pass_context
def start_sitl_command(ctx: click.Context, task_id: str) -> None:
    """Start the PX4/Gazebo SITL environment readiness action."""
    resolved_task_id = task_id or _stored_sitl_task_id(ctx)
    if not resolved_task_id:
        raise click.ClickException(
            "task id is required; run `missionos run` first or pass --task-id"
        )
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    payload = client.start_sitl(task_id=resolved_task_id)
    _remember_sitl_task_id_from_payload(
        ctx,
        payload,
        fallback_task_id=resolved_task_id,
    )
    if ctx.obj["missionos_json_output"]:
        _print_json(payload)
        return
    _print_sitl_start_result(payload)


@missionos.command("job-status")
@click.option(
    "--task-id",
    default="",
    help="Task/job id to inspect. Defaults to the task stored by `run`.",
)
@click.option(
    "--timeline-limit",
    default=8,
    show_default=True,
    type=click.IntRange(0, 100),
    help="Number of recent task timeline events to show.",
)
@click.pass_context
def job_status_command(ctx: click.Context, task_id: str, timeline_limit: int) -> None:
    """Show a running or completed MissionOS task through the Gateway task API."""
    resolved_task_id = task_id or _stored_sitl_task_id(ctx)
    if not resolved_task_id:
        raise click.ClickException(
            "task id is required; run `missionos run` first or pass --task-id"
        )
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    encoded_task_id = quote(resolved_task_id, safe="")
    task_payload = client.get(f"/tasks/{encoded_task_id}")
    timeline_payload = (
        client.get(f"/tasks/{encoded_task_id}/timeline?limit={timeline_limit}")
        if timeline_limit
        else {"events": []}
    )
    if ctx.obj["missionos_json_output"]:
        _print_json(
            {
                "task_id": resolved_task_id,
                "task": task_payload,
                "timeline": timeline_payload,
            }
        )
        return
    _print_job_status(task_payload, timeline_payload)


# ── Live terminal dot-art map (`missionos watch`) ─────────────────────────────
FLIGHT_MAP_WIDTH = 64
FLIGHT_MAP_HEIGHT = 24
FLIGHT_PROFILE_HEIGHT = 9
FLIGHT_MAP_POLL_INTERVAL = 1.0
_FLIGHT_MAP_TRAIL_LIMIT = 4000


def _project_flight_points(
    points: list[tuple[float, float]],
    *,
    width: int,
    height: int,
) -> list[tuple[int, int]]:
    """Project NED points (north_x, east_y) onto a (row, col) character grid.

    North is up (smaller row), East is right (larger col). One uniform scale is
    used for both axes so geometry is not distorted; rows count double because
    terminal cells are roughly twice as tall as they are wide.
    """
    if not points:
        return []
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    span_x = max(xmax - xmin, 1e-6)
    span_y = max(ymax - ymin, 1e-6)
    scale = max(span_y / max(width - 1, 1), span_x / max((height - 1) * 2, 1)) or 1.0
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    projected: list[tuple[int, int]] = []
    for north_x, east_y in points:
        col = round((width - 1) / 2.0 + (east_y - cy) / scale)
        row = round((height - 1) / 2.0 - (north_x - cx) / (scale * 2.0))
        col = min(max(col, 0), width - 1)
        row = min(max(row, 0), height - 1)
        projected.append((row, col))
    return projected


def _dropoff_ned_from_route(artifacts: dict[str, Any]) -> tuple[float, float] | None:
    """Approximate dropoff position in NED metres relative to takeoff (home)."""
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    tlat = _as_float(route.get("takeoff_latitude") or route.get("takeoff_latitude_deg"))
    tlon = _as_float(route.get("takeoff_longitude") or route.get("takeoff_longitude_deg"))
    dlat = _as_float(route.get("dropoff_latitude") or route.get("dropoff_latitude_deg"))
    dlon = _as_float(route.get("dropoff_longitude") or route.get("dropoff_longitude_deg"))
    if None in (tlat, tlon, dlat, dlon):
        return None
    north = (dlat - tlat) * 111320.0
    east = (dlon - tlon) * 111320.0 * math.cos(math.radians(tlat))
    return (north, east)


def _mission_map_latlon_to_local(
    *,
    takeoff_lat: float,
    takeoff_lon: float,
    lat: float,
    lon: float,
) -> tuple[float, float]:
    north = (lat - takeoff_lat) * 111320.0
    east = (lon - takeoff_lon) * 111320.0 * math.cos(math.radians(takeoff_lat))
    return north, east


def _mission_command_label(command: Any) -> str:
    command_id = _as_int(command)
    labels = {
        16: "waypoint",
        19: "dropoff_loiter",
        21: "land",
        22: "takeoff",
    }
    return labels.get(command_id, f"command_{command_id}") if command_id is not None else "-"


def _mission_map_planned_points(
    artifacts: dict[str, Any],
    *,
    takeoff_lat: float,
    takeoff_lon: float,
    dropoff_lat: float,
    dropoff_lon: float,
) -> list[dict[str, Any]]:
    compilation = artifacts.get("missionos_auto_mission_compilation")
    compilation = compilation if isinstance(compilation, dict) else {}
    points: list[dict[str, Any]] = []
    mission_items = compilation.get("mission_items")
    if isinstance(mission_items, list):
        for idx, item in enumerate(mission_items):
            if not isinstance(item, dict):
                continue
            latlon = _mission_map_sample_latlon(
                item,
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
            )
            if latlon is None:
                continue
            lat, lon, source = latlon
            seq = _as_int(item.get("seq"))
            command = _as_int(item.get("command"))
            planned_source = (
                "planned_wgs84"
                if source == "observed_wgs84"
                else "planned_from_local_ned"
                if source == "estimated_from_local_ned"
                else f"planned_{source}"
            )
            points.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "source": planned_source,
                    "phase": _mission_command_label(command),
                    "seq": seq if seq is not None else idx,
                    "command": command,
                    "alt_m": _as_float(
                        item.get("altitude_m")
                        or item.get("relative_alt_m")
                        or item.get("z_m")
                    ),
                }
            )
    if len(points) >= 2:
        return points
    return [
        {
            "lat": takeoff_lat,
            "lon": takeoff_lon,
            "source": "planned_route_takeoff",
            "phase": "takeoff",
            "seq": 0,
            "command": None,
            "alt_m": 0.0,
        },
        {
            "lat": dropoff_lat,
            "lon": dropoff_lon,
            "source": "planned_route_dropoff",
            "phase": "dropoff",
            "seq": 1,
            "command": None,
            "alt_m": None,
        },
    ]


def _mission_obstacle_records_from_artifacts(
    artifacts: dict[str, Any],
) -> list[dict[str, Any]]:
    def obstacle_xy(record: dict[str, Any]) -> tuple[float, float] | None:
        pose = record.get("pose_readback")
        pose = pose if isinstance(pose, dict) else {}
        x_m = _first_numeric(
            record.get("x_m"),
            record.get("local_x_m"),
            record.get("x"),
            pose.get("x"),
        )
        y_m = _first_numeric(
            record.get("y_m"),
            record.get("local_y_m"),
            record.get("y"),
            pose.get("y"),
        )
        if x_m is None or y_m is None:
            return None
        return float(x_m), float(y_m)

    sources: list[tuple[str, dict[str, Any], bool | None]] = []
    direct = artifacts.get("obstacle_manifest")
    if isinstance(direct, dict):
        sources.append(("obstacle_manifest", direct, _as_bool(direct.get("gazebo_obstacle_model_spawned"))))
    probe = artifacts.get("missionos_auto_mission_probe_observed")
    probe = probe if isinstance(probe, dict) else {}
    probe_manifest = probe.get("obstacle_manifest")
    if isinstance(probe_manifest, dict):
        sources.append(
            (
                "probe_observed.obstacle_manifest",
                probe_manifest,
                _as_bool(probe_manifest.get("gazebo_obstacle_model_spawned")),
            )
        )
    gazebo_application = probe.get("gazebo_obstacle_application")
    gazebo_application = gazebo_application if isinstance(gazebo_application, dict) else {}
    app_manifest = gazebo_application.get("obstacle_manifest")
    if isinstance(app_manifest, dict):
        sources.append(
            (
                "gazebo_obstacle_application.obstacle_manifest",
                app_manifest,
                _as_bool(
                    _first_present(
                        app_manifest.get("gazebo_obstacle_model_spawned"),
                        gazebo_application.get("gazebo_obstacle_model_spawned"),
                    )
                ),
            )
        )
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    snapshot_manifest = snapshot.get("obstacle_manifest")
    if isinstance(snapshot_manifest, dict):
        sources.append(
            (
                "runtime_snapshot.obstacle_manifest",
                snapshot_manifest,
                _as_bool(snapshot_manifest.get("gazebo_obstacle_model_spawned")),
            )
        )
    snapshot_app = snapshot.get("gazebo_obstacle_application")
    snapshot_app = snapshot_app if isinstance(snapshot_app, dict) else {}
    snapshot_app_manifest = snapshot_app.get("obstacle_manifest")
    if isinstance(snapshot_app_manifest, dict):
        sources.append(
            (
                "runtime_snapshot.gazebo_obstacle_application.obstacle_manifest",
                snapshot_app_manifest,
                _as_bool(
                    _first_present(
                        snapshot_app_manifest.get("gazebo_obstacle_model_spawned"),
                        snapshot_app.get("gazebo_obstacle_model_spawned"),
                    )
                ),
            )
        )

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, float, float]] = set()
    for source, manifest, manifest_spawned in sources:
        obstacles = manifest.get("obstacles")
        if not isinstance(obstacles, list):
            continue
        for idx, obstacle in enumerate(obstacles):
            if not isinstance(obstacle, dict):
                continue
            xy = obstacle_xy(obstacle)
            if xy is None:
                continue
            x_m, y_m = xy
            name = _status_text(obstacle.get("name"), f"obstacle_{idx}")
            key = (name, round(x_m, 2), round(y_m, 2))
            if key in seen:
                continue
            seen.add(key)
            spawned = _as_bool(obstacle.get("gazebo_obstacle_model_spawned"))
            if spawned is None:
                spawned = manifest_spawned
            records.append(
                {
                    "name": name,
                    "kind": _status_text(obstacle.get("kind"), "obstacle"),
                    "source": _status_text(obstacle.get("source"), source),
                    "source_ref": source,
                    "x_m": x_m,
                    "y_m": y_m,
                    "z_m": _as_float(obstacle.get("z_m") or obstacle.get("z")),
                    "size_x_m": _as_float(obstacle.get("size_x_m")),
                    "size_y_m": _as_float(obstacle.get("size_y_m")),
                    "size_z_m": _as_float(obstacle.get("size_z_m")),
                    "spawned": spawned,
                }
            )

    models = gazebo_application.get("models")
    if isinstance(models, list):
        for idx, model in enumerate(models):
            if not isinstance(model, dict):
                continue
            xy = obstacle_xy(model)
            if xy is None:
                continue
            x_m, y_m = xy
            name = _status_text(model.get("name"), f"gazebo_model_{idx}")
            key = (name, round(x_m, 2), round(y_m, 2))
            if key in seen:
                continue
            seen.add(key)
            records.append(
                {
                    "name": name,
                    "kind": _status_text(model.get("kind"), "obstacle"),
                    "source": _status_text(model.get("source"), "gazebo_obstacle_application.models"),
                    "source_ref": "gazebo_obstacle_application.models",
                    "x_m": x_m,
                    "y_m": y_m,
                    "z_m": _as_float(model.get("z_m") or model.get("z")),
                    "size_x_m": _as_float(model.get("size_x_m")),
                    "size_y_m": _as_float(model.get("size_y_m")),
                    "size_z_m": _as_float(model.get("size_z_m")),
                    "spawned": _as_bool(
                        _first_present(
                            model.get("pose_readback_observed"),
                            model.get("spawn_request_accepted"),
                            model.get("spawn_performed"),
                        )
                    ),
                }
            )

    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    if not records and _as_bool(route.get("landing_zone_blocked")) is True:
        dropoff = _dropoff_ned_from_route(artifacts)
        if dropoff is not None:
            records.append(
                {
                    "name": "landing_zone_blocked",
                    "kind": "landing_zone_risk",
                    "source": "mission_designer_coordinate_pair_route",
                    "source_ref": "route.landing_zone_blocked",
                    "x_m": dropoff[0],
                    "y_m": dropoff[1],
                    "z_m": None,
                    "size_x_m": None,
                    "size_y_m": None,
                    "size_z_m": None,
                    "spawned": False,
                }
            )
    return records


def _operator_recovery_local_maneuver_model(
    *,
    artifacts: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    probe = artifacts.get("missionos_auto_mission_probe_observed")
    probe = probe if isinstance(probe, dict) else {}
    monitor = probe.get("monitor")
    monitor = monitor if isinstance(monitor, dict) else {}
    operator_recovery = monitor.get("operator_recovery")
    operator_recovery = operator_recovery if isinstance(operator_recovery, dict) else {}
    command = operator_recovery.get("command")
    command = command if isinstance(command, dict) else {}

    recovery_path = _status_text(
        command.get("recovery_path") or snapshot.get("operator_recovery_path")
    )
    action = _status_text(command.get("action") or snapshot.get("operator_recovery_action"))
    if "avoid_obstacle" in recovery_path:
        action = "avoid_obstacle"
    target = command.get("target")
    target = target if isinstance(target, dict) else {}
    snapshot_target = snapshot.get("operator_recovery_target")
    snapshot_target = snapshot_target if isinstance(snapshot_target, dict) else {}
    parameters = snapshot.get("operator_recovery_parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    proposal_revalidation = artifacts.get(
        "missionos_runtime_recovery_proposal_revalidation"
    )
    proposal_revalidation = (
        proposal_revalidation if isinstance(proposal_revalidation, dict) else {}
    )
    recovery_start = proposal_revalidation.get("current_position")
    recovery_start = recovery_start if isinstance(recovery_start, dict) else {}
    if not recovery_start:
        last_proposal = artifacts.get("missionos_runtime_recovery_last_proposal")
        last_proposal = last_proposal if isinstance(last_proposal, dict) else {}
        recovery_start = last_proposal.get("origin_position")
        recovery_start = recovery_start if isinstance(recovery_start, dict) else {}

    target_x = _first_numeric(
        target.get("target_x_m"),
        target.get("x_m"),
        target.get("x"),
        snapshot_target.get("target_x_m"),
        snapshot_target.get("x_m"),
        parameters.get("target_x_m"),
    )
    target_y = _first_numeric(
        target.get("target_y_m"),
        target.get("y_m"),
        target.get("y"),
        snapshot_target.get("target_y_m"),
        snapshot_target.get("y_m"),
        parameters.get("target_y_m"),
    )
    target_altitude = _first_numeric(
        command.get("target_altitude_m"),
        target.get("target_altitude_m"),
        snapshot_target.get("target_altitude_m"),
        parameters.get("target_altitude_m"),
    )
    target_z = _first_numeric(target.get("target_z_m"), snapshot_target.get("target_z_m"))
    if target_altitude is None and target_z is not None:
        target_altitude = abs(float(target_z))

    samples: list[dict[str, Any]] = []
    raw_samples = command.get("maneuver_observation_samples")
    if isinstance(raw_samples, list):
        for idx, sample in enumerate(raw_samples):
            if not isinstance(sample, dict):
                continue
            x_m = _first_numeric(sample.get("local_x_m"), sample.get("x_m"), sample.get("x"))
            y_m = _first_numeric(sample.get("local_y_m"), sample.get("y_m"), sample.get("y"))
            if x_m is None or y_m is None:
                continue
            samples.append(
                {
                    "x_m": float(x_m),
                    "y_m": float(y_m),
                    "altitude_m": _as_float(
                        sample.get("altitude_above_home_m")
                        or sample.get("relative_alt_m")
                        or sample.get("local_z_m")
                        or sample.get("z_m")
                    ),
                    "distance_to_target_m": _as_float(sample.get("distance_to_target_m")),
                    "elapsed_s": sample.get("elapsed_seconds")
                    or sample.get("elapsed_s")
                    or sample.get("sample_time_s")
                    or idx,
                    "nav_state": sample.get("nav_state"),
                }
            )

    if target_x is None and target_y is None and not samples:
        return {}
    target_point = None
    if target_x is not None and target_y is not None:
        target_point = {
            "x_m": float(target_x),
            "y_m": float(target_y),
            "altitude_m": float(target_altitude) if target_altitude is not None else None,
        }
    start_x = _as_float(recovery_start.get("local_x_m"))
    start_y = _as_float(recovery_start.get("local_y_m"))
    start_point = None
    if start_x is not None and start_y is not None:
        start_point = {
            "x_m": start_x,
            "y_m": start_y,
            "altitude_m": _as_float(recovery_start.get("altitude_above_home_m")),
            "source": "dispatch_revalidation_current_position",
        }
    return {
        "action": action,
        "status": _status_text(
            command.get("status") or snapshot.get("operator_recovery_assist_status")
        ),
        "recovery_path": recovery_path,
        "start": start_point,
        "target": target_point,
        "samples": samples,
        "target_reached": _as_bool(
            _first_present(
                command.get("target_reached"),
                snapshot.get("operator_recovery_target_reached"),
            )
        ),
        "target_distance_m": _as_float(
            command.get("target_distance_m")
            or snapshot.get("operator_recovery_target_distance_m")
        ),
        "resume_auto_status": _status_text(
            command.get("resume_auto_status")
            or snapshot.get("operator_recovery_resume_auto_status")
        ),
        "source": "operator_recovery_command"
        if command
        else "missionos_auto_mission_runtime_snapshot",
    }


def _fmt_metres(value: Any) -> str:
    metres = _as_float(value)
    if metres is None:
        return "-"
    if abs(metres) < 0.5:
        metres = 0.0
    if abs(metres) >= 1000.0:
        return f"{metres / 1000.0:.2f}km"
    return f"{metres:.0f}m"


def _fmt_signed_metres(value: Any) -> str:
    metres = _as_float(value)
    if metres is None:
        return "-"
    if abs(metres) < 0.5:
        metres = 0.0
    prefix = "+" if metres >= 0 else ""
    return f"{prefix}{_fmt_metres(metres)}"


def _projection_computed(projection: dict[str, Any]) -> bool:
    return projection.get("projection_status") == "computed"


def _operate_altitude_text(
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
) -> str:
    """Show altitude references explicitly: AMSL, home-relative, and AGL."""
    alt_home = _as_float(snapshot.get("altitude_above_home_m"))
    terrain = _as_float(snapshot.get("terrain_elevation_m"))
    clearance = _as_float(snapshot.get("terrain_clearance_m"))
    target = _as_float(snapshot.get("terrain_clearance_target_m"))
    margin = _as_float(snapshot.get("terrain_clearance_margin_m"))

    samples, _planned_route_m = _terrain_profile_samples_for_watch(artifacts)
    first_terrain = samples[0]["terrain_elevation_m"] if samples else None
    current_amsl = (
        terrain + clearance
        if terrain is not None and clearance is not None
        else first_terrain + alt_home
        if first_terrain is not None and alt_home is not None
        else None
    )
    destination_target_amsl = next(
        (
            sample.get("target_amsl_m")
            for sample in reversed(samples)
            if sample.get("target_amsl_m") is not None
        ),
        None,
    )
    climb_to_destination = (
        destination_target_amsl - current_amsl
        if destination_target_amsl is not None and current_amsl is not None
        else None
    )

    parts: list[str] = []
    if current_amsl is not None:
        parts.append(f"alt={_fmt_metres(current_amsl)} AMSL")
    if alt_home is not None:
        parts.append(f"alt(home)={_fmt_signed_metres(alt_home)}")
    if clearance is not None or target is not None:
        agl = f"AGL={_fmt_metres(clearance)}"
        if target is not None:
            agl += f"/target {_fmt_metres(target)}"
        if margin is not None:
            agl += f" (margin {_fmt_signed_metres(margin)})"
        parts.append(agl)
    if destination_target_amsl is not None:
        destination = f"dest={_fmt_metres(destination_target_amsl)} AMSL"
        if climb_to_destination is not None:
            destination += f"/climb {_fmt_signed_metres(climb_to_destination)}"
        parts.append(destination)
    return " · ".join(parts) if parts else "alt=-"


def _watch_altitude_status(snapshot: dict[str, Any]) -> str:
    """Summarize altitude without implying terrain data exists when it does not."""
    alt_home = _as_float(snapshot.get("altitude_above_home_m"))
    terrain = _as_float(snapshot.get("terrain_elevation_m"))
    clearance = _as_float(snapshot.get("terrain_clearance_m"))
    target = _as_float(snapshot.get("terrain_clearance_target_m"))
    status = _status_text(snapshot.get("terrain_clearance_status"))
    if terrain is None and clearance is None and target is None:
        return (
            f"alt(home)={_fmt_metres(alt_home)}  "
            "terrain_elev(AMSL)=not_configured  AGL=-  target=-  "
            "drone_amsl=-"
        )
    amsl = terrain + clearance if terrain is not None and clearance is not None else None
    return (
        f"alt(home)={_fmt_metres(alt_home)}  "
        f"terrain_elev(AMSL)={_fmt_metres(terrain)}  "
        f"AGL={_fmt_metres(clearance)}  "
        f"target={_fmt_metres(target)} ({status})  "
        f"drone_amsl={_fmt_metres(amsl)}"
    )


def _watch_process_status(
    *,
    artifacts: dict[str, Any],
    snapshot: dict[str, Any],
) -> str | None:
    process_status = _auto_process_status_text(
        artifacts=artifacts,
        snapshot=snapshot,
    )
    if process_status:
        return process_status.removeprefix("Process: ")
    monitor_stop = _status_text(snapshot.get("monitor_stop_reason"))
    if monitor_stop != "-":
        return f"terminal_receipt=pending; stop={monitor_stop}"
    return None


def _terrain_profile_samples_for_watch(
    artifacts: dict[str, Any],
) -> tuple[list[dict[str, float]], float | None]:
    compilation = artifacts.get("missionos_auto_mission_compilation")
    compilation = compilation if isinstance(compilation, dict) else {}
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    raw_profile = compilation.get("terrain_clearance_profile")
    if not raw_profile:
        raw_profile = route.get("terrain_profile")
    if not isinstance(raw_profile, list):
        return [], None

    planned_route_m = _as_float(
        compilation.get("planned_route_m")
        or route.get("planned_route_m")
        or route.get("derived_route_distance_m")
    )
    if planned_route_m is None:
        distances = [
            _as_float(sample.get("distance_m"))
            for sample in raw_profile
            if isinstance(sample, dict)
        ]
        distances = [distance for distance in distances if distance is not None]
        planned_route_m = max(distances) if distances else None

    target_clearance = _as_float(
        compilation.get("terrain_clearance_target_m")
        or route.get("terrain_clearance_agl_m")
        or route.get("terrain_clearance_target_m")
    )
    first_terrain = None
    samples: list[dict[str, float]] = []
    for sample in raw_profile:
        if not isinstance(sample, dict):
            continue
        terrain = _as_float(sample.get("terrain_elevation_m"))
        if terrain is None:
            continue
        if first_terrain is None:
            first_terrain = terrain
        distance = _as_float(sample.get("distance_m"))
        fraction = _as_float(sample.get("fraction"))
        if fraction is None and distance is not None and planned_route_m:
            fraction = distance / planned_route_m
        if fraction is None:
            continue
        mission_altitude = _as_float(sample.get("mission_altitude_m"))
        sample_target = _as_float(sample.get("target_clearance_m")) or target_clearance
        if mission_altitude is not None and first_terrain is not None:
            target_amsl = first_terrain + mission_altitude
        elif sample_target is not None:
            target_amsl = terrain + sample_target
        else:
            target_amsl = None
        normalized = {
            "fraction": min(1.0, max(0.0, fraction)),
            "terrain_elevation_m": terrain,
        }
        if distance is not None:
            normalized["distance_m"] = distance
        if target_amsl is not None:
            normalized["target_amsl_m"] = target_amsl
        samples.append(normalized)
    samples.sort(key=lambda item: item["fraction"])
    return samples, planned_route_m


def _interpolate_watch_profile_value(
    samples: list[dict[str, float]],
    *,
    fraction: float,
    key: str,
) -> float | None:
    points = [
        (sample["fraction"], sample[key])
        for sample in samples
        if sample.get(key) is not None
    ]
    if not points:
        return None
    if fraction <= points[0][0]:
        return points[0][1]
    if fraction >= points[-1][0]:
        return points[-1][1]
    for (left_fraction, left_value), (right_fraction, right_value) in zip(
        points,
        points[1:],
        strict=False,
    ):
        if left_fraction <= fraction <= right_fraction:
            span = max(right_fraction - left_fraction, 1e-9)
            ratio = (fraction - left_fraction) / span
            return left_value + (right_value - left_value) * ratio
    return points[-1][1]


def _render_elevation_profile(
    *,
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
    width: int = FLIGHT_MAP_WIDTH,
    height: int = FLIGHT_PROFILE_HEIGHT,
) -> Panel | None:
    samples, planned_route_m = _terrain_profile_samples_for_watch(artifacts)
    if not samples:
        return None

    terrain_values = [
        _interpolate_watch_profile_value(
            samples,
            fraction=col / max(width - 1, 1),
            key="terrain_elevation_m",
        )
        for col in range(width)
    ]
    target_values = [
        _interpolate_watch_profile_value(
            samples,
            fraction=col / max(width - 1, 1),
            key="target_amsl_m",
        )
        for col in range(width)
    ]
    progress_m = _as_float(snapshot.get("progress_m"))
    progress_fraction = (
        min(1.0, max(0.0, progress_m / planned_route_m))
        if progress_m is not None and planned_route_m
        else _as_float(snapshot.get("route_completion_fraction"))
    )
    terrain = _as_float(snapshot.get("terrain_elevation_m"))
    clearance = _as_float(snapshot.get("terrain_clearance_m"))
    alt_home = _as_float(snapshot.get("altitude_above_home_m"))
    first_terrain = samples[0]["terrain_elevation_m"]
    current_amsl = (
        terrain + clearance
        if terrain is not None and clearance is not None
        else first_terrain + alt_home
        if alt_home is not None
        else None
    )

    plotted_values = [
        value
        for value in [*terrain_values, *target_values, current_amsl]
        if value is not None
    ]
    if not plotted_values:
        return None
    vmin = min(plotted_values)
    vmax = max(plotted_values)
    if math.isclose(vmin, vmax):
        vmin -= 1.0
        vmax += 1.0
    pad = max((vmax - vmin) * 0.08, 1.0)
    vmin -= pad
    vmax += pad

    def row_for(value: float) -> int:
        ratio = (value - vmin) / max(vmax - vmin, 1e-9)
        return min(max(round((height - 1) * (1.0 - ratio)), 0), height - 1)

    grid: list[list[tuple[str, str]]] = [
        [(" ", "")] * width for _ in range(height)
    ]
    for col, value in enumerate(terrain_values):
        if value is not None:
            grid[row_for(value)][col] = ("▁", "green")
    for col, value in enumerate(target_values):
        if value is not None:
            row = row_for(value)
            if grid[row][col][0] == " ":
                grid[row][col] = ("·", "cyan")
    if progress_fraction is not None and current_amsl is not None:
        col = min(max(round(progress_fraction * (width - 1)), 0), width - 1)
        grid[row_for(current_amsl)][col] = ("◆", "bold red")

    body = Text()
    for row in range(height):
        for col in range(width):
            char, style = grid[row][col]
            body.append(char, style=style)
        if row != height - 1:
            body.append("\n")

    footer = (
        f"progress={_fmt_metres(progress_m)} / {_fmt_metres(planned_route_m)}  "
        f"terrain={_fmt_metres(terrain)} AMSL  AGL={_fmt_metres(clearance)}  "
        f"drone={_fmt_metres(current_amsl)} AMSL"
    )
    body.append(f"\n{footer}", style="dim")
    body.append("\n")
    body.append("▁=terrain AMSL  ·=target altitude  ◆=drone AMSL", style="dim")
    return Panel(
        body,
        title="Altitude Profile (horizontal=route progress / vertical=AMSL)",
        border_style="magenta",
    )


def _watch_planned_route_points(artifacts: dict[str, Any]) -> list[tuple[float, float]]:
    route = _mission_map_latlon_from_route(artifacts)
    if route is None:
        dropoff = _dropoff_ned_from_route(artifacts)
        return [(0.0, 0.0), dropoff] if dropoff is not None else [(0.0, 0.0)]
    takeoff_lat, takeoff_lon, dropoff_lat, dropoff_lon = route
    planned_points = _mission_map_planned_points(
        artifacts,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
        dropoff_lat=dropoff_lat,
        dropoff_lon=dropoff_lon,
    )
    local_points: list[tuple[float, float]] = []
    for point in planned_points:
        lat = _as_float(point.get("lat"))
        lon = _as_float(point.get("lon"))
        if lat is None or lon is None:
            continue
        local_points.append(
            _mission_map_latlon_to_local(
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
                lat=lat,
                lon=lon,
            )
        )
    return local_points


def _watch_overlay_status_text(
    *,
    planned_points: list[tuple[float, float]],
    obstacle_records: list[dict[str, Any]],
    maneuver: dict[str, Any],
) -> str | None:
    parts: list[str] = []
    if planned_points:
        parts.append(f"planned={len(planned_points)}pts")
    if obstacle_records:
        spawned = [record.get("spawned") for record in obstacle_records]
        if any(value is True for value in spawned):
            spawn_status = "spawned"
        elif all(value is False for value in spawned):
            spawn_status = "not_spawned"
        else:
            spawn_status = "unknown"
        parts.append(f"obstacles={len(obstacle_records)}({spawn_status})")
    if maneuver:
        samples = maneuver.get("samples")
        samples_count = len(samples) if isinstance(samples, list) else 0
        parts.append(
            "avoid="
            f"{_status_text(maneuver.get('status'))}"
            f"/target={_status_text(maneuver.get('target_reached'))}"
            f"/resume={_status_text(maneuver.get('resume_auto_status'))}"
            f"/samples={samples_count}"
        )
    return "overlay: " + " · ".join(parts) if parts else None


def _indoor_xy_points(records: Any) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    if not isinstance(records, list):
        return points
    for record in records:
        if not isinstance(record, dict):
            continue
        x_m = _as_float(record.get("x_m"))
        y_m = _as_float(record.get("y_m"))
        if x_m is None or y_m is None:
            continue
        points.append((x_m, y_m))
    return points


def _project_indoor_xy_points(
    points: list[tuple[float, float]],
    *,
    width: int,
    height: int,
) -> list[tuple[int, int]]:
    """Project ROS local-XY points onto a terminal grid.

    Unlike the PX4/NED map, TurtleBot3 indoor maps use x to the right and y up.
    Terminal rows are roughly twice as tall as columns, so vertical scale uses the
    same compensation as the flight map projection.
    """
    if not points:
        return []
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    span_x = max(xmax - xmin, 1e-6)
    span_y = max(ymax - ymin, 1e-6)
    scale = max(span_x / max(width - 1, 1), span_y / max((height - 1) * 2, 1)) or 1.0
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    projected: list[tuple[int, int]] = []
    for x_m, y_m in points:
        col = round((width - 1) / 2.0 + (x_m - cx) / scale)
        row = round((height - 1) / 2.0 - (y_m - cy) / (scale * 2.0))
        col = min(max(col, 0), width - 1)
        row = min(max(row, 0), height - 1)
        projected.append((row, col))
    return projected


TURTLEBOT3_MAP_ICON = "🐢"


def _render_turtlebot3_indoor_map(
    *,
    indoor_map: dict[str, Any],
    status: str,
    task_id: str,
) -> Group:
    robot_label = _status_text(indoor_map.get("robot_label"), "TurtleBot3")
    planned_records = indoor_map.get("planned_points")
    planned_records = planned_records if isinstance(planned_records, list) else []
    observed_records = indoor_map.get("observed_points")
    observed_records = observed_records if isinstance(observed_records, list) else []
    obstacle_records = indoor_map.get("obstacles")
    obstacle_records = obstacle_records if isinstance(obstacle_records, list) else []
    floor_plan = indoor_map.get("floor_plan")
    floor_plan = floor_plan if isinstance(floor_plan, dict) else {}
    furniture_records = floor_plan.get("furniture")
    furniture_records = furniture_records if isinstance(furniture_records, list) else []
    recovery = indoor_map.get("recovery")
    recovery = recovery if isinstance(recovery, dict) else {}
    recovery_records = recovery.get("observed_points")
    recovery_records = recovery_records if isinstance(recovery_records, list) else []
    recovery_target = recovery.get("target")
    recovery_target_records = [recovery_target] if isinstance(recovery_target, dict) else []
    live_records = indoor_map.get("live_display_points")
    live_records = live_records if isinstance(live_records, list) else []
    live_telemetry = indoor_map.get("live_telemetry")
    live_telemetry = live_telemetry if isinstance(live_telemetry, dict) else {}
    live_preview_ended = live_telemetry.get("telemetry_status") == "ended"
    current_pose = indoor_map.get("current_pose")
    current_pose_records = [current_pose] if isinstance(current_pose, dict) else []

    planned_points = _indoor_xy_points(planned_records)
    observed_points = _indoor_xy_points(observed_records)
    obstacle_points = _indoor_xy_points(obstacle_records)
    furniture_points = _indoor_xy_points(furniture_records)
    recovery_points = _indoor_xy_points(recovery_records)
    recovery_target_points = _indoor_xy_points(recovery_target_records)
    live_points = _indoor_xy_points(live_records)
    current_pose_points = _indoor_xy_points(current_pose_records)
    obstacle_record = (
        obstacle_records[0]
        if obstacle_records and isinstance(obstacle_records[0], dict)
        else {}
    )
    anchors = [
        *planned_points,
        *observed_points,
        *furniture_points,
        *obstacle_points,
        *recovery_points,
        *recovery_target_points,
        *live_points,
        *current_pose_points,
    ]
    if not anchors:
        anchors = [(0.0, 0.0)]
    projected = _project_indoor_xy_points(
        anchors,
        width=FLIGHT_MAP_WIDTH,
        height=FLIGHT_MAP_HEIGHT,
    )
    sections: dict[str, tuple[int, int]] = {}
    cursor = 0
    for name, points in (
        ("planned", planned_points),
        ("observed", observed_points),
        ("furniture", furniture_points),
        ("obstacles", obstacle_points),
        ("recovery", recovery_points),
        ("recovery_target", recovery_target_points),
        ("live", live_points),
        ("current_pose", current_pose_points),
    ):
        sections[name] = (cursor, cursor + len(points))
        cursor += len(points)

    def section(name: str) -> list[tuple[int, int]]:
        start, end = sections.get(name, (0, 0))
        return projected[start:end]

    grid: list[list[tuple[str, str]]] = [
        [(" ", "")] * FLIGHT_MAP_WIDTH for _ in range(FLIGHT_MAP_HEIGHT)
    ]
    for row, col in section("planned"):
        grid[row][col] = ("p", "cyan")
    for index, (row, col) in enumerate(section("observed")):
        style = "green" if index >= max(0, len(observed_points) - 12) else "grey42"
        grid[row][col] = ("·", style)
    for (row, col), record in zip(section("furniture"), furniture_records, strict=False):
        label = str(record.get("label") or record.get("kind") or "f").lower()
        char = "F"
        if "sofa" in label:
            char = "S"
        elif "table" in label:
            char = "T"
        elif "book" in label:
            char = "B"
        elif "counter" in label:
            char = "C"
        grid[row][col] = (char, "bold white")
    for row, col in section("recovery"):
        grid[row][col] = ("r", "bright_magenta")
    for row, col in section("obstacles"):
        grid[row][col] = ("O", "bold red")
    for row, col in section("recovery_target"):
        grid[row][col] = ("R", "bold magenta")
    for row, col in section("live"):
        grid[row][col] = (
            "·",
            "dim green" if live_preview_ended else "bright_green",
        )
    if planned_points:
        home_row, home_col = section("planned")[0]
        grid[home_row][home_col] = ("H", "bold blue")
        drop_row, drop_col = section("planned")[-1]
        grid[drop_row][drop_col] = ("D", "bold yellow")
    if live_points and not live_preview_ended:
        robot_row, robot_col = section("live")[-1]
        grid[robot_row][robot_col] = (TURTLEBOT3_MAP_ICON, "bold bright_green")
    elif current_pose_points:
        robot_row, robot_col = section("current_pose")[-1]
        grid[robot_row][robot_col] = (TURTLEBOT3_MAP_ICON, "bold green")
    elif recovery_points:
        robot_row, robot_col = section("recovery")[-1]
        grid[robot_row][robot_col] = (TURTLEBOT3_MAP_ICON, "bold green")
    elif observed_points:
        robot_row, robot_col = section("observed")[-1]
        grid[robot_row][robot_col] = (TURTLEBOT3_MAP_ICON, "bold green")

    body = Text()
    for row in range(FLIGHT_MAP_HEIGHT):
        for col in range(FLIGHT_MAP_WIDTH):
            char, style = grid[row][col]
            body.append(char, style=style)
        if row != FLIGHT_MAP_HEIGHT - 1:
            body.append("\n")

    motion = indoor_map.get("motion")
    motion = motion if isinstance(motion, dict) else {}
    room = indoor_map.get("room_boundary")
    room = room if isinstance(room, dict) else {}
    alignment = indoor_map.get("display_alignment")
    alignment = alignment if isinstance(alignment, dict) else {}
    live_twist = live_telemetry.get("twist")
    live_twist = live_twist if isinstance(live_twist, dict) else {}
    recovery_phase = _status_text(recovery.get("runtime_status"))
    hud = Text.from_markup(
        f"[bold]task[/bold]={task_id}  [bold]status[/bold]={status}  "
        f"[bold]mission[/bold]={_status_text(indoor_map.get('mission_kind'))}\n"
        f"frame={_status_text(indoor_map.get('frame_id'), 'map')}  "
        f"planned={len(planned_points)}pts  observed={len(observed_points)}pts  "
        f"recovery_observed={len(recovery_points)}pts  "
        f"obstacles={len(obstacle_points)}  furniture={len(furniture_points)}  "
        f"recovery={_status_text(recovery.get('triggered'))}\n"
        f"recovery_phase={recovery_phase or '-'}  "
        f"recovery_action={_status_text(recovery.get('selected_action')) or '-'}\n"
        f"recovery_goal={_status_text(recovery.get('goal_status')) or '-'}  "
        f"verification={_status_text(recovery.get('verification_status')) or '-'}  "
        f"resume_status={_status_text(recovery.get('route_resume_status')) or '-'}\n"
        f"route_segments={_status_text(recovery.get('route_segment_completion_count')) or '-'}/"
        f"{_status_text(recovery.get('route_segment_planned_count')) or '-'}  "
        f"recovery_complete={_status_text(recovery.get('recovery_completion_claimed')) or '-'}  "
        f"route_resumed={_status_text(recovery.get('route_resumed_after_recovery')) or '-'}\n"
        f"live_preview={_status_text(live_telemetry.get('telemetry_status')) or '-'} "
        "(display-only, not evidence)  "
        f"live_samples={len(live_points)}  "
        f"live_path={_fmt_metres(live_telemetry.get('display_path_length_m'))}  "
        f"linear_x={_status_text(live_twist.get('linear_x_mps')) or '-'}m/s  "
        f"captured_at={_status_text(live_telemetry.get('captured_at')) or '-'}\n"
        f"motion={_status_text(motion.get('robot_motion_observed'))}  "
        f"odom={_fmt_metres(motion.get('odom_delta_m'))}  "
        f"observed_source={_status_text(indoor_map.get('observed_pose_source'))}\n"
        f"obstacle_clearance={_status_text(obstacle_record.get('trajectory_clearance_observed'))}  "
        f"intersects_obstacle={_status_text(obstacle_record.get('trajectory_intersects_obstacle'))}\n"
        f"display_alignment={_status_text(alignment.get('method'))}  "
        f"applied={_status_text(alignment.get('applied'))}  "
        f"dx={_fmt_metres(alignment.get('dx_m'))}  "
        f"dy={_fmt_metres(alignment.get('dy_m'))}\n"
        f"room={_status_text(room.get('source'))}; physical_execution_invoked=false\n"
        f"floor_plan={_status_text(floor_plan.get('floor_plan_id'))}; "
        "furniture is display-only unless separately spawned\n"
        f"[blue]H[/blue]=home  [yellow]D[/yellow]=dropoff  [bright_green]{TURTLEBOT3_MAP_ICON}[/bright_green]=live preview marker  "
        "[cyan]p[/cyan]=plan  [green]·[/green]=persisted observed odom  "
        "[dim green]·[/dim green]=live odom preview (not evidence)  "
        "[white]S/T/B/C[/white]=sofa/table/bookshelf/counter  "
        "[bright_magenta]r/R[/bright_magenta]=recovery path/target  [red]O[/red]=obstacle"
    )
    return Group(
        Panel(
            body,
            title=(
                f"MissionOS Indoor Map ({robot_label}/Nav2 sim · "
                "right=+map x top=+map y)"
            ),
            border_style="cyan",
        ),
        hud,
    )


def _render_flight_map(
    *,
    trail: list[tuple[float, float]],
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
    status: str,
    task_id: str,
) -> Group:
    dropoff = _dropoff_ned_from_route(artifacts)
    planned_points = _watch_planned_route_points(artifacts)
    obstacle_records = _mission_obstacle_records_from_artifacts(artifacts)
    obstacle_points = [
        (float(record["x_m"]), float(record["y_m"]))
        for record in obstacle_records
        if record.get("x_m") is not None and record.get("y_m") is not None
    ]
    maneuver = _operator_recovery_local_maneuver_model(
        artifacts=artifacts,
        snapshot=snapshot,
    )
    maneuver_samples = [
        (float(sample["x_m"]), float(sample["y_m"]))
        for sample in maneuver.get("samples") or []
        if sample.get("x_m") is not None and sample.get("y_m") is not None
    ]
    maneuver_target = maneuver.get("target") if isinstance(maneuver, dict) else None
    maneuver_target_point = (
        (float(maneuver_target["x_m"]), float(maneuver_target["y_m"]))
        if isinstance(maneuver_target, dict)
        and maneuver_target.get("x_m") is not None
        and maneuver_target.get("y_m") is not None
        else None
    )
    anchors: list[tuple[float, float]] = []
    sections: dict[str, tuple[int, int]] = {}

    def add_section(name: str, points: list[tuple[float, float]]) -> None:
        start = len(anchors)
        anchors.extend(points)
        sections[name] = (start, len(anchors))

    add_section("planned", planned_points)
    add_section("trail", list(trail))
    add_section("home", [(0.0, 0.0)])
    if dropoff is not None:
        add_section("dropoff", [dropoff])
    add_section("obstacles", obstacle_points)
    add_section("maneuver_samples", maneuver_samples)
    if maneuver_target_point is not None:
        add_section("maneuver_target", [maneuver_target_point])
    projected = _project_flight_points(
        anchors, width=FLIGHT_MAP_WIDTH, height=FLIGHT_MAP_HEIGHT
    )

    def projected_section(name: str) -> list[tuple[int, int]]:
        start, end = sections.get(name, (0, 0))
        return projected[start:end]

    grid: list[list[tuple[str, str]]] = [
        [(" ", "")] * FLIGHT_MAP_WIDTH for _ in range(FLIGHT_MAP_HEIGHT)
    ]
    for row, col in projected_section("planned"):
        grid[row][col] = ("p", "cyan")
    n_trail = len(trail)
    for idx, (row, col) in enumerate(projected_section("trail")):
        # Older path dim, recent path brighter green.
        style = "green" if idx >= n_trail - 12 else "grey42"
        grid[row][col] = ("·", style)
    for row, col in projected_section("maneuver_samples"):
        grid[row][col] = ("a", "bright_yellow")
    for row, col in projected_section("obstacles"):
        grid[row][col] = ("O", "bold red")
    for row, col in projected_section("maneuver_target"):
        grid[row][col] = ("A", "bold yellow")
    home_row, home_col = projected_section("home")[0]
    grid[home_row][home_col] = ("H", "bold blue")
    if dropoff is not None:
        d_row, d_col = projected_section("dropoff")[0]
        grid[d_row][d_col] = ("D", "bold yellow")
    if n_trail:
        dr, dc = projected_section("trail")[-1]
        grid[dr][dc] = ("◆", "bold red")

    body = Text()
    for row in range(FLIGHT_MAP_HEIGHT):
        for col in range(FLIGHT_MAP_WIDTH):
            char, style = grid[row][col]
            body.append(char, style=style)
        if row != FLIGHT_MAP_HEIGHT - 1:
            body.append("\n")

    battery_model = _mission_map_battery_model(
        snapshot=snapshot,
        artifacts=artifacts,
    )
    battery = _format_percent(battery_model.get("display_percent"))
    battery_detail = (
        f"source={_status_text(battery_model.get('source'), 'unknown')}  "
        f"status={_status_text(battery_model.get('status'))}  "
        f"sample={_status_text(battery_model.get('sample_index'))}  "
        f"observed_at={_status_text(battery_model.get('observed_at'))}"
    )
    if battery_model.get("reset_detected") is True:
        battery_detail += (
            "  reported="
            f"{_format_percent(battery_model.get('reported_percent'))} rejected_reset="
            f"+{round(float(battery_model.get('reset_delta_percent') or 0.0), 1)}pp"
        )
    reached = _status_text(_as_int(snapshot.get("mission_reached_seq")))
    total = _status_text(_as_int(snapshot.get("waypoint_total")))
    home_dist = snapshot.get("distance_to_home_m")
    title = "MissionOS Live Map (SITL · top=North right=East)"
    process_status = _watch_process_status(artifacts=artifacts, snapshot=snapshot)
    process_line = f"{process_status}\n" if process_status else ""
    monitor_ended = snapshot.get("monitor_window_ended") is True or (
        snapshot.get("snapshot_status") == "monitor_window_ended"
    )
    recovery_hint = _operator_recovery_dispatch_status_text(
        artifacts=artifacts,
        snapshot=snapshot,
        compact=True,
    )
    if recovery_hint is None and status == "running" and not monitor_ended:
        recovery_hint = _operator_recovery_dispatch_hint(
            task_id=task_id,
            action=_runtime_recovery_agent_action(artifacts),
            parameters=_runtime_recovery_agent_parameters(artifacts),
            compact=True,
        )
    recovery_line = f"{recovery_hint}\n" if recovery_hint else ""
    overlay_status = _watch_overlay_status_text(
        planned_points=planned_points,
        obstacle_records=obstacle_records,
        maneuver=maneuver,
    )
    overlay_line = f"{overlay_status}\n" if overlay_status else ""
    hud = Text.from_markup(
        f"[bold]task[/bold]={task_id}  [bold]status[/bold]={status}\n"
        f"{process_line}"
        f"{recovery_line}"
        f"{overlay_line}"
        f"{_watch_altitude_status(snapshot)}\n"
        f"battery={battery}  {battery_detail}\n"
        f"wp={reached}/{total}  home_dist={_fmt_metres(home_dist)}\n"
        "[blue]H[/blue]=home  [yellow]D[/yellow]=dropoff  [red]◆[/red]=drone  "
        "[red]X/![/red]=blocked dropoff / drone at blocked dropoff  "
        "[cyan]p[/cyan]=initial plan  [green]·[/green]=observed  "
        "[bright_yellow]a/A[/bright_yellow]=avoid path/target  [red]O[/red]=obstacle"
    )
    profile = _render_elevation_profile(snapshot=snapshot, artifacts=artifacts)
    if profile is not None:
        return Group(Panel(body, title=title, border_style="cyan"), profile, hud)
    return Group(Panel(body, title=title, border_style="cyan"), hud)


MISSION_MAP_OUTPUT_DIR = Path("output/missionos_maps")
MISSION_MAP_POLL_INTERVAL = 1.0
MISSION_MAP_PROVIDERS: dict[str, dict[str, str]] = {
    "osm": {
        "label": "OpenStreetMap",
        "url_template": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors",
        "attribution_url": "https://www.openstreetmap.org/copyright",
    },
    "gsi": {
        "label": "GSI Maps",
        "url_template": "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png",
        "attribution": "GSI Tiles",
        "attribution_url": "https://maps.gsi.go.jp/development/ichiran.html",
    },
}


def _mission_map_latlon_from_route(
    artifacts: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    takeoff_lat = _first_numeric(
        route.get("takeoff_latitude"), route.get("takeoff_latitude_deg")
    )
    takeoff_lon = _first_numeric(
        route.get("takeoff_longitude"), route.get("takeoff_longitude_deg")
    )
    dropoff_lat = _first_numeric(
        route.get("dropoff_latitude"), route.get("dropoff_latitude_deg")
    )
    dropoff_lon = _first_numeric(
        route.get("dropoff_longitude"), route.get("dropoff_longitude_deg")
    )
    if None in (takeoff_lat, takeoff_lon, dropoff_lat, dropoff_lon):
        return None
    return (
        float(takeoff_lat),
        float(takeoff_lon),
        float(dropoff_lat),
        float(dropoff_lon),
    )


def _mission_map_local_to_latlon(
    *,
    takeoff_lat: float,
    takeoff_lon: float,
    north_m: float,
    east_m: float,
) -> tuple[float, float]:
    lat = takeoff_lat + north_m / 111320.0
    lon_scale = max(1e-9, 111320.0 * math.cos(math.radians(takeoff_lat)))
    lon = takeoff_lon + east_m / lon_scale
    return lat, lon


def _mission_map_sample_latlon(
    sample: dict[str, Any],
    *,
    takeoff_lat: float,
    takeoff_lon: float,
) -> tuple[float, float, str] | None:
    lat = _first_numeric(
        sample.get("latitude_deg"),
        sample.get("global_latitude_deg"),
        sample.get("lat"),
        sample.get("latitude"),
    )
    lon = _first_numeric(
        sample.get("longitude_deg"),
        sample.get("global_longitude_deg"),
        sample.get("lon"),
        sample.get("longitude"),
    )
    if lat is not None and lon is not None:
        return float(lat), float(lon), "observed_wgs84"
    north = _first_numeric(sample.get("local_x_m"), sample.get("x_m"), sample.get("x"))
    east = _first_numeric(sample.get("local_y_m"), sample.get("y_m"), sample.get("y"))
    if north is None or east is None:
        return None
    lat, lon = _mission_map_local_to_latlon(
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
        north_m=float(north),
        east_m=float(east),
    )
    return lat, lon, "estimated_from_local_ned"


def _mission_map_flight_samples(artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    for key in (
        "missionos_auto_mission_runtime_replay",
        "auto_mission_runtime_replay",
        "px4_gazebo_mission_designer_sitl_live_flight_run",
        "mission_designer_live_telemetry_snapshot",
    ):
        candidate = artifacts.get(key)
        candidate = candidate if isinstance(candidate, dict) else {}
        for samples_key in (
            "flight_path_profile",
            "position_profile",
            "route_preview_waypoints",
        ):
            samples = candidate.get(samples_key)
            if isinstance(samples, list) and samples:
                return [sample for sample in samples if isinstance(sample, dict)]
    return []


def _mission_map_live_trajectory_samples(
    artifacts: dict[str, Any],
) -> list[dict[str, Any]]:
    trajectory = artifacts.get("missionos_auto_mission_live_trajectory")
    trajectory = trajectory if isinstance(trajectory, dict) else {}
    samples = trajectory.get("samples")
    if not isinstance(samples, list):
        return []
    return [dict(sample) for sample in samples if isinstance(sample, dict)]


def _mission_map_observed_trace(
    *,
    artifacts: dict[str, Any],
    takeoff_lat: float,
    takeoff_lon: float,
) -> dict[str, Any]:
    """Build observed path segments without inventing telemetry continuity."""

    segments: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    def point(
        sample: dict[str, Any], index: int, source_prefix: str
    ) -> dict[str, Any] | None:
        latlon = _mission_map_sample_latlon(
            sample,
            takeoff_lat=takeoff_lat,
            takeoff_lon=takeoff_lon,
        )
        if latlon is None:
            return None
        lat, lon, source = latlon
        return {
            "lat": lat,
            "lon": lon,
            "source": f"{source_prefix}:{source}",
            "phase": _status_text(sample.get("phase"), f"sample_{index}"),
            "alt_m": _as_float(
                sample.get("relative_alt_m")
                or sample.get("altitude_above_home_m")
                or sample.get("local_z_m")
                or sample.get("z_m")
                or sample.get("z")
            ),
            "elapsed_s": sample.get("elapsed_s")
            or sample.get("elapsed_seconds")
            or sample.get("sample_time_s")
            or sample.get("sample_index"),
            "sample_index": sample.get("sample_index"),
            "segment_index": sample.get("segment_index"),
            "segment_break_reason": _status_text(
                sample.get("segment_break_reason"), ""
            ),
        }

    replay_samples = _mission_map_flight_samples(artifacts)
    live_samples = _mission_map_live_trajectory_samples(artifacts)

    def numeric_indices(samples: list[dict[str, Any]]) -> list[int]:
        return [
            int(value)
            for sample in samples
            if isinstance((value := sample.get("sample_index")), (int, float))
        ]

    replay_indices = numeric_indices(replay_samples)
    live_indices = numeric_indices(live_samples)
    live_covers_replay = bool(
        live_samples
        and replay_samples
        and len(live_samples) > len(replay_samples)
        and live_indices
        and replay_indices
        and min(live_indices) <= min(replay_indices)
        and max(live_indices) >= max(replay_indices)
    )
    streams: list[tuple[str, list[dict[str, Any]]]] = []
    if live_covers_replay or (live_samples and not replay_samples):
        streams.append(("live_trajectory", live_samples))
        trace_source = "missionos_auto_mission_live_trajectory"
    else:
        if replay_samples:
            streams.append(("runtime_replay", replay_samples))
        max_replay_index = max(replay_indices) if replay_indices else None
        later_live_samples = [
            sample
            for sample in live_samples
            if max_replay_index is None
            or not isinstance(sample.get("sample_index"), (int, float))
            or int(sample["sample_index"]) > max_replay_index
        ]
        if later_live_samples:
            streams.append(("live_trajectory", later_live_samples))
        trace_source = (
            "runtime_replay_with_later_live_segments" if streams else "unavailable"
        )

    def point_distance_m(left: dict[str, Any], right: dict[str, Any]) -> float:
        north_m = (float(right["lat"]) - float(left["lat"])) * 111320.0
        lon_scale = 111320.0 * math.cos(math.radians(takeoff_lat))
        east_m = (float(right["lon"]) - float(left["lon"])) * lon_scale
        return math.hypot(north_m, east_m)

    def elapsed_gap_s(
        left: dict[str, Any], right: dict[str, Any]
    ) -> float | None:
        left_elapsed = _as_float(left.get("elapsed_s"))
        right_elapsed = _as_float(right.get("elapsed_s"))
        if left_elapsed is None or right_elapsed is None:
            return None
        return max(0.0, right_elapsed - left_elapsed)

    previous_stream_last: dict[str, Any] | None = None
    for source, samples in streams:
        current_points: list[dict[str, Any]] = []
        current_segment_index: int | None = None
        current_break_reason = ""

        def finish_segment() -> None:
            nonlocal current_points, current_break_reason
            if not current_points:
                return
            segments.append(
                {
                    "points": current_points,
                    "source": source,
                    "segment_index": current_segment_index,
                    "break_reason": current_break_reason,
                }
            )
            current_points = []
            current_break_reason = ""

        for index, sample in enumerate(samples):
            mapped = point(sample, index, source)
            if mapped is None:
                continue
            mapped_segment_index = (
                int(sample["segment_index"])
                if isinstance(sample.get("segment_index"), (int, float))
                else None
            )
            previous = current_points[-1] if current_points else previous_stream_last
            source_transition = bool(previous is not None and not current_points)
            explicit_break = bool(sample.get("segment_break_reason")) or bool(
                current_points
                and mapped_segment_index is not None
                and current_segment_index is not None
                and mapped_segment_index != current_segment_index
            )
            distance_m = point_distance_m(previous, mapped) if previous else 0.0
            gap_seconds = elapsed_gap_s(previous, mapped) if previous else None
            inferred_gap = bool(
                previous
                and gap_seconds is not None
                and gap_seconds > 5.0
                and distance_m > 10.0
            )
            if explicit_break or inferred_gap or source_transition:
                if current_points:
                    finish_segment()
                reason = _status_text(sample.get("segment_break_reason"), "")
                if not reason:
                    reason = (
                        "source_transition_not_observed"
                        if source_transition
                        else "telemetry_time_and_distance_gap"
                    )
                current_break_reason = reason
                if previous and (inferred_gap or distance_m > 10.0):
                    gaps.append(
                        {
                            "from": previous,
                            "to": mapped,
                            "reason": reason,
                            "distance_m": round(distance_m, 3),
                            "elapsed_gap_s": (
                                round(gap_seconds, 3)
                                if gap_seconds is not None
                                else None
                            ),
                            "from_sample_index": previous.get("sample_index"),
                            "to_sample_index": mapped.get("sample_index"),
                            "evidence_status": "not_observed_between_endpoints",
                        }
                    )
            if not current_points:
                current_segment_index = mapped_segment_index
            current_points.append(mapped)
        finish_segment()
        if segments:
            points = segments[-1].get("points") or []
            if points:
                previous_stream_last = points[-1]

    return {
        "segments": segments,
        "gaps": gaps,
        "source": trace_source,
        "live_trajectory_preferred": live_covers_replay,
    }


def _mission_map_battery_model(
    *, snapshot: dict[str, Any], artifacts: dict[str, Any]
) -> dict[str, Any]:
    return battery_truth_model(snapshot=snapshot, artifacts=artifacts)


def _mission_map_recovery_provenance(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    proposal = artifacts.get("missionos_runtime_recovery_last_proposal")
    proposal = proposal if isinstance(proposal, dict) else {}
    origin = proposal.get("proposal_origin")
    origin = origin if isinstance(origin, dict) else {}
    dispatch = artifacts.get("missionos_runtime_recovery_dispatch_request")
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    attempt = artifacts.get("missionos_runtime_recovery_last_attempt")
    attempt = attempt if isinstance(attempt, dict) else {}
    safety = attempt.get("resume_safety_verification")
    safety = safety if isinstance(safety, dict) else {}
    if not any((proposal, dispatch, attempt)):
        return {}
    return {
        "proposal_id": proposal.get("proposal_id") or dispatch.get("proposal_id"),
        "origin_kind": origin.get("origin_kind"),
        "provider": origin.get("provider"),
        "model_id": origin.get("model_id"),
        "invocation_kind": origin.get("invocation_kind"),
        "proposal_origin_sha256": proposal.get("proposal_origin_sha256")
        or dispatch.get("proposal_origin_sha256"),
        "operator_approved": _as_bool(dispatch.get("operator_approved")) is True,
        "explicit_recovery_dispatch_approval": (
            _as_bool(dispatch.get("explicit_recovery_dispatch_approval")) is True
        ),
        "approval_ref": dispatch.get("approval_ref"),
        "recovery_action": attempt.get("recovery_action")
        or dispatch.get("recovery_action"),
        "target_reached": _as_bool(attempt.get("target_reached")) is True,
        "resume_status": attempt.get("resume_status"),
        "resume_mission_current_seq": _as_int(
            safety.get("resume_mission_current_seq")
        ),
        "resume_mission_seq_after_obstacle": _as_int(
            safety.get("resume_mission_seq_after_obstacle")
        ),
        "resume_mission_current_seq_observed": (
            _as_bool(safety.get("resume_mission_current_seq_observed")) is True
        ),
        "simulator_execution_observed": (
            _as_bool(attempt.get("simulator_execution_observed")) is True
        ),
        "source_refs": [
            "missionos_runtime_recovery_last_proposal.proposal_origin",
            "missionos_runtime_recovery_dispatch_request",
            "missionos_runtime_recovery_last_attempt.resume_safety_verification",
        ],
        "claim_boundary": (
            "Recovery provenance is display evidence only. Proposal, approval, "
            "dispatch, execution, and verification remain distinct source facts."
        ),
    }


def _mission_map_obstacles(
    artifacts: dict[str, Any],
    *,
    takeoff_lat: float,
    takeoff_lon: float,
    dropoff_lat: float,
    dropoff_lon: float,
) -> list[dict[str, Any]]:
    obstacles: list[dict[str, Any]] = []
    for record in _mission_obstacle_records_from_artifacts(artifacts):
        x_m = _as_float(record.get("x_m"))
        y_m = _as_float(record.get("y_m"))
        if x_m is None or y_m is None:
            continue
        lat, lon = _mission_map_local_to_latlon(
            takeoff_lat=takeoff_lat,
            takeoff_lon=takeoff_lon,
            north_m=x_m,
            east_m=y_m,
        )
        half_x_m = (_as_float(record.get("size_x_m")) or 0.0) / 2.0
        half_y_m = (_as_float(record.get("size_y_m")) or 0.0) / 2.0
        footprint = []
        if half_x_m > 0.0 and half_y_m > 0.0:
            for corner_x_m, corner_y_m in (
                (x_m - half_x_m, y_m - half_y_m),
                (x_m - half_x_m, y_m + half_y_m),
                (x_m + half_x_m, y_m + half_y_m),
                (x_m + half_x_m, y_m - half_y_m),
            ):
                corner_lat, corner_lon = _mission_map_local_to_latlon(
                    takeoff_lat=takeoff_lat,
                    takeoff_lon=takeoff_lon,
                    north_m=corner_x_m,
                    east_m=corner_y_m,
                )
                footprint.append({"lat": corner_lat, "lon": corner_lon})
        obstacle_z_m = _as_float(record.get("z_m"))
        obstacle_height_m = _as_float(record.get("size_z_m"))
        obstacles.append(
            {
                **record,
                "lat": lat,
                "lon": lon,
                "footprint": footprint,
                "top_altitude_m": (
                    obstacle_z_m + obstacle_height_m / 2.0
                    if obstacle_z_m is not None and obstacle_height_m is not None
                    else obstacle_height_m
                ),
                "source": _status_text(record.get("source")),
                "coincident_with_dropoff": (
                    math.hypot(
                        (lat - dropoff_lat) * 111320.0,
                        (lon - dropoff_lon)
                        * 111320.0
                        * math.cos(math.radians(takeoff_lat)),
                    )
                    <= max(3.0, half_x_m, half_y_m)
                ),
            }
        )
    return obstacles


def _mission_map_maneuver(
    *,
    artifacts: dict[str, Any],
    snapshot: dict[str, Any],
    takeoff_lat: float,
    takeoff_lon: float,
) -> dict[str, Any]:
    maneuver = _operator_recovery_local_maneuver_model(
        artifacts=artifacts,
        snapshot=snapshot,
    )
    if not maneuver:
        return {}
    samples: list[dict[str, Any]] = []
    for sample in maneuver.get("samples") or []:
        x_m = _as_float(sample.get("x_m"))
        y_m = _as_float(sample.get("y_m"))
        if x_m is None or y_m is None:
            continue
        lat, lon = _mission_map_local_to_latlon(
            takeoff_lat=takeoff_lat,
            takeoff_lon=takeoff_lon,
            north_m=x_m,
            east_m=y_m,
        )
        samples.append({**sample, "lat": lat, "lon": lon})
    target = maneuver.get("target")
    target_point = None
    if isinstance(target, dict):
        x_m = _as_float(target.get("x_m"))
        y_m = _as_float(target.get("y_m"))
        if x_m is not None and y_m is not None:
            lat, lon = _mission_map_local_to_latlon(
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
                north_m=x_m,
                east_m=y_m,
            )
            target_point = {**target, "lat": lat, "lon": lon}
    start = maneuver.get("start")
    start_point = None
    if isinstance(start, dict):
        x_m = _as_float(start.get("x_m"))
        y_m = _as_float(start.get("y_m"))
        if x_m is not None and y_m is not None:
            lat, lon = _mission_map_local_to_latlon(
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
                north_m=x_m,
                east_m=y_m,
            )
            start_point = {**start, "lat": lat, "lon": lon}
    return {
        **maneuver,
        "start": start_point,
        "target": target_point,
        "samples": samples,
    }


def _mission_map_telemetry_model(
    *,
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    alt_home = _as_float(snapshot.get("altitude_above_home_m"))
    terrain = _as_float(snapshot.get("terrain_elevation_m"))
    agl = _as_float(snapshot.get("terrain_clearance_m"))
    agl_target = _as_float(snapshot.get("terrain_clearance_target_m"))
    agl_margin = _as_float(snapshot.get("terrain_clearance_margin_m"))
    samples, _planned_route_m = _terrain_profile_samples_for_watch(artifacts)
    first_terrain = samples[0]["terrain_elevation_m"] if samples else None
    current_amsl = (
        terrain + agl
        if terrain is not None and agl is not None
        else first_terrain + alt_home
        if first_terrain is not None and alt_home is not None
        else None
    )
    destination_target_amsl = next(
        (
            sample.get("target_amsl_m")
            for sample in reversed(samples)
            if sample.get("target_amsl_m") is not None
        ),
        None,
    )
    climb_to_destination = (
        destination_target_amsl - current_amsl
        if destination_target_amsl is not None and current_amsl is not None
        else None
    )
    return {
        "altitude_amsl_m": current_amsl,
        "home_relative_altitude_m": alt_home,
        "terrain_elevation_amsl_m": terrain,
        "agl_m": agl,
        "agl_target_m": agl_target,
        "agl_margin_m": agl_margin,
        "agl_status": _status_text(snapshot.get("terrain_clearance_status")),
        "destination_target_amsl_m": destination_target_amsl,
        "climb_to_destination_m": climb_to_destination,
    }


def _mission_map_weather_model(artifacts: dict[str, Any]) -> dict[str, Any]:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, dict) else {}
    keys = (
        "wind_speed_mps",
        "wind_direction_deg",
        "wind_gust_mps",
        "wind_variance",
        "temperature_c",
        "pressure_hpa",
        "precipitation_mm_per_hour",
    )
    if not any(route.get(key) not in (None, "") for key in keys):
        return {}
    return {
        "wind_speed_mps": _as_float(route.get("wind_speed_mps")),
        "wind_direction_deg": _as_float(route.get("wind_direction_deg")),
        "wind_gust_mps": _as_float(route.get("wind_gust_mps")),
        "wind_variance": _status_text(route.get("wind_variance")),
        "temperature_c": _as_float(route.get("temperature_c")),
        "pressure_hpa": _as_float(route.get("pressure_hpa")),
        "precipitation_mm_per_hour": _as_float(route.get("precipitation_mm_per_hour")),
    }


def _turtlebot3_indoor_map_model_from_artifacts(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    # Progress callbacks update summary first while a synchronous Nav2 dispatch
    # is still running. Prefer that newest copy; the top-level artifact remains
    # the previous stable snapshot until finalization.
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    summary_embedded = summary.get("turtlebot3_indoor_map_model")
    if isinstance(summary_embedded, dict):
        return _repair_turtlebot3_indoor_map_display_alignment(
            dict(summary_embedded)
        )
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    embedded = execution.get("turtlebot3_indoor_map_model")
    if isinstance(embedded, dict):
        return _repair_turtlebot3_indoor_map_display_alignment(dict(embedded))
    direct = artifacts.get("turtlebot3_indoor_map_model")
    if isinstance(direct, dict):
        return _repair_turtlebot3_indoor_map_display_alignment(dict(direct))
    return {}


def _normalize_turtlebot_robot_profile(raw: Any) -> str:
    profile = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if profile in {"turtlebot4", "tb4"}:
        return "turtlebot4"
    if profile in {"nova_carter", "novacarter", "carter", "isaac_carter"}:
        return "nova_carter"
    if profile in {"turtlebot3", "tb3"}:
        return "turtlebot3"
    return ""


def _turtlebot_robot_profile_from_artifacts(artifacts: dict[str, Any]) -> str:
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    indoor_map = _turtlebot3_indoor_map_model_from_artifacts(artifacts)
    for source in (summary, execution, indoor_map):
        profile = _normalize_turtlebot_robot_profile(source.get("robot_profile"))
        if profile:
            return profile
    for source in (summary, execution, indoor_map):
        target = str(source.get("execution_target") or "").strip().lower()
        if target == "ros2_nav2_turtlebot4_sim":
            return "turtlebot4"
        if target == "isaac_ros_nav2_nova_carter_sim":
            return "nova_carter"
        if target == "ros2_nav2_turtlebot3_sim":
            return "turtlebot3"
    return "turtlebot3" if indoor_map else ""


def _turtlebot_robot_label_from_profile(profile: str) -> str:
    normalized = _normalize_turtlebot_robot_profile(profile)
    if normalized == "turtlebot4":
        return "TurtleBot4"
    if normalized == "nova_carter":
        return "Nova Carter"
    return "TurtleBot3"


def _turtlebot_robot_label_from_artifacts(artifacts: dict[str, Any]) -> str:
    return _turtlebot_robot_label_from_profile(
        _turtlebot_robot_profile_from_artifacts(artifacts)
    )


def _turtlebot3_recovery_candidate_resolution_from_artifacts(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    """Return the candidate resolution bound to the newest task summary.

    Recovery revisions update the summary/execution copies atomically while the
    top-level copy may still describe the superseded checkpoint. Presence of an
    empty current value is meaningful (for example, a return-home follow-up), so
    it must suppress rather than fall through to an older direct artifact.
    """

    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    revision_geometry = checkpoint.get("recovery_revision_geometry")
    revision_geometry = (
        revision_geometry if isinstance(revision_geometry, dict) else {}
    )
    for owner in (summary, execution, revision_geometry, artifacts):
        if "recovery_candidate_resolution" not in owner:
            continue
        resolution = owner.get("recovery_candidate_resolution")
        return dict(resolution) if isinstance(resolution, dict) else {}
    return {}


def _turtlebot3_xy(point: dict[str, Any], *, raw: bool = False) -> tuple[float, float] | None:
    x_key = "raw_x_m" if raw else "x_m"
    y_key = "raw_y_m" if raw else "y_m"
    x = _as_float(point.get(x_key))
    y = _as_float(point.get(y_key))
    if x is None or y is None:
        return None
    return x, y


def _turtlebot3_indoor_map_dropoff_xy(
    indoor_map: dict[str, Any],
) -> tuple[float, float] | None:
    planned = indoor_map.get("planned_points")
    if not isinstance(planned, list):
        return None
    candidates = [point for point in planned if isinstance(point, dict)]
    for point in reversed(candidates):
        label = " ".join(
            _status_text(point.get(key)).lower()
            for key in ("phase", "label", "name", "kind", "role")
        )
        if "dropoff" in label:
            xy = _turtlebot3_xy(point)
            if xy is not None:
                return xy
    for point in reversed(candidates):
        xy = _turtlebot3_xy(point)
        if xy is not None:
            return xy
    return None


def _repair_turtlebot3_indoor_map_points(
    points: Any,
) -> list[dict[str, Any]]:
    repaired: list[dict[str, Any]] = []
    if not isinstance(points, list):
        return repaired
    for point in points:
        if not isinstance(point, dict):
            continue
        next_point = dict(point)
        raw_xy = _turtlebot3_xy(next_point, raw=True)
        if raw_xy is not None:
            next_point["x_m"], next_point["y_m"] = raw_xy
            next_point["frame_id"] = "map"
            next_point["display_alignment_applied"] = False
            next_point["display_alignment_repaired_from_raw_fields"] = True
        repaired.append(next_point)
    segment_counts: dict[str, int] = {}
    for point in repaired:
        segment_ref = _status_text(point.get("segment_ref"))
        if segment_ref:
            segment_counts[segment_ref] = segment_counts.get(segment_ref, 0) + 1
    pruned: list[dict[str, Any]] = []
    for point in repaired:
        segment_ref = _status_text(point.get("segment_ref"))
        sample_index = _as_float(point.get("sample_index"))
        if (
            point.get("display_alignment_repaired_from_raw_fields") is True
            and segment_counts.get(segment_ref, 0) > 1
            and sample_index == 0
        ):
            continue
        pruned.append(point)
    return pruned


def _repair_turtlebot3_indoor_map_display_alignment(
    indoor_map: dict[str, Any],
) -> dict[str, Any]:
    """Display-only repair for older maps that aligned map-frame samples as odom.

    Some saved TurtleBot3 artifacts contain mixed odom/map trajectory samples.
    Older display code aligned the whole observed path to home when the first
    sample was odom, which also shifted later map-frame samples. When the raw
    coordinates are clearly closer to the planned dropoff than the displayed
    coordinates, recover the read-only map from the preserved raw fields.
    """

    alignment = indoor_map.get("display_alignment")
    if not isinstance(alignment, dict):
        return indoor_map
    if alignment.get("method") != "first_observed_pose_to_planned_home":
        return indoor_map
    if alignment.get("applied") is not True:
        return indoor_map
    observed = indoor_map.get("observed_points")
    if not isinstance(observed, list) or not observed:
        return indoor_map
    observed_points = [point for point in observed if isinstance(point, dict)]
    if not observed_points:
        return indoor_map
    latest_observed = observed_points[-1]
    latest_display_xy = _turtlebot3_xy(latest_observed)
    latest_raw_xy = _turtlebot3_xy(latest_observed, raw=True)
    dropoff_xy = _turtlebot3_indoor_map_dropoff_xy(indoor_map)
    if (
        latest_display_xy is None
        or latest_raw_xy is None
        or dropoff_xy is None
    ):
        return indoor_map
    display_distance = math.hypot(
        latest_display_xy[0] - dropoff_xy[0],
        latest_display_xy[1] - dropoff_xy[1],
    )
    raw_distance = math.hypot(
        latest_raw_xy[0] - dropoff_xy[0],
        latest_raw_xy[1] - dropoff_xy[1],
    )
    if display_distance < 0.5 or raw_distance + 0.25 >= display_distance:
        return indoor_map

    repaired = dict(indoor_map)
    repaired_observed = _repair_turtlebot3_indoor_map_points(observed_points)
    repaired["observed_points"] = repaired_observed
    recovery = repaired.get("recovery")
    if isinstance(recovery, dict):
        repaired_recovery = dict(recovery)
        repaired_recovery["observed_points"] = _repair_turtlebot3_indoor_map_points(
            recovery.get("observed_points")
        )
        repaired["recovery"] = repaired_recovery
    if repaired_observed:
        latest = dict(repaired_observed[-1])
        latest["source"] = _status_text(
            latest.get("source"),
            "ros2_nav2_bridge_trajectory_samples",
        )
        repaired["current_pose"] = latest
    repaired["display_alignment"] = {
        "applied": False,
        "method": "map_frame_samples_recovered_from_raw_fields",
        "dx_m": 0.0,
        "dy_m": 0.0,
        "previous_method": "first_observed_pose_to_planned_home",
        "previous_dx_m": alignment.get("dx_m"),
        "previous_dy_m": alignment.get("dy_m"),
        "repaired_display_only": True,
        "claim_boundary": (
            "Display-only recovery for older TurtleBot3 indoor maps that "
            "preserved map-frame raw coordinates after odom-origin alignment. "
            "This does not rewrite runtime evidence or change completion claims."
        ),
    }
    return repaired


def _overlay_turtlebot3_live_telemetry(
    indoor_map: dict[str, Any],
    *,
    artifacts: dict[str, Any],
    trail: list[dict[str, Any]],
    alignment_state: dict[str, Any],
    freeze_live_preview: bool = False,
) -> dict[str, Any]:
    """Add a response-only live odom preview without changing artifact evidence.

    ``trail`` belongs to the current watch/map process. A terminal response may
    freeze that process-local trail for operator orientation, but the preview is
    never written back to the task, final artifact, or verifier input. A new
    process (including a page reload) starts with an empty trail and therefore
    shows only persisted observed/recovery evidence.
    """

    telemetry = artifacts.get("turtlebot3_live_telemetry")
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    raw_position = telemetry.get("raw_odom_position")
    raw_position = raw_position if isinstance(raw_position, dict) else {}
    raw_x = _as_float(raw_position.get("x_m"))
    raw_y = _as_float(raw_position.get("y_m"))
    if raw_x is None or raw_y is None:
        if freeze_live_preview and trail:
            frozen = dict(indoor_map)
            frozen["live_display_points"] = [dict(item) for item in trail]
            live_path_length_m = sum(
                math.hypot(
                    float(end.get("raw_x_m") or 0.0)
                    - float(start.get("raw_x_m") or 0.0),
                    float(end.get("raw_y_m") or 0.0)
                    - float(start.get("raw_y_m") or 0.0),
                )
                for start, end in zip(trail, trail[1:])
            )
            frozen["live_telemetry"] = {
                "telemetry_status": "ended",
                "display_path_length_m": round(live_path_length_m, 6),
                "display_path_length_only": True,
                "display_only": True,
                "evidence_status": "not_evidence",
                "persistence": "process_local_response_overlay_only",
            }
            frozen["live_display_alignment"] = {
                "method": "latest_artifact_pose_plus_live_odom_delta",
                "display_only": True,
                "evidence_status": "not_evidence",
                "persistence": "process_local_response_overlay_only",
                "claim_boundary": (
                    "Live preview ended. This frozen process-local projection "
                    "is not persisted, is not verifier input, and is not "
                    "trajectory evidence."
                ),
            }
            return frozen
        return indoor_map
    observed = indoor_map.get("observed_points")
    observed = observed if isinstance(observed, list) else []
    artifact_count = len(observed)
    if (
        alignment_state.get("artifact_observed_count") != artifact_count
        or "raw_anchor_x_m" not in alignment_state
    ):
        anchor = indoor_map.get("current_pose")
        anchor = anchor if isinstance(anchor, dict) else {}
        if not anchor and observed and isinstance(observed[-1], dict):
            anchor = observed[-1]
        map_x = _as_float(anchor.get("x_m"))
        map_y = _as_float(anchor.get("y_m"))
        if map_x is None or map_y is None:
            planned = indoor_map.get("planned_points")
            planned = planned if isinstance(planned, list) else []
            home = planned[0] if planned and isinstance(planned[0], dict) else {}
            map_x = _as_float(home.get("x_m")) or 0.0
            map_y = _as_float(home.get("y_m")) or 0.0
        alignment_state.update(
            {
                "artifact_observed_count": artifact_count,
                "raw_anchor_x_m": raw_x,
                "raw_anchor_y_m": raw_y,
                "map_anchor_x_m": map_x,
                "map_anchor_y_m": map_y,
            }
        )
    display_x = float(alignment_state["map_anchor_x_m"]) + raw_x - float(
        alignment_state["raw_anchor_x_m"]
    )
    display_y = float(alignment_state["map_anchor_y_m"]) + raw_y - float(
        alignment_state["raw_anchor_y_m"]
    )
    point = {
        "x_m": display_x,
        "y_m": display_y,
        "raw_x_m": raw_x,
        "raw_y_m": raw_y,
        "raw_frame_id": str(telemetry.get("frame_id") or "odom"),
        "frame_id": "map",
        "captured_at": telemetry.get("captured_at"),
        "source": "ros2_telemetry_sidecar_live_display",
        "display_only": True,
        "evidence_status": "not_evidence",
        "persistence": "process_local_response_overlay_only",
        "display_alignment_applied": True,
        "display_alignment_method": "latest_artifact_pose_plus_live_odom_delta",
    }
    if not trail or (
        trail[-1].get("raw_x_m"), trail[-1].get("raw_y_m")
    ) != (raw_x, raw_y):
        trail.append(point)
        if len(trail) > _FLIGHT_MAP_TRAIL_LIMIT:
            del trail[: len(trail) - _FLIGHT_MAP_TRAIL_LIMIT]
    overlaid = dict(indoor_map)
    overlaid["live_display_points"] = [dict(item) for item in trail]
    live_path_length_m = sum(
        math.hypot(
            float(end.get("raw_x_m") or 0.0)
            - float(start.get("raw_x_m") or 0.0),
            float(end.get("raw_y_m") or 0.0)
            - float(start.get("raw_y_m") or 0.0),
        )
        for start, end in zip(trail, trail[1:])
    )
    overlaid["live_telemetry"] = {
        **dict(telemetry),
        "telemetry_status": (
            "ended" if freeze_live_preview else telemetry.get("telemetry_status")
        ),
        "display_path_length_m": round(live_path_length_m, 6),
        "display_path_length_only": True,
        "display_only": True,
        "evidence_status": "not_evidence",
        "persistence": "process_local_response_overlay_only",
    }
    overlaid["live_display_alignment"] = {
        "method": "latest_artifact_pose_plus_live_odom_delta",
        "display_only": True,
        "evidence_status": "not_evidence",
        "persistence": "process_local_response_overlay_only",
        "claim_boundary": (
            "Live odom preview is a process-local projection for operator "
            "orientation. It is not persisted, is not verifier input, and does "
            "not alter stored trajectory evidence or completion claims."
        ),
    }
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    recovery = overlaid.get("recovery")
    recovery = dict(recovery) if isinstance(recovery, dict) else {}
    checkpoint_status = str(checkpoint.get("checkpoint_status") or "")
    candidate_resolution = (
        _turtlebot3_recovery_candidate_resolution_from_artifacts(artifacts)
    )
    runtime_status = (
        "awaiting_operator_approval"
        if checkpoint_status == "awaiting_operator_approval"
        else "approved_recovery_and_route_in_progress"
        if checkpoint_status == "dispatching"
        else "recovery_completed_and_route_completed"
        if checkpoint_status == "consumed"
        and summary.get("route_completed_after_recovery") is True
        else "recovery_failed"
        if checkpoint_status in {"failed", "dispatch_unknown"}
        else "not_triggered"
    )
    recovery["runtime_status"] = runtime_status
    recovery["selected_action"] = checkpoint.get("selected_action")
    recovery["checkpoint_status"] = checkpoint_status or None
    recovery["route_segment_completion_count"] = summary.get(
        "segment_completion_count"
    )
    recovery["route_segment_planned_count"] = summary.get("planned_segment_count")
    recovery["recovery_completion_claimed"] = summary.get(
        "recovery_completion_claimed"
    )
    recovery["route_resumed_after_recovery"] = summary.get(
        "route_resumed_after_recovery"
    )
    recovery["goal_status"] = summary.get("recovery_goal_status") or recovery.get(
        "goal_status"
    )
    recovery["verification_status"] = summary.get(
        "recovery_verification_status"
    ) or recovery.get("verification_status")
    recovery["route_resume_status"] = summary.get(
        "route_resume_status"
    ) or recovery.get("route_resume_status")
    selected_candidate = candidate_resolution.get("selected_candidate")
    selected_candidate = (
        selected_candidate if isinstance(selected_candidate, dict) else {}
    )
    recovery["candidate_resolution_status"] = candidate_resolution.get(
        "resolution_status"
    )
    recovery["candidate_id"] = selected_candidate.get("candidate_id")
    recovery["candidate_path_length_m"] = selected_candidate.get("path_length_m")
    overlaid["recovery"] = recovery
    return overlaid


def _mission_indoor_map_model(
    *,
    task_payload: dict[str, Any],
    indoor_map: dict[str, Any],
    live_task_url: str | None,
    poll_interval: float,
) -> dict[str, Any]:
    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    robot_profile = _normalize_turtlebot_robot_profile(
        indoor_map.get("robot_profile")
    ) or _turtlebot_robot_profile_from_artifacts(artifacts)
    robot_label = _status_text(
        indoor_map.get("robot_label"),
        _turtlebot_robot_label_from_profile(robot_profile),
    )
    return {
        **indoor_map,
        "schema_version": "missionos_cli_indoor_map.v1",
        "source_schema_version": indoor_map.get("schema_version"),
        "map_kind": "indoor_local_xy",
        "robot_profile": robot_profile or indoor_map.get("robot_profile"),
        "robot_label": robot_label,
        "task_id": _status_text(task.get("task_id")),
        "task_status": _task_status(task_payload),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": {
            "label": "Indoor local XY",
            "url_template": "",
            "attribution": f"MissionOS {robot_label}/Nav2 simulator evidence",
            "attribution_url": "",
        },
        "live": {
            "enabled": bool(live_task_url),
            "task_url": live_task_url or "",
            "poll_interval_ms": max(500, int(float(poll_interval) * 1000)),
            "terminal_statuses": sorted(TERMINAL_TASK_STATUSES),
        },
        "boundaries": [
            *list(indoor_map.get("claim_boundaries") or []),
            "Indoor map display is read-only and is not a verifier, dispatch control, delivery claim, or physical-execution claim.",
        ],
    }


def _mission_map_model(
    *,
    task_payload: dict[str, Any],
    provider: str,
    live_task_url: str | None = None,
    poll_interval: float = MISSION_MAP_POLL_INTERVAL,
) -> dict[str, Any]:
    artifacts = _task_artifacts(task_payload)
    task = _task_record(task_payload)
    indoor_map = _turtlebot3_indoor_map_model_from_artifacts(artifacts)
    if indoor_map:
        return _mission_indoor_map_model(
            task_payload=task_payload,
            indoor_map=indoor_map,
            live_task_url=live_task_url,
            poll_interval=poll_interval,
        )
    route = _mission_map_latlon_from_route(artifacts)
    if route is None:
        raise click.ClickException(
            "task does not include source coordinates; `missionos map` needs "
            "mission_designer_coordinate_pair_route takeoff/dropoff lat/lon"
        )
    takeoff_lat, takeoff_lon, dropoff_lat, dropoff_lon = route
    planned_points = _mission_map_planned_points(
        artifacts,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
        dropoff_lat=dropoff_lat,
        dropoff_lon=dropoff_lon,
    )
    observed_trace = _mission_map_observed_trace(
        artifacts=artifacts,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
    )
    observed_segment_details = list(observed_trace["segments"])
    observed_segments = [
        list(segment.get("points") or []) for segment in observed_segment_details
    ]
    lon_scale = 111320.0 * math.cos(math.radians(takeoff_lat))

    def distance_to(point: dict[str, Any], *, lat: float, lon: float) -> float:
        return math.hypot(
            (float(point["lat"]) - lat) * 111320.0,
            (float(point["lon"]) - lon) * lon_scale,
        )

    for index, segment in enumerate(observed_segment_details):
        points = segment.get("points") or []
        if not points:
            segment["role"] = "observed"
            continue
        first = points[0]
        last = points[-1]
        is_return = (
            index > 0
            and distance_to(last, lat=takeoff_lat, lon=takeoff_lon)
            < distance_to(last, lat=dropoff_lat, lon=dropoff_lon)
            and (
                (_as_int(segment.get("segment_index")) or 0) > 0
                or distance_to(first, lat=dropoff_lat, lon=dropoff_lon)
                < distance_to(first, lat=takeoff_lat, lon=takeoff_lon)
            )
        )
        segment["role"] = (
            "return_to_home"
            if is_return
            else "outbound"
            if index == 0
            else "outbound_after_observation_gap"
        )
    observed_points = [point for segment in observed_segments for point in segment]
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    latest_snapshot_point = _mission_map_sample_latlon(
        snapshot,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
    ) if snapshot else None
    if latest_snapshot_point is not None:
        lat, lon, source = latest_snapshot_point
        latest = {
            "lat": lat,
            "lon": lon,
            "source": f"{source}_latest_snapshot",
            "phase": _status_text(snapshot.get("phase"), "latest_snapshot"),
            "alt_m": _as_float(
                snapshot.get("relative_alt_m")
                or snapshot.get("altitude_above_home_m")
                or snapshot.get("local_z_m")
                or snapshot.get("z_m")
            ),
            "elapsed_s": snapshot.get("elapsed_seconds")
            or snapshot.get("elapsed_s")
            or snapshot.get("sample_index"),
        }
        if observed_points and (
            abs(observed_points[-1]["lat"] - latest["lat"]) <= 1e-8
            and abs(observed_points[-1]["lon"] - latest["lon"]) <= 1e-8
        ):
            latest = observed_points[-1]
    else:
        latest = None
    compatibility_points = list(observed_points)
    if not compatibility_points:
        compatibility_points = [
            {
                "lat": takeoff_lat,
                "lon": takeoff_lon,
                "source": "route_takeoff",
                "phase": "takeoff",
                "alt_m": 0,
                "elapsed_s": None,
            },
            {
                "lat": dropoff_lat,
                "lon": dropoff_lon,
                "source": "route_dropoff",
                "phase": "dropoff",
                "alt_m": None,
                "elapsed_s": None,
            },
        ]
    provider_config = MISSION_MAP_PROVIDERS[provider]
    obstacles = _mission_map_obstacles(
        artifacts,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
        dropoff_lat=dropoff_lat,
        dropoff_lon=dropoff_lon,
    )
    avoidance = _mission_map_maneuver(
        artifacts=artifacts,
        snapshot=snapshot,
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
    )
    route_north_m, route_east_m = _mission_map_latlon_to_local(
        takeoff_lat=takeoff_lat,
        takeoff_lon=takeoff_lon,
        lat=dropoff_lat,
        lon=dropoff_lon,
    )
    route_length_m = math.hypot(route_north_m, route_east_m)
    if route_length_m > 1e-6:
        route_unit_north = route_north_m / route_length_m
        route_unit_east = route_east_m / route_length_m

        def route_geometry(point: dict[str, Any]) -> tuple[float, float]:
            north_m, east_m = _mission_map_latlon_to_local(
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
                lat=float(point["lat"]),
                lon=float(point["lon"]),
            )
            progress_m = north_m * route_unit_north + east_m * route_unit_east
            cross_track_m = abs(
                north_m * route_unit_east - east_m * route_unit_north
            )
            return progress_m, cross_track_m

        target = avoidance.get("target") if isinstance(avoidance, dict) else None
        target = target if isinstance(target, dict) else None
        start = avoidance.get("start") if isinstance(avoidance, dict) else None
        start = start if isinstance(start, dict) else {}
        obstacle = min(
            obstacles,
            key=lambda item: math.hypot(
                (_as_float(item.get("x_m")) or 0.0)
                - (_as_float(start.get("x_m")) or 0.0),
                (_as_float(item.get("y_m")) or 0.0)
                - (_as_float(start.get("y_m")) or 0.0),
            ),
            default=None,
        )
        target_progress_m = route_geometry(target)[0] if target else None
        obstacle_progress_m = None
        obstacle_half_along_m = 0.0
        if obstacle is not None:
            obstacle_progress_m = (
                (_as_float(obstacle.get("x_m")) or 0.0) * route_unit_north
                + (_as_float(obstacle.get("y_m")) or 0.0) * route_unit_east
            )
            obstacle_half_along_m = (
                abs(route_unit_north)
                * ((_as_float(obstacle.get("size_x_m")) or 0.0) / 2.0)
                + abs(route_unit_east)
                * ((_as_float(obstacle.get("size_y_m")) or 0.0) / 2.0)
            )
        target_beyond_obstacle = bool(
            target_progress_m is not None
            and obstacle_progress_m is not None
            and target_progress_m > obstacle_progress_m + obstacle_half_along_m
        )
        rejoin_point = None
        if target:
            outbound_points = [
                point
                for detail in observed_segment_details
                if detail.get("role") != "return_to_home"
                for point in detail.get("points") or []
            ]
            if outbound_points:
                target_index = min(
                    range(len(outbound_points)),
                    key=lambda index: distance_to(
                        outbound_points[index],
                        lat=float(target["lat"]),
                        lon=float(target["lon"]),
                    ),
                )
                for point in outbound_points[target_index + 1 :]:
                    progress_m, cross_track_m = route_geometry(point)
                    if (
                        target_progress_m is not None
                        and progress_m >= target_progress_m
                        and cross_track_m <= 12.0
                    ):
                        rejoin_point = {
                            **point,
                            "route_progress_m": round(progress_m, 3),
                            "cross_track_m": round(cross_track_m, 3),
                        }
                        break
        if avoidance:
            avoidance["route_rejoin"] = rejoin_point
            avoidance["geometry_status"] = (
                "lateral_bypass_target_beyond_obstacle"
                if target_beyond_obstacle
                else "legacy_target_before_obstacle"
                if target and obstacle_progress_m is not None
                else "geometry_unavailable"
            )
            avoidance["target_beyond_obstacle"] = target_beyond_obstacle
            avoidance["target_route_progress_m"] = (
                round(target_progress_m, 3)
                if target_progress_m is not None
                else None
            )
            avoidance["obstacle_route_progress_m"] = (
                round(obstacle_progress_m, 3)
                if obstacle_progress_m is not None
                else None
            )
    task_status = _task_status(task_payload)
    latest_point = latest or (observed_points[-1] if observed_points else None)
    terminal_marker_label = "current"
    if (
        task_status in TERMINAL_TASK_STATUSES
        and latest_point is not None
        and distance_to(latest_point, lat=takeoff_lat, lon=takeoff_lon) <= 15.0
    ):
        terminal_marker_label = (
            "landed at home"
            if _as_bool(snapshot.get("landed")) is True
            else "mission ended at home"
        )
    return {
        "schema_version": "missionos_cli_2d_map.v1",
        "task_id": _status_text(task.get("task_id")),
        "task_status": task_status,
        "task_updated_at": task.get("updated_at"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": {**provider_config, "key": provider},
        "route": {
            "takeoff": {"lat": takeoff_lat, "lon": takeoff_lon, "label": "H"},
            "dropoff": {"lat": dropoff_lat, "lon": dropoff_lon, "label": "D"},
        },
        "planned_points": planned_points,
        "observed_points": observed_points,
        "observed_segments": observed_segments,
        "observed_segment_details": observed_segment_details,
        "observed_gaps": list(observed_trace["gaps"]),
        "observed_trace_source": observed_trace["source"],
        "points": compatibility_points,
        "latest": latest_point,
        "terminal_marker_label": terminal_marker_label,
        "avoidance": avoidance,
        "obstacles": obstacles,
        "telemetry": _mission_map_telemetry_model(
            snapshot=snapshot,
            artifacts=artifacts,
        ),
        "battery": _mission_map_battery_model(
            snapshot=snapshot,
            artifacts=artifacts,
        ),
        "recovery_provenance": _mission_map_recovery_provenance(artifacts),
        "weather": _mission_map_weather_model(artifacts),
        "live": {
            "enabled": bool(live_task_url),
            "task_url": live_task_url or "",
            "poll_interval_ms": max(500, int(float(poll_interval) * 1000)),
            "terminal_statuses": sorted(TERMINAL_TASK_STATUSES),
        },
        "boundaries": [
            "2D map uses real browser-fetched basemap tiles from the configured provider.",
            "MissionOS overlays source planned route, observed telemetry, operator-approved recovery maneuver traces, and source-backed obstacle markers.",
            "Solid observed paths contain saved telemetry only; dashed gap connectors are display-only and are not observation evidence.",
            "Map display is read-only and is not a verifier, dispatch control, or delivery claim.",
        ],
    }


def _write_mission_map_html(
    *,
    model: dict[str, Any],
    output_path: Path | None,
) -> Path:
    task_id = str(model.get("task_id") or "task").replace("/", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_path or MISSION_MAP_OUTPUT_DIR / f"{task_id}_{timestamp}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_mission_map_html(model), encoding="utf-8")
    return path


def _write_terminal_route_evidence(
    *,
    model: dict[str, Any],
    output_dir: Path = MISSION_MAP_OUTPUT_DIR,
    stem: str | None = None,
) -> dict[str, Any] | None:
    """Write source-backed terminal route evidence for supported flight maps."""

    if model.get("map_kind") == "indoor_local_xy":
        return None
    if str(model.get("task_status") or "").strip().lower() not in (
        TERMINAL_TASK_STATUSES
    ):
        return None
    return write_mission_route_evidence_artifacts(
        model=model,
        output_dir=output_dir,
        stem=stem,
    )


def _watch_flight_map(
    client: MissionOSGatewayClient,
    task_id: str,
    *,
    poll_interval: float,
) -> None:
    trail: list[tuple[float, float]] = []
    turtlebot3_live_trail: list[dict[str, Any]] = []
    turtlebot3_alignment_state: dict[str, Any] = {}
    with Live(console=console, refresh_per_second=8, screen=False) as live:
        while True:
            try:
                task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
            except click.ClickException as exc:
                live.update(
                    Panel(f"[red]{exc.message}[/red]", title="MissionOS Live Map")
                )
                time.sleep(max(0.05, poll_interval))
                continue
            artifacts = _task_artifacts(task_payload)
            indoor_map = _turtlebot3_indoor_map_model_from_artifacts(artifacts)
            status = _task_status(task_payload)
            if indoor_map:
                indoor_map = _overlay_turtlebot3_live_telemetry(
                    indoor_map,
                    artifacts=artifacts,
                    trail=turtlebot3_live_trail,
                    alignment_state=turtlebot3_alignment_state,
                    freeze_live_preview=status in TERMINAL_TASK_STATUSES,
                )
                live.update(
                    _render_turtlebot3_indoor_map(
                        indoor_map=indoor_map,
                        status=status,
                        task_id=task_id,
                    )
                )
                if status in TERMINAL_TASK_STATUSES:
                    break
                time.sleep(max(0.05, poll_interval))
                continue
            snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            north = _as_float(snapshot.get("local_x_m"))
            east = _as_float(snapshot.get("local_y_m"))
            if north is not None and east is not None:
                if not trail or trail[-1] != (north, east):
                    trail.append((north, east))
                    if len(trail) > _FLIGHT_MAP_TRAIL_LIMIT:
                        del trail[: len(trail) - _FLIGHT_MAP_TRAIL_LIMIT]
            if trail:
                live.update(
                    _render_flight_map(
                        trail=trail,
                        snapshot=snapshot,
                        artifacts=artifacts,
                        status=status,
                        task_id=task_id,
                    )
                )
            else:
                live.update(
                    Panel(
                        f"[dim]task={task_id} status={status} — waiting for telemetry...[/dim]",
                        title="MissionOS Live Map",
                        border_style="cyan",
                    )
                )
            if status in TERMINAL_TASK_STATUSES:
                break
            time.sleep(max(0.05, poll_interval))


@missionos.command("watch")
@click.option(
    "--task-id",
    default="",
    help="Task/job id to render. Defaults to the task stored by `run`.",
)
@click.option(
    "--poll-interval",
    default=FLIGHT_MAP_POLL_INTERVAL,
    show_default=True,
    type=click.FloatRange(0.2, 10.0),
    help="Seconds between telemetry polls.",
)
@click.pass_context
def watch_command(ctx: click.Context, task_id: str, poll_interval: float) -> None:
    """Render a live top-down dot-art map of the AUTO mission in the terminal."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    resolved_task_id = _resolve_live_task_id(
        client,
        explicit_task_id=task_id,
        stored_task_id=_stored_sitl_task_id(ctx),
    )
    try:
        _watch_flight_map(client, resolved_task_id, poll_interval=poll_interval)
    except KeyboardInterrupt:
        console.print("[yellow](watch stopped)[/yellow]")


def _serve_authenticated_live_mission_map(
    *,
    client: MissionOSGatewayClient,
    task_id: str,
    model: dict[str, Any],
    no_open: bool,
) -> None:
    """Serve live map HTML and an authenticated task proxy on loopback."""

    token = secrets.token_urlsafe(18)
    page_path = f"/{token}/"
    task_path = f"/{token}/task"
    evidence_path = f"/{token}/evidence.svg"
    live_model = dict(model)
    live_model["live"] = {
        **dict(model.get("live") or {}),
        "enabled": True,
        "task_url": task_path,
        "evidence_image_url": evidence_path,
    }
    html_bytes = _mission_map_html(live_model).encode("utf-8")
    terminal_seen = threading.Event()
    browser_live_trail: list[dict[str, Any]] = []
    browser_alignment_state: dict[str, Any] = {}
    overlay_lock = threading.Lock()
    evidence_lock = threading.Lock()
    evidence_state: dict[str, Any] = {}

    def ensure_terminal_evidence(
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with evidence_lock:
            if evidence_state:
                return evidence_state
            try:
                supplied_model = (
                    payload.get("missionos_map_model")
                    if isinstance(payload, dict)
                    else None
                )
                if isinstance(supplied_model, dict):
                    terminal_model = dict(supplied_model)
                else:
                    latest = payload or client.get(
                        f"/tasks/{quote(task_id, safe='')}"
                    )
                    terminal_model = _mission_map_model(
                        task_payload=latest,
                        provider=str(
                            (model.get("provider") or {}).get("key") or "osm"
                        ),
                        live_task_url=task_path,
                        poll_interval=float(
                            (model.get("live") or {}).get("poll_interval_ms")
                            or 1000
                        )
                        / 1000.0,
                    )
                terminal_model["live"] = {
                    **dict(terminal_model.get("live") or {}),
                    "evidence_image_url": evidence_path,
                }
                generated = _write_terminal_route_evidence(model=terminal_model)
            except (click.ClickException, ValueError):
                return None
            if generated is None:
                return None
            evidence_state.update(generated)
            console.print(
                Panel(
                    "\n".join(
                        (
                            f"task_id={task_id}",
                            f"image={generated['svg_path']}",
                            f"manifest={generated['manifest_path']}",
                            "boundary=source-backed display evidence; source task "
                            "artifacts remain authoritative",
                        )
                    ),
                    title="MissionOS E2E Route Evidence",
                    border_style="green",
                )
            )
            return evidence_state

    class LiveMapHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send(self, status: int, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == page_path:
                self._send(200, "text/html; charset=utf-8", html_bytes)
                return
            if self.path.split("?", 1)[0] == evidence_path:
                generated = ensure_terminal_evidence()
                if generated is None:
                    self._send(
                        409,
                        "text/plain; charset=utf-8",
                        b"terminal route evidence is not available",
                    )
                    return
                self._send(
                    200,
                    "image/svg+xml; charset=utf-8",
                    bytes(generated["svg_bytes"]),
                )
                return
            if self.path == task_path:
                try:
                    payload = client.get(f"/tasks/{quote(task_id, safe='')}")
                    task = payload.get("task")
                    task = task if isinstance(task, dict) else {}
                    artifacts = _task_artifacts(payload)
                    task_status = str(task.get("status") or "")
                    indoor_map = _turtlebot3_indoor_map_model_from_artifacts(
                        artifacts
                    )
                    if indoor_map:
                        with overlay_lock:
                            overlaid = _overlay_turtlebot3_live_telemetry(
                                indoor_map,
                                artifacts=artifacts,
                                trail=browser_live_trail,
                                alignment_state=browser_alignment_state,
                                freeze_live_preview=(
                                    task_status in TERMINAL_TASK_STATUSES
                                ),
                            )
                        next_task = dict(task)
                        next_artifacts = dict(artifacts)
                        next_artifacts["turtlebot3_indoor_map_model"] = overlaid
                        next_task["artifacts"] = next_artifacts
                        payload = {**payload, "task": next_task}
                        task = next_task
                    else:
                        provider_key = str(
                            (model.get("provider") or {}).get("key") or "osm"
                        )
                        fresh_model = _mission_map_model(
                            task_payload=payload,
                            provider=provider_key,
                            live_task_url=task_path,
                            poll_interval=float(
                                (model.get("live") or {}).get("poll_interval_ms")
                                or 1000
                            )
                            / 1000.0,
                        )
                        fresh_model["live"] = {
                            **dict(fresh_model.get("live") or {}),
                            "evidence_image_url": evidence_path,
                        }
                        payload = {
                            "missionos_map_model": fresh_model,
                            "task": task,
                        }
                    terminal_response = (
                        str(task.get("status") or "") in TERMINAL_TASK_STATUSES
                    )
                    if terminal_response:
                        terminal_seen.set()
                        ensure_terminal_evidence(payload)
                    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    if terminal_response:
                        # The terminal preview is a one-response operator aid.
                        # A browser reload must reconstruct from persisted
                        # blue/purple evidence and therefore starts without it.
                        with overlay_lock:
                            browser_live_trail.clear()
                except click.ClickException as exc:
                    encoded = json.dumps(
                        {"detail": exc.message}, ensure_ascii=False
                    ).encode("utf-8")
                    self._send(502, "application/json; charset=utf-8", encoded)
                    return
                self._send(200, "application/json; charset=utf-8", encoded)
                return
            self._send(404, "text/plain; charset=utf-8", b"not found")

    server = ThreadingHTTPServer(("127.0.0.1", 0), LiveMapHandler)
    server.timeout = 0.5
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}{page_path}"
    opened = False if no_open else click.launch(url) == 0
    console.print(
        Panel(
            "\n".join(
                (
                    f"task_id={task_id}",
                    f"url={url}",
                    f"opened={str(opened).lower()}",
                    "live=true; authenticated_gateway_proxy=loopback",
                    "boundary=read-only display proxy; no approval, dispatch, "
                    "completion, or physical claim",
                )
            ),
            title="MissionOS Live 2D Map",
            border_style="cyan",
        )
    )
    try:
        next_status_poll = 0.0
        while not terminal_seen.is_set():
            server.handle_request()
            if time.monotonic() >= next_status_poll:
                try:
                    latest = client.get(f"/tasks/{quote(task_id, safe='')}")
                    latest_task = latest.get("task")
                    latest_task = (
                        latest_task if isinstance(latest_task, dict) else {}
                    )
                    if str(latest_task.get("status") or "") in TERMINAL_TASK_STATUSES:
                        ensure_terminal_evidence(latest)
                        terminal_seen.set()
                except click.ClickException:
                    pass
                next_status_poll = time.monotonic() + 1.0
        # Keep the authenticated read-only snapshot available after terminal
        # state. The companion lifecycle or Ctrl-C owns shutdown, so a browser
        # reload can reconstruct only persisted blue/purple evidence.
        while True:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@missionos.command("map")
@click.option(
    "--task-id",
    default="",
    help="Task/job id to map. Defaults to the latest running SITL task.",
)
@click.option(
    "--provider",
    default="osm",
    show_default=True,
    type=click.Choice(sorted(MISSION_MAP_PROVIDERS)),
    help="Real basemap tile provider used by the generated browser view.",
)
@click.option(
    "--output",
    "output_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="HTML output path. Defaults to output/missionos_maps/<task>_<time>.html.",
)
@click.option(
    "--poll-interval",
    default=MISSION_MAP_POLL_INTERVAL,
    show_default=True,
    type=click.FloatRange(0.5, 10.0),
    help="Seconds between live Gateway polls in the generated browser map.",
)
@click.option(
    "--snapshot",
    is_flag=True,
    help="Generate a static one-time map instead of a live-polling map.",
)
@click.option(
    "--serve-live",
    is_flag=True,
    help="Serve an authenticated loopback live map until the task finishes.",
)
@click.option("--no-open", is_flag=True, help="Generate the HTML file without opening a browser.")
@click.pass_context
def map_command(
    ctx: click.Context,
    task_id: str,
    provider: str,
    output_path: Path | None,
    poll_interval: float,
    snapshot: bool,
    serve_live: bool,
    no_open: bool,
) -> None:
    """Generate a source-backed 2D browser map for the selected MissionOS task."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    if client.api_key and not snapshot:
        # An authenticated Gateway cannot be polled from a file:// document.
        # Default to the loopback proxy so `missionos map` never presents a
        # misleading frozen snapshot unless --snapshot was explicit.
        serve_live = True
    resolved_task_id = _resolve_live_task_id(
        client,
        explicit_task_id=task_id,
        stored_task_id=_stored_sitl_task_id(ctx),
    )
    task_payload, _ = _task_and_timeline(client, resolved_task_id, timeline_limit=0)
    task_record = _task_record(task_payload)
    if (
        serve_live
        and task_record.get("kind") == "turtlebot3_home_mission_execution"
        and not _turtlebot3_indoor_map_model_from_artifacts(
            _task_artifacts(task_payload)
        )
    ):
        # The companion can start as soon as the task record is created, a few
        # seconds before the first progress payload contains the indoor model.
        # Wait for that source-backed artifact instead of exiting into a stale
        # generic-map fallback.
        map_ready_deadline = time.monotonic() + 60.0
        while time.monotonic() < map_ready_deadline:
            time.sleep(0.5)
            task_payload, _ = _task_and_timeline(
                client,
                resolved_task_id,
                timeline_limit=0,
            )
            if _turtlebot3_indoor_map_model_from_artifacts(
                _task_artifacts(task_payload)
            ):
                break
            if str(_task_record(task_payload).get("status") or "") in (
                TERMINAL_TASK_STATUSES
            ):
                break
    live_task_url = None
    authenticated_file_snapshot = bool(client.api_key and not snapshot and not serve_live)
    if not snapshot and not authenticated_file_snapshot:
        encoded_task_id = quote(resolved_task_id, safe="")
        live_task_url = _join_url(client.base_url, f"/tasks/{encoded_task_id}")
    model = _mission_map_model(
        task_payload=task_payload,
        provider=provider,
        live_task_url=live_task_url,
        poll_interval=poll_interval,
    )
    if serve_live and not snapshot:
        _serve_authenticated_live_mission_map(
            client=client,
            task_id=resolved_task_id,
            model=model,
            no_open=no_open,
        )
        return
    path = _write_mission_map_html(model=model, output_path=output_path)
    evidence = _write_terminal_route_evidence(
        model=model,
        output_dir=path.parent,
        stem=f"{path.stem}_e2e_route_evidence",
    )
    file_url = path.resolve().as_uri()
    if ctx.obj["missionos_json_output"]:
        display_points = len(
            model.get("points")
            or model.get("observed_points")
            or model.get("planned_points")
            or []
        )
        _print_json(
            {
                "task_id": resolved_task_id,
                "map_kind": model.get("map_kind", "wgs84_route_overlay"),
                "map_provider": model["provider"]["label"],
                "output_path": str(path),
                "file_url": file_url,
                "evidence_image_path": (
                    str(evidence["svg_path"]) if evidence is not None else None
                ),
                "evidence_manifest_path": (
                    str(evidence["manifest_path"])
                    if evidence is not None
                    else None
                ),
                "point_count": display_points,
                "planned_point_count": len(model.get("planned_points") or []),
                "observed_point_count": len(model.get("observed_points") or []),
                "obstacle_count": len(model.get("obstacles") or []),
	                "avoidance_sample_count": len(
	                    (model.get("avoidance") or {}).get("samples") or []
	                ),
	                "live": bool(model.get("live", {}).get("enabled")),
	                "opened": False,
	            }
	        )
        return
    if authenticated_file_snapshot:
        console.print(
            "[yellow]The Gateway requires authentication, so this file:// map is "
            "a snapshot. Use the automatically opened map companion or "
            "`missionos map --serve-live` for authenticated live updates.[/yellow]"
        )
    opened = False
    if not no_open:
        opened = click.launch(file_url) == 0
    display_points = len(
        model.get("points")
        or model.get("observed_points")
        or model.get("planned_points")
        or []
    )
    boundary_text = (
        "boundary=indoor local-XY MissionOS/Nav2 evidence display; read-only, not verifier/dispatch/delivery/physical claim"
        if model.get("map_kind") == "indoor_local_xy"
        else "boundary=real basemap tiles + MissionOS route/telemetry overlay; read-only, not verifier/dispatch/delivery claim"
    )
    console.print(
        Panel(
            "\n".join(
                [
                    f"task_id={resolved_task_id}",
                    f"map_kind={model.get('map_kind', 'wgs84_route_overlay')}",
                    f"provider={model['provider']['label']}",
                    f"points={display_points}",
                    f"planned={len(model.get('planned_points') or [])}",
                    f"observed={len(model.get('observed_points') or [])}",
                    f"obstacles={len(model.get('obstacles') or [])}",
	                    "avoidance_samples="
	                    f"{len((model.get('avoidance') or {}).get('samples') or [])}",
                    f"html={path}",
                    "evidence_image="
                    + (
                        str(evidence["svg_path"])
                        if evidence is not None
                        else "not_generated_task_not_terminal_or_unsupported"
                    ),
                    "evidence_manifest="
                    + (
                        str(evidence["manifest_path"])
                        if evidence is not None
                        else "-"
                    ),
                    f"url={file_url}",
                    "live=" + ("true" if model.get("live", {}).get("enabled") else "false"),
                    "opened=" + ("true" if opened else "false"),
                    boundary_text,
                ]
            ),
            title="MissionOS 2D Map",
            border_style="cyan",
        )
    )


# ── Interactive operator view (`missionos operate`) ──────────────────────────
# Non-modal: live telemetry keeps refreshing while an agent proposal is shown.
# Dismissing ("view status") re-surfaces the proposal after a cooldown. A real
# LAND/RTL dispatch always requires an explicit `y` confirmation — Enter/any key
# never fires recovery. Dispatch still goes through the same recovery-dispatch
# route with explicit approval; the agent never gains dispatch authority.
PROPOSAL_REDISPLAY_SECONDS = 30.0
_OPERATOR_RECOVERY_ACTIONS = {
    "return_to_launch": "RTL",
    "land": "LAND",
    "adjust_altitude": "ADJUST ALTITUDE",
    "adjust_speed": "ADJUST SPEED",
    "reroute": "REROUTE",
    "avoid_obstacle": "AVOID OBSTACLE",
}


def _agent_proposal_from_task(task_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the proposal-only runtime recovery-agent recommendation, if any."""
    artifacts = _task_artifacts(task_payload)
    bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    result = bridge.get("runtime_recovery_agent_result")
    result = result if isinstance(result, dict) else {}
    assessment = result.get("assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    action = _first_present(
        assessment.get("selected_bounded_action"),
        assessment.get("recommended_action"),
        assessment.get("recovery_action"),
    )
    if not action:
        return None
    risks = assessment.get("observed_risk_reasons")
    if not isinstance(risks, (list, tuple)):
        risks = [risks] if risks else []
    return {
        "task_id": str(_task_record(task_payload).get("task_id") or ""),
        "action": str(action),
        "status": _runtime_recovery_effective_status(result, bridge, assessment),
        "risks": [str(r) for r in risks if r],
        "parameters": dict(assessment.get("proposed_parameters"))
        if isinstance(assessment.get("proposed_parameters"), dict)
        else {},
    }


def _first_mapping_item(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def _turtlebot3_recovery_summary_from_artifacts(
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    summary = artifacts.get("summary")
    if isinstance(summary, dict):
        return summary
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    nested = execution.get("summary")
    return nested if isinstance(nested, dict) else {}


def _turtlebot3_recovery_decision_summary_from_artifacts(
    artifacts: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    decision = artifacts.get("turtlebot3_recovery_decision_summary")
    if isinstance(decision, dict):
        return decision
    nested = summary.get("turtlebot3_recovery_decision_summary")
    return nested if isinstance(nested, dict) else {}


def _recovery_dispatch_action_from_proposal_action(action: Any) -> str:
    normalized = str(action or "").strip().lower().replace("-", "_")
    if normalized in {"return_home", "return_to_home", "rtl"}:
        return "return_to_launch"
    return normalized


def _recovery_proposal_parameters(
    *,
    action: str,
    summary: dict[str, Any],
    proposal: dict[str, Any],
) -> dict[str, Any]:
    if action in {"return_to_launch", "land", "hold", "safe_stop"}:
        return {}
    for key in ("recovery_parameters", "proposed_parameters"):
        value = proposal.get(key)
        if isinstance(value, dict) and value:
            return dict(value)
    observations = proposal.get("input_observations")
    observations = observations if isinstance(observations, dict) else {}
    if action == "avoid_obstacle":
        scenario = summary.get("runtime_recovery_obstacle_scenario")
        scenario = scenario if isinstance(scenario, dict) else {}
        x_value = _first_present(
            observations.get("recommended_avoidance_target_x_m"),
            scenario.get("recommended_avoidance_target_x_m"),
        )
        y_value = _first_present(
            observations.get("recommended_avoidance_target_y_m"),
            scenario.get("recommended_avoidance_target_y_m"),
        )
        parameters: dict[str, Any] = {}
        if x_value is not None and y_value is not None:
            parameters["target_x_m"] = x_value
            parameters["target_y_m"] = y_value
        if observations.get("target_altitude_m") is not None:
            parameters["target_altitude_m"] = observations["target_altitude_m"]
        return parameters
    if action == "reroute":
        x_value = _first_present(observations.get("target_x_m"), observations.get("x_m"))
        y_value = _first_present(observations.get("target_y_m"), observations.get("y_m"))
        if x_value is not None and y_value is not None:
            return {"target_x_m": x_value, "target_y_m": y_value}
    if action == "adjust_altitude":
        altitude = observations.get("target_altitude_m")
        return {"target_altitude_m": altitude} if altitude is not None else {}
    if action == "adjust_speed":
        speed = observations.get("target_speed_mps")
        return {"target_speed_mps": speed} if speed is not None else {}
    return {}


def _pending_recovery_approval_from_task(
    task_payload: dict[str, Any],
) -> dict[str, Any] | None:
    artifacts = _task_artifacts(task_payload)
    summary = _turtlebot3_recovery_summary_from_artifacts(artifacts)
    decision = _turtlebot3_recovery_decision_summary_from_artifacts(artifacts, summary)
    task = _task_record(task_payload)
    task_id = str(task.get("task_id") or "").strip()
    task_kind = str(task.get("kind") or "")
    task_status = str(task.get("status") or "").strip().lower()
    runtime_proposal = artifacts.get("missionos_runtime_recovery_last_proposal")
    runtime_proposal = (
        runtime_proposal if isinstance(runtime_proposal, Mapping) else {}
    )
    if (
        task_kind != "turtlebot3_home_mission_execution"
        and task_status == "running"
        and runtime_proposal.get("schema_version")
        == "missionos_runtime_recovery_proposal_evidence.v1"
        and runtime_proposal.get("proposal_status")
        == "awaiting_operator_approval"
    ):
        runtime_result = runtime_proposal.get("runtime_recovery_agent_result")
        runtime_result = (
            runtime_result if isinstance(runtime_result, Mapping) else {}
        )
        runtime_assessment = runtime_result.get("assessment")
        runtime_assessment = (
            runtime_assessment if isinstance(runtime_assessment, Mapping) else {}
        )
        selected_action = str(
            runtime_assessment.get("selected_bounded_action") or ""
        ).strip()
        dispatch_action = _recovery_dispatch_action_from_proposal_action(
            selected_action
        )
        proposed_parameters = runtime_assessment.get("proposed_parameters")
        proposed_parameters = (
            dict(proposed_parameters)
            if isinstance(proposed_parameters, Mapping)
            else {}
        )
        receipt = artifacts.get("missionos_runtime_recovery_dispatch_receipt")
        receipt = receipt if isinstance(receipt, Mapping) else {}
        receipt_revalidation = receipt.get("proposal_revalidation")
        receipt_revalidation = (
            receipt_revalidation
            if isinstance(receipt_revalidation, Mapping)
            else {}
        )
        proposal_id = str(runtime_proposal.get("proposal_id") or "")
        matching_authority_exists = bool(
            proposal_id
            and receipt_revalidation.get("proposal_id") == proposal_id
            and receipt.get("dispatch_authority_created") is True
        )
        if task_id and dispatch_action and not matching_authority_exists:
            agent_output = runtime_result.get("agent_output")
            agent_output = agent_output if isinstance(agent_output, Mapping) else {}
            invocations = runtime_result.get("agent_invocations")
            invocations = (
                invocations
                if isinstance(invocations, Sequence)
                and not isinstance(invocations, (str, bytes))
                else []
            )
            invocation = next(
                (dict(item) for item in invocations if isinstance(item, Mapping)),
                {},
            )
            bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
            bridge = bridge if isinstance(bridge, Mapping) else {}
            observations = bridge.get("telemetry_snapshot")
            observations = (
                dict(observations) if isinstance(observations, Mapping) else {}
            )
            return {
                "task_id": task_id,
                "selected_action": selected_action,
                "recovery_action": dispatch_action,
                "recovery_parameters": proposed_parameters,
                "proposal_source": str(
                    runtime_proposal.get("proposal_source") or ""
                ),
                "rules_execution_class": str(
                    runtime_assessment.get("assessment_status") or ""
                ),
                "requires_new_human_approval": True,
                "checkpoint_id": "",
                "checkpoint_hash": "",
                "checkpoint_approval_supported": True,
                "checkpoint_revision_supported": False,
                "checkpoint_dispatch_supported": True,
                "operator_guidance_required": False,
                "recovery_proposal_id": proposal_id,
                "proposal_reason": str(agent_output.get("rationale") or ""),
                "input_observations": observations,
                "llm_provider": str(invocation.get("provider") or ""),
                "llm_model_id": str(invocation.get("model_id") or ""),
                "dispatch_authority_created": False,
                "physical_execution_invoked": False,
            }
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    if not isinstance(checkpoint, dict):
        checkpoint = summary.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    is_turtlebot3_recovery = (
        task_kind == "turtlebot3_home_mission_execution"
        or _is_home_robot_nav2_execution_target(summary.get("execution_target"))
        or bool(checkpoint)
    )
    if is_turtlebot3_recovery:
        if (
            task_status != "pending"
            or checkpoint.get("schema_version")
            != "turtlebot3_recovery_checkpoint.v1"
            or checkpoint.get("checkpoint_status")
            != "awaiting_operator_approval"
        ):
            return None
        selected_action = str(checkpoint.get("selected_action") or "")
        # TurtleBot3 checkpoint actions are native Nav2 recovery actions. In
        # particular, return_home must not be rewritten to PX4's
        # return_to_launch command.
        dispatch_action = selected_action.strip().lower().replace("-", "_")
        parameters = checkpoint.get("approved_parameters")
        parameters = dict(parameters) if isinstance(parameters, dict) else {}
        proposal_id = str(checkpoint.get("recovery_proposal_id") or "")
        classification_id = str(
            checkpoint.get("recovery_classification_id") or ""
        )
        proposals = summary.get("recovery_proposals")
        proposals = proposals if isinstance(proposals, list) else []
        proposal = next(
            (
                dict(item)
                for item in proposals
                if proposal_id
                and isinstance(item, dict)
                and str(item.get("proposal_id") or "") == proposal_id
            ),
            {},
        )
        classifications = summary.get("recovery_proposal_classifications")
        classifications = (
            classifications if isinstance(classifications, list) else []
        )
        classification = next(
            (
                dict(item)
                for item in classifications
                if classification_id
                and isinstance(item, dict)
                and str(item.get("classification_id") or "")
                == classification_id
            ),
            {},
        )
        llm_evidence = proposal.get("llm_invocation_evidence")
        llm_evidence = dict(llm_evidence) if isinstance(llm_evidence, dict) else {}
        observations = proposal.get("input_observations")
        observations = dict(observations) if isinstance(observations, dict) else {}
        execution_target = str(
            checkpoint.get("execution_target")
            or summary.get("execution_target")
            or ""
        )
        robot_profile = str(
            checkpoint.get("robot_profile")
            or summary.get("robot_profile")
            or ""
        )
        plan = artifacts.get("turtlebot3_home_mission_plan")
        plan = plan if isinstance(plan, Mapping) else {}
        stored_checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
        stored_checkpoint = (
            stored_checkpoint if isinstance(stored_checkpoint, Mapping) else {}
        )
        strict_turtlebot3_scope = (
            task_kind == "turtlebot3_home_mission_execution"
            and all(
                str(view.get("robot_profile") or "") == "turtlebot3"
                and str(view.get("execution_target") or "")
                == "ros2_nav2_turtlebot3_sim"
                for view in (plan, stored_checkpoint, summary)
            )
        )
        operator_guidance_required = (
            checkpoint.get("operator_guidance_required") is True
            or dispatch_action in {"ask_human", "hold", "safe_stop"}
        )
        checkpoint_dispatch_supported = (
            dispatch_action in {"avoid_obstacle", "return_home", "reroute"}
            and not operator_guidance_required
        )
        if not task_id or not dispatch_action:
            return None
        return {
            "task_id": task_id,
            "selected_action": selected_action,
            "recovery_action": dispatch_action,
            "recovery_parameters": parameters,
            "proposal_source": str(proposal.get("proposal_source") or ""),
            "rules_execution_class": str(
                classification.get("execution_class") or ""
            ),
            "requires_new_human_approval": True,
            "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
            "checkpoint_hash": str(checkpoint.get("checkpoint_hash") or ""),
            "parent_checkpoint_id": str(
                checkpoint.get("parent_checkpoint_id") or ""
            ),
            "parent_checkpoint_hash": str(
                checkpoint.get("parent_checkpoint_hash") or ""
            ),
            "revision_id": str(checkpoint.get("revision_id") or ""),
            "operator_instruction_sha256": str(
                checkpoint.get("operator_instruction_sha256") or ""
            ),
            "robot_profile": robot_profile,
            "execution_target": execution_target,
            "checkpoint_approval_supported": (
                strict_turtlebot3_scope and checkpoint_dispatch_supported
            ),
            "checkpoint_revision_supported": strict_turtlebot3_scope,
            "checkpoint_dispatch_supported": checkpoint_dispatch_supported,
            "operator_guidance_required": operator_guidance_required,
            "recovery_proposal_id": proposal_id,
            "recovery_classification_id": classification_id,
            "proposal_reason": str(proposal.get("reason") or ""),
            "input_observations": observations,
            "llm_provider": str(llm_evidence.get("provider") or ""),
            "llm_model_id": str(llm_evidence.get("model_id") or ""),
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
        }
    if not summary and not decision:
        return None
    classification = _first_mapping_item(
        summary.get("recovery_proposal_classifications")
    )
    proposal = _first_mapping_item(summary.get("recovery_proposals"))
    requires_approval = (
        decision.get("requires_new_human_approval") is True
        or classification.get("requires_new_human_approval") is True
    )
    if not requires_approval:
        return None
    already_dispatched = (
        decision.get("recovery_dispatch_request_sent") is True
        or summary.get("recovery_dispatch_request_sent") is True
    )
    if already_dispatched:
        return None
    selected_action = _first_present(
        decision.get("selected_action"),
        summary.get("runtime_recovery_action_kind"),
        summary.get("recovery_action_suggested"),
        proposal.get("selected_action"),
    )
    dispatch_action = _recovery_dispatch_action_from_proposal_action(selected_action)
    if not dispatch_action:
        return None
    parameters = _recovery_proposal_parameters(
        action=dispatch_action,
        summary=summary,
        proposal=proposal,
    )
    return {
        "task_id": task_id,
        "selected_action": str(selected_action or ""),
        "recovery_action": dispatch_action,
        "recovery_parameters": parameters,
        "proposal_source": decision.get("recovery_proposal_source")
        or proposal.get("proposal_source"),
        "rules_execution_class": decision.get("rules_execution_class")
        or classification.get("execution_class"),
        "requires_new_human_approval": True,
        "checkpoint_id": "",
        "checkpoint_hash": "",
        "checkpoint_approval_supported": True,
        "checkpoint_revision_supported": False,
        "proposal_reason": str(proposal.get("reason") or ""),
        "input_observations": dict(proposal.get("input_observations"))
        if isinstance(proposal.get("input_observations"), dict)
        else {},
        "llm_provider": "",
        "llm_model_id": "",
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }


def _is_recovery_approval_text(raw: str) -> bool:
    text = raw.strip().lower()
    compact = text.replace(" ", "").replace("　", "")
    if compact in {
        "承認",
        "承認します",
        "リカバリ承認",
        "回復承認",
        "承認して",
        "承認する",
    }:
        return True
    return text in {
        "approve recovery",
        "approve the recovery",
        "approve recovery proposal",
        "approve the recovery proposal",
        "operator approve recovery",
    }


def _lookup_pending_recovery_approval(
    client: MissionOSGatewayClient,
    *,
    task_id: str,
) -> dict[str, Any] | None:
    task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
    pending = _pending_recovery_approval_from_task(task_payload)
    if pending and not pending.get("task_id"):
        pending["task_id"] = task_id
    return pending


def _chat_recovery_revision_context(ctx: click.Context) -> dict[str, str]:
    value = ctx.obj.get("missionos_chat_recovery_revision_context")
    if not isinstance(value, dict):
        return {}
    context = {
        "task_id": str(value.get("task_id") or "").strip(),
        "checkpoint_id": str(value.get("checkpoint_id") or "").strip(),
        "checkpoint_hash": str(value.get("checkpoint_hash") or "").strip(),
    }
    if not all(context.values()):
        return {}
    return context


def _set_chat_recovery_revision_context(
    ctx: click.Context,
    *,
    pending: Mapping[str, Any],
) -> bool:
    context = {
        "task_id": str(pending.get("task_id") or "").strip(),
        "checkpoint_id": str(pending.get("checkpoint_id") or "").strip(),
        "checkpoint_hash": str(pending.get("checkpoint_hash") or "").strip(),
    }
    if not all(context.values()):
        return False
    ctx.obj["missionos_chat_recovery_revision_context"] = context
    return True


def _clear_chat_recovery_revision_context(ctx: click.Context) -> None:
    ctx.obj.pop("missionos_chat_recovery_revision_context", None)


def _turtlebot3_recovery_checkpoint_content_hash(
    checkpoint: Mapping[str, Any],
) -> str:
    mutable_fields = {
        "checkpoint_id",
        "checkpoint_hash",
        "checkpoint_status",
        "claimed_at",
        "claimed_by_approval_ref",
        "consumed_at",
        "consumed_by_approval_ref",
        "failed_at",
        "failure_reasons",
    }
    payload = {
        str(key): value
        for key, value in checkpoint.items()
        if str(key) not in mutable_fields
        and not str(key).startswith("superseded_")
    }
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _refetched_turtlebot3_revision_state(
    client: MissionOSGatewayClient,
    *,
    task_id: str,
) -> tuple[dict[str, Any] | None, bool, bool, dict[str, str]]:
    task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
    task = _task_record(task_payload)
    artifacts = _task_artifacts(task_payload)
    summary = _turtlebot3_recovery_summary_from_artifacts(artifacts)
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    receipt = artifacts.get("missionos_runtime_recovery_dispatch_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    pending = _pending_recovery_approval_from_task(task_payload)
    checkpoints = artifacts.get("turtlebot3_recovery_checkpoints")
    checkpoints = checkpoints if isinstance(checkpoints, dict) else {}
    parent_id = str(checkpoint.get("parent_checkpoint_id") or "")
    parent_hash = str(checkpoint.get("parent_checkpoint_hash") or "")
    checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
    checkpoint_hash = str(checkpoint.get("checkpoint_hash") or "")

    def _binds_current_checkpoint(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        return (
            str(value.get("checkpoint_id") or "") == checkpoint_id
            and str(value.get("checkpoint_hash") or "") == checkpoint_hash
        )

    operator_approval = artifacts.get("turtlebot3_recovery_operator_approval")
    bounded_action = artifacts.get("turtlebot3_recovery_bounded_action")
    receipt_approval = receipt.get("turtlebot3_recovery_operator_approval")
    receipt_bounded_action = receipt.get("turtlebot3_recovery_bounded_action")
    receipt_directly_binds_current = (
        str(receipt.get("reviewed_recovery_checkpoint_id") or "")
        == checkpoint_id
        and str(receipt.get("reviewed_recovery_checkpoint_hash") or "")
        == checkpoint_hash
    )
    receipt_has_current_authority = (
        _binds_current_checkpoint(receipt_approval)
        or _binds_current_checkpoint(receipt_bounded_action)
        or (
            receipt_directly_binds_current
            and (
                receipt.get("dispatch_authority_created") is True
                or receipt.get("operator_approved") is True
                or receipt.get("explicit_recovery_dispatch_approval") is True
            )
        )
    )
    durable_child = checkpoints.get(checkpoint_id)
    durable_child = durable_child if isinstance(durable_child, dict) else {}
    durable_parent = checkpoints.get(parent_id)
    durable_parent = durable_parent if isinstance(durable_parent, dict) else {}
    revision_id = str(checkpoint.get("revision_id") or "")
    revision_records = artifacts.get("turtlebot3_recovery_revisions")
    revision_records = (
        revision_records if isinstance(revision_records, dict) else {}
    )
    current_revision_record = artifacts.get("turtlebot3_recovery_revision")
    current_revision_record = (
        current_revision_record
        if isinstance(current_revision_record, dict)
        else {}
    )
    durable_revision_record = revision_records.get(revision_id)
    durable_revision_record = (
        durable_revision_record
        if isinstance(durable_revision_record, dict)
        else {}
    )
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    embedded_checkpoint = execution.get("turtlebot3_recovery_checkpoint")
    embedded_checkpoint = (
        embedded_checkpoint if isinstance(embedded_checkpoint, dict) else {}
    )
    summary_checkpoint = summary.get("turtlebot3_recovery_checkpoint")
    summary_checkpoint = (
        summary_checkpoint if isinstance(summary_checkpoint, dict) else {}
    )
    execution_revision_lineage = execution.get("recovery_checkpoint_revision")
    execution_revision_lineage = (
        execution_revision_lineage
        if isinstance(execution_revision_lineage, dict)
        else {}
    )
    summary_revision_lineage = summary.get("recovery_checkpoint_revision")
    summary_revision_lineage = (
        summary_revision_lineage
        if isinstance(summary_revision_lineage, dict)
        else {}
    )
    computed_checkpoint_hash = _turtlebot3_recovery_checkpoint_content_hash(
        checkpoint
    )
    current_checkpoint_integrity_valid = (
        bool(checkpoint_hash)
        and checkpoint_hash == computed_checkpoint_hash
        and checkpoint_id
        == f"turtlebot3_recovery_checkpoint_{computed_checkpoint_hash[:12]}"
        and durable_child == checkpoint
        and embedded_checkpoint == checkpoint
        and summary_checkpoint == checkpoint
    )
    computed_parent_hash = _turtlebot3_recovery_checkpoint_content_hash(
        durable_parent
    )
    durable_parent_integrity_valid = (
        bool(parent_id and parent_hash)
        and parent_hash == computed_parent_hash
        and parent_id
        == f"turtlebot3_recovery_checkpoint_{computed_parent_hash[:12]}"
    )
    durable_revision_lineage_valid = (
        bool(revision_id)
        and current_revision_record == durable_revision_record
        and current_revision_record.get("schema_version")
        == "missionos_turtlebot3_recovery_checkpoint_revision.v1"
        and current_revision_record.get("revision_status") == "proposed"
        and str(current_revision_record.get("revision_id") or "")
        == revision_id
        and str(current_revision_record.get("parent_checkpoint_id") or "")
        == parent_id
        and str(current_revision_record.get("parent_checkpoint_hash") or "")
        == parent_hash
        and current_revision_record.get("turtlebot3_recovery_checkpoint")
        == checkpoint
        and current_revision_record.get("superseded_checkpoint")
        == durable_parent
        and current_revision_record.get("turtlebot3_home_mission_execution")
        == execution
        and current_revision_record.get("summary") == summary
        and execution_revision_lineage == summary_revision_lineage
        and str(execution_revision_lineage.get("revision_id") or "")
        == revision_id
        and str(execution_revision_lineage.get("parent_checkpoint_id") or "")
        == parent_id
        and str(execution_revision_lineage.get("child_checkpoint_id") or "")
        == checkpoint_id
        and str(execution_revision_lineage.get("revision_intent") or "")
        == str(checkpoint.get("revision_intent") or "")
        and execution_revision_lineage.get("operator_approval_created") is False
        and execution_revision_lineage.get("dispatch_authority_created") is False
        and execution_revision_lineage.get("physical_execution_invoked") is False
        and execution_revision_lineage.get("progress_counted") is False
    )
    lineage_valid = (
        bool(parent_id and parent_hash and checkpoint_id)
        and current_checkpoint_integrity_valid
        and durable_parent_integrity_valid
        and durable_revision_lineage_valid
        and durable_parent.get("checkpoint_status") == "superseded"
        and str(durable_parent.get("checkpoint_hash") or "") == parent_hash
        and str(durable_parent.get("superseded_by_checkpoint_id") or "")
        == checkpoint_id
        and str(durable_parent.get("superseded_by_checkpoint_hash") or "")
        == str(checkpoint.get("checkpoint_hash") or "")
        and str(durable_parent.get("superseded_by_revision_id") or "")
        == revision_id
        and str(durable_parent.get("superseded_by_revision_ref") or "")
        == revision_id
    )
    no_authority = (
        str(task.get("status") or "").strip().lower() == "pending"
        and checkpoint.get("checkpoint_status") == "awaiting_operator_approval"
        and checkpoint.get("dispatch_authority_created") is False
        and checkpoint.get("physical_execution_invoked") is False
        and summary.get("recovery_dispatch_request_sent") is not True
        and not _binds_current_checkpoint(operator_approval)
        and not _binds_current_checkpoint(bounded_action)
        and not receipt_has_current_authority
    )
    return (
        pending,
        no_authority,
        lineage_valid,
        {
            "task_status": str(task.get("status") or "").strip().lower(),
            "checkpoint_status": str(
                checkpoint.get("checkpoint_status") or ""
            ).strip(),
            "checkpoint_id": checkpoint_id,
            "checkpoint_hash": checkpoint_hash,
        },
    )


def _pending_revision_is_child_of_context(
    pending: Mapping[str, Any] | None,
    context: Mapping[str, str],
    *,
    operator_instruction_sha256: str = "",
) -> bool:
    if not isinstance(pending, Mapping):
        return False
    child_matches = (
        str(pending.get("checkpoint_id") or "") != context.get("checkpoint_id")
        and str(pending.get("checkpoint_hash") or "")
        != context.get("checkpoint_hash")
        and str(pending.get("parent_checkpoint_id") or "")
        == context.get("checkpoint_id")
        and str(pending.get("parent_checkpoint_hash") or "")
        == context.get("checkpoint_hash")
    )
    if not child_matches:
        return False
    if operator_instruction_sha256:
        return (
            str(pending.get("operator_instruction_sha256") or "")
            == operator_instruction_sha256
        )
    return True


def _show_concurrent_turtlebot3_recovery_revision(
    ctx: click.Context,
    *,
    task_id: str,
    pending: Mapping[str, Any],
) -> None:
    _clear_chat_recovery_revision_context(ctx)
    console.print(
        "[yellow]A different checkpoint-bound operator instruction replaced "
        "the reviewed checkpoint first. Your instruction is not claimed as "
        "accepted; no approval or dispatch was sent. Review the durable latest "
        "proposal before deciding.[/yellow]"
    )
    console.print(_render_chat_recovery_review(dict(pending)))
    _set_chat_suggestion(
        ctx,
        raw=f"/review-recovery {task_id}",
        label="review latest recovery",
    )


def _reviewed_turtlebot3_revision_binding_is_active(
    revision_state: Mapping[str, str],
    context: Mapping[str, str],
) -> bool:
    return (
        revision_state.get("task_status") == "pending"
        and revision_state.get("checkpoint_status")
        == "awaiting_operator_approval"
        and revision_state.get("checkpoint_id") == context.get("checkpoint_id")
        and revision_state.get("checkpoint_hash")
        == context.get("checkpoint_hash")
    )


def _show_unaccepted_turtlebot3_recovery_revision(
    ctx: click.Context,
    *,
    context: Mapping[str, str],
    revision_state: Mapping[str, str],
    latest_pending: Mapping[str, Any] | None,
    reason: str,
) -> None:
    if _reviewed_turtlebot3_revision_binding_is_active(revision_state, context):
        console.print(
            "[yellow]No replacement recovery checkpoint was accepted; no "
            "approval or dispatch was sent by this revision request. The reviewed "
            "checkpoint was still pending at refetch, so its exact binding "
            f"remains in revision mode. reason={rich_escape(reason)}[/yellow]"
        )
        return

    _clear_chat_recovery_revision_context(ctx)
    task_id = str(context.get("task_id") or "")
    task_status = revision_state.get("task_status") or "unknown"
    checkpoint_status = revision_state.get("checkpoint_status") or "unknown"
    console.print(
        "[yellow]The reviewed checkpoint binding is no longer active. Your "
        "revision is not claimed as accepted; this revision request did not "
        "send approval or dispatch. "
        f"task_status={rich_escape(task_status)}; "
        f"checkpoint_status={rich_escape(checkpoint_status)}; "
        f"reason={rich_escape(reason)}[/yellow]"
    )
    if (
        isinstance(latest_pending, Mapping)
        and task_status == "pending"
        and checkpoint_status == "awaiting_operator_approval"
    ):
        console.print(_render_chat_recovery_review(dict(latest_pending)))
        _set_chat_suggestion(
            ctx,
            raw=f"/review-recovery {task_id}",
            label="review latest recovery",
        )
        return
    _set_chat_suggestion(
        ctx,
        raw=f"/job-status {task_id}",
        label="check recovery task status",
    )


def _handle_chat_recovery_revision_instruction(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    *,
    operator_instruction: str,
) -> bool:
    context = _chat_recovery_revision_context(ctx)
    if not context:
        return False
    operator_instruction_sha256 = hashlib.sha256(
        operator_instruction.encode("utf-8")
    ).hexdigest()
    _clear_chat_suggestion(ctx)
    try:
        with console.status(
            "[cyan]Recovery Agent: revising the pending checkpoint…[/cyan]",
            spinner="dots",
        ):
            payload = client.turtlebot3_recovery_revision(
                task_id=context["task_id"],
                operator_instruction=operator_instruction,
                expected_recovery_checkpoint_id=context["checkpoint_id"],
                expected_recovery_checkpoint_hash=context["checkpoint_hash"],
            )
            if not isinstance(payload, dict):
                raise TypeError("recovery revision response must be an object")
            (
                pending,
                no_refetched_authority,
                refetched_lineage_valid,
                refetched_revision_state,
            ) = _refetched_turtlebot3_revision_state(
                client,
                task_id=context["task_id"],
            )
    except Exception as exc:
        try:
            (
                recovered_pending,
                recovered_no_authority,
                recovered_lineage_valid,
                recovered_revision_state,
            ) = _refetched_turtlebot3_revision_state(
                client,
                task_id=context["task_id"],
            )
        except Exception:
            recovered_pending = None
            recovered_no_authority = False
            recovered_lineage_valid = False
            recovered_revision_state = {}
        if (
            recovered_no_authority
            and recovered_lineage_valid
            and _pending_revision_is_child_of_context(
                recovered_pending,
                context,
            )
        ):
            if not _pending_revision_is_child_of_context(
                recovered_pending,
                context,
                operator_instruction_sha256=operator_instruction_sha256,
            ):
                _show_concurrent_turtlebot3_recovery_revision(
                    ctx,
                    task_id=context["task_id"],
                    pending=recovered_pending or {},
                )
                return True
            _clear_chat_recovery_revision_context(ctx)
            console.print(
                "[green]The response was interrupted, but the durable task shows "
                "one source-bound child checkpoint with no approval or dispatch. "
                "Reviewing that child now.[/green]"
            )
            console.print(_render_chat_recovery_review(recovered_pending))
            _set_chat_suggestion(
                ctx,
                raw=f"/review-recovery {context['task_id']}",
                label="review revised recovery",
            )
            return True
        if recovered_revision_state:
            _show_unaccepted_turtlebot3_recovery_revision(
                ctx,
                context=context,
                revision_state=recovered_revision_state,
                latest_pending=(
                    recovered_pending if recovered_lineage_valid else None
                ),
                reason=type(exc).__name__,
            )
            return True
        console.print(
            "[yellow]Recovery revision could not be verified; no approval or "
            "dispatch was sent by this request. The client retains only its local "
            "review binding because current task state could not be refetched. "
            f"error={rich_escape(type(exc).__name__)}[/yellow]"
        )
        return True

    durable_child_of_context = (
        no_refetched_authority
        and refetched_lineage_valid
        and _pending_revision_is_child_of_context(pending, context)
    )
    if durable_child_of_context and not _pending_revision_is_child_of_context(
        pending,
        context,
        operator_instruction_sha256=operator_instruction_sha256,
    ):
        _show_concurrent_turtlebot3_recovery_revision(
            ctx,
            task_id=context["task_id"],
            pending=pending or {},
        )
        return True
    if durable_child_of_context:
        _clear_chat_recovery_revision_context(ctx)
        console.print(
            "[green]A new checkpoint-bound recovery proposal is ready for review. "
            "No approval artifact or dispatch was created.[/green]"
        )
        console.print(_render_chat_recovery_review(pending))
        _set_chat_suggestion(
            ctx,
            raw=f"/review-recovery {context['task_id']}",
            label="review revised recovery",
        )
        return True

    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    blocked_reasons = [str(item) for item in summary.get("blocked_reasons") or []]
    reason_text = ", ".join(blocked_reasons) or "revision_result_not_source_bound"
    _show_unaccepted_turtlebot3_recovery_revision(
        ctx,
        context=context,
        revision_state=refetched_revision_state,
        latest_pending=pending if refetched_lineage_valid else None,
        reason=reason_text,
    )
    return True


def _has_bounded_recovery_xy(parameters: Mapping[str, Any]) -> bool:
    def _finite_number(value: Any) -> bool:
        number = _as_float(value)
        return number is not None and math.isfinite(number)

    if _finite_number(parameters.get("target_x_m")) and _finite_number(
        parameters.get("target_y_m")
    ):
        return True
    waypoints = parameters.get("recovery_waypoints")
    return (
        isinstance(waypoints, list)
        and bool(waypoints)
        and all(
            isinstance(waypoint, dict)
            and _finite_number(waypoint.get("target_x_m"))
            and _finite_number(waypoint.get("target_y_m"))
            for waypoint in waypoints
        )
    )


def _render_chat_recovery_review(pending: dict[str, Any]) -> Panel:
    parameters = pending.get("recovery_parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    parameter_text = (
        ", ".join(
            f"{key}={_recovery_command_number(value)}"
            for key, value in sorted(parameters.items())
        )
        if parameters
        else "-"
    )
    observations = pending.get("input_observations")
    observations = observations if isinstance(observations, dict) else {}
    evidence_keys = (
        "runtime_obstacle_observed",
        "costmap_obstacle_observed",
        "robot_motion_observed",
        "odom_delta_m",
        "motion_observation_source",
    )
    evidence_text = ", ".join(
        f"{key}={_status_text(observations.get(key))}"
        for key in evidence_keys
        if observations.get(key) is not None
    ) or "exact referenced evidence unavailable"
    provider = str(pending.get("llm_provider") or "")
    model = str(pending.get("llm_model_id") or "")
    planner_text = "/".join(value for value in (provider, model) if value) or str(
        pending.get("proposal_source") or "-"
    )
    checkpoint_id = str(pending.get("checkpoint_id") or "")
    checkpoint_hash = str(pending.get("checkpoint_hash") or "")
    checkpoint_text = checkpoint_id or "not-applicable"
    if checkpoint_hash:
        checkpoint_text += f" hash={checkpoint_hash[:12]}"
    reason = str(pending.get("proposal_reason") or "").strip() or "-"
    approval_supported = pending.get("checkpoint_approval_supported") is True
    revision_supported = pending.get("checkpoint_revision_supported") is True
    operator_guidance_required = pending.get("operator_guidance_required") is True
    if approval_supported and revision_supported:
        decision_text = (
            "[bold]y[/bold]=approve exact checkpoint  "
            "[bold]d/Enter[/bold]=defer with no dispatch  "
            "[bold]c[/bold]=change by natural-language proposal"
        )
    elif approval_supported:
        decision_text = (
            "[bold]y[/bold]=approve exact checkpoint  "
            "[bold]d/Enter[/bold]=defer with no dispatch  "
            "change unavailable for this robot profile"
        )
    elif revision_supported and operator_guidance_required:
        decision_text = (
            "Gemini requested operator guidance; this checkpoint cannot dispatch.  "
            "[bold]c[/bold]=give a bounded change in natural language  "
            "[bold]d/Enter[/bold]=defer with no dispatch"
        )
    else:
        decision_text = (
            "[bold]d/Enter[/bold]=defer with no dispatch  "
            "approval/change unavailable for this robot scope"
        )
    lines = [
        f"task_id={rich_escape(str(pending.get('task_id') or '-'))}",
        f"action={rich_escape(str(pending.get('recovery_action') or '-'))}",
        f"parameters={rich_escape(parameter_text)}",
        f"reason={rich_escape(reason)}",
        f"evidence={rich_escape(evidence_text)}",
        f"planner={rich_escape(planner_text)}",
        f"checkpoint={rich_escape(checkpoint_text)}",
        "dispatch_authority=False · physical_execution_invoked=False",
        "",
        decision_text,
    ]
    return Panel(
        "\n".join(lines),
        title="Recovery Agent Proposal Review",
        border_style="yellow",
    )


def _handle_chat_recovery_approval(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    *,
    explicit_task_id: str,
    quiet_if_missing: bool = False,
    expected_checkpoint_id: str = "",
    expected_checkpoint_hash: str = "",
) -> bool:
    try:
        task_id = _resolve_operator_recovery_task_id(
            client,
            explicit_task_id=explicit_task_id,
            stored_task_id=_stored_sitl_task_id(ctx),
        )
        pending = _lookup_pending_recovery_approval(client, task_id=task_id)
    except click.ClickException as exc:
        if quiet_if_missing:
            return False
        console.print(f"[yellow]{exc.message}[/yellow]")
        return True
    if not pending:
        if quiet_if_missing:
            return False
        console.print(
            "[yellow]No pending Recovery Agent proposal requires new human approval.[/yellow]"
        )
        return True
    if pending.get("operator_guidance_required") is True:
        if _set_chat_recovery_revision_context(ctx, pending=pending):
            console.print(
                "[yellow]Gemini requested operator guidance. This proposal-only "
                "checkpoint cannot be approved or dispatched. Type a bounded "
                "natural-language change such as '右へ大きく迂回して障害物を避けて'. "
                "No approval artifact or dispatch was created.[/yellow]"
            )
        else:
            console.print(
                "[yellow]This proposal-only checkpoint cannot be approved or "
                "dispatched, and its revision binding is incomplete. No approval "
                "artifact or dispatch was created.[/yellow]"
            )
        return True
    if pending.get("checkpoint_approval_supported") is not True:
        console.print(
            "[yellow]Checkpoint approval is unavailable because the durable plan, "
            "current checkpoint, and summary do not all identify TurtleBot3 on "
            "ros2_nav2_turtlebot3_sim. No approval artifact or dispatch was "
            "created.[/yellow]"
        )
        return True
    current_checkpoint_id = str(pending.get("checkpoint_id") or "")
    current_checkpoint_hash = str(pending.get("checkpoint_hash") or "")
    if (
        expected_checkpoint_id
        and current_checkpoint_id != expected_checkpoint_id
    ) or (
        expected_checkpoint_hash
        and current_checkpoint_hash != expected_checkpoint_hash
    ):
        console.print(
            "[yellow]The pending recovery checkpoint changed after review; "
            "no dispatch was sent. Review the latest proposal again.[/yellow]"
        )
        _set_chat_suggestion(
            ctx,
            raw=f"/review-recovery {pending.get('task_id') or task_id}",
            label="review latest recovery",
        )
        return True
    action = str(pending.get("recovery_action") or "")
    parameters = pending.get("recovery_parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    if action in {"avoid_obstacle", "reroute"} and not _has_bounded_recovery_xy(
        parameters
    ):
        console.print(
            "[yellow]Pending recovery proposal is missing bounded recovery "
            "coordinates; no dispatch was sent.[/yellow]"
        )
        return True
    _clear_chat_recovery_revision_context(ctx)
    _clear_chat_back_stack(ctx)
    with console.status("[cyan]approving recovery proposal…[/cyan]", spinner="dots"):
        payload = client.recovery_dispatch(
            task_id=str(pending.get("task_id") or task_id),
            recovery_action=action,
            recovery_parameters=parameters,
            expected_recovery_checkpoint_id=(
                expected_checkpoint_id or current_checkpoint_id
            ),
            expected_recovery_checkpoint_hash=(
                expected_checkpoint_hash or current_checkpoint_hash
            ),
        )
        response_summary = payload.get("summary")
        response_summary = (
            response_summary if isinstance(response_summary, dict) else {}
        )
        blocked_reasons = [
            str(item) for item in response_summary.get("blocked_reasons") or []
        ]
        reviewed_checkpoint_changed = any(
            reason.startswith("reviewed_turtlebot3_recovery_checkpoint_")
            or reason == "turtlebot3_recovery_checkpoint_claim_conflict"
            for reason in blocked_reasons
        )
        task_payload = (
            payload
            if reviewed_checkpoint_changed and isinstance(payload.get("task"), dict)
            else _wait_for_active_runner_recovery_observation(client, payload)
        )
    _print_recovery_result(payload, task_payload=task_payload)
    if reviewed_checkpoint_changed:
        console.print(
            "[yellow]The reviewed checkpoint was no longer current; no approval "
            "authority or dispatch was created. Review the latest task state.[/yellow]"
        )
        _set_chat_suggestion(
            ctx,
            raw=f"/review-recovery {pending.get('task_id') or task_id}",
            label="review latest recovery",
        )
        return True
    _set_chat_suggestion(
        ctx,
        raw=f"/job-status {pending.get('task_id') or task_id}",
        label="show status",
    )
    return True


def _handle_chat_recovery_review(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    *,
    explicit_task_id: str,
) -> bool:
    try:
        task_id = _resolve_operator_recovery_task_id(
            client,
            explicit_task_id=explicit_task_id,
            stored_task_id=_stored_sitl_task_id(ctx),
        )
        pending = _lookup_pending_recovery_approval(client, task_id=task_id)
    except click.ClickException as exc:
        console.print(f"[yellow]{exc.message}[/yellow]")
        _clear_chat_suggestion(ctx)
        return True
    if not pending:
        console.print(
            "[yellow]No pending Recovery Agent proposal is available for review.[/yellow]"
        )
        _clear_chat_suggestion(ctx)
        return True
    _clear_chat_suggestion(ctx)
    console.print(_render_chat_recovery_review(pending))
    approval_supported = pending.get("checkpoint_approval_supported") is True
    revision_supported = pending.get("checkpoint_revision_supported") is True
    operator_guidance_required = pending.get("operator_guidance_required") is True
    if approval_supported and revision_supported:
        decision_prompt = "Recovery decision [y=approve, d/Enter=defer, c=change]"
    elif approval_supported:
        decision_prompt = "Recovery decision [y=approve, d/Enter=defer]"
    elif revision_supported and operator_guidance_required:
        decision_prompt = "Recovery decision [c=bounded change, d/Enter=defer]"
    else:
        decision_prompt = "Recovery decision [d/Enter=defer]"
    choice = str(
        click.prompt(
            decision_prompt,
            default="d",
            show_default=False,
        )
    ).strip()
    normalized = choice.lower()
    compact = normalized.replace(" ", "").replace("　", "")
    if compact in {"y", "yes", "はい", "承認", "承認します"}:
        if not approval_supported:
            _clear_chat_recovery_revision_context(ctx)
            if operator_guidance_required and revision_supported:
                _set_chat_recovery_revision_context(ctx, pending=pending)
                console.print(
                    "[yellow]This proposal-only checkpoint cannot be approved or "
                    "dispatched. Type a bounded natural-language change. No approval "
                    "artifact or dispatch was created.[/yellow]"
                )
            else:
                console.print(
                    "[yellow]Approval is unavailable for this robot scope; no approval "
                    "artifact or dispatch was created. Choose d/Enter to defer.[/yellow]"
                )
            return True
        _clear_chat_recovery_revision_context(ctx)
        return _handle_chat_recovery_approval(
            ctx,
            client,
            explicit_task_id=str(pending.get("task_id") or task_id),
            expected_checkpoint_id=str(pending.get("checkpoint_id") or ""),
            expected_checkpoint_hash=str(pending.get("checkpoint_hash") or ""),
        )
    if compact in {
        "",
        "d",
        "defer",
        "n",
        "no",
        "いいえ",
        "保留",
        "キャンセル",
    }:
        _clear_chat_recovery_revision_context(ctx)
        console.print(
            "[yellow]Deferred; no approval artifact or dispatch was created. "
            "The checkpoint remains pending.[/yellow]"
        )
        return True
    if compact in {"c", "change", "revise", "revision", "変更", "修正", "再提案"}:
        if not revision_supported:
            console.print(
                "[yellow]Checkpoint-bound natural-language revision is only "
                "verified for TurtleBot3 on ros2_nav2_turtlebot3_sim. No "
                "approval artifact or dispatch was created.[/yellow]"
            )
            return True
        if not _set_chat_recovery_revision_context(ctx, pending=pending):
            console.print(
                "[yellow]This proposal has no TurtleBot3 checkpoint binding, so "
                "checkpoint-bound natural-language revision is unavailable. No "
                "approval artifact or dispatch was created.[/yellow]"
            )
            return True
        console.print(
            "[yellow]Recovery revision mode is active for this exact checkpoint. "
            "The robot remains stopped and the current checkpoint remains pending. "
            "Type a natural-language alternative such as '左に大きく旋回してかわして' "
            "or '出発地点へ引き返して'. No approval or dispatch occurs "
            "until the revised checkpoint is reviewed and approved.[/yellow]"
        )
        return True
    allowed_choices = (
        "y, d/Enter, or c"
        if approval_supported and revision_supported
        else "c or d/Enter"
        if revision_supported and operator_guidance_required
        else "y or d/Enter"
        if approval_supported
        else "d/Enter"
    )
    console.print(
        "[yellow]Unknown recovery decision. Choose "
        f"{allowed_choices}. No approval artifact or dispatch was created; the "
        "checkpoint remains pending.[/yellow]"
    )
    return True


def _is_home_robot_nav2_execution_target(value: Any) -> bool:
    return str(value or "") in {
        "ros2_nav2_turtlebot3_sim",
        "ros2_nav2_turtlebot4_sim",
        "isaac_ros_nav2_nova_carter_sim",
    }


def _is_turtlebot3_task_artifacts(artifacts: dict[str, Any]) -> bool:
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    execution = artifacts.get("turtlebot3_home_mission_execution")
    execution = execution if isinstance(execution, dict) else {}
    indoor_map = artifacts.get("turtlebot3_indoor_map_model")
    return (
        _is_home_robot_nav2_execution_target(summary.get("execution_target"))
        or _is_home_robot_nav2_execution_target(execution.get("execution_target"))
        or isinstance(indoor_map, dict)
    )


def _is_real_mission_designer_sitl_task(task: dict[str, Any]) -> bool:
    """Return true for production Mission Designer SITL tasks.

    Loopback/runtime smoke tests can intentionally create small synthetic
    `mission_designer_sitl_execution` tasks. They may remain `running` after a
    local smoke, but they are not live user flights and should never be selected
    by operator commands with omitted --task-id.
    """

    kind = str(task.get("kind") or "")
    if kind == "px4_gazebo_mission_designer_sitl_execution_request":
        return True
    artifacts = task.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    return "px4_gazebo_mission_designer_sitl_execution_request" in artifacts


def _task_has_active_auto_runner_request_path(task: dict[str, Any]) -> bool:
    artifacts = task.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    receipt = artifacts.get("missionos_auto_mission_gui_dispatch_running_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    return bool(receipt.get("operator_recovery_request_container_path"))


def _latest_running_sitl_task_id(
    client: MissionOSGatewayClient,
    *,
    prefer_active_runner: bool = False,
    require_active_runner: bool = False,
) -> str | None:
    """Find the most recent running production Mission Designer SITL task."""
    try:
        payload = client.get("/tasks?page=1&page_size=20")
    except click.ClickException:
        return None
    items = payload.get("items") or payload.get("tasks") or []
    if not isinstance(items, list):
        return None
    candidates: list[dict[str, Any]] = []
    for task in items:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or task.get("task_status") or "")
        has_active_runner = _task_has_active_auto_runner_request_path(task)
        if status != "running" or (
            not _is_real_mission_designer_sitl_task(task)
            and not has_active_runner
        ):
            continue
        candidates.append(task)
    if prefer_active_runner:
        active = [task for task in candidates if _task_has_active_auto_runner_request_path(task)]
        if active:
            candidates = active
    if require_active_runner:
        candidates = [task for task in candidates if _task_has_active_auto_runner_request_path(task)]
    for task in candidates:
        task_id = task.get("task_id")
        if task_id:
            return str(task_id)
    return None


def _resolve_live_task_id(
    client: MissionOSGatewayClient,
    *,
    explicit_task_id: str,
    stored_task_id: str,
) -> str:
    """Resolve which task a live view should attach to.

    An explicit --task-id always wins. Otherwise prefer the actual running SITL
    task (so a stale stored id like a leftover placeholder does not 404), and
    only fall back to the stored id when nothing is running.
    """
    if explicit_task_id:
        return explicit_task_id
    running = _latest_running_sitl_task_id(
        client,
        prefer_active_runner=True,
        require_active_runner=True,
    )
    if running:
        return running
    if stored_task_id:
        return stored_task_id
    running = _latest_running_sitl_task_id(client)
    if running:
        return running
    raise click.ClickException(
        "no running SITL task found; run a flight first or pass --task-id"
    )


def _resolve_operator_recovery_task_id(
    client: MissionOSGatewayClient,
    *,
    explicit_task_id: str,
    stored_task_id: str,
) -> str:
    if explicit_task_id:
        return explicit_task_id
    if stored_task_id:
        try:
            payload = client.get(f"/tasks/{quote(stored_task_id, safe='')}")
        except (click.ClickException, OSError):
            payload = {}
        task = payload.get("task") if isinstance(payload, dict) else None
        artifacts = (
            task.get("artifacts")
            if isinstance(task, dict) and isinstance(task.get("artifacts"), dict)
            else {}
        )
        checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
        if (
            isinstance(task, dict)
            and task.get("kind") == "turtlebot3_home_mission_execution"
            and str(task.get("status") or "").lower() == "pending"
            and isinstance(checkpoint, dict)
            and checkpoint.get("checkpoint_status")
            == "awaiting_operator_approval"
        ):
            return stored_task_id
    running = _latest_running_sitl_task_id(
        client,
        prefer_active_runner=True,
        require_active_runner=True,
    )
    if running:
        return running
    raise click.ClickException(
        "no active live SITL runner or pending TurtleBot3 recovery checkpoint found; "
        "start a fresh live mission, or pass --task-id explicitly"
    )


def _proposal_signature(
    proposal: dict[str, Any] | None,
) -> tuple[str, tuple[str, ...]] | None:
    if not proposal:
        return None
    return (proposal.get("action", ""), tuple(sorted(proposal.get("risks", []))))


@dataclass
class ProposalGate:
    """Re-display gate for recovery proposals.

    A dismissed proposal is hidden until the cooldown elapses, then re-surfaces.
    A different (escalated) proposal signature bypasses the cooldown and shows
    immediately so the operator is not kept waiting on a worse situation.
    """

    cooldown_seconds: float = PROPOSAL_REDISPLAY_SECONDS
    dismissed_signature: tuple[str, tuple[str, ...]] | None = None
    dismissed_at: float = 0.0

    def should_show(self, proposal: dict[str, Any] | None, now: float) -> bool:
        if not proposal:
            return False
        signature = _proposal_signature(proposal)
        if (
            self.dismissed_signature is not None
            and signature == self.dismissed_signature
        ):
            return (now - self.dismissed_at) >= self.cooldown_seconds
        return True

    def dismiss(self, proposal: dict[str, Any] | None, now: float) -> None:
        self.dismissed_signature = _proposal_signature(proposal)
        self.dismissed_at = now


def _render_action_panel(proposal: dict[str, Any], *, confirming: str | None) -> Panel:
    risks = ", ".join(proposal.get("risks", [])) or "-"
    parameters = proposal.get("parameters")
    parameter_text = (
        ", ".join(f"{key}={value}" for key, value in sorted(parameters.items()))
        if isinstance(parameters, dict) and parameters
        else "-"
    )
    lines = [
        f"[bold]Agent Proposal:[/bold] {proposal.get('action', '-')}   "
        f"[dim](status={proposal.get('status', '-')}; dispatch_authority=False)[/dim]",
        f"[dim]risk = {risks}[/dim]",
        f"[dim]params = {parameter_text}[/dim]",
        "",
    ]
    if confirming:
        label = _OPERATOR_RECOVERY_ACTIONS.get(confirming, confirming)
        lines.append(
            f"[bold red]Send {label}. Press[/bold red] [bold]y[/bold]"
            "[bold red] to execute; any other key cancels.[/bold red]"
        )
        border = "red"
    else:
        lines.append(
            "[green]Default: do nothing (no dispatch)[/green]   "
            "[dim]proposal will reappear in 30s[/dim]"
        )
        if str(proposal.get("action") or "") in {"return_to_launch", "land"}:
            lines.append(
                "  [bold]r[/bold]=approve RTL (requires y)   "
                "[bold]l[/bold]=approve LAND (requires y)   "
                "[bold]d[/bold]/Esc=view status   [bold]q[/bold]=quit"
            )
        else:
            lines.append(
                "  type [bold]climb <m>[/bold] / [bold]speed <m/s>[/bold] / "
                "[bold]reroute <x> <y> (alt)[/bold] / [bold]avoid <x> <y> (alt)[/bold]   "
                "[bold]d[/bold]/Esc=view status   [bold]q[/bold]=quit"
            )
        border = "yellow"
    return Panel("\n".join(lines), title="Operator Action", border_style=border)


_RECOVERY_RISK_LABELS = {
    "battery_projected_insufficient_for_route": "battery insufficient to complete route",
    "battery_projected_insufficient_for_return_home": "battery insufficient to return home",
    "terrain_clearance_below_minimum": "terrain clearance below minimum",
    "route_deviation_above_limit": "route deviation above limit",
    "telemetry_stale": "telemetry is stale",
    "obstacle_or_building_risk": "obstacle or building risk",
}


def _humanize_risks(risks: list[str]) -> str:
    if not risks:
        return "none"
    return ", ".join(_RECOVERY_RISK_LABELS.get(r, r) for r in risks)


def _humanize_recovery_summary(
    proposal: dict[str, Any],
    endurance: dict[str, Any],
    return_home: dict[str, Any],
) -> list[str]:
    """Plain-language situation + return feasibility + recommendation for a human."""
    route_computed = _projection_computed(endurance)
    rtl_computed = _projection_computed(return_home)
    needs = _as_float(endurance.get("projected_battery_required_percent"))
    route_arrival = _as_float(endurance.get("projected_arrival_battery_percent"))
    route_infeasible = route_computed and (
        (needs is not None and needs > 100.0)
        or (route_arrival is not None and route_arrival < 0.0)
    )
    rtl_insufficient = (
        rtl_computed and return_home.get("projected_insufficient_for_return_home") is True
    )
    rtl_arrival = _as_float(return_home.get("projected_return_arrival_battery_percent"))
    home_m = return_home.get("distance_to_home_m")

    lines: list[str] = []
    if not route_computed:
        lines.append(
            "[yellow]Situation:[/yellow] Route battery projection is unavailable "
            f"({_status_text(endurance.get('projection_status'))})."
        )
    elif route_infeasible and needs is not None:
        lines.append(
            f"[bold red]Situation:[/bold red] This route cannot be completed "
            f"(requires about {needs / 100.0:.1f}x the available battery; "
            "continuing risks depletion)."
        )
    elif route_infeasible:
        lines.append("[bold red]Situation:[/bold red] This route cannot be completed (battery shortfall).")
    else:
        lines.append("[green]Situation:[/green] The route appears battery-feasible.")
    if proposal.get("risks"):
        lines.append(f"[dim]Detected:[/dim] {_humanize_risks(proposal['risks'])}.")
    if return_home:
        if not rtl_computed:
            home_txt = f" (home {_fmt_metres(home_m)})" if home_m is not None else ""
            lines.append(
                "[yellow]Return:[/yellow] RTL battery projection is unavailable"
                f"{home_txt}."
            )
        elif not rtl_insufficient:
            extra = f"; arrival battery {rtl_arrival:.0f}%" if rtl_arrival is not None else ""
            home_txt = (
                f" (home {_fmt_metres(home_m)}{extra})" if home_m is not None else ""
            )
            lines.append(f"[green]Return:[/green] Returning now appears safe{home_txt}.")
        else:
            lines.append("[bold red]Return:[/bold red] Battery is also tight for RTL.")
    if proposal.get("risks"):
        rec = "[bold]-> Operator review required; continuing is not recommended until active risks are resolved.[/bold]"
    elif not route_computed:
        rec = "[bold]-> Operator review required; do not treat route battery as verified.[/bold]"
    elif route_infeasible and return_home and rtl_computed and not rtl_insufficient:
        rec = "[bold]-> RTL (`missionos rtl`) is usually appropriate. Continuing is not recommended.[/bold]"
    elif route_infeasible and (rtl_insufficient or not rtl_computed):
        rec = "[bold]-> Consider LAND (`missionos land`); RTL battery margin is also tight.[/bold]"
    else:
        rec = "[bold]-> Continuing appears acceptable; the proposal is advisory.[/bold]"
    if proposal.get("action") == "operator_review":
        rec += " [dim](the agent leaves the final decision to the operator)[/dim]"
    lines.append(rec)
    return lines


def _render_recovery_agent_console(
    task_payload: dict[str, Any],
    *,
    proposal: dict[str, Any] | None,
    show_proposal: bool,
    status: str,
    task_id: str = "",
) -> Panel:
    """Operator console for the Runtime Recovery Agent: recognition + proposal + how to act.

    Rendered at the top of `operate` so it is always visible (never scrolled off).
    """
    artifacts = _task_artifacts(task_payload)
    is_home_robot = _is_turtlebot3_task_artifacts(artifacts)
    robot_label = _turtlebot_robot_label_from_artifacts(artifacts)
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    telemetry = bridge.get("telemetry_snapshot")
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    battery = telemetry.get("battery") if isinstance(telemetry.get("battery"), dict) else {}
    endurance = battery.get("endurance_projection")
    endurance = endurance if isinstance(endurance, dict) else {}
    return_home = battery.get("return_home_projection")
    return_home = return_home if isinstance(return_home, dict) else {}
    pending = _pending_recovery_approval_from_task(task_payload)
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    checkpoint_status = str(checkpoint.get("checkpoint_status") or "")
    candidate_resolution = (
        _turtlebot3_recovery_candidate_resolution_from_artifacts(artifacts)
    )

    lines: list[str] = []
    if checkpoint_status == "dispatching":
        action = str(checkpoint.get("selected_action") or "recovery action")
        lines.extend(
            [
                "[bold cyan]Approved Recovery workflow is in progress[/bold cyan]",
                f"Action: [bold]{rich_escape(action)}[/bold]",
                "[green]A fresh operator approval is bound to this checkpoint.[/green]",
                "[dim]Nav2 goal="
                f"{rich_escape(_status_text(summary.get('recovery_goal_status')))}; "
                "verification="
                f"{rich_escape(_status_text(summary.get('recovery_verification_status')))}; "
                "route="
                f"{rich_escape(_status_text(summary.get('route_resume_status')))}[/dim]",
                "[dim]MissionOS is executing the approved recovery and any remaining "
                "route segments. The robot may pause while Nav2 replans or runs its "
                "local recovery behaviors. Do not approve the same checkpoint again.[/dim]",
            ]
        )
    elif pending:
        action = str(pending.get("selected_action") or "recovery action")
        operator_guidance_required = (
            pending.get("operator_guidance_required") is True
        )
        observations = pending.get("input_observations")
        observations = observations if isinstance(observations, dict) else {}
        reason = str(
            pending.get("proposal_reason")
            or observations.get("runtime_failure_source")
            or "runtime route failure"
        )
        selected_candidate = candidate_resolution.get("selected_candidate")
        selected_candidate = (
            selected_candidate if isinstance(selected_candidate, dict) else {}
        )
        planner = artifacts.get("recovery_planner_result")
        planner = planner if isinstance(planner, dict) else {}
        if not planner:
            planner = summary.get("recovery_planner_result")
            planner = planner if isinstance(planner, dict) else {}
        is_repair = bool(checkpoint.get("parent_checkpoint_id"))
        lines.extend(
            [
                "[bold yellow]Robot stopped — repair decision required[/bold yellow]"
                if is_repair
                else "[bold yellow]Robot stopped — recovery decision required[/bold yellow]",
                f"Recovery Agent proposes: [bold]{rich_escape(action)}[/bold]",
                f"[dim]Reason: {rich_escape(reason)}[/dim]",
                "[dim]Proposal source: "
                f"{rich_escape(_status_text(planner.get('proposal_source')))}; "
                f"checkpoint={rich_escape(_status_text(checkpoint.get('checkpoint_id')))}; "
                f"parent={rich_escape(_status_text(checkpoint.get('parent_checkpoint_id')))}[/dim]",
                "[dim]Candidate validation: "
                f"{rich_escape(_status_text(candidate_resolution.get('resolution_status')))}; "
                f"candidate={rich_escape(_status_text(selected_candidate.get('candidate_id')))}; "
                f"path={rich_escape(_status_text(selected_candidate.get('path_length_m')))}m; "
                f"global_max_cost={rich_escape(_status_text(selected_candidate.get('maximum_path_cost')))}; "
                f"local_max_cost={rich_escape(_status_text(selected_candidate.get('local_maximum_path_cost')))}; "
                f"bounded_retreat={rich_escape(_status_text(candidate_resolution.get('bounded_retreat_required')))}[/dim]",
                "[green]No recovery dispatch has been sent.[/green]",
            ]
        )
        if operator_guidance_required:
            lines.extend(
                [
                    "",
                    "[bold yellow]Gemini requested operator guidance; this "
                    "proposal-only checkpoint cannot dispatch.[/bold yellow]",
                    "  [bold]defer[/bold]    keep the robot stopped; create no authority",
                    "  type a bounded change in plain language, e.g. "
                    "[bold]右へ大きく迂回して障害物を避けて[/bold]",
                    "  [dim]approve is unavailable until that change creates a new "
                    "dispatchable checkpoint.[/dim]",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "[bold]Choose one:[/bold]",
                    "  [bold green]approve[/bold green]  execute this exact recovery "
                    "(asks y/N)",
                    "  [bold]defer[/bold]    keep the robot stopped; create no authority",
                    "  type a change in plain language, e.g. "
                    "[bold]左へ大きく迂回して[/bold]",
                ]
            )
    elif show_proposal and proposal:
        lines.extend(_humanize_recovery_summary(proposal, endurance, return_home))
        suggested = _operator_recovery_console_command(
            proposal.get("action"),
            proposal.get("parameters") if isinstance(proposal.get("parameters"), dict) else None,
        )
        if suggested:
            lines.append(
                "[bold yellow]Suggested command:[/bold yellow] "
                f"[bold]{suggested}[/bold] "
                "[dim](asks y/N before dispatch)[/dim]"
            )
        detail = (
            f"[dim]Details: proposal={proposal.get('action', '-')} "
            f"({proposal.get('status', '-')}; dispatch_authority=False); "
            f"risk={', '.join(proposal.get('risks', [])) or '-'}"
        )
        if endurance and _projection_computed(endurance):
            detail += (
                "; route "
                f"needs={_format_percent(endurance.get('projected_battery_required_percent'))}/"
                f"arrival={_format_percent(endurance.get('projected_arrival_battery_percent'))}/"
                f"burn={_format_percent(endurance.get('battery_burn_percent_per_km'))}per_km"
            )
        elif endurance:
            detail += (
                "; route projection="
                f"{_status_text(endurance.get('projection_status')) or 'unavailable'}"
            )
        if return_home and _projection_computed(return_home):
            detail += (
                "; RTL "
                f"home={_format_distance(return_home.get('distance_to_home_m'))}/"
                f"needs={_format_percent(return_home.get('projected_return_battery_required_percent'))}/"
                f"arrival={_format_percent(return_home.get('projected_return_arrival_battery_percent'))}"
            )
        elif return_home:
            detail += (
                "; RTL projection="
                f"{_status_text(return_home.get('projection_status')) or 'unavailable'}"
            )
        detail += "[/dim]"
        lines.append(detail)
    elif status == "running":
        lines.extend(
            [
                "[green]Mission running — no Recovery decision is pending.[/green]",
                "[dim]MissionOS is waiting for the current Nav2 result. If the robot "
                "stops and Recovery Agent creates a proposal, this panel will show "
                "approve / defer / change choices.[/dim]",
            ]
        )
    else:
        if is_home_robot:
            if status == "completed":
                if summary.get("runtime_recovery_triggered") is True:
                    lines.extend(
                        [
                            "[green]Mission completed after approved Recovery.[/green]",
                            "[dim]Recovery was proposed, explicitly approved, "
                            "completed, and the remaining route finished.[/dim]",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            "[green]Mission completed normally.[/green]",
                            "[dim]No Recovery condition was triggered, so Recovery "
                            "Agent created no proposal, approval request, or dispatch.[/dim]",
                        ]
                    )
            else:
                lines.append(
                    f"[dim]status={status} "
                    f"({rich_escape(robot_label)} recovery proposals appear only "
                    "during an active sim route)[/dim]"
                )
        else:
            lines.append(f"[dim]status={status} (proposals are shown only while flying)[/dim]")

    if not pending and checkpoint_status != "dispatching":
        lines.append("")
    tid = task_id or "<task>"
    if is_home_robot:
        if not pending and checkpoint_status != "dispatching":
            lines.append(
                f"[bold]status[/bold] refreshes evidence. [dim]Recovery changes "
                f"become available only after a proposal is displayed "
                f"(task={tid}) · exit: Ctrl-C[/dim]"
            )
    else:
        lines.append(
            "[dim]Type here; every dispatch still uses standard y/N confirmation:[/dim] "
            f"[bold]rtl[/bold] / [bold]land[/bold] / [bold]climb <m>[/bold] / "
            f"[bold]speed <m/s>[/bold] / [bold]reroute <x> <y> (alt)[/bold] / "
            f"[bold]avoid <x> <y> (alt)[/bold]  "
            f"[dim](task={tid}) · exit: Ctrl-C[/dim]"
        )
    border = (
        "cyan"
        if checkpoint_status == "dispatching"
        else "yellow"
        if (pending or (show_proposal and proposal))
        else "cyan"
    )
    return Panel(
        "\n".join(lines),
        title="Runtime Recovery Agent — operator console",
        border_style=border,
    )


@dataclass
class OperateConsoleCommand:
    kind: str
    action: str = ""
    parameters: dict[str, Any] | None = None
    assume_yes: bool = False


_OPERATE_CONSOLE_COMMANDS = (
    "status",
    "refresh",
    "wait",
    "help",
    "approve",
    "defer",
    "rtl",
    "land",
    "climb",
    "speed",
    "reroute",
    "avoid",
    "avoid-obstacle",
    "quit",
)

_OPERATE_RECOVERY_ACTION_ALIASES = {
    "rtl": "return_to_launch",
    "return": "return_to_launch",
    "return-to-launch": "return_to_launch",
    "return_to_launch": "return_to_launch",
    "land": "land",
    "climb": "adjust_altitude",
    "altitude": "adjust_altitude",
    "adjust-altitude": "adjust_altitude",
    "adjust_altitude": "adjust_altitude",
    "speed": "adjust_speed",
    "adjust-speed": "adjust_speed",
    "adjust_speed": "adjust_speed",
    "reroute": "reroute",
    "route": "reroute",
    "avoid": "avoid_obstacle",
    "avoid-obstacle": "avoid_obstacle",
    "avoid_obstacle": "avoid_obstacle",
}

_OPERATE_PARAMETER_ALIASES = {
    "alt": "target_altitude_m",
    "altitude": "target_altitude_m",
    "altitude_m": "target_altitude_m",
    "target_altitude": "target_altitude_m",
    "target_altitude_m": "target_altitude_m",
    "speed": "target_speed_mps",
    "speed_mps": "target_speed_mps",
    "target_speed": "target_speed_mps",
    "target_speed_mps": "target_speed_mps",
    "x": "target_x_m",
    "x_m": "target_x_m",
    "target_x": "target_x_m",
    "target_x_m": "target_x_m",
    "y": "target_y_m",
    "y_m": "target_y_m",
    "target_y": "target_y_m",
    "target_y_m": "target_y_m",
}


def _operate_console_help_panel(task_id: str, *, robot: str = "px4") -> Panel:
    robot_profile = _normalize_turtlebot_robot_profile(robot)
    if robot_profile in {"turtlebot3", "turtlebot4", "nova_carter"}:
        robot_label = _turtlebot_robot_label_from_profile(robot_profile)
        lines = [
            "[bold]Operator controls[/bold]",
            "  While running: status shows current Nav2 evidence",
            "  When Recovery stops the robot:",
            "    approve               approve the displayed recovery proposal",
            "    defer                 keep stopped; create no dispatch authority",
            "    or type a change: 左へ大きく迂回して / 障害物を避けて / 引き返して",
            f"  status                  show the latest {robot_label} sim state",
            "  quit                    exit operate",
            "",
            "[dim]Dispatches still go through recovery-dispatch and require human confirmation. "
            f"{robot_label} operate does not expose land/climb/speed/RTL flight controls.[/dim]",
        ]
    else:
        lines = [
            "[bold]Commands[/bold]",
            "  status | refresh        show the latest recovery/telemetry state",
            "  rtl                     request return-to-launch",
            "  land                    request land",
            "  climb 45                request altitude adjustment to 45 m above home",
            "  speed 7                 request speed adjustment to 7 m/s",
            "  reroute 120 -20 (45)    request local NED x/y target, optional altitude",
            "  avoid 40 20 (45)        request obstacle-avoidance target",
            "  quit                    exit operate",
            "",
            "[dim]Dispatches still go through recovery-dispatch and require human confirmation.[/dim]",
        ]
    return Panel(
        "\n".join(lines),
        title=f"Operate Commands · task={task_id}",
        border_style="cyan",
    )


def _build_operate_session(history_path: Path) -> PromptSession[str]:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.touch(exist_ok=True)
    return PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(list(_OPERATE_CONSOLE_COMMANDS), ignore_case=True),
        complete_while_typing=True,
        multiline=False,
        mouse_support=False,
    )


def _float_operate_argument(raw: Any, *, label: str) -> float:
    try:
        return float(str(raw).strip())
    except ValueError as exc:
        raise click.ClickException(f"{label} must be a number: {raw}") from exc


def _normalize_operate_parameter_key(raw: str) -> str:
    key = raw.strip().lstrip("-").replace("-", "_")
    return _OPERATE_PARAMETER_ALIASES.get(key, key)


def _parse_operate_console_parameters(
    action: str,
    tokens: list[str],
) -> tuple[dict[str, Any], bool]:
    assume_yes = False
    values: dict[str, Any] = {}
    positional: list[str] = []
    for token in tokens:
        if token == "--yes":
            assume_yes = True
            continue
        if "=" in token:
            key, value = token.split("=", 1)
            key = _normalize_operate_parameter_key(key)
            if not key:
                raise click.ClickException(f"parameter key is empty: {token}")
            values[key] = _float_operate_argument(value, label=key)
            continue
        positional.append(token)

    if action in {"return_to_launch", "land"}:
        if values or positional:
            raise click.ClickException(f"{action} does not accept parameters")
        return {}, assume_yes

    if action == "adjust_altitude":
        if positional:
            values["target_altitude_m"] = _float_operate_argument(
                positional.pop(0),
                label="target_altitude_m",
            )
        if positional:
            raise click.ClickException("climb accepts one altitude value")
        if "target_altitude_m" not in values:
            raise click.ClickException("usage: climb <altitude_m>")
        return values, assume_yes

    if action == "adjust_speed":
        if positional:
            values["target_speed_mps"] = _float_operate_argument(
                positional.pop(0),
                label="target_speed_mps",
            )
        if positional:
            raise click.ClickException("speed accepts one speed value")
        if "target_speed_mps" not in values:
            raise click.ClickException("usage: speed <speed_mps>")
        return values, assume_yes

    if action in {"reroute", "avoid_obstacle"}:
        if positional:
            values["target_x_m"] = _float_operate_argument(
                positional.pop(0),
                label="target_x_m",
            )
        if positional:
            values["target_y_m"] = _float_operate_argument(
                positional.pop(0),
                label="target_y_m",
            )
        if positional:
            values["target_altitude_m"] = _float_operate_argument(
                positional.pop(0),
                label="target_altitude_m",
            )
        if positional:
            raise click.ClickException("reroute/avoid accepts x y and optional altitude")
        if "target_x_m" not in values or "target_y_m" not in values:
            verb = "avoid" if action == "avoid_obstacle" else "reroute"
            raise click.ClickException(f"usage: {verb} <target_x_m> <target_y_m> [altitude_m]")
        return values, assume_yes

    raise click.ClickException(f"unsupported recovery action: {action}")


def _parse_operate_console_command(raw: str) -> OperateConsoleCommand:
    text = raw.strip()
    if not text:
        return OperateConsoleCommand(kind="refresh")
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise click.ClickException(f"could not parse operate command: {exc}") from exc
    if not tokens:
        return OperateConsoleCommand(kind="refresh")
    command = tokens[0].lower()
    if command in {"q", "quit", "exit"}:
        return OperateConsoleCommand(kind="quit")
    if command in {"?", "help"}:
        return OperateConsoleCommand(kind="help")
    if command in {"approve", "y", "yes"}:
        return OperateConsoleCommand(kind="approve_pending")
    if command in {"defer", "d", "hold"}:
        return OperateConsoleCommand(kind="defer_pending")
    if command in {"status", "refresh", "wait", "sleep", "back"}:
        return OperateConsoleCommand(kind="refresh")
    if command == "recover":
        if len(tokens) < 2:
            raise click.ClickException("usage: recover <action> [parameters]")
        command = tokens[1].lower()
        tokens = [tokens[0], *tokens[2:]]
    action = _OPERATE_RECOVERY_ACTION_ALIASES.get(command)
    if not action:
        raise click.ClickException(
            "unknown operate command; type `help` for available commands"
        )
    parameters, assume_yes = _parse_operate_console_parameters(action, tokens[1:])
    return OperateConsoleCommand(
        kind="dispatch",
        action=action,
        parameters=parameters,
        assume_yes=assume_yes,
    )


def _render_operate_status_line(
    snapshot: dict[str, Any], *, artifacts: dict[str, Any], status: str, task_id: str
) -> Text:
    """One compact live-telemetry line for operate (full map is in `missionos watch`)."""
    if _is_turtlebot3_task_artifacts(artifacts):
        summary = artifacts.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        motion = summary.get("motion_evidence")
        motion = motion if isinstance(motion, dict) else summary
        indoor_map = _turtlebot3_indoor_map_model_from_artifacts(artifacts)
        observed_points = indoor_map.get("observed_points")
        planned_points = indoor_map.get("planned_points")
        dispatched = _as_int(summary.get("segment_dispatch_count")) or 0
        completed = _as_int(summary.get("segment_completion_count")) or 0
        planned = _as_int(summary.get("planned_segment_count")) or 0
        checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
        checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
        phase = (
            "approved Recovery workflow in progress"
            if checkpoint.get("checkpoint_status") == "dispatching"
            else "waiting for Nav2 result"
            if status == "running" and dispatched > completed
            else "recovery decision required"
            if status == "pending" and summary.get("runtime_recovery_triggered") is True
            else status
        )
        robot_label = _turtlebot_robot_label_from_artifacts(artifacts)
        return Text.from_markup(
            f"[dim]task={task_id} · {phase} · "
            f"robot={rich_escape(robot_label)} sim · "
            f"segments={completed}/{planned or '-'} · "
            f"recovery_goal={_status_text(summary.get('recovery_goal_status'))} · "
            f"verification={_status_text(summary.get('recovery_verification_status'))} · "
            f"route={_status_text(summary.get('route_resume_status'))} · "
            f"motion={_status_text(motion.get('robot_motion_observed'))} · "
            f"odom={_status_text(motion.get('odom_delta_m'))}m · "
            f"observed_samples={len(observed_points) if isinstance(observed_points, list) else 0} · "
            f"planned_waypoints={len(planned_points) if isinstance(planned_points, list) else 0} · "
            "map: `missionos watch`[/dim]"
        )
    reached = _status_text(_as_int(snapshot.get("mission_reached_seq")))
    total = _status_text(_as_int(snapshot.get("waypoint_total")))
    return Text.from_markup(
        f"[dim]task={task_id} status={status} · "
        f"battery={_battery_display_text(snapshot=snapshot, artifacts=artifacts)} · "
        f"{_operate_altitude_text(snapshot, artifacts)} · "
        f"wp={reached}/{total} · "
        f"progress={_fmt_metres(snapshot.get('progress_m'))} · "
        f"home_dist={_fmt_metres(snapshot.get('distance_to_home_m'))} · "
        "full map in a separate pane: `missionos watch`[/dim]"
    )


def _operate_status_group(
    client: MissionOSGatewayClient,
    task_id: str,
) -> tuple[Group, str, str]:
    task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
    artifacts = _task_artifacts(task_payload)
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    status = _task_status(task_payload)
    proposal = _agent_proposal_from_task(task_payload)
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    checkpoint = artifacts.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    indoor_map = _turtlebot3_indoor_map_model_from_artifacts(artifacts)
    observed_points = indoor_map.get("observed_points")
    fingerprint = json.dumps(
        {
            "status": status,
            "segment_dispatch_count": summary.get("segment_dispatch_count"),
            "segment_completion_count": summary.get("segment_completion_count"),
            "runtime_recovery_triggered": summary.get("runtime_recovery_triggered"),
            "recovery_action_suggested": summary.get("recovery_action_suggested"),
            "recovery_goal_status": summary.get("recovery_goal_status"),
            "recovery_verification_status": summary.get(
                "recovery_verification_status"
            ),
            "route_resume_status": summary.get("route_resume_status"),
            "checkpoint_status": checkpoint.get("checkpoint_status"),
            "checkpoint_hash": checkpoint.get("checkpoint_hash"),
            "observed_point_count": (
                len(observed_points) if isinstance(observed_points, list) else 0
            ),
        },
        sort_keys=True,
        default=str,
    )
    return (
        Group(
            _render_recovery_agent_console(
                task_payload,
                proposal=proposal,
                show_proposal=bool(proposal) and status == "running",
                status=status,
                task_id=task_id,
            ),
            _render_operate_status_line(
                snapshot,
                artifacts=artifacts,
                status=status,
                task_id=task_id,
            ),
        ),
        status,
        fingerprint,
    )


def _operate_robot_for_task(client: MissionOSGatewayClient, task_id: str) -> str:
    task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
    artifacts = _task_artifacts(task_payload)
    return _turtlebot_robot_profile_from_artifacts(artifacts) or "px4"


def _handle_operate_console_command(
    client: MissionOSGatewayClient,
    task_id: str,
    command: OperateConsoleCommand,
) -> bool:
    if command.kind == "quit":
        return False
    if command.kind == "help":
        console.print(
            _operate_console_help_panel(
                task_id,
                robot=_operate_robot_for_task(client, task_id),
            )
        )
        return True
    if command.kind == "refresh":
        return True
    if command.kind == "defer_pending":
        console.print(
            "[yellow]Deferred. The robot remains stopped; no approval or dispatch "
            "was created.[/yellow]"
        )
        return True
    if command.kind == "approve_pending":
        task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
        pending = _pending_recovery_approval_from_task(task_payload)
        if not pending:
            raise click.ClickException("no recovery proposal is awaiting approval")
        if pending.get("operator_guidance_required") is True:
            console.print(
                "[yellow]Gemini requested operator guidance. This proposal-only "
                "checkpoint cannot be approved or dispatched. Type a bounded "
                "natural-language change; no approval artifact was created.[/yellow]"
            )
            return True
        action = str(pending.get("recovery_action") or "")
        if not click.confirm(
            f"Approve {action} for task {task_id}?", default=False
        ):
            console.print("[yellow]Deferred; no dispatch was sent.[/yellow]")
            return True
        payload = client.recovery_dispatch(
            task_id=task_id,
            recovery_action=action,
            recovery_parameters=dict(pending.get("recovery_parameters") or {}),
            expected_recovery_checkpoint_id=str(pending.get("checkpoint_id") or ""),
            expected_recovery_checkpoint_hash=str(pending.get("checkpoint_hash") or ""),
        )
        _print_recovery_result(payload)
        return True
    if command.kind != "dispatch":
        raise click.ClickException(f"unsupported operate command kind: {command.kind}")
    action = command.action
    label = _OPERATOR_RECOVERY_ACTIONS.get(action, action)
    if not command.assume_yes and not click.confirm(
        f"Send {label} to task {task_id}?", default=False
    ):
        console.print("[yellow]Canceled; no dispatch was sent.[/yellow]")
        return True
    payload = client.recovery_dispatch(
        task_id=task_id,
        recovery_action=action,
        recovery_parameters=command.parameters or {},
    )
    task_payload = _wait_for_active_runner_recovery_observation(client, payload)
    _print_recovery_result(payload, task_payload=task_payload)
    return True


def _handle_turtlebot3_operate_instruction(
    client: MissionOSGatewayClient,
    task_id: str,
    operator_instruction: str,
) -> bool:
    task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
    pending = _pending_recovery_approval_from_task(task_payload)
    if pending:
        revision_ctx = click.Context(missionos)
        revision_ctx.obj = {}
        if not _set_chat_recovery_revision_context(revision_ctx, pending=pending):
            return False
        return _handle_chat_recovery_revision_instruction(
            revision_ctx,
            client,
            operator_instruction=operator_instruction,
        )
    console.print(
        "[yellow]No Recovery decision is pending. The current Nav2 action is still "
        "running, so this console did not create a proposal, approval, or dispatch. "
        "Wait for the robot to stop and for a Recovery proposal to appear before "
        "requesting a route change.[/yellow]"
    )
    return True


def _operate_live(
    client: MissionOSGatewayClient,
    task_id: str,
    *,
    poll_interval: float,
    history_path: Path,
) -> None:
    """Interactive operator console.

    The console keeps the recovery-agent status visible and accepts full-line
    commands. Each dispatch still uses the same approval-gated recovery route;
    the prompt is only a local operator convenience.
    """
    session = _build_operate_session(history_path) if sys.stdin.isatty() else None
    scripted_input = None if session is not None else iter(sys.stdin)
    console.print(
        _operate_console_help_panel(
            task_id,
            robot=_operate_robot_for_task(client, task_id),
        )
    )

    render_lock = threading.Lock()
    stop_refresh = threading.Event()
    last_fingerprint = ""

    def _print_status(*, force: bool = False) -> str:
        nonlocal last_fingerprint
        with render_lock:
            try:
                group, current_status, fingerprint = _operate_status_group(
                    client, task_id
                )
                if force or fingerprint != last_fingerprint:
                    console.print(group)
                    last_fingerprint = fingerprint
                return current_status
            except click.ClickException as exc:
                console.print(
                    Panel(f"[red]{exc.message}[/red]", title="MissionOS Operate")
                )
                return "unavailable"

    def _auto_refresh() -> None:
        while not stop_refresh.wait(max(5.0, poll_interval)):
            current_status = _print_status()
            if current_status in TERMINAL_TASK_STATUSES:
                stop_refresh.set()
                break

    refresh_thread: threading.Thread | None = None
    if session is not None:
        refresh_thread = threading.Thread(
            target=_auto_refresh,
            name=f"missionos-operate-refresh-{task_id}",
            daemon=True,
        )
        refresh_thread.start()
    while True:
        status = _print_status(force=not last_fingerprint)
        if status in TERMINAL_TASK_STATUSES:
            break
        try:
            if scripted_input is not None:
                raw = next(scripted_input).strip()
                console.print(f"[bold cyan]operate>[/bold cyan] {raw}")
            else:
                with patch_stdout(raw=True):
                    raw = session.prompt(HTML("<ansicyan>operate></ansicyan> "))
        except StopIteration:
            break
        except KeyboardInterrupt:
            console.print("[yellow](Ctrl+C - type quit or Ctrl+D to exit)[/yellow]")
            continue
        except EOFError:
            break
        try:
            command = _parse_operate_console_command(raw)
        except click.ClickException as exc:
            if (
                _operate_robot_for_task(client, task_id) == "turtlebot3"
                and raw.strip()
                and _handle_turtlebot3_operate_instruction(client, task_id, raw)
            ):
                pass
            else:
                console.print(
                    f"[red]{exc.message}[/red]\n"
                    "[dim]You can also describe the change naturally, for example: "
                    "左へ大きく迂回して[/dim]"
                )
        else:
            try:
                if not _handle_operate_console_command(client, task_id, command):
                    break
            except click.ClickException as exc:
                console.print(
                    f"[red]{exc.message}[/red]\n"
                    "[yellow]The approved operation may still be running. This "
                    "console will rely on the durable task state; do not approve "
                    "the same checkpoint again.[/yellow]"
                )
        if raw.strip() in {"wait", "sleep"}:
            time.sleep(max(0.2, poll_interval))
    stop_refresh.set()
    if refresh_thread is not None:
        refresh_thread.join(timeout=1.0)


@missionos.command("operate")
@click.option(
    "--task-id",
    default="",
    help="Task/job id to operate. Defaults to the task stored by `run`.",
)
@click.option(
    "--poll-interval",
    default=FLIGHT_MAP_POLL_INTERVAL,
    show_default=True,
    type=click.FloatRange(0.2, 10.0),
    help="Seconds to wait for explicit wait/sleep refresh commands.",
)
@click.option(
    "--history-path",
    default=DEFAULT_OPERATE_HISTORY_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Persist operate-console command history.",
)
@click.pass_context
def operate_command(
    ctx: click.Context,
    task_id: str,
    poll_interval: float,
    history_path: Path,
) -> None:
    """Recovery-agent operator console. Type recovery commands here; exit with quit/Ctrl-C."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    resolved_task_id = _resolve_live_task_id(
        client,
        explicit_task_id=task_id,
        stored_task_id=_stored_sitl_task_id(ctx),
    )
    try:
        _operate_live(
            client,
            resolved_task_id,
            poll_interval=poll_interval,
            history_path=history_path,
        )
    except KeyboardInterrupt:
        pass
    console.print("[yellow](operate stopped)[/yellow]")


def _operator_recovery_command(
    ctx: click.Context,
    *,
    task_id: str,
    action: str,
    assume_yes: bool,
    recovery_parameters: dict[str, Any] | None = None,
) -> None:
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    resolved_task_id = _resolve_operator_recovery_task_id(
        client,
        explicit_task_id=task_id,
        stored_task_id=_stored_sitl_task_id(ctx),
    )
    label = _OPERATOR_RECOVERY_ACTIONS.get(action, action)
    if not assume_yes and not click.confirm(
        f"Send {label} to task {resolved_task_id}?", default=False
    ):
        console.print("[yellow]Canceled; no dispatch was sent.[/yellow]")
        return
    payload = client.recovery_dispatch(
        task_id=resolved_task_id,
        recovery_action=action,
        recovery_parameters=recovery_parameters,
    )
    if ctx.obj["missionos_json_output"]:
        _print_json(payload)
        return
    task_payload = _wait_for_active_runner_recovery_observation(client, payload)
    _print_recovery_result(payload, task_payload=task_payload)


@missionos.command("rtl")
@click.option("--task-id", default="", help="Target task. Defaults to auto-detecting a running task.")
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the dispatch.")
@click.pass_context
def rtl_command(ctx: click.Context, task_id: str, yes: bool) -> None:
    """Dispatch operator-approved RTL (return to launch) with standard y/N confirmation."""
    _operator_recovery_command(ctx, task_id=task_id, action="return_to_launch", assume_yes=yes)


@missionos.command("land")
@click.option("--task-id", default="", help="Target task. Defaults to auto-detecting a running task.")
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the dispatch.")
@click.pass_context
def land_command(ctx: click.Context, task_id: str, yes: bool) -> None:
    """Dispatch operator-approved LAND with standard y/N confirmation."""
    _operator_recovery_command(ctx, task_id=task_id, action="land", assume_yes=yes)


@missionos.command("climb")
@click.option("--task-id", default="", help="Target task. Defaults to auto-detecting a running task.")
@click.option("--altitude-m", required=True, type=float, help="Target altitude above home in metres.")
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the request.")
@click.pass_context
def climb_command(ctx: click.Context, task_id: str, altitude_m: float, yes: bool) -> None:
    """Request an operator-approved bounded altitude adjustment."""
    _operator_recovery_command(
        ctx,
        task_id=task_id,
        action="adjust_altitude",
        assume_yes=yes,
        recovery_parameters={"target_altitude_m": altitude_m},
    )


@missionos.command("speed")
@click.option("--task-id", default="", help="Target task. Defaults to auto-detecting a running task.")
@click.option("--speed-mps", required=True, type=float, help="Target groundspeed in metres per second.")
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the request.")
@click.pass_context
def speed_command(ctx: click.Context, task_id: str, speed_mps: float, yes: bool) -> None:
    """Request an operator-approved bounded speed adjustment."""
    _operator_recovery_command(
        ctx,
        task_id=task_id,
        action="adjust_speed",
        assume_yes=yes,
        recovery_parameters={"target_speed_mps": speed_mps},
    )


@missionos.command("reroute")
@click.option("--task-id", default="", help="Target task. Defaults to auto-detecting a running task.")
@click.option("--target-x-m", required=True, type=float, help="Local NED north target in metres.")
@click.option("--target-y-m", required=True, type=float, help="Local NED east target in metres.")
@click.option("--altitude-m", type=float, default=None, help="Optional target altitude above home in metres.")
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the request.")
@click.pass_context
def reroute_command(
    ctx: click.Context,
    task_id: str,
    target_x_m: float,
    target_y_m: float,
    altitude_m: float | None,
    yes: bool,
) -> None:
    """Request an operator-approved bounded local reroute target."""
    params: dict[str, Any] = {"target_x_m": target_x_m, "target_y_m": target_y_m}
    if altitude_m is not None:
        params["target_altitude_m"] = altitude_m
    _operator_recovery_command(
        ctx,
        task_id=task_id,
        action="reroute",
        assume_yes=yes,
        recovery_parameters=params,
    )


@missionos.command("avoid-obstacle")
@click.option("--task-id", default="", help="Target task. Defaults to auto-detecting a running task.")
@click.option("--target-x-m", required=True, type=float, help="Obstacle-aware local NED north target in metres.")
@click.option("--target-y-m", required=True, type=float, help="Obstacle-aware local NED east target in metres.")
@click.option("--altitude-m", type=float, default=None, help="Optional target altitude above home in metres.")
@click.option("--yes", is_flag=True, help="Skip y/N confirmation and send the request.")
@click.pass_context
def avoid_obstacle_command(
    ctx: click.Context,
    task_id: str,
    target_x_m: float,
    target_y_m: float,
    altitude_m: float | None,
    yes: bool,
) -> None:
    """Request an operator-approved obstacle-avoidance reroute target."""
    params: dict[str, Any] = {
        "target_x_m": target_x_m,
        "target_y_m": target_y_m,
    }
    if altitude_m is not None:
        params["target_altitude_m"] = altitude_m
    _operator_recovery_command(
        ctx,
        task_id=task_id,
        action="avoid_obstacle",
        assume_yes=yes,
        recovery_parameters=params,
    )


@dataclass
class TutorialStep:
    """One teaching step: what it does, the literal CLI, the boundary, the action."""

    key: str
    title: str
    explanation: str
    command: str
    boundary: str
    action: Callable[..., TutorialOutcome]
    live: bool = False


def _tutorial_status(
    ctx: click.Context, client: MissionOSGatewayClient, session_id: str
) -> TutorialOutcome:
    payloads = {
        "health": client.health(),
        "form2a": client.get("/missionos/form2a-response-selection"),
        "review": client.get("/missionos/form2a-operator-review"),
        "action": client.get("/missionos/form2a-action-consumption"),
        "repair": client.get("/missionos/llm-repair-planner"),
    }
    _print_status(payloads, base_url=ctx.obj["missionos_gateway_url"])
    return None


def _tutorial_plan(
    ctx: click.Context, client: MissionOSGatewayClient, session_id: str
) -> TutorialOutcome:
    payload = client.conversation(
        TUTORIAL_PLAN_INSTRUCTION,
        session_id=session_id,
        mission_designer_context=_stored_mission_designer_context(ctx, session_id),
        coordinate_route=dict(FUJI_DELIVERY_ROUTE),
        route_hint="mission_designer_plan",
    )
    _remember_mission_designer_context(ctx, payload, session_id=session_id)
    _print_conversation_result(payload)
    return None


def _tutorial_intent(
    intent: str,
) -> Callable[[click.Context, MissionOSGatewayClient, str], TutorialOutcome]:
    def _action(
        ctx: click.Context, client: MissionOSGatewayClient, session_id: str
    ) -> TutorialOutcome:
        payload = client.conversation(
            INTENT_INSTRUCTIONS[intent],
            session_id=session_id,
            mission_designer_context=_stored_mission_designer_context(ctx, session_id),
            route_hint=INTENT_ROUTE_HINTS[intent],
        )
        _remember_mission_designer_context(ctx, payload, session_id=session_id)
        _print_conversation_result(payload)
        return None

    return _action


def _tutorial_resolve_task_id(ctx: click.Context) -> str:
    task_id = _stored_sitl_task_id(ctx)
    if not task_id:
        raise click.ClickException(
            "No prepared SITL task id is stored. The run step must return a task first."
        )
    return task_id


def _tutorial_start_sitl(
    ctx: click.Context, client: MissionOSGatewayClient, session_id: str
) -> TutorialOutcome:
    task_id = _tutorial_resolve_task_id(ctx)
    payload = client.start_sitl(task_id=task_id)
    _remember_sitl_task_id(ctx, task_id)
    _print_sitl_start_result(payload)
    return None


def _tutorial_execute_sitl(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    session_id: str,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> TutorialOutcome:
    task_id = _tutorial_resolve_task_id(ctx)
    payload, task_payload, timeline_payload = _execute_sitl_with_task_polling(
        client,
        task_id=task_id,
        live_flight_mode=True,
        progress_callback=progress_callback,
    )
    _remember_sitl_task_id(ctx, task_id)
    if payload is None and task_payload is not None and timeline_payload is not None:
        console.print(f"[yellow]{LIVE_SITL_RESPONSE_WAIT_EXCEEDED_MESSAGE}[/yellow]")
        _print_job_status(task_payload, timeline_payload)
        return _task_status(task_payload)
    _print_sitl_execution_result(payload)
    if isinstance(payload, dict):
        summary = payload.get("summary")
        task_payload = payload.get("task")
        if isinstance(summary, dict):
            return str(summary.get("task_status") or summary.get("live_flight_status") or "")
        if isinstance(task_payload, dict):
            return _task_status(task_payload)
    return None


def build_tutorial_steps() -> list[TutorialStep]:
    """The ordered Fuji-delivery CLI walkthrough."""
    return [
        TutorialStep(
            key="status",
            title="Read Current State",
            explanation=(
                "Read the MissionOS operator surfaces (Gateway / Plan / Review / "
                "Execution / Repair). This does not start anything."
            ),
            command="missionos status",
            boundary="Read-only. No PX4/Gazebo process and no dispatch authority.",
            action=_tutorial_status,
        ),
        TutorialStep(
            key="plan",
            title="Plan (say)",
            explanation=(
                "Ask for the plan in natural language. The CLI passes the bundled "
                "Mt. Fuji route coordinates (the same values as route.yaml). The "
                "Gateway creates a source-bound Mission Designer context, and the "
                "CLI stores that reference in state."
            ),
            command=(
                "missionos say --route-hint mission_designer_plan "
                "--coordinate-route-file docs/mission_os/fuji_delivery_route.yaml "
                '"Plan the Mt. Fuji delivery"'
            ),
            boundary="Planning only. No approval and no execution.",
            action=_tutorial_plan,
        ),
        TutorialStep(
            key="approve",
            title="Approve (approve)",
            explanation=(
                "Approve the plan as the operator. This uses the same conversation "
                "route as MissionOS chat approval, with Gateway policy gates still active."
            ),
            command="missionos approve",
            boundary="Sends only the approval intent. It does not bypass gates.",
            action=_tutorial_intent("approve"),
        ),
        TutorialStep(
            key="run",
            title="Prepare Bounded Action (run)",
            explanation=(
                "Prepare the approved bounded action through the execution gate. "
                "When a SITL execution task is returned, the CLI stores the task_id "
                "in state so later commands can reuse it."
            ),
            command="missionos run",
            boundary="Passes the execution gate, but the simulator is not started yet.",
            action=_tutorial_intent("run"),
        ),
        TutorialStep(
            key="start-sitl",
            title="Start SITL (start-sitl)",
            explanation=(
                "Use the PX4/Gazebo SITL startup boundary. This is where simulator "
                "readiness is brought up (task_id is read from state)."
            ),
            command="missionos start-sitl",
            boundary="Real PX4/Gazebo processes begin here.",
            action=_tutorial_start_sitl,
        ),
        TutorialStep(
            key="execute-sitl",
            title="Execute Live SITL (execute-sitl)",
            explanation=(
                "Use the Execute Live SITL boundary. The CLI sends explicit execution "
                "approval and live_flight_mode=true. This is a real execution gate, "
                "so it requires explicit confirmation."
            ),
            command="missionos execute-sitl --live-flight",
            boundary=(
                "Live execution. delivery_completion_claimed / "
                "physical_delivery_verified remain false; the CLI has no path that "
                "turns them true."
            ),
            action=_tutorial_execute_sitl,
            live=True,
        ),
    ]


def _print_tutorial_step(index: int, total: int, step: TutorialStep) -> None:
    body = (
        f"{step.explanation}\n\n"
        f"[dim]Manual command:[/dim]\n  [green]{step.command}[/green]\n\n"
        f"[dim]Boundary:[/dim] {step.boundary}"
    )
    border = "red" if step.live else "cyan"
    console.print(
        Panel(body, title=f"Step {index}/{total} — {step.title}", border_style=border)
    )


TutorialReader = Callable[[str], str]


def run_fuji_tutorial(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    *,
    session_id: str,
    interactive: bool,
    allow_live: bool,
    reader: TutorialReader | None = None,
) -> None:
    """Drive the guided Fuji-delivery walkthrough.

    Non-live steps run on Enter (interactive) or automatically (auto mode). The
    live Execute Live SITL step never fires without an explicit human 'yes' in
    interactive mode, or the --yes/allow_live opt-in in auto mode.
    """
    ask: TutorialReader = reader or (lambda prompt: console.input(prompt))
    steps = build_tutorial_steps()
    console.print(
        Panel(
            "Walk through the Mt. Fuji delivery while learning the CLI one command "
            "at a time.\n"
            "Each step shows the manual command and the production boundary it crosses.\n"
            "[dim]Enter=run / s=skip / q=quit. Live SITL execution requires 'yes'.[/dim]",
            title="MissionOS CLI Tutorial (Mt. Fuji Delivery)",
            border_style="magenta",
        )
    )
    for index, step in enumerate(steps, 1):
        _print_tutorial_step(index, len(steps), step)
        if step.live:
            if interactive:
                answer = ask("[bold red]Live execution will start. Type 'yes' to run > [/bold red]")
                if answer.strip().lower() != "yes":
                    console.print(
                        "[yellow]Skipped live execution. Run the command above "
                        "manually when you are ready.[/yellow]"
                    )
                    break
            elif not allow_live:
                console.print(
                    "[yellow]Skipped live execution because --yes was not set. "
                    "Use `missionos tutorial --auto --yes` for a full auto run.[/yellow]"
                )
                break
        elif interactive:
            decision = ask("[cyan]Enter=run / s=skip / q=quit > [/cyan]").strip().lower()
            if decision in {"q", "quit"}:
                console.print("[yellow]Tutorial stopped.[/yellow]")
                return
            if decision in {"s", "skip"}:
                console.print("[dim](skipped this step)[/dim]")
                continue
        try:
            if step.live:
                console.print(
                    "[bold red]Live execution started.[/bold red]"
                    "PX4/Gazebo AUTO missions can take several to many minutes. "
                    "Wait for the completion or failure panel."
                )
                with console.status(
                    "[red]Execute Live SITL is running... waiting for Gateway response[/red]",
                    spinner="dots",
                ) as status:
                    outcome = step.action(
                        ctx,
                        client,
                        session_id,
                        progress_callback=lambda latest: status.update(
                            f"[red]{_job_progress_status_text(latest)}[/red]"
                        ),
                    )
            else:
                outcome = step.action(ctx, client, session_id)
            if step.live and outcome and outcome not in TERMINAL_TASK_STATUSES:
                console.print(
                    Panel(
                        "The AUTO mission is still running.\n"
                        "Run `missionos job-status` again to track position, distance, and battery.\n"
                        "delivery_completion_claimed remains false until the task becomes completed or blocked.",
                        title="Live Execution Still Running",
                        border_style="yellow",
                    )
                )
                return
            if step.live and outcome in {"blocked", "failed", "cancelled", "canceled"}:
                console.print(
                    Panel(
                        "Execute Live SITL stopped before completion.\n"
                        "Run `missionos job-status` to inspect the latest state and artifact_root.",
                        title="Live Execution Stopped",
                        border_style="red",
                    )
                )
                return
        except click.ClickException as exc:
            console.print(f"[red]{exc.message}[/red]")
            console.print("[yellow]Stopped at this step. Fix the condition and resume.[/yellow]")
            return
    console.print(
        Panel(
            "Done. Each manual command shown above is the real operational CLI.\n"
            "You can run each command directly as `missionos <sub>` (for example, `missionos status`).\n"
            "Before starting a different mission, clear state with `missionos clear-state`.",
            title="Tutorial Complete",
            border_style="green",
        )
    )


@missionos.command("tutorial")
@click.option("--session-id", default=DEFAULT_TUTORIAL_SESSION_ID, show_default=True)
@click.option(
    "--auto",
    is_flag=True,
    help="Run each step without pauses (for guided demos).",
)
@click.option(
    "--yes",
    "allow_live",
    is_flag=True,
    help="Allow the live SITL execution step in --auto mode (default stops before live execution).",
)
@click.option(
    "--autostart/--no-autostart",
    default=False,
    show_default=True,
    help="Autostart the Gateway when it is not running, then stop it on exit.",
)
@click.option(
    "--enable-live-sitl/--planning-only",
    default=False,
    show_default=True,
    help="Enable live SITL/dispatch opt-in env for an autostarted Gateway.",
)
@click.pass_context
def tutorial_command(
    ctx: click.Context,
    session_id: str,
    auto: bool,
    allow_live: bool,
    autostart: bool,
    enable_live_sitl: bool,
) -> None:
    """Experimental guided walkthrough; not the public quickstart."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    gateway_proc = _ensure_gateway(
        client,
        ctx.obj["missionos_gateway_url"],
        autostart=autostart,
        enable_live_sitl=enable_live_sitl,
    )
    try:
        run_fuji_tutorial(
            ctx,
            client,
            session_id=session_id,
            interactive=not auto,
            allow_live=allow_live,
        )
    finally:
        if gateway_proc is not None:
            console.print("[blue]Stopping the autostarted Gateway...[/blue]")
            _terminate_gateway(gateway_proc)


CHAT_HELP_LINES = (
    "Type a MissionOS instruction, or a slash command.",
    "You can also start here with: missionos chat \"Plan a delivery from Tokyo Station to Kawasaki Station\"",
    "  /status                      — show operator surfaces",
    "  /approve /reject /revision   — operator review intents",
    "  /run /repair                 — execution and repair intents",
    "  /start-sitl [task_id]        — SITL startup boundary",
    "  /execute-sitl [task_id]      — Execute Live SITL boundary",
    "                                interactive chat opens operate/watch/map companion terminals",
    "  /job-status [task_id]        — show stored/running task status",
    "  /map [task_id]               — open the live source-backed route map",
    "  /land <task_id>              — operator-approved LAND dispatch",
    "  /rtl <task_id>               — operator-approved RTL dispatch",
    "  /review-recovery [task_id]   — review with y=approve, d=defer, c=change",
    "  /approve-recovery [task_id]  — expert explicit approval fallback",
    "  /climb 45                    — operator-approved altitude adjustment",
    "  /speed 7                     — operator-approved speed adjustment",
    "  /reroute 120 -20 (45)        — operator-approved local reroute",
    "  /avoid 40 20 (45)            — operator-approved obstacle avoidance",
    "  高度を45mに上げて             — ask Recovery Agent for a proposal",
    "  障害物を避けて迂回して        — ask Recovery Agent for an avoidance proposal",
    "  /back                        — return to the previous chat decision point",
    "  /help /clear /quit",
    "Flow: Enter opens the suggested step; recovery review requires y (default defer).",
    "Editing: ↑/↓ history, Ctrl+R search, Tab completes /commands,",
    "         Esc then Enter inserts a newline, Enter submits, Ctrl+D quits.",
)


def _chat_help_panel() -> Panel:
    return Panel(
        Text("\n".join(CHAT_HELP_LINES)),
        title="MissionOS CLI",
        border_style="cyan",
    )


_RECOVERY_NATURAL_LANGUAGE_TRANSLATION = str.maketrans(
    "０１２３４５６７８９．，、ｍＭ",
    "0123456789.,,mM",
)
_RECOVERY_METRIC_NUMBER_RE = re.compile(
    r"(?P<value>-?\d+(?:[.,]\d+)?)\s*(?:m|meter|meters|metre|metres|メートル)?",
    re.IGNORECASE,
)


def _normalize_recovery_natural_language(raw: str) -> str:
    return raw.translate(_RECOVERY_NATURAL_LANGUAGE_TRANSLATION).lower()


def _recovery_natural_language_number(raw: str) -> float | None:
    match = _RECOVERY_METRIC_NUMBER_RE.search(
        _normalize_recovery_natural_language(raw).replace(",", ".")
    )
    if not match:
        return None
    return _as_float(match.group("value"))


def _recovery_natural_language_xy(raw: str) -> tuple[float, float] | None:
    text = _normalize_recovery_natural_language(raw).replace(",", ".")
    x_match = re.search(r"(?:target_)?x(?:_m)?\s*[=:]?\s*(-?\d+(?:\.\d+)?)", text)
    y_match = re.search(r"(?:target_)?y(?:_m)?\s*[=:]?\s*(-?\d+(?:\.\d+)?)", text)
    if x_match and y_match:
        x_value = _as_float(x_match.group(1))
        y_value = _as_float(y_match.group(1))
        if x_value is not None and y_value is not None:
            return x_value, y_value
    return None


def _looks_like_mission_planning_request(raw: str) -> bool:
    text = _normalize_recovery_natural_language(raw)
    if any(marker in text for marker in ("->", "→", "⇒")):
        return True
    if re.search(r"\S+\s*から\s*\S+\s*まで", text):
        return True
    if re.search(r"\bfrom\s+.+\bto\s+.+", text):
        return True
    home_robot_terms = (
        "turtlebot3",
        "turtlebot",
        "nova carter",
        "nova-carter",
        "nova_carter",
        "isaac sim",
        "nvidia",
        "亀",
        "かめ",
        "タートルボット",
        "屋内",
        "家の中",
        "部屋",
    )
    mission_terms = (
        "配送",
        "配達",
        "届け",
        "目的地",
        "ルート",
        "走って",
        "一周",
        "patrol",
        "delivery",
        "deliver",
        "route",
    )
    if any(term in text for term in home_robot_terms) and any(
        term in text for term in mission_terms
    ):
        return True
    return False


def _natural_language_recovery_request(raw: str) -> dict[str, Any] | None:
    text = _normalize_recovery_natural_language(raw)
    altitude_terms = (
        "高度",
        "上げ",
        "あげ",
        "上昇",
        "climb",
        "altitude",
        "higher",
        "raise",
    )
    obstacle_terms = (
        "障害物",
        "ビル",
        "建物",
        "障害",
        "回避",
        "避け",
        "avoid",
        "obstacle",
        "building",
    )
    reroute_terms = (
        "迂回",
        "ルート変更",
        "経路変更",
        "route change",
        "change route",
        "reroute",
        "detour",
    )
    has_altitude = any(term in text for term in altitude_terms)
    has_obstacle = any(term in text for term in obstacle_terms)
    has_reroute = any(term in text for term in reroute_terms)
    if not has_altitude and not has_obstacle and not has_reroute:
        return None
    action = (
        "avoid_obstacle"
        if has_obstacle
        else "reroute"
        if has_reroute
        else "adjust_altitude"
    )
    parameters: dict[str, Any] = {}
    if has_altitude or action in {"avoid_obstacle", "reroute"}:
        altitude = _recovery_natural_language_number(raw)
        if altitude is not None and (has_altitude or "alt" in text):
            parameters["target_altitude_m"] = altitude
    xy = _recovery_natural_language_xy(raw)
    if xy is not None:
        parameters["target_x_m"], parameters["target_y_m"] = xy
    return {
        "requested_action": action,
        "requested_parameters": parameters,
    }


def _recovery_command_number(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    number = _as_float(value)
    if number is None:
        return str(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _recovery_proposal_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary")
    return summary if isinstance(summary, dict) else {}


def _recovery_proposal_command(payload: dict[str, Any]) -> str | None:
    summary = _recovery_proposal_summary(payload)
    action = str(
        payload.get("selected_bounded_action")
        or summary.get("selected_bounded_action")
        or ""
    )
    params = payload.get("proposed_parameters")
    params = params if isinstance(params, dict) else {}
    if not params:
        params = summary.get("proposed_parameters")
        params = params if isinstance(params, dict) else {}
    if action == "adjust_altitude":
        altitude = params.get("target_altitude_m")
        if altitude is None:
            return None
        return f"/climb {_recovery_command_number(altitude)}"
    if action == "adjust_speed":
        speed = params.get("target_speed_mps")
        if speed is None:
            return None
        return f"/speed {_recovery_command_number(speed)}"
    if action in {"reroute", "avoid_obstacle"}:
        x_value = params.get("target_x_m")
        y_value = params.get("target_y_m")
        if x_value is None or y_value is None:
            return None
        command = "/avoid" if action == "avoid_obstacle" else "/reroute"
        parts = [
            command,
            _recovery_command_number(x_value),
            _recovery_command_number(y_value),
        ]
        if params.get("target_altitude_m") is not None:
            parts.append(_recovery_command_number(params["target_altitude_m"]))
        return " ".join(parts)
    return None


def _print_recovery_agent_request_proposal(payload: dict[str, Any]) -> None:
    summary = _recovery_proposal_summary(payload)
    action = str(
        payload.get("selected_bounded_action")
        or summary.get("selected_bounded_action")
        or "operator_review"
    )
    status = str(
        payload.get("proposal_status")
        or summary.get("proposal_status")
        or "-"
    )
    params = payload.get("proposed_parameters")
    params = params if isinstance(params, dict) else {}
    if not params:
        params = summary.get("proposed_parameters")
        params = params if isinstance(params, dict) else {}
    param_text = (
        ", ".join(
            f"{key}={_recovery_command_number(value)}"
            for key, value in sorted(params.items())
        )
        if params
        else "-"
    )
    lines = [
        f"proposal_status={status}",
        f"selected_bounded_action={action}",
        f"proposed_parameters={param_text}",
        "dispatch_authority=False · operator_approval_required=True",
        "physical_execution_invoked=False · progress_counted=False",
    ]
    if status != "computed":
        lines.append(
            "No bounded maneuver was available from the current telemetry/context."
        )
    console.print(
        Panel(
            Text("\n".join(lines)),
            title="Recovery Agent Proposal",
            border_style="yellow" if status == "computed" else "red",
        )
    )


def _set_chat_suggestion(ctx: click.Context, *, raw: str, label: str) -> None:
    ctx.obj["missionos_chat_suggestion"] = {"raw": raw, "label": label}


def _clear_chat_suggestion(ctx: click.Context) -> None:
    ctx.obj.pop("missionos_chat_suggestion", None)


def _chat_back_stack(ctx: click.Context) -> list[dict[str, Any]]:
    stack = ctx.obj.get("missionos_chat_back_stack")
    if not isinstance(stack, list):
        stack = []
        ctx.obj["missionos_chat_back_stack"] = stack
    return stack


def _chat_back_available(ctx: click.Context) -> bool:
    stack = ctx.obj.get("missionos_chat_back_stack")
    return isinstance(stack, list) and bool(stack)


def _chat_suggestion(ctx: click.Context) -> dict[str, str]:
    suggestion = ctx.obj.get("missionos_chat_suggestion")
    if not isinstance(suggestion, dict):
        return {}
    raw = str(suggestion.get("raw") or "").strip()
    label = str(suggestion.get("label") or "").strip()
    if not raw or not label:
        return {}
    return {"raw": raw, "label": label}


def _chat_state_snapshot(ctx: click.Context) -> dict[str, Any]:
    state_path = ctx.obj.get("missionos_state_path")
    state = _load_state(state_path) if isinstance(state_path, Path) else {}
    return {
        "state": state,
        "suggestion": _chat_suggestion(ctx),
    }


def _push_chat_back_state(ctx: click.Context) -> None:
    stack = _chat_back_stack(ctx)
    snapshot = _chat_state_snapshot(ctx)
    if stack and stack[-1] == snapshot:
        return
    stack.append(snapshot)
    del stack[:-20]


def _clear_chat_back_stack(ctx: click.Context) -> None:
    ctx.obj.pop("missionos_chat_back_stack", None)


def _restore_chat_back_state(ctx: click.Context) -> bool:
    stack = _chat_back_stack(ctx)
    if not stack:
        return False
    snapshot = stack.pop()
    state_path = ctx.obj.get("missionos_state_path")
    state = snapshot.get("state") if isinstance(snapshot, dict) else {}
    if isinstance(state_path, Path):
        if isinstance(state, dict) and state:
            _save_state(state_path, state)
        else:
            try:
                state_path.unlink()
            except FileNotFoundError:
                pass
    suggestion = snapshot.get("suggestion") if isinstance(snapshot, dict) else {}
    if (
        isinstance(suggestion, dict)
        and str(suggestion.get("raw") or "").strip()
        and str(suggestion.get("label") or "").strip()
    ):
        _set_chat_suggestion(
            ctx,
            raw=str(suggestion["raw"]).strip(),
            label=str(suggestion["label"]).strip(),
        )
    else:
        _clear_chat_suggestion(ctx)
    return True


def _is_chat_back_request(raw: str) -> bool:
    normalized = raw.strip().lower()
    return normalized in {
        "/back",
        "back",
        "go back",
        "previous",
        "undo",
        "戻る",
        "戻って",
        "前に戻る",
        "一つ前",
        "ひとつ前",
    }


def _chat_prompt_fragment(ctx: click.Context) -> HTML:
    suggestion = _chat_suggestion(ctx)
    if suggestion:
        back_hint = ", /back" if _chat_back_available(ctx) else ""
        return HTML(
            "\n<ansigreen><b>MissionOS</b></ansigreen> "
            f"<ansiyellow>[Enter={suggestion['label']}{back_hint}]</ansiyellow>"
            "<ansigreen><b>&gt;</b></ansigreen> "
        )
    if _chat_back_available(ctx):
        return HTML(
            "\n<ansigreen><b>MissionOS</b></ansigreen> "
            "<ansiyellow>[/back]</ansiyellow>"
            "<ansigreen><b>&gt;</b></ansigreen> "
        )
    return HTML("\n<ansigreen><b>MissionOS&gt;</b></ansigreen> ")


def _print_chat_followup(message: str) -> None:
    console.print(
        Panel(
            f"[bold]MissionOS[/bold]: {message}",
            title="Next",
            border_style="cyan",
        )
    )


def _safe_chat_companion_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug[:80] or "missionos"


def _chat_companion_terminals_enabled(ctx: click.Context) -> bool:
    env_value = os.environ.get("MISSIONOS_CHAT_COMPANION_TERMINALS", "1").strip().lower()
    if env_value in {"0", "false", "no", "off"}:
        return False
    if not bool(ctx.obj.get("missionos_chat_companion_terminals_enabled")):
        return False
    return sys.stdin.isatty()


def _missionos_chat_companion_command_prefix(ctx: click.Context) -> str:
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else Path("missionos")
    if argv0.exists() and argv0.is_file() and os.access(argv0, os.X_OK):
        parts = [str(argv0.resolve())]
    elif argv0.name == "__main__.py" and argv0.parent.name == "missionos_cli":
        # `python -m missionos_cli` exposes the package's non-executable
        # __main__.py as argv[0]. A companion must preserve module invocation;
        # executing that file directly produces exit 126 on macOS.
        parts = [sys.executable, "-m", "missionos_cli"]
    else:
        parts = ["missionos"]
    gateway_url = str(ctx.obj.get("missionos_gateway_url") or "").strip()
    if gateway_url:
        parts.extend(["--gateway-url", gateway_url])
    client = ctx.obj.get("missionos_client")
    if isinstance(client, MissionOSGatewayClient):
        parts.extend(["--timeout", str(client.timeout)])
    state_path = ctx.obj.get("missionos_state_path")
    if state_path:
        parts.extend(["--state-path", str(state_path)])
    return " ".join(shlex.quote(part) for part in parts)


def _chat_companion_terminal_script(
    *,
    title: str,
    command: str,
    stop_path: Path,
    gateway_api_key_path: Path | None,
    cwd: Path,
    hold_after_command: bool,
) -> str:
    hold = "1" if hold_after_command else "0"
    api_key_path = str(gateway_api_key_path or "")
    return f"""#!/bin/sh
set +e
cd {shlex.quote(str(cwd))}
STOP_PATH={shlex.quote(str(stop_path))}
GATEWAY_API_KEY_PATH={shlex.quote(api_key_path)}
TITLE={shlex.quote(title)}
HOLD_AFTER_COMMAND={hold}
if [ -n "$GATEWAY_API_KEY_PATH" ] && [ -f "$GATEWAY_API_KEY_PATH" ]; then
  IFS= read -r GATEWAY_API_KEY < "$GATEWAY_API_KEY_PATH"
  export GATEWAY_API_KEY
fi
printf '\\033]0;%s\\007' "$TITLE"
echo "$TITLE"
echo "This MissionOS companion terminal closes when missionos chat exits."
(
  while [ ! -f "$STOP_PATH" ]; do
    sleep 1
  done
  pkill -TERM -P $$ 2>/dev/null || true
  kill -TERM $$ 2>/dev/null || true
) &
WATCHER_PID=$!
trap 'kill "$WATCHER_PID" 2>/dev/null || true' EXIT INT TERM
{command}
COMMAND_STATUS=$?
if [ "$HOLD_AFTER_COMMAND" = "1" ]; then
  echo
  echo "Command finished. Waiting for missionos chat to close..."
  while [ ! -f "$STOP_PATH" ]; do
    sleep 1
  done
fi
exit "$COMMAND_STATUS"
"""


def _launch_macos_terminal_script(script_path: Path, *, title: str) -> bool:
    if sys.platform != "darwin":
        return False
    command = f"sh {shlex.quote(str(script_path.resolve()))}"
    applescript = "\n".join(
        [
            'tell application "Terminal"',
            "activate",
            f"set newTab to do script {json.dumps(command)}",
            "delay 0.1",
            f"set custom title of newTab to {json.dumps(title)}",
            "end tell",
        ]
    )
    try:
        subprocess.run(
            ["osascript", "-e", applescript],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def _close_macos_companion_terminal_titles(titles: list[str]) -> None:
    if sys.platform != "darwin" or not titles:
        return
    conditions = " or ".join(
        f"custom title of t contains {json.dumps(title)}" for title in titles
    )
    applescript = "\n".join(
        [
            'tell application "Terminal"',
            "repeat 10 times",
            "set closedOne to false",
            "repeat with w in windows",
            "repeat with t in tabs of w",
            "try",
            f"if {conditions} then",
            "close w saving no",
            "set closedOne to true",
            "exit repeat",
            "end if",
            "end try",
            "end repeat",
            "if closedOne then exit repeat",
            "end repeat",
            "if not closedOne then exit repeat",
            "delay 0.1",
            "end repeat",
            "end tell",
        ]
    )
    try:
        subprocess.run(
            ["osascript", "-e", applescript],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _stop_chat_companion_terminals(ctx: click.Context) -> None:
    state = ctx.obj.pop("missionos_chat_companion_terminals", None)
    if not isinstance(state, dict):
        return
    stop_raw = str(state.get("stop_path") or "")
    if stop_raw:
        stop_path = Path(stop_raw)
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.touch()
    time.sleep(0.5)
    api_key_path_raw = str(state.get("gateway_api_key_path") or "")
    if api_key_path_raw:
        Path(api_key_path_raw).unlink(missing_ok=True)
    titles = [str(title) for title in state.get("titles") or [] if str(title)]
    _close_macos_companion_terminal_titles(titles)


def _ensure_chat_companion_terminals(ctx: click.Context, task_id: str) -> None:
    if not task_id or not _chat_companion_terminals_enabled(ctx):
        return
    existing = ctx.obj.get("missionos_chat_companion_terminals")
    if isinstance(existing, dict) and existing.get("task_id") == task_id:
        return
    if isinstance(existing, dict):
        _stop_chat_companion_terminals(ctx)

    session_slug = _safe_chat_companion_slug(
        str(ctx.obj.get("missionos_chat_session_id") or "chat")
    )
    task_slug = _safe_chat_companion_slug(task_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (
        Path.cwd() / CHAT_COMPANION_TERMINAL_ROOT / f"{session_slug}_{task_slug}_{stamp}"
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stop_path = root / "stop"
    client = ctx.obj.get("missionos_client")
    gateway_api_key = (
        str(client.api_key or "")
        if isinstance(client, MissionOSGatewayClient)
        else ""
    )
    gateway_api_key_path: Path | None = None
    if gateway_api_key:
        gateway_api_key_path = root / "gateway_api_key"
        gateway_api_key_path.write_text(gateway_api_key, encoding="utf-8")
        gateway_api_key_path.chmod(0o600)
    command_prefix = _missionos_chat_companion_command_prefix(ctx)
    commands = {
        "operate": f"{command_prefix} operate --task-id {shlex.quote(task_id)}",
        "watch": f"{command_prefix} watch --task-id {shlex.quote(task_id)}",
        "map": (
            f"{command_prefix} map --task-id {shlex.quote(task_id)} --serve-live"
        ),
    }
    titles: list[str] = []
    launched: list[str] = []
    for surface in CHAT_COMPANION_TERMINAL_SURFACES:
        title = f"MissionOS {surface} {task_id}"
        script_path = root / f"{surface}.sh"
        script_path.write_text(
            _chat_companion_terminal_script(
                title=title,
                command=commands[surface],
                stop_path=stop_path,
                gateway_api_key_path=gateway_api_key_path,
                cwd=Path.cwd(),
                hold_after_command=surface == "map",
            ),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        titles.append(title)
        if _launch_macos_terminal_script(script_path, title=title):
            launched.append(surface)

    if launched:
        ctx.obj["missionos_chat_companion_terminals"] = {
            "task_id": task_id,
            "root": str(root),
            "stop_path": str(stop_path),
            "gateway_api_key_path": str(gateway_api_key_path or ""),
            "titles": titles,
            "launched": launched,
        }
        console.print(
            "[blue]Opened companion terminals: "
            + ", ".join(launched)
            + ". They will close when chat exits.[/blue]"
        )
    else:
        console.print(
            "[yellow]Companion terminals are unavailable here. Run these manually if needed: "
            f"missionos operate --task-id {task_id}; missionos watch --task-id {task_id}; missionos map --task-id {task_id}[/yellow]"
        )


def _maybe_open_turtlebot3_companion_terminals(
    ctx: click.Context,
    payload: dict[str, Any],
) -> None:
    operation = payload.get("operation_result")
    operation = operation if isinstance(operation, dict) else {}
    summary = operation.get("summary") if isinstance(operation.get("summary"), dict) else {}
    if not _is_home_robot_nav2_execution_target(summary.get("execution_target")):
        return
    task_id = _payload_task_id(operation) or _payload_task_id(payload)
    if task_id:
        _ensure_chat_companion_terminals(ctx, task_id)


def _listed_home_robot_task_ids(client: MissionOSGatewayClient) -> set[str]:
    try:
        payload = client.get("/tasks?page=1&page_size=20")
    except click.ClickException:
        return set()
    items = payload.get("items") or payload.get("tasks") or []
    if not isinstance(items, list):
        return set()
    task_ids: set[str] = set()
    for task in items:
        if not isinstance(task, dict):
            continue
        artifacts = task.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        task_id = str(task.get("task_id") or "").strip()
        if task_id and _is_turtlebot3_task_artifacts(artifacts):
            task_ids.add(task_id)
    return task_ids


def _run_turtlebot3_conversation_with_companion_monitor(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Open operator surfaces as soon as a synchronous run creates its task."""

    if not _chat_companion_terminals_enabled(ctx):
        return operation()
    existing_task_ids = _listed_home_robot_task_ids(client)
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(operation)
    try:
        while True:
            try:
                return future.result(timeout=0.5)
            except FutureTimeout:
                new_task_ids = (
                    _listed_home_robot_task_ids(client) - existing_task_ids
                )
                if new_task_ids:
                    task_id = sorted(new_task_ids)[-1]
                    _remember_sitl_task_id(ctx, task_id)
                    _ensure_chat_companion_terminals(ctx, task_id)
    finally:
        executor.shutdown(wait=False, cancel_futures=False)


def _print_turtlebot3_chat_task_terminal_update(
    task_payload: dict[str, Any],
) -> None:
    """Append the durable terminal task truth to the main chat transcript."""

    task = task_payload.get("task")
    task = task if isinstance(task, dict) else {}
    artifacts = _task_artifacts(task_payload)
    summary = artifacts.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    status = str(task.get("status") or summary.get("status") or "unknown")
    completed = summary.get("segment_completion_count")
    planned = summary.get("planned_segment_count")
    lines = [
        f"task_id={_status_text(task.get('task_id'))}",
        f"operation_status={_status_text(status)}",
        "recovery_goal="
        f"{_status_text(summary.get('recovery_goal_status'))}; "
        "verification="
        f"{_status_text(summary.get('recovery_verification_status'))}; "
        f"route={_status_text(summary.get('route_resume_status'))}",
        f"segments={_status_text(completed)}/{_status_text(planned)}; "
        f"completion_claimed={summary.get('completion_claimed') is True}",
        "mission_delivery_completion_claimed="
        f"{summary.get('mission_delivery_completion_claimed') is True}; "
        "physical_execution_invoked="
        f"{summary.get('physical_execution_invoked') is True}",
    ]
    blocking_reasons = [
        str(reason)
        for reason in summary.get("blocking_reasons") or []
        if str(reason)
    ]
    if blocking_reasons:
        lines.append("blocking_reasons=" + ", ".join(blocking_reasons))
    console.print(
        Panel(
            Text("\n".join(lines)),
            title="MissionOS task final update",
            border_style="green" if status == "completed" else "yellow",
        )
    )


def _stop_turtlebot3_chat_task_status_monitor(ctx: click.Context) -> None:
    state = ctx.obj.pop("missionos_turtlebot3_chat_task_status_monitor", None)
    if not isinstance(state, dict):
        return
    stop_event = state.get("stop_event")
    if isinstance(stop_event, threading.Event):
        stop_event.set()
    thread = state.get("thread")
    if (
        isinstance(thread, threading.Thread)
        and thread is not threading.current_thread()
    ):
        thread.join(timeout=1.0)


def _start_turtlebot3_chat_task_status_monitor(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    *,
    task_id: str,
) -> None:
    """Notify main chat when a companion-driven TurtleBot3 task terminates."""

    if not task_id:
        return
    existing = ctx.obj.get("missionos_turtlebot3_chat_task_status_monitor")
    if isinstance(existing, dict) and existing.get("task_id") == task_id:
        thread = existing.get("thread")
        if isinstance(thread, threading.Thread) and thread.is_alive():
            return
    _stop_turtlebot3_chat_task_status_monitor(ctx)
    stop_event = threading.Event()

    def monitor() -> None:
        encoded_task_id = quote(task_id, safe="")
        while not stop_event.is_set():
            try:
                task_payload = client.get(f"/tasks/{encoded_task_id}")
            except click.ClickException:
                if stop_event.wait(TURTLEBOT3_CHAT_TASK_STATUS_POLL_INTERVAL):
                    return
                continue
            task = task_payload.get("task")
            task = task if isinstance(task, dict) else {}
            status = str(task.get("status") or "").strip().lower()
            if status in TERMINAL_TASK_STATUSES:
                _print_turtlebot3_chat_task_terminal_update(task_payload)
                return
            if stop_event.wait(TURTLEBOT3_CHAT_TASK_STATUS_POLL_INTERVAL):
                return

    thread = threading.Thread(
        target=monitor,
        name=f"missionos-chat-task-status-{task_id}",
        daemon=True,
    )
    ctx.obj["missionos_turtlebot3_chat_task_status_monitor"] = {
        "task_id": task_id,
        "stop_event": stop_event,
        "thread": thread,
    }
    thread.start()


def _maybe_start_turtlebot3_chat_task_status_monitor(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    payload: dict[str, Any],
) -> None:
    operation = payload.get("operation_result")
    operation = operation if isinstance(operation, dict) else {}
    summary = operation.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    status = str(
        summary.get("status")
        or operation.get("summary_status")
        or operation.get("response_status")
        or ""
    ).strip().lower()
    if status in TERMINAL_TASK_STATUSES:
        return
    task_id = (
        _payload_task_id(operation)
        or _payload_task_id(payload)
        or _stored_sitl_task_id(ctx)
    )
    if task_id:
        _start_turtlebot3_chat_task_status_monitor(
            ctx,
            client,
            task_id=task_id,
        )


def _conversation_has_approvable_plan(payload: dict[str, Any]) -> bool:
    mission_designer = payload.get("mission_designer")
    mission_designer = mission_designer if isinstance(mission_designer, dict) else {}
    mission_summary = (
        mission_designer.get("summary")
        if isinstance(mission_designer.get("summary"), dict)
        else {}
    )
    mission_context_ref = str(
        mission_designer.get("mission_designer_context_ref")
        or mission_summary.get("mission_designer_context_ref")
        or ""
    ).strip()
    mission_context_sha = str(
        mission_designer.get("mission_designer_context_sha256")
        or mission_summary.get("mission_designer_context_sha256")
        or ""
    ).strip()
    if (
        isinstance(mission_designer.get("scenario_proposal"), dict)
        and isinstance(mission_designer.get("validation_result"), dict)
        and mission_context_ref
        and mission_context_sha
    ):
        return True

    selection = payload.get("selection")
    selection = selection if isinstance(selection, dict) else {}
    if str(selection.get("summary_status") or "").lower() == "form2a_response_selected":
        return True

    operation = payload.get("operation_result")
    operation = operation if isinstance(operation, dict) else {}
    if operation.get("error"):
        return False
    if str(operation.get("summary_status") or "").lower() == "form2a_response_selected":
        return True
    return False


def _conversation_should_advance_suggestion(payload: dict[str, Any]) -> bool:
    action = str(payload.get("routed_action") or payload.get("route") or "")
    message = str(payload.get("message") or "").lower()
    if any(
        marker in message
        for marker in (
            "cannot use",
            "not source-bound",
            "not source bound",
        )
    ):
        return False
    if action == "approve" and "did not approve" in message:
        return False
    if action == "run" and "did not prepare" in message:
        return False
    operation = payload.get("operation_result")
    operation = operation if isinstance(operation, dict) else {}
    summary = operation.get("summary") if isinstance(operation.get("summary"), dict) else {}
    status = str(
        summary.get("status")
        or operation.get("summary_status")
        or operation.get("response_status")
        or ""
    ).lower()
    return not any(marker in status for marker in ("blocked", "failed", "rejected"))


def _update_chat_suggestion_from_conversation(
    ctx: click.Context,
    payload: dict[str, Any],
    client: MissionOSGatewayClient | None = None,
) -> None:
    if not _conversation_should_advance_suggestion(payload):
        _clear_chat_suggestion(ctx)
        return
    action = str(payload.get("routed_action") or payload.get("route") or "")
    repair_prompt = payload.get("missionos_repair_prompt")
    if isinstance(repair_prompt, dict) and repair_prompt.get("suggested_command") == "/repair":
        _set_chat_suggestion(ctx, raw="/repair", label="repair")
        return
    if action in {"fixture_plan", "mission_designer_plan", "plan"}:
        if _conversation_has_approvable_plan(payload):
            _set_chat_suggestion(ctx, raw="/approve", label="approve")
        else:
            _clear_chat_suggestion(ctx)
    elif action == "approve":
        _set_chat_suggestion(ctx, raw="/run", label="prepare")
    elif action == "execute" and _stored_sitl_task_id(ctx):
        operation = payload.get("operation_result")
        operation = operation if isinstance(operation, dict) else {}
        summary = (
            operation.get("summary")
            if isinstance(operation.get("summary"), dict)
            else {}
        )
        if _is_home_robot_nav2_execution_target(summary.get("execution_target")):
            task_id = _stored_sitl_task_id(ctx)
            pending = (
                _lookup_pending_recovery_approval(client, task_id=task_id)
                if isinstance(client, MissionOSGatewayClient)
                else _pending_recovery_approval_from_task(
                    {
                        "task_id": task_id,
                        "status": operation.get("task_status")
                        or operation.get("summary_status")
                        or "",
                        "artifacts": {
                            "summary": summary,
                            "turtlebot3_recovery_decision_summary": summary.get(
                                "turtlebot3_recovery_decision_summary"
                            )
                            if isinstance(
                                summary.get(
                                    "turtlebot3_recovery_decision_summary"
                                ),
                                dict,
                            )
                            else {},
                        },
                    }
                )
            )
            if pending:
                console.print(_render_chat_recovery_review(pending))
                _set_chat_suggestion(
                    ctx,
                    raw=f"/review-recovery {task_id}",
                    label="review recovery",
                )
            else:
                _set_chat_suggestion(ctx, raw=f"/map {task_id}", label="map")
        else:
            _set_chat_suggestion(ctx, raw="/start-sitl", label="start")
    else:
        _clear_chat_suggestion(ctx)


def _handle_chat_input(
    ctx: click.Context,
    client: MissionOSGatewayClient,
    raw: str,
    *,
    session_id: str,
) -> bool:
    """Process one chat line. Return False to exit the loop."""
    robot_profile = str(ctx.obj.get("missionos_chat_robot_profile") or "")
    raw = raw.strip()
    if not raw:
        revision_context = _chat_recovery_revision_context(ctx)
        if revision_context:
            console.print(
                "[yellow]Recovery revision mode remains active; Enter does not "
                "approve, dispatch, or alter the pending checkpoint. Type a "
                "natural-language alternative.[/yellow]"
            )
            return True
        suggestion = _chat_suggestion(ctx)
        if not suggestion:
            return True
        raw = suggestion["raw"]
        console.print(f"[dim]Enter -> {suggestion['label']}[/dim]")
    else:
        revision_context = _chat_recovery_revision_context(ctx)
        if revision_context and raw == "/back":
            _clear_chat_recovery_revision_context(ctx)
            _set_chat_suggestion(
                ctx,
                raw=f"/review-recovery {revision_context['task_id']}",
                label="review pending recovery",
            )
            console.print(
                "[yellow]Exited recovery revision mode. No approval artifact or "
                "dispatch was created; the checkpoint remains pending.[/yellow]"
            )
            return True
        if revision_context and not raw.startswith("/"):
            _clear_chat_suggestion(ctx)
            return _handle_chat_recovery_revision_instruction(
                ctx,
                client,
                operator_instruction=raw,
            )
        if _is_chat_back_request(raw):
            raw = "/back"
        else:
            _clear_chat_suggestion(ctx)
    if raw.startswith("missionos "):
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()
        if parts and parts[0] == "missionos":
            args = parts[1:]
            if args and args[0] in {"--json", "--gateway-url", "--timeout", "--state-path"}:
                console.print(
                    "[yellow]Inside MissionOS chat, use slash commands such as /approve or /run.[/yellow]"
                )
                return True
            if len(args) == 1 and args[0] in INTENT_INSTRUCTIONS:
                raw = f"/{args[0]}"
            elif args and args[0] in {"start-sitl", "execute-sitl", "job-status"}:
                raw = "/" + " ".join(args)
            elif args and args[0] == "recover":
                console.print(
                    "[yellow]Inside MissionOS chat, use /land <task_id> or /rtl <task_id>.[/yellow]"
                )
                return True
            elif args and args[0] in {"say", "chat"}:
                raw = " ".join(args[1:]).strip()
                if not raw:
                    console.print(
                        "[yellow]Type the instruction directly inside MissionOS chat.[/yellow]"
                    )
                    return True
            else:
                console.print(
                    "[yellow]Inside MissionOS chat, use slash commands such as /approve or /run.[/yellow]"
                )
                return True
    if raw in {"/quit", "/exit", "exit", "quit", "q"}:
        return False
    if not raw.startswith("/"):
        stored_task_id = _stored_sitl_task_id(ctx)
        lower = raw.lower()
        if any(token in lower for token in ("prepare", "ready", "execution request")):
            raw = "/run"
        elif any(token in lower for token in ("start", "boot", "bring up")):
            raw = "/start-sitl"
        elif (
            stored_task_id
            and any(token in lower for token in ("fly", "launch", "execute live", "start live"))
        ):
            raw = "/execute-sitl"
        elif stored_task_id and any(
            token in lower for token in ("map", "地図", "軌跡", "route trace")
        ):
            raw = "/map"
        elif stored_task_id and any(
            token in lower for token in ("status", "progress", "show status")
        ):
            raw = "/job-status"
        elif _is_recovery_approval_text(raw):
            if _handle_chat_recovery_approval(
                ctx,
                client,
                explicit_task_id="",
                quiet_if_missing=True,
            ):
                return True
            raw = "/approve"
        elif _is_chat_back_request(raw):
            raw = "/back"
    if raw == "/help":
        console.print(_chat_help_panel())
        return True
    if raw == "/back":
        if _restore_chat_back_state(ctx):
            suggestion = _chat_suggestion(ctx)
            next_text = (
                f"Returned to the previous chat step. Press Enter for {suggestion['label']}, "
                "or type a different instruction."
                if suggestion
                else "Returned to the previous chat step. Type a new instruction to continue."
            )
            _print_chat_followup(
                next_text
                + " Already-sent Gateway/simulator actions are not undone by /back."
            )
        else:
            _print_chat_followup(
                "No previous reversible chat step is available. External actions already sent "
                "to the Gateway or simulator cannot be undone with /back."
            )
        return True
    if raw == "/clear":
        console.clear()
        return True
    try:
        if raw == "/status":
            ctx.invoke(status_command)
            return True
        if raw.startswith("/review-recovery"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /review-recovery [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else ""
            return _handle_chat_recovery_review(
                ctx,
                client,
                explicit_task_id=task_id,
            )
        if raw.startswith("/approve-recovery"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /approve-recovery [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else ""
            return _handle_chat_recovery_approval(
                ctx,
                client,
                explicit_task_id=task_id,
            )
        if raw.startswith("/land ") or raw.startswith("/rtl "):
            parts = shlex.split(raw)
            if len(parts) != 2:
                console.print("[yellow]Usage: /land <task_id> or /rtl <task_id>[/yellow]")
                return True
            _clear_chat_back_stack(ctx)
            action = "land" if parts[0] == "/land" else "return_to_launch"
            with console.status("[cyan]dispatching recovery…[/cyan]", spinner="dots"):
                payload = client.recovery_dispatch(task_id=parts[1], recovery_action=action)
                task_payload = _wait_for_active_runner_recovery_observation(client, payload)
            _print_recovery_result(payload, task_payload=task_payload)
            return True
        if raw.startswith(
            ("/climb", "/speed", "/reroute", "/avoid ", "/avoid-obstacle")
        ):
            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                console.print(f"[red]could not parse recovery command: {exc}[/red]")
                return True
            if not parts:
                return True
            command_text = " ".join([parts[0].lstrip("/"), *parts[1:]])
            try:
                command = _parse_operate_console_command(command_text)
            except click.ClickException as exc:
                console.print(f"[red]{exc.message}[/red]")
                return True
            task_id = _resolve_operator_recovery_task_id(
                client,
                explicit_task_id="",
                stored_task_id=_stored_sitl_task_id(ctx),
            )
            if command.kind != "dispatch":
                console.print("[yellow]Usage: /climb, /speed, /reroute, or /avoid[/yellow]")
                return True
            _clear_chat_back_stack(ctx)
            if _handle_operate_console_command(client, task_id, command):
                _set_chat_suggestion(ctx, raw=f"/job-status {task_id}", label="show status")
            return True
        if raw.startswith("/map"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /map [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else _stored_sitl_task_id(ctx)
            if task_id and _chat_companion_terminals_enabled(ctx):
                _ensure_chat_companion_terminals(ctx, task_id)
                console.print(
                    "[cyan]The authenticated live map is the map companion for "
                    f"{task_id}. /map will not open a stale file:// snapshot.[/cyan]"
                )
                _set_chat_suggestion(
                    ctx,
                    raw=f"/job-status {task_id}",
                    label="show status",
                )
                return True
            try:
                ctx.invoke(
                    map_command,
                    task_id=task_id or "",
                    provider="osm",
                    output_path=None,
                    poll_interval=MISSION_MAP_POLL_INTERVAL,
                    snapshot=False,
                    serve_live=True,
                    no_open=False,
                )
            except click.ClickException as exc:
                console.print(f"[red]{exc.message}[/red]")
                return True
            if task_id:
                _set_chat_suggestion(ctx, raw=f"/job-status {task_id}", label="show status")
            return True
        if raw.startswith("/execute-sitl"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /execute-sitl [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else _stored_sitl_task_id(ctx)
            if not task_id:
                console.print(
                    "[yellow]No stored task id; run /run or pass /execute-sitl <task_id>[/yellow]"
                )
                return True
            _clear_chat_back_stack(ctx)
            _ensure_chat_companion_terminals(ctx, task_id)
            with console.status("[green]executing SITL…[/green]", spinner="dots") as status:
                payload, task_payload, timeline_payload = _execute_sitl_with_task_polling(
                    client,
                    task_id=task_id,
                    live_flight_mode=True,
                    progress_callback=lambda latest: status.update(
                        f"[green]{_job_progress_status_text(latest)}[/green]"
                    ),
                )
            if payload is None and task_payload is not None and timeline_payload is not None:
                latest_task_id = _remember_sitl_task_id_from_payload(
                    ctx,
                    task_payload,
                    fallback_task_id=task_id,
                )
                console.print(f"[yellow]{LIVE_SITL_RESPONSE_WAIT_EXCEEDED_MESSAGE}[/yellow]")
                _print_job_status(task_payload, timeline_payload)
                latest_task = task_payload.get("task")
                latest_status = (
                    str(latest_task.get("status") or "").strip().lower()
                    if isinstance(latest_task, dict)
                    else ""
                )
                followup = (
                    "You can inspect the final state if needed. Type 'show status' to view it."
                    if latest_status in TERMINAL_TASK_STATUSES
                    else "The AUTO mission is still running. Type 'show status' to view progress."
                )
                _print_chat_followup(followup)
                _set_chat_suggestion(ctx, raw=f"/job-status {latest_task_id}", label="show status")
                return True
            latest_task_id = _remember_sitl_task_id_from_payload(
                ctx,
                payload,
                fallback_task_id=task_id,
            )
            _print_sitl_execution_result(payload)
            _print_chat_followup(
                "You can inspect the final state if needed. Type 'show status' to view it."
            )
            _set_chat_suggestion(ctx, raw=f"/job-status {latest_task_id}", label="show status")
            return True
        if raw.startswith("/start-sitl"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /start-sitl [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else _stored_sitl_task_id(ctx)
            if not task_id:
                console.print(
                    "[yellow]No stored task id; run /run or pass /start-sitl <task_id>[/yellow]"
                )
                return True
            _clear_chat_back_stack(ctx)
            with console.status("[blue]starting SITL…[/blue]", spinner="dots"):
                payload = client.start_sitl(task_id=task_id)
            latest_task_id = _remember_sitl_task_id_from_payload(
                ctx,
                payload,
                fallback_task_id=task_id,
            )
            _print_sitl_start_result(payload)
            _print_chat_followup(
                "SITL is ready. Start live execution? Type 'fly' to proceed."
            )
            _set_chat_suggestion(ctx, raw=f"/execute-sitl {latest_task_id}", label="fly")
            return True
        if raw.startswith("/job-status"):
            parts = shlex.split(raw)
            if len(parts) > 2:
                console.print("[yellow]Usage: /job-status [task_id][/yellow]")
                return True
            task_id = parts[1] if len(parts) == 2 else _stored_sitl_task_id(ctx)
            if not task_id:
                console.print(
                    "[yellow]No stored task id; run /run or pass /job-status <task_id>[/yellow]"
                )
                return True
            encoded_task_id = quote(task_id, safe="")
            with console.status("[magenta]checking job status…[/magenta]", spinner="dots"):
                task_payload = client.get(f"/tasks/{encoded_task_id}")
                timeline_payload = client.get(
                    f"/tasks/{encoded_task_id}/timeline?limit=8"
                )
            _print_job_status(task_payload, timeline_payload)
            task = task_payload.get("task") if isinstance(task_payload.get("task"), dict) else {}
            if str(task.get("status") or "").lower() in {"running", "pending"}:
                _set_chat_suggestion(ctx, raw="/job-status", label="refresh")
            else:
                _clear_chat_suggestion(ctx)
            return True
        if not raw.startswith("/") and not _looks_like_mission_planning_request(raw):
            recovery_request = _natural_language_recovery_request(raw)
            if recovery_request is not None:
                task_id = _resolve_operator_recovery_task_id(
                    client,
                    explicit_task_id="",
                    stored_task_id=_stored_sitl_task_id(ctx),
                )
                with console.status(
                    "[cyan]Recovery Agent: planning maneuver…[/cyan]",
                    spinner="dots",
                ):
                    payload = client.recovery_agent_propose_for_task(
                        task_id=task_id,
                        operator_instruction=raw,
                        requested_action=str(
                            recovery_request.get("requested_action") or ""
                        ),
                        requested_parameters=(
                            recovery_request.get("requested_parameters")
                            if isinstance(
                                recovery_request.get("requested_parameters"), dict
                            )
                            else {}
                        ),
                    )
                _print_recovery_agent_request_proposal(payload)
                command_raw = _recovery_proposal_command(payload)
                if command_raw:
                    _set_chat_suggestion(
                        ctx,
                        raw=command_raw,
                        label="review recovery",
                    )
                    _print_chat_followup(
                        "Press Enter to review the proposed recovery command; "
                        "dispatch still asks for y/N confirmation."
                    )
                else:
                    _clear_chat_suggestion(ctx)
                return True
        if raw.startswith("/"):
            intent = raw[1:]
            if intent in INTENT_INSTRUCTIONS:
                _push_chat_back_state(ctx)
                with console.status(f"[cyan]MissionOS: {intent}…[/cyan]", spinner="dots"):
                    def conversation() -> dict[str, Any]:
                        return client.conversation(
                            INTENT_INSTRUCTIONS[intent],
                            session_id=session_id,
                            mission_designer_context=_stored_mission_designer_context(
                                ctx, session_id
                            ),
                            route_hint=INTENT_ROUTE_HINTS[intent],
                            client_surface="chat",
                            robot_profile=robot_profile or None,
                        )

                    payload = (
                        _run_turtlebot3_conversation_with_companion_monitor(
                            ctx,
                            client,
                            conversation,
                        )
                        if robot_profile == "turtlebot3" and intent == "run"
                        else conversation()
                    )
                _remember_mission_designer_context(ctx, payload, session_id=session_id)
                _maybe_open_turtlebot3_companion_terminals(ctx, payload)
                _print_conversation_result(payload)
                if robot_profile == "turtlebot3" and intent == "run":
                    _maybe_start_turtlebot3_chat_task_status_monitor(
                        ctx,
                        client,
                        payload,
                    )
                _update_chat_suggestion_from_conversation(ctx, payload, client)
                return True
            console.print(
                "[yellow]Unknown command. Type /help for the slash-command list.[/yellow]"
            )
            return True
        _push_chat_back_state(ctx)
        route_hint = (
            "mission_designer_plan"
            if _looks_like_mission_planning_request(raw)
            else None
        )
        with console.status("[cyan]MissionOS…[/cyan]", spinner="dots"):
            payload = client.conversation(
                raw,
                session_id=session_id,
                mission_designer_context=_stored_mission_designer_context(ctx, session_id),
                route_hint=route_hint,
                client_surface="chat",
                robot_profile=robot_profile or None,
        )
        _remember_mission_designer_context(ctx, payload, session_id=session_id)
        _maybe_open_turtlebot3_companion_terminals(ctx, payload)
        _print_conversation_result(payload)
        _update_chat_suggestion_from_conversation(ctx, payload, client)
    except click.ClickException as exc:
        console.print(f"[red]{exc.message}[/red]")
    return True


def _build_chat_session(history_path: Path) -> PromptSession[str]:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.touch(exist_ok=True)
    bindings = KeyBindings()

    @bindings.add(Keys.Escape, Keys.Enter)
    def _(event):  # type: ignore[no-redef]
        event.current_buffer.insert_text("\n")

    return PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(list(CHAT_SLASH_COMMANDS), ignore_case=True),
        complete_while_typing=True,
        multiline=False,
        key_bindings=bindings,
        mouse_support=False,
    )


def _chat_initial_instruction_and_autostart(
    initial_instruction: tuple[str, ...],
    *,
    autostart: bool,
    enable_live_sitl: bool,
) -> tuple[str, bool, bool]:
    text = " ".join(str(part) for part in initial_instruction).strip()
    while True:
        option_match = re.search(
            r"(?:\s|\u3000)+(--autostart|--no-autostart|--enable-live-sitl|--planning-only)\s*$",
            text,
        )
        if not option_match:
            return text, autostart, enable_live_sitl
        option = option_match.group(1)
        text = text[: option_match.start()].strip()
        if option == "--autostart":
            autostart = True
        elif option == "--no-autostart":
            autostart = False
        elif option == "--enable-live-sitl":
            enable_live_sitl = True
        elif option == "--planning-only":
            enable_live_sitl = False


def _find_repo_root_for_turtlebot3_smoke() -> Path:
    script_rel = Path("scripts/smoke_ros2_nav2_turtlebot3_obstacle_delivery_docker.sh")
    candidates: list[Path] = []
    cwd = Path.cwd().resolve()
    candidates.extend([cwd, *cwd.parents])
    module_path = Path(__file__).resolve()
    candidates.extend([module_path.parent, *module_path.parents])
    for candidate in candidates:
        if (candidate / script_rel).is_file():
            return candidate
    return cwd


def _run_turtlebot3_chat_smoke(
    *,
    instruction: str,
    build_image: bool,
    mid_recovery: bool,
    dry_run: bool,
) -> int:
    repo_root = _find_repo_root_for_turtlebot3_smoke()
    script = repo_root / "scripts" / "smoke_ros2_nav2_turtlebot3_obstacle_delivery_docker.sh"
    if not script.is_file():
        console.print(
            "[red]TurtleBot3 Docker smoke script was not found. "
            "Run this command from the MissionOS repository root.[/red]"
        )
        return 2
    image = os.environ.get(
        "MISSIONOS_TB3_DOCKER_IMAGE",
        "missionos-ros2-nav2-turtlebot3:local",
    )
    env = os.environ.copy()
    env["MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_INSTRUCTION"] = (
        instruction.strip() or DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION
    )
    if mid_recovery:
        env["MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE"] = "1"

    build_cmd = (
        "docker",
        "build",
        "-f",
        "docker/ros2_nav2_turtlebot3/Dockerfile",
        "-t",
        image,
        ".",
    )
    run_cmd = (str(script),)
    console.print(
        Panel(
            "\n".join(
                (
                    f"instruction={env['MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_INSTRUCTION']}",
                    f"repo_root={repo_root}",
                    f"image={image}",
                    "boundary=MissionOS chat -> Gateway -> TurtleBot3/Nav2/Gazebo sim",
                    "claim_scope=sim_action; physical_execution_invoked=false",
                )
            ),
            title="TurtleBot3 MissionOS Chat",
            border_style="green",
        )
    )
    if dry_run:
        if build_image:
            console.print("[cyan]build:[/cyan] " + shlex.join(build_cmd))
        console.print(
            "[cyan]run:[/cyan] "
            + "MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_INSTRUCTION="
            + shlex.quote(env["MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_INSTRUCTION"])
            + (" MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE=1" if mid_recovery else "")
            + " "
            + shlex.join(run_cmd)
        )
        return 0
    if build_image:
        build_result = subprocess.run(build_cmd, cwd=str(repo_root), env=env, check=False)
        if build_result.returncode != 0:
            return int(build_result.returncode)
    run_result = subprocess.run(run_cmd, cwd=str(repo_root), env=env, check=False)
    return int(run_result.returncode)


def _turtlebot3_gateway_container_name() -> str:
    return os.environ.get(
        "MISSIONOS_TB3_GATEWAY_CONTAINER",
        "missionos-turtlebot3-gateway",
    ).strip() or "missionos-turtlebot3-gateway"


def _turtlebot3_gateway_start_script(repo_root: Path) -> Path:
    return repo_root / "scripts" / "start_ros2_nav2_turtlebot3_gateway_docker.sh"


def _start_turtlebot3_gateway_container(
    *,
    gateway_url: str,
    instruction: str,
    build_image: bool,
    dry_run: bool,
    gateway_api_key: str = "",
) -> bool:
    repo_root = _find_repo_root_for_turtlebot3_smoke()
    script = _turtlebot3_gateway_start_script(repo_root)
    if not script.is_file():
        raise click.ClickException(
            "TurtleBot3 Gateway Docker launcher was not found. "
            "Run this command from the MissionOS repository root."
        )
    host, port = _gateway_host_port(gateway_url)
    if host not in {"127.0.0.1", "localhost"}:
        raise click.ClickException(
            "--robot turtlebot3 can autostart the Docker Gateway only on localhost. "
            f"Current gateway host is {host!r}."
        )
    image = os.environ.get(
        "MISSIONOS_TB3_DOCKER_IMAGE",
        "missionos-ros2-nav2-turtlebot3:local",
    )
    env = os.environ.copy()
    if gateway_api_key:
        env["GATEWAY_API_KEY"] = gateway_api_key
    env["MISSIONOS_TB3_DOCKER_IMAGE"] = image
    env["MISSIONOS_TB3_GATEWAY_CONTAINER"] = _turtlebot3_gateway_container_name()
    env["MISSIONOS_TB3_GATEWAY_PORT"] = str(port)
    world_profile = os.environ.get("MISSIONOS_TURTLEBOT3_WORLD_PROFILE", "house").strip()
    world_profile = world_profile if world_profile in {"arena", "house"} else "house"
    env["MISSIONOS_TURTLEBOT3_WORLD_PROFILE"] = world_profile
    build_cmd = (
        "docker",
        "build",
        "-f",
        "docker/ros2_nav2_turtlebot3/Dockerfile",
        "-t",
        image,
        ".",
    )
    console.print(
        Panel(
            "\n".join(
                (
                    f"instruction={instruction.strip() or DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION}",
                    f"gateway_url={gateway_url}",
                    f"repo_root={repo_root}",
                    f"image={image}",
                    f"world_profile={world_profile}",
                    "boundary=MissionOS chat -> Gateway -> TurtleBot3/Nav2/Gazebo sim",
                    "surfaces=chat + operate + watch + map",
                    "claim_scope=sim_action; physical_execution_invoked=false",
                )
            ),
            title="TurtleBot3 MissionOS Gateway",
            border_style="green",
        )
    )
    if dry_run:
        if build_image:
            console.print("[cyan]build:[/cyan] " + shlex.join(build_cmd))
        console.print(
            "[cyan]start gateway/sim:[/cyan] "
            + f"MISSIONOS_TB3_GATEWAY_PORT={port} "
            + f"MISSIONOS_TB3_GATEWAY_CONTAINER={shlex.quote(env['MISSIONOS_TB3_GATEWAY_CONTAINER'])} "
            + f"MISSIONOS_TURTLEBOT3_WORLD_PROFILE={world_profile} "
            + shlex.join((str(script),))
        )
        return False
    if build_image:
        build_result = subprocess.run(build_cmd, cwd=str(repo_root), env=env, check=False)
        if build_result.returncode != 0:
            raise click.ClickException(
                f"TurtleBot3 Docker image build failed with exit code {build_result.returncode}."
            )
    start_result = subprocess.run((str(script),), cwd=str(repo_root), env=env, check=False)
    if start_result.returncode != 0:
        raise click.ClickException(
            f"TurtleBot3 Docker Gateway startup failed with exit code {start_result.returncode}."
        )
    return True


def _stop_turtlebot3_gateway_container() -> None:
    subprocess.run(
        ("docker", "rm", "-f", _turtlebot3_gateway_container_name()),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _maybe_retarget_turtlebot3_gateway_url(ctx: click.Context) -> None:
    gateway_url = str(ctx.obj.get("missionos_gateway_url") or DEFAULT_GATEWAY_URL)
    if gateway_url.rstrip("/") != DEFAULT_GATEWAY_URL:
        return
    client = ctx.obj.get("missionos_client")
    if not isinstance(client, MissionOSGatewayClient) or not _gateway_reachable(client):
        return
    alternate_url = os.environ.get(
        "MISSIONOS_TB3_GATEWAY_URL",
        "http://127.0.0.1:18792",
    ).strip()
    if not alternate_url:
        return
    ctx.obj["missionos_gateway_url"] = alternate_url
    ctx.obj["missionos_client"] = make_client(alternate_url, client.timeout)
    console.print(
        "[yellow]Default Gateway is already reachable at "
        f"{DEFAULT_GATEWAY_URL}; using TurtleBot3 Gateway URL {alternate_url}. "
        "Pass --gateway-url explicitly to override.[/yellow]"
    )


def _floor_turtlebot3_chat_timeout(ctx: click.Context) -> None:
    client = ctx.obj.get("missionos_client")
    if not isinstance(client, MissionOSGatewayClient):
        return
    if client.timeout >= TURTLEBOT3_CHAT_TIMEOUT:
        return
    gateway_url = str(ctx.obj.get("missionos_gateway_url") or DEFAULT_GATEWAY_URL)
    ctx.obj["missionos_client"] = make_client(gateway_url, TURTLEBOT3_CHAT_TIMEOUT)


@missionos.command("chat")
@click.argument("initial_instruction", nargs=-1, required=False)
@click.option(
    "--robot",
    type=click.Choice(
        ["default", "turtlebot3", "turtlebot4", "nova-carter"],
        case_sensitive=False,
    ),
    default="default",
    show_default=True,
    help="Route chat through a robot-specific simulator entrypoint.",
)
@click.option(
    "--turtlebot3-build-image/--no-turtlebot3-build-image",
    default=False,
    show_default=True,
    help="Build the TurtleBot3 Docker image before running --robot turtlebot3.",
)
@click.option(
    "--turtlebot3-mid-recovery/--no-turtlebot3-mid-recovery",
    default=False,
    show_default=True,
    help="Run the TurtleBot3 mid-mission low-battery recovery smoke.",
)
@click.option(
    "--turtlebot3-dry-run",
    is_flag=True,
    help="Print the TurtleBot3 simulator/Gateway command without starting Docker.",
)
@click.option(
    "--turtlebot3-smoke",
    is_flag=True,
    help=(
        "Run the non-interactive TurtleBot3 Docker smoke. By default, "
        "`--robot turtlebot3` uses normal MissionOS chat plus operate/watch/map."
    ),
)
@click.option("--session-id", default=DEFAULT_SESSION_ID, show_default=True)
@click.option(
    "--history-path",
    default=DEFAULT_HISTORY_PATH,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Persist input history between chat sessions (Claude-Code-like ↑/↓).",
)
@click.option(
    "--autostart/--no-autostart",
    default=False,
    show_default=True,
    help="Autostart the Gateway when it is not running, then stop it when chat exits.",
)
@click.option(
    "--enable-live-sitl/--planning-only",
    default=False,
    show_default=True,
    help="Enable live SITL/dispatch opt-in env for an autostarted Gateway.",
)
@click.option(
    "--companion-terminals/--no-companion-terminals",
    default=True,
    show_default=True,
    help=(
        "When an interactive chat starts live flight, open operate/watch/map "
        "companion terminals and close them when chat exits."
    ),
)
@click.pass_context
def chat_command(
    ctx: click.Context,
    initial_instruction: tuple[str, ...],
    robot: str,
    turtlebot3_build_image: bool,
    turtlebot3_mid_recovery: bool,
    turtlebot3_dry_run: bool,
    turtlebot3_smoke: bool,
    session_id: str,
    history_path: Path,
    autostart: bool,
    enable_live_sitl: bool,
    companion_terminals: bool,
) -> None:
    """Start a text-first MissionOS operator session."""
    initial_raw, autostart, enable_live_sitl = _chat_initial_instruction_and_autostart(
        initial_instruction,
        autostart=autostart,
        enable_live_sitl=enable_live_sitl,
    )
    robot_profile = robot.lower()
    if robot_profile == "nova-carter":
        ctx.obj["missionos_chat_robot_profile"] = "nova_carter"
        if not initial_raw:
            initial_raw = DEFAULT_NOVA_CARTER_CHAT_INSTRUCTION
        if session_id == DEFAULT_SESSION_ID:
            session_id = "missionos-cli-nova-carter"
        _floor_turtlebot3_chat_timeout(ctx)
    elif robot_profile in {"turtlebot3", "turtlebot4"}:
        ctx.obj["missionos_chat_robot_profile"] = robot_profile
        _floor_turtlebot3_chat_timeout(ctx)
        if robot_profile == "turtlebot4" and turtlebot3_smoke:
            raise click.UsageError(
                "--turtlebot3-smoke can only be used with --robot turtlebot3."
            )
        if robot_profile == "turtlebot3" and turtlebot3_smoke:
            raise SystemExit(
                _run_turtlebot3_chat_smoke(
                    instruction=initial_raw or DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION,
                    build_image=turtlebot3_build_image,
                    mid_recovery=turtlebot3_mid_recovery,
                    dry_run=turtlebot3_dry_run,
                )
            )
        if not initial_raw:
            initial_raw = (
                DEFAULT_TURTLEBOT4_CHAT_INSTRUCTION
                if robot_profile == "turtlebot4"
                else DEFAULT_TURTLEBOT3_CHAT_INSTRUCTION
            )
        if session_id == DEFAULT_SESSION_ID:
            session_id = f"missionos-cli-{robot_profile}"
        if robot_profile == "turtlebot4" and turtlebot3_dry_run:
            raise click.UsageError(
                "--turtlebot3-dry-run can only be used with --robot turtlebot3."
            )
        if robot_profile == "turtlebot3" and turtlebot3_dry_run:
            _maybe_retarget_turtlebot3_gateway_url(ctx)
            _start_turtlebot3_gateway_container(
                gateway_url=ctx.obj["missionos_gateway_url"],
                instruction=initial_raw,
                build_image=turtlebot3_build_image,
                dry_run=True,
            )
            return
        if robot_profile == "turtlebot3":
            _maybe_retarget_turtlebot3_gateway_url(ctx)
    else:
        ctx.obj["missionos_chat_robot_profile"] = ""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    ctx.obj["missionos_chat_session_id"] = session_id
    ctx.obj["missionos_chat_companion_terminals_enabled"] = (
        companion_terminals and sys.stdin.isatty()
    )
    turtlebot3_container_started = False
    if robot_profile == "turtlebot3" and not _gateway_reachable(client):
        turtlebot3_gateway_api_key = client.api_key or secrets.token_urlsafe(32)
        client.api_key = turtlebot3_gateway_api_key
        turtlebot3_container_started = _start_turtlebot3_gateway_container(
            gateway_url=ctx.obj["missionos_gateway_url"],
            instruction=initial_raw,
            build_image=turtlebot3_build_image,
            dry_run=turtlebot3_dry_run,
            gateway_api_key=turtlebot3_gateway_api_key,
        )
        if turtlebot3_dry_run:
            return
    gateway_proc = _ensure_gateway(
        client,
        ctx.obj["missionos_gateway_url"],
        autostart=autostart and robot_profile != "turtlebot3",
        enable_live_sitl=enable_live_sitl,
    )
    console.print(_chat_help_panel())
    session = _build_chat_session(history_path)
    try:
        if initial_raw:
            console.print(f"[bold green]MissionOS>[/bold green] {initial_raw}")
            if not _handle_chat_input(ctx, client, initial_raw, session_id=session_id):
                return
            if not sys.stdin.isatty():
                return
        while True:
            try:
                with patch_stdout(raw=True):
                    raw = session.prompt(_chat_prompt_fragment(ctx))
            except KeyboardInterrupt:
                console.print("[yellow](Ctrl+C — type /quit or Ctrl+D to exit)[/yellow]")
                continue
            except EOFError:
                break
            if not _handle_chat_input(ctx, client, raw, session_id=session_id):
                break
    finally:
        _stop_turtlebot3_chat_task_status_monitor(ctx)
        _stop_chat_companion_terminals(ctx)
        if turtlebot3_container_started:
            console.print("[blue]Stopping the TurtleBot3 Gateway container...[/blue]")
            _stop_turtlebot3_gateway_container()
        if gateway_proc is not None:
            console.print("[blue]Stopping the autostarted Gateway...[/blue]")
            _terminate_gateway(gateway_proc)


# ---------------------------------------------------------------------------
# missionos play — AI mission-control lab (deterministic what-if)
# ---------------------------------------------------------------------------

PLAY_STATUS_STYLE = {"ready": "green", "warning": "yellow", "blocked": "red"}


def _play_exposure_style(exposure: str) -> str:
    return {"low": "green", "medium": "yellow", "high": "red"}.get(exposure, "white")


def _play_plan_table(scenario, plan) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(justify="right", style="bold")
    table.add_column()
    route = scenario.route(plan.route_name)
    table.add_row("Route", f"{plan.route_name} — {route.description}")
    table.add_row("Distance", f"{plan.route_distance_m:,.0f} m (round trip modelled)")
    table.add_row("Altitude", f"{plan.knobs.altitude_m:,.0f} m MSL")
    margin_style = "green" if plan.clearance_margin_m >= 0 else "red"
    table.add_row(
        "Terrain clearance",
        f"{plan.clearance_m:,.0f} m  "
        f"([{margin_style}]{plan.clearance_margin_m:+.0f} m vs {plan.knobs.min_clearance_rule_m:.0f} m rule[/{margin_style}])",
    )
    exposure_style = _play_exposure_style(plan.wind_exposure)
    table.add_row(
        "Wind exposure",
        f"[{exposure_style}]{plan.wind_exposure}[/{exposure_style}] "
        f"({plan.effective_wind_mps:.1f} m/s effective)",
    )
    reserve_style = "green" if plan.return_feasible else "red"
    table.add_row(
        "Return reserve",
        f"[{reserve_style}]{plan.return_reserve_wh:,.0f} Wh "
        f"({plan.battery_reserve_fraction * 100:.0f}% of pack)[/{reserve_style}]",
    )
    table.add_row("Return feasible", "yes" if plan.return_feasible else "[red]no[/red]")
    if plan.risk_labels:
        table.add_row("Risk", ", ".join(plan.risk_labels))
    return table


def _render_play_plan(scenario, plan) -> None:
    style = PLAY_STATUS_STYLE.get(plan.status, "white")
    body = Group(
        _play_plan_table(scenario, plan),
        Text(""),
        Text.from_markup(
            f"[bold]MissionOS proposes[/bold] → [{style}]{plan.recommendation.value}[/{style}]\n"
            f"{plan.recommendation_reason}"
        ),
    )
    console.print(
        Panel(
            body,
            title=f"[{style}]{scenario.title} — status: {plan.status}[/{style}]",
            border_style=style,
        )
    )


def _render_play_compare(scenario, plan_a, plan_b, compare_plans) -> None:
    delta = compare_plans(plan_a, plan_b)
    table = Table(title="Compare: baseline → current", box=None)
    table.add_column("Metric", style="bold")
    table.add_column("Baseline", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Δ", justify="right")

    def signed(value: float, unit: str, good_when_positive: bool) -> str:
        good = value >= 0 if good_when_positive else value <= 0
        style = "green" if good else "red"
        return f"[{style}]{value:+,.0f} {unit}[/{style}]"

    table.add_row(
        "Clearance",
        f"{plan_a.clearance_m:,.0f} m",
        f"{plan_b.clearance_m:,.0f} m",
        signed(delta.clearance_m, "m", True),
    )
    table.add_row(
        "Return reserve",
        f"{plan_a.return_reserve_wh:,.0f} Wh",
        f"{plan_b.return_reserve_wh:,.0f} Wh",
        signed(delta.return_reserve_wh, "Wh", True),
    )
    table.add_row(
        "Effective wind",
        f"{plan_a.effective_wind_mps:.1f} m/s",
        f"{plan_b.effective_wind_mps:.1f} m/s",
        signed(delta.effective_wind_mps, "m/s", False),
    )
    table.add_row(
        "Distance",
        f"{plan_a.route_distance_m:,.0f} m",
        f"{plan_b.route_distance_m:,.0f} m",
        signed(delta.route_distance_m, "m", False),
    )
    console.print(table)


def _render_play_weather(scenario, forecast, knobs) -> None:
    """Show the real forecast and the SITL realism env it would forward."""
    from src.runtime.missionos_play_sitl_conditions import build_sitl_conditions

    agl = max(0.0, knobs.altitude_m - scenario.takeoff_elevation_m)
    conditions = build_sitl_conditions(
        forecast, flight_agl_m=agl, payload_kg=knobs.payload_kg
    )

    forecast_table = Table(title="Real weather (Open-Meteo)", box=None)
    forecast_table.add_column("Time (UTC)", style="bold")
    forecast_table.add_column("Surface wind", justify="right")
    forecast_table.add_column("Gust", justify="right")
    forecast_table.add_column("Dir", justify="right")
    cur = forecast.current
    forecast_table.add_row(
        f"{cur.valid_at} (now)",
        f"{cur.wind_speed_mps} m/s",
        f"{cur.wind_gust_mps} m/s",
        f"{cur.wind_direction_deg}°",
    )
    for sample in forecast.hourly[:6]:
        forecast_table.add_row(
            sample.valid_at,
            f"{sample.wind_speed_mps} m/s",
            f"{sample.wind_gust_mps} m/s",
            f"{sample.wind_direction_deg}°",
        )
    console.print(forecast_table)

    env_table = Table(
        title=f"Forwarded to SITL @ {agl:,.0f} m AGL (modelled altitude profile)",
        box=None,
    )
    env_table.add_column("Realism env", style="bold")
    env_table.add_column("Value", justify="right")
    for key, value in conditions.realism_env.items():
        env_table.add_row(key.replace("MISSION_DESIGNER_REALISM_", ""), value)
    console.print(env_table)

    matrix = conditions.capability_matrix
    notes = ", ".join(matrix.get("approximation_reasons", [])) or "none"
    console.print(
        f"[dim]real=forwarded surface wind/gust/direction · "
        f"modelled=altitude profile · approximations: {notes}\n"
        f"Final Gazebo/PX4 application is recorded by the runner's own "
        f"capability matrix at flight time.[/dim]"
    )


def _render_play_flight_result(result) -> None:
    style = "green" if result.status == "completed" else "red"
    table = Table(title="Live PX4/Gazebo SITL flight", box=None)
    table.add_column("Evidence", style="bold")
    table.add_column("Value")
    table.add_row("Status", f"[{style}]{result.status}[/{style}]")
    table.add_row(
        "Takeoff observed", "yes" if result.takeoff_observed else "[red]no[/red]"
    )
    table.add_row("Wind updates", str(len(result.wind_steps)))
    if result.wind_steps:
        latest = result.wind_steps[-1]
        table.add_row(
            "Latest wind",
            f"{latest.wind_mps:.2f} m/s from {latest.bearing_from_deg:.0f}° "
            f"@ {latest.altitude_agl_m:.1f} m AGL",
        )
        table.add_row(
            "Latest force",
            f"east={latest.force_east_n:.2f} N, north={latest.force_north_n:.2f} N",
        )
    recovery = result.recovery_agent_result or {}
    table.add_row("Recovery agent", str(recovery.get("runtime_status") or "not_run"))
    if recovery.get("blocking_reasons"):
        table.add_row("Recovery blocking", ", ".join(recovery["blocking_reasons"]))
    if result.blocking_reasons:
        table.add_row("Blocking", ", ".join(result.blocking_reasons))
    console.print(table)
    console.print(
        "[dim]This is a live simulator takeoff and wind-disturbance run. "
        "It does not claim delivery completion, physical execution, or progress.[/dim]"
    )


def _play_help_panel() -> Panel:
    return Panel(
        Text.from_markup(
            "[bold]MissionOS play — you are the controller.[/bold]\n"
            "Turn the knobs, read how the situation changes, take MissionOS's\n"
            "recommendation, and approve. Going higher is never a free win.\n\n"
            "[bold]Commands[/bold]\n"
            "  altitude <m>            set flight altitude (MSL)\n"
            "  route direct|east|west  pick a corridor\n"
            "  wind <m/s>              declare wind speed\n"
            "  payload <kg>            set payload weight\n"
            "  rule min-clearance <m>  set the safety clearance rule\n"
            "  weather                 show real weather + the SITL env it forwards\n"
            "  show                    re-render the current plan\n"
            "  compare                 compare baseline → current\n"
            "  approve                 accept current as the new baseline (human gate)\n"
            "  fly                     run live PX4/Gazebo takeoff + wind disturbance\n"
            "  help / quit"
        ),
        title="play",
        border_style="cyan",
    )


def _play_apply_command(knobs, raw: str, valid_routes):
    """Return (new_knobs, message). ``message`` non-empty signals a notice."""
    parts = raw.split()
    if not parts:
        return knobs, ""
    verb = parts[0].lower()
    try:
        if verb in {"altitude", "alt"}:
            return knobs.with_(altitude_m=float(parts[1])), ""
        if verb == "route":
            choice = parts[1].lower()
            if choice not in valid_routes:
                return knobs, (
                    f"[red]Unknown route '{choice}'.[/red] "
                    f"Choices: {', '.join(valid_routes)}"
                )
            return knobs.with_(route=choice), ""
        if verb == "wind":
            return knobs.with_(declared_wind_mps=float(parts[1])), ""
        if verb == "payload":
            return knobs.with_(payload_kg=float(parts[1])), ""
        if verb == "rule":
            # "rule min-clearance 40" or "rule 40"
            value = parts[-1]
            return knobs.with_(min_clearance_rule_m=float(value)), ""
    except (IndexError, ValueError):
        return knobs, f"[red]Could not parse:[/red] {raw}"
    return knobs, ""


@missionos.command("play")
@click.argument("destination", nargs=-1, required=False)
@click.option(
    "--scenario",
    "scenario_key",
    default=None,
    help="Bundled scenario key (default: the flagship Fuji mountain-hut delivery).",
)
@click.option(
    "--real-weather/--bundled-weather",
    default=False,
    show_default=True,
    help="Fetch real local weather (Open-Meteo hourly) and drive wind from it.",
)
@click.option(
    "--forecast-hours",
    default=12,
    show_default=True,
    type=click.IntRange(1, 48),
    help="How many forecast hours to pull when --real-weather is on.",
)
@click.option(
    "--flight-duration",
    default=20.0,
    show_default=True,
    type=click.FloatRange(4.0, 300.0),
    help="Seconds to run the live SITL wind-disturbance segment after takeoff.",
)
@click.option(
    "--wind-step",
    default=2.0,
    show_default=True,
    type=click.FloatRange(0.5, 30.0),
    help="Seconds between live wind-force updates during `fly`.",
)
@click.option(
    "--battery-coupling/--no-battery-coupling",
    default=False,
    show_default=True,
    help="Enable the rotor-load-coupled battery in the live `fly` flight.",
)
@click.option(
    "--gps-denied/--gps-available",
    default=False,
    show_default=True,
    help="Disable EKF GPS fusion in the live `fly` flight (GPS-denied scenario).",
)
@click.option(
    "--history-path",
    default=DEFAULT_HISTORY_PATH + "_play",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
def play_command(
    destination: tuple[str, ...],
    scenario_key: str | None,
    real_weather: bool,
    forecast_hours: int,
    flight_duration: float,
    wind_step: float,
    battery_coupling: bool,
    gps_denied: bool,
    history_path: Path,
) -> None:
    """Experimental deterministic lab; not the main LLM chat path.

    Deterministic what-if lab. With --bundled-weather (default) it needs no
    network; --real-weather pulls live Open-Meteo conditions for the scenario
    and drives the wind from the real, altitude-adjusted forecast.
    """
    from src.runtime.missionos_play_scenario import DEFAULT_SCENARIO_KEY, load_scenario
    from src.runtime.missionos_play_session import (
        PlayKnobs,
        compare_plans,
        evaluate_plan,
    )
    from src.runtime.missionos_play_weather import fetch_weather_forecast, profile_wind_at
    from src.runtime.missionos_play_sitl_conditions import wind_at_altitude

    try:
        scenario = load_scenario(scenario_key or DEFAULT_SCENARIO_KEY)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc

    if destination:
        stated = " ".join(destination)
        console.print(
            f"[yellow]Custom destinations are not planned yet.[/yellow] "
            f"'{stated}' is noted but not routed — play runs the bundled "
            f"scenario [bold]'{scenario.title}'[/bold] (pick one with --scenario)."
        )

    forecast = None
    if real_weather:
        with console.status("[cyan]Fetching real weather + altitude profile (Open-Meteo)...[/cyan]"):
            forecast = fetch_weather_forecast(
                scenario.takeoff_lat, scenario.takeoff_lon,
                forecast_hours=forecast_hours, with_profile=True,
            )
        if forecast.source_unavailable:
            console.print(
                "[yellow]Real weather unavailable; falling back to bundled "
                f"ambient wind.[/yellow] ({forecast.provider_response_status})"
            )
            forecast = None
        else:
            console.print(
                f"[green]Real weather:[/green] surface wind "
                f"{forecast.current.wind_speed_mps} m/s, gust "
                f"{forecast.current.wind_gust_mps} m/s, dir "
                f"{forecast.current.wind_direction_deg}° "
                f"({len(forecast.hourly)} forecast hours)"
            )

    wind_pinned = {"value": False}

    def resolve_knobs(current: PlayKnobs) -> PlayKnobs:
        """Drive wind from real, altitude-adjusted weather unless pinned manually.

        Prefer the real multi-height profile (matches what `fly` injects); fall
        back to the modelled power-law only when no profile was fetched.
        """
        if forecast is None or wind_pinned["value"]:
            return current
        agl = max(0.0, current.altitude_m - scenario.takeoff_elevation_m)
        real = profile_wind_at(forecast, agl)
        if real is None:
            surface = forecast.current.wind_speed_mps or scenario.ambient_wind_mps
            real = wind_at_altitude(surface, agl)
        return current.with_(declared_wind_mps=real)

    def eval_current(current: PlayKnobs):
        return evaluate_plan(scenario, resolve_knobs(current))

    knobs = PlayKnobs(altitude_m=3000.0, route="direct", payload_kg=1.0)
    baseline = eval_current(knobs)

    console.print(_play_help_panel())
    _render_play_plan(scenario, baseline)

    history_path.parent.mkdir(parents=True, exist_ok=True)
    session = PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(
            ["altitude", "route", "wind", "payload", "rule min-clearance",
             "weather", "show", "compare", "approve", "fly", "help", "quit"],
            ignore_case=True,
        ),
    )

    while True:
        try:
            raw = session.prompt(HTML("<ansicyan>play></ansicyan> ")).strip()
        except KeyboardInterrupt:
            console.print("[yellow](Ctrl+C — type quit or Ctrl+D to exit)[/yellow]")
            continue
        except EOFError:
            break
        if not raw:
            continue
        verb = raw.split()[0].lower()
        if verb in {"quit", "exit"}:
            break
        if verb == "help":
            console.print(_play_help_panel())
            continue
        if verb == "weather":
            if forecast is None:
                console.print(
                    "[yellow]No real weather loaded.[/yellow] Start with "
                    "[bold]missionos play --real-weather[/bold] to pull live "
                    "Open-Meteo conditions for this scenario."
                )
                continue
            _render_play_weather(scenario, forecast, resolve_knobs(knobs))
            continue
        if verb in {"show", "status"}:
            _render_play_plan(scenario, eval_current(knobs))
            continue
        if verb == "compare":
            _render_play_compare(
                scenario, baseline, eval_current(knobs), compare_plans
            )
            continue
        if verb == "approve":
            plan = eval_current(knobs)
            if plan.status == "blocked":
                console.print(
                    "[red]Cannot approve a blocked plan.[/red] "
                    "Resolve the risks above first."
                )
                continue
            baseline = plan
            console.print(
                "[green]Approved.[/green] Recorded as the new baseline (human gate). "
                "Rules still constrain dispatch; approval is not flight."
            )
            continue
        if verb == "fly":
            if forecast is None:
                console.print(
                    "[yellow]Live play flight needs real weather for the wind "
                    "driver.[/yellow] Restart with "
                    "[bold]missionos play --real-weather[/bold]."
                )
                continue
            plan = eval_current(knobs)
            if plan.status == "blocked":
                console.print(
                    "[red]Cannot dispatch a blocked play plan.[/red] "
                    "Resolve the risks first."
                )
                continue
            from src.runtime.missionos_play_live_sitl import run_play_live_sitl

            console.print(
                "[cyan]Starting live PX4/Gazebo SITL, taking off, and injecting "
                "time/altitude-varying wind...[/cyan]"
            )
            with console.status("[cyan]Live SITL flight in progress...[/cyan]"):
                result = run_play_live_sitl(
                    scenario=scenario,
                    forecast=forecast,
                    duration_s=flight_duration,
                    step_s=wind_step,
                    battery_coupling=battery_coupling,
                    gps_denied=gps_denied,
                )
            _render_play_flight_result(result)
            continue
        if verb == "wind":
            wind_pinned["value"] = True
        knobs, message = _play_apply_command(knobs, raw, set(scenario.routes))
        if message:
            console.print(message)
            continue
        _render_play_plan(scenario, eval_current(knobs))

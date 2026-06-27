"""MissionOS operator CLI backed by the same Gateway routes as the Control UI."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse
import html
import json
import math
import os
import re
import signal
import shlex
import subprocess
import sys
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
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# Live SITL start/dispatch (Gazebo flight) routinely runs for minutes, well past
# the 45s default. Floor the SITL calls at a long timeout so the live path does
# not abandon a dispatch the Gateway is still running.
SITL_DISPATCH_TIMEOUT = 900.0
SITL_EXECUTION_POLL_INTERVAL = 5.0
SITL_EXECUTION_POLL_TIMELINE_LIMIT = 5
ACTIVE_RUNNER_RECOVERY_OBSERVATION_TIMEOUT_SECONDS = 95.0
TERMINAL_TASK_STATUSES = frozenset({"completed", "blocked", "failed", "cancelled", "canceled"})
TutorialOutcome = str | None

DEFAULT_GATEWAY_URL = "http://127.0.0.1:18791"
DEFAULT_SESSION_ID = "missionos-cli"
DEFAULT_STATE_PATH = "data/missionos_cli_state.json"
DEFAULT_HISTORY_PATH = "data/missionos_cli_history"
DEFAULT_GATEWAY_PID_PATH = Path("data/missionos_gateway.pid")
DEFAULT_GATEWAY_LOG_PATH = Path("data/missionos_gateway.log")
GATEWAY_PID_RECORD_SCHEMA_VERSION = "missionos_gateway_pidfile.v1"
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
    "/land",
    "/rtl",
    "/help",
    "/clear",
    "/quit",
)
CONVERSATION_ROUTE = "/missionos/autonomy-conversation/run"
RECOVERY_DISPATCH_ROUTE = "/px4-gazebo/mission-scenarios/recovery-dispatch"
SITL_START_ROUTE = "/px4-gazebo/mission-scenarios/start-sitl"
SITL_EXECUTION_ROUTE = "/px4-gazebo/mission-scenarios/execute-sitl"

INTENT_INSTRUCTIONS = {
    "approve": "承認して",
    "reject": "拒否して",
    "revision": "修正して",
    "run": "実行して",
    "repair": "修復して",
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
    "GUI と同じ富士山デリバリーを計画して。Mt. Fuji coordinate route を使い、"
    "payload delivery SITL readiness まで準備して。"
)
DEFAULT_TUTORIAL_SESSION_ID = "missionos-cli-tutorial"

console = Console()


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _format_http_error_detail(
    method: str,
    path: str,
    status_code: int,
    payload: Any,
) -> str:
    if (
        path == SITL_EXECUTION_ROUTE
        and status_code == 409
        and isinstance(payload, dict)
    ):
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
                limit = item.get("limit_value")
                unit = str(item.get("unit") or "")
                if requested is not None and limit is not None:
                    violation_details.append(
                        f"{kind} (requested={requested}{unit}, max={limit}{unit})"
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
        # Compact summary from common fields so we never dump the whole task
        # (recovery-dispatch 409 etc. embed the full task + heightmap arrays).
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


@dataclass
class MissionOSGatewayClient:
    base_url: str
    timeout: float = 45.0

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        ok_status_codes: set[int] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=timeout if timeout is not None else self.timeout) as client:
                response = client.request(method, _join_url(self.base_url, path), **kwargs)
        except httpx.ConnectError as exc:
            raise click.ClickException(_gateway_unreachable_message(self.base_url)) from exc
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
        return self._request(
            "POST",
            CONVERSATION_ROUTE,
            json=payload,
        )

    def recovery_dispatch(self, *, task_id: str, recovery_action: str) -> dict[str, Any]:
        return self._request(
            "POST",
            RECOVERY_DISPATCH_ROUTE,
            ok_status_codes={409},
            json={
                "task_id": task_id,
                "recovery_action": recovery_action,
                "explicit_recovery_dispatch_approval": True,
            },
        )

    def execute_sitl(self, *, task_id: str, live_flight_mode: bool) -> dict[str, Any]:
        return self._request(
            "POST",
            SITL_EXECUTION_ROUTE,
            json={
                "task_id": task_id,
                "explicit_execution_approval": True,
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


def make_client(base_url: str, timeout: float) -> MissionOSGatewayClient:
    return MissionOSGatewayClient(base_url=base_url, timeout=timeout)


def _gateway_host_port(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    return parsed.hostname or "127.0.0.1", parsed.port or 18791


def _gateway_start_command(base_url: str) -> str:
    """Render the `web` invocation whose host/port match this gateway URL."""
    host, port = _gateway_host_port(base_url)
    return f"boiled-claw web --host {host} --port {port}"


def _gateway_unreachable_message(base_url: str) -> str:
    return (
        f"Gateway に接続できません: {base_url}\n"
        f"MissionOS CLI から起動できます:\n"
        f"  missionos gateway start\n"
        f"  missionos gateway start --enable-live-sitl  # SITL dispatch opt-in\n"
        f"  # raw: {_gateway_start_command(base_url)}\n"
        "一時起動なら `missionos chat --autostart` / "
        "`missionos tutorial --autostart` も使えます。ライブ SITL まで行う場合は "
        "`--enable-live-sitl` も明示してください。"
    )


_GATEWAY_LIVE_SITL_ENV = {
    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION": "1",
    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_LIVE_FLIGHT": "1",
    "RUN_MISSIONOS_SITL_DISPATCH_RUNTIME": "1",
    "RUN_MISSIONOS_AUTO_MISSION_GUI_DISPATCH": "1",
    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER": "1",
    "MISSION_DESIGNER_PX4_GAZEBO_SITL_DOCKER_CONTAINER": (
        "boiled-claw-px4-gazebo-sitl-mission-upload-smoke"
    ),
}

_GATEWAY_LLM_ADK_ENV_KEYS = (
    "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED",
    "MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED",
    "MISSIONOS_LLM_DIALOGUE_ROUTER_ADK_ENABLED",
    "MISSIONOS_LLM_REPAIR_PLANNER_ADK_ENABLED",
    "MISSIONOS_LLM_RESPONSE_PLANNER_ADK_ENABLED",
    "MISSIONOS_REAL_HARDWARE_ARM_DISARM_PLANNER_ADK_ENABLED",
)


def _dotenv_process_values(path: Path = Path(".env")) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip().strip("'\"")
        values[key] = value
    return values


def _llm_backend_from_env(env: dict[str, str]) -> str:
    backend = (
        env.get("MISSIONOS_LLM_BACKEND")
        or env.get("BOILED_CLAW_LLM_BACKEND")
        or "off"
    ).strip().lower()
    if backend in {"google", "google_adk"}:
        return "gemini"
    return backend


def _llm_backend_uses_google_credentials(env: dict[str, str]) -> bool:
    return _llm_backend_from_env(env) == "gemini"


def _llm_backend_default_adk_enabled(env: dict[str, str]) -> str:
    backend = _llm_backend_from_env(env)
    if backend in {"off", "none", "disabled", "deterministic"}:
        return "0"
    return "1"


def _apply_gateway_llm_env(env: dict[str, str]) -> None:
    env.setdefault("MISSIONOS_LLM_BACKEND", _llm_backend_from_env(env))
    default_adk_enabled = _llm_backend_default_adk_enabled(env)
    for key in _GATEWAY_LLM_ADK_ENV_KEYS:
        if default_adk_enabled == "0":
            env[key] = "0"
        else:
            env.setdefault(key, default_adk_enabled)

    if not _llm_backend_uses_google_credentials(env):
        env.pop("GOOGLE_API_KEY", None)


def _gateway_process_env(*, enable_live_sitl: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in _dotenv_process_values().items():
        env.setdefault(key, value)
    _apply_gateway_llm_env(env)
    if enable_live_sitl:
        env.update(_GATEWAY_LIVE_SITL_ENV)
    path_parts = [
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/Applications/Docker.app/Contents/Resources/bin",
        env.get("PATH", ""),
    ]
    env["PATH"] = os.pathsep.join(part for part in path_parts if part)
    return env


def _gateway_argv(base_url: str) -> list[str]:
    host, port = _gateway_host_port(base_url)
    return [sys.executable, "-m", "src.main", "web", "--host", host, "--port", str(port)]


def _gateway_command_signature(base_url: str) -> str:
    argv = _gateway_argv(base_url)
    return " ".join(shlex.quote(part) for part in argv)


def _gateway_reachable(client: MissionOSGatewayClient) -> bool:
    """Return True when the gateway answers a health probe."""
    try:
        client.health()
    except (click.ClickException, httpx.HTTPError):
        return False
    return True


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
        if autostart:
            console.print(
                "[yellow]Gateway は既に起動しています。--autostart は既存 Gateway を"
                f"再利用します: {base_url}[/yellow]"
            )
            if enable_live_sitl:
                console.print(
                    "[yellow]既存 Gateway の live SITL env は変更されません。"
                    "コード更新や env 変更を反映するには "
                    "`missionos gateway restart --enable-live-sitl` を使ってください。"
                    "[/yellow]"
                )
        return None
    if not autostart:
        raise click.ClickException(_gateway_unreachable_message(base_url))
    console.print(f"[blue]Gateway を自動起動します ({base_url})…[/blue]")
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
            raise click.ClickException("Gateway の自動起動に失敗しました（プロセスが終了）。")
        if _gateway_reachable(client):
            console.print("[green]Gateway 起動完了。[/green]")
            return proc
        time.sleep(0.5)
    _terminate_gateway(proc)
    raise click.ClickException("Gateway 起動の待機がタイムアウトしました。")


def _read_gateway_pid_record(pid_path: Path) -> dict[str, Any] | None:
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            payload["pid"] = int(payload.get("pid"))
        except (TypeError, ValueError):
            return None
        return payload
    try:
        pid = int(raw)
    except ValueError:
        return None
    return {"schema_version": "legacy_pidfile", "pid": pid}


def _read_gateway_pid(pid_path: Path) -> int | None:
    record = _read_gateway_pid_record(pid_path)
    if record is None:
        return None
    try:
        return int(record.get("pid"))
    except (TypeError, ValueError):
        return None


def _process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_group_id(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        return None


def _process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _process_start_time(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _build_gateway_pid_record(
    *,
    pid: int,
    base_url: str,
    enable_live_sitl: bool,
) -> dict[str, Any]:
    host, port = _gateway_host_port(base_url)
    return {
        "schema_version": GATEWAY_PID_RECORD_SCHEMA_VERSION,
        "pid": int(pid),
        "pgid": _process_group_id(pid),
        "argv": _gateway_argv(base_url),
        "command_signature": _gateway_command_signature(base_url),
        "cwd": str(Path.cwd()),
        "base_url": base_url,
        "host": host,
        "port": int(port),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "process_start_time": _process_start_time(pid),
        "enable_live_sitl": bool(enable_live_sitl),
        "managed_by": "missionos_cli_gateway_start",
    }


def _gateway_pid_record_matches_running_process(record: dict[str, Any]) -> bool:
    if record.get("schema_version") != GATEWAY_PID_RECORD_SCHEMA_VERSION:
        return False
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return False
    if not _process_running(pid):
        return True
    expected_pgid = record.get("pgid")
    current_pgid = _process_group_id(pid)
    if expected_pgid is not None and current_pgid != expected_pgid:
        return False
    expected_start = str(record.get("process_start_time") or "").strip()
    if expected_start:
        current_start = _process_start_time(pid)
        if not current_start or current_start != expected_start:
            return False
    command = _process_command(pid)
    host = str(record.get("host") or "")
    port = str(record.get("port") or "")
    if not command or "-m src.main web" not in command:
        return False
    if host and f"--host {host}" not in command:
        return False
    if port and f"--port {port}" not in command:
        return False
    return True


def _stop_gateway_pid(pid: int, *, timeout: float = 5.0) -> bool:
    if not _process_running(pid):
        return True
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _process_running(pid):
            return True
        time.sleep(0.1)
    if _process_running(pid):
        os.kill(pid, signal.SIGKILL)
    return not _process_running(pid)


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
    console.print(f"[blue]Gateway を起動しました:[/blue] pid={proc.pid} url={base_url}")
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
            "(live SITL/dispatch env は未設定)"
        )
    if not wait:
        return
    for _ in range(40):
        if proc.poll() is not None:
            pid_path.unlink(missing_ok=True)
            raise click.ClickException(
                f"Gateway の起動に失敗しました。ログを確認してください: {log_path}"
            )
        if _gateway_reachable(client):
            console.print("[green]Gateway health: healthy[/green]")
            return
        time.sleep(0.5)
    _stop_gateway_pid(proc.pid)
    pid_path.unlink(missing_ok=True)
    raise click.ClickException(
        f"Gateway health check timed out. ログを確認してください: {log_path}"
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
    task_id = summary.get("sitl_execution_task_id")
    if task_id:
        return str(task_id)
    task = mission_designer.get("sitl_execution_task")
    if isinstance(task, dict) and task.get("task_id"):
        return str(task["task_id"])
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
    state["mission_designer_context"] = context
    task_id = _mission_designer_sitl_task_id(payload)
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
        if snapshot.get("operator_recovery_request_observed") is True:
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
        if snapshot.get("operator_recovery_assist_attempted") is not None:
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
                f"setpoints={_status_text(snapshot.get('operator_recovery_assist_setpoint_frames_sent'))}"
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


def _operator_recovery_dispatch_command(action: Any) -> tuple[str, str, str] | None:
    normalized = str(action or "").strip().lower().replace("-", "_")
    if normalized in {"return_to_launch", "return_to_home", "return_home", "rtl"}:
        return ("RTL", "/rtl", "return_to_launch")
    if normalized == "land":
        return ("LAND", "/land", "land")
    return None


def _operator_recovery_dispatch_hint(
    *,
    task_id: Any,
    action: Any,
    compact: bool = False,
) -> str | None:
    task_text = _status_text(task_id)
    if task_text == "-":
        return None
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
        snapshot.get("operator_recovery_action") or receipt.get("recovery_action")
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
    if compact:
        return (
            f"operator_dispatch={status}; action={action}; "
            f"active_runner={active_runner}; runner_observed={runner_observed}; "
            f"ack={ack}{tracking_text}"
        )
    return (
        "Operator Dispatch: "
        f"status={status}; action={action}; active_runner={active_runner}; "
        f"runner_observed={runner_observed}; ack={ack}{tracking_text}"
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
    battery_text = _format_percent(snapshot.get("battery_remaining_percent"))
    terrain_clearance_m = _as_float(snapshot.get("terrain_clearance_m"))
    terrain_clearance_target_m = _as_float(snapshot.get("terrain_clearance_target_m"))
    terrain_clearance_status = _status_text(snapshot.get("terrain_clearance_status"))
    monitor_stop = _status_text(snapshot.get("monitor_stop_reason"))
    readiness_text = _status_text(readiness.get("readiness_status"))
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
    if actual_sitl_evidence is None and (
        progress_m is not None and progress_m > 0
    ):
        actual_sitl_evidence = True
    operator_recovery_hint = None

    if task_status == "running" and monitor_window_ended:
        headline = "後処理中: AUTO monitor ended; waiting for terminal receipt"
    elif task_status == "running":
        headline = "飛行中: AUTO mission telemetry is still updating"
    elif task_status == "completed" and recovery_was_guard_response:
        headline = "中断帰還完了: Gateway recorded a guarded recovery terminal result"
    elif task_status == "completed":
        headline = "完了: Gateway recorded a terminal live SITL result"
    elif task_status == "blocked":
        headline = "停止: Gateway blocked the task before completion"
    else:
        headline = f"状態: {task_status}"

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
        (
            "Terrain: "
            f"clearance={_format_distance(terrain_clearance_m)}; "
            f"target={_format_distance(terrain_clearance_target_m)}; "
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
            f"status={_status_text(agent_result.get('runtime_status') or agent_bridge.get('bridge_status'))}; "
            f"action={_status_text(agent_action)}; "
            f"risk_observed={agent_risk_text}; "
            f"blocked={blocking_text}; "
            "dispatch_authority=False"
        )
        if task_status == "running" and not monitor_window_ended:
            operator_recovery_hint = _operator_recovery_dispatch_hint(
                task_id=task.get("task_id"),
                action=agent_action,
            )
            if operator_recovery_hint:
                lines.append(operator_recovery_hint)
        if endurance:
            lines.append(
                "Agent Basis: "
                f"burn={_format_percent(endurance.get('battery_burn_percent_per_km'))}/km; "
                f"remaining={_format_distance(endurance.get('remaining_route_m'))}; "
                f"needs={_format_percent(endurance.get('projected_battery_required_percent'))}; "
                f"arrival={_format_percent(endurance.get('projected_arrival_battery_percent'))}; "
                f"reserve_margin={_format_percent(endurance.get('projected_reserve_margin_percent'))}"
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
        return "Execute Live SITL 実行中… Gateway 応答を待っています"
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
        proposal_status = _status_text(
            agent_result.get("runtime_status") or agent_bridge.get("bridge_status")
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
                compact=True,
            )
            if recovery_hint:
                parts.append(recovery_hint)
    if monitor_ended and status == "running":
        parts.append("後処理中")
    return "Execute Live SITL 実行中… " + " · ".join(parts)


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
                        "Execute Live SITL HTTP read timed out and task status "
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
    """Operate MissionOS through the same Gateway boundaries as the GUI."""
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
        console.print(f"[green]Gateway は既に起動しています:[/green] {base_url}")
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
            "[yellow]古い Gateway PID file が別プロセスを指していたため破棄しました。"
            "プロセスは停止していません。[/yellow]"
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
        console.print("[yellow]管理中の Gateway PID はありません。[/yellow]")
        return
    if _process_running(pid) and not _gateway_pid_record_matches_running_process(record or {}):
        pid_path.unlink(missing_ok=True)
        raise click.ClickException(
            f"Gateway PID file did not match a managed MissionOS Gateway: pid={pid}. "
            "Stale PID file was removed; no process was stopped."
        )
    if _stop_gateway_pid(pid):
        pid_path.unlink(missing_ok=True)
        console.print(f"[green]Gateway を停止しました:[/green] pid={pid}")
        return
    raise click.ClickException(f"Gateway を停止できませんでした: pid={pid}")


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
            raise click.ClickException(f"Gateway を停止できませんでした: pid={pid}")
        pid_path.unlink(missing_ok=True)
        console.print(f"[green]Gateway を停止しました:[/green] pid={pid}")
    elif pid_path.exists():
        pid_path.unlink(missing_ok=True)
        console.print("[yellow]停止済み Gateway の PID file を破棄しました。[/yellow]")
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
@click.pass_context
def recover_command(ctx: click.Context, task_id: str, recovery_action: str) -> None:
    """Send the same operator-approved LAND/RTL dispatch used by the GUI."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    payload = client.recovery_dispatch(task_id=task_id, recovery_action=recovery_action)
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
    help="Request the GUI-equivalent Execute Live SITL boundary.",
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
    """Run the GUI-equivalent explicit Execute Live SITL boundary."""
    resolved_task_id = task_id or _stored_sitl_task_id(ctx)
    if not resolved_task_id:
        raise click.ClickException(
            "task id is required; run `missionos run` first or pass --task-id"
        )
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    if live_flight:
        with console.status(
            "[red]Execute Live SITL 実行中… Gateway 応答を待っています[/red]",
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
        console.print(
            "[yellow]Execute Live SITL HTTP read timed out; showing latest task state.[/yellow]"
        )
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
    """Start the GUI-equivalent PX4/Gazebo SITL environment readiness action."""
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
    tlat = _as_float(route.get("takeoff_latitude"))
    tlon = _as_float(route.get("takeoff_longitude"))
    dlat = _as_float(route.get("dropoff_latitude"))
    dlon = _as_float(route.get("dropoff_longitude"))
    if None in (tlat, tlon, dlat, dlon):
        return None
    north = (dlat - tlat) * 111320.0
    east = (dlon - tlon) * 111320.0 * math.cos(math.radians(tlat))
    return (north, east)


def _fmt_metres(value: Any) -> str:
    metres = _as_float(value)
    if metres is None:
        return "-"
    if abs(metres) >= 1000.0:
        return f"{metres / 1000.0:.2f}km"
    return f"{metres:.0f}m"


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
        f"terrain_elev={_fmt_metres(terrain)}  AGL={_fmt_metres(clearance)}  "
        f"drone_amsl={_fmt_metres(current_amsl)}"
    )
    body.append(f"\n{footer}", style="dim")
    body.append("\n")
    body.append("▁=terrain elevation  ·=target AGL  ◆=drone altitude", style="dim")
    return Panel(
        body,
        title="Elevation Profile (横=route progress / 縦=AMSL altitude)",
        border_style="magenta",
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
    anchors: list[tuple[float, float]] = list(trail)
    anchors.append((0.0, 0.0))  # home
    if dropoff is not None:
        anchors.append(dropoff)
    projected = _project_flight_points(
        anchors, width=FLIGHT_MAP_WIDTH, height=FLIGHT_MAP_HEIGHT
    )
    grid: list[list[tuple[str, str]]] = [
        [(" ", "")] * FLIGHT_MAP_WIDTH for _ in range(FLIGHT_MAP_HEIGHT)
    ]
    n_trail = len(trail)
    for idx, (row, col) in enumerate(projected[:n_trail]):
        # Older path dim, recent path brighter green.
        style = "green" if idx >= n_trail - 12 else "grey42"
        grid[row][col] = ("·", style)
    cursor = n_trail
    home_row, home_col = projected[cursor]
    grid[home_row][home_col] = ("H", "bold blue")
    cursor += 1
    if dropoff is not None:
        d_row, d_col = projected[cursor]
        grid[d_row][d_col] = ("D", "bold yellow")
        cursor += 1
    if n_trail:
        dr, dc = projected[n_trail - 1]
        grid[dr][dc] = ("◆", "bold red")

    body = Text()
    for row in range(FLIGHT_MAP_HEIGHT):
        for col in range(FLIGHT_MAP_WIDTH):
            char, style = grid[row][col]
            body.append(char, style=style)
        if row != FLIGHT_MAP_HEIGHT - 1:
            body.append("\n")

    battery = _format_percent(snapshot.get("battery_remaining_percent"))
    reached = _status_text(_as_int(snapshot.get("mission_reached_seq")))
    total = _status_text(_as_int(snapshot.get("waypoint_total")))
    home_dist = snapshot.get("distance_to_home_m")
    title = "MissionOS Live Map (SITL · 上=North 右=East)"
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
            compact=True,
        )
    recovery_line = f"{recovery_hint}\n" if recovery_hint else ""
    hud = Text.from_markup(
        f"[bold]task[/bold]={task_id}  [bold]status[/bold]={status}\n"
        f"{process_line}"
        f"{recovery_line}"
        f"{_watch_altitude_status(snapshot)}\n"
        f"battery={battery}  wp={reached}/{total}  home_dist={_fmt_metres(home_dist)}\n"
        "[blue]H[/blue]=home  [yellow]D[/yellow]=dropoff  [red]◆[/red]=drone  "
        "[green]·[/green]=trail"
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
        "label": "地理院地図",
        "url_template": "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png",
        "attribution": "地理院タイル",
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


def _mission_map_model(
    *,
    task_payload: dict[str, Any],
    provider: str,
    live_task_url: str | None = None,
    poll_interval: float = MISSION_MAP_POLL_INTERVAL,
) -> dict[str, Any]:
    artifacts = _task_artifacts(task_payload)
    task = _task_record(task_payload)
    route = _mission_map_latlon_from_route(artifacts)
    if route is None:
        raise click.ClickException(
            "task does not include source coordinates; `missionos map` needs "
            "mission_designer_coordinate_pair_route takeoff/dropoff lat/lon"
        )
    takeoff_lat, takeoff_lon, dropoff_lat, dropoff_lon = route
    points: list[dict[str, Any]] = []
    for idx, sample in enumerate(_mission_map_flight_samples(artifacts)):
        latlon = _mission_map_sample_latlon(
            sample,
            takeoff_lat=takeoff_lat,
            takeoff_lon=takeoff_lon,
        )
        if latlon is None:
            continue
        lat, lon, source = latlon
        points.append(
            {
                "lat": lat,
                "lon": lon,
                "source": source,
                "phase": _status_text(sample.get("phase"), f"sample_{idx}"),
                "alt_m": _as_float(
                    sample.get("relative_alt_m")
                    or sample.get("local_z_m")
                    or sample.get("z_m")
                    or sample.get("z")
                ),
                "elapsed_s": sample.get("elapsed_s")
                or sample.get("elapsed_seconds")
                or sample.get("sample_time_s")
                or sample.get("sample_index"),
            }
        )
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
        if not points or (
            abs(points[-1]["lat"] - latest["lat"]) > 1e-8
            or abs(points[-1]["lon"] - latest["lon"]) > 1e-8
        ):
            points.append(latest)
    if not points:
        points = [
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
    return {
        "schema_version": "missionos_cli_2d_map.v1",
        "task_id": _status_text(task.get("task_id")),
        "task_status": _task_status(task_payload),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider_config,
        "route": {
            "takeoff": {"lat": takeoff_lat, "lon": takeoff_lon, "label": "H"},
            "dropoff": {"lat": dropoff_lat, "lon": dropoff_lon, "label": "D"},
        },
        "points": points,
        "latest": points[-1] if points else None,
        "live": {
            "enabled": bool(live_task_url),
            "task_url": live_task_url or "",
            "poll_interval_ms": max(500, int(float(poll_interval) * 1000)),
            "terminal_statuses": sorted(TERMINAL_TASK_STATUSES),
        },
        "boundaries": [
            "2D map uses real browser-fetched basemap tiles from the configured provider.",
            "MissionOS overlays only source route coordinates and observed/derived telemetry points.",
            "Map display is read-only and is not a verifier, dispatch control, or delivery claim.",
        ],
    }


def _json_for_html_script(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def _mission_map_html(model: dict[str, Any]) -> str:
    model_json = _json_for_html_script(model)
    escaped_title = html.escape(f"MissionOS 2D Map · {model['task_id']}")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escaped_title}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #07101d;
      --panel: rgba(8, 14, 25, 0.92);
      --line: rgba(148, 163, 184, 0.25);
      --text: #e5eefb;
      --muted: #96a4b8;
      --green: #22c55e;
      --blue: #38bdf8;
      --yellow: #facc15;
      --red: #f97373;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .shell {{ display: grid; gap: 14px; padding: 16px; }}
    header {{
      align-items: start;
      display: flex;
      justify-content: space-between;
      gap: 16px;
    }}
    h1 {{ margin: 0; font-size: 1.15rem; letter-spacing: 0; }}
    .muted {{ color: var(--muted); font-size: 0.86rem; line-height: 1.45; }}
    .live-status {{ margin-top: 4px; }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--text);
      background: rgba(15, 23, 42, 0.62);
      padding: 6px 10px;
      white-space: nowrap;
      font-size: 0.75rem;
      font-weight: 700;
    }}
    .map {{
      position: relative;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #dbe4ef;
      height: min(72vh, 760px);
      min-height: 420px;
      overflow: hidden;
    }}
    .tile {{
      position: absolute;
      display: block;
      width: 256px;
      height: 256px;
      max-width: none;
      user-select: none;
    }}
    svg.overlay {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
    }}
    .corridor {{
      fill: none;
      stroke: rgba(2, 6, 23, 0.46);
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 13;
    }}
    .path {{
      fill: none;
      stroke: var(--blue);
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 4;
    }}
    .marker-h {{ fill: var(--blue); stroke: white; stroke-width: 2; }}
    .marker-d {{ fill: var(--green); stroke: white; stroke-width: 2; }}
    .marker-current {{ fill: var(--red); stroke: white; stroke-width: 2; }}
    .label {{
      fill: white;
      font-size: 13px;
      font-weight: 800;
      paint-order: stroke;
      stroke: rgba(2, 6, 23, 0.88);
      stroke-width: 4;
    }}
    .attribution {{
      position: absolute;
      right: 8px;
      bottom: 8px;
      border-radius: 4px;
      background: rgba(255, 255, 255, 0.88);
      color: #111827;
      font-size: 0.72rem;
      padding: 5px 7px;
      text-decoration: none;
    }}
    .facts {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(220px, 100%), 1fr));
      gap: 8px;
    }}
    .fact {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 10px;
      min-width: 0;
    }}
    .fact span {{ display: block; color: var(--muted); font-size: 0.74rem; }}
    .fact strong {{ display: block; margin-top: 3px; overflow-wrap: anywhere; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <h1>MissionOS 2D Map</h1>
        <div class="muted">Real basemap tiles plus MissionOS route/telemetry overlay. This is read-only evidence display, not a verifier, dispatch control, or delivery claim.</div>
        <div class="muted live-status" id="liveStatus">Snapshot loaded.</div>
      </div>
      <div class="pill" id="providerPill">provider</div>
    </header>
    <section id="map" class="map" aria-label="MissionOS 2D map"></section>
    <section class="facts" id="facts"></section>
  </main>
  <script id="mission-map-data" type="application/json">{model_json}</script>
  <script>
    let data = JSON.parse(document.getElementById("mission-map-data").textContent);
    const TILE_SIZE = 256;
    const mapEl = document.getElementById("map");
    const factsEl = document.getElementById("facts");
    const providerEl = document.getElementById("providerPill");
    const liveStatusEl = document.getElementById("liveStatus");
    providerEl.textContent = data.provider.label;
    const liveConfig = data.live || {{ enabled: false }};
    const terminalStatuses = new Set(liveConfig.terminal_statuses || []);

    function setLiveStatus(message) {{
      liveStatusEl.textContent = message;
    }}

    function firstNumber(...values) {{
      for (const value of values) {{
        if (value === null || value === undefined || value === "") continue;
        const number = Number(value);
        if (Number.isFinite(number)) return number;
      }}
      return null;
    }}

    function statusText(value, fallback = "-") {{
      return value === null || value === undefined || value === "" ? fallback : String(value);
    }}

    function taskRecord(payload) {{
      return payload && typeof payload.task === "object" && payload.task !== null
        ? payload.task
        : (payload || {{}});
    }}

    function taskArtifacts(payload) {{
      if (payload && typeof payload.artifacts === "object" && payload.artifacts !== null) {{
        return payload.artifacts;
      }}
      const task = taskRecord(payload);
      return task && typeof task.artifacts === "object" && task.artifacts !== null
        ? task.artifacts
        : {{}};
    }}

    function taskStatus(payload) {{
      const task = taskRecord(payload);
      return statusText(task.status || task.task_status, "");
    }}

    function routeFromArtifacts(artifacts) {{
      const route = artifacts.mission_designer_coordinate_pair_route || {{}};
      const takeoffLat = firstNumber(route.takeoff_latitude, route.takeoff_latitude_deg);
      const takeoffLon = firstNumber(route.takeoff_longitude, route.takeoff_longitude_deg);
      const dropoffLat = firstNumber(route.dropoff_latitude, route.dropoff_latitude_deg);
      const dropoffLon = firstNumber(route.dropoff_longitude, route.dropoff_longitude_deg);
      if ([takeoffLat, takeoffLon, dropoffLat, dropoffLon].some((value) => value === null)) {{
        return null;
      }}
      return {{
        takeoff: {{ lat: takeoffLat, lon: takeoffLon, label: "H" }},
        dropoff: {{ lat: dropoffLat, lon: dropoffLon, label: "D" }},
      }};
    }}

    function localToLatLon(takeoff, northM, eastM) {{
      const lat = takeoff.lat + northM / 111320.0;
      const lonScale = Math.max(1e-9, 111320.0 * Math.cos((takeoff.lat * Math.PI) / 180));
      return {{ lat, lon: takeoff.lon + eastM / lonScale }};
    }}

    function sampleLatLon(sample, takeoff) {{
      const lat = firstNumber(sample.latitude_deg, sample.global_latitude_deg, sample.lat, sample.latitude);
      const lon = firstNumber(sample.longitude_deg, sample.global_longitude_deg, sample.lon, sample.longitude);
      if (lat !== null && lon !== null) {{
        return {{ lat, lon, source: "observed_wgs84" }};
      }}
      const north = firstNumber(sample.local_x_m, sample.x_m, sample.x);
      const east = firstNumber(sample.local_y_m, sample.y_m, sample.y);
      if (north === null || east === null) return null;
      return {{ ...localToLatLon(takeoff, north, east), source: "estimated_from_local_ned" }};
    }}

    function flightSamples(artifacts) {{
      for (const key of [
        "missionos_auto_mission_runtime_replay",
        "auto_mission_runtime_replay",
        "px4_gazebo_mission_designer_sitl_live_flight_run",
        "mission_designer_live_telemetry_snapshot",
      ]) {{
        const candidate = artifacts[key] || {{}};
        for (const samplesKey of ["flight_path_profile", "position_profile", "route_preview_waypoints"]) {{
          const samples = candidate[samplesKey];
          if (Array.isArray(samples) && samples.length) {{
            return samples.filter((sample) => sample && typeof sample === "object");
          }}
        }}
      }}
      return [];
    }}

    function telemetryPoint(sample, route, index, sourceSuffix = "") {{
      const latlon = sampleLatLon(sample, route.takeoff);
      if (!latlon) return null;
      return {{
        lat: latlon.lat,
        lon: latlon.lon,
        source: `${{latlon.source}}${{sourceSuffix}}`,
        phase: statusText(sample.phase, `sample_${{index}}`),
        alt_m: firstNumber(sample.relative_alt_m, sample.altitude_above_home_m, sample.local_z_m, sample.z_m, sample.z),
        elapsed_s: sample.elapsed_s ?? sample.elapsed_seconds ?? sample.sample_time_s ?? sample.sample_index ?? null,
      }};
    }}

    function mapModelFromTaskPayload(payload) {{
      const artifacts = taskArtifacts(payload);
      const route = routeFromArtifacts(artifacts);
      if (!route) throw new Error("task does not include source route coordinates");
      const points = [];
      flightSamples(artifacts).forEach((sample, index) => {{
        const point = telemetryPoint(sample, route, index);
        if (point) points.push(point);
      }});
      const snapshot = artifacts.missionos_auto_mission_runtime_snapshot || {{}};
      if (snapshot && typeof snapshot === "object") {{
        const latest = telemetryPoint(snapshot, route, points.length, "_latest_snapshot");
        if (latest && (!points.length
          || Math.abs(points[points.length - 1].lat - latest.lat) > 1e-8
          || Math.abs(points[points.length - 1].lon - latest.lon) > 1e-8)) {{
          points.push(latest);
        }}
      }}
      if (!points.length) {{
        points.push({{ ...route.takeoff, source: "route_takeoff", phase: "takeoff", alt_m: 0, elapsed_s: null }});
        points.push({{ ...route.dropoff, source: "route_dropoff", phase: "dropoff", alt_m: null, elapsed_s: null }});
      }}
      const task = taskRecord(payload);
      return {{
        ...data,
        task_id: statusText(task.task_id, data.task_id),
        task_status: taskStatus(payload),
        generated_at: new Date().toISOString(),
        route,
        points,
        latest: points[points.length - 1] || null,
      }};
    }}

    function mercator(lon, lat, zoom) {{
      const boundedLat = Math.max(-85.05112878, Math.min(85.05112878, lat));
      const sinLat = Math.sin((boundedLat * Math.PI) / 180);
      const worldSize = TILE_SIZE * (2 ** zoom);
      return {{
        x: ((lon + 180) / 360) * worldSize,
        y: (0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI)) * worldSize,
      }};
    }}

    function zoomFor(points, width, height) {{
      const padding = 110;
      for (let zoom = 18; zoom >= 2; zoom -= 1) {{
        const projected = points.map((point) => mercator(point.lon, point.lat, zoom));
        const xs = projected.map((point) => point.x);
        const ys = projected.map((point) => point.y);
        if ((Math.max(...xs) - Math.min(...xs)) <= width - padding
          && (Math.max(...ys) - Math.min(...ys)) <= height - padding) {{
          return zoom;
        }}
      }}
      return 2;
    }}

    function render() {{
      mapEl.innerHTML = "";
      const width = mapEl.clientWidth || 980;
      const height = mapEl.clientHeight || 560;
      const routePoints = [
        data.route.takeoff,
        data.route.dropoff,
        ...(data.points || []),
      ].filter((point) => Number.isFinite(point.lat) && Number.isFinite(point.lon));
      const zoom = zoomFor(routePoints, width, height);
      const projected = routePoints.map((point) => mercator(point.lon, point.lat, zoom));
      const xs = projected.map((point) => point.x);
      const ys = projected.map((point) => point.y);
      const centerX = (Math.min(...xs) + Math.max(...xs)) / 2;
      const centerY = (Math.min(...ys) + Math.max(...ys)) / 2;
      const left = centerX - width / 2;
      const top = centerY - height / 2;
      const tileCount = 2 ** zoom;
      const minTileX = Math.floor(left / TILE_SIZE);
      const maxTileX = Math.floor((left + width) / TILE_SIZE);
      const minTileY = Math.floor(top / TILE_SIZE);
      const maxTileY = Math.floor((top + height) / TILE_SIZE);
      for (let y = minTileY; y <= maxTileY; y += 1) {{
        if (y < 0 || y >= tileCount) continue;
        for (let x = minTileX; x <= maxTileX; x += 1) {{
          const wrappedX = ((x % tileCount) + tileCount) % tileCount;
          const img = document.createElement("img");
          img.className = "tile";
          img.alt = "";
          img.loading = "lazy";
          img.src = data.provider.url_template
            .replace("{{z}}", zoom)
            .replace("{{x}}", wrappedX)
            .replace("{{y}}", y);
          img.style.left = `${{(x * TILE_SIZE - left).toFixed(2)}}px`;
          img.style.top = `${{(y * TILE_SIZE - top).toFixed(2)}}px`;
          mapEl.appendChild(img);
        }}
      }}
      const toOverlay = (point) => {{
        const projectedPoint = mercator(point.lon, point.lat, zoom);
        return {{ x: projectedPoint.x - left, y: projectedPoint.y - top }};
      }};
      const overlayRoutePoints = [
        data.route.takeoff,
        ...(data.points || []),
        data.route.dropoff,
      ];
      const overlayPoints = overlayRoutePoints.map(toOverlay);
      const routeD = overlayPoints
        .map((point, index) => `${{index ? "L" : "M"}}${{point.x.toFixed(2)}} ${{point.y.toFixed(2)}}`)
        .join(" ");
      const home = toOverlay(data.route.takeoff);
      const dropoff = toOverlay(data.route.dropoff);
      const latest = data.latest ? toOverlay(data.latest) : overlayPoints[overlayPoints.length - 1];
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("class", "overlay");
      svg.setAttribute("viewBox", `0 0 ${{width}} ${{height}}`);
      svg.innerHTML = `
        ${{routeD ? `<path class="corridor" d="${{routeD}}"></path><path class="path" d="${{routeD}}"></path>` : ""}}
        <circle class="marker-h" cx="${{home.x.toFixed(2)}}" cy="${{home.y.toFixed(2)}}" r="7"></circle>
        <text class="label" x="${{Math.min(width - 70, home.x + 12).toFixed(2)}}" y="${{Math.max(22, home.y - 10).toFixed(2)}}">H home</text>
        <circle class="marker-d" cx="${{dropoff.x.toFixed(2)}}" cy="${{dropoff.y.toFixed(2)}}" r="9"></circle>
        <text class="label" x="${{Math.min(width - 90, dropoff.x + 12).toFixed(2)}}" y="${{Math.max(22, dropoff.y - 10).toFixed(2)}}">D dropoff</text>
        ${{latest ? `<circle class="marker-current" cx="${{latest.x.toFixed(2)}}" cy="${{latest.y.toFixed(2)}}" r="7"></circle><text class="label" x="${{Math.min(width - 110, latest.x + 12).toFixed(2)}}" y="${{Math.min(height - 18, latest.y + 22).toFixed(2)}}">current</text>` : ""}}
      `;
      mapEl.appendChild(svg);
      const attribution = document.createElement("a");
      attribution.className = "attribution";
      attribution.href = data.provider.attribution_url;
      attribution.target = "_blank";
      attribution.rel = "noopener noreferrer";
      attribution.textContent = data.provider.attribution;
      mapEl.appendChild(attribution);
      factsEl.innerHTML = [
        ["task", data.task_id],
        ["status", data.task_status || "-"],
        ["provider", data.provider.label],
        ["samples", String((data.points || []).length)],
        ["latest source", data.latest ? data.latest.source : "-"],
        ["live", data.live && data.live.enabled ? "polling" : "snapshot"],
        ["generated", data.generated_at],
      ].map(([key, value]) => `<div class="fact"><span>${{key}}</span><strong><code>${{String(value)}}</code></strong></div>`).join("");
    }}

    async function refreshLive() {{
      if (!liveConfig.enabled || !liveConfig.task_url) return;
      try {{
        const response = await fetch(liveConfig.task_url, {{ cache: "no-store" }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        data = mapModelFromTaskPayload(await response.json());
        render();
        const status = data.task_status || "-";
        setLiveStatus(`Live: updated ${{new Date().toLocaleTimeString()}} · status=${{status}}`);
        if (terminalStatuses.has(status) && window.__missionMapLiveTimer) {{
          window.clearInterval(window.__missionMapLiveTimer);
          window.__missionMapLiveTimer = null;
          setLiveStatus(`Live: terminal status ${{status}} · final update shown`);
        }}
      }} catch (error) {{
        setLiveStatus(`Live update failed: ${{error.message}}`);
      }}
    }}

    window.addEventListener("resize", render);
    render();
    if (liveConfig.enabled && liveConfig.task_url) {{
      setLiveStatus(`Live: polling Gateway every ${{Math.round((liveConfig.poll_interval_ms || 1000) / 100) / 10}}s`);
      window.__missionMapLiveTimer = window.setInterval(
        refreshLive,
        liveConfig.poll_interval_ms || 1000,
      );
      refreshLive();
    }} else {{
      setLiveStatus("Snapshot: no live polling");
    }}
  </script>
</body>
</html>
"""


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


def _watch_flight_map(
    client: MissionOSGatewayClient,
    task_id: str,
    *,
    poll_interval: float,
) -> None:
    trail: list[tuple[float, float]] = []
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
            snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            north = _as_float(snapshot.get("local_x_m"))
            east = _as_float(snapshot.get("local_y_m"))
            if north is not None and east is not None:
                if not trail or trail[-1] != (north, east):
                    trail.append((north, east))
                    if len(trail) > _FLIGHT_MAP_TRAIL_LIMIT:
                        del trail[: len(trail) - _FLIGHT_MAP_TRAIL_LIMIT]
            status = _task_status(task_payload)
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
                        f"[dim]task={task_id} status={status} — telemetry を待っています…[/dim]",
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
        console.print("[yellow](watch を終了しました)[/yellow]")


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
@click.option("--no-open", is_flag=True, help="Generate the HTML file without opening a browser.")
@click.pass_context
def map_command(
    ctx: click.Context,
    task_id: str,
    provider: str,
    output_path: Path | None,
    poll_interval: float,
    snapshot: bool,
    no_open: bool,
) -> None:
    """Generate a source-backed 2D browser map for the selected MissionOS task."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    resolved_task_id = _resolve_live_task_id(
        client,
        explicit_task_id=task_id,
        stored_task_id=_stored_sitl_task_id(ctx),
    )
    task_payload, _ = _task_and_timeline(client, resolved_task_id, timeline_limit=0)
    live_task_url = None
    if not snapshot:
        encoded_task_id = quote(resolved_task_id, safe="")
        live_task_url = _join_url(client.base_url, f"/tasks/{encoded_task_id}")
    model = _mission_map_model(
        task_payload=task_payload,
        provider=provider,
        live_task_url=live_task_url,
        poll_interval=poll_interval,
    )
    path = _write_mission_map_html(model=model, output_path=output_path)
    file_url = path.resolve().as_uri()
    if ctx.obj["missionos_json_output"]:
        _print_json(
            {
                "task_id": resolved_task_id,
                "map_provider": model["provider"]["label"],
                "output_path": str(path),
                "file_url": file_url,
                "point_count": len(model.get("points") or []),
                "live": bool(model.get("live", {}).get("enabled")),
                "opened": False,
            }
        )
        return
    opened = False
    if not no_open:
        opened = click.launch(file_url) == 0
    console.print(
        Panel(
            "\n".join(
                [
                    f"task_id={resolved_task_id}",
                    f"provider={model['provider']['label']}",
                    f"points={len(model.get('points') or [])}",
                    f"html={path}",
                    f"url={file_url}",
                    "live=" + ("true" if model.get("live", {}).get("enabled") else "false"),
                    "opened=" + ("true" if opened else "false"),
                    "boundary=real basemap tiles + MissionOS route/telemetry overlay; read-only, not verifier/dispatch/delivery claim",
                ]
            ),
            title="MissionOS 2D Map",
            border_style="cyan",
        )
    )


# ── Interactive operator view (`missionos operate`) ──────────────────────────
# Non-modal: live telemetry keeps refreshing while an agent proposal is shown.
# Dismissing ("状況を見る") re-surfaces the proposal after a cooldown. A real
# LAND/RTL dispatch always requires an explicit `y` confirmation — Enter/any key
# never fires recovery. Dispatch still goes through the same recovery-dispatch
# route with explicit approval; the agent never gains dispatch authority.
PROPOSAL_REDISPLAY_SECONDS = 30.0
_OPERATOR_RECOVERY_ACTIONS = {"return_to_launch": "RTL", "land": "LAND"}


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
        "status": _status_text(
            result.get("runtime_status") or bridge.get("bridge_status")
        ),
        "risks": [str(r) for r in risks if r],
    }


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
        if status != "running" or not _is_real_mission_designer_sitl_task(task):
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
    running = _latest_running_sitl_task_id(
        client,
        prefer_active_runner=True,
        require_active_runner=True,
    )
    if running:
        return running
    raise click.ClickException(
        "no active live SITL runner found for operator recovery; "
        "start a fresh live flight after restarting the Gateway, or pass --task-id explicitly"
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
    lines = [
        f"[bold]Agent Proposal:[/bold] {proposal.get('action', '-')}   "
        f"[dim](status={proposal.get('status', '-')}; dispatch_authority=False)[/dim]",
        f"[dim]risk = {risks}[/dim]",
        "",
    ]
    if confirming:
        label = _OPERATOR_RECOVERY_ACTIONS.get(confirming, confirming)
        lines.append(
            f"[bold red]{label} を送信します。実行するなら[/bold red] [bold]y[/bold]"
            "[bold red] を押す（他キー＝中止）[/bold red]"
        )
        border = "red"
    else:
        lines.append(
            "[green]既定: 何もしない（dispatch しない）[/green]   "
            "[dim]30秒後にまた提案します[/dim]"
        )
        lines.append(
            "  [bold]r[/bold]=RTL を承認(要 y)   "
            "[bold]l[/bold]=LAND を承認(要 y)   "
            "[bold]d[/bold]/Esc=状況を見る   [bold]q[/bold]=終了"
        )
        border = "yellow"
    return Panel("\n".join(lines), title="Operator Action", border_style=border)


_RECOVERY_RISK_LABELS = {
    "battery_projected_insufficient_for_route": "電池がルート完走に不足",
    "battery_projected_insufficient_for_return_home": "電池が帰還にも不足",
    "terrain_clearance_below_minimum": "地形クリアランス不足",
    "route_deviation_above_limit": "ルート逸脱が大きい",
    "telemetry_stale": "テレメトリが途切れ気味",
}


def _humanize_risks(risks: list[str]) -> str:
    if not risks:
        return "特になし"
    return "、".join(_RECOVERY_RISK_LABELS.get(r, r) for r in risks)


def _humanize_recovery_summary(
    proposal: dict[str, Any],
    endurance: dict[str, Any],
    return_home: dict[str, Any],
) -> list[str]:
    """Plain-language situation + return feasibility + recommendation for a human."""
    needs = _as_float(endurance.get("projected_battery_required_percent"))
    route_arrival = _as_float(endurance.get("projected_arrival_battery_percent"))
    route_infeasible = (needs is not None and needs > 100.0) or (
        route_arrival is not None and route_arrival < 0.0
    )
    rtl_insufficient = return_home.get("projected_insufficient_for_return_home") is True
    rtl_arrival = _as_float(return_home.get("projected_return_arrival_battery_percent"))
    home_m = return_home.get("distance_to_home_m")

    lines: list[str] = []
    if route_infeasible and needs is not None:
        lines.append(
            f"[bold red]状況:[/bold red] このルートは完走できません"
            f"（電池が約{needs / 100.0:.1f}倍必要・このままだと電池切れ）。"
        )
    elif route_infeasible:
        lines.append("[bold red]状況:[/bold red] このルートは完走できません（電池不足）。")
    else:
        lines.append("[green]状況:[/green] ルートは電池的に完走可能。")
    if proposal.get("risks"):
        lines.append(f"[dim]検知:[/dim] {_humanize_risks(proposal['risks'])}。")
    if return_home:
        if not rtl_insufficient:
            extra = f"・帰着時 {rtl_arrival:.0f}% 残" if rtl_arrival is not None else ""
            home_txt = (
                f"（home まで {_fmt_metres(home_m)}{extra}）" if home_m is not None else ""
            )
            lines.append(f"[green]帰還:[/green] いま戻れば安全{home_txt}。")
        else:
            lines.append("[bold red]帰還:[/bold red] 帰還も電池が厳しい状況。")
    if route_infeasible and return_home and not rtl_insufficient:
        rec = "[bold]→ 通常は RTL（`missionos rtl`）が妥当。続行は非推奨。[/bold]"
    elif route_infeasible and rtl_insufficient:
        rec = "[bold]→ LAND（`missionos land`）を検討（RTL も電池が厳しい）。[/bold]"
    else:
        rec = "[bold]→ 続行で可。提案は念のため。[/bold]"
    if proposal.get("action") == "operator_review":
        rec += " [dim](エージェントは最終判断をオペレーターに委ねています)[/dim]"
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
    bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    telemetry = bridge.get("telemetry_snapshot")
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    battery = telemetry.get("battery") if isinstance(telemetry.get("battery"), dict) else {}
    endurance = battery.get("endurance_projection")
    endurance = endurance if isinstance(endurance, dict) else {}
    return_home = battery.get("return_home_projection")
    return_home = return_home if isinstance(return_home, dict) else {}

    lines: list[str] = []
    if show_proposal and proposal:
        lines.extend(_humanize_recovery_summary(proposal, endurance, return_home))
        detail = (
            f"[dim]詳細: proposal={proposal.get('action', '-')} "
            f"({proposal.get('status', '-')}; dispatch_authority=False); "
            f"risk={', '.join(proposal.get('risks', [])) or '-'}"
        )
        if endurance:
            detail += (
                "; route "
                f"needs={_format_percent(endurance.get('projected_battery_required_percent'))}/"
                f"arrival={_format_percent(endurance.get('projected_arrival_battery_percent'))}/"
                f"burn={_format_percent(endurance.get('battery_burn_percent_per_km'))}per_km"
            )
        if return_home:
            detail += (
                "; RTL "
                f"home={_format_distance(return_home.get('distance_to_home_m'))}/"
                f"needs={_format_percent(return_home.get('projected_return_battery_required_percent'))}/"
                f"arrival={_format_percent(return_home.get('projected_return_arrival_battery_percent'))}"
            )
        detail += "[/dim]"
        lines.append(detail)
    elif status == "running":
        lines.append("[dim]認識・提案: 待機中（live 提案が出るとここに表示）[/dim]")
    else:
        lines.append(f"[dim]status={status}（提案は飛行中のみ）[/dim]")

    lines.append("")
    tid = task_id or "<task>"
    lines.append(
        "[dim]承認は別ペインで（標準の y/N 確認あり）:[/dim] "
        f"[bold]missionos rtl[/bold] / [bold]missionos land[/bold]  "
        f"[dim](task={tid}) · 終了: Ctrl-C[/dim]"
    )
    border = "yellow" if (show_proposal and proposal) else "cyan"
    return Panel(
        "\n".join(lines),
        title="Runtime Recovery Agent — operator console",
        border_style=border,
    )


def _render_operate_status_line(
    snapshot: dict[str, Any], *, status: str, task_id: str
) -> Text:
    """One compact live-telemetry line for operate (full map is in `missionos watch`)."""
    reached = _status_text(_as_int(snapshot.get("mission_reached_seq")))
    total = _status_text(_as_int(snapshot.get("waypoint_total")))
    return Text.from_markup(
        f"[dim]task={task_id} status={status} · "
        f"battery={_format_percent(snapshot.get('battery_remaining_percent'))} · "
        f"clearance={_fmt_metres(snapshot.get('terrain_clearance_m'))} · "
        f"alt(home)={_fmt_metres(snapshot.get('altitude_above_home_m'))} · "
        f"wp={reached}/{total} · "
        f"progress={_fmt_metres(snapshot.get('progress_m'))} · "
        f"home={_fmt_metres(snapshot.get('distance_to_home_m'))} · "
        "全体マップは別ペインで `missionos watch`[/dim]"
    )


def _operate_live(
    client: MissionOSGatewayClient,
    task_id: str,
    *,
    poll_interval: float,
) -> None:
    """Live, read-only operator viewer (acting is via `missionos rtl` / `land`).

    Single-key capture proved unreliable across terminals (VSCode etc.), so this
    is a pure viewer: it renders the agent recognition/recommendation and tells
    the operator the exact command to approve. No keyboard capture.
    """
    with Live(console=console, refresh_per_second=4, screen=False) as live:
        while True:
            try:
                task_payload, _ = _task_and_timeline(client, task_id, timeline_limit=0)
            except click.ClickException as exc:
                live.update(Panel(f"[red]{exc.message}[/red]", title="MissionOS Operate"))
                time.sleep(max(0.2, poll_interval))
                continue
            artifacts = _task_artifacts(task_payload)
            snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            status = _task_status(task_payload)
            proposal = _agent_proposal_from_task(task_payload)
            live.update(
                Group(
                    _render_recovery_agent_console(
                        task_payload,
                        proposal=proposal,
                        show_proposal=bool(proposal) and status == "running",
                        status=status,
                        task_id=task_id,
                    ),
                    _render_operate_status_line(snapshot, status=status, task_id=task_id),
                )
            )
            if status in TERMINAL_TASK_STATUSES:
                break
            time.sleep(max(0.2, poll_interval))


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
    help="Seconds between telemetry polls.",
)
@click.pass_context
def operate_command(
    ctx: click.Context,
    task_id: str,
    poll_interval: float,
) -> None:
    """Recovery-agent operator console (viewer). 承認は `missionos rtl`/`land`。終了は Ctrl-C."""
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    resolved_task_id = _resolve_live_task_id(
        client,
        explicit_task_id=task_id,
        stored_task_id=_stored_sitl_task_id(ctx),
    )
    try:
        _operate_live(client, resolved_task_id, poll_interval=poll_interval)
    except KeyboardInterrupt:
        pass
    console.print("[yellow](operate を終了しました)[/yellow]")


def _operator_recovery_command(
    ctx: click.Context, *, task_id: str, action: str, assume_yes: bool
) -> None:
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    resolved_task_id = _resolve_operator_recovery_task_id(
        client,
        explicit_task_id=task_id,
        stored_task_id=_stored_sitl_task_id(ctx),
    )
    label = _OPERATOR_RECOVERY_ACTIONS.get(action, action)
    if not assume_yes and not click.confirm(
        f"{label} を task {resolved_task_id} に送信しますか？", default=False
    ):
        console.print("[yellow]中止しました（dispatch していません）。[/yellow]")
        return
    payload = client.recovery_dispatch(task_id=resolved_task_id, recovery_action=action)
    if ctx.obj["missionos_json_output"]:
        _print_json(payload)
        return
    task_payload = _wait_for_active_runner_recovery_observation(client, payload)
    _print_recovery_result(payload, task_payload=task_payload)


@missionos.command("rtl")
@click.option("--task-id", default="", help="対象 task。省略時は running を自動検出。")
@click.option("--yes", is_flag=True, help="y/N 確認をスキップして送信。")
@click.pass_context
def rtl_command(ctx: click.Context, task_id: str, yes: bool) -> None:
    """承認済み RTL（発射地点へ帰還）を dispatch（標準の y/N 確認あり）."""
    _operator_recovery_command(ctx, task_id=task_id, action="return_to_launch", assume_yes=yes)


@missionos.command("land")
@click.option("--task-id", default="", help="対象 task。省略時は running を自動検出。")
@click.option("--yes", is_flag=True, help="y/N 確認をスキップして送信。")
@click.pass_context
def land_command(ctx: click.Context, task_id: str, yes: bool) -> None:
    """承認済み LAND（その場着陸）を dispatch（標準の y/N 確認あり）."""
    _operator_recovery_command(ctx, task_id=task_id, action="land", assume_yes=yes)


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
        )
        _remember_mission_designer_context(ctx, payload, session_id=session_id)
        _print_conversation_result(payload)
        return None

    return _action


def _tutorial_resolve_task_id(ctx: click.Context) -> str:
    task_id = _stored_sitl_task_id(ctx)
    if not task_id:
        raise click.ClickException(
            "準備済みの SITL task id がありません。先に run ステップが task を返している必要があります。"
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
        console.print(
            "[yellow]Execute Live SITL HTTP read timed out; showing latest task state.[/yellow]"
        )
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
    """The ordered Fuji-delivery walkthrough, mirroring the GUI operator flow."""
    return [
        TutorialStep(
            key="status",
            title="現状を読む",
            explanation=(
                "MissionOS の operator surface（Gateway / Plan / Review / Execution / "
                "Repair）を読みます。まだ何も起動しません。"
            ),
            command="missionos status",
            boundary="読むだけ。PX4/Gazebo も dispatch authority も発生しない。",
            action=_tutorial_status,
        ),
        TutorialStep(
            key="plan",
            title="計画する (say)",
            explanation=(
                "自然文で計画を依頼します。座標は同梱の富士山ルート（route.yaml と同値）を "
                "渡します。Gateway が source-bound な Mission Designer context を作り、CLI は "
                "その参照を state に保存します。"
            ),
            command=(
                "missionos say --route-hint mission_designer_plan "
                "--coordinate-route-file docs/mission_os/fuji_delivery_route.yaml "
                '"富士山デリバリーを計画して"'
            ),
            boundary="計画を作るだけ。承認も実行もしない。",
            action=_tutorial_plan,
        ),
        TutorialStep(
            key="approve",
            title="承認する (approve)",
            explanation=(
                "operator として計画を承認します。GUI の承認ボタンと同じ会話ルートで、"
                "ポリシーゲートは Gateway 側で効いたままです。"
            ),
            command="missionos approve",
            boundary="承認 intent を送るだけ。ゲートはバイパスしない。",
            action=_tutorial_intent("approve"),
        ),
        TutorialStep(
            key="run",
            title="bounded action を準備する (run)",
            explanation=(
                "承認済みの bounded action を実行ゲート越しに準備します。SITL 実行 task が "
                "返れば CLI が task_id を state に保存し、後続コマンドが自動補完します。"
            ),
            command="missionos run",
            boundary="実行ゲートを通すが、まだシミュレータは起動しない。",
            action=_tutorial_intent("run"),
        ),
        TutorialStep(
            key="start-sitl",
            title="SITL を起動する (start-sitl)",
            explanation=(
                "GUI と同じ PX4/Gazebo SITL 起動境界です。ここで実シミュレータの readiness が "
                "立ち上がります（task_id は state から自動補完）。"
            ),
            command="missionos start-sitl",
            boundary="ここから実プロセス（PX4/Gazebo）が動き始める。",
            action=_tutorial_start_sitl,
        ),
        TutorialStep(
            key="execute-sitl",
            title="ライブ実行する (execute-sitl)",
            explanation=(
                "GUI の Execute Live SITL と同じ境界です。explicit execution approval と "
                "live_flight_mode=true を送ります。これは本物のゲートなので明示確認します。"
            ),
            command="missionos execute-sitl --live-flight",
            boundary=(
                "ライブ実行。delivery_completion_claimed / physical_delivery_verified は "
                "false のまま（CLI に true 化する経路はない）。"
            ),
            action=_tutorial_execute_sitl,
            live=True,
        ),
    ]


def _print_tutorial_step(index: int, total: int, step: TutorialStep) -> None:
    body = (
        f"{step.explanation}\n\n"
        f"[dim]手で打つなら:[/dim]\n  [green]{step.command}[/green]\n\n"
        f"[dim]境界:[/dim] {step.boundary}"
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
            "GUI と同じ富士山デリバリーを、CLI のコマンドを1つずつ学びながら通します。\n"
            "各ステップで『手で打つなら何を打つか』と『どの境界を通るか』を示します。\n"
            "[dim]Enter=実行 / s=スキップ / q=終了。ライブ SITL 実行だけは 'yes' を要求します。[/dim]",
            title="MissionOS CLI チュートリアル（富士山デリバリー）",
            border_style="magenta",
        )
    )
    for index, step in enumerate(steps, 1):
        _print_tutorial_step(index, len(steps), step)
        if step.live:
            if interactive:
                answer = ask("[bold red]ライブ実行します。実行するなら 'yes' と入力 > [/bold red]")
                if answer.strip().lower() != "yes":
                    console.print(
                        "[yellow]ライブ実行はスキップしました。"
                        "実行するなら上記コマンドを手動で実行してください。[/yellow]"
                    )
                    break
            elif not allow_live:
                console.print(
                    "[yellow]ライブ実行は --yes 未指定のためスキップしました。"
                    "通し実行するなら `missionos tutorial --auto --yes`。[/yellow]"
                )
                break
        elif interactive:
            decision = ask("[cyan]Enter=実行 / s=スキップ / q=終了 > [/cyan]").strip().lower()
            if decision in {"q", "quit"}:
                console.print("[yellow]チュートリアルを終了しました。[/yellow]")
                return
            if decision in {"s", "skip"}:
                console.print("[dim](このステップはスキップしました)[/dim]")
                continue
        try:
            if step.live:
                console.print(
                    "[bold red]ライブ実行を開始しました。[/bold red]"
                    "PX4/Gazebo の AUTO mission は数分から十数分かかることがあります。"
                    "完了または失敗の結果 Panel が出るまで待ってください。"
                )
                with console.status(
                    "[red]Execute Live SITL 実行中… Gateway 応答を待っています[/red]",
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
                        "AUTO mission はまだ running です。\n"
                        "`missionos job-status` を再実行すると現在位置・距離・battery を追跡できます。\n"
                        "完了または blocked になるまで delivery_completion_claimed は false のままです。",
                        title="ライブ実行継続中",
                        border_style="yellow",
                    )
                )
                return
            if step.live and outcome in {"blocked", "failed", "cancelled", "canceled"}:
                console.print(
                    Panel(
                        "Execute Live SITL は完了せず停止しました。\n"
                        "`missionos job-status` で最新状態と artifact_root を確認してください。",
                        title="ライブ実行停止",
                        border_style="red",
                    )
                )
                return
        except click.ClickException as exc:
            console.print(f"[red]{exc.message}[/red]")
            console.print("[yellow]このステップで止まりました。状況を直してから再開してください。[/yellow]")
            return
    console.print(
        Panel(
            "完了。各ステップの『手で打つなら』が、そのまま実運用の CLI です。\n"
            "個別に `missionos <sub>` として使えます（例: `missionos status`）。\n"
            "別ミッションを始める前に `missionos clear-state` で state を消去してください。",
            title="チュートリアル終了",
            border_style="green",
        )
    )


@missionos.command("tutorial")
@click.option("--session-id", default=DEFAULT_TUTORIAL_SESSION_ID, show_default=True)
@click.option(
    "--auto",
    is_flag=True,
    help="一時停止せず各ステップを自動実行する通し再生（学習デモ用）。",
)
@click.option(
    "--yes",
    "allow_live",
    is_flag=True,
    help="--auto 時にライブ SITL 実行ステップも自動で行う（既定はライブ手前で停止）。",
)
@click.option(
    "--autostart/--no-autostart",
    default=False,
    show_default=True,
    help="Gateway が未起動なら自動で立ち上げ、終了時に停止する。",
)
@click.option(
    "--enable-live-sitl/--planning-only",
    default=False,
    show_default=True,
    help="--autostart で起動する Gateway に live SITL/dispatch opt-in env を入れる。",
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
    """Guided, step-by-step walkthrough that teaches the MissionOS CLI."""
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
            console.print("[blue]自動起動した Gateway を停止します…[/blue]")
            _terminate_gateway(gateway_proc)


CHAT_HELP_LINES = (
    "Type a MissionOS instruction, or a slash command.",
    "You can also start here with: missionos chat \"東京駅から川崎駅へ...\"",
    "  /status                      — show operator surfaces",
    "  /approve /reject /revision   — operator review intents",
    "  /run /repair                 — execution and repair intents",
    "  /start-sitl [task_id]        — GUI-equivalent SITL startup",
    "  /execute-sitl [task_id]      — GUI-equivalent Execute Live SITL",
    "                                interactive chat opens operate/watch/map companion terminals",
    "  /job-status [task_id]        — show stored/running task status",
    "  /land <task_id>              — operator-approved LAND dispatch",
    "  /rtl <task_id>               — operator-approved RTL dispatch",
    "  /help /clear /quit",
    "Flow: press Enter to accept the suggested next action; type only to change course.",
    "Editing: ↑/↓ history, Ctrl+R search, Tab completes /commands,",
    "         Esc then Enter inserts a newline, Enter submits, Ctrl+D quits.",
)


def _chat_help_panel() -> Panel:
    return Panel(
        Text("\n".join(CHAT_HELP_LINES)),
        title="MissionOS CLI",
        border_style="cyan",
    )


def _set_chat_suggestion(ctx: click.Context, *, raw: str, label: str) -> None:
    ctx.obj["missionos_chat_suggestion"] = {"raw": raw, "label": label}


def _clear_chat_suggestion(ctx: click.Context) -> None:
    ctx.obj.pop("missionos_chat_suggestion", None)


def _chat_suggestion(ctx: click.Context) -> dict[str, str]:
    suggestion = ctx.obj.get("missionos_chat_suggestion")
    if not isinstance(suggestion, dict):
        return {}
    raw = str(suggestion.get("raw") or "").strip()
    label = str(suggestion.get("label") or "").strip()
    if not raw or not label:
        return {}
    return {"raw": raw, "label": label}


def _chat_prompt_fragment(ctx: click.Context) -> HTML:
    suggestion = _chat_suggestion(ctx)
    if suggestion:
        return HTML(
            "\n<ansigreen><b>MissionOS</b></ansigreen> "
            f"<ansiyellow>[Enter={suggestion['label']}]</ansiyellow>"
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
    executable = str(argv0.resolve()) if argv0.exists() else "missionos"
    parts = [executable]
    gateway_url = str(ctx.obj.get("missionos_gateway_url") or "").strip()
    if gateway_url:
        parts.extend(["--gateway-url", gateway_url])
    state_path = ctx.obj.get("missionos_state_path")
    if state_path:
        parts.extend(["--state-path", str(state_path)])
    return " ".join(shlex.quote(part) for part in parts)


def _chat_companion_terminal_script(
    *,
    title: str,
    command: str,
    stop_path: Path,
    cwd: Path,
    hold_after_command: bool,
) -> str:
    hold = "1" if hold_after_command else "0"
    return f"""#!/bin/sh
set +e
cd {shlex.quote(str(cwd))}
STOP_PATH={shlex.quote(str(stop_path))}
TITLE={shlex.quote(title)}
HOLD_AFTER_COMMAND={hold}
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
    command_prefix = _missionos_chat_companion_command_prefix(ctx)
    commands = {
        "operate": f"{command_prefix} operate --task-id {shlex.quote(task_id)}",
        "watch": f"{command_prefix} watch --task-id {shlex.quote(task_id)}",
        "map": f"{command_prefix} map --task-id {shlex.quote(task_id)}",
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


def _looks_like_mission_planning_request(raw: str) -> bool:
    text = str(raw or "").lower()
    if any(marker in text for marker in ("->", "→", "⇒")):
        return True
    if re.search(r"\S+\s*から\s*\S+\s*まで", text):
        return True
    if re.search(r"\bfrom\s+.+\bto\s+.+", text):
        return True
    return False


def _update_chat_suggestion_from_conversation(
    ctx: click.Context, payload: dict[str, Any]
) -> None:
    action = str(payload.get("routed_action") or "")
    repair_prompt = payload.get("missionos_repair_prompt")
    if isinstance(repair_prompt, dict) and repair_prompt.get("suggested_command") == "/repair":
        _set_chat_suggestion(ctx, raw="/repair", label="修復")
        return
    if action == "mission_designer_plan":
        _set_chat_suggestion(ctx, raw="/approve", label="承認")
    elif action == "approve":
        _set_chat_suggestion(ctx, raw="/run", label="準備")
    elif action == "execute" and _stored_sitl_task_id(ctx):
        _set_chat_suggestion(ctx, raw="/start-sitl", label="起動")
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
    raw = raw.strip()
    if not raw:
        suggestion = _chat_suggestion(ctx)
        if not suggestion:
            return True
        raw = suggestion["raw"]
        console.print(f"[dim]Enter -> {suggestion['label']}[/dim]")
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
    if not raw.startswith("/"):
        stored_task_id = _stored_sitl_task_id(ctx)
        lower = raw.lower()
        if any(token in raw for token in ("準備", "SITL準備", "実行リクエスト")):
            raw = "/run"
        elif any(token in raw for token in ("起動", "立ち上げ")):
            raw = "/start-sitl"
        elif (
            stored_task_id
            and (
                any(token in raw for token in ("飛ばして", "飛ばす", "ライブ実行", "飛行開始"))
                or any(token in lower for token in ("fly", "launch", "execute live"))
            )
        ):
            raw = "/execute-sitl"
        elif stored_task_id and any(token in raw for token in ("状況", "進捗", "どうなって")):
            raw = "/job-status"
    if raw in {"/quit", "/exit", "exit", "quit", "q"}:
        return False
    if raw == "/help":
        console.print(_chat_help_panel())
        return True
    if raw == "/clear":
        console.clear()
        return True
    try:
        if raw == "/status":
            ctx.invoke(status_command)
            return True
        if raw.startswith("/land ") or raw.startswith("/rtl "):
            parts = shlex.split(raw)
            if len(parts) != 2:
                console.print("[yellow]Usage: /land <task_id> or /rtl <task_id>[/yellow]")
                return True
            action = "land" if parts[0] == "/land" else "return_to_launch"
            with console.status("[cyan]dispatching recovery…[/cyan]", spinner="dots"):
                payload = client.recovery_dispatch(task_id=parts[1], recovery_action=action)
                task_payload = _wait_for_active_runner_recovery_observation(client, payload)
            _print_recovery_result(payload, task_payload=task_payload)
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
                console.print(
                    "[yellow]Execute Live SITL HTTP read timed out; showing latest task state.[/yellow]"
                )
                _print_job_status(task_payload, timeline_payload)
                latest_task = task_payload.get("task")
                latest_status = (
                    str(latest_task.get("status") or "").strip().lower()
                    if isinstance(latest_task, dict)
                    else ""
                )
                followup = (
                    "必要なら最終状態を確認できます。見るなら「状況を見せて」と入力してください。"
                    if latest_status in TERMINAL_TASK_STATUSES
                    else "AUTO mission はまだ継続中です。進捗を見るなら「状況を見せて」と入力してください。"
                )
                _print_chat_followup(followup)
                _set_chat_suggestion(ctx, raw=f"/job-status {latest_task_id}", label="状況を見る")
                return True
            latest_task_id = _remember_sitl_task_id_from_payload(
                ctx,
                payload,
                fallback_task_id=task_id,
            )
            _print_sitl_execution_result(payload)
            _print_chat_followup(
                "必要なら最終状態を確認できます。見るなら「状況を見せて」と入力してください。"
            )
            _set_chat_suggestion(ctx, raw=f"/job-status {latest_task_id}", label="状況を見る")
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
            with console.status("[blue]starting SITL…[/blue]", spinner="dots"):
                payload = client.start_sitl(task_id=task_id)
            latest_task_id = _remember_sitl_task_id_from_payload(
                ctx,
                payload,
                fallback_task_id=task_id,
            )
            _print_sitl_start_result(payload)
            _print_chat_followup(
                "SITL は ready です。ライブ実行しますか？飛ばすなら「飛ばして」と入力してください。"
            )
            _set_chat_suggestion(ctx, raw=f"/execute-sitl {latest_task_id}", label="飛ばす")
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
                _set_chat_suggestion(ctx, raw="/job-status", label="更新")
            else:
                _clear_chat_suggestion(ctx)
            return True
        if raw.startswith("/"):
            intent = raw[1:]
            if intent in INTENT_INSTRUCTIONS:
                with console.status(f"[cyan]MissionOS: {intent}…[/cyan]", spinner="dots"):
                    payload = client.conversation(
                        INTENT_INSTRUCTIONS[intent],
                        session_id=session_id,
                        mission_designer_context=_stored_mission_designer_context(
                            ctx, session_id
                        ),
                        client_surface="chat",
                )
                _remember_mission_designer_context(ctx, payload, session_id=session_id)
                _print_conversation_result(payload)
                _update_chat_suggestion_from_conversation(ctx, payload)
                return True
            console.print(
                "[yellow]Unknown command. Type /help for the slash-command list.[/yellow]"
            )
            return True
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
        )
        _remember_mission_designer_context(ctx, payload, session_id=session_id)
        _print_conversation_result(payload)
        _update_chat_suggestion_from_conversation(ctx, payload)
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


@missionos.command("chat")
@click.argument("initial_instruction", nargs=-1, required=False)
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
    help="Gateway が未起動なら自動で立ち上げ、chat 終了時に停止する。",
)
@click.option(
    "--enable-live-sitl/--planning-only",
    default=False,
    show_default=True,
    help="--autostart で起動する Gateway に live SITL/dispatch opt-in env を入れる。",
)
@click.option(
    "--companion-terminals/--no-companion-terminals",
    default=True,
    show_default=True,
    help=(
        "対話 chat で live flight を始める時に operate/watch/map の別 Terminal を開き、"
        "chat 終了時に閉じる。"
    ),
)
@click.pass_context
def chat_command(
    ctx: click.Context,
    initial_instruction: tuple[str, ...],
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
    client: MissionOSGatewayClient = ctx.obj["missionos_client"]
    ctx.obj["missionos_chat_session_id"] = session_id
    ctx.obj["missionos_chat_companion_terminals_enabled"] = (
        companion_terminals and sys.stdin.isatty()
    )
    gateway_proc = _ensure_gateway(
        client,
        ctx.obj["missionos_gateway_url"],
        autostart=autostart,
        enable_live_sitl=enable_live_sitl,
    )
    console.print(_chat_help_panel())
    session = _build_chat_session(history_path)
    try:
        if initial_raw:
            console.print(f"[bold green]MissionOS>[/bold green] {initial_raw}")
            if not _handle_chat_input(ctx, client, initial_raw, session_id=session_id):
                return
        while True:
            try:
                raw = session.prompt(_chat_prompt_fragment(ctx))
            except KeyboardInterrupt:
                console.print("[yellow](Ctrl+C — type /quit or Ctrl+D to exit)[/yellow]")
                continue
            except EOFError:
                break
            if not _handle_chat_input(ctx, client, raw, session_id=session_id):
                break
    finally:
        _stop_chat_companion_terminals(ctx)
        if gateway_proc is not None:
            console.print("[blue]自動起動した Gateway を停止します…[/blue]")
            _terminate_gateway(gateway_proc)

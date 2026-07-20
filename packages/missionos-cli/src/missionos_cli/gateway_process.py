"""Gateway environment and managed-process ownership contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

from .gateway_client import _gateway_host_port


GATEWAY_PID_RECORD_SCHEMA_VERSION = "missionos_gateway_pidfile.v1"

_GATEWAY_LIVE_SITL_ENV = {
    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION": "1",
    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_LIVE_FLIGHT": "1",
    "RUN_MISSIONOS_SITL_DISPATCH_RUNTIME": "1",
    "RUN_MISSIONOS_AUTO_MISSION_GUI_DISPATCH": "1",
    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER": "1",
    "MISSION_DESIGNER_PX4_GAZEBO_SITL_DOCKER_CONTAINER": (
        "missionos-px4-gazebo-sitl-mission-upload-smoke"
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
        (env.get("MISSIONOS_LLM_BACKEND") or env.get("BOILED_CLAW_LLM_BACKEND") or "gemini")
        .strip()
        .lower()
    )
    if backend in {"google", "google_adk"}:
        return "gemini"
    return backend


def _llm_backend_uses_google_credentials(env: dict[str, str]) -> bool:
    return _llm_backend_from_env(env) == "gemini"


def _llm_backend_uses_deepseek_credentials(env: dict[str, str]) -> bool:
    return _llm_backend_from_env(env) == "deepseek"


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
    if not _llm_backend_uses_deepseek_credentials(env):
        env.pop("DEEPSEEK_API_KEY", None)


def _gateway_process_env(*, enable_live_sitl: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in _dotenv_process_values().items():
        env.setdefault(key, value)
    _apply_gateway_llm_env(env)
    if enable_live_sitl:
        env["MISSIONOS_GATEWAY_BACKEND"] = "production"
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
    return [
        sys.executable,
        "-m",
        "missionos_gateway",
        "web",
        "--host",
        host,
        "--port",
        str(port),
    ]


def _gateway_command_signature(base_url: str) -> str:
    argv = _gateway_argv(base_url)
    return " ".join(shlex.quote(part) for part in argv)


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
        "backend": "production" if enable_live_sitl else "fixture",
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
    if not command or "-m missionos_gateway web" not in command:
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

"""MissionOS bridge to the existing PX4/Gazebo horizontal route smoke."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any, Mapping
import uuid

from src.runtime.runtime_claim_evidence import RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION


MISSIONOS_SITL_DISPATCH_RUNTIME_OPT_IN_ENV = "RUN_MISSIONOS_SITL_DISPATCH_RUNTIME"
MISSIONOS_SITL_DISPATCH_RUNTIME_TIMEOUT_ENV = "MISSIONOS_SITL_DISPATCH_RUNTIME_TIMEOUT_SECONDS"
MISSIONOS_SITL_DISPATCH_RUNTIME_COMMAND_ENV = "MISSIONOS_SITL_DISPATCH_RUNTIME_COMMAND"
MISSIONOS_ALLOW_RUNTIME_COMMAND_OVERRIDE_ENV = "MISSIONOS_ALLOW_RUNTIME_COMMAND_OVERRIDE"
PX4_GAZEBO_HORIZONTAL_ROUTE_OPT_IN_ENV = "RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE"
PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT_ENV = "PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT"
MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND_ENV = "MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND"
WIND_PREEMPTIVE_OFFSET_M_ENV = "MISSION_DESIGNER_REALISM_WIND_PREEMPTIVE_OFFSET_M"
WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV = (
    "MISSION_DESIGNER_REALISM_WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG"
)
WIND_COMPENSATED_ROUTE_ENV = "MISSION_DESIGNER_REALISM_WIND_COMPENSATED_ROUTE"
WIND_COMPENSATION_SOURCE_RESPONSE_ENV = (
    "MISSION_DESIGNER_REALISM_WIND_COMPENSATION_SOURCE_RESPONSE"
)
WIND_FEED_FORWARD_MPS_ENV = "MISSION_DESIGNER_REALISM_WIND_FEED_FORWARD_MPS"
WIND_FEED_FORWARD_RAMP_START_FRACTION_ENV = (
    "MISSION_DESIGNER_REALISM_WIND_FEED_FORWARD_RAMP_START_FRACTION"
)
WIND_FEED_FORWARD_RAMP_END_FRACTION_ENV = (
    "MISSION_DESIGNER_REALISM_WIND_FEED_FORWARD_RAMP_END_FRACTION"
)
WIND_COMPENSATION_METHOD_ENV = "MISSION_DESIGNER_REALISM_WIND_COMPENSATION_METHOD"
DEFAULT_RUNTIME_TIMEOUT_SECONDS = 900
REPO_ROOT = Path(__file__).resolve().parents[2]
FORM2A_WIND_COMPENSATION_RESPONSE_KINDS = frozenset(
    {
        "operator_gated_wind_replan_with_compensation",
        "operator_gated_wind_compensated_reroute",
    }
)
DEFAULT_FORM2A_WIND_PREEMPTIVE_OFFSET_M = 0.5
DEFAULT_FORM2A_WIND_FEED_FORWARD_MPS = 0.5
DEFAULT_FORM2A_WIND_FEED_FORWARD_RAMP_START_FRACTION = 0.65
DEFAULT_FORM2A_WIND_FEED_FORWARD_RAMP_END_FRACTION = 0.9


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_last_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    decoder = json.JSONDecoder()
    latest: dict[str, Any] = {}
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            latest = value
    return latest


def _latest_smoke_summary(smoke_root: Path) -> tuple[dict[str, Any], str]:
    summaries = sorted(
        smoke_root.rglob("summary.json"),
        key=lambda path: (path.stat().st_mtime, path.as_posix()),
        reverse=True,
    )
    for path in summaries:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload, str(path)
    return {}, ""


def _default_command() -> list[str]:
    return [
        sys.executable,
        str(REPO_ROOT / "scripts" / "smoke_px4_gazebo_horizontal_route_delivery.py"),
    ]


def _runtime_command() -> list[str]:
    override = os.getenv(MISSIONOS_SITL_DISPATCH_RUNTIME_COMMAND_ENV)
    if override:
        return shlex.split(override)
    return _default_command()


def _runtime_timeout_seconds() -> int:
    try:
        return int(os.getenv(MISSIONOS_SITL_DISPATCH_RUNTIME_TIMEOUT_ENV, ""))
    except ValueError:
        return DEFAULT_RUNTIME_TIMEOUT_SECONDS


def _optional_float_text(parameters: Mapping[str, Any], key: str) -> str:
    value = parameters.get(key)
    if value is None:
        return ""
    try:
        return str(float(value))
    except (TypeError, ValueError):
        return ""


def _env_or_default(key: str, default: str) -> str:
    return os.getenv(key, default)


def form2a_backend_action_smoke_env(
    backend_action: str,
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Map a bounded MissionOS Form 2a response and parameters into smoke inputs."""

    selected = str(backend_action or "").strip()
    if not selected:
        return {}
    parameters = parameters or {}
    env = {MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND_ENV: selected}
    if selected not in FORM2A_WIND_COMPENSATION_RESPONSE_KINDS:
        return env
    feed_forward_mps = _optional_float_text(parameters, "feed_forward_mps")
    preemptive_offset_m = _optional_float_text(parameters, "preemptive_offset_m")
    direction_deg = _optional_float_text(parameters, "direction_deg")
    inferred_method = (
        "mid_route_velocity_feed_forward"
        if feed_forward_mps
        else "static_target_offset"
        if preemptive_offset_m
        else "mid_route_velocity_feed_forward"
    )
    env.update(
        {
            WIND_COMPENSATED_ROUTE_ENV: "1",
            WIND_COMPENSATION_METHOD_ENV: inferred_method
            if (feed_forward_mps or preemptive_offset_m)
            else _env_or_default(WIND_COMPENSATION_METHOD_ENV, inferred_method),
            WIND_FEED_FORWARD_MPS_ENV: feed_forward_mps
            or _env_or_default(
                WIND_FEED_FORWARD_MPS_ENV,
                str(DEFAULT_FORM2A_WIND_FEED_FORWARD_MPS),
            ),
            WIND_FEED_FORWARD_RAMP_START_FRACTION_ENV: _env_or_default(
                WIND_FEED_FORWARD_RAMP_START_FRACTION_ENV,
                str(DEFAULT_FORM2A_WIND_FEED_FORWARD_RAMP_START_FRACTION),
            ),
            WIND_FEED_FORWARD_RAMP_END_FRACTION_ENV: _env_or_default(
                WIND_FEED_FORWARD_RAMP_END_FRACTION_ENV,
                str(DEFAULT_FORM2A_WIND_FEED_FORWARD_RAMP_END_FRACTION),
            ),
            WIND_PREEMPTIVE_OFFSET_M_ENV: preemptive_offset_m
            or _env_or_default(
                WIND_PREEMPTIVE_OFFSET_M_ENV,
                "0.0",
            ),
            WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV: direction_deg
            or _env_or_default(
                WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV,
                os.getenv("MISSION_DESIGNER_REALISM_WIND_DIRECTION_DEG", "90.0"),
            ),
            WIND_COMPENSATION_SOURCE_RESPONSE_ENV: selected,
        }
    )
    return env


def _docker_container_id(container_name: str) -> str:
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.Id}}", container_name],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def invoke_missionos_sitl_dispatch_runtime(
    *,
    artifact_root: Path,
    backend_action: str = "operator_gated_recovery_dispatch",
    backend_action_parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke the real smoke runtime and return evidence plus parsed summary."""

    if os.getenv(MISSIONOS_SITL_DISPATCH_RUNTIME_OPT_IN_ENV) != "1":
        return {
            "runtime_invoked": False,
            "blocked_reason": f"{MISSIONOS_SITL_DISPATCH_RUNTIME_OPT_IN_ENV}_not_enabled",
        }
    if (
        os.getenv(MISSIONOS_SITL_DISPATCH_RUNTIME_COMMAND_ENV)
        and os.getenv(MISSIONOS_ALLOW_RUNTIME_COMMAND_OVERRIDE_ENV) != "1"
    ):
        return {
            "runtime_invoked": False,
            "blocked_reason": f"{MISSIONOS_SITL_DISPATCH_RUNTIME_COMMAND_ENV}_requires_{MISSIONOS_ALLOW_RUNTIME_COMMAND_OVERRIDE_ENV}",
        }

    artifact_root.mkdir(parents=True, exist_ok=True)
    run_dir = artifact_root / "missionos_sitl_dispatch_runtime" / (
        f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    command = _runtime_command()
    smoke_root = run_dir / "smoke"
    smoke_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env[PX4_GAZEBO_HORIZONTAL_ROUTE_OPT_IN_ENV] = "1"
    env[PX4_GAZEBO_HORIZONTAL_ROUTE_ARTIFACT_ROOT_ENV] = str(smoke_root)
    env["MISSIONOS_SITL_BACKEND_ACTION"] = backend_action
    form2a_env = form2a_backend_action_smoke_env(
        backend_action,
        backend_action_parameters,
    )
    env.update(form2a_env)
    env["PYTHONPATH"] = env.get("PYTHONPATH") or "."

    started_at = _utc_now()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )
    try:
        stdout, stderr = process.communicate(timeout=_runtime_timeout_seconds())
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=30)
        stderr = f"{stderr}\nmissionos runtime subprocess timeout"
    completed_at = _utc_now()
    exit_code = int(process.returncode if process.returncode is not None else -1)
    stdout_path = run_dir / "missionos_sitl_dispatch_runtime_stdout.txt"
    stderr_path = run_dir / "missionos_sitl_dispatch_runtime_stderr.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    parsed_summary, summary_source_path = _latest_smoke_summary(smoke_root)
    summary_source = "smoke_summary_json"
    override_command = os.getenv(MISSIONOS_SITL_DISPATCH_RUNTIME_COMMAND_ENV)
    if not parsed_summary:
        summary_source = "missing_smoke_summary_json"
        summary_source_path = ""
        if (
            override_command
            and os.getenv(MISSIONOS_ALLOW_RUNTIME_COMMAND_OVERRIDE_ENV) == "1"
        ):
            parsed_summary = _parse_last_json_object(stdout)
            summary_source = "stdout_json"
            summary_source_path = str(stdout_path) if parsed_summary else ""
    runtime_summary_path = run_dir / "missionos_sitl_dispatch_runtime_summary.json"
    runtime_summary_path.write_text(
        json.dumps(parsed_summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    command_argv_json = json.dumps(command, sort_keys=True)
    container_name = "boiled-claw-px4-gazebo-horizontal-route-smoke"
    evidence = {
        "schema_version": RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION,
        "missionos_schema_version": "missionos_runtime_invocation_evidence.v1",
        "invocation_kind": "subprocess",
        "invocation_target": "scripts/smoke_px4_gazebo_horizontal_route_delivery.py",
        "invocation_started_at": started_at,
        "invocation_completed_at": completed_at,
        "invocation_stdout_sha256": _sha256_text(stdout),
        "invocation_stderr_sha256": _sha256_text(stderr),
        "invocation_exit_code": exit_code,
        "process_pid": process.pid,
        "command_argv_sha256": _sha256_text(command_argv_json),
        "command_argv": command,
        "docker_container_name": container_name,
        "docker_container_id": _docker_container_id(container_name),
        "opt_in_env": True,
        "backend_target": "px4_gazebo_sitl",
        "backend_action": backend_action,
        "form2a_backend_action_parameters": dict(backend_action_parameters or {}),
        "form2a_smoke_env": form2a_env,
        "artifact_dir": str(run_dir),
        "started_at": started_at,
        "completed_at": completed_at,
        "stdout_sha256": _sha256_text(stdout),
        "stderr_sha256": _sha256_text(stderr),
        "exit_code": exit_code,
        "stdout_artifact_path": str(stdout_path),
        "stderr_artifact_path": str(stderr_path),
        "runtime_summary_artifact_path": str(runtime_summary_path),
        "runtime_summary_source": summary_source,
        "runtime_summary_source_path": summary_source_path,
    }
    return {
        "runtime_invoked": True,
        "backend_action": backend_action,
        "runtime_invocation_evidence": evidence,
        "runtime_stdout_tail": stdout[-4000:],
        "runtime_stderr_tail": stderr[-4000:],
        "runtime_summary": parsed_summary,
        "runtime_summary_artifact_path": str(runtime_summary_path),
        "runtime_summary_source": summary_source,
        "runtime_summary_source_path": summary_source_path,
        "runtime_artifact_root": str(smoke_root),
    }


def runtime_summary_supports_dispatch(summary: dict[str, Any]) -> bool:
    """Return true when the smoke summary proves actual PX4/Gazebo dispatch."""

    return bool(
        summary.get("actual_px4_gazebo_horizontal_smoke_observed") is True
        and int(summary.get("setpoint_frames_sent") or 0) > 0
        and summary.get("bounded_setpoint_stream_allowed") is True
        and summary.get("unbounded_setpoint_stream_allowed") is False
        and summary.get("physical_execution_invoked") is False
        and summary.get("hardware_target_allowed") is False
    )


__all__ = [
    "MISSIONOS_SITL_DISPATCH_RUNTIME_COMMAND_ENV",
    "MISSIONOS_ALLOW_RUNTIME_COMMAND_OVERRIDE_ENV",
    "MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND_ENV",
    "MISSIONOS_SITL_DISPATCH_RUNTIME_OPT_IN_ENV",
    "MISSIONOS_SITL_DISPATCH_RUNTIME_TIMEOUT_ENV",
    "WIND_COMPENSATED_ROUTE_ENV",
    "WIND_COMPENSATION_SOURCE_RESPONSE_ENV",
    "WIND_COMPENSATION_METHOD_ENV",
    "WIND_FEED_FORWARD_MPS_ENV",
    "WIND_FEED_FORWARD_RAMP_END_FRACTION_ENV",
    "WIND_FEED_FORWARD_RAMP_START_FRACTION_ENV",
    "WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV",
    "WIND_PREEMPTIVE_OFFSET_M_ENV",
    "form2a_backend_action_smoke_env",
    "invoke_missionos_sitl_dispatch_runtime",
    "runtime_summary_supports_dispatch",
]

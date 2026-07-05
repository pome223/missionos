"""Process runner for TurtleBot3/Nav2 simulator smokes.

This runner wraps the existing Docker smoke as an opt-in process boundary and
turns stdout into a structured, read-only MissionOS result. It does not start
ROS2/Gazebo by default and it never creates dispatch authority by itself.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


TURTLEBOT3_SIM_PROCESS_RUN_SCHEMA_VERSION = (
    "missionos_turtlebot3_sim_process_run.v1"
)
TURTLEBOT3_SIM_PROCESS_RUNNER_ENABLE_ENV = (
    "RUN_MISSIONOS_TURTLEBOT3_SIM_PROCESS_RUNNER"
)
TURTLEBOT3_SIM_PROCESS_RUNNER_COMMAND_ENV = (
    "MISSIONOS_TURTLEBOT3_SIM_PROCESS_RUNNER_COMMAND"
)
TURTLEBOT3_SIM_PROCESS_RUNNER_SCENARIO_ENV = (
    "MISSIONOS_TURTLEBOT3_SIM_PROCESS_RUNNER_SCENARIO"
)

TurtleBot3SimProcessScenario = Literal[
    "obstacle_delivery",
    "mid_recovery",
    "localization_drift_fault",
    "recovery_guardrail_fallback",
]
TurtleBot3SimProcessExitStatus = Literal[
    "not_attempted",
    "completed",
    "failed",
    "timed_out",
]

_TRUE_VALUES = {"1", "true", "yes", "on"}


class TurtleBot3SimProcessRunnerError(RuntimeError):
    """Raised when a TurtleBot3 sim process run cannot be evaluated safely."""


@dataclass(frozen=True)
class TurtleBot3ProcessCommandResult:
    exit_code: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False


CommandRunner = Callable[
    [tuple[str, ...], Mapping[str, str], float, Path],
    TurtleBot3ProcessCommandResult,
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def _stable_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _as_tuple(values: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


def turtlebot3_sim_process_run_ref(run: "TurtleBot3SimProcessRun") -> str:
    return f"turtlebot3_sim_process_run:{run.process_run_id}"


def _default_command(repo_root: Path) -> tuple[str, ...]:
    raw_command = os.environ.get(TURTLEBOT3_SIM_PROCESS_RUNNER_COMMAND_ENV, "").strip()
    if raw_command:
        return tuple(shlex.split(raw_command))
    return (
        str(repo_root / "scripts" / "smoke_ros2_nav2_turtlebot3_obstacle_delivery_docker.sh"),
    )


def _scenario_env(scenario: TurtleBot3SimProcessScenario) -> dict[str, str]:
    env: dict[str, str] = {}
    if scenario == "obstacle_delivery":
        env["MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE"] = "0"
        env["MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_FAULT_SMOKE"] = "0"
    elif scenario == "mid_recovery":
        env["MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE"] = "1"
        env["MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_FAULT_SMOKE"] = "0"
    elif scenario == "localization_drift_fault":
        env["MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE"] = "1"
        env["MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_FAULT_SMOKE"] = "1"
    else:
        env["MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE"] = "0"
        env["MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_FAULT_SMOKE"] = "0"
        env["MISSIONOS_CHAT_TURTLEBOT3_DYNAMIC_OBSTACLE_RECOVERY_SMOKE"] = "1"
        env["MISSIONOS_CHAT_TURTLEBOT3_RECOVERY_GUARDRAIL_FALLBACK_SMOKE"] = "1"
    return env


def _command_invokes_docker_lifecycle(command: tuple[str, ...]) -> bool:
    command_text = " ".join(command)
    return bool(command) and (
        Path(command[0]).name == "docker"
        or "smoke_ros2_nav2_turtlebot3_obstacle_delivery_docker.sh" in command_text
    )


def _default_runner(
    command: tuple[str, ...],
    env: Mapping[str, str],
    timeout_s: float,
    cwd: Path,
) -> TurtleBot3ProcessCommandResult:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=dict(env),
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return TurtleBot3ProcessCommandResult(
            exit_code=124,
            stdout=str(exc.stdout or ""),
            stderr=str(exc.stderr or ""),
            timed_out=True,
        )
    return TurtleBot3ProcessCommandResult(
        exit_code=int(completed.returncode),
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _extract_smoke_result(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    index = 0
    while index < len(stdout):
        start = stdout.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(stdout[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict) and value.get("smoke") == (
            "missionos_chat_turtlebot3_home_mission"
        ):
            return value
        index = start + max(end, 1)
    return {}


def _nested(payload: Mapping[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _evaluate_scenario(
    *,
    scenario: TurtleBot3SimProcessScenario,
    exit_code: int | None,
    parsed_result: Mapping[str, Any],
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    assertions: list[str] = []
    blocked: list[str] = []
    if exit_code != 0:
        blocked.append("process_exit_nonzero")
    if not parsed_result:
        blocked.append("smoke_result_json_missing")
        return False, tuple(assertions), _as_tuple(blocked)
    if parsed_result.get("physical_execution_invoked") is not False:
        blocked.append("physical_execution_claimed")
    if parsed_result.get("mission_delivery_completion_claimed") is not False:
        blocked.append("mission_delivery_completion_claimed")

    if scenario == "obstacle_delivery":
        assertions.extend(
            (
                "normal_delivery_completed",
                "normal_delivery_sim_action_claimed",
                "physical_execution_not_invoked",
            )
        )
        if parsed_result.get("status") != "completed":
            blocked.append("normal_delivery_not_completed")
        if parsed_result.get("completion_claimed") is not True:
            blocked.append("normal_delivery_completion_not_claimed")
        if parsed_result.get("completion_scope") != "sim_action":
            blocked.append("normal_delivery_completion_scope_not_sim_action")
    elif scenario == "mid_recovery":
        mid = parsed_result.get("mid_mission_recovery")
        mid = mid if isinstance(mid, Mapping) else {}
        assertions.extend(
            (
                "mid_recovery_triggered",
                "return_home_recovery_completed",
                "delivery_completion_not_claimed",
                "physical_execution_not_invoked",
            )
        )
        if mid.get("status") != "recovered":
            blocked.append("mid_recovery_not_recovered")
        if mid.get("runtime_recovery_triggered") is not True:
            blocked.append("mid_recovery_trigger_not_observed")
        if mid.get("recovery_dispatch_request_sent") is not True:
            blocked.append("mid_recovery_return_home_not_dispatched")
        if mid.get("recovery_completion_claimed") is not True:
            blocked.append("mid_recovery_return_home_not_completed")
        if mid.get("completion_claimed") is not False:
            blocked.append("mid_recovery_claimed_delivery_completion")
        if mid.get("physical_execution_invoked") is not False:
            blocked.append("mid_recovery_claimed_physical_execution")
    elif scenario == "localization_drift_fault":
        mid = parsed_result.get("mid_mission_recovery")
        mid = mid if isinstance(mid, Mapping) else {}
        assertions.extend(
            (
                "localization_drift_fault_blocks_or_recovers",
                "failure_recovery_convened",
                "motion_delta_supplied",
                "fault_diagnostics_ready",
                "recovery_completion_bounded",
                "physical_execution_not_invoked",
            )
        )
        if parsed_result.get("localization_drift_fault_injection_enabled") is not True:
            blocked.append("localization_drift_fault_not_enabled")
        if mid.get("status") not in {"blocked", "recovered"}:
            blocked.append("localization_drift_fault_not_blocked_or_recovered")
        if mid.get("runtime_recovery_triggered") is not True:
            blocked.append("localization_drift_fault_recovery_not_triggered")
        if mid.get("runtime_failure_recovery_triggered") is not True:
            blocked.append("localization_drift_fault_failure_recovery_not_recorded")
        if not isinstance(mid.get("recovery_proposal_count"), int) or mid.get(
            "recovery_proposal_count"
        ) < 1:
            blocked.append("localization_drift_fault_recovery_proposal_missing")
        motion = mid.get("runtime_recovery_motion_context")
        motion = motion if isinstance(motion, Mapping) else {}
        if not motion:
            blocked.append("localization_drift_fault_motion_context_missing")
        if "odom_delta_m" not in motion:
            blocked.append("localization_drift_fault_motion_delta_missing")
        if mid.get("recovery_approval_created_count") not in {0, None}:
            blocked.append("localization_drift_fault_recovery_approval_created")
        if mid.get("completion_claimed") is not False:
            blocked.append("localization_drift_fault_claimed_completion")
        if (
            mid.get("status") == "blocked"
            and mid.get("recovery_completion_claimed") is not False
        ):
            blocked.append("localization_drift_fault_claimed_recovery_completion")
        if (
            mid.get("status") == "recovered"
            and mid.get("recovery_completion_claimed") is not True
        ):
            blocked.append("localization_drift_fault_recovery_completion_missing")
        if mid.get("mission_delivery_completion_claimed") is not False:
            blocked.append("localization_drift_fault_claimed_delivery_completion")
        if mid.get("physical_execution_invoked") is not False:
            blocked.append("localization_drift_fault_claimed_physical_execution")
        if mid.get("nav2_log_diagnostics_status") != "ready":
            blocked.append("localization_drift_fault_diagnostics_not_ready")
    else:
        dynamic = parsed_result.get("dynamic_obstacle_recovery")
        dynamic = dynamic if isinstance(dynamic, Mapping) else {}
        assertions.extend(
            (
                "llm_output_guardrail_blocked",
                "deterministic_fallback_used",
                "fallback_recovery_completed",
                "physical_execution_not_invoked",
            )
        )
        if (
            parsed_result.get("recovery_guardrail_fallback_injection_enabled")
            is not True
        ):
            blocked.append("recovery_guardrail_fallback_not_enabled")
        if dynamic.get("status") not in {"completed", "recovered"}:
            blocked.append("dynamic_obstacle_fallback_not_completed_or_recovered")
        if dynamic.get("recovery_planner_status") != "guardrail_blocked":
            blocked.append("llm_output_not_guardrail_blocked")
        if dynamic.get("recovery_proposal_source") != "deterministic_fallback":
            blocked.append("deterministic_fallback_not_used")
        if dynamic.get("runtime_recovery_action_kind") != "avoid_obstacle":
            blocked.append("fallback_recovery_action_not_avoid_obstacle")
        if dynamic.get("route_resumed_after_recovery") is not True:
            blocked.append("fallback_recovery_route_not_resumed")
        if dynamic.get("recovery_dispatch_request_sent") is not True:
            blocked.append("fallback_recovery_not_dispatched")
        if dynamic.get("recovery_completion_claimed") is not True:
            blocked.append("fallback_recovery_not_completed")
        if dynamic.get("route_completed_after_recovery") is True:
            assertions += ("route_completed_after_fallback_recovery",)
            if dynamic.get("completion_claimed") is not True:
                blocked.append("fallback_route_completed_without_completion_claim")
        elif dynamic.get("completion_claimed") is not False:
            blocked.append("fallback_route_blocked_but_completion_claimed")
        if dynamic.get("physical_execution_invoked") is not False:
            blocked.append("fallback_recovery_claimed_physical_execution")
    return not blocked, tuple(assertions), _as_tuple(blocked)


class TurtleBot3SimProcessRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["missionos_turtlebot3_sim_process_run.v1"] = (
        TURTLEBOT3_SIM_PROCESS_RUN_SCHEMA_VERSION
    )
    process_run_id: str
    scenario: TurtleBot3SimProcessScenario
    process_launch_attempted: bool
    docker_lifecycle_invoked: bool
    command: tuple[str, ...] = ()
    started_at: datetime = Field(default_factory=_utc_now)
    stopped_at: datetime | None = None
    exit_status: TurtleBot3SimProcessExitStatus
    exit_code: int | None = None
    timeout_s: float = Field(gt=0)
    parsed_result: dict[str, Any] = Field(default_factory=dict)
    parsed_status: str | None = None
    parsed_completion_claimed: bool | None = None
    parsed_completion_scope: str | None = None
    parsed_mid_recovery_status: str | None = None
    parsed_fault_injection_enabled: bool | None = None
    scenario_passed: bool
    scenario_assertions: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    stdout_ref: str
    stderr_ref: str
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    stdout_sha256: str
    stderr_sha256: str
    simulation_only: Literal[True] = True
    command_payload_allowed: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    mission_delivery_completion_claimed: Literal[False] = False
    run_hash: str

    @model_validator(mode="after")
    def _validate_run(self) -> "TurtleBot3SimProcessRun":
        if self.process_launch_attempted and not self.command:
            raise TurtleBot3SimProcessRunnerError(
                "attempted TurtleBot3 sim process run requires command"
            )
        if self.docker_lifecycle_invoked and not self.process_launch_attempted:
            raise TurtleBot3SimProcessRunnerError(
                "Docker lifecycle cannot be invoked without process launch"
            )
        if self.exit_status == "not_attempted" and self.process_launch_attempted:
            raise TurtleBot3SimProcessRunnerError(
                "attempted TurtleBot3 sim process run cannot be not_attempted"
            )
        if self.scenario_passed and self.blocked_reasons:
            raise TurtleBot3SimProcessRunnerError(
                "passed TurtleBot3 sim process run cannot include blocked reasons"
            )
        expected_hash = _stable_hash(
            {
                "scenario": self.scenario,
                "command": self.command,
                "exit_status": self.exit_status,
                "exit_code": self.exit_code,
                "scenario_passed": self.scenario_passed,
                "blocked_reasons": self.blocked_reasons,
                "stdout_sha256": self.stdout_sha256,
                "stderr_sha256": self.stderr_sha256,
            }
        )
        if self.run_hash != expected_hash:
            raise TurtleBot3SimProcessRunnerError(
                "TurtleBot3 sim process run hash mismatch"
            )
        return self


def run_turtlebot3_sim_process_scenario(
    *,
    scenario: TurtleBot3SimProcessScenario = "obstacle_delivery",
    repo_root: str | Path = ".",
    command: tuple[str, ...] | None = None,
    timeout_s: float = 900.0,
    opt_in: bool | None = None,
    runner: CommandRunner | None = None,
) -> TurtleBot3SimProcessRun:
    root = Path(repo_root).resolve()
    command_tuple = tuple(command or _default_command(root))
    started_at = _utc_now()
    explicit_opt_in = (
        _truthy(os.environ.get(TURTLEBOT3_SIM_PROCESS_RUNNER_ENABLE_ENV))
        if opt_in is None
        else opt_in
    )
    if not explicit_opt_in:
        stdout_sha = sha256(b"").hexdigest()
        stderr_sha = sha256(b"").hexdigest()
        blocked = ("RUN_MISSIONOS_TURTLEBOT3_SIM_PROCESS_RUNNER_not_enabled",)
        run_hash = _stable_hash(
            {
                "scenario": scenario,
                "command": (),
                "exit_status": "not_attempted",
                "exit_code": None,
                "scenario_passed": False,
                "blocked_reasons": blocked,
                "stdout_sha256": stdout_sha,
                "stderr_sha256": stderr_sha,
            }
        )
        return TurtleBot3SimProcessRun(
            process_run_id=f"turtlebot3_sim_process_run_{run_hash[:12]}",
            scenario=scenario,
            process_launch_attempted=False,
            docker_lifecycle_invoked=False,
            command=(),
            started_at=started_at,
            stopped_at=started_at,
            exit_status="not_attempted",
            exit_code=None,
            timeout_s=timeout_s,
            scenario_passed=False,
            blocked_reasons=blocked,
            stdout_ref=f"turtlebot3_sim_process_stdout:{stdout_sha[:16]}",
            stderr_ref=f"turtlebot3_sim_process_stderr:{stderr_sha[:16]}",
            stdout_sha256=stdout_sha,
            stderr_sha256=stderr_sha,
            run_hash=run_hash,
        )

    env = os.environ.copy()
    env.update(_scenario_env(scenario))
    command_runner = runner or _default_runner
    result = command_runner(command_tuple, env, timeout_s, root)
    stopped_at = _utc_now()
    parsed = _extract_smoke_result(result.stdout)
    scenario_passed, assertions, blocked_reasons = _evaluate_scenario(
        scenario=scenario,
        exit_code=result.exit_code,
        parsed_result=parsed,
    )
    stdout_sha = sha256(result.stdout.encode("utf-8")).hexdigest()
    stderr_sha = sha256(result.stderr.encode("utf-8")).hexdigest()
    exit_status: TurtleBot3SimProcessExitStatus = (
        "timed_out"
        if result.timed_out
        else "completed"
        if result.exit_code == 0
        else "failed"
    )
    run_hash = _stable_hash(
        {
            "scenario": scenario,
            "command": command_tuple,
            "exit_status": exit_status,
            "exit_code": result.exit_code,
            "scenario_passed": scenario_passed,
            "blocked_reasons": blocked_reasons,
            "stdout_sha256": stdout_sha,
            "stderr_sha256": stderr_sha,
        }
    )
    return TurtleBot3SimProcessRun(
        process_run_id=f"turtlebot3_sim_process_run_{run_hash[:12]}",
        scenario=scenario,
        process_launch_attempted=True,
        docker_lifecycle_invoked=_command_invokes_docker_lifecycle(command_tuple),
        command=command_tuple,
        started_at=started_at,
        stopped_at=stopped_at,
        exit_status=exit_status,
        exit_code=result.exit_code,
        timeout_s=timeout_s,
        parsed_result=dict(parsed),
        parsed_status=str(parsed.get("status")) if parsed.get("status") else None,
        parsed_completion_claimed=parsed.get("completion_claimed")
        if isinstance(parsed.get("completion_claimed"), bool)
        else None,
        parsed_completion_scope=str(parsed.get("completion_scope"))
        if parsed.get("completion_scope")
        else None,
        parsed_mid_recovery_status=str(_nested(parsed, "mid_mission_recovery", "status"))
        if _nested(parsed, "mid_mission_recovery", "status")
        else None,
        parsed_fault_injection_enabled=parsed.get(
            "localization_drift_fault_injection_enabled"
        )
        if isinstance(
            parsed.get("localization_drift_fault_injection_enabled"),
            bool,
        )
        else None,
        scenario_passed=scenario_passed,
        scenario_assertions=assertions,
        blocked_reasons=blocked_reasons,
        stdout_ref=f"turtlebot3_sim_process_stdout:{stdout_sha[:16]}",
        stderr_ref=f"turtlebot3_sim_process_stderr:{stderr_sha[:16]}",
        stdout_excerpt=result.stdout[:4000],
        stderr_excerpt=result.stderr[:4000],
        stdout_sha256=stdout_sha,
        stderr_sha256=stderr_sha,
        run_hash=run_hash,
    )


__all__ = [
    "TURTLEBOT3_SIM_PROCESS_RUNNER_COMMAND_ENV",
    "TURTLEBOT3_SIM_PROCESS_RUNNER_ENABLE_ENV",
    "TURTLEBOT3_SIM_PROCESS_RUNNER_SCENARIO_ENV",
    "TURTLEBOT3_SIM_PROCESS_RUN_SCHEMA_VERSION",
    "TurtleBot3ProcessCommandResult",
    "TurtleBot3SimProcessRun",
    "TurtleBot3SimProcessRunnerError",
    "run_turtlebot3_sim_process_scenario",
    "turtlebot3_sim_process_run_ref",
]

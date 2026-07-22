"""Command-bridge client for bounded Unitree MuJoCo dispatch.

MissionOS does not implement Unitree low-level motor control here. This bridge
executes an operator-provided command with a bounded JSON request and expects a
bounded JSON receipt. The command can be a future Unitree SDK2/MuJoCo adapter
owned outside the core MissionOS safety contract.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from src.runtime.unitree_hardware_adapter import UnitreeBoundedLocalMove


UNITREE_MUJOCO_BRIDGE_COMMAND_ENV = "UNITREE_MUJOCO_BRIDGE_COMMAND"
UNITREE_MUJOCO_BRIDGE_TIMEOUT_ENV = "UNITREE_MUJOCO_BRIDGE_TIMEOUT_S"
UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV = (
    "RUN_MISSIONOS_UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE"
)
UNITREE_MUJOCO_SPORT_CLIENT_BRIDGE_ENV = (
    "RUN_MISSIONOS_UNITREE_MUJOCO_SPORT_CLIENT_BRIDGE"
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


class UnitreeMujocoBridgeError(RuntimeError):
    """Raised when the external bridge command fails its bounded contract."""


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _bridge_command() -> tuple[str, ...]:
    command = os.environ.get(UNITREE_MUJOCO_BRIDGE_COMMAND_ENV, "").strip()
    if not command:
        raise UnitreeMujocoBridgeError("UNITREE_MUJOCO_BRIDGE_COMMAND is not set")
    return tuple(shlex.split(command))


def _bridge_timeout_s(default: float) -> float:
    raw = os.environ.get(UNITREE_MUJOCO_BRIDGE_TIMEOUT_ENV, "").strip()
    if not raw:
        return default
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise UnitreeMujocoBridgeError(
            "UNITREE_MUJOCO_BRIDGE_TIMEOUT_S must be a number"
        ) from exc
    if timeout <= 0:
        raise UnitreeMujocoBridgeError(
            "UNITREE_MUJOCO_BRIDGE_TIMEOUT_S must be positive"
        )
    return timeout


def _run_bridge(
    *,
    action: str,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 5.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _truthy_env(UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV):
        raise UnitreeMujocoBridgeError(
            "RUN_MISSIONOS_UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE is not enabled"
        )
    request = {
        "schema_version": "missionos_unitree_mujoco_bridge_request.v1",
        "action": action,
        "payload": payload or {},
        "physical_execution_invoked": False,
        "raw_motor_allowed": False,
        "raw_velocity_allowed": False,
        "special_motion_allowed": False,
    }
    started_at = datetime.now(timezone.utc)
    try:
        completed = subprocess.run(
            _bridge_command(),
            input=json.dumps(request, ensure_ascii=True, sort_keys=True),
            capture_output=True,
            text=True,
            timeout=_bridge_timeout_s(timeout_s),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UnitreeMujocoBridgeError(
            f"Unitree MuJoCo bridge timed out after {exc.timeout} seconds"
        ) from exc
    if completed.returncode != 0:
        raise UnitreeMujocoBridgeError(
            f"Unitree MuJoCo bridge exited {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    completed_at = datetime.now(timezone.utc)
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UnitreeMujocoBridgeError("Unitree MuJoCo bridge returned non-JSON") from exc
    if not isinstance(response, dict):
        raise UnitreeMujocoBridgeError("Unitree MuJoCo bridge returned non-object JSON")
    if response.get("physical_execution_invoked") is True:
        raise UnitreeMujocoBridgeError(
            "Unitree MuJoCo bridge may not claim physical execution"
        )
    forbidden_true_fields = (
        "raw_lowcmd_published",
        "raw_motor_invoked",
        "raw_velocity_invoked",
        "special_motion_invoked",
    )
    for field in forbidden_true_fields:
        if response.get(field) is True:
            raise UnitreeMujocoBridgeError(
                f"Unitree MuJoCo bridge may not claim {field}"
            )
    stdout = completed.stdout
    stderr = completed.stderr
    invocation = {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_kind": "subprocess",
        "invocation_target": f"unitree_mujoco_bridge_command:{action}",
        "invocation_started_at": started_at.isoformat(),
        "invocation_completed_at": completed_at.isoformat(),
        "invocation_stdout_sha256": sha256(stdout.encode("utf-8")).hexdigest(),
        "invocation_stderr_sha256": sha256(stderr.encode("utf-8")).hexdigest(),
        "invocation_stdout_preimage": stdout,
        "invocation_stderr_preimage": stderr,
        "invocation_exit_code": completed.returncode,
        "bridge_command_sha256": sha256(
            json.dumps(_bridge_command(), sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "physical_execution_invoked": False,
    }
    return response, invocation


class UnitreeMujocoBridgeCommandClient:
    """UnitreeSimClient implementation backed by an operator-provided command."""

    def __init__(self) -> None:
        self._responses: list[dict[str, Any]] = []
        self._runtime_invocations: list[dict[str, Any]] = []

    def _invoke(
        self,
        *,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response, invocation = _run_bridge(action=action, payload=payload)
        self._responses.append({"action": action, **response})
        self._runtime_invocations.append(invocation)
        return response

    def send_bounded_local_move(self, move: UnitreeBoundedLocalMove) -> dict[str, Any]:
        action = "bounded_local_move"
        return self._invoke(
            action=action,
            payload=move.model_dump(mode="json"),
        )

    def hold(self) -> dict[str, Any]:
        action = "hold"
        return self._invoke(action=action)

    def safe_stop(self) -> dict[str, Any]:
        action = "safe_stop"
        return self._invoke(action=action)

    def read_state(self) -> dict[str, Any]:
        action = "read_state"
        return self._invoke(action=action)

    def read_progress(self) -> dict[str, Any]:
        action = "read_progress"
        return self._invoke(action=action)

    def collect_responses(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(response) for response in self._responses)

    def collect_runtime_invocation_evidence(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._runtime_invocations)


__all__ = [
    "UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV",
    "UNITREE_MUJOCO_BRIDGE_COMMAND_ENV",
    "UNITREE_MUJOCO_BRIDGE_TIMEOUT_ENV",
    "UNITREE_MUJOCO_SPORT_CLIENT_BRIDGE_ENV",
    "UnitreeMujocoBridgeCommandClient",
    "UnitreeMujocoBridgeError",
]

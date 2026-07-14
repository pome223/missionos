"""Command bridge for bounded ROS2/Nav2 simulator dispatch.

MissionOS keeps ROS2 out of the core adapter contract. This bridge client sends
one bounded JSON request to an operator-provided command and rejects receipts
that claim physical execution or raw ROS velocity/topic publication.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
import shlex
import subprocess
from typing import Any

from src.runtime.ros2_nav2_hardware_adapter import Nav2GoalPose


ROS2_NAV2_BRIDGE_COMMAND_ENV = "ROS2_NAV2_BRIDGE_COMMAND"
ROS2_NAV2_BRIDGE_TIMEOUT_ENV = "ROS2_NAV2_BRIDGE_TIMEOUT_S"
ROS2_NAV2_RECOVERY_EVALUATION_TIMEOUT_ENV = (
    "ROS2_NAV2_RECOVERY_EVALUATION_TIMEOUT_S"
)
ROS2_NAV2_REQUEST_SIM_FAULT_CANCEL_AFTER_ACCEPT_ENV = (
    "MISSIONOS_ROS2_NAV2_REQUEST_SIM_FAULT_CANCEL_AFTER_ACCEPT"
)
ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV = (
    "RUN_MISSIONOS_ROS2_NAV2_BOUNDED_DISPATCH_SMOKE"
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


class Ros2Nav2BridgeError(RuntimeError):
    """Raised when the external ROS2/Nav2 bridge fails its bounded contract."""


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _bridge_command() -> tuple[str, ...]:
    command = os.environ.get(ROS2_NAV2_BRIDGE_COMMAND_ENV, "").strip()
    if not command:
        raise Ros2Nav2BridgeError("ROS2_NAV2_BRIDGE_COMMAND is not set")
    return tuple(shlex.split(command))


def _bridge_timeout_s(default: float) -> float:
    raw = os.environ.get(ROS2_NAV2_BRIDGE_TIMEOUT_ENV, "").strip()
    if not raw:
        return default
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise Ros2Nav2BridgeError(
            "ROS2_NAV2_BRIDGE_TIMEOUT_S must be a number"
        ) from exc
    if timeout <= 0:
        raise Ros2Nav2BridgeError("ROS2_NAV2_BRIDGE_TIMEOUT_S must be positive")
    return timeout


def _recovery_evaluation_timeout_s() -> float:
    raw = os.environ.get(ROS2_NAV2_RECOVERY_EVALUATION_TIMEOUT_ENV, "").strip()
    if not raw:
        return 90.0
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise Ros2Nav2BridgeError(
            "ROS2_NAV2_RECOVERY_EVALUATION_TIMEOUT_S must be a number"
        ) from exc
    if timeout <= 0:
        raise Ros2Nav2BridgeError(
            "ROS2_NAV2_RECOVERY_EVALUATION_TIMEOUT_S must be positive"
        )
    return timeout


def _reject_forbidden_claims(response: Mapping[str, Any]) -> None:
    forbidden_true_fields = (
        "physical_execution_invoked",
        "raw_velocity_invoked",
        "raw_velocity_published",
        "raw_ros_topic_published",
        "cmd_vel_published_by_missionos",
    )
    for field in forbidden_true_fields:
        if response.get(field) is True:
            raise Ros2Nav2BridgeError(f"ROS2/Nav2 bridge may not claim {field}")


def _run_bridge(
    *,
    action: str,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 30.0,
    env_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not _truthy_env(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV):
        raise Ros2Nav2BridgeError(
            "RUN_MISSIONOS_ROS2_NAV2_BOUNDED_DISPATCH_SMOKE is not enabled"
        )
    request = {
        "schema_version": "missionos_ros2_nav2_bridge_request.v1",
        "action": action,
        "payload": payload or {},
        "execution_mode": "sim",
        "physical_execution_invoked": False,
        "raw_velocity_allowed": False,
        "raw_ros_topic_publication_allowed": False,
    }
    try:
        env = os.environ.copy()
        if env_overrides:
            env.update({str(key): str(value) for key, value in env_overrides.items()})
        completed = subprocess.run(
            _bridge_command(),
            input=json.dumps(request, ensure_ascii=True, sort_keys=True),
            capture_output=True,
            text=True,
            timeout=_bridge_timeout_s(timeout_s),
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise Ros2Nav2BridgeError(
            f"ROS2/Nav2 bridge timed out after {exc.timeout} seconds"
        ) from exc
    if completed.returncode != 0:
        raise Ros2Nav2BridgeError(
            f"ROS2/Nav2 bridge exited {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise Ros2Nav2BridgeError("ROS2/Nav2 bridge returned non-JSON") from exc
    if not isinstance(response, dict):
        raise Ros2Nav2BridgeError("ROS2/Nav2 bridge returned non-object JSON")
    _reject_forbidden_claims(response)
    return response


class Ros2Nav2BridgeCommandClient:
    """Nav2DispatchClient backed by an operator-provided command."""

    def __init__(self, *, env_overrides: Mapping[str, str] | None = None) -> None:
        self._responses: list[dict[str, Any]] = []
        self._last_dispatch_response: dict[str, Any] | None = None
        self._env_overrides = dict(env_overrides or {})

    def _record_response(self, action: str, response: dict[str, Any]) -> dict[str, Any]:
        self._responses.append({"action": action, **response})
        return response

    def send_goal_pose(self, goal: Nav2GoalPose) -> dict[str, Any]:
        action = "send_goal_pose"
        payload = goal.model_dump(mode="json")
        if str(
            self._env_overrides.get(
                ROS2_NAV2_REQUEST_SIM_FAULT_CANCEL_AFTER_ACCEPT_ENV,
                "",
            )
        ).strip().lower() in _TRUE_VALUES:
            payload["sim_fault_cancel_after_accept"] = True
        response = self._record_response(
            action,
            _run_bridge(
                action=action,
                payload=payload,
                timeout_s=45.0,
                env_overrides=self._env_overrides,
            ),
        )
        self._last_dispatch_response = response
        return response

    def evaluate_recovery_candidates(
        self,
        *,
        candidates: list[dict[str, Any]],
        obstacle: Mapping[str, Any],
        frame_id: str = "map",
    ) -> dict[str, Any]:
        """Evaluate recovery goals through a read-only Nav2 planning action."""

        response = _run_bridge(
            action="evaluate_recovery_candidates",
            payload={
                "candidates": [dict(item) for item in candidates],
                "obstacle": dict(obstacle),
                "frame_id": frame_id,
            },
            timeout_s=_recovery_evaluation_timeout_s(),
            env_overrides=self._env_overrides,
        )
        if (
            response.get("dispatch_request_sent") is not False
            or response.get("dispatch_authority_created") is not False
            or response.get("command_ack_observed") is not False
        ):
            raise Ros2Nav2BridgeError(
                "plan-only recovery evaluation returned authority-bearing evidence"
            )
        return self._record_response("evaluate_recovery_candidates", response)

    def cancel_goal(self) -> dict[str, Any]:
        action = "cancel_goal"
        response = self._record_response(
            action,
            _run_bridge(action=action, env_overrides=self._env_overrides),
        )
        self._last_dispatch_response = response
        return response

    def read_state(self) -> dict[str, Any]:
        if self._last_dispatch_response:
            state_result = self._last_dispatch_response.get("state_result")
            if isinstance(state_result, Mapping):
                return dict(state_result)
        action = "read_state"
        return self._record_response(
            action,
            _run_bridge(action=action, env_overrides=self._env_overrides),
        )

    def read_progress(self) -> dict[str, Any]:
        if self._last_dispatch_response:
            progress_result = self._last_dispatch_response.get("progress_result")
            if isinstance(progress_result, Mapping):
                return dict(progress_result)
        action = "read_progress"
        return self._record_response(
            action,
            _run_bridge(action=action, env_overrides=self._env_overrides),
        )

    def collect_responses(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(response) for response in self._responses)


__all__ = [
    "ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV",
    "ROS2_NAV2_BRIDGE_COMMAND_ENV",
    "ROS2_NAV2_BRIDGE_TIMEOUT_ENV",
    "ROS2_NAV2_REQUEST_SIM_FAULT_CANCEL_AFTER_ACCEPT_ENV",
    "Ros2Nav2BridgeCommandClient",
    "Ros2Nav2BridgeError",
]

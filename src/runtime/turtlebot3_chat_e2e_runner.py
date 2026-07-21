"""Maintained MissionOS chat -> TurtleBot3 production-boundary E2E runner.

The legacy ``scripts/smoke_missionos_chat_turtlebot3_home_mission.py`` path is
now a thin compatibility entrypoint.  This module owns Gateway process
lifecycle, real loopback HTTP calls, scenario selection, and evidence
assertions.  It does not bypass the production approval or dispatch routes.

Default mode verifies that plan/approve/run reaches the production Gateway and
blocks before ROS2 dispatch when no bridge is opted in. Set
``RUN_MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_SMOKE_WITH_BRIDGE=1`` and provide
the ROS2/Nav2 bridge environment to exercise the simulator boundary.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any
from urllib import error, request

from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
)


WITH_BRIDGE_ENV = "RUN_MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_SMOKE_WITH_BRIDGE"
MAP_MODEL_OUT_ENV = "MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_MAP_MODEL_OUT"
INSTRUCTION_ENV = "MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_INSTRUCTION"
RECOVERY_INSTRUCTION_ENV = "MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_RECOVERY_INSTRUCTION"
HTTP_TIMEOUT_ENV = "MISSIONOS_CHAT_TURTLEBOT3_HOME_MISSION_HTTP_TIMEOUT_SECONDS"
MID_RECOVERY_SMOKE_ENV = "MISSIONOS_CHAT_TURTLEBOT3_MID_RECOVERY_SMOKE"
EXPECTED_RECOVERY_PROPOSAL_SOURCE_ENV = (
    "MISSIONOS_EXPECT_TURTLEBOT3_RECOVERY_PROPOSAL_SOURCE"
)
DYNAMIC_OBSTACLE_RECOVERY_SMOKE_ENV = (
    "MISSIONOS_CHAT_TURTLEBOT3_DYNAMIC_OBSTACLE_RECOVERY_SMOKE"
)
DECISION_DEMO_SMOKE_ENV = "MISSIONOS_CHAT_TURTLEBOT3_DECISION_DEMO_SMOKE"
HUMAN_APPROVAL_DEMO_SMOKE_ENV = (
    "MISSIONOS_CHAT_TURTLEBOT3_HUMAN_APPROVAL_DEMO_SMOKE"
)
RECOVERY_REQUIRES_APPROVAL_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL"
)
RECOVERY_GUARDRAIL_FALLBACK_SMOKE_ENV = (
    "MISSIONOS_CHAT_TURTLEBOT3_RECOVERY_GUARDRAIL_FALLBACK_SMOKE"
)
LOCALIZATION_DRIFT_FAULT_SMOKE_ENV = (
    "MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_FAULT_SMOKE"
)
LOCALIZATION_DRIFT_INITIALPOSE_X_ENV = (
    "MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_INITIALPOSE_X_M"
)
LOCALIZATION_DRIFT_INITIALPOSE_Y_ENV = (
    "MISSIONOS_CHAT_TURTLEBOT3_LOCALIZATION_DRIFT_INITIALPOSE_Y_M"
)
_TRUE_VALUES = {"1", "true", "yes", "on"}
_ADK_ENV_KEYS = (
    "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED",
    "MISSIONOS_CHIEF_ROUTE_SEMANTIC_ADK_ENABLED",
    "MISSIONOS_LLM_DIALOGUE_ROUTER_ADK_ENABLED",
    "MISSIONOS_LLM_REPAIR_PLANNER_ADK_ENABLED",
    "MISSIONOS_LLM_RESPONSE_PLANNER_ADK_ENABLED",
)
_LLM_OFF_VALUES = {"off", "none", "disabled", "deterministic"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _http_timeout_seconds() -> float:
    raw_value = os.environ.get(HTTP_TIMEOUT_ENV)
    if raw_value is None:
        return 180.0
    try:
        timeout = float(raw_value)
    except ValueError:
        return 180.0
    return timeout if timeout > 0 else 180.0


def _mid_recovery_smoke_enabled() -> bool:
    raw_value = os.environ.get(MID_RECOVERY_SMOKE_ENV)
    if raw_value is None:
        return True
    return raw_value.strip().lower() in _TRUE_VALUES


def _localization_drift_fault_smoke_enabled() -> bool:
    return _truthy_env(LOCALIZATION_DRIFT_FAULT_SMOKE_ENV)


def _dynamic_obstacle_recovery_smoke_enabled() -> bool:
    return (
        _truthy_env(DYNAMIC_OBSTACLE_RECOVERY_SMOKE_ENV)
        or _truthy_env(DECISION_DEMO_SMOKE_ENV)
    )


def _decision_demo_smoke_enabled() -> bool:
    return _truthy_env(DECISION_DEMO_SMOKE_ENV)


def _recovery_guardrail_fallback_smoke_enabled() -> bool:
    return _truthy_env(RECOVERY_GUARDRAIL_FALLBACK_SMOKE_ENV)


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gateway_env(
    *,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env["MISSIONOS_GATEWAY_BACKEND"] = "production"
    env.setdefault("MISSIONOS_LLM_BACKEND", "gemini")
    backend = env.get("MISSIONOS_LLM_BACKEND", "").strip().lower()
    default_adk_enabled = "0" if backend in _LLM_OFF_VALUES else "1"
    for key in _ADK_ENV_KEYS:
        env.setdefault(key, default_adk_enabled)
    if not _truthy_env(WITH_BRIDGE_ENV):
        env.pop(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV, None)
        env.pop(ROS2_NAV2_BRIDGE_COMMAND_ENV, None)
    if env_overrides:
        env.update(env_overrides)
    return env


def _localization_drift_env_overrides() -> dict[str, str]:
    return {
        "ROS2_NAV2_INITIALPOSE_ENABLE": "1",
        "ROS2_NAV2_INITIALPOSE_X_M": os.environ.get(
            LOCALIZATION_DRIFT_INITIALPOSE_X_ENV,
            "8.0",
        ),
        "ROS2_NAV2_INITIALPOSE_Y_M": os.environ.get(
            LOCALIZATION_DRIFT_INITIALPOSE_Y_ENV,
            "8.0",
        ),
    }


def _start_gateway(
    *,
    env_overrides: dict[str, str] | None = None,
) -> tuple[str, subprocess.Popen[bytes]]:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "missionos_gateway",
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_gateway_env(env_overrides=env_overrides),
    )
    _wait_for_gateway(base_url, proc)
    return base_url, proc


def _stop_gateway(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _wait_for_gateway(base_url: str, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            gateway_output = ""
            if proc.stdout is not None:
                gateway_output = proc.stdout.read().decode(
                    "utf-8",
                    errors="replace",
                )
            raise RuntimeError(
                "Gateway exited before health was reachable"
                f"\n{gateway_output[-4000:]}"
            )
        try:
            with request.urlopen(f"{base_url}/health", timeout=1.0) as response:
                if response.status == 200:
                    return
        except (OSError, error.URLError):
            pass
        time.sleep(0.25)
    raise RuntimeError("Timed out waiting for Gateway health")


def _post_json(url: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(http_request, timeout=timeout_s) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Gateway returned HTTP {response.status}")
        data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Gateway returned non-object JSON")
        return data


def _post_conversation(
    *,
    base_url: str,
    instruction: str,
    session_id: str,
    context: dict[str, Any] | None = None,
    route_hint: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operator_instruction": instruction,
        "missionos_client_surface": "chat",
        "session_id": session_id,
    }
    if context:
        payload["mission_designer_context"] = context
    if route_hint:
        payload["missionos_route_hint"] = route_hint
    return _post_json(
        f"{base_url}/missionos/autonomy-conversation/run",
        payload,
        timeout_s=_http_timeout_seconds(),
    )


def _home_distance(summary: dict[str, Any], key: str) -> Any:
    envelope = summary.get("home_distance_envelope")
    if not isinstance(envelope, dict):
        return None
    return envelope.get(key)


def _first_recovery_proposal_source(proposals: Any) -> str | None:
    if not isinstance(proposals, list) or not proposals:
        return None
    first = proposals[0]
    if not isinstance(first, dict):
        return None
    source = first.get("proposal_source")
    return str(source) if source is not None else None


def _proposal_count(proposals: Any, *, source: str | None = None) -> int:
    if not isinstance(proposals, list):
        return 0
    count = 0
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        if source is not None and proposal.get("proposal_source") != source:
            continue
        count += 1
    return count


def _approval_created_count(proposals: Any, planner_result: Any = None) -> int:
    count = 0
    if isinstance(proposals, list):
        for proposal in proposals:
            if isinstance(proposal, dict) and proposal.get("approval_created") is True:
                count += 1
    if isinstance(planner_result, dict) and planner_result.get("approval_created") is True:
        count += 1
    return count


def _first_recovery_classification(summary: dict[str, Any]) -> dict[str, Any]:
    classifications = summary.get("recovery_proposal_classifications")
    if not isinstance(classifications, list) or not classifications:
        return {}
    first = classifications[0]
    return dict(first) if isinstance(first, dict) else {}


def _first_recovery_input_observation_keys(proposals: Any) -> list[str]:
    if not isinstance(proposals, list) or not proposals:
        return []
    first = proposals[0]
    if not isinstance(first, dict):
        return []
    observations = first.get("input_observations")
    if not isinstance(observations, dict):
        return []
    return sorted(str(key) for key in observations)


def _guardrail_blocked_llm_output_count(planner_result: Any) -> int:
    if not isinstance(planner_result, dict):
        return 0
    if planner_result.get("planner_status") != "guardrail_blocked":
        return 0
    guardrail = planner_result.get("guardrail")
    if not isinstance(guardrail, dict):
        return 0
    return 1 if guardrail.get("guardrail_passed") is False else 0


def _recovery_decision_demo_summary(
    *,
    scenario: str,
    trigger: str,
    approved: dict[str, Any],
    executed: dict[str, Any],
    summary: dict[str, Any],
    proposals: list[dict[str, Any]],
    planner_result: dict[str, Any],
) -> dict[str, Any]:
    classification = _first_recovery_classification(summary)
    mission_operator_approval_count = 1 if approved.get("routed_action") == "approve" else 0
    summary_fresh_approval_count = int(
        summary.get("fresh_recovery_operator_approval_count") or 0
    )
    fresh_recovery_approval_count = _approval_created_count(
        proposals,
        planner_result,
    ) + summary_fresh_approval_count
    recovery_dispatch_request_sent = summary.get("recovery_dispatch_request_sent")
    return {
        "schema_version": "missionos_turtlebot3_recovery_decision_demo.v1",
        "enabled": True,
        "scenario": scenario,
        "trigger": trigger,
        "approve_route": approved.get("routed_action"),
        "execute_route": executed.get("routed_action"),
        "judgment_required": summary.get("runtime_recovery_triggered") is True,
        "accepted_recovery_proposal_count": _proposal_count(proposals),
        "llm_recovery_judgment_count": _proposal_count(proposals, source="llm"),
        "deterministic_fallback_count": _proposal_count(
            proposals,
            source="deterministic_fallback",
        ),
        "guardrail_blocked_llm_output_count": _guardrail_blocked_llm_output_count(
            planner_result
        ),
        "recovery_proposal_source": _first_recovery_proposal_source(proposals),
        "source_backed_input_observation_keys": _first_recovery_input_observation_keys(
            proposals
        ),
        "selected_action": summary.get("runtime_recovery_action_kind")
        or summary.get("recovery_action_suggested"),
        "rules_execution_class": classification.get("execution_class"),
        "requires_new_human_approval": classification.get(
            "requires_new_human_approval"
        ),
        "execution_permitted_by_envelope": classification.get(
            "execution_permitted_by_envelope"
        ),
        "proposal_allowed": classification.get("proposal_allowed"),
        "mission_operator_approval_count": mission_operator_approval_count,
        "fresh_recovery_operator_approval_count": fresh_recovery_approval_count,
        "fresh_recovery_operator_approvals": summary.get(
            "fresh_recovery_operator_approvals"
        )
        or [],
        "operator_approval_created_for_recovery": (
            summary_fresh_approval_count > 0
        ),
        "operator_approval_reused_for_recovery": recovery_dispatch_request_sent is True
        and mission_operator_approval_count == 1
        and fresh_recovery_approval_count == 0,
        "recovery_execution_permitted_by_operator_approval": summary.get(
            "recovery_execution_permitted_by_operator_approval"
        ),
        "recovery_dispatch_authority_source": summary.get(
            "recovery_dispatch_authority_source"
        ),
        "recovery_dispatch_request_sent": recovery_dispatch_request_sent,
        "recovery_completion_claimed": summary.get("recovery_completion_claimed"),
        "route_resumed_after_recovery": summary.get("route_resumed_after_recovery"),
        "route_completed_after_recovery": summary.get("route_completed_after_recovery"),
        "runtime_failure_recovery_triggered": summary.get(
            "runtime_failure_recovery_triggered"
        ),
        "runtime_failure_context": summary.get("runtime_failure_context") or {},
        "runtime_motion_context": summary.get("runtime_recovery_motion_context") or {},
        "recovery_planner_status": summary.get("recovery_planner_status"),
        "completion_scope": summary.get("completion_scope"),
        "completion_claimed": summary.get("completion_claimed"),
        "mission_delivery_completion_claimed": summary.get(
            "mission_delivery_completion_claimed"
        ),
        "physical_execution_invoked": summary.get("physical_execution_invoked"),
    }


def _recovery_decision_trigger(
    summary: dict[str, Any],
    *,
    fallback: str,
) -> str:
    if summary.get("runtime_failure_recovery_triggered") is True:
        return "runtime_segment_failure"
    return fallback


def _disabled_recovery_decision_demo_summary() -> dict[str, Any]:
    return {
        "schema_version": "missionos_turtlebot3_recovery_decision_demo.v1",
        "enabled": False,
    }


def _recovery_planner_blocking_reasons(summary: dict[str, Any]) -> list[str]:
    result = summary.get("recovery_planner_result")
    if not isinstance(result, dict):
        return []
    reasons = result.get("blocking_reasons")
    return [str(reason) for reason in reasons] if isinstance(reasons, list) else []


def _recovery_planner_guardrail_checks(summary: dict[str, Any]) -> dict[str, Any]:
    result = summary.get("recovery_planner_result")
    if not isinstance(result, dict):
        return {}
    guardrail = result.get("guardrail")
    if not isinstance(guardrail, dict):
        return {}
    checks = guardrail.get("checks")
    return dict(checks) if isinstance(checks, dict) else {}


def _task_recovery_decision_summary(operation: dict[str, Any]) -> dict[str, Any]:
    summary = operation.get("turtlebot3_recovery_decision_summary")
    return dict(summary) if isinstance(summary, dict) else {}


def _run_chat_flow(
    *,
    base_url: str,
    session_id: str,
    instruction: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    plan = _post_conversation(
        base_url=base_url,
        instruction=instruction,
        session_id=session_id,
    )
    approved = _post_conversation(
        base_url=base_url,
        instruction="approve",
        session_id=session_id,
        context=plan.get("mission_designer") if isinstance(plan, dict) else {},
        route_hint="approve",
    )
    executed = _post_conversation(
        base_url=base_url,
        instruction="run",
        session_id=session_id,
        context=approved.get("mission_designer")
        if isinstance(approved, dict)
        else {},
        route_hint="execute",
    )
    return plan, approved, executed


def _approve_turtlebot3_recovery_checkpoint(
    *,
    base_url: str,
    executed: dict[str, Any],
) -> dict[str, Any]:
    """Approve the exact pending checkpoint through the Gateway boundary."""

    operation = executed.get("operation_result")
    operation = dict(operation) if isinstance(operation, dict) else {}
    summary = operation.get("summary")
    summary = dict(summary) if isinstance(summary, dict) else {}
    checkpoint = operation.get("turtlebot3_recovery_checkpoint")
    checkpoint = dict(checkpoint) if isinstance(checkpoint, dict) else {}
    task_id = str(summary.get("task_id") or "")
    if not task_id:
        raise RuntimeError("pending TurtleBot3 recovery task_id is missing")
    if checkpoint.get("checkpoint_status") != "awaiting_operator_approval":
        raise RuntimeError(
            "TurtleBot3 recovery checkpoint is not awaiting operator approval"
        )
    approval_response = _post_json(
        f"{base_url}/px4-gazebo/mission-scenarios/recovery-dispatch",
        {
            "task_id": task_id,
            "recovery_action": checkpoint.get("selected_action"),
            "recovery_parameters": checkpoint.get("approved_parameters") or {},
            "explicit_recovery_dispatch_approval": True,
            "expected_recovery_checkpoint_id": checkpoint.get("checkpoint_id"),
            "expected_recovery_checkpoint_hash": checkpoint.get(
                "checkpoint_hash"
            ),
        },
        timeout_s=_http_timeout_seconds(),
    )
    task = approval_response.get("task")
    task = dict(task) if isinstance(task, dict) else {}
    artifacts = task.get("artifacts")
    artifacts = dict(artifacts) if isinstance(artifacts, dict) else {}
    if not artifacts:
        raise RuntimeError("approved TurtleBot3 recovery task artifacts are missing")
    return {
        **executed,
        "operation_result": artifacts,
        "recovery_approval_response": approval_response,
    }


def main() -> int:
    base_url, proc = _start_gateway()
    try:
        mid_recovery_plan: dict[str, Any] = {}
        mid_recovery_approved: dict[str, Any] = {}
        mid_recovery_executed: dict[str, Any] = {}
        dynamic_obstacle_plan: dict[str, Any] = {}
        dynamic_obstacle_approved: dict[str, Any] = {}
        dynamic_obstacle_executed: dict[str, Any] = {}
        mid_recovery_enabled = (
            _truthy_env(WITH_BRIDGE_ENV) and _mid_recovery_smoke_enabled()
        )
        dynamic_obstacle_recovery_enabled = (
            _truthy_env(WITH_BRIDGE_ENV)
            and _dynamic_obstacle_recovery_smoke_enabled()
        )
        # The env flag selects an E2E scenario only. Approval authority is minted
        # by the Gateway after it verifies the exact pending checkpoint.
        human_approval_demo_enabled = bool(
            dynamic_obstacle_recovery_enabled
            and _truthy_env(HUMAN_APPROVAL_DEMO_SMOKE_ENV)
        )
        localization_drift_fault_enabled = (
            mid_recovery_enabled and _localization_drift_fault_smoke_enabled()
        )

        session_id = "smoke-chat-turtlebot3-home-mission"
        instruction = os.environ.get(
            INSTRUCTION_ENV,
            "TurtleBot3で家の中を一周して",
        )
        plan: dict[str, Any]
        approved: dict[str, Any]
        executed: dict[str, Any]
        if localization_drift_fault_enabled:
            plan, approved, executed = _run_chat_flow(
                base_url=base_url,
                session_id=session_id,
                instruction=instruction,
            )
            _stop_gateway(proc)
            base_url, proc = _start_gateway(
                env_overrides=_localization_drift_env_overrides()
            )

        if mid_recovery_enabled:
            mid_recovery_instruction = os.environ.get(RECOVERY_INSTRUCTION_ENV) or (
                "TurtleBot3 indoor delivery route with obstacle avoidance. "
                "During the mission, if battery becomes insufficient, return home"
            )
            (
                mid_recovery_plan,
                mid_recovery_approved,
                mid_recovery_executed,
            ) = _run_chat_flow(
                base_url=base_url,
                session_id="smoke-chat-turtlebot3-mid-recovery",
                instruction=mid_recovery_instruction,
            )

        if dynamic_obstacle_recovery_enabled:
            (
                dynamic_obstacle_plan,
                dynamic_obstacle_approved,
                dynamic_obstacle_executed,
            ) = _run_chat_flow(
                base_url=base_url,
                session_id="smoke-chat-turtlebot3-dynamic-obstacle-recovery",
                instruction=(
                    "TurtleBot3 indoor delivery route. During the mission, "
                    "if an obstacle appears, the Recovery Agent should propose "
                    "avoid_obstacle, MissionOS should dispatch the bounded "
                    "recovery waypoint, then resume delivery."
                ),
            )
            if human_approval_demo_enabled:
                dynamic_obstacle_executed = (
                    _approve_turtlebot3_recovery_checkpoint(
                        base_url=base_url,
                        executed=dynamic_obstacle_executed,
                    )
                )
            plan = dynamic_obstacle_plan
            approved = dynamic_obstacle_approved
            executed = dynamic_obstacle_executed

        if not localization_drift_fault_enabled and not dynamic_obstacle_recovery_enabled:
            plan, approved, executed = _run_chat_flow(
                base_url=base_url,
                session_id=session_id,
                instruction=instruction,
            )
        (
            low_battery_plan,
            low_battery_approved,
            low_battery_executed,
        ) = _run_chat_flow(
            base_url=base_url,
            session_id="smoke-chat-turtlebot3-low-battery",
            instruction="TurtleBot3で家の中を一周して。バッテリーが足りない",
        )
    finally:
        _stop_gateway(proc)

    operation = (
        executed.get("operation_result")
        if isinstance(executed.get("operation_result"), dict)
        else {}
    )
    summary = operation.get("summary") if isinstance(operation.get("summary"), dict) else {}
    task_decision_summary = _task_recovery_decision_summary(operation)
    low_battery_operation = (
        low_battery_executed.get("operation_result")
        if isinstance(low_battery_executed.get("operation_result"), dict)
        else {}
    )
    low_battery_summary = (
        low_battery_operation.get("summary")
        if isinstance(low_battery_operation.get("summary"), dict)
        else {}
    )
    mid_recovery_operation = (
        mid_recovery_executed.get("operation_result")
        if isinstance(mid_recovery_executed.get("operation_result"), dict)
        else {}
    )
    mid_recovery_summary = (
        mid_recovery_operation.get("summary")
        if isinstance(mid_recovery_operation.get("summary"), dict)
        else {}
    )
    dynamic_obstacle_operation = (
        dynamic_obstacle_executed.get("operation_result")
        if isinstance(dynamic_obstacle_executed.get("operation_result"), dict)
        else {}
    )
    dynamic_obstacle_summary = (
        dynamic_obstacle_operation.get("summary")
        if isinstance(dynamic_obstacle_operation.get("summary"), dict)
        else {}
    )
    dynamic_obstacle_task_decision_summary = _task_recovery_decision_summary(
        dynamic_obstacle_operation
    )
    indoor_map = summary.get("turtlebot3_indoor_map_model")
    indoor_map = indoor_map if isinstance(indoor_map, dict) else {}
    indoor_observed = indoor_map.get("observed_points")
    indoor_planned = indoor_map.get("planned_points")
    indoor_obstacles = indoor_map.get("obstacles")
    recovery_proposals = summary.get("recovery_proposals")
    recovery_proposals = recovery_proposals if isinstance(recovery_proposals, list) else []
    recovery_planner_result = summary.get("recovery_planner_result")
    recovery_planner_result = (
        recovery_planner_result if isinstance(recovery_planner_result, dict) else {}
    )
    low_battery_recovery_proposals = low_battery_summary.get("recovery_proposals")
    low_battery_recovery_proposals = (
        low_battery_recovery_proposals
        if isinstance(low_battery_recovery_proposals, list)
        else []
    )
    low_battery_recovery_planner_result = low_battery_summary.get(
        "recovery_planner_result"
    )
    low_battery_recovery_planner_result = (
        low_battery_recovery_planner_result
        if isinstance(low_battery_recovery_planner_result, dict)
        else {}
    )
    mid_recovery_proposals = mid_recovery_summary.get("recovery_proposals")
    mid_recovery_proposals = (
        mid_recovery_proposals
        if isinstance(mid_recovery_proposals, list)
        else []
    )
    mid_recovery_planner_result = mid_recovery_summary.get("recovery_planner_result")
    mid_recovery_planner_result = (
        mid_recovery_planner_result
        if isinstance(mid_recovery_planner_result, dict)
        else {}
    )
    dynamic_obstacle_recovery_proposals = dynamic_obstacle_summary.get(
        "recovery_proposals"
    )
    dynamic_obstacle_recovery_proposals = (
        dynamic_obstacle_recovery_proposals
        if isinstance(dynamic_obstacle_recovery_proposals, list)
        else []
    )
    dynamic_obstacle_recovery_planner_result = dynamic_obstacle_summary.get(
        "recovery_planner_result"
    )
    dynamic_obstacle_recovery_planner_result = (
        dynamic_obstacle_recovery_planner_result
        if isinstance(dynamic_obstacle_recovery_planner_result, dict)
        else {}
    )
    decision_demo = _disabled_recovery_decision_demo_summary()
    if dynamic_obstacle_recovery_enabled:
        decision_demo = _recovery_decision_demo_summary(
            scenario="dynamic_obstacle_recovery",
            trigger=_recovery_decision_trigger(
                dynamic_obstacle_summary,
                fallback="runtime_obstacle",
            ),
            approved=dynamic_obstacle_approved,
            executed=dynamic_obstacle_executed,
            summary=dynamic_obstacle_summary,
            proposals=dynamic_obstacle_recovery_proposals,
            planner_result=dynamic_obstacle_recovery_planner_result,
        )
    elif localization_drift_fault_enabled:
        decision_demo = _recovery_decision_demo_summary(
            scenario="localization_drift_failure_recovery",
            trigger="runtime_segment_failure",
            approved=mid_recovery_approved,
            executed=mid_recovery_executed,
            summary=mid_recovery_summary,
            proposals=mid_recovery_proposals,
            planner_result=mid_recovery_planner_result,
        )
    elif mid_recovery_enabled:
        decision_demo = _recovery_decision_demo_summary(
            scenario="low_battery_return_home_recovery",
            trigger="battery_envelope",
            approved=mid_recovery_approved,
            executed=mid_recovery_executed,
            summary=mid_recovery_summary,
            proposals=mid_recovery_proposals,
            planner_result=mid_recovery_planner_result,
        )
    result = {
        "smoke": "missionos_chat_turtlebot3_home_mission",
        "with_bridge": _truthy_env(WITH_BRIDGE_ENV),
        "mid_mission_recovery_enabled": _truthy_env(WITH_BRIDGE_ENV)
        and _mid_recovery_smoke_enabled(),
        "localization_drift_fault_injection_enabled": _truthy_env(WITH_BRIDGE_ENV)
        and _mid_recovery_smoke_enabled()
        and _localization_drift_fault_smoke_enabled(),
        "dynamic_obstacle_recovery_enabled": _truthy_env(WITH_BRIDGE_ENV)
        and _dynamic_obstacle_recovery_smoke_enabled(),
        "decision_demo_smoke_enabled": _truthy_env(WITH_BRIDGE_ENV)
        and _decision_demo_smoke_enabled(),
        "human_approval_demo_smoke_enabled": human_approval_demo_enabled,
        "recovery_guardrail_fallback_injection_enabled": _truthy_env(WITH_BRIDGE_ENV)
        and _dynamic_obstacle_recovery_smoke_enabled()
        and _recovery_guardrail_fallback_smoke_enabled(),
        "decision_demo": decision_demo,
        "task_recovery_decision_summary": task_decision_summary,
        "plan_route": plan.get("routed_action"),
        "approve_route": approved.get("routed_action"),
        "execute_route": executed.get("routed_action"),
        "status": summary.get("status"),
        "home_robot_mission_kind": summary.get("home_robot_mission_kind"),
        "dispatch_request_sent": summary.get("dispatch_request_sent"),
        "completion_claimed": summary.get("completion_claimed"),
        "completion_scope": summary.get("completion_scope"),
        "home_distance_m": _home_distance(summary, "distance_to_home_m"),
        "home_distance_source": _home_distance(summary, "distance_to_home_source"),
        "planned_segment_count": summary.get("planned_segment_count"),
        "planned_route_distance_m": summary.get("planned_route_distance_m"),
        "segment_dispatch_count": summary.get("segment_dispatch_count"),
        "segment_completion_count": summary.get("segment_completion_count"),
        "multi_segment_mission_claimed": summary.get("multi_segment_mission_claimed"),
        "llm_recovery_proposals_allowed": summary.get(
            "llm_recovery_proposals_allowed"
        ),
        "proposal_first_classification": summary.get("proposal_first_classification"),
        "recovery_action_suggested": summary.get("recovery_action_suggested"),
        "recovery_planner_status": summary.get("recovery_planner_status"),
        "runtime_recovery_motion_context": summary.get(
            "runtime_recovery_motion_context"
        )
        or {},
        "recovery_proposal_count": _proposal_count(recovery_proposals),
        "llm_recovery_proposal_count": _proposal_count(
            recovery_proposals,
            source="llm",
        ),
        "recovery_approval_created_count": _approval_created_count(
            recovery_proposals,
            recovery_planner_result,
        ),
        "recovery_proposal_source": _first_recovery_proposal_source(
            recovery_proposals
        ),
        "recovery_execution_permitted_by_envelope": summary.get(
            "recovery_execution_permitted_by_envelope"
        ),
        "recovery_dispatch_request_sent": summary.get("recovery_dispatch_request_sent"),
        "robot_motion_observed": summary.get("robot_motion_observed"),
        "odom_delta_m": summary.get("odom_delta_m"),
        "robot_motion_observation_source": summary.get(
            "robot_motion_observation_source"
        ),
        "telemetry_sidecar_required": summary.get("telemetry_sidecar_required"),
        "telemetry_sidecar_motion_correlation_confirmed": summary.get(
            "telemetry_sidecar_motion_correlation_confirmed"
        ),
        "telemetry_sidecar_blocking_reasons": summary.get(
            "telemetry_sidecar_blocking_reasons"
        )
        or [],
        "telemetry_window_ref": summary.get("telemetry_window_ref"),
        "telemetry_raw_logs_ref": summary.get("telemetry_raw_logs_ref"),
        "log_bundle_status": summary.get("log_bundle_status"),
        "raw_logs_ref": summary.get("raw_logs_ref"),
        "log_bundle_observed_source_count": summary.get(
            "log_bundle_observed_source_count"
        ),
        "log_bundle_source_count": summary.get("log_bundle_source_count"),
        "log_bundle_blocking_reasons": summary.get("log_bundle_blocking_reasons")
        or [],
        "nav2_log_diagnostics_status": summary.get("nav2_log_diagnostics_status"),
        "nav2_log_observed_patterns": summary.get("nav2_log_observed_patterns")
        or [],
        "nav2_log_failure_hypotheses": summary.get("nav2_log_failure_hypotheses")
        or [],
        "obstacle_challenge_required": summary.get("obstacle_challenge_required"),
        "costmap_obstacle_observed": summary.get("costmap_obstacle_observed"),
        "obstacle_avoidance_observed": summary.get("obstacle_avoidance_observed"),
        "trajectory_lateral_deviation_observed": summary.get(
            "trajectory_lateral_deviation_observed"
        ),
        "max_lateral_deviation_m": summary.get("max_lateral_deviation_m"),
        "obstacle_avoidance_completion_claimed": summary.get(
            "obstacle_avoidance_completion_claimed"
        ),
        "obstacle_trajectory_3d_clearance_status": summary.get(
            "obstacle_trajectory_3d_clearance_status"
        ),
        "obstacle_trajectory_3d_clearance_observed": summary.get(
            "obstacle_trajectory_3d_clearance_observed"
        ),
        "obstacle_trajectory_3d_collision_observed": summary.get(
            "obstacle_trajectory_3d_collision_observed"
        ),
        "obstacle_trajectory_3d_minimum_surface_clearance_m": (
            (summary.get("obstacle_trajectory_3d_clearance") or {}).get(
                "minimum_surface_clearance_m"
            )
        ),
        "indoor_delivery_route_completion_claimed": summary.get(
            "indoor_delivery_route_completion_claimed"
        ),
        "indoor_map_model_present": bool(indoor_map),
        "indoor_map_kind": indoor_map.get("map_kind"),
        "indoor_map_observed_point_count": len(indoor_observed)
        if isinstance(indoor_observed, list)
        else 0,
        "indoor_map_planned_point_count": len(indoor_planned)
        if isinstance(indoor_planned, list)
        else 0,
        "indoor_map_obstacle_count": len(indoor_obstacles)
        if isinstance(indoor_obstacles, list)
        else 0,
        "indoor_map_observed_pose_source": indoor_map.get("observed_pose_source"),
        "dropoff_arrival_claimed": summary.get("dropoff_arrival_claimed"),
        "physical_execution_invoked": summary.get("physical_execution_invoked"),
        "mission_delivery_completion_claimed": summary.get(
            "mission_delivery_completion_claimed"
        ),
        "mission_episode_review_ref": summary.get("mission_episode_review_ref"),
        "mission_episode_review_status": summary.get("mission_episode_review_status"),
        "mission_episode_review_passed": summary.get("mission_episode_review_passed"),
        "mission_episode_review_blocked_buckets": summary.get(
            "mission_episode_review_blocked_buckets"
        )
        or [],
        "blocking_reasons": summary.get("blocking_reasons") or [],
        "low_battery": {
            "plan_route": low_battery_plan.get("routed_action"),
            "approve_route": low_battery_approved.get("routed_action"),
            "execute_route": low_battery_executed.get("routed_action"),
            "status": low_battery_summary.get("status"),
            "dispatch_request_sent": low_battery_summary.get("dispatch_request_sent"),
            "completion_claimed": low_battery_summary.get("completion_claimed"),
            "home_distance_m": _home_distance(
                low_battery_summary,
                "distance_to_home_m",
            ),
            "home_distance_source": _home_distance(
                low_battery_summary,
                "distance_to_home_source",
            ),
            "llm_recovery_proposals_allowed": low_battery_summary.get(
                "llm_recovery_proposals_allowed"
            ),
            "proposal_first_classification": low_battery_summary.get(
                "proposal_first_classification"
            ),
            "recovery_action_suggested": low_battery_summary.get(
                "recovery_action_suggested"
            ),
            "recovery_planner_status": low_battery_summary.get(
                "recovery_planner_status"
            ),
            "runtime_recovery_motion_context": low_battery_summary.get(
                "runtime_recovery_motion_context"
            )
            or {},
            "recovery_proposal_count": _proposal_count(
                low_battery_recovery_proposals
            ),
            "llm_recovery_proposal_count": _proposal_count(
                low_battery_recovery_proposals,
                source="llm",
            ),
            "recovery_approval_created_count": _approval_created_count(
                low_battery_recovery_proposals,
                low_battery_recovery_planner_result,
            ),
            "recovery_planner_blocking_reasons": _recovery_planner_blocking_reasons(
                low_battery_summary
            ),
            "recovery_planner_guardrail_checks": _recovery_planner_guardrail_checks(
                low_battery_summary
            ),
            "recovery_proposal_source": _first_recovery_proposal_source(
                low_battery_recovery_proposals
            ),
            "recovery_execution_permitted_by_envelope": low_battery_summary.get(
                "recovery_execution_permitted_by_envelope"
            ),
            "recovery_dispatch_request_sent": low_battery_summary.get(
                "recovery_dispatch_request_sent"
            ),
            "recovery_proposal_classifications": low_battery_summary.get(
                "recovery_proposal_classifications"
            )
            or [],
            "physical_execution_invoked": low_battery_summary.get(
                "physical_execution_invoked"
            ),
            "blocking_reasons": low_battery_summary.get("blocking_reasons") or [],
        },
        "mid_mission_recovery": {
            "plan_route": mid_recovery_plan.get("routed_action"),
            "approve_route": mid_recovery_approved.get("routed_action"),
            "execute_route": mid_recovery_executed.get("routed_action"),
            "status": mid_recovery_summary.get("status"),
            "dispatch_request_sent": mid_recovery_summary.get("dispatch_request_sent"),
            "completion_claimed": mid_recovery_summary.get("completion_claimed"),
            "runtime_recovery_triggered": mid_recovery_summary.get(
                "runtime_recovery_triggered"
            ),
            "route_interrupted_for_recovery": mid_recovery_summary.get(
                "route_interrupted_for_recovery"
            ),
            "planned_segment_count": mid_recovery_summary.get("planned_segment_count"),
            "planned_route_distance_m": mid_recovery_summary.get(
                "planned_route_distance_m"
            ),
            "segment_dispatch_count": mid_recovery_summary.get("segment_dispatch_count"),
            "segment_completion_count": mid_recovery_summary.get(
                "segment_completion_count"
            ),
            "recovery_action_suggested": mid_recovery_summary.get(
                "recovery_action_suggested"
            ),
            "recovery_dispatch_request_sent": mid_recovery_summary.get(
                "recovery_dispatch_request_sent"
            ),
            "recovery_completion_claimed": mid_recovery_summary.get(
                "recovery_completion_claimed"
            ),
            "recovery_planner_status": mid_recovery_summary.get(
                "recovery_planner_status"
            ),
            "runtime_failure_recovery_triggered": mid_recovery_summary.get(
                "runtime_failure_recovery_triggered"
            ),
            "runtime_failure_context": mid_recovery_summary.get(
                "runtime_failure_context"
            )
            or {},
            "runtime_recovery_motion_context": mid_recovery_summary.get(
                "runtime_recovery_motion_context"
            )
            or {},
            "runtime_recovery_action_kind": mid_recovery_summary.get(
                "runtime_recovery_action_kind"
            ),
            "recovery_proposal_count": _proposal_count(mid_recovery_proposals),
            "llm_recovery_proposal_count": _proposal_count(
                mid_recovery_proposals,
                source="llm",
            ),
            "recovery_approval_created_count": _approval_created_count(
                mid_recovery_proposals,
                mid_recovery_planner_result,
            ),
            "recovery_proposals": mid_recovery_proposals,
            "recovery_proposal_source": _first_recovery_proposal_source(
                mid_recovery_proposals
            ),
            "recovery_proposal_classifications": mid_recovery_summary.get(
                "recovery_proposal_classifications"
            )
            or [],
            "recovery_planner_blocking_reasons": _recovery_planner_blocking_reasons(
                mid_recovery_summary
            ),
            "recovery_planner_guardrail_checks": _recovery_planner_guardrail_checks(
                mid_recovery_summary
            ),
            "recovery_execution_permitted_by_envelope": mid_recovery_summary.get(
                "recovery_execution_permitted_by_envelope"
            ),
            "physical_execution_invoked": mid_recovery_summary.get(
                "physical_execution_invoked"
            ),
            "mission_delivery_completion_claimed": mid_recovery_summary.get(
                "mission_delivery_completion_claimed"
            ),
            "mission_episode_review_status": mid_recovery_summary.get(
                "mission_episode_review_status"
            ),
            "mission_episode_review_passed": mid_recovery_summary.get(
                "mission_episode_review_passed"
            ),
            "mission_episode_review_blocked_buckets": mid_recovery_summary.get(
                "mission_episode_review_blocked_buckets"
            )
            or [],
            "nav2_log_diagnostics_status": mid_recovery_summary.get(
                "nav2_log_diagnostics_status"
            ),
            "nav2_log_observed_patterns": mid_recovery_summary.get(
                "nav2_log_observed_patterns"
            )
            or [],
            "nav2_log_failure_hypotheses": mid_recovery_summary.get(
                "nav2_log_failure_hypotheses"
            )
            or [],
            "blocking_reasons": mid_recovery_summary.get("blocking_reasons") or [],
        },
        "dynamic_obstacle_recovery": {
            "plan_route": dynamic_obstacle_plan.get("routed_action"),
            "approve_route": dynamic_obstacle_approved.get("routed_action"),
            "execute_route": dynamic_obstacle_executed.get("routed_action"),
            "status": dynamic_obstacle_summary.get("status"),
            "dispatch_request_sent": dynamic_obstacle_summary.get(
                "dispatch_request_sent"
            ),
            "completion_claimed": dynamic_obstacle_summary.get("completion_claimed"),
            "runtime_recovery_triggered": dynamic_obstacle_summary.get(
                "runtime_recovery_triggered"
            ),
            "runtime_recovery_action_kind": dynamic_obstacle_summary.get(
                "runtime_recovery_action_kind"
            ),
            "route_resumed_after_recovery": dynamic_obstacle_summary.get(
                "route_resumed_after_recovery"
            ),
            "route_completed_after_recovery": dynamic_obstacle_summary.get(
                "route_completed_after_recovery"
            ),
            "planned_segment_count": dynamic_obstacle_summary.get(
                "planned_segment_count"
            ),
            "planned_route_distance_m": dynamic_obstacle_summary.get(
                "planned_route_distance_m"
            ),
            "segment_dispatch_count": dynamic_obstacle_summary.get(
                "segment_dispatch_count"
            ),
            "segment_completion_count": dynamic_obstacle_summary.get(
                "segment_completion_count"
            ),
            "recovery_action_suggested": dynamic_obstacle_summary.get(
                "recovery_action_suggested"
            ),
            "recovery_dispatch_request_sent": dynamic_obstacle_summary.get(
                "recovery_dispatch_request_sent"
            ),
            "recovery_completion_claimed": dynamic_obstacle_summary.get(
                "recovery_completion_claimed"
            ),
            "recovery_planner_status": dynamic_obstacle_summary.get(
                "recovery_planner_status"
            ),
            "runtime_recovery_motion_context": dynamic_obstacle_summary.get(
                "runtime_recovery_motion_context"
            )
            or {},
            "recovery_proposal_count": _proposal_count(
                dynamic_obstacle_recovery_proposals
            ),
            "llm_recovery_proposal_count": _proposal_count(
                dynamic_obstacle_recovery_proposals,
                source="llm",
            ),
            "recovery_approval_created_count": _approval_created_count(
                dynamic_obstacle_recovery_proposals,
                dynamic_obstacle_recovery_planner_result,
            )
            + int(dynamic_obstacle_summary.get("fresh_recovery_operator_approval_count") or 0),
            "fresh_recovery_operator_approval_count": dynamic_obstacle_summary.get(
                "fresh_recovery_operator_approval_count"
            ),
            "fresh_recovery_operator_approvals": dynamic_obstacle_summary.get(
                "fresh_recovery_operator_approvals"
            )
            or [],
            "recovery_execution_permitted_by_operator_approval": (
                dynamic_obstacle_summary.get(
                    "recovery_execution_permitted_by_operator_approval"
                )
            ),
            "recovery_dispatch_authority_source": dynamic_obstacle_summary.get(
                "recovery_dispatch_authority_source"
            ),
            "recovery_planner_blocking_reasons": _recovery_planner_blocking_reasons(
                dynamic_obstacle_summary
            ),
            "recovery_planner_guardrail_checks": _recovery_planner_guardrail_checks(
                dynamic_obstacle_summary
            ),
            "recovery_proposals": dynamic_obstacle_recovery_proposals,
            "recovery_proposal_source": _first_recovery_proposal_source(
                dynamic_obstacle_recovery_proposals
            ),
            "recovery_proposal_classifications": dynamic_obstacle_summary.get(
                "recovery_proposal_classifications"
            )
            or [],
            "obstacle_avoidance_completion_claimed": dynamic_obstacle_summary.get(
                "obstacle_avoidance_completion_claimed"
            ),
            "obstacle_trajectory_clearance_observed": dynamic_obstacle_summary.get(
                "obstacle_trajectory_clearance_observed"
            ),
            "obstacle_trajectory_intersects_obstacle": dynamic_obstacle_summary.get(
                "obstacle_trajectory_intersects_obstacle"
            ),
            "obstacle_trajectory_3d_clearance_status": (
                dynamic_obstacle_summary.get(
                    "obstacle_trajectory_3d_clearance_status"
                )
            ),
            "obstacle_trajectory_3d_clearance_observed": (
                dynamic_obstacle_summary.get(
                    "obstacle_trajectory_3d_clearance_observed"
                )
            ),
            "obstacle_trajectory_3d_collision_observed": (
                dynamic_obstacle_summary.get(
                    "obstacle_trajectory_3d_collision_observed"
                )
            ),
            "indoor_delivery_route_completion_claimed": dynamic_obstacle_summary.get(
                "indoor_delivery_route_completion_claimed"
            ),
            "physical_execution_invoked": dynamic_obstacle_summary.get(
                "physical_execution_invoked"
            ),
            "mission_delivery_completion_claimed": dynamic_obstacle_summary.get(
                "mission_delivery_completion_claimed"
            ),
            "mission_episode_review_status": dynamic_obstacle_summary.get(
                "mission_episode_review_status"
            ),
            "mission_episode_review_passed": dynamic_obstacle_summary.get(
                "mission_episode_review_passed"
            ),
            "blocking_reasons": dynamic_obstacle_summary.get("blocking_reasons") or [],
            "task_recovery_decision_summary": dynamic_obstacle_task_decision_summary,
        },
    }
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))

    map_model_out = os.environ.get(MAP_MODEL_OUT_ENV, "").strip()
    if map_model_out and indoor_map:
        Path(map_model_out).parent.mkdir(parents=True, exist_ok=True)
        Path(map_model_out).write_text(
            json.dumps(indoor_map, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    if result["plan_route"] != "mission_designer_plan":
        raise SystemExit("chat did not route TurtleBot3 request to mission_designer_plan")
    if result["approve_route"] != "approve":
        raise SystemExit("chat approval did not route to approve")
    if result["execute_route"] != "execute":
        raise SystemExit("chat run did not route to execute")
    if result["physical_execution_invoked"] is not False:
        raise SystemExit("TurtleBot3 chat smoke claimed physical execution")
    if result["mission_delivery_completion_claimed"] is not False:
        raise SystemExit("TurtleBot3 chat smoke claimed mission delivery completion")
    if (
        _truthy_env(WITH_BRIDGE_ENV)
        and not result["decision_demo_smoke_enabled"]
        and result["mission_episode_review_status"] != "passed"
    ):
        raise SystemExit("TurtleBot3 chat smoke did not attach passing episode review")
    obstacle_requested = "障害物" in instruction or "obstacle" in instruction.lower()
    delivery_requested = "配送" in instruction or "deliver" in instruction.lower()
    if (
        obstacle_requested
        and _truthy_env(WITH_BRIDGE_ENV)
        and not _dynamic_obstacle_recovery_smoke_enabled()
    ):
        if result["obstacle_avoidance_completion_claimed"] is not True:
            raise SystemExit("obstacle mission completed without avoidance observation")
        if result["obstacle_trajectory_3d_clearance_status"] != "verified_clear":
            raise SystemExit("obstacle mission lacks verified 3D swept-volume clearance")
        if result["obstacle_trajectory_3d_clearance_observed"] is not True:
            raise SystemExit("obstacle mission lacks 3D clearance observation")
        if result["obstacle_trajectory_3d_collision_observed"] is not False:
            raise SystemExit("obstacle mission observed a 3D swept-volume collision")
    if _truthy_env(WITH_BRIDGE_ENV) and os.environ.get(
        "MISSIONOS_TURTLEBOT3_TELEMETRY_SIDECAR_JSONL",
        "",
    ).strip():
        if result["telemetry_sidecar_required"] is not True:
            raise SystemExit("telemetry sidecar JSONL was not required by MissionOS")
        if result["telemetry_sidecar_motion_correlation_confirmed"] is not True:
            raise SystemExit("telemetry sidecar motion correlation was not confirmed")
        if result["robot_motion_observation_source"] != "ros2_telemetry_sidecar_jsonl":
            raise SystemExit("robot motion did not use telemetry sidecar source")
    if _truthy_env(WITH_BRIDGE_ENV) and os.environ.get(
        "MISSIONOS_TURTLEBOT3_LOG_BUNDLE_PATHS",
        "",
    ).strip():
        if result["log_bundle_status"] != "ready":
            raise SystemExit("TurtleBot3 process log bundle was not ready")
        if not str(result["raw_logs_ref"] or "").startswith(
            "turtlebot3_process_log_bundle:"
        ):
            raise SystemExit("TurtleBot3 process log bundle ref was not recorded")
        if result["nav2_log_diagnostics_status"] != "ready":
            raise SystemExit("TurtleBot3 Nav2 log diagnostics were not ready")
    if (
        delivery_requested
        and _truthy_env(WITH_BRIDGE_ENV)
        and not result["recovery_guardrail_fallback_injection_enabled"]
        and not result["decision_demo_smoke_enabled"]
    ):
        if result["indoor_delivery_route_completion_claimed"] is not True:
            raise SystemExit("delivery route did not claim simulated dropoff arrival")
        if result["indoor_map_model_present"] is not True:
            raise SystemExit("delivery route did not emit TurtleBot3 indoor map model")
        if result["indoor_map_kind"] != "indoor_local_xy":
            raise SystemExit("delivery route emitted invalid indoor map kind")
        if result["dropoff_arrival_claimed"] is not True:
            raise SystemExit("delivery route did not claim dropoff arrival")
        if result["planned_segment_count"] is None or result["planned_segment_count"] < 2:
            raise SystemExit("delivery route did not execute as a multi-segment route")
        if result["segment_completion_count"] != result["planned_segment_count"]:
            raise SystemExit("delivery route did not complete every planned segment")
    low_battery = result["low_battery"]
    if low_battery["execute_route"] != "execute":
        raise SystemExit("low-battery chat run did not route to execute")
    if low_battery["dispatch_request_sent"] is not False:
        raise SystemExit("low-battery smoke dispatched despite battery judgment")
    if low_battery["recovery_action_suggested"] != "return_home":
        raise SystemExit("low-battery smoke did not preserve return_home proposal")
    if _truthy_env(RECOVERY_REQUIRES_APPROVAL_ENV):
        if low_battery["recovery_execution_permitted_by_envelope"] is not False:
            raise SystemExit(
                "low-battery return_home bypassed the approval-required policy"
            )
    elif low_battery["recovery_execution_permitted_by_envelope"] is not True:
        raise SystemExit(
            "low-battery return_home proposal was not classified executable"
        )
    if low_battery["recovery_dispatch_request_sent"] is not False:
        raise SystemExit(
            "low-battery smoke dispatched recovery without fresh authority"
        )
    if "battery_below_minimum_required" not in low_battery["blocking_reasons"]:
        raise SystemExit("low-battery smoke did not report battery gate")
    if _truthy_env(WITH_BRIDGE_ENV) and _mid_recovery_smoke_enabled():
        mid_recovery = result["mid_mission_recovery"]
        if mid_recovery["execute_route"] != "execute":
            raise SystemExit("mid-mission recovery run did not route to execute")
        if result["localization_drift_fault_injection_enabled"] is True:
            decision_demo = result["decision_demo"]
            if decision_demo["enabled"] is not True:
                raise SystemExit("localization drift fault did not emit decision demo")
            if decision_demo["scenario"] != "localization_drift_failure_recovery":
                raise SystemExit("localization drift fault emitted wrong decision scenario")
            if decision_demo["mission_operator_approval_count"] != 1:
                raise SystemExit("localization drift fault lost mission approval count")
            if decision_demo["fresh_recovery_operator_approval_count"] != 0:
                raise SystemExit(
                    "localization drift fault created fresh recovery approval"
                )
            if decision_demo["physical_execution_invoked"] is not False:
                raise SystemExit("localization drift decision demo claimed physical")
            if mid_recovery["status"] not in {"blocked", "recovered"}:
                raise SystemExit(
                    "localization drift fault injection did not fail safe as blocked/recovered"
                )
            if mid_recovery["runtime_recovery_triggered"] is not True:
                raise SystemExit(
                    "localization drift fault injection did not convene recovery"
                )
            if mid_recovery["runtime_failure_recovery_triggered"] is not True:
                raise SystemExit(
                    "localization drift fault injection did not record failure recovery"
                )
            if mid_recovery["recovery_proposal_count"] < 1:
                raise SystemExit(
                    "localization drift fault injection did not record a recovery proposal"
                )
            if mid_recovery["recovery_approval_created_count"] != 0:
                raise SystemExit(
                    "localization drift fault injection created recovery approval"
                )
            if mid_recovery["completion_claimed"] is not False:
                raise SystemExit(
                    "localization drift fault injection claimed mission completion"
                )
            if (
                mid_recovery["status"] == "blocked"
                and mid_recovery["recovery_completion_claimed"] is True
            ):
                raise SystemExit(
                    "localization drift fault injection claimed recovery completion"
                )
            if (
                mid_recovery["status"] == "recovered"
                and mid_recovery["recovery_completion_claimed"] is not True
            ):
                raise SystemExit(
                    "localization drift fault recovery did not claim return_home completion"
                )
            if mid_recovery["mission_delivery_completion_claimed"] is not False:
                raise SystemExit(
                    "localization drift fault injection claimed delivery completion"
                )
            if mid_recovery["mission_episode_review_status"] not in {"blocked", "passed"}:
                raise SystemExit(
                    "localization drift fault injection did not attach episode review"
                )
            if mid_recovery["physical_execution_invoked"] is not False:
                raise SystemExit(
                    "localization drift fault injection claimed physical execution"
                )
            if mid_recovery["nav2_log_diagnostics_status"] != "ready":
                raise SystemExit(
                    "localization drift fault injection did not expose Nav2 diagnostics"
                )
            if (
                not mid_recovery["blocking_reasons"]
                and not mid_recovery["nav2_log_failure_hypotheses"]
            ):
                raise SystemExit(
                    "localization drift fault injection lacked fail-safe diagnosis"
                )
            return 0
        if mid_recovery["status"] != "recovered":
            raise SystemExit("mid-mission recovery did not finish as recovered")
        if mid_recovery["runtime_recovery_triggered"] is not True:
            raise SystemExit("mid-mission recovery trigger was not observed")
        if mid_recovery["completion_claimed"] is not False:
            raise SystemExit("mid-mission recovery claimed delivery completion")
        if mid_recovery["segment_completion_count"] != 1:
            raise SystemExit("mid-mission recovery did not complete exactly one route segment")
        if mid_recovery["recovery_dispatch_request_sent"] is not True:
            raise SystemExit("mid-mission recovery did not dispatch return_home")
        if mid_recovery["recovery_completion_claimed"] is not True:
            raise SystemExit("mid-mission recovery did not claim return_home completion")
        if mid_recovery["physical_execution_invoked"] is not False:
            raise SystemExit("mid-mission recovery claimed physical execution")
        if mid_recovery["mission_episode_review_status"] != "passed":
            raise SystemExit("mid-mission recovery did not attach passing episode review")
    if _truthy_env(WITH_BRIDGE_ENV) and _dynamic_obstacle_recovery_smoke_enabled():
        dynamic_obstacle = result["dynamic_obstacle_recovery"]
        decision_demo = result["decision_demo"]
        decision_demo_smoke = result["decision_demo_smoke_enabled"] is True
        expected_source = os.environ.get(EXPECTED_RECOVERY_PROPOSAL_SOURCE_ENV, "")
        expected_source = expected_source.strip()
        if dynamic_obstacle["execute_route"] != "execute":
            raise SystemExit("dynamic obstacle recovery run did not route to execute")
        if decision_demo["enabled"] is not True:
            raise SystemExit("dynamic obstacle recovery did not emit decision demo")
        if decision_demo["scenario"] != "dynamic_obstacle_recovery":
            raise SystemExit("dynamic obstacle recovery emitted wrong decision scenario")
        if decision_demo["judgment_required"] is not True:
            raise SystemExit("dynamic obstacle recovery did not require judgment")
        if decision_demo["mission_operator_approval_count"] != 1:
            raise SystemExit("dynamic obstacle recovery lost mission approval count")
        task_decision_summary = dynamic_obstacle["task_recovery_decision_summary"]
        if task_decision_summary.get("schema_version") != (
            "missionos_turtlebot3_recovery_decision_summary.v1"
        ):
            raise SystemExit(
                "dynamic obstacle recovery did not persist task decision summary"
            )
        if task_decision_summary.get("read_only") is not True:
            raise SystemExit("task decision summary was not read-only")
        if task_decision_summary.get("decision_summary_creates_dispatch_authority") is not False:
            raise SystemExit("task decision summary created dispatch authority")
        for key in (
            "judgment_required",
            "llm_recovery_judgment_count",
            "fresh_recovery_operator_approval_count",
            "rules_execution_class",
            "requires_new_human_approval",
            "recovery_dispatch_authority_source",
        ):
            if task_decision_summary.get(key) != decision_demo.get(key):
                raise SystemExit(f"task decision summary mismatched {key}")
        human_approval_demo = result["human_approval_demo_smoke_enabled"] is True
        if human_approval_demo:
            if decision_demo["fresh_recovery_operator_approval_count"] != 1:
                raise SystemExit(
                    "human approval demo did not create exactly one recovery approval"
                )
            if decision_demo["operator_approval_reused_for_recovery"] is not False:
                raise SystemExit("human approval demo reused mission approval")
            if decision_demo["operator_approval_created_for_recovery"] is not True:
                raise SystemExit("human approval demo did not record fresh approval")
        elif decision_demo["fresh_recovery_operator_approval_count"] != 0:
            raise SystemExit("dynamic obstacle recovery created fresh recovery approval")
        if decision_demo_smoke:
            if decision_demo["selected_action"] not in {"avoid_obstacle", "return_home"}:
                raise SystemExit(
                    "decision demo selected an unexpected recovery action"
                )
        elif decision_demo["selected_action"] != "avoid_obstacle":
            raise SystemExit("dynamic obstacle decision did not select avoid_obstacle")
        if human_approval_demo:
            if decision_demo["rules_execution_class"] != "requires_human_approval":
                raise SystemExit("human approval demo did not require approval")
            if decision_demo["requires_new_human_approval"] is not True:
                raise SystemExit("human approval demo missing approval requirement")
            if decision_demo["execution_permitted_by_envelope"] is not False:
                raise SystemExit("human approval demo let envelope self-dispatch")
            if (
                decision_demo["recovery_execution_permitted_by_operator_approval"]
                is not True
            ):
                raise SystemExit("human approval demo did not permit via operator")
            if decision_demo["recovery_dispatch_authority_source"] != (
                "fresh_operator_approval"
            ):
                raise SystemExit("human approval demo used wrong dispatch authority")
        else:
            if decision_demo["rules_execution_class"] != "auto_executable":
                raise SystemExit("dynamic obstacle decision was not auto_executable")
            if decision_demo["requires_new_human_approval"] is not False:
                raise SystemExit("dynamic obstacle decision required unexpected approval")
        if decision_demo["physical_execution_invoked"] is not False:
            raise SystemExit("dynamic obstacle decision demo claimed physical execution")
        if decision_demo["mission_delivery_completion_claimed"] is not False:
            raise SystemExit("dynamic obstacle decision demo claimed payload delivery")
        if result["recovery_guardrail_fallback_injection_enabled"] is True:
            if dynamic_obstacle["status"] not in {"completed", "recovered"}:
                raise SystemExit(
                    "dynamic obstacle fallback did not finish as completed/recovered"
                )
        elif decision_demo_smoke:
            if dynamic_obstacle["status"] not in {"completed", "recovered", "blocked"}:
                raise SystemExit("decision demo did not finish in a bounded state")
        elif dynamic_obstacle["status"] != "completed":
            raise SystemExit("dynamic obstacle recovery did not finish as completed")
        if dynamic_obstacle["runtime_recovery_triggered"] is not True:
            raise SystemExit("dynamic obstacle recovery trigger was not observed")
        if decision_demo_smoke:
            if dynamic_obstacle["runtime_recovery_action_kind"] not in {
                "avoid_obstacle",
                "return_home",
            }:
                raise SystemExit(
                    "decision demo selected an unexpected runtime recovery action"
                )
        elif dynamic_obstacle["runtime_recovery_action_kind"] != "avoid_obstacle":
            raise SystemExit("dynamic obstacle recovery did not select avoid_obstacle")
        if (
            expected_source
            and dynamic_obstacle["recovery_proposal_source"] != expected_source
        ):
            raise SystemExit(
                "dynamic obstacle recovery proposal source mismatch: "
                f"expected={expected_source} "
                f"observed={dynamic_obstacle['recovery_proposal_source']}"
            )
        if expected_source == "llm" and decision_demo["llm_recovery_judgment_count"] < 1:
            raise SystemExit("dynamic obstacle recovery lacked accepted LLM judgment")
        if result["recovery_guardrail_fallback_injection_enabled"] is True:
            if decision_demo["guardrail_blocked_llm_output_count"] < 1:
                raise SystemExit(
                    "dynamic obstacle fallback did not count blocked LLM output"
                )
            if decision_demo["deterministic_fallback_count"] < 1:
                raise SystemExit(
                    "dynamic obstacle fallback did not count deterministic fallback"
                )
            if dynamic_obstacle["recovery_planner_status"] != "guardrail_blocked":
                raise SystemExit(
                    "dynamic obstacle fallback smoke did not block injected LLM output"
                )
            if dynamic_obstacle["recovery_proposal_source"] != "deterministic_fallback":
                raise SystemExit(
                    "dynamic obstacle fallback smoke did not use deterministic fallback"
                )
            checks = dynamic_obstacle["recovery_planner_guardrail_checks"]
            if checks.get("forbidden_authority_keys_absent") is not False:
                raise SystemExit(
                    "dynamic obstacle fallback smoke did not catch authority claim"
                )
            reasons = set(dynamic_obstacle["recovery_planner_blocking_reasons"])
            if "raw_llm_output_forbidden_authority_key:dispatch_request_sent" not in reasons:
                raise SystemExit(
                    "dynamic obstacle fallback smoke missed dispatch authority guardrail"
                )
            if "unsupported_observation_claim:fabricated_distance_to_home_m" not in reasons:
                raise SystemExit(
                    "dynamic obstacle fallback smoke missed fabricated observation guardrail"
                )
        if dynamic_obstacle["route_resumed_after_recovery"] is not True:
            raise SystemExit("dynamic obstacle recovery did not resume the route")
        if dynamic_obstacle["recovery_dispatch_request_sent"] is not True:
            raise SystemExit("dynamic obstacle recovery did not dispatch recovery goal")
        if (
            dynamic_obstacle["recovery_completion_claimed"] is not True
            and not decision_demo_smoke
        ):
            raise SystemExit("dynamic obstacle recovery did not complete recovery goal")
        if dynamic_obstacle["obstacle_trajectory_clearance_observed"] is not True:
            raise SystemExit("dynamic obstacle recovery lacked trajectory clearance")
        if dynamic_obstacle["obstacle_trajectory_intersects_obstacle"] is not False:
            raise SystemExit("dynamic obstacle recovery trajectory intersected obstacle")
        if dynamic_obstacle["route_completed_after_recovery"] is True:
            if dynamic_obstacle["completion_claimed"] is not True:
                raise SystemExit(
                    "dynamic obstacle completed route without sim completion claim"
                )
            if dynamic_obstacle["obstacle_avoidance_completion_claimed"] is not True:
                raise SystemExit(
                    "dynamic obstacle completed route without obstacle claim"
                )
            if dynamic_obstacle["indoor_delivery_route_completion_claimed"] is not True:
                raise SystemExit("dynamic obstacle recovery did not reach simulated dropoff")
        elif result["recovery_guardrail_fallback_injection_enabled"] is True:
            if dynamic_obstacle["completion_claimed"] is not False:
                raise SystemExit(
                    "dynamic obstacle fallback claimed completion after route block"
                )
            if dynamic_obstacle["indoor_delivery_route_completion_claimed"] is not False:
                raise SystemExit(
                    "dynamic obstacle fallback claimed delivery after route block"
                )
        elif decision_demo_smoke:
            if dynamic_obstacle["completion_claimed"] is not False:
                raise SystemExit("decision demo claimed completion after route block")
            if dynamic_obstacle["indoor_delivery_route_completion_claimed"] is not False:
                raise SystemExit("decision demo claimed delivery after route block")
            if dynamic_obstacle["mission_episode_review_status"] not in {
                "blocked",
                "passed",
            }:
                raise SystemExit("decision demo did not attach bounded episode review")
        else:
            raise SystemExit("dynamic obstacle recovery did not complete after resume")
        if dynamic_obstacle["mission_delivery_completion_claimed"] is not False:
            raise SystemExit("dynamic obstacle recovery claimed delivery completion")
        if dynamic_obstacle["physical_execution_invoked"] is not False:
            raise SystemExit("dynamic obstacle recovery claimed physical execution")
        if (
            dynamic_obstacle["mission_episode_review_status"] != "passed"
            and not decision_demo_smoke
        ):
            raise SystemExit(
                "dynamic obstacle recovery did not attach passing episode review"
            )
    if _truthy_env(WITH_BRIDGE_ENV):
        if result["decision_demo_smoke_enabled"]:
            decision_demo = result["decision_demo"]
            decision_demo_ok = (
                decision_demo.get("enabled") is True
                and decision_demo.get("judgment_required") is True
                and int(decision_demo.get("llm_recovery_judgment_count") or 0) >= 1
                and decision_demo.get("mission_delivery_completion_claimed") is False
                and decision_demo.get("physical_execution_invoked") is False
            )
            if result["human_approval_demo_smoke_enabled"]:
                decision_demo_ok = (
                    decision_demo_ok
                    and decision_demo.get("fresh_recovery_operator_approval_count") == 1
                    and decision_demo.get("rules_execution_class")
                    == "requires_human_approval"
                    and decision_demo.get("requires_new_human_approval") is True
                    and decision_demo.get("recovery_dispatch_authority_source")
                    == "fresh_operator_approval"
                )
            else:
                decision_demo_ok = (
                    decision_demo_ok
                    and decision_demo.get("fresh_recovery_operator_approval_count") == 0
                )
            return 0 if decision_demo_ok else 2
        return 0 if result["completion_claimed"] is True else 2
    return 0 if result["dispatch_request_sent"] is False else 2


if __name__ == "__main__":
    raise SystemExit(main())

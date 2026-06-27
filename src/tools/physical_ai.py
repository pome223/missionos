"""Physical AI adapters for simulation-first validation flows."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import httpx
from google.adk.agents.context import Context as ToolContext

from src.computer_use.trajectory_store import get_computer_trajectory_store
from src.config.settings import get_settings
from src.physical_ai.runtime_schema import (
    PhysicalMissionContract,
    SafetyGovernorDecisionValue,
    build_action_envelope,
    build_physical_mission_contract,
    build_physical_replay_plan,
    build_physical_verifier_result,
    build_safety_governor_decision,
)
from src.physical_ai.validation_store import (
    get_physical_ai_validation_store,
    reset_physical_ai_validation_store,
)
from src.intelligence.real_hardware_arm_disarm_planner import (
    REAL_HARDWARE_ARM_DISARM_RESPONSE_KIND,
    build_real_hardware_arm_disarm_prompt,
)
from src.security.audit import AuditEventType, get_audit_logger
from src.tools.context import resolve_tool_context
from src.tools.tasks import create_task_record, update_task_record


def reset_physical_ai_validation_runs() -> None:
    get_physical_ai_validation_store().clear()
    reset_physical_ai_validation_store()


async def physical_ai_prepare_real_hardware_arm_disarm_proposal(
    operator_instruction: str,
    serial_device: Optional[str] = None,
    props_removed: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Prepare the ADK proposal contract for a real-hardware arm/disarm bench.

    This is deliberately proposal-only. It does not approve, register dispatch
    authority, call MAVLink, or touch hardware; Gateway remains the only path to
    approval + executor dispatch.
    """

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    prompt = build_real_hardware_arm_disarm_prompt(
        bench_context={
            "props_removed": bool(props_removed),
            "serial_device": serial_device,
        },
        operator_instruction={
            "text": operator_instruction,
            "source": ctx.get("user_id") or "missionos_operator",
        },
    )
    return {
        "success": True,
        "tool_kind": "missionos_real_hardware_arm_disarm_proposal_contract.v1",
        "response_kind": REAL_HARDWARE_ARM_DISARM_RESPONSE_KIND,
        "proposal_only": True,
        "requires_human_approval": True,
        "dispatch_authority_created": False,
        "operator_approved": False,
        "physical_execution_invoked": False,
        "flight_execution_invoked": False,
        "gateway_route": "/missionos/real-hardware-arm-disarm-dispatch/run",
        "prompt_contract": prompt,
    }


def _adapter_url(adapter: str) -> str | None:
    settings = get_settings()
    if adapter == "isaac_sim":
        return settings.physical_ai_isaac_sim_url
    if adapter == "osmo":
        return settings.physical_ai_osmo_url
    return None


def _adapter_status_url(adapter: str) -> str | None:
    settings = get_settings()
    if adapter == "isaac_sim":
        return settings.physical_ai_isaac_sim_status_url
    if adapter == "osmo":
        return settings.physical_ai_osmo_status_url
    return None


async def _post_adapter_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.physical_ai_timeout_seconds) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


def _record_validation_run(run: dict[str, Any]) -> None:
    get_physical_ai_validation_store().upsert(run)


def _validation_status_payload(run_id: str) -> dict[str, Any] | None:
    return get_physical_ai_validation_store().get(run_id)


def _simulation_contract(
    *,
    workflow: str,
    scenario: str,
    robot: str | None,
    task: str | None,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    existing = parameters.get("mission_contract")
    if isinstance(existing, dict) and existing:
        return PhysicalMissionContract.model_validate(existing).model_dump(mode="json")
    contract_id = f"mission_{workflow}_{scenario}".replace("/", "_").replace(" ", "_")
    return build_physical_mission_contract(
        contract_id=contract_id,
        objective_type="simulation_validation",
        objective_target=scenario or workflow,
        workflow=workflow,
        scenario=scenario,
        robot=robot,
        task=task,
        additional_allowed_actions=["validate_replay"],
        additional_completion_criteria=["simulation_validated"],
        metadata={"validation_mode": "simulation_first"},
    ).model_dump(mode="json")


def _validation_mission_contract(validation: dict[str, Any], validation_run_id: str) -> PhysicalMissionContract:
    existing = validation.get("mission_contract")
    if isinstance(existing, dict) and existing:
        return PhysicalMissionContract.model_validate(existing)
    return build_physical_mission_contract(
        contract_id=f"mission_{validation_run_id}",
        objective_type="simulation_validation",
        objective_target=str(validation.get("scenario") or validation_run_id),
        workflow=str(validation.get("workflow") or "simulation_validation"),
        scenario=str(validation.get("scenario") or validation_run_id),
        robot=str(validation.get("robot") or ""),
        task=str(validation.get("task") or ""),
    )


def _attempt_error(attempt: dict[str, Any]) -> str | None:
    result = attempt.get("result") if isinstance(attempt, dict) else None
    if isinstance(result, dict):
        error = str(result.get("error") or "").strip()
        if error:
            return error
    verification = attempt.get("verification") if isinstance(attempt, dict) else None
    if isinstance(verification, dict) and not verification.get("success"):
        return f"verification {verification.get('status', 'failed')}"
    return None


def _trajectory_context(trajectory: dict[str, Any]) -> dict[str, Any]:
    attempts = trajectory.get("attempts") or []
    attempt_summaries: list[dict[str, Any]] = []
    failure_reason: str | None = None

    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        summary = {
            "surface": attempt.get("surface"),
            "strategy": attempt.get("strategy"),
            "success": bool(attempt.get("success")),
        }
        error = _attempt_error(attempt)
        if error:
            summary["error"] = error
            failure_reason = failure_reason or error
        verification = attempt.get("verification")
        if isinstance(verification, dict):
            summary["verification_status"] = verification.get("status")
        attempt_summaries.append(summary)

    verification = trajectory.get("verification") or {}
    context = {
        "id": trajectory.get("id"),
        "action": trajectory.get("action"),
        "status": trajectory.get("status"),
        "final_surface": trajectory.get("final_surface"),
        "attempt_count": len(attempt_summaries),
        "verification_status": verification.get("status"),
        "request": trajectory.get("request") or {},
        "observation": trajectory.get("observation") or {},
        "attempts": attempt_summaries,
    }
    if failure_reason:
        context["failure_reason"] = failure_reason
    return context


async def _refresh_validation_run(validation: dict[str, Any]) -> dict[str, Any] | None:
    status_url = _adapter_status_url(str(validation.get("adapter") or ""))
    if not status_url:
        return None

    response = await _post_adapter_json(
        status_url,
        {
            "operation": "status",
            "run_id": validation["run_id"],
            "workflow": validation.get("workflow"),
            "scenario": validation.get("scenario"),
            "robot": validation.get("robot"),
            "task": validation.get("task"),
        },
    )
    mission_contract = _validation_mission_contract(validation, str(validation["run_id"]))
    verifier_result = build_physical_verifier_result(
        response,
        validation_run_id=str(validation["run_id"]),
        mission_contract_id=mission_contract.contract_id,
    ).model_dump(mode="json")
    refreshed = {
        **validation,
        "status": str(response.get("status") or validation.get("status") or "queued"),
        "validated": _is_validated_response(response),
        "response": response,
        "mission_contract": mission_contract.model_dump(mode="json"),
        "telemetry_health": verifier_result.get("telemetry_health") or {},
        "verifier_result": verifier_result,
    }
    _record_validation_run(refreshed)
    return refreshed


def _is_validated_response(response: dict[str, Any]) -> bool:
    if response.get("validated") is True:
        return True
    validation_status = str(response.get("validation_status") or "").strip().lower()
    if validation_status in {"pass", "validated"}:
        return True
    status = str(response.get("status") or "").strip().lower()
    return status in {"pass", "validated"}


def _ros2_topics(namespace: str, action_name: str) -> dict[str, str]:
    prefix = f"/{namespace.strip('/')}/{action_name.strip('/')}".replace("//", "/")
    return {
        "send_goal": f"{prefix}/_action/send_goal",
        "feedback": f"{prefix}/_action/feedback",
        "get_result": f"{prefix}/_action/get_result",
        "cancel_goal": f"{prefix}/_action/cancel_goal",
        "status": f"{prefix}/_action/status",
    }


async def physical_ai_submit_simulation(
    adapter: str,
    workflow: str,
    scenario: str,
    robot: Optional[str] = None,
    task: Optional[str] = None,
    parameters_json: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Submit an Isaac Sim or OSMO simulation run for validation."""

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()

    adapter_name = (adapter or "").strip().lower()
    url = _adapter_url(adapter_name)
    if adapter_name not in {"isaac_sim", "osmo"}:
        return {"success": False, "error": "adapter must be isaac_sim or osmo"}
    if not url:
        return {"success": False, "error": f"{adapter_name} adapter URL is not configured"}

    parameters = json.loads(parameters_json) if parameters_json else {}
    mission_contract = _simulation_contract(
        workflow=workflow,
        scenario=scenario,
        robot=robot,
        task=task,
        parameters=parameters,
    )
    request = {
        "workflow": workflow,
        "scenario": scenario,
        "robot": robot,
        "task": task,
        "parameters": parameters,
        "mission_contract": mission_contract,
        "validation_mode": "simulation_first",
    }
    response = await _post_adapter_json(url, request)
    run_id = str(response.get("run_id") or response.get("id") or f"sim-{uuid.uuid4().hex[:12]}")
    status = str(response.get("status") or "queued")
    validated = _is_validated_response(response)
    verifier_result = build_physical_verifier_result(
        response,
        validation_run_id=run_id,
        mission_contract_id=str(mission_contract.get("contract_id") or ""),
    ).model_dump(mode="json")
    telemetry_health = verifier_result.get("telemetry_health") or {}
    replay_plan = parameters.get("offline_replay_plan") if isinstance(parameters.get("offline_replay_plan"), dict) else {}
    payload = {
        "success": True,
        "adapter": adapter_name,
        "run_id": run_id,
        "status": status,
        "validated": validated,
        "mission_contract": mission_contract,
        "telemetry_health": telemetry_health,
        "verifier_result": verifier_result,
        "replay_plan": replay_plan,
        "response": response,
    }
    _record_validation_run(
        {
            **payload,
            "workflow": workflow,
            "scenario": scenario,
            "robot": robot,
            "task": task,
            "mission_contract": mission_contract,
            "telemetry_health": telemetry_health,
            "verifier_result": verifier_result,
            "replay_plan": replay_plan,
            "created_at": time.time(),
        }
    )
    audit_logger.log(
        event_type=AuditEventType.PHYSICAL_AI,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="physical_ai_submit_simulation",
        resource=run_id,
        result=status,
        metadata={"adapter": adapter_name, "validated": validated, "workflow": workflow},
    )
    return payload


async def physical_ai_validation_status(
    run_id: str,
    refresh: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Return the persisted status for a simulation-first validation run."""

    validation = _validation_status_payload(run_id)
    if validation is None:
        return {"success": False, "error": f"Unknown validation run: {run_id}"}

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()
    refreshed = False
    refresh_error: str | None = None

    if refresh and not validation.get("validated"):
        try:
            refreshed_validation = await _refresh_validation_run(validation)
        except Exception as exc:  # pragma: no cover - guarded by tests via payload
            refresh_error = str(exc)
        else:
            if refreshed_validation is not None:
                validation = refreshed_validation
                refreshed = True

    audit_logger.log(
        event_type=AuditEventType.PHYSICAL_AI,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="physical_ai_validation_status",
        resource=run_id,
        result=str(validation.get("status") or "unknown"),
        metadata={"validated": bool(validation.get("validated")), "refreshed": refreshed},
    )

    payload = {
        "success": True,
        "run_id": run_id,
        "status": validation.get("status"),
        "validated": bool(validation.get("validated")),
        "validation": validation,
        "refreshed": refreshed,
        "mission_contract": validation.get("mission_contract") or {},
        "telemetry_health": validation.get("telemetry_health") or {},
        "verifier_result": validation.get("verifier_result") or {},
        "replay_plan": validation.get("replay_plan") or {},
        "action_envelope": validation.get("action_envelope") or {},
        "governor_decision": validation.get("governor_decision") or {},
    }
    if refresh_error:
        payload["refresh_error"] = refresh_error
    elif refresh and not refreshed and not validation.get("validated") and not _adapter_status_url(str(validation.get("adapter") or "")):
        payload["refresh_skipped_reason"] = "adapter_status_url_not_configured"
    return payload


async def physical_ai_build_ros2_action(
    robot_namespace: str,
    action_name: str,
    action_type: str,
    goal_json: str,
    frame_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build a ROS2-friendly action envelope for downstream adapters."""

    goal = json.loads(goal_json)
    action = {
        "namespace": robot_namespace.strip("/") or "robot",
        "action_name": action_name.strip("/"),
        "action_type": action_type,
        "frame_id": frame_id,
        "goal": goal,
    }
    action["topics"] = _ros2_topics(action["namespace"], action["action_name"])
    mission_contract_id = str(goal.get("mission_contract_id") or "")
    action_envelope = build_action_envelope(
        capability=action["action_name"],
        target=goal,
        robot_namespace=action["namespace"],
        frame_id=frame_id,
        validation_run_id=str(goal.get("validation_run_id") or ""),
        mission_contract_id=mission_contract_id,
    ).model_dump(mode="json")
    governor_decision = {
        "decision": SafetyGovernorDecisionValue.REQUIRE_OPERATOR.value,
        "reasons": ["simulation_first_required"],
        "mission_contract_id": mission_contract_id,
    }
    return {
        "success": True,
        "simulation_first_required": True,
        "ros2_action": action,
        "action_envelope": action_envelope,
        "governor_decision": governor_decision,
    }


async def physical_ai_dispatch_ros2_action(
    validation_run_id: str,
    ros2_action_json: str,
    allow_real_hardware: bool = False,
    dry_run: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Dispatch a ROS2 action only after a validated simulation run exists."""

    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    audit_logger = get_audit_logger()

    validation = _validation_status_payload(validation_run_id)
    if validation is None:
        return {"success": False, "error": f"Unknown validation run: {validation_run_id}"}
    mission_contract = _validation_mission_contract(validation, validation_run_id)
    verifier_payload = {
        **(validation.get("response") or {}),
        "status": validation.get("status"),
        "validated": validation.get("validated"),
        "telemetry_health": validation.get("telemetry_health") or {},
        "validation_status": (validation.get("verifier_result") or {}).get("verdict"),
        "failure_type": (validation.get("verifier_result") or {}).get("failure_type"),
    }
    verifier_result = build_physical_verifier_result(
        verifier_payload,
        validation_run_id=validation_run_id,
        mission_contract_id=mission_contract.contract_id,
    )
    governor_decision = build_safety_governor_decision(
        mission_contract=mission_contract,
        telemetry_health=verifier_result.telemetry_health,
        verifier_result=verifier_result,
        allow_real_hardware=allow_real_hardware,
        dry_run=dry_run,
    ).model_dump(mode="json")
    if not validation.get("validated"):
        governor_decision = {
            **governor_decision,
            "decision": SafetyGovernorDecisionValue.REJECT.value,
            "reasons": [
                "simulation_validation_not_passed",
                *[
                    reason
                    for reason in governor_decision.get("reasons", [])
                    if str(reason).strip() != "simulation_validation_not_passed"
                ],
            ],
        }
    ros2_action = json.loads(ros2_action_json)
    action_envelope = build_action_envelope(
        capability=str(ros2_action.get("action_name") or "physical_action"),
        target=ros2_action.get("goal") if isinstance(ros2_action.get("goal"), dict) else {},
        robot_namespace=str(ros2_action.get("namespace") or "robot"),
        frame_id=str(ros2_action.get("frame_id") or "") or None,
        validation_run_id=validation_run_id,
        mission_contract_id=mission_contract.contract_id,
    ).model_dump(mode="json")
    validation = {
        **validation,
        "mission_contract": mission_contract.model_dump(mode="json"),
        "telemetry_health": verifier_result.telemetry_health.model_dump(mode="json"),
        "verifier_result": verifier_result.model_dump(mode="json"),
        "action_envelope": action_envelope,
        "governor_decision": governor_decision,
    }
    _record_validation_run(validation)

    if not validation.get("validated"):
        return {
            "success": False,
            "error": f"Simulation-first validation has not passed for run {validation_run_id}",
            "validation": validation,
            "governor_decision": governor_decision,
        }
    if governor_decision["decision"] == SafetyGovernorDecisionValue.SAFE_MODE.value:
        return {
            "success": False,
            "error": f"Safety governor entered safe_mode for run {validation_run_id}",
            "validation": validation,
            "governor_decision": governor_decision,
        }
    if governor_decision["decision"] == SafetyGovernorDecisionValue.REJECT.value:
        return {
            "success": False,
            "error": f"Safety governor rejected action for run {validation_run_id}",
            "validation": validation,
            "governor_decision": governor_decision,
        }

    dispatch_payload = {
        "validation_run_id": validation_run_id,
        "validation": validation,
        "ros2_action": ros2_action,
        "action_envelope": action_envelope,
        "governor_decision": governor_decision,
    }
    if dry_run or not allow_real_hardware:
        return {
            "success": True,
            "dispatched": False,
            "dry_run": True,
            "dispatch_payload": dispatch_payload,
            "action_envelope": action_envelope,
            "governor_decision": governor_decision,
        }

    settings = get_settings()
    if not settings.physical_ai_ros2_bridge_url:
        return {"success": False, "error": "physical_ai_ros2_bridge_url is not configured"}

    response = await _post_adapter_json(settings.physical_ai_ros2_bridge_url, dispatch_payload)
    audit_logger.log(
        event_type=AuditEventType.PHYSICAL_AI,
        user_id=ctx.get("user_id") or None,
        session_id=ctx.get("session_id") or None,
        action="physical_ai_dispatch_ros2_action",
        resource=validation_run_id,
        result="success",
        metadata={"action_name": ros2_action.get("action_name"), "namespace": ros2_action.get("namespace")},
    )
    return {
        "success": True,
        "dispatched": True,
        "dry_run": False,
        "response": response,
        "validation_run_id": validation_run_id,
        "action_envelope": action_envelope,
        "governor_decision": governor_decision,
    }


async def physical_ai_replay_computer_trajectory(
    trajectory_id: int,
    adapter: str,
    workflow: str = "computer_use_replay",
    scenario: str = "browser_failure_replay",
    robot: Optional[str] = None,
    task: Optional[str] = None,
    simulation_parameters_json: Optional[str] = None,
    robot_namespace: str = "robot",
    action_name: str = "follow_joint_trajectory",
    action_type: str = "control_msgs/action/FollowJointTrajectory",
    goal_json: str = "{}",
    frame_id: Optional[str] = None,
    allow_real_hardware: bool = False,
    dry_run: bool = True,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Replay a recorded computer-use trajectory into a simulation-first physical AI flow."""

    trajectory = get_computer_trajectory_store().get(trajectory_id)
    if trajectory is None:
        return {"success": False, "error": f"Unknown computer trajectory: {trajectory_id}"}

    trajectory_context = _trajectory_context(trajectory)
    mission_contract = build_physical_mission_contract(
        contract_id=f"mission_replay_{trajectory_id}",
        objective_type="replay_validation",
        objective_target=scenario,
        workflow=workflow,
        scenario=scenario,
        robot=robot,
        task=task,
        additional_allowed_actions=["simulation_replay", "dispatch_validated_action"],
        additional_completion_criteria=["simulation_replay_reviewed"],
        metadata={"source_trajectory_id": trajectory_id, "offline_only": True},
    ).model_dump(mode="json")
    replay_plan = build_physical_replay_plan(
        replay_id=f"physical_replay_{trajectory_id}",
        source_trajectory_id=trajectory_id,
        adapter=adapter,
        workflow=workflow,
        scenario=scenario,
        mission_contract=PhysicalMissionContract.model_validate(mission_contract),
        metadata={"robot": robot or "", "task": task or ""},
    ).model_dump(mode="json")
    task_record = create_task_record(
        kind="physical_ai_replay",
        title=f"Physical replay for trajectory {trajectory_id}",
        status="running",
        artifacts={
            "trajectory": trajectory_context,
            "mission_contract": mission_contract,
            "replay_plan": replay_plan,
        },
        metadata={
            "trajectory_id": trajectory_id,
            "adapter": adapter,
            "workflow": workflow,
            "scenario": scenario,
        },
        tool_context=tool_context,
    )
    task_id = str(task_record["task_id"])
    simulation_parameters = json.loads(simulation_parameters_json) if simulation_parameters_json else {}
    simulation_parameters["computer_trajectory"] = trajectory_context
    simulation_parameters["mission_contract"] = mission_contract
    simulation_parameters["offline_replay_plan"] = replay_plan

    simulation = await physical_ai_submit_simulation(
        adapter=adapter,
        workflow=workflow,
        scenario=scenario,
        robot=robot,
        task=task or f"replay_{trajectory_context['action']}_{trajectory_id}",
        parameters_json=json.dumps(simulation_parameters, ensure_ascii=True),
        tool_context=tool_context,
    )
    if not simulation.get("success"):
        update_task_record(
            task_id,
            status="failed",
            artifacts={
                "mission_contract": mission_contract,
                "replay_plan": replay_plan,
                "simulation": simulation,
            },
            error=simulation.get("error") or "simulation submit failed",
        )
        return {
            "success": False,
            "task_id": task_id,
            "error": simulation.get("error") or "simulation submit failed",
            "trajectory": trajectory_context,
            "mission_contract": mission_contract,
            "replay_plan": replay_plan,
            "simulation": simulation,
        }

    goal = json.loads(goal_json)
    goal["computer_trajectory"] = {
        "id": trajectory_context["id"],
        "status": trajectory_context["status"],
        "action": trajectory_context["action"],
        "final_surface": trajectory_context["final_surface"],
    }
    goal["validation_run_id"] = simulation["run_id"]
    goal["mission_contract_id"] = mission_contract["contract_id"]

    ros2_action = await physical_ai_build_ros2_action(
        robot_namespace=robot_namespace,
        action_name=action_name,
        action_type=action_type,
        goal_json=json.dumps(goal, ensure_ascii=True),
        frame_id=frame_id,
    )

    payload = {
        "success": True,
        "task_id": task_id,
        "trajectory": trajectory_context,
        "mission_contract": mission_contract,
        "replay_plan": replay_plan,
        "simulation": simulation,
        "ros2_action": ros2_action,
    }
    if not simulation.get("validated"):
        payload["dispatch"] = None
        payload["dispatch_skipped_reason"] = "simulation_not_validated"
        update_task_record(
            task_id,
            status="awaiting_validation",
            artifacts={
                "mission_contract": mission_contract,
                "replay_plan": replay_plan,
                "simulation": simulation,
                "ros2_action": ros2_action,
                "dispatch_skipped_reason": payload["dispatch_skipped_reason"],
            },
        )
        return payload

    dispatch = await physical_ai_dispatch_ros2_action(
        validation_run_id=simulation["run_id"],
        ros2_action_json=json.dumps(ros2_action["ros2_action"], ensure_ascii=True),
        allow_real_hardware=allow_real_hardware,
        dry_run=dry_run,
        tool_context=tool_context,
    )
    payload["dispatch"] = dispatch
    payload["success"] = bool(dispatch.get("success"))
    if not dispatch.get("success"):
        payload["error"] = dispatch.get("error") or "dispatch failed"
    update_task_record(
        task_id,
        status="completed" if payload["success"] else "failed",
        artifacts={
            "mission_contract": mission_contract,
            "replay_plan": replay_plan,
            "simulation": simulation,
            "ros2_action": ros2_action,
            "dispatch": dispatch,
        },
        error=payload.get("error"),
    )
    return payload

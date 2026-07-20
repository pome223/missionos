"""Gateway projections and persistence for TurtleBot3 mission tasks.

This module owns no executor.  It can recognize an already-built mission
context, attach a read-only recovery-decision summary, and persist the result
that the runtime returned.  Approval creation and Nav2 dispatch stay outside
this module so moving these helpers out of ``server.py`` cannot widen
authority.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from src.runtime.task_store import TaskStore
from src.runtime.turtlebot3_home_mission import (
    instruction_requests_turtlebot3_home_mission,
)
from src.runtime.turtlebot3_telemetry_sidecar import (
    TURTLEBOT3_LIVE_TASK_ID_PATH_ENV,
)


def _mission_designer_has_proposal(context: Mapping[str, Any]) -> bool:
    proposal = context.get("scenario_proposal")
    validation = context.get("validation_result")
    return isinstance(proposal, Mapping) and isinstance(validation, Mapping)


def missionos_turtlebot3_home_mission_has_proposal(
    context: Mapping[str, Any],
) -> bool:
    return _mission_designer_has_proposal(context) and isinstance(
        context.get("turtlebot3_home_mission_plan"),
        Mapping,
    )


def missionos_turtlebot3_home_mission_approval(
    context: Mapping[str, Any],
) -> dict[str, Any]:
    approval = context.get("turtlebot3_home_mission_approval")
    return dict(approval) if isinstance(approval, Mapping) else {}


def missionos_turtlebot3_home_mission_plan(
    context: Mapping[str, Any],
) -> dict[str, Any]:
    plan = context.get("turtlebot3_home_mission_plan")
    if isinstance(plan, Mapping):
        return dict(plan)
    proposal = context.get("scenario_proposal")
    return dict(proposal) if isinstance(proposal, Mapping) else {}


def missionos_instruction_requests_turtlebot3_home_mission(
    text: str,
    *,
    context: Mapping[str, Any],
) -> bool:
    if instruction_requests_turtlebot3_home_mission(text):
        return True
    if not missionos_turtlebot3_home_mission_has_proposal(context):
        return False
    return instruction_requests_turtlebot3_home_mission(f"TurtleBot3 {text}")


def _recovery_proposal_count(
    proposals: Any,
    *,
    source: str | None = None,
) -> int:
    if not isinstance(proposals, list):
        return 0
    count = 0
    for proposal in proposals:
        if not isinstance(proposal, Mapping):
            continue
        if source is not None and proposal.get("proposal_source") != source:
            continue
        count += 1
    return count


def _recovery_approval_created_count(
    proposals: Any,
    planner_result: Any,
) -> int:
    count = 0
    if isinstance(proposals, list):
        for proposal in proposals:
            if isinstance(proposal, Mapping) and proposal.get("approval_created") is True:
                count += 1
    if isinstance(planner_result, Mapping) and planner_result.get("approval_created") is True:
        count += 1
    return count


def _first_recovery_classification(
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    classifications = summary.get("recovery_proposal_classifications")
    if not isinstance(classifications, list) or not classifications:
        return {}
    first = classifications[0]
    return dict(first) if isinstance(first, Mapping) else {}


def _first_recovery_observation_keys(proposals: Any) -> list[str]:
    if not isinstance(proposals, list) or not proposals:
        return []
    first = proposals[0]
    if not isinstance(first, Mapping):
        return []
    observations = first.get("input_observations")
    if not isinstance(observations, Mapping):
        return []
    return sorted(str(key) for key in observations)


def _guardrail_blocked_llm_output_count(planner_result: Any) -> int:
    if not isinstance(planner_result, Mapping):
        return 0
    if planner_result.get("planner_status") != "guardrail_blocked":
        return 0
    guardrail = planner_result.get("guardrail")
    if not isinstance(guardrail, Mapping):
        return 0
    return 1 if guardrail.get("guardrail_passed") is False else 0


def _recovery_decision_trigger(summary: Mapping[str, Any]) -> str:
    if summary.get("runtime_failure_recovery_triggered") is True:
        return "runtime_segment_failure"
    if summary.get("runtime_recovery_triggered") is True:
        action = str(
            summary.get("runtime_recovery_action_kind")
            or summary.get("recovery_action_suggested")
            or ""
        )
        if action == "avoid_obstacle":
            return "runtime_obstacle"
        if action == "return_home":
            return "battery_or_failure_envelope"
        return "runtime_recovery"
    return "not_required"


def missionos_turtlebot3_recovery_decision_summary(
    execution_result: Mapping[str, Any],
    *,
    mission_operator_approval_count: int = 1,
) -> dict[str, Any]:
    """Build a read-only artifact; it never creates dispatch authority."""

    summary = execution_result.get("summary")
    summary = dict(summary) if isinstance(summary, Mapping) else {}
    proposals = summary.get("recovery_proposals")
    proposals = proposals if isinstance(proposals, list) else []
    planner_result = summary.get("recovery_planner_result")
    planner_result = dict(planner_result) if isinstance(planner_result, Mapping) else {}
    classification = _first_recovery_classification(summary)
    fresh_approval_count = int(summary.get("fresh_recovery_operator_approval_count") or 0)
    proposal_approval_count = _recovery_approval_created_count(
        proposals,
        planner_result,
    )
    recovery_dispatch_request_sent = summary.get("recovery_dispatch_request_sent")
    selected_action = summary.get("runtime_recovery_action_kind") or summary.get(
        "recovery_action_suggested"
    )
    payload = {
        "schema_version": "missionos_turtlebot3_recovery_decision_summary.v1",
        "artifact_kind": "turtlebot3_recovery_decision_summary",
        "summary_source": "turtlebot3_home_mission_execution.summary",
        "read_only": True,
        "judgment_required": summary.get("runtime_recovery_triggered") is True,
        "trigger": _recovery_decision_trigger(summary),
        "accepted_recovery_proposal_count": _recovery_proposal_count(proposals),
        "llm_recovery_judgment_count": _recovery_proposal_count(
            proposals,
            source="llm",
        ),
        "deterministic_fallback_count": _recovery_proposal_count(
            proposals,
            source="deterministic_fallback",
        ),
        "guardrail_blocked_llm_output_count": (_guardrail_blocked_llm_output_count(planner_result)),
        "recovery_proposal_source": (
            str(proposals[0].get("proposal_source")) if proposals else None
        ),
        "source_backed_input_observation_keys": (_first_recovery_observation_keys(proposals)),
        "selected_action": selected_action,
        "rules_execution_class": classification.get("execution_class"),
        "requires_new_human_approval": classification.get("requires_new_human_approval"),
        "execution_permitted_by_envelope": classification.get("execution_permitted_by_envelope"),
        "proposal_allowed": classification.get("proposal_allowed"),
        "mission_operator_approval_count": mission_operator_approval_count,
        "proposal_approval_created_count": proposal_approval_count,
        "fresh_recovery_operator_approval_count": fresh_approval_count,
        "fresh_recovery_operator_approvals": summary.get("fresh_recovery_operator_approvals") or [],
        "operator_approval_created_for_recovery": fresh_approval_count > 0,
        "operator_approval_reused_for_recovery": (
            recovery_dispatch_request_sent is True
            and mission_operator_approval_count == 1
            and fresh_approval_count == 0
        ),
        "recovery_execution_permitted_by_operator_approval": summary.get(
            "recovery_execution_permitted_by_operator_approval"
        ),
        "recovery_dispatch_authority_source": summary.get("recovery_dispatch_authority_source"),
        "recovery_dispatch_request_sent": recovery_dispatch_request_sent,
        "recovery_completion_claimed": summary.get("recovery_completion_claimed"),
        "route_resumed_after_recovery": summary.get("route_resumed_after_recovery"),
        "route_completed_after_recovery": summary.get("route_completed_after_recovery"),
        "runtime_failure_recovery_triggered": summary.get("runtime_failure_recovery_triggered"),
        "runtime_failure_context": summary.get("runtime_failure_context") or {},
        "runtime_motion_context": summary.get("runtime_recovery_motion_context") or {},
        "recovery_planner_status": summary.get("recovery_planner_status"),
        "completion_scope": summary.get("completion_scope"),
        "completion_claimed": summary.get("completion_claimed"),
        "mission_delivery_completion_claimed": summary.get("mission_delivery_completion_claimed"),
        "physical_execution_invoked": summary.get("physical_execution_invoked"),
        "decision_summary_creates_dispatch_authority": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    payload["decision_summary_id"] = f"turtlebot3_recovery_decision_summary_{digest}"
    payload["decision_summary_ref"] = (
        f"turtlebot3_recovery_decision_summary:turtlebot3_recovery_decision_summary_{digest}"
    )
    return payload


def missionos_attach_turtlebot3_recovery_decision_summary(
    execution_result: Mapping[str, Any],
    *,
    mission_operator_approval_count: int = 1,
) -> dict[str, Any]:
    result = dict(execution_result)
    decision_summary = missionos_turtlebot3_recovery_decision_summary(
        result,
        mission_operator_approval_count=mission_operator_approval_count,
    )
    result["turtlebot3_recovery_decision_summary"] = decision_summary
    summary = result.get("summary")
    if isinstance(summary, Mapping):
        result["summary"] = {
            **dict(summary),
            "turtlebot3_recovery_decision_summary_ref": decision_summary["decision_summary_ref"],
        }
    return result


def bind_turtlebot3_live_telemetry_task(task_id: str) -> None:
    """Atomically bind display-only live telemetry to its owning task."""

    raw_path = os.environ.get(TURTLEBOT3_LIVE_TASK_ID_PATH_ENV, "").strip()
    if not raw_path:
        return
    path = Path(raw_path)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(f"{task_id}\n", encoding="utf-8")
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def create_running_turtlebot3_home_mission_task(
    *,
    task_store: TaskStore,
    session_id: str,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    """Create the task before dispatch so read-only surfaces can poll it."""

    robot_label = str(proposal.get("robot_label") or "TurtleBot3")
    execution_target = str(proposal.get("execution_target") or "ros2_nav2_turtlebot3_sim")
    task = task_store.create(
        kind="turtlebot3_home_mission_execution",
        title=f"{robot_label} Nav2 simulator mission",
        status="running",
        owner_session_id=session_id or None,
        artifacts={
            "turtlebot3_home_mission_plan": dict(proposal),
            "turtlebot3_home_mission_approval": dict(approval),
            "summary": {
                "status": "running",
                "robot_profile": proposal.get("robot_profile") or "turtlebot3",
                "robot_label": robot_label,
                "robot_model": proposal.get("robot_model"),
                "execution_target": execution_target,
                "runtime_substrate": proposal.get("runtime_substrate"),
                "runtime_profile": proposal.get("runtime_profile"),
                "completion_claimed": False,
                "completion_scope": "none",
                "physical_execution_invoked": False,
                "mission_delivery_completion_claimed": False,
            },
        },
        metadata={
            "source": "missionos_autonomy_conversation_execute",
            "robot_profile": proposal.get("robot_profile") or "turtlebot3",
            "robot_label": robot_label,
            "execution_target": execution_target,
            "execution_mode": "sim",
            "physical_execution_invoked": False,
            "mission_delivery_completion_claimed": False,
        },
    )
    bind_turtlebot3_live_telemetry_task(str(task["task_id"]))
    return dict(task)


def _task_metadata(
    summary: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    *,
    awaiting_recovery_approval: bool,
) -> dict[str, Any]:
    return {
        "home_robot_mission_kind": summary.get("home_robot_mission_kind"),
        "robot_profile": summary.get("robot_profile") or "turtlebot3",
        "robot_label": summary.get("robot_label") or "TurtleBot3",
        "execution_target": (summary.get("execution_target") or "ros2_nav2_turtlebot3_sim"),
        "execution_mode": "sim",
        "completion_claimed": summary.get("completion_claimed") is True,
        "completion_scope": summary.get("completion_scope"),
        "turtlebot3_recovery_lifecycle": (
            "awaiting_operator_approval"
            if awaiting_recovery_approval
            else checkpoint.get("checkpoint_status") or "not_pending"
        ),
        "read_only_map_available": isinstance(
            summary.get("turtlebot3_indoor_map_model"),
            Mapping,
        ),
    }


def create_turtlebot3_home_mission_task(
    *,
    task_store: TaskStore,
    execution_result: Mapping[str, Any],
    session_id: str,
    existing_task_id: str | None = None,
) -> dict[str, Any]:
    summary = execution_result.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    result_status = str(summary.get("status") or "completed")
    checkpoint = execution_result.get("turtlebot3_recovery_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
    awaiting_recovery_approval = checkpoint.get("checkpoint_status") == "awaiting_operator_approval"
    status = "pending" if awaiting_recovery_approval else result_status
    task_artifacts = dict(execution_result)
    checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
    if checkpoint_id:
        task_artifacts["turtlebot3_recovery_checkpoints"] = {checkpoint_id: dict(checkpoint)}
    metadata = _task_metadata(
        summary,
        checkpoint,
        awaiting_recovery_approval=awaiting_recovery_approval,
    )
    if existing_task_id:
        updated = task_store.update(
            existing_task_id,
            status=status,
            artifacts=task_artifacts,
            metadata=metadata,
        )
        if isinstance(updated, dict):
            bind_turtlebot3_live_telemetry_task(str(updated["task_id"]))
            return dict(updated)
    task = task_store.create(
        kind="turtlebot3_home_mission_execution",
        title=f"{summary.get('robot_label') or 'TurtleBot3'} Nav2 simulator mission",
        status=status,
        owner_session_id=session_id or None,
        artifacts=task_artifacts,
        metadata={
            "source": "missionos_autonomy_conversation_execute",
            **metadata,
            "physical_execution_invoked": False,
            "mission_delivery_completion_claimed": False,
        },
    )
    bind_turtlebot3_live_telemetry_task(str(task["task_id"]))
    return dict(task)

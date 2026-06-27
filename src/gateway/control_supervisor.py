"""Opt-in long-running supervisor for repeated control-loop maintenance runs."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from src.control_loop.live_failure_taxonomy import classify_control_loop_failure
from src.control_loop.root_workflow import ExecutionResult
from src.runtime.durable_execution_schema import (
    CheckpointBudget,
    DurableArtifactRef,
    DurableCheckpoint,
    DurableEscalationRecord,
    DurableJobRun,
    DurableResumeState,
    DurableTaskGraph,
    DurableTaskNode,
    DurableTaskNodeStatus,
    DurableVerifierVerdict,
    DurableVerifierVerdictValue,
    GuardrailBudgetPolicy,
    RecoveryActionType,
    RecoveryBudgetImpact,
    RecoveryDecision,
    RecoveryLadderStep,
    SchedulerQueueEntry,
    SchedulerQueueKind,
    SchedulerQueueState,
)
from src.runtime.mission_contract import (
    MissionAbortConditionType,
    MissionContract,
    normalize_mission_contract,
)
from src.runtime.mission_reuse import build_mission_reuse_plan
from src.runtime.orchestration_policy import (
    append_scheduler_queue_entry,
    budget_exhaustion_reasons,
    build_escalation_record,
    default_guardrail_budget_policy,
    default_recovery_policies,
    job_run_status_from_verdict,
    recovery_action_for_ladder_step,
    recovery_ladder_step_for_decision,
    recovery_outcome_for_queue,
    recovery_policy_for_failure_type,
    repair_depth_increment,
    scheduler_queue_for_recovery_ladder_step,
    scheduler_queue_reason,
    select_recovery_ladder_step,
)
from src.runtime.mission_runtime import (
    build_mission_scorecard,
    build_post_mission_review_artifacts,
)
from src.tools.tasks import (
    append_task_event_record,
    create_task_record,
    update_task_record,
)

_SUPERVISOR_AGENT_NAME = "control_supervisor"


def _abort_condition_type_values(mission_contract: MissionContract) -> list[str]:
    return [condition.type.value for condition in mission_contract.abort_conditions]


def _abort_condition_payloads(mission_contract: MissionContract) -> list[dict[str, Any]]:
    return [
        condition.model_dump(mode="json")
        for condition in mission_contract.abort_conditions
    ]


def _abort_condition_metadata(mission_contract: MissionContract) -> dict[str, Any]:
    return {
        "abort_conditions": _abort_condition_payloads(mission_contract),
        "abort_condition_types": _abort_condition_type_values(mission_contract),
    }


def _format_mission_contract_section(mission_contract: MissionContract | None) -> str:
    if mission_contract is None:
        return ""

    lines = [
        "Mission contract:",
        f"- contract_id: {mission_contract.contract_id}",
        f"- objective: {mission_contract.objective}",
    ]
    sections = [
        ("allowed_actions", mission_contract.allowed_actions),
        ("forbidden_actions", mission_contract.forbidden_actions),
        ("abort_conditions", _abort_condition_type_values(mission_contract)),
        ("completion_criteria", mission_contract.completion_criteria),
        ("evidence_requirements", mission_contract.evidence_requirements),
        ("success_metrics", mission_contract.success_metrics),
    ]
    for label, values in sections:
        if values:
            lines.append(f"- {label}: {', '.join(values)}")
    return "\n".join(lines)


def _append_mission_contract_section(
    goal: str,
    mission_contract: MissionContract | None,
) -> str:
    section = _format_mission_contract_section(mission_contract)
    if not section:
        return goal
    return f"{goal.rstrip()}\n\n{section}"


def build_maintenance_goal(
    objective: str,
    mission_contract: MissionContract | None = None,
) -> str:
    normalized = str(objective or "").strip()
    if not normalized:
        raise ValueError("objective is required")
    goal = (
        "Maintain the following long-running objective for the active session.\n"
        f"Objective: {normalized}\n\n"
        "Inspect the current state, keep the objective satisfied, and perform only the "
        "next minimal action required. If the objective already looks healthy, prefer "
        "verification over disruptive changes."
    )
    return _append_mission_contract_section(goal, mission_contract)


RunControlLoopWithTaskFn = Callable[..., Awaitable[tuple[ExecutionResult, str]]]
EmitSessionEventFn = Callable[..., Awaitable[None]]
TaskCreateFn = Callable[..., dict[str, Any]]
TaskUpdateFn = Callable[..., dict[str, Any] | None]
TaskAppendEventFn = Callable[..., dict[str, Any] | None]


def _utc_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)


def _datetime_timestamp(value: datetime | str | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _supervisor_node_id(control_session_id: str) -> str:
    return f"{control_session_id}/maintain-objective"


def _mission_graph_node_id(control_session_id: str, contract_node_id: str) -> str:
    normalized = str(contract_node_id or "").strip()
    if not normalized:
        return _supervisor_node_id(control_session_id)
    if normalized.startswith(f"{control_session_id}/"):
        return normalized
    return f"{control_session_id}/{normalized}"


def _queue_entry_for_node(
    *,
    node_id: str,
    queue: SchedulerQueueKind,
    checkpoint_id: str | None,
    available_at: datetime | None,
    failure_type: str | None,
    chosen_action: RecoveryActionType | None,
    mission_contract: MissionContract | None = None,
    escalation_id: str | None = None,
) -> SchedulerQueueEntry:
    metadata: dict[str, Any] = {
        "failure_type": str(failure_type or ""),
        "chosen_action": chosen_action.value if chosen_action is not None else "",
    }
    if mission_contract is not None:
        metadata["mission_contract_id"] = mission_contract.contract_id
        metadata.update(_abort_condition_metadata(mission_contract))
    return SchedulerQueueEntry(
        entry_id=f"{node_id}/queue",
        node_id=node_id,
        queue=queue,
        checkpoint_id=checkpoint_id,
        available_at=available_at,
        escalation_id=escalation_id,
        metadata=metadata,
    )


def _available_at_for_queue(
    queue: SchedulerQueueKind,
    *,
    next_run_at: float | None,
) -> datetime | None:
    if next_run_at is None or queue not in {
        SchedulerQueueKind.RETRY_LATER,
        SchedulerQueueKind.PERIODIC_CHECK,
    }:
        return None
    return _utc_datetime(next_run_at)


def _task_node_status_for_queue(
    queue: SchedulerQueueKind,
) -> DurableTaskNodeStatus:
    if queue == SchedulerQueueKind.COMPLETED:
        return DurableTaskNodeStatus.DONE
    if queue in {
        SchedulerQueueKind.WAITING_FOR_APPROVAL,
        SchedulerQueueKind.BLOCKED,
    }:
        return DurableTaskNodeStatus.BLOCKED
    if queue == SchedulerQueueKind.RETRY_LATER:
        return DurableTaskNodeStatus.FAILED
    return DurableTaskNodeStatus.READY


def _verdict_for_result(
    *,
    result: ExecutionResult,
    failure_type: str | None,
) -> DurableVerifierVerdictValue:
    if str(failure_type or "") == "weak_evidence":
        return DurableVerifierVerdictValue.UNCERTAIN
    if result.success:
        return DurableVerifierVerdictValue.PASS
    return DurableVerifierVerdictValue.FAIL


def _artifact_refs_for_result(
    *,
    child_task_id: str,
    result: ExecutionResult,
    failure_type: str | None,
    mission_contract: MissionContract,
) -> list[DurableArtifactRef]:
    verification_inputs = (
        result.metadata.get("verification_inputs")
        if isinstance(result.metadata.get("verification_inputs"), dict)
        else {}
    )
    current_tab = (
        verification_inputs.get("current_tab")
        if isinstance(verification_inputs.get("current_tab"), dict)
        else {}
    )
    refs = [
        _mission_contract_artifact_ref(mission_contract),
        DurableArtifactRef(
            kind="task",
            ref=child_task_id,
            label=f"child-task:{child_task_id}",
            metadata={"task_id": child_task_id},
        )
    ]
    if current_tab.get("info_succeeded"):
        refs.append(
            DurableArtifactRef(
                kind="current_tab_info",
                ref=f"{child_task_id}#verification_inputs.current_tab",
                label="current_tab_info",
                metadata={
                    key: current_tab.get(key)
                    for key in ("url", "title", "tab_id", "window_id")
                    if current_tab.get(key) is not None
                },
            )
        )
    if result.verification_report_id:
        refs.append(
            DurableArtifactRef(
                kind="verification_report",
                ref=result.verification_report_id,
                label="verification_report",
                metadata={"failure_type": failure_type},
            )
        )
    approval_request = result.metadata.get("approval_request")
    if isinstance(approval_request, dict) and str(approval_request.get("request_id") or "").strip():
        refs.append(
            DurableArtifactRef(
                kind="approval_request",
                ref=str(approval_request["request_id"]),
                label="approval_request",
                metadata={"plan_id": approval_request.get("plan_id")},
            )
        )
    return refs


def _mission_contract_artifact_ref(
    mission_contract: MissionContract,
) -> DurableArtifactRef:
    return DurableArtifactRef(
        kind="mission_contract",
        ref=mission_contract.contract_id,
        label="mission_contract",
        metadata={
            "schema_version": mission_contract.schema_version,
            "objective": mission_contract.objective,
            "success_metrics": list(mission_contract.success_metrics),
            "completion_criteria": list(mission_contract.completion_criteria),
            "evidence_requirements": list(mission_contract.evidence_requirements),
        },
    )


def _build_live_verifier_verdict(
    *,
    result: ExecutionResult,
    failure_type: str | None,
    child_task_id: str,
    created_at: float,
) -> DurableVerifierVerdict:
    verdict = _verdict_for_result(result=result, failure_type=failure_type)
    confidence = 0.95 if verdict == DurableVerifierVerdictValue.PASS else (
        0.45 if verdict == DurableVerifierVerdictValue.UNCERTAIN else 0.8
    )
    report = result.metadata.get("verification_report")
    evidence_refs = []
    if isinstance(report, dict):
        refs = report.get("artifact_refs") or report.get("evidence_refs") or []
        if isinstance(refs, list):
            evidence_refs = [str(item).strip() for item in refs if str(item).strip()]
    verification_inputs = (
        result.metadata.get("verification_inputs")
        if isinstance(result.metadata.get("verification_inputs"), dict)
        else {}
    )
    current_tab = (
        verification_inputs.get("current_tab")
        if isinstance(verification_inputs.get("current_tab"), dict)
        else {}
    )
    if current_tab.get("info_succeeded"):
        evidence_refs.append(f"{child_task_id}#verification_inputs.current_tab")
    if result.verification_report_id:
        evidence_refs.append(f"verification_report:{result.verification_report_id}")
    evidence_refs = list(dict.fromkeys(evidence_refs))
    replay_reference = {"child_task_id": child_task_id}
    if result.verification_report_id:
        replay_reference["verification_report_id"] = result.verification_report_id
    return DurableVerifierVerdict(
        verdict=verdict,
        evidence_refs=evidence_refs,
        failure_type=failure_type,
        confidence=confidence,
        confidence_source="synthetic_default",
        verifier_source="control_supervisor_phase0",
        recommended_repair_target=(str(result.final_text or "").strip() or None),
        replay_reference=replay_reference,
        created_at=_utc_datetime(created_at),
    )


def _recovery_source_refs(
    *,
    child_task_id: str,
    result: ExecutionResult,
    verifier_verdict: DurableVerifierVerdict,
) -> list[str]:
    refs: list[str] = []

    def _add_ref(value: object) -> None:
        ref = str(value or "").strip()
        if ref and ref not in refs:
            refs.append(ref)

    _add_ref(f"task:{child_task_id}")
    for ref in verifier_verdict.evidence_refs:
        _add_ref(ref)
    if result.verification_report_id:
        _add_ref(f"verification_report:{result.verification_report_id}")
    if str(result.metadata.get("error") or "").strip():
        _add_ref(f"runtime_error:{child_task_id}")
    return refs


def _recovery_budget_snapshot(
    *,
    runtime_state: dict[str, Any],
    runtime_hours_used: int,
    retry_count: int,
    pending_approvals_count: int,
    budget_policy: GuardrailBudgetPolicy,
    mission_contract: MissionContract,
) -> dict[str, Any]:
    recovery_policy = getattr(mission_contract, "recovery_policy", None)
    max_retries_per_step = int(
        getattr(recovery_policy, "max_retries_per_step", 0) or 0
    )
    return {
        "runtime_hours_used": runtime_hours_used,
        "llm_calls_used": runtime_state["llm_calls_used"],
        "tool_calls_used": runtime_state["tool_calls_used"],
        "repair_depth_used": runtime_state["repair_depth_used"],
        "pending_approvals_count": pending_approvals_count,
        "same_failure_retry_count": retry_count,
        "retry_budget_remaining": max(
            0,
            budget_policy.max_same_failure_retries - retry_count,
        ),
        "max_same_failure_retries": budget_policy.max_same_failure_retries,
        "max_retries_per_step": max_retries_per_step,
    }


@dataclass(frozen=True)
class SupervisorStartResult:
    task: dict[str, Any]
    control_session_id: str
    max_iterations: int
    ends_at: float
    next_run_at: float
    mission_contract: dict[str, Any] | None = None
    reuse_plan: dict[str, Any] | None = None


@dataclass
class _SupervisorHandle:
    task_id: str
    owner_session_id: str
    user_id: str
    stop_requested: asyncio.Event
    task: asyncio.Task[None]


@dataclass(frozen=True)
class _SchedulerSelection:
    entry: SchedulerQueueEntry | None
    skipped_entries: list[dict[str, Any]]


@dataclass(frozen=True)
class SupervisorWatchdogFinding:
    task_id: str
    status: str
    reason: str
    action: str
    last_heartbeat_at: float | None
    stale_after_seconds: float
    has_active_handle: bool


class ControlLoopSupervisor:
    def __init__(
        self,
        *,
        run_control_loop_with_task: RunControlLoopWithTaskFn,
        emit_session_event: EmitSessionEventFn,
        create_task_record_fn: TaskCreateFn = create_task_record,
        update_task_record_fn: TaskUpdateFn = update_task_record,
        append_task_event_record_fn: TaskAppendEventFn = append_task_event_record,
        budget_policy: GuardrailBudgetPolicy | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._run_control_loop_with_task = run_control_loop_with_task
        self._emit_session_event = emit_session_event
        self._create_task_record = create_task_record_fn
        self._update_task_record = update_task_record_fn
        self._append_task_event_record = append_task_event_record_fn
        self._budget_policy = budget_policy or default_guardrail_budget_policy()
        self._now = now_fn
        self._handles: dict[str, _SupervisorHandle] = {}

    async def start(
        self,
        *,
        user_id: str,
        owner_session_id: str,
        objective: str,
        constraints: list[str],
        duration_seconds: int,
        interval_seconds: int,
        source: str,
        maintenance_goal: Optional[str] = None,
        request_id: Optional[str] = None,
        max_iterations: Optional[int] = None,
        mission_contract: MissionContract | dict[str, Any] | None = None,
        approved_promotion_artifacts: dict[str, Any] | list[Any] | None = None,
    ) -> SupervisorStartResult:
        started_at = self._now()
        resolved_duration = max(1, int(duration_seconds))
        resolved_interval = max(0, int(interval_seconds))
        resolved_max_iterations = max_iterations or max(
            1,
            math.ceil(resolved_duration / max(resolved_interval, 1)),
        )
        control_session_id = f"ctrlsup_{uuid.uuid4().hex[:12]}"
        resolved_contract = normalize_mission_contract(
            mission_contract,
            objective=objective,
            constraints=constraints,
            contract_id=f"mission:{control_session_id}",
            metadata={
                "source": source,
                "request_id": request_id,
                "owner_session_id": owner_session_id,
            },
        )
        if resolved_contract.task_nodes:
            resolved_max_iterations = max(
                resolved_max_iterations,
                len(resolved_contract.task_nodes),
            )
        resolved_objective = resolved_contract.objective
        raw_loop_goal = str(maintenance_goal or "").strip()
        loop_goal = (
            _append_mission_contract_section(raw_loop_goal, resolved_contract)
            if raw_loop_goal
            else build_maintenance_goal(resolved_objective, resolved_contract)
        )
        mission_contract_payload = resolved_contract.model_dump(mode="json")
        ends_at = started_at + float(resolved_duration)
        task = self._create_task_record(
            kind="control_supervisor",
            title=resolved_objective,
            status="running",
            owner_session_id=owner_session_id,
            owner_user_id=user_id,
            artifacts={
                "mission_contract": mission_contract_payload,
                "supervisor": {
                    "objective": resolved_objective,
                    "loop_goal": loop_goal,
                    "constraints": list(constraints),
                    "mission_contract_id": resolved_contract.contract_id,
                    "duration_seconds": resolved_duration,
                    "interval_seconds": resolved_interval,
                    "control_session_id": control_session_id,
                    "started_at": started_at,
                    "ends_at": ends_at,
                    "max_iterations": resolved_max_iterations,
                },
                "progress": {
                    "iteration": 0,
                    "completed_iterations": 0,
                    "next_run_at": started_at,
                    "child_task_ids": [],
                    "last_child_task_id": None,
                    "last_result": None,
                    "stop_requested": False,
                    "heartbeat": {
                        "last_heartbeat_at": started_at,
                        "status": "accepted",
                        "reason": "supervisor_started",
                        "active_node_id": "",
                        "scheduler_queue_counts": {},
                    },
                    "last_heartbeat_at": started_at,
                },
                "durable_execution": self._initial_durable_execution_payload(
                    objective=resolved_objective,
                    loop_goal=loop_goal,
                    control_session_id=control_session_id,
                    created_at=started_at,
                    next_run_at=started_at,
                    mission_contract=resolved_contract,
                ),
            },
            metadata={
                "source": source,
                "request_id": request_id,
                "type": "control_supervisor",
                "control_session_id": control_session_id,
                "mission_contract_id": resolved_contract.contract_id,
            },
        )
        task_id = str(task["task_id"])
        reuse_plan_payload: dict[str, Any] | None = None
        if approved_promotion_artifacts is not None:
            reuse_plan_payload = build_mission_reuse_plan(
                resolved_contract,
                approved_promotion_artifacts,
                mission_task_id=task_id,
                now=_utc_datetime(started_at),
            ).model_dump(mode="json")
            updated_task = self._update_task_record(
                task_id,
                artifacts={
                    "reuse_plan": reuse_plan_payload,
                    "durable_execution": {"reuse_plan": reuse_plan_payload},
                },
            )
            if updated_task is not None:
                task = updated_task
            self._append_task_event_record(
                task_id,
                event_type="mission_reuse_plan_recorded",
                status="running",
                title="Mission reuse plan recorded",
                payload={
                    "summary": "Recorded operator-visible reuse_plan.v1 for mission start.",
                    "reuse_plan": reuse_plan_payload,
                    "selected_counts": {
                        "memories": len(
                            reuse_plan_payload.get("selected_memories") or []
                        ),
                        "skills": len(reuse_plan_payload.get("selected_skills") or []),
                        "policies": len(
                            reuse_plan_payload.get("selected_policies") or []
                        ),
                        "capabilities": len(
                            reuse_plan_payload.get("selected_capabilities") or []
                        ),
                    },
                    "excluded_count": len(
                        reuse_plan_payload.get("excluded_candidates") or []
                    ),
                    "operator_visible": reuse_plan_payload.get("operator_visible"),
                    "automatic_runtime_application": (
                        (reuse_plan_payload.get("metadata") or {}).get(
                            "automatic_runtime_application"
                        )
                    ),
                },
            )
        stop_requested = asyncio.Event()
        runner_task = asyncio.create_task(
            self._run_supervisor(
                task_id=task_id,
                owner_session_id=owner_session_id,
                user_id=user_id,
                objective=resolved_objective,
                loop_goal=loop_goal,
                constraints=list(constraints),
                control_session_id=control_session_id,
                interval_seconds=resolved_interval,
                max_iterations=resolved_max_iterations,
                ends_at=ends_at,
                mission_contract=resolved_contract,
                stop_requested=stop_requested,
            ),
            name=f"control-supervisor:{task_id}",
        )
        self._handles[task_id] = _SupervisorHandle(
            task_id=task_id,
            owner_session_id=owner_session_id,
            user_id=user_id,
            stop_requested=stop_requested,
            task=runner_task,
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="accepted",
            message=(
                "Started long-running control supervisor "
                f"for {resolved_duration}s (interval {resolved_interval}s)."
            ),
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )
        return SupervisorStartResult(
            task=task,
            control_session_id=control_session_id,
            max_iterations=resolved_max_iterations,
            ends_at=ends_at,
            next_run_at=started_at,
            mission_contract=mission_contract_payload,
            reuse_plan=reuse_plan_payload,
        )

    async def request_stop(self, task_id: str) -> dict[str, Any] | None:
        handle = self._handles.get(task_id)
        if handle is None:
            return None
        handle.stop_requested.set()
        updated = self._update_task_record(
            task_id,
            artifacts={
                "progress": {
                    "stop_requested": True,
                    "stop_requested_at": self._now(),
                }
            },
            metadata={"stop_requested": True},
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_stop_requested",
            status="running",
            title="Stop requested",
            payload={
                "summary": "Graceful stop requested; the supervisor will stop after the current iteration.",
            },
        )
        await self._emit_session_event(
            handle.owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="stop_requested",
            message=(
                "Graceful stop requested; the supervisor will stop after the current iteration."
            ),
            user_id=handle.user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )
        return updated

    async def shutdown(self) -> None:
        handles = list(self._handles.values())
        for handle in handles:
            if handle.stop_requested.is_set():
                continue
            self._append_task_event_record(
                handle.task_id,
                event_type="supervisor_shutdown_deferred",
                status="running",
                title="Supervisor deferred for resume",
                payload={
                    "summary": (
                        "Gateway shutdown interrupted the live supervisor; the durable "
                        "execution state remains running for startup resume."
                    ),
                },
            )
            handle.task.cancel()
        if handles:
            await asyncio.gather(
                *(handle.task for handle in handles),
                return_exceptions=True,
            )

    async def resume_open_supervisors(
        self,
        tasks: list[dict[str, Any]],
    ) -> int:
        resumed = 0
        for task in tasks:
            if await self.resume_task(task):
                resumed += 1
        return resumed

    def _append_resume_event(
        self,
        task_id: str,
        *,
        event_type: str,
        title: str,
        reason: str,
        status: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "summary": summary,
            "reason": reason,
        }
        if metadata:
            payload.update(metadata)
        self._append_task_event_record(
            task_id,
            event_type=event_type,
            status=status,
            title=title,
            payload=payload,
        )

    async def resume_task(self, task: dict[str, Any]) -> bool:
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            return False
        task_status = str(task.get("status") or "unknown")

        handle = self._handles.get(task_id)
        if handle is not None:
            if not handle.task.done():
                self._append_resume_event(
                    task_id,
                    event_type="supervisor_resume_duplicate_skipped",
                    status=task_status,
                    title="Supervisor resume skipped",
                    reason="active_handle_exists",
                    summary=(
                        "Skipped startup resume because this supervisor already "
                        "has an active live handle."
                    ),
                )
                return False
            self._handles.pop(task_id, None)
            self._append_resume_event(
                task_id,
                event_type="supervisor_resume_stale_handle",
                status=task_status,
                title="Supervisor stale handle cleared",
                reason="stale_handle_done",
                summary=(
                    "Cleared a completed stale supervisor handle before retrying "
                    "startup resume."
                ),
                metadata={"handle_task_cancelled": handle.task.cancelled()},
            )

        if str(task.get("kind") or "") != "control_supervisor":
            self._append_resume_event(
                task_id,
                event_type="supervisor_resume_skipped",
                status=task_status,
                title="Supervisor resume skipped",
                reason="wrong_kind",
                summary="Skipped startup resume because the task is not a control supervisor.",
                metadata={"kind": str(task.get("kind") or "")},
            )
            return False
        if task_status != "running":
            self._append_resume_event(
                task_id,
                event_type="supervisor_resume_skipped",
                status=task_status,
                title="Supervisor resume skipped",
                reason="not_running",
                summary="Skipped startup resume because the task is not running.",
            )
            return False

        artifacts = task.get("artifacts") if isinstance(task.get("artifacts"), dict) else {}
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        supervisor = artifacts.get("supervisor") if isinstance(artifacts.get("supervisor"), dict) else {}
        progress = artifacts.get("progress") if isinstance(artifacts.get("progress"), dict) else {}
        if progress.get("stop_requested"):
            self._append_resume_event(
                task_id,
                event_type="supervisor_resume_skipped",
                status=task_status,
                title="Supervisor resume skipped",
                reason="explicit_stop_requested",
                summary=(
                    "Skipped startup resume because this supervisor was explicitly "
                    "stopped by an operator."
                ),
            )
            return False

        contract_payload = artifacts.get("mission_contract")
        durable_execution = (
            artifacts.get("durable_execution")
            if isinstance(artifacts.get("durable_execution"), dict)
            else {}
        )
        if not isinstance(contract_payload, dict):
            contract_payload = durable_execution.get("mission_contract")
        objective = str(supervisor.get("objective") or task.get("title") or "").strip()
        constraints = list(supervisor.get("constraints") or [])
        mission_contract = normalize_mission_contract(
            contract_payload if isinstance(contract_payload, dict) else None,
            objective=objective,
            constraints=constraints,
            contract_id=str(metadata.get("mission_contract_id") or f"mission:{task_id}"),
            metadata={
                "source": "resume",
                "owner_session_id": task.get("owner_session_id"),
            },
        )
        objective = mission_contract.objective
        loop_goal = str(supervisor.get("loop_goal") or "").strip() or build_maintenance_goal(
            objective,
            mission_contract,
        )
        control_session_id = str(
            supervisor.get("control_session_id")
            or metadata.get("control_session_id")
            or f"ctrlsup_resume_{uuid.uuid4().hex[:8]}"
        )
        interval_seconds = int(supervisor.get("interval_seconds") or 0)
        max_iterations = int(supervisor.get("max_iterations") or 1)
        ends_at = float(supervisor.get("ends_at") or self._now())
        created_at = float(supervisor.get("started_at") or task.get("created_at") or self._now())
        child_task_ids = [str(item) for item in progress.get("child_task_ids") or []]
        completed_iterations = int(progress.get("completed_iterations") or 0)
        next_run_at = progress.get("next_run_at")
        runtime_state = self._runtime_state_from_durable_execution(
            objective=objective,
            loop_goal=loop_goal,
            control_session_id=control_session_id,
            created_at=created_at,
            next_run_at=float(next_run_at) if next_run_at is not None else self._now(),
            mission_contract=mission_contract,
            durable_execution=durable_execution,
        )

        stop_requested = asyncio.Event()
        runner_task = asyncio.create_task(
            self._run_supervisor(
                task_id=task_id,
                owner_session_id=str(task.get("owner_session_id") or ""),
                user_id=str(task.get("owner_user_id") or ""),
                objective=objective,
                loop_goal=loop_goal,
                constraints=constraints,
                control_session_id=control_session_id,
                interval_seconds=interval_seconds,
                max_iterations=max_iterations,
                ends_at=ends_at,
                mission_contract=mission_contract,
                stop_requested=stop_requested,
                initial_runtime_state=runtime_state,
                initial_child_task_ids=child_task_ids,
                initial_completed_iterations=completed_iterations,
                resumed=True,
            ),
            name=f"control-supervisor:{task_id}:resume",
        )
        self._handles[task_id] = _SupervisorHandle(
            task_id=task_id,
            owner_session_id=str(task.get("owner_session_id") or ""),
            user_id=str(task.get("owner_user_id") or ""),
            stop_requested=stop_requested,
            task=runner_task,
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_resumed",
            status="running",
            title="Supervisor resumed",
            payload={
                "summary": "Resumed live supervisor from durable_execution.resume_state.",
                "completed_iterations": completed_iterations,
                "control_session_id": control_session_id,
            },
        )
        return True

    def _mission_graph_nodes(
        self,
        *,
        objective: str,
        loop_goal: str,
        control_session_id: str,
        mission_contract: MissionContract,
    ) -> list[DurableTaskNode]:
        if not mission_contract.task_nodes:
            node_id = _supervisor_node_id(control_session_id)
            return [
                DurableTaskNode(
                    node_id=node_id,
                    title=objective,
                    description=loop_goal,
                    status=DurableTaskNodeStatus.READY,
                    completion_criteria=list(mission_contract.completion_criteria),
                    artifacts=[_mission_contract_artifact_ref(mission_contract)],
                    scheduler_queue=SchedulerQueueKind.READY,
                    metadata={
                        "mission_contract_id": mission_contract.contract_id,
                        "allowed_actions": list(mission_contract.allowed_actions),
                        "forbidden_actions": list(mission_contract.forbidden_actions),
                        **_abort_condition_metadata(mission_contract),
                        "evidence_requirements": list(mission_contract.evidence_requirements),
                        "mission_graph_mode": "single_node",
                    },
                )
            ]

        nodes: list[DurableTaskNode] = []
        for contract_node in mission_contract.task_nodes:
            node_id = _mission_graph_node_id(
                control_session_id,
                contract_node.node_id,
            )
            dependencies = [
                _mission_graph_node_id(control_session_id, dependency)
                for dependency in contract_node.depends_on
            ]
            is_ready = not dependencies
            node_title = contract_node.title or contract_node.node_id
            node_description = contract_node.description or node_title
            nodes.append(
                DurableTaskNode(
                    node_id=node_id,
                    title=node_title,
                    description=_append_mission_contract_section(
                        node_description,
                        mission_contract,
                    ),
                    status=(
                        DurableTaskNodeStatus.READY
                        if is_ready
                        else DurableTaskNodeStatus.BLOCKED
                    ),
                    depends_on=dependencies,
                    completion_criteria=list(
                        contract_node.completion_criteria
                        or mission_contract.completion_criteria
                    ),
                    artifacts=[_mission_contract_artifact_ref(mission_contract)],
                    scheduler_queue=(
                        SchedulerQueueKind.READY
                        if is_ready
                        else SchedulerQueueKind.BLOCKED
                    ),
                    metadata={
                        "mission_contract_id": mission_contract.contract_id,
                        "contract_node_id": contract_node.node_id,
                        "allowed_actions": list(mission_contract.allowed_actions),
                        "forbidden_actions": list(mission_contract.forbidden_actions),
                        **_abort_condition_metadata(mission_contract),
                        "evidence_requirements": list(mission_contract.evidence_requirements),
                        "mission_graph_mode": "multi_node",
                        **dict(contract_node.metadata),
                    },
                )
            )
        return nodes

    def _initial_scheduler_queue_state(
        self,
        *,
        nodes: list[DurableTaskNode],
        mission_contract: MissionContract,
    ) -> SchedulerQueueState:
        queue_state = SchedulerQueueState()
        for node in nodes:
            queue = node.scheduler_queue or SchedulerQueueKind.BLOCKED
            entry = _queue_entry_for_node(
                node_id=node.node_id,
                queue=queue,
                checkpoint_id=None,
                available_at=None,
                failure_type=None,
                chosen_action=None,
                mission_contract=mission_contract,
            )
            entry.reason = (
                "dependencies_satisfied"
                if queue == SchedulerQueueKind.READY
                else "waiting_for_dependencies"
            )
            append_scheduler_queue_entry(queue_state, entry)
        return queue_state

    @staticmethod
    def _runtime_nodes(runtime_state: dict[str, Any]) -> list[DurableTaskNode]:
        nodes = runtime_state.get("nodes")
        if isinstance(nodes, list) and nodes:
            return nodes
        node = runtime_state.get("node")
        if isinstance(node, DurableTaskNode):
            runtime_state["nodes"] = [node]
            return runtime_state["nodes"]
        return []

    def _node_by_id(
        self,
        runtime_state: dict[str, Any],
        node_id: str | None,
    ) -> DurableTaskNode | None:
        if not node_id:
            return None
        for node in self._runtime_nodes(runtime_state):
            if node.node_id == node_id:
                return node
        return None

    def _set_current_node(
        self,
        runtime_state: dict[str, Any],
        node_id: str | None,
    ) -> DurableTaskNode | None:
        node = self._node_by_id(runtime_state, node_id)
        if node is None:
            return None
        runtime_state["node_id"] = node.node_id
        runtime_state["node"] = node
        return node

    def _is_multi_node_runtime(self, runtime_state: dict[str, Any]) -> bool:
        return len(self._runtime_nodes(runtime_state)) > 1

    def _initial_runtime_state(
        self,
        *,
        objective: str,
        loop_goal: str,
        control_session_id: str,
        created_at: float,
        next_run_at: float | None,
        mission_contract: MissionContract,
    ) -> dict[str, Any]:
        graph_id = f"control_supervisor:{control_session_id}"
        nodes = self._mission_graph_nodes(
            objective=objective,
            loop_goal=loop_goal,
            control_session_id=control_session_id,
            mission_contract=mission_contract,
        )
        queue_state = self._initial_scheduler_queue_state(
            nodes=nodes,
            mission_contract=mission_contract,
        )
        current_node = next(
            (node for node in nodes if node.scheduler_queue == SchedulerQueueKind.READY),
            nodes[0],
        )
        return {
            "created_at": created_at,
            "graph_id": graph_id,
            "control_session_id": control_session_id,
            "node_id": current_node.node_id,
            "node": current_node,
            "nodes": nodes,
            "mission_contract": mission_contract,
            "queue_state": queue_state,
            "job_runs": [],
            "checkpoints": [],
            "escalations": [],
            "recovery_decisions": [],
            "successful_artifacts": {},
            "heartbeat": {
                "last_heartbeat_at": created_at,
                "status": "initialized",
                "reason": "initial_runtime_state",
                "active_node_id": current_node.node_id,
            },
            "retry_counters": Counter(),
            "llm_calls_used": 0,
            "tool_calls_used": 0,
            "repair_depth_used": 0,
            "pending_approvals_count": 0,
        }

    def _runtime_state_from_durable_execution(
        self,
        *,
        objective: str,
        loop_goal: str,
        control_session_id: str,
        created_at: float,
        next_run_at: float | None,
        mission_contract: MissionContract,
        durable_execution: dict[str, Any] | None,
    ) -> dict[str, Any]:
        runtime_state = self._initial_runtime_state(
            objective=objective,
            loop_goal=loop_goal,
            control_session_id=control_session_id,
            created_at=created_at,
            next_run_at=next_run_at,
            mission_contract=mission_contract,
        )
        if not isinstance(durable_execution, dict):
            return runtime_state

        task_graph_payload = durable_execution.get("task_graph")
        if isinstance(task_graph_payload, dict):
            task_graph = DurableTaskGraph.model_validate(task_graph_payload)
            if task_graph.nodes:
                runtime_state["nodes"] = list(task_graph.nodes)

        queue_state_payload = durable_execution.get("scheduler_state")
        if isinstance(queue_state_payload, dict):
            runtime_state["queue_state"] = SchedulerQueueState.model_validate(
                queue_state_payload
            )

        runtime_state["job_runs"] = [
            DurableJobRun.model_validate(item)
            for item in durable_execution.get("job_runs") or []
            if isinstance(item, dict)
        ]
        runtime_state["checkpoints"] = [
            DurableCheckpoint.model_validate(item)
            for item in durable_execution.get("checkpoints") or []
            if isinstance(item, dict)
        ]
        runtime_state["escalations"] = [
            DurableEscalationRecord.model_validate(item)
            for item in durable_execution.get("escalations") or []
            if isinstance(item, dict)
        ]
        runtime_state["recovery_decisions"] = [
            RecoveryDecision.model_validate(item)
            for item in durable_execution.get("recovery_decisions") or []
            if isinstance(item, dict)
        ]
        if runtime_state["checkpoints"]:
            latest_budget = runtime_state["checkpoints"][-1].budget
            runtime_state["retry_counters"] = Counter(latest_budget.same_failure_retries)
            runtime_state["llm_calls_used"] = latest_budget.llm_calls_used
            runtime_state["tool_calls_used"] = latest_budget.tool_calls_used
            runtime_state["repair_depth_used"] = latest_budget.repair_depth_used
            runtime_state["pending_approvals_count"] = latest_budget.pending_approvals_count
            runtime_state["successful_artifacts"] = dict(
                runtime_state["checkpoints"][-1].last_successful_artifacts
            )
        resume_state_payload = durable_execution.get("resume_state")
        resume_node_id = None
        if isinstance(resume_state_payload, dict):
            resume_node_id = str(
                resume_state_payload.get("next_actionable_task_node_id") or ""
            ).strip() or None
        if resume_node_id is None:
            selected_entry = self._next_scheduler_entry(runtime_state)
            resume_node_id = selected_entry.node_id if selected_entry is not None else None
        if resume_node_id is None and runtime_state["checkpoints"]:
            latest_checkpoint = runtime_state["checkpoints"][-1]
            resume_node_id = (
                latest_checkpoint.next_actionable_task_node_id
                or latest_checkpoint.current_task_node_id
            )
        if resume_node_id is None:
            task_graph = DurableTaskGraph(
                graph_id=runtime_state["graph_id"],
                goal=objective,
                nodes=list(self._runtime_nodes(runtime_state)),
            )
            resume_node_id = task_graph.next_actionable_task_node_id()
        if self._set_current_node(runtime_state, resume_node_id) is None:
            nodes = self._runtime_nodes(runtime_state)
            if nodes:
                self._set_current_node(runtime_state, nodes[0].node_id)
        health_payload = durable_execution.get("supervisor_health")
        if isinstance(health_payload, dict):
            runtime_state["heartbeat"] = dict(health_payload)
        return runtime_state

    @staticmethod
    def _scheduler_queue_entries(
        queue_state: SchedulerQueueState,
    ) -> list[SchedulerQueueEntry]:
        return [
            *queue_state.ready_queue,
            *queue_state.retry_later_queue,
            *queue_state.periodic_check_queue,
        ]

    @staticmethod
    def _remove_scheduler_entries(
        queue_state: SchedulerQueueState,
        entry_ids: set[str],
    ) -> None:
        if not entry_ids:
            return
        queue_state.ready_queue = [
            entry for entry in queue_state.ready_queue if entry.entry_id not in entry_ids
        ]
        queue_state.retry_later_queue = [
            entry for entry in queue_state.retry_later_queue if entry.entry_id not in entry_ids
        ]
        queue_state.periodic_check_queue = [
            entry
            for entry in queue_state.periodic_check_queue
            if entry.entry_id not in entry_ids
        ]

    def _scheduler_entry_skip_reason(
        self,
        runtime_state: dict[str, Any],
        entry: SchedulerQueueEntry,
        *,
        now: float,
    ) -> str | None:
        expires_at = _datetime_timestamp(entry.metadata.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            return "expired"
        node = self._node_by_id(runtime_state, entry.node_id)
        if node is None:
            return "stale_unknown_node"
        if node.status == DurableTaskNodeStatus.DONE and entry.queue != SchedulerQueueKind.COMPLETED:
            return "stale_completed_node"
        if node.status == DurableTaskNodeStatus.BLOCKED and entry.queue == SchedulerQueueKind.READY:
            return "stale_blocked_node"
        return None

    @staticmethod
    def _scheduler_entry_sort_key(
        entry: SchedulerQueueEntry,
        *,
        now: float,
        due_only: bool,
    ) -> tuple[float, int, str]:
        queue_priority = {
            SchedulerQueueKind.READY: 0,
            SchedulerQueueKind.RETRY_LATER: 1,
            SchedulerQueueKind.PERIODIC_CHECK: 2,
        }
        available_at = _datetime_timestamp(entry.available_at)
        if due_only:
            return (
                available_at if available_at is not None else 0.0,
                queue_priority.get(entry.queue, 99),
                entry.entry_id,
            )
        return (
            available_at if available_at is not None else now,
            queue_priority.get(entry.queue, 99),
            entry.entry_id,
        )

    def _select_scheduler_entry(
        self,
        runtime_state: dict[str, Any],
        *,
        now: float | None = None,
    ) -> _SchedulerSelection:
        selected_now = self._now() if now is None else now
        entries = self._scheduler_queue_entries(runtime_state["queue_state"])
        actionable_entries: list[SchedulerQueueEntry] = []
        future_entries: list[SchedulerQueueEntry] = []
        skipped_entries: list[dict[str, Any]] = []
        stale_entry_ids: set[str] = set()

        for entry in entries:
            skip_reason = self._scheduler_entry_skip_reason(
                runtime_state,
                entry,
                now=selected_now,
            )
            if skip_reason:
                stale_entry_ids.add(entry.entry_id)
                skipped_entries.append(
                    {
                        "reason": skip_reason,
                        "entry": entry.model_dump(mode="json"),
                    }
                )
                continue
            available_at = _datetime_timestamp(entry.available_at)
            if available_at is None or available_at <= selected_now:
                actionable_entries.append(entry)
            else:
                future_entries.append(entry)

        self._remove_scheduler_entries(runtime_state["queue_state"], stale_entry_ids)

        if actionable_entries:
            selected = sorted(
                actionable_entries,
                key=lambda entry: (
                    {
                        SchedulerQueueKind.READY: 0,
                        SchedulerQueueKind.RETRY_LATER: 1,
                        SchedulerQueueKind.PERIODIC_CHECK: 2,
                    }.get(entry.queue, 99),
                    _datetime_timestamp(entry.available_at) or 0.0,
                    entry.entry_id,
                ),
            )[0]
            return _SchedulerSelection(entry=selected, skipped_entries=skipped_entries)

        if future_entries:
            selected = sorted(
                future_entries,
                key=lambda entry: self._scheduler_entry_sort_key(
                    entry,
                    now=selected_now,
                    due_only=False,
                ),
            )[0]
            return _SchedulerSelection(entry=selected, skipped_entries=skipped_entries)

        return _SchedulerSelection(entry=None, skipped_entries=skipped_entries)

    def _next_scheduler_entry(
        self,
        runtime_state: dict[str, Any],
    ) -> SchedulerQueueEntry | None:
        return self._select_scheduler_entry(runtime_state).entry

    def _mission_contract_abort_reason(
        self,
        *,
        mission_contract: MissionContract,
        result: ExecutionResult,
        next_queue: SchedulerQueueKind,
        budget_exhausted: bool,
    ) -> str | None:
        aborts = mission_contract.abort_condition_types
        if not aborts:
            return None
        if (
            MissionAbortConditionType.HUMAN_APPROVAL_REQUIRED in aborts
            and next_queue == SchedulerQueueKind.WAITING_FOR_APPROVAL
        ):
            return MissionAbortConditionType.HUMAN_APPROVAL_REQUIRED.value
        if (
            MissionAbortConditionType.GUARDRAIL_BUDGET_EXHAUSTED in aborts
            and budget_exhausted
        ):
            return MissionAbortConditionType.GUARDRAIL_BUDGET_EXHAUSTED.value
        text = " ".join(
            str(part or "")
            for part in (
                result.final_text,
                result.metadata.get("error"),
                result.metadata.get("normalized_failure_type"),
            )
        ).lower()
        if (
            not result.success
            and MissionAbortConditionType.CURRENT_TAB_CONNECTION_UNAVAILABLE in aborts
            and any(
                token in text
                for token in (
                    "current tab extension",
                    "current_tab",
                    "all connection attempts failed",
                    "connection unavailable",
                    "disconnected",
                )
            )
        ):
            return MissionAbortConditionType.CURRENT_TAB_CONNECTION_UNAVAILABLE.value
        return None

    def _serialize_runtime_state(
        self,
        runtime_state: dict[str, Any],
        *,
        objective: str,
    ) -> dict[str, Any]:
        # `queue_state` is the current actionable snapshot for the live supervisor,
        # not a cumulative history of every queue the node visited. Historical
        # transitions remain in `job_runs`, `checkpoints`, and task timeline events.
        created_at = _utc_datetime(runtime_state["created_at"])
        node: DurableTaskNode = runtime_state["node"]
        nodes = list(self._runtime_nodes(runtime_state))
        mission_contract: MissionContract = runtime_state["mission_contract"]
        task_graph = DurableTaskGraph(
            graph_id=runtime_state["graph_id"],
            goal=objective,
            nodes=nodes,
            created_at=created_at,
            updated_at=_utc_datetime(self._now()),
            metadata={
                "runtime_mode": (
                    "live_supervisor_mission_graph"
                    if len(nodes) > 1
                    else "live_supervisor_phase0"
                ),
                "scheduler_phase": "live_worker",
                "mission_contract_id": mission_contract.contract_id,
                "queue_selection": "ready_then_due_retry_then_due_periodic",
            },
        )
        checkpoints: list[DurableCheckpoint] = runtime_state["checkpoints"]
        if checkpoints:
            resume_state = checkpoints[-1].resume_state(task_graph).model_copy(
                update={
                    "scheduler_queue_counts": runtime_state["queue_state"].counts(),
                    "pending_approval_ids": list(checkpoints[-1].pending_approval_ids),
                }
            )
        else:
            resume_state = DurableResumeState(
                checkpoint_id=f"{runtime_state['graph_id']}/checkpoint-0",
                graph_id=runtime_state["graph_id"],
                next_actionable_task_node_id=node.node_id,
                open_task_node_ids=task_graph.open_task_node_ids(),
                blocked_task_node_ids=task_graph.blocked_task_node_ids(),
                pending_approval_ids=[],
                scheduler_queue_counts=runtime_state["queue_state"].counts(),
                reason="resume_from_open_task",
            )
        durable_execution = {
            "mission_contract": mission_contract.model_dump(mode="json"),
            "task_graph": task_graph.model_dump(mode="json"),
            "job_runs": [
                item.model_dump(mode="json") for item in runtime_state["job_runs"]
            ],
            "checkpoints": [item.model_dump(mode="json") for item in checkpoints],
            "verifier_verdicts": [
                node.verifier_verdict.model_dump(mode="json")
                for node in task_graph.nodes
                if node.verifier_verdict is not None
            ],
            "recovery_policies": {
                key: value.model_dump(mode="json")
                for key, value in default_recovery_policies().items()
            },
            "recovery_decisions": [
                item.model_dump(mode="json")
                for item in runtime_state.get("recovery_decisions") or []
            ],
            "scheduler_state": runtime_state["queue_state"].model_dump(mode="json"),
            "escalations": [
                item.model_dump(mode="json") for item in runtime_state["escalations"]
            ],
            "resume_state": resume_state.model_dump(mode="json"),
            "supervisor_health": dict(runtime_state.get("heartbeat") or {}),
        }
        durable_execution["mission_scorecard"] = build_mission_scorecard(
            durable_execution,
        ).model_dump(mode="json")
        return durable_execution

    def _terminal_mission_artifacts(
        self,
        *,
        task_id: str,
        durable_execution: dict[str, Any],
        final_status: str,
        child_task_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        return build_post_mission_review_artifacts(
            durable_execution,
            final_status=final_status,
            source_task_id=task_id,
            child_task_ids=child_task_ids,
            created_at=_utc_datetime(self._now()),
        )

    def _append_post_mission_review_event(
        self,
        task_id: str,
        *,
        final_status: str,
        artifacts: dict[str, Any],
    ) -> None:
        review = artifacts.get("mission_review")
        if not isinstance(review, dict):
            return
        self._append_task_event_record(
            task_id,
            event_type="post_mission_review_recorded",
            status=final_status,
            title="Post-mission review recorded",
            payload={
                "summary": review.get("summary") or "Post-mission review recorded.",
                "final_status": final_status,
                "scorecard": artifacts.get("mission_scorecard") or {},
                "failure_buckets": review.get("failure_buckets") or [],
                "improvement_candidates": review.get("improvement_candidates") or [],
                "memory_promotion_candidates": review.get("memory_promotion_candidates") or [],
            },
        )

    def _initial_durable_execution_payload(
        self,
        *,
        objective: str,
        loop_goal: str,
        control_session_id: str,
        created_at: float,
        next_run_at: float | None,
        mission_contract: MissionContract,
    ) -> dict[str, Any]:
        runtime_state = self._initial_runtime_state(
            objective=objective,
            loop_goal=loop_goal,
            control_session_id=control_session_id,
            created_at=created_at,
            next_run_at=next_run_at,
            mission_contract=mission_contract,
        )
        return self._serialize_runtime_state(runtime_state, objective=objective)

    def _runtime_failure_classification(self, result: ExecutionResult) -> dict[str, Any]:
        existing = str(result.metadata.get("normalized_failure_type") or "").strip() or None
        classification = classify_control_loop_failure(
            success=result.success,
            needs_human=bool(result.metadata.get("needs_human")),
            final_text=result.final_text,
            verification_status=result.metadata.get("verification_status"),
            verification_report=(
                result.metadata.get("verification_report")
                if isinstance(result.metadata.get("verification_report"), dict)
                else None
            ),
            error=str(result.metadata.get("error") or "").strip() or None,
            existing_failure_type=existing,
        )
        result.metadata.update(classification)
        return classification

    def _child_exception_result(
        self,
        *,
        exc: Exception,
        user_id: str,
        session_id: str,
    ) -> ExecutionResult:
        failure_type = "policy_blocked" if isinstance(exc, PermissionError) else "unknown"
        return ExecutionResult(
            request_id=f"supervisor_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            user_id=user_id,
            final_text=f"Child control-loop failed before durable result: {exc}",
            success=False,
            metadata={
                "error": str(exc),
                "exception_type": type(exc).__name__,
                "normalized_failure_type": failure_type,
            },
        )

    def _dependency_ready_nodes(
        self,
        runtime_state: dict[str, Any],
    ) -> list[DurableTaskNode]:
        nodes = self._runtime_nodes(runtime_state)
        completed_node_ids = {
            node.node_id
            for node in nodes
            if node.status == DurableTaskNodeStatus.DONE
            or node.scheduler_queue == SchedulerQueueKind.COMPLETED
        }
        ready_nodes: list[DurableTaskNode] = []
        for node in nodes:
            if not node.depends_on:
                continue
            if node.status != DurableTaskNodeStatus.BLOCKED:
                continue
            if node.scheduler_queue not in {SchedulerQueueKind.BLOCKED, None}:
                continue
            if all(dependency in completed_node_ids for dependency in node.depends_on):
                ready_nodes.append(node)
        return ready_nodes

    def _queue_ready_dependency_nodes(
        self,
        runtime_state: dict[str, Any],
        *,
        mission_contract: MissionContract,
        checkpoint_id: str,
    ) -> list[SchedulerQueueEntry]:
        ready_entries: list[SchedulerQueueEntry] = []
        for node in self._dependency_ready_nodes(runtime_state):
            node.status = DurableTaskNodeStatus.READY
            node.scheduler_queue = SchedulerQueueKind.READY
            entry = _queue_entry_for_node(
                node_id=node.node_id,
                queue=SchedulerQueueKind.READY,
                checkpoint_id=checkpoint_id,
                available_at=None,
                failure_type=None,
                chosen_action=None,
                mission_contract=mission_contract,
            )
            entry.reason = "dependencies_satisfied"
            ready_entries.append(entry)
        return ready_entries

    def _runtime_task_graph(
        self,
        runtime_state: dict[str, Any],
        *,
        objective: str,
    ) -> DurableTaskGraph:
        mission_contract: MissionContract = runtime_state["mission_contract"]
        nodes = list(self._runtime_nodes(runtime_state))
        return DurableTaskGraph(
            graph_id=runtime_state["graph_id"],
            goal=objective,
            nodes=nodes,
            created_at=_utc_datetime(runtime_state["created_at"]),
            updated_at=_utc_datetime(self._now()),
            metadata={
                "runtime_mode": (
                    "live_supervisor_mission_graph"
                    if len(nodes) > 1
                    else "live_supervisor_phase0"
                ),
                "scheduler_phase": "live_worker",
                "mission_contract_id": mission_contract.contract_id,
                "queue_selection": "ready_then_due_retry_then_due_periodic",
            },
        )

    def _record_runtime_iteration(
        self,
        runtime_state: dict[str, Any],
        *,
        objective: str,
        iteration: int,
        max_iterations: int,
        result: ExecutionResult,
        child_task_id: str,
        next_run_at: float | None,
        has_more_iterations: bool,
    ) -> dict[str, Any]:
        mission_contract: MissionContract = runtime_state["mission_contract"]
        node: DurableTaskNode = runtime_state["node"]
        is_multi_node = self._is_multi_node_runtime(runtime_state)
        classification = self._runtime_failure_classification(result)
        failure_type = classification["normalized_failure_type"]
        verifier_verdict = _build_live_verifier_verdict(
            result=result,
            failure_type=failure_type,
            child_task_id=child_task_id,
            created_at=self._now(),
        )
        needs_recovery = (
            not result.success
            or verifier_verdict.verdict != DurableVerifierVerdictValue.PASS
            or bool(failure_type)
        )
        policy = None
        chosen_action = None
        preferred_step = None
        selected_step = None
        selection_reason = "no_recovery_needed"
        if not needs_recovery:
            current_node_queue = SchedulerQueueKind.COMPLETED if is_multi_node else (
                SchedulerQueueKind.PERIODIC_CHECK
                if has_more_iterations
                else SchedulerQueueKind.COMPLETED
            )
        else:
            policy = recovery_policy_for_failure_type(failure_type)
            chosen_action = policy.allowed_actions[0] if policy and policy.allowed_actions else None
            current_node_queue = (
                policy.next_scheduler_queue
                if policy is not None
                else SchedulerQueueKind.BLOCKED
            )
        retry_count = runtime_state["retry_counters"][failure_type] if failure_type else 0
        # Phase 0 uses a coarse hourly budget counter so live supervisor artifacts
        # stay shape-compatible with eval-derived substrate reports. If a future
        # worker needs shorter smoke budgets, promote this to a finer-grained clock.
        runtime_hours_used = max(
            1,
            math.ceil(max(0.0, self._now() - runtime_state["created_at"]) / 3600.0),
        )
        budget_before = _recovery_budget_snapshot(
            runtime_state=runtime_state,
            runtime_hours_used=runtime_hours_used,
            retry_count=retry_count,
            pending_approvals_count=runtime_state["pending_approvals_count"],
            budget_policy=self._budget_policy,
            mission_contract=mission_contract,
        )
        recovery_policy = getattr(mission_contract, "recovery_policy", None)
        recovery_ladder = getattr(recovery_policy, "ladder", None)
        max_retries_per_step = int(
            getattr(recovery_policy, "max_retries_per_step", 0) or 0
        )
        if needs_recovery:
            preferred_step = recovery_ladder_step_for_decision(
                chosen_action=chosen_action,
                next_scheduler_queue=current_node_queue,
                budget_exhausted=False,
            )
            selected_step, selection_reason = select_recovery_ladder_step(
                preferred_step=preferred_step,
                ladder=recovery_ladder,
                retry_count=retry_count,
                max_retries_per_step=max_retries_per_step,
            )
            chosen_action = recovery_action_for_ladder_step(
                selected_step,
                fallback=chosen_action,
            )
            current_node_queue = scheduler_queue_for_recovery_ladder_step(
                selected_step,
                fallback=current_node_queue,
            )

        runtime_state["llm_calls_used"] += 1 + (
            policy.budget_impact.llm_calls if policy is not None else 0
        )
        runtime_state["tool_calls_used"] += max(1, len(verifier_verdict.evidence_refs)) + (
            policy.budget_impact.tool_calls if policy is not None else 0
        )
        runtime_state["repair_depth_used"] += repair_depth_increment(chosen_action)
        budget_reasons = budget_exhaustion_reasons(
            budget_policy=self._budget_policy,
            runtime_hours_used=runtime_hours_used,
            llm_calls_used=runtime_state["llm_calls_used"],
            tool_calls_used=runtime_state["tool_calls_used"],
            repair_depth_used=runtime_state["repair_depth_used"],
            pending_approvals_count=runtime_state["pending_approvals_count"],
            next_scheduler_queue=current_node_queue,
            failure_type=failure_type,
            retry_count=retry_count,
        )
        if (
            current_node_queue == SchedulerQueueKind.WAITING_FOR_APPROVAL
            and runtime_state["pending_approvals_count"] + 1
            > self._budget_policy.max_pending_approvals
            and "max_pending_approvals_exhausted" not in budget_reasons
        ):
            budget_reasons.append("max_pending_approvals_exhausted")
        budget_exhausted = bool(budget_reasons)
        if budget_exhausted:
            selected_step, selection_reason = select_recovery_ladder_step(
                preferred_step=preferred_step,
                ladder=recovery_ladder,
                retry_count=retry_count,
                max_retries_per_step=max_retries_per_step,
                budget_exhausted=True,
            )
            chosen_action = recovery_action_for_ladder_step(
                selected_step,
                fallback=RecoveryActionType.MARK_FAILED,
            )
            current_node_queue = SchedulerQueueKind.BLOCKED
        abort_reason = self._mission_contract_abort_reason(
            mission_contract=mission_contract,
            result=result,
            next_queue=current_node_queue,
            budget_exhausted=budget_exhausted,
        )
        if abort_reason:
            current_node_queue = SchedulerQueueKind.BLOCKED
            chosen_action = RecoveryActionType.MARK_FAILED
            selected_step = RecoveryLadderStep.PAUSE_OR_BLOCK
            selection_reason = f"mission_contract_abort:{abort_reason}"
        pending_approvals_after = runtime_state["pending_approvals_count"] + (
            1 if current_node_queue == SchedulerQueueKind.WAITING_FOR_APPROVAL else 0
        )
        budget_after = _recovery_budget_snapshot(
            runtime_state=runtime_state,
            runtime_hours_used=runtime_hours_used,
            retry_count=retry_count + (1 if failure_type and needs_recovery else 0),
            pending_approvals_count=pending_approvals_after,
            budget_policy=self._budget_policy,
            mission_contract=mission_contract,
        )
        decision = RecoveryDecision(
            node_id=runtime_state["node_id"],
            failure_type=failure_type,
            chosen_action=chosen_action,
            recovery_ladder_step=selected_step,
            selected_step=selected_step,
            attempt_index=retry_count + (1 if failure_type and needs_recovery else 0),
            budget_before=budget_before,
            budget_after=budget_after,
            outcome=recovery_outcome_for_queue(
                current_node_queue,
                result_success=result.success and not needs_recovery,
                aborted=bool(abort_reason),
            ),
            budget_consumption=(
                policy.budget_impact if policy is not None else RecoveryBudgetImpact()
            ),
            source_refs=_recovery_source_refs(
                child_task_id=child_task_id,
                result=result,
                verifier_verdict=verifier_verdict,
            ),
            policy=policy,
            next_scheduler_queue=current_node_queue,
            budget_exhausted=budget_exhausted,
            budget_exhausted_reasons=budget_reasons,
        )
        queue_reason = scheduler_queue_reason(
            decision,
            verifier_verdict=verifier_verdict,
        )
        if abort_reason:
            queue_reason = f"mission_aborted:{abort_reason}"
        decision.reason = (
            queue_reason
            if selection_reason in {"mission_recovery_policy_selected", "no_recovery_needed"}
            else f"{selection_reason}:{queue_reason}"
        )
        if selected_step is not None or needs_recovery or budget_exhausted or abort_reason:
            runtime_state["recovery_decisions"].append(decision)
        available_at = _available_at_for_queue(
            current_node_queue,
            next_run_at=next_run_at,
        )
        checkpoint_id = f"{runtime_state['graph_id']}/checkpoint-{iteration}"
        escalation_record = None
        if current_node_queue == SchedulerQueueKind.WAITING_FOR_APPROVAL:
            approval_request = result.metadata.get("approval_request")
            approval_request_id = None
            if isinstance(approval_request, dict):
                approval_request_id = str(approval_request.get("request_id") or "").strip() or None
            escalation_record = build_escalation_record(
                run_id=f"{runtime_state['graph_id']}/run-{iteration}",
                node_id=runtime_state["node_id"],
                checkpoint_id=checkpoint_id,
                created_at=_utc_datetime(self._now()),
                failure_type=failure_type,
                reason=queue_reason,
                approval_request_id=approval_request_id,
            )
            runtime_state["escalations"].append(escalation_record)
            runtime_state["pending_approvals_count"] = pending_approvals_after

        queue_entry = _queue_entry_for_node(
            node_id=runtime_state["node_id"],
            queue=current_node_queue,
            checkpoint_id=checkpoint_id,
            available_at=available_at,
            failure_type=failure_type,
            chosen_action=chosen_action,
            mission_contract=mission_contract,
            escalation_id=(
                escalation_record.escalation_id if escalation_record is not None else None
            ),
        )
        queue_entry.reason = queue_reason
        if decision.recovery_ladder_step is not None:
            queue_entry.metadata["recovery_ladder_step"] = (
                decision.recovery_ladder_step.value
            )
        if decision.selected_step is not None:
            queue_entry.metadata["selected_step"] = decision.selected_step.value
        if decision.outcome is not None:
            queue_entry.metadata["recovery_outcome"] = decision.outcome.value
        if decision.reason:
            queue_entry.metadata["recovery_reason"] = decision.reason
        if abort_reason:
            queue_entry.metadata["abort_reason"] = abort_reason

        if failure_type and needs_recovery:
            runtime_state["retry_counters"][failure_type] += 1

        node.status = _task_node_status_for_queue(current_node_queue)
        node.retry_count = runtime_state["retry_counters"][failure_type] if failure_type else 0
        node.next_retry_at = available_at
        node.scheduler_queue = current_node_queue
        node.verifier_verdict = verifier_verdict
        node.checkpoint_refs.append(checkpoint_id)
        if not node.completion_criteria:
            node.completion_criteria = list(mission_contract.completion_criteria)
        node.metadata.update(
            {
                "mission_contract_id": mission_contract.contract_id,
                "allowed_actions": list(mission_contract.allowed_actions),
                "forbidden_actions": list(mission_contract.forbidden_actions),
                **_abort_condition_metadata(mission_contract),
                "evidence_requirements": list(mission_contract.evidence_requirements),
            }
        )
        node.artifacts = _artifact_refs_for_result(
            child_task_id=child_task_id,
            result=result,
            failure_type=failure_type,
            mission_contract=mission_contract,
        )

        if result.success and not needs_recovery:
            runtime_state["successful_artifacts"][node.node_id] = list(node.artifacts)

        if is_multi_node and result.success and not needs_recovery:
            self._queue_ready_dependency_nodes(
                runtime_state,
                mission_contract=mission_contract,
                checkpoint_id=checkpoint_id,
            )

        runtime_state["queue_state"] = SchedulerQueueState()
        append_scheduler_queue_entry(runtime_state["queue_state"], queue_entry)
        if is_multi_node:
            for graph_node in self._runtime_nodes(runtime_state):
                if graph_node.node_id == node.node_id:
                    continue
                graph_queue = graph_node.scheduler_queue
                if graph_queue is None:
                    continue
                graph_entry = _queue_entry_for_node(
                    node_id=graph_node.node_id,
                    queue=graph_queue,
                    checkpoint_id=checkpoint_id,
                    available_at=graph_node.next_retry_at,
                    failure_type=None,
                    chosen_action=None,
                    mission_contract=mission_contract,
                )
                graph_entry.reason = (
                    "dependencies_satisfied"
                    if graph_queue == SchedulerQueueKind.READY
                    else "waiting_for_dependencies"
                    if graph_queue == SchedulerQueueKind.BLOCKED
                    else "preserved_graph_queue"
                )
                append_scheduler_queue_entry(runtime_state["queue_state"], graph_entry)

        scheduler_selection = self._select_scheduler_entry(
            runtime_state,
            now=self._now(),
        )
        selected_queue_entry = scheduler_selection.entry or queue_entry
        if scheduler_selection.entry is not None:
            worker_next_queue = selected_queue_entry.queue
        elif current_node_queue in {
            SchedulerQueueKind.WAITING_FOR_APPROVAL,
            SchedulerQueueKind.BLOCKED,
            SchedulerQueueKind.RETRY_LATER,
            SchedulerQueueKind.PERIODIC_CHECK,
        }:
            worker_next_queue = current_node_queue
        elif all(
            graph_node.status == DurableTaskNodeStatus.DONE
            for graph_node in self._runtime_nodes(runtime_state)
        ):
            worker_next_queue = SchedulerQueueKind.COMPLETED
        elif any(
            graph_node.scheduler_queue == SchedulerQueueKind.WAITING_FOR_APPROVAL
            for graph_node in self._runtime_nodes(runtime_state)
        ):
            worker_next_queue = SchedulerQueueKind.WAITING_FOR_APPROVAL
        else:
            worker_next_queue = SchedulerQueueKind.BLOCKED
        next_actionable_node_id = (
            scheduler_selection.entry.node_id
            if scheduler_selection.entry is not None
            else None
        )
        if scheduler_selection.entry is not None:
            self._set_current_node(runtime_state, scheduler_selection.entry.node_id)

        task_graph = self._runtime_task_graph(runtime_state, objective=objective)
        checkpoint = DurableCheckpoint(
            checkpoint_id=checkpoint_id,
            graph_id=runtime_state["graph_id"],
            run_id=f"{runtime_state['graph_id']}/run-{iteration}",
            current_goal=objective,
            current_task_node_id=node.node_id,
            open_task_node_ids=task_graph.open_task_node_ids(),
            blocked_task_node_ids=task_graph.blocked_task_node_ids(),
            pending_approval_ids=[
                escalation.approval_request_id
                for escalation in runtime_state["escalations"]
                if escalation.approval_request_id
                and escalation.status.name.lower() == "waiting_for_approval"
            ],
            last_successful_artifacts=dict(runtime_state["successful_artifacts"]),
            budget=CheckpointBudget(
                run_budget_remaining=max(0, max_iterations - iteration),
                retry_budget_remaining={
                    key: max(
                        0,
                        self._budget_policy.max_same_failure_retries - value,
                    )
                    for key, value in runtime_state["retry_counters"].items()
                },
                policy=self._budget_policy,
                runtime_hours_used=runtime_hours_used,
                llm_calls_used=runtime_state["llm_calls_used"],
                tool_calls_used=runtime_state["tool_calls_used"],
                same_failure_retries=dict(runtime_state["retry_counters"]),
                repair_depth_used=runtime_state["repair_depth_used"],
                pending_approvals_count=runtime_state["pending_approvals_count"],
                budget_exhausted=budget_exhausted,
                budget_exhausted_reasons=budget_reasons,
            ),
            retry_counters={
                node.node_id: node.retry_count,
            } if node.retry_count > 0 else {},
            trajectory_ids=[],
            replay_references=[{"child_task_id": child_task_id}],
            next_actionable_task_node_id=next_actionable_node_id,
            created_at=_utc_datetime(self._now()),
        )
        job_run = DurableJobRun(
            run_id=f"{runtime_state['graph_id']}/run-{iteration}",
            graph_id=runtime_state["graph_id"],
            node_id=node.node_id,
            goal=objective,
            status=job_run_status_from_verdict(verifier_verdict.verdict),
            attempt=iteration,
            trajectory_id=None,
            replay_reference={"child_task_id": child_task_id},
            checkpoint_id=checkpoint_id,
            scheduler_queue=current_node_queue,
            verifier_verdict=verifier_verdict,
            started_at=_utc_datetime(self._now()),
            ended_at=_utc_datetime(self._now()),
        )
        runtime_state["checkpoints"].append(checkpoint)
        runtime_state["job_runs"].append(job_run)
        durable_execution = self._serialize_runtime_state(
            runtime_state,
            objective=objective,
        )
        return {
            "durable_execution": durable_execution,
            "mission_scorecard": durable_execution.get("mission_scorecard"),
            "failure_type": failure_type,
            "recovery_policy": policy.model_dump(mode="json") if policy is not None else None,
            "recovery_decision": decision.model_dump(mode="json"),
            "scheduler_queue": worker_next_queue.value,
            "node_scheduler_queue": current_node_queue.value,
            "scheduler_queue_entry": selected_queue_entry.model_dump(mode="json"),
            "scheduler_skipped_entries": scheduler_selection.skipped_entries,
            "budget_state": checkpoint.budget.model_dump(mode="json"),
            "checkpoint": checkpoint.model_dump(mode="json"),
            "job_run": job_run.model_dump(mode="json"),
            "abort_reason": abort_reason,
            "escalation_record": (
                escalation_record.model_dump(mode="json")
                if escalation_record is not None
                else None
            ),
        }

    def _record_supervisor_heartbeat(
        self,
        *,
        task_id: str,
        runtime_state: dict[str, Any],
        reason: str,
        status: str = "running",
        emit_event: bool = False,
        objective: str,
    ) -> dict[str, Any]:
        heartbeat = {
            "last_heartbeat_at": self._now(),
            "status": status,
            "reason": reason,
            "active_node_id": str(runtime_state.get("node_id") or ""),
            "scheduler_queue_counts": runtime_state["queue_state"].counts(),
        }
        runtime_state["heartbeat"] = heartbeat
        self._update_task_record(
            task_id,
            artifacts={
                "progress": {
                    "heartbeat": heartbeat,
                    "last_heartbeat_at": heartbeat["last_heartbeat_at"],
                },
                "durable_execution": self._serialize_runtime_state(
                    runtime_state,
                    objective=objective,
                ),
            },
        )
        if emit_event:
            self._append_task_event_record(
                task_id,
                event_type="supervisor_heartbeat",
                status=status,
                title="Supervisor heartbeat",
                payload={
                    "summary": "Supervisor heartbeat recorded.",
                    "heartbeat": heartbeat,
                },
            )
        return heartbeat

    def watchdog_running_supervisors(
        self,
        tasks: list[dict[str, Any]],
        *,
        stale_after_seconds: float = 300.0,
    ) -> list[SupervisorWatchdogFinding]:
        findings: list[SupervisorWatchdogFinding] = []
        for task in tasks:
            finding = self.watchdog_task(
                task,
                stale_after_seconds=stale_after_seconds,
            )
            if finding is not None:
                findings.append(finding)
        return findings

    def watchdog_task(
        self,
        task: dict[str, Any],
        *,
        stale_after_seconds: float = 300.0,
    ) -> SupervisorWatchdogFinding | None:
        task_id = str(task.get("task_id") or "").strip()
        if not task_id or str(task.get("kind") or "") != "control_supervisor":
            return None
        task_status = str(task.get("status") or "unknown")
        if task_status != "running":
            return None
        artifacts = task.get("artifacts") if isinstance(task.get("artifacts"), dict) else {}
        progress = artifacts.get("progress") if isinstance(artifacts.get("progress"), dict) else {}
        if progress.get("stop_requested"):
            self._append_task_event_record(
                task_id,
                event_type="supervisor_watchdog_skipped",
                status=task_status,
                title="Supervisor watchdog skipped",
                payload={
                    "summary": "Watchdog skipped an explicitly stopped supervisor.",
                    "reason": "explicit_stop_requested",
                },
            )
            return None
        heartbeat = progress.get("heartbeat") if isinstance(progress.get("heartbeat"), dict) else {}
        last_heartbeat_at = heartbeat.get("last_heartbeat_at", progress.get("last_heartbeat_at"))
        try:
            heartbeat_ts = (
                float(last_heartbeat_at)
                if last_heartbeat_at is not None
                else None
            )
        except (TypeError, ValueError):
            heartbeat_ts = None
        has_active_handle = (
            task_id in self._handles and not self._handles[task_id].task.done()
        )
        now = self._now()
        stale = heartbeat_ts is None or now - heartbeat_ts > stale_after_seconds
        if not stale and has_active_handle:
            return None

        reason = "missing_heartbeat" if heartbeat_ts is None else "stale_heartbeat"
        if not has_active_handle and heartbeat_ts is not None and not stale:
            reason = "missing_live_handle"
        action = "resume" if not has_active_handle else "require_operator"
        finding = SupervisorWatchdogFinding(
            task_id=task_id,
            status=task_status,
            reason=reason,
            action=action,
            last_heartbeat_at=heartbeat_ts,
            stale_after_seconds=float(stale_after_seconds),
            has_active_handle=has_active_handle,
        )
        payload = {
            "summary": f"Supervisor watchdog detected {reason}; recommended action: {action}.",
            "reason": reason,
            "action": action,
            "last_heartbeat_at": heartbeat_ts,
            "stale_after_seconds": float(stale_after_seconds),
            "has_active_handle": has_active_handle,
        }
        self._update_task_record(
            task_id,
            artifacts={
                "progress": {
                    "watchdog": payload,
                }
            },
            metadata={
                "watchdog_reason": reason,
                "watchdog_action": action,
            },
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_watchdog_warning",
            status=task_status,
            title="Supervisor watchdog warning",
            payload=payload,
        )
        return finding

    async def _run_supervisor(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        objective: str,
        loop_goal: str,
        constraints: list[str],
        control_session_id: str,
        interval_seconds: int,
        max_iterations: int,
        ends_at: float,
        mission_contract: MissionContract,
        stop_requested: asyncio.Event,
        initial_runtime_state: dict[str, Any] | None = None,
        initial_child_task_ids: list[str] | None = None,
        initial_completed_iterations: int = 0,
        resumed: bool = False,
    ) -> None:
        child_task_ids = list(initial_child_task_ids or [])
        completed_iterations = max(0, int(initial_completed_iterations or 0))
        runtime_state = initial_runtime_state or self._initial_runtime_state(
            objective=objective,
            loop_goal=loop_goal,
            control_session_id=control_session_id,
            created_at=self._now(),
            next_run_at=self._now(),
            mission_contract=mission_contract,
        )
        if not resumed:
            self._append_task_event_record(
                task_id,
                event_type="supervisor_started",
                status="running",
                title="Supervisor started",
                payload={
                    "summary": (
                        f"Maintaining objective for up to {max_iterations} iteration(s)."
                    ),
                    "supervisor": {
                        "control_session_id": control_session_id,
                        "max_iterations": max_iterations,
                        "ends_at": ends_at,
                    },
                },
            )
        try:
            try:
                for iteration in range(completed_iterations + 1, max_iterations + 1):
                    if stop_requested.is_set():
                        await self._finish_cancelled(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                        )
                        return

                    now = self._now()
                    if now >= ends_at and completed_iterations > 0:
                        break

                    self._record_supervisor_heartbeat(
                        task_id=task_id,
                        runtime_state=runtime_state,
                        reason="scheduler_selecting",
                        objective=objective,
                        emit_event=(iteration == completed_iterations + 1),
                    )
                    scheduler_selection = self._select_scheduler_entry(
                        runtime_state,
                        now=now,
                    )
                    for skipped_entry in scheduler_selection.skipped_entries:
                        self._append_task_event_record(
                            task_id,
                            event_type="scheduler_worker_stale_entry",
                            status="running",
                            title="Scheduler entry skipped",
                            payload={
                                "summary": "Skipped a stale or expired scheduler queue entry.",
                                "iteration": iteration,
                                **skipped_entry,
                            },
                        )
                    scheduler_entry = scheduler_selection.entry
                    if scheduler_entry is not None:
                        self._set_current_node(runtime_state, scheduler_entry.node_id)
                        self._append_task_event_record(
                            task_id,
                            event_type="scheduler_worker_decision",
                            status="running",
                            title="Scheduler worker decision",
                            payload={
                                "summary": "Selected scheduler queue entry for worker execution.",
                                "iteration": iteration,
                                "queue": scheduler_entry.queue.value,
                                "entry": scheduler_entry.model_dump(mode="json"),
                                "skipped_entries": scheduler_selection.skipped_entries,
                                "queue_counts": runtime_state["queue_state"].counts(),
                            },
                        )
                        available_at = _datetime_timestamp(scheduler_entry.available_at)
                        if available_at is not None and available_at > now:
                            self._record_supervisor_heartbeat(
                                task_id=task_id,
                                runtime_state=runtime_state,
                                reason="scheduler_waiting",
                                objective=objective,
                            )
                            self._update_task_record(
                                task_id,
                                artifacts={
                                    "progress": {
                                        "next_run_at": available_at,
                                    },
                                    "durable_execution": self._serialize_runtime_state(
                                        runtime_state,
                                        objective=objective,
                                    ),
                                },
                            )
                            self._append_task_event_record(
                                task_id,
                                event_type="scheduler_worker_waiting",
                                status="running",
                                title="Scheduler worker waiting",
                                payload={
                                    "summary": "Waiting for the next due scheduler queue entry.",
                                    "iteration": iteration,
                                    "queue": scheduler_entry.queue.value,
                                    "available_at": available_at,
                                    "entry": scheduler_entry.model_dump(mode="json"),
                                },
                            )
                            if await self._wait_for_stop_or_timeout(
                                stop_requested=stop_requested,
                                timeout_seconds=max(0.0, available_at - self._now()),
                            ):
                                await self._finish_cancelled(
                                    task_id=task_id,
                                    owner_session_id=owner_session_id,
                                    user_id=user_id,
                                    completed_iterations=completed_iterations,
                                    child_task_ids=child_task_ids,
                                )
                                return
                        self._append_task_event_record(
                            task_id,
                            event_type="scheduler_worker_tick",
                            status="running",
                            title="Scheduler worker tick",
                            payload={
                                "summary": "Executing due scheduler queue entry.",
                                "iteration": iteration,
                                "queue": scheduler_entry.queue.value,
                                "entry": scheduler_entry.model_dump(mode="json"),
                                "resumed": resumed,
                            },
                        )
                    else:
                        self._append_task_event_record(
                            task_id,
                            event_type="scheduler_worker_noop",
                            status="running",
                            title="Scheduler worker idle",
                            payload={
                                "summary": "No actionable scheduler queue entry remained.",
                                "iteration": iteration,
                                "skipped_entries": scheduler_selection.skipped_entries,
                                "queue_counts": runtime_state["queue_state"].counts(),
                            },
                        )
                        break

                    active_node: DurableTaskNode = runtime_state["node"]
                    active_goal = active_node.description or loop_goal
                    self._update_task_record(
                        task_id,
                        artifacts={
                            "progress": {
                                "iteration": iteration,
                                "next_run_at": now,
                                "child_task_ids": child_task_ids,
                                "active_node_id": active_node.node_id,
                            }
                        },
                    )
                    self._append_task_event_record(
                        task_id,
                        event_type="supervisor_iteration_started",
                        status="running",
                        title=f"Iteration {iteration}",
                        payload={
                            "summary": f"Starting iteration {iteration}.",
                            "iteration": iteration,
                            "active_node_id": active_node.node_id,
                        },
                    )

                    try:
                        result, child_task_id = await self._run_control_loop_with_task(
                            user_id=user_id,
                            session_id=control_session_id,
                            owner_session_id=owner_session_id,
                            goal=active_goal,
                            constraints=constraints,
                            request_id=None,
                            source="supervisor",
                            preserve_control_ui_tab=False,
                            parent_task_id=task_id,
                            reset_if_terminal=False,
                        )
                    except Exception as exc:
                        child_task_id = f"{task_id}/supervisor-exception-{iteration}"
                        result = self._child_exception_result(
                            exc=exc,
                            user_id=user_id,
                            session_id=control_session_id,
                        )
                    child_task_ids.append(child_task_id)
                    completed_iterations = iteration
                    has_more_iterations = (
                        iteration < max_iterations and self._now() < ends_at
                    )
                    scheduled_next_run_at = (
                        min(ends_at, self._now() + float(interval_seconds))
                        if has_more_iterations
                        else None
                    )
                    runtime_report = self._record_runtime_iteration(
                        runtime_state,
                        objective=objective,
                        iteration=iteration,
                        max_iterations=max_iterations,
                        result=result,
                        child_task_id=child_task_id,
                        next_run_at=scheduled_next_run_at,
                        has_more_iterations=has_more_iterations,
                    )
                    result_summary = {
                        "success": result.success,
                        "final_text": result.final_text,
                        "plan_id": result.plan_id,
                        "verification_report_id": result.verification_report_id,
                        "needs_human": bool(result.metadata.get("needs_human")),
                        "child_task_id": child_task_id,
                        "active_node_id": active_node.node_id,
                        "failure_type": runtime_report["failure_type"],
                        "scheduler_queue": runtime_report["scheduler_queue"],
                        "node_scheduler_queue": runtime_report["node_scheduler_queue"],
                        "budget_exhausted": bool(
                            runtime_report["budget_state"].get("budget_exhausted")
                        ),
                    }
                    self._update_task_record(
                        task_id,
                        artifacts={
                            "progress": {
                                "iteration": iteration,
                                "completed_iterations": completed_iterations,
                                "child_task_ids": child_task_ids,
                                "last_child_task_id": child_task_id,
                                "last_result": result_summary,
                            },
                            "durable_execution": runtime_report["durable_execution"],
                        },
                    )
                    self._append_task_event_record(
                        task_id,
                        event_type="supervisor_iteration_completed",
                        status="completed" if result.success else "failed",
                        title=f"Iteration {iteration}",
                        payload={
                            "summary": result.final_text,
                            "iteration": iteration,
                            "child_task_id": child_task_id,
                            "result": result_summary,
                            "runtime": {
                                "recovery_decision": runtime_report["recovery_decision"],
                                "scheduler_queue_entry": runtime_report["scheduler_queue_entry"],
                                "scheduler_skipped_entries": runtime_report[
                                    "scheduler_skipped_entries"
                                ],
                                "budget_state": runtime_report["budget_state"],
                                "escalation_record": runtime_report["escalation_record"],
                            },
                        },
                    )

                    if (
                        runtime_report["scheduler_queue"]
                        == SchedulerQueueKind.WAITING_FOR_APPROVAL.value
                    ):
                        await self._finish_waiting_for_approval(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                            child_task_id=child_task_id,
                            result=result,
                            runtime_report=runtime_report,
                        )
                        return
                    if runtime_report.get("abort_reason"):
                        await self._finish_mission_aborted(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                            child_task_id=child_task_id,
                            result=result,
                            runtime_report=runtime_report,
                        )
                        return
                    if runtime_report["scheduler_queue"] == SchedulerQueueKind.BLOCKED.value:
                        await self._finish_runtime_blocked(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                            child_task_id=child_task_id,
                            result=result,
                            runtime_report=runtime_report,
                        )
                        return

                    if runtime_report["scheduler_queue"] == SchedulerQueueKind.COMPLETED.value:
                        break

                    if iteration >= max_iterations or self._now() >= ends_at:
                        break

                    next_run_at = (
                        self._now()
                        if runtime_report["scheduler_queue"] == SchedulerQueueKind.READY.value
                        else scheduled_next_run_at
                        if scheduled_next_run_at is not None
                        else self._now()
                    )
                    self._record_supervisor_heartbeat(
                        task_id=task_id,
                        runtime_state=runtime_state,
                        reason="supervisor_waiting",
                        objective=objective,
                    )
                    self._update_task_record(
                        task_id,
                        artifacts={
                            "progress": {
                                "next_run_at": next_run_at,
                            }
                        },
                    )
                    self._append_task_event_record(
                        task_id,
                        event_type="supervisor_waiting",
                        status="running",
                        title="Waiting for next iteration",
                        payload={
                            "summary": (
                                "Waiting for the next runtime queue release "
                                f"before iteration {iteration + 1}."
                            ),
                            "iteration": iteration,
                            "next_run_at": next_run_at,
                            "scheduler_queue": runtime_report["scheduler_queue"],
                        },
                    )
                    if await self._wait_for_stop_or_timeout(
                        stop_requested=stop_requested,
                        timeout_seconds=max(0.0, next_run_at - self._now()),
                    ):
                        await self._finish_cancelled(
                            task_id=task_id,
                            owner_session_id=owner_session_id,
                            user_id=user_id,
                            completed_iterations=completed_iterations,
                            child_task_ids=child_task_ids,
                        )
                        return

                final_durable_execution = self._serialize_runtime_state(
                    runtime_state,
                    objective=objective,
                )
                terminal_artifacts = self._terminal_mission_artifacts(
                    task_id=task_id,
                    durable_execution=final_durable_execution,
                    final_status="completed",
                    child_task_ids=child_task_ids,
                )
                self._update_task_record(
                    task_id,
                    status="completed",
                    artifacts={
                        "progress": {
                            "completed_iterations": completed_iterations,
                            "child_task_ids": child_task_ids,
                            "next_run_at": None,
                        },
                        **terminal_artifacts,
                    },
                    metadata={"completed_iterations": completed_iterations},
                    error=None,
                )
                self._append_post_mission_review_event(
                    task_id,
                    final_status="completed",
                    artifacts=terminal_artifacts,
                )
                self._append_task_event_record(
                    task_id,
                    event_type="supervisor_completed",
                    status="completed",
                    title="Supervisor completed",
                    payload={
                        "summary": (
                            "Completed long-running supervision after "
                            f"{completed_iterations} successful iteration(s)."
                        ),
                        "completed_iterations": completed_iterations,
                    },
                )
                await self._emit_session_event(
                    owner_session_id,
                    source=_SUPERVISOR_AGENT_NAME,
                    status="completed",
                    message=(
                        "Long-running control supervisor completed after "
                        f"{completed_iterations} successful iteration(s)."
                    ),
                    user_id=user_id,
                    task_id=task_id,
                    agent_name=_SUPERVISOR_AGENT_NAME,
                )
            except Exception as exc:
                self._update_task_record(
                    task_id,
                    status="failed",
                    artifacts={
                        "progress": {
                            "completed_iterations": completed_iterations,
                            "child_task_ids": child_task_ids,
                            "next_run_at": None,
                        },
                        "result": {
                            "success": False,
                            "final_text": f"Supervisor crashed: {exc}",
                        },
                    },
                    error=f"supervisor_error:{exc}",
                )
                self._append_task_event_record(
                    task_id,
                    event_type="supervisor_error",
                    status="failed",
                    title="Supervisor crashed",
                    payload={
                        "summary": f"Supervisor crashed: {exc}",
                    },
                )
                await self._emit_session_event(
                    owner_session_id,
                    source=_SUPERVISOR_AGENT_NAME,
                    status="failed",
                    message=f"Long-running control supervisor crashed: {exc}",
                    user_id=user_id,
                    task_id=task_id,
                    agent_name=_SUPERVISOR_AGENT_NAME,
                )
                raise
        finally:
            self._handles.pop(task_id, None)

    async def _finish_waiting_for_approval(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        completed_iterations: int,
        child_task_ids: list[str],
        child_task_id: str,
        result: ExecutionResult,
        runtime_report: dict[str, Any],
    ) -> None:
        approval_request = result.metadata.get("approval_request")
        review_artifacts = build_post_mission_review_artifacts(
            runtime_report["durable_execution"],
            final_status="paused",
            source_task_id=task_id,
            child_task_ids=child_task_ids,
            created_at=_utc_datetime(self._now()),
        )
        self._update_task_record(
            task_id,
            status="pending",
            artifacts={
                "progress": {
                    "completed_iterations": completed_iterations,
                    "child_task_ids": child_task_ids,
                    "last_child_task_id": child_task_id,
                    "next_run_at": None,
                },
                "result": {
                    "success": False,
                    "final_text": result.final_text,
                    "blocking_child_task_id": child_task_id,
                    "approval_request": approval_request,
                    "failure_type": runtime_report.get("failure_type"),
                    "scheduler_queue": runtime_report.get("scheduler_queue"),
                },
                **review_artifacts,
            },
            metadata={
                "needs_human": True,
                "blocking_child_task_id": child_task_id,
                "normalized_failure_type": runtime_report.get("failure_type"),
            },
            error="supervisor_needs_human",
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_blocked",
            status="pending",
            title="Supervisor blocked",
            payload={
                "summary": "Supervisor stopped because a child control-loop task requires human approval.",
                "child_task_id": child_task_id,
                "approval_request": approval_request,
                "runtime": {
                    "recovery_decision": runtime_report["recovery_decision"],
                    "scheduler_queue_entry": runtime_report["scheduler_queue_entry"],
                    "escalation_record": runtime_report["escalation_record"],
                },
            },
        )
        self._append_post_mission_review_event(
            task_id,
            final_status="paused",
            artifacts=review_artifacts,
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="blocked",
            message=(
                "Supervisor stopped because a child control-loop task requires human approval."
            ),
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )

    async def _finish_runtime_blocked(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        completed_iterations: int,
        child_task_ids: list[str],
        child_task_id: str,
        result: ExecutionResult,
        runtime_report: dict[str, Any],
    ) -> None:
        terminal_artifacts = self._terminal_mission_artifacts(
            task_id=task_id,
            durable_execution=runtime_report["durable_execution"],
            final_status="blocked",
            child_task_ids=child_task_ids,
        )
        self._update_task_record(
            task_id,
            status="blocked",
            artifacts={
                "progress": {
                    "completed_iterations": completed_iterations,
                    "child_task_ids": child_task_ids,
                    "last_child_task_id": child_task_id,
                    "next_run_at": None,
                },
                "result": {
                    "success": False,
                    "final_text": result.final_text,
                    "blocking_child_task_id": child_task_id,
                    "failure_type": runtime_report.get("failure_type"),
                    "scheduler_queue": runtime_report.get("scheduler_queue"),
                },
                **terminal_artifacts,
            },
            metadata={
                "blocking_child_task_id": child_task_id,
                "normalized_failure_type": runtime_report.get("failure_type"),
            },
            error=(
                result.final_text
                or ",".join(runtime_report["budget_state"].get("budget_exhausted_reasons") or [])
                or "control supervisor blocked"
            ),
            ended_at=self._now(),
        )
        self._append_post_mission_review_event(
            task_id,
            final_status="blocked",
            artifacts=terminal_artifacts,
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_blocked_by_runtime",
            status="blocked",
            title="Supervisor blocked",
            payload={
                "summary": result.final_text,
                "child_task_id": child_task_id,
                "runtime": {
                    "recovery_decision": runtime_report["recovery_decision"],
                    "scheduler_queue_entry": runtime_report["scheduler_queue_entry"],
                    "budget_state": runtime_report["budget_state"],
                },
            },
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="blocked",
            message=result.final_text or "Long-running control supervisor blocked.",
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )

    async def _finish_mission_aborted(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        completed_iterations: int,
        child_task_ids: list[str],
        child_task_id: str,
        result: ExecutionResult,
        runtime_report: dict[str, Any],
    ) -> None:
        abort_reason = str(runtime_report.get("abort_reason") or "").strip()
        terminal_artifacts = self._terminal_mission_artifacts(
            task_id=task_id,
            durable_execution=runtime_report["durable_execution"],
            final_status="failed",
            child_task_ids=child_task_ids,
        )
        self._update_task_record(
            task_id,
            status="failed",
            artifacts={
                "progress": {
                    "completed_iterations": completed_iterations,
                    "child_task_ids": child_task_ids,
                    "last_child_task_id": child_task_id,
                    "next_run_at": None,
                },
                "result": {
                    "success": False,
                    "mission_aborted": True,
                    "abort_reason": abort_reason,
                    "final_text": result.final_text,
                    "blocking_child_task_id": child_task_id,
                    "failure_type": runtime_report.get("failure_type"),
                    "scheduler_queue": runtime_report.get("scheduler_queue"),
                },
                **terminal_artifacts,
            },
            metadata={
                "mission_aborted": True,
                "abort_reason": abort_reason,
                "blocking_child_task_id": child_task_id,
                "normalized_failure_type": runtime_report.get("failure_type"),
            },
            error=f"mission_aborted:{abort_reason or 'abort_condition'}",
            ended_at=self._now(),
        )
        self._append_post_mission_review_event(
            task_id,
            final_status="failed",
            artifacts=terminal_artifacts,
        )
        self._append_task_event_record(
            task_id,
            event_type="mission_aborted",
            status="failed",
            title="Mission aborted",
            payload={
                "summary": f"Mission Contract abort condition matched: {abort_reason}",
                "child_task_id": child_task_id,
                "runtime": {
                    "recovery_decision": runtime_report["recovery_decision"],
                    "scheduler_queue_entry": runtime_report["scheduler_queue_entry"],
                    "budget_state": runtime_report["budget_state"],
                },
            },
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="failed",
            message=f"Mission aborted: {abort_reason}",
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )

    async def _finish_cancelled(
        self,
        *,
        task_id: str,
        owner_session_id: str,
        user_id: str,
        completed_iterations: int,
        child_task_ids: list[str],
    ) -> None:
        self._update_task_record(
            task_id,
            status="cancelled",
            artifacts={
                "progress": {
                    "completed_iterations": completed_iterations,
                    "child_task_ids": child_task_ids,
                    "next_run_at": None,
                    "stop_requested": True,
                },
            },
            metadata={"stop_requested": True},
            error=None,
        )
        self._append_task_event_record(
            task_id,
            event_type="supervisor_cancelled",
            status="cancelled",
            title="Supervisor stopped",
            payload={
                "summary": (
                    f"Supervisor stopped after {completed_iterations} completed iteration(s)."
                ),
                "completed_iterations": completed_iterations,
            },
        )
        await self._emit_session_event(
            owner_session_id,
            source=_SUPERVISOR_AGENT_NAME,
            status="cancelled",
            message=(
                f"Long-running control supervisor stopped after {completed_iterations} completed iteration(s)."
            ),
            user_id=user_id,
            task_id=task_id,
            agent_name=_SUPERVISOR_AGENT_NAME,
        )

    async def _wait_for_stop_or_timeout(
        self,
        *,
        stop_requested: asyncio.Event,
        timeout_seconds: float,
    ) -> bool:
        if stop_requested.is_set():
            return True
        if timeout_seconds <= 0:
            return stop_requested.is_set()
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=timeout_seconds)
            return True
        except TimeoutError:
            return False

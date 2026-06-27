from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from src.runtime.durable_execution_schema import (
    DurableEscalationRecord,
    DurableJobRunStatus,
    DurableTaskNodeStatus,
    DurableVerifierVerdict,
    DurableVerifierVerdictValue,
    EscalationStatus,
    GuardrailBudgetPolicy,
    RecoveryActionType,
    RecoveryBudgetImpact,
    RecoveryDecision,
    RecoveryLadderStep,
    RecoveryOutcome,
    RecoveryPolicy,
    SchedulerQueueEntry,
    SchedulerQueueKind,
    SchedulerQueueState,
)

_DEFAULT_RECOVERY_LADDER = [
    RecoveryLadderStep.OBSERVE_AGAIN,
    RecoveryLadderStep.VERIFY_STATE,
    RecoveryLadderStep.RETRY_SAME_STEP,
    RecoveryLadderStep.RETRY_SMALLER_STEP,
    RecoveryLadderStep.ALTERNATE_CAPABILITY,
    RecoveryLadderStep.DIAGNOSTIC_TASK,
    RecoveryLadderStep.REQUEST_APPROVAL,
    RecoveryLadderStep.PAUSE_OR_BLOCK,
    RecoveryLadderStep.CREATE_IMPROVEMENT_CANDIDATE,
]

_TERMINAL_RECOVERY_STEPS = {
    RecoveryLadderStep.REQUEST_APPROVAL,
    RecoveryLadderStep.PAUSE_OR_BLOCK,
    RecoveryLadderStep.CREATE_IMPROVEMENT_CANDIDATE,
}


def task_node_status_from_verdict(
    verdict: DurableVerifierVerdictValue,
) -> DurableTaskNodeStatus:
    if verdict == DurableVerifierVerdictValue.PASS:
        return DurableTaskNodeStatus.DONE
    if verdict in {
        DurableVerifierVerdictValue.UNCERTAIN,
        DurableVerifierVerdictValue.UNSAFE,
    }:
        return DurableTaskNodeStatus.BLOCKED
    return DurableTaskNodeStatus.FAILED


def job_run_status_from_verdict(
    verdict: DurableVerifierVerdictValue,
) -> DurableJobRunStatus:
    if verdict == DurableVerifierVerdictValue.PASS:
        return DurableJobRunStatus.COMPLETED
    if verdict in {
        DurableVerifierVerdictValue.UNCERTAIN,
        DurableVerifierVerdictValue.UNSAFE,
    }:
        return DurableJobRunStatus.BLOCKED
    return DurableJobRunStatus.FAILED


def default_guardrail_budget_policy(
    budget: dict[str, Any] | None = None,
) -> GuardrailBudgetPolicy:
    payload = budget if isinstance(budget, dict) else {}

    def _int_field(name: str, default: int) -> int:
        raw = payload.get(name, default)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return default

    return GuardrailBudgetPolicy(
        max_runtime_hours=max(1, _int_field("max_runtime_hours", 72)),
        max_total_llm_calls=max(1, _int_field("max_total_llm_calls", 1200)),
        max_total_tool_calls=max(1, _int_field("max_total_tool_calls", 5000)),
        max_same_failure_retries=max(0, _int_field("max_same_failure_retries", 3)),
        max_repair_depth=max(0, _int_field("max_repair_depth", 2)),
        max_pending_approvals=max(0, _int_field("max_pending_approvals", 5)),
    )


def default_recovery_policies() -> dict[str, RecoveryPolicy]:
    return {
        "weak_evidence": RecoveryPolicy(
            failure_type="weak_evidence",
            allowed_actions=[
                RecoveryActionType.GATHER_DESTINATION_EVIDENCE,
                RecoveryActionType.REQUEST_HUMAN_APPROVAL,
            ],
            retry_limit=1,
            escalation_condition="unresolved_uncertain_verdict",
            budget_impact=RecoveryBudgetImpact(tool_calls=1, pending_approvals=1),
            next_scheduler_queue=SchedulerQueueKind.WAITING_FOR_APPROVAL,
        ),
        "focus_mismatch": RecoveryPolicy(
            failure_type="focus_mismatch",
            allowed_actions=[
                RecoveryActionType.RESELECT_SURFACE,
                RecoveryActionType.RETRY_WITH_BACKOFF,
            ],
            retry_limit=2,
            escalation_condition="retry_limit_exhausted",
            budget_impact=RecoveryBudgetImpact(tool_calls=1),
            next_scheduler_queue=SchedulerQueueKind.RETRY_LATER,
        ),
        "wrong_surface": RecoveryPolicy(
            failure_type="wrong_surface",
            allowed_actions=[
                RecoveryActionType.SWITCH_SURFACE,
                RecoveryActionType.RETRY_WITH_BACKOFF,
            ],
            retry_limit=2,
            escalation_condition="retry_limit_exhausted",
            budget_impact=RecoveryBudgetImpact(tool_calls=1),
            next_scheduler_queue=SchedulerQueueKind.RETRY_LATER,
        ),
        "target_context_mismatch": RecoveryPolicy(
            failure_type="target_context_mismatch",
            allowed_actions=[
                RecoveryActionType.SWITCH_SURFACE,
                RecoveryActionType.RETRY_WITH_BACKOFF,
            ],
            retry_limit=2,
            escalation_condition="retry_limit_exhausted",
            budget_impact=RecoveryBudgetImpact(tool_calls=1),
            next_scheduler_queue=SchedulerQueueKind.RETRY_LATER,
        ),
        "unknown": RecoveryPolicy(
            failure_type="unknown",
            allowed_actions=[
                RecoveryActionType.INSPECT_REPLAY,
                RecoveryActionType.MARK_FAILED,
            ],
            retry_limit=0,
            escalation_condition="manual_triage_required",
            budget_impact=RecoveryBudgetImpact(),
            next_scheduler_queue=SchedulerQueueKind.BLOCKED,
        ),
        "policy_blocked": RecoveryPolicy(
            failure_type="policy_blocked",
            allowed_actions=[RecoveryActionType.REQUEST_HUMAN_APPROVAL],
            retry_limit=0,
            escalation_condition="approval_required",
            budget_impact=RecoveryBudgetImpact(pending_approvals=1),
            next_scheduler_queue=SchedulerQueueKind.WAITING_FOR_APPROVAL,
        ),
        "tool_timeout": RecoveryPolicy(
            failure_type="tool_timeout",
            allowed_actions=[RecoveryActionType.RETRY_WITH_BACKOFF],
            retry_limit=2,
            escalation_condition="backoff_exhausted",
            budget_impact=RecoveryBudgetImpact(tool_calls=1),
            next_scheduler_queue=SchedulerQueueKind.RETRY_LATER,
        ),
    }


def recovery_policy_for_failure_type(
    failure_type: str | None,
) -> RecoveryPolicy | None:
    if not str(failure_type or "").strip():
        return None
    policies = default_recovery_policies()
    return policies.get(str(failure_type or "unknown"), policies["unknown"])


def budget_exhaustion_reasons(
    *,
    budget_policy: GuardrailBudgetPolicy,
    runtime_hours_used: int,
    llm_calls_used: int,
    tool_calls_used: int,
    repair_depth_used: int,
    pending_approvals_count: int,
    next_scheduler_queue: SchedulerQueueKind,
    failure_type: str | None,
    retry_count: int,
) -> list[str]:
    reasons: list[str] = []
    if runtime_hours_used > budget_policy.max_runtime_hours:
        reasons.append("max_runtime_hours_exhausted")
    if llm_calls_used > budget_policy.max_total_llm_calls:
        reasons.append("max_total_llm_calls_exhausted")
    if tool_calls_used > budget_policy.max_total_tool_calls:
        reasons.append("max_total_tool_calls_exhausted")
    if repair_depth_used > budget_policy.max_repair_depth:
        reasons.append("max_repair_depth_exhausted")
    if pending_approvals_count > budget_policy.max_pending_approvals:
        reasons.append("max_pending_approvals_exhausted")
    if (
        next_scheduler_queue == SchedulerQueueKind.RETRY_LATER
        and failure_type
        and retry_count >= budget_policy.max_same_failure_retries
    ):
        reasons.append("max_same_failure_retries_exhausted")
    return reasons


def repair_depth_increment(chosen_action: RecoveryActionType | None) -> int:
    if chosen_action in {
        RecoveryActionType.RESELECT_SURFACE,
        RecoveryActionType.GATHER_DESTINATION_EVIDENCE,
        RecoveryActionType.SWITCH_SURFACE,
        RecoveryActionType.RETRY_WITH_BACKOFF,
        RecoveryActionType.INSPECT_REPLAY,
    }:
        return 1
    return 0


def recovery_ladder_step_for_decision(
    *,
    chosen_action: RecoveryActionType | None,
    next_scheduler_queue: SchedulerQueueKind,
    budget_exhausted: bool = False,
) -> RecoveryLadderStep | None:
    # This is the compatibility adapter from existing recovery actions/queues to
    # the abstract ladder vocabulary. `select_recovery_ladder_step()` remains the
    # final mission-policy and budget-aware selection authority.
    if budget_exhausted:
        return RecoveryLadderStep.PAUSE_OR_BLOCK
    if next_scheduler_queue == SchedulerQueueKind.COMPLETED:
        return None
    if chosen_action == RecoveryActionType.GATHER_DESTINATION_EVIDENCE:
        return RecoveryLadderStep.VERIFY_STATE
    if chosen_action == RecoveryActionType.RESELECT_SURFACE:
        return RecoveryLadderStep.OBSERVE_AGAIN
    if chosen_action == RecoveryActionType.SWITCH_SURFACE:
        return RecoveryLadderStep.ALTERNATE_CAPABILITY
    if chosen_action == RecoveryActionType.RETRY_WITH_BACKOFF:
        return RecoveryLadderStep.RETRY_SMALLER_STEP
    if chosen_action == RecoveryActionType.INSPECT_REPLAY:
        return RecoveryLadderStep.DIAGNOSTIC_TASK
    if chosen_action == RecoveryActionType.REQUEST_HUMAN_APPROVAL:
        return RecoveryLadderStep.REQUEST_APPROVAL
    if chosen_action == RecoveryActionType.MARK_FAILED:
        return RecoveryLadderStep.PAUSE_OR_BLOCK
    if next_scheduler_queue in {
        SchedulerQueueKind.BLOCKED,
        SchedulerQueueKind.WAITING_FOR_APPROVAL,
    }:
        return RecoveryLadderStep.PAUSE_OR_BLOCK
    return RecoveryLadderStep.RETRY_SAME_STEP


def normalize_recovery_ladder(
    ladder: list[str] | tuple[str, ...] | None,
) -> list[RecoveryLadderStep]:
    steps: list[RecoveryLadderStep] = []
    for item in ladder or []:
        try:
            step = RecoveryLadderStep(str(item).strip())
        except ValueError:
            continue
        if step not in steps:
            steps.append(step)
    return steps or list(_DEFAULT_RECOVERY_LADDER)


def select_recovery_ladder_step(
    *,
    preferred_step: RecoveryLadderStep | None,
    ladder: list[str] | tuple[str, ...] | None,
    retry_count: int,
    max_retries_per_step: int,
    budget_exhausted: bool = False,
) -> tuple[RecoveryLadderStep | None, str]:
    if budget_exhausted:
        return RecoveryLadderStep.PAUSE_OR_BLOCK, "budget_exhausted"
    if preferred_step is None:
        return None, "no_recovery_needed"

    allowed_steps = normalize_recovery_ladder(ladder)
    if preferred_step not in allowed_steps:
        fallback = allowed_steps[0]
        return fallback, "preferred_step_not_allowed_by_mission_policy"

    if retry_count >= max(0, int(max_retries_per_step)):
        preferred_index = allowed_steps.index(preferred_step)
        later_steps = allowed_steps[preferred_index + 1 :] + allowed_steps[:preferred_index]
        for step in later_steps:
            if step in _TERMINAL_RECOVERY_STEPS:
                return step, "mission_recovery_step_retry_limit_exhausted"
        return RecoveryLadderStep.PAUSE_OR_BLOCK, "mission_recovery_step_retry_limit_exhausted"

    return preferred_step, "mission_recovery_policy_selected"


def recovery_action_for_ladder_step(
    step: RecoveryLadderStep | None,
    *,
    fallback: RecoveryActionType | None = None,
) -> RecoveryActionType | None:
    if step == RecoveryLadderStep.OBSERVE_AGAIN:
        return RecoveryActionType.RESELECT_SURFACE
    if step == RecoveryLadderStep.VERIFY_STATE:
        return RecoveryActionType.GATHER_DESTINATION_EVIDENCE
    if step in {
        RecoveryLadderStep.RETRY_SAME_STEP,
        RecoveryLadderStep.RETRY_SMALLER_STEP,
    }:
        return RecoveryActionType.RETRY_WITH_BACKOFF
    if step == RecoveryLadderStep.ALTERNATE_CAPABILITY:
        return RecoveryActionType.SWITCH_SURFACE
    if step == RecoveryLadderStep.DIAGNOSTIC_TASK:
        return RecoveryActionType.INSPECT_REPLAY
    if step == RecoveryLadderStep.REQUEST_APPROVAL:
        return RecoveryActionType.REQUEST_HUMAN_APPROVAL
    if step in {
        RecoveryLadderStep.PAUSE_OR_BLOCK,
        RecoveryLadderStep.CREATE_IMPROVEMENT_CANDIDATE,
    }:
        return RecoveryActionType.MARK_FAILED
    return fallback


def scheduler_queue_for_recovery_ladder_step(
    step: RecoveryLadderStep | None,
    *,
    fallback: SchedulerQueueKind,
) -> SchedulerQueueKind:
    if step is None:
        return fallback
    if step == RecoveryLadderStep.REQUEST_APPROVAL:
        return SchedulerQueueKind.WAITING_FOR_APPROVAL
    if step in {
        RecoveryLadderStep.PAUSE_OR_BLOCK,
        RecoveryLadderStep.CREATE_IMPROVEMENT_CANDIDATE,
    }:
        return SchedulerQueueKind.BLOCKED
    if step in {
        RecoveryLadderStep.OBSERVE_AGAIN,
        RecoveryLadderStep.VERIFY_STATE,
        RecoveryLadderStep.RETRY_SAME_STEP,
        RecoveryLadderStep.RETRY_SMALLER_STEP,
        RecoveryLadderStep.ALTERNATE_CAPABILITY,
        RecoveryLadderStep.DIAGNOSTIC_TASK,
    }:
        return fallback
    return fallback


def recovery_outcome_for_queue(
    queue: SchedulerQueueKind,
    *,
    result_success: bool,
    aborted: bool = False,
    cancelled: bool = False,
) -> RecoveryOutcome:
    if cancelled:
        return RecoveryOutcome.CANCELLED
    if aborted:
        return RecoveryOutcome.FAILED
    if queue == SchedulerQueueKind.WAITING_FOR_APPROVAL:
        return RecoveryOutcome.PAUSED
    if queue == SchedulerQueueKind.BLOCKED:
        return RecoveryOutcome.BLOCKED
    if queue == SchedulerQueueKind.COMPLETED or result_success:
        return RecoveryOutcome.COMPLETED
    return RecoveryOutcome.RECOVERY_SCHEDULED


def scheduler_available_at(
    queue: SchedulerQueueKind,
    *,
    created_at: datetime,
) -> datetime | None:
    if queue == SchedulerQueueKind.RETRY_LATER:
        return (created_at + timedelta(minutes=15)).replace(second=0, microsecond=0)
    if queue == SchedulerQueueKind.PERIODIC_CHECK:
        return (created_at + timedelta(hours=1)).replace(second=0, microsecond=0)
    return None


def scheduler_queue_reason(
    decision: RecoveryDecision,
    *,
    verifier_verdict: DurableVerifierVerdict,
) -> str:
    if decision.budget_exhausted:
        return "guardrail_budget_exhausted"
    if decision.next_scheduler_queue == SchedulerQueueKind.COMPLETED:
        return "verifier_passed"
    if decision.next_scheduler_queue == SchedulerQueueKind.WAITING_FOR_APPROVAL:
        if verifier_verdict.verdict == DurableVerifierVerdictValue.UNCERTAIN:
            return "unresolved_uncertain_verifier_result"
        if verifier_verdict.verdict == DurableVerifierVerdictValue.UNSAFE:
            return "unsafe_verifier_boundary"
        return "human_approval_required"
    if decision.next_scheduler_queue == SchedulerQueueKind.RETRY_LATER:
        return f"{decision.failure_type or 'unknown'}_retry_later"
    if decision.next_scheduler_queue == SchedulerQueueKind.PERIODIC_CHECK:
        return f"{decision.failure_type or 'unknown'}_periodic_check"
    if decision.next_scheduler_queue == SchedulerQueueKind.READY:
        return "ready_for_worker"
    return f"{decision.failure_type or 'unknown'}_blocked"


def append_scheduler_queue_entry(
    queue_state: SchedulerQueueState,
    entry: SchedulerQueueEntry,
) -> None:
    if entry.queue == SchedulerQueueKind.READY:
        queue_state.ready_queue.append(entry)
    elif entry.queue == SchedulerQueueKind.WAITING_FOR_APPROVAL:
        queue_state.waiting_for_approval_queue.append(entry)
    elif entry.queue == SchedulerQueueKind.RETRY_LATER:
        queue_state.retry_later_queue.append(entry)
    elif entry.queue == SchedulerQueueKind.PERIODIC_CHECK:
        queue_state.periodic_check_queue.append(entry)
    elif entry.queue == SchedulerQueueKind.COMPLETED:
        queue_state.completed_queue.append(entry)
    else:
        queue_state.blocked_queue.append(entry)


def build_escalation_record(
    *,
    run_id: str,
    node_id: str,
    checkpoint_id: str,
    created_at: datetime,
    failure_type: str | None,
    reason: str,
    approval_request_id: str | None = None,
    audit_ref: str | None = None,
) -> DurableEscalationRecord:
    resolved_approval_request_id = approval_request_id or f"approval:{run_id}"
    return DurableEscalationRecord(
        escalation_id=f"{run_id}/escalation",
        node_id=node_id,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        status=EscalationStatus.WAITING_FOR_APPROVAL,
        reason=reason,
        failure_type=failure_type,
        approval_request_id=resolved_approval_request_id,
        audit_ref=audit_ref or f"audit://{run_id}/approval",
        resume_checkpoint_id=checkpoint_id,
        created_at=created_at,
        updated_at=created_at,
    )

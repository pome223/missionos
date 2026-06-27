from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DurableTaskNodeStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


class DurableJobRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class DurableVerifierVerdictValue(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"
    UNSAFE = "unsafe"


class SchedulerQueueKind(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RETRY_LATER = "retry_later"
    PERIODIC_CHECK = "periodic_check"
    COMPLETED = "completed"


class RecoveryActionType(str, Enum):
    RESELECT_SURFACE = "reselect_surface"
    GATHER_DESTINATION_EVIDENCE = "gather_destination_evidence"
    SWITCH_SURFACE = "switch_surface"
    REQUEST_HUMAN_APPROVAL = "request_human_approval"
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    INSPECT_REPLAY = "inspect_replay"
    MARK_FAILED = "mark_failed"


class RecoveryLadderStep(str, Enum):
    OBSERVE_AGAIN = "observe_again"
    VERIFY_STATE = "verify_state"
    RETRY_SAME_STEP = "retry_same_step"
    RETRY_SMALLER_STEP = "retry_smaller_step"
    ALTERNATE_CAPABILITY = "alternate_capability"
    DIAGNOSTIC_TASK = "diagnostic_task"
    REQUEST_APPROVAL = "request_approval"
    PAUSE_OR_BLOCK = "pause_or_block"
    CREATE_IMPROVEMENT_CANDIDATE = "create_improvement_candidate"


class RecoveryOutcome(str, Enum):
    COMPLETED = "completed"
    RECOVERY_SCHEDULED = "recovery_scheduled"
    PAUSED = "paused"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EscalationStatus(str, Enum):
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class DurableArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    ref: str
    label: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class DurableVerifierVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: DurableVerifierVerdictValue
    evidence_refs: list[str] = Field(default_factory=list)
    failure_type: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_source: str = ""
    verifier_source: str = ""
    recommended_repair_target: str | None = None
    trajectory_id: int | None = None
    replay_reference: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)


class DurableTaskNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    title: str
    description: str = ""
    status: DurableTaskNodeStatus = DurableTaskNodeStatus.READY
    depends_on: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    artifacts: list[DurableArtifactRef] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    next_retry_at: datetime | None = None
    scheduler_queue: SchedulerQueueKind | None = None
    trajectory_ids: list[int] = Field(default_factory=list)
    replay_references: list[dict[str, Any]] = Field(default_factory=list)
    checkpoint_refs: list[str] = Field(default_factory=list)
    verifier_verdict: DurableVerifierVerdict | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_open(self) -> bool:
        if self.scheduler_queue == SchedulerQueueKind.COMPLETED:
            return False
        return self.status in {
            DurableTaskNodeStatus.READY,
            DurableTaskNodeStatus.RUNNING,
            DurableTaskNodeStatus.FAILED,
            DurableTaskNodeStatus.BLOCKED,
        }


class DurableTaskGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph_id: str
    goal: str
    nodes: list[DurableTaskNode] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def open_task_node_ids(self) -> list[str]:
        return [node.node_id for node in self.nodes if node.is_open()]

    def blocked_task_node_ids(self) -> list[str]:
        return [
            node.node_id
            for node in self.nodes
            if node.status == DurableTaskNodeStatus.BLOCKED
            or node.scheduler_queue in {
                SchedulerQueueKind.BLOCKED,
                SchedulerQueueKind.WAITING_FOR_APPROVAL,
                SchedulerQueueKind.RETRY_LATER,
                SchedulerQueueKind.PERIODIC_CHECK,
            }
        ]

    def next_actionable_task_node_id(self) -> str | None:
        for node in self.nodes:
            if node.scheduler_queue == SchedulerQueueKind.READY:
                return node.node_id
        for preferred_status in (
            DurableTaskNodeStatus.READY,
            DurableTaskNodeStatus.FAILED,
            DurableTaskNodeStatus.RUNNING,
        ):
            for node in self.nodes:
                if node.scheduler_queue is None and node.status == preferred_status:
                    return node.node_id
        return None


class DurableJobRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    graph_id: str
    node_id: str
    goal: str
    status: DurableJobRunStatus
    attempt: int = Field(default=1, ge=1)
    trajectory_id: int | None = None
    replay_reference: dict[str, Any] = Field(default_factory=dict)
    checkpoint_id: str | None = None
    scheduler_queue: SchedulerQueueKind | None = None
    verifier_verdict: DurableVerifierVerdict | None = None
    started_at: datetime = Field(default_factory=_utc_now)
    ended_at: datetime | None = None


class GuardrailBudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_runtime_hours: int = Field(default=72, ge=1)
    max_total_llm_calls: int = Field(default=1200, ge=1)
    max_total_tool_calls: int = Field(default=5000, ge=1)
    max_same_failure_retries: int = Field(default=3, ge=0)
    max_repair_depth: int = Field(default=2, ge=0)
    max_pending_approvals: int = Field(default=5, ge=0)


class CheckpointBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_budget_remaining: int | None = Field(default=None, ge=0)
    retry_budget_remaining: dict[str, int] = Field(default_factory=dict)
    policy: GuardrailBudgetPolicy = Field(default_factory=GuardrailBudgetPolicy)
    runtime_hours_used: int = Field(default=0, ge=0)
    llm_calls_used: int = Field(default=0, ge=0)
    tool_calls_used: int = Field(default=0, ge=0)
    same_failure_retries: dict[str, int] = Field(default_factory=dict)
    repair_depth_used: int = Field(default=0, ge=0)
    pending_approvals_count: int = Field(default=0, ge=0)
    budget_exhausted: bool = False
    budget_exhausted_reasons: list[str] = Field(default_factory=list)


class RecoveryBudgetImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    repairs: int = Field(default=0, ge=0)
    pending_approvals: int = Field(default=0, ge=0)


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_type: str
    allowed_actions: list[RecoveryActionType] = Field(default_factory=list)
    retry_limit: int = Field(default=0, ge=0)
    escalation_condition: str = ""
    budget_impact: RecoveryBudgetImpact = Field(default_factory=RecoveryBudgetImpact)
    next_scheduler_queue: SchedulerQueueKind = SchedulerQueueKind.BLOCKED


class RecoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "recovery_decision.v1"
    node_id: str
    failure_type: str | None = None
    chosen_action: RecoveryActionType | None = None
    recovery_ladder_step: RecoveryLadderStep | None = None
    selected_step: RecoveryLadderStep | None = None
    reason: str = ""
    attempt_index: int = Field(default=0, ge=0)
    budget_before: dict[str, Any] = Field(default_factory=dict)
    budget_after: dict[str, Any] = Field(default_factory=dict)
    outcome: RecoveryOutcome | None = None
    budget_consumption: RecoveryBudgetImpact = Field(default_factory=RecoveryBudgetImpact)
    source_refs: list[str] = Field(default_factory=list)
    policy: RecoveryPolicy | None = None
    next_scheduler_queue: SchedulerQueueKind
    budget_exhausted: bool = False
    budget_exhausted_reasons: list[str] = Field(default_factory=list)


class SchedulerQueueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str
    node_id: str
    queue: SchedulerQueueKind
    reason: str = ""
    available_at: datetime | None = None
    checkpoint_id: str | None = None
    trajectory_ids: list[int] = Field(default_factory=list)
    escalation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SchedulerQueueState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready_queue: list[SchedulerQueueEntry] = Field(default_factory=list)
    blocked_queue: list[SchedulerQueueEntry] = Field(default_factory=list)
    waiting_for_approval_queue: list[SchedulerQueueEntry] = Field(default_factory=list)
    retry_later_queue: list[SchedulerQueueEntry] = Field(default_factory=list)
    periodic_check_queue: list[SchedulerQueueEntry] = Field(default_factory=list)
    completed_queue: list[SchedulerQueueEntry] = Field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "ready": len(self.ready_queue),
            "blocked": len(self.blocked_queue),
            "waiting_for_approval": len(self.waiting_for_approval_queue),
            "retry_later": len(self.retry_later_queue),
            "periodic_check": len(self.periodic_check_queue),
            "completed": len(self.completed_queue),
        }


class DurableEscalationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    escalation_id: str
    node_id: str
    run_id: str
    checkpoint_id: str
    status: EscalationStatus = EscalationStatus.WAITING_FOR_APPROVAL
    reason: str = ""
    failure_type: str | None = None
    approval_request_id: str | None = None
    audit_ref: str = ""
    resume_checkpoint_id: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class DurableResumeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str
    graph_id: str
    next_actionable_task_node_id: str | None = None
    open_task_node_ids: list[str] = Field(default_factory=list)
    blocked_task_node_ids: list[str] = Field(default_factory=list)
    pending_approval_ids: list[str] = Field(default_factory=list)
    scheduler_queue_counts: dict[str, int] = Field(default_factory=dict)
    reason: str = ""


class DurableCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str
    graph_id: str
    run_id: str
    current_goal: str
    current_task_node_id: str | None = None
    open_task_node_ids: list[str] = Field(default_factory=list)
    blocked_task_node_ids: list[str] = Field(default_factory=list)
    pending_approval_ids: list[str] = Field(default_factory=list)
    last_successful_artifacts: dict[str, list[DurableArtifactRef]] = Field(default_factory=dict)
    budget: CheckpointBudget = Field(default_factory=CheckpointBudget)
    retry_counters: dict[str, int] = Field(default_factory=dict)
    trajectory_ids: list[int] = Field(default_factory=list)
    replay_references: list[dict[str, Any]] = Field(default_factory=list)
    next_actionable_task_node_id: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)

    def resume_state(self, task_graph: DurableTaskGraph) -> DurableResumeState:
        next_actionable = self.next_actionable_task_node_id
        if not next_actionable:
            next_actionable = task_graph.next_actionable_task_node_id()

        reason = ""
        if next_actionable:
            reason = "resume_from_open_task"
        elif self.pending_approval_ids:
            reason = "awaiting_approval"
        elif self.blocked_task_node_ids:
            reason = "awaiting_unblock_or_human_input"
        else:
            reason = "graph_complete"

        return DurableResumeState(
            checkpoint_id=self.checkpoint_id,
            graph_id=self.graph_id,
            next_actionable_task_node_id=next_actionable,
            open_task_node_ids=list(self.open_task_node_ids),
            blocked_task_node_ids=list(self.blocked_task_node_ids),
            pending_approval_ids=list(self.pending_approval_ids),
            scheduler_queue_counts={},
            reason=reason,
        )

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.replay_schema import StepComparePayload, TaskReplayRequest, TaskResultSnapshot
from src.runtime.mission_contract import MissionContract


class PaginationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int
    page_size: int
    total: int
    has_more: bool


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    kind: str
    title: str
    status: str
    owner_session_id: str | None = None
    owner_user_id: str | None = None
    parent_task_id: str | None = None
    run_id: str | None = None
    winner_task_id: str | None = None
    loser_task_ids: list[str] = Field(default_factory=list)
    approval_dependencies: list[str] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: float
    updated_at: float
    started_at: float | None = None
    ended_at: float | None = None


class TaskQueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_session_id: str | None = None
    owner_user_id: str | None = None
    kind: str | None = None
    status: str | None = None
    parent_task_id: str | None = None
    q: str | None = None


class TaskQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskRecord] = Field(default_factory=list)
    pagination: PaginationPayload
    filters: TaskQueryFilters


class TaskEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: TaskRecord


class TaskTimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeline_id: str
    kind: Literal["task_event", "approval", "audit"]
    timestamp: float
    title: str
    status: str
    event_type: str
    summary: str
    task_id: str | None = None
    request_id: str | None = None
    source_request_id: str | None = None
    audit_entry_id: str | None = None
    payload: dict[str, Any] | None = None
    task_event: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    history_entry: dict[str, Any] | None = None
    audit_focus: dict[str, Any] | None = None
    entry: dict[str, Any] | None = None


class TaskTimelineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: TaskRecord
    entries: list[TaskTimelineEntry] = Field(default_factory=list)
    pagination: PaginationPayload


class TaskReplayAcceptedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    task: TaskRecord
    replay_of_task_id: str
    compare_to_task_id: str
    replay_from_step: str | None = None
    replay_mode: Literal["full", "tail"]


class ControlSupervisorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    session_id: str | None = None
    goal: str | None = None
    mission_contract: MissionContract | None = None
    approved_promotion_artifacts: dict[str, Any] | list[Any] | None = None
    constraints: list[str] = Field(default_factory=list)
    duration_seconds: int = Field(default=3600, ge=60, le=86_400)
    interval_seconds: int = Field(default=60, ge=5, le=3_600)
    maintenance_goal: str | None = None


class ControlSupervisorAcceptedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    task: TaskRecord
    control_session_id: str
    duration_seconds: int
    interval_seconds: int
    max_iterations: int
    ends_at: float
    next_run_at: float
    mission_contract: dict[str, Any] | None = None
    reuse_plan: dict[str, Any] | None = None


class TaskCancelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    task: TaskRecord
    message: str


class TimelineSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    kind_counts: dict[str, int] = Field(default_factory=dict)
    entries: list[TaskTimelineEntry] = Field(default_factory=list)


class TaskCompareSide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    result: TaskResultSnapshot
    timeline: TimelineSummary


class TaskCompareResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_task: TaskRecord
    right_task: TaskRecord
    left: TaskCompareSide
    right: TaskCompareSide
    summary: list[str] = Field(default_factory=list)
    step_compare: StepComparePayload


class StepCriterionCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    count: int


class StepFailureRankingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    title: str
    total: int
    succeeded: int
    failed: int
    preserved: int
    other: int
    failure_rate: float
    task_count: int
    top_failed_criteria: list[StepCriterionCount] = Field(default_factory=list)


class StepFailureRankingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[StepFailureRankingEntry] = Field(default_factory=list)
    total_events: int
    sampled_events: int
    truncated: bool


class ReplayImprovementEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    title: str
    source_fail: int
    replay_pass: int
    replay_fail: int
    pair_count: int
    improvement_rate: float


class ReplayImprovementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[ReplayImprovementEntry] = Field(default_factory=list)
    total_replay_tasks: int
    sampled_replay_tasks: int
    truncated: bool


class TaskAnalyticsOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_tasks: int
    by_status: dict[str, int] = Field(default_factory=dict)
    total_replays: int
    replay_success_rate: float


class TaskAnalyticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overview: TaskAnalyticsOverview
    step_failure_ranking: StepFailureRankingPayload
    replay_improvement: ReplayImprovementPayload


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str
    timestamp: float
    datetime: str
    event_type: str
    user_id: str | None = None
    session_id: str | None = None
    action: str | None = None
    resource: str | None = None
    result: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditQueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_user_id: str | None = None
    session_id: str | None = None
    tool: str | None = None
    source: str | None = None
    result: str | None = None
    q: str | None = None


class AuditQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[AuditEntry] = Field(default_factory=list)
    pagination: PaginationPayload
    filters: AuditQueryFilters


__all__ = [
    "AuditQueryResponse",
    "TaskAnalyticsResponse",
    "TaskCompareResponse",
    "TaskEnvelope",
    "TaskQueryResponse",
    "TaskReplayAcceptedResponse",
    "TaskReplayRequest",
    "TaskTimelineResponse",
]

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReplayMode(str, Enum):
    FULL = "full"
    TAIL = "tail"


class StepTraceType(str, Enum):
    PLAN = "plan"
    VERIFICATION = "verification"
    REPAIR = "repair"


class ReplayScope(str, Enum):
    PRESERVED = "preserved"
    REPLAYED = "replayed"


class StepTraceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    title: str
    description: str = ""
    step_type: StepTraceType
    status: str
    tool: str = ""
    artifact_ref: str = ""
    output_summary: str = ""
    replay_scope: ReplayScope | None = None
    failed_criteria: list[str] = Field(default_factory=list)
    repair_actions: list[dict[str, Any]] = Field(default_factory=list)
    failure_type: str | None = None
    overall_score: float | None = Field(default=None, ge=0.0, le=1.0)
    target_step_ids: list[str] = Field(default_factory=list)


class ReplayContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_task_id: str
    from_step: str
    mode: ReplayMode = ReplayMode.TAIL
    previous_verification_status: str | None = None
    previous_failed_criteria: list[str] = Field(default_factory=list)
    step_trace: list[StepTraceEntry] = Field(default_factory=list)


class TaskReplayRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    from_step: str | None = None

    @field_validator("from_step", mode="before")
    @classmethod
    def _normalize_from_step(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None


class StepCompareRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    title: str
    left: StepTraceEntry | None = None
    right: StepTraceEntry | None = None
    changed: bool


class StepComparePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(default=0, ge=0)
    changed: int = Field(default=0, ge=0)
    rows: list[StepCompareRow] = Field(default_factory=list)


class TaskResultSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = False
    final_text: str = ""
    verification_report_id: str | None = None
    verification_status: str = ""
    overall_score: float = Field(default=0.0, ge=0.0, le=1.0)
    repair_count: int = Field(default=0, ge=0)
    artifact_refs: list[str] = Field(default_factory=list)
    screenshot_refs: list[str] = Field(default_factory=list)
    failed_criteria: list[str] = Field(default_factory=list)
    passed_criteria_count: int = Field(default=0, ge=0)
    criteria_count: int = Field(default=0, ge=0)
    verification_report: dict[str, Any] = Field(default_factory=dict)
    verification_inputs: dict[str, Any] = Field(default_factory=dict)
    approved_plan: dict[str, Any] = Field(default_factory=dict)
    step_trace: list[StepTraceEntry] = Field(default_factory=list)
    tail_replay_from_step_id: str = ""

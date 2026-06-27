from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    AUTO_APPROVED = "auto_approved"
    POLICY_APPROVED = "policy_approved"
    HUMAN_APPROVED = "human_approved"
    DENIED = "denied"


class CapabilityMode(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    ADMIN = "admin"


class CapabilityRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="e.g. file.read, web.search, browser.navigate")
    mode: CapabilityMode
    scope: str | None = Field(default=None, description="Optional scope restriction")
    required: bool = True
    reason: str | None = None


class SuccessCriterionType(str, Enum):
    COUNT = "count"
    FORMAT = "format"
    EVIDENCE = "evidence"
    GROUNDEDNESS = "groundedness"
    DEDUPLICATION = "deduplication"
    POLICY = "policy"
    CUSTOM = "custom"


class SuccessCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    criterion_type: SuccessCriterionType
    description: str
    threshold: float | int | str | None = None
    required: bool = True


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    title: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    capabilities: list[CapabilityRequirement] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    status: PlanStepStatus = PlanStepStatus.PENDING
    retryable: bool = True


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    session_id: str
    user_id: str

    goal: str
    constraints: list[str] = Field(default_factory=list)
    subgoals: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)

    required_capabilities: list[CapabilityRequirement] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    approval_status: ApprovalStatus = ApprovalStatus.PENDING

    created_at: datetime
    updated_at: datetime
    planner_agent_id: str | None = None
    approved_by: str | None = None

    max_repair_attempts: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("max_repair_attempts")
    @classmethod
    def validate_repair_attempts(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_repair_attempts must be >= 0")
        return v

    def ready_steps(self) -> list[PlanStep]:
        completed = {
            step.step_id
            for step in self.steps
            if step.status in {PlanStepStatus.SUCCEEDED, PlanStepStatus.SKIPPED}
        }
        return [
            step
            for step in self.steps
            if step.status in {PlanStepStatus.PENDING, PlanStepStatus.READY}
            and all(dep in completed for dep in step.depends_on)
        ]

    def is_complete(self) -> bool:
        return all(
            step.status in {PlanStepStatus.SUCCEEDED, PlanStepStatus.SKIPPED}
            for step in self.steps
        )

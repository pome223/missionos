from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VerificationStatus(str, Enum):
    PASS = "pass"
    PARTIAL_PASS = "partial_pass"
    FAIL = "fail"
    ERROR = "error"


class FailureType(str, Enum):
    TOOL_FAILURE = "tool_failure"
    PLAN_FAILURE = "plan_failure"
    FORMAT_FAILURE = "format_failure"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    POLICY_DENIED = "policy_denied"
    MEMORY_CONFLICT = "memory_conflict"
    UNKNOWN = "unknown"


class CriterionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    explanation: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class RepairAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    action_type: Literal[
        "retry_step",
        "replan_partial",
        "regenerate_format",
        "gather_more_evidence",
        "downscope_capabilities",
        "resolve_memory_conflict",
    ]
    description: str
    target_step_ids: list[str] = Field(default_factory=list)
    priority: int = 1


class VerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    plan_id: str
    session_id: str

    status: VerificationStatus
    overall_score: float = Field(..., ge=0.0, le=1.0)
    confidence: float = Field(..., ge=0.0, le=1.0)

    criterion_results: list[CriterionResult] = Field(default_factory=list)
    failure_type: FailureType | None = None
    summary: str | None = None
    repair_actions: list[RepairAction] = Field(default_factory=list)

    generated_at: datetime
    verifier_agent_id: str | None = None

    def passed_required_criteria(self) -> bool:
        return all(r.passed for r in self.criterion_results)

    def failed_criteria_names(self) -> list[str]:
        return [r.name for r in self.criterion_results if not r.passed]

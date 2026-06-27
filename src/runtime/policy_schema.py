from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.plan_schema import RiskLevel


class ApprovalMode(str, Enum):
    AUTO = "auto"
    POLICY = "policy"
    HUMAN = "human"


class ApprovalDecisionStatus(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"
    NEEDS_HUMAN = "needs_human"


class CapabilityGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_name: str
    granted: bool
    scope: str | None = None
    reason: str | None = None


class RiskAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_level: RiskLevel
    score: float = Field(..., ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str
    plan_id: str
    session_id: str

    mode: ApprovalMode
    status: ApprovalDecisionStatus

    granted_capabilities: list[CapabilityGrant] = Field(default_factory=list)
    denied_capabilities: list[CapabilityGrant] = Field(default_factory=list)

    risk_assessment: RiskAssessment
    human_approval_required: bool = False
    rationale: str | None = None

    created_at: datetime
    decided_by: str | None = None

    def granted_capability_names(self) -> set[str]:
        return {c.capability_name for c in self.granted_capabilities if c.granted}

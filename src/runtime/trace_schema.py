from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TraceEventType(str, Enum):
    PLAN_DRAFTED = "plan_drafted"
    PLAN_APPROVED = "plan_approved"
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"
    VERIFICATION_COMPLETED = "verification_completed"
    REPAIR_TRIGGERED = "repair_triggered"
    MEMORY_CANDIDATE_CREATED = "memory_candidate_created"
    MEMORY_PROMOTED = "memory_promoted"


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    session_id: str
    plan_id: str | None = None

    event_type: TraceEventType
    actor_id: str
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

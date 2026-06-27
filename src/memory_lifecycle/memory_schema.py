from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MemoryType(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    POLICY = "policy"


class ReviewStatus(str, Enum):
    CANDIDATE = "candidate"
    REVIEWED = "reviewed"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"
    MERGED = "merged"


class SensitivityLevel(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class OriginatorType(str, Enum):
    USER = "user"
    REPO = "repo"
    WEB = "web"
    SYSTEM = "system"
    MODEL_INFERENCE = "model_inference"
    TOOL_OUTPUT = "tool_output"


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    originator_type: OriginatorType
    originator_id: str | None = None
    capture_method: str
    source_ref: str | None = None
    captured_at: datetime


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    session_id: str
    user_id: str

    memory_type: MemoryType
    content: str
    subject: str | None = None
    tags: list[str] = Field(default_factory=list)

    provenance: Provenance
    confidence: float = Field(..., ge=0.0, le=1.0)
    trust_score: float = Field(..., ge=0.0, le=1.0)
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL

    ttl_seconds: int | None = Field(default=None, ge=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    review_status: ReviewStatus = ReviewStatus.CANDIDATE
    dedup_key: str | None = None
    contradiction_refs: list[str] = Field(default_factory=list)

    source_event_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PromotedMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    user_id: str

    memory_type: MemoryType
    content: str
    subject: str | None = None
    tags: list[str] = Field(default_factory=list)

    provenance: Provenance
    confidence: float = Field(..., ge=0.0, le=1.0)
    trust_score: float = Field(..., ge=0.0, le=1.0)
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL

    valid_from: datetime | None = None
    valid_until: datetime | None = None
    review_status: ReviewStatus = ReviewStatus.PROMOTED

    supersedes: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    merged_from: list[str] = Field(default_factory=list)

    metadata: dict[str, Any] = Field(default_factory=dict)


class ConflictType(str, Enum):
    DUPLICATE = "duplicate"
    CONTRADICTION = "contradiction"
    STALE = "stale"
    SENSITIVITY_MISMATCH = "sensitivity_mismatch"


class ConflictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    conflict_type: ConflictType
    left_ref: str
    right_ref: str
    summary: str
    detected_at: datetime
    resolved: bool = False
    resolution: str | None = None

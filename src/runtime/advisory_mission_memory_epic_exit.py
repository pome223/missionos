"""Epic-exit artifact for Advisory Mission Memory."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.advisory_lesson_invariance import (
    canonical_verifier_digest,
    validate_verifier_contract_ref_is_current,
)

ADVISORY_MISSION_MEMORY_EPIC_EXIT_SCHEMA_VERSION = (
    "advisory_mission_memory_epic_exit.v1"
)


class AdvisoryMissionMemoryEpicExitError(RuntimeError):
    """Raised when the advisory mission memory epic exit cannot be proven."""


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _as_tuple(values: Any) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in values or () if str(item).strip()}))


class AdvisoryMissionMemoryEpicExitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[ADVISORY_MISSION_MEMORY_EPIC_EXIT_SCHEMA_VERSION] = (
        ADVISORY_MISSION_MEMORY_EPIC_EXIT_SCHEMA_VERSION
    )
    result_id: str
    lesson_promotion_receipt_ref: str
    lesson_ref: str
    scenario_proposal_ref: str
    used_lesson_refs: tuple[str, ...]
    suppressed_scenario_candidates_count: int = Field(ge=1)
    verifier_contract_ref: str
    verifier_output_hash_with_lessons: str
    verifier_output_hash_without_lessons: str
    completed_at: datetime
    epic_invariant_lessons_never_authority: Literal[True] = True
    verifier_output_byte_equal_with_and_without_lessons: Literal[True] = True
    auto_promotion_used: Literal[False] = False
    llm_decided_promotion: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    external_dispatch_performed: Literal[False] = False

    @field_validator("used_lesson_refs", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_completed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_exit(self) -> "AdvisoryMissionMemoryEpicExitResult":
        if not self.lesson_promotion_receipt_ref.startswith(
            "delivery_mission_lesson_promotion_receipt:"
        ):
            raise AdvisoryMissionMemoryEpicExitError(
                "lesson_promotion_receipt_ref_invalid"
            )
        if not self.lesson_ref.startswith("delivery_mission_lesson:"):
            raise AdvisoryMissionMemoryEpicExitError("lesson_ref_invalid")
        if not self.scenario_proposal_ref.startswith(
            "px4_gazebo_mission_scenario_proposal:"
        ):
            raise AdvisoryMissionMemoryEpicExitError("scenario_proposal_ref_invalid")
        if self.lesson_ref not in self.used_lesson_refs:
            raise AdvisoryMissionMemoryEpicExitError("lesson_ref_not_used_by_proposal")
        if (
            self.verifier_output_hash_with_lessons
            != self.verifier_output_hash_without_lessons
        ):
            raise AdvisoryMissionMemoryEpicExitError("verifier_output_hash_mismatch")
        validate_verifier_contract_ref_is_current(self.verifier_contract_ref)
        return self


def build_advisory_mission_memory_epic_exit_result(
    *,
    lesson_promotion_receipt_ref: str,
    lesson_ref: str,
    scenario_proposal_ref: str,
    used_lesson_refs: Any,
    suppressed_scenario_candidates_count: int,
    verifier_contract_ref: str,
    verifier_output_with_lessons: Mapping[str, Any],
    verifier_output_without_lessons: Mapping[str, Any],
    auto_promotion_used: bool,
    llm_decided_promotion: bool,
    completed_at: datetime | None = None,
) -> AdvisoryMissionMemoryEpicExitResult:
    with_lessons = canonical_verifier_digest(verifier_output_with_lessons)
    without_lessons = canonical_verifier_digest(verifier_output_without_lessons)
    completed = _utc(completed_at)
    if auto_promotion_used:
        raise AdvisoryMissionMemoryEpicExitError("auto_promotion_used")
    if llm_decided_promotion:
        raise AdvisoryMissionMemoryEpicExitError("llm_decided_promotion")
    payload = {
        "lesson_ref": lesson_ref,
        "proposal_ref": scenario_proposal_ref,
        "used_lesson_refs": _as_tuple(used_lesson_refs),
        "verifier_contract_ref": verifier_contract_ref,
        "verifier_hash": with_lessons,
        "completed_at": completed.isoformat(),
    }
    return AdvisoryMissionMemoryEpicExitResult(
        result_id=_stable_id("advisory_mission_memory_epic_exit", payload),
        lesson_promotion_receipt_ref=lesson_promotion_receipt_ref,
        lesson_ref=lesson_ref,
        scenario_proposal_ref=scenario_proposal_ref,
        used_lesson_refs=_as_tuple(used_lesson_refs),
        suppressed_scenario_candidates_count=suppressed_scenario_candidates_count,
        verifier_contract_ref=verifier_contract_ref,
        verifier_output_hash_with_lessons=with_lessons,
        verifier_output_hash_without_lessons=without_lessons,
        completed_at=completed,
    )


__all__ = [
    "ADVISORY_MISSION_MEMORY_EPIC_EXIT_SCHEMA_VERSION",
    "AdvisoryMissionMemoryEpicExitError",
    "AdvisoryMissionMemoryEpicExitResult",
    "build_advisory_mission_memory_epic_exit_result",
]

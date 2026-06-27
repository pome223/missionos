"""Epic-exit artifact for Advisory Recovery Context (#476)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.recovery_advisory_context import (
    RECOVERY_ADVISORY_CONTEXT_SCHEMA_VERSION,
    RECOVERY_ADVISORY_PROPOSAL_SCHEMA_VERSION,
    RecoveryAdvisoryContext,
    RecoveryAdvisoryProposal,
)

RECOVERY_ADVISORY_EPIC_EXIT_SCHEMA_VERSION = "recovery_advisory_epic_exit.v1"


class RecoveryAdvisoryEpicExitError(RuntimeError):
    """Raised when the advisory recovery epic-exit proof is inconsistent."""


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _as_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(
        sorted({str(item).strip() for item in (values or ()) if str(item).strip()})
    )


def _as_ordered_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in (values or ()))


def _context_ref(context: RecoveryAdvisoryContext) -> str:
    return f"recovery_advisory_context:{context.recovery_advisory_context_id}"


def _proposal_ref(proposal: RecoveryAdvisoryProposal) -> str:
    return f"recovery_advisory_proposal:{proposal.proposal_id}"


def _validate_lesson_refs(refs: Sequence[str]) -> None:
    for ref in refs:
        if not ref.startswith("delivery_mission_lesson:"):
            raise RecoveryAdvisoryEpicExitError(
                f"recovery_advisory_epic_exit_lesson_ref_invalid:{ref}"
            )


def _validate_shared_observation_refs(refs: Sequence[str]) -> None:
    for ref in refs:
        if not ref.startswith("mission_shared_observation:"):
            raise RecoveryAdvisoryEpicExitError(
                f"recovery_advisory_epic_exit_shared_observation_ref_invalid:{ref}"
            )


class RecoveryAdvisoryEpicExitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[RECOVERY_ADVISORY_EPIC_EXIT_SCHEMA_VERSION] = (
        RECOVERY_ADVISORY_EPIC_EXIT_SCHEMA_VERSION
    )
    epic_exit_id: str
    recovery_advisory_context_ref: str
    recovery_request_ref: str
    recovery_advisory_proposal_ref: str
    recovery_decision_ref: str | None = None
    used_lesson_refs: tuple[str, ...] = ()
    used_shared_observation_refs: tuple[str, ...] = ()
    ignored_lesson_refs: tuple[str, ...] = ()
    ignored_shared_observation_refs: tuple[str, ...] = ()
    advisory_validation_evidence_count: int = Field(ge=1)
    suppressed_recovery_candidates_count: int = Field(ge=1)
    recovery_outcome_hash_with_advisory: str
    recovery_outcome_hash_without_advisory: str
    verifier_invariance_evidence_count: int = Field(ge=1)
    verifier_invariance_evidence_case_ids: tuple[str, ...]
    negative_observed_fact_failed_closed: Literal[True] = True
    negative_scorecard_evidence_failed_closed: Literal[True] = True
    negative_success_proof_failed_closed: Literal[True] = True
    negative_outcome_input_failed_closed: Literal[True] = True
    negative_predicate_change_failed_closed: Literal[True] = True
    negative_command_authority_failed_closed: Literal[True] = True
    created_at: datetime
    recovery_advisory_context_schema_version: Literal[
        RECOVERY_ADVISORY_CONTEXT_SCHEMA_VERSION
    ] = RECOVERY_ADVISORY_CONTEXT_SCHEMA_VERSION
    recovery_advisory_proposal_schema_version: Literal[
        RECOVERY_ADVISORY_PROPOSAL_SCHEMA_VERSION
    ] = RECOVERY_ADVISORY_PROPOSAL_SCHEMA_VERSION
    recovery_outcome_byte_equal_with_and_without_advisory: Literal[True] = True
    epic_invariant_advisory_context_never_outcome_authority: Literal[True] = True
    advisory_context_may_shape_recovery_proposals: Literal[True] = True
    recovery_outcome_observed_facts_only: Literal[True] = True
    advisory_used_as_outcome_evidence: Literal[False] = False
    advisory_used_as_scorecard_evidence: Literal[False] = False
    advisory_used_as_success_proof: Literal[False] = False
    advisory_modifies_observed_facts: Literal[False] = False
    advisory_modifies_recovery_outcome_predicates: Literal[False] = False
    command_authority_granted: Literal[False] = False
    dispatch_authority_granted: Literal[False] = False
    external_dispatch_performed: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_recovery_allowed: Literal[False] = False
    public_sync_performed: Literal[False] = False
    readme_or_architecture_updated: Literal[False] = False

    @field_validator(
        "used_lesson_refs",
        "used_shared_observation_refs",
        "ignored_lesson_refs",
        "ignored_shared_observation_refs",
        mode="before",
    )
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("verifier_invariance_evidence_case_ids", mode="before")
    @classmethod
    def _coerce_invariance_case_ids(cls, value: Any) -> tuple[str, ...]:
        return _as_ordered_tuple(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_result(self) -> "RecoveryAdvisoryEpicExitResult":
        if not self.recovery_advisory_context_ref.startswith(
            "recovery_advisory_context:"
        ):
            raise RecoveryAdvisoryEpicExitError(
                "recovery_advisory_epic_exit_context_ref_invalid"
            )
        if not self.recovery_request_ref.startswith("delivery_recovery_request:"):
            raise RecoveryAdvisoryEpicExitError(
                "recovery_advisory_epic_exit_recovery_request_ref_invalid"
            )
        if not self.recovery_advisory_proposal_ref.startswith(
            "recovery_advisory_proposal:"
        ):
            raise RecoveryAdvisoryEpicExitError(
                "recovery_advisory_epic_exit_proposal_ref_invalid"
            )
        if self.recovery_decision_ref is not None and (
            not self.recovery_decision_ref.startswith("delivery_recovery_decision:")
        ):
            raise RecoveryAdvisoryEpicExitError(
                "recovery_advisory_epic_exit_decision_ref_invalid"
            )
        _validate_lesson_refs((*self.used_lesson_refs, *self.ignored_lesson_refs))
        _validate_shared_observation_refs(
            (
                *self.used_shared_observation_refs,
                *self.ignored_shared_observation_refs,
            )
        )
        if not (self.used_lesson_refs or self.used_shared_observation_refs):
            raise RecoveryAdvisoryEpicExitError(
                "recovery_advisory_epic_exit_used_advisory_ref_required"
            )
        if (
            self.recovery_outcome_hash_with_advisory
            != self.recovery_outcome_hash_without_advisory
        ):
            raise RecoveryAdvisoryEpicExitError(
                "recovery_advisory_epic_exit_outcome_hash_mismatch"
            )
        if len(self.verifier_invariance_evidence_case_ids) != (
            self.verifier_invariance_evidence_count
        ):
            raise RecoveryAdvisoryEpicExitError(
                "recovery_advisory_epic_exit_invariance_evidence_count_mismatch"
            )
        if not self.verifier_invariance_evidence_case_ids:
            raise RecoveryAdvisoryEpicExitError(
                "recovery_advisory_epic_exit_invariance_evidence_required"
            )
        if any(not item for item in self.verifier_invariance_evidence_case_ids):
            raise RecoveryAdvisoryEpicExitError(
                "recovery_advisory_epic_exit_invariance_case_id_required"
            )
        if len(set(self.verifier_invariance_evidence_case_ids)) != len(
            self.verifier_invariance_evidence_case_ids
        ):
            raise RecoveryAdvisoryEpicExitError(
                "recovery_advisory_epic_exit_invariance_case_id_duplicate"
            )
        return self


def build_recovery_advisory_epic_exit_result(
    *,
    recovery_advisory_context: RecoveryAdvisoryContext | Mapping[str, Any],
    recovery_advisory_proposal: RecoveryAdvisoryProposal | Mapping[str, Any],
    recovery_outcome_hash_with_advisory: str,
    recovery_outcome_hash_without_advisory: str,
    verifier_invariance_evidence: Sequence[Mapping[str, Any]],
    negative_observed_fact_failed_closed: bool,
    negative_scorecard_evidence_failed_closed: bool,
    negative_success_proof_failed_closed: bool,
    negative_outcome_input_failed_closed: bool,
    negative_predicate_change_failed_closed: bool,
    negative_command_authority_failed_closed: bool,
    recovery_decision_ref: str | None = None,
    created_at: datetime | None = None,
) -> RecoveryAdvisoryEpicExitResult:
    context = (
        recovery_advisory_context
        if isinstance(recovery_advisory_context, RecoveryAdvisoryContext)
        else RecoveryAdvisoryContext.model_validate(dict(recovery_advisory_context))
    )
    proposal = (
        recovery_advisory_proposal
        if isinstance(recovery_advisory_proposal, RecoveryAdvisoryProposal)
        else RecoveryAdvisoryProposal.model_validate(dict(recovery_advisory_proposal))
    )
    if proposal.recovery_advisory_context_ref != _context_ref(context):
        raise RecoveryAdvisoryEpicExitError(
            "recovery_advisory_epic_exit_context_proposal_ref_mismatch"
        )
    if proposal.recovery_request_ref != context.recovery_request_ref:
        raise RecoveryAdvisoryEpicExitError(
            "recovery_advisory_epic_exit_request_ref_mismatch"
        )
    if proposal.used_lesson_refs != context.used_lesson_refs:
        raise RecoveryAdvisoryEpicExitError(
            "recovery_advisory_epic_exit_used_lesson_refs_mismatch"
        )
    if proposal.used_shared_observation_refs != context.used_shared_observation_refs:
        raise RecoveryAdvisoryEpicExitError(
            "recovery_advisory_epic_exit_used_shared_observation_refs_mismatch"
        )
    if proposal.ignored_lesson_refs != context.ignored_lesson_refs:
        raise RecoveryAdvisoryEpicExitError(
            "recovery_advisory_epic_exit_ignored_lesson_refs_mismatch"
        )
    if (
        proposal.ignored_shared_observation_refs
        != context.ignored_shared_observation_refs
    ):
        raise RecoveryAdvisoryEpicExitError(
            "recovery_advisory_epic_exit_ignored_shared_observation_refs_mismatch"
        )
    if not all(
        (
            negative_observed_fact_failed_closed,
            negative_scorecard_evidence_failed_closed,
            negative_success_proof_failed_closed,
            negative_outcome_input_failed_closed,
            negative_predicate_change_failed_closed,
            negative_command_authority_failed_closed,
        )
    ):
        raise RecoveryAdvisoryEpicExitError(
            "recovery_advisory_epic_exit_negative_branch_not_proven"
        )
    created = _utc(created_at)
    evidence_case_ids = tuple(
        str(item.get("case_id") or "") for item in verifier_invariance_evidence
    )
    payload = {
        "recovery_advisory_context_ref": _context_ref(context),
        "recovery_request_ref": context.recovery_request_ref,
        "recovery_advisory_proposal_ref": _proposal_ref(proposal),
        "recovery_decision_ref": recovery_decision_ref,
        "used_lesson_refs": context.used_lesson_refs,
        "used_shared_observation_refs": context.used_shared_observation_refs,
        "ignored_lesson_refs": context.ignored_lesson_refs,
        "ignored_shared_observation_refs": context.ignored_shared_observation_refs,
        "recovery_outcome_hash_with_advisory": recovery_outcome_hash_with_advisory,
        "recovery_outcome_hash_without_advisory": recovery_outcome_hash_without_advisory,
        "verifier_invariance_evidence_case_ids": evidence_case_ids,
        "created_at": created.isoformat(),
    }
    return RecoveryAdvisoryEpicExitResult(
        epic_exit_id=_stable_id("recovery_advisory_epic_exit", payload),
        recovery_advisory_context_ref=_context_ref(context),
        recovery_request_ref=context.recovery_request_ref,
        recovery_advisory_proposal_ref=_proposal_ref(proposal),
        recovery_decision_ref=recovery_decision_ref,
        used_lesson_refs=context.used_lesson_refs,
        used_shared_observation_refs=context.used_shared_observation_refs,
        ignored_lesson_refs=context.ignored_lesson_refs,
        ignored_shared_observation_refs=context.ignored_shared_observation_refs,
        advisory_validation_evidence_count=len(context.advisory_validation_evidence),
        suppressed_recovery_candidates_count=len(
            proposal.suppressed_recovery_candidates
        ),
        recovery_outcome_hash_with_advisory=recovery_outcome_hash_with_advisory,
        recovery_outcome_hash_without_advisory=recovery_outcome_hash_without_advisory,
        verifier_invariance_evidence_count=len(verifier_invariance_evidence),
        verifier_invariance_evidence_case_ids=evidence_case_ids,
        negative_observed_fact_failed_closed=True,
        negative_scorecard_evidence_failed_closed=True,
        negative_success_proof_failed_closed=True,
        negative_outcome_input_failed_closed=True,
        negative_predicate_change_failed_closed=True,
        negative_command_authority_failed_closed=True,
        created_at=created,
    )


__all__ = [
    "RECOVERY_ADVISORY_EPIC_EXIT_SCHEMA_VERSION",
    "RecoveryAdvisoryEpicExitError",
    "RecoveryAdvisoryEpicExitResult",
    "build_recovery_advisory_epic_exit_result",
]

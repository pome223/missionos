"""Advisory context artifacts for recovery proposal selection.

Advisory context may shape recovery proposals, but recovery outcomes remain
observed-facts only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.advisory_mission_memory import validate_lesson_refs
from src.runtime.delivery_shared_observation import (
    DeliveryMissionSession,
    DeliveryVehicleSession,
    MissionSharedObservation,
    validate_shared_observation_refs,
)
from src.runtime.delivery_recovery_safety import raise_for_command_like_payload
from src.runtime.task_store import TaskStore

RECOVERY_ADVISORY_CONTEXT_SCHEMA_VERSION = "recovery_advisory_context.v1"
RECOVERY_ADVISORY_PROPOSAL_SCHEMA_VERSION = "recovery_advisory_proposal.v1"

_SCORECARD_CONTEXT_KEYS = frozenset(
    {
        "delivery_scorecard",
        "scorecard",
        "scorecard_evidence",
    }
)
_SUCCESS_PROOF_CONTEXT_KEYS = frozenset(
    {
        "success_proof",
        "success_proofs",
        "recovery_success_proof",
    }
)
_OUTCOME_CONTEXT_KEYS = frozenset(
    {
        "delivery_recovery_outcome",
        "delivery_recovery_outcome_real_sitl",
        "delivery_recovery_outcome_real_sitl_retry_recovered",
        "recovery_outcome",
    }
)
_FALSE_SAFETY_FLAG_KEYS = frozenset(
    {
        "actuator_command_allowed",
        "advisory_grants_recovery_authority",
        "advisory_modifies_observed_facts",
        "advisory_modifies_recovery_outcome_predicates",
        "advisory_used_as_outcome_evidence",
        "advisory_used_as_scorecard_evidence",
        "advisory_used_as_success_proof",
        "approval_free_stronger_recovery_allowed",
        "command_authority_granted",
        "dispatch_authority_granted",
        "gazebo_entity_mutation_allowed",
        "hardware_target_allowed",
        "physical_execution_invoked",
        "raw_mavlink_command_allowed",
        "raw_ros_action_allowed",
        "real_hardware_target",
        "setpoint_stream_allowed",
    }
)


class RecoveryAdvisoryContextError(RuntimeError):
    """Raised when advisory recovery context violates its authority boundary."""


class RecoveryAdvisoryRefKind(str, Enum):
    LESSON = "lesson"
    SHARED_OBSERVATION = "shared_observation"


class RecoveryAdvisoryRefDisposition(str, Enum):
    USED = "used"
    IGNORED = "ignored"


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


def _enum_value(value: Enum | str) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _validate_lesson_ref(ref: str) -> None:
    if not ref.startswith("delivery_mission_lesson:"):
        raise RecoveryAdvisoryContextError(
            f"recovery_advisory_lesson_ref_invalid:{ref}"
        )


def _validate_shared_observation_ref(ref: str) -> None:
    if not ref.startswith("mission_shared_observation:"):
        raise RecoveryAdvisoryContextError(
            f"recovery_advisory_shared_observation_ref_invalid:{ref}"
        )


def _shared_observation_ref(observation: MissionSharedObservation) -> str:
    return f"mission_shared_observation:{observation.observation_id}"


def _contains_ref(value: Any, ref: str) -> bool:
    if isinstance(value, str):
        return value == ref
    if isinstance(value, Mapping):
        return any(_contains_ref(item, ref) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_ref(item, ref) for item in value)
    return False


def _without_false_safety_flags(value: Any) -> Any:
    """Return a command-like-scan-only copy with explicit false safety flags removed."""

    if isinstance(value, Mapping):
        return {
            key: _without_false_safety_flags(item)
            for key, item in value.items()
            if not (str(key) in _FALSE_SAFETY_FLAG_KEYS and item is False)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_without_false_safety_flags(item) for item in value)
    return value


def _has_authority_context(
    path: tuple[str, ...],
    *,
    exact: frozenset[str],
    suffixes: tuple[str, ...],
    prefixes: tuple[str, ...] = (),
) -> bool:
    for item in path:
        if item in exact:
            return True
        if prefixes and any(item.startswith(prefix) for prefix in prefixes):
            return True
        if suffixes and any(item.endswith(suffix) for suffix in suffixes):
            return True
    return False


def _raise_if_advisory_refs_used_as_authority(
    *,
    recovery_artifacts: Mapping[str, Any],
    advisory_refs: Sequence[str],
) -> None:
    if not recovery_artifacts:
        return

    ref_set = frozenset(advisory_refs)

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            keyset = {str(key) for key in value.keys()}
            for ref in ref_set:
                if any(
                    key in keyset for key in ("observed_facts", "observed_fact_refs")
                ):
                    if _contains_ref(value.get("observed_facts"), ref) or _contains_ref(
                        value.get("observed_fact_refs"),
                        ref,
                    ):
                        raise RecoveryAdvisoryContextError(
                            "recovery_advisory_ref_used_as_observed_fact"
                        )
                if _has_authority_context(
                    path,
                    exact=_SCORECARD_CONTEXT_KEYS,
                    suffixes=("_scorecard", "_scorecard_evidence"),
                    prefixes=("scorecard_",),
                ) and _contains_ref(value.get("evidence_refs"), ref):
                    raise RecoveryAdvisoryContextError(
                        "recovery_advisory_ref_used_as_scorecard_evidence"
                    )
                if _has_authority_context(
                    path,
                    exact=_SUCCESS_PROOF_CONTEXT_KEYS,
                    suffixes=("_success_proof", "_success_proofs"),
                    prefixes=("success_proof",),
                ) and _contains_ref(value, ref):
                    raise RecoveryAdvisoryContextError(
                        "recovery_advisory_ref_used_as_success_proof"
                    )
                if _has_authority_context(
                    path,
                    exact=_OUTCOME_CONTEXT_KEYS,
                    suffixes=("_outcome", "_outcome_evidence"),
                    prefixes=("delivery_recovery_outcome", "recovery_outcome"),
                ) and (
                    _contains_ref(value.get("evidence_refs"), ref)
                    or _contains_ref(value.get("observed_fact_refs"), ref)
                    or _contains_ref(value.get("verifier_input_refs"), ref)
                    or _contains_ref(value.get("outcome_input_refs"), ref)
                ):
                    raise RecoveryAdvisoryContextError(
                        "recovery_advisory_ref_used_as_outcome_input"
                    )
            for key, item in value.items():
                key_text = str(key)
                if key_text in {
                    "recovery_outcome_predicate_overrides",
                    "outcome_predicate_overrides",
                    "verifier_predicate_overrides",
                }:
                    raise RecoveryAdvisoryContextError(
                        "recovery_advisory_modifies_recovery_outcome_predicates"
                    )
                if key_text.endswith("_predicate_overrides"):
                    raise RecoveryAdvisoryContextError(
                        "recovery_advisory_modifies_recovery_outcome_predicates"
                    )
                walk(item, (*path, key_text))
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for index, item in enumerate(value):
                walk(item, (*path, str(index)))

    raise_for_command_like_payload(
        _without_false_safety_flags(recovery_artifacts),
        root="recovery_advisory_ref_validation.recovery_artifacts",
        error_type=RecoveryAdvisoryContextError,
        prefix="recovery advisory validation refused command-like recovery artifacts",
    )
    walk(recovery_artifacts, ())


class RecoveryAdvisoryValidationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    advisory_ref: str
    ref_kind: RecoveryAdvisoryRefKind
    disposition: RecoveryAdvisoryRefDisposition
    validation_summary: str
    validated_at: datetime
    advisory_validation_only: Literal[True] = True
    advisory_grants_recovery_authority: Literal[False] = False
    advisory_used_as_outcome_evidence: Literal[False] = False
    advisory_used_as_scorecard_evidence: Literal[False] = False
    advisory_used_as_success_proof: Literal[False] = False
    advisory_modifies_observed_facts: Literal[False] = False
    advisory_modifies_recovery_outcome_predicates: Literal[False] = False
    command_authority_granted: Literal[False] = False
    dispatch_authority_granted: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_recovery_allowed: Literal[False] = False

    @field_validator("validated_at", mode="before")
    @classmethod
    def _coerce_validated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_evidence(self) -> "RecoveryAdvisoryValidationEvidence":
        if self.ref_kind is RecoveryAdvisoryRefKind.LESSON:
            _validate_lesson_ref(self.advisory_ref)
        if self.ref_kind is RecoveryAdvisoryRefKind.SHARED_OBSERVATION:
            _validate_shared_observation_ref(self.advisory_ref)
        if not self.validation_summary.strip():
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_validation_summary_empty"
            )
        raise_for_command_like_payload(
            {"validation_summary": self.validation_summary},
            root="recovery_advisory_validation_evidence",
            error_type=RecoveryAdvisoryContextError,
            prefix="recovery advisory validation refused command-like summary",
        )
        return self


class RecoveryAdvisoryContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[RECOVERY_ADVISORY_CONTEXT_SCHEMA_VERSION] = (
        RECOVERY_ADVISORY_CONTEXT_SCHEMA_VERSION
    )
    recovery_advisory_context_id: str
    mission_ref: str | None = None
    mission_session_ref: str | None = None
    recovery_request_ref: str
    used_lesson_refs: tuple[str, ...] = ()
    used_shared_observation_refs: tuple[str, ...] = ()
    ignored_lesson_refs: tuple[str, ...] = ()
    ignored_shared_observation_refs: tuple[str, ...] = ()
    advisory_validation_evidence: tuple[RecoveryAdvisoryValidationEvidence, ...] = ()
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    advisory_context_only: Literal[True] = True
    advisory_grants_recovery_authority: Literal[False] = False
    advisory_used_as_outcome_evidence: Literal[False] = False
    advisory_used_as_scorecard_evidence: Literal[False] = False
    advisory_used_as_success_proof: Literal[False] = False
    advisory_modifies_observed_facts: Literal[False] = False
    advisory_modifies_recovery_outcome_predicates: Literal[False] = False
    command_authority_granted: Literal[False] = False
    dispatch_authority_granted: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_recovery_allowed: Literal[False] = False

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

    @field_validator("advisory_validation_evidence", mode="before")
    @classmethod
    def _coerce_evidence(
        cls, value: Any
    ) -> tuple[RecoveryAdvisoryValidationEvidence, ...]:
        return tuple(value or ())

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_context(self) -> "RecoveryAdvisoryContext":
        if not self.mission_ref and not self.mission_session_ref:
            raise RecoveryAdvisoryContextError("recovery_advisory_mission_ref_required")
        if self.mission_ref and not self.mission_ref.startswith("task:"):
            raise RecoveryAdvisoryContextError("recovery_advisory_mission_ref_invalid")
        if self.mission_session_ref and not self.mission_session_ref.startswith(
            "delivery_mission_session:"
        ):
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_mission_session_ref_invalid"
            )
        if not self.recovery_request_ref.startswith("delivery_recovery_request:"):
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_recovery_request_ref_invalid"
            )
        for ref in (*self.used_lesson_refs, *self.ignored_lesson_refs):
            _validate_lesson_ref(ref)
        for ref in (
            *self.used_shared_observation_refs,
            *self.ignored_shared_observation_refs,
        ):
            _validate_shared_observation_ref(ref)
        if set(self.used_lesson_refs) & set(self.ignored_lesson_refs):
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_lesson_ref_used_and_ignored"
            )
        if set(self.used_shared_observation_refs) & set(
            self.ignored_shared_observation_refs
        ):
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_shared_observation_ref_used_and_ignored"
            )
        expected = (
            set(self.used_lesson_refs)
            | set(self.ignored_lesson_refs)
            | set(self.used_shared_observation_refs)
            | set(self.ignored_shared_observation_refs)
        )
        evidence_refs = {
            item.advisory_ref for item in self.advisory_validation_evidence
        }
        if expected != evidence_refs:
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_refs_must_match_validation_evidence"
            )
        for item in self.advisory_validation_evidence:
            if item.ref_kind is RecoveryAdvisoryRefKind.LESSON:
                if item.disposition is RecoveryAdvisoryRefDisposition.USED:
                    expected_refs = self.used_lesson_refs
                else:
                    expected_refs = self.ignored_lesson_refs
            else:
                if item.disposition is RecoveryAdvisoryRefDisposition.USED:
                    expected_refs = self.used_shared_observation_refs
                else:
                    expected_refs = self.ignored_shared_observation_refs
            if item.advisory_ref not in expected_refs:
                raise RecoveryAdvisoryContextError(
                    "recovery_advisory_evidence_disposition_mismatch"
                )
        raise_for_command_like_payload(
            self.metadata,
            root="recovery_advisory_context.metadata",
            error_type=RecoveryAdvisoryContextError,
            prefix="recovery advisory context refused command-like metadata",
        )
        return self


class SuppressedRecoveryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_kind: str
    suppressing_advisory_ref: str
    suppression_rationale: str

    @model_validator(mode="after")
    def _validate_suppression(self) -> "SuppressedRecoveryCandidate":
        if not self.candidate_kind.strip():
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_suppressed_candidate_kind_required"
            )
        if not (
            self.suppressing_advisory_ref.startswith("delivery_mission_lesson:")
            or self.suppressing_advisory_ref.startswith("mission_shared_observation:")
        ):
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_suppression_requires_advisory_ref"
            )
        if not self.suppression_rationale.strip():
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_suppression_rationale_required"
            )
        raise_for_command_like_payload(
            {
                "candidate_kind": self.candidate_kind,
                "suppression_rationale": self.suppression_rationale,
            },
            root="suppressed_recovery_candidate",
            error_type=RecoveryAdvisoryContextError,
            prefix="recovery advisory proposal refused command-like suppression",
        )
        return self


class RecoveryAdvisoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[RECOVERY_ADVISORY_PROPOSAL_SCHEMA_VERSION] = (
        RECOVERY_ADVISORY_PROPOSAL_SCHEMA_VERSION
    )
    proposal_id: str
    recovery_request_ref: str
    recovery_advisory_context_ref: str = ""
    used_lesson_refs: tuple[str, ...] = ()
    used_shared_observation_refs: tuple[str, ...] = ()
    ignored_lesson_refs: tuple[str, ...] = ()
    ignored_shared_observation_refs: tuple[str, ...] = ()
    advisory_validation_evidence: tuple[RecoveryAdvisoryValidationEvidence, ...] = ()
    suppressed_recovery_candidates: tuple[SuppressedRecoveryCandidate, ...] = ()
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    recovery_advisory_context_schema_version: Literal[
        RECOVERY_ADVISORY_CONTEXT_SCHEMA_VERSION
    ] = RECOVERY_ADVISORY_CONTEXT_SCHEMA_VERSION
    proposal_surface_only: Literal[True] = True
    advisory_context_recorded_for_selection: Literal[True] = True
    proposal_uses_advisory_authority_for_judgement: Literal[False] = False
    proposal_modifies_recovery_outcome_predicates: Literal[False] = False
    advisory_modifies_observed_facts: Literal[False] = False
    advisory_used_as_outcome_evidence: Literal[False] = False
    advisory_used_as_scorecard_evidence: Literal[False] = False
    advisory_used_as_success_proof: Literal[False] = False
    command_authority_granted: Literal[False] = False
    dispatch_authority_granted: Literal[False] = False
    raw_mavlink_command_allowed: Literal[False] = False
    raw_ros_action_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    actuator_command_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_recovery_allowed: Literal[False] = False

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

    @field_validator(
        "advisory_validation_evidence",
        "suppressed_recovery_candidates",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_proposal(self) -> "RecoveryAdvisoryProposal":
        if not self.recovery_request_ref.startswith("delivery_recovery_request:"):
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_proposal_recovery_request_ref_invalid"
            )
        advisory_refs = (
            *self.used_lesson_refs,
            *self.ignored_lesson_refs,
            *self.used_shared_observation_refs,
            *self.ignored_shared_observation_refs,
        )
        if advisory_refs:
            if not self.recovery_advisory_context_ref.startswith(
                "recovery_advisory_context:"
            ):
                raise RecoveryAdvisoryContextError(
                    "recovery_advisory_proposal_context_ref_required"
                )
        elif self.recovery_advisory_context_ref:
            if not self.recovery_advisory_context_ref.startswith(
                "recovery_advisory_context:"
            ):
                raise RecoveryAdvisoryContextError(
                    "recovery_advisory_proposal_context_ref_invalid"
                )
        for ref in (*self.used_lesson_refs, *self.ignored_lesson_refs):
            _validate_lesson_ref(ref)
        for ref in (
            *self.used_shared_observation_refs,
            *self.ignored_shared_observation_refs,
        ):
            _validate_shared_observation_ref(ref)
        if set(self.used_lesson_refs) & set(self.ignored_lesson_refs):
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_proposal_lesson_ref_used_and_ignored"
            )
        if set(self.used_shared_observation_refs) & set(
            self.ignored_shared_observation_refs
        ):
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_proposal_shared_observation_ref_used_and_ignored"
            )
        expected = set(advisory_refs)
        evidence_refs = {
            item.advisory_ref for item in self.advisory_validation_evidence
        }
        if expected != evidence_refs:
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_proposal_refs_must_match_validation_evidence"
            )
        used_refs = set(self.used_lesson_refs) | set(self.used_shared_observation_refs)
        for item in self.suppressed_recovery_candidates:
            if item.suppressing_advisory_ref not in used_refs:
                raise RecoveryAdvisoryContextError(
                    "recovery_advisory_suppressed_candidate_requires_used_ref"
                )
        raise_for_command_like_payload(
            self.metadata,
            root="recovery_advisory_proposal.metadata",
            error_type=RecoveryAdvisoryContextError,
            prefix="recovery advisory proposal refused command-like metadata",
        )
        return self


def _evidence_for_ref(
    *,
    advisory_ref: str,
    ref_kind: RecoveryAdvisoryRefKind,
    disposition: RecoveryAdvisoryRefDisposition,
    validated_at: datetime,
) -> RecoveryAdvisoryValidationEvidence:
    return RecoveryAdvisoryValidationEvidence(
        advisory_ref=advisory_ref,
        ref_kind=ref_kind,
        disposition=disposition,
        validation_summary=(
            f"{ref_kind.value} advisory ref recorded for recovery {disposition.value}"
        ),
        validated_at=validated_at,
    )


def build_recovery_advisory_context(
    *,
    recovery_request_ref: str,
    mission_ref: str | None = None,
    mission_session_ref: str | None = None,
    used_lesson_refs: Sequence[str] | None = None,
    used_shared_observation_refs: Sequence[str] | None = None,
    ignored_lesson_refs: Sequence[str] | None = None,
    ignored_shared_observation_refs: Sequence[str] | None = None,
    advisory_validation_evidence: (
        Sequence[RecoveryAdvisoryValidationEvidence | Mapping[str, Any]] | None
    ) = None,
    created_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> RecoveryAdvisoryContext:
    created = _utc(created_at)
    used_lessons = _as_tuple(used_lesson_refs)
    used_shared = _as_tuple(used_shared_observation_refs)
    ignored_lessons = _as_tuple(ignored_lesson_refs)
    ignored_shared = _as_tuple(ignored_shared_observation_refs)
    if advisory_validation_evidence is None:
        evidence: tuple[RecoveryAdvisoryValidationEvidence, ...] = tuple(
            [
                *(
                    _evidence_for_ref(
                        advisory_ref=ref,
                        ref_kind=RecoveryAdvisoryRefKind.LESSON,
                        disposition=RecoveryAdvisoryRefDisposition.USED,
                        validated_at=created,
                    )
                    for ref in used_lessons
                ),
                *(
                    _evidence_for_ref(
                        advisory_ref=ref,
                        ref_kind=RecoveryAdvisoryRefKind.LESSON,
                        disposition=RecoveryAdvisoryRefDisposition.IGNORED,
                        validated_at=created,
                    )
                    for ref in ignored_lessons
                ),
                *(
                    _evidence_for_ref(
                        advisory_ref=ref,
                        ref_kind=RecoveryAdvisoryRefKind.SHARED_OBSERVATION,
                        disposition=RecoveryAdvisoryRefDisposition.USED,
                        validated_at=created,
                    )
                    for ref in used_shared
                ),
                *(
                    _evidence_for_ref(
                        advisory_ref=ref,
                        ref_kind=RecoveryAdvisoryRefKind.SHARED_OBSERVATION,
                        disposition=RecoveryAdvisoryRefDisposition.IGNORED,
                        validated_at=created,
                    )
                    for ref in ignored_shared
                ),
            ]
        )
    else:
        evidence = tuple(
            (
                item
                if isinstance(item, RecoveryAdvisoryValidationEvidence)
                else RecoveryAdvisoryValidationEvidence.model_validate(item)
            )
            for item in advisory_validation_evidence
        )
    payload = {
        "mission_ref": mission_ref,
        "mission_session_ref": mission_session_ref,
        "recovery_request_ref": recovery_request_ref,
        "used_lesson_refs": used_lessons,
        "used_shared_observation_refs": used_shared,
        "ignored_lesson_refs": ignored_lessons,
        "ignored_shared_observation_refs": ignored_shared,
        "advisory_validation_evidence": [
            item.model_dump(mode="json") for item in evidence
        ],
        "created_at": created.isoformat(),
    }
    return RecoveryAdvisoryContext(
        recovery_advisory_context_id=_stable_id(
            "recovery_advisory_context",
            payload,
        ),
        mission_ref=mission_ref,
        mission_session_ref=mission_session_ref,
        recovery_request_ref=recovery_request_ref,
        used_lesson_refs=used_lessons,
        used_shared_observation_refs=used_shared,
        ignored_lesson_refs=ignored_lessons,
        ignored_shared_observation_refs=ignored_shared,
        advisory_validation_evidence=evidence,
        created_at=created,
        metadata=dict(metadata or {}),
    )


def _context_ref(context: RecoveryAdvisoryContext) -> str:
    return f"recovery_advisory_context:{context.recovery_advisory_context_id}"


def build_recovery_advisory_proposal(
    *,
    recovery_request_ref: str,
    recovery_advisory_context: (
        RecoveryAdvisoryContext | Mapping[str, Any] | None
    ) = None,
    suppressed_recovery_candidates: (
        Sequence[SuppressedRecoveryCandidate | Mapping[str, Any]] | None
    ) = None,
    created_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> RecoveryAdvisoryProposal:
    created = _utc(created_at)
    context = (
        None
        if recovery_advisory_context is None
        else (
            recovery_advisory_context
            if isinstance(recovery_advisory_context, RecoveryAdvisoryContext)
            else RecoveryAdvisoryContext.model_validate(dict(recovery_advisory_context))
        )
    )
    suppressed = tuple(
        (
            item
            if isinstance(item, SuppressedRecoveryCandidate)
            else SuppressedRecoveryCandidate.model_validate(item)
        )
        for item in (suppressed_recovery_candidates or ())
    )
    used_lesson_refs = context.used_lesson_refs if context else ()
    used_shared_observation_refs = (
        context.used_shared_observation_refs if context else ()
    )
    ignored_lesson_refs = context.ignored_lesson_refs if context else ()
    ignored_shared_observation_refs = (
        context.ignored_shared_observation_refs if context else ()
    )
    evidence = context.advisory_validation_evidence if context else ()
    payload = {
        "recovery_request_ref": recovery_request_ref,
        "recovery_advisory_context_ref": _context_ref(context) if context else "",
        "used_lesson_refs": used_lesson_refs,
        "used_shared_observation_refs": used_shared_observation_refs,
        "ignored_lesson_refs": ignored_lesson_refs,
        "ignored_shared_observation_refs": ignored_shared_observation_refs,
        "advisory_validation_evidence": [
            item.model_dump(mode="json") for item in evidence
        ],
        "suppressed_recovery_candidates": [
            item.model_dump(mode="json") for item in suppressed
        ],
        "created_at": created.isoformat(),
    }
    return RecoveryAdvisoryProposal(
        proposal_id=_stable_id("recovery_advisory_proposal", payload),
        recovery_request_ref=recovery_request_ref,
        recovery_advisory_context_ref=_context_ref(context) if context else "",
        used_lesson_refs=used_lesson_refs,
        used_shared_observation_refs=used_shared_observation_refs,
        ignored_lesson_refs=ignored_lesson_refs,
        ignored_shared_observation_refs=ignored_shared_observation_refs,
        advisory_validation_evidence=evidence,
        suppressed_recovery_candidates=suppressed,
        created_at=created,
        metadata=dict(metadata or {}),
    )


def validate_recovery_advisory_refs(
    *,
    recovery_advisory_context: RecoveryAdvisoryContext | Mapping[str, Any],
    lesson_task: Mapping[str, Any] | None = None,
    shared_observation_mission_session: (
        DeliveryMissionSession | Mapping[str, Any] | None
    ) = None,
    shared_observation_vehicle_sessions: (
        Sequence[DeliveryVehicleSession | Mapping[str, Any]] | None
    ) = None,
    shared_observations: (
        Sequence[MissionSharedObservation | Mapping[str, Any]] | None
    ) = None,
    shared_observation_decision_at: datetime | None = None,
    max_observation_age_seconds: float | None = None,
    recovery_artifacts: Mapping[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> RecoveryAdvisoryContext:
    """Validate recovery advisory refs without granting recovery authority."""

    context = (
        recovery_advisory_context
        if isinstance(recovery_advisory_context, RecoveryAdvisoryContext)
        else RecoveryAdvisoryContext.model_validate(dict(recovery_advisory_context))
    )
    lesson_refs = (*context.used_lesson_refs, *context.ignored_lesson_refs)
    if lesson_refs and lesson_task is None:
        raise RecoveryAdvisoryContextError("recovery_advisory_lesson_task_required")
    for ref in lesson_refs:
        validate_lesson_refs(
            task=lesson_task or {},
            lesson_ref=ref,
            task_store_factory=task_store_factory,
        )

    shared_refs = (
        *context.used_shared_observation_refs,
        *context.ignored_shared_observation_refs,
    )
    if shared_refs:
        if shared_observation_mission_session is None:
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_shared_observation_mission_required"
            )
        if shared_observation_decision_at is None:
            raise RecoveryAdvisoryContextError(
                "recovery_advisory_shared_observation_decision_at_required"
            )
        resolved_shared = tuple(
            (
                item
                if isinstance(item, MissionSharedObservation)
                else MissionSharedObservation.model_validate(item)
            )
            for item in (shared_observations or ())
        )
        shared_by_ref = {
            _shared_observation_ref(item): item for item in resolved_shared
        }
        missing = tuple(ref for ref in shared_refs if ref not in shared_by_ref)
        if missing:
            raise RecoveryAdvisoryContextError(
                f"recovery_advisory_shared_observation_ref_not_found:{missing[0]}"
            )
        for ref in shared_refs:
            validate_shared_observation_refs(
                mission_session=shared_observation_mission_session,
                vehicle_sessions=tuple(shared_observation_vehicle_sessions or ()),
                shared_observation=shared_by_ref[ref],
                decision_at=shared_observation_decision_at,
                decision_shared_observation_refs=shared_refs,
                max_observation_age_seconds=max_observation_age_seconds,
            )

    _raise_if_advisory_refs_used_as_authority(
        recovery_artifacts=dict(recovery_artifacts or {}),
        advisory_refs=(
            *lesson_refs,
            *shared_refs,
            f"recovery_advisory_context:{context.recovery_advisory_context_id}",
        ),
    )
    return context


__all__ = [
    "RECOVERY_ADVISORY_CONTEXT_SCHEMA_VERSION",
    "RECOVERY_ADVISORY_PROPOSAL_SCHEMA_VERSION",
    "RecoveryAdvisoryContext",
    "RecoveryAdvisoryContextError",
    "RecoveryAdvisoryProposal",
    "RecoveryAdvisoryRefDisposition",
    "RecoveryAdvisoryRefKind",
    "RecoveryAdvisoryValidationEvidence",
    "SuppressedRecoveryCandidate",
    "build_recovery_advisory_context",
    "build_recovery_advisory_proposal",
    "validate_recovery_advisory_refs",
]

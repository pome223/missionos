"""Backend-neutral Mission Contract types and their evidence-readiness check.

This module does two things and refuses to do a third.

It separates approval-frozen contract material from runtime evidence, as
different types, so a runtime fact has no write path into digest-bound
material.

It reports whether the runtime evidence is *ready* to be evaluated: bound to
the exact contract, complete against the frozen requirements, fresh, in the
frozen execution scope, and content-bound.

It does not decide whether the outcome claim holds. Readiness is a statement
about evidence, not about a predicate. No predicate is evaluated here, so
``evaluated_outcome_claim`` is structurally ``False`` and
``actual_verification_basis`` is structurally ``unverified``. Both become
computable only when an approved versioned predicate package returns a typed
evaluation result carrying its own basis and evidence refs.

A verification basis is never derived from an evidence origin. Where evidence
came from and how a conclusion was reached are different axes; collapsing them
would let a ``stored_artifact`` label produce a ``deterministic`` conclusion.

Nothing here creates approval, dispatch authority, execution, progress,
delivery, or physical-execution claims.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any, Literal

from .action_feasibility import (
    EvidenceOrigin,
    VerificationBasis,
    VerificationItem,
    VerificationItemStatus,
    canonical_sha256,
)
from .execution_scope import HardwareExecutionMode, parse_hardware_execution_mode


MISSION_CONTRACT_SCHEMA_VERSION = "missionos_core_mission_contract.v3"
MISSION_RUNTIME_EVIDENCE_SCHEMA_VERSION = (
    "missionos_core_mission_runtime_evidence.v2"
)
MISSION_CONTRACT_EVALUATION_SCHEMA_VERSION = (
    "missionos_core_mission_contract_evaluation.v2"
)
UTC_WALL_CLOCK_DOMAIN_REF = "clock:utc-wall"

CONTRACT_BINDING_ITEM_ID = "mission_contract_binding"
CONTRACT_STRUCTURE_ITEM_ID = "mission_contract_structure"
QUANTIFICATION_SCOPE_ITEM_ID = "mission_contract_quantification_scope"
OBSERVATION_COMPLETENESS_ITEM_ID = "mission_contract_observation_completeness"
OBSERVATION_FRESHNESS_ITEM_ID = "mission_contract_observation_freshness"
OBSERVATION_TEMPORAL_BINDING_ITEM_ID = (
    "mission_contract_observation_temporal_binding"
)
OBSERVATION_SCOPE_ITEM_ID = "mission_contract_observation_execution_scope"
OBSERVATION_CONTENT_BINDING_ITEM_ID = (
    "mission_contract_observation_content_binding"
)
TERMINATION_SEPARATION_ITEM_ID = "mission_contract_termination_separation"
OUTCOME_EVALUATION_ITEM_ID = "mission_contract_outcome_evaluation"

MISSION_CONTRACT_REQUIRED_ITEM_IDS = (
    CONTRACT_STRUCTURE_ITEM_ID,
    CONTRACT_BINDING_ITEM_ID,
    QUANTIFICATION_SCOPE_ITEM_ID,
    OBSERVATION_COMPLETENESS_ITEM_ID,
    OBSERVATION_FRESHNESS_ITEM_ID,
    OBSERVATION_TEMPORAL_BINDING_ITEM_ID,
    OBSERVATION_SCOPE_ITEM_ID,
    OBSERVATION_CONTENT_BINDING_ITEM_ID,
    TERMINATION_SEPARATION_ITEM_ID,
    OUTCOME_EVALUATION_ITEM_ID,
)

OUTCOME_REQUIRES_PREDICATE_REASON = (
    "outcome_claim_requires_approved_predicate_package"
)


def _sha256_is_valid(value: str | None) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _enum_material(value: Any) -> str:
    """Serialize an enum without letting malformed public input raise."""

    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _timestamp(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def mission_observation_source_receipt_binding_sha256(
    *,
    observation_id: str,
    content_sha256: str,
    observed_at: str,
    source_clock_domain_ref: str,
    received_at: str,
    receipt_clock_domain_ref: str,
) -> str:
    """Bind one source timestamp and one receipt timestamp to exact content.

    This proves only the integrity of the recorded association. It does not
    prove a clock offset, source latency, or source freshness.
    """

    return canonical_sha256(
        {
            "observation_id": observation_id,
            "content_sha256": content_sha256,
            "observed_at": observed_at,
            "source_clock_domain_ref": source_clock_domain_ref,
            "received_at": received_at,
            "receipt_clock_domain_ref": receipt_clock_domain_ref,
        }
    )


class QuantificationScopeKind(str, Enum):
    """How the outcome claim is quantified.

    ``NONE`` is an explicit decision that the claim quantifies over nothing,
    not an omission. Every contract must state one of these.
    """

    NONE = "none"
    ENUMERATED_MEMBERS = "enumerated_members"
    SPATIAL_REGION = "spatial_region"
    TIME_WINDOW = "time_window"
    OBSERVATION_EPOCH = "observation_epoch"


class TerminationReason(str, Enum):
    """How an instruction may end. None of these is an outcome."""

    EXPIRY = "expiry"
    OPERATOR_INTERRUPTION = "operator_interruption"
    SAFE_STOP = "safe_stop"
    TERMINAL_PREDICATE_SATISFIED = "terminal_predicate_satisfied"


class MissionEvidenceReadiness(str, Enum):
    """Whether the evidence is fit to be handed to a predicate package."""

    READY = "ready"
    INCOMPLETE = "incomplete"
    REFUSED = "refused"


class MissionContractOutcomeStatus(str, Enum):
    """Outcome state of one contract.

    ``VERIFIED_OUTCOME`` is intentionally absent. This module cannot reach it,
    because it evaluates no predicate.
    """

    BLOCKED = "blocked"
    UNVERIFIED = "unverified"


class ObservationFreshnessBasis(str, Enum):
    """Which timestamp a frozen requirement permits for freshness.

    Receipt freshness proves only when MissionOS received the observation. It
    never promotes that fact into source freshness.
    """

    SOURCE_OBSERVED_AT = "source_observed_at"
    MISSION_RECEIVED_AT = "mission_received_at"


@dataclass(frozen=True)
class QuantificationScope:
    """Mandatory statement of what the claim ranges over."""

    kind: QuantificationScopeKind
    member_ids: tuple[str, ...] = ()
    scope_ref: str | None = None
    reason: str = ""

    def to_material(self) -> dict[str, Any]:
        return {
            "kind": _enum_material(self.kind),
            "member_ids": list(self.member_ids),
            "scope_ref": self.scope_ref,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReferenceInput:
    """Approval-frozen value. Never an observation."""

    input_id: str
    kind: str
    content_sha256: str

    def to_material(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ObservationRequirement:
    """What the runtime must observe. Not the observation itself."""

    requirement_id: str
    evidence_kind: str
    required_origin: EvidenceOrigin
    maximum_age_seconds: float
    freshness_basis: ObservationFreshnessBasis = (
        ObservationFreshnessBasis.SOURCE_OBSERVED_AT
    )
    source_clock_domain_ref: str = UTC_WALL_CLOCK_DOMAIN_REF
    receipt_clock_domain_ref: str | None = None
    require_source_receipt_binding: bool = False

    def to_material(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "evidence_kind": self.evidence_kind,
            "required_origin": _enum_material(self.required_origin),
            "maximum_age_seconds": self.maximum_age_seconds,
            "freshness_basis": _enum_material(self.freshness_basis),
            "source_clock_domain_ref": self.source_clock_domain_ref,
            "receipt_clock_domain_ref": self.receipt_clock_domain_ref,
            "require_source_receipt_binding": (
                self.require_source_receipt_binding
            ),
        }


@dataclass(frozen=True)
class OutcomeClaimSpec:
    """The claim this contract may support, and nothing wider."""

    claim_id: str
    statement: str
    claim_scope: str
    universal: bool = False

    def to_material(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PredicatePackageBinding:
    """Approval-frozen identity of the package allowed to evaluate the claim."""

    package_id: str
    package_version: str
    content_sha256: str

    def to_material(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TerminationPolicy:
    """Frozen statement of how the instruction may end.

    ``maximum_duration_seconds`` is approval-frozen policy material. This
    readiness checker validates its shape but cannot enforce elapsed runtime
    because runtime evidence does not yet carry an execution-start timestamp.
    A later operational-termination evaluator must enforce it without treating
    expiry as an outcome claim.
    """

    allowed_reasons: tuple[TerminationReason, ...]
    maximum_duration_seconds: float | None = None

    def to_material(self) -> dict[str, Any]:
        return {
            "allowed_reasons": [
                _enum_material(reason) for reason in self.allowed_reasons
            ],
            "maximum_duration_seconds": self.maximum_duration_seconds,
        }


@dataclass(frozen=True)
class FrozenMissionContract:
    """Approval-frozen contract. Carries no runtime field by construction."""

    contract_id: str
    contract_version: str
    execution_scope: HardwareExecutionMode
    reference_inputs: tuple[ReferenceInput, ...]
    observation_requirements: tuple[ObservationRequirement, ...]
    quantification_scope: QuantificationScope
    outcome_claim_spec: OutcomeClaimSpec
    predicate_package: PredicatePackageBinding
    termination_policy: TerminationPolicy
    required_verification_basis: VerificationBasis
    schema_version: str = MISSION_CONTRACT_SCHEMA_VERSION

    def to_material(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "execution_scope": (
                self.execution_scope.value
                if isinstance(self.execution_scope, HardwareExecutionMode)
                else str(self.execution_scope)
            ),
            "reference_inputs": [
                item.to_material() for item in self.reference_inputs
            ],
            "observation_requirements": [
                item.to_material() for item in self.observation_requirements
            ],
            "quantification_scope": self.quantification_scope.to_material(),
            "outcome_claim_spec": self.outcome_claim_spec.to_material(),
            "predicate_package": (
                self.predicate_package.to_material()
                if isinstance(self.predicate_package, PredicatePackageBinding)
                else {"invalid": str(self.predicate_package)}
            ),
            "termination_policy": self.termination_policy.to_material(),
            "required_verification_basis": _enum_material(
                self.required_verification_basis
            ),
            "schema_version": self.schema_version,
        }

    @property
    def contract_sha256(self) -> str:
        return canonical_sha256(self.to_material())


@dataclass(frozen=True)
class MissionObservation:
    """One runtime fact. Produced during execution, never frozen.

    ``origin`` records where the fact came from. It is not a conclusion and it
    does not imply a verification basis.
    """

    observation_id: str
    requirement_id: str
    evidence_kind: str
    origin: EvidenceOrigin
    observed_at: str
    content_sha256: str
    execution_scope: HardwareExecutionMode
    source_clock_domain_ref: str = UTC_WALL_CLOCK_DOMAIN_REF
    received_at: str | None = None
    receipt_clock_domain_ref: str | None = None
    source_receipt_binding_sha256: str | None = None

    def to_material(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "requirement_id": self.requirement_id,
            "evidence_kind": self.evidence_kind,
            "origin": _enum_material(self.origin),
            "observed_at": self.observed_at,
            "source_clock_domain_ref": self.source_clock_domain_ref,
            "received_at": self.received_at,
            "receipt_clock_domain_ref": self.receipt_clock_domain_ref,
            "source_receipt_binding_sha256": (
                self.source_receipt_binding_sha256
            ),
            "content_sha256": self.content_sha256,
            "execution_scope": (
                self.execution_scope.value
                if isinstance(self.execution_scope, HardwareExecutionMode)
                else str(self.execution_scope)
            ),
        }


@dataclass(frozen=True)
class TerminationEvent:
    """A termination that occurred. Not evidence for the outcome claim."""

    reason: TerminationReason
    occurred_at: str

    def to_material(self) -> dict[str, Any]:
        return {
            "reason": _enum_material(self.reason),
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True)
class MissionRuntimeEvidence:
    """Runtime inputs to the readiness check, bound to one frozen contract.

    This type deliberately has no evaluated claim and no verification basis.
    Neither is an input a caller may declare.
    """

    contract_sha256: str
    observations: tuple[MissionObservation, ...] = ()
    termination_event: TerminationEvent | None = None
    closure_reason: str | None = None
    schema_version: str = MISSION_RUNTIME_EVIDENCE_SCHEMA_VERSION

    def to_material(self) -> dict[str, Any]:
        return {
            "contract_sha256": self.contract_sha256,
            "observations": [item.to_material() for item in self.observations],
            "termination_event": (
                self.termination_event.to_material()
                if self.termination_event is not None
                else None
            ),
            "closure_reason": self.closure_reason,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class MissionContractEvaluation:
    """The runtime record. Separate from the contract it was checked against.

    ``evaluated_outcome_claim`` and ``actual_verification_basis`` are present
    so the record has a stable shape, and are pinned to their weakest values
    because no predicate was evaluated.
    """

    contract_id: str
    contract_sha256: str
    evidence_readiness: MissionEvidenceReadiness
    status: MissionContractOutcomeStatus
    observations: tuple[MissionObservation, ...]
    termination_event: TerminationEvent | None
    closure_reason: str | None
    verification_items: tuple[VerificationItem, ...]
    reasons: tuple[str, ...] = ()
    evaluated_outcome_claim: Literal[False] = False
    actual_verification_basis: Literal[VerificationBasis.UNVERIFIED] = (
        VerificationBasis.UNVERIFIED
    )
    outcome_predicate_evaluated: Literal[False] = False
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    runtime_effect_requested: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    schema_version: str = MISSION_CONTRACT_EVALUATION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_sha256": self.contract_sha256,
            "evidence_readiness": self.evidence_readiness.value,
            "status": self.status.value,
            "evaluated_outcome_claim": self.evaluated_outcome_claim,
            "actual_verification_basis": self.actual_verification_basis.value,
            "outcome_predicate_evaluated": self.outcome_predicate_evaluated,
            "observations": [item.to_material() for item in self.observations],
            "termination_event": (
                self.termination_event.to_material()
                if self.termination_event is not None
                else None
            ),
            "closure_reason": self.closure_reason,
            "verification_items": [
                item.to_dict() for item in self.verification_items
            ],
            "reasons": list(self.reasons),
            "approval_created": self.approval_created,
            "dispatch_authority_created": self.dispatch_authority_created,
            "runtime_effect_requested": self.runtime_effect_requested,
            "physical_execution_invoked": self.physical_execution_invoked,
            "schema_version": self.schema_version,
        }


def validate_frozen_mission_contract(
    contract: FrozenMissionContract,
) -> tuple[str, ...]:
    """Return structural refusal reasons for a contract, fail-closed."""

    reasons: list[str] = []
    if contract.schema_version != MISSION_CONTRACT_SCHEMA_VERSION:
        reasons.append("contract_schema_not_supported")
    if not str(contract.contract_id or "").strip():
        reasons.append("contract_id_missing")
    if not str(contract.contract_version or "").strip():
        reasons.append("contract_version_missing")
    if parse_hardware_execution_mode(contract.execution_scope) is None:
        reasons.append("contract_execution_scope_invalid")

    seen_inputs: set[str] = set()
    for reference in contract.reference_inputs:
        input_id = str(reference.input_id or "").strip()
        if not input_id:
            reasons.append("reference_input_id_missing")
            continue
        if input_id in seen_inputs:
            reasons.append(f"reference_input_duplicate:{input_id}")
        seen_inputs.add(input_id)
        if not str(reference.kind or "").strip():
            reasons.append(f"reference_input_kind_missing:{input_id}")
        if not _sha256_is_valid(reference.content_sha256):
            reasons.append(f"reference_input_digest_invalid:{input_id}")

    seen_requirements: set[str] = set()
    for requirement in contract.observation_requirements:
        requirement_id = str(requirement.requirement_id or "").strip()
        if not requirement_id:
            reasons.append("observation_requirement_id_missing")
            continue
        if requirement_id in seen_requirements:
            reasons.append(
                f"observation_requirement_duplicate:{requirement_id}"
            )
        seen_requirements.add(requirement_id)
        if not str(requirement.evidence_kind or "").strip():
            reasons.append(
                f"observation_requirement_kind_missing:{requirement_id}"
            )
        if not isinstance(requirement.required_origin, EvidenceOrigin):
            reasons.append(
                f"observation_requirement_origin_invalid:{requirement_id}"
            )
        age = requirement.maximum_age_seconds
        if not isinstance(age, (int, float)) or isinstance(age, bool):
            reasons.append(
                f"observation_requirement_age_invalid:{requirement_id}"
            )
        elif not isfinite(float(age)):
            reasons.append(
                f"observation_requirement_age_not_finite:{requirement_id}"
            )
        elif age <= 0:
            reasons.append(
                f"observation_requirement_age_not_positive:{requirement_id}"
            )
        if not isinstance(
            requirement.freshness_basis, ObservationFreshnessBasis
        ):
            reasons.append(
                f"observation_requirement_freshness_basis_invalid:{requirement_id}"
            )
        if not str(requirement.source_clock_domain_ref or "").strip():
            reasons.append(
                f"observation_requirement_source_clock_domain_missing:{requirement_id}"
            )
        if not isinstance(requirement.require_source_receipt_binding, bool):
            reasons.append(
                f"observation_requirement_source_receipt_binding_invalid:{requirement_id}"
            )
        if requirement.require_source_receipt_binding is True:
            if not str(requirement.receipt_clock_domain_ref or "").strip():
                reasons.append(
                    f"observation_requirement_receipt_clock_domain_missing:{requirement_id}"
                )
        if (
            requirement.freshness_basis
            is ObservationFreshnessBasis.MISSION_RECEIVED_AT
        ):
            if requirement.require_source_receipt_binding is not True:
                reasons.append(
                    f"observation_requirement_source_receipt_binding_required:{requirement_id}"
                )
    if not contract.observation_requirements:
        reasons.append("observation_requirements_missing")

    reasons.extend(_quantification_scope_reasons(contract))

    claim = contract.outcome_claim_spec
    if not str(claim.claim_id or "").strip():
        reasons.append("outcome_claim_id_missing")
    if not str(claim.statement or "").strip():
        reasons.append("outcome_claim_statement_missing")
    if not str(claim.claim_scope or "").strip():
        reasons.append("outcome_claim_scope_missing")

    package = contract.predicate_package
    if not isinstance(package, PredicatePackageBinding):
        reasons.append("predicate_package_binding_missing")
    else:
        if not str(package.package_id or "").strip():
            reasons.append("predicate_package_id_missing")
        if not str(package.package_version or "").strip():
            reasons.append("predicate_package_version_missing")
        if not _sha256_is_valid(package.content_sha256):
            reasons.append("predicate_package_digest_invalid")

    termination_policy = contract.termination_policy
    if not termination_policy.allowed_reasons:
        reasons.append("termination_policy_missing")
    for index, reason in enumerate(termination_policy.allowed_reasons):
        if not isinstance(reason, TerminationReason):
            reasons.append(f"termination_policy_reason_invalid:{index}")
    duration = termination_policy.maximum_duration_seconds
    if duration is not None:
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            reasons.append("termination_policy_maximum_duration_invalid")
        elif not isfinite(float(duration)):
            reasons.append("termination_policy_maximum_duration_not_finite")
        elif duration <= 0:
            reasons.append(
                "termination_policy_maximum_duration_not_positive"
            )
    if not isinstance(contract.required_verification_basis, VerificationBasis):
        reasons.append("required_verification_basis_invalid")
    elif contract.required_verification_basis is VerificationBasis.UNVERIFIED:
        reasons.append("required_verification_basis_unverified")

    return tuple(reasons)


def _quantification_scope_reasons(
    contract: FrozenMissionContract,
) -> tuple[str, ...]:
    scope = contract.quantification_scope
    reasons: list[str] = []
    if not isinstance(scope, QuantificationScope) or not isinstance(
        scope.kind, QuantificationScopeKind
    ):
        return ("quantification_scope_missing",)

    if scope.kind is QuantificationScopeKind.NONE:
        if not str(scope.reason or "").strip():
            reasons.append("quantification_scope_none_reason_missing")
        if contract.outcome_claim_spec.universal:
            reasons.append("universal_claim_without_quantification_scope")
        return tuple(reasons)

    if scope.kind is QuantificationScopeKind.ENUMERATED_MEMBERS:
        members = tuple(str(member).strip() for member in scope.member_ids)
        if not members or any(not member for member in members):
            reasons.append("quantification_scope_members_missing")
        elif len(set(members)) != len(members):
            reasons.append("quantification_scope_members_duplicate")
        return tuple(reasons)

    scope_ref = str(scope.scope_ref or "").strip()
    if not scope_ref:
        reasons.append("quantification_scope_ref_missing")
    elif scope_ref not in {
        str(reference.input_id) for reference in contract.reference_inputs
    }:
        reasons.append(f"quantification_scope_ref_unbound:{scope_ref}")
    return tuple(reasons)


def _item(
    item_id: str,
    *,
    predicate: str,
    passed: bool,
    evidence_refs: Sequence[str],
    blocked: bool = False,
) -> VerificationItem:
    if passed:
        status = VerificationItemStatus.PASS
    elif blocked:
        status = VerificationItemStatus.BLOCKED
    else:
        status = VerificationItemStatus.FAIL
    return VerificationItem(
        item_id=item_id,
        predicate=predicate,
        status=status,
        verification_basis=VerificationBasis.DETERMINISTIC,
        evidence_refs=tuple(evidence_refs) or ("mission-contract:none",),
    )


@dataclass(frozen=True)
class _ObservationChecks:
    complete: bool
    fresh: bool
    temporally_bound: bool
    in_scope: bool
    content_bound: bool
    reasons: tuple[str, ...]


def check_mission_evidence_readiness(
    *,
    contract: FrozenMissionContract,
    evidence: MissionRuntimeEvidence,
    evaluated_at: str,
    evaluation_clock_domain_ref: str = UTC_WALL_CLOCK_DOMAIN_REF,
) -> MissionContractEvaluation:
    """Report whether runtime evidence is fit for predicate evaluation.

    This never concludes that the outcome claim holds. A complete, fresh,
    in-scope, content-bound observation set only means a predicate package may
    now be run against it. Whether the observed content satisfies the claim is
    outside this module.
    """

    contract_sha256 = contract.contract_sha256
    reasons: list[str] = [OUTCOME_REQUIRES_PREDICATE_REASON]
    items: list[VerificationItem] = []
    contract_ref = f"mission-contract:{contract_sha256}"

    structure_reasons = validate_frozen_mission_contract(contract)
    reasons.extend(structure_reasons)
    items.append(
        _item(
            CONTRACT_STRUCTURE_ITEM_ID,
            predicate="the frozen contract is structurally complete",
            passed=not structure_reasons,
            blocked=True,
            evidence_refs=(contract_ref,),
        )
    )

    binding_matches = (
        _sha256_is_valid(evidence.contract_sha256)
        and evidence.contract_sha256 == contract_sha256
        and evidence.schema_version == MISSION_RUNTIME_EVIDENCE_SCHEMA_VERSION
    )
    if not binding_matches:
        reasons.append("runtime_evidence_contract_binding_mismatch")
    items.append(
        _item(
            CONTRACT_BINDING_ITEM_ID,
            predicate=(
                "the runtime evidence is bound to the exact frozen contract "
                "digest"
            ),
            passed=binding_matches,
            blocked=True,
            evidence_refs=(contract_ref,),
        )
    )

    scope_reasons = _quantification_scope_reasons(contract)
    items.append(
        _item(
            QUANTIFICATION_SCOPE_ITEM_ID,
            predicate=(
                "the contract states an explicit quantification scope, "
                "including an explicit none"
            ),
            passed=not scope_reasons,
            blocked=True,
            evidence_refs=(contract_ref,),
        )
    )

    checks = _observation_checks(
        contract=contract,
        evidence=evidence,
        evaluated_at=evaluated_at,
        evaluation_clock_domain_ref=evaluation_clock_domain_ref,
    )
    reasons.extend(checks.reasons)
    observation_refs = tuple(
        f"mission-observation:{observation.observation_id}"
        for observation in evidence.observations
    ) or (contract_ref,)
    items.append(
        _item(
            OBSERVATION_COMPLETENESS_ITEM_ID,
            predicate=(
                "every frozen observation requirement has a matching runtime "
                "observation of the required kind and origin"
            ),
            passed=checks.complete,
            evidence_refs=observation_refs,
        )
    )
    items.append(
        _item(
            OBSERVATION_FRESHNESS_ITEM_ID,
            predicate=(
                "every observation's frozen freshness basis is within the "
                "maximum age in the evaluation clock domain"
            ),
            passed=checks.fresh,
            evidence_refs=observation_refs,
        )
    )
    items.append(
        _item(
            OBSERVATION_TEMPORAL_BINDING_ITEM_ID,
            predicate=(
                "source and receipt times retain their clock domains, and "
                "receipt-based freshness is content-bound without being "
                "promoted to source freshness"
            ),
            passed=checks.temporally_bound,
            evidence_refs=observation_refs,
        )
    )
    items.append(
        _item(
            OBSERVATION_SCOPE_ITEM_ID,
            predicate=(
                "every observation was observed in the contract's frozen "
                "execution scope"
            ),
            passed=checks.in_scope,
            evidence_refs=observation_refs,
        )
    )
    items.append(
        _item(
            OBSERVATION_CONTENT_BINDING_ITEM_ID,
            predicate="every observation carries a valid content digest",
            passed=checks.content_bound,
            evidence_refs=observation_refs,
        )
    )

    termination_ok, termination_reasons = _termination_results(
        contract=contract, evidence=evidence
    )
    reasons.extend(termination_reasons)
    items.append(
        _item(
            TERMINATION_SEPARATION_ITEM_ID,
            predicate=(
                "termination and closure are recorded separately from the "
                "outcome claim and never substitute for it"
            ),
            passed=termination_ok,
            blocked=True,
            evidence_refs=(contract_ref,),
        )
    )

    items.append(
        _item(
            OUTCOME_EVALUATION_ITEM_ID,
            predicate=(
                "the outcome claim is evaluated by an approved versioned "
                "predicate package"
            ),
            passed=False,
            evidence_refs=(contract_ref,),
        )
    )

    refused = (
        bool(structure_reasons)
        or not binding_matches
        or bool(scope_reasons)
        or not termination_ok
    )
    ready = (
        not refused
        and checks.complete
        and checks.fresh
        and checks.temporally_bound
        and checks.in_scope
        and checks.content_bound
    )
    if refused:
        readiness = MissionEvidenceReadiness.REFUSED
    elif ready:
        readiness = MissionEvidenceReadiness.READY
    else:
        readiness = MissionEvidenceReadiness.INCOMPLETE

    status = (
        MissionContractOutcomeStatus.BLOCKED
        if refused
        else MissionContractOutcomeStatus.UNVERIFIED
    )

    return MissionContractEvaluation(
        contract_id=contract.contract_id,
        contract_sha256=contract_sha256,
        evidence_readiness=readiness,
        status=status,
        observations=tuple(evidence.observations),
        termination_event=evidence.termination_event,
        closure_reason=evidence.closure_reason,
        verification_items=tuple(items),
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _observation_checks(
    *,
    contract: FrozenMissionContract,
    evidence: MissionRuntimeEvidence,
    evaluated_at: str,
    evaluation_clock_domain_ref: str,
) -> _ObservationChecks:
    reasons: list[str] = []
    complete = bool(contract.observation_requirements)
    fresh = True
    temporally_bound = True
    in_scope = True
    content_bound = True

    evaluated_time = _timestamp(evaluated_at)
    if evaluated_time is None:
        reasons.append("evaluation_time_invalid")
        fresh = False
    evaluation_domain = str(evaluation_clock_domain_ref or "").strip()
    if not evaluation_domain:
        reasons.append("evaluation_clock_domain_missing")
        temporally_bound = False

    expected_scope = parse_hardware_execution_mode(contract.execution_scope)
    if expected_scope is None:
        in_scope = False

    by_requirement: dict[str, list[MissionObservation]] = {}
    for observation in evidence.observations:
        by_requirement.setdefault(
            str(observation.requirement_id or ""), []
        ).append(observation)

    known = {
        str(requirement.requirement_id)
        for requirement in contract.observation_requirements
    }
    for requirement_id in by_requirement:
        if requirement_id not in known:
            reasons.append(f"observation_not_required:{requirement_id}")
            complete = False

    for requirement in contract.observation_requirements:
        requirement_id = str(requirement.requirement_id)
        matches = by_requirement.get(requirement_id, [])
        if not matches:
            reasons.append(f"observation_missing:{requirement_id}")
            complete = False
            continue
        for observation in matches:
            if observation.evidence_kind != requirement.evidence_kind:
                reasons.append(f"observation_kind_mismatch:{requirement_id}")
                complete = False
            if observation.origin is not requirement.required_origin:
                reasons.append(f"observation_origin_mismatch:{requirement_id}")
                complete = False

            source_time = _timestamp(observation.observed_at)
            if source_time is None:
                reasons.append(f"observation_time_invalid:{requirement_id}")
                fresh = False
            source_domain = str(
                observation.source_clock_domain_ref or ""
            ).strip()
            required_source_domain = str(
                requirement.source_clock_domain_ref or ""
            ).strip()
            if not source_domain:
                reasons.append(
                    f"observation_source_clock_domain_missing:{requirement_id}"
                )
                temporally_bound = False
            elif source_domain != required_source_domain:
                reasons.append(
                    f"observation_source_clock_domain_mismatch:{requirement_id}"
                )
                temporally_bound = False

            freshness_time = source_time
            freshness_domain = source_domain
            receipt_time = _timestamp(observation.received_at)
            receipt_domain = str(
                observation.receipt_clock_domain_ref or ""
            ).strip()
            required_receipt_domain = str(
                requirement.receipt_clock_domain_ref or ""
            ).strip()
            if requirement.require_source_receipt_binding is True:
                if receipt_time is None:
                    reasons.append(
                        f"observation_receipt_time_invalid:{requirement_id}"
                    )
                    temporally_bound = False
                if not receipt_domain:
                    reasons.append(
                        f"observation_receipt_clock_domain_missing:{requirement_id}"
                    )
                    temporally_bound = False
                elif receipt_domain != required_receipt_domain:
                    reasons.append(
                        f"observation_receipt_clock_domain_mismatch:{requirement_id}"
                    )
                    temporally_bound = False
                if not _sha256_is_valid(
                    observation.source_receipt_binding_sha256
                ):
                    reasons.append(
                        f"observation_source_receipt_binding_invalid:{requirement_id}"
                    )
                    temporally_bound = False
                elif (
                    observation.source_receipt_binding_sha256
                    != mission_observation_source_receipt_binding_sha256(
                        observation_id=observation.observation_id,
                        content_sha256=observation.content_sha256,
                        observed_at=observation.observed_at,
                        source_clock_domain_ref=source_domain,
                        received_at=str(observation.received_at or ""),
                        receipt_clock_domain_ref=receipt_domain,
                    )
                ):
                    reasons.append(
                        f"observation_source_receipt_binding_mismatch:{requirement_id}"
                    )
                    temporally_bound = False
            if (
                requirement.freshness_basis
                is ObservationFreshnessBasis.MISSION_RECEIVED_AT
            ):
                freshness_time = receipt_time
                freshness_domain = receipt_domain
                if freshness_time is None:
                    fresh = False

            if (
                freshness_domain
                and evaluation_domain
                and freshness_domain != evaluation_domain
            ):
                reasons.append(
                    f"observation_freshness_clock_domain_mismatch:{requirement_id}"
                )
                fresh = False
                temporally_bound = False
            elif freshness_time is not None and evaluated_time is not None:
                age = (evaluated_time - freshness_time).total_seconds()
                if age < 0:
                    reasons.append(
                        f"observation_time_after_evaluation:{requirement_id}"
                    )
                    fresh = False
                elif (
                    isinstance(requirement.maximum_age_seconds, (int, float))
                    and not isinstance(requirement.maximum_age_seconds, bool)
                    and age > float(requirement.maximum_age_seconds)
                ):
                    reasons.append(f"observation_stale:{requirement_id}")
                    fresh = False

            observed_scope = parse_hardware_execution_mode(
                observation.execution_scope
            )
            if observed_scope is None:
                reasons.append(
                    f"observation_execution_scope_invalid:{requirement_id}"
                )
                in_scope = False
            elif expected_scope is not None and observed_scope != expected_scope:
                reasons.append(
                    f"observation_execution_scope_mismatch:{requirement_id}"
                )
                in_scope = False

            if not _sha256_is_valid(observation.content_sha256):
                reasons.append(
                    f"observation_content_digest_invalid:{requirement_id}"
                )
                content_bound = False

    if not contract.observation_requirements:
        complete = False

    return _ObservationChecks(
        complete=complete,
        fresh=fresh,
        temporally_bound=temporally_bound,
        in_scope=in_scope,
        content_bound=content_bound,
        reasons=tuple(reasons),
    )


def _termination_results(
    *,
    contract: FrozenMissionContract,
    evidence: MissionRuntimeEvidence,
) -> tuple[bool, tuple[str, ...]]:
    event = evidence.termination_event
    if event is None:
        if evidence.closure_reason is not None:
            return False, ("closure_reason_without_termination_event",)
        return True, ()

    if not isinstance(event.reason, TerminationReason):
        return False, ("termination_reason_invalid",)
    if event.reason not in contract.termination_policy.allowed_reasons:
        return False, (
            f"termination_reason_not_allowed:{event.reason.value}",
        )
    if _timestamp(event.occurred_at) is None:
        return False, ("termination_time_invalid",)
    return True, ()


def frozen_contract_from_mapping(
    value: Mapping[str, Any],
) -> FrozenMissionContract | None:
    """Rebuild a frozen contract from stored material, or refuse it.

    Returns ``None`` rather than a partially populated contract so a caller
    cannot recover a contract whose digest would no longer match.
    """

    try:
        scope_value = dict(value["quantification_scope"])
        claim_value = dict(value["outcome_claim_spec"])
        package_value = dict(value["predicate_package"])
        termination_value = dict(value["termination_policy"])
        contract = FrozenMissionContract(
            contract_id=str(value["contract_id"]),
            contract_version=str(value["contract_version"]),
            execution_scope=HardwareExecutionMode(value["execution_scope"]),
            reference_inputs=tuple(
                ReferenceInput(
                    input_id=str(item["input_id"]),
                    kind=str(item["kind"]),
                    content_sha256=str(item["content_sha256"]),
                )
                for item in value.get("reference_inputs", ())
            ),
            observation_requirements=tuple(
                ObservationRequirement(
                    requirement_id=str(item["requirement_id"]),
                    evidence_kind=str(item["evidence_kind"]),
                    required_origin=EvidenceOrigin(item["required_origin"]),
                    maximum_age_seconds=float(item["maximum_age_seconds"]),
                    freshness_basis=ObservationFreshnessBasis(
                        item.get(
                            "freshness_basis",
                            ObservationFreshnessBasis.SOURCE_OBSERVED_AT.value,
                        )
                    ),
                    source_clock_domain_ref=str(
                        item.get(
                            "source_clock_domain_ref",
                            UTC_WALL_CLOCK_DOMAIN_REF,
                        )
                    ),
                    receipt_clock_domain_ref=(
                        str(item["receipt_clock_domain_ref"])
                        if item.get("receipt_clock_domain_ref") is not None
                        else None
                    ),
                    require_source_receipt_binding=item.get(
                        "require_source_receipt_binding", False
                    ),
                )
                for item in value.get("observation_requirements", ())
            ),
            quantification_scope=QuantificationScope(
                kind=QuantificationScopeKind(scope_value["kind"]),
                member_ids=tuple(
                    str(member) for member in scope_value.get("member_ids", ())
                ),
                scope_ref=scope_value.get("scope_ref"),
                reason=str(scope_value.get("reason") or ""),
            ),
            outcome_claim_spec=OutcomeClaimSpec(
                claim_id=str(claim_value["claim_id"]),
                statement=str(claim_value["statement"]),
                claim_scope=str(claim_value["claim_scope"]),
                universal=bool(claim_value.get("universal", False)),
            ),
            predicate_package=PredicatePackageBinding(
                package_id=str(package_value["package_id"]),
                package_version=str(package_value["package_version"]),
                content_sha256=str(package_value["content_sha256"]),
            ),
            termination_policy=TerminationPolicy(
                allowed_reasons=tuple(
                    TerminationReason(reason)
                    for reason in termination_value.get("allowed_reasons", ())
                ),
                maximum_duration_seconds=termination_value.get(
                    "maximum_duration_seconds"
                ),
            ),
            required_verification_basis=VerificationBasis(
                value["required_verification_basis"]
            ),
            schema_version=str(value["schema_version"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return contract


__all__ = [
    "CONTRACT_BINDING_ITEM_ID",
    "CONTRACT_STRUCTURE_ITEM_ID",
    "MISSION_CONTRACT_EVALUATION_SCHEMA_VERSION",
    "MISSION_CONTRACT_REQUIRED_ITEM_IDS",
    "MISSION_CONTRACT_SCHEMA_VERSION",
    "MISSION_RUNTIME_EVIDENCE_SCHEMA_VERSION",
    "OBSERVATION_COMPLETENESS_ITEM_ID",
    "OBSERVATION_CONTENT_BINDING_ITEM_ID",
    "OBSERVATION_FRESHNESS_ITEM_ID",
    "OBSERVATION_TEMPORAL_BINDING_ITEM_ID",
    "OBSERVATION_SCOPE_ITEM_ID",
    "OUTCOME_EVALUATION_ITEM_ID",
    "OUTCOME_REQUIRES_PREDICATE_REASON",
    "QUANTIFICATION_SCOPE_ITEM_ID",
    "TERMINATION_SEPARATION_ITEM_ID",
    "FrozenMissionContract",
    "MissionContractEvaluation",
    "MissionContractOutcomeStatus",
    "MissionEvidenceReadiness",
    "MissionObservation",
    "MissionRuntimeEvidence",
    "ObservationRequirement",
    "ObservationFreshnessBasis",
    "OutcomeClaimSpec",
    "PredicatePackageBinding",
    "QuantificationScope",
    "QuantificationScopeKind",
    "ReferenceInput",
    "TerminationEvent",
    "TerminationPolicy",
    "TerminationReason",
    "UTC_WALL_CLOCK_DOMAIN_REF",
    "check_mission_evidence_readiness",
    "frozen_contract_from_mapping",
    "mission_observation_source_receipt_binding_sha256",
    "validate_frozen_mission_contract",
]

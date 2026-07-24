"""Backend-neutral evidence and action-feasibility contracts.

Core records what was observed, what was derived, and why an action is or is
not verifiable. Backend extensions perform geometry and performance
calculations. Core never creates approval, dispatch, execution, progress, or
completion authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol


HAZARD_STATE_SCHEMA_VERSION = "missionos_core_hazard_state.v1"
ACTION_CANDIDATE_SCHEMA_VERSION = "missionos_core_action_candidate.v1"
ACTION_FEASIBILITY_SCHEMA_VERSION = "missionos_core_action_feasibility.v1"
REVALIDATION_SCHEMA_VERSION = "missionos_core_action_revalidation.v1"


def canonical_sha256(value: Mapping[str, Any]) -> str:
    """Return a stable digest for policy, model, and evidence bindings."""

    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _parse_timestamp(value: str) -> datetime | None:
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


class FactStatus(str, Enum):
    """Evidence availability without implying action authority."""

    OBSERVED = "observed"
    DERIVED = "derived"
    UNVERIFIED = "unverified"


class FeasibilityStatus(str, Enum):
    """Only the deterministic verifier may produce these states."""

    VERIFIED_FEASIBLE = "verified_feasible"
    BLOCKED = "blocked"
    UNVERIFIED = "unverified"


class CursorOrder(str, Enum):
    """Ordering proof returned by a backend cursor comparator."""

    BEFORE = "before"
    EQUAL = "equal"
    AFTER = "after"
    INCOMPARABLE = "incomparable"


@dataclass(frozen=True)
class EvidenceSourceRef:
    """Reference to source evidence without embedding private source content."""

    source_id: str
    evidence_kind: str
    observed_at: str | None
    freshness_deadline: str | None = None
    content_sha256: str | None = None
    freshness_proof: str = "timestamp"

    def freshness_reason(self, *, evaluated_at: str) -> str | None:
        if self.freshness_proof == "adapter_cursor_verified":
            return None
        if self.freshness_proof != "timestamp":
            return "evidence_freshness_proof_unverified"
        observed = _parse_timestamp(self.observed_at)
        evaluated = _parse_timestamp(evaluated_at)
        if observed is None or evaluated is None:
            return "evidence_timestamp_unverified"
        if observed > evaluated:
            return "evidence_timestamp_in_future"
        if self.freshness_deadline is None:
            return None
        deadline = _parse_timestamp(self.freshness_deadline)
        if deadline is None:
            return "evidence_freshness_deadline_unverified"
        if evaluated > deadline:
            return "evidence_stale"
        return None


@dataclass(frozen=True)
class ObservationCursor:
    """Opaque backend cursor whose ordering requires adapter proof."""

    adapter_id: str
    comparison_contract: str
    value: Mapping[str, Any]


class ObservationCursorComparator(Protocol):
    """Backend proof of whether two opaque cursors are comparable and ordered."""

    comparator_id: str
    comparison_contract: str

    def compare(
        self,
        earlier: ObservationCursor,
        later: ObservationCursor,
    ) -> CursorOrder: ...


@dataclass(frozen=True)
class ObservedFact:
    name: str
    value: Any
    unit: str | None
    source: EvidenceSourceRef
    frame: str | None = None
    status: FactStatus = FactStatus.OBSERVED


@dataclass(frozen=True)
class ModelBinding:
    model_id: str
    model_version: str
    parameters_sha256: str
    uncertainty: Mapping[str, Any]

    @property
    def digest(self) -> str:
        return canonical_sha256(asdict(self))


@dataclass(frozen=True)
class DerivedFact:
    name: str
    value: Any
    unit: str | None
    source_refs: tuple[str, ...]
    derivation_id: str
    model_binding: ModelBinding | None = None
    uncertainty: Mapping[str, Any] = field(default_factory=dict)
    status: FactStatus = FactStatus.DERIVED


@dataclass(frozen=True)
class PolicyBinding:
    policy_id: str
    policy_version: str
    policy_sha256: str


@dataclass(frozen=True)
class HazardState:
    """Normalized evidence presented to deterministic verifier extensions."""

    state_id: str
    collected_at: str
    cursor: ObservationCursor
    policy_binding: PolicyBinding
    observed_facts: tuple[ObservedFact, ...]
    derived_facts: tuple[DerivedFact, ...] = ()
    assumptions: tuple[str, ...] = ()
    schema_version: str = HAZARD_STATE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> HazardState:
        observed = tuple(
            ObservedFact(
                name=str(item["name"]),
                value=item.get("value"),
                unit=item.get("unit"),
                source=EvidenceSourceRef(**dict(item["source"])),
                frame=item.get("frame"),
                status=FactStatus(item.get("status", FactStatus.OBSERVED)),
            )
            for item in value.get("observed_facts", ())
        )
        derived: list[DerivedFact] = []
        for item in value.get("derived_facts", ()):
            item = dict(item)
            binding = item.get("model_binding")
            derived.append(
                DerivedFact(
                    name=str(item["name"]),
                    value=item.get("value"),
                    unit=item.get("unit"),
                    source_refs=tuple(item.get("source_refs", ())),
                    derivation_id=str(item["derivation_id"]),
                    model_binding=(
                        ModelBinding(**dict(binding)) if binding else None
                    ),
                    uncertainty=dict(item.get("uncertainty", {})),
                    status=FactStatus(
                        item.get("status", FactStatus.DERIVED)
                    ),
                )
            )
        cursor = dict(value["cursor"])
        policy = dict(value["policy_binding"])
        return cls(
            state_id=str(value["state_id"]),
            collected_at=str(value["collected_at"]),
            cursor=ObservationCursor(
                adapter_id=str(cursor["adapter_id"]),
                comparison_contract=str(cursor["comparison_contract"]),
                value=dict(cursor["value"]),
            ),
            policy_binding=PolicyBinding(**policy),
            observed_facts=observed,
            derived_facts=tuple(derived),
            assumptions=tuple(value.get("assumptions", ())),
            schema_version=str(
                value.get("schema_version", HAZARD_STATE_SCHEMA_VERSION)
            ),
        )


@dataclass(frozen=True)
class ActionCandidate:
    candidate_id: str
    action: str
    parameters: Mapping[str, Any]
    evidence_refs: tuple[str, ...]
    extension_inputs: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = ACTION_CANDIDATE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ActionCandidate:
        return cls(
            candidate_id=str(value["candidate_id"]),
            action=str(value["action"]),
            parameters=dict(value.get("parameters", {})),
            evidence_refs=tuple(value.get("evidence_refs", ())),
            extension_inputs=dict(value.get("extension_inputs", {})),
            schema_version=str(
                value.get("schema_version", ACTION_CANDIDATE_SCHEMA_VERSION)
            ),
        )


@dataclass(frozen=True)
class ExtensionVerdict:
    """Backend calculation returned without authority-bearing side effects."""

    extension_id: str
    status: FeasibilityStatus
    blocked_reasons: tuple[str, ...] = ()
    unverified_reasons: tuple[str, ...] = ()
    measurements: Mapping[str, Any] = field(default_factory=dict)
    assumptions: tuple[str, ...] = ()


class FeasibilityVerifierExtension(Protocol):
    """Backend-specific geometry/performance calculation boundary."""

    extension_id: str

    def verify(
        self,
        *,
        hazard_state: HazardState,
        candidate: ActionCandidate,
    ) -> ExtensionVerdict: ...


@dataclass(frozen=True)
class ActionFeasibilityResult:
    candidate_id: str
    hazard_state_id: str
    status: FeasibilityStatus
    blocked_reasons: tuple[str, ...]
    unverified_reasons: tuple[str, ...]
    assumptions: tuple[str, ...]
    extension_verdicts: tuple[ExtensionVerdict, ...]
    policy_sha256: str
    evaluated_at: str
    approval_created: bool = False
    dispatch_authority_created: bool = False
    execution_invoked: bool = False
    progress_claimed: bool = False
    completion_claimed: bool = False
    delivery_completion_claimed: bool = False
    physical_execution_invoked: bool = False
    schema_version: str = ACTION_FEASIBILITY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> ActionFeasibilityResult:
        verdicts = tuple(
            ExtensionVerdict(
                extension_id=str(item["extension_id"]),
                status=FeasibilityStatus(item["status"]),
                blocked_reasons=tuple(item.get("blocked_reasons", ())),
                unverified_reasons=tuple(item.get("unverified_reasons", ())),
                measurements=dict(item.get("measurements", {})),
                assumptions=tuple(item.get("assumptions", ())),
            )
            for item in value.get("extension_verdicts", ())
        )
        return cls(
            candidate_id=str(value["candidate_id"]),
            hazard_state_id=str(value["hazard_state_id"]),
            status=FeasibilityStatus(value["status"]),
            blocked_reasons=tuple(value.get("blocked_reasons", ())),
            unverified_reasons=tuple(value.get("unverified_reasons", ())),
            assumptions=tuple(value.get("assumptions", ())),
            extension_verdicts=verdicts,
            policy_sha256=str(value["policy_sha256"]),
            evaluated_at=str(value["evaluated_at"]),
            approval_created=bool(value.get("approval_created", False)),
            dispatch_authority_created=bool(
                value.get("dispatch_authority_created", False)
            ),
            execution_invoked=bool(value.get("execution_invoked", False)),
            progress_claimed=bool(value.get("progress_claimed", False)),
            completion_claimed=bool(value.get("completion_claimed", False)),
            delivery_completion_claimed=bool(
                value.get("delivery_completion_claimed", False)
            ),
            physical_execution_invoked=bool(
                value.get("physical_execution_invoked", False)
            ),
            schema_version=str(
                value.get("schema_version", ACTION_FEASIBILITY_SCHEMA_VERSION)
            ),
        )


def verify_action_candidate(
    *,
    hazard_state: HazardState,
    candidate: ActionCandidate,
    active_policy: PolicyBinding,
    evaluated_at: str,
    extensions: Sequence[FeasibilityVerifierExtension],
    previous_cursor: ObservationCursor | None = None,
    cursor_comparator: ObservationCursorComparator | None = None,
    active_model_digests: Mapping[str, str] | None = None,
) -> ActionFeasibilityResult:
    """Aggregate source validity and extension verdicts without authority."""

    blocked: list[str] = []
    unverified: list[str] = []
    assumptions = list(hazard_state.assumptions)

    if hazard_state.schema_version != HAZARD_STATE_SCHEMA_VERSION:
        unverified.append("hazard_state_schema_not_supported")
    if candidate.schema_version != ACTION_CANDIDATE_SCHEMA_VERSION:
        unverified.append("action_candidate_schema_not_supported")
    if active_policy.policy_sha256 != hazard_state.policy_binding.policy_sha256:
        blocked.append("policy_binding_drift")

    sources = {fact.source.source_id: fact.source for fact in hazard_state.observed_facts}
    if not candidate.evidence_refs:
        unverified.append("candidate_evidence_refs_missing")
    for source_id in candidate.evidence_refs:
        source = sources.get(source_id)
        if source is None:
            unverified.append("candidate_evidence_source_missing")
            continue
        reason = source.freshness_reason(evaluated_at=evaluated_at)
        if reason:
            unverified.append(reason)

    for fact in hazard_state.derived_facts:
        if not fact.source_refs or any(ref not in sources for ref in fact.source_refs):
            unverified.append("derived_fact_source_missing")
        if fact.status is FactStatus.UNVERIFIED:
            unverified.append("derived_fact_unverified")
        binding = fact.model_binding
        if binding is not None:
            active_digest = (active_model_digests or {}).get(binding.model_id)
            if active_digest is None:
                unverified.append("active_model_binding_missing")
            elif active_digest != binding.digest:
                blocked.append("model_binding_drift")

    if previous_cursor is not None:
        if cursor_comparator is None:
            unverified.append("cursor_comparator_missing")
        elif (
            cursor_comparator.comparison_contract
            != hazard_state.cursor.comparison_contract
            or previous_cursor.comparison_contract
            != hazard_state.cursor.comparison_contract
        ):
            unverified.append("cursor_comparison_contract_mismatch")
        else:
            order = cursor_comparator.compare(previous_cursor, hazard_state.cursor)
            if order is CursorOrder.INCOMPARABLE:
                unverified.append("cursor_incomparable")
            elif order is CursorOrder.AFTER:
                blocked.append("cursor_regression")

    extension_verdicts: list[ExtensionVerdict] = []
    if not extensions:
        unverified.append("verifier_extension_missing")
    for extension in extensions:
        verdict = extension.verify(hazard_state=hazard_state, candidate=candidate)
        extension_verdicts.append(verdict)
        assumptions.extend(verdict.assumptions)
        blocked.extend(verdict.blocked_reasons)
        unverified.extend(verdict.unverified_reasons)
        if verdict.status is FeasibilityStatus.BLOCKED and not verdict.blocked_reasons:
            blocked.append("extension_blocked")
        if (
            verdict.status is FeasibilityStatus.UNVERIFIED
            and not verdict.unverified_reasons
        ):
            unverified.append("extension_unverified")

    blocked = list(dict.fromkeys(blocked))
    unverified = list(dict.fromkeys(unverified))
    if blocked:
        status = FeasibilityStatus.BLOCKED
    elif unverified:
        status = FeasibilityStatus.UNVERIFIED
    else:
        status = FeasibilityStatus.VERIFIED_FEASIBLE
    return ActionFeasibilityResult(
        candidate_id=candidate.candidate_id,
        hazard_state_id=hazard_state.state_id,
        status=status,
        blocked_reasons=tuple(blocked),
        unverified_reasons=tuple(unverified),
        assumptions=tuple(dict.fromkeys(assumptions)),
        extension_verdicts=tuple(extension_verdicts),
        policy_sha256=hazard_state.policy_binding.policy_sha256,
        evaluated_at=evaluated_at,
    )


@dataclass(frozen=True)
class RevalidationArtifact:
    """Result of rechecking evidence, not permission to dispatch."""

    proposal_ref: str
    original_result_sha256: str
    current_result_sha256: str
    status: FeasibilityStatus
    reasons: tuple[str, ...]
    approval_created: bool = False
    dispatch_authority_created: bool = False
    execution_invoked: bool = False
    completion_claimed: bool = False
    schema_version: str = REVALIDATION_SCHEMA_VERSION

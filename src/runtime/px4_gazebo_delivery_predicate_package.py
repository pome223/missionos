"""Concrete PX4/Gazebo simulator-delivery predicate package.

This is the first Mission Contract predicate package. It deliberately remains
backend-specific and does not establish a general predicate language.

The package deterministically evaluates the content of one content-bound
PX4/Gazebo delivery result. The evidence origin remains a separate fact:
deterministic evaluation of a stored artifact does not make that artifact a
machine observation, physical execution, or real-world delivery evidence.

Predicate satisfaction updates verifier state only. It never creates approval,
dispatch authority, an executor effect, operational closure, or a next action.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Literal

from missionos_core import (
    EvidenceOrigin,
    FrozenMissionContract,
    HardwareExecutionMode,
    MissionEvidenceReadiness,
    MissionObservation,
    MissionRuntimeEvidence,
    ObservationRequirement,
    OutcomeClaimSpec,
    PredicatePackageBinding,
    QuantificationScope,
    QuantificationScopeKind,
    ReferenceInput,
    TerminationPolicy,
    TerminationReason,
    VerificationBasis,
    canonical_sha256,
    check_mission_evidence_readiness,
)

from .px4_gazebo_sitl_e2e_delivery_smoke import (
    PX4_GAZEBO_SITL_E2E_DELIVERY_EPIC_EXIT_RESULT_SCHEMA_VERSION,
    PX4GazeboSITLE2EDeliveryEpicExitResult,
    PX4GazeboSITLE2EDeliverySmokeResult,
)


PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SCHEMA_VERSION = (
    "missionos_px4_gazebo_delivery_predicate_package.v1"
)
PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_ID = (
    "px4_gazebo_sim_delivery_contract_satisfied"
)
PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_VERSION = "1"
PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_ID = (
    "px4_gazebo_sim_delivery_contract_satisfied"
)
PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_SCOPE = "px4_gazebo_simulator_delivery"
PX4_GAZEBO_DELIVERY_OBSERVATION_REQUIREMENT_ID = "px4_gazebo_delivery_result"
PX4_GAZEBO_DELIVERY_EVIDENCE_KIND = (
    "px4_gazebo_sitl_e2e_delivery_result"
)
PX4_GAZEBO_DELIVERY_EXTERNAL_DISPATCH_SCOPE = (
    "same_session_sitl_mission_upload_and_detachable_joint_release"
)
PX4_GAZEBO_DELIVERY_RELEASE_SOURCE = (
    "gazebo_detachable_joint_detach_event"
)

_PACKAGE_MATERIAL = {
    "schema_version": PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SCHEMA_VERSION,
    "package_id": PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_ID,
    "package_version": PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_VERSION,
    "outcome_claim_id": PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_ID,
    "outcome_claim_scope": PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_SCOPE,
    "required_checks": [
        "same_session_mission_upload_and_ack",
        "mission_request_sequences_0_through_3",
        "takeoff_observed",
        "approved_drop_zone_reached",
        "detachable_joint_release_observed",
        "release_and_dropoff_evidence_refs_bound",
        "landing_observed",
        "no_blocking_reasons",
        "simulator_scope_only",
    ],
    "requested_runtime_effect": "none",
}
PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SHA256 = canonical_sha256(
    _PACKAGE_MATERIAL
)


class PX4GazeboDeliveryPredicateStatus(str, Enum):
    """Result of evaluating this one concrete package."""

    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    UNVERIFIED = "unverified"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class PX4GazeboDeliveryEvidenceBindings:
    """Content digests for the source artifacts cited by the typed result."""

    payload_release_event_sha256: str
    dropoff_verification_sha256: str
    sitl_telemetry_log_sha256: str
    gazebo_pose_trace_sha256: str
    mission_artifacts_sha256: str

    def to_material(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PX4GazeboDeliveryPredicateContent:
    """Typed content addressed by one MissionObservation digest."""

    source_schema_version: str
    source_result_id: str
    source_result_status: str
    executed_in_same_sitl_session: bool
    mission_upload_observed: bool
    mission_ack_observed: bool
    mission_ack_type: int | None
    mission_request_sequences: tuple[int, ...]
    actual_takeoff_observed: bool
    actual_dropoff_region_reached: bool
    actual_land_observed: bool
    payload_release_observed: bool
    payload_release_verified: bool
    payload_release_event_ref: str
    dropoff_verification_ref: str
    payload_release_event_source: str
    gazebo_detachable_joint_release_observed: bool
    external_dispatch_scope: str
    blocked_reasons: tuple[str, ...]
    physical_execution_invoked: bool
    observed_at: str
    artifact_manifest_sha256: str
    artifact_payload_release_event_ref: str
    artifact_dropoff_verification_ref: str
    evidence_bindings: PX4GazeboDeliveryEvidenceBindings

    def to_material(self) -> dict[str, Any]:
        material = asdict(self)
        material["evidence_bindings"] = self.evidence_bindings.to_material()
        return material

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.to_material())

    @classmethod
    def from_epic_exit_result(
        cls,
        result: PX4GazeboSITLE2EDeliveryEpicExitResult,
        *,
        evidence_bindings: PX4GazeboDeliveryEvidenceBindings,
    ) -> PX4GazeboDeliveryPredicateContent:
        return cls(
            source_schema_version=result.schema_version,
            source_result_id=result.result_id,
            source_result_status=result.result_status,
            executed_in_same_sitl_session=result.executed_in_same_sitl_session,
            mission_upload_observed=result.mission_upload_observed,
            mission_ack_observed=result.mission_ack_observed,
            mission_ack_type=result.mission_ack_type,
            mission_request_sequences=result.mission_request_sequences,
            actual_takeoff_observed=result.actual_takeoff_observed,
            actual_dropoff_region_reached=result.actual_dropoff_region_reached,
            actual_land_observed=result.actual_land_observed,
            payload_release_observed=result.payload_release_observed,
            payload_release_verified=result.payload_release_verified,
            payload_release_event_ref=result.payload_release_event_ref,
            dropoff_verification_ref=result.dropoff_verification_ref,
            payload_release_event_source=result.payload_release_event_source,
            gazebo_detachable_joint_release_observed=(
                result.gazebo_detachable_joint_release_observed
            ),
            external_dispatch_scope=result.external_dispatch_scope,
            blocked_reasons=result.blocked_reasons,
            physical_execution_invoked=result.physical_execution_invoked,
            observed_at=result.observed_at.isoformat(),
            artifact_manifest_sha256=canonical_sha256(
                result.artifact_manifest
            ),
            artifact_payload_release_event_ref=str(
                result.artifact_manifest.get("payload_release_event_ref") or ""
            ),
            artifact_dropoff_verification_ref=str(
                result.artifact_manifest.get("dropoff_verification_ref") or ""
            ),
            evidence_bindings=evidence_bindings,
        )

    @classmethod
    def from_flight_only_result(
        cls,
        result: PX4GazeboSITLE2EDeliverySmokeResult,
        *,
        evidence_bindings: PX4GazeboDeliveryEvidenceBindings,
    ) -> PX4GazeboDeliveryPredicateContent:
        return cls(
            source_schema_version=result.schema_version,
            source_result_id=result.result_id,
            source_result_status=result.result_status,
            executed_in_same_sitl_session=result.executed_in_same_sitl_session,
            mission_upload_observed=result.mission_upload_observed,
            mission_ack_observed=result.mission_ack_observed,
            mission_ack_type=result.mission_ack_type,
            mission_request_sequences=result.mission_request_sequences,
            actual_takeoff_observed=result.actual_takeoff_observed,
            actual_dropoff_region_reached=result.actual_dropoff_region_reached,
            actual_land_observed=result.actual_land_observed,
            payload_release_observed=result.payload_release_observed,
            payload_release_verified=result.payload_release_verified,
            payload_release_event_ref="",
            dropoff_verification_ref="",
            payload_release_event_source="",
            gazebo_detachable_joint_release_observed=False,
            external_dispatch_scope=result.external_dispatch_scope,
            blocked_reasons=result.blocked_reasons,
            physical_execution_invoked=result.physical_execution_invoked,
            observed_at=result.observed_at.isoformat(),
            artifact_manifest_sha256=canonical_sha256(
                result.artifact_manifest
            ),
            artifact_payload_release_event_ref=str(
                result.artifact_manifest.get("payload_release_event_ref") or ""
            ),
            artifact_dropoff_verification_ref=str(
                result.artifact_manifest.get("dropoff_verification_ref") or ""
            ),
            evidence_bindings=evidence_bindings,
        )


@dataclass(frozen=True)
class PX4GazeboDeliveryReplayInput:
    """Content and metadata kept separate but joined by the content digest."""

    content: PX4GazeboDeliveryPredicateContent
    evidence: MissionRuntimeEvidence


@dataclass(frozen=True)
class PX4GazeboDeliveryPredicateEvaluation:
    """Package-specific evaluation record; not yet a shared Core result type."""

    contract_id: str
    contract_sha256: str
    predicate_package_id: str
    predicate_package_version: str
    predicate_package_sha256: str
    outcome_claim_id: str
    outcome_claim_scope: str
    evaluated_at: str
    observation_content_sha256: str
    evidence_readiness: MissionEvidenceReadiness
    status: PX4GazeboDeliveryPredicateStatus
    evaluated_outcome_claim: bool
    actual_verification_basis: VerificationBasis
    evidence_origins: tuple[EvidenceOrigin, ...]
    evidence_refs: tuple[str, ...]
    reasons: tuple[str, ...]
    predicate_package_evaluated: bool
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    runtime_effect_requested: Literal[False] = False
    operational_closure_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_sha256": self.contract_sha256,
            "predicate_package_id": self.predicate_package_id,
            "predicate_package_version": self.predicate_package_version,
            "predicate_package_sha256": self.predicate_package_sha256,
            "outcome_claim_id": self.outcome_claim_id,
            "outcome_claim_scope": self.outcome_claim_scope,
            "evaluated_at": self.evaluated_at,
            "observation_content_sha256": self.observation_content_sha256,
            "evidence_readiness": self.evidence_readiness.value,
            "status": self.status.value,
            "evaluated_outcome_claim": self.evaluated_outcome_claim,
            "actual_verification_basis": self.actual_verification_basis.value,
            "evidence_origins": [
                origin.value for origin in self.evidence_origins
            ],
            "evidence_refs": list(self.evidence_refs),
            "reasons": list(self.reasons),
            "predicate_package_evaluated": self.predicate_package_evaluated,
            "approval_created": self.approval_created,
            "dispatch_authority_created": self.dispatch_authority_created,
            "runtime_effect_requested": self.runtime_effect_requested,
            "operational_closure_created": self.operational_closure_created,
            "physical_execution_invoked": self.physical_execution_invoked,
        }


def px4_gazebo_delivery_predicate_package_binding() -> PredicatePackageBinding:
    """Return the exact package identity to freeze in a Mission Contract."""

    return PredicatePackageBinding(
        package_id=PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_ID,
        package_version=PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_VERSION,
        content_sha256=PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SHA256,
    )


def build_px4_gazebo_delivery_replay_contract(
    *,
    contract_id: str,
    contract_version: str,
    approved_drop_zone: dict[str, Any],
    approved_payload_release_rule: dict[str, Any],
    approved_same_session_rule: dict[str, Any],
    maximum_observation_age_seconds: float,
) -> FrozenMissionContract:
    """Build the narrow simulator contract evaluated by this package."""

    return FrozenMissionContract(
        contract_id=contract_id,
        contract_version=contract_version,
        execution_scope=HardwareExecutionMode.SIM,
        reference_inputs=(
            ReferenceInput(
                input_id="approved_drop_zone",
                kind="approved_spatial_region",
                content_sha256=canonical_sha256(approved_drop_zone),
            ),
            ReferenceInput(
                input_id="approved_payload_release_rule",
                kind="approved_event_ordering_rule",
                content_sha256=canonical_sha256(approved_payload_release_rule),
            ),
            ReferenceInput(
                input_id="approved_simulator_session_rule",
                kind="approved_same_session_requirement",
                content_sha256=canonical_sha256(approved_same_session_rule),
            ),
        ),
        observation_requirements=(
            ObservationRequirement(
                requirement_id=PX4_GAZEBO_DELIVERY_OBSERVATION_REQUIREMENT_ID,
                evidence_kind=PX4_GAZEBO_DELIVERY_EVIDENCE_KIND,
                required_origin=EvidenceOrigin.STORED_ARTIFACT,
                maximum_age_seconds=maximum_observation_age_seconds,
            ),
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.SPATIAL_REGION,
            scope_ref="approved_drop_zone",
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id=PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_ID,
            statement=(
                "The frozen PX4/Gazebo simulator delivery contract was "
                "satisfied."
            ),
            claim_scope=PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_SCOPE,
        ),
        predicate_package=px4_gazebo_delivery_predicate_package_binding(),
        termination_policy=TerminationPolicy(
            allowed_reasons=(
                TerminationReason.EXPIRY,
                TerminationReason.OPERATOR_INTERRUPTION,
                TerminationReason.SAFE_STOP,
                TerminationReason.TERMINAL_PREDICATE_SATISFIED,
            ),
        ),
        required_verification_basis=VerificationBasis.DETERMINISTIC,
    )


def build_px4_gazebo_delivery_replay_input(
    *,
    contract: FrozenMissionContract,
    content: PX4GazeboDeliveryPredicateContent,
) -> PX4GazeboDeliveryReplayInput:
    """Bind package-specific content to generic MissionRuntimeEvidence."""

    observation = MissionObservation(
        observation_id=(
            f"px4-gazebo-delivery-result:{content.source_result_id}"
        ),
        requirement_id=PX4_GAZEBO_DELIVERY_OBSERVATION_REQUIREMENT_ID,
        evidence_kind=PX4_GAZEBO_DELIVERY_EVIDENCE_KIND,
        origin=EvidenceOrigin.STORED_ARTIFACT,
        observed_at=content.observed_at,
        content_sha256=content.content_sha256,
        execution_scope=HardwareExecutionMode.SIM,
    )
    return PX4GazeboDeliveryReplayInput(
        content=content,
        evidence=MissionRuntimeEvidence(
            contract_sha256=contract.contract_sha256,
            observations=(observation,),
        ),
    )


def evaluate_px4_gazebo_delivery_predicate(
    *,
    contract: FrozenMissionContract,
    replay: PX4GazeboDeliveryReplayInput,
    evaluated_at: str,
) -> PX4GazeboDeliveryPredicateEvaluation:
    """Evaluate the frozen PX4 package without creating runtime authority."""

    readiness = check_mission_evidence_readiness(
        contract=contract,
        evidence=replay.evidence,
        evaluated_at=evaluated_at,
    )
    observation = (
        replay.evidence.observations[0]
        if len(replay.evidence.observations) == 1
        else None
    )
    package_matches = (
        contract.predicate_package
        == px4_gazebo_delivery_predicate_package_binding()
        and contract.outcome_claim_spec.claim_id
        == PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_ID
        and contract.outcome_claim_spec.claim_scope
        == PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_SCOPE
    )
    content_matches = (
        observation is not None
        and observation.content_sha256 == replay.content.content_sha256
        and observation.observed_at == replay.content.observed_at
    )

    boundary_reasons: list[str] = []
    if not package_matches:
        boundary_reasons.append("predicate_package_binding_mismatch")
    if not content_matches:
        boundary_reasons.append("predicate_observation_content_binding_mismatch")

    if (
        readiness.evidence_readiness is not MissionEvidenceReadiness.READY
        or boundary_reasons
    ):
        status = (
            PX4GazeboDeliveryPredicateStatus.BLOCKED
            if readiness.evidence_readiness is MissionEvidenceReadiness.REFUSED
            or boundary_reasons
            else PX4GazeboDeliveryPredicateStatus.UNVERIFIED
        )
        readiness_reasons = tuple(
            reason
            for reason in readiness.reasons
            if reason != "outcome_claim_requires_approved_predicate_package"
        )
        return _evaluation(
            contract=contract,
            replay=replay,
            readiness=readiness.evidence_readiness,
            status=status,
            evaluated_outcome_claim=False,
            actual_verification_basis=VerificationBasis.UNVERIFIED,
            reasons=readiness_reasons + tuple(boundary_reasons),
            predicate_package_evaluated=False,
            evaluated_at=evaluated_at,
        )

    predicate_reasons = _predicate_reasons(replay.content)
    satisfied = not predicate_reasons
    return _evaluation(
        contract=contract,
        replay=replay,
        readiness=MissionEvidenceReadiness.READY,
        status=(
            PX4GazeboDeliveryPredicateStatus.SATISFIED
            if satisfied
            else PX4GazeboDeliveryPredicateStatus.NOT_SATISFIED
        ),
        evaluated_outcome_claim=satisfied,
        actual_verification_basis=VerificationBasis.DETERMINISTIC,
        reasons=predicate_reasons,
        predicate_package_evaluated=True,
        evaluated_at=evaluated_at,
    )


def _predicate_reasons(
    content: PX4GazeboDeliveryPredicateContent,
) -> tuple[str, ...]:
    bindings = content.evidence_bindings
    checks = (
        (
            "source_result_schema_mismatch",
            content.source_schema_version
            == PX4_GAZEBO_SITL_E2E_DELIVERY_EPIC_EXIT_RESULT_SCHEMA_VERSION,
        ),
        (
            "source_result_id_missing",
            bool(str(content.source_result_id or "").strip()),
        ),
        (
            "same_session_execution_not_observed",
            content.executed_in_same_sitl_session,
        ),
        ("mission_upload_not_observed", content.mission_upload_observed),
        ("mission_ack_not_observed", content.mission_ack_observed),
        ("mission_ack_type_not_accepted", content.mission_ack_type == 0),
        (
            "mission_request_sequences_incomplete",
            content.mission_request_sequences == (0, 1, 2, 3),
        ),
        ("takeoff_not_observed", content.actual_takeoff_observed),
        (
            "approved_drop_zone_not_reached",
            content.actual_dropoff_region_reached,
        ),
        ("landing_not_observed", content.actual_land_observed),
        ("payload_release_not_observed", content.payload_release_observed),
        ("payload_release_not_verified", content.payload_release_verified),
        (
            "payload_release_source_mismatch",
            content.payload_release_event_source
            == PX4_GAZEBO_DELIVERY_RELEASE_SOURCE,
        ),
        (
            "detachable_joint_release_not_observed",
            content.gazebo_detachable_joint_release_observed,
        ),
        (
            "payload_release_event_ref_invalid",
            str(content.payload_release_event_ref or "").startswith(
                "px4_gazebo_sitl_payload_release_event:"
            ),
        ),
        (
            "dropoff_verification_ref_invalid",
            str(content.dropoff_verification_ref or "").startswith(
                "px4_gazebo_sitl_dropoff_verification:"
            ),
        ),
        (
            "artifact_payload_release_event_ref_mismatch",
            content.artifact_payload_release_event_ref
            == content.payload_release_event_ref,
        ),
        (
            "artifact_dropoff_verification_ref_mismatch",
            content.artifact_dropoff_verification_ref
            == content.dropoff_verification_ref,
        ),
        (
            "external_dispatch_scope_mismatch",
            content.external_dispatch_scope
            == PX4_GAZEBO_DELIVERY_EXTERNAL_DISPATCH_SCOPE,
        ),
        ("blocked_reasons_present", not content.blocked_reasons),
        (
            "physical_execution_claim_forbidden",
            content.physical_execution_invoked is False,
        ),
        (
            "artifact_manifest_digest_invalid",
            _sha256_is_valid(content.artifact_manifest_sha256),
        ),
        (
            "payload_release_event_content_digest_invalid",
            _sha256_is_valid(bindings.payload_release_event_sha256),
        ),
        (
            "dropoff_verification_content_digest_invalid",
            _sha256_is_valid(bindings.dropoff_verification_sha256),
        ),
        (
            "sitl_telemetry_log_digest_invalid",
            _sha256_is_valid(bindings.sitl_telemetry_log_sha256),
        ),
        (
            "gazebo_pose_trace_digest_invalid",
            _sha256_is_valid(bindings.gazebo_pose_trace_sha256),
        ),
        (
            "mission_artifacts_digest_invalid",
            _sha256_is_valid(bindings.mission_artifacts_sha256),
        ),
    )
    return tuple(reason for reason, passed in checks if not passed)


def _evaluation(
    *,
    contract: FrozenMissionContract,
    replay: PX4GazeboDeliveryReplayInput,
    readiness: MissionEvidenceReadiness,
    status: PX4GazeboDeliveryPredicateStatus,
    evaluated_outcome_claim: bool,
    actual_verification_basis: VerificationBasis,
    reasons: tuple[str, ...],
    predicate_package_evaluated: bool,
    evaluated_at: str,
) -> PX4GazeboDeliveryPredicateEvaluation:
    evidence_refs = [
        f"mission-observation:{observation.observation_id}"
        for observation in replay.evidence.observations
    ]
    for value in (
        replay.content.payload_release_event_ref,
        replay.content.dropoff_verification_ref,
    ):
        if value:
            evidence_refs.append(value)
    return PX4GazeboDeliveryPredicateEvaluation(
        contract_id=contract.contract_id,
        contract_sha256=contract.contract_sha256,
        predicate_package_id=PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_ID,
        predicate_package_version=PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_VERSION,
        predicate_package_sha256=(
            PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SHA256
        ),
        outcome_claim_id=PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_ID,
        outcome_claim_scope=PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_SCOPE,
        evaluated_at=evaluated_at,
        observation_content_sha256=replay.content.content_sha256,
        evidence_readiness=readiness,
        status=status,
        evaluated_outcome_claim=evaluated_outcome_claim,
        actual_verification_basis=actual_verification_basis,
        evidence_origins=tuple(
            observation.origin for observation in replay.evidence.observations
        ),
        evidence_refs=tuple(evidence_refs),
        reasons=tuple(dict.fromkeys(reasons)),
        predicate_package_evaluated=predicate_package_evaluated,
    )


def _sha256_is_valid(value: str) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


__all__ = [
    "PX4_GAZEBO_DELIVERY_EVIDENCE_KIND",
    "PX4_GAZEBO_DELIVERY_OBSERVATION_REQUIREMENT_ID",
    "PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_ID",
    "PX4_GAZEBO_DELIVERY_OUTCOME_CLAIM_SCOPE",
    "PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_ID",
    "PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SHA256",
    "PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_VERSION",
    "PX4GazeboDeliveryPredicateContent",
    "PX4GazeboDeliveryEvidenceBindings",
    "PX4GazeboDeliveryPredicateEvaluation",
    "PX4GazeboDeliveryPredicateStatus",
    "PX4GazeboDeliveryReplayInput",
    "build_px4_gazebo_delivery_replay_contract",
    "build_px4_gazebo_delivery_replay_input",
    "evaluate_px4_gazebo_delivery_predicate",
    "px4_gazebo_delivery_predicate_package_binding",
]

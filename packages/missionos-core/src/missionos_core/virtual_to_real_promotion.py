"""Backend-neutral Virtual-to-Real promotion receipt contracts.

A verified receipt is only a prerequisite for evaluating a bounded physical
deployment candidate. It never creates approval, dispatch authority, runtime
effects, completion, physical execution, or a physical-safety claim.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
from typing import Any, Literal

from .action_feasibility import (
    EvidenceOrigin,
    EvidenceSourceRef,
    PolicyBinding,
    VerificationBasis,
    canonical_sha256,
)
from .execution_scope import HardwareExecutionMode, parse_hardware_execution_mode
from .mission_contract import UTC_WALL_CLOCK_DOMAIN_REF


V2R_PROMOTION_POLICY_SCHEMA_VERSION = "missionos_core_v2r_promotion_policy.v1"
V2R_PROMOTION_RECEIPT_SCHEMA_VERSION = "missionos_core_v2r_promotion_receipt.v1"

PHYSICAL_TARGET_SCOPES = frozenset(
    {
        HardwareExecutionMode.BENCH,
        HardwareExecutionMode.CAGE,
        HardwareExecutionMode.FIELD,
    }
)


def _timestamp(value: object) -> datetime | None:
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


def _sha256_is_valid(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


class PromotionComponentKind(str, Enum):
    MODEL = "model"
    POLICY = "policy"
    SIMULATOR = "simulator"
    ENVIRONMENT = "environment"
    SCENE = "scene"
    DATASET = "dataset"
    ROBOT_DESCRIPTION = "robot_description"
    EMBODIMENT = "embodiment"
    CONTROLLER_PROFILE = "controller_profile"
    EXECUTOR_PROFILE = "executor_profile"


class PromotionGapStatus(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    UNKNOWN = "unknown"


class PromotionReceiptValidationStatus(str, Enum):
    VERIFIED_PREREQUISITE = "verified_prerequisite"
    UNVERIFIED = "unverified"
    BLOCKED = "blocked"


class PromotionEvidenceScope(str, Enum):
    """Which side of the promotion must contain a gap-resolution source."""

    SOURCE = "source"
    TARGET = "target"


@dataclass(frozen=True)
class PromotionComponentBinding:
    kind: PromotionComponentKind
    component_id: str
    component_version: str
    content_sha256: str

    def to_material(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value
            if isinstance(self.kind, PromotionComponentKind)
            else str(self.kind),
            "component_id": self.component_id,
            "component_version": self.component_version,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class PromotionSourceEvidenceBinding:
    evidence_ref: str
    evidence_kind: str
    content_sha256: str
    execution_scope: HardwareExecutionMode
    origin: EvidenceOrigin

    def to_material(self) -> dict[str, Any]:
        return {
            "evidence_ref": self.evidence_ref,
            "evidence_kind": self.evidence_kind,
            "content_sha256": self.content_sha256,
            "execution_scope": (
                self.execution_scope.value
                if isinstance(self.execution_scope, HardwareExecutionMode)
                else str(self.execution_scope)
            ),
            "origin": self.origin.value
            if isinstance(self.origin, EvidenceOrigin)
            else str(self.origin),
        }


@dataclass(frozen=True)
class PromotionGapRequirement:
    """Policy-owned evidence requirement for one known sim-to-real gap."""

    gap_id: str
    required_evidence_kind: str
    required_origin: EvidenceOrigin
    evidence_scope: PromotionEvidenceScope

    def to_material(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "required_evidence_kind": self.required_evidence_kind,
            "required_origin": (
                self.required_origin.value
                if isinstance(self.required_origin, EvidenceOrigin)
                else str(self.required_origin)
            ),
            "evidence_scope": (
                self.evidence_scope.value
                if isinstance(self.evidence_scope, PromotionEvidenceScope)
                else str(self.evidence_scope)
            ),
        }


@dataclass(frozen=True)
class PromotionGap:
    gap_id: str
    status: PromotionGapStatus
    resolution_evidence_ref: str | None = None
    resolution_evidence_sha256: str | None = None
    resolution_evidence_kind: str | None = None
    resolution_evidence_origin: EvidenceOrigin | None = None
    resolution_execution_scope: HardwareExecutionMode | None = None

    def to_material(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "status": self.status.value
            if isinstance(self.status, PromotionGapStatus)
            else str(self.status),
            "resolution_evidence_ref": self.resolution_evidence_ref,
            "resolution_evidence_sha256": self.resolution_evidence_sha256,
            "resolution_evidence_kind": self.resolution_evidence_kind,
            "resolution_evidence_origin": (
                self.resolution_evidence_origin.value
                if isinstance(self.resolution_evidence_origin, EvidenceOrigin)
                else (
                    str(self.resolution_evidence_origin)
                    if self.resolution_evidence_origin is not None
                    else None
                )
            ),
            "resolution_execution_scope": (
                self.resolution_execution_scope.value
                if isinstance(self.resolution_execution_scope, HardwareExecutionMode)
                else (
                    str(self.resolution_execution_scope)
                    if self.resolution_execution_scope is not None
                    else None
                )
            ),
        }


@dataclass(frozen=True)
class VirtualToRealPromotionPolicy:
    policy_id: str
    policy_version: str
    source_scope: HardwareExecutionMode
    allowed_target_scopes: tuple[HardwareExecutionMode, ...]
    required_source_verification_basis: VerificationBasis
    approval_clock_domain_ref: str
    required_component_kinds: tuple[PromotionComponentKind, ...]
    required_gaps: tuple[PromotionGapRequirement, ...]
    required_target_evidence_kinds: tuple[str, ...]
    required_rollback_condition_ids: tuple[str, ...]
    required_disable_condition_ids: tuple[str, ...]
    maximum_age_seconds: float
    schema_version: str = V2R_PROMOTION_POLICY_SCHEMA_VERSION

    def to_material(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "source_scope": (
                self.source_scope.value
                if isinstance(self.source_scope, HardwareExecutionMode)
                else str(self.source_scope)
            ),
            "allowed_target_scopes": [
                scope.value if isinstance(scope, HardwareExecutionMode) else str(scope)
                for scope in self.allowed_target_scopes
            ],
            "required_source_verification_basis": (
                self.required_source_verification_basis.value
                if isinstance(
                    self.required_source_verification_basis,
                    VerificationBasis,
                )
                else str(self.required_source_verification_basis)
            ),
            "approval_clock_domain_ref": self.approval_clock_domain_ref,
            "required_component_kinds": [
                kind.value if isinstance(kind, PromotionComponentKind) else str(kind)
                for kind in self.required_component_kinds
            ],
            "required_gaps": [item.to_material() for item in self.required_gaps],
            "required_target_evidence_kinds": list(self.required_target_evidence_kinds),
            "required_rollback_condition_ids": list(self.required_rollback_condition_ids),
            "required_disable_condition_ids": list(self.required_disable_condition_ids),
            "maximum_age_seconds": self.maximum_age_seconds,
        }

    @property
    def binding(self) -> PolicyBinding:
        return PolicyBinding(
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            policy_sha256=canonical_sha256(self.to_material()),
        )


@dataclass(frozen=True)
class VirtualToRealPromotionReceipt:
    receipt_id: str
    source_scope: HardwareExecutionMode
    target_scope: HardwareExecutionMode
    source_contract_sha256: str
    source_predicate_package_id: str
    source_predicate_package_version: str
    source_predicate_package_sha256: str
    source_outcome_claim_scope: str
    source_outcome_evidence_ref: str
    source_outcome_claim_satisfied: Literal[True]
    source_verification_basis: VerificationBasis
    source_evidence_bindings: tuple[PromotionSourceEvidenceBinding, ...]
    component_bindings: tuple[PromotionComponentBinding, ...]
    target_executor_profile_sha256: str
    target_controller_profile_sha256: str
    gaps: tuple[PromotionGap, ...]
    target_required_evidence_kinds: tuple[str, ...]
    approved_by: str
    approval_artifact_ref: str
    approval_artifact_sha256: str
    approved_at: str
    expires_at: str
    approval_clock_domain_ref: str
    policy_binding: PolicyBinding
    rollback_condition_ids: tuple[str, ...]
    disable_condition_ids: tuple[str, ...]
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    runtime_effect_requested: Literal[False] = False
    task_completion_claimed: Literal[False] = False
    physical_safety_claimed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    schema_version: str = V2R_PROMOTION_RECEIPT_SCHEMA_VERSION

    def approval_material(self) -> dict[str, Any]:
        """Return the exact proposal material a human approval must bind."""

        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "source_scope": (
                self.source_scope.value
                if isinstance(self.source_scope, HardwareExecutionMode)
                else str(self.source_scope)
            ),
            "target_scope": (
                self.target_scope.value
                if isinstance(self.target_scope, HardwareExecutionMode)
                else str(self.target_scope)
            ),
            "source_contract_sha256": self.source_contract_sha256,
            "source_predicate_package_id": self.source_predicate_package_id,
            "source_predicate_package_version": (self.source_predicate_package_version),
            "source_predicate_package_sha256": (self.source_predicate_package_sha256),
            "source_outcome_claim_scope": self.source_outcome_claim_scope,
            "source_outcome_evidence_ref": self.source_outcome_evidence_ref,
            "source_outcome_claim_satisfied": (self.source_outcome_claim_satisfied),
            "source_verification_basis": (
                self.source_verification_basis.value
                if isinstance(self.source_verification_basis, VerificationBasis)
                else str(self.source_verification_basis)
            ),
            "source_evidence_bindings": [
                item.to_material() for item in self.source_evidence_bindings
            ],
            "component_bindings": [item.to_material() for item in self.component_bindings],
            "target_executor_profile_sha256": (self.target_executor_profile_sha256),
            "target_controller_profile_sha256": (self.target_controller_profile_sha256),
            "gaps": [item.to_material() for item in self.gaps],
            "target_required_evidence_kinds": list(self.target_required_evidence_kinds),
            "policy_binding": asdict(self.policy_binding),
            "rollback_condition_ids": list(self.rollback_condition_ids),
            "disable_condition_ids": list(self.disable_condition_ids),
        }

    def to_material(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "source_scope": (
                self.source_scope.value
                if isinstance(self.source_scope, HardwareExecutionMode)
                else str(self.source_scope)
            ),
            "target_scope": (
                self.target_scope.value
                if isinstance(self.target_scope, HardwareExecutionMode)
                else str(self.target_scope)
            ),
            "source_contract_sha256": self.source_contract_sha256,
            "source_predicate_package_id": self.source_predicate_package_id,
            "source_predicate_package_version": self.source_predicate_package_version,
            "source_predicate_package_sha256": self.source_predicate_package_sha256,
            "source_outcome_claim_scope": self.source_outcome_claim_scope,
            "source_outcome_evidence_ref": self.source_outcome_evidence_ref,
            "source_outcome_claim_satisfied": self.source_outcome_claim_satisfied,
            "source_verification_basis": (
                self.source_verification_basis.value
                if isinstance(self.source_verification_basis, VerificationBasis)
                else str(self.source_verification_basis)
            ),
            "source_evidence_bindings": [
                item.to_material() for item in self.source_evidence_bindings
            ],
            "component_bindings": [item.to_material() for item in self.component_bindings],
            "target_executor_profile_sha256": self.target_executor_profile_sha256,
            "target_controller_profile_sha256": self.target_controller_profile_sha256,
            "gaps": [item.to_material() for item in self.gaps],
            "target_required_evidence_kinds": list(self.target_required_evidence_kinds),
            "approved_by": self.approved_by,
            "approval_artifact_ref": self.approval_artifact_ref,
            "approval_artifact_sha256": self.approval_artifact_sha256,
            "approved_at": self.approved_at,
            "expires_at": self.expires_at,
            "approval_clock_domain_ref": self.approval_clock_domain_ref,
            "policy_binding": asdict(self.policy_binding),
            "rollback_condition_ids": list(self.rollback_condition_ids),
            "disable_condition_ids": list(self.disable_condition_ids),
            "approval_created": self.approval_created,
            "dispatch_authority_created": self.dispatch_authority_created,
            "runtime_effect_requested": self.runtime_effect_requested,
            "task_completion_claimed": self.task_completion_claimed,
            "physical_safety_claimed": self.physical_safety_claimed,
            "physical_execution_invoked": self.physical_execution_invoked,
        }

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.to_material())


def promotion_approval_artifact_sha256(
    receipt: VirtualToRealPromotionReceipt,
) -> str:
    """Bind human approval metadata to the exact frozen promotion material."""

    return canonical_sha256(
        {
            "approved_material_sha256": canonical_sha256(receipt.approval_material()),
            "approved_by": receipt.approved_by,
            "approval_artifact_ref": receipt.approval_artifact_ref,
            "approved_at": receipt.approved_at,
            "expires_at": receipt.expires_at,
            "approval_clock_domain_ref": receipt.approval_clock_domain_ref,
        }
    )


@dataclass(frozen=True)
class VirtualToRealPromotionValidationContext:
    expected_source_scope: HardwareExecutionMode
    expected_target_scope: HardwareExecutionMode
    observed_source_outcome_evidence_ref: str
    observed_source_outcome_claim_satisfied: bool
    observed_source_verification_basis: VerificationBasis
    expected_target_executor_profile_sha256: str
    expected_target_controller_profile_sha256: str
    active_policy: VirtualToRealPromotionPolicy
    evidence_sources: Mapping[str, EvidenceSourceRef]
    evaluated_at: str
    evaluation_clock_domain_ref: str = UTC_WALL_CLOCK_DOMAIN_REF


@dataclass(frozen=True)
class VirtualToRealPromotionValidation:
    status: PromotionReceiptValidationStatus
    verification_basis: VerificationBasis
    reasons: tuple[str, ...]
    promotion_prerequisite_satisfied: bool
    receipt_sha256: str | None
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    runtime_effect_requested: Literal[False] = False
    task_completion_claimed: Literal[False] = False
    physical_safety_claimed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _duplicates(values: tuple[str, ...]) -> bool:
    return len(set(values)) != len(values)


def _policy_shape_reasons(
    policy: VirtualToRealPromotionPolicy,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if policy.schema_version != V2R_PROMOTION_POLICY_SCHEMA_VERSION:
        reasons.append("v2r_promotion_policy_schema_not_supported")
    if not str(policy.policy_id or "").strip() or not str(policy.policy_version or "").strip():
        reasons.append("v2r_promotion_policy_identity_missing")
    if parse_hardware_execution_mode(policy.source_scope) is None:
        reasons.append("v2r_promotion_policy_source_scope_invalid")
    if (
        not isinstance(policy.required_source_verification_basis, VerificationBasis)
        or policy.required_source_verification_basis is VerificationBasis.UNVERIFIED
    ):
        reasons.append("v2r_promotion_policy_source_basis_invalid")
    if policy.approval_clock_domain_ref != UTC_WALL_CLOCK_DOMAIN_REF:
        reasons.append("v2r_promotion_policy_clock_domain_invalid")
    if not isinstance(policy.allowed_target_scopes, tuple) or not policy.allowed_target_scopes:
        reasons.append("v2r_promotion_policy_target_scope_invalid")
    elif any(
        parse_hardware_execution_mode(scope) not in PHYSICAL_TARGET_SCOPES
        for scope in policy.allowed_target_scopes
    ):
        reasons.append("v2r_promotion_policy_target_scope_invalid")
    elif len(set(policy.allowed_target_scopes)) != len(policy.allowed_target_scopes):
        reasons.append("v2r_promotion_policy_target_scope_duplicate")

    if (
        not isinstance(policy.required_component_kinds, tuple)
        or not policy.required_component_kinds
        or any(
            not isinstance(kind, PromotionComponentKind) for kind in policy.required_component_kinds
        )
        or len(set(policy.required_component_kinds)) != len(policy.required_component_kinds)
    ):
        reasons.append("v2r_promotion_policy_component_requirements_invalid")

    if not isinstance(policy.required_gaps, tuple) or not policy.required_gaps:
        reasons.append("v2r_promotion_policy_gap_requirements_invalid")
    else:
        gap_ids: list[str] = []
        for requirement in policy.required_gaps:
            if not isinstance(requirement, PromotionGapRequirement):
                reasons.append("v2r_promotion_policy_gap_requirement_invalid")
                continue
            gap_ids.append(requirement.gap_id)
            if (
                not str(requirement.gap_id or "").strip()
                or not str(requirement.required_evidence_kind or "").strip()
                or not isinstance(requirement.required_origin, EvidenceOrigin)
                or requirement.required_origin is EvidenceOrigin.UNVERIFIED
                or not isinstance(requirement.evidence_scope, PromotionEvidenceScope)
            ):
                reasons.append(f"v2r_promotion_policy_gap_requirement_invalid:{requirement.gap_id}")
        if len(gap_ids) != len(set(gap_ids)):
            reasons.append("v2r_promotion_policy_gap_requirement_duplicate")

    for values, reason in (
        (
            policy.required_target_evidence_kinds,
            "v2r_promotion_policy_target_evidence_requirements_invalid",
        ),
        (
            policy.required_rollback_condition_ids,
            "v2r_promotion_policy_rollback_conditions_invalid",
        ),
        (
            policy.required_disable_condition_ids,
            "v2r_promotion_policy_disable_conditions_invalid",
        ),
    ):
        if (
            not isinstance(values, tuple)
            or not values
            or any(not str(value or "").strip() for value in values)
            or len(values) != len(set(values))
        ):
            reasons.append(reason)
    if isinstance(policy.maximum_age_seconds, bool):
        reasons.append("v2r_promotion_policy_maximum_age_invalid")
    else:
        try:
            maximum_age_seconds = float(policy.maximum_age_seconds)
        except (TypeError, ValueError):
            maximum_age_seconds = float("nan")
        if not math.isfinite(maximum_age_seconds) or maximum_age_seconds <= 0:
            reasons.append("v2r_promotion_policy_maximum_age_invalid")
    return tuple(dict.fromkeys(reasons))


def _validate_virtual_to_real_promotion_receipt(
    receipt: VirtualToRealPromotionReceipt | None,
    *,
    context: VirtualToRealPromotionValidationContext,
) -> VirtualToRealPromotionValidation:
    """Validate a receipt without creating deployment or dispatch authority."""

    if receipt is None:
        return VirtualToRealPromotionValidation(
            status=PromotionReceiptValidationStatus.BLOCKED,
            verification_basis=VerificationBasis.UNVERIFIED,
            reasons=("v2r_promotion_receipt_missing",),
            promotion_prerequisite_satisfied=False,
            receipt_sha256=None,
        )

    blocked: list[str] = []
    unverified: list[str] = []

    policy_shape_reasons = _policy_shape_reasons(context.active_policy)
    blocked.extend(policy_shape_reasons)

    source_scope = parse_hardware_execution_mode(receipt.source_scope)
    target_scope = parse_hardware_execution_mode(receipt.target_scope)
    expected_source_scope = parse_hardware_execution_mode(context.expected_source_scope)
    expected_target_scope = parse_hardware_execution_mode(context.expected_target_scope)
    policy_source_scope = parse_hardware_execution_mode(context.active_policy.source_scope)
    policy_target_scopes = {
        scope
        for scope in (
            parse_hardware_execution_mode(item)
            for item in (
                context.active_policy.allowed_target_scopes
                if isinstance(context.active_policy.allowed_target_scopes, tuple)
                else ()
            )
        )
        if scope is not None
    }

    if receipt.schema_version != V2R_PROMOTION_RECEIPT_SCHEMA_VERSION:
        blocked.append("v2r_promotion_receipt_schema_not_supported")
    if not receipt.receipt_id:
        blocked.append("v2r_promotion_receipt_id_missing")
    if source_scope is None or source_scope is HardwareExecutionMode.SCHEMA_EXAMPLE_ONLY:
        blocked.append("v2r_promotion_source_scope_invalid")
    if target_scope not in PHYSICAL_TARGET_SCOPES:
        blocked.append("v2r_promotion_target_scope_not_physical")
    if source_scope is not None and target_scope is source_scope:
        blocked.append("v2r_promotion_scope_transfer_invalid")
    if source_scope is not expected_source_scope or source_scope is not policy_source_scope:
        blocked.append("v2r_promotion_source_scope_mismatch")
    if target_scope is not expected_target_scope or target_scope not in policy_target_scopes:
        blocked.append("v2r_promotion_target_scope_mismatch")

    if not policy_shape_reasons and receipt.policy_binding != context.active_policy.binding:
        blocked.append("v2r_promotion_policy_binding_mismatch")
    if receipt.approval_artifact_sha256 != promotion_approval_artifact_sha256(receipt):
        blocked.append("v2r_promotion_approval_material_binding_mismatch")

    for value, reason in (
        (receipt.source_contract_sha256, "v2r_promotion_source_contract_sha256_invalid"),
        (
            receipt.source_predicate_package_sha256,
            "v2r_promotion_source_predicate_package_sha256_invalid",
        ),
        (
            receipt.target_executor_profile_sha256,
            "v2r_promotion_target_executor_profile_sha256_invalid",
        ),
        (
            receipt.target_controller_profile_sha256,
            "v2r_promotion_target_controller_profile_sha256_invalid",
        ),
        (receipt.approval_artifact_sha256, "v2r_promotion_approval_artifact_sha256_invalid"),
    ):
        if not _sha256_is_valid(value):
            blocked.append(reason)
    for value, reason in (
        (receipt.source_predicate_package_id, "v2r_promotion_source_predicate_package_id_missing"),
        (
            receipt.source_predicate_package_version,
            "v2r_promotion_source_predicate_package_version_missing",
        ),
        (receipt.source_outcome_claim_scope, "v2r_promotion_source_outcome_scope_missing"),
        (
            receipt.source_outcome_evidence_ref,
            "v2r_promotion_source_outcome_evidence_ref_missing",
        ),
        (receipt.approved_by, "v2r_promotion_approver_missing"),
        (receipt.approval_artifact_ref, "v2r_promotion_approval_artifact_ref_missing"),
    ):
        if not str(value or "").strip():
            blocked.append(reason)

    if receipt.source_outcome_claim_satisfied is not True:
        blocked.append("v2r_promotion_source_outcome_not_satisfied")
    if context.observed_source_outcome_claim_satisfied is not True:
        blocked.append("v2r_promotion_observed_source_outcome_not_satisfied")
    if receipt.source_outcome_evidence_ref != context.observed_source_outcome_evidence_ref:
        blocked.append("v2r_promotion_source_outcome_evidence_ref_mismatch")
    if (
        not isinstance(receipt.source_verification_basis, VerificationBasis)
        or receipt.source_verification_basis
        is not context.active_policy.required_source_verification_basis
        or receipt.source_verification_basis is not context.observed_source_verification_basis
    ):
        blocked.append("v2r_promotion_source_verification_basis_mismatch")

    for actual, expected, reason in (
        (
            receipt.target_executor_profile_sha256,
            context.expected_target_executor_profile_sha256,
            "v2r_promotion_target_executor_profile_mismatch",
        ),
        (
            receipt.target_controller_profile_sha256,
            context.expected_target_controller_profile_sha256,
            "v2r_promotion_target_controller_profile_mismatch",
        ),
    ):
        if actual != expected:
            blocked.append(reason)
    if not _sha256_is_valid(context.expected_target_executor_profile_sha256):
        blocked.append("v2r_promotion_expected_target_executor_profile_invalid")
    if not _sha256_is_valid(context.expected_target_controller_profile_sha256):
        blocked.append("v2r_promotion_expected_target_controller_profile_invalid")

    approved_at = _timestamp(receipt.approved_at)
    expires_at = _timestamp(receipt.expires_at)
    evaluated_at = _timestamp(context.evaluated_at)
    if (
        receipt.approval_clock_domain_ref != context.active_policy.approval_clock_domain_ref
        or context.evaluation_clock_domain_ref != context.active_policy.approval_clock_domain_ref
    ):
        blocked.append("v2r_promotion_clock_domain_mismatch")
    try:
        maximum_age_seconds = float(context.active_policy.maximum_age_seconds)
    except (TypeError, ValueError):
        maximum_age_seconds = float("nan")
    if approved_at is None or expires_at is None or evaluated_at is None:
        blocked.append("v2r_promotion_time_binding_invalid")
    elif (
        isinstance(context.active_policy.maximum_age_seconds, bool)
        or not math.isfinite(maximum_age_seconds)
        or maximum_age_seconds <= 0
    ):
        blocked.append("v2r_promotion_policy_maximum_age_invalid")
    else:
        if expires_at != approved_at + timedelta(seconds=maximum_age_seconds):
            blocked.append("v2r_promotion_expiry_not_policy_derived")
        if approved_at > evaluated_at:
            blocked.append("v2r_promotion_approval_in_future")
        if evaluated_at > expires_at:
            unverified.append("v2r_promotion_receipt_expired")

    registry = dict(context.evidence_sources)
    approval_source = registry.get(receipt.approval_artifact_ref)
    if approval_source is None:
        unverified.append("v2r_promotion_approval_evidence_missing")
    else:
        if approval_source.origin is not EvidenceOrigin.AUTHORITY_ARTIFACT:
            blocked.append("v2r_promotion_approval_evidence_origin_invalid")
        if approval_source.content_sha256 != receipt.approval_artifact_sha256:
            blocked.append("v2r_promotion_approval_evidence_digest_mismatch")
        if parse_hardware_execution_mode(approval_source.execution_scope) is not target_scope:
            blocked.append("v2r_promotion_approval_evidence_scope_mismatch")

    if not receipt.source_evidence_bindings:
        unverified.append("v2r_promotion_source_evidence_missing")
    source_refs = tuple(item.evidence_ref for item in receipt.source_evidence_bindings)
    if _duplicates(source_refs):
        blocked.append("v2r_promotion_source_evidence_duplicate")
    if receipt.source_outcome_evidence_ref not in source_refs:
        blocked.append("v2r_promotion_source_outcome_evidence_not_bound")
    for binding in receipt.source_evidence_bindings:
        if (
            not isinstance(binding.origin, EvidenceOrigin)
            or binding.origin is EvidenceOrigin.UNVERIFIED
        ):
            blocked.append(f"v2r_promotion_source_evidence_origin_invalid:{binding.evidence_ref}")
        source = registry.get(binding.evidence_ref)
        if source is None:
            unverified.append(f"v2r_promotion_source_evidence_unavailable:{binding.evidence_ref}")
            continue
        if source.evidence_kind != binding.evidence_kind:
            blocked.append(f"v2r_promotion_source_evidence_kind_mismatch:{binding.evidence_ref}")
        if source.origin is not binding.origin:
            blocked.append(f"v2r_promotion_source_evidence_origin_mismatch:{binding.evidence_ref}")
        if source.content_sha256 != binding.content_sha256 or not _sha256_is_valid(
            binding.content_sha256
        ):
            blocked.append(f"v2r_promotion_source_evidence_digest_mismatch:{binding.evidence_ref}")
        if (
            parse_hardware_execution_mode(binding.execution_scope) is not source_scope
            or parse_hardware_execution_mode(source.execution_scope) is not source_scope
        ):
            blocked.append(f"v2r_promotion_source_evidence_scope_mismatch:{binding.evidence_ref}")

    component_kinds = tuple(
        item.kind.value if isinstance(item.kind, PromotionComponentKind) else str(item.kind)
        for item in receipt.component_bindings
    )
    if _duplicates(component_kinds):
        blocked.append("v2r_promotion_component_kind_duplicate")
    required_component_kinds = {
        kind.value if isinstance(kind, PromotionComponentKind) else str(kind)
        for kind in context.active_policy.required_component_kinds
    }
    if not required_component_kinds.issubset(set(component_kinds)):
        unverified.append("v2r_promotion_required_component_missing")
    for component in receipt.component_bindings:
        if not isinstance(component.kind, PromotionComponentKind):
            blocked.append("v2r_promotion_component_kind_invalid")
        if not component.component_id or not component.component_version:
            blocked.append(f"v2r_promotion_component_identity_missing:{component.component_id}")
        if not _sha256_is_valid(component.content_sha256):
            blocked.append(f"v2r_promotion_component_digest_invalid:{component.kind}")

    gap_ids = tuple(item.gap_id for item in receipt.gaps)
    if _duplicates(gap_ids):
        blocked.append("v2r_promotion_gap_id_duplicate")
    gap_requirements = {
        requirement.gap_id: requirement
        for requirement in (
            context.active_policy.required_gaps
            if isinstance(context.active_policy.required_gaps, tuple)
            else ()
        )
        if isinstance(requirement, PromotionGapRequirement)
    }
    if not set(gap_requirements).issubset(set(gap_ids)):
        unverified.append("v2r_promotion_required_gap_missing")
    for gap in receipt.gaps:
        if not gap.gap_id or not isinstance(gap.status, PromotionGapStatus):
            blocked.append("v2r_promotion_gap_invalid")
            continue
        if gap.status is not PromotionGapStatus.RESOLVED:
            unverified.append(f"v2r_promotion_gap_{gap.status.value}:{gap.gap_id}")
            continue
        if not gap.resolution_evidence_ref or not _sha256_is_valid(gap.resolution_evidence_sha256):
            unverified.append(f"v2r_promotion_gap_resolution_evidence_missing:{gap.gap_id}")
            continue
        requirement = gap_requirements.get(gap.gap_id)
        if requirement is None:
            blocked.append(f"v2r_promotion_gap_not_policy_required:{gap.gap_id}")
            continue
        required_scope = (
            source_scope
            if requirement.evidence_scope is PromotionEvidenceScope.SOURCE
            else target_scope
        )
        if gap.resolution_evidence_kind != requirement.required_evidence_kind:
            blocked.append(f"v2r_promotion_gap_resolution_kind_mismatch:{gap.gap_id}")
        if gap.resolution_evidence_origin is not requirement.required_origin:
            blocked.append(f"v2r_promotion_gap_resolution_origin_mismatch:{gap.gap_id}")
        if parse_hardware_execution_mode(gap.resolution_execution_scope) is not required_scope:
            blocked.append(f"v2r_promotion_gap_resolution_scope_mismatch:{gap.gap_id}")
        resolution = registry.get(gap.resolution_evidence_ref)
        if resolution is None:
            unverified.append(f"v2r_promotion_gap_resolution_evidence_unavailable:{gap.gap_id}")
        else:
            if resolution.content_sha256 != gap.resolution_evidence_sha256:
                blocked.append(f"v2r_promotion_gap_resolution_evidence_mismatch:{gap.gap_id}")
            if resolution.evidence_kind != gap.resolution_evidence_kind:
                blocked.append(f"v2r_promotion_gap_resolution_kind_mismatch:{gap.gap_id}")
            if resolution.origin is not gap.resolution_evidence_origin:
                blocked.append(f"v2r_promotion_gap_resolution_origin_mismatch:{gap.gap_id}")
            if parse_hardware_execution_mode(resolution.execution_scope) is not required_scope:
                blocked.append(f"v2r_promotion_gap_resolution_scope_mismatch:{gap.gap_id}")

    for actual, required, reason in (
        (
            set(receipt.target_required_evidence_kinds),
            set(context.active_policy.required_target_evidence_kinds),
            "v2r_promotion_target_evidence_requirements_mismatch",
        ),
        (
            set(receipt.rollback_condition_ids),
            set(context.active_policy.required_rollback_condition_ids),
            "v2r_promotion_rollback_conditions_mismatch",
        ),
        (
            set(receipt.disable_condition_ids),
            set(context.active_policy.required_disable_condition_ids),
            "v2r_promotion_disable_conditions_mismatch",
        ),
    ):
        if actual != required:
            blocked.append(reason)
    if _duplicates(receipt.target_required_evidence_kinds):
        blocked.append("v2r_promotion_target_evidence_requirement_duplicate")

    if any(
        value is not False
        for value in (
            receipt.approval_created,
            receipt.dispatch_authority_created,
            receipt.runtime_effect_requested,
            receipt.task_completion_claimed,
            receipt.physical_safety_claimed,
            receipt.physical_execution_invoked,
        )
    ):
        blocked.append("v2r_promotion_authority_or_physical_claim_forbidden")

    reasons = tuple(dict.fromkeys((*blocked, *unverified)))
    if blocked:
        status = PromotionReceiptValidationStatus.BLOCKED
    elif unverified:
        status = PromotionReceiptValidationStatus.UNVERIFIED
    else:
        status = PromotionReceiptValidationStatus.VERIFIED_PREREQUISITE
    verified = status is PromotionReceiptValidationStatus.VERIFIED_PREREQUISITE
    return VirtualToRealPromotionValidation(
        status=status,
        verification_basis=(
            VerificationBasis.DETERMINISTIC if verified else VerificationBasis.UNVERIFIED
        ),
        reasons=reasons,
        promotion_prerequisite_satisfied=verified,
        receipt_sha256=receipt.receipt_sha256,
    )


def validate_virtual_to_real_promotion_receipt(
    receipt: VirtualToRealPromotionReceipt | None,
    *,
    context: VirtualToRealPromotionValidationContext,
) -> VirtualToRealPromotionValidation:
    """Validate a receipt and refuse malformed runtime objects without raising."""

    try:
        return _validate_virtual_to_real_promotion_receipt(
            receipt,
            context=context,
        )
    except (AttributeError, TypeError, ValueError):
        receipt_sha256: str | None = None
        if isinstance(receipt, VirtualToRealPromotionReceipt):
            try:
                receipt_sha256 = receipt.receipt_sha256
            except (AttributeError, TypeError, ValueError):
                pass
        return VirtualToRealPromotionValidation(
            status=PromotionReceiptValidationStatus.BLOCKED,
            verification_basis=VerificationBasis.UNVERIFIED,
            reasons=("v2r_promotion_receipt_structure_invalid",),
            promotion_prerequisite_satisfied=False,
            receipt_sha256=receipt_sha256,
        )


__all__ = [
    "PHYSICAL_TARGET_SCOPES",
    "PromotionComponentBinding",
    "PromotionComponentKind",
    "PromotionEvidenceScope",
    "PromotionGap",
    "PromotionGapRequirement",
    "PromotionGapStatus",
    "PromotionReceiptValidationStatus",
    "PromotionSourceEvidenceBinding",
    "V2R_PROMOTION_POLICY_SCHEMA_VERSION",
    "V2R_PROMOTION_RECEIPT_SCHEMA_VERSION",
    "VirtualToRealPromotionPolicy",
    "VirtualToRealPromotionReceipt",
    "VirtualToRealPromotionValidation",
    "VirtualToRealPromotionValidationContext",
    "promotion_approval_artifact_sha256",
    "validate_virtual_to_real_promotion_receipt",
]

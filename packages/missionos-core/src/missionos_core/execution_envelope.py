"""Policy-authoritative execution-envelope contracts.

Adapters may report the limits they observed as configured. They cannot choose
the policy bounds, widen them, or turn a declaration into enforcement evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
from typing import Any

from .action_feasibility import (
    EvidenceOrigin,
    EvidenceSourceRef,
    FeasibilityStatus,
    PolicyBinding,
    VerificationBasis,
    VerificationItem,
    VerificationItemStatus,
    aggregate_verification_items,
    canonical_sha256,
)
from .execution_scope import HardwareExecutionMode, parse_hardware_execution_mode
from .safe_stop_receipt import (
    SafeStopExerciseReceipt,
    SafeStopReceiptValidationStatus,
    SafeStopValidationContext,
    validate_safe_stop_exercise_receipt,
)


EXECUTION_ENVELOPE_SCHEMA_VERSION = "missionos_core_execution_envelope.v1"
EXECUTION_ENVELOPE_POLICY_SCHEMA_VERSION = (
    "missionos_core_execution_envelope_policy.v1"
)
EXECUTION_ENVELOPE_READBACK_SCHEMA_VERSION = (
    "missionos_core_execution_envelope_readback.v1"
)
EXECUTION_ENVELOPE_READBACK_EVIDENCE_KIND = (
    "execution_envelope_limit_readback"
)
ENVELOPE_STOP_RECEIPT_ITEM_ID = "execution_envelope_safe_stop_receipt"


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


def _valid_sha256(value: str | None) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


class EnvelopeLimitType(str, Enum):
    """Backend-neutral limit meanings; unknown types are not defaulted."""

    WORKSPACE_X = "workspace_x"
    WORKSPACE_Y = "workspace_y"
    WORKSPACE_Z = "workspace_z"
    LINEAR_SPEED = "linear_speed"
    ANGULAR_SPEED = "angular_speed"
    FORCE = "force"
    TORQUE = "torque"
    SEPARATION = "separation"
    OPERATION_TIMEOUT = "operation_timeout"


class EnvelopeUnit(str, Enum):
    METRE = "m"
    METRES_PER_SECOND = "m/s"
    RADIANS_PER_SECOND = "rad/s"
    NEWTON = "N"
    NEWTON_METRE = "N*m"
    SECOND = "s"


class EnvelopeBoundKind(str, Enum):
    MAXIMUM = "maximum"
    MINIMUM = "minimum"
    RANGE = "range"


class EnvelopeEnforcementLocation(str, Enum):
    ADAPTER = "adapter"
    CONTROLLER = "controller"
    PLANNER = "planner"
    EXTERNAL_SAFETY_MONITOR = "external_safety_monitor"


_LIMIT_SEMANTICS = {
    EnvelopeLimitType.WORKSPACE_X: (
        EnvelopeUnit.METRE,
        EnvelopeBoundKind.RANGE,
    ),
    EnvelopeLimitType.WORKSPACE_Y: (
        EnvelopeUnit.METRE,
        EnvelopeBoundKind.RANGE,
    ),
    EnvelopeLimitType.WORKSPACE_Z: (
        EnvelopeUnit.METRE,
        EnvelopeBoundKind.RANGE,
    ),
    EnvelopeLimitType.LINEAR_SPEED: (
        EnvelopeUnit.METRES_PER_SECOND,
        EnvelopeBoundKind.MAXIMUM,
    ),
    EnvelopeLimitType.ANGULAR_SPEED: (
        EnvelopeUnit.RADIANS_PER_SECOND,
        EnvelopeBoundKind.MAXIMUM,
    ),
    EnvelopeLimitType.FORCE: (
        EnvelopeUnit.NEWTON,
        EnvelopeBoundKind.MAXIMUM,
    ),
    EnvelopeLimitType.TORQUE: (
        EnvelopeUnit.NEWTON_METRE,
        EnvelopeBoundKind.MAXIMUM,
    ),
    EnvelopeLimitType.SEPARATION: (
        EnvelopeUnit.METRE,
        EnvelopeBoundKind.MINIMUM,
    ),
    EnvelopeLimitType.OPERATION_TIMEOUT: (
        EnvelopeUnit.SECOND,
        EnvelopeBoundKind.MAXIMUM,
    ),
}


@dataclass(frozen=True)
class EnvelopeLimitPolicy:
    """One policy-owned bound, never supplied by an adapter readback."""

    limit_id: str
    limit_type: EnvelopeLimitType | None
    unit: EnvelopeUnit | None
    bound_kind: EnvelopeBoundKind | None
    enforcement_location: EnvelopeEnforcementLocation | None
    lower_bound: float | None = None
    upper_bound: float | None = None
    unknown_fields: tuple[str, ...] = ()

    def to_material(self) -> dict[str, Any]:
        return {
            "limit_id": self.limit_id,
            "limit_type": self.limit_type.value if self.limit_type else None,
            "unit": self.unit.value if self.unit else None,
            "bound_kind": self.bound_kind.value if self.bound_kind else None,
            "enforcement_location": (
                self.enforcement_location.value
                if self.enforcement_location
                else None
            ),
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EnvelopeLimitPolicy:
        known = {
            "limit_id",
            "limit_type",
            "unit",
            "bound_kind",
            "enforcement_location",
            "lower_bound",
            "upper_bound",
        }
        return cls(
            limit_id=str(value.get("limit_id") or ""),
            limit_type=_enum_or_none(EnvelopeLimitType, value.get("limit_type")),
            unit=_enum_or_none(EnvelopeUnit, value.get("unit")),
            bound_kind=_enum_or_none(EnvelopeBoundKind, value.get("bound_kind")),
            enforcement_location=_enum_or_none(
                EnvelopeEnforcementLocation,
                value.get("enforcement_location"),
            ),
            lower_bound=_float_or_none(value.get("lower_bound")),
            upper_bound=_float_or_none(value.get("upper_bound")),
            unknown_fields=tuple(sorted(set(value) - known)),
        )


@dataclass(frozen=True)
class ExecutionEnvelopePolicy:
    """Versioned policy material that is the sole bound authority."""

    policy_id: str
    policy_version: str
    execution_scope: HardwareExecutionMode | None
    maximum_readback_age_seconds: float
    limits: tuple[EnvelopeLimitPolicy, ...]
    unknown_fields: tuple[str, ...] = ()
    schema_version: str = EXECUTION_ENVELOPE_POLICY_SCHEMA_VERSION

    def to_material(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "execution_scope": (
                self.execution_scope.value if self.execution_scope else None
            ),
            "maximum_readback_age_seconds": self.maximum_readback_age_seconds,
            "limits": [limit.to_material() for limit in self.limits],
        }

    @property
    def binding(self) -> PolicyBinding:
        return PolicyBinding(
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            policy_sha256=canonical_sha256(self.to_material()),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExecutionEnvelopePolicy:
        known = {
            "schema_version",
            "policy_id",
            "policy_version",
            "execution_scope",
            "maximum_readback_age_seconds",
            "limits",
        }
        return cls(
            policy_id=str(value.get("policy_id") or ""),
            policy_version=str(value.get("policy_version") or ""),
            execution_scope=parse_hardware_execution_mode(
                value.get("execution_scope")
            ),
            maximum_readback_age_seconds=(
                _float_or_none(value.get("maximum_readback_age_seconds"))
                or float("nan")
            ),
            limits=tuple(
                EnvelopeLimitPolicy.from_dict(item)
                for item in value.get("limits", ())
                if isinstance(item, Mapping)
            ),
            unknown_fields=tuple(sorted(set(value) - known)),
            schema_version=str(value.get("schema_version") or ""),
        )


@dataclass(frozen=True)
class EnvelopeLimitReadback:
    """Machine observation of an applied limit; never policy authority."""

    limit_id: str
    unit: EnvelopeUnit | None
    enforcement_location: EnvelopeEnforcementLocation | None
    evidence_ref: str
    lower_bound: float | None = None
    upper_bound: float | None = None
    unknown_fields: tuple[str, ...] = ()
    schema_version: str = EXECUTION_ENVELOPE_READBACK_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EnvelopeLimitReadback:
        known = {
            "schema_version",
            "limit_id",
            "unit",
            "enforcement_location",
            "evidence_ref",
            "lower_bound",
            "upper_bound",
        }
        return cls(
            limit_id=str(value.get("limit_id") or ""),
            unit=_enum_or_none(EnvelopeUnit, value.get("unit")),
            enforcement_location=_enum_or_none(
                EnvelopeEnforcementLocation,
                value.get("enforcement_location"),
            ),
            evidence_ref=str(value.get("evidence_ref") or ""),
            lower_bound=_float_or_none(value.get("lower_bound")),
            upper_bound=_float_or_none(value.get("upper_bound")),
            unknown_fields=tuple(sorted(set(value) - known)),
            schema_version=str(value.get("schema_version") or ""),
        )


@dataclass(frozen=True)
class ExecutionEnvelopeDescriptor:
    """Preparation binding to policy, observed readbacks, and one stop receipt."""

    envelope_id: str
    adapter_id: str
    adapter_version: str
    controller_configuration_sha256: str
    safety_configuration_sha256: str
    execution_scope: HardwareExecutionMode | None
    policy_binding: PolicyBinding
    safe_stop_receipt_ref: str
    readbacks: tuple[EnvelopeLimitReadback, ...]
    unknown_fields: tuple[str, ...] = ()
    schema_version: str = EXECUTION_ENVELOPE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExecutionEnvelopeDescriptor:
        known = {
            "schema_version",
            "envelope_id",
            "adapter_id",
            "adapter_version",
            "controller_configuration_sha256",
            "safety_configuration_sha256",
            "execution_scope",
            "policy_binding",
            "safe_stop_receipt_ref",
            "readbacks",
        }
        binding = dict(value.get("policy_binding") or {})
        return cls(
            envelope_id=str(value.get("envelope_id") or ""),
            adapter_id=str(value.get("adapter_id") or ""),
            adapter_version=str(value.get("adapter_version") or ""),
            controller_configuration_sha256=str(
                value.get("controller_configuration_sha256") or ""
            ),
            safety_configuration_sha256=str(
                value.get("safety_configuration_sha256") or ""
            ),
            execution_scope=parse_hardware_execution_mode(
                value.get("execution_scope")
            ),
            policy_binding=PolicyBinding(
                policy_id=str(binding.get("policy_id") or ""),
                policy_version=str(binding.get("policy_version") or ""),
                policy_sha256=str(binding.get("policy_sha256") or ""),
            ),
            safe_stop_receipt_ref=str(value.get("safe_stop_receipt_ref") or ""),
            readbacks=tuple(
                EnvelopeLimitReadback.from_dict(item)
                for item in value.get("readbacks", ())
                if isinstance(item, Mapping)
            ),
            unknown_fields=tuple(sorted(set(value) - known)),
            schema_version=str(value.get("schema_version") or ""),
        )


@dataclass(frozen=True)
class ExecutionEnvelopeValidationContext:
    expected_execution_scope: HardwareExecutionMode | str
    adapter_id: str
    adapter_version: str
    controller_configuration_sha256: str
    safety_configuration_sha256: str
    active_policy: ExecutionEnvelopePolicy
    evidence_sources: Mapping[str, EvidenceSourceRef]
    safe_stop_receipt: SafeStopExerciseReceipt
    safe_stop_context: SafeStopValidationContext
    evaluated_at: str


@dataclass(frozen=True)
class ExecutionEnvelopeValidation:
    status: FeasibilityStatus
    verification_basis: VerificationBasis
    reasons: tuple[str, ...]
    verification_items: tuple[VerificationItem, ...]
    required_verification_item_ids: tuple[str, ...]
    policy_binding: PolicyBinding
    approval_created: bool = False
    dispatch_authority_created: bool = False
    task_completion_claimed: bool = False
    physical_execution_invoked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _enum_or_none(enum_type: type[Enum], value: Any) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _limit_structure_reasons(limit: EnvelopeLimitPolicy) -> list[str]:
    reasons: list[str] = []
    prefix = f"execution_envelope_policy_limit:{limit.limit_id or 'missing'}"
    if not limit.limit_id:
        reasons.append("execution_envelope_policy_limit_id_missing")
    if limit.unknown_fields:
        reasons.append(f"{prefix}:unknown_fields")
    if limit.limit_type is None:
        reasons.append(f"{prefix}:type_unverified")
    if limit.unit is None:
        reasons.append(f"{prefix}:unit_unverified")
    if limit.enforcement_location is None:
        reasons.append(f"{prefix}:enforcement_location_unverified")
    semantics = _LIMIT_SEMANTICS.get(limit.limit_type)
    if semantics is not None:
        expected_unit, expected_bound_kind = semantics
        if limit.unit is not expected_unit:
            reasons.append(f"{prefix}:unit_not_valid_for_type")
        if limit.bound_kind is not expected_bound_kind:
            reasons.append(f"{prefix}:bound_kind_not_valid_for_type")
    if limit.bound_kind is EnvelopeBoundKind.MAXIMUM:
        if not _finite(limit.upper_bound) or limit.lower_bound is not None:
            reasons.append(f"{prefix}:maximum_bound_invalid")
    elif limit.bound_kind is EnvelopeBoundKind.MINIMUM:
        if not _finite(limit.lower_bound) or limit.upper_bound is not None:
            reasons.append(f"{prefix}:minimum_bound_invalid")
    elif limit.bound_kind is EnvelopeBoundKind.RANGE:
        if (
            not _finite(limit.lower_bound)
            or not _finite(limit.upper_bound)
            or limit.lower_bound > limit.upper_bound
        ):
            reasons.append(f"{prefix}:range_bound_invalid")
    else:
        reasons.append(f"{prefix}:bound_kind_unverified")
    if (
        limit.limit_type
        not in {
            EnvelopeLimitType.WORKSPACE_X,
            EnvelopeLimitType.WORKSPACE_Y,
            EnvelopeLimitType.WORKSPACE_Z,
        }
        and (
            (_finite(limit.lower_bound) and limit.lower_bound < 0)
            or (_finite(limit.upper_bound) and limit.upper_bound < 0)
        )
    ):
        reasons.append(f"{prefix}:negative_bound_invalid")
    return reasons


def _readback_within_policy(
    readback: EnvelopeLimitReadback,
    policy: EnvelopeLimitPolicy,
) -> bool | None:
    if policy.bound_kind is EnvelopeBoundKind.MAXIMUM:
        if not _finite(readback.upper_bound) or readback.lower_bound is not None:
            return None
        return readback.upper_bound <= policy.upper_bound
    if policy.bound_kind is EnvelopeBoundKind.MINIMUM:
        if not _finite(readback.lower_bound) or readback.upper_bound is not None:
            return None
        return readback.lower_bound >= policy.lower_bound
    if policy.bound_kind is EnvelopeBoundKind.RANGE:
        if not _finite(readback.lower_bound) or not _finite(readback.upper_bound):
            return None
        if readback.lower_bound > readback.upper_bound:
            return None
        return (
            readback.lower_bound >= policy.lower_bound
            and readback.upper_bound <= policy.upper_bound
        )
    return None


def validate_execution_envelope(
    descriptor: ExecutionEnvelopeDescriptor,
    *,
    context: ExecutionEnvelopeValidationContext,
) -> ExecutionEnvelopeValidation:
    """Validate policy bounds, applied readbacks, and a same-scope stop receipt."""

    unverified: list[str] = []
    blocked: list[str] = []
    expected_scope = parse_hardware_execution_mode(
        context.expected_execution_scope
    )
    descriptor_scope = parse_hardware_execution_mode(descriptor.execution_scope)
    policy_scope = parse_hardware_execution_mode(context.active_policy.execution_scope)
    if expected_scope is None:
        unverified.append("execution_envelope_expected_scope_unverified")
    if descriptor_scope is None:
        unverified.append("execution_envelope_scope_unverified")
    elif expected_scope is not None and descriptor_scope is not expected_scope:
        unverified.append("execution_envelope_scope_mismatch")
    if policy_scope is None or policy_scope is not expected_scope:
        unverified.append("execution_envelope_policy_scope_mismatch")
    if descriptor.schema_version != EXECUTION_ENVELOPE_SCHEMA_VERSION:
        unverified.append("execution_envelope_schema_not_supported")
    if descriptor.unknown_fields:
        unverified.append("execution_envelope_unknown_fields")
    if not descriptor.envelope_id:
        unverified.append("execution_envelope_id_missing")

    for actual, expected, reason in (
        (descriptor.adapter_id, context.adapter_id, "execution_envelope_adapter_id_drift"),
        (
            descriptor.adapter_version,
            context.adapter_version,
            "execution_envelope_adapter_version_drift",
        ),
        (
            descriptor.controller_configuration_sha256,
            context.controller_configuration_sha256,
            "execution_envelope_controller_configuration_drift",
        ),
        (
            descriptor.safety_configuration_sha256,
            context.safety_configuration_sha256,
            "execution_envelope_safety_configuration_drift",
        ),
    ):
        if not actual or actual != expected:
            unverified.append(reason)
    if not _valid_sha256(descriptor.controller_configuration_sha256):
        unverified.append("execution_envelope_controller_digest_unverified")
    if not _valid_sha256(descriptor.safety_configuration_sha256):
        unverified.append("execution_envelope_safety_digest_unverified")

    policy = context.active_policy
    if policy.schema_version != EXECUTION_ENVELOPE_POLICY_SCHEMA_VERSION:
        unverified.append("execution_envelope_policy_schema_not_supported")
    if policy.unknown_fields:
        unverified.append("execution_envelope_policy_unknown_fields")
    if not policy.policy_id or not policy.policy_version:
        unverified.append("execution_envelope_policy_identity_missing")
    if (
        not math.isfinite(policy.maximum_readback_age_seconds)
        or policy.maximum_readback_age_seconds <= 0
    ):
        unverified.append("execution_envelope_readback_freshness_policy_invalid")
    if descriptor.policy_binding != policy.binding:
        unverified.append("execution_envelope_policy_binding_mismatch")

    limit_by_id: dict[str, EnvelopeLimitPolicy] = {}
    for limit in policy.limits:
        unverified.extend(_limit_structure_reasons(limit))
        if limit.limit_id in limit_by_id:
            unverified.append(f"execution_envelope_policy_limit_duplicate:{limit.limit_id}")
        else:
            limit_by_id[limit.limit_id] = limit
    if not limit_by_id:
        unverified.append("execution_envelope_policy_limits_missing")

    readback_by_id: dict[str, EnvelopeLimitReadback] = {}
    for readback in descriptor.readbacks:
        if readback.limit_id in readback_by_id:
            unverified.append(f"execution_envelope_readback_duplicate:{readback.limit_id}")
        else:
            readback_by_id[readback.limit_id] = readback
        if readback.limit_id not in limit_by_id:
            unverified.append(f"execution_envelope_readback_not_in_policy:{readback.limit_id}")
    items: list[VerificationItem] = []
    evaluated_at = _timestamp(context.evaluated_at)
    combined_sources = dict(context.safe_stop_context.evidence_sources)
    for source_id, source in context.evidence_sources.items():
        existing = combined_sources.get(source_id)
        if existing is not None and existing != source:
            unverified.append(
                f"execution_envelope_evidence_source_collision:{source_id}"
            )
            continue
        combined_sources[source_id] = source

    for limit_id, limit in limit_by_id.items():
        item_id = f"execution_envelope_limit:{limit_id}"
        readback = readback_by_id.get(limit_id)
        item_reasons: list[str] = []
        evidence_refs: tuple[str, ...] = ()
        item_status = VerificationItemStatus.PASS
        item_basis = VerificationBasis.DETERMINISTIC
        if readback is None:
            item_reasons.append(f"execution_envelope_readback_missing:{limit_id}")
        else:
            evidence_refs = (readback.evidence_ref,)
            if readback.schema_version != EXECUTION_ENVELOPE_READBACK_SCHEMA_VERSION:
                item_reasons.append(f"execution_envelope_readback_schema_invalid:{limit_id}")
            if readback.unknown_fields:
                item_reasons.append(f"execution_envelope_readback_unknown_fields:{limit_id}")
            if readback.unit is None or readback.unit is not limit.unit:
                item_reasons.append(f"execution_envelope_readback_unit_mismatch:{limit_id}")
            if (
                readback.enforcement_location is None
                or readback.enforcement_location is not limit.enforcement_location
            ):
                item_reasons.append(
                    f"execution_envelope_readback_enforcement_location_mismatch:{limit_id}"
                )
            source = context.evidence_sources.get(readback.evidence_ref)
            if not readback.evidence_ref or source is None:
                item_reasons.append(
                    f"execution_envelope_readback_evidence_missing:{limit_id}"
                )
            else:
                if source.source_id != readback.evidence_ref:
                    item_reasons.append(
                        f"execution_envelope_readback_registry_mismatch:{limit_id}"
                    )
                if (
                    source.evidence_kind
                    != EXECUTION_ENVELOPE_READBACK_EVIDENCE_KIND
                ):
                    item_reasons.append(
                        f"execution_envelope_readback_evidence_kind_mismatch:{limit_id}"
                    )
                if source.origin is not EvidenceOrigin.MACHINE_OBSERVED:
                    item_reasons.append(
                        f"execution_envelope_readback_origin_unverified:{limit_id}"
                    )
                if source.execution_scope is not expected_scope:
                    item_reasons.append(
                        f"execution_envelope_readback_scope_mismatch:{limit_id}"
                    )
                if not _valid_sha256(source.content_sha256):
                    item_reasons.append(
                        f"execution_envelope_readback_digest_unverified:{limit_id}"
                    )
                observed_at = _timestamp(source.observed_at)
                deadline = _timestamp(source.freshness_deadline)
                if (
                    observed_at is None
                    or evaluated_at is None
                    or deadline is None
                    or not math.isfinite(policy.maximum_readback_age_seconds)
                ):
                    item_reasons.append(
                        f"execution_envelope_readback_freshness_unverified:{limit_id}"
                    )
                else:
                    expected_deadline = observed_at + timedelta(
                        seconds=policy.maximum_readback_age_seconds
                    )
                    if deadline != expected_deadline:
                        item_reasons.append(
                            f"execution_envelope_readback_deadline_not_policy_derived:{limit_id}"
                        )
                    if observed_at > evaluated_at:
                        item_reasons.append(
                            f"execution_envelope_readback_observed_in_future:{limit_id}"
                        )
                    if evaluated_at > deadline:
                        item_reasons.append(
                            f"execution_envelope_readback_stale:{limit_id}"
                        )
            within_policy = _readback_within_policy(readback, limit)
            if within_policy is None:
                item_reasons.append(
                    f"execution_envelope_readback_bound_invalid:{limit_id}"
                )
            elif not within_policy and not item_reasons:
                blocked.append(
                    f"execution_envelope_readback_exceeds_policy:{limit_id}"
                )
                item_status = VerificationItemStatus.BLOCKED
        if item_reasons:
            unverified.extend(item_reasons)
            item_status = VerificationItemStatus.PENDING
            item_basis = VerificationBasis.UNVERIFIED
        items.append(
            VerificationItem(
                item_id=item_id,
                predicate=f"applied {limit_id} remains within policy bound",
                status=item_status,
                verification_basis=item_basis,
                evidence_refs=evidence_refs,
            )
        )

    stop_validation = validate_safe_stop_exercise_receipt(
        context.safe_stop_receipt,
        context=context.safe_stop_context,
    )
    if descriptor.safe_stop_receipt_ref != context.safe_stop_receipt.receipt_id:
        unverified.append("execution_envelope_safe_stop_receipt_ref_mismatch")
    if context.safe_stop_context.expected_execution_scope != expected_scope:
        unverified.append("execution_envelope_safe_stop_context_scope_mismatch")
    for actual, expected, reason in (
        (
            context.safe_stop_context.adapter_id,
            context.adapter_id,
            "execution_envelope_safe_stop_adapter_mismatch",
        ),
        (
            context.safe_stop_context.adapter_version,
            context.adapter_version,
            "execution_envelope_safe_stop_adapter_version_mismatch",
        ),
        (
            context.safe_stop_context.controller_configuration_sha256,
            context.controller_configuration_sha256,
            "execution_envelope_safe_stop_controller_mismatch",
        ),
        (
            context.safe_stop_context.safety_configuration_sha256,
            context.safety_configuration_sha256,
            "execution_envelope_safe_stop_safety_mismatch",
        ),
    ):
        if actual != expected:
            unverified.append(reason)
    stop_verified = (
        stop_validation.status is SafeStopReceiptValidationStatus.VERIFIED
        and descriptor.safe_stop_receipt_ref == context.safe_stop_receipt.receipt_id
    )
    if not stop_verified:
        unverified.append("execution_envelope_safe_stop_receipt_unverified")
    items.append(
        VerificationItem(
            item_id=ENVELOPE_STOP_RECEIPT_ITEM_ID,
            predicate="matching same-scope safe-stop receipt remains valid",
            status=(
                VerificationItemStatus.PASS
                if stop_verified
                else VerificationItemStatus.PENDING
            ),
            verification_basis=(
                VerificationBasis.DETERMINISTIC
                if stop_verified
                else VerificationBasis.UNVERIFIED
            ),
            evidence_refs=(context.safe_stop_receipt.observed_effect_evidence_ref,),
        )
    )

    required_ids = tuple(
        [f"execution_envelope_limit:{limit_id}" for limit_id in limit_by_id]
        + [ENVELOPE_STOP_RECEIPT_ITEM_ID]
    )
    aggregate = aggregate_verification_items(
        items=items,
        required_item_ids=required_ids,
        evidence_sources=combined_sources,
        expected_execution_scope=expected_scope,
    )
    blocked.extend(aggregate.blocked_reasons)
    unverified.extend(aggregate.unverified_reasons)
    blocked = list(dict.fromkeys(blocked))
    unverified = list(dict.fromkeys(unverified))
    if blocked:
        status = FeasibilityStatus.BLOCKED
        basis = VerificationBasis.DETERMINISTIC
        reasons = tuple(blocked + unverified)
    elif unverified or not aggregate.positive:
        status = FeasibilityStatus.UNVERIFIED
        basis = VerificationBasis.UNVERIFIED
        reasons = tuple(unverified)
    else:
        status = FeasibilityStatus.VERIFIED_FEASIBLE
        basis = VerificationBasis.DETERMINISTIC
        reasons = ()
    return ExecutionEnvelopeValidation(
        status=status,
        verification_basis=basis,
        reasons=reasons,
        verification_items=tuple(items),
        required_verification_item_ids=required_ids,
        policy_binding=policy.binding,
    )


__all__ = [
    "ENVELOPE_STOP_RECEIPT_ITEM_ID",
    "EXECUTION_ENVELOPE_POLICY_SCHEMA_VERSION",
    "EXECUTION_ENVELOPE_READBACK_EVIDENCE_KIND",
    "EXECUTION_ENVELOPE_READBACK_SCHEMA_VERSION",
    "EXECUTION_ENVELOPE_SCHEMA_VERSION",
    "EnvelopeBoundKind",
    "EnvelopeEnforcementLocation",
    "EnvelopeLimitPolicy",
    "EnvelopeLimitReadback",
    "EnvelopeLimitType",
    "EnvelopeUnit",
    "ExecutionEnvelopeDescriptor",
    "ExecutionEnvelopePolicy",
    "ExecutionEnvelopeValidation",
    "ExecutionEnvelopeValidationContext",
    "validate_execution_envelope",
]

"""Backend-neutral safe-stop exercise receipt and fail-closed validator."""

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
    PolicyBinding,
    VerificationBasis,
    VerificationItem,
    aggregate_verification_items,
    canonical_sha256,
)
from .execution_scope import (
    HardwareExecutionMode,
    parse_hardware_execution_mode,
)


SAFE_STOP_EXERCISE_RECEIPT_SCHEMA_VERSION = (
    "missionos_core_safe_stop_exercise_receipt.v1"
)

SAFE_STOP_BOUNDS_ITEM_ID = "safe_stop_exercise_bounds_observed"
SAFE_STOP_REQUEST_ITEM_ID = "safe_stop_request_observed"
SAFE_STOP_ACK_ITEM_ID = "safe_stop_ack_observed"
SAFE_STOP_EFFECT_ITEM_ID = "safe_stop_effect_observed"
SAFE_STOP_REQUIRED_ITEM_IDS = (
    SAFE_STOP_BOUNDS_ITEM_ID,
    SAFE_STOP_REQUEST_ITEM_ID,
    SAFE_STOP_ACK_ITEM_ID,
    SAFE_STOP_EFFECT_ITEM_ID,
)

SAFE_STOP_PRE_STATE_EVIDENCE_KIND = "safe_stop_pre_state"
SAFE_STOP_REQUEST_EVIDENCE_KIND = "safe_stop_request"
SAFE_STOP_ACK_EVIDENCE_KIND = "safe_stop_ack"
SAFE_STOP_EFFECT_EVIDENCE_KIND = "safe_stop_observed_effect"
SAFE_STOP_POST_STATE_EVIDENCE_KIND = "safe_stop_post_state"
SAFE_STOP_RUNTIME_INVOCATION_EVIDENCE_KIND = "runtime_invocation_evidence"
SAFE_STOP_EXERCISE_APPROVAL_EVIDENCE_KIND = "safe_stop_exercise_approval"


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


def _sha256_is_valid(value: str) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


class SafeStopReceiptValidationStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class SafeStopFreshnessPolicy:
    """Policy-owned receipt lifetime; adapters cannot choose this value."""

    policy_id: str
    policy_version: str
    maximum_age_seconds: float

    def to_material(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
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
class SafeStopValidationContext:
    """Current consuming context against which a receipt is checked."""

    expected_execution_scope: HardwareExecutionMode | str
    adapter_id: str
    adapter_version: str
    controller_configuration_sha256: str
    safety_configuration_sha256: str
    active_policy: SafeStopFreshnessPolicy
    evidence_sources: Mapping[str, EvidenceSourceRef]
    evaluated_at: str


@dataclass(frozen=True)
class SafeStopExerciseReceipt:
    """Observed safe-stop exercise facts without action authority."""

    receipt_id: str
    adapter_id: str
    adapter_version: str
    controller_configuration_sha256: str
    safety_configuration_sha256: str
    stop_mechanism: str
    exercise_recipe_id: str
    exercise_recipe_version: str
    exercise_recipe_sha256: str
    exercise_approval_ref: str
    execution_scope: HardwareExecutionMode | None
    observed_at: str
    freshness_deadline: str
    policy_binding: PolicyBinding
    verification_items: tuple[VerificationItem, ...]
    required_verification_item_ids: tuple[str, ...]
    pre_state_evidence_ref: str
    request_evidence_ref: str
    ack_evidence_ref: str
    observed_effect_evidence_ref: str
    post_state_evidence_ref: str
    runtime_invocation_evidence_ref: str
    approval_created: bool = False
    dispatch_authority_created: bool = False
    task_completion_claimed: bool = False
    physical_execution_invoked: bool = False
    schema_version: str = SAFE_STOP_EXERCISE_RECEIPT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> SafeStopExerciseReceipt:
        policy = dict(value.get("policy_binding") or {})
        return cls(
            receipt_id=str(value.get("receipt_id") or ""),
            adapter_id=str(value.get("adapter_id") or ""),
            adapter_version=str(value.get("adapter_version") or ""),
            controller_configuration_sha256=str(
                value.get("controller_configuration_sha256") or ""
            ),
            safety_configuration_sha256=str(
                value.get("safety_configuration_sha256") or ""
            ),
            stop_mechanism=str(value.get("stop_mechanism") or ""),
            exercise_recipe_id=str(value.get("exercise_recipe_id") or ""),
            exercise_recipe_version=str(
                value.get("exercise_recipe_version") or ""
            ),
            exercise_recipe_sha256=str(
                value.get("exercise_recipe_sha256") or ""
            ),
            exercise_approval_ref=str(
                value.get("exercise_approval_ref") or ""
            ),
            execution_scope=parse_hardware_execution_mode(
                value.get("execution_scope")
            ),
            observed_at=str(value.get("observed_at") or ""),
            freshness_deadline=str(value.get("freshness_deadline") or ""),
            policy_binding=PolicyBinding(
                policy_id=str(policy.get("policy_id") or ""),
                policy_version=str(policy.get("policy_version") or ""),
                policy_sha256=str(policy.get("policy_sha256") or ""),
            ),
            verification_items=tuple(
                VerificationItem.from_dict(item)
                for item in value.get("verification_items", ())
                if isinstance(item, Mapping)
            ),
            required_verification_item_ids=tuple(
                str(item_id)
                for item_id in value.get(
                    "required_verification_item_ids",
                    (),
                )
            ),
            pre_state_evidence_ref=str(
                value.get("pre_state_evidence_ref") or ""
            ),
            request_evidence_ref=str(
                value.get("request_evidence_ref") or ""
            ),
            ack_evidence_ref=str(value.get("ack_evidence_ref") or ""),
            observed_effect_evidence_ref=str(
                value.get("observed_effect_evidence_ref") or ""
            ),
            post_state_evidence_ref=str(
                value.get("post_state_evidence_ref") or ""
            ),
            runtime_invocation_evidence_ref=str(
                value.get("runtime_invocation_evidence_ref") or ""
            ),
            approval_created=bool(value.get("approval_created", False)),
            dispatch_authority_created=bool(
                value.get("dispatch_authority_created", False)
            ),
            task_completion_claimed=bool(
                value.get("task_completion_claimed", False)
            ),
            physical_execution_invoked=bool(
                value.get("physical_execution_invoked", False)
            ),
            schema_version=str(
                value.get(
                    "schema_version",
                    SAFE_STOP_EXERCISE_RECEIPT_SCHEMA_VERSION,
                )
            ),
        )


@dataclass(frozen=True)
class SafeStopReceiptValidation:
    status: SafeStopReceiptValidationStatus
    verification_basis: VerificationBasis
    reasons: tuple[str, ...]
    stop_capability_evidenced: bool
    approval_created: bool = False
    dispatch_authority_created: bool = False
    task_completion_claimed: bool = False
    physical_execution_invoked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_safe_stop_exercise_receipt(
    receipt: SafeStopExerciseReceipt,
    *,
    context: SafeStopValidationContext,
) -> SafeStopReceiptValidation:
    """Validate one receipt for one exact consuming execution scope."""

    reasons: list[str] = []
    expected_scope = parse_hardware_execution_mode(
        context.expected_execution_scope
    )
    receipt_scope = parse_hardware_execution_mode(receipt.execution_scope)
    if expected_scope is None:
        reasons.append("safe_stop_expected_execution_scope_unverified")
    if receipt_scope is None:
        reasons.append("safe_stop_receipt_execution_scope_unverified")
    elif expected_scope is not None and receipt_scope is not expected_scope:
        reasons.append("safe_stop_receipt_execution_scope_mismatch")
    if receipt_scope is HardwareExecutionMode.SCHEMA_EXAMPLE_ONLY:
        reasons.append("safe_stop_schema_example_cannot_evidence_effect")

    if receipt.schema_version != SAFE_STOP_EXERCISE_RECEIPT_SCHEMA_VERSION:
        reasons.append("safe_stop_receipt_schema_not_supported")
    if not receipt.receipt_id:
        reasons.append("safe_stop_receipt_id_missing")
    for value, reason in (
        (receipt.stop_mechanism, "safe_stop_mechanism_missing"),
        (receipt.exercise_recipe_id, "safe_stop_exercise_recipe_id_missing"),
        (
            receipt.exercise_recipe_version,
            "safe_stop_exercise_recipe_version_missing",
        ),
        (
            receipt.exercise_approval_ref,
            "safe_stop_exercise_approval_ref_missing",
        ),
    ):
        if not value:
            reasons.append(reason)
    if not _sha256_is_valid(receipt.exercise_recipe_sha256):
        reasons.append("safe_stop_exercise_recipe_sha256_unverified")

    for actual, expected, reason in (
        (
            receipt.adapter_id,
            context.adapter_id,
            "safe_stop_adapter_id_drift",
        ),
        (
            receipt.adapter_version,
            context.adapter_version,
            "safe_stop_adapter_version_drift",
        ),
        (
            receipt.controller_configuration_sha256,
            context.controller_configuration_sha256,
            "safe_stop_controller_configuration_drift",
        ),
        (
            receipt.safety_configuration_sha256,
            context.safety_configuration_sha256,
            "safe_stop_safety_configuration_drift",
        ),
    ):
        if not actual or actual != expected:
            reasons.append(reason)
    for digest, reason in (
        (
            receipt.controller_configuration_sha256,
            "safe_stop_controller_configuration_sha256_unverified",
        ),
        (
            receipt.safety_configuration_sha256,
            "safe_stop_safety_configuration_sha256_unverified",
        ),
    ):
        if not _sha256_is_valid(digest):
            reasons.append(reason)

    active_binding = context.active_policy.binding
    if receipt.policy_binding != active_binding:
        reasons.append("safe_stop_policy_binding_mismatch")
    observed_at = _timestamp(receipt.observed_at)
    evaluated_at = _timestamp(context.evaluated_at)
    deadline = _timestamp(receipt.freshness_deadline)
    try:
        maximum_age_seconds = float(
            context.active_policy.maximum_age_seconds
        )
    except (TypeError, ValueError):
        maximum_age_seconds = float("nan")
    if observed_at is None or evaluated_at is None or deadline is None:
        reasons.append("safe_stop_freshness_timestamp_unverified")
    elif (
        not context.active_policy.policy_id
        or not context.active_policy.policy_version
        or not math.isfinite(maximum_age_seconds)
        or maximum_age_seconds <= 0
    ):
        reasons.append("safe_stop_freshness_policy_unverified")
    else:
        expected_deadline = observed_at + timedelta(
            seconds=maximum_age_seconds
        )
        if deadline != expected_deadline:
            reasons.append("safe_stop_freshness_deadline_not_policy_derived")
        if evaluated_at > deadline:
            reasons.append("safe_stop_receipt_stale")
        if observed_at > evaluated_at:
            reasons.append("safe_stop_receipt_observed_in_future")

    sources: dict[str, EvidenceSourceRef] = {}
    for source_id, source in context.evidence_sources.items():
        if not source_id or not source.source_id:
            reasons.append("safe_stop_evidence_source_id_missing")
            continue
        if source_id != source.source_id:
            reasons.append("safe_stop_evidence_source_registry_mismatch")
            continue
        sources[source_id] = source
        if not _sha256_is_valid(str(source.content_sha256 or "")):
            reasons.append(
                f"safe_stop_evidence_content_sha256_unverified:{source_id}"
            )

    required_refs = {
        "exercise_approval": (
            receipt.exercise_approval_ref,
            SAFE_STOP_EXERCISE_APPROVAL_EVIDENCE_KIND,
        ),
        "pre_state": (
            receipt.pre_state_evidence_ref,
            SAFE_STOP_PRE_STATE_EVIDENCE_KIND,
        ),
        "request": (
            receipt.request_evidence_ref,
            SAFE_STOP_REQUEST_EVIDENCE_KIND,
        ),
        "ack": (
            receipt.ack_evidence_ref,
            SAFE_STOP_ACK_EVIDENCE_KIND,
        ),
        "effect": (
            receipt.observed_effect_evidence_ref,
            SAFE_STOP_EFFECT_EVIDENCE_KIND,
        ),
        "post_state": (
            receipt.post_state_evidence_ref,
            SAFE_STOP_POST_STATE_EVIDENCE_KIND,
        ),
        "runtime_invocation": (
            receipt.runtime_invocation_evidence_ref,
            SAFE_STOP_RUNTIME_INVOCATION_EVIDENCE_KIND,
        ),
    }
    resolved: dict[str, EvidenceSourceRef] = {}
    for role, (source_ref, expected_kind) in required_refs.items():
        source = sources.get(source_ref)
        if source is None:
            reasons.append(f"safe_stop_{role}_evidence_missing")
            continue
        resolved[role] = source
        if source.evidence_kind != expected_kind:
            reasons.append(f"safe_stop_{role}_evidence_kind_unverified")
        expected_origin = (
            EvidenceOrigin.AUTHORITY_ARTIFACT
            if role == "exercise_approval"
            else EvidenceOrigin.MACHINE_OBSERVED
        )
        if source.origin is not expected_origin:
            reasons.append(f"safe_stop_{role}_evidence_origin_unverified")
        source_scope = parse_hardware_execution_mode(source.execution_scope)
        if (
            source_scope is None
            or expected_scope is None
            or receipt_scope is None
            or source_scope is not expected_scope
            or source_scope is not receipt_scope
        ):
            reasons.append(f"safe_stop_{role}_evidence_scope_mismatch")
    if len({source_ref for source_ref, _kind in required_refs.values()}) != len(
        required_refs
    ):
        reasons.append("safe_stop_request_ack_effect_not_separate")

    ordered_roles = (
        "exercise_approval",
        "pre_state",
        "request",
        "ack",
        "effect",
        "post_state",
    )
    ordered_timestamps = [
        _timestamp(resolved[role].observed_at)
        for role in ordered_roles
        if role in resolved
    ]
    if len(ordered_timestamps) != len(ordered_roles) or any(
        value is None for value in ordered_timestamps
    ):
        reasons.append("safe_stop_evidence_timestamps_unverified")
    elif any(
        later < earlier
        for earlier, later in zip(
            ordered_timestamps,
            ordered_timestamps[1:],
            strict=False,
        )
    ):
        reasons.append("safe_stop_evidence_temporal_order_invalid")
    post_state = resolved.get("post_state")
    if (
        post_state is not None
        and _timestamp(post_state.observed_at) != observed_at
    ):
        reasons.append("safe_stop_receipt_observed_at_not_post_state")

    aggregate = aggregate_verification_items(
        items=receipt.verification_items,
        required_item_ids=receipt.required_verification_item_ids,
        evidence_sources=sources,
        expected_execution_scope=expected_scope,
    )
    reasons.extend(aggregate.blocked_reasons)
    reasons.extend(aggregate.unverified_reasons)
    if aggregate.verification_basis is not VerificationBasis.DETERMINISTIC:
        reasons.append("safe_stop_verification_basis_not_deterministic")
    if not set(SAFE_STOP_REQUIRED_ITEM_IDS).issubset(
        receipt.required_verification_item_ids
    ):
        reasons.append("safe_stop_required_verification_items_missing")

    items = {item.item_id: item for item in receipt.verification_items}
    required_item_refs = {
        SAFE_STOP_BOUNDS_ITEM_ID: {
            receipt.pre_state_evidence_ref,
            receipt.post_state_evidence_ref,
        },
        SAFE_STOP_REQUEST_ITEM_ID: {
            receipt.request_evidence_ref,
            receipt.runtime_invocation_evidence_ref,
        },
        SAFE_STOP_ACK_ITEM_ID: {receipt.ack_evidence_ref},
        SAFE_STOP_EFFECT_ITEM_ID: {
            receipt.observed_effect_evidence_ref,
            receipt.post_state_evidence_ref,
        },
    }
    for item_id, expected_refs in required_item_refs.items():
        item = items.get(item_id)
        if item is None:
            continue
        if not expected_refs.issubset(item.evidence_refs):
            reasons.append(
                f"safe_stop_item_evidence_binding_missing:{item_id}"
            )

    if any(
        (
            receipt.approval_created,
            receipt.dispatch_authority_created,
            receipt.task_completion_claimed,
        )
    ):
        reasons.append("safe_stop_receipt_authority_claimed")
    physical_scopes = {
        HardwareExecutionMode.HITL,
        HardwareExecutionMode.BENCH,
        HardwareExecutionMode.CAGE,
        HardwareExecutionMode.FIELD,
    }
    if receipt_scope is not None and (
        receipt.physical_execution_invoked
        != (receipt_scope in physical_scopes)
    ):
        reasons.append("safe_stop_physical_execution_claim_mismatch")

    reasons = list(dict.fromkeys(reasons))
    verified = not reasons
    return SafeStopReceiptValidation(
        status=(
            SafeStopReceiptValidationStatus.VERIFIED
            if verified
            else SafeStopReceiptValidationStatus.UNVERIFIED
        ),
        verification_basis=(
            VerificationBasis.DETERMINISTIC
            if verified
            else VerificationBasis.UNVERIFIED
        ),
        reasons=tuple(reasons),
        stop_capability_evidenced=verified,
        physical_execution_invoked=(
            receipt.physical_execution_invoked if verified else False
        ),
    )


__all__ = [
    "SAFE_STOP_ACK_EVIDENCE_KIND",
    "SAFE_STOP_ACK_ITEM_ID",
    "SAFE_STOP_BOUNDS_ITEM_ID",
    "SAFE_STOP_EFFECT_EVIDENCE_KIND",
    "SAFE_STOP_EFFECT_ITEM_ID",
    "SAFE_STOP_EXERCISE_APPROVAL_EVIDENCE_KIND",
    "SAFE_STOP_EXERCISE_RECEIPT_SCHEMA_VERSION",
    "SAFE_STOP_POST_STATE_EVIDENCE_KIND",
    "SAFE_STOP_PRE_STATE_EVIDENCE_KIND",
    "SAFE_STOP_REQUEST_EVIDENCE_KIND",
    "SAFE_STOP_REQUEST_ITEM_ID",
    "SAFE_STOP_REQUIRED_ITEM_IDS",
    "SAFE_STOP_RUNTIME_INVOCATION_EVIDENCE_KIND",
    "SafeStopExerciseReceipt",
    "SafeStopFreshnessPolicy",
    "SafeStopReceiptValidation",
    "SafeStopReceiptValidationStatus",
    "SafeStopValidationContext",
    "validate_safe_stop_exercise_receipt",
]

"""Backend-neutral registry, dispatch runner, and verifier for hardware adapters.

The registry knows adapter factories. The runner knows only the common
``HardwareAdapter`` protocol and immutable contract artifacts. Approval remains
an input from the Gateway; adapters neither mint approval nor issue the final
verification verdict.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.hardware_adapter_contract import (
    HardwareAckStatus,
    HardwareActionKind,
    HardwareAdapter,
    HardwareAdapterCapabilities,
    HardwareAdapterEvidence,
    HardwareDispatchCandidate,
    HardwareOperatorApproval,
)
from src.runtime.runtime_claim_evidence import (
    RuntimeClaimValidationError,
    validate_runtime_invocation_evidence,
)


HARDWARE_ADAPTER_RUNTIME_REQUEST_SCHEMA_VERSION = (
    "missionos_hardware_adapter_runtime_request.v1"
)
HARDWARE_ADAPTER_PREPARATION_SCHEMA_VERSION = (
    "missionos_hardware_adapter_preparation.v1"
)
HARDWARE_ADAPTER_RUNTIME_RESULT_SCHEMA_VERSION = (
    "missionos_hardware_adapter_runtime_result.v1"
)
HARDWARE_ADAPTER_VERIFICATION_SCHEMA_VERSION = (
    "missionos_hardware_adapter_verification.v1"
)
HARDWARE_ADAPTER_APPROVAL_TTL = timedelta(minutes=2)


class HardwareAdapterRegistryError(ValueError):
    """Raised when adapter registration or resolution fails closed."""


class HardwareAdapterRuntimeError(ValueError):
    """Raised when a generic adapter runtime request violates scope."""


class HardwareAdapterVerificationStatus(str, Enum):
    VERIFIED = "verified"
    BLOCKED = "blocked"
    UNVERIFIED = "unverified"
    FAILED = "failed"


class HardwareAdapterRuntimeRequest(BaseModel):
    """Backend-neutral request handed to a registered adapter factory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "missionos_hardware_adapter_runtime_request.v1"
    ] = HARDWARE_ADAPTER_RUNTIME_REQUEST_SCHEMA_VERSION
    adapter_id: str
    missionos_action_ref: str
    action_kind: HardwareActionKind
    adapter_parameters: dict[str, Any] = Field(default_factory=dict)
    execution_mode: str = "sim"
    opt_in: bool = False
    telemetry_fresh: bool
    heartbeat_alive: bool
    geofence_satisfied: bool
    operating_volume_satisfied: bool


class HardwareAdapterPreparation(BaseModel):
    """Prepared candidate. This artifact sends nothing and grants no authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "missionos_hardware_adapter_preparation.v1"
    ] = HARDWARE_ADAPTER_PREPARATION_SCHEMA_VERSION
    preparation_ref: str
    preparation_sha256: str
    request: dict[str, Any]
    capabilities: dict[str, Any]
    preflight: dict[str, Any]
    dispatch_candidate: dict[str, Any]
    dispatch_request_sent: bool = False
    dispatch_authority_created: bool = False
    physical_execution_invoked: bool = False


class HardwareAdapterVerificationVerdict(BaseModel):
    """Core-owned verdict over adapter evidence and runtime invocation evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "missionos_hardware_adapter_verification.v1"
    ] = HARDWARE_ADAPTER_VERIFICATION_SCHEMA_VERSION
    verification_status: HardwareAdapterVerificationStatus
    adapter_id: str
    missionos_action_ref: str
    action_kind: HardwareActionKind
    adapter_evidence_sha256: str
    approval_scope_valid: bool
    runtime_invocation_evidence_present: bool
    runtime_invocation_evidence_valid: bool
    command_ack_observed: bool
    runtime_state_observed: bool
    runtime_progress_observed: bool
    adapter_action_verified: bool
    completion_claimed: bool
    completion_scope: str
    physical_execution_invoked: bool
    blocking_reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class HardwareAdapterRuntimeResult(BaseModel):
    """One generic dispatch attempt with facts kept in separate stages."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "missionos_hardware_adapter_runtime_result.v1"
    ] = HARDWARE_ADAPTER_RUNTIME_RESULT_SCHEMA_VERSION
    preparation: dict[str, Any]
    operator_approval: dict[str, Any] | None
    adapter_evidence: dict[str, Any] | None
    runtime_invocation_evidence: tuple[dict[str, Any], ...] = ()
    verification_verdict: dict[str, Any]
    dispatch_status: Literal["blocked", "sent", "unknown"]
    dispatch_request_sent: bool | None
    command_ack_observed: bool
    runtime_progress_observed: bool
    completion_claimed: bool
    physical_execution_invoked: bool


AdapterFactory = Callable[
    [HardwareAdapterRuntimeRequest, HardwareOperatorApproval | None], HardwareAdapter
]


class HardwareAdapterRegistry:
    """Explicit adapter-id to factory registry with no authority semantics."""

    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, adapter_id: str, factory: AdapterFactory) -> None:
        normalized = str(adapter_id).strip()
        if not normalized:
            raise HardwareAdapterRegistryError("adapter_id_required")
        if normalized in self._factories:
            raise HardwareAdapterRegistryError(f"adapter_already_registered:{normalized}")
        self._factories[normalized] = factory

    def resolve(self, adapter_id: str) -> AdapterFactory:
        normalized = str(adapter_id).strip()
        try:
            return self._factories[normalized]
        except KeyError as exc:
            raise HardwareAdapterRegistryError(
                f"adapter_not_registered:{normalized or '<empty>'}"
            ) from exc

    def registered_adapter_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _preparation_payload(
    *,
    request: HardwareAdapterRuntimeRequest,
    capabilities: HardwareAdapterCapabilities,
    preflight: Any,
    candidate: HardwareDispatchCandidate,
) -> dict[str, Any]:
    return {
        "request": request.model_dump(mode="json"),
        "capabilities": capabilities.model_dump(mode="json"),
        "preflight": preflight.model_dump(mode="json"),
        "dispatch_candidate": candidate.model_dump(mode="json"),
    }


def prepare_hardware_adapter_action(
    *,
    registry: HardwareAdapterRegistry,
    request: HardwareAdapterRuntimeRequest,
) -> HardwareAdapterPreparation:
    """Resolve and prepare one adapter action without approval or dispatch."""

    adapter = registry.resolve(request.adapter_id)(request, None)
    capabilities = adapter.capabilities()
    preflight = adapter.preflight_check()
    candidate = adapter.propose_dispatch()
    if not (
        capabilities.adapter_id == request.adapter_id
        and preflight.adapter_id == request.adapter_id
        and candidate.adapter_id == request.adapter_id
    ):
        raise HardwareAdapterRuntimeError("adapter_identity_mismatch")
    if candidate.adapter_action_kind is not request.action_kind:
        raise HardwareAdapterRuntimeError("adapter_action_kind_mismatch")
    if candidate.missionos_action_ref != request.missionos_action_ref:
        raise HardwareAdapterRuntimeError("adapter_action_ref_mismatch")
    payload = _preparation_payload(
        request=request,
        capabilities=capabilities,
        preflight=preflight,
        candidate=candidate,
    )
    digest = _canonical_sha256(payload)
    return HardwareAdapterPreparation(
        preparation_ref=f"hardware_adapter_preparation:{digest[:16]}",
        preparation_sha256=digest,
        **payload,
    )


def validate_hardware_operator_approval_scope(
    *,
    request: HardwareAdapterRuntimeRequest,
    preparation: HardwareAdapterPreparation,
    approval: HardwareOperatorApproval | None,
) -> tuple[str, ...]:
    """Return deterministic blocking reasons for missing or mismatched approval."""

    if approval is None:
        return ("operator_approval_missing",)
    reasons: list[str] = []
    if approval.approved_adapter_id != request.adapter_id:
        reasons.append("operator_approval_adapter_mismatch")
    if approval.approved_preparation_ref != preparation.preparation_ref:
        reasons.append("operator_approval_preparation_ref_mismatch")
    if approval.approved_preparation_sha256 != preparation.preparation_sha256:
        reasons.append("operator_approval_preparation_sha256_mismatch")
    if approval.approved_action_ref != request.missionos_action_ref:
        reasons.append("operator_approval_action_ref_mismatch")
    if approval.approved_action_kind is not request.action_kind:
        reasons.append("operator_approval_action_kind_mismatch")
    if not approval.operator_approval_ref.strip() or not approval.approval_actor.strip():
        reasons.append("operator_approval_identity_missing")
    now = datetime.now(timezone.utc)
    approved_at = approval.approval_timestamp
    if approved_at.tzinfo is None:
        reasons.append("operator_approval_timestamp_timezone_missing")
        approved_at = approved_at.replace(tzinfo=timezone.utc)
    else:
        approved_at = approved_at.astimezone(timezone.utc)
    expires_at = approval.approval_expires_at
    if expires_at is None:
        reasons.append("operator_approval_expiry_missing")
    else:
        if expires_at.tzinfo is None:
            reasons.append("operator_approval_expiry_timezone_missing")
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = expires_at.astimezone(timezone.utc)
        if expires_at <= approved_at:
            reasons.append("operator_approval_expiry_not_after_approval")
        if expires_at - approved_at > HARDWARE_ADAPTER_APPROVAL_TTL:
            reasons.append("operator_approval_expiry_window_too_long")
        if now >= expires_at:
            reasons.append("operator_approval_stale")
    if approved_at > now:
        reasons.append("operator_approval_timestamp_in_future")
    if now - approved_at > HARDWARE_ADAPTER_APPROVAL_TTL:
        reasons.append("operator_approval_stale")
    return tuple(dict.fromkeys(reasons))


def verify_hardware_adapter_outcome(
    *,
    request: HardwareAdapterRuntimeRequest,
    approval_scope_valid: bool,
    evidence: HardwareAdapterEvidence | None,
    runtime_invocation_evidence: Sequence[Mapping[str, Any]] = (),
    preparation: HardwareAdapterPreparation | None = None,
    operator_approval_ref: str | None = None,
    prior_blocking_reasons: Sequence[str] = (),
) -> HardwareAdapterVerificationVerdict:
    """Verify adapter-action scope without promoting it to mission completion."""

    reasons = list(prior_blocking_reasons)
    limitations: list[str] = [
        "adapter_action_verdict_not_mission_completion",
        "adapter_action_verdict_not_delivery_completion",
    ]
    validated_invocations: list[dict[str, Any]] = []
    for invocation in runtime_invocation_evidence:
        try:
            validated_invocations.append(validate_runtime_invocation_evidence(invocation))
        except RuntimeClaimValidationError as exc:
            reasons.append(f"runtime_invocation_evidence_invalid:{exc}")

    if validated_invocations:
        if preparation is None or not operator_approval_ref:
            reasons.append("runtime_invocation_source_binding_context_missing")
        else:
            expected_binding = {
                "missionos_adapter_id": request.adapter_id,
                "missionos_action_ref": request.missionos_action_ref,
                "missionos_preparation_ref": preparation.preparation_ref,
                "missionos_preparation_sha256": preparation.preparation_sha256,
                "operator_approval_ref": operator_approval_ref,
            }
            for invocation in validated_invocations:
                for field, expected_value in expected_binding.items():
                    if invocation.get(field) != expected_value:
                        reasons.append(f"runtime_invocation_{field}_mismatch")

    evidence_payload = evidence.model_dump(mode="json") if evidence else {}
    if evidence is None:
        reasons.append("adapter_evidence_missing")
    else:
        if evidence.adapter_id != request.adapter_id:
            reasons.append("adapter_evidence_adapter_mismatch")
        if evidence.missionos_action_ref != request.missionos_action_ref:
            reasons.append("adapter_evidence_action_ref_mismatch")
        if evidence.adapter_action_kind is not request.action_kind:
            reasons.append("adapter_evidence_action_kind_mismatch")
    if not approval_scope_valid:
        reasons.append("operator_approval_scope_invalid")

    invocation_present = bool(runtime_invocation_evidence)
    invocation_valid = invocation_present and (
        len(validated_invocations) == len(runtime_invocation_evidence)
    )
    if evidence and evidence.dispatch_request_sent and not invocation_present:
        reasons.append("runtime_invocation_evidence_missing")

    ack = bool(evidence and evidence.command_ack_observed)
    state = bool(evidence and evidence.runtime_state_observed)
    progress = bool(evidence and evidence.runtime_progress_observed)
    verified = bool(
        evidence
        and approval_scope_valid
        and evidence.dispatch_request_sent
        and evidence.ack_status is HardwareAckStatus.ACCEPTED
        and ack
        and state
        and progress
        and evidence.completion_claimed
        and invocation_valid
        and not reasons
    )
    if ack and not progress:
        limitations.append("ack_is_not_runtime_progress_or_success")
    if evidence and evidence.completion_claimed and not verified:
        limitations.append("adapter_local_completion_not_promoted_by_verifier")

    if verified:
        status = HardwareAdapterVerificationStatus.VERIFIED
    elif evidence is None or not evidence.dispatch_request_sent:
        status = HardwareAdapterVerificationStatus.BLOCKED
    elif reasons:
        status = HardwareAdapterVerificationStatus.UNVERIFIED
    else:
        status = HardwareAdapterVerificationStatus.FAILED

    return HardwareAdapterVerificationVerdict(
        verification_status=status,
        adapter_id=request.adapter_id,
        missionos_action_ref=request.missionos_action_ref,
        action_kind=request.action_kind,
        adapter_evidence_sha256=_canonical_sha256(evidence_payload),
        approval_scope_valid=approval_scope_valid,
        runtime_invocation_evidence_present=invocation_present,
        runtime_invocation_evidence_valid=invocation_valid,
        command_ack_observed=ack,
        runtime_state_observed=state,
        runtime_progress_observed=progress,
        adapter_action_verified=verified,
        completion_claimed=verified,
        completion_scope=(evidence.completion_scope if verified and evidence else "none"),
        physical_execution_invoked=bool(
            verified and evidence and evidence.physical_execution_invoked
        ),
        blocking_reasons=tuple(dict.fromkeys(reasons)),
        limitations=tuple(dict.fromkeys(limitations)),
    )


def _unknown_dispatch_result(
    *,
    request: HardwareAdapterRuntimeRequest,
    preparation: HardwareAdapterPreparation,
    approval: HardwareOperatorApproval,
    exc: Exception,
) -> HardwareAdapterRuntimeResult:
    reason = f"adapter_dispatch_failed:{type(exc).__name__}"
    verdict = verify_hardware_adapter_outcome(
        request=request,
        approval_scope_valid=True,
        evidence=None,
        prior_blocking_reasons=(reason, "dispatch_state_unknown"),
    )
    return HardwareAdapterRuntimeResult(
        preparation=preparation.model_dump(mode="json"),
        operator_approval=approval.model_dump(mode="json"),
        adapter_evidence=None,
        verification_verdict=verdict.model_dump(mode="json"),
        dispatch_status="unknown",
        dispatch_request_sent=None,
        command_ack_observed=False,
        runtime_progress_observed=False,
        completion_claimed=False,
        physical_execution_invoked=False,
    )


def _blocked_adapter_factory_result(
    *,
    request: HardwareAdapterRuntimeRequest,
    preparation: HardwareAdapterPreparation,
    approval: HardwareOperatorApproval,
    exc: Exception,
) -> HardwareAdapterRuntimeResult:
    verdict = verify_hardware_adapter_outcome(
        request=request,
        approval_scope_valid=True,
        evidence=None,
        prior_blocking_reasons=(
            f"adapter_factory_failed:{type(exc).__name__}",
            "dispatch_not_attempted",
        ),
    )
    return HardwareAdapterRuntimeResult(
        preparation=preparation.model_dump(mode="json"),
        operator_approval=approval.model_dump(mode="json"),
        adapter_evidence=None,
        verification_verdict=verdict.model_dump(mode="json"),
        dispatch_status="blocked",
        dispatch_request_sent=False,
        command_ack_observed=False,
        runtime_progress_observed=False,
        completion_claimed=False,
        physical_execution_invoked=False,
    )


def dispatch_hardware_adapter_action(
    *,
    registry: HardwareAdapterRegistry,
    preparation: HardwareAdapterPreparation,
    approval: HardwareOperatorApproval | None,
) -> HardwareAdapterRuntimeResult:
    """Dispatch through a registered adapter and apply the generic verifier."""

    request = HardwareAdapterRuntimeRequest.model_validate(preparation.request)
    approval_reasons = validate_hardware_operator_approval_scope(
        request=request,
        preparation=preparation,
        approval=approval,
    )
    if approval_reasons:
        verdict = verify_hardware_adapter_outcome(
            request=request,
            approval_scope_valid=False,
            evidence=None,
            prior_blocking_reasons=approval_reasons,
        )
        return HardwareAdapterRuntimeResult(
            preparation=preparation.model_dump(mode="json"),
            operator_approval=(approval.model_dump(mode="json") if approval else None),
            adapter_evidence=None,
            verification_verdict=verdict.model_dump(mode="json"),
            dispatch_status="blocked",
            dispatch_request_sent=False,
            command_ack_observed=False,
            runtime_progress_observed=False,
            completion_claimed=False,
            physical_execution_invoked=False,
        )

    try:
        expected = prepare_hardware_adapter_action(registry=registry, request=request)
    except Exception as exc:
        return _blocked_adapter_factory_result(
            request=request,
            preparation=preparation,
            approval=approval,
            exc=exc,
        )
    if (
        expected.preparation_ref != preparation.preparation_ref
        or expected.preparation_sha256 != preparation.preparation_sha256
    ):
        raise HardwareAdapterRuntimeError("preparation_hash_mismatch")

    try:
        adapter = registry.resolve(request.adapter_id)(request, approval)
    except Exception as exc:
        return _blocked_adapter_factory_result(
            request=request,
            preparation=preparation,
            approval=approval,
            exc=exc,
        )
    adapter_approval_reasons = adapter.validate_operator_approval(approval)
    if adapter_approval_reasons:
        verdict = verify_hardware_adapter_outcome(
            request=request,
            approval_scope_valid=False,
            evidence=None,
            prior_blocking_reasons=adapter_approval_reasons,
        )
        return HardwareAdapterRuntimeResult(
            preparation=preparation.model_dump(mode="json"),
            operator_approval=approval.model_dump(mode="json"),
            adapter_evidence=None,
            verification_verdict=verdict.model_dump(mode="json"),
            dispatch_status="blocked",
            dispatch_request_sent=False,
            command_ack_observed=False,
            runtime_progress_observed=False,
            completion_claimed=False,
            physical_execution_invoked=False,
        )
    try:
        evidence = adapter.dispatch_approved_action()
    except Exception as exc:  # adapter boundary: dispatch may already have escaped
        return _unknown_dispatch_result(
            request=request,
            preparation=preparation,
            approval=approval,
            exc=exc,
        )
    invocation_supplier = getattr(adapter, "collect_runtime_invocation_evidence", None)
    invocations: tuple[dict[str, Any], ...] = ()
    if callable(invocation_supplier):
        invocations = tuple(dict(item) for item in invocation_supplier())
    verdict = verify_hardware_adapter_outcome(
        request=request,
        approval_scope_valid=True,
        evidence=evidence,
        runtime_invocation_evidence=invocations,
        preparation=preparation,
        operator_approval_ref=approval.operator_approval_ref,
    )
    return HardwareAdapterRuntimeResult(
        preparation=preparation.model_dump(mode="json"),
        operator_approval=approval.model_dump(mode="json") if approval else None,
        adapter_evidence=evidence.model_dump(mode="json"),
        runtime_invocation_evidence=invocations,
        verification_verdict=verdict.model_dump(mode="json"),
        dispatch_status=("sent" if evidence.dispatch_request_sent else "blocked"),
        dispatch_request_sent=evidence.dispatch_request_sent,
        command_ack_observed=evidence.command_ack_observed,
        runtime_progress_observed=evidence.runtime_progress_observed,
        completion_claimed=verdict.completion_claimed,
        physical_execution_invoked=verdict.physical_execution_invoked,
    )


def abort_hardware_adapter_action(
    *,
    registry: HardwareAdapterRegistry,
    preparation: HardwareAdapterPreparation,
    approval: HardwareOperatorApproval | None,
) -> HardwareAdapterRuntimeResult:
    """Request adapter safe-stop without treating it as successful completion."""

    request = HardwareAdapterRuntimeRequest.model_validate(preparation.request)
    approval_reasons = validate_hardware_operator_approval_scope(
        request=request,
        preparation=preparation,
        approval=approval,
    )
    if approval_reasons:
        evidence = None
    else:
        adapter = registry.resolve(request.adapter_id)(request, approval)
        evidence = adapter.abort_or_safe_stop()
    verdict = verify_hardware_adapter_outcome(
        request=request,
        approval_scope_valid=not approval_reasons,
        evidence=evidence,
        prior_blocking_reasons=(*approval_reasons, "abort_or_safe_stop_requested"),
    )
    return HardwareAdapterRuntimeResult(
        preparation=preparation.model_dump(mode="json"),
        operator_approval=approval.model_dump(mode="json") if approval else None,
        adapter_evidence=evidence.model_dump(mode="json") if evidence else None,
        verification_verdict=verdict.model_dump(mode="json"),
        dispatch_status="blocked",
        dispatch_request_sent=False,
        command_ack_observed=False,
        runtime_progress_observed=False,
        completion_claimed=False,
        physical_execution_invoked=False,
    )


__all__ = [
    "AdapterFactory",
    "HardwareAdapterPreparation",
    "HardwareAdapterRegistry",
    "HardwareAdapterRegistryError",
    "HardwareAdapterRuntimeError",
    "HardwareAdapterRuntimeRequest",
    "HardwareAdapterRuntimeResult",
    "HardwareAdapterVerificationStatus",
    "HardwareAdapterVerificationVerdict",
    "HARDWARE_ADAPTER_APPROVAL_TTL",
    "abort_hardware_adapter_action",
    "dispatch_hardware_adapter_action",
    "prepare_hardware_adapter_action",
    "validate_hardware_operator_approval_scope",
    "verify_hardware_adapter_outcome",
]

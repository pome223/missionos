from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from missionos_core import (
    SAFE_STOP_ACK_EVIDENCE_KIND,
    SAFE_STOP_ACK_ITEM_ID,
    SAFE_STOP_BOUNDS_ITEM_ID,
    SAFE_STOP_EFFECT_EVIDENCE_KIND,
    SAFE_STOP_EFFECT_ITEM_ID,
    SAFE_STOP_EXERCISE_APPROVAL_EVIDENCE_KIND,
    SAFE_STOP_POST_STATE_EVIDENCE_KIND,
    SAFE_STOP_PRE_STATE_EVIDENCE_KIND,
    SAFE_STOP_REQUEST_EVIDENCE_KIND,
    SAFE_STOP_REQUEST_ITEM_ID,
    SAFE_STOP_REQUIRED_ITEM_IDS,
    SAFE_STOP_RUNTIME_INVOCATION_EVIDENCE_KIND,
    EnvelopeBoundKind,
    EnvelopeEnforcementLocation,
    EnvelopeLimitPolicy,
    EnvelopeLimitReadback,
    EnvelopeLimitType,
    EnvelopeUnit,
    EXECUTION_ENVELOPE_READBACK_EVIDENCE_KIND,
    EvidenceOrigin,
    EvidenceSourceRef,
    ExecutionEnvelopeDescriptor,
    ExecutionEnvelopePolicy,
    ExecutionEnvelopeValidationContext,
    FeasibilityStatus,
    HardwareExecutionMode,
    SafeStopExerciseReceipt,
    SafeStopFreshnessPolicy,
    SafeStopValidationContext,
    VerificationBasis,
    VerificationItem,
    VerificationItemStatus,
    validate_execution_envelope,
)


BASE_TIME = datetime(2026, 7, 26, 3, 0, 0, tzinfo=timezone.utc)


def _iso(offset_seconds: float) -> str:
    return (BASE_TIME + timedelta(seconds=offset_seconds)).isoformat()


def _source(
    source_id: str,
    evidence_kind: str,
    offset_seconds: float,
    digest_character: str,
    *,
    scope: HardwareExecutionMode = HardwareExecutionMode.LOOPBACK,
    origin: EvidenceOrigin = EvidenceOrigin.MACHINE_OBSERVED,
    deadline_offset_seconds: float | None = None,
) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_id=source_id,
        evidence_kind=evidence_kind,
        observed_at=_iso(offset_seconds),
        freshness_deadline=(
            _iso(deadline_offset_seconds)
            if deadline_offset_seconds is not None
            else None
        ),
        content_sha256=digest_character * 64,
        execution_scope=scope,
        origin=origin,
    )


def _safe_stop_fixture() -> tuple[
    SafeStopExerciseReceipt,
    SafeStopValidationContext,
]:
    policy = SafeStopFreshnessPolicy(
        policy_id="safe-stop-policy",
        policy_version="1",
        maximum_age_seconds=300.0,
    )
    sources = {
        source.source_id: source
        for source in (
            _source(
                "stop:approval",
                SAFE_STOP_EXERCISE_APPROVAL_EVIDENCE_KIND,
                -0.1,
                "0",
                origin=EvidenceOrigin.AUTHORITY_ARTIFACT,
            ),
            _source("stop:pre", SAFE_STOP_PRE_STATE_EVIDENCE_KIND, 0.0, "a"),
            _source(
                "stop:request",
                SAFE_STOP_REQUEST_EVIDENCE_KIND,
                0.1,
                "b",
            ),
            _source(
                "stop:runtime",
                SAFE_STOP_RUNTIME_INVOCATION_EVIDENCE_KIND,
                0.1,
                "c",
            ),
            _source("stop:ack", SAFE_STOP_ACK_EVIDENCE_KIND, 0.2, "d"),
            _source("stop:effect", SAFE_STOP_EFFECT_EVIDENCE_KIND, 0.3, "e"),
            _source("stop:post", SAFE_STOP_POST_STATE_EVIDENCE_KIND, 0.4, "f"),
        )
    }
    receipt = SafeStopExerciseReceipt(
        receipt_id="safe-stop:loopback",
        adapter_id="fixture.adapter",
        adapter_version="1",
        controller_configuration_sha256="1" * 64,
        safety_configuration_sha256="2" * 64,
        stop_mechanism="loopback.cancel",
        exercise_recipe_id="bounded-stop",
        exercise_recipe_version="1",
        exercise_recipe_sha256="3" * 64,
        exercise_approval_ref="stop:approval",
        execution_scope=HardwareExecutionMode.LOOPBACK,
        observed_at=_iso(0.4),
        freshness_deadline=_iso(300.4),
        policy_binding=policy.binding,
        verification_items=(
            VerificationItem(
                item_id=SAFE_STOP_BOUNDS_ITEM_ID,
                predicate="exercise stayed inside its bounds",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("stop:pre", "stop:post"),
            ),
            VerificationItem(
                item_id=SAFE_STOP_REQUEST_ITEM_ID,
                predicate="stop request was invoked",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("stop:request", "stop:runtime"),
            ),
            VerificationItem(
                item_id=SAFE_STOP_ACK_ITEM_ID,
                predicate="stop request was acknowledged",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("stop:ack",),
            ),
            VerificationItem(
                item_id=SAFE_STOP_EFFECT_ITEM_ID,
                predicate="independent observer measured stopped state",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("stop:effect", "stop:post"),
            ),
        ),
        required_verification_item_ids=SAFE_STOP_REQUIRED_ITEM_IDS,
        pre_state_evidence_ref="stop:pre",
        request_evidence_ref="stop:request",
        ack_evidence_ref="stop:ack",
        observed_effect_evidence_ref="stop:effect",
        post_state_evidence_ref="stop:post",
        runtime_invocation_evidence_ref="stop:runtime",
    )
    context = SafeStopValidationContext(
        expected_execution_scope=HardwareExecutionMode.LOOPBACK,
        adapter_id=receipt.adapter_id,
        adapter_version=receipt.adapter_version,
        controller_configuration_sha256=(
            receipt.controller_configuration_sha256
        ),
        safety_configuration_sha256=receipt.safety_configuration_sha256,
        active_policy=policy,
        evidence_sources=sources,
        evaluated_at=_iso(1.0),
    )
    return receipt, context


def _policy() -> ExecutionEnvelopePolicy:
    return ExecutionEnvelopePolicy(
        policy_id="execution-envelope",
        policy_version="1",
        execution_scope=HardwareExecutionMode.LOOPBACK,
        maximum_readback_age_seconds=30.0,
        limits=(
            EnvelopeLimitPolicy(
                limit_id="max_linear_speed",
                limit_type=EnvelopeLimitType.LINEAR_SPEED,
                unit=EnvelopeUnit.METRES_PER_SECOND,
                bound_kind=EnvelopeBoundKind.MAXIMUM,
                enforcement_location=EnvelopeEnforcementLocation.CONTROLLER,
                upper_bound=0.25,
            ),
            EnvelopeLimitPolicy(
                limit_id="minimum_separation",
                limit_type=EnvelopeLimitType.SEPARATION,
                unit=EnvelopeUnit.METRE,
                bound_kind=EnvelopeBoundKind.MINIMUM,
                enforcement_location=(
                    EnvelopeEnforcementLocation.EXTERNAL_SAFETY_MONITOR
                ),
                lower_bound=0.5,
            ),
        ),
    )


def _fixture() -> tuple[
    ExecutionEnvelopeDescriptor,
    ExecutionEnvelopeValidationContext,
]:
    policy = _policy()
    receipt, stop_context = _safe_stop_fixture()
    sources = {
        "readback:speed": _source(
            "readback:speed",
            EXECUTION_ENVELOPE_READBACK_EVIDENCE_KIND,
            0.5,
            "4",
            deadline_offset_seconds=30.5,
        ),
        "readback:separation": _source(
            "readback:separation",
            EXECUTION_ENVELOPE_READBACK_EVIDENCE_KIND,
            0.5,
            "5",
            deadline_offset_seconds=30.5,
        ),
    }
    descriptor = ExecutionEnvelopeDescriptor(
        envelope_id="envelope:loopback",
        adapter_id="fixture.adapter",
        adapter_version="1",
        controller_configuration_sha256="1" * 64,
        safety_configuration_sha256="2" * 64,
        execution_scope=HardwareExecutionMode.LOOPBACK,
        policy_binding=policy.binding,
        safe_stop_receipt_ref=receipt.receipt_id,
        readbacks=(
            EnvelopeLimitReadback(
                limit_id="max_linear_speed",
                unit=EnvelopeUnit.METRES_PER_SECOND,
                enforcement_location=EnvelopeEnforcementLocation.CONTROLLER,
                evidence_ref="readback:speed",
                upper_bound=0.2,
            ),
            EnvelopeLimitReadback(
                limit_id="minimum_separation",
                unit=EnvelopeUnit.METRE,
                enforcement_location=(
                    EnvelopeEnforcementLocation.EXTERNAL_SAFETY_MONITOR
                ),
                evidence_ref="readback:separation",
                lower_bound=0.6,
            ),
        ),
    )
    context = ExecutionEnvelopeValidationContext(
        expected_execution_scope=HardwareExecutionMode.LOOPBACK,
        adapter_id=descriptor.adapter_id,
        adapter_version=descriptor.adapter_version,
        controller_configuration_sha256=(
            descriptor.controller_configuration_sha256
        ),
        safety_configuration_sha256=descriptor.safety_configuration_sha256,
        active_policy=policy,
        evidence_sources=sources,
        safe_stop_receipt=receipt,
        safe_stop_context=stop_context,
        evaluated_at=_iso(1.0),
    )
    return descriptor, context


def test_policy_owned_bounds_and_valid_stop_receipt_verify() -> None:
    descriptor, context = _fixture()

    validation = validate_execution_envelope(descriptor, context=context)

    assert validation.status is FeasibilityStatus.VERIFIED_FEASIBLE
    assert validation.verification_basis is VerificationBasis.DETERMINISTIC
    assert validation.reasons == ()
    assert validation.policy_binding == context.active_policy.binding
    assert validation.approval_created is False
    assert validation.dispatch_authority_created is False
    assert validation.task_completion_claimed is False
    assert validation.physical_execution_invoked is False


def test_readback_cannot_widen_policy_bound() -> None:
    descriptor, context = _fixture()
    speed = replace(descriptor.readbacks[0], upper_bound=0.3)

    validation = validate_execution_envelope(
        replace(descriptor, readbacks=(speed, descriptor.readbacks[1])),
        context=context,
    )

    assert validation.status is FeasibilityStatus.BLOCKED
    assert (
        "execution_envelope_readback_exceeds_policy:max_linear_speed"
        in validation.reasons
    )


def test_more_restrictive_readback_does_not_mutate_policy() -> None:
    descriptor, context = _fixture()
    speed = replace(descriptor.readbacks[0], upper_bound=0.1)

    validation = validate_execution_envelope(
        replace(descriptor, readbacks=(speed, descriptor.readbacks[1])),
        context=context,
    )

    assert validation.status is FeasibilityStatus.VERIFIED_FEASIBLE
    assert validation.policy_binding == _policy().binding
    assert context.active_policy.limits[0].upper_bound == 0.25


def test_policy_digest_drift_is_unverified() -> None:
    descriptor, context = _fixture()

    validation = validate_execution_envelope(
        replace(
            descriptor,
            policy_binding=replace(
                descriptor.policy_binding,
                policy_sha256="9" * 64,
            ),
        ),
        context=context,
    )

    assert validation.status is FeasibilityStatus.UNVERIFIED
    assert "execution_envelope_policy_binding_mismatch" in validation.reasons


def test_declaration_only_readback_is_unverified() -> None:
    descriptor, context = _fixture()
    sources = dict(context.evidence_sources)
    sources["readback:speed"] = replace(
        sources["readback:speed"],
        origin=EvidenceOrigin.OPERATOR_DECLARED,
    )

    validation = validate_execution_envelope(
        descriptor,
        context=replace(context, evidence_sources=sources),
    )

    assert validation.status is FeasibilityStatus.UNVERIFIED
    assert (
        "execution_envelope_readback_origin_unverified:max_linear_speed"
        in validation.reasons
    )


def test_declaration_only_excess_is_not_promoted_to_observed_contradiction() -> None:
    descriptor, context = _fixture()
    speed = replace(descriptor.readbacks[0], upper_bound=100.0)
    sources = dict(context.evidence_sources)
    sources["readback:speed"] = replace(
        sources["readback:speed"],
        origin=EvidenceOrigin.OPERATOR_DECLARED,
    )

    validation = validate_execution_envelope(
        replace(descriptor, readbacks=(speed, descriptor.readbacks[1])),
        context=replace(context, evidence_sources=sources),
    )

    assert validation.status is FeasibilityStatus.UNVERIFIED
    assert (
        "execution_envelope_readback_exceeds_policy:max_linear_speed"
        not in validation.reasons
    )


def test_readback_freshness_deadline_must_be_policy_derived() -> None:
    descriptor, context = _fixture()
    sources = dict(context.evidence_sources)
    sources["readback:speed"] = replace(
        sources["readback:speed"],
        freshness_deadline=_iso(300.5),
    )

    validation = validate_execution_envelope(
        descriptor,
        context=replace(context, evidence_sources=sources),
    )

    assert (
        "execution_envelope_readback_deadline_not_policy_derived:"
        "max_linear_speed"
    ) in validation.reasons


def test_missing_and_stale_readbacks_are_unverified() -> None:
    descriptor, context = _fixture()
    missing = validate_execution_envelope(
        replace(descriptor, readbacks=(descriptor.readbacks[0],)),
        context=context,
    )
    stale = validate_execution_envelope(
        descriptor,
        context=replace(context, evaluated_at=_iso(31.0)),
    )

    assert missing.status is FeasibilityStatus.UNVERIFIED
    assert (
        "execution_envelope_readback_missing:minimum_separation"
        in missing.reasons
    )
    assert stale.status is FeasibilityStatus.UNVERIFIED
    assert (
        "execution_envelope_readback_stale:max_linear_speed"
        in stale.reasons
    )


def test_scope_transfer_is_unverified_in_every_direction() -> None:
    descriptor, context = _fixture()
    sources = dict(context.evidence_sources)
    sources["readback:speed"] = replace(
        sources["readback:speed"],
        execution_scope=HardwareExecutionMode.SIM,
    )

    validation = validate_execution_envelope(
        descriptor,
        context=replace(context, evidence_sources=sources),
    )

    assert validation.status is FeasibilityStatus.UNVERIFIED
    assert (
        "execution_envelope_readback_scope_mismatch:max_linear_speed"
        in validation.reasons
    )


def test_missing_or_expired_stop_receipt_is_unverified() -> None:
    descriptor, context = _fixture()

    missing = validate_execution_envelope(
        replace(descriptor, safe_stop_receipt_ref="safe-stop:other"),
        context=context,
    )
    expired = validate_execution_envelope(
        descriptor,
        context=replace(
            context,
            safe_stop_context=replace(
                context.safe_stop_context,
                evaluated_at=_iso(301.0),
            ),
        ),
    )

    assert missing.status is FeasibilityStatus.UNVERIFIED
    assert "execution_envelope_safe_stop_receipt_ref_mismatch" in missing.reasons
    assert expired.status is FeasibilityStatus.UNVERIFIED
    assert "execution_envelope_safe_stop_receipt_unverified" in expired.reasons


def test_unknown_unit_and_adapter_advertisement_fail_closed() -> None:
    descriptor, context = _fixture()
    payload = descriptor.to_dict()
    payload["readbacks"][0]["unit"] = "adapter_units"
    payload["readbacks"][0]["advertised_max"] = 100.0

    validation = validate_execution_envelope(
        ExecutionEnvelopeDescriptor.from_dict(payload),
        context=context,
    )

    assert validation.status is FeasibilityStatus.UNVERIFIED
    assert (
        "execution_envelope_readback_unit_mismatch:max_linear_speed"
        in validation.reasons
    )
    assert (
        "execution_envelope_readback_unknown_fields:max_linear_speed"
        in validation.reasons
    )


def test_unknown_policy_field_and_configuration_drift_fail_closed() -> None:
    descriptor, context = _fixture()
    policy_payload = context.active_policy.to_material()
    policy_payload["adapter_suggested_max"] = 100.0
    unknown_policy = ExecutionEnvelopePolicy.from_dict(policy_payload)

    unknown = validate_execution_envelope(
        replace(descriptor, policy_binding=unknown_policy.binding),
        context=replace(context, active_policy=unknown_policy),
    )
    drifted = validate_execution_envelope(
        descriptor,
        context=replace(
            context,
            controller_configuration_sha256="9" * 64,
        ),
    )

    assert unknown.status is FeasibilityStatus.UNVERIFIED
    assert "execution_envelope_policy_unknown_fields" in unknown.reasons
    assert drifted.status is FeasibilityStatus.UNVERIFIED
    assert (
        "execution_envelope_controller_configuration_drift"
        in drifted.reasons
    )


def test_policy_limit_type_cannot_relabel_unit_or_bound_semantics() -> None:
    descriptor, context = _fixture()
    invalid_limit = replace(
        context.active_policy.limits[0],
        unit=EnvelopeUnit.NEWTON,
        bound_kind=EnvelopeBoundKind.MINIMUM,
        lower_bound=0.25,
        upper_bound=None,
    )
    invalid_policy = replace(
        context.active_policy,
        limits=(invalid_limit, context.active_policy.limits[1]),
    )

    validation = validate_execution_envelope(
        replace(descriptor, policy_binding=invalid_policy.binding),
        context=replace(context, active_policy=invalid_policy),
    )

    assert validation.status is FeasibilityStatus.UNVERIFIED
    assert (
        "execution_envelope_policy_limit:max_linear_speed:"
        "unit_not_valid_for_type"
    ) in validation.reasons
    assert (
        "execution_envelope_policy_limit:max_linear_speed:"
        "bound_kind_not_valid_for_type"
    ) in validation.reasons

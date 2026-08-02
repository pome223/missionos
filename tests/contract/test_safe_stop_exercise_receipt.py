from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

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
    EvidenceOrigin,
    EvidenceSourceRef,
    HardwareExecutionMode,
    SafeStopExerciseReceipt,
    SafeStopFreshnessPolicy,
    SafeStopReceiptValidationStatus,
    SafeStopValidationContext,
    VerificationBasis,
    VerificationItem,
    VerificationItemStatus,
    validate_safe_stop_exercise_receipt,
)


pytestmark = pytest.mark.contract

BASE_TIME = datetime(2026, 7, 26, 1, 0, 0, tzinfo=timezone.utc)


def _iso(offset_seconds: float) -> str:
    return (BASE_TIME + timedelta(seconds=offset_seconds)).isoformat()


def _source(
    source_id: str,
    evidence_kind: str,
    offset_seconds: float,
    *,
    scope: HardwareExecutionMode = HardwareExecutionMode.LOOPBACK,
    digest_character: str,
    origin: EvidenceOrigin = EvidenceOrigin.MACHINE_OBSERVED,
) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_id=source_id,
        evidence_kind=evidence_kind,
        observed_at=_iso(offset_seconds),
        content_sha256=digest_character * 64,
        execution_scope=scope,
        origin=origin,
    )


def _policy() -> SafeStopFreshnessPolicy:
    return SafeStopFreshnessPolicy(
        policy_id="safe-stop-receipt",
        policy_version="1",
        maximum_age_seconds=300.0,
    )


def _sources(
    *,
    scope: HardwareExecutionMode = HardwareExecutionMode.LOOPBACK,
) -> dict[str, EvidenceSourceRef]:
    values = (
        _source(
            "source:approval",
            SAFE_STOP_EXERCISE_APPROVAL_EVIDENCE_KIND,
            -0.1,
            scope=scope,
            digest_character="0",
            origin=EvidenceOrigin.AUTHORITY_ARTIFACT,
        ),
        _source(
            "source:pre",
            SAFE_STOP_PRE_STATE_EVIDENCE_KIND,
            0.0,
            scope=scope,
            digest_character="a",
        ),
        _source(
            "source:request",
            SAFE_STOP_REQUEST_EVIDENCE_KIND,
            0.1,
            scope=scope,
            digest_character="b",
        ),
        _source(
            "source:runtime",
            SAFE_STOP_RUNTIME_INVOCATION_EVIDENCE_KIND,
            0.1,
            scope=scope,
            digest_character="c",
        ),
        _source(
            "source:ack",
            SAFE_STOP_ACK_EVIDENCE_KIND,
            0.2,
            scope=scope,
            digest_character="d",
        ),
        _source(
            "source:effect",
            SAFE_STOP_EFFECT_EVIDENCE_KIND,
            0.3,
            scope=scope,
            digest_character="e",
        ),
        _source(
            "source:post",
            SAFE_STOP_POST_STATE_EVIDENCE_KIND,
            0.4,
            scope=scope,
            digest_character="f",
        ),
    )
    return {source.source_id: source for source in values}


def _context(
    *,
    scope: HardwareExecutionMode = HardwareExecutionMode.LOOPBACK,
    evaluated_at: str = _iso(1.0),
    evidence_sources: dict[str, EvidenceSourceRef] | None = None,
) -> SafeStopValidationContext:
    return SafeStopValidationContext(
        expected_execution_scope=scope,
        adapter_id="fixture.adapter",
        adapter_version="1.2.3",
        controller_configuration_sha256="1" * 64,
        safety_configuration_sha256="2" * 64,
        active_policy=_policy(),
        evidence_sources=(
            evidence_sources
            if evidence_sources is not None
            else _sources(scope=scope)
        ),
        evaluated_at=evaluated_at,
    )


def _receipt(
    *,
    scope: HardwareExecutionMode = HardwareExecutionMode.LOOPBACK,
) -> SafeStopExerciseReceipt:
    items = (
        VerificationItem(
            item_id=SAFE_STOP_BOUNDS_ITEM_ID,
            predicate="the exercise remained inside its bounded recipe",
            status=VerificationItemStatus.PASS,
            verification_basis=VerificationBasis.DETERMINISTIC,
            evidence_refs=("source:pre", "source:post"),
        ),
        VerificationItem(
            item_id=SAFE_STOP_REQUEST_ITEM_ID,
            predicate="a safe-stop request was invoked",
            status=VerificationItemStatus.PASS,
            verification_basis=VerificationBasis.DETERMINISTIC,
            evidence_refs=("source:request", "source:runtime"),
        ),
        VerificationItem(
            item_id=SAFE_STOP_ACK_ITEM_ID,
            predicate="the stop request received an ACK",
            status=VerificationItemStatus.PASS,
            verification_basis=VerificationBasis.DETERMINISTIC,
            evidence_refs=("source:ack",),
        ),
        VerificationItem(
            item_id=SAFE_STOP_EFFECT_ITEM_ID,
            predicate="an independent observer measured the stopped state",
            status=VerificationItemStatus.PASS,
            verification_basis=VerificationBasis.DETERMINISTIC,
            evidence_refs=("source:effect", "source:post"),
        ),
    )
    return SafeStopExerciseReceipt(
        receipt_id="safe-stop-receipt:fixture",
        adapter_id="fixture.adapter",
        adapter_version="1.2.3",
        controller_configuration_sha256="1" * 64,
        safety_configuration_sha256="2" * 64,
        stop_mechanism="fixture.loopback.cancel",
        exercise_recipe_id="bounded-stop",
        exercise_recipe_version="1",
        exercise_recipe_sha256="3" * 64,
        exercise_approval_ref="source:approval",
        execution_scope=scope,
        observed_at=_iso(0.4),
        freshness_deadline=_iso(300.4),
        policy_binding=_policy().binding,
        verification_items=items,
        required_verification_item_ids=SAFE_STOP_REQUIRED_ITEM_IDS,
        pre_state_evidence_ref="source:pre",
        request_evidence_ref="source:request",
        ack_evidence_ref="source:ack",
        observed_effect_evidence_ref="source:effect",
        post_state_evidence_ref="source:post",
        runtime_invocation_evidence_ref="source:runtime",
    )


def _validate(
    receipt: SafeStopExerciseReceipt,
    *,
    context: SafeStopValidationContext | None = None,
):
    return validate_safe_stop_exercise_receipt(
        receipt,
        context=context or _context(),
    )


def test_loopback_fixture_validates_schema_without_claiming_execution() -> None:
    receipt = _receipt()

    validation = _validate(receipt)

    assert validation.status is SafeStopReceiptValidationStatus.VERIFIED
    assert validation.verification_basis is VerificationBasis.DETERMINISTIC
    assert validation.stop_capability_evidenced is True
    assert validation.approval_created is False
    assert validation.dispatch_authority_created is False
    assert validation.task_completion_claimed is False
    assert validation.physical_execution_invoked is False


def test_receipt_round_trip_preserves_items_and_receipt_scope() -> None:
    receipt = _receipt()

    decoded = SafeStopExerciseReceipt.from_dict(
        json.loads(json.dumps(receipt.to_dict()))
    )

    assert decoded == receipt
    assert decoded.execution_scope is HardwareExecutionMode.LOOPBACK
    assert decoded.verification_items == receipt.verification_items


def test_ack_without_observed_effect_is_unverified() -> None:
    receipt = _receipt()
    sources = _sources()
    sources.pop(receipt.observed_effect_evidence_ref)

    validation = _validate(
        receipt,
        context=_context(evidence_sources=sources),
    )

    assert validation.status is SafeStopReceiptValidationStatus.UNVERIFIED
    assert "safe_stop_effect_evidence_missing" in validation.reasons
    assert validation.stop_capability_evidenced is False


def test_stale_receipt_is_unverified() -> None:
    validation = _validate(
        _receipt(),
        context=_context(evaluated_at=_iso(301.0)),
    )

    assert "safe_stop_receipt_stale" in validation.reasons


def test_configuration_drift_is_unverified() -> None:
    context = replace(
        _context(),
        controller_configuration_sha256="9" * 64,
    )

    validation = _validate(_receipt(), context=context)

    assert "safe_stop_controller_configuration_drift" in validation.reasons


def test_self_reported_effect_is_not_machine_observed() -> None:
    receipt = _receipt()
    sources = _sources()
    sources[receipt.observed_effect_evidence_ref] = replace(
        sources[receipt.observed_effect_evidence_ref],
        origin=EvidenceOrigin.OPERATOR_DECLARED,
    )

    validation = _validate(
        receipt,
        context=_context(evidence_sources=sources),
    )

    assert "safe_stop_effect_evidence_origin_unverified" in validation.reasons


def test_missing_runtime_invocation_evidence_is_unverified() -> None:
    receipt = _receipt()
    sources = _sources()
    sources.pop(receipt.runtime_invocation_evidence_ref)

    validation = _validate(
        receipt,
        context=_context(evidence_sources=sources),
    )

    assert "safe_stop_runtime_invocation_evidence_missing" in validation.reasons


def test_unknown_receipt_scope_is_not_inferred() -> None:
    material = json.loads(json.dumps(_receipt().to_dict()))
    material["execution_scope"] = "near_bench"

    validation = _validate(SafeStopExerciseReceipt.from_dict(material))

    assert "safe_stop_receipt_execution_scope_unverified" in validation.reasons


def test_missing_item_source_scope_is_unverified() -> None:
    receipt = _receipt()
    sources = _sources()
    sources[receipt.observed_effect_evidence_ref] = replace(
        sources[receipt.observed_effect_evidence_ref],
        execution_scope=None,
    )

    validation = _validate(
        receipt,
        context=_context(evidence_sources=sources),
    )

    assert (
        f"verification_item_evidence_scope_unverified:"
        f"{SAFE_STOP_EFFECT_ITEM_ID}"
    ) in validation.reasons


@pytest.mark.parametrize(
    ("receipt_scope", "source_scope"),
    [
        (HardwareExecutionMode.SIM, HardwareExecutionMode.BENCH),
        (HardwareExecutionMode.BENCH, HardwareExecutionMode.SIM),
    ],
)
def test_item_evidence_cannot_transfer_between_scopes_in_any_direction(
    receipt_scope: HardwareExecutionMode,
    source_scope: HardwareExecutionMode,
) -> None:
    receipt = _receipt(scope=receipt_scope)
    sources = _sources(scope=receipt_scope)
    sources[receipt.observed_effect_evidence_ref] = replace(
        sources[receipt.observed_effect_evidence_ref],
        execution_scope=source_scope,
    )

    validation = _validate(
        receipt,
        context=_context(
            scope=receipt_scope,
            evidence_sources=sources,
        ),
    )

    assert (
        f"verification_item_evidence_scope_mismatch:"
        f"{SAFE_STOP_EFFECT_ITEM_ID}"
    ) in validation.reasons
    assert validation.stop_capability_evidenced is False


def test_mixed_source_scopes_within_item_are_unverified() -> None:
    receipt = _receipt()
    sources = _sources()
    sources[receipt.observed_effect_evidence_ref] = replace(
        sources[receipt.observed_effect_evidence_ref],
        execution_scope=HardwareExecutionMode.BENCH,
    )

    validation = _validate(
        receipt,
        context=_context(evidence_sources=sources),
    )

    assert (
        f"verification_item_evidence_scope_mixed:"
        f"{SAFE_STOP_EFFECT_ITEM_ID}"
    ) in validation.reasons


def test_fabricated_item_source_ref_is_unverified() -> None:
    receipt = _receipt()
    receipt = replace(
        receipt,
        verification_items=tuple(
            replace(item, evidence_refs=(*item.evidence_refs, "source:invented"))
            if item.item_id == SAFE_STOP_EFFECT_ITEM_ID
            else item
            for item in receipt.verification_items
        ),
    )

    validation = _validate(receipt)

    assert (
        f"verification_item_evidence_source_missing:"
        f"{SAFE_STOP_EFFECT_ITEM_ID}"
    ) in validation.reasons


def test_receipt_local_source_registry_cannot_satisfy_missing_verifier_source() -> None:
    receipt = _receipt()
    material = json.loads(json.dumps(receipt.to_dict()))
    material["evidence_sources"] = [
        json.loads(
            json.dumps(
                _sources()[receipt.observed_effect_evidence_ref].__dict__
            )
        )
    ]
    sources = _sources()
    sources.pop(receipt.observed_effect_evidence_ref)

    validation = _validate(
        SafeStopExerciseReceipt.from_dict(material),
        context=_context(evidence_sources=sources),
    )

    assert "safe_stop_effect_evidence_missing" in validation.reasons
    assert validation.stop_capability_evidenced is False


def test_verifier_source_registry_key_must_match_source_id() -> None:
    receipt = _receipt()
    sources = _sources()
    effect = sources.pop(receipt.observed_effect_evidence_ref)
    sources["source:alias"] = effect

    validation = _validate(
        receipt,
        context=_context(evidence_sources=sources),
    )

    assert "safe_stop_evidence_source_registry_mismatch" in validation.reasons
    assert "safe_stop_effect_evidence_missing" in validation.reasons


def test_policy_binding_mismatch_is_unverified() -> None:
    receipt = _receipt()
    receipt = replace(
        receipt,
        policy_binding=replace(
            receipt.policy_binding,
            policy_sha256="9" * 64,
        ),
    )

    validation = _validate(receipt)

    assert "safe_stop_policy_binding_mismatch" in validation.reasons


@pytest.mark.parametrize("maximum_age_seconds", [0.0, float("nan")])
def test_invalid_freshness_policy_is_unverified(
    maximum_age_seconds: float,
) -> None:
    context = replace(
        _context(),
        active_policy=replace(
            _policy(),
            maximum_age_seconds=maximum_age_seconds,
        ),
    )

    validation = _validate(_receipt(), context=context)

    assert "safe_stop_freshness_policy_unverified" in validation.reasons


def test_adapter_cannot_choose_a_longer_freshness_deadline() -> None:
    receipt = replace(
        _receipt(),
        freshness_deadline=_iso(600.4),
    )

    validation = _validate(receipt)

    assert (
        "safe_stop_freshness_deadline_not_policy_derived"
        in validation.reasons
    )


def test_schema_example_cannot_evidence_observed_stop_effect() -> None:
    receipt = _receipt(scope=HardwareExecutionMode.SCHEMA_EXAMPLE_ONLY)

    validation = _validate(
        receipt,
        context=_context(scope=HardwareExecutionMode.SCHEMA_EXAMPLE_ONLY),
    )

    assert "safe_stop_schema_example_cannot_evidence_effect" in validation.reasons


def test_model_inferred_stop_effect_cannot_support_capability() -> None:
    receipt = _receipt()
    receipt = replace(
        receipt,
        verification_items=tuple(
            replace(
                item,
                verification_basis=VerificationBasis.MODEL_INFERRED,
            )
            if item.item_id == SAFE_STOP_EFFECT_ITEM_ID
            else item
            for item in receipt.verification_items
        ),
    )

    validation = _validate(receipt)

    assert "safe_stop_verification_basis_not_deterministic" in validation.reasons


def test_receipt_cannot_create_authority_or_claim_task_completion() -> None:
    receipt = replace(
        _receipt(),
        task_completion_claimed=True,
        dispatch_authority_created=True,
    )

    validation = _validate(receipt)

    assert "safe_stop_receipt_authority_claimed" in validation.reasons
    assert validation.task_completion_claimed is False
    assert validation.physical_execution_invoked is False


@pytest.mark.parametrize(
    ("scope", "physical_execution_invoked"),
    [
        (HardwareExecutionMode.LOOPBACK, True),
        (HardwareExecutionMode.SIM, True),
        (HardwareExecutionMode.BENCH, False),
        (HardwareExecutionMode.FIELD, False),
    ],
)
def test_physical_execution_fact_must_match_exact_scope(
    scope: HardwareExecutionMode,
    physical_execution_invoked: bool,
) -> None:
    receipt = replace(
        _receipt(scope=scope),
        physical_execution_invoked=physical_execution_invoked,
    )

    validation = _validate(receipt, context=_context(scope=scope))

    assert "safe_stop_physical_execution_claim_mismatch" in validation.reasons

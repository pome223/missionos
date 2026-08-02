#!/usr/bin/env python3
"""Exercise the safe-stop receipt validator on a loopback-only fixture."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

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


BASE_TIME = datetime(2026, 7, 26, 1, 0, 0, tzinfo=timezone.utc)


def _iso(offset_seconds: float) -> str:
    return (BASE_TIME + timedelta(seconds=offset_seconds)).isoformat()


def _source(
    source_id: str,
    evidence_kind: str,
    offset_seconds: float,
    digest_character: str,
    *,
    origin: EvidenceOrigin = EvidenceOrigin.STORED_ARTIFACT,
) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_id=source_id,
        evidence_kind=evidence_kind,
        observed_at=_iso(offset_seconds),
        content_sha256=digest_character * 64,
        execution_scope=HardwareExecutionMode.LOOPBACK,
        origin=origin,
    )


def _fixture() -> tuple[SafeStopExerciseReceipt, SafeStopValidationContext]:
    policy = SafeStopFreshnessPolicy(
        policy_id="safe-stop-loopback-smoke",
        policy_version="1",
        maximum_age_seconds=300.0,
    )
    source_values = (
        _source(
            "source:approval",
            SAFE_STOP_EXERCISE_APPROVAL_EVIDENCE_KIND,
            -0.1,
            "0",
            origin=EvidenceOrigin.AUTHORITY_ARTIFACT,
        ),
        _source(
            "source:pre",
            SAFE_STOP_PRE_STATE_EVIDENCE_KIND,
            0.0,
            "a",
        ),
        _source(
            "source:request",
            SAFE_STOP_REQUEST_EVIDENCE_KIND,
            0.1,
            "b",
        ),
        _source(
            "source:runtime",
            SAFE_STOP_RUNTIME_INVOCATION_EVIDENCE_KIND,
            0.1,
            "c",
        ),
        _source(
            "source:ack",
            SAFE_STOP_ACK_EVIDENCE_KIND,
            0.2,
            "d",
        ),
        _source(
            "source:effect",
            SAFE_STOP_EFFECT_EVIDENCE_KIND,
            0.3,
            "e",
        ),
        _source(
            "source:post",
            SAFE_STOP_POST_STATE_EVIDENCE_KIND,
            0.4,
            "f",
        ),
    )
    sources = {source.source_id: source for source in source_values}
    receipt = SafeStopExerciseReceipt(
        receipt_id="safe-stop-receipt:loopback-smoke",
        adapter_id="fixture.loopback.adapter",
        adapter_version="1",
        controller_configuration_sha256="1" * 64,
        safety_configuration_sha256="2" * 64,
        stop_mechanism="loopback.cancel",
        exercise_recipe_id="bounded-loopback-stop",
        exercise_recipe_version="1",
        exercise_recipe_sha256="3" * 64,
        exercise_approval_ref="source:approval",
        execution_scope=HardwareExecutionMode.LOOPBACK,
        observed_at=_iso(0.4),
        freshness_deadline=_iso(300.4),
        policy_binding=policy.binding,
        verification_items=(
            VerificationItem(
                item_id=SAFE_STOP_BOUNDS_ITEM_ID,
                predicate="loopback exercise stayed inside its fixture bounds",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("source:pre", "source:post"),
            ),
            VerificationItem(
                item_id=SAFE_STOP_REQUEST_ITEM_ID,
                predicate="loopback stop request was invoked",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("source:request", "source:runtime"),
            ),
            VerificationItem(
                item_id=SAFE_STOP_ACK_ITEM_ID,
                predicate="loopback stop request received an ACK",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("source:ack",),
            ),
            VerificationItem(
                item_id=SAFE_STOP_EFFECT_ITEM_ID,
                predicate="loopback observer recorded the stopped state",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("source:effect", "source:post"),
            ),
        ),
        required_verification_item_ids=SAFE_STOP_REQUIRED_ITEM_IDS,
        pre_state_evidence_ref="source:pre",
        request_evidence_ref="source:request",
        ack_evidence_ref="source:ack",
        observed_effect_evidence_ref="source:effect",
        post_state_evidence_ref="source:post",
        runtime_invocation_evidence_ref="source:runtime",
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


def main() -> int:
    receipt, context = _fixture()
    fixture_validation = validate_safe_stop_exercise_receipt(
        receipt,
        context=context,
    )
    ack_only_sources = dict(context.evidence_sources)
    ack_only_sources.pop(receipt.observed_effect_evidence_ref)
    ack_only = validate_safe_stop_exercise_receipt(
        receipt,
        context=replace(context, evidence_sources=ack_only_sources),
    )
    cross_scope_sources = dict(context.evidence_sources)
    cross_scope_sources[receipt.observed_effect_evidence_ref] = replace(
        cross_scope_sources[receipt.observed_effect_evidence_ref],
        execution_scope=HardwareExecutionMode.BENCH,
    )
    cross_scope = validate_safe_stop_exercise_receipt(
        receipt,
        context=replace(context, evidence_sources=cross_scope_sources),
    )
    result = {
        "schema_validator_only": True,
        "live_stop_executed": False,
        "fixture_status": fixture_validation.status,
        "fixture_stop_capability_evidenced": (
            fixture_validation.stop_capability_evidenced
        ),
        "fixture_origin_rejected": (
            "safe_stop_effect_evidence_origin_unverified"
            in fixture_validation.reasons
        ),
        "ack_only_status": ack_only.status,
        "ack_only_effect_missing": "safe_stop_effect_evidence_missing"
        in ack_only.reasons,
        "cross_scope_status": cross_scope.status,
        "cross_scope_item_rejected": (
            f"verification_item_evidence_scope_mismatch:"
            f"{SAFE_STOP_EFFECT_ITEM_ID}"
        )
        in cross_scope.reasons,
        "approval_created": fixture_validation.approval_created,
        "dispatch_authority_created": (
            fixture_validation.dispatch_authority_created
        ),
        "task_completion_claimed": (
            fixture_validation.task_completion_claimed
        ),
        "physical_execution_invoked": (
            fixture_validation.physical_execution_invoked
        ),
    }
    print(json.dumps(result, sort_keys=True))
    if fixture_validation.status is not SafeStopReceiptValidationStatus.UNVERIFIED:
        return 1
    if fixture_validation.stop_capability_evidenced:
        return 1
    if not result["fixture_origin_rejected"]:
        return 1
    if ack_only.status is not SafeStopReceiptValidationStatus.UNVERIFIED:
        return 1
    if cross_scope.status is not SafeStopReceiptValidationStatus.UNVERIFIED:
        return 1
    if any(
        result[key]
        for key in (
            "live_stop_executed",
            "approval_created",
            "dispatch_authority_created",
            "task_completion_claimed",
            "physical_execution_invoked",
        )
    ):
        return 1
    if not result["ack_only_effect_missing"]:
        return 1
    if not result["cross_scope_item_rejected"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

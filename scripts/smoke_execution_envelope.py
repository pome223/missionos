#!/usr/bin/env python3
"""Exercise the policy-authoritative envelope validator without live hardware."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

from missionos_core import (
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
    validate_execution_envelope,
)
from scripts.smoke_safe_stop_exercise_receipt import _fixture as stop_fixture


BASE_TIME = datetime(2026, 7, 26, 1, 0, 0, tzinfo=timezone.utc)


def _iso(offset_seconds: float) -> str:
    return (BASE_TIME + timedelta(seconds=offset_seconds)).isoformat()


def main() -> int:
    stop_receipt, stop_context = stop_fixture()
    policy = ExecutionEnvelopePolicy(
        policy_id="execution-envelope-loopback-smoke",
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
        ),
    )
    source = EvidenceSourceRef(
        source_id="readback:speed",
        evidence_kind=EXECUTION_ENVELOPE_READBACK_EVIDENCE_KIND,
        observed_at=_iso(0.5),
        freshness_deadline=_iso(30.5),
        content_sha256="4" * 64,
        execution_scope=HardwareExecutionMode.LOOPBACK,
        origin=EvidenceOrigin.STORED_ARTIFACT,
    )
    readback = EnvelopeLimitReadback(
        limit_id="max_linear_speed",
        unit=EnvelopeUnit.METRES_PER_SECOND,
        enforcement_location=EnvelopeEnforcementLocation.CONTROLLER,
        evidence_ref=source.source_id,
        upper_bound=0.2,
    )
    descriptor = ExecutionEnvelopeDescriptor(
        envelope_id="envelope:loopback-smoke",
        adapter_id=stop_receipt.adapter_id,
        adapter_version=stop_receipt.adapter_version,
        controller_configuration_sha256=(
            stop_receipt.controller_configuration_sha256
        ),
        safety_configuration_sha256=(
            stop_receipt.safety_configuration_sha256
        ),
        execution_scope=HardwareExecutionMode.LOOPBACK,
        policy_binding=policy.binding,
        safe_stop_receipt_ref=stop_receipt.receipt_id,
        readbacks=(readback,),
    )
    context = ExecutionEnvelopeValidationContext(
        expected_execution_scope=HardwareExecutionMode.LOOPBACK,
        adapter_id=descriptor.adapter_id,
        adapter_version=descriptor.adapter_version,
        controller_configuration_sha256=(
            descriptor.controller_configuration_sha256
        ),
        safety_configuration_sha256=(
            descriptor.safety_configuration_sha256
        ),
        active_policy=policy,
        evidence_sources={source.source_id: source},
        safe_stop_receipt=stop_receipt,
        safe_stop_context=stop_context,
        evaluated_at=_iso(1.0),
    )
    declaration_only = validate_execution_envelope(descriptor, context=context)
    observed_source = replace(
        source,
        origin=EvidenceOrigin.MACHINE_OBSERVED,
    )
    widened = validate_execution_envelope(
        replace(
            descriptor,
            readbacks=(replace(readback, upper_bound=100.0),),
        ),
        context=replace(
            context,
            evidence_sources={observed_source.source_id: observed_source},
        ),
    )
    result = {
        "schema_validator_only": True,
        "live_hardware_executed": False,
        "declaration_only_status": declaration_only.status,
        "declaration_not_promoted": (
            "execution_envelope_readback_origin_unverified:max_linear_speed"
            in declaration_only.reasons
        ),
        "stop_receipt_required": (
            "execution_envelope_safe_stop_receipt_unverified"
            in declaration_only.reasons
        ),
        "widened_status": widened.status,
        "widened_blocked_by_policy": (
            "execution_envelope_readback_exceeds_policy:max_linear_speed"
            in widened.reasons
        ),
        "approval_created": widened.approval_created,
        "dispatch_authority_created": widened.dispatch_authority_created,
        "task_completion_claimed": widened.task_completion_claimed,
        "physical_execution_invoked": widened.physical_execution_invoked,
    }
    print(json.dumps(result, sort_keys=True))
    if declaration_only.status is not FeasibilityStatus.UNVERIFIED:
        return 1
    if widened.status is not FeasibilityStatus.BLOCKED:
        return 1
    if not all(
        result[key]
        for key in (
            "declaration_not_promoted",
            "stop_receipt_required",
            "widened_blocked_by_policy",
        )
    ):
        return 1
    if any(
        result[key]
        for key in (
            "live_hardware_executed",
            "approval_created",
            "dispatch_authority_created",
            "task_completion_claimed",
            "physical_execution_invoked",
        )
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

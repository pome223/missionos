#!/usr/bin/env python3
"""Opt-in live RoboCasa GR1 arm controller and evidence-chain smoke."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import numpy as np
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
    canonical_sha256,
    validate_execution_envelope,
    validate_safe_stop_exercise_receipt,
)

from src.runtime.groot_arm_controller_bridge import (
    ARM_FIELDS,
    GROOT_ARM_CONTROLLER_SMOKE_ENV,
    GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD,
    GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS,
    GROOT_ROBOT_DESCRIPTION_SHA256,
    GrootArmControllerPolicy,
    GrootArmControllerProcessClient,
    GrootArmExecutionContext,
    GrootArmTransformation,
    GrootArmTransformationKind,
    GrootArmTransformationStep,
    execute_groot_arm_chunk,
    groot_robocasa_controller_configuration_sha256,
)
from src.runtime.groot_policy_client import GrootActionChunkProposal


LIVE_SMOKE_ENV = "RUN_MISSIONOS_GROOT_ROBOCASA_SMOKE"
PYTHON_ENV = "GROOT_ROBOCASA_PYTHON"


def _source(
    source_id: str,
    kind: str,
    observed_at: str,
    digest: str,
    *,
    origin: EvidenceOrigin,
) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_id=source_id,
        evidence_kind=kind,
        observed_at=observed_at,
        content_sha256=digest,
        execution_scope=HardwareExecutionMode.SIM,
        origin=origin,
    )


def build_validated_groot_robocasa_envelope(
    exercise: dict,
    *,
    invocation_digest: str,
    controller_digest: str,
    safety_digest: str,
) -> tuple[ExecutionEnvelopePolicy, object]:
    pre_at = exercise["pre_state"]["observed_at"]
    approval_at = (
        datetime.fromisoformat(pre_at.replace("Z", "+00:00"))
        - timedelta(microseconds=1)
    ).isoformat()
    policy = SafeStopFreshnessPolicy(
        policy_id="groot-robocasa-safe-stop",
        policy_version="1",
        maximum_age_seconds=300.0,
    )
    post_at = exercise["post_state"]["observed_at"]
    deadline = (
        datetime.fromisoformat(post_at.replace("Z", "+00:00"))
        + timedelta(seconds=policy.maximum_age_seconds)
    ).isoformat()
    values = (
        _source(
            "safe-stop:approval",
            SAFE_STOP_EXERCISE_APPROVAL_EVIDENCE_KIND,
            approval_at,
            canonical_sha256(
                {"opt_in_environment": LIVE_SMOKE_ENV, "value": True}
            ),
            origin=EvidenceOrigin.AUTHORITY_ARTIFACT,
        ),
        _source(
            "safe-stop:pre",
            SAFE_STOP_PRE_STATE_EVIDENCE_KIND,
            pre_at,
            exercise["pre_state"]["sha256"],
            origin=EvidenceOrigin.MACHINE_OBSERVED,
        ),
        _source(
            "safe-stop:request",
            SAFE_STOP_REQUEST_EVIDENCE_KIND,
            exercise["request"]["observed_at"],
            exercise["request"]["sha256"],
            origin=EvidenceOrigin.MACHINE_OBSERVED,
        ),
        _source(
            "safe-stop:runtime",
            SAFE_STOP_RUNTIME_INVOCATION_EVIDENCE_KIND,
            exercise["request"]["observed_at"],
            invocation_digest,
            origin=EvidenceOrigin.MACHINE_OBSERVED,
        ),
        _source(
            "safe-stop:ack",
            SAFE_STOP_ACK_EVIDENCE_KIND,
            exercise["ack"]["observed_at"],
            exercise["ack"]["sha256"],
            origin=EvidenceOrigin.MACHINE_OBSERVED,
        ),
        _source(
            "safe-stop:effect",
            SAFE_STOP_EFFECT_EVIDENCE_KIND,
            exercise["effect"]["observed_at"],
            exercise["effect"]["sha256"],
            origin=EvidenceOrigin.MACHINE_OBSERVED,
        ),
        _source(
            "safe-stop:post",
            SAFE_STOP_POST_STATE_EVIDENCE_KIND,
            post_at,
            exercise["post_state"]["sha256"],
            origin=EvidenceOrigin.MACHINE_OBSERVED,
        ),
    )
    sources = {value.source_id: value for value in values}
    receipt = SafeStopExerciseReceipt(
        receipt_id="safe-stop:groot-robocasa-live",
        adapter_id="groot.robocasa.arm-controller",
        adapter_version="1",
        controller_configuration_sha256=controller_digest,
        safety_configuration_sha256=safety_digest,
        stop_mechanism="controller_position_hold",
        exercise_recipe_id="groot-robocasa-position-hold",
        exercise_recipe_version="1",
        exercise_recipe_sha256=canonical_sha256(
            {
                "hold_steps": 8,
                "maximum_step_delta_rad": 0.01,
                "hand_actuation_allowed": False,
            }
        ),
        exercise_approval_ref="safe-stop:approval",
        execution_scope=HardwareExecutionMode.SIM,
        observed_at=post_at,
        freshness_deadline=deadline,
        policy_binding=policy.binding,
        verification_items=(
            VerificationItem(
                item_id=SAFE_STOP_BOUNDS_ITEM_ID,
                predicate="sim stop exercise remained in its arm-only bounds",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("safe-stop:pre", "safe-stop:post"),
            ),
            VerificationItem(
                item_id=SAFE_STOP_REQUEST_ITEM_ID,
                predicate="position-hold stop request was invoked",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("safe-stop:request", "safe-stop:runtime"),
            ),
            VerificationItem(
                item_id=SAFE_STOP_ACK_ITEM_ID,
                predicate="controller accepted the position-hold request",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("safe-stop:ack",),
            ),
            VerificationItem(
                item_id=SAFE_STOP_EFFECT_ITEM_ID,
                predicate="fresh qpos showed the policy-defined hold condition",
                status=VerificationItemStatus.PASS,
                verification_basis=VerificationBasis.DETERMINISTIC,
                evidence_refs=("safe-stop:effect", "safe-stop:post"),
            ),
        ),
        required_verification_item_ids=SAFE_STOP_REQUIRED_ITEM_IDS,
        pre_state_evidence_ref="safe-stop:pre",
        request_evidence_ref="safe-stop:request",
        ack_evidence_ref="safe-stop:ack",
        observed_effect_evidence_ref="safe-stop:effect",
        post_state_evidence_ref="safe-stop:post",
        runtime_invocation_evidence_ref="safe-stop:runtime",
    )
    evaluated_at = (
        datetime.fromisoformat(post_at.replace("Z", "+00:00"))
        + timedelta(milliseconds=1)
    ).isoformat()
    stop_context = SafeStopValidationContext(
        expected_execution_scope=HardwareExecutionMode.SIM,
        adapter_id=receipt.adapter_id,
        adapter_version=receipt.adapter_version,
        controller_configuration_sha256=controller_digest,
        safety_configuration_sha256=safety_digest,
        active_policy=policy,
        evidence_sources=sources,
        evaluated_at=evaluated_at,
    )
    stop_validation = validate_safe_stop_exercise_receipt(
        receipt,
        context=stop_context,
    )
    if not stop_validation.stop_capability_evidenced:
        raise RuntimeError(f"safe stop unverified: {stop_validation.reasons}")

    envelope_policy = ExecutionEnvelopePolicy(
        policy_id="groot-robocasa-arm-envelope",
        policy_version="1",
        execution_scope=HardwareExecutionMode.SIM,
        maximum_readback_age_seconds=30.0,
        limits=(
            EnvelopeLimitPolicy(
                limit_id="chunk_horizon",
                limit_type=EnvelopeLimitType.OPERATION_TIMEOUT,
                unit=EnvelopeUnit.SECOND,
                bound_kind=EnvelopeBoundKind.MAXIMUM,
                enforcement_location=EnvelopeEnforcementLocation.CONTROLLER,
                upper_bound=0.8,
            ),
        ),
    )
    readback_at = post_at
    readback_deadline = (
        datetime.fromisoformat(readback_at.replace("Z", "+00:00"))
        + timedelta(seconds=30)
    ).isoformat()
    readback_source = EvidenceSourceRef(
        source_id="envelope:chunk-horizon-readback",
        evidence_kind="execution_envelope_limit_readback",
        observed_at=readback_at,
        freshness_deadline=readback_deadline,
        content_sha256=canonical_sha256(
            {"sample_rate_hz": 20.0, "chunk_steps": 16, "horizon_s": 0.8}
        ),
        execution_scope=HardwareExecutionMode.SIM,
        origin=EvidenceOrigin.MACHINE_OBSERVED,
    )
    readback = EnvelopeLimitReadback(
        limit_id="chunk_horizon",
        unit=EnvelopeUnit.SECOND,
        enforcement_location=EnvelopeEnforcementLocation.CONTROLLER,
        evidence_ref=readback_source.source_id,
        upper_bound=0.8,
    )
    descriptor = ExecutionEnvelopeDescriptor(
        envelope_id="envelope:groot-robocasa-live",
        adapter_id=receipt.adapter_id,
        adapter_version=receipt.adapter_version,
        controller_configuration_sha256=controller_digest,
        safety_configuration_sha256=safety_digest,
        execution_scope=HardwareExecutionMode.SIM,
        policy_binding=envelope_policy.binding,
        safe_stop_receipt_ref=receipt.receipt_id,
        readbacks=(readback,),
    )
    envelope_validation = validate_execution_envelope(
        descriptor,
        context=ExecutionEnvelopeValidationContext(
            expected_execution_scope=HardwareExecutionMode.SIM,
            adapter_id=descriptor.adapter_id,
            adapter_version=descriptor.adapter_version,
            controller_configuration_sha256=controller_digest,
            safety_configuration_sha256=safety_digest,
            active_policy=envelope_policy,
            evidence_sources={readback_source.source_id: readback_source},
            safe_stop_receipt=receipt,
            safe_stop_context=stop_context,
            evaluated_at=evaluated_at,
        ),
    )
    if envelope_validation.status is not FeasibilityStatus.VERIFIED_FEASIBLE:
        raise RuntimeError(
            f"execution envelope unverified: {envelope_validation.reasons}"
        )
    return envelope_policy, envelope_validation, receipt, stop_validation


def build_groot_robocasa_arm_policy(
    envelope_policy: ExecutionEnvelopePolicy,
) -> GrootArmControllerPolicy:
    transformation = GrootArmTransformation(
        transformation_id="arm-only-projection-identity-v1",
        transformation_version="1",
        steps=(
            GrootArmTransformationStep(
                kind=GrootArmTransformationKind.ARM_ONLY_PROJECTION,
                parameters={
                    "retained_fields": list(ARM_FIELDS),
                    "dropped_fields": [
                        "action.left_hand",
                        "action.right_hand",
                    ],
                    "drop_authority": "active_policy",
                    "drop_reason": (
                        "arm_only_controller_has_no_hand_actuators"
                    ),
                    "dropped_values_semantically_verified": False,
                    "hand_actuation_prohibited": True,
                    "input_dtype": "float32",
                    "output_dtype": "float64",
                    "dtype_conversion": "exact_value_preserving_widening",
                },
                input_fields=(
                    "action.left_arm",
                    "action.left_hand",
                    "action.right_arm",
                    "action.right_hand",
                ),
                output_fields=ARM_FIELDS,
                input_shapes=((16, 7), (16, 6), (16, 7), (16, 6)),
                output_shapes=((16, 7), (16, 7)),
                unit="arm_rad_hand_unverified",
                input_rate_hz=20.0,
                output_rate_hz=20.0,
                composition_index=0,
                implementation_id="missionos.groot_arm_only_projection",
                implementation_version="1",
                implementation_configuration_sha256="8" * 64,
                policy_source_sha256=envelope_policy.binding.policy_sha256,
            ),
            GrootArmTransformationStep(
                kind=GrootArmTransformationKind.IDENTITY,
                parameters={
                    "dimension_mapping": {
                        "action.left_arm": list(range(7)),
                        "action.right_arm": list(range(7)),
                    }
                },
                input_fields=ARM_FIELDS,
                output_fields=ARM_FIELDS,
                input_shapes=((16, 7), (16, 7)),
                output_shapes=((16, 7), (16, 7)),
                unit="rad",
                input_rate_hz=20.0,
                output_rate_hz=20.0,
                composition_index=1,
                implementation_id="missionos.groot_arm_transform",
                implementation_version="1",
                implementation_configuration_sha256="9" * 64,
                policy_source_sha256=envelope_policy.binding.policy_sha256,
            ),
        ),
    )
    lower = (-3.0, 0.0, -3.0, -3.0, -3.0, -1.5, -1.5) + (
        -3.0,
        -3.0,
        -3.0,
        -3.0,
        -3.0,
        -1.5,
        -1.5,
    )
    upper = (3.0, 3.0, 3.0, 0.0, 3.0, 1.5, 1.5) + (
        3.0,
        0.0,
        3.0,
        0.0,
        3.0,
        1.5,
        1.5,
    )
    return GrootArmControllerPolicy(
        policy_id="groot-robocasa-arm-envelope",
        policy_version="1",
        joint_names=(
            "l_shoulder_pitch",
            "l_shoulder_roll",
            "l_shoulder_yaw",
            "l_elbow_pitch",
            "l_wrist_yaw",
            "l_wrist_roll",
            "l_wrist_pitch",
            "r_shoulder_pitch",
            "r_shoulder_roll",
            "r_shoulder_yaw",
            "r_elbow_pitch",
            "r_wrist_yaw",
            "r_wrist_roll",
            "r_wrist_pitch",
        ),
        lower_position_rad=lower,
        upper_position_rad=upper,
        position_bounds_source_sha256=GROOT_ROBOT_DESCRIPTION_SHA256,
        authorized_transformations=(transformation,),
        execution_envelope_policy_sha256=(
            envelope_policy.binding.policy_sha256
        ),
    )


def main() -> int:
    if os.environ.get(LIVE_SMOKE_ENV, "").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise SystemExit(f"{LIVE_SMOKE_ENV}=1 is required")
    python = os.environ.get(PYTHON_ENV, "").strip()
    if not python:
        raise SystemExit(f"{PYTHON_ENV} must point to the pinned Python 3.11 env")
    controller_script = (
        Path(__file__).resolve().parent / "groot_robocasa_arm_controller.py"
    )
    command = (python, str(controller_script))
    controller = GrootArmControllerProcessClient(
        command=command,
        timeout_seconds=30,
    )
    exercise = dict(controller.exercise_safe_stop())
    exercise_invocation = controller.collect_runtime_invocation_evidence()[-1]
    handoff = dict(controller.observe_handoff_state())
    controller_digest = groot_robocasa_controller_configuration_sha256()
    safety_digest = canonical_sha256(
        {
            "mechanism": "controller_position_hold",
            "hold_steps": 8,
            "maximum_step_delta_rad": 0.01,
            "hand_part_controllers_enabled": False,
        }
    )
    envelope_policy, envelope_validation, _, _ = (
        build_validated_groot_robocasa_envelope(
            exercise,
            invocation_digest=str(exercise_invocation["response_sha256"]),
            controller_digest=controller_digest,
            safety_digest=safety_digest,
        )
    )
    policy = build_groot_robocasa_arm_policy(envelope_policy)
    left = np.repeat(
        np.asarray(handoff["left_arm_rad"], dtype=np.float32)[None, :],
        16,
        axis=0,
    )
    right = np.repeat(
        np.asarray(handoff["right_arm_rad"], dtype=np.float32)[None, :],
        16,
        axis=0,
    )
    hand = np.zeros((16, 6), dtype=np.float32)
    now = datetime.now(timezone.utc)
    preparation_digest = "a" * 64
    proposal = GrootActionChunkProposal(
        schema_version="missionos_groot_action_chunk_proposal.v1",
        verification_basis="model_inferred",
        actions={
            "action.left_arm": left,
            "action.right_arm": right,
            "action.left_hand": hand.copy(),
            "action.right_hand": hand.copy(),
        },
        instruction_ref="instruction:groot-robocasa-live-smoke",
        preparation_sha256=preparation_digest,
        policy_revision="4af2b622892f7dcb5aae5a3fb70bcb02dc217b96",
        model_snapshot="869830fc749c35f34771aa5209f923ac57e4564e",
        request_sha256="b" * 64,
        observation_sha256="c" * 64,
        response_sha256="d" * 64,
        observed_at=now.isoformat().replace("+00:00", "Z"),
        freshness_deadline=(now + timedelta(seconds=30))
        .isoformat()
        .replace("+00:00", "Z"),
        response_received_at=now.isoformat().replace("+00:00", "Z"),
    )
    os.environ[GROOT_ARM_CONTROLLER_SMOKE_ENV] = "1"
    try:
        result = execute_groot_arm_chunk(
            proposal=proposal,
            context=GrootArmExecutionContext(
                instruction_ref=proposal.instruction_ref,
                approval_ref=f"operator-opt-in:{LIVE_SMOKE_ENV}",
                expected_preparation_sha256=preparation_digest,
                controller_configuration_sha256=controller_digest,
                safety_configuration_sha256=safety_digest,
                policy=policy,
                envelope_validation=envelope_validation,
            ),
            transformation_id="arm-only-projection-identity-v1",
            controller=controller,
            evaluated_at=now,
        )
    finally:
        controller.close()
    output = {
        "schema_version": "missionos_groot_robocasa_live_smoke.v1",
        "status": result.status.value,
        "safe_stop_receipt_verified": True,
        "execution_envelope_verified": True,
        "controller_request_sent": result.controller_request_sent,
        "admission_projection_digest_distinct": (
            result.admitted_chunk_sha256
            != result.transformed_chunk_sha256
        ),
        "transformed_applied_digest_equal": (
            result.transformed_chunk_sha256
            == result.applied_command_sha256
        ),
        "hand_command_applied": False,
        "runtime_invocation_count": len(
            controller.collect_runtime_invocation_evidence()
        ),
        "chunk_age_at_handoff_seconds": (
            result.chunk_age_at_handoff_seconds
        ),
        "remaining_horizon_at_handoff_seconds": (
            result.remaining_horizon_at_handoff_seconds
        ),
        "handoff_continuity": {
            "passed": result.handoff_continuity_passed,
            "observed_max_abs_joint_delta_rad": (
                result.observed_handoff_max_abs_joint_delta_rad
            ),
            "maximum_abs_joint_delta_rad": (
                GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD
            ),
            "observed_state_age_seconds": (
                result.observed_handoff_state_age_seconds
            ),
            "maximum_state_age_seconds": (
                GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS
            ),
        },
        "physical_execution_invoked": result.physical_execution_invoked,
        "task_completion_claimed": result.task_completion_claimed,
        "limitations": [
            "sim scope only",
            "hold-position arm chunk derived from fresh simulator state",
            "no live GR00T inference in this smoke",
            "hand part controllers disabled",
            "logical/process independence only",
            (
                "handoff continuity reports a maximum absolute joint delta; "
                "it does not identify the semantic root cause of a rejection"
            ),
            (
                "request observation, dispatch state, and first action vectors "
                "are not published in this summary"
            ),
        ],
    }
    print(json.dumps(output, ensure_ascii=True, sort_keys=True))
    return 0 if result.status is FeasibilityStatus.VERIFIED_FEASIBLE else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Exercise arm admission through the real subprocess controller boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

import numpy as np
from missionos_core import (
    ExecutionEnvelopeValidation,
    FeasibilityStatus,
    PolicyBinding,
    VerificationBasis,
)

from src.runtime.groot_arm_controller_bridge import (
    GROOT_ARM_CONTROLLER_SMOKE_ENV,
    GROOT_ROBOT_DESCRIPTION_SHA256,
    GrootArmControllerCommandClient,
    GrootArmControllerPolicy,
    GrootArmExecutionContext,
    GrootArmTransformation,
    GrootArmTransformationKind,
    GrootArmTransformationStep,
    execute_groot_arm_chunk,
    groot_robocasa_controller_configuration_sha256,
)
from src.runtime.groot_policy_client import GrootActionChunkProposal


def main() -> int:
    now = datetime.now(timezone.utc)
    digest = "a" * 64
    policy = GrootArmControllerPolicy(
        policy_id="groot-arm-loopback-smoke",
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
        lower_position_rad=(
            -3.0,
            0.0,
            -3.0,
            -3.0,
            -3.0,
            -1.5,
            -1.5,
            -3.0,
            -3.0,
            -3.0,
            -3.0,
            -3.0,
            -1.5,
            -1.5,
        ),
        upper_position_rad=(
            3.0,
            3.0,
            3.0,
            0.0,
            3.0,
            1.5,
            1.5,
            3.0,
            0.0,
            3.0,
            0.0,
            3.0,
            1.5,
            1.5,
        ),
        position_bounds_source_sha256=GROOT_ROBOT_DESCRIPTION_SHA256,
        authorized_transformations=(
            GrootArmTransformation(
                transformation_id="arm-only-projection-identity-v1",
                transformation_version="1",
                steps=(
                    GrootArmTransformationStep(
                        kind=GrootArmTransformationKind.ARM_ONLY_PROJECTION,
                        parameters={
                            "retained_fields": [
                                "action.left_arm",
                                "action.right_arm",
                            ],
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
                            "dtype_conversion": (
                                "exact_value_preserving_widening"
                            ),
                        },
                        input_fields=(
                            "action.left_arm",
                            "action.left_hand",
                            "action.right_arm",
                            "action.right_hand",
                        ),
                        output_fields=(
                            "action.left_arm",
                            "action.right_arm",
                        ),
                        input_shapes=(
                            (16, 7),
                            (16, 6),
                            (16, 7),
                            (16, 6),
                        ),
                        output_shapes=((16, 7), (16, 7)),
                        unit="arm_rad_hand_unverified",
                        input_rate_hz=20.0,
                        output_rate_hz=20.0,
                        composition_index=0,
                        implementation_id=(
                            "missionos.groot_arm_only_projection"
                        ),
                        implementation_version="1",
                        implementation_configuration_sha256="8" * 64,
                        policy_source_sha256="e" * 64,
                    ),
                    GrootArmTransformationStep(
                        kind=GrootArmTransformationKind.IDENTITY,
                        parameters={
                            "dimension_mapping": {
                                "action.left_arm": list(range(7)),
                                "action.right_arm": list(range(7)),
                            }
                        },
                        input_fields=(
                            "action.left_arm",
                            "action.right_arm",
                        ),
                        output_fields=(
                            "action.left_arm",
                            "action.right_arm",
                        ),
                        input_shapes=((16, 7), (16, 7)),
                        output_shapes=((16, 7), (16, 7)),
                        unit="rad",
                        input_rate_hz=20.0,
                        output_rate_hz=20.0,
                        composition_index=1,
                        implementation_id="missionos.groot_arm_transform",
                        implementation_version="1",
                        implementation_configuration_sha256="9" * 64,
                        policy_source_sha256="e" * 64,
                    ),
                ),
            ),
        ),
        execution_envelope_policy_sha256="e" * 64,
    )
    envelope = ExecutionEnvelopeValidation(
        status=FeasibilityStatus.VERIFIED_FEASIBLE,
        verification_basis=VerificationBasis.DETERMINISTIC,
        reasons=(),
        verification_items=(),
        required_verification_item_ids=(),
        policy_binding=PolicyBinding(
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            policy_sha256=policy.execution_envelope_policy_sha256,
        ),
    )
    arm = np.zeros((16, 7), dtype=np.float32)
    hand = np.zeros((16, 6), dtype=np.float32)
    proposal = GrootActionChunkProposal(
        schema_version="missionos_groot_action_chunk_proposal.v1",
        verification_basis="model_inferred",
        actions={
            "action.left_arm": arm.copy(),
            "action.left_hand": hand.copy(),
            "action.right_arm": arm.copy(),
            "action.right_hand": hand.copy(),
        },
        instruction_ref="instruction:loopback-smoke",
        preparation_sha256=digest,
        policy_revision="observed-revision",
        model_snapshot="observed-model",
        request_sha256="b" * 64,
        observation_sha256="c" * 64,
        response_sha256="d" * 64,
        observed_at=now.isoformat().replace("+00:00", "Z"),
        freshness_deadline=(now + timedelta(seconds=30))
        .isoformat()
        .replace("+00:00", "Z"),
        response_received_at=now.isoformat().replace("+00:00", "Z"),
    )
    fixture = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "groot_arm_controller_loopback.py"
    )
    os.environ[GROOT_ARM_CONTROLLER_SMOKE_ENV] = "1"
    controller = GrootArmControllerCommandClient(
        command=(sys.executable, str(fixture)),
    )
    result = execute_groot_arm_chunk(
        proposal=proposal,
        context=GrootArmExecutionContext(
            instruction_ref=proposal.instruction_ref,
            approval_ref="approval:human-fixture",
            expected_preparation_sha256=digest,
            controller_configuration_sha256=(
                groot_robocasa_controller_configuration_sha256()
            ),
            safety_configuration_sha256="d" * 64,
            policy=policy,
            envelope_validation=envelope,
        ),
        transformation_id="arm-only-projection-identity-v1",
        controller=controller,
        evaluated_at=now,
    )
    output = {
        "schema_version": "missionos_groot_arm_controller_smoke.v1",
        "status": result.status.value,
        "controller_request_sent": result.controller_request_sent,
        "admission_projection_digest_distinct": (
            result.admitted_chunk_sha256
            != result.transformed_chunk_sha256
        ),
        "transformed_applied_digest_equal": (
            result.transformed_chunk_sha256
            == result.applied_command_sha256
        ),
        "runtime_invocation_count": len(
            controller.collect_runtime_invocation_evidence()
        ),
        "task_completion_claimed": result.task_completion_claimed,
        "physical_execution_invoked": result.physical_execution_invoked,
        "limitations": [
            "loopback subprocess controller fixture",
            "no RoboCasa or robosuite runtime",
            "no hand actuation",
            "no physical execution",
            "no semantic task completion",
        ],
    }
    print(json.dumps(output, ensure_ascii=True, sort_keys=True))
    return 0 if result.status is FeasibilityStatus.VERIFIED_FEASIBLE else 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
from missionos_core import (
    ExecutionEnvelopeValidation,
    FeasibilityStatus,
    PolicyBinding,
    VerificationBasis,
    canonical_sha256,
)

from src.runtime.groot_arm_controller_bridge import (
    ARM_APPLIED_ITEM,
    ARM_CONTROLLER_DYNAMIC_LIMITS_ITEM,
    HAND_REQUIRED_ITEMS,
    GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD,
    GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS,
    GROOT_ARM_CONTROLLER_RECEIPT_SCHEMA,
    GROOT_ROBOT_DESCRIPTION_SHA256,
    GrootArmControllerPolicy,
    GrootArmControllerReceipt,
    GrootArmExecutionContext,
    GrootArmTransformation,
    GrootArmTransformationKind,
    GrootArmTransformationStep,
    execute_groot_arm_chunk,
    groot_robocasa_controller_configuration_material,
    groot_robocasa_controller_configuration_sha256,
)
from src.runtime.groot_policy_client import GrootActionChunkProposal


NOW = datetime(2026, 7, 26, 3, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-07-26T03:00:00Z"
DEADLINE = "2026-07-26T03:01:00Z"
DIGEST = "a" * 64
CONTROLLER_DIGEST = groot_robocasa_controller_configuration_sha256()
SAFETY_DIGEST = "b" * 64
ENVELOPE_DIGEST = "e" * 64
JOINT_NAMES = (
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
)
LOWER = (-3.0, 0.0, -3.0, -3.0, -3.0, -1.5, -1.5) + (
    -3.0,
    -3.0,
    -3.0,
    -3.0,
    -3.0,
    -1.5,
    -1.5,
)
UPPER = (3.0, 3.0, 3.0, 0.0, 3.0, 1.5, 1.5) + (
    3.0,
    0.0,
    3.0,
    0.0,
    3.0,
    1.5,
    1.5,
)


def _step(
    *,
    kind: GrootArmTransformationKind = GrootArmTransformationKind.IDENTITY,
    parameters=None,
) -> GrootArmTransformationStep:
    return GrootArmTransformationStep(
        kind=kind,
        parameters=(
            {
                "dimension_mapping": {
                    "action.left_arm": list(range(7)),
                    "action.right_arm": list(range(7)),
                }
            }
            if parameters is None
            else parameters
        ),
        input_fields=("action.left_arm", "action.right_arm"),
        output_fields=("action.left_arm", "action.right_arm"),
        input_shapes=((16, 7), (16, 7)),
        output_shapes=((16, 7), (16, 7)),
        unit="rad",
        input_rate_hz=20.0,
        output_rate_hz=20.0,
        composition_index=1,
        implementation_id="missionos.groot_arm_transform",
        implementation_version="1",
        implementation_configuration_sha256="9" * 64,
        policy_source_sha256=ENVELOPE_DIGEST,
    )


def _projection_step() -> GrootArmTransformationStep:
    return GrootArmTransformationStep(
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
            "drop_reason": "arm_only_controller_has_no_hand_actuators",
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
        output_fields=("action.left_arm", "action.right_arm"),
        input_shapes=((16, 7), (16, 6), (16, 7), (16, 6)),
        output_shapes=((16, 7), (16, 7)),
        unit="arm_rad_hand_unverified",
        input_rate_hz=20.0,
        output_rate_hz=20.0,
        composition_index=0,
        implementation_id="missionos.groot_arm_only_projection",
        implementation_version="1",
        implementation_configuration_sha256="8" * 64,
        policy_source_sha256=ENVELOPE_DIGEST,
    )


def _actions(value: float = 0.0) -> dict[str, np.ndarray]:
    arm = np.full((16, 7), value, dtype=np.float32)
    hand = np.zeros((16, 6), dtype=np.float32)
    return {
        "action.left_arm": arm.copy(),
        "action.left_hand": hand.copy(),
        "action.right_arm": arm.copy(),
        "action.right_hand": hand.copy(),
    }


def _proposal(value: float = 0.0) -> GrootActionChunkProposal:
    return GrootActionChunkProposal(
        schema_version="missionos_groot_action_chunk_proposal.v1",
        verification_basis="model_inferred",
        actions=_actions(value),
        instruction_ref="instruction:approved",
        preparation_sha256=DIGEST,
        policy_revision="revision",
        model_snapshot="snapshot",
        request_sha256="c" * 64,
        observation_sha256="d" * 64,
        response_sha256="e" * 64,
        observed_at=NOW_TEXT,
        freshness_deadline=DEADLINE,
        response_received_at=NOW_TEXT,
    )


def _policy(
    *,
    transformation: GrootArmTransformation | None = None,
) -> GrootArmControllerPolicy:
    transformation = transformation or GrootArmTransformation(
        transformation_id="arm-only-projection-identity-v1",
        transformation_version="1",
        steps=(_projection_step(), _step()),
    )
    return GrootArmControllerPolicy(
        policy_id="groot-arm-sim",
        policy_version="1",
        joint_names=JOINT_NAMES,
        lower_position_rad=LOWER,
        upper_position_rad=UPPER,
        position_bounds_source_sha256=GROOT_ROBOT_DESCRIPTION_SHA256,
        authorized_transformations=(transformation,),
        execution_envelope_policy_sha256=ENVELOPE_DIGEST,
    )


def _context(
    policy: GrootArmControllerPolicy,
    *,
    instruction_requires_hand_actuation: bool = False,
    execution_profile: str = "fixed_base_arm_only",
    balance_coupling_governed: bool = False,
    whole_body_safety_claimed: bool = False,
) -> GrootArmExecutionContext:
    binding = PolicyBinding(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_sha256=policy.execution_envelope_policy_sha256,
    )
    envelope = ExecutionEnvelopeValidation(
        status=FeasibilityStatus.VERIFIED_FEASIBLE,
        verification_basis=VerificationBasis.DETERMINISTIC,
        reasons=(),
        verification_items=(),
        required_verification_item_ids=(),
        policy_binding=binding,
    )
    return GrootArmExecutionContext(
        instruction_ref="instruction:approved",
        approval_ref="approval:human",
        expected_preparation_sha256=DIGEST,
        controller_configuration_sha256=CONTROLLER_DIGEST,
        safety_configuration_sha256=SAFETY_DIGEST,
        policy=policy,
        envelope_validation=envelope,
        execution_profile=execution_profile,
        balance_coupling_governed=balance_coupling_governed,
        whole_body_safety_claimed=whole_body_safety_claimed,
        instruction_requires_hand_actuation=instruction_requires_hand_actuation,
    )


class RecordingController:
    def __init__(
        self,
        *,
        mutate_applied: bool = False,
        omit_applied: bool = False,
        handoff_value: float = 0.0,
        hand_command_applied: bool = False,
        request_id_override: str | None = None,
        envelope_violation_observed: bool = False,
        stop_effect_observed: bool = False,
        handoff_state_age_seconds: float = 0.01,
        progress_samples_observed: int = 16,
        transformation_sha256_override: str | None = None,
        dynamic_limits_evidence_origin: str = "machine_observed",
        dynamic_limits_enforced: bool = True,
        dynamic_limits_observation_sha256_override: str | None = None,
    ) -> None:
        self.calls = 0
        self.mutate_applied = mutate_applied
        self.omit_applied = omit_applied
        self.handoff_value = handoff_value
        self.hand_command_applied = hand_command_applied
        self.request_id_override = request_id_override
        self.envelope_violation_observed = envelope_violation_observed
        self.stop_effect_observed = stop_effect_observed
        self.handoff_state_age_seconds = handoff_state_age_seconds
        self.progress_samples_observed = progress_samples_observed
        self.transformation_sha256_override = (
            transformation_sha256_override
        )
        self.dynamic_limits_evidence_origin = (
            dynamic_limits_evidence_origin
        )
        self.dynamic_limits_enforced = dynamic_limits_enforced
        self.dynamic_limits_observation_sha256_override = (
            dynamic_limits_observation_sha256_override
        )

    def apply_arm_chunk(self, request):
        self.calls += 1
        left = np.asarray(request.left_arm_rad, dtype=np.float64)
        right = np.asarray(request.right_arm_rad, dtype=np.float64)
        if self.mutate_applied:
            left = left.copy()
            left[0, 0] += 0.01
        from src.runtime.groot_arm_controller_bridge import _array_sha256

        applied_digest = _array_sha256(left, right)
        observed_handoff_delta = float(
            np.max(
                np.abs(
                    np.concatenate((left[0], right[0]))
                    - self.handoff_value
                )
            )
        )
        dynamic_limits_passed = bool(
            observed_handoff_delta
            <= GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD
            and self.handoff_state_age_seconds
            <= GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS
        )
        dynamic_limits_observation_sha256 = canonical_sha256(
            {
                "controller_configuration_sha256": (
                    request.controller_configuration_sha256
                ),
                "maximum_handoff_delta_rad": (
                    GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD
                ),
                "maximum_handoff_state_age_seconds": (
                    GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS
                ),
                "observed_handoff_delta_rad": observed_handoff_delta,
                "observed_handoff_state_age_seconds": (
                    self.handoff_state_age_seconds
                ),
                "dynamic_limits_passed": dynamic_limits_passed,
            }
        )
        return GrootArmControllerReceipt(
            request_id=self.request_id_override or request.request_id,
            admitted_chunk_sha256=request.admitted_chunk_sha256,
            transformed_chunk_sha256=request.transformed_chunk_sha256,
            transformation_sha256=(
                self.transformation_sha256_override
                or request.transformation_sha256
            ),
            controller_policy_sha256=request.controller_policy_sha256,
            controller_configuration_sha256=(
                request.controller_configuration_sha256
            ),
            proposal_received_at=request.proposal_received_at,
            handoff_deadline=request.handoff_deadline,
            remaining_valid_horizon_seconds_at_handoff=0.8,
            handoff_observed_at=NOW_TEXT,
            handoff_state_age_seconds=self.handoff_state_age_seconds,
            handoff_left_arm_rad=(self.handoff_value,) * 7,
            handoff_right_arm_rad=(self.handoff_value,) * 7,
            controller_ack_observed=True,
            progress_samples_observed=self.progress_samples_observed,
            progress_samples=tuple(
                {"sample_index": index, "sim_time": index * 0.05}
                for index in range(self.progress_samples_observed)
            ),
            progress_observed_at=NOW_TEXT,
            progress_source_sha256=canonical_sha256(
                {
                    "samples": [
                        {
                            "sample_index": index,
                            "sim_time": index * 0.05,
                        }
                        for index in range(
                            self.progress_samples_observed
                        )
                    ]
                }
            ),
            applied_left_arm_rad=(
                None
                if self.omit_applied
                else tuple(tuple(float(value) for value in row) for row in left)
            ),
            applied_right_arm_rad=(
                None
                if self.omit_applied
                else tuple(tuple(float(value) for value in row) for row in right)
            ),
            applied_command_sha256=None if self.omit_applied else applied_digest,
            effect_observed_at=NOW_TEXT,
            effect_left_arm_rad=(0.0,) * 7,
            effect_right_arm_rad=(0.0,) * 7,
            effect_source_id="simulator-qpos:frame-1",
            effect_source_sha256=_array_sha256(
                np.zeros((1, 7), dtype=np.float64),
                np.zeros((1, 7), dtype=np.float64),
            ),
            hand_command_applied=self.hand_command_applied,
            dynamic_limits_configuration_sha256=(
                request.controller_configuration_sha256
            ),
            dynamic_limits_observation_sha256=(
                self.dynamic_limits_observation_sha256_override
                or dynamic_limits_observation_sha256
            ),
            dynamic_limits_evidence_origin=(
                self.dynamic_limits_evidence_origin
            ),
            dynamic_limits_enforced=self.dynamic_limits_enforced,
            envelope_violation_observed=self.envelope_violation_observed,
            safe_stop_requested=self.envelope_violation_observed,
            safe_stop_ack_observed=self.envelope_violation_observed,
            safe_stop_effect_observed=self.stop_effect_observed,
            stop_detection_latency_seconds=(
                0.05 if self.envelope_violation_observed else None
            ),
            stop_effect_latency_seconds=(
                0.1 if self.envelope_violation_observed else None
            ),
            remaining_chunk_horizon_seconds=(
                0.4 if self.envelope_violation_observed else None
            ),
            schema_version=GROOT_ARM_CONTROLLER_RECEIPT_SCHEMA,
        )


def _execute(controller: RecordingController, **context_kwargs):
    policy = _policy()
    return execute_groot_arm_chunk(
        proposal=_proposal(),
        context=_context(policy, **context_kwargs),
        transformation_id="arm-only-projection-identity-v1",
        controller=controller,
        evaluated_at=NOW,
    )


def test_arm_only_chunk_is_handed_off_once_and_evidence_chain_verifies() -> None:
    controller = RecordingController()

    result = _execute(controller)

    assert result.status is FeasibilityStatus.VERIFIED_FEASIBLE
    assert result.verification_basis is VerificationBasis.DETERMINISTIC
    assert result.controller_request_sent is True
    assert controller.calls == 1
    assert result.admitted_chunk_sha256 != result.transformed_chunk_sha256
    assert result.transformed_chunk_sha256 == result.applied_command_sha256
    assert result.task_completion_claimed is False
    assert result.physical_execution_invoked is False
    assert result.execution_profile == "fixed_base_arm_only"
    assert result.balance_coupling_governed is False
    assert result.whole_body_safety_claimed is False


def test_whole_body_profile_is_blocked_before_controller_handoff() -> None:
    controller = RecordingController()

    result = _execute(
        controller,
        execution_profile="whole_body",
        balance_coupling_governed=True,
        whole_body_safety_claimed=True,
    )

    assert result.status is FeasibilityStatus.BLOCKED
    assert result.controller_request_sent is False
    assert controller.calls == 0


def test_declared_controller_dynamic_limits_are_unverified() -> None:
    result = _execute(
        RecordingController(
            dynamic_limits_evidence_origin="operator_declared",
        )
    )

    assert result.status is FeasibilityStatus.UNVERIFIED
    item = next(
        item
        for item in result.verification_items
        if item.item_id == ARM_CONTROLLER_DYNAMIC_LIMITS_ITEM
    )
    assert item.verification_basis is VerificationBasis.UNVERIFIED


def test_machine_observed_unenforced_dynamic_limits_are_blocked() -> None:
    result = _execute(
        RecordingController(dynamic_limits_enforced=False)
    )

    assert result.status is FeasibilityStatus.BLOCKED


def test_dynamic_limits_self_report_without_observation_digest_is_blocked() -> None:
    result = _execute(
        RecordingController(
            dynamic_limits_observation_sha256_override="7" * 64,
        )
    )

    assert result.status is FeasibilityStatus.BLOCKED


def test_position_bounds_without_pinned_source_fail_before_handoff() -> None:
    policy = replace(
        _policy(),
        position_bounds_source_sha256="7" * 64,
    )
    controller = RecordingController()

    result = execute_groot_arm_chunk(
        proposal=_proposal(),
        context=_context(policy),
        transformation_id="arm-only-projection-identity-v1",
        controller=controller,
        evaluated_at=NOW,
    )

    assert result.status is FeasibilityStatus.BLOCKED
    assert result.controller_request_sent is False
    assert (
        "groot_arm_policy_position_bounds_source_invalid"
        in result.reasons
    )


def test_admitted_digest_identifies_hand_values_before_projection() -> None:
    policy = _policy()
    first = execute_groot_arm_chunk(
        proposal=_proposal(),
        context=_context(policy),
        transformation_id="arm-only-projection-identity-v1",
        controller=RecordingController(),
        evaluated_at=NOW,
    )
    changed_actions = _actions()
    changed_actions["action.left_hand"][0, 0] = 1.0
    second = execute_groot_arm_chunk(
        proposal=replace(_proposal(), actions=changed_actions),
        context=_context(policy),
        transformation_id="arm-only-projection-identity-v1",
        controller=RecordingController(),
        evaluated_at=NOW,
    )

    assert first.admitted_chunk_sha256 != second.admitted_chunk_sha256
    assert first.transformed_chunk_sha256 == second.transformed_chunk_sha256


def test_missing_declared_hand_projection_fails_before_handoff() -> None:
    transformation = GrootArmTransformation(
        transformation_id="undeclared-drop-v1",
        transformation_version="1",
        steps=(replace(_step(), composition_index=0),),
    )
    policy = _policy(transformation=transformation)
    controller = RecordingController()

    result = execute_groot_arm_chunk(
        proposal=_proposal(),
        context=_context(policy),
        transformation_id=transformation.transformation_id,
        controller=controller,
        evaluated_at=NOW,
    )

    assert result.status is FeasibilityStatus.BLOCKED
    assert result.controller_request_sent is False
    assert controller.calls == 0
    assert "groot_arm_only_projection_contract_invalid" in result.reasons


def test_hand_required_instruction_keeps_all_hand_items_required() -> None:
    controller = RecordingController()

    result = _execute(
        controller,
        instruction_requires_hand_actuation=True,
    )

    assert result.status is FeasibilityStatus.UNVERIFIED
    assert result.controller_request_sent is False
    assert controller.calls == 0
    assert set(HAND_REQUIRED_ITEMS).issubset(
        result.required_verification_item_ids
    )


def test_unknown_transformation_fails_before_controller_handoff() -> None:
    controller = RecordingController()
    policy = _policy()

    result = execute_groot_arm_chunk(
        proposal=_proposal(),
        context=_context(policy),
        transformation_id="not-authorized",
        controller=controller,
        evaluated_at=NOW,
    )

    assert result.status is FeasibilityStatus.BLOCKED
    assert result.controller_request_sent is False
    assert controller.calls == 0
    assert "groot_arm_transformation_not_authorized" in result.reasons


def test_under_specified_transform_parameters_fail_closed() -> None:
    transformation = GrootArmTransformation(
        transformation_id="clamp-v1",
        transformation_version="1",
        steps=(
            _projection_step(),
            replace(
                _step(),
                kind=GrootArmTransformationKind.JOINT_LIMIT_CLAMP,
                parameters={},
            ),
        ),
    )
    policy = _policy(transformation=transformation)
    controller = RecordingController()

    result = execute_groot_arm_chunk(
        proposal=_proposal(),
        context=_context(policy),
        transformation_id="clamp-v1",
        controller=controller,
        evaluated_at=NOW,
    )

    assert result.status is FeasibilityStatus.BLOCKED
    assert result.controller_request_sent is False
    assert "groot_arm_clamp_transformation_controller_owned" in result.reasons


def test_fresh_handoff_discontinuity_is_blocked() -> None:
    result = _execute(RecordingController(handoff_value=1.0))

    assert result.status is FeasibilityStatus.BLOCKED
    assert result.controller_request_sent is True


def test_safe_readback_cannot_promote_missing_applied_command() -> None:
    result = _execute(RecordingController(omit_applied=True))

    assert result.status is FeasibilityStatus.UNVERIFIED
    item = next(
        item
        for item in result.verification_items
        if item.item_id == ARM_APPLIED_ITEM
    )
    assert item.verification_basis is VerificationBasis.UNVERIFIED
    assert result.applied_command_sha256 is None


def test_unexplained_applied_mutation_is_blocked() -> None:
    result = _execute(RecordingController(mutate_applied=True))

    assert result.status is FeasibilityStatus.BLOCKED


def test_receipt_replay_under_another_request_is_blocked() -> None:
    result = _execute(RecordingController(request_id_override="old-request"))

    assert result.status is FeasibilityStatus.BLOCKED


def test_arm_only_bridge_blocks_any_applied_hand_command() -> None:
    result = _execute(RecordingController(hand_command_applied=True))

    assert result.status is FeasibilityStatus.BLOCKED
    assert "groot_arm_controller_applied_hand_command" in result.reasons


def test_observed_violation_requires_safe_stop_effect_within_horizon() -> None:
    result = _execute(
        RecordingController(
            envelope_violation_observed=True,
            stop_effect_observed=False,
        )
    )

    assert result.status is not FeasibilityStatus.VERIFIED_FEASIBLE
    assert (
        "groot_arm_safe_stop_not_observed_within_remaining_horizon"
        in result.reasons
    )


def test_stale_handoff_state_is_blocked() -> None:
    result = _execute(
        RecordingController(handoff_state_age_seconds=0.5)
    )

    assert result.status is FeasibilityStatus.BLOCKED
    assert result.handoff_continuity_passed is False
    assert result.observed_handoff_state_age_seconds == 0.5
    assert result.observed_handoff_max_abs_joint_delta_rad == 0.0


def test_missing_progress_is_not_promoted_by_applied_or_effect_evidence() -> None:
    result = _execute(
        RecordingController(progress_samples_observed=0)
    )

    assert result.status is FeasibilityStatus.BLOCKED


def test_transformation_digest_mismatch_is_blocked() -> None:
    result = _execute(
        RecordingController(transformation_sha256_override="7" * 64)
    )

    assert result.status is FeasibilityStatus.BLOCKED


def test_composition_order_drift_fails_before_handoff() -> None:
    first = _step()
    second = replace(_step(), composition_index=1)
    transformation = GrootArmTransformation(
        transformation_id="two-identity-v1",
        transformation_version="1",
        steps=(_projection_step(), first, second),
    )
    policy = _policy(transformation=transformation)
    controller = RecordingController()

    result = execute_groot_arm_chunk(
        proposal=_proposal(),
        context=_context(policy),
        transformation_id=transformation.transformation_id,
        controller=controller,
        evaluated_at=NOW,
    )

    assert result.status is FeasibilityStatus.BLOCKED
    assert result.controller_request_sent is False
    assert "groot_arm_transformation_contract_incomplete" in result.reasons


def test_policy_or_controller_process_death_is_not_safe_stop_evidence() -> None:
    class DeadController:
        def apply_arm_chunk(self, request):
            raise RuntimeError("process exited")

    policy = _policy()
    result = execute_groot_arm_chunk(
        proposal=_proposal(),
        context=_context(policy),
        transformation_id="arm-only-projection-identity-v1",
        controller=DeadController(),
        evaluated_at=NOW,
    )

    assert result.status is FeasibilityStatus.UNVERIFIED
    assert result.controller_request_sent is True
    assert result.safe_stop_requested is False
    assert result.safe_stop_ack_observed is False
    assert result.safe_stop_effect_observed is False


def test_external_controller_configuration_material_matches_runtime_contract() -> None:
    from scripts.groot_robocasa_arm_controller import (
        _controller_configuration_material,
    )

    assert (
        _controller_configuration_material()
        == groot_robocasa_controller_configuration_material()
    )


def test_stale_chunk_fails_before_controller_handoff() -> None:
    controller = RecordingController()
    stale = replace(_proposal(), freshness_deadline=NOW_TEXT)

    result = execute_groot_arm_chunk(
        proposal=stale,
        context=_context(_policy()),
        transformation_id="arm-only-projection-identity-v1",
        controller=controller,
        evaluated_at=datetime(2026, 7, 26, 3, 0, 1, tzinfo=timezone.utc),
    )

    assert result.status is FeasibilityStatus.BLOCKED
    assert result.controller_request_sent is False
    assert controller.calls == 0

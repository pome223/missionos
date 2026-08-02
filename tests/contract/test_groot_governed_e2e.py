from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from missionos_core import (
    ExecutionEnvelopeValidation,
    FeasibilityStatus,
    HardwareExecutionMode,
    PolicyBinding,
    VerificationBasis,
    canonical_sha256,
)

from src.runtime.corpus_publication_sanitation import publication_findings
from src.runtime.groot_arm_controller_bridge import (
    ARM_FIELDS,
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
    _array_sha256,
    groot_robocasa_controller_configuration_sha256,
)
from src.runtime.groot_governed_e2e import (
    GROOT_ARM_HOLD_INSTRUCTION,
    GROOT_ARM_HOLD_INSTRUCTION_ID,
    GrootGovernedApproval,
    GrootGovernedE2EError,
    GrootSafeStopEvidenceSummary,
    build_groot_governed_preparation,
    run_groot_governed_e2e,
)
from src.runtime.groot_policy_client import (
    GROOT_MODEL_SNAPSHOT,
    GROOT_REPOSITORY_REVISION,
    GrootPolicyBinding,
    GrootPolicyBoundaryError,
    GrootZmqPolicyTransport,
    _digest_payload,
    build_groot_sim_freshness_policy,
)


NOW = datetime.now(timezone.utc)
CONTROLLER_DIGEST = groot_robocasa_controller_configuration_sha256()
SAFETY_DIGEST = "b" * 64
ENVELOPE_DIGEST = "e" * 64
TRANSFORMATION_ID = "arm-only-projection-identity-v1"


class _Clock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


class _PolicyTransport:
    def __init__(
        self,
        *,
        non_finite: bool = False,
        fail: bool = False,
        malformed: bool = False,
    ) -> None:
        self.calls = 0
        self.non_finite = non_finite
        self.fail = fail
        self.malformed = malformed

    def get_action(self, payload):
        self.calls += 1
        if self.fail:
            raise GrootPolicyBoundaryError("groot_transport_timeout")
        left = np.repeat(
            np.asarray(payload["state.left_arm"], dtype=np.float32),
            16,
            axis=0,
        )
        right = np.repeat(
            np.asarray(payload["state.right_arm"], dtype=np.float32),
            16,
            axis=0,
        )
        if self.non_finite:
            left[0, 0] = np.nan
        hand = np.zeros((16, 6), dtype=np.float32)
        response = {
            "action.left_arm": left,
            "action.left_hand": hand.copy(),
            "action.right_arm": right,
            "action.right_hand": hand.copy(),
        }
        if self.malformed:
            response.pop("action.right_hand")
        return response


class _ZmqPolicyTransportFixture(GrootZmqPolicyTransport):
    """No socket or model; exercises source-bound runtime evidence wiring."""

    def __init__(self) -> None:
        super().__init__(endpoint="tcp://127.0.0.1:5567", timeout_ms=100)

    def get_action(self, payload):
        response = _PolicyTransport().get_action(payload)
        request_sha256 = _digest_payload(payload, include_instruction=True)
        response_sha256 = _digest_payload(
            response,
            include_instruction=False,
        )
        stdout_preimage = json.dumps(
            {
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        now = datetime.now(timezone.utc).isoformat()
        self._runtime_invocation_evidence = {
            "schema_version": "runtime_invocation_evidence.v1",
            "invocation_kind": "llm_api",
            "invocation_target": "groot_n1_5:tcp://127.0.0.1:5567",
            "invocation_started_at": now,
            "invocation_completed_at": now,
            "invocation_exit_code": 0,
            "invocation_stdout_preimage": stdout_preimage,
            "invocation_stdout_sha256": hashlib.sha256(
                stdout_preimage.encode()
            ).hexdigest(),
            "invocation_stderr_preimage": "",
            "invocation_stderr_sha256": hashlib.sha256(b"").hexdigest(),
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "policy_revision": GROOT_REPOSITORY_REVISION,
            "model_snapshot": GROOT_MODEL_SNAPSHOT,
            "execution_scope": "sim",
        }
        return response


class _MissingRuntimeTimingTransport(_ZmqPolicyTransportFixture):
    def get_action(self, payload):
        response = super().get_action(payload)
        self._runtime_invocation_evidence.pop("invocation_completed_at")
        return response


class _ControllerMutation:
    def __init__(self, base, *, mutation: str) -> None:
        self.base = base
        self.mutation = mutation

    def observe_handoff_state(self):
        if self.mutation == "predispatch_handoff_incompatible":
            return {
                **self.base.observe_handoff_state(),
                "left_arm_rad": [1.0] * 7,
                "right_arm_rad": [1.0] * 7,
            }
        if self.mutation == "predispatch_observation_precedes_response":
            return {
                **self.base.observe_handoff_state(),
                "observed_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )
                .isoformat()
                .replace("+00:00", "Z"),
            }
        return self.base.observe_handoff_state()

    def apply_arm_chunk(self, request):
        if self.mutation == "missing_receipt":
            raise RuntimeError("receipt unavailable")
        receipt = self.base.apply_arm_chunk(request)
        if self.mutation == "missing_applied":
            return replace(
                receipt,
                applied_left_arm_rad=None,
                applied_right_arm_rad=None,
                applied_command_sha256=None,
            )
        if self.mutation == "handoff_discontinuity":
            return replace(
                receipt,
                handoff_left_arm_rad=(1.0,) * 7,
                handoff_right_arm_rad=(1.0,) * 7,
            )
        return receipt


class _DeterministicController:
    """In-process unit fixture; process timing is covered by runtime smokes."""

    def observe_handoff_state(self):
        return {
            "schema_version": "missionos_groot_robocasa_handoff_state.v1",
            "environment_id": "contract-fixture",
            "left_arm_rad": [0.0] * 7,
            "right_arm_rad": [0.0] * 7,
            "observed_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "execution_scope": "sim",
            "physical_execution_invoked": False,
        }

    def apply_arm_chunk(self, request):
        left = np.asarray(request.left_arm_rad, dtype=np.float64)
        right = np.asarray(request.right_arm_rad, dtype=np.float64)
        received_at = datetime.fromisoformat(
            request.proposal_received_at.replace("Z", "+00:00")
        )
        deadline = datetime.fromisoformat(
            request.handoff_deadline.replace("Z", "+00:00")
        )
        handoff_at = received_at + timedelta(milliseconds=1)
        observed_at = handoff_at.isoformat().replace("+00:00", "Z")
        progress = tuple(
            {"sample_index": index, "sim_time": index * 0.05}
            for index in range(16)
        )
        effect_digest = _array_sha256(
            np.zeros((1, 7), dtype=np.float64),
            np.zeros((1, 7), dtype=np.float64),
        )
        handoff = np.zeros(14, dtype=np.float64)
        observed_handoff_delta = float(
            np.max(
                np.abs(
                    np.concatenate((left[0], right[0])) - handoff
                )
            )
        )
        dynamic_limits_passed = bool(
            observed_handoff_delta
            <= GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD
            and 0.001
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
                "observed_handoff_state_age_seconds": 0.001,
                "dynamic_limits_passed": dynamic_limits_passed,
            }
        )
        return GrootArmControllerReceipt(
            request_id=request.request_id,
            admitted_chunk_sha256=request.admitted_chunk_sha256,
            transformed_chunk_sha256=request.transformed_chunk_sha256,
            transformation_sha256=request.transformation_sha256,
            controller_policy_sha256=request.controller_policy_sha256,
            controller_configuration_sha256=(
                request.controller_configuration_sha256
            ),
            proposal_received_at=request.proposal_received_at,
            handoff_deadline=request.handoff_deadline,
            remaining_valid_horizon_seconds_at_handoff=(
                deadline - handoff_at
            ).total_seconds(),
            handoff_observed_at=observed_at,
            handoff_state_age_seconds=0.001,
            handoff_left_arm_rad=(0.0,) * 7,
            handoff_right_arm_rad=(0.0,) * 7,
            controller_ack_observed=True,
            progress_samples_observed=16,
            progress_samples=progress,
            progress_observed_at=observed_at,
            progress_source_sha256=canonical_sha256(
                {"samples": [dict(sample) for sample in progress]}
            ),
            applied_left_arm_rad=tuple(
                tuple(float(value) for value in row)
                for row in left
            ),
            applied_right_arm_rad=tuple(
                tuple(float(value) for value in row)
                for row in right
            ),
            applied_command_sha256=_array_sha256(left, right),
            effect_observed_at=observed_at,
            effect_left_arm_rad=(0.0,) * 7,
            effect_right_arm_rad=(0.0,) * 7,
            effect_source_id="simulator-qpos:test-frame",
            effect_source_sha256=effect_digest,
            hand_command_applied=False,
            dynamic_limits_configuration_sha256=(
                request.controller_configuration_sha256
            ),
            dynamic_limits_observation_sha256=(
                dynamic_limits_observation_sha256
            ),
            dynamic_limits_evidence_origin="machine_observed",
            dynamic_limits_enforced=True,
            schema_version=GROOT_ARM_CONTROLLER_RECEIPT_SCHEMA,
        )


def _transformation() -> GrootArmTransformation:
    return GrootArmTransformation(
        transformation_id=TRANSFORMATION_ID,
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
                policy_source_sha256=ENVELOPE_DIGEST,
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
                policy_source_sha256=ENVELOPE_DIGEST,
            ),
        ),
    )


def _policy() -> GrootArmControllerPolicy:
    return GrootArmControllerPolicy(
        policy_id="groot-e2e-test",
        policy_version="1",
        joint_names=tuple(f"joint_{index}" for index in range(14)),
        lower_position_rad=(-3.0,) * 14,
        upper_position_rad=(3.0,) * 14,
        position_bounds_source_sha256=GROOT_ROBOT_DESCRIPTION_SHA256,
        authorized_transformations=(_transformation(),),
        execution_envelope_policy_sha256=ENVELOPE_DIGEST,
    )


def _preparation(
    policy: GrootArmControllerPolicy,
    *,
    now: datetime = NOW,
):
    freshness_policy = build_groot_sim_freshness_policy()
    return build_groot_governed_preparation(
        run_ref="groot-e2e-run:test-001",
        instruction_allowlist_id=GROOT_ARM_HOLD_INSTRUCTION_ID,
        controller_configuration_sha256=CONTROLLER_DIGEST,
        safety_configuration_sha256=SAFETY_DIGEST,
        envelope_policy_sha256=policy.execution_envelope_policy_sha256,
        freshness_policy_sha256=freshness_policy.policy_sha256,
        transformation_id=TRANSFORMATION_ID,
        prepared_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=30),
    )


def _context(policy, preparation, approval_ref):
    return GrootArmExecutionContext(
        instruction_ref=preparation.instruction_ref,
        approval_ref=approval_ref,
        expected_preparation_sha256=preparation.preparation_sha256,
        controller_configuration_sha256=CONTROLLER_DIGEST,
        safety_configuration_sha256=SAFETY_DIGEST,
        policy=policy,
        envelope_validation=ExecutionEnvelopeValidation(
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
        ),
    )


def _payload():
    return {
        "annotation.human.action.task_description": [
            GROOT_ARM_HOLD_INSTRUCTION
        ],
        "state.left_arm": np.zeros((1, 7), dtype=np.float64),
        "state.left_hand": np.zeros((1, 6), dtype=np.float64),
        "state.right_arm": np.zeros((1, 7), dtype=np.float64),
        "state.right_hand": np.zeros((1, 6), dtype=np.float64),
        "video.ego_view": np.zeros((1, 256, 256, 3), dtype=np.uint8),
    }


def _run(
    tmp_path: Path,
    monkeypatch,
    *,
    approval_change=None,
    preparation_change=None,
    preparation_expired=False,
    transport=None,
    binding_change=None,
    binding_expired=False,
    policy_response_delay_ms=0,
    evaluated_at_offset_ms=20,
    controller_mutation=None,
    policy_change=None,
    envelope_status=FeasibilityStatus.VERIFIED_FEASIBLE,
):
    base = datetime.now(timezone.utc)
    policy = _policy()
    if policy_change:
        policy = replace(policy, **policy_change)
    preparation = _preparation(policy, now=base)
    if preparation_expired:
        preparation = replace(
            preparation,
            expires_at=(
                base - timedelta(milliseconds=1)
            )
            .isoformat()
            .replace("+00:00", "Z"),
        )
        preparation_digest = canonical_sha256(preparation.material())
        preparation = replace(
            preparation,
            preparation_sha256=preparation_digest,
            preparation_ref=(
                f"groot-e2e-preparation:{preparation_digest[:16]}"
            ),
        )
    if preparation_change:
        preparation = replace(preparation, **preparation_change)
        if not {
            "preparation_sha256",
            "preparation_ref",
        }.intersection(preparation_change):
            preparation_digest = canonical_sha256(
                preparation.material()
            )
            preparation = replace(
                preparation,
                preparation_sha256=preparation_digest,
                preparation_ref=(
                    f"groot-e2e-preparation:{preparation_digest[:16]}"
                ),
            )
    approval_ref = "groot-e2e-approval:test-001"
    approval = GrootGovernedApproval(
        run_ref=preparation.run_ref,
        instruction_ref=preparation.instruction_ref,
        preparation_ref=preparation.preparation_ref,
        preparation_sha256=preparation.preparation_sha256,
        operator_approval_ref=approval_ref,
        approved_at=(base - timedelta(milliseconds=500))
        .isoformat()
        .replace("+00:00", "Z"),
        expires_at=(base + timedelta(seconds=20))
        .isoformat()
        .replace("+00:00", "Z"),
    )
    if approval_change:
        approval = replace(approval, **approval_change)
    binding = GrootPolicyBinding(
        instruction_ref=preparation.instruction_ref,
        preparation_sha256=preparation.preparation_sha256,
        observed_at=(base - timedelta(milliseconds=100))
        .isoformat()
        .replace("+00:00", "Z"),
        freshness_deadline=(
            base
            - timedelta(milliseconds=100)
            + timedelta(
                seconds=build_groot_sim_freshness_policy()
                .maximum_observation_age_seconds
            )
        )
        .isoformat()
        .replace("+00:00", "Z"),
        freshness_policy=build_groot_sim_freshness_policy(),
    )
    if binding_expired:
        stale_observed_at = base - timedelta(seconds=4)
        binding = replace(
            binding,
            observed_at=stale_observed_at.isoformat().replace(
                "+00:00", "Z"
            ),
            freshness_deadline=(
                stale_observed_at
                + timedelta(
                    seconds=(
                        binding.freshness_policy
                        .maximum_observation_age_seconds
                    )
                )
            )
            .isoformat()
            .replace("+00:00", "Z"),
        )
    if binding_change:
        binding = replace(binding, **binding_change)
    controller = _DeterministicController()
    if controller_mutation:
        controller = _ControllerMutation(
            controller,
            mutation=controller_mutation,
        )
    context = _context(policy, preparation, approval_ref)
    if envelope_status is not FeasibilityStatus.VERIFIED_FEASIBLE:
        context = replace(
            context,
            envelope_validation=replace(
                context.envelope_validation,
                status=envelope_status,
                verification_basis=VerificationBasis.UNVERIFIED,
                reasons=("safe_stop_receipt_expired",),
            ),
        )
    return run_groot_governed_e2e(
        preparation=preparation,
        approval=approval,
        policy_payload=_payload(),
        policy_binding=binding,
        policy_transport=transport or _PolicyTransport(),
        policy_clock=_Clock(
            base,
            base + timedelta(milliseconds=policy_response_delay_ms),
        ),
        controller=controller,
        controller_context=context,
        safe_stop_summary=GrootSafeStopEvidenceSummary(
            receipt_ref="safe-stop:test-001",
            receipt_sha256="f" * 64,
            request_observed=True,
            ack_observed=True,
            effect_observed=True,
            capability_evidenced=True,
            execution_scope=HardwareExecutionMode.SIM,
        ),
        authority_state_path=tmp_path / "authority.json",
        evaluated_at=base + timedelta(
            milliseconds=evaluated_at_offset_ms
        ),
        limitations=("contract test fixture",),
    )


def test_complete_run_keeps_all_authority_and_evidence_stages_separate(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(tmp_path, monkeypatch)

    assert report["status"] == "verified_fixture_execution_evidence"
    assert report["approval_single_use_consumed"] is True
    assert report["approval_replay_blocked"] is True
    assert report["policy_service"]["model_runtime_invoked"] is False
    assert (
        report["policy_service"]["effective_verification_basis"]
        == "unverified"
    )
    assert report["policy_service"]["dispatch_request_sent"] is False
    assert (
        report["policy_service"]["freshness_policy_sha256"]
        == build_groot_sim_freshness_policy().policy_sha256
    )
    assert (
        report["policy_service"]["manufacturer_limit_claimed"] is False
    )
    timing = report["timing"]
    assert timing["observation_observed_at"] is not None
    assert timing["policy_request_started_at"] is not None
    assert timing["policy_response_received_at"] is not None
    assert timing["joint_revalidation_observed_at"] is not None
    assert timing["dispatch_authority_validated_at"] is not None
    assert (
        datetime.fromisoformat(
            timing["observation_observed_at"].replace("Z", "+00:00")
        )
        <= datetime.fromisoformat(
            timing["policy_request_started_at"].replace("Z", "+00:00")
        )
        <= datetime.fromisoformat(
            timing["policy_response_received_at"].replace("Z", "+00:00")
        )
        <= datetime.fromisoformat(
            timing["joint_revalidation_observed_at"].replace("Z", "+00:00")
        )
        <= datetime.fromisoformat(
            timing["dispatch_authority_validated_at"].replace(
                "Z", "+00:00"
            )
        )
    )
    assert report["pre_dispatch_verification"]["status"] == (
        "verified_feasible"
    )
    assert {
        item["item_id"]: item["status"]
        for item in report["pre_dispatch_verification"]["items"]
    } == {
        "groot_response_schema_valid": "pass",
        "groot_temporal_freshness_valid": "pass",
        "groot_joint_state_compatible": "pass",
        "groot_object_state_compatible": "pass",
    }
    assert report["execution"]["controller_request_sent"] is True
    assert report["execution"]["controller_ack_observed"] is True
    assert report["execution"]["progress_observed"] is True
    assert report["execution"]["effect_observed"] is True
    assert report["execution"]["handoff_continuity"] == {
        "passed": True,
        "observed_max_abs_joint_delta_rad": 0.0,
        "maximum_abs_joint_delta_rad": 0.25,
        "observed_state_age_seconds": 0.001,
        "maximum_state_age_seconds": 0.05,
    }
    assert (
        report["execution"]["execution_profile"]
        == "fixed_base_arm_only"
    )
    assert report["execution"]["balance_coupling_governed"] is False
    assert report["execution"]["whole_body_safety_claimed"] is False
    assert report["semantic_completion"]["claimed"] is False
    assert not any(
        report["semantic_completion"]["negative_cases"].values()
    )
    assert report["physical_execution_invoked"] is False
    assert publication_findings(report) == []


def test_approval_mismatch_fails_before_policy_or_controller(
    tmp_path,
    monkeypatch,
) -> None:
    transport = _PolicyTransport()
    with pytest.raises(
        GrootGovernedE2EError,
        match="groot_e2e_approval_invalid:scope",
    ):
        _run(
            tmp_path,
            monkeypatch,
            approval_change={"preparation_sha256": "0" * 64},
            transport=transport,
        )
    assert transport.calls == 0


def test_expired_instruction_fails_before_policy_or_controller(
    tmp_path,
    monkeypatch,
) -> None:
    transport = _PolicyTransport()
    with pytest.raises(
        GrootGovernedE2EError,
        match="groot_e2e_instruction_expired",
    ):
        _run(
            tmp_path,
            monkeypatch,
            preparation_expired=True,
            transport=transport,
        )
    assert transport.calls == 0


def test_approved_preparation_binds_freshness_policy_digest(
    tmp_path,
    monkeypatch,
) -> None:
    transport = _PolicyTransport()
    with pytest.raises(
        GrootGovernedE2EError,
        match="groot_e2e_freshness_policy_binding_mismatch",
    ):
        _run(
            tmp_path,
            monkeypatch,
            preparation_change={"freshness_policy_sha256": "0" * 64},
            transport=transport,
        )
    assert transport.calls == 0


def test_source_bound_runtime_reports_round_trip_not_inference_time(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        transport=_ZmqPolicyTransportFixture(),
    )

    assert report["status"] == "verified_execution_evidence"
    assert report["policy_service"]["model_runtime_invoked"] is True
    assert report["policy_service"]["invocation_started_at"] is not None
    assert report["policy_service"]["invocation_completed_at"] is not None
    assert (
        report["policy_service"]["policy_service_round_trip_seconds"]
        == 0.0
    )
    assert "inference_time_seconds" not in report["policy_service"]


def test_missing_runtime_timing_cannot_create_model_runtime_claim(
    tmp_path,
    monkeypatch,
) -> None:
    with pytest.raises(
        GrootGovernedE2EError,
        match="groot_e2e_policy_runtime_evidence_invalid",
    ):
        _run(
            tmp_path,
            monkeypatch,
            transport=_MissingRuntimeTimingTransport(),
        )


def test_policy_process_termination_sends_no_controller_request(
    tmp_path,
    monkeypatch,
) -> None:
    transport = _PolicyTransport(fail=True)
    with pytest.raises(
        GrootPolicyBoundaryError,
        match="groot_transport_timeout",
    ):
        _run(tmp_path, monkeypatch, transport=transport)
    assert transport.calls == 1


def test_non_finite_policy_output_fails_before_controller_handoff(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        transport=_PolicyTransport(non_finite=True),
    )

    assert report["status"] == "blocked"
    assert report["policy_service"]["response_schema_valid"] is False
    assert report["policy_service"]["temporal_freshness_valid"] is True
    assert report["execution"]["controller_request_sent"] is False
    schema_item = report["pre_dispatch_verification"]["items"][0]
    assert schema_item["status"] == "fail"
    assert schema_item["reason"] == (
        "groot_response_action_left_arm_non_finite"
    )


def test_malformed_policy_output_fails_before_controller_handoff(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        transport=_PolicyTransport(malformed=True),
    )

    assert report["status"] == "blocked"
    assert report["policy_service"]["response_schema_valid"] is False
    assert report["policy_service"]["temporal_freshness_valid"] is True
    assert report["execution"]["controller_request_sent"] is False
    schema_item = report["pre_dispatch_verification"]["items"][0]
    assert schema_item["status"] == "fail"
    assert schema_item["reason"] == "groot_response_fields_invalid"


def test_stale_observation_fails_at_policy_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    with pytest.raises(
        GrootPolicyBoundaryError,
        match="groot_observation_stale",
    ):
        _run(
            tmp_path,
            monkeypatch,
            binding_expired=True,
        )


def test_response_schema_and_freshness_items_block_before_authority(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        policy_response_delay_ms=3000,
    )

    assert report["status"] == "blocked"
    assert report["approval_single_use_consumed"] is False
    assert report["policy_service"]["service_response_received"] is True
    assert report["policy_service"]["response_schema_valid"] is True
    assert report["policy_service"]["temporal_freshness_valid"] is False
    assert report["execution"]["controller_request_sent"] is False
    items = {
        item["item_id"]: item
        for item in report["pre_dispatch_verification"]["items"]
    }
    assert items["groot_response_schema_valid"]["status"] == "pass"
    assert items["groot_temporal_freshness_valid"]["status"] == "fail"
    assert items["groot_joint_state_compatible"]["status"] == "pending"
    assert (
        items["groot_joint_state_compatible"]["verification_basis"]
        == "unverified"
    )
    assert (
        items["groot_joint_state_compatible"]["reason"]
        == "prerequisite_policy_response_not_accepted"
    )
    assert items["groot_object_state_compatible"]["status"] == "pass"


def test_source_bound_runtime_evidence_survives_stale_rejection(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        transport=_ZmqPolicyTransportFixture(),
        policy_response_delay_ms=3000,
    )

    assert report["status"] == "blocked"
    assert report["policy_service"]["model_runtime_invoked"] is True
    assert (
        report["policy_service"]["runtime_invocation_evidence_valid"] is True
    )
    assert report["policy_service"]["response_schema_valid"] is True
    assert report["policy_service"]["temporal_freshness_valid"] is False
    assert report["execution"]["controller_request_sent"] is False


def test_joint_state_incompatibility_blocks_before_authority(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        controller_mutation="predispatch_handoff_incompatible",
    )

    assert report["status"] == "blocked"
    assert report["approval_single_use_consumed"] is False
    assert report["execution"]["controller_request_sent"] is False
    items = {
        item["item_id"]: item
        for item in report["pre_dispatch_verification"]["items"]
    }
    assert items["groot_joint_state_compatible"]["status"] == "fail"
    assert items["groot_joint_state_compatible"]["reason"] == (
        "controller_joint_state_changed_since_policy_input"
    )


def test_joint_revalidation_before_response_blocks_before_authority(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        controller_mutation="predispatch_observation_precedes_response",
    )

    assert report["status"] == "blocked"
    assert report["approval_single_use_consumed"] is False
    assert report["execution"]["controller_request_sent"] is False
    items = {
        item["item_id"]: item
        for item in report["pre_dispatch_verification"]["items"]
    }
    assert items["groot_joint_state_compatible"]["reason"] == (
        "controller_joint_revalidation_precedes_policy_response"
    )


def test_exhausted_chunk_is_blocked_before_controller_handoff(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        evaluated_at_offset_ms=900,
    )

    assert report["status"] == "blocked"
    assert report["execution"]["controller_request_sent"] is False


def test_missing_controller_receipt_remains_unverified(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        controller_mutation="missing_receipt",
    )

    assert report["status"] == "unverified"
    assert report["execution"]["controller_request_sent"] is True
    assert "groot_arm_controller_receipt_unavailable" in (
        report["execution"]["reasons"]
    )
    assert report["execution"]["handoff_continuity"]["passed"] is None


def test_handoff_discontinuity_reports_observed_values(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        controller_mutation="handoff_discontinuity",
    )

    continuity = report["execution"]["handoff_continuity"]
    assert report["status"] == "blocked"
    assert continuity["passed"] is False
    assert continuity["observed_max_abs_joint_delta_rad"] == 1.0
    assert continuity["maximum_abs_joint_delta_rad"] == 0.25
    assert continuity["observed_state_age_seconds"] == 0.001
    assert continuity["maximum_state_age_seconds"] == 0.05


def test_expired_safe_stop_envelope_blocks_authority_consumption(
    tmp_path,
    monkeypatch,
) -> None:
    with pytest.raises(
        GrootGovernedE2EError,
        match="groot_e2e_dispatch_authority_blocked",
    ):
        _run(
            tmp_path,
            monkeypatch,
            envelope_status=FeasibilityStatus.UNVERIFIED,
        )


def test_scope_mismatch_is_blocked(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        policy_change={"execution_scope": HardwareExecutionMode.LOOPBACK},
    )

    assert report["status"] == "blocked"
    assert "groot_arm_policy_scope_not_sim" in report["execution"]["reasons"]


def test_unauthorized_transformation_fails_before_policy_request(
    tmp_path,
    monkeypatch,
) -> None:
    transport = _PolicyTransport()
    with pytest.raises(
        GrootGovernedE2EError,
        match="groot_e2e_preparation_transformation_not_authorized",
    ):
        _run(
            tmp_path,
            monkeypatch,
            preparation_change={"transformation_id": "unknown"},
            transport=transport,
        )
    assert transport.calls == 0


def test_missing_applied_command_cannot_be_promoted_by_effect(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        controller_mutation="missing_applied",
    )

    assert report["status"] == "unverified"
    assert report["execution"]["effect_observed"] is True
    assert report["execution"]["applied_command_sha256"] is None


def test_handoff_discontinuity_is_blocked(
    tmp_path,
    monkeypatch,
) -> None:
    report = _run(
        tmp_path,
        monkeypatch,
        controller_mutation="handoff_discontinuity",
    )

    assert report["status"] == "blocked"
    assert report["execution"]["controller_request_sent"] is True

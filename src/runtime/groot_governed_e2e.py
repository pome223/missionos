"""Govern one GR00T proposal through the simulator evidence boundary.

This module composes existing policy-client, dispatch-authority, controller,
envelope, and verifier contracts. It does not create model, approval,
controller, safe-stop, or semantic-completion authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np
from missionos_core import (
    EvidenceOrigin,
    EvidenceSourceRef,
    FeasibilityStatus,
    HardwareExecutionMode,
    VerificationBasis,
    VerificationItem,
    VerificationItemStatus,
    aggregate_verification_items,
    canonical_sha256,
)

from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.runtime.corpus_publication_sanitation import publication_findings
from src.runtime.groot_arm_controller_bridge import (
    GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD,
    GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS,
    GrootArmController,
    GrootArmControllerResult,
    GrootArmExecutionContext,
    execute_groot_arm_chunk,
)
from src.runtime.groot_policy_client import (
    GROOT_MODEL_SNAPSHOT,
    GROOT_REPOSITORY_REVISION,
    GrootActionChunkProposal,
    GrootFreshnessPolicy,
    GrootPolicyBinding,
    GrootPolicyResponseAssessment,
    GrootPolicyTransport,
    GrootZmqPolicyTransport,
    assess_groot_action_chunk,
)
from src.runtime.runtime_claim_evidence import (
    RuntimeClaimValidationError,
    validate_runtime_invocation_evidence,
)


GROOT_GOVERNED_E2E_REPORT_SCHEMA = "missionos_groot_governed_e2e_report.v1"
GROOT_GOVERNED_PREPARATION_SCHEMA = "missionos_groot_governed_preparation.v1"
GROOT_GOVERNED_APPROVAL_SCHEMA = "missionos_groot_governed_approval.v1"
GROOT_ARM_HOLD_INSTRUCTION_ID = "groot-arm-hold-current-pose.v1"
GROOT_ARM_HOLD_INSTRUCTION = "hold the current arm pose"
GROOT_GOVERNED_INSTRUCTION_ALLOWLIST = MappingProxyType(
    {GROOT_ARM_HOLD_INSTRUCTION_ID: GROOT_ARM_HOLD_INSTRUCTION}
)
SEMANTIC_COMPLETION_NEGATIVE_CASES = (
    "joint_target_reached",
    "controller_ack_received",
    "motion_observed",
    "chunk_exhausted",
    "instruction_expired",
    "operator_stop",
    "safe_stop_effect_observed",
    "policy_timeout_or_process_termination",
    "deterministic_arm_envelope_passed",
)
RESPONSE_SCHEMA_ITEM = "groot_response_schema_valid"
TEMPORAL_FRESHNESS_ITEM = "groot_temporal_freshness_valid"
JOINT_STATE_COMPATIBILITY_ITEM = "groot_joint_state_compatible"
OBJECT_STATE_COMPATIBILITY_ITEM = "groot_object_state_compatible"
PRE_DISPATCH_REQUIRED_ITEM_IDS = (
    RESPONSE_SCHEMA_ITEM,
    TEMPORAL_FRESHNESS_ITEM,
    JOINT_STATE_COMPATIBILITY_ITEM,
    OBJECT_STATE_COMPATIBILITY_ITEM,
)


class GrootGovernedE2EError(ValueError):
    """A preparation, approval, authority, or report boundary failed closed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _parse_utc(value: str, *, reason: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise GrootGovernedE2EError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GrootGovernedE2EError(reason)
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GrootGovernedE2EError("groot_e2e_time_timezone_missing")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_ref(value: str, *, prefix: str) -> bool:
    return bool(value) and value.startswith(prefix) and all(
        character.isalnum() or character in ".:_-"
        for character in value
    )


@dataclass(frozen=True)
class GrootGovernedPreparation:
    """Immutable bounded instruction and runtime selection; sends nothing."""

    run_ref: str
    instruction_ref: str
    instruction_allowlist_id: str
    instruction_sha256: str
    preparation_ref: str
    preparation_sha256: str
    controller_configuration_sha256: str
    safety_configuration_sha256: str
    envelope_policy_sha256: str
    freshness_policy_sha256: str
    transformation_id: str
    prepared_at: str
    expires_at: str
    execution_scope: HardwareExecutionMode = HardwareExecutionMode.SIM
    hand_actuation_allowed: bool = False
    schema_version: str = GROOT_GOVERNED_PREPARATION_SCHEMA

    def material(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_ref": self.run_ref,
            "instruction_ref": self.instruction_ref,
            "instruction_allowlist_id": self.instruction_allowlist_id,
            "instruction_sha256": self.instruction_sha256,
            "controller_configuration_sha256": (
                self.controller_configuration_sha256
            ),
            "safety_configuration_sha256": self.safety_configuration_sha256,
            "envelope_policy_sha256": self.envelope_policy_sha256,
            "freshness_policy_sha256": self.freshness_policy_sha256,
            "transformation_id": self.transformation_id,
            "prepared_at": self.prepared_at,
            "expires_at": self.expires_at,
            "execution_scope": self.execution_scope.value,
            "hand_actuation_allowed": self.hand_actuation_allowed,
            "policy_revision": GROOT_REPOSITORY_REVISION,
            "model_snapshot": GROOT_MODEL_SNAPSHOT,
        }


def build_groot_governed_preparation(
    *,
    run_ref: str,
    instruction_allowlist_id: str,
    controller_configuration_sha256: str,
    safety_configuration_sha256: str,
    envelope_policy_sha256: str,
    freshness_policy_sha256: str,
    transformation_id: str,
    prepared_at: datetime,
    expires_at: datetime,
) -> GrootGovernedPreparation:
    """Build one source-bound preparation without creating approval."""

    if not _valid_ref(run_ref, prefix="groot-e2e-run:"):
        raise GrootGovernedE2EError("groot_e2e_run_ref_invalid")
    try:
        instruction = GROOT_GOVERNED_INSTRUCTION_ALLOWLIST[
            instruction_allowlist_id
        ]
    except KeyError as exc:
        raise GrootGovernedE2EError(
            "groot_e2e_instruction_not_allowlisted"
        ) from exc
    prepared = _parse_utc(
        _utc_text(prepared_at),
        reason="groot_e2e_preparation_time_invalid",
    )
    expires = _parse_utc(
        _utc_text(expires_at),
        reason="groot_e2e_preparation_expiry_invalid",
    )
    if expires <= prepared:
        raise GrootGovernedE2EError("groot_e2e_preparation_expiry_invalid")
    instruction_ref = f"groot-e2e-instruction:{run_ref.rsplit(':', 1)[-1]}"
    base = GrootGovernedPreparation(
        run_ref=run_ref,
        instruction_ref=instruction_ref,
        instruction_allowlist_id=instruction_allowlist_id,
        instruction_sha256=canonical_sha256({"instruction": instruction}),
        preparation_ref="",
        preparation_sha256="",
        controller_configuration_sha256=controller_configuration_sha256,
        safety_configuration_sha256=safety_configuration_sha256,
        envelope_policy_sha256=envelope_policy_sha256,
        freshness_policy_sha256=freshness_policy_sha256,
        transformation_id=transformation_id,
        prepared_at=_utc_text(prepared),
        expires_at=_utc_text(expires),
    )
    digest = canonical_sha256(base.material())
    return replace(
        base,
        preparation_ref=f"groot-e2e-preparation:{digest[:16]}",
        preparation_sha256=digest,
    )


@dataclass(frozen=True)
class GrootGovernedApproval:
    """Human-provided approval bound to one preparation and instruction."""

    run_ref: str
    instruction_ref: str
    preparation_ref: str
    preparation_sha256: str
    operator_approval_ref: str
    approved_at: str
    expires_at: str
    operator_approved: bool = True
    automatic_dispatch_executed: bool = False
    schema_version: str = GROOT_GOVERNED_APPROVAL_SCHEMA


@dataclass(frozen=True)
class GrootSafeStopEvidenceSummary:
    """Derived references to a separately validated safe-stop exercise."""

    receipt_ref: str
    receipt_sha256: str
    request_observed: bool
    ack_observed: bool
    effect_observed: bool
    capability_evidenced: bool
    execution_scope: HardwareExecutionMode


def _validate_preparation(
    preparation: GrootGovernedPreparation,
    *,
    context: GrootArmExecutionContext,
    evaluated_at: datetime,
) -> None:
    if preparation.schema_version != GROOT_GOVERNED_PREPARATION_SCHEMA:
        raise GrootGovernedE2EError("groot_e2e_preparation_schema_invalid")
    expected_digest = canonical_sha256(preparation.material())
    if preparation.preparation_sha256 != expected_digest:
        raise GrootGovernedE2EError("groot_e2e_preparation_digest_mismatch")
    if preparation.preparation_ref != (
        f"groot-e2e-preparation:{expected_digest[:16]}"
    ):
        raise GrootGovernedE2EError("groot_e2e_preparation_ref_mismatch")
    if preparation.execution_scope is not HardwareExecutionMode.SIM:
        raise GrootGovernedE2EError("groot_e2e_preparation_scope_invalid")
    if preparation.hand_actuation_allowed:
        raise GrootGovernedE2EError("groot_e2e_hand_actuation_forbidden")
    if (
        preparation.controller_configuration_sha256
        != context.controller_configuration_sha256
        or preparation.safety_configuration_sha256
        != context.safety_configuration_sha256
        or preparation.envelope_policy_sha256
        != context.policy.execution_envelope_policy_sha256
    ):
        raise GrootGovernedE2EError("groot_e2e_preparation_runtime_mismatch")
    if preparation.transformation_id not in {
        value.transformation_id
        for value in context.policy.authorized_transformations
    }:
        raise GrootGovernedE2EError(
            "groot_e2e_preparation_transformation_not_authorized"
        )
    if evaluated_at > _parse_utc(
        preparation.expires_at,
        reason="groot_e2e_preparation_expiry_invalid",
    ):
        raise GrootGovernedE2EError("groot_e2e_instruction_expired")


def _validate_approval(
    approval: GrootGovernedApproval,
    *,
    preparation: GrootGovernedPreparation,
    evaluated_at: datetime,
) -> None:
    reasons: list[str] = []
    if approval.schema_version != GROOT_GOVERNED_APPROVAL_SCHEMA:
        reasons.append("schema")
    if (
        approval.run_ref != preparation.run_ref
        or approval.instruction_ref != preparation.instruction_ref
        or approval.preparation_ref != preparation.preparation_ref
        or approval.preparation_sha256 != preparation.preparation_sha256
    ):
        reasons.append("scope")
    if not _valid_ref(
        approval.operator_approval_ref,
        prefix="groot-e2e-approval:",
    ):
        reasons.append("ref")
    approved_at = _parse_utc(
        approval.approved_at,
        reason="groot_e2e_approval_time_invalid",
    )
    expires_at = _parse_utc(
        approval.expires_at,
        reason="groot_e2e_approval_expiry_invalid",
    )
    if (
        not approval.operator_approved
        or approval.automatic_dispatch_executed
        or approved_at > evaluated_at
        or expires_at <= approved_at
        or evaluated_at > expires_at
        or expires_at
        > _parse_utc(
            preparation.expires_at,
            reason="groot_e2e_preparation_expiry_invalid",
        )
    ):
        reasons.append("state")
    if reasons:
        raise GrootGovernedE2EError(
            f"groot_e2e_approval_invalid:{','.join(reasons)}"
        )


def _validate_safe_stop(summary: GrootSafeStopEvidenceSummary) -> None:
    if (
        not summary.receipt_ref
        or len(summary.receipt_sha256) != 64
        or summary.execution_scope is not HardwareExecutionMode.SIM
        or not summary.request_observed
        or not summary.ack_observed
        or not summary.effect_observed
        or not summary.capability_evidenced
    ):
        raise GrootGovernedE2EError(
            "groot_e2e_safe_stop_evidence_invalid"
        )


def _authority_records(
    *,
    table: DispatchAuthorityTable,
    preparation: GrootGovernedPreparation,
    approval: GrootGovernedApproval,
    gate_passed: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    suffix = preparation.run_ref.rsplit(":", 1)[-1]
    authority_id = f"groot-e2e-authority:{suffix}"
    gate_result_id = f"groot-e2e-gate:{suffix}"
    table.register_authority(
        {
            "dispatch_authority_id": authority_id,
            "dispatch_ref": f"groot-e2e-dispatch:{suffix}",
            "bounded_action_ref": preparation.instruction_ref,
            "approval_ref": approval.operator_approval_ref,
            "operator_approval_required": True,
            "automatic_dispatch_suppressed": True,
        },
        artifact_path=preparation.preparation_ref,
        backend_target="groot_robocasa_sim",
    )
    authority = table.lookup(authority_id)
    if (
        authority.get("approval_ref") != approval.operator_approval_ref
        or authority.get("bounded_action_ref") != preparation.instruction_ref
    ):
        raise GrootGovernedE2EError("groot_e2e_authority_scope_mismatch")
    operator = {
        "approval_id": approval.operator_approval_ref,
        "operator_approved": approval.operator_approved,
        "automatic_dispatch_executed": False,
        "session_id": preparation.run_ref,
    }
    gate = {
        "gate_result_id": gate_result_id,
        "deterministic_gate_passed": gate_passed,
        "automatic_dispatch_executed": False,
        "session_id": preparation.run_ref,
    }
    accepted = table.validate_dispatch_request(
        authority_id=authority_id,
        operator_approval=operator,
        deterministic_gate=gate,
    )
    replay = table.validate_dispatch_request(
        authority_id=authority_id,
        operator_approval=operator,
        deterministic_gate=gate,
    )
    return accepted, replay


def _policy_boundary(proposal: GrootActionChunkProposal) -> dict[str, Any]:
    return {
        "proposal_declared_verification_basis": proposal.verification_basis,
        "proposal_received": True,
        "request_sha256": proposal.request_sha256,
        "observation_sha256": proposal.observation_sha256,
        "response_sha256": proposal.response_sha256,
        "approval_created": proposal.approval_created,
        "dispatch_authority_created": proposal.dispatch_authority_created,
        "dispatch_request_sent": proposal.dispatch_request_sent,
        "execution_claimed": proposal.execution_claimed,
        "progress_claimed": proposal.progress_claimed,
        "safe_stop_claimed": proposal.safe_stop_claimed,
        "completion_claimed": proposal.completion_claimed,
        "physical_execution_invoked": proposal.physical_execution_invoked,
    }


def _freshness_policy_boundary(
    policy: GrootFreshnessPolicy,
) -> dict[str, Any]:
    return {
        "freshness_policy_id": policy.policy_id,
        "freshness_policy_version": policy.policy_version,
        "freshness_policy_sha256": policy.policy_sha256,
        "maximum_observation_age_seconds": (
            policy.maximum_observation_age_seconds
        ),
        "freshness_policy_rationale": policy.rationale,
        "manufacturer_limit_claimed": False,
    }


def _timing_boundary(
    *,
    assessment: GrootPolicyResponseAssessment,
    pre_dispatch: Mapping[str, Any],
    dispatch_authority_validated_at: str | None,
) -> dict[str, Any]:
    validated_at = (
        _utc_text(
            _parse_utc(
                dispatch_authority_validated_at,
                reason="groot_e2e_dispatch_authority_time_invalid",
            )
        )
        if dispatch_authority_validated_at is not None
        else None
    )
    return {
        "observation_observed_at": assessment.observation_observed_at,
        "policy_request_started_at": assessment.request_started_at,
        "policy_response_received_at": assessment.response_received_at,
        "joint_revalidation_observed_at": pre_dispatch.get(
            "joint_state_observed_at"
        ),
        "dispatch_authority_validated_at": validated_at,
    }


def _runtime_timing(evidence: Mapping[str, Any]) -> dict[str, Any]:
    started_text = str(evidence.get("invocation_started_at") or "")
    completed_text = str(evidence.get("invocation_completed_at") or "")
    started = _parse_utc(
        started_text,
        reason="groot_e2e_policy_runtime_started_at_invalid",
    )
    completed = _parse_utc(
        completed_text,
        reason="groot_e2e_policy_runtime_completed_at_invalid",
    )
    duration = (completed - started).total_seconds()
    if duration < 0:
        raise GrootGovernedE2EError(
            "groot_e2e_policy_runtime_clock_regressed"
        )
    return {
        "invocation_started_at": _utc_text(started),
        "invocation_completed_at": _utc_text(completed),
        # This includes serialization, transport, and service work. It is not
        # claimed as pure model inference time.
        "policy_service_round_trip_seconds": duration,
    }


def _policy_runtime_boundary(
    *,
    transport: GrootPolicyTransport,
    proposal: GrootActionChunkProposal,
) -> dict[str, Any]:
    if not isinstance(transport, GrootZmqPolicyTransport):
        return {
            "service_kind": "in_process_fixture",
            "model_runtime_invoked": False,
            "runtime_invocation_evidence_valid": False,
            "runtime_invocation_evidence_sha256": None,
            "invocation_started_at": None,
            "invocation_completed_at": None,
            "policy_service_round_trip_seconds": None,
        }
    values = transport.collect_runtime_invocation_evidence()
    if len(values) != 1:
        raise GrootGovernedE2EError(
            "groot_e2e_policy_runtime_evidence_missing"
        )
    try:
        evidence = validate_runtime_invocation_evidence(values[0])
    except RuntimeClaimValidationError as exc:
        raise GrootGovernedE2EError(
            f"groot_e2e_policy_runtime_evidence_invalid:{exc}"
        ) from exc
    if (
        evidence.get("invocation_kind") != "llm_api"
        or evidence.get("invocation_exit_code") != 0
        or not str(evidence.get("invocation_target") or "").startswith(
            "groot_n1_5:tcp://"
        )
        or evidence.get("request_sha256") != proposal.request_sha256
        or evidence.get("response_sha256") != proposal.response_sha256
        or evidence.get("policy_revision") != GROOT_REPOSITORY_REVISION
        or evidence.get("model_snapshot") != GROOT_MODEL_SNAPSHOT
        or evidence.get("execution_scope") != "sim"
    ):
        raise GrootGovernedE2EError(
            "groot_e2e_policy_runtime_evidence_binding_mismatch"
        )
    digest_material = {
        key: value
        for key, value in evidence.items()
        if key
        not in {
            "invocation_stdout_preimage",
            "invocation_stderr_preimage",
        }
    }
    return {
        "service_kind": "groot_zmq_policy",
        "model_runtime_invoked": True,
        "runtime_invocation_evidence_valid": True,
        "runtime_invocation_evidence_sha256": canonical_sha256(
            digest_material
        ),
        **_runtime_timing(evidence),
    }


def _policy_runtime_assessment_boundary(
    *,
    transport: GrootPolicyTransport,
    assessment: GrootPolicyResponseAssessment,
) -> dict[str, Any]:
    if not isinstance(transport, GrootZmqPolicyTransport):
        return {
            "service_kind": "in_process_fixture",
            "model_runtime_invoked": False,
            "runtime_invocation_evidence_valid": False,
            "runtime_invocation_evidence_sha256": None,
            "invocation_started_at": None,
            "invocation_completed_at": None,
            "policy_service_round_trip_seconds": None,
        }
    values = transport.collect_runtime_invocation_evidence()
    if len(values) != 1:
        return {
            "service_kind": "groot_zmq_policy",
            "model_runtime_invoked": False,
            "runtime_invocation_evidence_valid": False,
            "runtime_invocation_evidence_sha256": None,
            "invocation_started_at": None,
            "invocation_completed_at": None,
            "policy_service_round_trip_seconds": None,
        }
    try:
        evidence = validate_runtime_invocation_evidence(values[0])
    except RuntimeClaimValidationError:
        return {
            "service_kind": "groot_zmq_policy",
            "model_runtime_invoked": False,
            "runtime_invocation_evidence_valid": False,
            "runtime_invocation_evidence_sha256": None,
            "invocation_started_at": None,
            "invocation_completed_at": None,
            "policy_service_round_trip_seconds": None,
        }
    valid = bool(
        evidence.get("invocation_kind") == "llm_api"
        and evidence.get("invocation_exit_code") == 0
        and str(evidence.get("invocation_target") or "").startswith(
            "groot_n1_5:tcp://"
        )
        and evidence.get("request_sha256") == assessment.request_sha256
        and assessment.response_sha256 is not None
        and evidence.get("response_sha256") == assessment.response_sha256
        and evidence.get("policy_revision") == GROOT_REPOSITORY_REVISION
        and evidence.get("model_snapshot") == GROOT_MODEL_SNAPSHOT
        and evidence.get("execution_scope") == "sim"
    )
    digest_material = {
        key: value
        for key, value in evidence.items()
        if key
        not in {
            "invocation_stdout_preimage",
            "invocation_stderr_preimage",
        }
    }
    return {
        "service_kind": "groot_zmq_policy",
        "model_runtime_invoked": valid,
        "runtime_invocation_evidence_valid": valid,
        "runtime_invocation_evidence_sha256": (
            canonical_sha256(digest_material) if valid else None
        ),
        **(
            _runtime_timing(evidence)
            if valid
            else {
                "invocation_started_at": None,
                "invocation_completed_at": None,
                "policy_service_round_trip_seconds": None,
            }
        ),
    }


def _predicate_item(
    *,
    item_id: str,
    predicate: str,
    status: VerificationItemStatus,
    evidence_ref: str,
    verified: bool,
) -> VerificationItem:
    return VerificationItem(
        item_id=item_id,
        predicate=predicate,
        status=status,
        verification_basis=(
            VerificationBasis.DETERMINISTIC
            if verified
            else VerificationBasis.UNVERIFIED
        ),
        evidence_refs=(evidence_ref,),
    )


def _pre_dispatch_verification(
    *,
    assessment: GrootPolicyResponseAssessment,
    preparation: GrootGovernedPreparation,
    policy_payload: Mapping[str, Any],
    controller: GrootArmController,
) -> tuple[dict[str, Any], bool]:
    response_source_id = (
        f"groot-policy-response:{assessment.request_sha256[:16]}"
    )
    observation_source_id = (
        f"groot-policy-observation:{assessment.observation_sha256[:16]}"
    )
    instruction_source_id = preparation.instruction_ref
    sources: dict[str, EvidenceSourceRef] = {
        response_source_id: EvidenceSourceRef(
            source_id=response_source_id,
            evidence_kind="groot_policy_service_response",
            observed_at=assessment.response_received_at,
            content_sha256=assessment.response_sha256,
            execution_scope=HardwareExecutionMode.SIM,
            origin=EvidenceOrigin.MODEL_INFERRED,
        ),
        observation_source_id: EvidenceSourceRef(
            source_id=observation_source_id,
            evidence_kind="groot_policy_input_observation",
            observed_at=assessment.proposal.observed_at
            if assessment.proposal is not None
            else None,
            content_sha256=assessment.observation_sha256,
            execution_scope=HardwareExecutionMode.SIM,
            origin=EvidenceOrigin.MACHINE_OBSERVED,
        ),
        instruction_source_id: EvidenceSourceRef(
            source_id=instruction_source_id,
            evidence_kind="approved_instruction",
            observed_at=preparation.prepared_at,
            content_sha256=preparation.instruction_sha256,
            execution_scope=HardwareExecutionMode.SIM,
            origin=EvidenceOrigin.AUTHORITY_ARTIFACT,
        ),
    }
    items: list[VerificationItem] = [
        _predicate_item(
            item_id=RESPONSE_SCHEMA_ITEM,
            predicate="service response matches the pinned action schema",
            status=(
                VerificationItemStatus.PASS
                if assessment.response_schema_valid
                else VerificationItemStatus.FAIL
            ),
            evidence_ref=response_source_id,
            verified=True,
        ),
        _predicate_item(
            item_id=TEMPORAL_FRESHNESS_ITEM,
            predicate="policy input observation remains temporally fresh",
            status=(
                VerificationItemStatus.PASS
                if assessment.temporal_freshness_valid
                else VerificationItemStatus.FAIL
            ),
            evidence_ref=observation_source_id,
            verified=True,
        ),
    ]
    item_reasons: dict[str, str | None] = {
        RESPONSE_SCHEMA_ITEM: assessment.response_schema_reason,
        TEMPORAL_FRESHNESS_ITEM: assessment.temporal_freshness_reason,
    }

    joint_status = VerificationItemStatus.PENDING
    joint_verified = False
    joint_reason = "prerequisite_policy_response_not_accepted"
    joint_state_observed_at: str | None = None
    if assessment.proposal is not None:
        observe = getattr(controller, "observe_handoff_state", None)
        if not callable(observe):
            joint_reason = "controller_handoff_observation_unavailable"
        else:
            try:
                handoff = dict(observe())
                observed_at = _parse_utc(
                    str(handoff.get("observed_at") or ""),
                    reason="groot_joint_state_observed_at_invalid",
                )
                response_received_at = _parse_utc(
                    assessment.response_received_at,
                    reason="groot_policy_response_received_at_invalid",
                )
                joint_state_observed_at = _utc_text(observed_at)
                now = datetime.now(timezone.utc)
                left = np.asarray(handoff.get("left_arm_rad"), dtype=np.float64)
                right = np.asarray(
                    handoff.get("right_arm_rad"),
                    dtype=np.float64,
                )
                policy_input_state = np.concatenate(
                    (
                        np.asarray(
                            policy_payload["state.left_arm"],
                            dtype=np.float64,
                        )[0],
                        np.asarray(
                            policy_payload["state.right_arm"],
                            dtype=np.float64,
                        )[0],
                    )
                )
                observed_state = np.concatenate((left, right))
                state_age = (now - observed_at).total_seconds()
                state_unchanged = bool(
                    observed_state.shape == (14,)
                    and np.isfinite(observed_state).all()
                    and np.array_equal(observed_state, policy_input_state)
                )
                joint_passed = bool(
                    handoff.get("execution_scope") == "sim"
                    and handoff.get("physical_execution_invoked") is False
                    and state_age >= 0
                    and state_age
                    <= GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS
                    and observed_at >= response_received_at
                    and state_unchanged
                )
                joint_status = (
                    VerificationItemStatus.PASS
                    if joint_passed
                    else VerificationItemStatus.FAIL
                )
                joint_verified = True
                joint_reason = (
                    None
                    if joint_passed
                    else (
                        "controller_joint_revalidation_precedes_policy_response"
                        if observed_at < response_received_at
                        else "controller_joint_state_changed_since_policy_input"
                    )
                )
                joint_source_id = (
                    f"groot-controller-handoff:"
                    f"{canonical_sha256(handoff)[:16]}"
                )
                sources[joint_source_id] = EvidenceSourceRef(
                    source_id=joint_source_id,
                    evidence_kind="controller_handoff_state",
                    observed_at=str(handoff.get("observed_at") or ""),
                    content_sha256=canonical_sha256(handoff),
                    execution_scope=HardwareExecutionMode.SIM,
                    origin=EvidenceOrigin.MACHINE_OBSERVED,
                )
                joint_evidence_ref = joint_source_id
            except (TypeError, ValueError, GrootGovernedE2EError):
                joint_reason = "controller_handoff_observation_invalid"
    if not joint_verified:
        joint_evidence_ref = observation_source_id
    items.append(
        _predicate_item(
            item_id=JOINT_STATE_COMPATIBILITY_ITEM,
            predicate=(
                "dispatch-time controller joint state matches the policy "
                "input observation"
            ),
            status=joint_status,
            evidence_ref=joint_evidence_ref,
            verified=joint_verified,
        )
    )
    item_reasons[JOINT_STATE_COMPATIBILITY_ITEM] = joint_reason

    object_not_required = (
        preparation.instruction_allowlist_id == GROOT_ARM_HOLD_INSTRUCTION_ID
    )
    items.append(
        _predicate_item(
            item_id=OBJECT_STATE_COMPATIBILITY_ITEM,
            predicate=(
                "object-state compatibility is not applicable to the "
                "hold-current-pose instruction"
            ),
            status=(
                VerificationItemStatus.PASS
                if object_not_required
                else VerificationItemStatus.PENDING
            ),
            evidence_ref=instruction_source_id,
            verified=object_not_required,
        )
    )
    item_reasons[OBJECT_STATE_COMPATIBILITY_ITEM] = (
        None
        if object_not_required
        else "trusted_object_state_revalidation_unavailable"
    )
    aggregate = aggregate_verification_items(
        items=items,
        required_item_ids=PRE_DISPATCH_REQUIRED_ITEM_IDS,
        evidence_sources=sources,
        expected_execution_scope=HardwareExecutionMode.SIM,
    )
    status = (
        FeasibilityStatus.BLOCKED
        if aggregate.blocked_reasons
        else FeasibilityStatus.UNVERIFIED
        if aggregate.unverified_reasons
        else FeasibilityStatus.VERIFIED_FEASIBLE
    )
    return (
        {
            "status": status.value,
            "verification_basis": aggregate.verification_basis.value,
            "required_item_ids": list(aggregate.required_item_ids),
            "items": [
                {
                    **item.to_dict(),
                    "reason": item_reasons.get(item.item_id),
                }
                for item in items
            ],
            "blocked_reasons": list(aggregate.blocked_reasons),
            "unverified_reasons": list(aggregate.unverified_reasons),
            "joint_state_observed_at": joint_state_observed_at,
        },
        aggregate.positive,
    )


def _execution_boundary(result: GrootArmControllerResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "verification_basis": result.verification_basis.value,
        "reasons": list(result.reasons),
        "admitted_chunk_sha256": result.admitted_chunk_sha256,
        "transformed_chunk_sha256": result.transformed_chunk_sha256,
        "applied_command_sha256": result.applied_command_sha256,
        "controller_request_sent": result.controller_request_sent,
        "controller_ack_observed": any(
            item.item_id == "groot_arm_controller_ack"
            and item.status.value == "pass"
            for item in result.verification_items
        ),
        "progress_observed": any(
            item.item_id == "groot_arm_controller_progress"
            and item.status.value == "pass"
            for item in result.verification_items
        ),
        "effect_observed": any(
            item.item_id == "groot_arm_effect_observed"
            and item.status.value == "pass"
            for item in result.verification_items
        ),
        "safe_stop_requested": result.safe_stop_requested,
        "safe_stop_ack_observed": result.safe_stop_ack_observed,
        "safe_stop_effect_observed": result.safe_stop_effect_observed,
        "execution_profile": result.execution_profile,
        "balance_coupling_governed": (
            result.balance_coupling_governed
        ),
        "whole_body_safety_claimed": result.whole_body_safety_claimed,
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
        "task_completion_claimed": result.task_completion_claimed,
        "physical_execution_invoked": result.physical_execution_invoked,
    }


def run_groot_governed_e2e(
    *,
    preparation: GrootGovernedPreparation,
    approval: GrootGovernedApproval,
    policy_payload: Mapping[str, Any],
    policy_binding: GrootPolicyBinding,
    policy_transport: GrootPolicyTransport,
    policy_clock: Callable[[], datetime],
    controller: GrootArmController,
    controller_context: GrootArmExecutionContext,
    safe_stop_summary: GrootSafeStopEvidenceSummary,
    authority_state_path: Path,
    evaluated_at: datetime | None,
    limitations: Sequence[str] = (),
) -> dict[str, Any]:
    """Run one approved proposal and return a publication-safe evidence report."""

    evaluated = (evaluated_at or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    )
    _validate_preparation(
        preparation,
        context=controller_context,
        evaluated_at=evaluated,
    )
    _validate_approval(
        approval,
        preparation=preparation,
        evaluated_at=evaluated,
    )
    _validate_safe_stop(safe_stop_summary)
    expected_instruction = GROOT_GOVERNED_INSTRUCTION_ALLOWLIST[
        preparation.instruction_allowlist_id
    ]
    if policy_payload.get(
        "annotation.human.action.task_description"
    ) != [expected_instruction]:
        raise GrootGovernedE2EError("groot_e2e_instruction_payload_mismatch")
    if (
        policy_binding.instruction_ref != preparation.instruction_ref
        or policy_binding.preparation_sha256
        != preparation.preparation_sha256
    ):
        raise GrootGovernedE2EError("groot_e2e_policy_binding_mismatch")
    if (
        policy_binding.freshness_policy.policy_sha256
        != preparation.freshness_policy_sha256
    ):
        raise GrootGovernedE2EError(
            "groot_e2e_freshness_policy_binding_mismatch"
        )
    if (
        controller_context.instruction_ref != preparation.instruction_ref
        or controller_context.approval_ref != approval.operator_approval_ref
        or controller_context.expected_preparation_sha256
        != preparation.preparation_sha256
    ):
        raise GrootGovernedE2EError("groot_e2e_controller_binding_mismatch")

    assessment = assess_groot_action_chunk(
        transport=policy_transport,
        payload=policy_payload,
        binding=policy_binding,
        clock=policy_clock,
    )
    proposal = assessment.proposal
    pre_dispatch, pre_dispatch_positive = _pre_dispatch_verification(
        assessment=assessment,
        preparation=preparation,
        policy_payload=policy_payload,
        controller=controller,
    )
    if not pre_dispatch_positive:
        policy_runtime = _policy_runtime_assessment_boundary(
            transport=policy_transport,
            assessment=assessment,
        )
        policy_facts = (
            _policy_boundary(proposal)
            if proposal is not None
            else {
                "proposal_declared_verification_basis": "unverified",
                "proposal_received": False,
                "request_sha256": assessment.request_sha256,
                "observation_sha256": assessment.observation_sha256,
                "response_sha256": assessment.response_sha256,
                "approval_created": False,
                "dispatch_authority_created": False,
                "dispatch_request_sent": False,
                "execution_claimed": False,
                "progress_claimed": False,
                "safe_stop_claimed": False,
                "completion_claimed": False,
                "physical_execution_invoked": False,
            }
        )
        report = {
            "schema_version": GROOT_GOVERNED_E2E_REPORT_SCHEMA,
            "run_ref": preparation.run_ref,
            "instruction_ref": preparation.instruction_ref,
            "instruction_allowlist_id": preparation.instruction_allowlist_id,
            "instruction_sha256": preparation.instruction_sha256,
            "preparation_ref": preparation.preparation_ref,
            "preparation_sha256": preparation.preparation_sha256,
            "operator_approval_ref": approval.operator_approval_ref,
            "approval_single_use_consumed": False,
            "approval_replay_blocked": False,
            "policy_service": {
                **policy_runtime,
                "repository_revision": GROOT_REPOSITORY_REVISION,
                "model_snapshot": GROOT_MODEL_SNAPSHOT,
                **_freshness_policy_boundary(
                    policy_binding.freshness_policy
                ),
                **policy_facts,
                "service_response_received": assessment.response_received,
                "response_received_at": assessment.response_received_at,
                "response_schema_valid": assessment.response_schema_valid,
                "temporal_freshness_valid": (
                    assessment.temporal_freshness_valid
                ),
                "declared_verification_basis": (
                    policy_facts["proposal_declared_verification_basis"]
                ),
                "effective_verification_basis": "unverified",
            },
            "timing": _timing_boundary(
                assessment=assessment,
                pre_dispatch=pre_dispatch,
                dispatch_authority_validated_at=None,
            ),
            "pre_dispatch_verification": pre_dispatch,
            "execution": {
                "status": pre_dispatch["status"],
                "verification_basis": pre_dispatch["verification_basis"],
                "reasons": [
                    *pre_dispatch["blocked_reasons"],
                    *pre_dispatch["unverified_reasons"],
                ],
                "controller_request_sent": False,
                "controller_ack_observed": False,
                "progress_observed": False,
                "effect_observed": False,
                "safe_stop_requested": False,
                "safe_stop_ack_observed": False,
                "safe_stop_effect_observed": False,
                "execution_profile": controller_context.execution_profile,
                "balance_coupling_governed": (
                    controller_context.balance_coupling_governed
                ),
                "whole_body_safety_claimed": (
                    controller_context.whole_body_safety_claimed
                ),
                "task_completion_claimed": False,
                "physical_execution_invoked": False,
            },
            "safe_stop_exercise": {
                "receipt_ref": safe_stop_summary.receipt_ref,
                "receipt_sha256": safe_stop_summary.receipt_sha256,
                "request_observed": safe_stop_summary.request_observed,
                "ack_observed": safe_stop_summary.ack_observed,
                "effect_observed": safe_stop_summary.effect_observed,
                "capability_evidenced": (
                    safe_stop_summary.capability_evidenced
                ),
                "execution_scope": safe_stop_summary.execution_scope.value,
            },
            "semantic_completion": {
                "claimed": False,
                "verification_basis": "unverified",
                "reason": "no_separately_identified_semantic_verifier",
                "negative_cases": {
                    case: False
                    for case in SEMANTIC_COMPLETION_NEGATIVE_CASES
                },
            },
            "execution_scope": HardwareExecutionMode.SIM.value,
            "physical_execution_invoked": False,
            "limitations": list(dict.fromkeys(limitations)),
            "status": pre_dispatch["status"],
        }
        findings = publication_findings(report)
        if findings:
            raise GrootGovernedE2EError(
                "groot_e2e_report_publication_boundary_violated"
            )
        return report
    if proposal is None:
        raise GrootGovernedE2EError("groot_e2e_proposal_missing_after_gate")
    policy_runtime = _policy_runtime_boundary(
        transport=policy_transport,
        proposal=proposal,
    )
    dispatch_evaluated_at = (
        evaluated_at or datetime.now(timezone.utc)
    ).astimezone(timezone.utc)
    policy_facts = _policy_boundary(proposal)
    if any(
        policy_facts[field] is not False
        for field in (
            "approval_created",
            "dispatch_authority_created",
            "dispatch_request_sent",
            "execution_claimed",
            "progress_claimed",
            "safe_stop_claimed",
            "completion_claimed",
            "physical_execution_invoked",
        )
    ):
        raise GrootGovernedE2EError("groot_e2e_policy_crossed_authority")
    gate_passed = bool(
        pre_dispatch_positive
        and proposal.instruction_ref == preparation.instruction_ref
        and proposal.preparation_sha256 == preparation.preparation_sha256
        and controller_context.envelope_validation.status
        is FeasibilityStatus.VERIFIED_FEASIBLE
    )
    authority, replay = _authority_records(
        table=DispatchAuthorityTable(authority_state_path),
        preparation=preparation,
        approval=approval,
        gate_passed=gate_passed,
    )
    if authority.get("validation_status") != "valid":
        raise GrootGovernedE2EError("groot_e2e_dispatch_authority_blocked")
    if (
        replay.get("validation_status") != "blocked"
        or replay.get("dispatch_replay_detected") is not True
    ):
        raise GrootGovernedE2EError("groot_e2e_approval_replay_not_blocked")

    result = execute_groot_arm_chunk(
        proposal=proposal,
        context=controller_context,
        transformation_id=preparation.transformation_id,
        controller=controller,
        evaluated_at=dispatch_evaluated_at,
    )
    execution = _execution_boundary(result)
    report = {
        "schema_version": GROOT_GOVERNED_E2E_REPORT_SCHEMA,
        "run_ref": preparation.run_ref,
        "instruction_ref": preparation.instruction_ref,
        "instruction_allowlist_id": preparation.instruction_allowlist_id,
        "instruction_sha256": preparation.instruction_sha256,
        "preparation_ref": preparation.preparation_ref,
        "preparation_sha256": preparation.preparation_sha256,
        "operator_approval_ref": approval.operator_approval_ref,
        "approval_single_use_consumed": (
            authority.get("operator_approval_token_consumed") is True
        ),
        "approval_replay_blocked": (
            replay.get("dispatch_replay_detected") is True
        ),
        "policy_service": {
            **policy_runtime,
            "repository_revision": GROOT_REPOSITORY_REVISION,
            "model_snapshot": GROOT_MODEL_SNAPSHOT,
            **_freshness_policy_boundary(policy_binding.freshness_policy),
            **policy_facts,
            "declared_verification_basis": policy_facts[
                "proposal_declared_verification_basis"
            ],
            "effective_verification_basis": (
                policy_facts["proposal_declared_verification_basis"]
                if policy_runtime["model_runtime_invoked"]
                else "unverified"
            ),
        },
        "timing": _timing_boundary(
            assessment=assessment,
            pre_dispatch=pre_dispatch,
            dispatch_authority_validated_at=(
                str(authority.get("validated_at") or "") or None
            ),
        ),
        "pre_dispatch_verification": pre_dispatch,
        "execution": execution,
        "safe_stop_exercise": {
            "receipt_ref": safe_stop_summary.receipt_ref,
            "receipt_sha256": safe_stop_summary.receipt_sha256,
            "request_observed": safe_stop_summary.request_observed,
            "ack_observed": safe_stop_summary.ack_observed,
            "effect_observed": safe_stop_summary.effect_observed,
            "capability_evidenced": safe_stop_summary.capability_evidenced,
            "execution_scope": safe_stop_summary.execution_scope.value,
        },
        "semantic_completion": {
            "claimed": False,
            "verification_basis": "unverified",
            "reason": "no_separately_identified_semantic_verifier",
            "negative_cases": {
                case: False
                for case in SEMANTIC_COMPLETION_NEGATIVE_CASES
            },
        },
        "execution_scope": HardwareExecutionMode.SIM.value,
        "physical_execution_invoked": False,
        "limitations": list(dict.fromkeys(limitations)),
        "status": (
            (
                "verified_execution_evidence"
                if policy_runtime["model_runtime_invoked"]
                else "verified_fixture_execution_evidence"
            )
            if result.status is FeasibilityStatus.VERIFIED_FEASIBLE
            else result.status.value
        ),
    }
    findings = publication_findings(report)
    if findings:
        raise GrootGovernedE2EError(
            "groot_e2e_report_publication_boundary_violated"
        )
    return report


__all__ = [
    "GROOT_ARM_HOLD_INSTRUCTION",
    "GROOT_ARM_HOLD_INSTRUCTION_ID",
    "GROOT_GOVERNED_APPROVAL_SCHEMA",
    "GROOT_GOVERNED_E2E_REPORT_SCHEMA",
    "GROOT_GOVERNED_INSTRUCTION_ALLOWLIST",
    "GROOT_GOVERNED_PREPARATION_SCHEMA",
    "JOINT_STATE_COMPATIBILITY_ITEM",
    "OBJECT_STATE_COMPATIBILITY_ITEM",
    "PRE_DISPATCH_REQUIRED_ITEM_IDS",
    "RESPONSE_SCHEMA_ITEM",
    "SEMANTIC_COMPLETION_NEGATIVE_CASES",
    "TEMPORAL_FRESHNESS_ITEM",
    "GrootGovernedApproval",
    "GrootGovernedE2EError",
    "GrootGovernedPreparation",
    "GrootSafeStopEvidenceSummary",
    "build_groot_governed_preparation",
    "run_groot_governed_e2e",
]

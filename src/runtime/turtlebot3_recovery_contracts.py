"""Pure TurtleBot3 Recovery intent, authority, and outcome contracts.

This module owns projections and integrity checks only. It never chooses a
route, mints approval, dispatches Nav2, or claims delivery/physical execution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
from typing import Any


TURTLEBOT3_RECOVERY_INTENT_SCHEMA = "missionos_turtlebot3_recovery_intent.v1"
TURTLEBOT3_RECOVERY_COMPILATION_SCHEMA = (
    "missionos_turtlebot3_recovery_intent_compilation.v1"
)
TURTLEBOT3_RECOVERY_PREDISPATCH_VERIFICATION_SCHEMA = (
    "missionos_turtlebot3_recovery_predispatch_verification.v1"
)
TURTLEBOT3_RECOVERY_CONTRACT_BUNDLE_SCHEMA = (
    "missionos_turtlebot3_recovery_contract_bundle.v1"
)
TURTLEBOT3_RECOVERY_OUTCOME_VERIFICATION_SCHEMA = (
    "missionos_turtlebot3_recovery_outcome_verification.v1"
)

_SUPPORTED_ACTIONS = {"avoid_obstacle", "reroute", "return_home"}
_MUTABLE_CHECKPOINT_FIELDS = {
    "checkpoint_id",
    "checkpoint_hash",
    "checkpoint_status",
    "claimed_at",
    "claimed_by_approval_ref",
    "consumed_at",
    "consumed_by_approval_ref",
    "failed_at",
    "failure_reasons",
    "superseded_at",
    "superseded_by_checkpoint_id",
    "superseded_by_revision_id",
}


def _canonical_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _hashed_artifact(
    payload: Mapping[str, Any],
    *,
    id_prefix: str,
    id_key: str,
    sha_key: str,
) -> dict[str, Any]:
    material = {
        str(key): deepcopy(value)
        for key, value in payload.items()
        if key not in {id_key, sha_key}
    }
    digest = _canonical_sha256(material)
    return {
        **material,
        sha_key: digest,
        id_key: f"{id_prefix}_{digest[:12]}",
    }


def _artifact_hash_matches(
    artifact: Mapping[str, Any],
    *,
    id_prefix: str,
    id_key: str,
    sha_key: str,
) -> bool:
    material = {
        str(key): value
        for key, value in artifact.items()
        if key not in {id_key, sha_key}
    }
    digest = _canonical_sha256(material)
    return (
        artifact.get(sha_key) == digest
        and artifact.get(id_key) == f"{id_prefix}_{digest[:12]}"
    )


def recovery_checkpoint_hash_payload(
    checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Return immutable checkpoint fields covered by ``checkpoint_hash``."""

    return {
        str(key): value
        for key, value in checkpoint.items()
        if str(key) not in _MUTABLE_CHECKPOINT_FIELDS
        and not str(key).startswith("superseded_")
    }


def recovery_checkpoint_hash(checkpoint: Mapping[str, Any]) -> str:
    return _canonical_sha256(recovery_checkpoint_hash_payload(checkpoint))


def recovery_resume_state_hash(state: Mapping[str, Any]) -> str:
    source_bound_state = {
        key: (
            state.get(key) or []
            if key == "route_failure_observation_results"
            else state.get(key)
        )
        for key in (
            "planned_segments",
            "segment_results",
            "route_failure_observation_results",
            "recovery_proposals",
            "recovery_proposal_classifications",
            "recovery_planner_result",
            "runtime_recovery_obstacle_scenario",
            "runtime_recovery_motion_context",
        )
    }
    return _canonical_sha256(source_bound_state)


def planned_segments_sha256(goals: Sequence[Any]) -> str:
    payloads = [
        goal.model_dump(mode="json")
        if hasattr(goal, "model_dump")
        else dict(goal)
        if isinstance(goal, Mapping)
        else goal
        for goal in goals
    ]
    return _canonical_sha256(payloads)


def _strategy_for_action(action: str) -> str:
    return {
        "avoid_obstacle": "local_avoidance",
        "reroute": "reroute",
        "return_home": "return_home",
    }.get(action, "operator_review")


def build_turtlebot3_recovery_contract_bundle(
    checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind Recovery meaning to an exact checkpoint without creating authority."""

    selected_action = str(checkpoint.get("selected_action") or "")
    approved_parameters_value = checkpoint.get("approved_parameters")
    approved_parameters = (
        dict(approved_parameters_value)
        if isinstance(approved_parameters_value, Mapping)
        else {}
    )
    candidate_binding_value = checkpoint.get("recovery_candidate_binding")
    candidate_binding = (
        dict(candidate_binding_value)
        if isinstance(candidate_binding_value, Mapping)
        else {}
    )
    action_supported = selected_action in _SUPPORTED_ACTIONS
    intent = _hashed_artifact(
        {
            "schema_version": TURTLEBOT3_RECOVERY_INTENT_SCHEMA,
            "intent_status": "valid" if action_supported else "operator_review",
            "strategy": _strategy_for_action(selected_action),
            "selected_action": selected_action,
            "requested_parameters": approved_parameters,
            "recovery_proposal_id": str(
                checkpoint.get("recovery_proposal_id") or ""
            ),
            "recovery_classification_id": str(
                checkpoint.get("recovery_classification_id") or ""
            ),
            "requires_new_human_approval": True,
            "approval_created": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
        id_prefix="turtlebot3_recovery_intent",
        id_key="recovery_intent_id",
        sha_key="recovery_intent_sha256",
    )
    meaning_preserved = action_supported and bool(approved_parameters)
    candidate_binding_sha256 = (
        _canonical_sha256(candidate_binding) if candidate_binding else ""
    )
    compilation = _hashed_artifact(
        {
            "schema_version": TURTLEBOT3_RECOVERY_COMPILATION_SCHEMA,
            "compilation_status": "compiled" if meaning_preserved else "infeasible",
            "meaning_preserved": meaning_preserved,
            "source_intent_id": intent["recovery_intent_id"],
            "source_intent_sha256": intent["recovery_intent_sha256"],
            "compiled_action": selected_action if meaning_preserved else "",
            "compiled_parameters": approved_parameters if meaning_preserved else {},
            "candidate_binding_sha256": candidate_binding_sha256,
            "compiler_changed_meaning": False,
            "approval_created": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
        id_prefix="turtlebot3_recovery_compilation",
        id_key="recovery_compilation_id",
        sha_key="recovery_compilation_sha256",
    )
    if selected_action != "avoid_obstacle":
        candidate_verification_status = "not_required"
        candidate_binding_verified = True
    elif candidate_binding.get("dual_costmap_validated") is True:
        candidate_binding_verified = bool(
            candidate_binding.get("candidate_ids")
            and candidate_binding.get("path_sha256_sequence")
            and candidate_binding.get("global_costmap_snapshot_hash")
            and candidate_binding.get("local_costmap_snapshot_hash")
        )
        candidate_verification_status = (
            "verified" if candidate_binding_verified else "unverified"
        )
    else:
        candidate_binding_verified = bool(
            candidate_binding.get("live_costmap_validated") is True
            and candidate_binding.get("candidate_id")
            and candidate_binding.get("path_sha256")
            and candidate_binding.get("costmap_snapshot_hash")
        )
        candidate_verification_status = (
            "verified" if candidate_binding_verified else "unverified"
        )
    predispatch_verification = _hashed_artifact(
        {
            "schema_version": (
                TURTLEBOT3_RECOVERY_PREDISPATCH_VERIFICATION_SCHEMA
            ),
            "verification_status": (
                "verified"
                if meaning_preserved and candidate_binding_verified
                else "unverified"
            ),
            "source_compilation_id": compilation["recovery_compilation_id"],
            "source_compilation_sha256": compilation[
                "recovery_compilation_sha256"
            ],
            "candidate_binding_status": candidate_verification_status,
            "candidate_binding_verified": candidate_binding_verified,
            "candidate_binding_sha256": candidate_binding_sha256,
            "verification_scope": (
                "checkpoint_structure_and_existing_nav2_plan_only_evidence"
            ),
            "runtime_outcome_verified": False,
            "approval_created": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
        id_prefix="turtlebot3_recovery_predispatch",
        id_key="recovery_predispatch_verification_id",
        sha_key="recovery_predispatch_verification_sha256",
    )
    return _hashed_artifact(
        {
            "schema_version": TURTLEBOT3_RECOVERY_CONTRACT_BUNDLE_SCHEMA,
            "recovery_intent": intent,
            "intent_compilation": compilation,
            "predispatch_verification": predispatch_verification,
            "approval_created": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
        id_prefix="turtlebot3_recovery_contract_bundle",
        id_key="recovery_contract_bundle_id",
        sha_key="recovery_contract_bundle_sha256",
    )


def validate_turtlebot3_recovery_contract_bundle(
    checkpoint: Mapping[str, Any],
) -> list[str]:
    """Validate a present bundle; legacy checkpoints may omit it."""

    bundle_value = checkpoint.get("recovery_contract_bundle")
    if bundle_value is None or bundle_value == {}:
        return []
    if not isinstance(bundle_value, Mapping):
        return ["turtlebot3_recovery_contract_bundle_invalid"]
    bundle = dict(bundle_value)
    intent_value = bundle.get("recovery_intent")
    compilation_value = bundle.get("intent_compilation")
    verification_value = bundle.get("predispatch_verification")
    intent = dict(intent_value) if isinstance(intent_value, Mapping) else {}
    compilation = (
        dict(compilation_value) if isinstance(compilation_value, Mapping) else {}
    )
    verification = (
        dict(verification_value)
        if isinstance(verification_value, Mapping)
        else {}
    )
    reasons: list[str] = []
    if not _artifact_hash_matches(
        bundle,
        id_prefix="turtlebot3_recovery_contract_bundle",
        id_key="recovery_contract_bundle_id",
        sha_key="recovery_contract_bundle_sha256",
    ):
        reasons.append("turtlebot3_recovery_contract_bundle_hash_mismatch")
    if not _artifact_hash_matches(
        intent,
        id_prefix="turtlebot3_recovery_intent",
        id_key="recovery_intent_id",
        sha_key="recovery_intent_sha256",
    ):
        reasons.append("turtlebot3_recovery_intent_hash_mismatch")
    if not _artifact_hash_matches(
        compilation,
        id_prefix="turtlebot3_recovery_compilation",
        id_key="recovery_compilation_id",
        sha_key="recovery_compilation_sha256",
    ):
        reasons.append("turtlebot3_recovery_compilation_hash_mismatch")
    if not _artifact_hash_matches(
        verification,
        id_prefix="turtlebot3_recovery_predispatch",
        id_key="recovery_predispatch_verification_id",
        sha_key="recovery_predispatch_verification_sha256",
    ):
        reasons.append("turtlebot3_recovery_predispatch_hash_mismatch")
    if (
        compilation.get("source_intent_id") != intent.get("recovery_intent_id")
        or compilation.get("source_intent_sha256")
        != intent.get("recovery_intent_sha256")
    ):
        reasons.append("turtlebot3_recovery_intent_compilation_chain_mismatch")
    if (
        verification.get("source_compilation_id")
        != compilation.get("recovery_compilation_id")
        or verification.get("source_compilation_sha256")
        != compilation.get("recovery_compilation_sha256")
    ):
        reasons.append(
            "turtlebot3_recovery_compilation_verification_chain_mismatch"
        )
    approved_parameters = checkpoint.get("approved_parameters")
    approved_parameters = (
        dict(approved_parameters) if isinstance(approved_parameters, Mapping) else {}
    )
    if (
        intent.get("selected_action") != checkpoint.get("selected_action")
        or intent.get("requested_parameters") != approved_parameters
        or compilation.get("compiled_action") != checkpoint.get("selected_action")
        or compilation.get("compiled_parameters") != approved_parameters
    ):
        reasons.append("turtlebot3_recovery_contract_checkpoint_meaning_mismatch")
    candidate_binding = checkpoint.get("recovery_candidate_binding")
    candidate_binding = (
        dict(candidate_binding) if isinstance(candidate_binding, Mapping) else {}
    )
    expected_binding_sha256 = (
        _canonical_sha256(candidate_binding) if candidate_binding else ""
    )
    if (
        compilation.get("candidate_binding_sha256") != expected_binding_sha256
        or verification.get("candidate_binding_sha256")
        != expected_binding_sha256
    ):
        reasons.append("turtlebot3_recovery_contract_candidate_binding_mismatch")
    return list(dict.fromkeys(reasons))


def verify_turtlebot3_recovery_outcome(
    *,
    checkpoint: Mapping[str, Any],
    operator_approval: Mapping[str, Any] | None,
    action_results: Sequence[Mapping[str, Any]],
    goal_sequence_completed: bool,
    requested_side_required: bool,
    requested_side_observed: bool,
    obstacle_clearance_required: bool,
    obstacle_clearance_observed: bool,
    route_resume_explicitly_approved: bool,
) -> dict[str, Any]:
    """Project runtime facts without collapsing ACK, effect, or completion."""

    approval = dict(operator_approval or {})
    approved_parameters = checkpoint.get("approved_parameters")
    approved_parameters = (
        dict(approved_parameters) if isinstance(approved_parameters, Mapping) else {}
    )
    authority_bound = bool(
        approval.get("operator_approved") is True
        and approval.get("explicit_recovery_dispatch_approval") is True
        and approval.get("checkpoint_id") == checkpoint.get("checkpoint_id")
        and approval.get("checkpoint_hash") == checkpoint.get("checkpoint_hash")
        and approval.get("approved_action") == checkpoint.get("selected_action")
        and approval.get("approved_parameters") == approved_parameters
    )
    dispatch_request_sent = any(
        result.get("dispatch_request_sent") is True for result in action_results
    )
    command_ack_observed = any(
        (
            result.get("adapter_evidence")
            if isinstance(result.get("adapter_evidence"), Mapping)
            else {}
        ).get("command_ack_observed")
        is True
        for result in action_results
    )
    executor_effect_observed = any(
        result.get("robot_motion_observed") is True
        or result.get("completion_claimed") is True
        for result in action_results
    )
    side_verified = not requested_side_required or requested_side_observed
    clearance_verified = (
        not obstacle_clearance_required or obstacle_clearance_observed
    )
    recovery_success_verified = bool(
        authority_bound
        and dispatch_request_sent
        and executor_effect_observed
        and goal_sequence_completed
        and side_verified
        and clearance_verified
    )
    route_resume_authorized = bool(
        recovery_success_verified and route_resume_explicitly_approved
    )
    blocking_reasons: list[str] = []
    if not authority_bound:
        blocking_reasons.append("turtlebot3_recovery_outcome_authority_not_bound")
    if not dispatch_request_sent:
        blocking_reasons.append("turtlebot3_recovery_outcome_dispatch_not_observed")
    if not executor_effect_observed:
        blocking_reasons.append(
            "turtlebot3_recovery_outcome_executor_effect_not_observed"
        )
    if not goal_sequence_completed:
        blocking_reasons.append("turtlebot3_recovery_outcome_goal_not_completed")
    if not side_verified:
        blocking_reasons.append("turtlebot3_recovery_outcome_side_not_verified")
    if not clearance_verified:
        blocking_reasons.append(
            "turtlebot3_recovery_outcome_clearance_not_verified"
        )
    payload = {
        "schema_version": TURTLEBOT3_RECOVERY_OUTCOME_VERIFICATION_SCHEMA,
        "verification_status": (
            "verified" if recovery_success_verified else "failed"
        ),
        "checkpoint_id": checkpoint.get("checkpoint_id"),
        "checkpoint_hash": checkpoint.get("checkpoint_hash"),
        "selected_action": checkpoint.get("selected_action"),
        "operator_approval_ref": approval.get("operator_approval_ref"),
        "authority_bound": authority_bound,
        "dispatch_request_sent": dispatch_request_sent,
        "command_ack_observed": command_ack_observed,
        "ack_is_executor_effect": False,
        "executor_effect_observed": executor_effect_observed,
        "goal_sequence_completed": goal_sequence_completed,
        "requested_side_required": requested_side_required,
        "requested_side_observed": requested_side_observed,
        "obstacle_clearance_required": obstacle_clearance_required,
        "obstacle_clearance_observed": obstacle_clearance_observed,
        "recovery_success_verified": recovery_success_verified,
        "route_resume_explicitly_approved": route_resume_explicitly_approved,
        "route_resume_authorized": route_resume_authorized,
        "blocking_reasons": blocking_reasons,
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    return _hashed_artifact(
        payload,
        id_prefix="turtlebot3_recovery_outcome",
        id_key="recovery_outcome_verification_id",
        sha_key="recovery_outcome_verification_sha256",
    )

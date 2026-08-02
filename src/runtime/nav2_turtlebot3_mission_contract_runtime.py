"""Runtime binding from one TurtleBot3/Nav2 result to Mission Contract.

This module does not dispatch or create authority.  The caller freezes the
contract before dispatch, then passes the returned bridge and adapter evidence
here for content-bound predicate evaluation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from missionos_core import (
    FrozenMissionContract,
    VerificationBasis,
    canonical_sha256,
)

from .nav2_turtlebot3_predicate_package import (
    NAV2_TURTLEBOT3_OUTCOME_CLAIM_ID,
    NAV2_TURTLEBOT3_OUTCOME_CLAIM_SCOPE,
    NAV2_TURTLEBOT3_PREDICATE_PACKAGE_ID,
    NAV2_TURTLEBOT3_PREDICATE_PACKAGE_SHA256,
    NAV2_TURTLEBOT3_PREDICATE_PACKAGE_VERSION,
    Nav2TurtleBot3BoundedDispatchResult,
    Nav2TurtleBot3EvidenceBindings,
    Nav2TurtleBot3PredicateContent,
    build_nav2_turtlebot3_replay_contract,
    build_nav2_turtlebot3_replay_input,
    evaluate_nav2_turtlebot3_predicate,
)
from .ros2_nav2_hardware_adapter import Nav2GoalPose


NAV2_TURTLEBOT3_RUNTIME_COMPLETION_SCHEMA_VERSION = (
    "missionos_nav2_turtlebot3_runtime_completion.v1"
)
NAV2_TURTLEBOT3_RUNTIME_CONTRACT_VERSION = "2026-07-29"
NAV2_TURTLEBOT3_RUNTIME_MAXIMUM_OBSERVATION_AGE_SECONDS = 30.0


def build_nav2_turtlebot3_runtime_contract(
    *,
    proposal_id: str,
    action_ref_suffix: str,
    goal: Nav2GoalPose,
) -> FrozenMissionContract:
    """Freeze the concrete goal and predicate package before dispatch."""

    return build_nav2_turtlebot3_replay_contract(
        contract_id=f"{proposal_id}:{action_ref_suffix}",
        contract_version=NAV2_TURTLEBOT3_RUNTIME_CONTRACT_VERSION,
        approved_goal_pose=goal.model_dump(mode="json"),
        approved_goal_frame={"frame_id": goal.frame_id},
        maximum_observation_age_seconds=(
            NAV2_TURTLEBOT3_RUNTIME_MAXIMUM_OBSERVATION_AGE_SECONDS
        ),
    )


def evaluate_nav2_turtlebot3_runtime_result(
    *,
    contract: FrozenMissionContract,
    goal: Nav2GoalPose,
    action_result: Mapping[str, Any],
    evaluated_at: datetime,
) -> dict[str, Any]:
    """Evaluate one returned action result without trusting adapter completion."""

    bridge_responses = action_result.get("bridge_responses")
    bridge_responses = (
        bridge_responses if isinstance(bridge_responses, list) else []
    )
    adapter_evidence = action_result.get("adapter_evidence")
    adapter_evidence = (
        dict(adapter_evidence)
        if isinstance(adapter_evidence, Mapping)
        else {}
    )
    adapter_completion_claimed = (
        adapter_evidence.get("completion_claimed") is True
    )
    adapter_completion_scope = str(
        adapter_evidence.get("completion_scope") or "none"
    )
    observed_at = _runtime_result_observed_at(action_result)

    if observed_at is None:
        return _unverified_runtime_evaluation(
            contract=contract,
            adapter_completion_claimed=adapter_completion_claimed,
            adapter_completion_scope=adapter_completion_scope,
            reasons=("nav2_runtime_result_observed_at_invalid",),
        )
    if len(bridge_responses) != 1 or not isinstance(
        bridge_responses[0], Mapping
    ):
        return _unverified_runtime_evaluation(
            contract=contract,
            adapter_completion_claimed=adapter_completion_claimed,
            adapter_completion_scope=adapter_completion_scope,
            reasons=("nav2_runtime_bridge_response_count_invalid",),
        )
    if not adapter_evidence:
        return _unverified_runtime_evaluation(
            contract=contract,
            adapter_completion_claimed=adapter_completion_claimed,
            adapter_completion_scope=adapter_completion_scope,
            reasons=("nav2_runtime_adapter_evidence_missing",),
        )

    bridge_response = dict(bridge_responses[0])
    source_material_sha256 = canonical_sha256(
        {
            "bridge_response": bridge_response,
            "adapter_evidence": adapter_evidence,
        }
    )
    try:
        result = Nav2TurtleBot3BoundedDispatchResult.model_validate(
            {
                "result_id": (
                    f"{contract.contract_id}:"
                    f"{source_material_sha256[:16]}"
                ),
                "observed_at": observed_at,
                "requested_goal_pose": goal.model_dump(mode="json"),
                "bridge_response": bridge_response,
                "adapter_evidence": adapter_evidence,
            }
        )
    except (TypeError, ValueError) as exc:
        return _unverified_runtime_evaluation(
            contract=contract,
            adapter_completion_claimed=adapter_completion_claimed,
            adapter_completion_scope=adapter_completion_scope,
            reasons=(
                "nav2_runtime_result_schema_invalid",
                f"nav2_runtime_result_schema_error:{type(exc).__name__}",
            ),
        )

    content = Nav2TurtleBot3PredicateContent.from_result(
        result,
        evidence_bindings=Nav2TurtleBot3EvidenceBindings(
            bridge_response_sha256=canonical_sha256(bridge_response),
            adapter_evidence_sha256=canonical_sha256(adapter_evidence),
        ),
    )
    replay = build_nav2_turtlebot3_replay_input(
        contract=contract,
        content=content,
    )
    evaluation = evaluate_nav2_turtlebot3_predicate(
        contract=contract,
        replay=replay,
        evaluated_at=evaluated_at.isoformat(),
    )
    completion_claimed = evaluation.evaluated_outcome_claim is True
    return {
        "schema_version": NAV2_TURTLEBOT3_RUNTIME_COMPLETION_SCHEMA_VERSION,
        **evaluation.to_dict(),
        "adapter_completion_claimed": adapter_completion_claimed,
        "adapter_completion_scope": adapter_completion_scope,
        "reasons": list(evaluation.reasons),
        "completion_claimed": completion_claimed,
        "completion_scope": "sim_action" if completion_claimed else "none",
        "claim_boundary": (
            "The content-bound result for one frozen TurtleBot3/Nav2 "
            "simulator goal satisfied the approved predicate package. This "
            "does not establish navigation safety, delivery, operational "
            "closure, subsequent-action authority, or physical execution."
        ),
    }


def _runtime_result_observed_at(
    action_result: Mapping[str, Any],
) -> datetime | None:
    value = action_result.get("result_observed_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        observed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return None
    return observed_at


def _unverified_runtime_evaluation(
    *,
    contract: FrozenMissionContract,
    adapter_completion_claimed: bool,
    adapter_completion_scope: str,
    reasons: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema_version": NAV2_TURTLEBOT3_RUNTIME_COMPLETION_SCHEMA_VERSION,
        "contract_id": contract.contract_id,
        "contract_sha256": contract.contract_sha256,
        "predicate_package_id": NAV2_TURTLEBOT3_PREDICATE_PACKAGE_ID,
        "predicate_package_version": (
            NAV2_TURTLEBOT3_PREDICATE_PACKAGE_VERSION
        ),
        "predicate_package_sha256": (
            NAV2_TURTLEBOT3_PREDICATE_PACKAGE_SHA256
        ),
        "outcome_claim_id": NAV2_TURTLEBOT3_OUTCOME_CLAIM_ID,
        "outcome_claim_scope": NAV2_TURTLEBOT3_OUTCOME_CLAIM_SCOPE,
        "satisfied_alternative": None,
        "evidence_readiness": "incomplete",
        "status": "unverified",
        "evaluated_outcome_claim": False,
        "actual_verification_basis": VerificationBasis.UNVERIFIED.value,
        "evidence_origins": [],
        "reasons": list(reasons),
        "predicate_package_evaluated": False,
        "adapter_completion_claimed": adapter_completion_claimed,
        "adapter_completion_scope": adapter_completion_scope,
        "completion_claimed": False,
        "completion_scope": "none",
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "operational_closure_created": False,
        "physical_execution_invoked": False,
        "claim_boundary": (
            "No Mission Contract completion claim was established. Adapter "
            "completion remains a separately recorded, insufficient input."
        ),
    }


__all__ = [
    "NAV2_TURTLEBOT3_RUNTIME_COMPLETION_SCHEMA_VERSION",
    "NAV2_TURTLEBOT3_RUNTIME_CONTRACT_VERSION",
    "NAV2_TURTLEBOT3_RUNTIME_MAXIMUM_OBSERVATION_AGE_SECONDS",
    "build_nav2_turtlebot3_runtime_contract",
    "evaluate_nav2_turtlebot3_runtime_result",
]

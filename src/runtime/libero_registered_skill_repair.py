"""Governed LIBERO Repair binding for deterministic registered skills.

The wrapper keeps registered skill execution distinct from model inference.
Skill selection is an exact residual-predicate registry match; human approval,
single-use dispatch, simulator effects, and verifier-owned stable hold remain
separate runtime facts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from missionos_core import canonical_sha256
from src.runtime.groot_libero_same_world_repair import (
    REGISTERED_SKILL_BINDING_SCHEMA_VERSION,
    REGISTERED_SKILL_LIBERO_EXECUTION_ADAPTER,
    DispatchLedger,
    build_same_world_repair_proposal,
    normalize_goal_predicates,
    run_same_world_repair,
)


REGISTERED_SKILL_ACTION_STEPS = 1
REGISTERED_SKILL_STABLE_HOLD_STEPS = 20
REGISTERED_SKILL_HOLD_ACTION_7D = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0)
MOKA_POT_2_STOVE_SKILL_ID = "privileged_push_moka_pot_2_to_stove_region.v1"
REGISTERED_SKILL_COMPLETION_BASIS = "skill_complete_and_predicate_conjunction"


def select_registered_libero_skill(
    *,
    environment: str,
    source_goal_predicates: Sequence[Mapping[str, Any]],
) -> str:
    """Return the one exact skill registered for the observed residual."""

    normalized = normalize_goal_predicates(
        environment=environment,
        observations=source_goal_predicates,
    )
    residual = [
        (item["predicate_name"], tuple(item["arguments"]))
        for item in normalized
        if not item["satisfied"]
    ]
    expected = [("on", ("moka_pot_2", "flat_stove_1_cook_region"))]
    if residual != expected:
        raise ValueError("registered_skill_exact_residual_match_not_found")
    return MOKA_POT_2_STOVE_SKILL_ID


def build_registered_skill_binding(
    *,
    skill_id: str,
    privileged_state_required: bool,
) -> dict[str, Any]:
    normalized_skill_id = str(skill_id or "").strip()
    if not normalized_skill_id:
        raise ValueError("registered_skill_id_required")
    if not isinstance(privileged_state_required, bool):
        raise TypeError("registered_skill_privileged_state_boolean_required")
    material = {
        "schema_version": REGISTERED_SKILL_BINDING_SCHEMA_VERSION,
        "skill_id": normalized_skill_id,
        "selection_basis": "exact_registered_residual_predicate_match",
        "privileged_state_required": privileged_state_required,
        "model_inference_required": False,
        "completion_basis": REGISTERED_SKILL_COMPLETION_BASIS,
    }
    return {**material, "binding_sha256": canonical_sha256(material)}


def build_registered_skill_same_world_repair_proposal(
    *,
    environment: str,
    environment_session_id: str,
    source_contract_sha256: str,
    source_goal_predicates: Sequence[Mapping[str, Any]],
    reset_count: int,
    maximum_repair_steps: int,
    source_object_poses: Mapping[str, Sequence[float]],
    skill_id: str = MOKA_POT_2_STOVE_SKILL_ID,
    privileged_state_required: bool = True,
    proposal_id: str | None = None,
    proposed_at: str | None = None,
) -> dict[str, Any]:
    """Select and bind one exact registered skill to a live failure world."""

    selected_skill_id = select_registered_libero_skill(
        environment=environment,
        source_goal_predicates=source_goal_predicates,
    )
    if skill_id != selected_skill_id:
        raise ValueError("registered_skill_selection_does_not_match_residual")
    binding = build_registered_skill_binding(
        skill_id=skill_id,
        privileged_state_required=privileged_state_required,
    )
    return build_same_world_repair_proposal(
        environment=environment,
        environment_session_id=environment_session_id,
        source_contract_sha256=source_contract_sha256,
        source_goal_predicates=source_goal_predicates,
        reset_count=reset_count,
        maximum_repair_chunks=maximum_repair_steps,
        n_action_steps=REGISTERED_SKILL_ACTION_STEPS,
        maximum_repair_steps=maximum_repair_steps,
        execution_adapter=REGISTERED_SKILL_LIBERO_EXECUTION_ADAPTER,
        proposal_id=proposal_id,
        proposed_at=proposed_at,
        source_object_poses=source_object_poses,
        post_conjunction_stability_steps=REGISTERED_SKILL_STABLE_HOLD_STEPS,
        post_conjunction_hold_action=REGISTERED_SKILL_HOLD_ACTION_7D,
        registered_skill_binding=binding,
        preservation_requires_contact_observation=False,
    )


def run_registered_skill_same_world_repair(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    dispatch_ledger: DispatchLedger,
    initial_observation: Any,
    invoke_skill: Callable[[Any, int], tuple[Any, Mapping[str, Any]]],
    apply_action_step: Callable[[Any, int], tuple[Any, Mapping[str, Any]]],
    apply_verifier_hold_step: Callable[
        [Sequence[float], int], tuple[Any, Mapping[str, Any]]
    ],
    observe_goal_predicates: Callable[[], Sequence[Mapping[str, Any]]],
    observed_reset_count: Callable[[], int],
) -> dict[str, Any]:
    """Execute a registered skill without claiming model inference."""

    if proposal.get("execution_adapter") != REGISTERED_SKILL_LIBERO_EXECUTION_ADAPTER:
        raise ValueError("registered_skill_execution_adapter_mismatch")
    binding = proposal.get("registered_skill_binding")
    if not isinstance(binding, Mapping):
        raise ValueError("registered_skill_binding_required")

    def invoke_executor(
        observation: Any,
        _instruction: str,
        step_index: int,
    ) -> tuple[Any, Mapping[str, Any]]:
        action, raw_evidence = invoke_skill(observation, step_index)
        evidence = dict(raw_evidence)
        evidence.update(
            {
                "registered_skill_runtime_invoked": True,
                "model_runtime_invoked": False,
                "registered_skill_id": binding["skill_id"],
                "registered_skill_binding_sha256": binding["binding_sha256"],
                "repair_contract_sha256": proposal["repair_contract_sha256"],
                "registered_skill_ready_for_stability": raw_evidence.get(
                    "registered_skill_ready_for_stability", False
                ),
            }
        )
        return action, evidence

    result = run_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=dispatch_ledger,
        initial_observation=initial_observation,
        invoke_model=invoke_executor,
        apply_action_chunk=apply_action_step,
        apply_verifier_hold_step=apply_verifier_hold_step,
        observe_goal_predicates=observe_goal_predicates,
        observed_reset_count=observed_reset_count,
    )
    if result.get("model_inference_invoked") is not False:
        raise RuntimeError("registered_skill_result_claimed_model_inference")
    if result.get("registered_skill_execution_invoked") is not True:
        raise RuntimeError("registered_skill_result_execution_missing")
    return result


__all__ = [
    "MOKA_POT_2_STOVE_SKILL_ID",
    "REGISTERED_SKILL_ACTION_STEPS",
    "REGISTERED_SKILL_HOLD_ACTION_7D",
    "REGISTERED_SKILL_STABLE_HOLD_STEPS",
    "build_registered_skill_binding",
    "build_registered_skill_same_world_repair_proposal",
    "run_registered_skill_same_world_repair",
    "select_registered_libero_skill",
]

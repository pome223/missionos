"""VLA-0 binding for the bounded LIBERO same-world Repair core.

VLA-0's published LIBERO evaluation performs a new policy query at every
simulator step (``action_horizon=1``) while ensembling up to eight predictions.
This module binds that one-action execution unit to the Mission Contract.  It
does not grant VLA-0 approval, dispatch, completion, or verifier authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.runtime.groot_libero_same_world_repair import (
    DEFAULT_REPAIR_INSTRUCTION_VARIANT,
    STATE_CONTINUITY_BASES,
    STATE_CONTINUITY_LIVE_SAME_WORLD,
    VLA0_LIBERO_EXECUTION_ADAPTER,
    DispatchLedger,
    build_same_world_repair_proposal,
    run_same_world_repair,
)


VLA0_LIBERO_ACTION_STEPS = 1
VLA0_STABLE_SUCCESS_STEPS = 20
VLA0_VERIFIER_HOLD_ACTION_7D = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0)


def build_vla0_same_world_repair_proposal(
    *,
    environment: str,
    environment_session_id: str,
    source_contract_sha256: str,
    source_goal_predicates: Sequence[Mapping[str, Any]],
    reset_count: int,
    maximum_repair_steps: int,
    proposal_id: str | None = None,
    proposed_at: str | None = None,
    source_object_poses: Mapping[str, Sequence[float]] | None = None,
    preserved_object_max_displacement_metres: float = 0.005,
    repair_instruction_variant: str = DEFAULT_REPAIR_INSTRUCTION_VARIANT,
    state_continuity_basis: str = STATE_CONTINUITY_LIVE_SAME_WORLD,
    diagnostic_handoff_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    """Build an approval-eligible proposal bound to official VLA-0 stepping."""

    if isinstance(maximum_repair_steps, bool) or maximum_repair_steps <= 0:
        raise ValueError("vla0_repair_maximum_steps_invalid")
    return build_same_world_repair_proposal(
        environment=environment,
        environment_session_id=environment_session_id,
        source_contract_sha256=source_contract_sha256,
        source_goal_predicates=source_goal_predicates,
        reset_count=reset_count,
        maximum_repair_chunks=maximum_repair_steps,
        n_action_steps=VLA0_LIBERO_ACTION_STEPS,
        execution_adapter=VLA0_LIBERO_EXECUTION_ADAPTER,
        repair_instruction_variant=repair_instruction_variant,
        proposal_id=proposal_id,
        proposed_at=proposed_at,
        source_object_poses=source_object_poses,
        preserved_object_max_displacement_metres=preserved_object_max_displacement_metres,
        post_conjunction_stability_steps=VLA0_STABLE_SUCCESS_STEPS,
        post_conjunction_hold_action=VLA0_VERIFIER_HOLD_ACTION_7D,
        state_continuity_basis=state_continuity_basis,
        diagnostic_handoff_snapshot_sha256=diagnostic_handoff_snapshot_sha256,
    )


def run_vla0_same_world_repair(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    dispatch_ledger: DispatchLedger,
    initial_observation: Any,
    invoke_model: Callable[[Any, str, int], tuple[Any, Mapping[str, Any]]],
    apply_action_chunk: Callable[[Any, int], tuple[Any, Mapping[str, Any]]],
    apply_verifier_hold_step: Callable[
        [Sequence[float], int], tuple[Any, Mapping[str, Any]]
    ],
    observe_goal_predicates: Callable[[], Sequence[Mapping[str, Any]]],
    observed_reset_count: Callable[[], int],
    observed_state_continuity_basis: str = STATE_CONTINUITY_LIVE_SAME_WORLD,
) -> dict[str, Any]:
    """Run one VLA-0-bound dispatch through the shared verifier authority."""

    if proposal.get("execution_adapter") != VLA0_LIBERO_EXECUTION_ADAPTER:
        raise ValueError("vla0_repair_execution_adapter_mismatch")
    contract = proposal.get("repair_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("vla0_repair_contract_required")
    if contract.get("n_action_steps") != VLA0_LIBERO_ACTION_STEPS:
        raise ValueError("vla0_repair_action_steps_mismatch")
    stability = contract.get("post_conjunction_stability")
    if (
        not isinstance(stability, Mapping)
        or stability.get("authority") != "verifier_owned"
        or stability.get("required_steps") != VLA0_STABLE_SUCCESS_STEPS
        or stability.get("hold_action_7d") != list(VLA0_VERIFIER_HOLD_ACTION_7D)
        or stability.get("policy_inference_allowed_during_hold") is not False
    ):
        raise ValueError("vla0_stability_contract_mismatch")
    if observed_state_continuity_basis not in STATE_CONTINUITY_BASES:
        raise ValueError("vla0_repair_state_continuity_basis_invalid")
    return run_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=dispatch_ledger,
        initial_observation=initial_observation,
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        apply_verifier_hold_step=apply_verifier_hold_step,
        observe_goal_predicates=observe_goal_predicates,
        observed_reset_count=observed_reset_count,
        observed_state_continuity_basis=observed_state_continuity_basis,
    )


__all__ = [
    "VLA0_LIBERO_ACTION_STEPS",
    "VLA0_STABLE_SUCCESS_STEPS",
    "VLA0_VERIFIER_HOLD_ACTION_7D",
    "build_vla0_same_world_repair_proposal",
    "run_vla0_same_world_repair",
]

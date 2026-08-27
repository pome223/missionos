"""Cosmos Policy Predict2 binding for bounded LIBERO same-world Repair.

The official LIBERO policy predicts sixteen actions together with future visual
state and a value.  MissionOS admits those actions under an applied-action
budget; the final chunk may therefore be shorter than sixteen.  Predicted
future state is diagnostic evidence only and never grants completion authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from math import ceil
from typing import Any

from src.runtime.groot_libero_same_world_repair import (
    COSMOS_POLICY_LIBERO_EXECUTION_ADAPTER,
    STATE_CONTINUITY_BASES,
    STATE_CONTINUITY_LIVE_SAME_WORLD,
    DispatchLedger,
    build_same_world_repair_proposal,
    run_same_world_repair,
)


COSMOS_POLICY_LIBERO_ACTION_STEPS = 16


def build_cosmos_policy_same_world_repair_proposal(
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
    repair_instruction_variant: str = "original_task",
    state_continuity_basis: str = STATE_CONTINUITY_LIVE_SAME_WORLD,
    diagnostic_handoff_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a proposal bound to the official 16-action Cosmos policy unit."""

    if isinstance(maximum_repair_steps, bool) or maximum_repair_steps <= 0:
        raise ValueError("cosmos_policy_repair_maximum_steps_invalid")
    maximum_chunks = ceil(maximum_repair_steps / COSMOS_POLICY_LIBERO_ACTION_STEPS)
    return build_same_world_repair_proposal(
        environment=environment,
        environment_session_id=environment_session_id,
        source_contract_sha256=source_contract_sha256,
        source_goal_predicates=source_goal_predicates,
        reset_count=reset_count,
        maximum_repair_chunks=maximum_chunks,
        n_action_steps=COSMOS_POLICY_LIBERO_ACTION_STEPS,
        maximum_repair_steps=maximum_repair_steps,
        execution_adapter=COSMOS_POLICY_LIBERO_EXECUTION_ADAPTER,
        repair_instruction_variant=repair_instruction_variant,
        proposal_id=proposal_id,
        proposed_at=proposed_at,
        source_object_poses=source_object_poses,
        preserved_object_max_displacement_metres=preserved_object_max_displacement_metres,
        state_continuity_basis=state_continuity_basis,
        diagnostic_handoff_snapshot_sha256=diagnostic_handoff_snapshot_sha256,
    )


def run_cosmos_policy_same_world_repair(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    dispatch_ledger: DispatchLedger,
    initial_observation: Any,
    invoke_model: Callable[[Any, str, int], tuple[Any, Mapping[str, Any]]],
    apply_action_chunk: Callable[[Any, int], tuple[Any, Mapping[str, Any]]],
    observe_goal_predicates: Callable[[], Sequence[Mapping[str, Any]]],
    observed_reset_count: Callable[[], int],
    observed_state_continuity_basis: str = STATE_CONTINUITY_LIVE_SAME_WORLD,
) -> dict[str, Any]:
    """Run one Cosmos-bound dispatch through the shared predicate verifier."""

    if proposal.get("execution_adapter") != COSMOS_POLICY_LIBERO_EXECUTION_ADAPTER:
        raise ValueError("cosmos_policy_repair_execution_adapter_mismatch")
    contract = proposal.get("repair_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("cosmos_policy_repair_contract_required")
    if contract.get("n_action_steps") != COSMOS_POLICY_LIBERO_ACTION_STEPS:
        raise ValueError("cosmos_policy_repair_action_steps_mismatch")
    if observed_state_continuity_basis not in STATE_CONTINUITY_BASES:
        raise ValueError("cosmos_policy_repair_state_continuity_basis_invalid")
    return run_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=dispatch_ledger,
        initial_observation=initial_observation,
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=observe_goal_predicates,
        observed_reset_count=observed_reset_count,
        observed_state_continuity_basis=observed_state_continuity_basis,
    )


__all__ = [
    "COSMOS_POLICY_LIBERO_ACTION_STEPS",
    "build_cosmos_policy_same_world_repair_proposal",
    "run_cosmos_policy_same_world_repair",
]

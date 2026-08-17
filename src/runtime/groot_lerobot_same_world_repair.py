"""LeRobot binding for the existing bounded LIBERO same-world Repair core.

This module owns no new authority semantics. It fixes the LeRobot action
execution contract (16 actions per model chunk) and delegates proposal,
approval, dispatch, verification, and single-use ledger behavior to the shared
LIBERO Repair runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.runtime.groot_libero_same_world_repair import (
    LEROBOT_GROOT_N17_EXECUTION_ADAPTER,
    STATE_CONTINUITY_BASES,
    STATE_CONTINUITY_LIVE_SAME_WORLD,
    DispatchLedger,
    build_same_world_repair_proposal,
    run_same_world_repair,
)


LEROBOT_GROOT_N17_ACTION_STEPS = 16


def build_lerobot_same_world_repair_proposal(
    *,
    environment: str,
    environment_session_id: str,
    source_contract_sha256: str,
    source_goal_predicates: Sequence[Mapping[str, Any]],
    reset_count: int,
    maximum_repair_chunks: int,
    proposal_id: str | None = None,
    proposed_at: str | None = None,
    source_object_poses: Mapping[str, Sequence[float]] | None = None,
    preserved_object_max_displacement_metres: float = 0.005,
    repair_instruction_variant: str = "short_target",
    state_continuity_basis: str = STATE_CONTINUITY_LIVE_SAME_WORLD,
    diagnostic_handoff_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a Contract-bound LeRobot Repair instruction ablation.

    The short target-only instruction is intentional: the live instruction
    ablation showed that the longer preservation clause can be out of the
    checkpoint's task-language distribution. Preservation remains enforced by
    the deterministic verifier, not delegated to language compliance.
    """

    return build_same_world_repair_proposal(
        environment=environment,
        environment_session_id=environment_session_id,
        source_contract_sha256=source_contract_sha256,
        source_goal_predicates=source_goal_predicates,
        reset_count=reset_count,
        maximum_repair_chunks=maximum_repair_chunks,
        n_action_steps=LEROBOT_GROOT_N17_ACTION_STEPS,
        execution_adapter=LEROBOT_GROOT_N17_EXECUTION_ADAPTER,
        repair_instruction_variant=repair_instruction_variant,
        proposal_id=proposal_id,
        proposed_at=proposed_at,
        source_object_poses=source_object_poses,
        preserved_object_max_displacement_metres=(preserved_object_max_displacement_metres),
        state_continuity_basis=state_continuity_basis,
        diagnostic_handoff_snapshot_sha256=diagnostic_handoff_snapshot_sha256,
    )


def run_lerobot_same_world_repair(
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
    """Run one LeRobot-bound dispatch without creating inner dispatches."""

    if proposal.get("execution_adapter") != LEROBOT_GROOT_N17_EXECUTION_ADAPTER:
        raise ValueError("lerobot_repair_execution_adapter_mismatch")
    contract = proposal.get("repair_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("lerobot_repair_contract_required")
    if contract.get("n_action_steps") != LEROBOT_GROOT_N17_ACTION_STEPS:
        raise ValueError("lerobot_repair_action_steps_mismatch")
    if observed_state_continuity_basis not in STATE_CONTINUITY_BASES:
        raise ValueError("lerobot_repair_state_continuity_basis_invalid")
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
    "LEROBOT_GROOT_N17_ACTION_STEPS",
    "build_lerobot_same_world_repair_proposal",
    "run_lerobot_same_world_repair",
]

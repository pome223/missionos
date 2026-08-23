from __future__ import annotations

from copy import deepcopy

import numpy as np
import pytest

from missionos_core import canonical_sha256
from scripts.run_vla0_libero_snapshot_recovery import _official_ensemble_action
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.runtime.groot_libero_same_world_repair import (
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    VLA0_LIBERO_EXECUTION_ADAPTER,
    approve_same_world_repair,
    build_same_world_repair_dispatch,
)
from src.runtime.libero_panda_predicate_package import LIBERO_PANDA_SCENE8_ENVIRONMENT
from src.runtime.vla0_libero_same_world_repair import (
    VLA0_LIBERO_ACTION_STEPS,
    build_vla0_same_world_repair_proposal,
    run_vla0_same_world_repair,
)


def _vector(*, second: bool = False) -> list[dict]:
    predicates = [
        ("on", ["moka_pot_1", "flat_stove_1_cook_region"], True),
        ("on", ["moka_pot_2", "flat_stove_1_cook_region"], second),
        ("turnon", ["flat_stove_1"], True),
    ]
    return [
        {
            "predicate_index": index,
            "predicate_id": canonical_sha256(
                {
                    "predicate_index": index,
                    "predicate_name": name,
                    "arguments": arguments,
                }
            ),
            "predicate_name": name,
            "arguments": arguments,
            "satisfied": satisfied,
        }
        for index, (name, arguments, satisfied) in enumerate(predicates)
    ]


def _authorization(*, maximum_steps: int = 2, diagnostic_clone: bool = False):
    proposal = build_vla0_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="vla0-live-world:fixture",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        maximum_repair_steps=maximum_steps,
        proposal_id="vla0-proposal:fixture",
        proposed_at="2026-08-23T00:00:00+00:00",
        state_continuity_basis=(
            STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE if diagnostic_clone else "live_same_world"
        ),
        diagnostic_handoff_snapshot_sha256=("b" * 64 if diagnostic_clone else None),
    )
    approval = approve_same_world_repair(
        proposal=proposal,
        operator_approval_ref="operator:vla0-fixture",
        approval_id="vla0-approval:fixture",
        approved_at="2026-08-23T00:01:00+00:00",
    )
    dispatch = build_same_world_repair_dispatch(
        proposal=proposal,
        approval=approval,
        dispatch_ref="vla0-dispatch:fixture",
        created_at="2026-08-23T00:02:00+00:00",
    )
    return proposal, approval, dispatch


def _step_trace(*, chunk_index: int, vector: list[dict]) -> list[dict]:
    conjunction = all(item["satisfied"] for item in vector)
    witnesses = {}
    for predicate_index, object_name in enumerate(("moka_pot_1", "moka_pot_2")):
        on_stove = vector[predicate_index]["satisfied"]
        local_delta = [0.0 if on_stove else 0.085, 0.0, 0.02]
        half_extent = [0.075, 0.075, 0.0025]
        witnesses[object_name] = {
            "object_name": object_name,
            "position_metres": [0.1 + predicate_index, 0.2, 0.3],
            "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            "linear_velocity_metres_per_second": [0.0, 0.0, 0.0],
            "angular_velocity_radians_per_second": [0.0, 0.0, 0.0],
            "step_translation_distance_metres": 0.001,
            "end_effector_distance_metres": 0.2,
            "gripper_contact_observed": False,
            "stove_region_witness": {
                "region_name": "flat_stove_1_cook_region",
                "local_delta_metres": local_delta,
                "half_extent_metres": half_extent,
                "axis_margins_metres": {
                    "x": half_extent[0] - abs(local_delta[0]),
                    "y": half_extent[1] - abs(local_delta[1]),
                    "z_lower": local_delta[2] - (half_extent[2] - 0.005),
                    "z_upper": (half_extent[2] + 0.10) - local_delta[2],
                },
                "inside_under_region": on_stove,
                "stove_parent_contact_observed": on_stove,
                "on_predicate_witness": on_stove,
            },
        }
    return [
        {
            "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
            "chunk_index": chunk_index,
            "action_step_index": 0,
            "action_step_number": 1,
            "global_repair_step_index": chunk_index,
            "global_repair_step_number": chunk_index + 1,
            "action_step_sha256": canonical_sha256({"chunk_index": chunk_index}),
            "goal_predicate_observations": vector,
            "goal_predicate_vector_sha256": canonical_sha256(
                {"goal_predicate_observations": vector}
            ),
            "official_predicate_conjunction": conjunction,
            "official_predicate_result": conjunction,
            "conjunction_matches_official_result": True,
            "object_witnesses": witnesses,
        }
    ]


def test_vla0_contract_binds_official_one_step_execution_and_original_task() -> None:
    proposal, approval, dispatch = _authorization(maximum_steps=720)

    assert VLA0_LIBERO_ACTION_STEPS == 1
    assert proposal["execution_adapter"] == VLA0_LIBERO_EXECUTION_ADAPTER
    assert proposal["repair_instruction"] == "put both moka pots on the stove"
    assert proposal["repair_contract"]["n_action_steps"] == 1
    assert proposal["repair_contract"]["maximum_repair_steps"] == 720
    assert approval["execution_adapter"] == VLA0_LIBERO_EXECUTION_ADAPTER
    assert dispatch["execution_adapter"] == VLA0_LIBERO_EXECUTION_ADAPTER


def test_vla0_run_reaches_same_verifier_without_controller_ack_claim(tmp_path) -> None:
    proposal, approval, dispatch = _authorization(maximum_steps=2)
    current = _vector()

    def invoke_model(observation, instruction, chunk_index):
        assert instruction == "put both moka pots on the stove"
        return [[0.0] * 6 + [-1.0]], {
            "model_runtime_invoked": True,
            "repair_instruction_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_exact_match": True,
            "repair_instruction_payload_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_length": len(instruction),
            "repair_instruction_payload_kind": "text",
            "repair_instruction_payload_dtype": "utf-8",
            "repair_instruction_payload_shape": [len(instruction)],
            "policy_request_sha256": canonical_sha256({"chunk_index": chunk_index}),
            "policy_response_sha256": canonical_sha256({"action": chunk_index}),
        }

    def apply_action_chunk(action_chunk, chunk_index):
        nonlocal current
        current = _vector(second=chunk_index == 1)
        conjunction = all(item["satisfied"] for item in current)
        return {"version": chunk_index + 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": canonical_sha256({"actions": action_chunk}),
            "official_predicate_result": conjunction,
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index, vector=deepcopy(current)
            ),
        }

    result = run_vla0_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
        initial_observation={"version": 0},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current),
        observed_reset_count=lambda: 1,
    )

    assert result["status"] == "satisfied"
    assert result["chunks_executed"] == 2
    assert result["n_action_steps"] == 1
    assert result["execution_adapter"] == VLA0_LIBERO_EXECUTION_ADAPTER
    assert result["task_completion_claimed"] is True
    assert all(
        chunk["controller_ack_observed"] is False for chunk in result["chunk_evidence"]
    )


def test_vla0_wrapper_rejects_gr00t_adapter_before_model_invocation(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()
    forged = deepcopy(proposal)
    forged["execution_adapter"] = "lerobot_groot_n17_select_action_v1"

    with pytest.raises(ValueError, match="vla0_repair_execution_adapter_mismatch"):
        run_vla0_same_world_repair(
            proposal=forged,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
            initial_observation={},
            invoke_model=lambda *_: pytest.fail("must fail before model invocation"),
            apply_action_chunk=lambda *_: pytest.fail("must fail before execution"),
            observe_goal_predicates=lambda: _vector(),
            observed_reset_count=lambda: 1,
        )


def test_shared_builder_rejects_vla0_multi_action_contract() -> None:
    from src.runtime.groot_libero_same_world_repair import build_same_world_repair_proposal

    with pytest.raises(ValueError, match="vla0_libero_requires_one_action_step"):
        build_same_world_repair_proposal(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            environment_session_id="vla0-live-world:forged",
            source_contract_sha256="a" * 64,
            source_goal_predicates=_vector(),
            reset_count=1,
            maximum_repair_chunks=45,
            n_action_steps=8,
            execution_adapter=VLA0_LIBERO_EXECUTION_ADAPTER,
        )


def test_official_temporal_ensemble_shifts_prior_prediction_by_one_step() -> None:
    prior: list[np.ndarray] = []
    first = np.arange(56, dtype=np.float32).reshape(8, 7)
    second = np.full((8, 7), 100.0, dtype=np.float32)

    selected_first = _official_ensemble_action(
        prediction=first,
        previous_predictions=prior,
    )
    selected_second = _official_ensemble_action(
        prediction=second,
        previous_predictions=prior,
    )

    np.testing.assert_array_equal(selected_first, first[0])
    np.testing.assert_allclose(selected_second, (0.5 * first[1] + second[0]) / 1.5)
    assert len(prior) == 2
    np.testing.assert_array_equal(prior[0], first[1:])
    np.testing.assert_array_equal(prior[1], second)


def test_diagnostic_clone_success_cannot_claim_task_completion(tmp_path) -> None:
    proposal, approval, dispatch = _authorization(maximum_steps=1, diagnostic_clone=True)
    current = _vector()

    def invoke_model(_observation, instruction, _chunk_index):
        return [0.0] * 6 + [-1.0], {
            "model_runtime_invoked": True,
            "repair_instruction_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_exact_match": True,
            "repair_instruction_payload_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_length": len(instruction),
            "repair_instruction_payload_kind": "list",
            "repair_instruction_payload_dtype": "str",
            "repair_instruction_payload_shape": [1],
            "policy_request_sha256": "c" * 64,
            "policy_response_sha256": "d" * 64,
        }

    def apply_action_chunk(action_chunk, chunk_index):
        nonlocal current
        current = _vector(second=True)
        return {"version": 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": canonical_sha256({"actions": action_chunk}),
            "official_predicate_result": True,
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                vector=deepcopy(current),
            ),
        }

    result = run_vla0_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch-diagnostic.json"),
        initial_observation={"version": 0},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current),
        observed_reset_count=lambda: 1,
        observed_state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    )

    assert result["status"] == "satisfied_diagnostic_observation"
    assert result["predicate_improvement_observed"] is True
    assert result["task_completion_claimed"] is False
    assert result["semantic_repair_claim_eligible"] is False

from __future__ import annotations

from copy import deepcopy

import pytest

from missionos_core import canonical_sha256
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.runtime.groot_lerobot_same_world_repair import (
    LEROBOT_GROOT_N17_ACTION_STEPS,
    build_lerobot_same_world_repair_proposal,
    run_lerobot_same_world_repair,
)
from src.runtime.groot_libero_same_world_repair import (
    LEROBOT_GROOT_N17_EXECUTION_ADAPTER,
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    approve_same_world_repair,
    build_same_world_repair_dispatch,
)
from src.runtime.libero_panda_predicate_package import (
    LIBERO_PANDA_SCENE8_ENVIRONMENT,
)


def _vector(*, first: bool = False, second: bool = True, stove: bool = True) -> list[dict]:
    predicates = [
        ("on", ["moka_pot_1", "flat_stove_1_cook_region"], first),
        ("on", ["moka_pot_2", "flat_stove_1_cook_region"], second),
        ("turnon", ["flat_stove_1"], stove),
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


def _step_trace(
    *,
    chunk_index: int,
    final_first: bool,
    lose_second_at: int | None = None,
) -> list[dict]:
    trace = []
    for action_step_index in range(LEROBOT_GROOT_N17_ACTION_STEPS):
        first = final_first and action_step_index == LEROBOT_GROOT_N17_ACTION_STEPS - 1
        second = lose_second_at is None or action_step_index < lose_second_at
        vector = _vector(first=first, second=second)
        witnesses = {}
        for predicate_index, object_name in enumerate(("moka_pot_1", "moka_pot_2")):
            on_stove = vector[predicate_index]["satisfied"]
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
                    "local_delta_metres": [0.0 if on_stove else 0.085, 0.0, 0.02],
                    "half_extent_metres": [0.075, 0.075, 0.0025],
                    "axis_margins_metres": {
                        "x": 0.075 if on_stove else -0.01,
                        "y": 0.075,
                        "z_lower": 0.0225,
                        "z_upper": 0.0825,
                    },
                    "inside_under_region": on_stove,
                    "stove_parent_contact_observed": on_stove,
                    "on_predicate_witness": on_stove,
                },
            }
        conjunction = all(item["satisfied"] for item in vector)
        global_index = chunk_index * LEROBOT_GROOT_N17_ACTION_STEPS + action_step_index
        trace.append(
            {
                "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
                "chunk_index": chunk_index,
                "action_step_index": action_step_index,
                "action_step_number": action_step_index + 1,
                "global_repair_step_index": global_index,
                "global_repair_step_number": global_index + 1,
                "action_step_sha256": canonical_sha256(
                    {"chunk_index": chunk_index, "action_step_index": action_step_index}
                ),
                "goal_predicate_observations": vector,
                "goal_predicate_vector_sha256": canonical_sha256(
                    {"goal_predicate_observations": vector}
                ),
                "official_predicate_conjunction": conjunction,
                "official_predicate_result": conjunction,
                "conjunction_matches_official_result": True,
                "object_witnesses": witnesses,
            }
        )
    return trace


def _authorization(*, maximum_chunks: int = 2):
    proposal = build_lerobot_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="lerobot-live-world:fixture",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        maximum_repair_chunks=maximum_chunks,
        proposal_id="lerobot-proposal:fixture",
        proposed_at="2026-08-16T00:00:00+00:00",
    )
    approval = approve_same_world_repair(
        proposal=proposal,
        operator_approval_ref="operator:fixture",
        approval_id="lerobot-approval:fixture",
        approved_at="2026-08-16T00:01:00+00:00",
    )
    dispatch = build_same_world_repair_dispatch(
        proposal=proposal,
        approval=approval,
        dispatch_ref="lerobot-dispatch:fixture",
        created_at="2026-08-16T00:02:00+00:00",
    )
    return proposal, approval, dispatch


def test_lerobot_contract_binds_adapter_chunk_size_and_short_instruction() -> None:
    proposal, approval, dispatch = _authorization()

    assert proposal["execution_adapter"] == LEROBOT_GROOT_N17_EXECUTION_ADAPTER
    assert proposal["repair_contract"]["n_action_steps"] == 16
    assert proposal["repair_contract"]["maximum_repair_steps"] == 32
    assert proposal["repair_instruction"] == "put the first moka pot on the stove"
    assert approval["execution_adapter"] == LEROBOT_GROOT_N17_EXECUTION_ADAPTER
    assert dispatch["execution_adapter"] == LEROBOT_GROOT_N17_EXECUTION_ADAPTER


def test_lerobot_contract_can_bind_frozen_original_task_instruction() -> None:
    proposal = build_lerobot_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="lerobot-live-world:original-task-ablation",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        maximum_repair_chunks=45,
        repair_instruction_variant="original_task",
    )

    assert proposal["repair_instruction"] == "put both moka pots on the stove"
    assert proposal["repair_instruction_variant"] == "original_task"
    assert proposal["repair_contract"]["instruction_ablation"] == {
        "controlled_variable": "repair_instruction",
        "variant": "original_task",
        "target_specific_instruction": False,
        "fixed_comparison_metrics": [
            "target_minimum_end_effector_distance_metres",
            "target_gripper_contact_steps",
            "target_maximum_displacement_metres",
            "protected_object_gripper_contact_steps",
            "protected_object_maximum_displacement_metres",
            "chunk_predicate_timeline",
            "terminal_status",
        ],
        "root_cause_established": False,
    }


def test_lerobot_diagnostic_clone_is_digest_bound_and_cannot_claim_completion(
    tmp_path,
) -> None:
    proposal = build_lerobot_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="lerobot-diagnostic-clone:fixture",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        maximum_repair_chunks=1,
        state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
        diagnostic_handoff_snapshot_sha256="c" * 64,
    )
    approval = approve_same_world_repair(
        proposal=proposal,
        operator_approval_ref="operator:diagnostic-fixture",
    )
    dispatch = build_same_world_repair_dispatch(
        proposal=proposal,
        approval=approval,
        dispatch_ref="dispatch:diagnostic-fixture",
    )
    current_vector = _vector()

    def invoke_model(observation, instruction, chunk_index):
        return {"chunk_index": chunk_index}, {
            "model_runtime_invoked": True,
            "repair_instruction_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_exact_match": True,
            "repair_instruction_payload_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_length": len(instruction),
            "repair_instruction_payload_kind": "list",
            "repair_instruction_payload_dtype": None,
            "repair_instruction_payload_shape": [1],
            "policy_request_sha256": "request:diagnostic",
            "policy_response_sha256": "response:diagnostic",
        }

    def apply_action_chunk(action, chunk_index):
        current_vector[:] = _vector(first=True)
        return {"version": 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": canonical_sha256(action),
            "official_predicate_result": True,
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                final_first=True,
            ),
        }

    result = run_lerobot_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
        initial_observation={"version": 0},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current_vector),
        observed_reset_count=lambda: 1,
        observed_state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    )

    assert proposal["diagnostic_handoff_snapshot_sha256"] == "c" * 64
    assert proposal["semantic_repair_claim_eligible"] is False
    assert result["status"] == "satisfied_diagnostic_observation"
    assert result["predicate_improvement_observed"] is True
    assert result["task_completion_claimed"] is False


def test_lerobot_closed_loop_reobserves_and_completes_once(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()
    current_vector = _vector()
    observed_versions = []

    def invoke_model(observation, instruction, chunk_index):
        observed_versions.append(observation["version"])
        return {"chunk_index": chunk_index}, {
            "model_runtime_invoked": True,
            "repair_instruction_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_exact_match": True,
            "repair_instruction_payload_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_length": len(instruction),
            "repair_instruction_payload_kind": "list",
            "repair_instruction_payload_dtype": None,
            "repair_instruction_payload_shape": [1],
            "policy_request_sha256": f"request:{chunk_index}",
            "policy_response_sha256": f"response:{chunk_index}",
        }

    def apply_action_chunk(action, chunk_index):
        final_first = chunk_index == 1
        if final_first:
            current_vector[:] = _vector(first=True)
        return {"version": chunk_index + 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": canonical_sha256(action),
            "official_predicate_result": final_first,
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                final_first=final_first,
            ),
        }

    ledger = DispatchAuthorityTable(tmp_path / "dispatch.json")
    result = run_lerobot_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=ledger,
        initial_observation={"version": 0},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current_vector),
        observed_reset_count=lambda: 1,
    )

    assert observed_versions == [0, 1]
    assert result["status"] == "satisfied"
    assert result["chunks_executed"] == 2
    assert result["n_action_steps"] == 16
    assert result["execution_adapter"] == LEROBOT_GROOT_N17_EXECUTION_ADAPTER
    assert result["task_completion_claimed"] is True
    assert result["additional_attempt_authorized"] is False
    replay = ledger.claim_dispatch_ref(
        dispatch_ref=dispatch["dispatch_ref"],
        request_payload={"different": "payload"},
    )
    assert replay["send_permitted"] is False


def test_lerobot_wrapper_rejects_another_execution_adapter(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()
    forged = deepcopy(proposal)
    forged["execution_adapter"] = "isaac_groot_zmq_multistep_v1"

    with pytest.raises(ValueError, match="execution_adapter_mismatch"):
        run_lerobot_same_world_repair(
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


def test_lerobot_approval_and_dispatch_cannot_change_adapter(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()
    forged_approval = deepcopy(approval)
    forged_approval["execution_adapter"] = "isaac_groot_zmq_multistep_v1"
    forged_approval["approval_sha256"] = canonical_sha256(
        {key: value for key, value in forged_approval.items() if key != "approval_sha256"}
    )
    with pytest.raises(ValueError, match="approval_execution_adapter_binding_mismatch"):
        build_same_world_repair_dispatch(
            proposal=proposal,
            approval=forged_approval,
            dispatch_ref="forged-dispatch",
        )

    forged_dispatch = deepcopy(dispatch)
    forged_dispatch["execution_adapter"] = "isaac_groot_zmq_multistep_v1"
    forged_dispatch["dispatch_sha256"] = canonical_sha256(
        {key: value for key, value in forged_dispatch.items() if key != "dispatch_sha256"}
    )
    with pytest.raises(ValueError, match="dispatch_execution_adapter_binding_mismatch"):
        run_lerobot_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=forged_dispatch,
            dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
            initial_observation={},
            invoke_model=lambda *_: pytest.fail("must fail before model invocation"),
            apply_action_chunk=lambda *_: pytest.fail("must fail before execution"),
            observe_goal_predicates=lambda: _vector(),
            observed_reset_count=lambda: 1,
        )


@pytest.mark.parametrize(
    ("field", "forged_value", "reason"),
    [
        ("n_action_steps", 8, "dispatch_action_steps_binding_mismatch"),
        ("maximum_repair_chunks", 1, "dispatch_chunk_budget_binding_mismatch"),
        ("maximum_repair_chunks", 90, "dispatch_chunk_budget_binding_mismatch"),
    ],
)
def test_lerobot_dispatch_cannot_change_chunk_semantics(
    tmp_path, field: str, forged_value: int, reason: str
) -> None:
    proposal, approval, dispatch = _authorization()
    forged_dispatch = deepcopy(dispatch)
    forged_dispatch[field] = forged_value
    forged_dispatch["dispatch_sha256"] = canonical_sha256(
        {key: value for key, value in forged_dispatch.items() if key != "dispatch_sha256"}
    )

    with pytest.raises(ValueError, match=reason):
        run_lerobot_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=forged_dispatch,
            dispatch_ledger=DispatchAuthorityTable(tmp_path / f"{field}.json"),
            initial_observation={},
            invoke_model=lambda *_: pytest.fail("must fail before model invocation"),
            apply_action_chunk=lambda *_: pytest.fail("must fail before execution"),
            observe_goal_predicates=lambda: _vector(),
            observed_reset_count=lambda: 1,
        )


def test_lerobot_stops_before_next_chunk_when_preserved_predicate_is_lost(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()
    current_vector = _vector()
    invocations = []

    def invoke_model(observation, instruction, chunk_index):
        invocations.append(chunk_index)
        return {"chunk_index": chunk_index}, {
            "model_runtime_invoked": True,
            "repair_instruction_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_exact_match": True,
            "repair_instruction_payload_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_length": len(instruction),
            "repair_instruction_payload_kind": "list",
            "repair_instruction_payload_dtype": None,
            "repair_instruction_payload_shape": [1],
            "policy_request_sha256": "request:0",
            "policy_response_sha256": "response:0",
        }

    def apply_action_chunk(action, chunk_index):
        current_vector[:] = _vector(second=False)
        return {"version": 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": canonical_sha256(action),
            "official_predicate_result": False,
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                final_first=False,
                lose_second_at=5,
            ),
        }

    result = run_lerobot_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
        initial_observation={"version": 0},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current_vector),
        observed_reset_count=lambda: 1,
    )

    assert invocations == [0]
    assert result["status"] == "stopped_on_preservation_violation"
    assert result["task_completion_claimed"] is False
    assert result["first_preservation_violation"]["action_step_number"] == 6
    assert result["additional_attempt_authorized"] is False

from __future__ import annotations

from copy import deepcopy

import pytest

from missionos_core import canonical_sha256
from scripts.run_libero_registered_skill_same_world_repair import (
    _build_repair_diagnostic_report,
)
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.runtime.groot_libero_same_world_repair import (
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    REGISTERED_SKILL_LIBERO_EXECUTION_ADAPTER,
    approve_same_world_repair,
    build_same_world_repair_dispatch,
)
from src.runtime.libero_panda_predicate_package import LIBERO_PANDA_SCENE8_ENVIRONMENT
from src.runtime.libero_registered_skill_repair import (
    MOKA_POT_2_STOVE_SKILL_ID,
    REGISTERED_SKILL_HOLD_ACTION_7D,
    build_registered_skill_same_world_repair_proposal,
    run_registered_skill_same_world_repair,
)


def _vector(*, second: bool) -> list[dict]:
    predicates = (
        ("on", ["moka_pot_1", "flat_stove_1_cook_region"], True),
        ("on", ["moka_pot_2", "flat_stove_1_cook_region"], second),
        ("turnon", ["flat_stove_1"], True),
    )
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


def _witnesses(*, second: bool) -> dict[str, dict]:
    result = {}
    for predicate_index, object_name in enumerate(("moka_pot_1", "moka_pot_2")):
        on_stove = predicate_index == 0 or second
        result[object_name] = {
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
    return result


def _step_trace(*, global_index: int, second: bool) -> list[dict]:
    vector = _vector(second=second)
    return [
        {
            "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
            "chunk_index": global_index,
            "action_step_index": 0,
            "action_step_number": 1,
            "global_repair_step_index": global_index,
            "global_repair_step_number": global_index + 1,
            "action_step_sha256": canonical_sha256({"step": global_index}),
            "goal_predicate_observations": vector,
            "goal_predicate_vector_sha256": canonical_sha256(
                {"goal_predicate_observations": vector}
            ),
            "official_predicate_conjunction": second,
            "official_predicate_result": second,
            "conjunction_matches_official_result": True,
            "object_witnesses": _witnesses(second=second),
        }
    ]


def _authorization():
    proposal = build_registered_skill_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="libero-live-world:registered-skill-fixture",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(second=False),
        reset_count=1,
        maximum_repair_steps=24,
        source_object_poses={"moka_pot_1": [0.1, 0.2, 0.3]},
        proposal_id="registered-skill-proposal:fixture",
        proposed_at="2026-08-30T00:00:00+00:00",
    )
    approval = approve_same_world_repair(
        proposal=proposal,
        operator_approval_ref="operator:registered-skill-fixture",
        approval_id="registered-skill-approval:fixture",
        approved_at="2026-08-30T00:01:00+00:00",
    )
    dispatch = build_same_world_repair_dispatch(
        proposal=proposal,
        approval=approval,
        dispatch_ref="registered-skill-dispatch:fixture",
        created_at="2026-08-30T00:02:00+00:00",
    )
    return proposal, approval, dispatch


def test_registered_skill_closes_supervisory_loop_without_model_claim(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()
    current = _vector(second=False)
    invoked_steps: list[int] = []
    applied_steps: list[int] = []
    hold_steps: list[int] = []

    def invoke_skill(observation, step_index):
        invoked_steps.append(step_index)
        return [0.2, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0], {
            "skill_stage": "push_target_to_stove",
            "privileged_state_read": True,
            "registered_skill_ready_for_stability": step_index >= 2,
        }

    def apply_action_step(action, step_index):
        applied_steps.append(step_index)
        recovered = step_index >= 1
        current[:] = _vector(second=recovered)
        return {"version": step_index + 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": canonical_sha256({"action": action}),
            "official_predicate_result": recovered,
            "preservation_step_trace": _step_trace(
                global_index=step_index,
                second=recovered,
            ),
        }

    def apply_hold(action, global_index):
        assert list(action) == list(REGISTERED_SKILL_HOLD_ACTION_7D)
        hold_steps.append(global_index)
        return {"version": global_index + 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": False,
            "policy_inference_invoked": False,
            "verifier_hold_step": True,
            "verifier_hold_action_sha256": canonical_sha256(
                {"verifier_hold_action_7d": list(action)}
            ),
            "official_predicate_result": True,
            "preservation_step_trace": _step_trace(
                global_index=global_index,
                second=True,
            ),
        }

    result = run_registered_skill_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
        initial_observation={"version": 0},
        invoke_skill=invoke_skill,
        apply_action_step=apply_action_step,
        apply_verifier_hold_step=apply_hold,
        observe_goal_predicates=lambda: deepcopy(current),
        observed_reset_count=lambda: 1,
    )

    assert invoked_steps == [0, 1, 2]
    assert applied_steps == [0, 1, 2]
    assert hold_steps == list(range(3, 23))
    assert proposal["execution_adapter"] == REGISTERED_SKILL_LIBERO_EXECUTION_ADAPTER
    assert proposal["registered_skill_binding"]["skill_id"] == MOKA_POT_2_STOVE_SKILL_ID
    assert (
        proposal["repair_contract"]["preservation_invariant"][
            "requires_contact_observation"
        ]
        is False
    )
    assert result["status"] == "stable_satisfied"
    assert result["stable_completion_observed"] is True
    assert result["task_completion_claimed"] is True
    assert result["same_world_state_preserved"] is True
    assert result["model_inference_invoked"] is False
    assert result["registered_skill_execution_invoked"] is True
    assert result["registered_skill_action_count"] == 3
    assert result["policy_action_count"] == 0
    assert result["verifier_hold_action_count"] == 20
    assert result["dispatch_receipt_present"] is True
    assert result["controller_ack_observed"] is False
    assert result["physical_execution_invoked"] is False

    diagnostic = _build_repair_diagnostic_report(
        repair_result=result,
        raw_trace=[
            {
                "action_7d": [0.2, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0],
                "target_gripper_contact_observed": index == 1,
            }
            for index in range(3)
        ]
        + [
            {
                "action_7d": list(REGISTERED_SKILL_HOLD_ACTION_7D),
                "target_gripper_contact_observed": False,
            }
            for _ in range(20)
        ],
    )
    assert diagnostic["validation_status"] == "verified"
    assert diagnostic["bounded_stable_repair_observed"] is True
    assert [axis["status"] for axis in diagnostic["axes"]] == ["satisfied"] * 5
    assert diagnostic["approval_created"] is False
    assert diagnostic["dispatch_authority_created"] is False


def test_registered_skill_binding_tamper_is_rejected() -> None:
    proposal, _, _ = _authorization()
    proposal["registered_skill_binding"]["skill_id"] = "unapproved-skill"

    with pytest.raises(ValueError, match="proposal_sha256_mismatch"):
        approve_same_world_repair(
            proposal=proposal,
            operator_approval_ref="operator:fixture",
        )

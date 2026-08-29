from __future__ import annotations

from copy import deepcopy
import json
import pickle
from types import SimpleNamespace

import numpy as np
import pytest

from missionos_core import canonical_sha256
from scripts.run_vla0_libero_curriculum_probe import (
    EXACT_SEED0_THREE_CENTIMETRE_SNAPSHOT_SHA256,
    _actual_effect_statistics,
    _validate_probe_identity,
    _validate_curriculum_fixture_snapshot,
)
from scripts.run_vla0_libero_snapshot_recovery import (
    _capture_frames,
    _official_ensemble_action,
    _verify_loaded_dataset_stats,
    _validate_scripted_fixture_snapshot,
)
from scripts.replay_vla0_libero_curriculum_stability import _read_verified_trace
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.runtime.groot_libero_same_world_repair import (
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    VLA0_LIBERO_EXECUTION_ADAPTER,
    approve_same_world_repair,
    build_same_world_repair_dispatch,
)
from src.runtime.libero_panda_predicate_package import LIBERO_PANDA_SCENE8_ENVIRONMENT
from src.runtime.libero_repair_failure_fixture import (
    SCRIPTED_FAILURE_FIXTURE_BASIS,
    failure_fixture_contract,
)
from src.runtime.vla0_libero_same_world_repair import (
    VLA0_LIBERO_ACTION_STEPS,
    VLA0_STABLE_SUCCESS_STEPS,
    VLA0_VERIFIER_HOLD_ACTION_7D,
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


def _authorization(*, maximum_steps: int = 22, diagnostic_clone: bool = False):
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


def test_vla0_contract_binds_predicate_driven_semantic_repair_and_stability() -> None:
    proposal, approval, dispatch = _authorization(maximum_steps=720)

    assert VLA0_LIBERO_ACTION_STEPS == 1
    assert proposal["execution_adapter"] == VLA0_LIBERO_EXECUTION_ADAPTER
    assert proposal["repair_instruction"] == (
        "Place the second moka pot on the stove. Keep the first moka pot on the "
        "stove and keep the stove turned on."
    )
    assert proposal["repair_instruction_variant"] == "semantic_preserve"
    assert proposal["repair_intent_selection"]["selection_source"] == (
        "deterministic_non_model_predicate_diagnosis"
    )
    assert proposal["repair_intent_selection"]["repair_instruction"] == proposal[
        "repair_instruction"
    ]
    assert proposal["repair_intent_selection"]["human_supplied_runtime_instruction"] is False
    assert proposal["repair_contract"]["n_action_steps"] == 1
    assert proposal["repair_contract"]["maximum_repair_steps"] == 720
    assert proposal["repair_contract"]["post_conjunction_stability"][
        "required_steps"
    ] == VLA0_STABLE_SUCCESS_STEPS
    assert approval["execution_adapter"] == VLA0_LIBERO_EXECUTION_ADAPTER
    assert dispatch["execution_adapter"] == VLA0_LIBERO_EXECUTION_ADAPTER


def test_vla0_run_reaches_stable_verdict_without_controller_ack_claim(tmp_path) -> None:
    proposal, approval, dispatch = _authorization(maximum_steps=22)
    current = _vector()

    def invoke_model(observation, instruction, chunk_index):
        assert instruction == proposal["repair_instruction"]
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

    hold_calls = 0

    def apply_verifier_hold_step(action, global_action_index):
        nonlocal hold_calls
        hold_calls += 1
        assert list(action) == list(VLA0_VERIFIER_HOLD_ACTION_7D)
        return {"version": global_action_index + 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": False,
            "official_predicate_result": True,
            "policy_inference_invoked": False,
            "verifier_hold_step": True,
            "verifier_hold_action_sha256": canonical_sha256(
                {"verifier_hold_action_7d": list(VLA0_VERIFIER_HOLD_ACTION_7D)}
            ),
            "preservation_step_trace": _step_trace(
                chunk_index=global_action_index,
                vector=deepcopy(current),
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
        apply_verifier_hold_step=apply_verifier_hold_step,
        observe_goal_predicates=lambda: deepcopy(current),
        observed_reset_count=lambda: 1,
    )

    assert result["status"] == "stable_satisfied"
    assert result["chunks_executed"] == 2
    assert result["n_action_steps"] == 1
    assert result["execution_adapter"] == VLA0_LIBERO_EXECUTION_ADAPTER
    assert result["task_completion_claimed"] is True
    assert result["stable_completion_observed"] is True
    assert result["final_verdict"] == "stable"
    assert result["policy_action_count"] == 2
    assert result["verifier_hold_action_count"] == VLA0_STABLE_SUCCESS_STEPS
    assert result["total_simulator_action_count"] == 22
    assert hold_calls == VLA0_STABLE_SUCCESS_STEPS
    assert all(chunk["controller_ack_observed"] is False for chunk in result["chunk_evidence"])


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
            apply_verifier_hold_step=lambda *_: pytest.fail(
                "must fail before stability execution"
            ),
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


def test_official_temporal_ensemble_matches_pinned_upstream_v1_reference() -> None:
    """Compare against NVlabs/vla0 b78db19e eval.py version-1 logic."""

    rng = np.random.default_rng(1000)
    implementation_history: list[np.ndarray] = []
    reference_history: list[np.ndarray] = []

    for _ in range(12):
        prediction = rng.normal(size=(8, 7)).astype(np.float32)
        observed = _official_ensemble_action(
            prediction=prediction,
            previous_predictions=implementation_history,
        )

        reference_history.append(prediction.copy())
        if len(reference_history) > 8:
            reference_history.pop(0)
        retained: list[np.ndarray] = []
        combined = np.zeros_like(reference_history[-1])
        weights = np.zeros_like(reference_history[-1])
        for old_prediction in reference_history[:-1]:
            if len(old_prediction) <= 1:
                continue
            shifted = old_prediction[1:]
            retained.append(shifted)
            combined[: len(shifted)] += 0.5 * shifted
            weights[: len(shifted)] += 0.5
        retained.append(reference_history[-1])
        combined += reference_history[-1]
        weights += 1.0
        reference_history = retained
        expected = (combined / weights)[0]

        np.testing.assert_array_equal(observed, expected)


def test_loaded_action_decoder_stats_match_verified_checkpoint(tmp_path) -> None:
    action_stats = {
        "min": np.asarray([-0.9, -0.8, -0.7, -0.2, -0.3, -0.4, -1.0]),
        "max": np.asarray([0.9, 0.8, 0.7, 0.2, 0.3, 0.4, 1.0]),
    }
    with (tmp_path / "dataset_stats.pkl").open("wb") as stream:
        pickle.dump({"out_ori_act": action_stats}, stream)
    model = SimpleNamespace(
        original_dataset_stats={"out_ori_act": action_stats},
        dataset_stats=action_stats,
    )

    evidence = _verify_loaded_dataset_stats(
        model=model,
        verified_checkpoint_path=tmp_path,
    )

    assert evidence["loaded_values_match_verified_file"] is True
    assert evidence["action_dimension"] == 7
    assert evidence["min"] == action_stats["min"].tolist()
    assert evidence["max"] == action_stats["max"].tolist()


def _scripted_fixture_metadata() -> dict:
    fixture = {
        **failure_fixture_contract("displaced_from_stove"),
        "stable_failure_fixture_observed": True,
        "terminal_goal_predicate_vector": [True, False, True],
    }
    return {
        "source_failure_basis": SCRIPTED_FAILURE_FIXTURE_BASIS,
        "scripted_failure_fixture": fixture,
    }


def test_scripted_fixture_snapshot_validation_binds_authority_free_setup() -> None:
    fixture = _validate_scripted_fixture_snapshot(
        metadata=_scripted_fixture_metadata(),
        scenario="displaced_from_stove",
    )

    assert fixture["authority"] == "test_fixture_only"
    assert fixture["human_approval_created"] is False
    assert fixture["governed_dispatch_created"] is False
    assert fixture["model_inference_invoked"] is False
    assert fixture["terminal_goal_predicate_vector"] == [True, False, True]


def test_scripted_fixture_snapshot_validation_rejects_scenario_drift() -> None:
    metadata = _scripted_fixture_metadata()
    metadata["scripted_failure_fixture"]["scenario"] = "wrong_table_location"

    with pytest.raises(
        RuntimeError,
        match="vla0_scripted_fixture_snapshot_contract_mismatch:scenario",
    ):
        _validate_scripted_fixture_snapshot(
            metadata=metadata,
            scenario="displaced_from_stove",
        )


def _curriculum_fixture_metadata() -> dict:
    fixture = {
        "schema_version": "missionos.libero_displacement_curriculum_fixture.v2",
        "authority": "diagnostic_fixture_only",
        "construction": "protected_separating_horizontal_ray_from_success_state",
        "environment_seed": 0,
        "requested_translation_from_source_metres": 0.03,
        "observed_translation_from_source_metres": 0.030001,
        "protected_object_displacement_metres": 0.0,
        "fixture_settle_steps_applied": 60,
        "fixture_settle_trace": [
            {"fixture_step_index": index, "predicate_vector": [True, False, True]}
            for index in range(60)
        ],
        "terminal_goal_predicate_vector": [True, False, True],
        "actual_predicate_failure_observed": True,
        "model_inference_invoked": False,
        "repair_attempted": False,
        "physical_execution_invoked": False,
    }
    return {
        "source_failure_basis": "diagnostic_displacement_curriculum",
        "environment_seed": 0,
        "source_goal_predicate_vector": [True, False, True],
        "source_failure_is_repair_candidate": True,
        "displacement_curriculum_fixture": fixture,
        "displacement_curriculum_fixture_sha256": canonical_sha256(fixture),
    }


def test_curriculum_fixture_admission_binds_seed_stability_and_digest() -> None:
    fixture = _validate_curriculum_fixture_snapshot(_curriculum_fixture_metadata())

    assert fixture["requested_translation_from_source_metres"] == 0.03
    assert fixture["environment_seed"] == 0


def test_curriculum_fixture_admission_rejects_seed_mismatch() -> None:
    metadata = _curriculum_fixture_metadata()
    metadata["environment_seed"] = 7
    metadata["displacement_curriculum_fixture"]["environment_seed"] = 7
    metadata["displacement_curriculum_fixture_sha256"] = canonical_sha256(
        metadata["displacement_curriculum_fixture"]
    )

    with pytest.raises(RuntimeError, match="vla0_curriculum_fixture_contract_mismatch"):
        _validate_curriculum_fixture_snapshot(metadata)


def test_curriculum_probe_identity_requires_exact_seed0_three_centimetre_snapshot() -> None:
    fixture = _validate_curriculum_fixture_snapshot(_curriculum_fixture_metadata())

    _validate_probe_identity(
        snapshot_sha256=EXACT_SEED0_THREE_CENTIMETRE_SNAPSHOT_SHA256,
        fixture=fixture,
    )
    with pytest.raises(
        RuntimeError, match="vla0_curriculum_probe_snapshot_identity_mismatch"
    ):
        _validate_probe_identity(snapshot_sha256="0" * 64, fixture=fixture)


def test_actual_effect_statistics_records_initial_minimum_final_and_contact() -> None:
    first = _step_trace(chunk_index=0, vector=_vector())[0]
    second = _step_trace(chunk_index=1, vector=_vector())[0]
    first["object_witnesses"]["moka_pot_2"].update(
        {
            "end_effector_distance_metres": 0.25,
            "position_metres": [1.1, 0.2, 0.3],
            "gripper_contact_observed": False,
        }
    )
    second["object_witnesses"]["moka_pot_2"].update(
        {
            "end_effector_distance_metres": 0.20,
            "position_metres": [1.101, 0.2, 0.3],
            "gripper_contact_observed": True,
        }
    )
    result = _actual_effect_statistics(
        initial_eef_target_distance_metres=0.30,
        initial_target_position_metres=[1.1, 0.2, 0.3],
        repair_result={
            "chunk_evidence": [
                {"preservation_step_trace": [first]},
                {"preservation_step_trace": [second]},
            ]
        },
        raw_action_trace=[
            {"action_7d": [0, 0, 0, 0, 0, 0, -1]},
            {"action_7d": [0, 0, 0, 0, 0, 0, 1]},
        ],
    )

    assert result["initial_end_effector_distance_to_target_metres"] == 0.30
    assert result["minimum_end_effector_distance_to_target_metres"] == 0.20
    assert result["minimum_distance_after_action"] == 2
    assert result["final_end_effector_distance_to_target_metres"] == 0.20
    assert result["first_gripper_contact_after_action"] == 2
    assert result["maximum_target_translation_metres"] == pytest.approx(0.001)
    assert result["gripper_command"]["sign_transition_count"] == 1


def test_integrated_stability_hold_stops_on_first_target_regression(tmp_path) -> None:
    proposal, approval, dispatch = _authorization(maximum_steps=22, diagnostic_clone=True)
    current = _vector()
    model_calls = 0
    hold_calls = 0

    def invoke_model(_observation, instruction, chunk_index):
        nonlocal model_calls
        model_calls += 1
        return [0.0] * 6 + [-1.0], {
            "model_runtime_invoked": True,
            "repair_instruction_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_exact_match": True,
            "repair_instruction_payload_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_length": len(instruction),
            "repair_instruction_payload_kind": "list",
            "repair_instruction_payload_dtype": "str",
            "repair_instruction_payload_shape": [1],
            "policy_request_sha256": canonical_sha256({"request": chunk_index}),
            "policy_response_sha256": canonical_sha256({"response": chunk_index}),
        }

    def apply_action_chunk(action, chunk_index):
        del action
        nonlocal current
        current = _vector(second=True)
        return object(), {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "official_predicate_result": True,
            "action_chunk_sha256": canonical_sha256({"policy": chunk_index}),
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                vector=deepcopy(current),
            ),
        }

    def apply_verifier_hold_step(action, global_action_index):
        del action
        nonlocal current, hold_calls
        hold_calls += 1
        current = _vector(second=hold_calls < 3)
        conjunction = all(item["satisfied"] for item in current)
        return object(), {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": False,
            "official_predicate_result": conjunction,
            "policy_inference_invoked": False,
            "verifier_hold_step": True,
            "verifier_hold_action_sha256": canonical_sha256(
                {"verifier_hold_action_7d": list(VLA0_VERIFIER_HOLD_ACTION_7D)}
            ),
            "preservation_step_trace": _step_trace(
                chunk_index=global_action_index,
                vector=deepcopy(current),
            ),
        }

    result = run_vla0_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch-unstable.json"),
        initial_observation={},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        apply_verifier_hold_step=apply_verifier_hold_step,
        observe_goal_predicates=lambda: deepcopy(current),
        observed_reset_count=lambda: 1,
        observed_state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    )

    stability = result["post_conjunction_stability"]
    assert model_calls == 1
    assert hold_calls == 3
    assert result["status"] == "unstable_after_predicate_conjunction"
    assert result["predicate_conjunction_observed"] is True
    assert result["stable_completion_observed"] is False
    assert result["final_verdict"] == "unstable"
    assert stability["completed_steps"] == 2
    assert stability["termination_reason"] == "target_predicate_regressed"
    assert stability["policy_inference_invoked_during_hold"] is False


def test_stability_replay_admits_only_digest_bound_success_trace(tmp_path) -> None:
    material = {
        "source_contract": {
            "setup_snapshot_sha256": (
                "8064d6faeeb02a67a08649be0ca39529b4a79da459cf8d11493c0412bbc7b651"
            )
        },
        "source_goal_predicate_vector": [True, False, True],
        "final_goal_predicate_vector": [True, True, True],
        "diagnostic_clone_recovery_observed": True,
        "raw_action_trace": [
            {
                "global_repair_step_index": 0,
                "action_7d": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
            }
        ],
    }
    report = {**material, "result_sha256": canonical_sha256(material)}
    path = tmp_path / "base-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    actions, source = _read_verified_trace(path)

    assert actions == [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]]
    assert source == {"result_sha256": report["result_sha256"], "action_count": 1}


def test_frame_capture_writes_relative_digest_bound_pngs(tmp_path) -> None:
    observation = {
        "agentview_image": np.zeros((8, 12, 3), dtype=np.uint8),
        "robot0_eye_in_hand_image": np.full((8, 12, 3), 127, dtype=np.uint8),
    }

    record = _capture_frames(
        observation=observation,
        frame_capture_dir=tmp_path / "frames",
        artifact_root=tmp_path,
        step_number=3,
    )

    assert record["status"] == "captured"
    assert record["authority"] == "diagnostic_only"
    assert [camera["observation_key"] for camera in record["cameras"]] == [
        "video.image",
        "video.wrist_image",
    ]
    for camera in record["cameras"]:
        assert not camera["artifact_relative_path"].startswith("/")
        assert (tmp_path / camera["artifact_relative_path"]).is_file()


def test_diagnostic_clone_success_cannot_claim_task_completion(tmp_path) -> None:
    proposal, approval, dispatch = _authorization(maximum_steps=21, diagnostic_clone=True)
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

    def apply_verifier_hold_step(action, global_action_index):
        assert list(action) == list(VLA0_VERIFIER_HOLD_ACTION_7D)
        return {"version": global_action_index + 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": False,
            "official_predicate_result": True,
            "policy_inference_invoked": False,
            "verifier_hold_step": True,
            "verifier_hold_action_sha256": canonical_sha256(
                {"verifier_hold_action_7d": list(VLA0_VERIFIER_HOLD_ACTION_7D)}
            ),
            "preservation_step_trace": _step_trace(
                chunk_index=global_action_index,
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
        apply_verifier_hold_step=apply_verifier_hold_step,
        observe_goal_predicates=lambda: deepcopy(current),
        observed_reset_count=lambda: 1,
        observed_state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    )

    assert result["status"] == "stable_satisfied_diagnostic_observation"
    assert result["predicate_improvement_observed"] is True
    assert result["task_completion_claimed"] is False
    assert result["semantic_repair_claim_eligible"] is False
    assert result["stable_completion_observed"] is True

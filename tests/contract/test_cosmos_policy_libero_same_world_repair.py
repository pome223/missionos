from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import numpy as np
import pytest
from PIL import Image

from missionos_core import canonical_sha256
from scripts import analyze_cosmos_policy_future_actual_motion as future_actual_analyzer
from scripts import run_cosmos_policy_libero_experiment as experiment_runner
from scripts import run_cosmos_policy_libero_seed_probe as seed_probe
from scripts import probe_libero_displacement_curriculum as curriculum_probe
from scripts import run_cosmos_policy_libero_curriculum_probe as live_curriculum_probe
from scripts import run_cosmos_policy_libero_paired_pose_sensitivity as pose_sensitivity
from scripts import run_cosmos_policy_libero_pose_rollout_diagnostic as pose_rollout
from scripts import run_cosmos_policy_libero_t5_instruction_diagnostic as t5_instruction
from scripts import run_libero_curriculum_oracle as curriculum_oracle
from scripts import probe_libero_policy_input_visibility as input_visibility
from scripts import summarize_libero_oracle_repeatability as oracle_repeatability
from scripts import bind_libero_known_skill_repair_diagnostic as known_skill_binding
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.runtime.cosmos_policy_libero_same_world_repair import (
    COSMOS_POLICY_LIBERO_ACTION_STEPS,
    build_cosmos_policy_same_world_repair_proposal,
    run_cosmos_policy_same_world_repair,
)
from src.runtime.groot_libero_same_world_repair import (
    COSMOS_POLICY_LIBERO_EXECUTION_ADAPTER,
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    approve_same_world_repair,
    build_same_world_repair_dispatch,
)
from src.runtime.libero_panda_predicate_package import LIBERO_PANDA_SCENE8_ENVIRONMENT


def test_future_actual_motion_analysis_is_diagnostic_only(tmp_path) -> None:
    trial_root = tmp_path / "trial"
    actual_dir = trial_root / "repair" / "actual_observations"
    future_dir = trial_root / "repair" / "future_predictions"
    actual_dir.mkdir(parents=True)
    future_dir.mkdir(parents=True)
    cameras = []
    for step, value in ((0, 0), (16, 10)):
        step_cameras = []
        for key in ("agentview_image", "robot0_eye_in_hand_image"):
            path = actual_dir / f"step-{step:04d}-{key}.png"
            Image.fromarray(np.full((4, 4, 3), value, dtype=np.uint8)).save(path)
            step_cameras.append(
                {
                    "observation_key": key,
                    "artifact_relative_path": str(path.relative_to(trial_root)),
                }
            )
        cameras.append({"step_number": step, "cameras": step_cameras})
    future_images = []
    for key in ("future_image", "future_wrist_image"):
        path = future_dir / f"query-0000-{key}.png"
        Image.fromarray(np.full((4, 4, 3), 20, dtype=np.uint8)).save(path)
        future_images.append(
            {"prediction_key": key, "artifact_relative_path": str(path.relative_to(trial_root))}
        )
    report = {
        "result_sha256": "a" * 64,
        "actual_observation_manifest": cameras,
        "future_prediction_manifest": [
            {
                "query_index": 0,
                "applied_action_start_index": 0,
                "images": future_images,
            }
        ],
    }
    (trial_root / "repair" / "report.json").write_text(json.dumps(report), encoding="utf-8")

    result = future_actual_analyzer.analyze(
        trial_root=trial_root,
        output_path=tmp_path / "analysis.json",
    )

    assert result["agentview_mean_actual_motion_pixel_difference"] == 10
    assert result["agentview_mean_predicted_motion_pixel_difference"] == 20
    assert result["agentview_mean_prediction_error_pixel_difference"] == 10
    assert result["claim_boundary"]["future_predictions_may_establish_success"] is False
    assert result["claim_boundary"]["object_motion_established_by_pixel_difference"] is False


def test_pose_sensitivity_grid_uses_one_baseline_per_pose() -> None:
    poses, distances, seeds = pose_sensitivity._validate_grid(
        pose_action_counts=(0, 92, 184, 276, 369),
        displacements_metres=(0.0, 0.005, 0.05, 0.225),
        seeds=(17, 71, 195, 231),
    )

    assert len(poses) * len(distances) * len(seeds) == 80
    assert distances[0] == 0.0


def test_pose_sensitivity_action_delta_separates_translation_and_gripper() -> None:
    baseline = np.zeros((16, 7), dtype=np.float32)
    baseline[:, 6] = -1.0
    displaced = baseline.copy()
    displaced[:, 0] = 0.25
    displaced[:8, 6] = 1.0

    result = pose_sensitivity._action_delta(baseline, displaced)

    assert result["mean_translation_l2_delta"] == 0.25
    assert result["gripper_sign_mismatch_fraction"] == 0.5


def test_pose_rollout_requires_forward_only_sensitivity_admission(tmp_path) -> None:
    report_without_digest = {
        "schema_version": "missionos.cosmos_policy_libero_paired_pose_sensitivity.v1",
        "nominal_report_sha256": "nominal-digest",
        "forward_query_count": 80,
        "claim_boundary": {"queried_actions_applied_to_simulator": False},
    }
    report = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    path = tmp_path / "sensitivity.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    admitted = pose_rollout._verify_sensitivity_report(path, "nominal-digest")

    assert admitted["forward_query_count"] == 80
    assert admitted["claim_boundary"]["queried_actions_applied_to_simulator"] is False


def test_pose_rollout_requires_explicit_opt_in(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(pose_rollout.OPT_IN_ENV, raising=False)
    with pytest.raises(RuntimeError, match="cosmos_pose_rollout_opt_in_required"):
        pose_rollout.execute_live(
            source_root=tmp_path,
            checkpoint_path=tmp_path,
            tokenizer_path=tmp_path / "tokenizer.pth",
            nominal_report_path=tmp_path / "nominal.json",
            sensitivity_report_path=tmp_path / "sensitivity.json",
            output_dir=tmp_path / "output",
        )


def test_t5_instruction_parity_accepts_one_bf16_rounding_step(tmp_path) -> None:
    report = {
        "schema_version": "missionos.cosmos_policy_t5_11b_embedding_parity.v1",
        "baseline_prompt": t5_instruction.BASELINE_PROMPT,
        "model": "google-t5/t5-11b",
        "shape": [1, 512, 1024],
        "generated_storage_dtype": "torch.bfloat16",
        "maximum_absolute_difference": 0.00390625,
        "unknown_prompts": [prompt for _, prompt, _ in t5_instruction.PROMPT_ARMS],
    }
    path = tmp_path / "parity.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    admitted = t5_instruction._load_parity(path)

    assert admitted["maximum_absolute_difference"] == 0.00390625


def test_t5_instruction_parity_rejects_prompt_or_numeric_drift(tmp_path) -> None:
    report = {
        "schema_version": "missionos.cosmos_policy_t5_11b_embedding_parity.v1",
        "baseline_prompt": t5_instruction.BASELINE_PROMPT,
        "model": "google-t5/t5-11b",
        "shape": [1, 512, 1024],
        "generated_storage_dtype": "torch.bfloat16",
        "maximum_absolute_difference": 0.0078125,
        "unknown_prompts": [],
    }
    path = tmp_path / "parity.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(RuntimeError, match="cosmos_t5_instruction_numeric_parity_invalid"):
        t5_instruction._load_parity(path)


def test_t5_instruction_requires_explicit_opt_in(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(t5_instruction.OPT_IN_ENV, raising=False)
    with pytest.raises(RuntimeError, match="cosmos_t5_instruction_opt_in_required"):
        t5_instruction.execute_live(
            source_root=tmp_path / "source",
            checkpoint_path=tmp_path / "checkpoint",
            tokenizer_path=tmp_path / "tokenizer.pth",
            nominal_report_path=tmp_path / "nominal.json",
            sensitivity_report_path=tmp_path / "sensitivity.json",
            generated_embeddings_path=tmp_path / "generated.pkl",
            parity_report_path=tmp_path / "parity.json",
            output_dir=tmp_path / "output",
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


def _witnesses(vector: list[dict]) -> dict:
    result = {}
    for predicate_index, object_name in enumerate(("moka_pot_1", "moka_pot_2")):
        on_stove = vector[predicate_index]["satisfied"]
        local_delta = [0.0 if on_stove else 0.085, 0.0, 0.02]
        half_extent = [0.075, 0.075, 0.0025]
        result[object_name] = {
            "object_name": object_name,
            "position_metres": [0.1 + predicate_index, 0.2, 0.3],
            "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            "linear_velocity_metres_per_second": [0.0, 0.0, 0.0],
            "angular_velocity_radians_per_second": [0.0, 0.0, 0.0],
            "step_translation_distance_metres": 0.0,
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
    return result


def _trace(*, chunk_index: int, count: int, vector: list[dict]) -> list[dict]:
    conjunction = all(item["satisfied"] for item in vector)
    return [
        {
            "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
            "chunk_index": chunk_index,
            "action_step_index": action_step_index,
            "action_step_number": action_step_index + 1,
            "global_repair_step_index": chunk_index * 16 + action_step_index,
            "global_repair_step_number": chunk_index * 16 + action_step_index + 1,
            "action_step_sha256": canonical_sha256(
                {"chunk_index": chunk_index, "action_step_index": action_step_index}
            ),
            "goal_predicate_observations": deepcopy(vector),
            "goal_predicate_vector_sha256": canonical_sha256(
                {"goal_predicate_observations": vector}
            ),
            "official_predicate_conjunction": conjunction,
            "official_predicate_result": conjunction,
            "conjunction_matches_official_result": True,
            "object_witnesses": _witnesses(vector),
        }
        for action_step_index in range(count)
    ]


def _authorization(maximum_steps: int = 520):
    proposal = build_cosmos_policy_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="cosmos-policy-live-world:fixture",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        maximum_repair_steps=maximum_steps,
        proposal_id="cosmos-policy-proposal:fixture",
        proposed_at="2026-08-27T00:00:00+00:00",
    )
    approval = approve_same_world_repair(
        proposal=proposal,
        operator_approval_ref="operator:cosmos-policy-fixture",
        approval_id="cosmos-policy-approval:fixture",
        approved_at="2026-08-27T00:01:00+00:00",
    )
    dispatch = build_same_world_repair_dispatch(
        proposal=proposal,
        approval=approval,
        dispatch_ref="cosmos-policy-dispatch:fixture",
        created_at="2026-08-27T00:02:00+00:00",
    )
    return proposal, approval, dispatch


def test_contract_binds_520_applied_actions_to_32_full_chunks_and_8_actions() -> None:
    proposal, approval, dispatch = _authorization()

    assert COSMOS_POLICY_LIBERO_ACTION_STEPS == 16
    assert proposal["execution_adapter"] == COSMOS_POLICY_LIBERO_EXECUTION_ADAPTER
    assert proposal["repair_contract"]["maximum_repair_chunks"] == 33
    assert proposal["repair_contract"]["n_action_steps"] == 16
    assert proposal["repair_contract"]["maximum_repair_steps"] == 520
    assert approval["execution_adapter"] == COSMOS_POLICY_LIBERO_EXECUTION_ADAPTER
    assert dispatch["maximum_repair_steps"] == 520


def test_future_prediction_cannot_create_success_and_final_chunk_is_eight_actions(
    tmp_path,
) -> None:
    proposal, approval, dispatch = _authorization()
    current = _vector()
    applied_counts: list[int] = []
    future_predictions = []

    def invoke_model(_observation, instruction, chunk_index):
        future_predictions.append(
            {
                "query_index": chunk_index,
                "predicted_goal_satisfied": True,
                "authority": "diagnostic_only",
            }
        )
        return [[0.0] * 6 + [-1.0] for _ in range(16)], {
            "model_runtime_invoked": True,
            "repair_instruction_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_exact_match": True,
            "repair_instruction_payload_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_length": len(instruction),
            "repair_instruction_payload_kind": "str",
            "repair_instruction_payload_dtype": "utf-8",
            "repair_instruction_payload_shape": [1],
            "policy_request_sha256": canonical_sha256({"query_index": chunk_index}),
            "policy_response_sha256": canonical_sha256({"response": chunk_index}),
        }

    def apply_action_chunk(action_chunk, chunk_index):
        count = min(16, 520 - chunk_index * 16)
        applied_counts.append(count)
        return {"step": sum(applied_counts)}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": canonical_sha256({"actions": action_chunk[:count]}),
            "official_predicate_result": False,
            "preservation_step_trace": _trace(
                chunk_index=chunk_index,
                count=count,
                vector=current,
            ),
        }

    result = run_cosmos_policy_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
        initial_observation={"step": 0},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current),
        observed_reset_count=lambda: 1,
    )

    assert result["status"] == "budget_exhausted_without_improvement"
    assert result["predicate_conjunction_observed"] is False
    assert result["task_completion_claimed"] is False
    assert result["applied_action_count"] == 520
    assert applied_counts == [16] * 32 + [8]
    assert len(future_predictions) == 33
    assert all(item["predicted_goal_satisfied"] for item in future_predictions)


def test_actual_predicate_success_stops_run_even_if_prediction_says_failure(tmp_path) -> None:
    proposal, approval, dispatch = _authorization(maximum_steps=16)
    current = _vector()

    def invoke_model(_observation, instruction, _chunk_index):
        return [[0.0] * 7 for _ in range(16)], {
            "model_runtime_invoked": True,
            "repair_instruction_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_exact_match": True,
            "repair_instruction_payload_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_length": len(instruction),
            "repair_instruction_payload_kind": "str",
            "repair_instruction_payload_dtype": "utf-8",
            "repair_instruction_payload_shape": [1],
            "policy_request_sha256": "b" * 64,
            "policy_response_sha256": "c" * 64,
            "predicted_goal_satisfied": False,
        }

    def apply_action_chunk(action_chunk, chunk_index):
        nonlocal current
        current = _vector(second=True)
        return {"step": 16}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": canonical_sha256({"actions": action_chunk}),
            "official_predicate_result": True,
            "preservation_step_trace": _trace(
                chunk_index=chunk_index,
                count=16,
                vector=current,
            ),
        }

    result = run_cosmos_policy_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch-success.json"),
        initial_observation={"step": 0},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current),
        observed_reset_count=lambda: 1,
    )

    assert result["status"] == "satisfied"
    assert result["task_completion_claimed"] is True
    assert result["applied_action_count"] == 16
    assert result["controller_ack_observed"] is False
    assert result["physical_execution_invoked"] is False


def test_actual_predicate_success_can_stop_inside_admitted_chunk(tmp_path) -> None:
    proposal, approval, dispatch = _authorization(maximum_steps=16)
    current = _vector()

    def invoke_model(_observation, instruction, _chunk_index):
        return [[0.0] * 7 for _ in range(16)], {
            "model_runtime_invoked": True,
            "repair_instruction_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_exact_match": True,
            "repair_instruction_payload_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_length": len(instruction),
            "repair_instruction_payload_kind": "str",
            "repair_instruction_payload_dtype": "utf-8",
            "repair_instruction_payload_shape": [1],
            "policy_request_sha256": "d" * 64,
            "policy_response_sha256": "e" * 64,
        }

    def apply_action_chunk(_action_chunk, chunk_index):
        nonlocal current
        current = _vector(second=True)
        return {"step": 3}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": "f" * 64,
            "official_predicate_result": True,
            "stopped_early_on_official_success": True,
            "preservation_step_trace": _trace(
                chunk_index=chunk_index,
                count=3,
                vector=current,
            ),
        }

    result = run_cosmos_policy_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch-early.json"),
        initial_observation={"step": 0},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current),
        observed_reset_count=lambda: 1,
    )

    assert result["status"] == "satisfied"
    assert result["applied_action_count"] == 3


def test_future_and_actual_artifact_trees_are_disjoint_and_labeled(tmp_path) -> None:
    future_dir = tmp_path / "future_predictions"
    actual_dir = tmp_path / "actual_observations"
    experiment_runner._assert_disjoint_artifact_trees(
        future_prediction_dir=future_dir,
        actual_observation_dir=actual_dir,
    )
    future = experiment_runner._save_future_prediction(
        predictions={
            "future_image": np.zeros((8, 12, 3), dtype=np.uint8),
            "future_wrist_image": np.ones((8, 12, 3), dtype=np.uint8),
        },
        value_prediction=1.0,
        directory=future_dir,
        artifact_root=tmp_path,
        query_index=2,
        applied_action_start_index=32,
    )
    actual = experiment_runner._save_actual_observation(
        observation={
            "agentview_image": np.zeros((8, 12, 3), dtype=np.uint8),
            "robot0_eye_in_hand_image": np.ones((8, 12, 3), dtype=np.uint8),
        },
        directory=actual_dir,
        artifact_root=tmp_path,
        step_number=33,
    )

    assert future["authority"] == "diagnostic_only"
    assert future["may_establish_task_success"] is False
    assert actual["source"] == "actual_libero_simulator_observation"
    assert all(
        item["artifact_relative_path"].startswith("future_predictions/")
        for item in future["images"]
    )
    assert all(
        item["artifact_relative_path"].startswith("actual_observations/")
        for item in actual["cameras"]
    )


def test_nested_prediction_and_observation_directories_are_rejected(tmp_path) -> None:
    with pytest.raises(
        ValueError,
        match="cosmos_policy_prediction_observation_artifacts_not_disjoint",
    ):
        experiment_runner._assert_disjoint_artifact_trees(
            future_prediction_dir=tmp_path / "evidence",
            actual_observation_dir=tmp_path / "evidence" / "actual",
        )


def test_action_statistics_keep_command_magnitude_separate_from_physical_motion() -> None:
    trace = [
        {"action_7d": [0.3, 0.4, 0.0, 0.0, 0.0, 0.0, -1.01]},
        {"action_7d": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -0.99]},
        {"action_7d": [0.0, 0.0, 0.2, 0.0, 0.0, 0.0, 1.0]},
    ]

    result = experiment_runner._action_command_statistics(trace, chunk_size=2)

    assert result["authority"] == "diagnostic_only"
    assert result["command_space"] == "libero_env_step_action_input"
    assert result["physical_path_length_established"] is False
    assert result["normalization_scale_verified"] is False
    assert result["null_action_threshold_defined"] is False
    assert result["xyz_command_norm"]["mean"] == pytest.approx(0.7 / 3.0)
    assert result["xyz_command_norm"]["sum"] == pytest.approx(0.7)
    assert result["gripper_command"]["negative_count"] == 2
    assert result["gripper_command"]["positive_count"] == 1
    assert result["gripper_command"]["sign_transition_count"] == 1
    assert [chunk["applied_action_count"] for chunk in result["chunks"]] == [2, 1]


def test_action_statistics_report_non_monotonic_chunks_without_null_claim() -> None:
    trace = [
        {"action_7d": [value, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0]} for value in (0.4, 0.2, 0.5, 0.3)
    ]

    result = experiment_runner._action_command_statistics(trace, chunk_size=1)

    assert result["chunk_mean_xyz_command_norm_strictly_nonincreasing"] is False
    assert result["first_to_last_chunk_mean_xyz_command_norm_ratio"] == pytest.approx(0.75)
    assert result["gripper_command"]["sign_transition_count"] == 0
    assert "null_like_behavior_established" not in result


def test_gated_tokenizer_is_a_separately_digest_bound_input(monkeypatch, tmp_path) -> None:
    tokenizer_path = tmp_path / "tokenizer.pth"
    tokenizer_path.write_bytes(b"pinned-tokenizer-fixture")
    monkeypatch.setattr(
        experiment_runner,
        "UPSTREAM_TOKENIZER_SIZE_BYTES",
        tokenizer_path.stat().st_size,
    )
    monkeypatch.setattr(
        experiment_runner,
        "UPSTREAM_TOKENIZER_SHA256",
        experiment_runner._sha256_path(tokenizer_path),
    )

    evidence = experiment_runner._verify_tokenizer(tokenizer_path)

    assert evidence["repository"] == "nvidia/Cosmos-Predict2-2B-Video2World"
    assert evidence["relative_path"] == "tokenizer/tokenizer.pth"
    assert evidence["access_requirement"] == "upstream_gated_model_access"
    assert evidence["additional_training_performed"] is False


def test_unpinned_tokenizer_is_rejected(tmp_path) -> None:
    tokenizer_path = tmp_path / "tokenizer.pth"
    tokenizer_path.write_bytes(b"wrong-tokenizer")

    with pytest.raises(
        RuntimeError,
        match="cosmos_policy_gated_tokenizer_file_mismatch",
    ):
        experiment_runner._verify_tokenizer(tokenizer_path)


def _write_oracle_report(tmp_path, *, stable_success: bool) -> tuple:
    snapshot_path = tmp_path / "fixture.npz"
    snapshot_path.write_bytes(b"digest-bound-fixture")
    report_without_digest = {
        "schema_version": "missionos.libero_scripted_fixture_push_recoverability.v1",
        "status": (
            "scripted_7d_recoverability_observed"
            if stable_success
            else "scripted_7d_recoverability_not_observed"
        ),
        "snapshot_sha256": experiment_runner._sha256_path(snapshot_path),
        "stable_success_observed": stable_success,
        "stable_success_steps": 20,
        "terminal_goal_predicate_vector": (
            [True, True, True] if stable_success else [True, False, True]
        ),
        "preservation_violation_observed": False,
        "claim_boundary": {
            "same_7d_simulator_action_interface_used": True,
            "model_inference_invoked": False,
            "physical_execution_invoked": False,
        },
    }
    report = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    report_path = tmp_path / "oracle-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return snapshot_path, report_path


def test_oracle_gate_admits_only_same_hash_stable_diagnostic_recovery(tmp_path) -> None:
    snapshot_path, report_path = _write_oracle_report(tmp_path, stable_success=True)

    admission = experiment_runner._verify_oracle_recoverability_report(
        report_path=report_path,
        snapshot_path=snapshot_path,
    )

    assert admission["authority"] == "diagnostic_only"
    assert admission["may_establish_model_repair_success"] is False
    assert admission["snapshot_sha256"] == experiment_runner._sha256_path(snapshot_path)
    assert admission["stable_success_steps"] == 20


def test_oracle_gate_rejects_same_hash_negative_result(tmp_path) -> None:
    snapshot_path, report_path = _write_oracle_report(tmp_path, stable_success=False)

    with pytest.raises(
        RuntimeError,
        match="cosmos_policy_oracle_gate_recoverability_not_observed",
    ):
        experiment_runner._verify_oracle_recoverability_report(
            report_path=report_path,
            snapshot_path=snapshot_path,
        )


def test_oracle_gate_accepts_vla0_same_interface_report_shape(tmp_path) -> None:
    snapshot_path = tmp_path / "fixture.npz"
    snapshot_path.write_bytes(b"digest-bound-vla0-fixture")
    report_without_digest = {
        "schema_version": "missionos.vla0_same_interface_oracle_recoverability.v1",
        "status": "scripted_oracle_recoverability_established",
        "snapshot_sha256": experiment_runner._sha256_path(snapshot_path),
        "stable_success_observed": True,
        "stable_success_steps_completed": 20,
        "stable_success_steps_required": 20,
        "terminal_goal_predicate_vector": [True, True, True],
        "preservation_violation_observed": False,
        "claim_boundary": {
            "same_original_7d_action_interface_used": True,
            "model_inference_invoked": False,
            "physical_execution_invoked": False,
        },
    }
    report = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    report_path = tmp_path / "vla0-oracle-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    admission = experiment_runner._verify_oracle_recoverability_report(
        report_path=report_path,
        snapshot_path=snapshot_path,
    )

    assert admission["stable_success_steps"] == 20
    assert admission["report_schema_version"].startswith("missionos.vla0_")


def test_curriculum_oracle_event_summary_uses_preregistered_thresholds() -> None:
    trace = []
    distances = [0.50, 0.489, 0.488, 0.487]
    for action_index, distance in enumerate(distances, start=1):
        trace.append(
            {
                "action_index": action_index,
                "eef_target_distance_metres": distance,
                "target_displacement_metres": 0.0012 if action_index >= 3 else 0.0,
                "target_gripper_contact_observed": action_index == 4,
            }
        )

    events = curriculum_oracle._event_summary(
        trace, initial_eef_target_distance_metres=0.50
    )

    assert events["first_approach_after_action"] == 2
    assert events["first_target_motion_after_action"] == 3
    assert events["first_contact_after_action"] == 4
    assert events["definitions"]["first_approach"] == {
        "minimum_eef_target_distance_reduction_metres": 0.01,
        "consecutive_steps_required": 3,
    }


def test_curriculum_oracle_event_summary_preserves_absent_events() -> None:
    trace = [
        {
            "action_index": 1,
            "eef_target_distance_metres": 0.495,
            "target_displacement_metres": 0.0009,
            "target_gripper_contact_observed": False,
        }
    ]

    events = curriculum_oracle._event_summary(
        trace, initial_eef_target_distance_metres=0.50
    )

    assert events["first_approach_after_action"] is None
    assert events["first_target_motion_after_action"] is None
    assert events["first_contact_after_action"] is None


def test_policy_input_visibility_is_a_rejection_only_filter() -> None:
    source = np.zeros((8, 8, 3), dtype=np.uint8)
    fixture = source.copy()
    fixture[2:6, 2:6] = 20
    jpeg_noise = np.ones_like(source)

    metrics = input_visibility._camera_metrics(source, fixture, jpeg_noise)

    assert metrics["robust_changed_pixel_count"] == 16
    assert metrics["rejection_filter_passed"] is True
    assert metrics["robust_difference_bounding_box"] == {
        "x_min": 2,
        "y_min": 2,
        "x_max": 5,
        "y_max": 5,
        "width_pixels": 4,
        "height_pixels": 4,
    }


def test_policy_input_visibility_rejects_subthreshold_difference() -> None:
    source = np.zeros((8, 8, 3), dtype=np.uint8)
    fixture = source.copy()
    fixture[0:3, 0:3] = 20

    metrics = input_visibility._camera_metrics(
        source, fixture, np.ones_like(source)
    )

    assert metrics["robust_changed_pixel_count"] == 9
    assert metrics["rejection_filter_passed"] is False


def test_oracle_repeatability_requires_same_fixture_and_fixed_limit(tmp_path) -> None:
    reports = []
    for index, displacement in enumerate((0.001, 0.002, 0.003)):
        without_digest = {
            "snapshot_sha256": "a" * 64,
            "stable_success_observed": True,
            "terminal_goal_predicate_vector": [True, True, True],
            "preservation_violation_observed": False,
            "protected_maximum_displacement_metres": displacement,
            "actions_applied": 76 + index,
            "success_first_observed_after_action": 56 + index,
            "trajectory_events": {},
        }
        material = {
            **without_digest,
            "result_sha256": canonical_sha256(without_digest),
        }
        path = tmp_path / f"run-{index}.json"
        path.write_text(json.dumps(material), encoding="utf-8")
        reports.append(path)

    result = oracle_repeatability.summarize(
        reports=reports, output_path=tmp_path / "summary.json"
    )

    assert result["all_runs_stable_success_within_preregistered_preservation_limit"] is True
    assert result["preservation_limit_metres"] == 0.005
    assert result["protected_displacement_metres"] == {
        "minimum": 0.001,
        "maximum": 0.003,
        "mean": 0.002,
        "range": 0.002,
    }
    assert result["action_counts"]["range"] == 2


def test_known_skill_binding_is_privileged_and_not_learned_repair(tmp_path) -> None:
    snapshot = tmp_path / "fixture.npz"
    snapshot.write_bytes(b"fixture")
    oracle_without_digest = {
        "schema_version": "missionos.vla0_same_interface_oracle_recoverability.v2",
        "snapshot_sha256": hashlib.sha256(b"fixture").hexdigest(),
        "source_goal_predicate_vector": [True, False, True],
        "terminal_goal_predicate_vector": [True, True, True],
        "stable_success_observed": True,
        "preservation_violation_observed": False,
        "actions_applied": 54,
        "success_first_observed_after_action": 33,
        "trajectory_events": {"first_contact_after_action": 32},
        "claim_boundary": {
            "privileged_object_state_used_for_oracle_planning": True,
        },
    }
    oracle = {
        **oracle_without_digest,
        "result_sha256": canonical_sha256(oracle_without_digest),
    }
    oracle_path = tmp_path / "oracle.json"
    oracle_path.write_text(json.dumps(oracle), encoding="utf-8")

    result = known_skill_binding.bind(
        snapshot_path=snapshot,
        oracle_report_path=oracle_path,
        output_path=tmp_path / "binding.json",
    )

    assert result["selected_skill"]["first_contact_after_action"] == 32
    assert result["claim_boundary"]["privileged_object_state_used"] is True
    assert result["claim_boundary"]["learned_policy_repair_established"] is False


def _curriculum_fixture_metadata() -> dict:
    fixture = {
        "schema_version": experiment_runner.DISPLACEMENT_CURRICULUM_SCHEMA_VERSION,
        "authority": "diagnostic_fixture_only",
        "construction": experiment_runner.DISPLACEMENT_CURRICULUM_CONSTRUCTION,
        "environment_seed": experiment_runner.ENVIRONMENT_SEED,
        "requested_translation_from_source_metres": 0.005,
        "observed_translation_from_source_metres": 0.0050003,
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
        "environment_seed": experiment_runner.ENVIRONMENT_SEED,
        "source_failure_basis": experiment_runner.DISPLACEMENT_CURRICULUM_BASIS,
        "source_goal_predicate_vector": [True, False, True],
        "source_failure_is_repair_candidate": True,
        "displacement_curriculum_fixture": fixture,
        "displacement_curriculum_fixture_sha256": canonical_sha256(fixture),
    }


def test_curriculum_fixture_admission_is_digest_stability_and_claim_bound() -> None:
    admission = experiment_runner._validate_repair_fixture_snapshot(_curriculum_fixture_metadata())

    assert admission["fixture_family"] == "bounded_displacement_curriculum"
    assert admission["fixture_contract"]["requested_translation_from_source_metres"] == 0.005
    assert admission["fixture_contract"]["model_inference_invoked_for_fixture_creation"] is False


def test_curriculum_fixture_admission_rejects_settle_or_digest_drift() -> None:
    unstable = _curriculum_fixture_metadata()
    unstable["displacement_curriculum_fixture"]["fixture_settle_trace"][-1]["predicate_vector"] = [
        True,
        True,
        True,
    ]
    unstable["displacement_curriculum_fixture_sha256"] = canonical_sha256(
        unstable["displacement_curriculum_fixture"]
    )
    with pytest.raises(RuntimeError, match="cosmos_policy_curriculum_fixture_stability_invalid"):
        experiment_runner._validate_repair_fixture_snapshot(unstable)

    digest_drift = _curriculum_fixture_metadata()
    digest_drift["displacement_curriculum_fixture"]["requested_translation_from_source_metres"] = (
        0.01
    )
    with pytest.raises(RuntimeError, match="cosmos_policy_curriculum_fixture_digest_mismatch"):
        experiment_runner._validate_repair_fixture_snapshot(digest_drift)


def test_curriculum_fixture_admission_rejects_environment_seed_mismatch() -> None:
    mismatch = _curriculum_fixture_metadata()
    mismatch["environment_seed"] = 7
    mismatch["displacement_curriculum_fixture"]["environment_seed"] = 7
    mismatch["displacement_curriculum_fixture_sha256"] = canonical_sha256(
        mismatch["displacement_curriculum_fixture"]
    )

    with pytest.raises(RuntimeError, match="cosmos_policy_curriculum_fixture_contract_mismatch"):
        experiment_runner._validate_repair_fixture_snapshot(mismatch)


def test_curriculum_distance_grid_is_sorted_unique_and_bounded() -> None:
    assert curriculum_probe._validate_distances((0.005, 0.01, 0.02), 0.22) == (
        0.005,
        0.01,
        0.02,
    )
    with pytest.raises(ValueError, match="distances_not_unique_sorted"):
        curriculum_probe._validate_distances((0.01, 0.005), 0.22)


def test_live_curriculum_probe_reuses_admission_and_caps_one_trial(monkeypatch, tmp_path) -> None:
    calls = {"build": 0, "repair": 0}

    monkeypatch.setenv(live_curriculum_probe.OPT_IN_ENV, "1")
    monkeypatch.setattr(
        live_curriculum_probe,
        "_verify_oracle_recoverability_report",
        lambda **kwargs: {"authority": "diagnostic_only"},
    )
    monkeypatch.setattr(
        live_curriculum_probe,
        "_verify_nominal_admission_report",
        lambda path: {"nominal_success_observed": True},
    )

    def fake_build_model_runtime(**kwargs):
        calls["build"] += 1
        return "cfg", "model", "stats", "checkpoint"

    def fake_run_repair(**kwargs):
        calls["repair"] += 1
        assert kwargs["maximum_actions"] == 64
        assert kwargs["process_seed"] == 195
        assert kwargs["repair_instruction_variant"] == "cached_singular_task"
        return {
            "result_sha256": "repair-digest",
            "repair_result": {"applied_action_count": 64},
            "final_goal_predicate_vector": [True, False, True],
            "scripted_fixture_repair_established": False,
            "action_command_statistics": {"sample_count": 64},
        }

    monkeypatch.setattr(live_curriculum_probe, "_build_model_runtime", fake_build_model_runtime)
    monkeypatch.setattr(live_curriculum_probe, "_run_repair", fake_run_repair)
    monkeypatch.setattr(
        live_curriculum_probe,
        "_actual_effect_statistics",
        lambda report: {"sample_count": 64, "gripper_contact_observation_count": 0},
    )

    result = live_curriculum_probe.execute_live(
        source_root=tmp_path / "source",
        checkpoint_path=tmp_path / "checkpoint",
        tokenizer_path=tmp_path / "tokenizer.pth",
        snapshot_path=tmp_path / "fixture.npz",
        oracle_recoverability_report_path=tmp_path / "oracle.json",
        nominal_report_path=tmp_path / "nominal.json",
        output_dir=tmp_path / "output",
        operator_approval_ref="operator:test",
        maximum_actions=64,
        instruction_variant="cached_singular_task",
    )

    assert calls == {"build": 1, "repair": 1}
    assert result["additional_training_performed"] is False
    assert result["instruction_variant"] == "cached_singular_task"
    assert result["instruction_embedding_source"] == "verified_official_checkpoint_cache"
    assert result["fixture_repair_established"] is False
    assert result["claim_boundary"]["general_recovery_rate_established"] is False


def test_live_runner_requires_explicit_opt_in(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(experiment_runner.OPT_IN_ENV, raising=False)
    with pytest.raises(RuntimeError, match="cosmos_policy_libero_live_opt_in_required"):
        experiment_runner.execute_live(
            source_root=tmp_path / "source",
            checkpoint_path=tmp_path / "checkpoint",
            tokenizer_path=tmp_path / "tokenizer.pth",
            snapshot_path=tmp_path / "fixture.npz",
            oracle_recoverability_report_path=tmp_path / "oracle-report.json",
            output_dir=tmp_path / "output",
            dispatch_state_path=tmp_path / "dispatch.json",
            operator_approval_ref="operator:test",
        )


def _write_nominal_admission_report(tmp_path):
    report_without_digest = {
        "schema_version": "missionos.cosmos_policy_libero_nominal.v1",
        "task_suite": experiment_runner.TASK_SUITE,
        "task_name": experiment_runner.TASK_NAME,
        "task_id": experiment_runner.TASK_ID,
        "episode_init_state_index": experiment_runner.EPISODE_INIT_STATE_INDEX,
        "source_revision": experiment_runner.COSMOS_POLICY_SOURCE_REVISION,
        "applied_action_count": 369,
        "nominal_success_observed": True,
    }
    report = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    path = tmp_path / "nominal-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_seed_probe_nominal_admission_is_digest_and_task_bound(tmp_path) -> None:
    admission = seed_probe._verify_nominal_admission_report(
        _write_nominal_admission_report(tmp_path)
    )

    assert admission["nominal_success_observed"] is True
    assert admission["does_not_establish_repair"] is True
    assert admission["applied_action_count"] == 369


def test_seed_probe_requires_four_unique_seeds() -> None:
    with pytest.raises(
        ValueError,
        match="cosmos_policy_seed_probe_requires_four_unique_seeds",
    ):
        seed_probe._validate_seeds((17, 71, 195))
    with pytest.raises(
        ValueError,
        match="cosmos_policy_seed_probe_requires_four_unique_seeds",
    ):
        seed_probe._validate_seeds((17, 71, 195, 195))


def test_seed_probe_loads_model_once_and_keeps_results_diagnostic(monkeypatch, tmp_path) -> None:
    snapshot_path, oracle_path = _write_oracle_report(tmp_path, stable_success=True)
    nominal_path = _write_nominal_admission_report(tmp_path)
    model_loads = []
    repair_seeds = []

    def fake_build_model_runtime(**kwargs):
        model_loads.append(kwargs["process_seed"])
        return "cfg", "model", "stats", {"checkpoint": "fixture"}

    def fake_run_repair(**kwargs):
        seed = kwargs["process_seed"]
        repair_seeds.append(seed)
        witness = {
            "position_metres": [0.1, 0.2, 0.3],
            "end_effector_distance_metres": 0.5,
            "gripper_contact_observed": False,
        }
        return {
            "result_sha256": f"{seed:064x}",
            "repair_result": {
                "applied_action_count": 128,
                "chunk_evidence": [
                    {"preservation_step_trace": [{"object_witnesses": {"moka_pot_2": witness}}]}
                ],
            },
            "final_goal_predicate_vector": [True, False, True],
            "scripted_fixture_repair_established": False,
            "action_command_statistics": {"sample_count": 128},
        }

    monkeypatch.setenv(seed_probe.OPT_IN_ENV, "1")
    monkeypatch.setattr(seed_probe, "_build_model_runtime", fake_build_model_runtime)
    monkeypatch.setattr(seed_probe, "_run_repair", fake_run_repair)

    result = seed_probe.execute_live(
        source_root=tmp_path / "source",
        checkpoint_path=tmp_path / "checkpoint",
        tokenizer_path=tmp_path / "tokenizer.pth",
        snapshot_path=snapshot_path,
        oracle_recoverability_report_path=oracle_path,
        nominal_report_path=nominal_path,
        output_dir=tmp_path / "seed-output",
        operator_approval_ref="operator:seed-probe-test",
    )

    assert model_loads == [17]
    assert repair_seeds == [17, 71, 195, 231]
    assert result["instruction_ablation_performed"] is False
    assert result["claim_boundary"]["authority"] == "diagnostic_only"
    assert result["claim_boundary"]["candidate_selector_used"] is False
    assert result["results"][0]["actual_effect_statistics"] == {
        "schema_version": "missionos.cosmos_policy_actual_effect_statistics.v1",
        "authority": "actual_libero_simulator_observation",
        "sample_count": 1,
        "gripper_contact_observation_count": 0,
        "minimum_end_effector_distance_to_target_metres": 0.5,
        "maximum_target_translation_from_first_observation_metres": 0.0,
        "physical_path_length_established": False,
    }

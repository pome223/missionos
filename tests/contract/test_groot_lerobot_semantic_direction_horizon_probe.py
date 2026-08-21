from __future__ import annotations

import hashlib

import pytest

from missionos_core import canonical_sha256
from src.runtime.groot_lerobot_semantic_direction_horizon_probe import (
    build_semantic_direction_horizon_probe_result,
)


def _trial(
    index: int,
    label: str,
    instruction: str,
    *,
    target: str,
    target_progress: float,
    other_progress: float,
) -> dict:
    instruction_sha256 = hashlib.sha256(instruction.encode()).hexdigest()
    target_initial = 0.4
    other_initial = 0.3
    basis = "a" if label == "A" else "b"
    chunks = [
        {
            "chunk_index": chunk_index,
            "policy_queue_empty_before_forward": True,
            "policy_request_sha256": canonical_sha256(
                {
                    "instruction": instruction,
                    "observation": "same" if chunk_index == 0 else basis,
                    "chunk": chunk_index,
                }
            ),
            "policy_prediction_sha256": canonical_sha256(
                {"instruction": instruction, "prediction": basis, "chunk": chunk_index}
            ),
            "action_chunk_sha256": canonical_sha256(
                {"instruction": instruction, "actions": basis, "chunk": chunk_index}
            ),
            "model_forward_count": 1,
            "actions_applied": 16,
            "target_terminal_distance_metres": (
                target_initial - target_progress * (chunk_index + 1) / 3
            ),
            "non_target_terminal_distance_metres": (
                other_initial - other_progress * (chunk_index + 1) / 3
            ),
            "terminal_goal_predicate_vector": [True, False, True],
            "preservation_violation_observed": False,
        }
        for chunk_index in range(3)
    ]
    return {
        "trial_index": index,
        "label": label,
        "instruction": instruction,
        "instruction_sha256": instruction_sha256,
        "packed_language_exact_match": True,
        "packed_language_sha256": instruction_sha256,
        "target_object_name": target,
        "non_target_object_name": "moka_pot_1" if target == "moka_pot_2" else "moka_pot_2",
        "observation_sha256": "b" * 64,
        "restored_state_sha256": "c" * 64,
        "terminal_state_sha256": canonical_sha256({"terminal": basis, "index": index}),
        "sampling_seed": 1000,
        "chunks": chunks,
        "model_forward_count": 3,
        "actions_applied": 48,
        "simulator_effect_observed": True,
        "initial_end_effector_position_metres": [0.0, 0.0, 0.0],
        "terminal_end_effector_position_metres": [0.03, 0.04, 0.05],
        "target_initial_distance_metres": target_initial,
        "target_terminal_distance_metres": target_initial - target_progress,
        "non_target_initial_distance_metres": other_initial,
        "non_target_terminal_distance_metres": other_initial - other_progress,
        "initial_goal_predicate_vector": [True, False, True],
        "terminal_goal_predicate_vector": [True, False, True],
        "preservation_violation_observed": False,
    }


def _result(*, a0_progress: float = 0.08, a1_progress: float = 0.07) -> dict:
    instruction_a = "put the second moka pot on the stove"
    instruction_b = "put the first moka pot on the stove"
    return build_semantic_direction_horizon_probe_result(
        snapshot_artifact_sha256="a" * 64,
        observation_sha256="b" * 64,
        checkpoint_revision="d" * 40,
        lerobot_revision="e" * 40,
        sampling_seed=1000,
        diagnostic_authorization_ref="diagnostic:test-horizon",
        trials=(
            _trial(
                0,
                "A",
                instruction_a,
                target="moka_pot_2",
                target_progress=a0_progress,
                other_progress=0.01,
            ),
            _trial(
                1,
                "A",
                instruction_a,
                target="moka_pot_2",
                target_progress=a1_progress,
                other_progress=0.01,
            ),
            _trial(
                2,
                "B",
                instruction_b,
                target="moka_pot_1",
                target_progress=0.01,
                other_progress=0.02,
            ),
        ),
    )


def test_horizon_probe_requires_both_a_trajectories_to_align() -> None:
    result = _result()

    assert result["status"] == "local_three_chunk_failed_target_direction_alignment_observed"
    assert result["aa_control"]["initial_policy_request_prediction_and_chunk_reproduced"] is True
    assert result["aa_control"]["later_closed_loop_trajectory_identity_required"] is False
    assert result["model_forward_count"] == 9
    assert result["simulator_action_count"] == 144
    assert result["local_three_chunk_failed_target_direction_alignment_observed"] is True
    assert result["approval_created"] is False
    assert result["dispatch_created"] is False
    assert result["semantic_repair_established"] is False
    assert result["physical_execution_invoked"] is False
    stated = result.pop("result_sha256")
    assert stated == canonical_sha256(result)


def test_horizon_probe_reports_negative_if_either_a_does_not_progress() -> None:
    result = _result(a1_progress=-0.01)

    assert result["status"] == "local_three_chunk_direction_alignment_not_observed"
    assert result["local_three_chunk_failed_target_direction_alignment_observed"] is False


def test_horizon_probe_fails_initial_aa_control_without_requiring_terminal_identity() -> None:
    result = _result()
    trials = result["trials"]
    trials[1]["chunks"][0]["action_chunk_sha256"] = "f" * 64

    rebuilt = build_semantic_direction_horizon_probe_result(
        snapshot_artifact_sha256="a" * 64,
        observation_sha256="b" * 64,
        checkpoint_revision="d" * 40,
        lerobot_revision="e" * 40,
        sampling_seed=1000,
        diagnostic_authorization_ref="diagnostic:test-horizon",
        trials=trials,
    )

    assert rebuilt["status"] == "aa_initial_chunk_control_not_reproduced"


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("model_forward_count", 2, "semantic_direction_horizon_forward_count_invalid"),
        ("actions_applied", 47, "semantic_direction_horizon_action_count_invalid"),
        (
            "simulator_effect_observed",
            False,
            "semantic_direction_horizon_simulator_effect_not_observed",
        ),
        (
            "preservation_violation_observed",
            True,
            "semantic_direction_horizon_preservation_violation",
        ),
        (
            "packed_language_exact_match",
            False,
            "semantic_direction_horizon_packed_language_mismatch",
        ),
    ),
)
def test_horizon_probe_fails_closed_on_invalid_trial(field: str, value: object, error: str) -> None:
    result = _result()
    result["trials"][0][field] = value

    with pytest.raises(ValueError, match=error):
        build_semantic_direction_horizon_probe_result(
            snapshot_artifact_sha256="a" * 64,
            observation_sha256="b" * 64,
            checkpoint_revision="d" * 40,
            lerobot_revision="e" * 40,
            sampling_seed=1000,
            diagnostic_authorization_ref="diagnostic:test-horizon",
            trials=result["trials"],
        )

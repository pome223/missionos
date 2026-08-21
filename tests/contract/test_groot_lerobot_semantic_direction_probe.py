from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from missionos_core import canonical_sha256
from src.runtime.groot_lerobot_semantic_direction_probe import (
    build_semantic_direction_probe_result,
)


def _trial(
    index: int,
    label: str,
    instruction: str,
    *,
    target: str,
    target_progress: float,
    other_progress: float,
    terminal: str,
) -> dict:
    instruction_sha256 = hashlib.sha256(instruction.encode()).hexdigest()
    initial_target = 0.40
    initial_other = 0.30
    return {
        "trial_index": index,
        "label": label,
        "instruction": instruction,
        "instruction_sha256": instruction_sha256,
        "packed_language_exact_match": True,
        "packed_language_sha256": instruction_sha256,
        "target_object_name": target,
        "non_target_object_name": ("moka_pot_1" if target == "moka_pot_2" else "moka_pot_2"),
        "observation_sha256": "b" * 64,
        "restored_state_sha256": "c" * 64,
        "terminal_state_sha256": canonical_sha256({"terminal": terminal}),
        "sampling_seed": 1000,
        "policy_queue_empty_before_forward": True,
        "policy_request_sha256": canonical_sha256(
            {"instruction": instruction, "observation": "same"}
        ),
        "policy_prediction_sha256": canonical_sha256(
            {"instruction": instruction, "prediction": "fixed"}
        ),
        "action_chunk_sha256": canonical_sha256({"instruction": instruction, "chunk": "fixed"}),
        "model_forward_count": 1,
        "actions_applied": 16,
        "simulator_effect_observed": True,
        "initial_end_effector_position_metres": [0.0, 0.0, 0.0],
        "terminal_end_effector_position_metres": [0.01, 0.02, 0.03],
        "target_initial_distance_metres": initial_target,
        "target_terminal_distance_metres": initial_target - target_progress,
        "non_target_initial_distance_metres": initial_other,
        "non_target_terminal_distance_metres": initial_other - other_progress,
        "initial_goal_predicate_vector": [True, False, True],
        "terminal_goal_predicate_vector": [True, False, True],
        "preservation_violation_observed": False,
    }


def _result(*, a_target_progress: float = 0.08, a_other_progress: float = 0.01) -> dict:
    instruction_a = "put the second moka pot on the stove"
    instruction_b = "put the first moka pot on the stove"
    return build_semantic_direction_probe_result(
        snapshot_artifact_sha256="a" * 64,
        observation_sha256="b" * 64,
        checkpoint_revision="d" * 40,
        lerobot_revision="e" * 40,
        sampling_seed=1000,
        diagnostic_authorization_ref="diagnostic:test",
        trials=(
            _trial(
                0,
                "A",
                instruction_a,
                target="moka_pot_2",
                target_progress=a_target_progress,
                other_progress=a_other_progress,
                terminal="a",
            ),
            _trial(
                1,
                "A",
                instruction_a,
                target="moka_pot_2",
                target_progress=a_target_progress,
                other_progress=a_other_progress,
                terminal="a",
            ),
            _trial(
                2,
                "B",
                instruction_b,
                target="moka_pot_1",
                target_progress=0.01,
                other_progress=0.02,
                terminal="b",
            ),
        ),
    )


def test_probe_classifies_only_local_failed_target_direction_alignment() -> None:
    result = _result()

    assert result["status"] == "local_failed_target_direction_alignment_observed"
    assert result["aa_control"]["trajectory_reproduced"] is True
    assert result["local_failed_target_direction_alignment_observed"] is True
    assert result["simulator_action_application_observed"] is True
    assert result["approval_created"] is False
    assert result["dispatch_created"] is False
    assert result["policy_actions_dispatched"] is False
    assert result["instruction_comprehension_established"] is False
    assert result["semantic_repair_established"] is False
    assert result["physical_execution_invoked"] is False
    stated = result.pop("result_sha256")
    assert stated == canonical_sha256(result)


def test_probe_reports_inconclusive_when_failed_target_does_not_progress() -> None:
    result = _result(a_target_progress=-0.01)

    assert result["status"] == "local_direction_alignment_inconclusive"
    assert result["local_failed_target_direction_alignment_observed"] is False


def test_probe_rejects_nonreproducible_aa_trajectory() -> None:
    result = _result()
    trials = result["trials"]
    trials[1]["terminal_state_sha256"] = "f" * 64

    rebuilt = build_semantic_direction_probe_result(
        snapshot_artifact_sha256="a" * 64,
        observation_sha256="b" * 64,
        checkpoint_revision="d" * 40,
        lerobot_revision="e" * 40,
        sampling_seed=1000,
        diagnostic_authorization_ref="diagnostic:test",
        trials=trials,
    )

    assert rebuilt["status"] == "aa_trajectory_control_not_reproduced"
    assert rebuilt["local_failed_target_direction_alignment_observed"] is False


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("actions_applied", 15, "semantic_direction_action_count_invalid"),
        ("model_forward_count", 2, "semantic_direction_model_forward_count_invalid"),
        ("simulator_effect_observed", False, "semantic_direction_simulator_effect_not_observed"),
        ("preservation_violation_observed", True, "semantic_direction_preservation_violation"),
        ("packed_language_exact_match", False, "semantic_direction_packed_language_mismatch"),
    ),
)
def test_probe_fails_closed_on_invalid_trial(field: str, value: object, error: str) -> None:
    result = _result()
    trials = result["trials"]
    trials[0][field] = value

    with pytest.raises(ValueError, match=error):
        build_semantic_direction_probe_result(
            snapshot_artifact_sha256="a" * 64,
            observation_sha256="b" * 64,
            checkpoint_revision="d" * 40,
            lerobot_revision="e" * 40,
            sampling_seed=1000,
            diagnostic_authorization_ref="diagnostic:test",
            trials=trials,
        )


def test_live_publication_records_the_negative_one_chunk_boundary() -> None:
    publication = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs/agents/evidence"
            / "20260821-groot-n17-lerobot-semantic-direction-probe-publication.json"
        ).read_text(encoding="utf-8")
    )

    result = publication["result"]
    assert result["status"] == "aa_trajectory_control_not_reproduced"
    assert result["aa_policy_prediction_reproduced"] is True
    assert result["aa_action_chunk_reproduced"] is True
    assert result["aa_terminal_state_reproduced"] is False
    assert all(value < 0.0 for value in result["failed_target_progress_metres"])
    assert result["local_failed_target_direction_alignment_observed"] is False
    assert result["preservation_violation_observed"] is False
    assert publication["excluded_pre_forward_run"]["measurement_claimed"] is False
    assert publication["probe_configuration"]["model_forward_count"] == 3
    assert publication["probe_configuration"]["simulator_action_count"] == 48
    assert publication["evidence_separation"]["simulator_action_application_observed"] is True
    assert publication["evidence_separation"]["policy_actions_dispatched"] is False
    assert publication["claim_boundary"]["native_repair_cohort_result_changed"] is False

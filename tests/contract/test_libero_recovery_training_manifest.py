from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from src.runtime import libero_recovery_training_manifest as subject
from src.runtime.libero_recovery_training_phase0 import canonical_sha256, validate_transition


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/agents/evidence/20260905-libero-recovery-training-candidate-manifest.json"
HOLDOUT_EVIDENCE = (
    ROOT / "docs/agents/evidence/20260905-libero-recovery-evaluation-holdout-manifest.json"
)


def test_training_manifest_keeps_incomplete_holdouts_blocked(tmp_path, monkeypatch) -> None:
    digest = "a" * 64
    record = {
        "schema_version": "missionos.libero_recovery_transition.v1",
        "transition_id": "transition-1",
        "sample_kind": "recovery_demonstration",
        "split_id": "training",
        "task_suite": "libero_10",
        "task_id": 8,
        "episode_init_state_index": 0,
        "environment_seed": 101,
        "source_simulator_state_sha256": "b" * 64,
        "source_fixture_sha256": "c" * 64,
        "source_goal_predicate_vector": [True, False, True],
        "protected_object_reference_poses_sha256": digest,
        "observation_schema_sha256": digest,
        "proprioception_schema_sha256": digest,
        "corrective_action_sequence_sha256": digest,
        "applied_action_trace_sha256": "d" * 64,
        "predicate_trace_sha256": digest,
        "preservation_trace_sha256": digest,
        "stable_hold_result": "satisfied",
        "corrective_transition_observed": True,
        "privileged_state_used": True,
        "generator_type": "privileged_planner",
        "capture_archive_sha256": "e" * 64,
    }
    arrays = {
        "observation_state": np.zeros((2, 8), dtype=np.float32),
        "applied_action": np.zeros((2, 7), dtype=np.float32),
    }
    monkeypatch.setattr(subject, "build_transition_record", lambda _: (record, arrays))
    conversion = tmp_path / "conversion"
    conversion.mkdir()
    (conversion / "conversion-manifest.json").write_text(
        json.dumps(
            {
                "status": "lerobot_v2_conversion_complete_admission_pending",
                "source_archive_sha256": "e" * 64,
            }
        )
    )
    result = subject.build_training_manifest(
        candidate_dirs=[tmp_path / "candidate"],
        conversion_dirs=[conversion],
        phase0_record={
            "evaluation_holdouts": [{"source_fixture_sha256": "f" * 64, "training_excluded": True}]
        },
    )
    assert result["training_candidate_count"] == 1
    assert result["training_frame_count"] == 2
    assert result["leakage_audit"]["known_identifier_audit"]["status"] == "passed"
    assert result["leakage_audit"]["exact_holdout_materialization_complete"] is False
    assert result["leakage_audit"]["passed"] is False
    assert result["admission"]["training_examples_admitted"] == 0
    assert result["claim_boundary"]["gpu_provision_authorized"] is False


def test_training_manifest_admits_candidates_after_exact_holdout_audit(
    tmp_path, monkeypatch
) -> None:
    digest = "a" * 64
    record = {
        "schema_version": "missionos.libero_recovery_transition.v1",
        "transition_id": "transition-1",
        "sample_kind": "recovery_demonstration",
        "split_id": "training",
        "task_suite": "libero_10",
        "task_id": 8,
        "episode_init_state_index": 0,
        "environment_seed": 101,
        "source_simulator_state_sha256": "b" * 64,
        "source_fixture_sha256": "c" * 64,
        "source_goal_predicate_vector": [True, False, True],
        "protected_object_reference_poses_sha256": digest,
        "observation_schema_sha256": digest,
        "proprioception_schema_sha256": digest,
        "corrective_action_sequence_sha256": digest,
        "applied_action_trace_sha256": "d" * 64,
        "predicate_trace_sha256": digest,
        "preservation_trace_sha256": digest,
        "stable_hold_result": "satisfied",
        "corrective_transition_observed": True,
        "privileged_state_used": True,
        "generator_type": "privileged_planner",
        "capture_archive_sha256": "e" * 64,
    }
    arrays = {
        "observation_state": np.zeros((2, 8), dtype=np.float32),
        "applied_action": np.zeros((2, 7), dtype=np.float32),
    }
    monkeypatch.setattr(subject, "build_transition_record", lambda _: (record, arrays))
    conversion = tmp_path / "conversion"
    conversion.mkdir()
    (conversion / "conversion-manifest.json").write_text(
        json.dumps(
            {
                "status": "lerobot_v2_conversion_complete_admission_pending",
                "source_archive_sha256": "e" * 64,
            }
        )
    )
    holdout = {
        "split_id": "evaluation",
        "source_simulator_state_sha256": "f" * 64,
        "source_fixture_sha256": "1" * 64,
        "action_trace_sha256": "2" * 64,
        "training_excluded": True,
        "used_for_training": False,
    }
    holdout_manifest = {
        "schema_version": subject.HOLDOUT_MANIFEST_SCHEMA_VERSION,
        "evaluation_holdout_count": 1,
        "evaluation_holdouts": [holdout],
    }
    holdout_manifest["result_sha256"] = canonical_sha256(holdout_manifest)
    result = subject.build_training_manifest(
        candidate_dirs=[tmp_path / "candidate"],
        conversion_dirs=[conversion],
        phase0_record={"evaluation_holdouts": []},
        evaluation_holdout_manifest=holdout_manifest,
    )
    assert result["leakage_audit"]["passed"] is True
    assert result["admission"]["status"] == "admitted"
    assert result["admission"]["training_examples_admitted"] == 1
    assert result["claim_boundary"]["paid_training_authorized"] is False


def test_checked_in_candidate_manifest_is_valid_and_admitted_for_future_training() -> None:
    record = json.loads(EVIDENCE.read_text())
    material = {key: value for key, value in record.items() if key != "result_sha256"}
    assert record["result_sha256"] == canonical_sha256(material)
    assert record["training_candidate_count"] == 4
    assert record["training_frame_count"] == 206
    assert all(not validate_transition(item) for item in record["transition_records"])
    assert (
        len({item["source_simulator_state_sha256"] for item in record["transition_records"]}) == 4
    )
    assert record["leakage_audit"]["known_identifier_audit"]["status"] == "passed"
    assert record["leakage_audit"]["passed"] is True
    assert record["admission"]["training_examples_admitted"] == 4
    assert record["claim_boundary"]["paid_training_authorized"] is False


def test_checked_in_exact_holdouts_remain_evaluation_only() -> None:
    record = json.loads(HOLDOUT_EVIDENCE.read_text())
    material = {key: value for key, value in record.items() if key != "result_sha256"}
    assert record["result_sha256"] == canonical_sha256(material)
    assert record["evaluation_holdout_count"] == 3
    assert len({item["source_simulator_state_sha256"] for item in record["evaluation_holdouts"]}) == 3
    assert all(item["split_id"] == "evaluation" for item in record["evaluation_holdouts"])
    assert all(item["training_excluded"] is True for item in record["evaluation_holdouts"])
    assert all(item["used_for_training"] is False for item in record["evaluation_holdouts"])
    assert record["claim_boundary"]["evaluation_data_used_for_training"] is False

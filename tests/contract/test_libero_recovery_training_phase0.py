from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from src.runtime.libero_recovery_training_phase0 import (
    TRANSITION_SCHEMA_VERSION,
    audit_training_leakage,
    canonical_sha256,
    phase0_summary,
    validate_transition,
)


ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs/agents/evidence/20260905-libero-recovery-training-phase0.json"


def _transition(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": TRANSITION_SCHEMA_VERSION,
        "transition_id": "transition:train:1",
        "sample_kind": "recovery_demonstration",
        "split_id": "training",
        "task_suite": "libero_10",
        "task_id": 8,
        "episode_init_state_index": 1,
        "environment_seed": 101,
        "source_simulator_state_sha256": "1" * 64,
        "source_fixture_sha256": "2" * 64,
        "source_goal_predicate_vector": [True, False, True],
        "protected_object_reference_poses_sha256": "3" * 64,
        "observation_schema_sha256": "4" * 64,
        "proprioception_schema_sha256": "5" * 64,
        "corrective_action_sequence_sha256": "6" * 64,
        "applied_action_trace_sha256": "7" * 64,
        "predicate_trace_sha256": "8" * 64,
        "preservation_trace_sha256": "9" * 64,
        "stable_hold_result": "satisfied",
        "corrective_transition_observed": True,
        "privileged_state_used": True,
        "generator_type": "privileged_planner",
    }
    value.update(overrides)
    return value


def test_checked_in_phase0_record_is_digest_bound_and_no_go() -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    summary = phase0_summary(record, repository_root=ROOT)
    assert summary == {
        "status": "valid",
        "decision": "NO_GO",
        "paid_training_authorized": False,
        "gpu_provision_authorized": False,
        "training_performed": False,
        "errors": [],
    }
    body = {key: value for key, value in record.items() if key != "result_sha256"}
    assert record["result_sha256"] == canonical_sha256(body)


def test_recovery_demonstration_requires_corrective_transition_and_stable_hold() -> None:
    invalid = _transition(
        corrective_transition_observed=False,
        stable_hold_result="not_satisfied",
    )
    errors = validate_transition(invalid)
    assert "recovery_demonstration_requires_corrective_transition" in errors
    assert "recovery_demonstration_requires_stable_hold" in errors


def test_leakage_audit_rejects_evaluation_state_and_fixture() -> None:
    holdout = {
        "source_simulator_state_sha256": "1" * 64,
        "source_fixture_sha256": "2" * 64,
        "action_trace_sha256": "7" * 64,
    }
    audit = audit_training_leakage([_transition()], [holdout])
    assert audit["status"] == "failed"
    assert "record[0]:evaluation_state_leakage" in audit["errors"]
    assert "record[0]:evaluation_fixture_leakage" in audit["errors"]
    assert "record[0]:evaluation_action_trace_leakage" in audit["errors"]


def test_go_requires_every_prerequisite_without_authorizing_paid_training() -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    record["go_no_go"] = {
        "decision": "GO",
        "paid_training_authorized": False,
        "prerequisites": {"dataset_manifest_admitted": False},
        "blocking_reasons": [],
    }
    body = {key: value for key, value in record.items() if key != "result_sha256"}
    record["result_sha256"] = canonical_sha256(body)
    assert "phase0_go_requires_all_prerequisites" in phase0_summary(record)["errors"]


def test_tampered_phase0_record_is_rejected() -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    tampered = deepcopy(record)
    tampered["evaluation"]["required_hold_steps"] = 1
    errors = phase0_summary(tampered)["errors"]
    assert "phase0_result_digest_mismatch" in errors
    assert "phase0_twenty_step_hold_required" in errors


def test_evidence_file_digest_is_verified() -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    record["evaluation_holdouts"][0]["evidence_file_sha256"] = "0" * 64
    body = {key: value for key, value in record.items() if key != "result_sha256"}
    record["result_sha256"] = canonical_sha256(body)
    errors = phase0_summary(record, repository_root=ROOT)["errors"]
    assert "holdout[0]:evidence_file_digest_mismatch" in errors


def test_preregistered_training_and_evaluation_partitions_must_not_overlap() -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    pilot = record["matched_experiment"]["preregistered_pilot"]
    pilot["training_episode_init_state_indices"] = [0, 15]
    pilot["training_environment_seeds"] = [0, 101]
    body = {key: value for key, value in record.items() if key != "result_sha256"}
    record["result_sha256"] = canonical_sha256(body)
    errors = phase0_summary(record)["errors"]
    assert "phase0_training_evaluation_fixture_partition_invalid" in errors
    assert "phase0_training_evaluation_seed_partition_invalid" in errors


def test_preregistered_action_budget_includes_verifier_hold() -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    pilot = record["matched_experiment"]["preregistered_pilot"]
    assert pilot["maximum_policy_actions"] == 128
    assert pilot["required_stable_hold_actions"] == 20
    assert pilot["maximum_total_actions_including_hold"] == 148

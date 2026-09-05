"""Build a bounded recovery-training candidate manifest and leakage audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from src.runtime.libero_recovery_training_phase0 import (
    TRANSITION_SCHEMA_VERSION,
    audit_training_leakage,
    canonical_sha256,
    validate_transition,
)
from src.runtime.libero_recovery_lerobot_v2 import validate_capture


MANIFEST_SCHEMA_VERSION = "missionos.libero_recovery_training_manifest.v1"
HOLDOUT_MANIFEST_SCHEMA_VERSION = "missionos.libero_recovery_evaluation_holdout_manifest.v1"
PREREGISTERED_EVALUATION_HOLDOUTS = {(4, 0), (12, 0), (15, 0)}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"libero_recovery_manifest_json_object_required:{path.name}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def build_transition_record(candidate_dir: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    fixture = _read_json(candidate_dir / "fixture/result.json")
    oracle = _read_json(candidate_dir / "oracle/result.json")
    capture, arrays = validate_capture(candidate_dir / "oracle/transition-capture")
    trace = json.loads((candidate_dir / "oracle/raw-7d-actions.json").read_text(encoding="utf-8"))
    if not isinstance(trace, list):
        raise ValueError("libero_recovery_manifest_trace_array_required")
    identity = {
        "task_suite": capture["source"]["task_suite"],
        "task_id": capture["source"]["task_id"],
        "episode_init_state_index": capture["source"]["episode_init_state_index"],
        "environment_seed": capture["source"]["environment_seed"],
        "source_simulator_state_sha256": fixture["snapshot"]["simulator_state_sha256"],
    }
    protected_reference = {
        "protected_object": "moka_pot_1",
        "source_position_metres": fixture["fixture"]["source_protected_position_metres"],
    }
    observation_contract = {
        "videos": capture["official_libero_contract"]["videos"],
        "alignment": capture["alignment"],
        "control_frequency_hz": capture["control_frequency_hz"],
    }
    proprioception_contract = {
        "fields": capture["official_libero_contract"]["observation_state"],
        "dtype": capture["arrays"]["observation_state"]["dtype"],
        "width": capture["arrays"]["observation_state"]["shape"][1],
    }
    stable_success = oracle["stable_success_observed"] is True
    record = {
        "schema_version": TRANSITION_SCHEMA_VERSION,
        "transition_id": canonical_sha256(identity),
        "sample_kind": "recovery_demonstration" if stable_success else "failure_rollout",
        "split_id": "training",
        **identity,
        "source_fixture_sha256": fixture["fixture_sha256"],
        "source_goal_predicate_vector": oracle["source_goal_predicate_vector"],
        "protected_object_reference_poses_sha256": canonical_sha256(protected_reference),
        "observation_schema_sha256": canonical_sha256(observation_contract),
        "proprioception_schema_sha256": canonical_sha256(proprioception_contract),
        "corrective_action_sequence_sha256": canonical_sha256(
            {"actions": [item["action_7d"] for item in trace]}
        ),
        "applied_action_trace_sha256": capture["arrays"]["applied_action"]["sha256"],
        "predicate_trace_sha256": canonical_sha256(
            {"predicate_vectors": [item["predicate_vector"] for item in trace]}
        ),
        "preservation_trace_sha256": canonical_sha256(
            {
                "protected_displacement_metres": [
                    item["protected_displacement_metres"] for item in trace
                ],
                "violation_observed": oracle["preservation_violation_observed"],
            }
        ),
        "preservation_violation_observed": oracle["preservation_violation_observed"],
        "stable_hold_result": "satisfied" if stable_success else "not_satisfied",
        "corrective_transition_observed": stable_success,
        "privileged_state_used": True,
        "generator_type": "privileged_planner",
        "frame_count": capture["frame_count"],
        "capture_archive_sha256": capture["archive"]["sha256"],
    }
    errors = validate_transition(record)
    if errors:
        raise ValueError("libero_recovery_manifest_transition_invalid:" + ",".join(errors))
    return record, arrays


def build_evaluation_holdout_manifest(candidate_dirs: Sequence[Path]) -> dict[str, Any]:
    if not candidate_dirs:
        raise ValueError("libero_recovery_holdout_candidates_required")
    holdouts: list[dict[str, Any]] = []
    seen_states: set[str] = set()
    for candidate_dir in candidate_dirs:
        record, _ = build_transition_record(candidate_dir)
        state_digest = record["source_simulator_state_sha256"]
        if state_digest in seen_states:
            raise ValueError("libero_recovery_holdout_duplicate_source_state")
        seen_states.add(state_digest)
        holdouts.append(
            {
                "split_id": "evaluation",
                "task_suite": record["task_suite"],
                "task_id": record["task_id"],
                "episode_init_state_index": record["episode_init_state_index"],
                "environment_seed": record["environment_seed"],
                "source_simulator_state_sha256": state_digest,
                "source_fixture_sha256": record["source_fixture_sha256"],
                "action_trace_sha256": record["applied_action_trace_sha256"],
                "stable_success_observed": record["corrective_transition_observed"],
                "stable_hold_result": record["stable_hold_result"],
                "preservation_violation_observed": record[
                    "preservation_violation_observed"
                ],
                "training_excluded": True,
                "used_for_training": False,
                "generator_type": record["generator_type"],
                "privileged_state_used": record["privileged_state_used"],
            }
        )
    observed_candidates = {
        (item["episode_init_state_index"], item["environment_seed"]) for item in holdouts
    }
    if observed_candidates != PREREGISTERED_EVALUATION_HOLDOUTS:
        raise ValueError("libero_recovery_holdout_preregistered_cohort_required")
    result_without_digest = {
        "schema_version": HOLDOUT_MANIFEST_SCHEMA_VERSION,
        "status": "complete_training_excluded",
        "evaluation_holdout_count": len(holdouts),
        "evaluation_holdouts": holdouts,
        "claim_boundary": {
            "evaluation_data_used_for_training": False,
            "training_invoked": False,
            "model_inference_invoked": False,
            "physical_execution_invoked": False,
            "privileged_oracle_generation": True,
        },
    }
    return {
        **result_without_digest,
        "result_sha256": canonical_sha256(result_without_digest),
    }


def build_training_manifest(
    *,
    candidate_dirs: Sequence[Path],
    conversion_dirs: Sequence[Path],
    phase0_record: Mapping[str, Any],
    evaluation_holdout_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not candidate_dirs or len(candidate_dirs) != len(conversion_dirs):
        raise ValueError("libero_recovery_manifest_candidate_conversion_count_mismatch")
    records: list[dict[str, Any]] = []
    states: list[np.ndarray] = []
    actions: list[np.ndarray] = []
    conversion_bytes = 0
    conversion_digests: list[str] = []
    for candidate_dir, conversion_dir in zip(candidate_dirs, conversion_dirs, strict=True):
        record, arrays = build_transition_record(candidate_dir)
        conversion = _read_json(conversion_dir / "conversion-manifest.json")
        if conversion.get("source_archive_sha256") != record["capture_archive_sha256"]:
            raise ValueError("libero_recovery_manifest_conversion_source_mismatch")
        if conversion.get("status") != "lerobot_v2_conversion_complete_admission_pending":
            raise ValueError("libero_recovery_manifest_conversion_status_invalid")
        records.append(record)
        states.append(arrays["observation_state"])
        actions.append(arrays["applied_action"])
        files = [path for path in conversion_dir.rglob("*") if path.is_file()]
        conversion_bytes += sum(path.stat().st_size for path in files)
        conversion_digests.append(_sha256(conversion_dir / "conversion-manifest.json"))

    legacy_holdouts = phase0_record.get("evaluation_holdouts")
    if not isinstance(legacy_holdouts, list):
        raise ValueError("libero_recovery_manifest_holdouts_required")
    if evaluation_holdout_manifest is None:
        exact_holdouts = legacy_holdouts
        audited_holdouts = legacy_holdouts
    else:
        if evaluation_holdout_manifest.get("schema_version") != HOLDOUT_MANIFEST_SCHEMA_VERSION:
            raise ValueError("libero_recovery_holdout_manifest_schema_mismatch")
        holdout_material = {
            key: value
            for key, value in evaluation_holdout_manifest.items()
            if key != "result_sha256"
        }
        if evaluation_holdout_manifest.get("result_sha256") != canonical_sha256(
            holdout_material
        ):
            raise ValueError("libero_recovery_holdout_manifest_digest_mismatch")
        exact_holdouts = evaluation_holdout_manifest.get("evaluation_holdouts")
        if not isinstance(exact_holdouts, list) or not exact_holdouts:
            raise ValueError("libero_recovery_exact_holdouts_required")
        if evaluation_holdout_manifest.get("evaluation_holdout_count") != len(
            exact_holdouts
        ):
            raise ValueError("libero_recovery_holdout_manifest_count_mismatch")
        if any(
            item.get("training_excluded") is not True
            or item.get("used_for_training") is not False
            or item.get("split_id") != "evaluation"
            for item in exact_holdouts
        ):
            raise ValueError("libero_recovery_holdout_training_exclusion_required")
        audited_holdouts = [*legacy_holdouts, *exact_holdouts]
    known_audit = audit_training_leakage(records, audited_holdouts)
    materialized_holdouts = sum(
        all(
            _is_sha256(item.get(key))
            for key in (
                "source_simulator_state_sha256",
                "source_fixture_sha256",
                "action_trace_sha256",
            )
        )
        for item in exact_holdouts
    )
    state_array = np.concatenate(states, axis=0)
    action_array = np.concatenate(actions, axis=0)
    stats = {
        "observation_state": {
            "mean": state_array.mean(axis=0).tolist(),
            "std": state_array.std(axis=0).tolist(),
            "min": state_array.min(axis=0).tolist(),
            "max": state_array.max(axis=0).tolist(),
        },
        "action": {
            "mean": action_array.mean(axis=0).tolist(),
            "std": action_array.std(axis=0).tolist(),
            "min": action_array.min(axis=0).tolist(),
            "max": action_array.max(axis=0).tolist(),
        },
    }
    holdouts_complete = materialized_holdouts == len(exact_holdouts)
    blocking_reasons = []
    if known_audit["status"] != "passed":
        blocking_reasons.append("known_identifier_leakage")
    if not holdouts_complete:
        blocking_reasons.append("evaluation_holdout_materialization_incomplete")
    result_without_digest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": (
            "admitted"
            if not blocking_reasons
            else (
                "blocked_known_identifier_leakage"
                if known_audit["status"] != "passed"
                else "blocked_evaluation_holdout_materialization"
            )
        ),
        "training_candidate_count": len(records),
        "training_frame_count": int(state_array.shape[0]),
        "serialized_conversion_bytes": conversion_bytes,
        "transition_records": records,
        "conversion_manifest_file_sha256": conversion_digests,
        "statistics": stats,
        "leakage_audit": {
            "known_identifier_audit": known_audit,
            "legacy_holdout_reference_count": len(legacy_holdouts),
            "exact_evaluation_holdout_count": len(exact_holdouts),
            "fully_materialized_holdout_count": materialized_holdouts,
            "exact_holdout_materialization_complete": holdouts_complete,
            "passed": known_audit["status"] == "passed" and holdouts_complete,
        },
        "admission": {
            "status": "admitted" if not blocking_reasons else "blocked",
            "blocking_reasons": blocking_reasons,
            "training_examples_admitted": len(records) if not blocking_reasons else 0,
        },
        "claim_boundary": {
            "candidate_contracts_valid": True,
            "paid_training_authorized": False,
            "gpu_provision_authorized": False,
            "training_invoked": False,
            "model_inference_invoked": False,
            "physical_execution_invoked": False,
        },
    }
    return {
        **result_without_digest,
        "result_sha256": canonical_sha256(result_without_digest),
    }

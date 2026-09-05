"""Validate the LIBERO recovery-state training Phase 0 contract.

This module makes a readiness decision only.  It does not authorize paid
compute, provision a GPU, start training, or promote simulator evidence into a
capability or physical-execution claim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re
from typing import Any


PHASE0_SCHEMA_VERSION = "missionos.libero_recovery_training_phase0.v1"
TRANSITION_SCHEMA_VERSION = "missionos.libero_recovery_transition.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_TRANSITION_FIELDS = (
    "transition_id",
    "sample_kind",
    "split_id",
    "task_suite",
    "task_id",
    "episode_init_state_index",
    "environment_seed",
    "source_simulator_state_sha256",
    "source_fixture_sha256",
    "source_goal_predicate_vector",
    "protected_object_reference_poses_sha256",
    "observation_schema_sha256",
    "proprioception_schema_sha256",
    "corrective_action_sequence_sha256",
    "applied_action_trace_sha256",
    "predicate_trace_sha256",
    "preservation_trace_sha256",
    "stable_hold_result",
    "corrective_transition_observed",
    "privileged_state_used",
    "generator_type",
)


def canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def validate_transition(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if record.get("schema_version") != TRANSITION_SCHEMA_VERSION:
        errors.append("transition_schema_version_mismatch")
    missing = [field for field in REQUIRED_TRANSITION_FIELDS if field not in record]
    if missing:
        errors.append("transition_required_fields_missing:" + ",".join(missing))

    digest_fields = [field for field in REQUIRED_TRANSITION_FIELDS if field.endswith("_sha256")]
    for field in digest_fields:
        if field in record and not _is_sha256(record[field]):
            errors.append(f"transition_digest_invalid:{field}")

    predicates = record.get("source_goal_predicate_vector")
    if (
        not isinstance(predicates, list)
        or not predicates
        or not all(isinstance(value, bool) for value in predicates)
    ):
        errors.append("transition_source_predicates_invalid")

    sample_kind = record.get("sample_kind")
    if sample_kind not in {"recovery_demonstration", "failure_rollout"}:
        errors.append("transition_sample_kind_invalid")
    stable_hold = record.get("stable_hold_result")
    if stable_hold not in {"satisfied", "not_satisfied", "not_observed"}:
        errors.append("transition_stable_hold_result_invalid")

    if sample_kind == "recovery_demonstration":
        if record.get("corrective_transition_observed") is not True:
            errors.append("recovery_demonstration_requires_corrective_transition")
        if stable_hold != "satisfied":
            errors.append("recovery_demonstration_requires_stable_hold")
    if sample_kind == "failure_rollout" and record.get("corrective_transition_observed") is True:
        errors.append("failure_rollout_cannot_claim_corrective_transition")

    privileged = record.get("privileged_state_used")
    if not isinstance(privileged, bool):
        errors.append("transition_privileged_state_flag_required")
    generator = record.get("generator_type")
    if generator not in {
        "privileged_planner",
        "human_teleoperation",
        "learned_policy",
        "scripted_non_privileged",
    }:
        errors.append("transition_generator_type_invalid")
    if generator == "privileged_planner" and privileged is not True:
        errors.append("privileged_planner_must_disclose_privileged_state")
    return errors


def audit_training_leakage(
    training_records: Sequence[Mapping[str, Any]],
    evaluation_holdouts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    errors: list[str] = []
    if any(not isinstance(item, Mapping) for item in evaluation_holdouts):
        return {
            "status": "failed",
            "training_record_count": len(training_records),
            "evaluation_holdout_count": len(evaluation_holdouts),
            "errors": ["evaluation_holdout_object_required"],
        }
    valid_holdouts = [item for item in evaluation_holdouts if isinstance(item, Mapping)]
    seen_transition_ids: set[str] = set()
    seen_states: set[str] = set()
    holdout_states = {
        item.get("source_simulator_state_sha256")
        for item in valid_holdouts
        if _is_sha256(item.get("source_simulator_state_sha256"))
    }
    holdout_fixtures = {
        item.get("source_fixture_sha256")
        for item in valid_holdouts
        if _is_sha256(item.get("source_fixture_sha256"))
    }
    holdout_actions = {
        item.get("action_trace_sha256")
        for item in valid_holdouts
        if _is_sha256(item.get("action_trace_sha256"))
    }

    for index, record in enumerate(training_records):
        errors.extend(f"record[{index}]:{error}" for error in validate_transition(record))
        transition_id = record.get("transition_id")
        if isinstance(transition_id, str):
            if transition_id in seen_transition_ids:
                errors.append(f"record[{index}]:duplicate_transition_id")
            seen_transition_ids.add(transition_id)
        state_digest = record.get("source_simulator_state_sha256")
        if isinstance(state_digest, str):
            if state_digest in seen_states:
                errors.append(f"record[{index}]:duplicate_source_state")
            seen_states.add(state_digest)
            if state_digest in holdout_states:
                errors.append(f"record[{index}]:evaluation_state_leakage")
        if record.get("source_fixture_sha256") in holdout_fixtures:
            errors.append(f"record[{index}]:evaluation_fixture_leakage")
        if record.get("applied_action_trace_sha256") in holdout_actions:
            errors.append(f"record[{index}]:evaluation_action_trace_leakage")
        if record.get("split_id") == "evaluation":
            errors.append(f"record[{index}]:evaluation_split_in_training")

    return {
        "status": "passed" if not errors else "failed",
        "training_record_count": len(training_records),
        "evaluation_holdout_count": len(evaluation_holdouts),
        "errors": errors,
    }


def validate_phase0_record(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if record.get("schema_version") != PHASE0_SCHEMA_VERSION:
        errors.append("phase0_schema_version_mismatch")

    without_digest = {key: value for key, value in record.items() if key != "result_sha256"}
    if record.get("result_sha256") != canonical_sha256(without_digest):
        errors.append("phase0_result_digest_mismatch")

    contract = record.get("dataset_contract")
    if not isinstance(contract, Mapping):
        errors.append("phase0_dataset_contract_required")
    else:
        if contract.get("schema_version") != TRANSITION_SCHEMA_VERSION:
            errors.append("phase0_transition_schema_mismatch")
        if tuple(contract.get("required_fields", ())) != REQUIRED_TRANSITION_FIELDS:
            errors.append("phase0_required_transition_fields_mismatch")

    holdouts = record.get("evaluation_holdouts")
    if not isinstance(holdouts, list) or not holdouts:
        errors.append("phase0_evaluation_holdouts_required")
    elif any(
        not isinstance(item, Mapping) or item.get("training_excluded") is not True
        for item in holdouts
    ):
        errors.append("phase0_holdouts_must_be_training_excluded")

    experiment = record.get("matched_experiment")
    cells = experiment.get("cells") if isinstance(experiment, Mapping) else None
    cell_ids = (
        {cell.get("cell_id") for cell in cells}
        if isinstance(cells, list) and all(isinstance(cell, Mapping) for cell in cells)
        else set()
    )
    if cell_ids != {"nominal_only", "nominal_plus_recovery"}:
        errors.append("phase0_matched_cells_required")
    matched_fields = experiment.get("matched_fields") if isinstance(experiment, Mapping) else None
    required_matches = {"model", "optimizer", "compute", "seeds", "observations", "actions"}
    if not isinstance(matched_fields, list) or not required_matches.issubset(matched_fields):
        errors.append("phase0_matched_fields_incomplete")

    evaluation = record.get("evaluation")
    if not isinstance(evaluation, Mapping):
        errors.append("phase0_evaluation_required")
    else:
        if evaluation.get("required_hold_steps") != 20:
            errors.append("phase0_twenty_step_hold_required")
        if evaluation.get("success_requires_all_five_axes") is not True:
            errors.append("phase0_five_axis_success_required")

    decision = record.get("go_no_go")
    if not isinstance(decision, Mapping) or decision.get("decision") not in {"GO", "NO_GO"}:
        errors.append("phase0_go_no_go_decision_required")
    elif decision.get("decision") == "GO":
        prerequisites = decision.get("prerequisites")
        if not isinstance(prerequisites, Mapping) or not prerequisites or not all(
            value is True for value in prerequisites.values()
        ):
            errors.append("phase0_go_requires_all_prerequisites")
        if decision.get("paid_training_authorized") is not False:
            errors.append("phase0_readiness_must_not_authorize_paid_training")
    elif decision.get("paid_training_authorized") is not False:
        errors.append("phase0_no_go_cannot_authorize_paid_training")

    boundaries = record.get("claim_boundary")
    required_false = {
        "training_performed",
        "model_inference_performed",
        "repair_capability_established",
        "physical_execution_invoked",
    }
    if not isinstance(boundaries, Mapping) or any(
        boundaries.get(field) is not False for field in required_false
    ):
        errors.append("phase0_claim_boundary_invalid")
    return errors


def validate_evidence_files(record: Mapping[str, Any], repository_root: Path) -> list[str]:
    errors: list[str] = []
    root = repository_root.resolve()
    holdouts = record.get("evaluation_holdouts")
    if not isinstance(holdouts, list):
        return ["phase0_evaluation_holdouts_required"]
    for index, holdout in enumerate(holdouts):
        if not isinstance(holdout, Mapping):
            errors.append(f"holdout[{index}]:object_required")
            continue
        reference = holdout.get("evidence_ref")
        expected = holdout.get("evidence_file_sha256")
        if not isinstance(reference, str) or not _is_sha256(expected):
            errors.append(f"holdout[{index}]:reference_or_digest_invalid")
            continue
        candidate = (root / reference).resolve()
        if not candidate.is_relative_to(root):
            errors.append(f"holdout[{index}]:reference_outside_repository")
            continue
        try:
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            errors.append(f"holdout[{index}]:evidence_unreadable")
            continue
        if actual != expected:
            errors.append(f"holdout[{index}]:evidence_file_digest_mismatch")
    return errors


def phase0_summary(
    record: Mapping[str, Any], *, repository_root: Path | None = None
) -> dict[str, Any]:
    errors = validate_phase0_record(record)
    if repository_root is not None:
        errors.extend(validate_evidence_files(record, repository_root))
    decision = record.get("go_no_go", {})
    return {
        "status": "valid" if not errors else "invalid",
        "decision": decision.get("decision"),
        "paid_training_authorized": decision.get("paid_training_authorized", False),
        "gpu_provision_authorized": False,
        "training_performed": False,
        "errors": errors,
    }

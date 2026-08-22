"""Fail-closed evidence for a preregistered three-chunk direction probe.

The first A/A policy request, prediction, and action chunk form the exact
causal control.  After that first chunk each restored clone is evaluated as an
independent closed-loop trajectory; terminal simulator states are deliberately
not required to be bit-identical.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import math
import re
from typing import Any

from missionos_core import canonical_sha256


SCHEMA_VERSION = "missionos.groot_lerobot_semantic_direction_horizon_probe.v1"
AUTHORITY = "diagnostic_clone_only"
CHUNK_COUNT = 3
ACTIONS_PER_CHUNK = 16
_EXPECTED_SEQUENCE = ("A", "A", "B")
_OBJECTS = ("moka_pot_1", "moka_pot_2")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")


def _sha(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field}_invalid")
    return value


def _revision(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise ValueError(f"{field}_invalid")
    return value


def _number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}_invalid")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field}_invalid")
    return result


def _vector3(value: Any, *, field: str) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 3:
        raise ValueError(f"{field}_invalid")
    return [_number(item, field=field) for item in value]


def _predicate_vector(value: Any, *, field: str) -> list[bool]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 3
        or any(not isinstance(item, bool) for item in value)
    ):
        raise ValueError(f"{field}_invalid")
    return list(value)


def _chunk(value: Any, *, chunk_index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("semantic_direction_horizon_chunk_not_mapping")
    material = {
        "chunk_index": value.get("chunk_index"),
        "policy_queue_empty_before_forward": value.get("policy_queue_empty_before_forward"),
        "policy_request_sha256": _sha(
            value.get("policy_request_sha256"), field="semantic_direction_horizon_request_sha256"
        ),
        "policy_prediction_sha256": _sha(
            value.get("policy_prediction_sha256"),
            field="semantic_direction_horizon_prediction_sha256",
        ),
        "action_chunk_sha256": _sha(
            value.get("action_chunk_sha256"),
            field="semantic_direction_horizon_action_chunk_sha256",
        ),
        "model_forward_count": value.get("model_forward_count"),
        "actions_applied": value.get("actions_applied"),
        "target_terminal_distance_metres": _number(
            value.get("target_terminal_distance_metres"),
            field="semantic_direction_horizon_chunk_target_distance",
        ),
        "non_target_terminal_distance_metres": _number(
            value.get("non_target_terminal_distance_metres"),
            field="semantic_direction_horizon_chunk_non_target_distance",
        ),
        "terminal_goal_predicate_vector": _predicate_vector(
            value.get("terminal_goal_predicate_vector"),
            field="semantic_direction_horizon_chunk_predicates",
        ),
        "preservation_violation_observed": value.get("preservation_violation_observed"),
    }
    if material["chunk_index"] != chunk_index:
        raise ValueError("semantic_direction_horizon_chunk_sequence_mismatch")
    if material["policy_queue_empty_before_forward"] is not True:
        raise ValueError("semantic_direction_horizon_queue_boundary_unverified")
    if material["model_forward_count"] != 1:
        raise ValueError("semantic_direction_horizon_chunk_forward_count_invalid")
    if material["actions_applied"] != ACTIONS_PER_CHUNK:
        raise ValueError("semantic_direction_horizon_chunk_action_count_invalid")
    if material["preservation_violation_observed"] is not False:
        raise ValueError("semantic_direction_horizon_preservation_violation")
    return material


def _trial(
    value: Any,
    *,
    index: int,
    label: str,
    observation_sha256: str,
    sampling_seed: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("semantic_direction_horizon_trial_not_mapping")
    if value.get("trial_index") != index or value.get("label") != label:
        raise ValueError("semantic_direction_horizon_trial_sequence_mismatch")
    instruction = value.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("semantic_direction_horizon_instruction_invalid")
    instruction_sha256 = _sha(
        value.get("instruction_sha256"), field="semantic_direction_horizon_instruction_sha256"
    )
    if instruction_sha256 != hashlib.sha256(instruction.encode()).hexdigest():
        raise ValueError("semantic_direction_horizon_instruction_digest_mismatch")
    if value.get("packed_language_exact_match") is not True:
        raise ValueError("semantic_direction_horizon_packed_language_mismatch")
    if (
        _sha(
            value.get("packed_language_sha256"),
            field="semantic_direction_horizon_packed_language_sha256",
        )
        != instruction_sha256
    ):
        raise ValueError("semantic_direction_horizon_packed_language_digest_mismatch")
    target = value.get("target_object_name")
    non_target = value.get("non_target_object_name")
    if target not in _OBJECTS or non_target not in _OBJECTS or target == non_target:
        raise ValueError("semantic_direction_horizon_object_binding_invalid")
    chunks_value = value.get("chunks")
    if (
        not isinstance(chunks_value, Sequence)
        or isinstance(chunks_value, (str, bytes))
        or len(chunks_value) != CHUNK_COUNT
    ):
        raise ValueError("semantic_direction_horizon_chunks_invalid")
    chunks = [_chunk(chunk, chunk_index=i) for i, chunk in enumerate(chunks_value)]
    material = {
        "trial_index": index,
        "label": label,
        "instruction": instruction,
        "instruction_sha256": instruction_sha256,
        "packed_language_exact_match": True,
        "packed_language_sha256": instruction_sha256,
        "target_object_name": target,
        "non_target_object_name": non_target,
        "observation_sha256": _sha(
            value.get("observation_sha256"),
            field="semantic_direction_horizon_observation_sha256",
        ),
        "restored_state_sha256": _sha(
            value.get("restored_state_sha256"),
            field="semantic_direction_horizon_restored_state_sha256",
        ),
        "terminal_state_sha256": _sha(
            value.get("terminal_state_sha256"),
            field="semantic_direction_horizon_terminal_state_sha256",
        ),
        "sampling_seed": value.get("sampling_seed"),
        "chunks": chunks,
        "model_forward_count": value.get("model_forward_count"),
        "actions_applied": value.get("actions_applied"),
        "simulator_effect_observed": value.get("simulator_effect_observed"),
        "initial_end_effector_position_metres": _vector3(
            value.get("initial_end_effector_position_metres"),
            field="semantic_direction_horizon_initial_eef",
        ),
        "terminal_end_effector_position_metres": _vector3(
            value.get("terminal_end_effector_position_metres"),
            field="semantic_direction_horizon_terminal_eef",
        ),
        "target_initial_distance_metres": _number(
            value.get("target_initial_distance_metres"),
            field="semantic_direction_horizon_target_initial_distance",
        ),
        "target_terminal_distance_metres": _number(
            value.get("target_terminal_distance_metres"),
            field="semantic_direction_horizon_target_terminal_distance",
        ),
        "non_target_initial_distance_metres": _number(
            value.get("non_target_initial_distance_metres"),
            field="semantic_direction_horizon_non_target_initial_distance",
        ),
        "non_target_terminal_distance_metres": _number(
            value.get("non_target_terminal_distance_metres"),
            field="semantic_direction_horizon_non_target_terminal_distance",
        ),
        "initial_goal_predicate_vector": _predicate_vector(
            value.get("initial_goal_predicate_vector"),
            field="semantic_direction_horizon_initial_predicates",
        ),
        "terminal_goal_predicate_vector": _predicate_vector(
            value.get("terminal_goal_predicate_vector"),
            field="semantic_direction_horizon_terminal_predicates",
        ),
        "preservation_violation_observed": value.get("preservation_violation_observed"),
    }
    if material["observation_sha256"] != observation_sha256:
        raise ValueError("semantic_direction_horizon_observation_mismatch")
    if material["sampling_seed"] != sampling_seed:
        raise ValueError("semantic_direction_horizon_sampling_seed_mismatch")
    if material["model_forward_count"] != CHUNK_COUNT:
        raise ValueError("semantic_direction_horizon_forward_count_invalid")
    if material["actions_applied"] != CHUNK_COUNT * ACTIONS_PER_CHUNK:
        raise ValueError("semantic_direction_horizon_action_count_invalid")
    if material["simulator_effect_observed"] is not True:
        raise ValueError("semantic_direction_horizon_simulator_effect_not_observed")
    if material["preservation_violation_observed"] is not False:
        raise ValueError("semantic_direction_horizon_preservation_violation")
    material["target_progress_metres"] = (
        material["target_initial_distance_metres"] - material["target_terminal_distance_metres"]
    )
    material["non_target_progress_metres"] = (
        material["non_target_initial_distance_metres"]
        - material["non_target_terminal_distance_metres"]
    )
    material["target_preference_margin_metres"] = (
        material["target_progress_metres"] - material["non_target_progress_metres"]
    )
    return material


def build_semantic_direction_horizon_probe_result(
    *,
    snapshot_artifact_sha256: str,
    observation_sha256: str,
    checkpoint_revision: str,
    lerobot_revision: str,
    sampling_seed: int,
    diagnostic_authorization_ref: str,
    trials: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify the separately preregistered three-chunk A/A/B diagnostic."""

    if isinstance(sampling_seed, bool) or not isinstance(sampling_seed, int) or sampling_seed < 0:
        raise ValueError("semantic_direction_horizon_sampling_seed_invalid")
    if not isinstance(diagnostic_authorization_ref, str) or not diagnostic_authorization_ref:
        raise ValueError("semantic_direction_horizon_authorization_ref_invalid")
    if not isinstance(trials, Sequence) or isinstance(trials, (str, bytes)) or len(trials) != 3:
        raise ValueError("semantic_direction_horizon_trials_invalid")
    checked_observation = _sha(
        observation_sha256, field="semantic_direction_horizon_fixed_observation_sha256"
    )
    checked = [
        _trial(
            value,
            index=index,
            label=label,
            observation_sha256=checked_observation,
            sampling_seed=sampling_seed,
        )
        for index, (label, value) in enumerate(zip(_EXPECTED_SEQUENCE, trials, strict=True))
    ]
    if checked[0]["instruction"] != checked[1]["instruction"]:
        raise ValueError("semantic_direction_horizon_aa_instruction_mismatch")
    if checked[0]["instruction"] == checked[2]["instruction"]:
        raise ValueError("semantic_direction_horizon_ab_instruction_not_contrasted")
    if checked[0]["target_object_name"] != checked[1]["target_object_name"]:
        raise ValueError("semantic_direction_horizon_aa_target_mismatch")
    if checked[0]["target_object_name"] == checked[2]["target_object_name"]:
        raise ValueError("semantic_direction_horizon_ab_target_not_contrasted")

    aa_initial_chunk_reproduced = all(
        checked[0]["chunks"][0][field] == checked[1]["chunks"][0][field]
        for field in (
            "policy_request_sha256",
            "policy_prediction_sha256",
            "action_chunk_sha256",
        )
    )
    contrast_progress_to_failed_target = checked[2]["non_target_progress_metres"]
    a_criteria = [
        {
            "trial_index": trial["trial_index"],
            "failed_target_positive_progress": trial["target_progress_metres"] > 0.0,
            "failed_target_preferred_to_protected_object": (
                trial["target_preference_margin_metres"] > 0.0
            ),
            "failed_target_progress_exceeds_contrast_trial": (
                trial["target_progress_metres"] > contrast_progress_to_failed_target
            ),
            "preservation_violation_observed": False,
        }
        for trial in checked[:2]
    ]
    alignment = bool(
        aa_initial_chunk_reproduced
        and all(
            criterion["failed_target_positive_progress"]
            and criterion["failed_target_preferred_to_protected_object"]
            and criterion["failed_target_progress_exceeds_contrast_trial"]
            for criterion in a_criteria
        )
    )
    if not aa_initial_chunk_reproduced:
        status = "aa_initial_chunk_control_not_reproduced"
    elif alignment:
        status = "local_three_chunk_failed_target_direction_alignment_observed"
    else:
        status = "local_three_chunk_direction_alignment_not_observed"

    material = {
        "schema_version": SCHEMA_VERSION,
        "authority": AUTHORITY,
        "status": status,
        "snapshot_artifact_sha256": _sha(
            snapshot_artifact_sha256, field="semantic_direction_horizon_snapshot_sha256"
        ),
        "observation_sha256": checked_observation,
        "checkpoint_revision": _revision(
            checkpoint_revision, field="semantic_direction_horizon_checkpoint_revision"
        ),
        "lerobot_revision": _revision(
            lerobot_revision, field="semantic_direction_horizon_lerobot_revision"
        ),
        "sampling_seed": sampling_seed,
        "diagnostic_authorization_ref": diagnostic_authorization_ref,
        "trial_sequence": list(_EXPECTED_SEQUENCE),
        "chunks_per_trial": CHUNK_COUNT,
        "actions_per_chunk": ACTIONS_PER_CHUNK,
        "model_forward_count": CHUNK_COUNT * len(checked),
        "simulator_action_count": CHUNK_COUNT * ACTIONS_PER_CHUNK * len(checked),
        "trials": checked,
        "aa_control": {
            "initial_policy_request_prediction_and_chunk_reproduced": (aa_initial_chunk_reproduced),
            "later_closed_loop_trajectory_identity_required": False,
        },
        "pre_registered_direction_criteria": {
            "a_trials": a_criteria,
            "contrast_progress_to_failed_target_metres": contrast_progress_to_failed_target,
            "preservation_violation_observed": False,
        },
        "local_three_chunk_failed_target_direction_alignment_observed": alignment,
        "instruction_comprehension_established": False,
        "repair_capability_established": False,
        "task_completion_claimed": False,
        "semantic_repair_established": False,
        "controller_ack_observed": False,
        "approval_created": False,
        "dispatch_created": False,
        "policy_actions_dispatched": False,
        "simulator_action_application_observed": True,
        "physical_execution_invoked": False,
        "claim_boundary": (
            "A three-chunk diagnostic clone can establish only local semantic-direction "
            "alignment at one restored observation and seed. It cannot establish general "
            "instruction comprehension, Repair capability, task completion, Semantic Repair, "
            "controller ACK, governed dispatch, or physical execution."
        ),
    }
    return {**deepcopy(material), "result_sha256": canonical_sha256(material)}

"""Fail-closed evidence for a local GR00T language-conditioning probe.

The probe compares two model forwards from one frozen observation.  It does
not approve or dispatch actions and cannot establish instruction
comprehension, task completion, Semantic Repair, or physical execution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import re
from typing import Any

from missionos_core import canonical_sha256


SCHEMA_VERSION = "missionos.groot_lerobot_language_conditioning_probe.v1"
AUTHORITY = "diagnostic_only"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_EXPECTED_SEQUENCE = ("A", "A", "B")


def _required_sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field}_invalid")
    return value


def _required_revision(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise ValueError(f"{field}_invalid")
    return value


def _validated_trial(
    value: Mapping[str, Any],
    *,
    index: int,
    label: str,
    expected_observation_sha256: str,
    expected_sampling_seed: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("language_probe_trial_not_mapping")
    if value.get("trial_index") != index or value.get("label") != label:
        raise ValueError("language_probe_trial_sequence_mismatch")
    instruction = value.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("language_probe_instruction_invalid")
    instruction_sha256 = _required_sha256(
        value.get("instruction_sha256"), field="language_probe_instruction_sha256"
    )
    if instruction_sha256 != hashlib.sha256(instruction.encode("utf-8")).hexdigest():
        raise ValueError("language_probe_instruction_digest_mismatch")
    if value.get("packed_language_exact_match") is not True:
        raise ValueError("language_probe_packed_language_mismatch")
    packed_language_sha256 = _required_sha256(
        value.get("packed_language_sha256"), field="language_probe_packed_language_sha256"
    )
    if packed_language_sha256 != instruction_sha256:
        raise ValueError("language_probe_packed_language_digest_mismatch")
    material = {
        "trial_index": index,
        "label": label,
        "instruction": instruction,
        "instruction_sha256": instruction_sha256,
        "packed_language_exact_match": True,
        "packed_language_sha256": packed_language_sha256,
        "observation_sha256": _required_sha256(
            value.get("observation_sha256"), field="language_probe_trial_observation_sha256"
        ),
        "sampling_seed": value.get("sampling_seed"),
        "policy_queue_empty_before_forward": value.get(
            "policy_queue_empty_before_forward"
        ),
        "policy_request_sha256": _required_sha256(
            value.get("policy_request_sha256"), field="language_probe_policy_request_sha256"
        ),
        "policy_prediction_sha256": _required_sha256(
            value.get("policy_prediction_sha256"),
            field="language_probe_policy_prediction_sha256",
        ),
        "selected_action_sha256": _required_sha256(
            value.get("selected_action_sha256"),
            field="language_probe_selected_action_sha256",
        ),
        "model_forward_observed": value.get("model_forward_observed"),
        "simulator_action_applied": value.get("simulator_action_applied"),
    }
    if material["model_forward_observed"] is not True:
        raise ValueError("language_probe_model_forward_not_observed")
    if material["simulator_action_applied"] is not False:
        raise ValueError("language_probe_simulator_action_forbidden")
    if material["observation_sha256"] != expected_observation_sha256:
        raise ValueError("language_probe_trial_observation_mismatch")
    if material["sampling_seed"] != expected_sampling_seed:
        raise ValueError("language_probe_trial_sampling_seed_mismatch")
    if material["policy_queue_empty_before_forward"] is not True:
        raise ValueError("language_probe_policy_queue_boundary_unverified")
    return material


def build_language_conditioning_probe_result(
    *,
    snapshot_artifact_sha256: str,
    observation_sha256: str,
    checkpoint_revision: str,
    lerobot_revision: str,
    sampling_seed: int,
    diagnostic_authorization_ref: str,
    trials: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify one A/A/B probe without promoting a diagnostic difference."""

    if isinstance(sampling_seed, bool) or not isinstance(sampling_seed, int) or sampling_seed < 0:
        raise ValueError("language_probe_sampling_seed_invalid")
    if not isinstance(diagnostic_authorization_ref, str) or not diagnostic_authorization_ref.strip():
        raise ValueError("language_probe_diagnostic_authorization_ref_invalid")
    if not isinstance(trials, Sequence) or isinstance(trials, (str, bytes)):
        raise ValueError("language_probe_trials_invalid")
    if len(trials) != len(_EXPECTED_SEQUENCE):
        raise ValueError("language_probe_trial_count_invalid")
    checked_observation_sha256 = _required_sha256(
        observation_sha256, field="language_probe_observation_sha256"
    )
    checked = [
        _validated_trial(
            value,
            index=index,
            label=label,
            expected_observation_sha256=checked_observation_sha256,
            expected_sampling_seed=sampling_seed,
        )
        for index, (label, value) in enumerate(zip(_EXPECTED_SEQUENCE, trials, strict=True))
    ]
    if checked[0]["instruction"] != checked[1]["instruction"]:
        raise ValueError("language_probe_aa_instruction_mismatch")
    if checked[0]["instruction"] == checked[2]["instruction"]:
        raise ValueError("language_probe_ab_instruction_not_contrasted")

    aa_request_reproduced = (
        checked[0]["policy_request_sha256"] == checked[1]["policy_request_sha256"]
    )
    aa_prediction_reproduced = (
        checked[0]["policy_prediction_sha256"] == checked[1]["policy_prediction_sha256"]
    )
    aa_action_reproduced = (
        checked[0]["selected_action_sha256"] == checked[1]["selected_action_sha256"]
    )
    aa_control_reproduced = (
        aa_request_reproduced and aa_prediction_reproduced and aa_action_reproduced
    )
    ab_request_differs = (
        checked[0]["policy_request_sha256"] != checked[2]["policy_request_sha256"]
    )
    ab_prediction_differs = (
        checked[0]["policy_prediction_sha256"] != checked[2]["policy_prediction_sha256"]
    )
    ab_action_differs = (
        checked[0]["selected_action_sha256"] != checked[2]["selected_action_sha256"]
    )
    local_conditioning_observed = bool(
        aa_control_reproduced and ab_request_differs and ab_prediction_differs
    )
    if not aa_control_reproduced:
        status = "aa_control_not_reproduced"
    elif not ab_request_differs:
        status = "ab_language_request_not_distinguished"
    elif local_conditioning_observed:
        status = "local_instruction_conditioning_observed"
    else:
        status = "no_local_prediction_difference_observed"

    material = {
        "schema_version": SCHEMA_VERSION,
        "authority": AUTHORITY,
        "status": status,
        "snapshot_artifact_sha256": _required_sha256(
            snapshot_artifact_sha256, field="language_probe_snapshot_artifact_sha256"
        ),
        "observation_sha256": checked_observation_sha256,
        "checkpoint_revision": _required_revision(
            checkpoint_revision, field="language_probe_checkpoint_revision"
        ),
        "lerobot_revision": _required_revision(
            lerobot_revision, field="language_probe_lerobot_revision"
        ),
        "sampling_seed": sampling_seed,
        "diagnostic_authorization_ref": diagnostic_authorization_ref,
        "trial_sequence": list(_EXPECTED_SEQUENCE),
        "trials": checked,
        "aa_control": {
            "policy_request_reproduced": aa_request_reproduced,
            "policy_prediction_reproduced": aa_prediction_reproduced,
            "selected_action_reproduced": aa_action_reproduced,
            "control_reproduced": aa_control_reproduced,
        },
        "ab_contrast": {
            "policy_request_differs": ab_request_differs,
            "policy_prediction_differs": ab_prediction_differs,
            "selected_action_differs": ab_action_differs,
        },
        "local_instruction_conditioning_observed": local_conditioning_observed,
        "instruction_comprehension_established": False,
        "repair_capability_established": False,
        "task_completion_claimed": False,
        "semantic_repair_established": False,
        "controller_ack_observed": False,
        "approval_created": False,
        "dispatch_created": False,
        "policy_actions_dispatched": False,
        "simulator_action_application_observed": False,
        "physical_execution_invoked": False,
        "claim_boundary": (
            "An A/B prediction difference under one frozen observation and controlled seed "
            "can establish only local instruction sensitivity. It cannot establish semantic "
            "comprehension, successful Repair, controller ACK, action dispatch, simulator "
            "effect, or physical execution."
        ),
    }
    return {**deepcopy(material), "result_sha256": canonical_sha256(material)}

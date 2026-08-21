from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from missionos_core import canonical_sha256
from src.runtime.groot_lerobot_language_conditioning_probe import (
    build_language_conditioning_probe_result,
)


def _trial(index: int, label: str, instruction: str, prediction: str) -> dict:
    instruction_sha256 = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    return {
        "trial_index": index,
        "label": label,
        "instruction": instruction,
        "instruction_sha256": instruction_sha256,
        "packed_language_exact_match": True,
        "packed_language_sha256": instruction_sha256,
        "observation_sha256": "b" * 64,
        "sampling_seed": 1000,
        "policy_queue_empty_before_forward": True,
        "policy_request_sha256": canonical_sha256(
            {"observation": "same", "instruction": instruction}
        ),
        "policy_prediction_sha256": canonical_sha256({"prediction": prediction}),
        "selected_action_sha256": canonical_sha256({"prediction": prediction}),
        "model_forward_observed": True,
        "simulator_action_applied": False,
    }


def _result(*, contrast_prediction: str = "prediction-b") -> dict:
    instruction_a = "put the first moka pot on the stove"
    instruction_b = "put the second moka pot on the stove"
    return build_language_conditioning_probe_result(
        snapshot_artifact_sha256="a" * 64,
        observation_sha256="b" * 64,
        checkpoint_revision="c" * 40,
        lerobot_revision="d" * 40,
        sampling_seed=1000,
        diagnostic_authorization_ref="diagnostic:test",
        trials=(
            _trial(0, "A", instruction_a, "prediction-a"),
            _trial(1, "A", instruction_a, "prediction-a"),
            _trial(2, "B", instruction_b, contrast_prediction),
        ),
    )


def test_probe_establishes_only_local_instruction_conditioning() -> None:
    result = _result()

    assert result["status"] == "local_instruction_conditioning_observed"
    assert result["aa_control"]["control_reproduced"] is True
    assert result["ab_contrast"]["policy_request_differs"] is True
    assert result["ab_contrast"]["policy_prediction_differs"] is True
    assert result["local_instruction_conditioning_observed"] is True
    assert result["instruction_comprehension_established"] is False
    assert result["repair_capability_established"] is False
    assert result["semantic_repair_established"] is False
    assert result["policy_actions_dispatched"] is False
    assert result["simulator_action_application_observed"] is False
    assert result["physical_execution_invoked"] is False
    stated = result.pop("result_sha256")
    assert stated == canonical_sha256(result)


def test_probe_reports_no_local_prediction_difference_without_overclaim() -> None:
    result = _result(contrast_prediction="prediction-a")

    assert result["status"] == "no_local_prediction_difference_observed"
    assert result["local_instruction_conditioning_observed"] is False
    assert result["instruction_comprehension_established"] is False


def test_probe_rejects_nonreproducible_aa_control() -> None:
    instruction_a = "put the first moka pot on the stove"
    instruction_b = "put the second moka pot on the stove"
    result = build_language_conditioning_probe_result(
        snapshot_artifact_sha256="a" * 64,
        observation_sha256="b" * 64,
        checkpoint_revision="c" * 40,
        lerobot_revision="d" * 40,
        sampling_seed=1000,
        diagnostic_authorization_ref="diagnostic:test",
        trials=(
            _trial(0, "A", instruction_a, "prediction-a0"),
            _trial(1, "A", instruction_a, "prediction-a1"),
            _trial(2, "B", instruction_b, "prediction-b"),
        ),
    )

    assert result["status"] == "aa_control_not_reproduced"
    assert result["local_instruction_conditioning_observed"] is False


def test_probe_rejects_unpinned_revision() -> None:
    instruction_a = "put the first moka pot on the stove"
    instruction_b = "put the second moka pot on the stove"

    with pytest.raises(ValueError, match="language_probe_checkpoint_revision_invalid"):
        build_language_conditioning_probe_result(
            snapshot_artifact_sha256="a" * 64,
            observation_sha256="b" * 64,
            checkpoint_revision="main",
            lerobot_revision="d" * 40,
            sampling_seed=1000,
            diagnostic_authorization_ref="diagnostic:test",
            trials=(
                _trial(0, "A", instruction_a, "prediction-a"),
                _trial(1, "A", instruction_a, "prediction-a"),
                _trial(2, "B", instruction_b, "prediction-b"),
            ),
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("packed_language_exact_match", False, "language_probe_packed_language_mismatch"),
        ("model_forward_observed", False, "language_probe_model_forward_not_observed"),
        ("simulator_action_applied", True, "language_probe_simulator_action_forbidden"),
        ("observation_sha256", "e" * 64, "language_probe_trial_observation_mismatch"),
        ("sampling_seed", 1001, "language_probe_trial_sampling_seed_mismatch"),
        (
            "policy_queue_empty_before_forward",
            False,
            "language_probe_policy_queue_boundary_unverified",
        ),
        ("policy_prediction_sha256", None, "language_probe_policy_prediction_sha256_invalid"),
    ),
)
def test_probe_fails_closed_on_incomplete_or_overreaching_trial(
    field: str, value: object, error: str
) -> None:
    instruction_a = "put the first moka pot on the stove"
    instruction_b = "put the second moka pot on the stove"
    trials = [
        _trial(0, "A", instruction_a, "prediction-a"),
        _trial(1, "A", instruction_a, "prediction-a"),
        _trial(2, "B", instruction_b, "prediction-b"),
    ]
    forged = deepcopy(trials)
    forged[0][field] = value

    with pytest.raises(ValueError, match=error):
        build_language_conditioning_probe_result(
            snapshot_artifact_sha256="a" * 64,
            observation_sha256="b" * 64,
            checkpoint_revision="c" * 40,
            lerobot_revision="d" * 40,
            sampling_seed=1000,
            diagnostic_authorization_ref="diagnostic:test",
            trials=forged,
        )

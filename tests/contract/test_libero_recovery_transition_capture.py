from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from src.runtime.libero_recovery_transition_capture import (
    CAPTURE_SCHEMA_VERSION,
    LiberoRecoveryTransitionCapture,
    libero_observation_state,
    quaternion_xyzw_to_axis_angle,
)


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/agents/evidence/20260905-libero-recovery-transition-capture.json"


def _observation(value: int = 0) -> dict[str, np.ndarray]:
    return {
        "agentview_image": np.full((8, 12, 3), value, dtype=np.uint8),
        "robot0_eye_in_hand_image": np.full((8, 12, 3), value + 1, dtype=np.uint8),
        "robot0_eef_pos": np.array([0.1, -0.2, 0.3]),
        "robot0_eef_quat": np.array([0.0, 0.0, 0.0, 1.0]),
        "robot0_gripper_qpos": np.array([0.04, -0.04]),
    }


def test_libero_state_matches_official_eight_dimension_contract() -> None:
    state = libero_observation_state(_observation())
    assert state.dtype == np.float32
    assert state.tolist() == pytest.approx([0.1, -0.2, 0.3, 0.0, 0.0, 0.0, 0.04, -0.04])
    assert quaternion_xyzw_to_axis_angle([1.0, 0.0, 0.0, 0.0]).tolist() == pytest.approx(
        [np.pi, 0.0, 0.0]
    )


def test_capture_writes_aligned_digest_bound_candidate(tmp_path) -> None:
    capture = LiberoRecoveryTransitionCapture()
    capture.append(observation=_observation(0), action=[1, 0, 0, 0, 0, 0, -1])
    capture.append(observation=_observation(2), action=[0, 1, 0, 0, 0, 0, -1])

    summary = capture.write(
        output_dir=tmp_path / "capture",
        source={"episode_init_state_index": 0, "environment_seed": 101},
        outcome={"stable_success_observed": True},
    )
    manifest_path = tmp_path / "capture" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    archive_path = tmp_path / "capture" / "transition-arrays.npz"

    assert manifest["schema_version"] == CAPTURE_SCHEMA_VERSION
    assert manifest["frame_count"] == 2
    assert manifest["alignment"] == "observation_before_applied_action"
    assert manifest["claim_boundary"]["training_example_admitted"] is False
    assert summary["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert manifest["archive"]["sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    with np.load(archive_path, allow_pickle=False) as arrays:
        assert arrays["agentview_rgb"].shape == (2, 8, 12, 3)
        assert arrays["wrist_rgb"].shape == (2, 8, 12, 3)
        assert arrays["observation_state"].shape == (2, 8)
        assert arrays["applied_action"].shape == (2, 7)
        assert arrays["timestamp"].tolist() == pytest.approx([0.0, 0.05])
        for key, material in manifest["arrays"].items():
            assert (
                material["sha256"]
                == hashlib.sha256(np.ascontiguousarray(arrays[key]).tobytes()).hexdigest()
            )


def test_capture_rejects_non_rgb_or_wrong_action_shape() -> None:
    capture = LiberoRecoveryTransitionCapture()
    observation = _observation()
    observation["agentview_image"] = np.zeros((8, 12), dtype=np.uint8)
    with pytest.raises(ValueError, match="camera_invalid"):
        capture.append(observation=observation, action=np.zeros(7))
    with pytest.raises(ValueError, match="action_invalid"):
        capture.append(observation=_observation(), action=np.zeros(6))


def test_checked_in_capture_evidence_stays_bounded_and_schema_aligned() -> None:
    evidence = json.loads(EVIDENCE.read_text())
    assert evidence["decision"] == "RAW_CAPTURE_POSITIVE_PAID_TRAINING_NO_GO"
    assert evidence["scenario"]["episode_init_state_index"] == 0
    assert evidence["scenario"]["environment_seed"] == 101
    assert evidence["observed_result"]["stable_success_observed"] is True
    assert evidence["observed_result"]["preservation_violation_observed"] is False
    assert evidence["capture"]["arrays"]["observation_state"]["shape"] == [52, 8]
    assert evidence["capture"]["arrays"]["applied_action"]["shape"] == [52, 7]
    assert evidence["cost_control"]["gpu_used"] is False
    assert evidence["cost_control"]["instance_absent_after_run"] is True
    assert evidence["cost_control"]["boot_disk_absent_after_run"] is True
    assert evidence["claim_boundary"]["training_example_admitted"] is False
    assert evidence["claim_boundary"]["paid_training_authorized"] is False
    assert evidence["claim_boundary"]["repair_capability_established"] is False

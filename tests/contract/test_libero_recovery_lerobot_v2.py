from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.runtime.libero_recovery_lerobot_v2 import validate_capture
from src.runtime.libero_recovery_transition_capture import LiberoRecoveryTransitionCapture


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/agents/evidence/20260905-libero-recovery-lerobot-v2-conversion.json"


def _observation() -> dict[str, np.ndarray]:
    return {
        "agentview_image": np.zeros((256, 256, 3), dtype=np.uint8),
        "robot0_eye_in_hand_image": np.ones((256, 256, 3), dtype=np.uint8),
        "robot0_eef_pos": np.zeros(3),
        "robot0_eef_quat": np.array([0, 0, 0, 1]),
        "robot0_gripper_qpos": np.array([0.04, -0.04]),
    }


def test_validate_capture_accepts_digest_bound_arrays(tmp_path) -> None:
    capture = LiberoRecoveryTransitionCapture()
    capture.append(observation=_observation(), action=np.zeros(7))
    capture.write(
        output_dir=tmp_path / "capture",
        source={"instruction": "put both moka pots on the stove"},
        outcome={"stable_success_observed": True},
    )
    manifest, arrays = validate_capture(tmp_path / "capture")
    assert manifest["frame_count"] == 1
    assert arrays["observation_state"].shape == (1, 8)
    assert arrays["applied_action"].shape == (1, 7)


def test_validate_capture_rejects_manifest_tampering(tmp_path) -> None:
    capture = LiberoRecoveryTransitionCapture()
    capture.append(observation=_observation(), action=np.zeros(7))
    capture.write(
        output_dir=tmp_path / "capture",
        source={"instruction": "put both moka pots on the stove"},
        outcome={"stable_success_observed": True},
    )
    path = tmp_path / "capture/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["frame_count"] = 2
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest_digest_mismatch"):
        validate_capture(tmp_path / "capture")


def test_checked_in_conversion_evidence_does_not_admit_training() -> None:
    evidence = json.loads(EVIDENCE.read_text())
    assert evidence["decision"] == "SINGLE_CANDIDATE_CONVERSION_POSITIVE_DATASET_ADMISSION_PENDING"
    assert evidence["observed_output"]["parquet_rows"] == 52
    assert evidence["observed_output"]["determinism_check"]["all_output_file_digests_equal"]
    assert evidence["cost_control"]["cloud_resources_created"] is False
    assert evidence["claim_boundary"]["single_candidate_schema_conversion_complete"] is True
    assert evidence["claim_boundary"]["multi_example_dataset_complete"] is False
    assert evidence["claim_boundary"]["leakage_audit_passed"] is False
    assert evidence["claim_boundary"]["paid_training_authorized"] is False

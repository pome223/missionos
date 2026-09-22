"""Source identity must not be changed by the public-only ANWM validator.

These synthetic arrays satisfy its public input geometry solely to isolate
source admission. They are neither simulator captures nor model evidence.
"""

import numpy as np
import pytest

from scripts.aerial_anwm_runtime import CONTEXT_SIZE, run, target_pose_from_delta


@pytest.fixture
def public_compatible_request(tmp_path):
    poses = np.tile(np.eye(4), (CONTEXT_SIZE, 1, 1))
    poses[:, :3, :3] = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]
    np.savez(
        tmp_path / "synthetic-assets.npz",
        context_rgb=np.zeros((CONTEXT_SIZE, 16, 16, 3), dtype=np.uint8),
        context_depth=np.ones((CONTEXT_SIZE, 16, 16), dtype=np.float32),
        context_camera_poses=poses,
        camera_intrinsics=np.array([[8, 0, 8], [0, 8, 8], [0, 0, 1]]),
        goal_rgb=np.zeros((16, 16, 3), dtype=np.uint8),
    )
    candidates = []
    for identifier, forward in (("forward", 1.0), ("backward", -1.0)):
        delta = np.array([forward, 0.0, 0.0, 0.0])
        candidates.append({
            "candidate_id": identifier,
            "delta_local_m_rad": delta.tolist(),
            "target_camera_pose": target_pose_from_delta(np, poses[-1], delta).tolist(),
            "horizon_seconds": 1.0,
        })
    request = {
        "schema_version": "aerial_anwm_request.v1",
        "source_kind": "public_dataset_replay",
        "request_id": "synthetic-source-admission-test",
        "assets_npz": "synthetic-assets.npz",
        "delta_frame": "body_frd_at_observation",
        "num_timesteps": 4,
        "frame_interval_seconds": 0.25,
        "frame_interval_source": "ANWM public benchmark 4 fps",
        "physical_frame_timing_verified": False,
        "horizon_seconds_nominal": True,
        "source_timing": {
            "metadata_field": "timestamps",
            "metadata_semantics": "source_csv_row_indices",
            "source_row_indices": list(range(11, 11 + CONTEXT_SIZE + 4)),
            "nominal_input_fps": 4,
        },
        "public_provenance": {"dataset_repository": "EmbodiedCity/ANWM-Dataset"},
        "candidates": candidates,
        "checkpoint_path": "no-checkpoint-is-installed",
        "upstream_root": "no-model-is-installed",
    }
    return request, tmp_path


def test_public_source_validation_requires_no_model_or_gpu(public_compatible_request):
    request, base = public_compatible_request
    result = run(request, base, base / "unused", validate_only=True)
    assert result["validated"] is True
    assert result["input_manifest"]["source_kind"] == "public_dataset_replay"
    assert "runtime_invocation_evidence" not in result


@pytest.mark.parametrize("source_kind", [
    "px4_gazebo_frozen_capture",
    "actual_px4_gazebo_sensor_capture",
    "live_px4_observation",
    "hardware_frozen_capture",
])
def test_px4_and_other_sources_cannot_be_relabelled_as_public_dataset(public_compatible_request, source_kind):
    request, base = public_compatible_request
    request["source_kind"] = source_kind
    # Even otherwise acceptable public metadata cannot override explicit source
    # identity. A real adapter must validate the source under its own contract.
    with pytest.raises(ValueError, match="public_dataset_replay"):
        run(request, base, base / "unused", validate_only=True)

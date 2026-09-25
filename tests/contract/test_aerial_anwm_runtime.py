"""Input-boundary checks for the optional aerial model runner; no inference."""

import io
import os
import pickle

import numpy as np
import pytest

from scripts.aerial_anwm_runtime import (
    CONTEXT_SIZE,
    digest_json,
    run,
    target_pose_from_delta,
    validate_request,
)
from scripts.prepare_aerial_anwm_sample import NumpyOnlyUnpickler


@pytest.fixture
def request_assets(tmp_path):
    poses = np.tile(np.eye(4), (CONTEXT_SIZE, 1, 1))
    poses[:, :3, :3] = np.array([[0, 0, 1], [1, 0, 0], [0, 1, 0]])
    arrays = {
        "context_rgb": np.zeros((CONTEXT_SIZE, 16, 16, 3), dtype=np.uint8),
        "context_depth": np.ones((CONTEXT_SIZE, 16, 16)),
        "context_camera_poses": poses,
        "camera_intrinsics": np.array([[8, 0, 8], [0, 8, 8], [0, 0, 1]]),
        "goal_rgb": np.zeros((16, 16, 3), dtype=np.uint8),
    }
    np.savez(tmp_path / "assets.npz", **arrays)
    candidates = []
    for identifier, delta in (("forward", [5.0, 0, 0, 0]), ("left", [0, -5.0, 0, 0])):
        candidates.append(
            {
                "candidate_id": identifier,
                "delta_local_m_rad": delta,
                "target_camera_pose": target_pose_from_delta(
                    np, poses[-1], np.array(delta)
                ).tolist(),
                "horizon_seconds": 1.0,
            }
        )
    request = {
        "schema_version": "aerial_anwm_request.v1",
        "request_id": "validator-fixture-only",
        "assets_npz": "assets.npz",
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
    }
    return request, arrays, tmp_path


def test_validate_only_binds_input_without_model_or_checkpoint(request_assets):
    request, _, base = request_assets
    result = run(request, base, base / "result", validate_only=True)
    assert result["validated"] is True
    manifest = result["input_manifest"]
    assert result["input_manifest_sha256"] == digest_json(manifest)
    assert len(manifest["candidates"]) == 2
    assert manifest["context_size"] == 16
    assert manifest["num_timesteps"] * manifest["frame_interval_seconds"] == 1.0
    assert "runtime_invocation_evidence" not in result


def test_missing_assets_fail_before_model_loading(request_assets):
    request, _, base = request_assets
    request["assets_npz"] = "does-not-exist.npz"
    with pytest.raises(FileNotFoundError):
        validate_request(request, base)


def test_four_frame_public_config_cannot_satisfy_released_checkpoint(request_assets):
    request, arrays, base = request_assets
    for key in ("context_rgb", "context_depth", "context_camera_poses"):
        arrays[key] = arrays[key][:4]
    np.savez(base / "assets.npz", **arrays)
    with pytest.raises(ValueError, match="sixteen real historical"):
        validate_request(request, base)


@pytest.mark.parametrize(
    "field", ["num_timesteps", "frame_interval_seconds", "diffusion_steps", "seed"]
)
def test_boolean_numeric_fields_rejected(request_assets, field):
    request, _, base = request_assets
    request[field] = True
    with pytest.raises(ValueError):
        validate_request(request, base)


def test_boolean_candidate_horizon_rejected(request_assets):
    request, _, base = request_assets
    request["candidates"][0]["horizon_seconds"] = True
    with pytest.raises(ValueError, match="horizon"):
        validate_request(request, base)


def test_nominal_timing_cannot_be_claimed_as_measured(request_assets):
    request, _, base = request_assets
    request["physical_frame_timing_verified"] = True
    with pytest.raises(ValueError, match="explicitly marked nominal"):
        validate_request(request, base)


def test_mismatched_action_and_projection_target_rejected(request_assets):
    request, _, base = request_assets
    request["candidates"][0]["target_camera_pose"][0][3] += 1
    with pytest.raises(ValueError, match="does not match"):
        validate_request(request, base)


def test_future_ground_truth_array_cannot_enter_model_archive(request_assets):
    request, arrays, base = request_assets
    arrays["future_rgb"] = arrays["context_rgb"]
    np.savez(base / "assets.npz", **arrays)
    with pytest.raises(ValueError, match="only historical observations"):
        validate_request(request, base)


def test_preparation_unpickler_rejects_arbitrary_globals():
    class UnsafePayload:
        def __reduce__(self):
            return os.system, ("this command must never run",)

    data = pickle.dumps(UnsafePayload())
    with pytest.raises(pickle.UnpicklingError, match="forbidden global"):
        NumpyOnlyUnpickler(io.BytesIO(data)).load()


def test_preparation_unpickler_reads_numeric_numpy_metadata():
    source = {"depth": np.ones((4, 2, 2), dtype=np.float32)}
    # The released source uses the NumPy reconstruction protocol.
    data = pickle.dumps(source, protocol=4)
    restored = NumpyOnlyUnpickler(io.BytesIO(data)).load()
    np.testing.assert_array_equal(restored["depth"], source["depth"])

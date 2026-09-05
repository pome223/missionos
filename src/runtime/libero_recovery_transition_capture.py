"""Capture raw LIBERO transitions before GR00T LeRobot v2 conversion.

The capture is a simulator evidence artifact, not an admitted training example.
It keeps the observation that preceded each applied action aligned with that
action and binds every serialized array by shape, dtype, and SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np


CAPTURE_SCHEMA_VERSION = "missionos.libero_recovery_transition_capture.v1"
CONTROL_FREQUENCY_HZ = 20
CAMERA_KEYS = {
    "agentview_rgb": "agentview_image",
    "wrist_rgb": "robot0_eye_in_hand_image",
}


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def quaternion_xyzw_to_axis_angle(quaternion: Any) -> np.ndarray:
    """Use robosuite's LIBERO axis-angle convention after unit normalization."""

    quat = np.asarray(quaternion, dtype=np.float64).reshape(-1)
    if quat.shape != (4,) or not np.isfinite(quat).all():
        raise ValueError("libero_transition_capture_quaternion_invalid")
    norm = float(np.linalg.norm(quat))
    if norm == 0.0:
        raise ValueError("libero_transition_capture_quaternion_zero")
    quat = quat / norm
    quat[3] = np.clip(quat[3], -1.0, 1.0)
    denominator = math.sqrt(max(0.0, 1.0 - float(quat[3]) ** 2))
    if math.isclose(denominator, 0.0):
        return np.zeros(3, dtype=np.float64)
    return quat[:3] * (2.0 * math.acos(float(quat[3])) / denominator)


def libero_observation_state(observation: Mapping[str, Any]) -> np.ndarray:
    """Build the official LIBERO 8-D state: xyz, axis-angle, two gripper qpos."""

    position = np.asarray(observation.get("robot0_eef_pos"), dtype=np.float64).reshape(-1)
    gripper = np.asarray(observation.get("robot0_gripper_qpos"), dtype=np.float64).reshape(-1)
    if position.shape != (3,) or gripper.shape != (2,):
        raise ValueError("libero_transition_capture_state_shape_invalid")
    state = np.concatenate(
        (
            position,
            quaternion_xyzw_to_axis_angle(observation.get("robot0_eef_quat")),
            gripper,
        )
    ).astype(np.float32)
    if not np.isfinite(state).all():
        raise ValueError("libero_transition_capture_state_non_finite")
    return state


def _array_material(array: np.ndarray) -> dict[str, Any]:
    contiguous = np.ascontiguousarray(array)
    return {
        "dtype": str(contiguous.dtype),
        "shape": list(contiguous.shape),
        "sha256": hashlib.sha256(contiguous.tobytes()).hexdigest(),
    }


class LiberoRecoveryTransitionCapture:
    """Accumulate pre-action observations and the actions actually applied."""

    def __init__(self) -> None:
        self._images: dict[str, list[np.ndarray]] = {key: [] for key in CAMERA_KEYS}
        self._states: list[np.ndarray] = []
        self._actions: list[np.ndarray] = []

    def append(self, *, observation: Mapping[str, Any], action: Any) -> None:
        action_array = np.asarray(action, dtype=np.float32).reshape(-1)
        if action_array.shape != (7,) or not np.isfinite(action_array).all():
            raise ValueError("libero_transition_capture_action_invalid")
        images: dict[str, np.ndarray] = {}
        for output_key, observation_key in CAMERA_KEYS.items():
            image = np.asarray(observation.get(observation_key))
            if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
                raise ValueError(f"libero_transition_capture_camera_invalid:{observation_key}")
            images[output_key] = image.copy()
        state = libero_observation_state(observation)
        for key, image in images.items():
            self._images[key].append(image)
        self._states.append(state)
        self._actions.append(action_array.copy())

    def write(
        self,
        *,
        output_dir: Path,
        source: Mapping[str, Any],
        outcome: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not self._actions:
            raise ValueError("libero_transition_capture_empty")
        if output_dir.exists():
            raise ValueError("libero_transition_capture_output_exists")
        output_dir.mkdir(parents=True)

        arrays = {
            "agentview_rgb": np.stack(self._images["agentview_rgb"]),
            "wrist_rgb": np.stack(self._images["wrist_rgb"]),
            "observation_state": np.stack(self._states).astype(np.float32),
            "applied_action": np.stack(self._actions).astype(np.float32),
            "timestamp": (np.arange(len(self._actions), dtype=np.float32) / CONTROL_FREQUENCY_HZ),
        }
        archive_path = output_dir / "transition-arrays.npz"
        np.savez_compressed(archive_path, **arrays)
        manifest_without_digest = {
            "schema_version": CAPTURE_SCHEMA_VERSION,
            "status": "raw_transition_capture_complete_conversion_and_admission_pending",
            "frame_count": len(self._actions),
            "control_frequency_hz": CONTROL_FREQUENCY_HZ,
            "alignment": "observation_before_applied_action",
            "target_format": "GR00T-compatible LeRobot v2",
            "official_libero_contract": {
                "videos": [
                    "observation.images.image",
                    "observation.images.wrist_image",
                ],
                "observation_state": [
                    "eef_x",
                    "eef_y",
                    "eef_z",
                    "eef_axis_angle_x",
                    "eef_axis_angle_y",
                    "eef_axis_angle_z",
                    "gripper_qpos_0",
                    "gripper_qpos_1",
                ],
                "action": ["x", "y", "z", "roll", "pitch", "yaw", "gripper"],
                "annotation": "annotation.human.action.task_description",
            },
            "arrays": {key: _array_material(value) for key, value in arrays.items()},
            "archive": {
                "path": archive_path.name,
                "sha256": _sha256_path(archive_path),
                "size_bytes": archive_path.stat().st_size,
            },
            "source": dict(source),
            "outcome": dict(outcome),
            "claim_boundary": {
                "raw_capture_only": True,
                "lerobot_v2_conversion_complete": False,
                "training_example_admitted": False,
                "training_invoked": False,
                "model_inference_invoked": False,
                "physical_execution_invoked": False,
            },
        }
        manifest = {
            **manifest_without_digest,
            "manifest_sha256": _canonical_sha256(manifest_without_digest),
        }
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return {
            "manifest_path": str(manifest_path.relative_to(output_dir.parent)),
            "manifest_sha256": _sha256_path(manifest_path),
            "archive_path": str(archive_path.relative_to(output_dir.parent)),
            "archive_sha256": manifest_without_digest["archive"]["sha256"],
            "frame_count": len(self._actions),
            "status": manifest_without_digest["status"],
        }

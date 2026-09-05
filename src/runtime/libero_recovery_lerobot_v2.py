"""Convert a validated raw LIBERO recovery capture to GR00T LeRobot v2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import numpy as np

from src.runtime.libero_recovery_transition_capture import CAPTURE_SCHEMA_VERSION


CONVERSION_SCHEMA_VERSION = "missionos.libero_recovery_lerobot_v2_conversion.v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_capture(capture_dir: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    manifest_path = capture_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != CAPTURE_SCHEMA_VERSION:
        raise ValueError("libero_lerobot_capture_schema_mismatch")
    material = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    if manifest.get("manifest_sha256") != digest:
        raise ValueError("libero_lerobot_capture_manifest_digest_mismatch")
    archive_path = capture_dir / manifest["archive"]["path"]
    if _sha256(archive_path) != manifest["archive"]["sha256"]:
        raise ValueError("libero_lerobot_capture_archive_digest_mismatch")
    with np.load(archive_path, allow_pickle=False) as archive:
        arrays = {key: np.asarray(archive[key]) for key in archive.files}
    if set(arrays) != set(manifest["arrays"]):
        raise ValueError("libero_lerobot_capture_array_keys_mismatch")
    for key, array in arrays.items():
        expected = manifest["arrays"][key]
        if list(array.shape) != expected["shape"] or str(array.dtype) != expected["dtype"]:
            raise ValueError(f"libero_lerobot_capture_array_contract_mismatch:{key}")
        if hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest() != expected["sha256"]:
            raise ValueError(f"libero_lerobot_capture_array_digest_mismatch:{key}")
    frame_count = int(manifest["frame_count"])
    required_shapes = {
        "agentview_rgb": [frame_count, 256, 256, 3],
        "wrist_rgb": [frame_count, 256, 256, 3],
        "observation_state": [frame_count, 8],
        "applied_action": [frame_count, 7],
        "timestamp": [frame_count],
    }
    if any(list(arrays[key].shape) != shape for key, shape in required_shapes.items()):
        raise ValueError("libero_lerobot_capture_required_shape_mismatch")
    return manifest, arrays


def _write_video(frames: np.ndarray, path: Path, *, ffmpeg: str) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        "256x256",
        "-framerate",
        "20",
        "-i",
        "-",
        "-an",
        "-c:v",
        "libaom-av1",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    subprocess.run(command, input=np.ascontiguousarray(frames).tobytes(), check=True)  # noqa: S603


def convert_capture(capture_dir: Path, output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise ValueError("libero_lerobot_output_exists")
    manifest, arrays = validate_capture(capture_dir)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("libero_lerobot_pyarrow_required") from exc
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("libero_lerobot_ffmpeg_required")

    data_dir = output_dir / "data/chunk-000"
    image_dir = output_dir / "videos/chunk-000/observation.images.image"
    wrist_dir = output_dir / "videos/chunk-000/observation.images.wrist_image"
    meta_dir = output_dir / "meta"
    for path in (data_dir, image_dir, wrist_dir, meta_dir):
        path.mkdir(parents=True)
    episode_name = "episode_000000"
    _write_video(arrays["agentview_rgb"], image_dir / f"{episode_name}.mp4", ffmpeg=ffmpeg)
    _write_video(arrays["wrist_rgb"], wrist_dir / f"{episode_name}.mp4", ffmpeg=ffmpeg)

    count = int(manifest["frame_count"])
    state = pa.FixedSizeListArray.from_arrays(
        pa.array(arrays["observation_state"].reshape(-1), type=pa.float32()), 8
    )
    action = pa.FixedSizeListArray.from_arrays(
        pa.array(arrays["applied_action"].reshape(-1), type=pa.float32()), 7
    )
    table = pa.table(
        {
            "observation.state": state,
            "action": action,
            "timestamp": pa.array(arrays["timestamp"], type=pa.float32()),
            "frame_index": pa.array(range(count), type=pa.int64()),
            "episode_index": pa.array([0] * count, type=pa.int64()),
            "index": pa.array(range(count), type=pa.int64()),
            "task_index": pa.array([0] * count, type=pa.int64()),
        }
    )
    pq.write_table(table, data_dir / f"{episode_name}.parquet")

    instruction = manifest["source"]["instruction"]
    (meta_dir / "episodes.jsonl").write_text(
        json.dumps({"episode_index": 0, "tasks": [instruction], "length": count}) + "\n"
    )
    (meta_dir / "tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": instruction}) + "\n")
    modality = {
        "state": {
            key: {"start": i, "end": i + 1}
            for i, key in enumerate(["x", "y", "z", "roll", "pitch", "yaw"])
        },
        "action": {
            key: {"start": i, "end": i + 1}
            for i, key in enumerate(["x", "y", "z", "roll", "pitch", "yaw", "gripper"])
        },
        "video": {
            "image": {"original_key": "observation.images.image"},
            "wrist_image": {"original_key": "observation.images.wrist_image"},
        },
        "annotation": {"human.action.task_description": {"original_key": "task_index"}},
    }
    modality["state"]["gripper"] = {"start": 6, "end": 8}
    (meta_dir / "modality.json").write_text(json.dumps(modality, indent=2) + "\n")
    info = {
        "codebase_version": "v2.1",
        "robot_type": "franka",
        "total_episodes": 1,
        "total_frames": count,
        "total_tasks": 1,
        "total_videos": 2,
        "total_chunks": 1,
        "chunks_size": 1000,
        "fps": 20,
        "splits": {"train": "0:1"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            **{
                key: {
                    "dtype": "video",
                    "shape": [256, 256, 3],
                    "names": ["height", "width", "rgb"],
                    "info": {
                        "video.height": 256,
                        "video.width": 256,
                        "video.codec": "av1",
                        "video.pix_fmt": "yuv420p",
                        "video.is_depth_map": False,
                        "video.fps": 20,
                        "video.channels": 3,
                        "has_audio": False,
                    },
                }
                for key in (
                    "observation.images.wrist_image",
                    "observation.images.image",
                )
            },
            "observation.state": {
                "dtype": "float32",
                "shape": [8],
                "names": {
                    "motors": [
                        "x",
                        "y",
                        "z",
                        "axis_angle1",
                        "axis_angle2",
                        "axis_angle3",
                        "gripper",
                        "gripper",
                    ]
                },
            },
            "action": {
                "dtype": "float32",
                "shape": [7],
                "names": {
                    "motors": [
                        "x",
                        "y",
                        "z",
                        "axis_angle1",
                        "axis_angle2",
                        "axis_angle3",
                        "gripper",
                    ]
                },
            },
            **{
                key: {"dtype": dtype, "shape": [1], "names": None}
                for key, dtype in {
                    "timestamp": "float32",
                    "frame_index": "int64",
                    "episode_index": "int64",
                    "index": "int64",
                    "task_index": "int64",
                }.items()
            },
        },
    }
    (meta_dir / "info.json").write_text(json.dumps(info, indent=2) + "\n")
    files = sorted(path for path in output_dir.rglob("*") if path.is_file())
    result = {
        "schema_version": CONVERSION_SCHEMA_VERSION,
        "status": "lerobot_v2_conversion_complete_admission_pending",
        "episode_count": 1,
        "frame_count": count,
        "source_archive_sha256": manifest["archive"]["sha256"],
        "files": {str(path.relative_to(output_dir)): _sha256(path) for path in files},
        "claim_boundary": {
            "schema_conversion_complete": True,
            "training_example_admitted": False,
            "training_invoked": False,
            "model_inference_invoked": False,
        },
    }
    (output_dir / "conversion-manifest.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    return result

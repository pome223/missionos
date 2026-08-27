#!/usr/bin/env python3
"""Run the pinned Cosmos Policy LIBERO nominal and Repair experiment.

This runner uses NVIDIA's pretrained Predict2 2B LIBERO policy without
additional training.  Future-image predictions and actual simulator
observations are written to disjoint artifact trees.  Only predicates observed
from the live LIBERO simulator can establish nominal success or Repair success.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
from typing import Any, Mapping
from uuid import uuid4


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402
from scripts.run_groot_lerobot_same_world_repair import (  # noqa: E402
    _object_poses,
    _object_witnesses,
    _read_failure_snapshot,
)
from scripts.run_vla0_libero_snapshot_recovery import (  # noqa: E402
    _environment_adapter,
    _predicate_material,
    _validate_scripted_fixture_snapshot,
)
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable  # noqa: E402
from src.runtime.cosmos_policy_libero_same_world_repair import (  # noqa: E402
    COSMOS_POLICY_LIBERO_ACTION_STEPS,
    build_cosmos_policy_same_world_repair_proposal,
    run_cosmos_policy_same_world_repair,
)
from src.runtime.groot_libero_same_world_repair import (  # noqa: E402
    FRAME_CAPTURE_AUTHORITY,
    FRAME_CAPTURE_SCHEMA_VERSION,
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    STATE_CONTINUITY_LIVE_SAME_WORLD,
    approve_same_world_repair,
    build_same_world_repair_dispatch,
    verify_exact_repair_instruction_payload,
)
from src.runtime.libero_panda_official_runner_instrumentation import (  # noqa: E402
    digest_runtime_material,
)
from src.runtime.libero_panda_predicate_package import (  # noqa: E402
    LIBERO_PANDA_SCENE8_ENVIRONMENT,
)
from src.runtime.libero_repair_failure_fixture import (  # noqa: E402
    SCRIPTED_FAILURE_FIXTURE_BASIS,
    failure_fixture_contract,
)


OPT_IN_ENV = "RUN_MISSIONOS_COSMOS_POLICY_LIBERO_EXPERIMENT"
COSMOS_POLICY_SOURCE_REVISION = "18a2accadf4e7a3531e56754102af5a24d2316da"
COSMOS_POLICY_CHECKPOINT_REVISION = "cb689ec0e3347c13667d70a78a3447388f5c3bb8"
COSMOS_POLICY_REPOSITORY = "nvidia/Cosmos-Policy-LIBERO-Predict2-2B"
COSMOS_POLICY_CHECKPOINT_FILES = {
    "Cosmos-Policy-LIBERO-Predict2-2B.pt": (
        3_913_017_345,
        "8818528d8c9150cda0ddf8c711b0f221b21dac8ac379bd26d5690235954d33e2",
    ),
    "config.json": (
        1_354,
        "6c246e6588762546fd91c4fac62af570583da1156aa9b2800f2268e6a2c31f43",
    ),
    "libero_dataset_statistics.json": (
        2_372,
        "5b119a98ad7824507ddff3c6c7507ca244261ee416dcfa623876702160c580d3",
    ),
    "libero_t5_embeddings.pkl": (
        41_957_938,
        "8a03499676c6c196127c577144b9fd09bb02c30f9caf058adbcef09bb99ad8f5",
    ),
}
TASK_SUITE = "libero_10"
TASK_NAME = "KITCHEN_SCENE8_put_both_moka_pots_on_the_stove"
TASK_ID = 8
EPISODE_INIT_STATE_INDEX = 15
TASK_INSTRUCTION = "put both moka pots on the stove"
EXPECTED_REPAIR_SOURCE_VECTOR = [True, False, True]
DISPLACEMENT_CURRICULUM_BASIS = "diagnostic_displacement_curriculum"
DISPLACEMENT_CURRICULUM_SCHEMA_VERSION = "missionos.libero_displacement_curriculum_fixture.v1"
DISPLACEMENT_CURRICULUM_CONSTRUCTION = "protected_separating_horizontal_ray_from_success_state"
MAXIMUM_CURRICULUM_TRANSLATION_METRES = 0.02
ROBOT_POSE_NORMALIZED_BASIS = "diagnostic_robot_pose_normalized_curriculum"
ROBOT_POSE_NORMALIZED_SCHEMA_VERSION = "missionos.libero_robot_pose_normalized_fixture.v1"
ENVIRONMENT_SEED = 0
PROCESS_SEED = 195
OFFICIAL_STABILIZATION_STEPS = 10
ACTION_DIMENSION = 7
MODEL_CONFIG = "cosmos_predict2_2b_480p_libero__inference_only"
UPSTREAM_CONFIG_BASE_CHECKPOINT_URI = (
    "hf://nvidia/Cosmos-Predict2-2B-Video2World/model-480p-16fps.pt"
)
UPSTREAM_TOKENIZER_URI = "hf://nvidia/Cosmos-Predict2-2B-Video2World/tokenizer/tokenizer.pth"
UPSTREAM_TOKENIZER_REPOSITORY = "nvidia/Cosmos-Predict2-2B-Video2World"
UPSTREAM_TOKENIZER_REVISION = "f50c09f5d8ab133a90cac3f4886a6471e9ba3f18"
UPSTREAM_TOKENIZER_SIZE_BYTES = 507_609_880
UPSTREAM_TOKENIZER_SHA256 = "38071ab59bd94681c686fa51d75a1968f64e470262043be31f7a094e442fd981"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_revision(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _verify_checkpoint(checkpoint_path: Path) -> dict[str, Any]:
    files = {}
    for relative_path, (expected_size, expected_sha256) in COSMOS_POLICY_CHECKPOINT_FILES.items():
        path = checkpoint_path / relative_path
        if not path.is_file():
            raise RuntimeError(f"cosmos_policy_checkpoint_file_missing:{relative_path}")
        observed_size = path.stat().st_size
        observed_sha256 = _sha256_path(path)
        if observed_size != expected_size or observed_sha256 != expected_sha256:
            raise RuntimeError(f"cosmos_policy_checkpoint_file_mismatch:{relative_path}")
        files[relative_path] = {
            "size_bytes": observed_size,
            "sha256": observed_sha256,
        }
    return {
        "repository": COSMOS_POLICY_REPOSITORY,
        "revision": COSMOS_POLICY_CHECKPOINT_REVISION,
        "additional_training_performed": False,
        "files": files,
    }


def _verify_tokenizer(tokenizer_path: Path) -> dict[str, Any]:
    if not tokenizer_path.is_file():
        raise RuntimeError("cosmos_policy_gated_tokenizer_file_missing")
    observed_size = tokenizer_path.stat().st_size
    observed_sha256 = _sha256_path(tokenizer_path)
    if (
        observed_size != UPSTREAM_TOKENIZER_SIZE_BYTES
        or observed_sha256 != UPSTREAM_TOKENIZER_SHA256
    ):
        raise RuntimeError("cosmos_policy_gated_tokenizer_file_mismatch")
    return {
        "repository": UPSTREAM_TOKENIZER_REPOSITORY,
        "revision": UPSTREAM_TOKENIZER_REVISION,
        "relative_path": "tokenizer/tokenizer.pth",
        "size_bytes": observed_size,
        "sha256": observed_sha256,
        "access_requirement": "upstream_gated_model_access",
        "additional_training_performed": False,
    }


def _verify_oracle_recoverability_report(
    *,
    report_path: Path,
    snapshot_path: Path,
) -> dict[str, Any]:
    """Require diagnostic 7D recoverability before paid policy inference."""

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("cosmos_policy_oracle_gate_report_unreadable") from error
    if not isinstance(report, dict):
        raise RuntimeError("cosmos_policy_oracle_gate_report_not_mapping")
    supplied_digest = report.get("result_sha256")
    report_without_digest = {key: value for key, value in report.items() if key != "result_sha256"}
    if supplied_digest != canonical_sha256(report_without_digest):
        raise RuntimeError("cosmos_policy_oracle_gate_report_digest_mismatch")

    snapshot_sha256 = _sha256_path(snapshot_path)
    if report.get("snapshot_sha256") != snapshot_sha256:
        raise RuntimeError("cosmos_policy_oracle_gate_snapshot_mismatch")
    if report.get("status") not in {
        "scripted_7d_recoverability_observed",
        "scripted_oracle_recoverability_established",
    }:
        raise RuntimeError("cosmos_policy_oracle_gate_recoverability_not_observed")
    if report.get("stable_success_observed") is not True:
        raise RuntimeError("cosmos_policy_oracle_gate_stable_success_not_observed")
    stable_steps = report.get(
        "stable_success_steps_completed",
        report.get("stable_success_steps"),
    )
    if isinstance(stable_steps, bool) or not isinstance(stable_steps, int) or stable_steps < 20:
        raise RuntimeError("cosmos_policy_oracle_gate_settle_steps_insufficient")
    if report.get("terminal_goal_predicate_vector") != [True, True, True]:
        raise RuntimeError("cosmos_policy_oracle_gate_terminal_predicates_unsatisfied")
    if report.get("preservation_violation_observed") is not False:
        raise RuntimeError("cosmos_policy_oracle_gate_preservation_not_established")
    claim_boundary = report.get("claim_boundary")
    if not isinstance(claim_boundary, dict):
        raise RuntimeError("cosmos_policy_oracle_gate_claim_boundary_missing")
    same_action_interface = bool(
        claim_boundary.get("same_7d_simulator_action_interface_used") is True
        or claim_boundary.get("same_original_7d_action_interface_used") is True
    )
    if not same_action_interface:
        raise RuntimeError("cosmos_policy_oracle_gate_action_interface_mismatch")
    if claim_boundary.get("model_inference_invoked") is not False:
        raise RuntimeError("cosmos_policy_oracle_gate_model_authority_invalid")
    if claim_boundary.get("physical_execution_invoked") is not False:
        raise RuntimeError("cosmos_policy_oracle_gate_physical_claim_invalid")

    return {
        "schema_version": "missionos.cosmos_policy_oracle_admission.v1",
        "authority": "diagnostic_only",
        "may_establish_model_repair_success": False,
        "report_sha256": supplied_digest,
        "report_schema_version": report.get("schema_version"),
        "snapshot_sha256": snapshot_sha256,
        "stable_success_steps": stable_steps,
        "terminal_goal_predicate_vector": [True, True, True],
        "preservation_violation_observed": False,
        "same_7d_simulator_action_interface_used": True,
    }


def _validate_repair_fixture_snapshot(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Admit either the original fixed fixture or a bounded curriculum fixture."""

    if metadata.get("source_failure_basis") == SCRIPTED_FAILURE_FIXTURE_BASIS:
        fixture = _validate_scripted_fixture_snapshot(
            metadata=metadata,
            scenario="displaced_from_stove",
        )
        return {
            "fixture_family": "scripted_displaced_from_stove",
            "fixture_contract": failure_fixture_contract("displaced_from_stove"),
            "fixture_observation": fixture,
        }

    if metadata.get("source_failure_basis") == ROBOT_POSE_NORMALIZED_BASIS:
        normalization = metadata.get("robot_pose_normalization")
        base_fixture = metadata.get("base_displacement_curriculum_fixture")
        if not isinstance(normalization, Mapping) or not isinstance(base_fixture, Mapping):
            raise RuntimeError("cosmos_policy_robot_pose_fixture_material_missing")
        normalization_material = deepcopy(dict(normalization))
        base_material = deepcopy(dict(base_fixture))
        if metadata.get("robot_pose_normalization_sha256") != canonical_sha256(
            normalization_material
        ) or metadata.get("base_displacement_curriculum_fixture_sha256") != canonical_sha256(
            base_material
        ):
            raise RuntimeError("cosmos_policy_robot_pose_fixture_digest_mismatch")
        trace = normalization_material.get("fixture_settle_trace")
        if (
            normalization_material.get("schema_version") != ROBOT_POSE_NORMALIZED_SCHEMA_VERSION
            or normalization_material.get("authority") != "diagnostic_fixture_only"
            or normalization_material.get("robot_pose_changed") is not True
            or normalization_material.get("terminal_goal_predicate_vector")
            != EXPECTED_REPAIR_SOURCE_VECTOR
            or not isinstance(trace, list)
            or normalization_material.get("fixture_settle_steps_applied") != len(trace)
            or len(trace) < 10
            or any(
                not isinstance(item, Mapping)
                or item.get("predicate_vector") != EXPECTED_REPAIR_SOURCE_VECTOR
                for item in trace
            )
            or normalization_material.get("simulator_state_directly_changed_for_diagnostic")
            is not True
            or normalization_material.get("model_inference_invoked") is not False
            or normalization_material.get("repair_attempted") is not False
            or normalization_material.get("physical_execution_invoked") is not False
            or metadata.get("source_goal_predicate_vector") != EXPECTED_REPAIR_SOURCE_VECTOR
        ):
            raise RuntimeError("cosmos_policy_robot_pose_fixture_contract_invalid")
        maximum_displacement = normalization_material.get("maximum_object_displacement_metres")
        if (
            isinstance(maximum_displacement, bool)
            or not isinstance(maximum_displacement, (int, float))
            or not 0.0 <= float(maximum_displacement) <= 0.001
        ):
            raise RuntimeError("cosmos_policy_robot_pose_fixture_object_motion_invalid")
        contract = {
            "schema_version": ROBOT_POSE_NORMALIZED_SCHEMA_VERSION,
            "authority": "diagnostic_fixture_only",
            "source_failure_basis": ROBOT_POSE_NORMALIZED_BASIS,
            "normalization_sha256": canonical_sha256(normalization_material),
            "base_fixture_sha256": canonical_sha256(base_material),
            "simulator_state_directly_changed_for_diagnostic": True,
            "repair_claim_eligible": False,
            "model_inference_invoked_for_fixture_creation": False,
            "physical_execution_invoked": False,
        }
        return {
            "fixture_family": "robot_pose_normalized_diagnostic_clone",
            "fixture_contract": contract,
            "fixture_observation": normalization_material,
        }

    if metadata.get("source_failure_basis") != DISPLACEMENT_CURRICULUM_BASIS:
        raise RuntimeError("cosmos_policy_repair_fixture_basis_mismatch")
    fixture = metadata.get("displacement_curriculum_fixture")
    if not isinstance(fixture, Mapping):
        raise RuntimeError("cosmos_policy_curriculum_fixture_material_missing")
    material = deepcopy(dict(fixture))
    if metadata.get("displacement_curriculum_fixture_sha256") != canonical_sha256(material):
        raise RuntimeError("cosmos_policy_curriculum_fixture_digest_mismatch")
    if (
        material.get("schema_version") != DISPLACEMENT_CURRICULUM_SCHEMA_VERSION
        or material.get("authority") != "diagnostic_fixture_only"
        or material.get("construction") != DISPLACEMENT_CURRICULUM_CONSTRUCTION
    ):
        raise RuntimeError("cosmos_policy_curriculum_fixture_contract_mismatch")
    requested_translation = material.get("requested_translation_from_source_metres")
    observed_translation = material.get("observed_translation_from_source_metres")
    if (
        isinstance(requested_translation, bool)
        or not isinstance(requested_translation, (int, float))
        or not 0.0 < float(requested_translation) <= MAXIMUM_CURRICULUM_TRANSLATION_METRES
        or isinstance(observed_translation, bool)
        or not isinstance(observed_translation, (int, float))
        or abs(float(observed_translation) - float(requested_translation)) > 0.01
    ):
        raise RuntimeError("cosmos_policy_curriculum_fixture_translation_invalid")
    protected_displacement = material.get("protected_object_displacement_metres")
    if (
        isinstance(protected_displacement, bool)
        or not isinstance(protected_displacement, (int, float))
        or not 0.0 <= float(protected_displacement) <= 0.005
    ):
        raise RuntimeError("cosmos_policy_curriculum_fixture_preservation_invalid")
    trace = material.get("fixture_settle_trace")
    if (
        not isinstance(trace, list)
        or material.get("fixture_settle_steps_applied") != len(trace)
        or len(trace) < 60
        or any(
            not isinstance(item, Mapping)
            or item.get("predicate_vector") != EXPECTED_REPAIR_SOURCE_VECTOR
            for item in trace
        )
    ):
        raise RuntimeError("cosmos_policy_curriculum_fixture_stability_invalid")
    if (
        material.get("terminal_goal_predicate_vector") != EXPECTED_REPAIR_SOURCE_VECTOR
        or material.get("actual_predicate_failure_observed") is not True
        or material.get("model_inference_invoked") is not False
        or material.get("repair_attempted") is not False
        or material.get("physical_execution_invoked") is not False
        or metadata.get("source_goal_predicate_vector") != EXPECTED_REPAIR_SOURCE_VECTOR
        or metadata.get("source_failure_is_repair_candidate") is not True
    ):
        raise RuntimeError("cosmos_policy_curriculum_fixture_claim_boundary_invalid")
    contract = {
        "schema_version": DISPLACEMENT_CURRICULUM_SCHEMA_VERSION,
        "authority": "diagnostic_fixture_only",
        "source_failure_basis": DISPLACEMENT_CURRICULUM_BASIS,
        "construction": DISPLACEMENT_CURRICULUM_CONSTRUCTION,
        "requested_translation_from_source_metres": float(requested_translation),
        "maximum_admitted_translation_metres": MAXIMUM_CURRICULUM_TRANSLATION_METRES,
        "fixture_sha256": canonical_sha256(material),
        "model_inference_invoked_for_fixture_creation": False,
        "physical_execution_invoked": False,
    }
    return {
        "fixture_family": "bounded_displacement_curriculum",
        "fixture_contract": contract,
        "fixture_observation": material,
    }


def _artifact_relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise RuntimeError("cosmos_policy_artifact_outside_output_root") from error


def _save_image(path: Path, image: Any) -> dict[str, Any]:
    import numpy as np
    from PIL import Image

    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] not in (3, 4):
        raise RuntimeError("cosmos_policy_artifact_image_shape_invalid")
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(array).save(path, format="PNG", optimize=False)
    return {
        "sha256": _sha256_path(path),
        "height_pixels": int(array.shape[0]),
        "width_pixels": int(array.shape[1]),
        "channels": int(array.shape[2]),
        "encoding": "png",
    }


def _save_actual_observation(
    *,
    observation: Mapping[str, Any],
    directory: Path,
    artifact_root: Path,
    step_number: int,
) -> dict[str, Any]:
    cameras = []
    for key in ("agentview_image", "robot0_eye_in_hand_image"):
        path = directory / f"step-{step_number:04d}-{key}.png"
        material = _save_image(path, observation[key])
        cameras.append(
            {
                "observation_key": key,
                "artifact_relative_path": _artifact_relative_path(path, artifact_root),
                **material,
            }
        )
    return {
        "schema_version": FRAME_CAPTURE_SCHEMA_VERSION,
        "authority": FRAME_CAPTURE_AUTHORITY,
        "source": "actual_libero_simulator_observation",
        "step_number": step_number,
        "cameras": cameras,
    }


def _save_future_prediction(
    *,
    predictions: Mapping[str, Any],
    value_prediction: float,
    directory: Path,
    artifact_root: Path,
    query_index: int,
    applied_action_start_index: int,
) -> dict[str, Any]:
    images = []
    for key in ("future_image", "future_wrist_image"):
        image = predictions.get(key)
        if image is None:
            continue
        path = directory / f"query-{query_index:04d}-{key}.png"
        material = _save_image(path, image)
        images.append(
            {
                "prediction_key": key,
                "artifact_relative_path": _artifact_relative_path(path, artifact_root),
                **material,
            }
        )
    if not images:
        raise RuntimeError("cosmos_policy_future_prediction_images_missing")
    return {
        "schema_version": "missionos.cosmos_policy_future_prediction.v1",
        "authority": "diagnostic_only",
        "may_establish_task_success": False,
        "query_index": query_index,
        "applied_action_start_index": applied_action_start_index,
        "value_prediction": float(value_prediction),
        "images": images,
    }


def _assert_disjoint_artifact_trees(
    *,
    future_prediction_dir: Path,
    actual_observation_dir: Path,
) -> None:
    future = future_prediction_dir.resolve()
    actual = actual_observation_dir.resolve()
    if future == actual or future in actual.parents or actual in future.parents:
        raise ValueError("cosmos_policy_prediction_observation_artifacts_not_disjoint")


def _action_command_statistics(
    action_trace: list[Mapping[str, Any]],
    *,
    chunk_size: int,
) -> dict[str, Any]:
    """Summarize policy commands without treating them as physical motion."""

    if not action_trace:
        raise ValueError("cosmos_policy_action_trace_empty")
    if isinstance(chunk_size, bool) or chunk_size <= 0:
        raise ValueError("cosmos_policy_action_statistics_chunk_size_invalid")

    actions: list[list[float]] = []
    for entry in action_trace:
        raw = entry.get("action_7d")
        if not isinstance(raw, list) or len(raw) != ACTION_DIMENSION:
            raise ValueError("cosmos_policy_action_statistics_action_invalid")
        action = [float(value) for value in raw]
        if not all(math.isfinite(value) for value in action):
            raise ValueError("cosmos_policy_action_statistics_action_non_finite")
        actions.append(action)

    translation_norms = [math.dist((0.0, 0.0, 0.0), action[:3]) for action in actions]
    gripper_commands = [action[-1] for action in actions]

    def sign(value: float) -> int:
        if value > 0.0:
            return 1
        if value < 0.0:
            return -1
        return 0

    gripper_signs = [sign(value) for value in gripper_commands]
    chunks = []
    for chunk_index, start in enumerate(range(0, len(actions), chunk_size)):
        chunk = actions[start : start + chunk_size]
        chunk_norms = translation_norms[start : start + len(chunk)]
        vector_sum = [sum(action[axis] for action in chunk) for axis in range(3)]
        chunks.append(
            {
                "chunk_index": chunk_index,
                "applied_action_start_index": start,
                "applied_action_count": len(chunk),
                "mean_xyz_command_norm": sum(chunk_norms) / len(chunk_norms),
                "sum_xyz_command_norm": sum(chunk_norms),
                "xyz_command_vector_sum_norm": math.dist((0.0, 0.0, 0.0), vector_sum),
            }
        )

    chunk_means = [chunk["mean_xyz_command_norm"] for chunk in chunks]
    return {
        "schema_version": "missionos.cosmos_policy_action_command_statistics.v1",
        "authority": "diagnostic_only",
        "sample_count": len(actions),
        "action_dimension": ACTION_DIMENSION,
        "command_space": "libero_env_step_action_input",
        "physical_path_length_established": False,
        "normalization_scale_verified": False,
        "null_action_threshold_defined": False,
        "xyz_command_norm": {
            "minimum": min(translation_norms),
            "maximum": max(translation_norms),
            "mean": sum(translation_norms) / len(translation_norms),
            "sum": sum(translation_norms),
        },
        "gripper_command": {
            "minimum": min(gripper_commands),
            "maximum": max(gripper_commands),
            "range": max(gripper_commands) - min(gripper_commands),
            "negative_count": sum(value < 0.0 for value in gripper_commands),
            "zero_count": sum(value == 0.0 for value in gripper_commands),
            "positive_count": sum(value > 0.0 for value in gripper_commands),
            "sign_transition_count": sum(
                current != previous
                for previous, current in zip(gripper_signs, gripper_signs[1:], strict=False)
            ),
        },
        "chunks": chunks,
        "chunk_mean_xyz_command_norm_strictly_nonincreasing": all(
            current <= previous
            for previous, current in zip(chunk_means, chunk_means[1:], strict=False)
        ),
        "first_to_last_chunk_mean_xyz_command_norm_ratio": (
            chunk_means[-1] / chunk_means[0] if chunk_means[0] != 0.0 else None
        ),
    }


def _build_model_runtime(
    *,
    source_root: Path,
    checkpoint_path: Path,
    tokenizer_path: Path,
    process_seed: int = PROCESS_SEED,
):
    if _git_revision(source_root) != COSMOS_POLICY_SOURCE_REVISION:
        raise RuntimeError("cosmos_policy_source_revision_mismatch")
    checkpoint_evidence = _verify_checkpoint(checkpoint_path)
    tokenizer_evidence = _verify_tokenizer(tokenizer_path)
    normalized_source = str(source_root)
    if normalized_source not in sys.path:
        sys.path.insert(0, normalized_source)
    os.chdir(source_root)
    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    os.environ.setdefault("MUJOCO_EGL_DEVICE_ID", "0")
    os.environ["DETERMINISTIC"] = "True"
    # The official cu128 uv environment installs NVRTC inside the Python
    # environment rather than /usr/local/cuda. Transformer Engine probes
    # CUDA_HOME before importing torch, so bind it to the verified wheel path.
    bundled_nvrtc_root = (
        Path(sys.prefix) / "lib" / "python3.10" / "site-packages" / "nvidia" / "cuda_nvrtc"
    )
    if "CUDA_HOME" not in os.environ and bundled_nvrtc_root.is_dir():
        os.environ["CUDA_HOME"] = str(bundled_nvrtc_root)

    import numpy as np
    import torch

    from cosmos_policy.experiments.robot.cosmos_utils import (
        get_model,
        init_t5_text_embeddings_cache,
        load_dataset_stats,
    )
    from cosmos_policy.experiments.robot.libero.run_libero_eval import PolicyEvalConfig
    from cosmos_policy.utils.utils import set_seed_everywhere

    if not torch.cuda.is_available():
        raise RuntimeError("cosmos_policy_cuda_required")
    random.seed(process_seed)
    np.random.seed(process_seed)
    torch.manual_seed(process_seed)
    torch.cuda.manual_seed_all(process_seed)
    set_seed_everywhere(process_seed)

    verified_model_path = checkpoint_path / "Cosmos-Policy-LIBERO-Predict2-2B.pt"
    cfg = PolicyEvalConfig(
        config=MODEL_CONFIG,
        ckpt_path=str(verified_model_path),
        # Upstream converts this filesystem-style value into a module name;
        # an absolute path would become an invalid leading-dot import.
        config_file="cosmos_policy/config/config.py",
        use_wrist_image=True,
        use_proprio=True,
        normalize_proprio=True,
        unnormalize_actions=True,
        dataset_stats_path=str(checkpoint_path / "libero_dataset_statistics.json"),
        t5_text_embeddings_path=str(checkpoint_path / "libero_t5_embeddings.pkl"),
        trained_with_image_aug=True,
        chunk_size=COSMOS_POLICY_LIBERO_ACTION_STEPS,
        num_open_loop_steps=COSMOS_POLICY_LIBERO_ACTION_STEPS,
        task_suite_name=TASK_SUITE,
        randomize_seed=False,
        seed=process_seed,
        use_variance_scale=False,
        deterministic=True,
        ar_future_prediction=False,
        ar_value_prediction=False,
        use_jpeg_compression=True,
        flip_images=True,
        num_denoising_steps_action=5,
        num_denoising_steps_future_state=1,
        num_denoising_steps_value=1,
    )
    init_t5_text_embeddings_cache(cfg.t5_text_embeddings_path)
    dataset_stats = load_dataset_stats(cfg.dataset_stats_path)
    # The public inference config eagerly resolves its gated base-model URI
    # while being imported, even though load_model_from_checkpoint immediately
    # overrides that default with cfg.ckpt_path. Resolve only that unused
    # default to the already verified complete Policy checkpoint. The actual
    # loader then reads cfg.ckpt_path, whose digest is bound above.
    from cosmos_policy._src.imaginaire.utils import checkpoint_db

    original_get_checkpoint_by_hf = checkpoint_db.get_checkpoint_by_hf

    def resolve_unused_config_default(checkpoint_uri: str) -> str:
        if checkpoint_uri == UPSTREAM_CONFIG_BASE_CHECKPOINT_URI:
            return str(verified_model_path)
        if checkpoint_uri == UPSTREAM_TOKENIZER_URI:
            return str(tokenizer_path)
        return original_get_checkpoint_by_hf(checkpoint_uri)

    checkpoint_db.get_checkpoint_by_hf = resolve_unused_config_default
    try:
        model, cosmos_config = get_model(cfg)
    finally:
        checkpoint_db.get_checkpoint_by_hf = original_get_checkpoint_by_hf
    trained_chunk_size = int(cosmos_config.dataloader_train.dataset.chunk_size)
    if trained_chunk_size != COSMOS_POLICY_LIBERO_ACTION_STEPS:
        raise RuntimeError("cosmos_policy_checkpoint_chunk_size_mismatch")
    checkpoint_evidence["runtime_config_locator"] = {
        "upstream_eager_default_uri": UPSTREAM_CONFIG_BASE_CHECKPOINT_URI,
        "resolved_to_verified_policy_checkpoint": True,
        "resolved_file_sha256": _sha256_path(verified_model_path),
        "loader_overrides_config_default_with_cfg_ckpt_path": True,
        "gated_base_checkpoint_downloaded": False,
        "model_weights_source": "verified_public_policy_checkpoint",
        "tokenizer": tokenizer_evidence,
        "tokenizer_resolved_from_verified_local_file": True,
    }
    return cfg, model, dataset_stats, checkpoint_evidence


def _make_environment():
    from libero.libero import benchmark
    from cosmos_policy.experiments.robot.libero.libero_utils import get_libero_env

    task_suite = benchmark.get_benchmark_dict()[TASK_SUITE]()
    task = task_suite.get_task(TASK_ID)
    if task.name != TASK_NAME or task.language != TASK_INSTRUCTION:
        raise RuntimeError("cosmos_policy_libero_task_identity_mismatch")
    environment, instruction = get_libero_env(task, "cosmos", resolution=256)
    return environment, task_suite.get_task_init_states(TASK_ID), instruction


def _query_policy(*, cfg, model, dataset_stats, observation, instruction, seed):
    from cosmos_policy.experiments.robot.cosmos_utils import get_action
    from cosmos_policy.experiments.robot.libero.run_libero_eval import prepare_observation

    prepared = prepare_observation(observation, 224, cfg.flip_images)
    result = get_action(
        cfg,
        model,
        dataset_stats,
        prepared,
        instruction,
        seed=seed,
        randomize_seed=False,
        num_denoising_steps_action=cfg.num_denoising_steps_action,
        generate_future_state_and_value_in_parallel=True,
    )
    actions = result["actions"]
    if len(actions) != COSMOS_POLICY_LIBERO_ACTION_STEPS:
        raise RuntimeError("cosmos_policy_action_chunk_length_mismatch")
    return result, prepared


def _run_nominal(
    *,
    cfg,
    model,
    dataset_stats,
    checkpoint_evidence: Mapping[str, Any],
    source_root: Path,
    artifact_root: Path,
    maximum_actions: int,
    process_seed: int = PROCESS_SEED,
) -> dict[str, Any]:
    import numpy as np
    from cosmos_policy.experiments.robot.libero.libero_utils import get_libero_dummy_action

    phase_root = artifact_root / "nominal"
    future_dir = phase_root / "future_predictions"
    actual_dir = phase_root / "actual_observations"
    _assert_disjoint_artifact_trees(
        future_prediction_dir=future_dir,
        actual_observation_dir=actual_dir,
    )
    environment, init_states, instruction = _make_environment()
    actual_manifest = []
    future_manifest = []
    action_trace = []
    predicate_trace = []
    try:
        environment.reset()
        observation = environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        for _ in range(OFFICIAL_STABILIZATION_STEPS):
            observation, _, _, _ = environment.step(get_libero_dummy_action("cosmos"))
        actual_manifest.append(
            _save_actual_observation(
                observation=observation,
                directory=actual_dir,
                artifact_root=artifact_root,
                step_number=0,
            )
        )
        applied = 0
        query_index = 0
        success = bool(environment.check_success())
        while applied < maximum_actions and not success:
            result, _ = _query_policy(
                cfg=cfg,
                model=model,
                dataset_stats=dataset_stats,
                observation=observation,
                instruction=instruction,
                seed=process_seed,
            )
            future_manifest.append(
                _save_future_prediction(
                    predictions=result["future_image_predictions"],
                    value_prediction=result["value_prediction"],
                    directory=future_dir,
                    artifact_root=artifact_root,
                    query_index=query_index,
                    applied_action_start_index=applied,
                )
            )
            remaining = maximum_actions - applied
            for action in result["actions"][:remaining]:
                action_array = np.asarray(action, dtype=np.float32)
                if action_array.shape != (ACTION_DIMENSION,) or not np.all(
                    np.isfinite(action_array)
                ):
                    raise RuntimeError("cosmos_policy_nominal_action_invalid")
                observation, _, done, info = environment.step(action_array.tolist())
                applied += 1
                predicates = _predicate_material(environment)
                actual_success = bool(environment.check_success())
                conjunction = all(item["satisfied"] for item in predicates)
                if actual_success is not conjunction:
                    raise RuntimeError("cosmos_policy_nominal_predicate_mismatch")
                action_trace.append(
                    {
                        "applied_action_index": applied - 1,
                        "action_7d": [float(value) for value in action_array.tolist()],
                        "action_sha256": digest_runtime_material(
                            "cosmos_policy_nominal_action", action_array
                        ),
                    }
                )
                predicate_trace.append(
                    {
                        "applied_action_count": applied,
                        "goal_predicate_observations": predicates,
                        "official_predicate_result": actual_success,
                    }
                )
                actual_manifest.append(
                    _save_actual_observation(
                        observation=observation,
                        directory=actual_dir,
                        artifact_root=artifact_root,
                        step_number=applied,
                    )
                )
                success = actual_success
                if success:
                    break
                if done or info.get("done", False):
                    raise RuntimeError("cosmos_policy_nominal_done_without_predicate_success")
            query_index += 1
        report_without_digest = {
            "schema_version": "missionos.cosmos_policy_libero_nominal.v1",
            "status": "nominal_predicate_success_observed"
            if success
            else "nominal_budget_exhausted",
            "task_suite": TASK_SUITE,
            "task_name": TASK_NAME,
            "task_id": TASK_ID,
            "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
            "instruction": instruction,
            "environment_seed": ENVIRONMENT_SEED,
            "process_seed": process_seed,
            "official_stabilization_steps": OFFICIAL_STABILIZATION_STEPS,
            "maximum_applied_actions": maximum_actions,
            "applied_action_count": applied,
            "checkpoint": deepcopy(dict(checkpoint_evidence)),
            "source_revision": _git_revision(source_root),
            "action_trace": action_trace,
            "action_command_statistics": _action_command_statistics(
                action_trace,
                chunk_size=COSMOS_POLICY_LIBERO_ACTION_STEPS,
            ),
            "predicate_trace": predicate_trace,
            "future_prediction_manifest": future_manifest,
            "actual_observation_manifest": actual_manifest,
            "success_authority": "actual_libero_goal_predicates_only",
            "future_predictions_may_establish_success": False,
            "nominal_success_observed": success,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        }
        report = {
            **report_without_digest,
            "result_sha256": canonical_sha256(report_without_digest),
        }
        _write_json(phase_root / "report.json", report)
        return report
    finally:
        environment.close()


def _run_repair(
    *,
    cfg,
    model,
    dataset_stats,
    checkpoint_evidence: Mapping[str, Any],
    source_root: Path,
    snapshot_path: Path,
    artifact_root: Path,
    dispatch_state_path: Path,
    operator_approval_ref: str,
    maximum_actions: int,
    process_seed: int = PROCESS_SEED,
    repair_instruction_variant: str = "original_task",
) -> dict[str, Any]:
    import numpy as np

    phase_root = artifact_root / "repair"
    future_dir = phase_root / "future_predictions"
    actual_dir = phase_root / "actual_observations"
    _assert_disjoint_artifact_trees(
        future_prediction_dir=future_dir,
        actual_observation_dir=actual_dir,
    )
    environment, init_states, instruction = _make_environment()
    reset_count = 0
    future_manifest = []
    actual_manifest = []
    raw_action_trace = []
    try:
        environment.reset()
        reset_count += 1
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        simulator_state, snapshot_metadata = _read_failure_snapshot(snapshot_path)
        if snapshot_metadata.get("task_suite") != TASK_SUITE:
            raise RuntimeError("cosmos_policy_snapshot_task_suite_mismatch")
        if snapshot_metadata.get("task_id") != TASK_ID:
            raise RuntimeError("cosmos_policy_snapshot_task_id_mismatch")
        if snapshot_metadata.get("episode_init_state_index") != EPISODE_INIT_STATE_INDEX:
            raise RuntimeError("cosmos_policy_snapshot_init_state_mismatch")
        fixture_admission = _validate_repair_fixture_snapshot(snapshot_metadata)
        fixture_material = fixture_admission["fixture_observation"]
        fixture_repair_claim_eligible = (
            fixture_admission["fixture_family"] != "robot_pose_normalized_diagnostic_clone"
        )
        observation = environment.regenerate_obs_from_state(simulator_state)
        restored_state = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        if hashlib.sha256(restored_state.tobytes()).hexdigest() != snapshot_metadata.get(
            "simulator_state_sha256"
        ):
            raise RuntimeError("cosmos_policy_snapshot_restored_state_digest_mismatch")
        source_predicates = _predicate_material(environment)
        source_vector = [item["satisfied"] for item in source_predicates]
        if source_vector != EXPECTED_REPAIR_SOURCE_VECTOR:
            raise RuntimeError("cosmos_policy_snapshot_source_vector_mismatch")
        adapter = _environment_adapter(environment)
        source_object_poses = _object_poses(adapter)
        source_contract = {
            "task_suite": TASK_SUITE,
            "task_name": TASK_NAME,
            "task_id": TASK_ID,
            "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
            "source_instruction": instruction,
            "environment_seed": ENVIRONMENT_SEED,
            "process_seed": process_seed,
            "source_goal_predicate_vector": source_vector,
            "source_failure_basis": SCRIPTED_FAILURE_FIXTURE_BASIS,
            "setup_snapshot_sha256": snapshot_metadata["snapshot_artifact_sha256"],
            "repair_fixture_family": fixture_admission["fixture_family"],
            "repair_fixture_contract": fixture_admission["fixture_contract"],
            "repair_fixture_claim_eligible": fixture_repair_claim_eligible,
            "scripted_failure_fixture_observation": fixture_material,
            "fixture_setup_precedes_repair_proposal": True,
            "natural_policy_failure_observed": False,
            "maximum_applied_actions": maximum_actions,
            "model_action_chunk_size": COSMOS_POLICY_LIBERO_ACTION_STEPS,
            "final_chunk_maximum_actions": maximum_actions % COSMOS_POLICY_LIBERO_ACTION_STEPS
            or COSMOS_POLICY_LIBERO_ACTION_STEPS,
            "checkpoint": deepcopy(dict(checkpoint_evidence)),
            "source_revision": _git_revision(source_root),
            "additional_training_performed": False,
            "success_authority": "actual_libero_goal_predicates_only",
            "future_predictions_may_establish_success": False,
        }
        source_contract_sha256 = canonical_sha256(source_contract)
        proposal = build_cosmos_policy_same_world_repair_proposal(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            environment_session_id=f"cosmos-policy-libero10-fixture-world:{uuid4()}",
            source_contract_sha256=source_contract_sha256,
            source_goal_predicates=source_predicates,
            reset_count=reset_count,
            maximum_repair_steps=maximum_actions,
            source_object_poses=source_object_poses,
            repair_instruction_variant=repair_instruction_variant,
            state_continuity_basis=STATE_CONTINUITY_LIVE_SAME_WORLD,
        )
        approval = approve_same_world_repair(
            proposal=proposal,
            operator_approval_ref=operator_approval_ref,
        )
        dispatch = build_same_world_repair_dispatch(
            proposal=proposal,
            approval=approval,
            dispatch_ref=f"cosmos-policy-libero-repair:{uuid4()}",
        )
        actual_manifest.append(
            _save_actual_observation(
                observation=observation,
                directory=actual_dir,
                artifact_root=artifact_root,
                step_number=0,
            )
        )
        previous_positions = {
            name: np.asarray(position, dtype=np.float64)
            for name, position in source_object_poses.items()
        }
        applied_action_count = 0

        def invoke_model(raw_observation, repair_instruction, chunk_index):
            nonlocal applied_action_count
            instruction_evidence = verify_exact_repair_instruction_payload(
                payload=[repair_instruction],
                expected_instruction=repair_instruction,
            )
            request_sha256 = digest_runtime_material(
                "cosmos_policy_request",
                {
                    "observation": raw_observation,
                    "instruction": repair_instruction,
                    "chunk_index": chunk_index,
                },
            )
            result, _ = _query_policy(
                cfg=cfg,
                model=model,
                dataset_stats=dataset_stats,
                observation=raw_observation,
                instruction=repair_instruction,
                seed=process_seed,
            )
            future_manifest.append(
                _save_future_prediction(
                    predictions=result["future_image_predictions"],
                    value_prediction=result["value_prediction"],
                    directory=future_dir,
                    artifact_root=artifact_root,
                    query_index=chunk_index,
                    applied_action_start_index=applied_action_count,
                )
            )
            response_sha256 = digest_runtime_material(
                "cosmos_policy_response",
                {
                    "actions": result["actions"],
                    "future_image_predictions": result["future_image_predictions"],
                    "value_prediction": result["value_prediction"],
                },
            )
            return result["actions"], {
                "model_runtime_invoked": True,
                "policy_request_sha256": request_sha256,
                "policy_response_sha256": response_sha256,
                "repair_instruction_sha256": proposal["repair_instruction_sha256"],
                **instruction_evidence,
            }

        def apply_action_chunk(action_chunk, chunk_index):
            nonlocal observation, applied_action_count
            remaining = maximum_actions - applied_action_count
            admitted = min(COSMOS_POLICY_LIBERO_ACTION_STEPS, remaining)
            trace = []
            before = digest_runtime_material("cosmos_policy_observation", observation)
            for action_step_index, raw_action in enumerate(action_chunk[:admitted]):
                action = np.asarray(raw_action, dtype=np.float32)
                if action.shape != (ACTION_DIMENSION,) or not np.all(np.isfinite(action)):
                    raise RuntimeError("cosmos_policy_repair_action_invalid")
                observation, _, done, info = environment.step(action.tolist())
                applied_action_count += 1
                predicates = _predicate_material(environment)
                official_result = bool(environment.check_success())
                conjunction = all(item["satisfied"] for item in predicates)
                if official_result is not conjunction:
                    raise RuntimeError("cosmos_policy_repair_predicate_mismatch")
                witnesses = _object_witnesses(
                    adapter,
                    {"robot_state": {"eef": {"pos": observation["robot0_eef_pos"]}}},
                    previous_positions,
                )
                action_sha256 = digest_runtime_material("cosmos_policy_selected_action", action)
                raw_action_trace.append(
                    {
                        "global_repair_step_index": applied_action_count - 1,
                        "action_7d": [float(value) for value in action.tolist()],
                        "action_step_sha256": action_sha256,
                    }
                )
                actual_capture = _save_actual_observation(
                    observation=observation,
                    directory=actual_dir,
                    artifact_root=artifact_root,
                    step_number=applied_action_count,
                )
                actual_manifest.append(actual_capture)
                trace.append(
                    {
                        "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
                        "chunk_index": chunk_index,
                        "action_step_index": action_step_index,
                        "action_step_number": action_step_index + 1,
                        "global_repair_step_index": applied_action_count - 1,
                        "global_repair_step_number": applied_action_count,
                        "action_step_sha256": action_sha256,
                        "goal_predicate_observations": deepcopy(predicates),
                        "goal_predicate_vector_sha256": canonical_sha256(
                            {"goal_predicate_observations": predicates}
                        ),
                        "official_predicate_conjunction": conjunction,
                        "official_predicate_result": official_result,
                        "conjunction_matches_official_result": True,
                        "object_witnesses": witnesses,
                        "frame_capture": actual_capture,
                    }
                )
                if official_result:
                    break
                if done or info.get("done", False):
                    raise RuntimeError("cosmos_policy_repair_done_without_predicate_success")
            after = digest_runtime_material("cosmos_policy_observation", observation)
            final_official = bool(trace[-1]["official_predicate_result"])
            return observation, {
                "simulator_step_return_observed": True,
                "simulator_effect_observed": before != after,
                "official_predicate_result": final_official,
                "stopped_early_on_official_success": final_official and len(trace) < admitted,
                "action_chunk_sha256": digest_runtime_material(
                    "cosmos_policy_applied_action_chunk",
                    action_chunk[: len(trace)],
                ),
                "preservation_step_trace": trace,
            }

        repair_result = run_cosmos_policy_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=DispatchAuthorityTable(dispatch_state_path),
            initial_observation=observation,
            invoke_model=invoke_model,
            apply_action_chunk=apply_action_chunk,
            observe_goal_predicates=lambda: _predicate_material(environment),
            observed_reset_count=lambda: reset_count,
        )
        final_vector = [
            item["satisfied"] for item in repair_result["final_goal_predicate_observations"]
        ]
        actual_predicate_recovery_observed = bool(
            repair_result["status"] == "satisfied"
            and repair_result["predicate_improvement_observed"] is True
            and final_vector == [True, True, True]
            and repair_result["first_preservation_violation"] is None
            and repair_result["first_preservation_invariant_breach"] is None
        )
        repair_observed = bool(actual_predicate_recovery_observed and fixture_repair_claim_eligible)
        report_without_digest = {
            "schema_version": "missionos.cosmos_policy_libero_fixture_repair.v1",
            "status": (
                "scripted_fixture_repair_observed"
                if repair_observed
                else "scripted_fixture_repair_not_observed"
            ),
            "source_contract": source_contract,
            "source_contract_sha256": source_contract_sha256,
            "source_goal_predicate_vector": source_vector,
            "final_goal_predicate_vector": final_vector,
            "proposal": proposal,
            "approval": approval,
            "dispatch": dispatch,
            "repair_result": repair_result,
            "scripted_fixture_repair_established": repair_observed,
            "actual_predicate_recovery_observed": actual_predicate_recovery_observed,
            "repair_claim_eligible": fixture_repair_claim_eligible,
            "future_prediction_manifest": future_manifest,
            "actual_observation_manifest": actual_manifest,
            "raw_action_trace": raw_action_trace,
            "raw_action_trace_sha256": canonical_sha256({"raw_action_trace": raw_action_trace}),
            "action_command_statistics": _action_command_statistics(
                raw_action_trace,
                chunk_size=COSMOS_POLICY_LIBERO_ACTION_STEPS,
            ),
            "success_authority": "actual_libero_goal_predicates_only",
            "future_predictions_may_establish_success": False,
            "semantic_repair_established": repair_observed,
            "general_recovery_rate_established": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        }
        report = {
            **report_without_digest,
            "result_sha256": canonical_sha256(report_without_digest),
        }
        _write_json(phase_root / "report.json", report)
        return report
    finally:
        environment.close()


def execute_live(
    *,
    source_root: Path,
    checkpoint_path: Path,
    tokenizer_path: Path,
    snapshot_path: Path,
    oracle_recoverability_report_path: Path,
    output_dir: Path,
    dispatch_state_path: Path,
    operator_approval_ref: str,
    maximum_actions: int = 520,
    process_seed: int = PROCESS_SEED,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("cosmos_policy_libero_live_opt_in_required")
    if maximum_actions != 520:
        raise ValueError("cosmos_policy_comparison_requires_520_applied_actions")
    if output_dir.exists():
        raise ValueError("cosmos_policy_output_directory_already_exists")
    oracle_admission = _verify_oracle_recoverability_report(
        report_path=oracle_recoverability_report_path,
        snapshot_path=snapshot_path,
    )
    output_dir.mkdir(parents=True)
    cfg, model, dataset_stats, checkpoint_evidence = _build_model_runtime(
        source_root=source_root,
        checkpoint_path=checkpoint_path,
        tokenizer_path=tokenizer_path,
        process_seed=process_seed,
    )
    nominal = _run_nominal(
        cfg=cfg,
        model=model,
        dataset_stats=dataset_stats,
        checkpoint_evidence=checkpoint_evidence,
        source_root=source_root,
        artifact_root=output_dir,
        maximum_actions=maximum_actions,
        process_seed=process_seed,
    )
    if nominal["nominal_success_observed"] is not True:
        experiment = {
            "schema_version": "missionos.cosmos_policy_libero_experiment.v1",
            "status": "stopped_after_nominal_baseline_failure",
            "nominal": nominal,
            "repair": None,
            "repair_started": False,
            "claim_boundary": {
                "oracle_admission": oracle_admission,
                "nothing_measured_for_repair": True,
                "future_predictions_may_establish_success": False,
                "physical_execution_invoked": False,
            },
        }
    else:
        repair = _run_repair(
            cfg=cfg,
            model=model,
            dataset_stats=dataset_stats,
            checkpoint_evidence=checkpoint_evidence,
            source_root=source_root,
            snapshot_path=snapshot_path,
            artifact_root=output_dir,
            dispatch_state_path=dispatch_state_path,
            operator_approval_ref=operator_approval_ref,
            maximum_actions=maximum_actions,
            process_seed=process_seed,
        )
        experiment = {
            "schema_version": "missionos.cosmos_policy_libero_experiment.v1",
            "status": "nominal_and_repair_completed",
            "nominal": nominal,
            "repair": repair,
            "repair_started": True,
            "claim_boundary": {
                "oracle_admission": oracle_admission,
                "nominal_and_repair_use_same_loaded_model": True,
                "same_task_and_init_state": True,
                "same_maximum_applied_action_budget": maximum_actions,
                "future_and_actual_artifact_trees_separate": True,
                "success_authority": "actual_libero_goal_predicates_only",
                "future_predictions_may_establish_success": False,
                "additional_training_performed": False,
                "general_recovery_rate_established": False,
                "physical_execution_invoked": False,
            },
        }
    result = {**experiment, "result_sha256": canonical_sha256(experiment)}
    _write_json(output_dir / "experiment.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--checkpoint-path", type=Path, required=True)
    parser.add_argument("--tokenizer-path", type=Path, required=True)
    parser.add_argument("--restore-snapshot", type=Path, required=True)
    parser.add_argument("--oracle-recoverability-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dispatch-state", type=Path, required=True)
    parser.add_argument("--operator-approval-ref", required=True)
    parser.add_argument("--maximum-actions", type=int, default=520)
    parser.add_argument("--process-seed", type=int, default=PROCESS_SEED)
    args = parser.parse_args()
    result = execute_live(
        source_root=args.source_root.resolve(),
        checkpoint_path=args.checkpoint_path.resolve(),
        tokenizer_path=args.tokenizer_path.resolve(),
        snapshot_path=args.restore_snapshot.resolve(),
        oracle_recoverability_report_path=args.oracle_recoverability_report.resolve(),
        output_dir=args.output_dir.resolve(),
        dispatch_state_path=args.dispatch_state.resolve(),
        operator_approval_ref=args.operator_approval_ref,
        maximum_actions=args.maximum_actions,
        process_seed=args.process_seed,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] == "stopped_after_nominal_baseline_failure":
        return 3
    return 0 if result["repair"]["scripted_fixture_repair_established"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

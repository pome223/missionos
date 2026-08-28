#!/usr/bin/env python3
"""Compare an exact LIBERO goal state and fixture at the Cosmos policy input.

This is a rejection-only perceptibility probe.  A positive image difference does
not establish grounding or Repair competence; an absent robust difference means
the fixture is unsuitable for a policy competence claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_POLICY_INPUT_VISIBILITY"
TASK_NAME = "KITCHEN_SCENE8_put_both_moka_pots_on_the_stove"
TASK_SUITE = "libero_10"
TASK_ID = 8
EPISODE_INIT_STATE_INDEX = 15
ENVIRONMENT_SEED = 0
TARGET_OBJECT = "moka_pot_2"
ROBUST_CHANGED_PIXEL_MINIMUM = 16
RESTORE_MAXIMUM_ABSOLUTE_ERROR = 1e-12
FIXTURE_SCHEMA_VERSION = "missionos.libero_displacement_curriculum_fixture.v2"
NOOP_STEPS = 60
NOOP_TARGET_MAXIMUM_DRIFT_METRES = 0.001
PROTECTED_MAXIMUM_DRIFT_METRES = 0.005


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_snapshot(path: Path) -> tuple[Any, dict[str, Any]]:
    import numpy as np

    with np.load(path, allow_pickle=False) as archive:
        state = np.asarray(archive["simulator_state"], dtype=np.float64).reshape(-1)
        metadata = json.loads(str(archive["metadata_json"].item()))
    if metadata.get("simulator_state_sha256") != hashlib.sha256(state.tobytes()).hexdigest():
        raise RuntimeError("libero_visibility_snapshot_state_digest_mismatch")
    return state, metadata


def _make_environment() -> tuple[Any, Any]:
    from libero.libero import benchmark, get_libero_path
    from libero.libero.envs import OffScreenRenderEnv

    suite = benchmark.get_benchmark_dict()[TASK_SUITE]()
    task = suite.get_task(TASK_ID)
    if task.name != TASK_NAME:
        raise RuntimeError("libero_visibility_task_identity_mismatch")
    environment = OffScreenRenderEnv(
        bddl_file_name=os.path.join(
            get_libero_path("bddl_files"), task.problem_folder, task.bddl_file
        ),
        camera_heights=256,
        camera_widths=256,
        camera_depths=True,
    )
    environment.seed(ENVIRONMENT_SEED)
    return environment, suite.get_task_init_states(TASK_ID)


def _camera_metrics(source: Any, fixture: Any, jpeg_noise: Any) -> dict[str, Any]:
    import numpy as np

    absolute = np.abs(
        np.asarray(source, dtype=np.int16) - np.asarray(fixture, dtype=np.int16)
    )
    pixel_max = absolute.max(axis=2)
    noise_pixel_max = np.asarray(jpeg_noise, dtype=np.float64).max(axis=2)
    threshold = max(1.0, float(np.percentile(noise_pixel_max, 95)))
    robust = pixel_max > threshold
    coordinates = np.argwhere(robust)
    bbox = None
    if coordinates.size:
        y_min, x_min = coordinates.min(axis=0)
        y_max, x_max = coordinates.max(axis=0)
        bbox = {
            "x_min": int(x_min),
            "y_min": int(y_min),
            "x_max": int(x_max),
            "y_max": int(y_max),
            "width_pixels": int(x_max - x_min + 1),
            "height_pixels": int(y_max - y_min + 1),
        }
    return {
        "height_pixels": int(absolute.shape[0]),
        "width_pixels": int(absolute.shape[1]),
        "mean_absolute_channel_difference": float(absolute.mean()),
        "maximum_channel_difference": int(absolute.max()),
        "jpeg_noise_p95_pixel_threshold": threshold,
        "robust_changed_pixel_count": int(robust.sum()),
        "robust_changed_pixel_fraction": float(robust.mean()),
        "robust_difference_bounding_box": bbox,
        "rejection_filter_passed": int(robust.sum()) >= ROBUST_CHANGED_PIXEL_MINIMUM,
    }


def _prepare_policy_image(image: Any, *, use_jpeg_compression: bool) -> Any:
    """Apply the pinned Cosmos LIBERO image path without importing model code."""

    import io
    import math
    import numpy as np
    from PIL import Image
    import torch
    import torchvision.transforms.functional as functional

    flipped = np.flipud(np.asarray(image, dtype=np.uint8))
    if use_jpeg_compression:
        buffer = io.BytesIO()
        Image.fromarray(flipped).save(buffer, format="JPEG", quality=95)
        buffer.seek(0)
        flipped = np.asarray(Image.open(buffer), dtype=np.uint8)
    resized = np.asarray(Image.fromarray(flipped).resize((224, 224)), dtype=np.uint8)
    tensor = torch.from_numpy(resized.copy()).permute(2, 0, 1)
    crop_size = int(224 * math.sqrt(0.9))
    cropped = functional.center_crop(tensor, crop_size)
    transformed = functional.resize(cropped, [224, 224], antialias=True)
    return transformed.permute(1, 2, 0).cpu().numpy().astype(np.uint8)


def execute_live(
    *, source_snapshot: Path, fixture_snapshot: Path, cosmos_source_root: Path, output_dir: Path
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("libero_visibility_opt_in_required")
    if output_dir.exists():
        raise ValueError("libero_visibility_output_exists")

    import numpy as np
    from PIL import Image

    source_state, source_metadata = _read_snapshot(source_snapshot)
    fixture_state, fixture_metadata = _read_snapshot(fixture_snapshot)
    if source_metadata.get("task_id") != TASK_ID or fixture_metadata.get("task_id") != TASK_ID:
        raise RuntimeError("libero_visibility_snapshot_task_mismatch")
    source_files = {
        "libero_observation": cosmos_source_root
        / "cosmos_policy/experiments/robot/libero/run_libero_eval.py",
        "libero_image_helpers": cosmos_source_root
        / "cosmos_policy/experiments/robot/libero/libero_utils.py",
        "model_image_pipeline": cosmos_source_root
        / "cosmos_policy/experiments/robot/cosmos_utils.py",
        "dataset_image_helpers": cosmos_source_root
        / "cosmos_policy/datasets/dataset_utils.py",
    }
    if any(not path.is_file() for path in source_files.values()):
        raise RuntimeError("libero_visibility_pinned_source_files_missing")
    fixture_material = fixture_metadata.get("displacement_curriculum_fixture")
    if not isinstance(fixture_material, dict):
        raise RuntimeError("libero_visibility_fixture_material_missing")
    if (
        fixture_material.get("schema_version") != FIXTURE_SCHEMA_VERSION
        or fixture_material.get("environment_seed") != ENVIRONMENT_SEED
        or fixture_metadata.get("environment_seed") != ENVIRONMENT_SEED
    ):
        raise RuntimeError("libero_visibility_fixture_environment_contract_invalid")
    paired_settle_steps = fixture_material.get("fixture_settle_steps_applied")
    if (
        isinstance(paired_settle_steps, bool)
        or not isinstance(paired_settle_steps, int)
        or paired_settle_steps < 1
    ):
        raise RuntimeError("libero_visibility_fixture_settle_steps_invalid")

    os.environ.setdefault("MUJOCO_GL", "osmesa")
    os.environ.setdefault("PYOPENGL_PLATFORM", "osmesa")
    environment, init_states = _make_environment()
    output_dir.mkdir(parents=True)
    try:
        observations = {}
        restore_checks = {}
        paired_state_checks = {}
        for label, state in (("source_goal", source_state), ("fixture", fixture_state)):
            environment.reset()
            environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
            observation = environment.regenerate_obs_from_state(state)
            restored = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
            difference = np.abs(restored - state)
            maximum_error = float(difference.max())
            if maximum_error > RESTORE_MAXIMUM_ABSOLUTE_ERROR:
                raise RuntimeError(
                    f"libero_visibility_{label}_restore_not_exact:"
                    f"max_abs={maximum_error}:"
                    f"changed={int(np.count_nonzero(difference))}"
                )
            restore_checks[label] = {
                "bitwise_equal": bool(np.array_equal(restored, state)),
                "maximum_absolute_error": maximum_error,
                "changed_value_count": int(np.count_nonzero(difference)),
                "maximum_admitted_absolute_error": RESTORE_MAXIMUM_ABSOLUTE_ERROR,
            }
            if label == "source_goal":
                for _ in range(paired_settle_steps):
                    observation, _, _, _ = environment.step(
                        np.zeros(7, dtype=np.float64).tolist()
                    )
            simulator = environment.env
            target_body = int(simulator.obj_body_id[TARGET_OBJECT])
            paired_state_checks[label] = {
                "target_position_metres": np.asarray(
                    simulator.sim.data.body_xpos[target_body], dtype=np.float64
                ).tolist(),
                "eef_position_metres": np.asarray(
                    observation["robot0_eef_pos"], dtype=np.float64
                ).tolist(),
            }
            observations[label] = {
                "primary_image": np.asarray(observation["agentview_image"], dtype=np.uint8),
                "wrist_image": np.asarray(
                    observation["robot0_eye_in_hand_image"], dtype=np.uint8
                ),
            }

        camera_results = {}
        for camera in ("primary_image", "wrist_image"):
            source_raw = np.asarray(observations["source_goal"][camera], dtype=np.uint8)
            fixture_raw = np.asarray(observations["fixture"][camera], dtype=np.uint8)
            source_processed = _prepare_policy_image(
                source_raw, use_jpeg_compression=True
            )
            fixture_processed = _prepare_policy_image(
                fixture_raw, use_jpeg_compression=True
            )
            source_no_jpeg = _prepare_policy_image(
                source_raw, use_jpeg_compression=False
            )
            fixture_no_jpeg = _prepare_policy_image(
                fixture_raw, use_jpeg_compression=False
            )
            jpeg_noise = np.maximum(
                np.abs(source_processed.astype(np.int16) - source_no_jpeg.astype(np.int16)),
                np.abs(fixture_processed.astype(np.int16) - fixture_no_jpeg.astype(np.int16)),
            )
            for label, pixels in (
                ("source-goal-policy-input", source_processed),
                ("fixture-policy-input", fixture_processed),
                ("absolute-difference", np.abs(source_processed.astype(np.int16) - fixture_processed.astype(np.int16)).astype(np.uint8)),
            ):
                Image.fromarray(pixels).save(output_dir / f"{camera}-{label}.png")
            camera_results[camera] = _camera_metrics(
                source_processed, fixture_processed, jpeg_noise
            )

        perceptibility = any(
            result["rejection_filter_passed"] for result in camera_results.values()
        )

        # A separate fresh restore verifies that a policy episode would not
        # inherit a fixture that moves under zero action. This is deliberately
        # independent from the matched-horizon visibility comparison above.
        # The fixture was freshly restored as the second paired condition and
        # has not been stepped yet. Reuse that exact environment; constructing
        # another EGL context can terminate this pinned runtime before report
        # publication.
        noop_observation = observation
        simulator = environment.env
        target_body = int(simulator.obj_body_id[TARGET_OBJECT])
        protected_body = int(simulator.obj_body_id["moka_pot_1"])
        initial_target = np.asarray(simulator.sim.data.body_xpos[target_body], dtype=np.float64).copy()
        initial_protected = np.asarray(simulator.sim.data.body_xpos[protected_body], dtype=np.float64).copy()
        noop_predicates = []
        for _ in range(NOOP_STEPS):
            noop_observation, _, _, _ = environment.step(np.zeros(7, dtype=np.float64).tolist())
            noop_predicates.append(
                [
                    bool(environment.env._eval_predicate(state))
                    for state in environment.env.parsed_problem["goal_state"]
                ]
            )
        terminal_target = np.asarray(simulator.sim.data.body_xpos[target_body], dtype=np.float64)
        terminal_protected = np.asarray(simulator.sim.data.body_xpos[protected_body], dtype=np.float64)
        target_drift = float(np.linalg.norm(terminal_target - initial_target))
        protected_drift = float(np.linalg.norm(terminal_protected - initial_protected))
        noop_stable = bool(
            target_drift <= NOOP_TARGET_MAXIMUM_DRIFT_METRES
            and protected_drift <= PROTECTED_MAXIMUM_DRIFT_METRES
            and all(vector == [True, False, True] for vector in noop_predicates)
        )
        report_without_digest = {
            "schema_version": "missionos.cosmos_policy_libero_input_visibility.v1",
            "status": (
                "policy_input_difference_observed_on_stable_fixture"
                if perceptibility and noop_stable
                else (
                    "fixture_not_stable_under_zero_action"
                    if not noop_stable
                    else "policy_input_difference_not_robustly_observed"
                )
            ),
            "source_snapshot_sha256": _sha256_path(source_snapshot),
            "fixture_snapshot_sha256": _sha256_path(fixture_snapshot),
            "cosmos_source_revision": os.environ.get("COSMOS_POLICY_SOURCE_REVISION"),
            "pinned_source_files": {
                label: {"sha256": _sha256_path(path)}
                for label, path in source_files.items()
            },
            "snapshot_restore_checks": restore_checks,
            "paired_control": {
                "source_goal_zero_actions_applied": paired_settle_steps,
                "fixture_construction_zero_actions_applied": paired_settle_steps,
                "comparison_after_matched_settle_horizon": True,
                "paired_state_checks": paired_state_checks,
            },
            "policy_input_pipeline": {
                "flip_images": True,
                "jpeg_quality": 95,
                "model_resolution_pixels": 224,
                "trained_with_image_augmentation": True,
                "center_crop_area_fraction": 0.9,
            },
            "rejection_filter": {
                "minimum_robust_changed_pixels": ROBUST_CHANGED_PIXEL_MINIMUM,
                "threshold_basis": "greater_than_camera_specific_p95_jpeg_processing_difference",
                "passed": perceptibility and noop_stable,
                "positive_result_establishes_competence": False,
            },
            "fixture_noop_stability": {
                "fresh_restore": True,
                "zero_actions_applied": NOOP_STEPS,
                "target_drift_metres": target_drift,
                "target_maximum_admitted_drift_metres": NOOP_TARGET_MAXIMUM_DRIFT_METRES,
                "protected_drift_metres": protected_drift,
                "protected_maximum_admitted_drift_metres": PROTECTED_MAXIMUM_DRIFT_METRES,
                "predicate_vector_stable": all(
                    vector == [True, False, True] for vector in noop_predicates
                ),
                "passed": noop_stable,
            },
            "cameras": camera_results,
            "claim_boundary": {
                "authority": "diagnostic_only",
                "model_inference_invoked": False,
                "semantic_grounding_established": False,
                "repair_competence_established": False,
                "physical_execution_invoked": False,
            },
        }
        report = {
            **report_without_digest,
            "result_sha256": _canonical_sha256(report_without_digest),
        }
        (output_dir / "visibility.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return report
    finally:
        environment.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-snapshot", type=Path, required=True)
    parser.add_argument("--fixture-snapshot", type=Path, required=True)
    parser.add_argument("--cosmos-source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = execute_live(
        source_snapshot=args.source_snapshot.resolve(),
        fixture_snapshot=args.fixture_snapshot.resolve(),
        cosmos_source_root=args.cosmos_source_root.resolve(),
        output_dir=args.output_dir.resolve(),
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["rejection_filter"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

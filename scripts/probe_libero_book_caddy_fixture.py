#!/usr/bin/env python3
"""Construct one digest-bound book/caddy diagnostic fixture from a successful demo.

The script restores an actual successful LIBERO demonstration state, translates
only ``black_book_1``, settles through the normal 7D environment interface, and
records actual predicates plus image visibility.  It does not run a policy and
does not establish Repair.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any


OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_BOOK_CADDY_FIXTURE"
TASK_SUITE = "libero_10"
TASK_NAME = "STUDY_SCENE1_pick_up_the_book_and_place_it_in_the_back_compartment_of_the_caddy"
TASK_ID = 5
TASK_INSTRUCTION = "pick up the book and place it in the back compartment of the caddy"
ENVIRONMENT_SEED = 0
EPISODE_INIT_STATE_INDEX = 0
TARGET_OBJECT = "black_book_1"
CONTAINER_OBJECT = "desk_caddy_1"
PROTECTED_OBJECT = "white_yellow_mug_1"
SETTLE_STEPS = 60
STABLE_FAILURE_STEPS = 20
MAXIMUM_PROTECTED_DISPLACEMENT_METRES = 0.005
MAXIMUM_INJECTED_TRANSLATION_METRES = 0.05
MINIMUM_SETTLED_TRANSLATION_METRES = 0.03
MAXIMUM_SETTLED_TRANSLATION_METRES = 0.12
MAXIMUM_LAST_WINDOW_TARGET_DRIFT_METRES = 0.001
MINIMUM_CHANGED_PIXELS = 256
FIXTURE_SCHEMA_VERSION = "missionos.libero_book_caddy_fixture.v1"


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _predicate_material(environment: Any) -> list[dict[str, Any]]:
    task_environment = environment.env
    material = []
    for index, state in enumerate(task_environment.parsed_problem["goal_state"]):
        spec = tuple(str(part).casefold() for part in state)
        value = task_environment._eval_predicate(state)
        if hasattr(value, "item"):
            value = value.item()
        if not isinstance(value, bool):
            raise RuntimeError("book_caddy_fixture_predicate_not_boolean")
        material.append(
            {
                "predicate_index": index,
                "predicate_name": spec[0],
                "arguments": list(spec[1:]),
                "satisfied": value,
            }
        )
    if len(material) != 1:
        raise RuntimeError("book_caddy_fixture_predicate_count_invalid")
    return material


def _make_environment() -> tuple[Any, Any, str]:
    from libero.libero import benchmark, get_libero_path
    from libero.libero.envs import OffScreenRenderEnv

    suite = benchmark.get_benchmark_dict()[TASK_SUITE]()
    for task_index, task in enumerate(suite.tasks):
        if task.name != TASK_NAME:
            continue
        if task_index != TASK_ID:
            raise RuntimeError("book_caddy_fixture_task_index_mismatch")
        environment = OffScreenRenderEnv(
            bddl_file_name=os.path.join(
                get_libero_path("bddl_files"), task.problem_folder, task.bddl_file
            ),
            camera_heights=256,
            camera_widths=256,
            camera_depths=True,
        )
        environment.seed(ENVIRONMENT_SEED)
        return environment, suite.get_task_init_states(task_index), task.language
    raise RuntimeError("book_caddy_fixture_task_not_found")


def _read_demo_terminal_state(path: Path, demo_key: str) -> tuple[Any, dict[str, Any]]:
    import h5py
    import numpy as np

    with h5py.File(path, "r") as archive:
        if "data" not in archive or demo_key not in archive["data"]:
            raise RuntimeError("book_caddy_fixture_demo_missing")
        demo = archive["data"][demo_key]
        if "states" not in demo or len(demo["states"]) == 0:
            raise RuntimeError("book_caddy_fixture_demo_states_missing")
        state = np.asarray(demo["states"][-1], dtype=np.float64).reshape(-1)
        attrs = {}
        for key, raw_value in demo.attrs.items():
            value = raw_value.item() if hasattr(raw_value, "item") else raw_value
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if isinstance(value, (str, bool, int, float)):
                attrs[str(key)] = value
    return state, {
        "repository": "nvidia/LIBERO-Cosmos-Policy",
        "relative_path": (
            "success_only/libero_10_regen/"
            "STUDY_SCENE1_pick_up_the_book_and_place_it_in_the_back_compartment_of_the_caddy_demo.hdf5"
        ),
        "file_sha256": _sha256_path(path),
        "demo_key": demo_key,
        "demo_attributes": attrs,
        "terminal_state_sha256": hashlib.sha256(state.tobytes()).hexdigest(),
        "terminal_state_value_count": int(state.size),
    }


def _capture_observation_images(observation: dict[str, Any]) -> dict[str, Any]:
    import numpy as np

    return {
        "agentview": np.asarray(observation["agentview_image"], dtype=np.uint8).copy(),
        "wrist": np.asarray(observation["robot0_eye_in_hand_image"], dtype=np.uint8).copy(),
    }


def _visibility(success_images: dict[str, Any], fixture_images: dict[str, Any]) -> dict[str, Any]:
    import numpy as np

    result = {}
    for name in sorted(success_images):
        before = np.asarray(success_images[name], dtype=np.int16)
        after = np.asarray(fixture_images[name], dtype=np.int16)
        channel_delta = np.abs(after - before)
        changed = np.any(channel_delta > 0, axis=2)
        result[name] = {
            "changed_pixel_count": int(np.count_nonzero(changed)),
            "total_pixel_count": int(changed.size),
            "changed_pixel_fraction": float(np.count_nonzero(changed) / changed.size),
            "mean_absolute_channel_delta": float(channel_delta.mean()),
        }
    return result


def _write_snapshot(path: Path, state: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    import numpy as np

    flat = np.asarray(state, dtype=np.float64).reshape(-1)
    material = {
        "schema_version": "missionos.libero_diagnostic_snapshot.v1",
        "authority": "diagnostic_only",
        "raw_simulator_state_included": True,
        "simulator_state_value_count": int(flat.size),
        "simulator_state_sha256": hashlib.sha256(flat.tobytes()).hexdigest(),
        **deepcopy(metadata),
    }
    with path.open("xb") as stream:
        np.savez_compressed(
            stream,
            simulator_state=flat,
            metadata_json=np.asarray(json.dumps(material, sort_keys=True)),
        )
    return {**material, "snapshot_artifact_sha256": _sha256_path(path)}


def execute_live(
    *,
    demonstrations_hdf5: Path,
    demo_key: str,
    translation_metres: tuple[float, float, float],
    output_dir: Path,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("book_caddy_fixture_opt_in_required")
    if output_dir.exists():
        raise ValueError("book_caddy_fixture_output_exists")
    if len(translation_metres) != 3 or any(not math.isfinite(v) for v in translation_metres):
        raise ValueError("book_caddy_fixture_translation_invalid")

    import numpy as np
    from PIL import Image

    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    terminal_state, demo_evidence = _read_demo_terminal_state(demonstrations_hdf5, demo_key)
    environment, init_states, instruction = _make_environment()
    output_dir.mkdir(parents=True)
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        success_observation = environment.regenerate_obs_from_state(terminal_state)
        success_predicates = _predicate_material(environment)
        if [item["satisfied"] for item in success_predicates] != [True]:
            raise RuntimeError("book_caddy_fixture_demo_terminal_not_successful")
        if instruction != TASK_INSTRUCTION:
            raise RuntimeError("book_caddy_fixture_instruction_mismatch")

        success_images = _capture_observation_images(success_observation)
        simulator = environment.env
        target_body_id = int(simulator.obj_body_id[TARGET_OBJECT])
        container_body_id = int(simulator.obj_body_id[CONTAINER_OBJECT])
        protected_body_id = int(simulator.obj_body_id[PROTECTED_OBJECT])
        target_joint = simulator.get_object(TARGET_OBJECT).joints[0]
        source_target = np.asarray(simulator.sim.data.body_xpos[target_body_id], dtype=np.float64).copy()
        source_quaternion = np.asarray(
            simulator.sim.data.body_xquat[target_body_id], dtype=np.float64
        ).copy()
        source_container = np.asarray(
            simulator.sim.data.body_xpos[container_body_id], dtype=np.float64
        ).copy()
        source_protected = np.asarray(
            simulator.sim.data.body_xpos[protected_body_id], dtype=np.float64
        ).copy()

        requested = np.asarray(translation_metres, dtype=np.float64)
        injected_target = source_target + requested
        simulator.sim.data.set_joint_qpos(
            target_joint, np.concatenate([injected_target, source_quaternion])
        )
        try:
            simulator.sim.data.set_joint_qvel(target_joint, np.zeros(6, dtype=np.float64))
        except (AttributeError, ValueError):
            pass
        simulator.sim.forward()
        simulator._post_process()
        simulator._update_observables(force=True)

        settle_trace = []
        fixture_observation = success_observation
        for step_index in range(SETTLE_STEPS):
            fixture_observation, _, done, info = environment.step(
                np.zeros(7, dtype=np.float64)
            )
            vector = [item["satisfied"] for item in _predicate_material(environment)]
            target = np.asarray(simulator.sim.data.body_xpos[target_body_id], dtype=np.float64)
            settle_trace.append(
                {
                    "fixture_step_index": step_index,
                    "predicate_vector": vector,
                    "target_position_metres": target.tolist(),
                    "environment_done": bool(done),
                    "environment_info_success": bool(info.get("success", False)),
                }
            )

        predicates = _predicate_material(environment)
        vector = [item["satisfied"] for item in predicates]
        terminal_target = np.asarray(
            simulator.sim.data.body_xpos[target_body_id], dtype=np.float64
        ).copy()
        terminal_container = np.asarray(
            simulator.sim.data.body_xpos[container_body_id], dtype=np.float64
        ).copy()
        terminal_protected = np.asarray(
            simulator.sim.data.body_xpos[protected_body_id], dtype=np.float64
        ).copy()
        fixture_images = _capture_observation_images(fixture_observation)
        visibility = _visibility(success_images, fixture_images)
        stable_failure = (
            vector == [False]
            and all(item["predicate_vector"] == [False] for item in settle_trace[-STABLE_FAILURE_STEPS:])
        )
        container_displacement = float(np.linalg.norm(terminal_container - source_container))
        protected_displacement = float(np.linalg.norm(terminal_protected - source_protected))
        visible = all(
            item["changed_pixel_count"] >= MINIMUM_CHANGED_PIXELS
            for item in visibility.values()
        )
        requested_norm = float(np.linalg.norm(requested))
        observed_norm = float(np.linalg.norm(terminal_target - source_target))
        last_window_positions = np.asarray(
            [item["target_position_metres"] for item in settle_trace[-STABLE_FAILURE_STEPS:]],
            dtype=np.float64,
        )
        last_window_target_drift = float(
            np.max(np.linalg.norm(last_window_positions - terminal_target, axis=1))
        )
        translation_stable = bool(
            0.0 < requested_norm <= MAXIMUM_INJECTED_TRANSLATION_METRES
            and MINIMUM_SETTLED_TRANSLATION_METRES <= observed_norm
            <= MAXIMUM_SETTLED_TRANSLATION_METRES
            and last_window_target_drift <= MAXIMUM_LAST_WINDOW_TARGET_DRIFT_METRES
        )
        admitted = bool(
            stable_failure
            and translation_stable
            and container_displacement <= MAXIMUM_PROTECTED_DISPLACEMENT_METRES
            and protected_displacement <= MAXIMUM_PROTECTED_DISPLACEMENT_METRES
            and visible
        )

        for name, array in success_images.items():
            Image.fromarray(array).save(output_dir / f"success-{name}.png")
        for name, array in fixture_images.items():
            Image.fromarray(array).save(output_dir / f"fixture-{name}.png")

        fixture_material = {
            "schema_version": FIXTURE_SCHEMA_VERSION,
            "authority": "diagnostic_fixture_only",
            "task_suite": TASK_SUITE,
            "task_id": TASK_ID,
            "task_name": TASK_NAME,
            "instruction": instruction,
            "environment_seed": ENVIRONMENT_SEED,
            "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
            "construction": "translate_book_from_successful_demonstration_terminal_state",
            "target_object": TARGET_OBJECT,
            "container_object": CONTAINER_OBJECT,
            "protected_object": PROTECTED_OBJECT,
            "requested_translation_metres": requested.tolist(),
            "requested_translation_norm_metres": requested_norm,
            "source_target_position_metres": source_target.tolist(),
            "injected_target_position_metres": injected_target.tolist(),
            "terminal_target_position_metres": terminal_target.tolist(),
            "observed_translation_from_source_metres": observed_norm,
            "maximum_injected_translation_metres": MAXIMUM_INJECTED_TRANSLATION_METRES,
            "admitted_settled_translation_range_metres": [
                MINIMUM_SETTLED_TRANSLATION_METRES,
                MAXIMUM_SETTLED_TRANSLATION_METRES,
            ],
            "last_window_target_drift_metres": last_window_target_drift,
            "maximum_last_window_target_drift_metres": (
                MAXIMUM_LAST_WINDOW_TARGET_DRIFT_METRES
            ),
            "translation_stability_observed": translation_stable,
            "container_displacement_metres": container_displacement,
            "protected_object_displacement_metres": protected_displacement,
            "source_goal_predicate_observations": success_predicates,
            "terminal_goal_predicate_observations": predicates,
            "terminal_goal_predicate_vector": vector,
            "fixture_settle_steps_applied": len(settle_trace),
            "stable_failure_steps_required": STABLE_FAILURE_STEPS,
            "stable_failure_observed": stable_failure,
            "input_visibility": visibility,
            "minimum_changed_pixels_per_camera": MINIMUM_CHANGED_PIXELS,
            "input_visibility_observed": visible,
            "fixture_admitted": admitted,
            "model_inference_invoked": False,
            "repair_attempted": False,
            "physical_execution_invoked": False,
        }
        fixture_sha256 = canonical_sha256(fixture_material)
        snapshot = _write_snapshot(
            output_dir / "fixture.npz",
            environment.sim.get_state().flatten(),
            {
                "task_suite": TASK_SUITE,
                "task_id": TASK_ID,
                "task_name": TASK_NAME,
                "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
                "environment_seed": ENVIRONMENT_SEED,
                "source_failure_basis": "diagnostic_book_caddy_displacement",
                "source_goal_predicate_vector": vector,
                "book_caddy_fixture": fixture_material,
                "book_caddy_fixture_sha256": fixture_sha256,
                "source_failure_is_repair_candidate": admitted,
            },
        )
        report_without_digest = {
            "schema_version": "missionos.libero_book_caddy_fixture_probe.v1",
            "status": "fixture_admitted" if admitted else "fixture_rejected",
            "demonstration": demo_evidence,
            "fixture": fixture_material,
            "fixture_sha256": fixture_sha256,
            "snapshot": snapshot,
            "settle_trace": settle_trace,
            "claim_boundary": {
                "authority": "diagnostic_only",
                "simulator_state_directly_changed_for_fixture_construction": True,
                "model_inference_invoked": False,
                "repair_attempted": False,
                "semantic_repair_established": False,
                "physical_execution_invoked": False,
            },
        }
        report = {
            **report_without_digest,
            "result_sha256": canonical_sha256(report_without_digest),
        }
        (output_dir / "result.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return report
    finally:
        environment.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demonstrations-hdf5", type=Path, required=True)
    parser.add_argument("--demo-key", default="demo_0")
    parser.add_argument("--translation-x-metres", type=float, required=True)
    parser.add_argument("--translation-y-metres", type=float, required=True)
    parser.add_argument("--translation-z-metres", type=float, default=0.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = execute_live(
        demonstrations_hdf5=args.demonstrations_hdf5.resolve(),
        demo_key=args.demo_key,
        translation_metres=(
            args.translation_x_metres,
            args.translation_y_metres,
            args.translation_z_metres,
        ),
        output_dir=args.output_dir.resolve(),
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "fixture_admitted" else 2


if __name__ == "__main__":
    raise SystemExit(main())

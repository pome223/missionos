#!/usr/bin/env python3
"""Measure the actual LIBERO predicate boundary along a frozen fixture path.

This is a diagnostic fixture-construction probe.  It directly changes MuJoCo
state before any policy proposal exists, settles through the normal 7D LIBERO
interface, and records actual predicates.  It does not run a model or establish
Repair.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_DISPLACEMENT_CURRICULUM"
TASK_NAME = "KITCHEN_SCENE8_put_both_moka_pots_on_the_stove"
TASK_SUITE = "libero_10"
TASK_ID = 8
EPISODE_INIT_STATE_INDEX = 15
ENVIRONMENT_SEED = 7
TARGET_OBJECT = "moka_pot_2"
PROTECTED_OBJECT = "moka_pot_1"
STOVE_REGION = "flat_stove_1_cook_region"
DEFAULT_DISTANCES_METRES = (0.02, 0.05, 0.10, 0.15, 0.22)
SETTLE_STEPS = 60


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


def _read_failure_snapshot(path: Path) -> tuple[Any, dict[str, Any]]:
    import numpy as np

    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != {"simulator_state", "metadata_json"}:
            raise ValueError("libero_displacement_curriculum_snapshot_members_invalid")
        state = np.asarray(archive["simulator_state"], dtype=np.float64).reshape(-1)
        metadata = json.loads(str(archive["metadata_json"].item()))
    if metadata.get("simulator_state_sha256") != hashlib.sha256(state.tobytes()).hexdigest():
        raise ValueError("libero_displacement_curriculum_snapshot_state_digest_mismatch")
    return state, {
        **metadata,
        "snapshot_artifact_sha256": _sha256_path(path),
        "local_path_recorded": False,
    }


def _write_failure_snapshot(
    *, path: Path, simulator_state: Any, metadata: dict[str, Any]
) -> dict[str, Any]:
    import numpy as np

    state = np.asarray(simulator_state, dtype=np.float64).reshape(-1)
    material = {
        "schema_version": "missionos.groot_lerobot_failure_snapshot.v1",
        "authority": "diagnostic_only",
        "semantic_repair_claim_eligible": False,
        "raw_simulator_state_included": True,
        "simulator_state_value_count": int(state.size),
        "simulator_state_sha256": hashlib.sha256(state.tobytes()).hexdigest(),
        **deepcopy(metadata),
    }
    with path.open("xb") as stream:
        np.savez_compressed(
            stream,
            simulator_state=state,
            metadata_json=np.asarray(json.dumps(material, sort_keys=True)),
        )
    return {
        **material,
        "snapshot_artifact_sha256": _sha256_path(path),
        "local_path_recorded": False,
    }


def _predicate_material(environment: Any) -> list[dict[str, Any]]:
    task_environment = environment.env
    goal_state = task_environment.parsed_problem["goal_state"]
    material = []
    for index, state in enumerate(goal_state):
        spec = tuple(str(part).casefold() for part in state)
        satisfied = task_environment._eval_predicate(state)
        if hasattr(satisfied, "item"):
            satisfied = satisfied.item()
        if not isinstance(satisfied, bool):
            raise RuntimeError("libero_displacement_curriculum_predicate_not_boolean")
        observation = {
            "predicate_index": index,
            "predicate_name": spec[0],
            "arguments": list(spec[1:]),
            "satisfied": satisfied,
        }
        observation["predicate_id"] = canonical_sha256(observation)
        material.append(observation)
    if len(material) != 3:
        raise RuntimeError("libero_displacement_curriculum_predicate_count_invalid")
    return material


def _validate_distances(values: Sequence[float], maximum: float) -> tuple[float, ...]:
    distances = tuple(float(value) for value in values)
    if not distances or any(not math.isfinite(value) or value <= 0.0 for value in distances):
        raise ValueError("libero_displacement_curriculum_distance_invalid")
    if tuple(sorted(set(distances))) != distances:
        raise ValueError("libero_displacement_curriculum_distances_not_unique_sorted")
    if distances[-1] > maximum + 1e-9:
        raise ValueError("libero_displacement_curriculum_distance_exceeds_reference")
    return distances


def _make_environment() -> tuple[Any, Any]:
    from libero.libero import benchmark, get_libero_path
    from libero.libero.envs import OffScreenRenderEnv

    suite = benchmark.get_benchmark_dict()[TASK_SUITE]()
    for task_index, task in enumerate(suite.tasks):
        if task.name != TASK_NAME:
            continue
        environment = OffScreenRenderEnv(
            bddl_file_name=os.path.join(
                get_libero_path("bddl_files"), task.problem_folder, task.bddl_file
            ),
            camera_heights=256,
            camera_widths=256,
            camera_depths=True,
        )
        environment.seed(ENVIRONMENT_SEED)
        return environment, suite.get_task_init_states(task_index)
    raise RuntimeError("libero_displacement_curriculum_task_not_found")


def _write_json(path: Path, material: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(material, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _capture(simulator: Any, path: Path) -> dict[str, Any]:
    import numpy as np
    from PIL import Image

    pixels = simulator.sim.render(camera_name="agentview", height=512, width=512)
    Image.fromarray(np.asarray(pixels, dtype=np.uint8)[::-1]).save(path)
    return {"file_name": path.name, "sha256": _sha256_path(path)}


def execute_live(
    *,
    source_snapshot_path: Path,
    reference_snapshot_path: Path,
    output_dir: Path,
    distances_metres: Sequence[float] = DEFAULT_DISTANCES_METRES,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("libero_displacement_curriculum_opt_in_required")
    if output_dir.exists():
        raise ValueError("libero_displacement_curriculum_output_exists")

    import numpy as np

    source_state, source_metadata = _read_failure_snapshot(source_snapshot_path)
    reference_state, reference_metadata = _read_failure_snapshot(reference_snapshot_path)
    for label, metadata in (("source", source_metadata), ("reference", reference_metadata)):
        if (
            metadata.get("task_suite") != TASK_SUITE
            or metadata.get("task_id") != TASK_ID
            or metadata.get("episode_init_state_index") != EPISODE_INIT_STATE_INDEX
        ):
            raise RuntimeError(f"libero_displacement_curriculum_{label}_task_mismatch")

    os.environ.setdefault("MUJOCO_GL", "osmesa")
    os.environ.setdefault("PYOPENGL_PLATFORM", "osmesa")
    environment, init_states = _make_environment()
    output_dir.mkdir(parents=True)
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        environment.regenerate_obs_from_state(source_state)
        if not np.array_equal(environment.sim.get_state().flatten(), source_state):
            raise RuntimeError("libero_displacement_curriculum_source_restore_not_exact")
        source_vector = [item["satisfied"] for item in _predicate_material(environment)]
        if source_vector != [True, True, True]:
            raise RuntimeError(
                f"libero_displacement_curriculum_source_vector_invalid:{source_vector}"
            )
        simulator = environment.env
        target_body_id = int(simulator.obj_body_id[TARGET_OBJECT])
        protected_body_id = int(simulator.obj_body_id[PROTECTED_OBJECT])
        target_joint = simulator.get_object(TARGET_OBJECT).joints[0]
        source_target_position = np.asarray(
            simulator.sim.data.body_xpos[target_body_id], dtype=np.float64
        ).copy()
        source_target_quaternion = np.asarray(
            simulator.sim.data.body_xquat[target_body_id], dtype=np.float64
        ).copy()
        source_protected_position = np.asarray(
            simulator.sim.data.body_xpos[protected_body_id], dtype=np.float64
        ).copy()

        environment.regenerate_obs_from_state(reference_state)
        if not np.array_equal(environment.sim.get_state().flatten(), reference_state):
            raise RuntimeError("libero_displacement_curriculum_reference_restore_not_exact")
        reference_vector = [item["satisfied"] for item in _predicate_material(environment)]
        if reference_vector != [True, False, True]:
            raise RuntimeError(
                f"libero_displacement_curriculum_reference_vector_invalid:{reference_vector}"
            )
        reference_target_position = np.asarray(
            simulator.sim.data.body_xpos[target_body_id], dtype=np.float64
        ).copy()
        reference_vector_xy = reference_target_position - source_target_position
        reference_vector_xy[2] = 0.0
        reference_displacement = float(np.linalg.norm(reference_vector_xy))
        if reference_displacement <= 0.0:
            raise RuntimeError("libero_displacement_curriculum_reference_not_displaced")
        displacement_vector = source_target_position - source_protected_position
        displacement_vector[2] = 0.0
        separating_norm = float(np.linalg.norm(displacement_vector))
        if separating_norm <= 0.0:
            raise RuntimeError("libero_displacement_curriculum_separating_ray_invalid")
        direction = displacement_vector / separating_norm
        distances = _validate_distances(distances_metres, reference_displacement)

        points = []
        for distance in distances:
            # OffScreenRenderEnv retains termination and simulator wrapper state
            # across resets in this pinned runtime.  Use a fresh environment for
            # every independently admitted curriculum point.
            environment.close()
            environment, init_states = _make_environment()
            environment.reset()
            environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
            environment.regenerate_obs_from_state(source_state)
            simulator = environment.env
            target_body_id = int(simulator.obj_body_id[TARGET_OBJECT])
            protected_body_id = int(simulator.obj_body_id[PROTECTED_OBJECT])
            target_joint = simulator.get_object(TARGET_OBJECT).joints[0]
            region = simulator.object_sites_dict[STOVE_REGION]
            injected_position = source_target_position + direction * distance
            simulator.sim.data.set_joint_qpos(
                target_joint,
                np.concatenate([injected_position, source_target_quaternion]),
            )
            try:
                simulator.sim.data.set_joint_qvel(target_joint, np.zeros(6, dtype=np.float64))
            except (AttributeError, ValueError):
                pass
            simulator.sim.forward()
            simulator._post_process()
            simulator._update_observables(force=True)

            trace = []
            for step_index in range(SETTLE_STEPS):
                _, _, done, info = environment.step(np.zeros(7, dtype=np.float64))
                vector = [item["satisfied"] for item in _predicate_material(environment)]
                trace.append(
                    {
                        "fixture_step_index": step_index,
                        "predicate_vector": vector,
                        "environment_done": bool(done),
                        "environment_info_success": bool(info.get("success", False)),
                    }
                )

            predicates = _predicate_material(environment)
            vector = [item["satisfied"] for item in predicates]
            terminal_target = np.asarray(
                simulator.sim.data.body_xpos[target_body_id], dtype=np.float64
            ).copy()
            terminal_protected = np.asarray(
                simulator.sim.data.body_xpos[protected_body_id], dtype=np.float64
            ).copy()
            region_position = np.asarray(
                simulator.sim.data.get_site_xpos(STOVE_REGION), dtype=np.float64
            )
            region_matrix = np.asarray(
                simulator.sim.data.get_site_xmat(STOVE_REGION), dtype=np.float64
            ).reshape(3, 3)
            half_extent = np.asarray(region.size, dtype=np.float64)
            local_delta = region_matrix @ (terminal_target - region_position)
            axis_margins = {
                "x": float(half_extent[0] - abs(local_delta[0])),
                "y": float(half_extent[1] - abs(local_delta[1])),
            }
            observed_translation = float(np.linalg.norm(terminal_target - source_target_position))
            protected_displacement = float(
                np.linalg.norm(terminal_protected - source_protected_position)
            )
            if abs(observed_translation - distance) > 0.01:
                raise RuntimeError(
                    "libero_displacement_curriculum_translation_drift_too_large:"
                    f"requested={distance}:observed={observed_translation}"
                )
            if protected_displacement > 0.005:
                raise RuntimeError(
                    "libero_displacement_curriculum_protected_object_moved:"
                    f"requested={distance}:observed={protected_displacement}"
                )
            distance_label = f"{distance:.6f}".replace(".", "p")
            point_dir = output_dir / f"distance-{distance_label}-metres"
            point_dir.mkdir()
            render = _capture(simulator, point_dir / "terminal.png")
            terminal_state = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
            fixture_material = {
                "schema_version": "missionos.libero_displacement_curriculum_fixture.v1",
                "authority": "diagnostic_fixture_only",
                "construction": "protected_separating_horizontal_ray_from_success_state",
                "requested_translation_from_source_metres": distance,
                "observed_translation_from_source_metres": observed_translation,
                "source_target_position_metres": source_target_position.tolist(),
                "injected_target_position_metres": injected_position.tolist(),
                "terminal_target_position_metres": terminal_target.tolist(),
                "reference_target_position_metres": reference_target_position.tolist(),
                "reference_translation_metres": reference_displacement,
                "terminal_goal_predicate_observations": predicates,
                "terminal_goal_predicate_vector": vector,
                "terminal_local_stove_delta_metres": local_delta.tolist(),
                "stove_region_half_extent_metres": half_extent.tolist(),
                "terminal_stove_axis_margins_metres": axis_margins,
                "protected_object_displacement_metres": protected_displacement,
                "fixture_settle_steps_applied": len(trace),
                "fixture_settle_trace": trace,
                "actual_predicate_failure_observed": vector == [True, False, True],
                "model_inference_invoked": False,
                "repair_attempted": False,
                "physical_execution_invoked": False,
            }
            fixture_sha256 = canonical_sha256(fixture_material)
            snapshot_path = point_dir / "fixture.npz"
            snapshot = _write_failure_snapshot(
                path=snapshot_path,
                simulator_state=terminal_state,
                metadata={
                    "task_suite": TASK_SUITE,
                    "task_id": TASK_ID,
                    "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
                    "source_failure_basis": "diagnostic_displacement_curriculum",
                    "source_goal_predicate_observations": predicates,
                    "source_goal_predicate_vector": vector,
                    "source_goal_predicate_vector_sha256": canonical_sha256(
                        {"goal_predicate_observations": predicates}
                    ),
                    "displacement_curriculum_fixture": fixture_material,
                    "displacement_curriculum_fixture_sha256": fixture_sha256,
                    "source_failure_is_repair_candidate": vector == [True, False, True],
                    "model_runtime_invoked_for_snapshot_restore": False,
                    "physical_execution_invoked": False,
                },
            )
            points.append(
                {
                    "requested_translation_from_source_metres": distance,
                    "terminal_goal_predicate_vector": vector,
                    "actual_predicate_failure_observed": vector == [True, False, True],
                    "observed_translation_from_source_metres": fixture_material[
                        "observed_translation_from_source_metres"
                    ],
                    "terminal_stove_axis_margins_metres": axis_margins,
                    "protected_object_displacement_metres": fixture_material[
                        "protected_object_displacement_metres"
                    ],
                    "fixture_sha256": fixture_sha256,
                    "snapshot": snapshot,
                    "render": render,
                }
            )

        report_without_digest = {
            "schema_version": "missionos.libero_displacement_curriculum_probe.v1",
            "status": "diagnostic_curriculum_completed",
            "source_snapshot_sha256": _sha256_path(source_snapshot_path),
            "reference_snapshot_sha256": _sha256_path(reference_snapshot_path),
            "source_goal_predicate_vector": source_vector,
            "reference_goal_predicate_vector": reference_vector,
            "source_target_position_metres": source_target_position.tolist(),
            "reference_target_position_metres": reference_target_position.tolist(),
            "reference_translation_metres": reference_displacement,
            "interpolation_direction_unit_vector": direction.tolist(),
            "points": points,
            "claim_boundary": {
                "authority": "diagnostic_only",
                "simulator_state_directly_changed_for_fixture_construction": True,
                "model_inference_invoked": False,
                "repair_proposal_created": False,
                "human_approval_created": False,
                "governed_dispatch_created": False,
                "repair_attempted": False,
                "semantic_repair_established": False,
                "physical_execution_invoked": False,
            },
        }
        report = {
            **report_without_digest,
            "result_sha256": canonical_sha256(report_without_digest),
        }
        _write_json(output_dir / "curriculum.json", report)
        return report
    finally:
        environment.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-snapshot", type=Path, required=True)
    parser.add_argument("--reference-snapshot", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--distances-metres",
        default=",".join(str(value) for value in DEFAULT_DISTANCES_METRES),
    )
    args = parser.parse_args()
    report = execute_live(
        source_snapshot_path=args.source_snapshot.resolve(),
        reference_snapshot_path=args.reference_snapshot.resolve(),
        output_dir=args.output_dir.resolve(),
        distances_metres=tuple(float(value.strip()) for value in args.distances_metres.split(",")),
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

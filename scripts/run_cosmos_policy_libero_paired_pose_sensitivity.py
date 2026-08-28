#!/usr/bin/env python3
"""Measure Cosmos action sensitivity to robot phase pose and object displacement.

The probe replays a verified successful nominal action trace to recover five
robot poses, constructs a predicate-checked displacement grid from that exact
successful state, and performs forward-only policy queries.  It never applies
the queried actions.  All results are diagnostic-only.
"""

from __future__ import annotations

import argparse
import hashlib
from itertools import combinations
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402
from scripts.run_cosmos_policy_libero_experiment import (  # noqa: E402
    EPISODE_INIT_STATE_INDEX,
    OFFICIAL_STABILIZATION_STEPS,
    TASK_ID,
    TASK_NAME,
    TASK_SUITE,
    _build_model_runtime,
    _make_environment,
    _predicate_material,
    _query_policy,
    _save_actual_observation,
    _save_future_prediction,
    _write_json,
)


OPT_IN_ENV = "RUN_MISSIONOS_COSMOS_POLICY_LIBERO_PAIRED_POSE_SENSITIVITY"
DEFAULT_POSE_ACTION_COUNTS = (0, 92, 184, 276, 369)
DEFAULT_DISPLACEMENTS_METRES = (0.0, 0.005, 0.05, 0.225)
DEFAULT_SEEDS = (17, 71, 195, 231)
SETTLE_STEPS = 60
MAXIMUM_PROTECTED_DISPLACEMENT_METRES = 0.005
MAXIMUM_SETTLE_TRANSLATION_ERROR_METRES = 0.01
TARGET_OBJECT = "moka_pot_2"
PROTECTED_OBJECT = "moka_pot_1"


def _verify_nominal_report(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    supplied = report.get("result_sha256")
    material = {key: value for key, value in report.items() if key != "result_sha256"}
    actions = report.get("action_trace")
    predicates = report.get("predicate_trace")
    if supplied != canonical_sha256(material):
        raise RuntimeError("cosmos_pose_sensitivity_nominal_report_digest_mismatch")
    if (
        report.get("schema_version") != "missionos.cosmos_policy_libero_nominal.v1"
        or report.get("task_suite") != TASK_SUITE
        or report.get("task_name") != TASK_NAME
        or report.get("task_id") != TASK_ID
        or report.get("episode_init_state_index") != EPISODE_INIT_STATE_INDEX
        or report.get("nominal_success_observed") is not True
        or not isinstance(actions, list)
        or not isinstance(predicates, list)
        or len(actions) != report.get("applied_action_count")
        or len(predicates) != len(actions)
        or predicates[-1].get("official_predicate_result") is not True
    ):
        raise RuntimeError("cosmos_pose_sensitivity_nominal_report_contract_invalid")
    return report


def _validate_grid(
    *,
    pose_action_counts: Sequence[int],
    displacements_metres: Sequence[float],
    seeds: Sequence[int],
) -> tuple[tuple[int, ...], tuple[float, ...], tuple[int, ...]]:
    poses = tuple(int(value) for value in pose_action_counts)
    distances = tuple(float(value) for value in displacements_metres)
    seed_values = tuple(int(value) for value in seeds)
    if poses != tuple(sorted(set(poses))) or len(poses) != 5 or poses[0] != 0:
        raise ValueError("cosmos_pose_sensitivity_pose_grid_invalid")
    if (
        distances != tuple(sorted(set(distances)))
        or len(distances) < 2
        or distances[0] != 0.0
        or any(not math.isfinite(value) or value < 0.0 for value in distances)
    ):
        raise ValueError("cosmos_pose_sensitivity_displacement_grid_invalid")
    if len(seed_values) < 2 or len(set(seed_values)) != len(seed_values):
        raise ValueError("cosmos_pose_sensitivity_seed_grid_invalid")
    return poses, distances, seed_values


def _robot_indexes(environment: Any) -> tuple[list[int], list[int]]:
    robot = environment.env.robots[0]
    position = list(robot._ref_joint_pos_indexes) + list(robot._ref_gripper_joint_pos_indexes)
    velocity = list(robot._ref_joint_vel_indexes) + list(robot._ref_gripper_joint_vel_indexes)
    return [int(value) for value in position], [int(value) for value in velocity]


def _vector(environment: Any) -> list[bool]:
    return [item["satisfied"] for item in _predicate_material(environment)]


def _replay_nominal(
    *, nominal_report: Mapping[str, Any], pose_action_counts: Sequence[int]
) -> tuple[list[dict[str, Any]], Any]:
    import numpy as np
    from cosmos_policy.experiments.robot.libero.libero_utils import get_libero_dummy_action

    environment, init_states, _ = _make_environment()
    wanted = set(pose_action_counts)
    poses: list[dict[str, Any]] = []
    try:
        environment.reset()
        observation = environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        for _ in range(OFFICIAL_STABILIZATION_STEPS):
            observation, _, _, _ = environment.step(get_libero_dummy_action("cosmos"))
        position_indexes, velocity_indexes = _robot_indexes(environment)

        def capture(action_count: int) -> None:
            poses.append(
                {
                    "action_count": action_count,
                    "phase_fraction": action_count / int(nominal_report["applied_action_count"]),
                    "robot_qpos": np.asarray(
                        environment.sim.data.qpos[position_indexes], dtype=np.float64
                    ).tolist(),
                    "robot_qvel": np.asarray(
                        environment.sim.data.qvel[velocity_indexes], dtype=np.float64
                    ).tolist(),
                    "eef_position_metres": np.asarray(
                        observation["robot0_eef_pos"], dtype=np.float64
                    ).tolist(),
                    "goal_predicate_vector": _vector(environment),
                }
            )

        if 0 in wanted:
            capture(0)
        for index, action_material in enumerate(nominal_report["action_trace"], start=1):
            observation, _, done, info = environment.step(action_material["action_7d"])
            observed = _vector(environment)
            expected = [
                item["satisfied"]
                for item in nominal_report["predicate_trace"][index - 1][
                    "goal_predicate_observations"
                ]
            ]
            if observed != expected:
                raise RuntimeError(f"cosmos_pose_sensitivity_nominal_replay_drift:{index}")
            if index in wanted:
                capture(index)
            if index < len(nominal_report["action_trace"]) and (done or info.get("done", False)):
                raise RuntimeError("cosmos_pose_sensitivity_nominal_replay_ended_early")
        if _vector(environment) != [True, True, True]:
            raise RuntimeError("cosmos_pose_sensitivity_nominal_replay_success_missing")
        if {item["action_count"] for item in poses} != wanted:
            raise RuntimeError("cosmos_pose_sensitivity_pose_checkpoint_missing")
        final_state = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64).copy()
        return sorted(poses, key=lambda item: item["action_count"]), final_state
    finally:
        environment.close()


def _read_successful_source_snapshot(path: Path) -> tuple[Any, dict[str, Any]]:
    import numpy as np

    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != {"simulator_state", "metadata_json"}:
            raise ValueError("cosmos_pose_sensitivity_source_snapshot_members_invalid")
        state = np.asarray(archive["simulator_state"], dtype=np.float64).reshape(-1)
        metadata = json.loads(str(archive["metadata_json"].item()))
    if hashlib.sha256(state.tobytes()).hexdigest() != metadata.get("simulator_state_sha256"):
        raise RuntimeError("cosmos_pose_sensitivity_source_snapshot_state_digest_mismatch")
    return state, {
        "snapshot_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "simulator_state_sha256": metadata["simulator_state_sha256"],
        "metadata_schema_version": metadata.get("schema_version"),
        "metadata_authority": metadata.get("authority"),
        "metadata_source_goal_predicate_vector": metadata.get("source_goal_predicate_vector"),
        "metadata_predicates_revalidated_in_pinned_runtime": True,
        "local_path_recorded": False,
    }


def _successful_source_material(successful_state: Any) -> dict[str, Any]:
    import numpy as np

    environment, init_states, _ = _make_environment()
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        environment.regenerate_obs_from_state(successful_state)
        restored = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        if not np.array_equal(restored, successful_state):
            raise RuntimeError("cosmos_pose_sensitivity_source_snapshot_restore_not_exact")
        vector = _vector(environment)
        if vector != [True, True, True]:
            raise RuntimeError(f"cosmos_pose_sensitivity_source_not_successful:{vector}")
        simulator = environment.env
        target_body = int(simulator.obj_body_id[TARGET_OBJECT])
        protected_body = int(simulator.obj_body_id[PROTECTED_OBJECT])
        return {
            "target_position_metres": np.asarray(
                simulator.sim.data.body_xpos[target_body], dtype=np.float64
            ).tolist(),
            "target_quaternion_wxyz": np.asarray(
                simulator.sim.data.body_xquat[target_body], dtype=np.float64
            ).tolist(),
            "protected_position_metres": np.asarray(
                simulator.sim.data.body_xpos[protected_body], dtype=np.float64
            ).tolist(),
            "actual_goal_predicate_vector": vector,
        }
    finally:
        environment.close()


def _normalize_successful_source_robot(
    *, successful_state: Any, initial_pose: Mapping[str, Any]
) -> tuple[Any, dict[str, Any]]:
    import numpy as np
    from cosmos_policy.experiments.robot.libero.libero_utils import get_libero_dummy_action

    environment, init_states, _ = _make_environment()
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        environment.regenerate_obs_from_state(successful_state)
        simulator = environment.env
        target_body = int(simulator.obj_body_id[TARGET_OBJECT])
        protected_body = int(simulator.obj_body_id[PROTECTED_OBJECT])
        before_target = np.asarray(simulator.sim.data.body_xpos[target_body], dtype=np.float64).copy()
        before_protected = np.asarray(
            simulator.sim.data.body_xpos[protected_body], dtype=np.float64
        ).copy()
        position_indexes, velocity_indexes = _robot_indexes(environment)
        environment.sim.data.qpos[position_indexes] = np.asarray(
            initial_pose["robot_qpos"], dtype=np.float64
        )
        environment.sim.data.qvel[velocity_indexes] = np.asarray(
            initial_pose["robot_qvel"], dtype=np.float64
        )
        environment.sim.forward()
        simulator._post_process()
        simulator._update_observables(force=True)
        trace = []
        for step in range(SETTLE_STEPS):
            _, _, done, info = environment.step(get_libero_dummy_action("cosmos"))
            trace.append(
                {
                    "settle_step": step,
                    "goal_predicate_vector": _vector(environment),
                    "done": bool(done),
                    "info_success": bool(info.get("success", False)),
                }
            )
        after_target = np.asarray(simulator.sim.data.body_xpos[target_body], dtype=np.float64)
        after_protected = np.asarray(
            simulator.sim.data.body_xpos[protected_body], dtype=np.float64
        )
        target_motion = float(np.linalg.norm(after_target - before_target))
        protected_motion = float(np.linalg.norm(after_protected - before_protected))
        valid = all(item["goal_predicate_vector"] == [True, True, True] for item in trace)
        evidence = {
            "construction": "first_success_state_with_robot_transplanted_to_nominal_initial_pose",
            "settle_steps": SETTLE_STEPS,
            "target_motion_during_normalization_metres": target_motion,
            "protected_motion_during_normalization_metres": protected_motion,
            "object_configuration_exactly_preserved_during_normalization": bool(
                target_motion <= MAXIMUM_PROTECTED_DISPLACEMENT_METRES
                and protected_motion <= MAXIMUM_PROTECTED_DISPLACEMENT_METRES
            ),
            "terminal_goal_predicate_vector": _vector(environment),
            "stable_successful_source_observed": valid,
        }
        if not valid:
            raise RuntimeError(f"cosmos_pose_sensitivity_normalized_source_invalid:{evidence}")
        return (
            np.asarray(environment.sim.get_state().flatten(), dtype=np.float64).copy(),
            evidence,
        )
    finally:
        environment.close()


def _construct_displacement_states(
    *, successful_state: Any, source: Mapping[str, Any], distances: Sequence[float]
) -> list[dict[str, Any]]:
    import numpy as np

    source_target = np.asarray(source["target_position_metres"], dtype=np.float64)
    source_quaternion = np.asarray(source["target_quaternion_wxyz"], dtype=np.float64)
    source_protected = np.asarray(source["protected_position_metres"], dtype=np.float64)
    direction = source_target - source_protected
    direction[2] = 0.0
    direction /= np.linalg.norm(direction)
    fixtures = []
    for distance in distances:
        environment, init_states, _ = _make_environment()
        try:
            environment.reset()
            environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
            environment.regenerate_obs_from_state(successful_state)
            simulator = environment.env
            target_body = int(simulator.obj_body_id[TARGET_OBJECT])
            protected_body = int(simulator.obj_body_id[PROTECTED_OBJECT])
            target_joint = simulator.get_object(TARGET_OBJECT).joints[0]
            injected = source_target + direction * distance
            simulator.sim.data.set_joint_qpos(
                target_joint, np.concatenate([injected, source_quaternion])
            )
            try:
                simulator.sim.data.set_joint_qvel(target_joint, np.zeros(6, dtype=np.float64))
            except (AttributeError, ValueError):
                pass
            simulator.sim.forward()
            simulator._post_process()
            simulator._update_observables(force=True)
            trace = []
            for step in range(SETTLE_STEPS):
                _, _, done, info = environment.step(np.zeros(7, dtype=np.float64))
                trace.append(
                    {
                        "settle_step": step,
                        "goal_predicate_vector": _vector(environment),
                        "done": bool(done),
                        "info_success": bool(info.get("success", False)),
                    }
                )
            terminal_target = np.asarray(
                simulator.sim.data.body_xpos[target_body], dtype=np.float64
            ).copy()
            terminal_protected = np.asarray(
                simulator.sim.data.body_xpos[protected_body], dtype=np.float64
            ).copy()
            observed_distance = float(np.linalg.norm(terminal_target - source_target))
            protected_distance = float(np.linalg.norm(terminal_protected - source_protected))
            stable = all(
                item["goal_predicate_vector"] == trace[0]["goal_predicate_vector"]
                for item in trace
            )
            translation_within_tolerance = bool(
                abs(observed_distance - distance) <= MAXIMUM_SETTLE_TRANSLATION_ERROR_METRES
            )
            valid = bool(
                stable
                and protected_distance <= MAXIMUM_PROTECTED_DISPLACEMENT_METRES
            )
            fixtures.append(
                {
                    "requested_displacement_metres": distance,
                    "observed_displacement_metres": observed_distance,
                    "protected_displacement_metres": protected_distance,
                    "injected_translation_preserved_after_settle": translation_within_tolerance,
                    "goal_predicate_vector": _vector(environment),
                    "settle_trace": trace,
                    "valid_for_sensitivity_map": valid,
                    "simulator_state": np.asarray(
                        environment.sim.get_state().flatten(), dtype=np.float64
                    ).copy(),
                }
            )
        finally:
            environment.close()
    return fixtures


def _action_delta(left: Any, right: Any) -> dict[str, float]:
    import numpy as np

    left_array = np.asarray(left, dtype=np.float64)
    right_array = np.asarray(right, dtype=np.float64)
    delta = left_array - right_array
    translation_left = left_array[:, :3]
    translation_right = right_array[:, :3]
    denominators = np.linalg.norm(translation_left, axis=1) * np.linalg.norm(
        translation_right, axis=1
    )
    cosine = np.divide(
        np.sum(translation_left * translation_right, axis=1),
        denominators,
        out=np.zeros_like(denominators),
        where=denominators > 1e-12,
    )
    return {
        "mean_action_l2_delta": float(np.mean(np.linalg.norm(delta, axis=1))),
        "mean_translation_l2_delta": float(np.mean(np.linalg.norm(delta[:, :3], axis=1))),
        "mean_translation_cosine_similarity": float(np.mean(cosine)),
        "gripper_sign_mismatch_fraction": float(
            np.mean(np.signbit(left_array[:, 6]) != np.signbit(right_array[:, 6]))
        ),
    }


def _mean(items: Sequence[float]) -> float:
    return float(sum(items) / len(items))


def execute_live(
    *,
    source_root: Path,
    checkpoint_path: Path,
    tokenizer_path: Path,
    nominal_report_path: Path,
    successful_source_snapshot_path: Path | None,
    output_dir: Path,
    pose_action_counts: Sequence[int] = DEFAULT_POSE_ACTION_COUNTS,
    displacements_metres: Sequence[float] = DEFAULT_DISPLACEMENTS_METRES,
    seeds: Sequence[int] = DEFAULT_SEEDS,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("cosmos_pose_sensitivity_opt_in_required")
    if output_dir.exists():
        raise ValueError("cosmos_pose_sensitivity_output_exists")
    poses_grid, distances, seed_grid = _validate_grid(
        pose_action_counts=pose_action_counts,
        displacements_metres=displacements_metres,
        seeds=seeds,
    )
    nominal = _verify_nominal_report(nominal_report_path)
    if poses_grid[-1] != nominal["applied_action_count"]:
        raise ValueError("cosmos_pose_sensitivity_terminal_pose_count_mismatch")
    output_dir.mkdir(parents=True)
    poses, first_success_state = _replay_nominal(
        nominal_report=nominal, pose_action_counts=poses_grid
    )
    if successful_source_snapshot_path is None:
        successful_state, normalization = _normalize_successful_source_robot(
            successful_state=first_success_state, initial_pose=poses[0]
        )
        successful_source_evidence = {
            "source": "exact_replayed_nominal_first_success_state",
            "nominal_report_sha256": nominal["result_sha256"],
            "robot_normalization": normalization,
            "local_path_recorded": False,
        }
    else:
        successful_state, successful_source_evidence = _read_successful_source_snapshot(
            successful_source_snapshot_path
        )
    source = _successful_source_material(successful_state)
    fixtures = _construct_displacement_states(
        successful_state=successful_state, source=source, distances=distances
    )
    invalid = [item for item in fixtures if not item["valid_for_sensitivity_map"]]
    if invalid:
        _write_json(
            output_dir / "invalid-fixtures.json",
            {
                "schema_version": "missionos.cosmos_policy_pose_sensitivity_invalid.v1",
                "fixtures": [
                    {key: value for key, value in item.items() if key != "simulator_state"}
                    for item in fixtures
                ],
                "model_loaded": False,
                "result_sha256": canonical_sha256(
                    {
                        "fixtures": [
                            {
                                key: value
                                for key, value in item.items()
                                if key != "simulator_state"
                            }
                            for item in fixtures
                        ]
                    }
                ),
            },
        )
        raise RuntimeError("cosmos_pose_sensitivity_fixture_grid_invalid")

    cfg, model, dataset_stats, checkpoint_evidence = _build_model_runtime(
        source_root=source_root,
        checkpoint_path=checkpoint_path,
        tokenizer_path=tokenizer_path,
        process_seed=seed_grid[0],
    )
    import numpy as np

    conditions = []
    raw_actions: dict[tuple[int, float, int], Any] = {}
    raw_observations: dict[tuple[int, float], dict[str, Any]] = {}
    query_index = 0
    for pose in poses:
        for fixture in fixtures:
            distance = float(fixture["requested_displacement_metres"])
            environment, init_states, _ = _make_environment()
            try:
                environment.reset()
                environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
                environment.regenerate_obs_from_state(fixture["simulator_state"])
                position_indexes, velocity_indexes = _robot_indexes(environment)
                environment.sim.data.qpos[position_indexes] = np.asarray(
                    pose["robot_qpos"], dtype=np.float64
                )
                environment.sim.data.qvel[velocity_indexes] = np.asarray(
                    pose["robot_qvel"], dtype=np.float64
                )
                environment.sim.forward()
                environment.env._post_process()
                environment.env._update_observables(force=True)
                transplanted_state = np.asarray(
                    environment.sim.get_state().flatten(), dtype=np.float64
                ).copy()
                observation = environment.regenerate_obs_from_state(transplanted_state)
                vector = _vector(environment)
                if vector != fixture["goal_predicate_vector"]:
                    raise RuntimeError("cosmos_pose_sensitivity_pose_transplant_predicate_drift")
                condition_key = (int(pose["action_count"]), distance)
                raw_observations[condition_key] = {
                    "agentview_image": np.asarray(observation["agentview_image"]).copy(),
                    "robot0_eye_in_hand_image": np.asarray(
                        observation["robot0_eye_in_hand_image"]
                    ).copy(),
                }
                condition_dir = (
                    output_dir
                    / "conditions"
                    / f"pose-{pose['action_count']:04d}"
                    / f"displacement-{distance:.6f}"
                )
                actual = _save_actual_observation(
                    observation=observation,
                    directory=condition_dir / "actual",
                    artifact_root=output_dir,
                    step_number=0,
                )
                queries = []
                for seed in seed_grid:
                    result, _ = _query_policy(
                        cfg=cfg,
                        model=model,
                        dataset_stats=dataset_stats,
                        observation=observation,
                        instruction=nominal["instruction"],
                        seed=seed,
                    )
                    actions = np.asarray(result["actions"], dtype=np.float32)
                    raw_actions[(int(pose["action_count"]), distance, seed)] = actions
                    future = _save_future_prediction(
                        predictions=result["future_image_predictions"],
                        value_prediction=result["value_prediction"],
                        directory=condition_dir / "future" / f"seed-{seed}",
                        artifact_root=output_dir,
                        query_index=query_index,
                        applied_action_start_index=0,
                    )
                    queries.append(
                        {
                            "seed": seed,
                            "actions": actions.tolist(),
                            "action_sha256": canonical_sha256(
                                {"actions": actions.tolist()}
                            ),
                            "future_prediction": future,
                        }
                    )
                    query_index += 1
                conditions.append(
                    {
                        "pose_action_count": int(pose["action_count"]),
                        "phase_fraction": pose["phase_fraction"],
                        "requested_displacement_metres": distance,
                        "observed_displacement_metres": fixture[
                            "observed_displacement_metres"
                        ],
                        "injected_translation_preserved_after_settle": fixture[
                            "injected_translation_preserved_after_settle"
                        ],
                        "goal_predicate_vector": vector,
                        "actual_observation": actual,
                        "queries": queries,
                    }
                )
            finally:
                environment.close()

    comparisons = []
    pose_noise = {}
    for pose in poses:
        pose_count = int(pose["action_count"])
        noise_deltas = [
            _action_delta(
                raw_actions[(pose_count, 0.0, left)],
                raw_actions[(pose_count, 0.0, right)],
            )["mean_action_l2_delta"]
            for left, right in combinations(seed_grid, 2)
        ]
        pose_noise[pose_count] = _mean(noise_deltas)
        baseline_images = raw_observations[(pose_count, 0.0)]
        for distance in distances[1:]:
            per_seed = [
                _action_delta(
                    raw_actions[(pose_count, 0.0, seed)],
                    raw_actions[(pose_count, distance, seed)],
                )
                for seed in seed_grid
            ]
            signal = _mean([item["mean_action_l2_delta"] for item in per_seed])
            image_differences = {
                key: float(
                    np.mean(
                        np.abs(
                            np.asarray(
                                raw_observations[(pose_count, distance)][key], dtype=np.float32
                            )
                            - np.asarray(baseline_images[key], dtype=np.float32)
                        )
                    )
                )
                for key in baseline_images
            }
            comparisons.append(
                {
                    "pose_action_count": pose_count,
                    "phase_fraction": pose["phase_fraction"],
                    "requested_displacement_metres": distance,
                    "observed_displacement_metres": next(
                        float(item["observed_displacement_metres"])
                        for item in fixtures
                        if float(item["requested_displacement_metres"]) == distance
                    ),
                    "same_seed_paired_action_signal": {
                        key: _mean([item[key] for item in per_seed]) for key in per_seed[0]
                    },
                    "baseline_seed_pair_mean_action_l2_noise": pose_noise[pose_count],
                    "action_signal_to_seed_noise_ratio": (
                        signal / pose_noise[pose_count] if pose_noise[pose_count] > 1e-12 else None
                    ),
                    "actual_observation_mean_absolute_pixel_difference": image_differences,
                }
            )

    report_without_digest = {
        "schema_version": "missionos.cosmos_policy_libero_paired_pose_sensitivity.v1",
        "status": "forward_only_paired_pose_sensitivity_completed",
        "task_suite": TASK_SUITE,
        "task_name": TASK_NAME,
        "task_id": TASK_ID,
        "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
        "instruction": nominal["instruction"],
        "nominal_report_sha256": nominal["result_sha256"],
        "nominal_trace_replayed_exactly": True,
        "nominal_runner_stops_on_first_predicate_success": True,
        "nominal_last_actions_are_pre_success_not_post_success_retreat": True,
        "pose_checkpoints": poses,
        "successful_source_snapshot": successful_source_evidence,
        "successful_source_actual": source,
        "fixture_grid": [
            {
                key: value
                for key, value in item.items()
                if key not in {"simulator_state", "settle_trace"}
            }
            for item in fixtures
        ],
        "seeds": list(seed_grid),
        "forward_query_count": query_index,
        "conditions": conditions,
        "paired_comparisons": comparisons,
        "checkpoint": checkpoint_evidence,
        "additional_training_performed": False,
        "claim_boundary": {
            "authority": "diagnostic_only",
            "queried_actions_applied_to_simulator": False,
            "simulator_state_directly_changed_for_diagnostic": True,
            "future_predictions_may_establish_success": False,
            "repair_attempted": False,
            "repair_success_established": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        },
    }
    report = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    _write_json(output_dir / "paired-pose-sensitivity.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--checkpoint-path", type=Path, required=True)
    parser.add_argument("--tokenizer-path", type=Path, required=True)
    parser.add_argument("--nominal-report", type=Path, required=True)
    parser.add_argument("--successful-source-snapshot", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--pose-action-counts",
        default=",".join(str(value) for value in DEFAULT_POSE_ACTION_COUNTS),
    )
    parser.add_argument(
        "--displacements-metres",
        default=",".join(str(value) for value in DEFAULT_DISPLACEMENTS_METRES),
    )
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    args = parser.parse_args()
    result = execute_live(
        source_root=args.source_root.resolve(),
        checkpoint_path=args.checkpoint_path.resolve(),
        tokenizer_path=args.tokenizer_path.resolve(),
        nominal_report_path=args.nominal_report.resolve(),
        successful_source_snapshot_path=(
            args.successful_source_snapshot.resolve()
            if args.successful_source_snapshot is not None
            else None
        ),
        output_dir=args.output_dir.resolve(),
        pose_action_counts=tuple(int(value) for value in args.pose_action_counts.split(",")),
        displacements_metres=tuple(float(value) for value in args.displacements_metres.split(",")),
        seeds=tuple(int(value) for value in args.seeds.split(",")),
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "forward_query_count": result["forward_query_count"],
                "paired_comparisons": result["paired_comparisons"],
                "result_sha256": result["result_sha256"],
                "claim_boundary": result["claim_boundary"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

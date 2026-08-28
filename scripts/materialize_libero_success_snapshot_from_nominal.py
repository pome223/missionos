#!/usr/bin/env python3
"""Replay a digest-bound nominal trace and save its exact first-success state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_SUCCESS_SNAPSHOT_REPLAY"
TASK_NAME = "KITCHEN_SCENE8_put_both_moka_pots_on_the_stove"
TASK_SUITE = "libero_10"
TASK_ID = 8
EPISODE_INIT_STATE_INDEX = 15
ENVIRONMENT_SEED = 0
OFFICIAL_STABILIZATION_STEPS = 10


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


def _make_environment() -> tuple[Any, Any]:
    from libero.libero import benchmark, get_libero_path
    from libero.libero.envs import OffScreenRenderEnv

    suite = benchmark.get_benchmark_dict()[TASK_SUITE]()
    task = suite.get_task(TASK_ID)
    if task.name != TASK_NAME:
        raise RuntimeError("libero_success_snapshot_task_identity_mismatch")
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


def _vector(environment: Any) -> list[bool]:
    return [
        bool(environment.env._eval_predicate(state))
        for state in environment.env.parsed_problem["goal_state"]
    ]


def execute_live(*, nominal_report_path: Path, output_path: Path) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("libero_success_snapshot_opt_in_required")
    if output_path.exists():
        raise ValueError("libero_success_snapshot_output_exists")
    import numpy as np

    report = json.loads(nominal_report_path.read_text(encoding="utf-8"))
    supplied = report.get("result_sha256")
    material = {key: value for key, value in report.items() if key != "result_sha256"}
    actions = report.get("action_trace")
    predicates = report.get("predicate_trace")
    if supplied != _canonical_sha256(material):
        raise RuntimeError("libero_success_snapshot_nominal_digest_mismatch")
    if (
        report.get("schema_version") != "missionos.cosmos_policy_libero_nominal.v1"
        or report.get("environment_seed") != ENVIRONMENT_SEED
        or report.get("episode_init_state_index") != EPISODE_INIT_STATE_INDEX
        or report.get("nominal_success_observed") is not True
        or not isinstance(actions, list)
        or not isinstance(predicates, list)
        or len(actions) != len(predicates)
    ):
        raise RuntimeError("libero_success_snapshot_nominal_contract_invalid")

    environment, init_states = _make_environment()
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        for _ in range(OFFICIAL_STABILIZATION_STEPS):
            environment.step([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0])
        robot = environment.env.robots[0]
        position_indexes = list(robot._ref_joint_pos_indexes) + list(
            robot._ref_gripper_joint_pos_indexes
        )
        velocity_indexes = list(robot._ref_joint_vel_indexes) + list(
            robot._ref_gripper_joint_vel_indexes
        )
        initial_robot_qpos = np.asarray(
            environment.sim.data.qpos[position_indexes], dtype=np.float64
        ).copy()
        initial_robot_qvel = np.asarray(
            environment.sim.data.qvel[velocity_indexes], dtype=np.float64
        ).copy()
        success_action = None
        for index, (action, expected) in enumerate(zip(actions, predicates), start=1):
            environment.step(action["action_7d"])
            vector = _vector(environment)
            expected_vector = [
                item["satisfied"] for item in expected["goal_predicate_observations"]
            ]
            if vector != expected_vector:
                raise RuntimeError(f"libero_success_snapshot_nominal_replay_drift:{index}")
            if vector == [True, True, True]:
                success_action = index
                break
        if success_action is None:
            raise RuntimeError("libero_success_snapshot_success_not_replayed")
        environment.sim.data.qpos[position_indexes] = initial_robot_qpos
        environment.sim.data.qvel[velocity_indexes] = initial_robot_qvel
        environment.sim.forward()
        environment.env._post_process()
        environment.env._update_observables(force=True)
        if _vector(environment) != [True, True, True]:
            raise RuntimeError("libero_success_snapshot_normalized_source_predicate_drift")
        state = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        metadata = {
            "schema_version": "missionos.libero_success_snapshot_from_nominal.v1",
            "authority": "diagnostic_fixture_source_only",
            "task_suite": TASK_SUITE,
            "task_name": TASK_NAME,
            "task_id": TASK_ID,
            "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
            "environment_seed": ENVIRONMENT_SEED,
            "source_goal_predicate_vector": [True, True, True],
            "nominal_report_sha256": _sha256_path(nominal_report_path),
            "nominal_result_sha256": supplied,
            "first_success_after_action": success_action,
            "robot_pose_normalized_to_nominal_initial": True,
            "normalization_settle_steps": 0,
            "normalization_stability_claimed": False,
            "simulator_state_sha256": hashlib.sha256(state.tobytes()).hexdigest(),
            "simulator_state_value_count": int(state.size),
            "model_inference_invoked_during_replay": False,
            "physical_execution_invoked": False,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("xb") as stream:
            np.savez_compressed(
                stream,
                simulator_state=state,
                metadata_json=np.asarray(json.dumps(metadata, sort_keys=True)),
            )
        return {**metadata, "snapshot_artifact_sha256": _sha256_path(output_path)}
    finally:
        environment.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nominal-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(execute_live(
        nominal_report_path=args.nominal_report.resolve(),
        output_path=args.output.resolve(),
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

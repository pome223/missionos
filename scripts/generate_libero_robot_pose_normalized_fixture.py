#!/usr/bin/env python3
"""Create a diagnostic clone with fixture objects but task-initial robot pose."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402
from scripts.run_cosmos_policy_libero_experiment import (  # noqa: E402
    EPISODE_INIT_STATE_INDEX,
    EXPECTED_REPAIR_SOURCE_VECTOR,
    TASK_ID,
    TASK_SUITE,
    _make_environment,
)
from scripts.run_groot_lerobot_same_world_repair import (  # noqa: E402
    _read_failure_snapshot,
    _sha256_path,
    _write_failure_snapshot,
)
from scripts.run_vla0_libero_snapshot_recovery import _predicate_material  # noqa: E402


OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_ROBOT_POSE_NORMALIZED_FIXTURE"
SOURCE_BASIS = "diagnostic_displacement_curriculum"
OUTPUT_BASIS = "diagnostic_robot_pose_normalized_curriculum"
SCHEMA_VERSION = "missionos.libero_robot_pose_normalized_fixture.v1"
SETTLE_STEPS = 10
OBJECT_DISPLACEMENT_LIMIT_METRES = 0.001


def execute_live(
    *, source_snapshot_path: Path, output_snapshot_path: Path, output_report_path: Path
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("libero_robot_pose_normalized_fixture_opt_in_required")
    if output_snapshot_path.exists() or output_report_path.exists():
        raise ValueError("libero_robot_pose_normalized_fixture_output_exists")

    import numpy as np

    source_state, source_metadata = _read_failure_snapshot(source_snapshot_path)
    base_fixture = source_metadata.get("displacement_curriculum_fixture")
    if (
        source_metadata.get("source_failure_basis") != SOURCE_BASIS
        or not isinstance(base_fixture, dict)
        or source_metadata.get("displacement_curriculum_fixture_sha256")
        != canonical_sha256(base_fixture)
        or base_fixture.get("terminal_goal_predicate_vector") != EXPECTED_REPAIR_SOURCE_VECTOR
    ):
        raise RuntimeError("libero_robot_pose_normalized_fixture_source_invalid")

    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    environment, init_states, _ = _make_environment()
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
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

        observation = environment.regenerate_obs_from_state(source_state)
        if not np.array_equal(environment.sim.get_state().flatten(), source_state):
            raise RuntimeError("libero_robot_pose_normalized_fixture_restore_not_exact")
        source_predicates = _predicate_material(environment)
        if [item["satisfied"] for item in source_predicates] != EXPECTED_REPAIR_SOURCE_VECTOR:
            raise RuntimeError("libero_robot_pose_normalized_fixture_source_vector_invalid")
        simulator = environment.env
        object_ids = {
            name: int(simulator.obj_body_id[name]) for name in ("moka_pot_1", "moka_pot_2")
        }
        source_positions = {
            name: np.asarray(simulator.sim.data.body_xpos[body_id], dtype=np.float64).copy()
            for name, body_id in object_ids.items()
        }
        source_robot_qpos = np.asarray(
            environment.sim.data.qpos[position_indexes], dtype=np.float64
        ).copy()

        environment.sim.data.qpos[position_indexes] = initial_robot_qpos
        environment.sim.data.qvel[velocity_indexes] = initial_robot_qvel
        environment.sim.forward()
        simulator._post_process()
        simulator._update_observables(force=True)

        settle_trace = []
        for step_index in range(SETTLE_STEPS):
            observation, _, done, info = environment.step(np.zeros(7, dtype=np.float64))
            vector = [item["satisfied"] for item in _predicate_material(environment)]
            settle_trace.append(
                {
                    "fixture_step_index": step_index,
                    "predicate_vector": vector,
                    "environment_done": bool(done),
                    "environment_info_success": bool(info.get("success", False)),
                }
            )
        terminal_predicates = _predicate_material(environment)
        terminal_vector = [item["satisfied"] for item in terminal_predicates]
        object_displacements = {
            name: float(
                np.linalg.norm(
                    np.asarray(simulator.sim.data.body_xpos[body_id], dtype=np.float64)
                    - source_positions[name]
                )
            )
            for name, body_id in object_ids.items()
        }
        if terminal_vector != EXPECTED_REPAIR_SOURCE_VECTOR:
            raise RuntimeError("libero_robot_pose_normalized_fixture_terminal_vector_invalid")
        if any(value > OBJECT_DISPLACEMENT_LIMIT_METRES for value in object_displacements.values()):
            raise RuntimeError("libero_robot_pose_normalized_fixture_object_moved")

        terminal_state = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        normalization = {
            "schema_version": SCHEMA_VERSION,
            "authority": "diagnostic_fixture_only",
            "source_snapshot_sha256": _sha256_path(source_snapshot_path),
            "source_fixture_sha256": canonical_sha256(base_fixture),
            "robot_position_indexes": [int(value) for value in position_indexes],
            "robot_velocity_indexes": [int(value) for value in velocity_indexes],
            "source_robot_qpos_sha256": hashlib.sha256(source_robot_qpos.tobytes()).hexdigest(),
            "initial_robot_qpos_sha256": hashlib.sha256(initial_robot_qpos.tobytes()).hexdigest(),
            "robot_pose_changed": not np.array_equal(source_robot_qpos, initial_robot_qpos),
            "terminal_goal_predicate_observations": terminal_predicates,
            "terminal_goal_predicate_vector": terminal_vector,
            "object_displacements_metres": object_displacements,
            "maximum_object_displacement_metres": max(object_displacements.values()),
            "maximum_object_displacement_limit_metres": OBJECT_DISPLACEMENT_LIMIT_METRES,
            "fixture_settle_steps_applied": len(settle_trace),
            "fixture_settle_trace": settle_trace,
            "simulator_state_directly_changed_for_diagnostic": True,
            "model_inference_invoked": False,
            "repair_attempted": False,
            "physical_execution_invoked": False,
        }
        normalization_sha256 = canonical_sha256(normalization)
        snapshot = _write_failure_snapshot(
            path=output_snapshot_path,
            simulator_state=terminal_state,
            metadata={
                "task_suite": TASK_SUITE,
                "task_id": TASK_ID,
                "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
                "source_failure_basis": OUTPUT_BASIS,
                "source_goal_predicate_observations": terminal_predicates,
                "source_goal_predicate_vector": terminal_vector,
                "source_goal_predicate_vector_sha256": canonical_sha256(
                    {"goal_predicate_observations": terminal_predicates}
                ),
                "source_failure_is_repair_candidate": True,
                "base_displacement_curriculum_fixture": base_fixture,
                "base_displacement_curriculum_fixture_sha256": canonical_sha256(base_fixture),
                "robot_pose_normalization": normalization,
                "robot_pose_normalization_sha256": normalization_sha256,
                "model_runtime_invoked_for_snapshot_restore": False,
                "physical_execution_invoked": False,
            },
        )
        report_without_digest = {
            "schema_version": SCHEMA_VERSION,
            "status": "diagnostic_robot_pose_normalized_fixture_created",
            "snapshot": snapshot,
            "normalization": normalization,
            "normalization_sha256": normalization_sha256,
            "claim_boundary": {
                "authority": "diagnostic_only",
                "repair_claim_eligible": False,
                "simulator_state_directly_changed": True,
                "model_inference_invoked": False,
                "repair_attempted": False,
                "physical_execution_invoked": False,
            },
        }
        report = {
            **report_without_digest,
            "result_sha256": canonical_sha256(report_without_digest),
        }
        output_report_path.parent.mkdir(parents=True, exist_ok=True)
        output_report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return report
    finally:
        environment.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-snapshot", type=Path, required=True)
    parser.add_argument("--output-snapshot", type=Path, required=True)
    parser.add_argument("--output-report", type=Path, required=True)
    args = parser.parse_args()
    result = execute_live(
        source_snapshot_path=args.source_snapshot.resolve(),
        output_snapshot_path=args.output_snapshot.resolve(),
        output_report_path=args.output_report.resolve(),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

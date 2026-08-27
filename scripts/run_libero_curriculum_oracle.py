#!/usr/bin/env python3
"""Prove diagnostic recoverability of a curriculum fixture through raw 7D actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_CURRICULUM_ORACLE"
TASK_NAME = "KITCHEN_SCENE8_put_both_moka_pots_on_the_stove"
TASK_SUITE = "libero_10"
TASK_ID = 8
EPISODE_INIT_STATE_INDEX = 15
ENVIRONMENT_SEED = 7
TARGET_OBJECT = "moka_pot_2"
PROTECTED_OBJECT = "moka_pot_1"
MAXIMUM_ACTIONS = 320
STABLE_SUCCESS_STEPS = 20
PROTECTED_MAXIMUM_DISPLACEMENT_METRES = 0.005
CURRICULUM_BASIS = "diagnostic_displacement_curriculum"
ROBOT_POSE_NORMALIZED_BASIS = "diagnostic_robot_pose_normalized_curriculum"


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


def _predicate_material(environment: Any) -> list[dict[str, Any]]:
    task_environment = environment.env
    material = []
    for index, state in enumerate(task_environment.parsed_problem["goal_state"]):
        spec = tuple(str(part).casefold() for part in state)
        value = task_environment._eval_predicate(state)
        if hasattr(value, "item"):
            value = value.item()
        if not isinstance(value, bool):
            raise RuntimeError("libero_curriculum_oracle_predicate_not_boolean")
        material.append(
            {
                "predicate_index": index,
                "predicate_name": spec[0],
                "arguments": list(spec[1:]),
                "satisfied": value,
            }
        )
    return material


def _make_environment() -> tuple[Any, Any, str]:
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
        return environment, suite.get_task_init_states(task_index), task.language
    raise RuntimeError("libero_curriculum_oracle_task_not_found")


def execute_live(*, snapshot_path: Path, output_dir: Path) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("libero_curriculum_oracle_opt_in_required")
    if output_dir.exists():
        raise ValueError("libero_curriculum_oracle_output_exists")

    import numpy as np
    from PIL import Image

    with np.load(snapshot_path, allow_pickle=False) as archive:
        snapshot = np.asarray(archive["simulator_state"], dtype=np.float64).reshape(-1)
        metadata = json.loads(str(archive["metadata_json"].item()))
    if metadata.get("simulator_state_sha256") != hashlib.sha256(snapshot.tobytes()).hexdigest():
        raise RuntimeError("libero_curriculum_oracle_snapshot_state_digest_mismatch")
    source_basis = metadata.get("source_failure_basis")
    simulator_state_directly_changed = False
    if source_basis == CURRICULUM_BASIS:
        fixture = metadata.get("displacement_curriculum_fixture")
        fixture_sha256 = metadata.get("displacement_curriculum_fixture_sha256")
    elif source_basis == ROBOT_POSE_NORMALIZED_BASIS:
        fixture = metadata.get("base_displacement_curriculum_fixture")
        fixture_sha256 = metadata.get("base_displacement_curriculum_fixture_sha256")
        normalization = metadata.get("robot_pose_normalization")
        if (
            not isinstance(normalization, dict)
            or metadata.get("robot_pose_normalization_sha256") != _canonical_sha256(normalization)
            or normalization.get("simulator_state_directly_changed_for_diagnostic") is not True
            or normalization.get("terminal_goal_predicate_vector") != [True, False, True]
        ):
            raise RuntimeError("libero_curriculum_oracle_normalization_contract_invalid")
        simulator_state_directly_changed = True
    else:
        raise RuntimeError("libero_curriculum_oracle_fixture_basis_invalid")
    if (
        not isinstance(fixture, dict)
        or fixture.get("actual_predicate_failure_observed") is not True
        or fixture.get("terminal_goal_predicate_vector") != [True, False, True]
    ):
        raise RuntimeError("libero_curriculum_oracle_fixture_contract_invalid")
    if fixture_sha256 != _canonical_sha256(fixture):
        raise RuntimeError("libero_curriculum_oracle_fixture_digest_mismatch")

    os.environ.setdefault("MUJOCO_GL", "osmesa")
    os.environ.setdefault("PYOPENGL_PLATFORM", "osmesa")
    output_dir.mkdir(parents=True)
    frame_dir = output_dir / "frames"
    frame_dir.mkdir()
    environment, init_states, instruction = _make_environment()
    trace: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    action_count = 0
    success_first_action: int | None = None
    preservation_violation = False
    observation: dict[str, Any]
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        observation = environment.regenerate_obs_from_state(snapshot)
        if not np.array_equal(environment.sim.get_state().flatten(), snapshot):
            raise RuntimeError("libero_curriculum_oracle_snapshot_restore_not_exact")
        source_predicates = _predicate_material(environment)
        source_vector = [item["satisfied"] for item in source_predicates]
        if source_vector != [True, False, True]:
            raise RuntimeError("libero_curriculum_oracle_source_vector_invalid")

        simulator = environment.env
        target_body = int(simulator.obj_body_id[TARGET_OBJECT])
        protected_body = int(simulator.obj_body_id[PROTECTED_OBJECT])
        initial_target = np.asarray(
            simulator.sim.data.body_xpos[target_body], dtype=np.float64
        ).copy()
        initial_protected = np.asarray(
            simulator.sim.data.body_xpos[protected_body], dtype=np.float64
        ).copy()
        desired_target = np.asarray(fixture["source_target_position_metres"], dtype=np.float64)

        def capture(label: str) -> str:
            path = frame_dir / f"{action_count:04d}-{label}.png"
            pixels = simulator.sim.render(camera_name="agentview", height=512, width=512)
            Image.fromarray(np.asarray(pixels, dtype=np.uint8)[::-1]).save(path)
            return _sha256_path(path)

        def state_material() -> dict[str, Any]:
            target = np.asarray(simulator.sim.data.body_xpos[target_body], dtype=np.float64)
            protected = np.asarray(simulator.sim.data.body_xpos[protected_body], dtype=np.float64)
            return {
                "predicate_vector": [
                    item["satisfied"] for item in _predicate_material(environment)
                ],
                "eef_position_metres": np.asarray(
                    observation["robot0_eef_pos"], dtype=np.float64
                ).tolist(),
                "target_position_metres": target.tolist(),
                "protected_position_metres": protected.tolist(),
                "protected_displacement_metres": float(
                    np.linalg.norm(protected - initial_protected)
                ),
            }

        def apply(stage: str, action: Any) -> dict[str, Any]:
            nonlocal action_count, observation, success_first_action
            nonlocal preservation_violation
            array = np.asarray(action, dtype=np.float64)
            observation, _, done, info = environment.step(array.tolist())
            action_count += 1
            state = state_material()
            if (
                state["protected_displacement_metres"] > PROTECTED_MAXIMUM_DISPLACEMENT_METRES
                or state["predicate_vector"][0] is not True
                or state["predicate_vector"][2] is not True
            ):
                preservation_violation = True
            if state["predicate_vector"] == [True, True, True] and success_first_action is None:
                success_first_action = action_count
            trace.append(
                {
                    "action_index": action_count,
                    "stage": stage,
                    "action_7d": array.tolist(),
                    "done": bool(done),
                    "info_success": bool(info.get("success", False)),
                    **state,
                }
            )
            return state

        def move_to(
            stage: str,
            target: Any,
            maximum: int,
            *,
            stop_on_success: bool = True,
            success_seating_steps: int = 0,
        ) -> None:
            target_array = np.asarray(target, dtype=np.float64)
            start = action_count
            minimum_error = float("inf")
            seated_success_steps = 0
            for _ in range(maximum):
                if action_count >= MAXIMUM_ACTIONS or preservation_violation:
                    break
                eef = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
                error = target_array - eef
                error_norm = float(np.linalg.norm(error))
                minimum_error = min(minimum_error, error_norm)
                if error_norm <= 0.012:
                    break
                action = np.zeros(7, dtype=np.float64)
                action[:3] = np.clip(error / 0.05, -1.0, 1.0)
                action[6] = -1.0
                state = apply(stage, action)
                if state["predicate_vector"] == [True, True, True]:
                    if stop_on_success:
                        break
                    seated_success_steps += 1
                    if seated_success_steps >= success_seating_steps:
                        break
            stages.append(
                {
                    "stage": stage,
                    "target_metres": target_array.tolist(),
                    "actions_applied": action_count - start,
                    "minimum_position_error_metres": minimum_error,
                }
            )
            capture(stage)

        capture("source")
        move_to("hover_left_of_target", initial_target + np.array([-0.065, 0.0, 0.15]), 120)
        move_to("descend_left_of_target", initial_target + np.array([-0.055, 0.0, 0.032]), 100)
        move_to(
            "push_target_to_source_success_position",
            desired_target + np.array([0.035, 0.0, 0.032]),
            80,
            stop_on_success=False,
            success_seating_steps=2,
        )

        stable_steps = 0
        if success_first_action is not None and not preservation_violation:
            for _ in range(STABLE_SUCCESS_STEPS):
                if action_count >= MAXIMUM_ACTIONS:
                    break
                state = apply("stable_success_settle", np.array([0, 0, 0, 0, 0, 0, -1]))
                if state["predicate_vector"] != [True, True, True]:
                    break
                stable_steps += 1

        terminal = state_material()
        terminal_frame_sha256 = capture("terminal")
        stable_success = bool(
            success_first_action is not None
            and stable_steps == STABLE_SUCCESS_STEPS
            and terminal["predicate_vector"] == [True, True, True]
            and not preservation_violation
        )
        trace_path = output_dir / "raw-7d-actions.json"
        trace_path.write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n")
        result_without_digest = {
            "schema_version": "missionos.vla0_same_interface_oracle_recoverability.v1",
            "status": (
                "scripted_oracle_recoverability_established"
                if stable_success
                else "scripted_oracle_recoverability_not_established"
            ),
            "task_name": TASK_NAME,
            "instruction": instruction,
            "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
            "environment_seed": ENVIRONMENT_SEED,
            "snapshot_sha256": _sha256_path(snapshot_path),
            "source_failure_basis": source_basis,
            "source_goal_predicate_vector": source_vector,
            "terminal_goal_predicate_vector": terminal["predicate_vector"],
            "maximum_action_budget": MAXIMUM_ACTIONS,
            "actions_applied": action_count,
            "success_first_observed_after_action": success_first_action,
            "stable_success_steps_required": STABLE_SUCCESS_STEPS,
            "stable_success_steps_completed": stable_steps,
            "stable_success_observed": stable_success,
            "preservation_violation_observed": preservation_violation,
            "protected_maximum_displacement_metres": max(
                (item["protected_displacement_metres"] for item in trace), default=0.0
            ),
            "initial_target_position_metres": initial_target.tolist(),
            "desired_target_position_metres": desired_target.tolist(),
            "terminal_target_position_metres": terminal["target_position_metres"],
            "stage_summaries": stages,
            "raw_action_trace_sha256": _sha256_path(trace_path),
            "raw_action_count": len(trace),
            "terminal_frame_sha256": terminal_frame_sha256,
            "claim_boundary": {
                "authority": "diagnostic_only",
                "privileged_object_state_used_for_oracle_planning": True,
                "same_original_7d_action_interface_used": True,
                "simulator_state_directly_changed_during_fixture_construction": (
                    simulator_state_directly_changed
                ),
                "simulator_state_directly_changed_after_fixture_restore": False,
                "model_inference_invoked": False,
                "missionos_proposal_created": False,
                "human_approval_created": False,
                "governed_dispatch_created": False,
                "controller_ack_observed": False,
                "physical_execution_invoked": False,
            },
        }
        result = {
            **result_without_digest,
            "result_sha256": _canonical_sha256(result_without_digest),
        }
        (output_dir / "result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return result
    finally:
        environment.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = execute_live(
        snapshot_path=args.snapshot.resolve(), output_dir=args.output_dir.resolve()
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["stable_success_observed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

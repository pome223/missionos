#!/usr/bin/env python3
"""Test book/caddy fixture recoverability with a privileged 7D controller."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from scripts.probe_libero_book_caddy_fixture import (
    CONTAINER_OBJECT,
    ENVIRONMENT_SEED,
    EPISODE_INIT_STATE_INDEX,
    FIXTURE_SCHEMA_VERSION,
    PROTECTED_OBJECT,
    TARGET_OBJECT,
    TASK_ID,
    TASK_NAME,
    TASK_SUITE,
    _make_environment,
    _predicate_material,
    canonical_sha256,
)


OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_BOOK_CADDY_ORACLE"
MAXIMUM_ACTIONS = 320
STABLE_SUCCESS_STEPS = 20
MAXIMUM_PROTECTED_DISPLACEMENT_METRES = 0.005
RESTORE_MAXIMUM_ABSOLUTE_ERROR = 1e-12


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_snapshot(path: Path) -> tuple[Any, dict[str, Any]]:
    import numpy as np

    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != {"simulator_state", "metadata_json"}:
            raise RuntimeError("book_caddy_oracle_snapshot_members_invalid")
        state = np.asarray(archive["simulator_state"], dtype=np.float64).reshape(-1)
        metadata = json.loads(str(archive["metadata_json"].item()))
    if metadata.get("simulator_state_sha256") != hashlib.sha256(state.tobytes()).hexdigest():
        raise RuntimeError("book_caddy_oracle_snapshot_state_digest_mismatch")
    fixture = metadata.get("book_caddy_fixture")
    if (
        metadata.get("task_suite") != TASK_SUITE
        or metadata.get("task_id") != TASK_ID
        or metadata.get("task_name") != TASK_NAME
        or metadata.get("episode_init_state_index") != EPISODE_INIT_STATE_INDEX
        or metadata.get("environment_seed") != ENVIRONMENT_SEED
        or metadata.get("source_failure_basis") != "diagnostic_book_caddy_displacement"
        or metadata.get("source_goal_predicate_vector") != [False]
        or metadata.get("source_failure_is_repair_candidate") is not True
        or not isinstance(fixture, dict)
        or fixture.get("schema_version") != FIXTURE_SCHEMA_VERSION
        or fixture.get("fixture_admitted") is not True
        or fixture.get("terminal_goal_predicate_vector") != [False]
        or metadata.get("book_caddy_fixture_sha256") != canonical_sha256(fixture)
    ):
        raise RuntimeError("book_caddy_oracle_fixture_contract_invalid")
    return state, metadata


def execute_live(*, snapshot_path: Path, output_dir: Path) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("book_caddy_oracle_opt_in_required")
    if output_dir.exists():
        raise ValueError("book_caddy_oracle_output_exists")

    import numpy as np
    from PIL import Image

    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    snapshot, metadata = _read_snapshot(snapshot_path)
    fixture = metadata["book_caddy_fixture"]
    environment, init_states, instruction = _make_environment()
    output_dir.mkdir(parents=True)
    frame_dir = output_dir / "frames"
    frame_dir.mkdir()
    trace: list[dict[str, Any]] = []
    stages: list[dict[str, Any]] = []
    action_count = 0
    first_success: int | None = None
    observation: dict[str, Any]
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        observation = environment.regenerate_obs_from_state(snapshot)
        restored = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        difference = np.abs(restored - snapshot)
        restore_maximum_error = float(difference.max())
        if restore_maximum_error > RESTORE_MAXIMUM_ABSOLUTE_ERROR:
            raise RuntimeError("book_caddy_oracle_snapshot_restore_outside_tolerance")
        source_vector = [item["satisfied"] for item in _predicate_material(environment)]
        if source_vector != [False]:
            raise RuntimeError("book_caddy_oracle_source_vector_invalid")

        simulator = environment.env
        target_body = int(simulator.obj_body_id[TARGET_OBJECT])
        container_body = int(simulator.obj_body_id[CONTAINER_OBJECT])
        protected_body = int(simulator.obj_body_id[PROTECTED_OBJECT])
        initial_target = np.asarray(
            simulator.sim.data.body_xpos[target_body], dtype=np.float64
        ).copy()
        initial_container = np.asarray(
            simulator.sim.data.body_xpos[container_body], dtype=np.float64
        ).copy()
        initial_protected = np.asarray(
            simulator.sim.data.body_xpos[protected_body], dtype=np.float64
        ).copy()
        desired_target = np.asarray(
            fixture["source_target_position_metres"], dtype=np.float64
        )
        grasp_offset = None

        def capture(label: str) -> str:
            path = frame_dir / f"{action_count:04d}-{label}.png"
            pixels = simulator.sim.render(camera_name="agentview", height=512, width=512)
            Image.fromarray(np.asarray(pixels, dtype=np.uint8)[::-1]).save(path)
            return _sha256_path(path)

        def state_material() -> dict[str, Any]:
            target = np.asarray(simulator.sim.data.body_xpos[target_body], dtype=np.float64)
            container = np.asarray(
                simulator.sim.data.body_xpos[container_body], dtype=np.float64
            )
            protected = np.asarray(
                simulator.sim.data.body_xpos[protected_body], dtype=np.float64
            )
            eef = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
            return {
                "predicate_vector": [
                    item["satisfied"] for item in _predicate_material(environment)
                ],
                "eef_position_metres": eef.tolist(),
                "eef_target_distance_metres": float(np.linalg.norm(eef - target)),
                "target_position_metres": target.tolist(),
                "target_displacement_metres": float(np.linalg.norm(target - initial_target)),
                "target_gripper_contact_observed": bool(
                    simulator.check_contact(
                        simulator.get_object(TARGET_OBJECT), simulator.robots[0].gripper
                    )
                ),
                "container_displacement_metres": float(
                    np.linalg.norm(container - initial_container)
                ),
                "protected_object_displacement_metres": float(
                    np.linalg.norm(protected - initial_protected)
                ),
            }

        def apply(stage: str, action: Any) -> dict[str, Any]:
            nonlocal action_count, observation, first_success
            array = np.asarray(action, dtype=np.float64)
            observation, _, done, info = environment.step(array.tolist())
            action_count += 1
            state = state_material()
            if state["predicate_vector"] == [True] and first_success is None:
                first_success = action_count
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

        def move_to(stage: str, target: Any, maximum: int, gripper: float) -> None:
            target_array = np.asarray(target, dtype=np.float64)
            start = action_count
            minimum_error = float("inf")
            for _ in range(maximum):
                if action_count >= MAXIMUM_ACTIONS:
                    break
                eef = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
                error = target_array - eef
                error_norm = float(np.linalg.norm(error))
                minimum_error = min(minimum_error, error_norm)
                if error_norm <= 0.010:
                    break
                action = np.zeros(7, dtype=np.float64)
                action[:3] = np.clip(error / 0.05, -1.0, 1.0)
                action[6] = gripper
                state = apply(stage, action)
                if state["predicate_vector"] == [True]:
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

        def hold(stage: str, steps: int, gripper: float) -> None:
            start = action_count
            for _ in range(steps):
                if action_count >= MAXIMUM_ACTIONS:
                    break
                action = np.zeros(7, dtype=np.float64)
                action[6] = gripper
                apply(stage, action)
            stages.append({"stage": stage, "actions_applied": action_count - start})
            capture(stage)

        capture("source")
        move_to("hover_above_book", initial_target + np.array([0.0, 0.0, 0.14]), 70, 1.0)
        if first_success is None:
            move_to(
                "descend_to_book",
                initial_target + np.array([0.0, 0.0, 0.025]),
                60,
                1.0,
            )
        if first_success is None:
            hold("close_gripper", 14, -1.0)
            grasp_eef = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
            grasp_target = np.asarray(
                simulator.sim.data.body_xpos[target_body], dtype=np.float64
            )
            grasp_offset = grasp_eef - grasp_target
            move_to("lift_book", grasp_eef + np.array([0.0, 0.0, 0.15]), 70, -1.0)
            goal_eef = desired_target + grasp_offset
            move_to(
                "hover_above_back_compartment",
                goal_eef + np.array([0.0, 0.0, 0.14]),
                100,
                -1.0,
            )
            move_to(
                "lower_into_back_compartment",
                goal_eef + np.array([0.0, 0.0, 0.015]),
                80,
                -1.0,
            )
            hold("open_gripper", 14, 1.0)
            current_eef = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
            move_to(
                "retract_after_release",
                current_eef + np.array([0.0, 0.0, 0.12]),
                50,
                1.0,
            )

        stable_steps = 0
        for _ in range(STABLE_SUCCESS_STEPS):
            if action_count >= MAXIMUM_ACTIONS:
                break
            state = apply("stable_success_settle", np.array([0, 0, 0, 0, 0, 0, 1]))
            if state["predicate_vector"] != [True]:
                break
            stable_steps += 1

        terminal = state_material()
        maximum_container = max(
            (float(item["container_displacement_metres"]) for item in trace), default=0.0
        )
        maximum_protected = max(
            (float(item["protected_object_displacement_metres"]) for item in trace),
            default=0.0,
        )
        preservation_violation = bool(
            maximum_container > MAXIMUM_PROTECTED_DISPLACEMENT_METRES
            or maximum_protected > MAXIMUM_PROTECTED_DISPLACEMENT_METRES
        )
        stable_success = bool(
            first_success is not None
            and stable_steps == STABLE_SUCCESS_STEPS
            and terminal["predicate_vector"] == [True]
            and not preservation_violation
        )
        trace_path = output_dir / "raw-7d-actions.json"
        trace_path.write_text(json.dumps(trace, indent=2, sort_keys=True) + "\n")
        first_contact = next(
            (
                int(item["action_index"])
                for item in trace
                if item["target_gripper_contact_observed"] is True
            ),
            None,
        )
        result_without_digest = {
            "schema_version": "missionos.libero_book_caddy_oracle.v1",
            "status": (
                "scripted_oracle_recoverability_established"
                if stable_success
                else "scripted_oracle_recoverability_not_established"
            ),
            "task_suite": TASK_SUITE,
            "task_id": TASK_ID,
            "task_name": TASK_NAME,
            "instruction": instruction,
            "environment_seed": ENVIRONMENT_SEED,
            "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
            "snapshot_sha256": _sha256_path(snapshot_path),
            "snapshot_restore_check": {
                "bitwise_equal": bool(np.array_equal(restored, snapshot)),
                "maximum_absolute_error": restore_maximum_error,
                "changed_value_count": int(np.count_nonzero(difference)),
                "maximum_admitted_absolute_error": RESTORE_MAXIMUM_ABSOLUTE_ERROR,
            },
            "source_goal_predicate_vector": source_vector,
            "terminal_goal_predicate_vector": terminal["predicate_vector"],
            "maximum_action_budget": MAXIMUM_ACTIONS,
            "actions_applied": action_count,
            "success_first_observed_after_action": first_success,
            "first_contact_after_action": first_contact,
            "stable_success_steps_required": STABLE_SUCCESS_STEPS,
            "stable_success_steps_completed": stable_steps,
            "stable_success_observed": stable_success,
            "preservation_violation_observed": preservation_violation,
            "maximum_container_displacement_metres": maximum_container,
            "maximum_protected_object_displacement_metres": maximum_protected,
            "initial_target_position_metres": initial_target.tolist(),
            "desired_target_position_metres": desired_target.tolist(),
            "terminal_target_position_metres": terminal["target_position_metres"],
            "grasp_offset_metres": (
                None if grasp_offset is None else grasp_offset.tolist()
            ),
            "stage_summaries": stages,
            "raw_action_trace_sha256": _sha256_path(trace_path),
            "raw_action_count": len(trace),
            "terminal_frame_sha256": capture("terminal"),
            "claim_boundary": {
                "authority": "diagnostic_only",
                "privileged_object_state_used_for_oracle_planning": True,
                "same_original_7d_action_interface_used": True,
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
            "result_sha256": canonical_sha256(result_without_digest),
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

#!/usr/bin/env python3
"""Run Cosmos Policy on nominal and displaced book/caddy LIBERO states.

This is a diagnostic MuJoCo-clone comparison.  Model rollouts are proposals;
only actual LIBERO predicates can establish observed task success.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping

from scripts import run_cosmos_policy_libero_experiment as cosmos
from scripts.probe_libero_book_caddy_fixture import (
    CONTAINER_OBJECT,
    ENVIRONMENT_SEED,
    EPISODE_INIT_STATE_INDEX,
    PROTECTED_OBJECT,
    TARGET_OBJECT,
    TASK_ID,
    TASK_INSTRUCTION,
    TASK_NAME,
    TASK_SUITE,
    _make_environment,
    _predicate_material,
    canonical_sha256,
)
from scripts.run_libero_book_caddy_oracle import _read_snapshot


OPT_IN_ENV = "RUN_MISSIONOS_COSMOS_POLICY_LIBERO_BOOK_CADDY_PROBE"
PROCESS_SEED = 195
NOMINAL_MAXIMUM_ACTIONS = 520
REPAIR_MAXIMUM_ACTIONS = 128
STABLE_SUCCESS_STEPS = 20
MAXIMUM_PROTECTED_DISPLACEMENT_METRES = 0.005


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _verify_oracle(report_path: Path, snapshot_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    supplied = report.get("result_sha256")
    material = {key: value for key, value in report.items() if key != "result_sha256"}
    if supplied != canonical_sha256(material):
        raise RuntimeError("book_caddy_policy_oracle_digest_mismatch")
    if (
        report.get("snapshot_sha256") != _sha256_path(snapshot_path)
        or report.get("task_id") != TASK_ID
        or report.get("task_name") != TASK_NAME
        or report.get("stable_success_observed") is not True
        or report.get("stable_success_steps_completed") != STABLE_SUCCESS_STEPS
        or report.get("terminal_goal_predicate_vector") != [True]
        or report.get("preservation_violation_observed") is not False
        or report.get("claim_boundary", {}).get("model_inference_invoked") is not False
        or report.get("claim_boundary", {}).get("physical_execution_invoked") is not False
    ):
        raise RuntimeError("book_caddy_policy_oracle_contract_invalid")
    return {
        "report_sha256": supplied,
        "snapshot_sha256": report["snapshot_sha256"],
        "actions_applied": report["actions_applied"],
        "first_contact_after_action": report["first_contact_after_action"],
        "success_first_observed_after_action": report[
            "success_first_observed_after_action"
        ],
        "stable_success_steps_completed": report["stable_success_steps_completed"],
        "authority": "diagnostic_positive_control_only",
    }


def _save_actual(
    observation: Mapping[str, Any], directory: Path, root: Path, step: int
) -> dict[str, Any]:
    return cosmos._save_actual_observation(
        observation=observation,
        directory=directory,
        artifact_root=root,
        step_number=step,
    )


def _run_nominal(
    *, cfg: Any, model: Any, stats: Any, root: Path, maximum_actions: int
) -> dict[str, Any]:
    import numpy as np
    from cosmos_policy.experiments.robot.libero.libero_utils import get_libero_dummy_action

    phase = root / "nominal"
    actual_dir = phase / "actual_observations"
    future_dir = phase / "future_predictions"
    environment, init_states, instruction = _make_environment()
    actions: list[list[float]] = []
    predicate_trace = []
    actual_manifest = []
    future_manifest = []
    try:
        environment.reset()
        observation = environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        for _ in range(cosmos.OFFICIAL_STABILIZATION_STEPS):
            observation, _, _, _ = environment.step(get_libero_dummy_action("cosmos"))
        actual_manifest.append(_save_actual(observation, actual_dir, root, 0))
        applied = 0
        query_index = 0
        success = bool(environment.check_success())
        while applied < maximum_actions and not success:
            result, _ = cosmos._query_policy(
                cfg=cfg,
                model=model,
                dataset_stats=stats,
                observation=observation,
                instruction=instruction,
                seed=PROCESS_SEED,
            )
            future_manifest.append(
                cosmos._save_future_prediction(
                    predictions=result["future_image_predictions"],
                    value_prediction=result["value_prediction"],
                    directory=future_dir,
                    artifact_root=root,
                    query_index=query_index,
                    applied_action_start_index=applied,
                )
            )
            for raw_action in result["actions"][: maximum_actions - applied]:
                action = np.asarray(raw_action, dtype=np.float32)
                if action.shape != (7,) or not np.all(np.isfinite(action)):
                    raise RuntimeError("book_caddy_nominal_action_invalid")
                observation, _, done, info = environment.step(action.tolist())
                applied += 1
                vector = [item["satisfied"] for item in _predicate_material(environment)]
                success = bool(environment.check_success())
                if success != (vector == [True]):
                    raise RuntimeError("book_caddy_nominal_predicate_mismatch")
                actions.append([float(value) for value in action])
                predicate_trace.append(
                    {"applied_action_count": applied, "predicate_vector": vector}
                )
                actual_manifest.append(_save_actual(observation, actual_dir, root, applied))
                if success:
                    break
                if done or info.get("done", False):
                    raise RuntimeError("book_caddy_nominal_done_without_success")
            query_index += 1
        return {
            "status": "nominal_success_observed" if success else "nominal_budget_exhausted",
            "instruction": instruction,
            "applied_action_count": applied,
            "maximum_applied_actions": maximum_actions,
            "terminal_goal_predicate_vector": predicate_trace[-1]["predicate_vector"],
            "nominal_success_observed": success,
            "raw_actions": actions,
            "predicate_trace": predicate_trace,
            "future_prediction_manifest": future_manifest,
            "actual_observation_manifest": actual_manifest,
        }
    finally:
        environment.close()


def _run_repair(
    *,
    cfg: Any,
    model: Any,
    stats: Any,
    snapshot_path: Path,
    root: Path,
    maximum_actions: int,
) -> dict[str, Any]:
    import numpy as np

    phase = root / "repair"
    actual_dir = phase / "actual_observations"
    future_dir = phase / "future_predictions"
    snapshot, metadata = _read_snapshot(snapshot_path)
    environment, init_states, instruction = _make_environment()
    action_trace = []
    step_trace = []
    actual_manifest = []
    future_manifest = []
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        observation = environment.regenerate_obs_from_state(snapshot)
        restored = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        restore_difference = np.abs(restored - snapshot)
        if float(restore_difference.max()) > 1e-12:
            raise RuntimeError("book_caddy_policy_snapshot_restore_outside_tolerance")
        source_vector = [item["satisfied"] for item in _predicate_material(environment)]
        if source_vector != [False]:
            raise RuntimeError("book_caddy_policy_source_vector_invalid")
        simulator = environment.env
        target_body = int(simulator.obj_body_id[TARGET_OBJECT])
        container_body = int(simulator.obj_body_id[CONTAINER_OBJECT])
        protected_body = int(simulator.obj_body_id[PROTECTED_OBJECT])
        initial_target = np.asarray(simulator.sim.data.body_xpos[target_body]).copy()
        initial_container = np.asarray(simulator.sim.data.body_xpos[container_body]).copy()
        initial_protected = np.asarray(simulator.sim.data.body_xpos[protected_body]).copy()
        initial_eef = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
        initial_distance = float(np.linalg.norm(initial_eef - initial_target))
        actual_manifest.append(_save_actual(observation, actual_dir, root, 0))
        applied = 0
        query_index = 0
        first_contact = None
        first_success = None
        success = False
        while applied < maximum_actions and not success:
            result, _ = cosmos._query_policy(
                cfg=cfg,
                model=model,
                dataset_stats=stats,
                observation=observation,
                instruction=instruction,
                seed=PROCESS_SEED,
            )
            future_manifest.append(
                cosmos._save_future_prediction(
                    predictions=result["future_image_predictions"],
                    value_prediction=result["value_prediction"],
                    directory=future_dir,
                    artifact_root=root,
                    query_index=query_index,
                    applied_action_start_index=applied,
                )
            )
            for raw_action in result["actions"][: maximum_actions - applied]:
                action = np.asarray(raw_action, dtype=np.float32)
                if action.shape != (7,) or not np.all(np.isfinite(action)):
                    raise RuntimeError("book_caddy_repair_action_invalid")
                observation, _, done, info = environment.step(action.tolist())
                applied += 1
                target = np.asarray(simulator.sim.data.body_xpos[target_body], dtype=np.float64)
                container = np.asarray(
                    simulator.sim.data.body_xpos[container_body], dtype=np.float64
                )
                protected = np.asarray(
                    simulator.sim.data.body_xpos[protected_body], dtype=np.float64
                )
                eef = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
                contact = bool(
                    simulator.check_contact(
                        simulator.get_object(TARGET_OBJECT), simulator.robots[0].gripper
                    )
                )
                vector = [item["satisfied"] for item in _predicate_material(environment)]
                success = bool(environment.check_success())
                if success != (vector == [True]):
                    raise RuntimeError("book_caddy_repair_predicate_mismatch")
                if contact and first_contact is None:
                    first_contact = applied
                if success and first_success is None:
                    first_success = applied
                action_trace.append([float(value) for value in action])
                step_trace.append(
                    {
                        "applied_action_count": applied,
                        "predicate_vector": vector,
                        "target_gripper_contact_observed": contact,
                        "eef_target_distance_metres": float(np.linalg.norm(eef - target)),
                        "target_displacement_metres": float(
                            np.linalg.norm(target - initial_target)
                        ),
                        "container_displacement_metres": float(
                            np.linalg.norm(container - initial_container)
                        ),
                        "protected_object_displacement_metres": float(
                            np.linalg.norm(protected - initial_protected)
                        ),
                    }
                )
                actual_manifest.append(_save_actual(observation, actual_dir, root, applied))
                if success:
                    break
                if done or info.get("done", False):
                    raise RuntimeError("book_caddy_repair_done_without_success")
            query_index += 1

        stable_steps = 0
        if success:
            for hold_index in range(STABLE_SUCCESS_STEPS):
                hold_action = np.asarray([0, 0, 0, 0, 0, 0, 1], dtype=np.float32)
                observation, _, _, _ = environment.step(hold_action.tolist())
                vector = [item["satisfied"] for item in _predicate_material(environment)]
                actual_manifest.append(
                    _save_actual(
                        observation, actual_dir, root, applied + hold_index + 1
                    )
                )
                if vector != [True]:
                    break
                stable_steps += 1

        terminal_target = np.asarray(simulator.sim.data.body_xpos[target_body], dtype=np.float64)
        terminal_container = np.asarray(
            simulator.sim.data.body_xpos[container_body], dtype=np.float64
        )
        terminal_protected = np.asarray(
            simulator.sim.data.body_xpos[protected_body], dtype=np.float64
        )
        terminal_eef = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
        distances = [float(item["eef_target_distance_metres"]) for item in step_trace]
        minimum_distance = min(distances, default=initial_distance)
        minimum_index = (
            distances.index(minimum_distance) + 1 if distances else None
        )
        maximum_container = max(
            (float(item["container_displacement_metres"]) for item in step_trace),
            default=0.0,
        )
        maximum_protected = max(
            (float(item["protected_object_displacement_metres"]) for item in step_trace),
            default=0.0,
        )
        preservation_violation = bool(
            maximum_container > MAXIMUM_PROTECTED_DISPLACEMENT_METRES
            or maximum_protected > MAXIMUM_PROTECTED_DISPLACEMENT_METRES
        )
        stable_success = bool(
            success
            and stable_steps == STABLE_SUCCESS_STEPS
            and not preservation_violation
        )
        gripper = [values[6] for values in action_trace]
        return {
            "status": (
                "stable_predicate_recovery_observed"
                if stable_success
                else "transient_predicate_recovery_observed"
                if success
                else "predicate_recovery_not_observed"
            ),
            "instruction": instruction,
            "snapshot_sha256": _sha256_path(snapshot_path),
            "fixture_sha256": metadata["book_caddy_fixture_sha256"],
            "source_goal_predicate_vector": source_vector,
            "terminal_goal_predicate_vector": [
                item["satisfied"] for item in _predicate_material(environment)
            ],
            "maximum_applied_actions": maximum_actions,
            "applied_action_count": applied,
            "first_contact_after_action": first_contact,
            "success_first_observed_after_action": first_success,
            "stable_success_steps_required": STABLE_SUCCESS_STEPS,
            "stable_success_steps_completed": stable_steps,
            "stable_success_observed": stable_success,
            "preservation_violation_observed": preservation_violation,
            "initial_eef_target_distance_metres": initial_distance,
            "minimum_eef_target_distance_metres": minimum_distance,
            "minimum_eef_target_distance_after_action": minimum_index,
            "terminal_eef_target_distance_metres": float(
                np.linalg.norm(terminal_eef - terminal_target)
            ),
            "maximum_target_displacement_metres": max(
                (float(item["target_displacement_metres"]) for item in step_trace),
                default=0.0,
            ),
            "terminal_target_displacement_metres": float(
                np.linalg.norm(terminal_target - initial_target)
            ),
            "maximum_container_displacement_metres": maximum_container,
            "maximum_protected_object_displacement_metres": maximum_protected,
            "terminal_container_displacement_metres": float(
                np.linalg.norm(terminal_container - initial_container)
            ),
            "terminal_protected_object_displacement_metres": float(
                np.linalg.norm(terminal_protected - initial_protected)
            ),
            "gripper_command_minimum": min(gripper, default=None),
            "gripper_command_maximum": max(gripper, default=None),
            "gripper_command_change_count": sum(
                left != right for left, right in zip(gripper, gripper[1:])
            ),
            "raw_action_trace": action_trace,
            "step_trace": step_trace,
            "future_prediction_manifest": future_manifest,
            "actual_observation_manifest": actual_manifest,
        }
    finally:
        environment.close()


def execute_live(
    *,
    source_root: Path,
    checkpoint_path: Path,
    tokenizer_path: Path,
    snapshot_path: Path,
    oracle_report_path: Path,
    output_dir: Path,
    nominal_maximum_actions: int = NOMINAL_MAXIMUM_ACTIONS,
    repair_maximum_actions: int = REPAIR_MAXIMUM_ACTIONS,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("book_caddy_cosmos_probe_opt_in_required")
    if output_dir.exists():
        raise ValueError("book_caddy_cosmos_probe_output_exists")
    if repair_maximum_actions != REPAIR_MAXIMUM_ACTIONS:
        raise ValueError("book_caddy_cosmos_probe_requires_128_repair_actions")
    oracle_admission = _verify_oracle(oracle_report_path, snapshot_path)
    output_dir.mkdir(parents=True)
    cfg, model, stats, checkpoint = cosmos._build_model_runtime(
        source_root=source_root,
        checkpoint_path=checkpoint_path,
        tokenizer_path=tokenizer_path,
        process_seed=PROCESS_SEED,
    )
    nominal = _run_nominal(
        cfg=cfg,
        model=model,
        stats=stats,
        root=output_dir,
        maximum_actions=nominal_maximum_actions,
    )
    repair = None
    if nominal["nominal_success_observed"]:
        repair = _run_repair(
            cfg=cfg,
            model=model,
            stats=stats,
            snapshot_path=snapshot_path,
            root=output_dir,
            maximum_actions=repair_maximum_actions,
        )
    report_without_digest = {
        "schema_version": "missionos.cosmos_policy_libero_book_caddy_probe.v1",
        "status": "completed" if repair is not None else "stopped_at_nominal_gate",
        "task_suite": TASK_SUITE,
        "task_id": TASK_ID,
        "task_name": TASK_NAME,
        "instruction": TASK_INSTRUCTION,
        "environment_seed": ENVIRONMENT_SEED,
        "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
        "process_seed": PROCESS_SEED,
        "additional_training_performed": False,
        "checkpoint": deepcopy(checkpoint),
        "source_revision": subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "oracle_admission": oracle_admission,
        "nominal": nominal,
        "repair": repair,
        "success_authority": "actual_libero_goal_predicates_only",
        "future_predictions_may_establish_success": False,
        "claim_boundary": {
            "authority": "diagnostic_only",
            "diagnostic_mujoco_clone": True,
            "missionos_runtime_loop_executed": False,
            "human_approval_created": False,
            "governed_dispatch_created": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
            "general_recovery_rate_established": False,
        },
    }
    report = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    _write_json(output_dir / "result.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--checkpoint-path", type=Path, required=True)
    parser.add_argument("--tokenizer-path", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--oracle-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--nominal-maximum-actions", type=int, default=NOMINAL_MAXIMUM_ACTIONS)
    parser.add_argument("--repair-maximum-actions", type=int, default=REPAIR_MAXIMUM_ACTIONS)
    args = parser.parse_args()
    report = execute_live(
        source_root=args.source_root.resolve(),
        checkpoint_path=args.checkpoint_path.resolve(),
        tokenizer_path=args.tokenizer_path.resolve(),
        snapshot_path=args.snapshot.resolve(),
        oracle_report_path=args.oracle_report.resolve(),
        output_dir=args.output_dir.resolve(),
        nominal_maximum_actions=args.nominal_maximum_actions,
        repair_maximum_actions=args.repair_maximum_actions,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Apply bounded Cosmos actions only to promising paired-pose diagnostics."""

from __future__ import annotations

import argparse
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
    ACTION_DIMENSION,
    COSMOS_POLICY_LIBERO_ACTION_STEPS,
    EPISODE_INIT_STATE_INDEX,
    _action_command_statistics,
    _build_model_runtime,
    _make_environment,
    _query_policy,
    _save_actual_observation,
    _save_future_prediction,
    _write_json,
)
from scripts.run_cosmos_policy_libero_paired_pose_sensitivity import (  # noqa: E402
    DEFAULT_POSE_ACTION_COUNTS,
    DEFAULT_SEEDS,
    PROTECTED_OBJECT,
    TARGET_OBJECT,
    _construct_displacement_states,
    _normalize_successful_source_robot,
    _replay_nominal,
    _robot_indexes,
    _successful_source_material,
    _vector,
    _verify_nominal_report,
)


OPT_IN_ENV = "RUN_MISSIONOS_COSMOS_POLICY_LIBERO_POSE_ROLLOUT_DIAGNOSTIC"
DEFAULT_SELECTED_POSES = (0, 184, 369)
DEFAULT_SELECTED_DISPLACEMENT_METRES = 0.225
DEFAULT_MAXIMUM_ACTIONS = 128


def _verify_sensitivity_report(path: Path, nominal_sha256: str) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    supplied = report.get("result_sha256")
    material = {key: value for key, value in report.items() if key != "result_sha256"}
    if supplied != canonical_sha256(material):
        raise RuntimeError("cosmos_pose_rollout_sensitivity_digest_mismatch")
    if (
        report.get("schema_version")
        != "missionos.cosmos_policy_libero_paired_pose_sensitivity.v1"
        or report.get("nominal_report_sha256") != nominal_sha256
        or report.get("forward_query_count") != 80
        or report.get("claim_boundary", {}).get("queried_actions_applied_to_simulator")
        is not False
    ):
        raise RuntimeError("cosmos_pose_rollout_sensitivity_contract_invalid")
    return report


def _position(simulator: Any, object_name: str) -> list[float]:
    import numpy as np

    body_id = int(simulator.obj_body_id[object_name])
    return np.asarray(simulator.sim.data.body_xpos[body_id], dtype=np.float64).tolist()


def _run_trial(
    *,
    cfg: Any,
    model: Any,
    dataset_stats: Mapping[str, Any],
    nominal: Mapping[str, Any],
    pose: Mapping[str, Any],
    fixture: Mapping[str, Any],
    seed: int,
    output_dir: Path,
    artifact_root: Path,
    maximum_actions: int,
    instruction: str | None = None,
) -> dict[str, Any]:
    import numpy as np

    environment, init_states, _ = _make_environment()
    action_trace = []
    step_trace = []
    future_manifest = []
    actual_manifest = []
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
        observation = environment.regenerate_obs_from_state(
            np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        )
        initial_vector = _vector(environment)
        if initial_vector != [True, False, True]:
            raise RuntimeError(f"cosmos_pose_rollout_source_vector_invalid:{initial_vector}")
        simulator = environment.env
        initial_target = _position(simulator, TARGET_OBJECT)
        initial_protected = _position(simulator, PROTECTED_OBJECT)
        minimum_eef_distance = math.dist(
            initial_target, np.asarray(observation["robot0_eef_pos"], dtype=np.float64).tolist()
        )
        contact_count = 0
        maximum_target_translation = 0.0
        maximum_protected_translation = 0.0
        actual_manifest.append(
            _save_actual_observation(
                observation=observation,
                directory=output_dir / "actual",
                artifact_root=artifact_root,
                step_number=0,
            )
        )
        applied = 0
        query_index = 0
        success = False
        ended_without_success = False
        while applied < maximum_actions and not success and not ended_without_success:
            trial_instruction = instruction or str(nominal["instruction"])
            result, _ = _query_policy(
                cfg=cfg,
                model=model,
                dataset_stats=dataset_stats,
                observation=observation,
                instruction=trial_instruction,
                seed=seed,
            )
            future_manifest.append(
                _save_future_prediction(
                    predictions=result["future_image_predictions"],
                    value_prediction=result["value_prediction"],
                    directory=output_dir / "future" / f"query-{query_index:03d}",
                    artifact_root=artifact_root,
                    query_index=query_index,
                    applied_action_start_index=applied,
                )
            )
            for raw_action in result["actions"][: maximum_actions - applied]:
                action = np.asarray(raw_action, dtype=np.float32)
                if action.shape != (ACTION_DIMENSION,) or not np.all(np.isfinite(action)):
                    raise RuntimeError("cosmos_pose_rollout_action_invalid")
                observation, _, done, info = environment.step(action.tolist())
                applied += 1
                vector = _vector(environment)
                official = bool(environment.check_success())
                if official is not all(vector):
                    raise RuntimeError("cosmos_pose_rollout_predicate_mismatch")
                target = _position(simulator, TARGET_OBJECT)
                protected = _position(simulator, PROTECTED_OBJECT)
                eef = np.asarray(observation["robot0_eef_pos"], dtype=np.float64).tolist()
                contact = bool(
                    simulator.check_contact(
                        simulator.get_object(TARGET_OBJECT), simulator.robots[0].gripper
                    )
                )
                contact_count += int(contact)
                minimum_eef_distance = min(minimum_eef_distance, math.dist(target, eef))
                maximum_target_translation = max(
                    maximum_target_translation, math.dist(initial_target, target)
                )
                maximum_protected_translation = max(
                    maximum_protected_translation, math.dist(initial_protected, protected)
                )
                action_trace.append(
                    {
                        "global_action_index": applied - 1,
                        "action_7d": action.tolist(),
                        "action_sha256": canonical_sha256({"action": action.tolist()}),
                    }
                )
                step_trace.append(
                    {
                        "global_action_count": applied,
                        "goal_predicate_vector": vector,
                        "official_predicate_result": official,
                        "target_position_metres": target,
                        "protected_position_metres": protected,
                        "eef_position_metres": eef,
                        "target_gripper_contact_observed": contact,
                        "environment_done": bool(done or info.get("done", False)),
                    }
                )
                success = official
                ended_without_success = bool(done or info.get("done", False)) and not official
                if applied % COSMOS_POLICY_LIBERO_ACTION_STEPS == 0 or success:
                    actual_manifest.append(
                        _save_actual_observation(
                            observation=observation,
                            directory=output_dir / "actual",
                            artifact_root=artifact_root,
                            step_number=applied,
                        )
                    )
                if success or ended_without_success:
                    break
            query_index += 1
        final_vector = _vector(environment)
        return {
            "instruction": instruction or str(nominal["instruction"]),
            "pose_action_count": int(pose["action_count"]),
            "phase_fraction": pose["phase_fraction"],
            "seed": seed,
            "maximum_applied_actions": maximum_actions,
            "applied_action_count": applied,
            "initial_goal_predicate_vector": initial_vector,
            "final_goal_predicate_vector": final_vector,
            "actual_predicate_success_observed": success,
            "environment_ended_without_success": ended_without_success,
            "contact_observation_count": contact_count,
            "minimum_end_effector_distance_to_target_metres": minimum_eef_distance,
            "maximum_target_translation_metres": maximum_target_translation,
            "maximum_protected_translation_metres": maximum_protected_translation,
            "action_command_statistics": _action_command_statistics(
                action_trace, chunk_size=COSMOS_POLICY_LIBERO_ACTION_STEPS
            ),
            "action_trace": action_trace,
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
    nominal_report_path: Path,
    sensitivity_report_path: Path,
    output_dir: Path,
    selected_poses: Sequence[int] = DEFAULT_SELECTED_POSES,
    seeds: Sequence[int] = DEFAULT_SEEDS,
    displacement_metres: float = DEFAULT_SELECTED_DISPLACEMENT_METRES,
    maximum_actions: int = DEFAULT_MAXIMUM_ACTIONS,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("cosmos_pose_rollout_opt_in_required")
    if output_dir.exists():
        raise ValueError("cosmos_pose_rollout_output_exists")
    if not 1 <= maximum_actions <= 128:
        raise ValueError("cosmos_pose_rollout_action_budget_invalid")
    nominal = _verify_nominal_report(nominal_report_path)
    sensitivity = _verify_sensitivity_report(
        sensitivity_report_path, nominal["result_sha256"]
    )
    poses, first_success_state = _replay_nominal(
        nominal_report=nominal, pose_action_counts=DEFAULT_POSE_ACTION_COUNTS
    )
    pose_by_count = {int(item["action_count"]): item for item in poses}
    if any(int(value) not in pose_by_count for value in selected_poses):
        raise ValueError("cosmos_pose_rollout_selected_pose_invalid")
    successful_state, normalization = _normalize_successful_source_robot(
        successful_state=first_success_state, initial_pose=poses[0]
    )
    source = _successful_source_material(successful_state)
    fixtures = _construct_displacement_states(
        successful_state=successful_state,
        source=source,
        distances=(0.0, float(displacement_metres)),
    )
    fixture = fixtures[1]
    sensitivity_fixture = next(
        item
        for item in sensitivity["fixture_grid"]
        if float(item["requested_displacement_metres"]) == float(displacement_metres)
    )
    if not math.isclose(
        float(fixture["observed_displacement_metres"]),
        float(sensitivity_fixture["observed_displacement_metres"]),
        abs_tol=1e-9,
    ):
        raise RuntimeError("cosmos_pose_rollout_fixture_reproduction_drift")
    output_dir.mkdir(parents=True)
    cfg, model, dataset_stats, checkpoint = _build_model_runtime(
        source_root=source_root,
        checkpoint_path=checkpoint_path,
        tokenizer_path=tokenizer_path,
        process_seed=int(seeds[0]),
    )
    trials = []
    for pose_count in selected_poses:
        for seed in seeds:
            trial_dir = output_dir / f"pose-{int(pose_count):04d}" / f"seed-{int(seed)}"
            trial_dir.mkdir(parents=True)
            trials.append(
                _run_trial(
                    cfg=cfg,
                    model=model,
                    dataset_stats=dataset_stats,
                    nominal=nominal,
                    pose=pose_by_count[int(pose_count)],
                    fixture=fixture,
                    seed=int(seed),
                    output_dir=trial_dir,
                    artifact_root=output_dir,
                    maximum_actions=maximum_actions,
                )
            )
    report_without_digest = {
        "schema_version": "missionos.cosmos_policy_libero_pose_rollout_diagnostic.v1",
        "status": "bounded_pose_rollout_diagnostics_completed",
        "nominal_report_sha256": nominal["result_sha256"],
        "sensitivity_report_sha256": sensitivity["result_sha256"],
        "successful_source_robot_normalization": normalization,
        "requested_displacement_metres": displacement_metres,
        "observed_displacement_metres": fixture["observed_displacement_metres"],
        "selected_pose_action_counts": [int(value) for value in selected_poses],
        "seeds": [int(value) for value in seeds],
        "maximum_actions_per_trial": maximum_actions,
        "trial_count": len(trials),
        "checkpoint": checkpoint,
        "additional_training_performed": False,
        "trials": trials,
        "claim_boundary": {
            "authority": "diagnostic_only",
            "simulator_state_directly_changed_for_diagnostic": True,
            "governed_repair_dispatch_performed": False,
            "actual_predicate_success_is_diagnostic_only": True,
            "repair_success_established": False,
            "future_predictions_may_establish_success": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        },
    }
    report = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    _write_json(output_dir / "pose-rollout-diagnostic.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--checkpoint-path", type=Path, required=True)
    parser.add_argument("--tokenizer-path", type=Path, required=True)
    parser.add_argument("--nominal-report", type=Path, required=True)
    parser.add_argument("--sensitivity-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--selected-poses", default=",".join(str(value) for value in DEFAULT_SELECTED_POSES)
    )
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument(
        "--displacement-metres", type=float, default=DEFAULT_SELECTED_DISPLACEMENT_METRES
    )
    parser.add_argument("--maximum-actions", type=int, default=DEFAULT_MAXIMUM_ACTIONS)
    args = parser.parse_args()
    report = execute_live(
        source_root=args.source_root.resolve(),
        checkpoint_path=args.checkpoint_path.resolve(),
        tokenizer_path=args.tokenizer_path.resolve(),
        nominal_report_path=args.nominal_report.resolve(),
        sensitivity_report_path=args.sensitivity_report.resolve(),
        output_dir=args.output_dir.resolve(),
        selected_poses=tuple(int(value) for value in args.selected_poses.split(",")),
        seeds=tuple(int(value) for value in args.seeds.split(",")),
        displacement_metres=args.displacement_metres,
        maximum_actions=args.maximum_actions,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "trial_count": report["trial_count"],
                "trials": [
                    {
                        key: trial[key]
                        for key in (
                            "pose_action_count",
                            "seed",
                            "applied_action_count",
                            "final_goal_predicate_vector",
                            "actual_predicate_success_observed",
                            "contact_observation_count",
                            "minimum_end_effector_distance_to_target_metres",
                            "maximum_target_translation_metres",
                        )
                    }
                    for trial in report["trials"]
                ],
                "result_sha256": report["result_sha256"],
                "claim_boundary": report["claim_boundary"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

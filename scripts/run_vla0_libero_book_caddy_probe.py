#!/usr/bin/env python3
"""Run the pinned VLA-0 checkpoint on the book/caddy diagnostic fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Mapping

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
    _predicate_material,
    canonical_sha256,
)
from scripts.run_libero_book_caddy_oracle import _read_snapshot
from scripts.run_vla0_libero_snapshot_recovery import (
    ENSEMBLE_PREDICTIONS,
    VLA0_LEROBOT_REVISION,
    VLA0_SOURCE_REVISION,
    _capture_frames,
    _official_ensemble_action,
    _verify_checkpoint,
    _verify_loaded_dataset_stats,
)


OPT_IN_ENV = "RUN_MISSIONOS_VLA0_LIBERO_BOOK_CADDY_PROBE"
PROCESS_SEED = 1000
NOMINAL_MAXIMUM_ACTIONS = 520
REPAIR_MAXIMUM_ACTIONS = 128
STABLE_SUCCESS_STEPS = 20
MAXIMUM_PROTECTED_DISPLACEMENT_METRES = 0.005
CHECKPOINT_METADATA_FILES = {
    "config.yaml": (1_103, "6624f21b24a760dda442217454accf0873526d446b88ca257c5f130fc678c7ea"),
    "model_last/added_tokens.json": (
        605,
        "58b54bbe36fc752f79a24a271ef66a0a0830054b4dfad94bde757d851968060b",
    ),
    "model_last/chat_template.json": (
        1_049,
        "94174d7176c52a7192f96fc34eb2cf23c7c2059d63cdbfadca1586ba89731fb7",
    ),
    "model_last/config.json": (
        1_489,
        "01013e158ab7f6b1edb02783dabcec35739085e653a4601886b83f6c8f0e03dd",
    ),
    "model_last/generation_config.json": (
        214,
        "11c32bf551cf5cceeca5d418bf0d946cbe18820a45c5a6691b248431e899257f",
    ),
    "model_last/merges.txt": (
        1_671_853,
        "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
    ),
    "model_last/model.safetensors.index.json": (
        65_448,
        "eafcf7eb80f0e63f73ef902b792726372d9a5ac90f7c724898006882705360da",
    ),
    "model_last/preprocessor_config.json": (
        575,
        "175c605b8e102654dc95c999a092af0336c3d6bed718906ec18c7d6b7be73e5f",
    ),
    "model_last/special_tokens_map.json": (
        613,
        "76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd",
    ),
    "model_last/tokenizer_config.json": (
        5_929,
        "b190c9526d40b88dbe3dac29cdb6874010c5f7dad9d93ba156b12ae3122e32c1",
    ),
    "model_last/vocab.json": (
        2_776_833,
        "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
    ),
}


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_revision(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _verify_oracle(report_path: Path, snapshot_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    digest = report.get("result_sha256")
    material = {key: value for key, value in report.items() if key != "result_sha256"}
    if digest != canonical_sha256(material):
        raise RuntimeError("book_caddy_vla0_oracle_digest_mismatch")
    if (
        report.get("snapshot_sha256") != _sha256_path(snapshot_path)
        or report.get("task_id") != TASK_ID
        or report.get("stable_success_observed") is not True
        or report.get("stable_success_steps_completed") != STABLE_SUCCESS_STEPS
        or report.get("terminal_goal_predicate_vector") != [True]
        or report.get("preservation_violation_observed") is not False
    ):
        raise RuntimeError("book_caddy_vla0_oracle_contract_invalid")
    return {
        "report_sha256": digest,
        "snapshot_sha256": report["snapshot_sha256"],
        "actions_applied": report["actions_applied"],
        "first_contact_after_action": report["first_contact_after_action"],
        "success_first_observed_after_action": report["success_first_observed_after_action"],
        "stable_success_steps_completed": report["stable_success_steps_completed"],
        "authority": "diagnostic_positive_control_only",
    }


def _load_runtime(
    *, source_root: Path, checkpoint_path: Path
) -> tuple[Any, Any, Any, Any, dict[str, Any]]:
    if _git_revision(source_root) != VLA0_SOURCE_REVISION:
        raise RuntimeError("book_caddy_vla0_source_revision_mismatch")
    lerobot_root = source_root / "libs" / "RoboVerse" / "libs" / "lerobot"
    if _git_revision(lerobot_root) != VLA0_LEROBOT_REVISION:
        raise RuntimeError("book_caddy_vla0_lerobot_revision_mismatch")
    checkpoint = _verify_checkpoint(checkpoint_path)
    metadata_files = {}
    for relative_path, (expected_size, expected_sha256) in CHECKPOINT_METADATA_FILES.items():
        path = checkpoint_path / relative_path
        if (
            not path.is_file()
            or path.stat().st_size != expected_size
            or _sha256_path(path) != expected_sha256
        ):
            raise RuntimeError(f"book_caddy_vla0_checkpoint_metadata_mismatch:{relative_path}")
        metadata_files[relative_path] = {
            "size_bytes": expected_size,
            "sha256": expected_sha256,
        }
    checkpoint["metadata_files"] = metadata_files
    for runtime_path in reversed(
        (lerobot_root / "src", source_root, source_root / "libs" / "RoboVerse")
    ):
        if str(runtime_path) not in sys.path:
            sys.path.insert(0, str(runtime_path))
    os.chdir(source_root)

    import numpy as np
    import torch
    from roboverse.datasets.lerobot import dataloader as lerobot_dataloader
    from roboverse.evals.libero.eval import init_libero_env, libero_to_rv_obs
    from roboverse.main import get_cfg as get_roboverse_cfg
    from rv_train.train import get_cfg as get_vla0_cfg
    from rv_train.train import get_pretrained_model

    lerobot_dataloader.get_lerobot_metadata = lambda repo_id: SimpleNamespace(
        camera_keys=("image", "wrist_image")
    )
    random.seed(PROCESS_SEED)
    np.random.seed(PROCESS_SEED)
    torch.manual_seed(PROCESS_SEED)
    torch.cuda.manual_seed_all(PROCESS_SEED)
    model_cfg = get_vla0_cfg(str(checkpoint_path / "config.yaml"), cfg_opts="")
    dataset_cfg = get_roboverse_cfg(
        model_cfg.DATALOADER.ROBOVERSE.cfg_path,
        model_cfg.DATALOADER.ROBOVERSE.cfg_opts,
    )
    if bool(dataset_cfg.IMAGE.return_proprio):
        raise RuntimeError("book_caddy_vla0_return_proprio_mismatch")
    model, loaded_cfg = get_pretrained_model(
        str(checkpoint_path / "model_last.pth"), 0, torch_compile=False
    )
    model.eval()
    if (
        int(loaded_cfg.MODEL.QWEN.horizon) != 8
        or int(loaded_cfg.MODEL.QWEN.original_action_dim) != 7
    ):
        raise RuntimeError("book_caddy_vla0_loaded_model_contract_mismatch")
    stats = _verify_loaded_dataset_stats(model=model, verified_checkpoint_path=checkpoint_path)
    return (
        model,
        dataset_cfg,
        init_libero_env,
        libero_to_rv_obs,
        {
            **checkpoint,
            "loaded_dataset_stats": stats,
            "additional_training_performed": False,
        },
    )


def _make_environment(
    init_libero_env: Any, dataset_cfg: Any, maximum_steps: int
) -> tuple[Any, Any, str]:
    environment, init_states, _, instruction = init_libero_env(
        verse_config=dataset_cfg,
        task_name=TASK_NAME,
        seed=ENVIRONMENT_SEED,
        act_space="original",
        task_suite_name=TASK_SUITE,
        num_steps=maximum_steps,
    )
    return environment, init_states, instruction


def _predict_action(
    *,
    model: Any,
    observation: Mapping[str, Any],
    instruction: str,
    dataset_cfg: Any,
    libero_to_rv_obs: Any,
    previous_predictions: list[Any],
) -> Any:
    import numpy as np
    import torch

    model_observation = libero_to_rv_obs(observation, instruction, dataset_cfg)
    with torch.no_grad():
        output = model(
            **model_observation,
            get_loss=False,
            get_action=True,
            generate_temperature=0.0,
        )
    prediction = output["out_ori_act"][0].detach().cpu().numpy()
    if prediction.shape != (8, 7):
        raise RuntimeError("book_caddy_vla0_prediction_shape_mismatch")
    action = np.asarray(
        _official_ensemble_action(
            prediction=prediction,
            previous_predictions=previous_predictions,
        ),
        dtype=np.float32,
    )
    if action.shape != (7,) or not np.all(np.isfinite(action)):
        raise RuntimeError("book_caddy_vla0_action_invalid")
    action[-1] = 1.0 if action[-1] > 0.0 else -1.0
    return action


def _run_nominal(
    *,
    model: Any,
    dataset_cfg: Any,
    init_libero_env: Any,
    libero_to_rv_obs: Any,
    output_dir: Path,
    maximum_actions: int,
) -> dict[str, Any]:
    environment, init_states, instruction = _make_environment(
        init_libero_env, dataset_cfg, maximum_actions
    )
    frame_dir = output_dir / "nominal" / "actual_observations"
    predictions: list[Any] = []
    action_trace = []
    predicate_trace = []
    frame_manifest = []
    try:
        environment.reset()
        observation = environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        frame_manifest.append(
            _capture_frames(
                observation=observation,
                frame_capture_dir=frame_dir,
                artifact_root=output_dir,
                step_number=0,
            )
        )
        success = bool(environment.check_success())
        applied = 0
        while applied < maximum_actions and not success:
            action = _predict_action(
                model=model,
                observation=observation,
                instruction=instruction,
                dataset_cfg=dataset_cfg,
                libero_to_rv_obs=libero_to_rv_obs,
                previous_predictions=predictions,
            )
            observation, _, done, info = environment.step(action.tolist())
            applied += 1
            vector = [item["satisfied"] for item in _predicate_material(environment)]
            success = bool(environment.check_success())
            if success != (vector == [True]):
                raise RuntimeError("book_caddy_vla0_nominal_predicate_mismatch")
            action_trace.append(action.tolist())
            predicate_trace.append({"applied_action_count": applied, "predicate_vector": vector})
            frame_manifest.append(
                _capture_frames(
                    observation=observation,
                    frame_capture_dir=frame_dir,
                    artifact_root=output_dir,
                    step_number=applied,
                )
            )
            if done or info.get("done", False):
                break
        return {
            "status": "nominal_success_observed" if success else "nominal_budget_exhausted",
            "instruction": instruction,
            "applied_action_count": applied,
            "maximum_applied_actions": maximum_actions,
            "terminal_goal_predicate_vector": predicate_trace[-1]["predicate_vector"],
            "nominal_success_observed": success,
            "raw_action_trace": action_trace,
            "predicate_trace": predicate_trace,
            "actual_observation_manifest": frame_manifest,
        }
    finally:
        environment.close()


def _run_repair(
    *,
    model: Any,
    dataset_cfg: Any,
    init_libero_env: Any,
    libero_to_rv_obs: Any,
    snapshot_path: Path,
    output_dir: Path,
    maximum_actions: int,
) -> dict[str, Any]:
    import numpy as np

    snapshot, metadata = _read_snapshot(snapshot_path)
    environment, init_states, instruction = _make_environment(
        init_libero_env, dataset_cfg, maximum_actions
    )
    frame_dir = output_dir / "repair" / "actual_observations"
    predictions: list[Any] = []
    action_trace = []
    step_trace = []
    frame_manifest = []
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        observation = environment.regenerate_obs_from_state(snapshot)
        restored = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        if float(np.max(np.abs(restored - snapshot))) > 1e-12:
            raise RuntimeError("book_caddy_vla0_snapshot_restore_outside_tolerance")
        source_vector = [item["satisfied"] for item in _predicate_material(environment)]
        if source_vector != [False]:
            raise RuntimeError("book_caddy_vla0_source_vector_invalid")
        simulator = environment.env
        target_body = int(simulator.obj_body_id[TARGET_OBJECT])
        container_body = int(simulator.obj_body_id[CONTAINER_OBJECT])
        protected_body = int(simulator.obj_body_id[PROTECTED_OBJECT])
        initial_target = np.asarray(simulator.sim.data.body_xpos[target_body]).copy()
        initial_container = np.asarray(simulator.sim.data.body_xpos[container_body]).copy()
        initial_protected = np.asarray(simulator.sim.data.body_xpos[protected_body]).copy()
        initial_eef = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
        initial_distance = float(np.linalg.norm(initial_eef - initial_target))
        frame_manifest.append(
            _capture_frames(
                observation=observation,
                frame_capture_dir=frame_dir,
                artifact_root=output_dir,
                step_number=0,
            )
        )
        applied = 0
        first_contact = None
        first_success = None
        success = False
        while applied < maximum_actions and not success:
            action = _predict_action(
                model=model,
                observation=observation,
                instruction=instruction,
                dataset_cfg=dataset_cfg,
                libero_to_rv_obs=libero_to_rv_obs,
                previous_predictions=predictions,
            )
            observation, _, done, info = environment.step(action.tolist())
            applied += 1
            target = np.asarray(simulator.sim.data.body_xpos[target_body], dtype=np.float64)
            container = np.asarray(simulator.sim.data.body_xpos[container_body], dtype=np.float64)
            protected = np.asarray(simulator.sim.data.body_xpos[protected_body], dtype=np.float64)
            eef = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
            contact = bool(
                simulator.check_contact(
                    simulator.get_object(TARGET_OBJECT), simulator.robots[0].gripper
                )
            )
            vector = [item["satisfied"] for item in _predicate_material(environment)]
            success = bool(environment.check_success())
            if success != (vector == [True]):
                raise RuntimeError("book_caddy_vla0_repair_predicate_mismatch")
            if contact and first_contact is None:
                first_contact = applied
            if success and first_success is None:
                first_success = applied
            action_trace.append(action.tolist())
            step_trace.append(
                {
                    "applied_action_count": applied,
                    "predicate_vector": vector,
                    "target_gripper_contact_observed": contact,
                    "eef_target_distance_metres": float(np.linalg.norm(eef - target)),
                    "target_displacement_metres": float(np.linalg.norm(target - initial_target)),
                    "container_displacement_metres": float(
                        np.linalg.norm(container - initial_container)
                    ),
                    "protected_object_displacement_metres": float(
                        np.linalg.norm(protected - initial_protected)
                    ),
                }
            )
            frame_manifest.append(
                _capture_frames(
                    observation=observation,
                    frame_capture_dir=frame_dir,
                    artifact_root=output_dir,
                    step_number=applied,
                )
            )
            if done or info.get("done", False):
                break

        stable_steps = 0
        if success:
            for hold_index in range(STABLE_SUCCESS_STEPS):
                observation, _, _, _ = environment.step([0, 0, 0, 0, 0, 0, 1])
                vector = [item["satisfied"] for item in _predicate_material(environment)]
                frame_manifest.append(
                    _capture_frames(
                        observation=observation,
                        frame_capture_dir=frame_dir,
                        artifact_root=output_dir,
                        step_number=applied + hold_index + 1,
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
            success and stable_steps == STABLE_SUCCESS_STEPS and not preservation_violation
        )
        gripper = [float(action[6]) for action in action_trace]
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
            "minimum_eef_target_distance_after_action": (
                distances.index(minimum_distance) + 1 if distances else None
            ),
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
            "actual_observation_manifest": frame_manifest,
        }
    finally:
        environment.close()


def execute_live(
    *,
    source_root: Path,
    checkpoint_path: Path,
    snapshot_path: Path,
    oracle_report_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("book_caddy_vla0_probe_opt_in_required")
    if output_dir.exists():
        raise ValueError("book_caddy_vla0_probe_output_exists")
    oracle = _verify_oracle(oracle_report_path, snapshot_path)
    output_dir.mkdir(parents=True)
    model, dataset_cfg, init_libero_env, libero_to_rv_obs, checkpoint = _load_runtime(
        source_root=source_root, checkpoint_path=checkpoint_path
    )
    nominal = _run_nominal(
        model=model,
        dataset_cfg=dataset_cfg,
        init_libero_env=init_libero_env,
        libero_to_rv_obs=libero_to_rv_obs,
        output_dir=output_dir,
        maximum_actions=NOMINAL_MAXIMUM_ACTIONS,
    )
    repair = None
    if nominal["nominal_success_observed"]:
        repair = _run_repair(
            model=model,
            dataset_cfg=dataset_cfg,
            init_libero_env=init_libero_env,
            libero_to_rv_obs=libero_to_rv_obs,
            snapshot_path=snapshot_path,
            output_dir=output_dir,
            maximum_actions=REPAIR_MAXIMUM_ACTIONS,
        )
    report_without_digest = {
        "schema_version": "missionos.vla0_libero_book_caddy_probe.v1",
        "status": "completed" if repair is not None else "stopped_at_nominal_gate",
        "task_suite": TASK_SUITE,
        "task_id": TASK_ID,
        "task_name": TASK_NAME,
        "instruction": TASK_INSTRUCTION,
        "environment_seed": ENVIRONMENT_SEED,
        "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
        "process_seed": PROCESS_SEED,
        "ensemble_predictions": ENSEMBLE_PREDICTIONS,
        "inference_precision": "upstream_evaluator_default_fp32_no_amp_flag",
        "additional_training_performed": False,
        "checkpoint": checkpoint,
        "oracle_admission": oracle,
        "nominal": nominal,
        "repair": repair,
        "success_authority": "actual_libero_goal_predicates_only",
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
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--oracle-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = execute_live(
        source_root=args.source_root.resolve(),
        checkpoint_path=args.checkpoint_path.resolve(),
        snapshot_path=args.snapshot.resolve(),
        oracle_report_path=args.oracle_report.resolve(),
        output_dir=args.output_dir.resolve(),
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

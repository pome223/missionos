#!/usr/bin/env python3
"""Run one governed VLA-0 recovery diagnostic on a saved LIBERO state.

The saved state is a diagnostic MuJoCo clone, not the uninterrupted source
world.  A successful run can therefore establish recovery of that clone under
the shared deterministic verifier, but cannot establish same-world Semantic
Repair.  VLA-0 receives no approval, dispatch, or verifier authority here.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import uuid4


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402
from scripts.run_groot_lerobot_same_world_repair import (  # noqa: E402
    _object_poses,
    _object_witnesses,
    _read_failure_snapshot,
)
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable  # noqa: E402
from src.runtime.groot_libero_same_world_repair import (  # noqa: E402
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    approve_same_world_repair,
    build_same_world_repair_dispatch,
    verify_exact_repair_instruction_payload,
)
from src.runtime.libero_panda_official_runner_instrumentation import (  # noqa: E402
    _observe_libero_goal_predicates,
    digest_runtime_material,
)
from src.runtime.libero_panda_predicate_package import (  # noqa: E402
    LIBERO_PANDA_SCENE8_ENVIRONMENT,
)
from src.runtime.vla0_libero_same_world_repair import (  # noqa: E402
    build_vla0_same_world_repair_proposal,
    run_vla0_same_world_repair,
)


OPT_IN_ENV = "RUN_MISSIONOS_VLA0_LIBERO_SNAPSHOT_RECOVERY"
VLA0_SOURCE_REVISION = "b78db19eda67b02844fe82b8929e1f94de7f2dcc"
VLA0_LEROBOT_REVISION = "c55fbe1b3e36f640d659918945514fbb43a2b53d"
VLA0_CHECKPOINT_REVISION = "6c62a511ad56042b2b3ea0a90c4def33e1ea3b96"
VLA0_CHECKPOINT_FILES = {
    "model_last.pth": (
        15_019_196_217,
        "5b35632c59130ed8a67e5019707851735cb3e13fcc769f172eae45c37bf39b9d",
    ),
    "dataset_stats.pkl": (
        403,
        "ee365dfe83bb9a20a8e5b3777ce72d44e26b7352fa6e609cd9d6483aaae09c6b",
    ),
    "model_last/model-00001-of-00002.safetensors": (
        4_997_750_760,
        "2021480ff1b4fb6e635195591615d7cd93d7fba492865375d6f49722b4415e4a",
    ),
    "model_last/model-00002-of-00002.safetensors": (
        2_511_587_184,
        "2fd9786a84cf652f432173bb4f69951eaaca3d840995f730cd7ec4496b98d659",
    ),
    "model_last/tokenizer.json": (
        11_422_064,
        "ba0c439f7be467bf47d12a7e6f9adc6116201056fc60c67f431c679b7c16afc8",
    ),
}
TASK_SUITE = "libero_10"
TASK_NAME = "KITCHEN_SCENE8_put_both_moka_pots_on_the_stove"
TASK_ID = 8
SOURCE_INSTRUCTION = "put both moka pots on the stove"
EXPECTED_SOURCE_VECTOR = [True, False, True]
PROCESS_SEED = 1000
ENVIRONMENT_SEED = 0
ENSEMBLE_PREDICTIONS = 8


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
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _verify_checkpoint(checkpoint_path: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for relative_path, (expected_size, expected_sha256) in VLA0_CHECKPOINT_FILES.items():
        path = checkpoint_path / relative_path
        if not path.is_file():
            raise RuntimeError(f"vla0_checkpoint_file_missing:{relative_path}")
        observed_size = path.stat().st_size
        observed_sha256 = _sha256_path(path)
        if observed_size != expected_size or observed_sha256 != expected_sha256:
            raise RuntimeError(f"vla0_checkpoint_file_mismatch:{relative_path}")
        files[relative_path] = {
            "size_bytes": observed_size,
            "sha256": observed_sha256,
        }
    return {
        "repository": "ankgoyal/vla0-libero",
        "revision": VLA0_CHECKPOINT_REVISION,
        "files": files,
    }


def _predicate_material(environment: Any) -> list[dict[str, Any]]:
    return [
        item.to_material()
        for item in _observe_libero_goal_predicates(
            environment,
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        )
    ]


def _environment_adapter(environment: Any) -> Any:
    """Expose the shape used by the shared object-witness instrumentation."""

    return SimpleNamespace(environment=SimpleNamespace(_env=environment))


def _official_ensemble_action(
    *,
    prediction: Any,
    previous_predictions: list[Any],
    action_horizon: int = 1,
) -> Any:
    """Apply VLA-0's published version-1 temporal ensemble for one action."""

    import numpy as np

    previous_predictions.append(np.asarray(prediction, dtype=np.float32).copy())
    if len(previous_predictions) > ENSEMBLE_PREDICTIONS:
        previous_predictions.pop(0)

    retained: list[Any] = []
    combined = np.zeros_like(previous_predictions[-1])
    weights = np.zeros_like(previous_predictions[-1])
    for old_prediction in previous_predictions[:-1]:
        if len(old_prediction) <= action_horizon:
            continue
        shifted = old_prediction[action_horizon:]
        retained.append(shifted)
        combined[: len(shifted)] += 0.5 * shifted
        weights[: len(shifted)] += 0.5
    retained.append(previous_predictions[-1])
    combined += previous_predictions[-1]
    weights += 1.0
    previous_predictions[:] = retained
    return combined[0] / weights[0]


def execute_live(
    *,
    source_root: Path,
    checkpoint_source_path: Path,
    runtime_checkpoint_path: Path,
    snapshot_path: Path,
    output_path: Path,
    dispatch_state_path: Path,
    operator_approval_ref: str,
    maximum_repair_steps: int,
    episode_init_state_index: int,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("vla0_snapshot_recovery_live_opt_in_required")
    if maximum_repair_steps <= 0:
        raise ValueError("vla0_maximum_repair_steps_invalid")
    if _git_revision(source_root) != VLA0_SOURCE_REVISION:
        raise RuntimeError("vla0_source_revision_mismatch")
    lerobot_root = source_root / "libs" / "RoboVerse" / "libs" / "lerobot"
    if _git_revision(lerobot_root) != VLA0_LEROBOT_REVISION:
        raise RuntimeError("vla0_lerobot_revision_mismatch")
    checkpoint_evidence = _verify_checkpoint(checkpoint_source_path)

    runtime_paths = (
        lerobot_root / "src",
        source_root,
        source_root / "libs" / "RoboVerse",
    )
    for runtime_path in reversed(runtime_paths):
        normalized = str(runtime_path)
        if normalized not in sys.path:
            sys.path.insert(0, normalized)
    os.chdir(source_root)

    os.environ.setdefault("MUJOCO_GL", "egl")
    import numpy as np
    import torch

    from roboverse.datasets.lerobot import dataloader as lerobot_dataloader
    from roboverse.evals.libero.eval import init_libero_env, libero_to_rv_obs
    from roboverse.main import get_cfg as get_roboverse_cfg
    from rv_train.train import get_cfg as get_vla0_cfg
    from rv_train.train import get_pretrained_model

    # The public LIBERO dataset metadata is v2.0 while the pinned VLA-0
    # submodule expects v3.0 parquet metadata. Evaluation only needs these two
    # configured camera keys, so provide that exact auxiliary metadata without
    # altering the observation, model, prediction, ensemble, or action path.
    lerobot_dataloader.get_lerobot_metadata = lambda repo_id: SimpleNamespace(
        camera_keys=("image", "wrist_image")
    )

    random.seed(PROCESS_SEED)
    np.random.seed(PROCESS_SEED)
    torch.manual_seed(PROCESS_SEED)
    torch.cuda.manual_seed_all(PROCESS_SEED)

    model_cfg = get_vla0_cfg(
        str(runtime_checkpoint_path / "config.yaml"),
        cfg_opts="",
    )
    if model_cfg.EXP.DATASET != "roboverse" or model_cfg.EXP.MODEL != "qwen":
        raise RuntimeError("vla0_checkpoint_runtime_contract_mismatch")
    if int(model_cfg.MODEL.QWEN.horizon) != 8:
        raise RuntimeError("vla0_checkpoint_prediction_horizon_mismatch")
    if int(model_cfg.MODEL.QWEN.original_action_dim) != 7:
        raise RuntimeError("vla0_checkpoint_action_dimension_mismatch")
    dataset_cfg = get_roboverse_cfg(
        model_cfg.DATALOADER.ROBOVERSE.cfg_path,
        model_cfg.DATALOADER.ROBOVERSE.cfg_opts,
    )

    environment, init_states, _, task_instruction = init_libero_env(
        verse_config=dataset_cfg,
        task_name=TASK_NAME,
        seed=ENVIRONMENT_SEED,
        act_space="original",
        task_suite_name=TASK_SUITE,
        num_steps=maximum_repair_steps,
    )
    reset_count = 0
    try:
        environment.reset()
        reset_count += 1
        environment.set_init_state(init_states[episode_init_state_index])
        simulator_state, snapshot_metadata = _read_failure_snapshot(snapshot_path)
        if snapshot_metadata.get("task_suite") != TASK_SUITE:
            raise RuntimeError("vla0_snapshot_task_suite_mismatch")
        if snapshot_metadata.get("task_id") != TASK_ID:
            raise RuntimeError("vla0_snapshot_task_id_mismatch")
        if snapshot_metadata.get("episode_init_state_index") != episode_init_state_index:
            raise RuntimeError("vla0_snapshot_init_state_mismatch")
        observation = environment.regenerate_obs_from_state(simulator_state)
        restored_state = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        if hashlib.sha256(restored_state.tobytes()).hexdigest() != snapshot_metadata.get(
            "simulator_state_sha256"
        ):
            raise RuntimeError("vla0_snapshot_restored_state_digest_mismatch")

        source_predicates = _predicate_material(environment)
        source_vector = [item["satisfied"] for item in source_predicates]
        if source_vector != EXPECTED_SOURCE_VECTOR:
            raise RuntimeError("vla0_snapshot_source_vector_mismatch")
        adapter = _environment_adapter(environment)
        source_object_poses = _object_poses(adapter)
        source_contract_material = {
            "task_suite": TASK_SUITE,
            "task_name": TASK_NAME,
            "task_id": TASK_ID,
            "episode_init_state_index": episode_init_state_index,
            "source_instruction": SOURCE_INSTRUCTION,
            "environment_seed": ENVIRONMENT_SEED,
            "source_goal_predicate_vector": source_vector,
            "diagnostic_handoff_snapshot_sha256": snapshot_metadata[
                "snapshot_artifact_sha256"
            ],
            "snapshot_producer_checkpoint_revision": snapshot_metadata.get(
                "checkpoint_revision"
            ),
            "vla0_source_revision": VLA0_SOURCE_REVISION,
            "vla0_lerobot_revision": VLA0_LEROBOT_REVISION,
            "vla0_checkpoint": checkpoint_evidence,
            "maximum_repair_steps": maximum_repair_steps,
            "action_horizon": 1,
            "ensemble_predictions": ENSEMBLE_PREDICTIONS,
            "metadata_compatibility_shim": {
                "scope": "camera_keys_only",
                "camera_keys": ["image", "wrist_image"],
                "changes_model_or_action_path": False,
            },
            "runner_source_sha256": _sha256_path(Path(__file__)),
            "vla0_binding_source_sha256": _sha256_path(
                Path(build_vla0_same_world_repair_proposal.__code__.co_filename)
            ),
        }
        source_contract_sha256 = canonical_sha256(source_contract_material)
        if task_instruction != SOURCE_INSTRUCTION:
            raise RuntimeError("vla0_snapshot_task_instruction_mismatch")
        model, loaded_model_cfg = get_pretrained_model(
            str(runtime_checkpoint_path / "model_last.pth"),
            0,
            torch_compile=False,
        )
        model.eval()
        if (
            loaded_model_cfg.MODEL.QWEN.qwen_model_id
            != model_cfg.MODEL.QWEN.qwen_model_id
            or int(loaded_model_cfg.MODEL.QWEN.horizon) != 8
            or int(loaded_model_cfg.MODEL.QWEN.original_action_dim) != 7
        ):
            raise RuntimeError("vla0_loaded_model_contract_mismatch")
        proposal = build_vla0_same_world_repair_proposal(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            environment_session_id=f"vla0-libero10-diagnostic-clone:{uuid4()}",
            source_contract_sha256=source_contract_sha256,
            source_goal_predicates=source_predicates,
            reset_count=reset_count,
            maximum_repair_steps=maximum_repair_steps,
            source_object_poses=source_object_poses,
            repair_instruction_variant="original_task",
            state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
            diagnostic_handoff_snapshot_sha256=snapshot_metadata[
                "snapshot_artifact_sha256"
            ],
        )
        approval = approve_same_world_repair(
            proposal=proposal,
            operator_approval_ref=operator_approval_ref,
        )
        dispatch = build_same_world_repair_dispatch(
            proposal=proposal,
            approval=approval,
            dispatch_ref=f"vla0-libero-snapshot-dispatch:{uuid4()}",
        )

        prior_predictions: list[Any] = []
        previous_positions = {
            name: np.asarray(position, dtype=np.float64)
            for name, position in source_object_poses.items()
        }

        def invoke_model(
            raw_observation: Any,
            instruction: str,
            chunk_index: int,
        ) -> tuple[Any, dict[str, Any]]:
            del chunk_index
            model_observation = libero_to_rv_obs(raw_observation, instruction, dataset_cfg)
            instruction_evidence = verify_exact_repair_instruction_payload(
                payload=model_observation["instr"],
                expected_instruction=instruction,
            )
            request_sha256 = digest_runtime_material("vla0_policy_request", model_observation)
            with torch.no_grad():
                output = model(
                    **model_observation,
                    get_loss=False,
                    get_action=True,
                    generate_temperature=0.0,
                )
            prediction_sha256 = digest_runtime_material("vla0_policy_prediction", output)
            raw_prediction = output["out_ori_act"][0].detach().cpu().numpy()
            if raw_prediction.shape != (8, 7):
                raise RuntimeError("vla0_prediction_shape_mismatch")
            selected_action = _official_ensemble_action(
                prediction=raw_prediction,
                previous_predictions=prior_predictions,
            )
            if selected_action.shape != (7,):
                raise RuntimeError("vla0_selected_action_shape_mismatch")
            return selected_action, {
                "model_runtime_invoked": True,
                "policy_request_sha256": request_sha256,
                "policy_response_sha256": prediction_sha256,
                "repair_instruction_sha256": proposal["repair_instruction_sha256"],
                **instruction_evidence,
            }

        def apply_action(
            selected_action: Any,
            chunk_index: int,
        ) -> tuple[Any, dict[str, Any]]:
            nonlocal observation
            action = np.asarray(selected_action, dtype=np.float32).copy()
            if action.shape != (7,) or not np.all(np.isfinite(action)):
                raise RuntimeError("vla0_selected_action_invalid")
            action[-1] = 1.0 if action[-1] > 0.0 else -1.0
            before = digest_runtime_material("vla0_observation", observation)
            next_observation, _, done, info = environment.step(action.tolist())
            after = digest_runtime_material("vla0_observation", next_observation)
            predicates = _predicate_material(environment)
            official_result = bool(environment.check_success())
            conjunction = all(item["satisfied"] for item in predicates)
            if official_result is not conjunction:
                raise RuntimeError("vla0_official_predicate_result_mismatch")
            witnesses = _object_witnesses(
                adapter,
                {"robot_state": {"eef": {"pos": next_observation["robot0_eef_pos"]}}},
                previous_positions,
            )
            action_sha256 = digest_runtime_material("vla0_selected_action", action)
            trace = {
                "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
                "chunk_index": chunk_index,
                "action_step_index": 0,
                "action_step_number": 1,
                "global_repair_step_index": chunk_index,
                "global_repair_step_number": chunk_index + 1,
                "action_step_sha256": action_sha256,
                "goal_predicate_observations": deepcopy(predicates),
                "goal_predicate_vector_sha256": canonical_sha256(
                    {"goal_predicate_observations": predicates}
                ),
                "official_predicate_conjunction": conjunction,
                "official_predicate_result": official_result,
                "conjunction_matches_official_result": True,
                "object_witnesses": witnesses,
                "frame_capture": None,
            }
            observation = next_observation
            return next_observation, {
                "simulator_step_return_observed": True,
                "simulator_effect_observed": before != after,
                "official_predicate_result": official_result,
                "done": bool(done or info.get("done", False)),
                "truncated": False,
                "action_chunk_sha256": action_sha256,
                "preservation_step_trace": [trace],
            }

        repair_result = run_vla0_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=DispatchAuthorityTable(dispatch_state_path),
            initial_observation=observation,
            invoke_model=invoke_model,
            apply_action_chunk=apply_action,
            observe_goal_predicates=lambda: _predicate_material(environment),
            observed_reset_count=lambda: reset_count,
            observed_state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
        )
        final_vector = [
            item["satisfied"] for item in repair_result["final_goal_predicate_observations"]
        ]
        recovery_observed = bool(
            repair_result["status"] == "satisfied_diagnostic_observation"
            and repair_result["predicate_improvement_observed"] is True
            and final_vector == [True, True, True]
            and repair_result["first_preservation_violation"] is None
            and repair_result["first_preservation_invariant_breach"] is None
        )
        report_without_digest = {
            "schema_version": "missionos.vla0_libero_snapshot_recovery.v1",
            "status": (
                "diagnostic_clone_recovery_observed"
                if recovery_observed
                else "diagnostic_clone_recovery_not_observed"
            ),
            "source_contract": source_contract_material,
            "source_contract_sha256": source_contract_sha256,
            "source_goal_predicate_vector": source_vector,
            "final_goal_predicate_vector": final_vector,
            "proposal": proposal,
            "approval": approval,
            "dispatch": dispatch,
            "repair_result": repair_result,
            "diagnostic_clone_recovery_observed": recovery_observed,
            "semantic_repair_established": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
            "claim_boundary": {
                "state_continuity_basis": STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
                "same_verifier_as_groot_cohort": True,
                "same_7d_libero_action_interface": True,
                "general_vla0_recovery_rate_established": False,
                "same_world_semantic_repair_established": False,
                "autonomous_oracle_capability_established": False,
                "real_world_safety_established": False,
            },
        }
        report = {
            **report_without_digest,
            "result_sha256": canonical_sha256(report_without_digest),
        }
        _write_json(output_path, report)
        return report
    finally:
        environment.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--checkpoint-source-path", type=Path, required=True)
    parser.add_argument("--runtime-checkpoint-path", type=Path, required=True)
    parser.add_argument("--restore-snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dispatch-state", type=Path, required=True)
    parser.add_argument("--operator-approval-ref", required=True)
    parser.add_argument("--maximum-repair-steps", type=int, default=720)
    parser.add_argument("--episode-init-state-index", type=int, default=15)
    args = parser.parse_args()
    report = execute_live(
        source_root=args.source_root.resolve(),
        checkpoint_source_path=args.checkpoint_source_path.resolve(),
        runtime_checkpoint_path=args.runtime_checkpoint_path.resolve(),
        snapshot_path=args.restore_snapshot.resolve(),
        output_path=args.output.resolve(),
        dispatch_state_path=args.dispatch_state.resolve(),
        operator_approval_ref=args.operator_approval_ref,
        maximum_repair_steps=args.maximum_repair_steps,
        episode_init_state_index=args.episode_init_state_index,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["diagnostic_clone_recovery_observed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

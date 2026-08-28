#!/usr/bin/env python3
"""Test regenerated and target-specific T5 instructions on one fixed diagnostic."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import sys
from typing import Any, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402
from scripts.run_cosmos_policy_libero_experiment import (  # noqa: E402
    _build_model_runtime,
    _write_json,
)
from scripts.run_cosmos_policy_libero_paired_pose_sensitivity import (  # noqa: E402
    DEFAULT_POSE_ACTION_COUNTS,
    _construct_displacement_states,
    _normalize_successful_source_robot,
    _replay_nominal,
    _successful_source_material,
    _verify_nominal_report,
)
from scripts.run_cosmos_policy_libero_pose_rollout_diagnostic import (  # noqa: E402
    _run_trial,
    _verify_sensitivity_report,
)


OPT_IN_ENV = "RUN_MISSIONOS_COSMOS_POLICY_LIBERO_T5_INSTRUCTION_DIAGNOSTIC"
BASELINE_PROMPT = "put both moka pots on the stove"
DEFAULT_POSE_ACTION_COUNT = 184
DEFAULT_DISPLACEMENT_METRES = 0.225
DEFAULT_SEED = 195
DEFAULT_MAXIMUM_ACTIONS = 128
MAXIMUM_ACCEPTED_NUMERIC_PARITY_DIFFERENCE = 0.00390625
PROMPT_ARMS = (
    (
        "state_specific",
        "put the moka pot that is not on the stove on the stove",
        "target_specific_current_state_description",
    ),
    (
        "remaining",
        "put the remaining moka pot on the stove",
        "target_specific_short_description",
    ),
    (
        "move_state_specific",
        "move the moka pot that is not on the stove onto the stove",
        "target_specific_action_description",
    ),
    (
        "wrong_target",
        "put the moka pot that is already on the stove on the stove",
        "semantic_negative_control",
    ),
)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_parity(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    if (
        result.get("schema_version")
        != "missionos.cosmos_policy_t5_11b_embedding_parity.v1"
        or result.get("baseline_prompt") != BASELINE_PROMPT
        or result.get("model") != "google-t5/t5-11b"
        or result.get("shape") != [1, 512, 1024]
        or result.get("generated_storage_dtype") != "torch.bfloat16"
        or not isinstance(result.get("maximum_absolute_difference"), (int, float))
        or float(result["maximum_absolute_difference"])
        > MAXIMUM_ACCEPTED_NUMERIC_PARITY_DIFFERENCE
    ):
        raise RuntimeError("cosmos_t5_instruction_numeric_parity_invalid")
    expected = {prompt for _, prompt, _ in PROMPT_ARMS}
    if set(result.get("unknown_prompts", ())) != expected:
        raise RuntimeError("cosmos_t5_instruction_prompt_set_mismatch")
    return result


def _action_delta(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    left_actions = [item["action_7d"] for item in left["action_trace"]]
    right_actions = [item["action_7d"] for item in right["action_trace"]]
    if len(left_actions) != len(right_actions):
        raise RuntimeError("cosmos_t5_instruction_baseline_action_count_mismatch")
    per_step = [
        math.dist([float(value) for value in a], [float(value) for value in b])
        for a, b in zip(left_actions, right_actions, strict=True)
    ]
    return {
        "sample_count": len(per_step),
        "action_trace_bitwise_equal": all(a == b for a, b in zip(left_actions, right_actions, strict=True)),
        "mean_7d_action_l2_delta": sum(per_step) / len(per_step),
        "maximum_7d_action_l2_delta": max(per_step),
        "contact_count_equal": left["contact_observation_count"] == right["contact_observation_count"],
        "final_predicate_vector_equal": left["final_goal_predicate_vector"] == right["final_goal_predicate_vector"],
        "minimum_eef_distance_absolute_delta_metres": abs(
            float(left["minimum_end_effector_distance_to_target_metres"])
            - float(right["minimum_end_effector_distance_to_target_metres"])
        ),
        "maximum_target_translation_absolute_delta_metres": abs(
            float(left["maximum_target_translation_metres"])
            - float(right["maximum_target_translation_metres"])
        ),
    }


def execute_live(
    *,
    source_root: Path,
    checkpoint_path: Path,
    tokenizer_path: Path,
    nominal_report_path: Path,
    sensitivity_report_path: Path,
    generated_embeddings_path: Path,
    parity_report_path: Path,
    output_dir: Path,
    pose_action_count: int = DEFAULT_POSE_ACTION_COUNT,
    displacement_metres: float = DEFAULT_DISPLACEMENT_METRES,
    seed: int = DEFAULT_SEED,
    maximum_actions: int = DEFAULT_MAXIMUM_ACTIONS,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("cosmos_t5_instruction_opt_in_required")
    if output_dir.exists():
        raise ValueError("cosmos_t5_instruction_output_exists")
    if not 1 <= maximum_actions <= 128:
        raise ValueError("cosmos_t5_instruction_action_budget_invalid")
    parity = _load_parity(parity_report_path)
    nominal = _verify_nominal_report(nominal_report_path)
    sensitivity = _verify_sensitivity_report(sensitivity_report_path, nominal["result_sha256"])
    poses, first_success_state = _replay_nominal(
        nominal_report=nominal, pose_action_counts=DEFAULT_POSE_ACTION_COUNTS
    )
    pose_by_count = {int(item["action_count"]): item for item in poses}
    if pose_action_count not in pose_by_count:
        raise ValueError("cosmos_t5_instruction_pose_invalid")
    successful_state, normalization = _normalize_successful_source_robot(
        successful_state=first_success_state, initial_pose=poses[0]
    )
    source = _successful_source_material(successful_state)
    fixture = _construct_displacement_states(
        successful_state=successful_state,
        source=source,
        distances=(0.0, displacement_metres),
    )[1]
    sensitivity_fixture = next(
        item
        for item in sensitivity["fixture_grid"]
        if float(item["requested_displacement_metres"]) == displacement_metres
    )
    if not math.isclose(
        float(fixture["observed_displacement_metres"]),
        float(sensitivity_fixture["observed_displacement_metres"]),
        abs_tol=1e-9,
    ):
        raise RuntimeError("cosmos_t5_instruction_fixture_reproduction_drift")

    output_dir.mkdir(parents=True)
    cfg, model, dataset_stats, checkpoint = _build_model_runtime(
        source_root=source_root,
        checkpoint_path=checkpoint_path,
        tokenizer_path=tokenizer_path,
        process_seed=seed,
    )
    import torch
    from cosmos_policy.experiments.robot import cosmos_utils

    with generated_embeddings_path.open("rb") as stream:
        generated = pickle.load(stream)
    expected_prompts = {BASELINE_PROMPT, *(prompt for _, prompt, _ in PROMPT_ARMS)}
    if set(generated) != expected_prompts:
        raise RuntimeError("cosmos_t5_instruction_generated_cache_keys_invalid")
    if BASELINE_PROMPT not in cosmos_utils.t5_text_embeddings_cache:
        raise RuntimeError("cosmos_t5_instruction_official_baseline_missing")
    official_baseline = cosmos_utils.t5_text_embeddings_cache[BASELINE_PROMPT].detach().clone()

    arms: list[dict[str, Any]] = []

    def run_arm(arm_id: str, prompt: str, role: str, embedding: Any) -> None:
        cosmos_utils.t5_text_embeddings_cache[prompt] = embedding.to(
            device=official_baseline.device, dtype=torch.bfloat16
        )
        arm_dir = output_dir / arm_id
        arm_dir.mkdir(parents=True)
        trial = _run_trial(
            cfg=cfg,
            model=model,
            dataset_stats=dataset_stats,
            nominal=nominal,
            pose=pose_by_count[pose_action_count],
            fixture=fixture,
            seed=seed,
            output_dir=arm_dir,
            artifact_root=output_dir,
            maximum_actions=maximum_actions,
            instruction=prompt,
        )
        arms.append({"arm_id": arm_id, "role": role, **trial})

    run_arm("official_baseline", BASELINE_PROMPT, "released_cache_control", official_baseline)
    run_arm(
        "regenerated_baseline",
        BASELINE_PROMPT,
        "generated_embedding_behavioral_parity_control",
        generated[BASELINE_PROMPT],
    )
    for arm_id, prompt, role in PROMPT_ARMS:
        run_arm(arm_id, prompt, role, generated[prompt])

    baseline_behavior = _action_delta(arms[0], arms[1])
    report_without_digest = {
        "schema_version": "missionos.cosmos_policy_libero_t5_instruction_diagnostic.v1",
        "status": "bounded_t5_instruction_diagnostic_completed",
        "nominal_report_sha256": nominal["result_sha256"],
        "sensitivity_report_sha256": sensitivity["result_sha256"],
        "generated_embeddings_sha256": _sha256_path(generated_embeddings_path),
        "parity_report_sha256": _sha256_path(parity_report_path),
        "numeric_parity": parity,
        "numeric_parity_gate": {
            "maximum_accepted_absolute_difference": MAXIMUM_ACCEPTED_NUMERIC_PARITY_DIFFERENCE,
            "passed": True,
            "bitwise_equality_required": False,
        },
        "behavioral_parity": baseline_behavior,
        "successful_source_robot_normalization": normalization,
        "requested_displacement_metres": displacement_metres,
        "observed_displacement_metres": fixture["observed_displacement_metres"],
        "pose_action_count": pose_action_count,
        "seed": seed,
        "maximum_actions_per_arm": maximum_actions,
        "arm_count": len(arms),
        "additional_training_performed": False,
        "arms": arms,
        "claim_boundary": {
            "authority": "diagnostic_only",
            "simulator_state_directly_changed_for_diagnostic": True,
            "generated_embedding_is_not_a_trained_repair_skill": True,
            "actual_predicate_success_is_diagnostic_only": True,
            "future_predictions_may_establish_success": False,
            "governed_repair_dispatch_performed": False,
            "repair_success_established": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        },
    }
    report = {**report_without_digest, "result_sha256": canonical_sha256(report_without_digest)}
    _write_json(output_dir / "t5-instruction-diagnostic.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--checkpoint-path", type=Path, required=True)
    parser.add_argument("--tokenizer-path", type=Path, required=True)
    parser.add_argument("--nominal-report", type=Path, required=True)
    parser.add_argument("--sensitivity-report", type=Path, required=True)
    parser.add_argument("--generated-embeddings", type=Path, required=True)
    parser.add_argument("--parity-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pose-action-count", type=int, default=DEFAULT_POSE_ACTION_COUNT)
    parser.add_argument("--displacement-metres", type=float, default=DEFAULT_DISPLACEMENT_METRES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--maximum-actions", type=int, default=DEFAULT_MAXIMUM_ACTIONS)
    args = parser.parse_args()
    report = execute_live(
        source_root=args.source_root.resolve(),
        checkpoint_path=args.checkpoint_path.resolve(),
        tokenizer_path=args.tokenizer_path.resolve(),
        nominal_report_path=args.nominal_report.resolve(),
        sensitivity_report_path=args.sensitivity_report.resolve(),
        generated_embeddings_path=args.generated_embeddings.resolve(),
        parity_report_path=args.parity_report.resolve(),
        output_dir=args.output_dir.resolve(),
        pose_action_count=args.pose_action_count,
        displacement_metres=args.displacement_metres,
        seed=args.seed,
        maximum_actions=args.maximum_actions,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "behavioral_parity": report["behavioral_parity"],
                "arms": [
                    {
                        key: arm[key]
                        for key in (
                            "arm_id",
                            "role",
                            "instruction",
                            "contact_observation_count",
                            "minimum_end_effector_distance_to_target_metres",
                            "maximum_target_translation_metres",
                            "final_goal_predicate_vector",
                            "actual_predicate_success_observed",
                        )
                    }
                    for arm in report["arms"]
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

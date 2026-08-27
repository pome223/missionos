#!/usr/bin/env python3
"""Run a bounded cache-only multi-seed Repair diagnostic.

This probe reuses the exact pretrained LIBERO instruction embedding and loads
the policy once. It does not claim an instruction ablation, selector success,
or general recovery rate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402
from scripts.run_cosmos_policy_libero_experiment import (  # noqa: E402
    COSMOS_POLICY_SOURCE_REVISION,
    EPISODE_INIT_STATE_INDEX,
    TASK_ID,
    TASK_NAME,
    TASK_SUITE,
    _build_model_runtime,
    _run_repair,
    _verify_oracle_recoverability_report,
    _write_json,
)


OPT_IN_ENV = "RUN_MISSIONOS_COSMOS_POLICY_LIBERO_SEED_PROBE"
MAXIMUM_ACTIONS = 128
DEFAULT_SEEDS = (17, 71, 195, 231)


def _verify_nominal_admission_report(report_path: Path) -> dict[str, Any]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("cosmos_policy_seed_probe_nominal_report_unreadable") from error
    if not isinstance(report, dict):
        raise RuntimeError("cosmos_policy_seed_probe_nominal_report_not_mapping")
    supplied_digest = report.get("result_sha256")
    material = {key: value for key, value in report.items() if key != "result_sha256"}
    if supplied_digest != canonical_sha256(material):
        raise RuntimeError("cosmos_policy_seed_probe_nominal_report_digest_mismatch")
    if report.get("nominal_success_observed") is not True:
        raise RuntimeError("cosmos_policy_seed_probe_nominal_success_not_observed")
    if (
        report.get("task_suite") != TASK_SUITE
        or report.get("task_name") != TASK_NAME
        or report.get("task_id") != TASK_ID
        or report.get("episode_init_state_index") != EPISODE_INIT_STATE_INDEX
    ):
        raise RuntimeError("cosmos_policy_seed_probe_nominal_task_mismatch")
    if report.get("source_revision") != COSMOS_POLICY_SOURCE_REVISION:
        raise RuntimeError("cosmos_policy_seed_probe_nominal_source_revision_mismatch")
    return {
        "schema_version": "missionos.cosmos_policy_nominal_admission.v1",
        "authority": "prior_actual_libero_predicate_observation",
        "report_sha256": supplied_digest,
        "applied_action_count": report.get("applied_action_count"),
        "nominal_success_observed": True,
        "does_not_establish_repair": True,
    }


def _validate_seeds(seeds: tuple[int, ...]) -> None:
    if len(seeds) != 4 or len(set(seeds)) != len(seeds):
        raise ValueError("cosmos_policy_seed_probe_requires_four_unique_seeds")
    if any(isinstance(seed, bool) or seed < 0 or seed > 2**32 - 1 for seed in seeds):
        raise ValueError("cosmos_policy_seed_probe_seed_out_of_range")


def _actual_effect_statistics(report: dict[str, Any]) -> dict[str, Any]:
    traces = [
        step
        for chunk in report["repair_result"]["chunk_evidence"]
        for step in chunk["preservation_step_trace"]
    ]
    witnesses = [step["object_witnesses"]["moka_pot_2"] for step in traces]
    if not witnesses:
        raise RuntimeError("cosmos_policy_seed_probe_actual_effect_trace_empty")
    first_position = witnesses[0]["position_metres"]
    return {
        "schema_version": "missionos.cosmos_policy_actual_effect_statistics.v1",
        "authority": "actual_libero_simulator_observation",
        "sample_count": len(witnesses),
        "gripper_contact_observation_count": sum(
            item["gripper_contact_observed"] is True for item in witnesses
        ),
        "minimum_end_effector_distance_to_target_metres": min(
            float(item["end_effector_distance_metres"]) for item in witnesses
        ),
        "maximum_target_translation_from_first_observation_metres": max(
            math.dist(first_position, item["position_metres"]) for item in witnesses
        ),
        "physical_path_length_established": False,
    }


def execute_live(
    *,
    source_root: Path,
    checkpoint_path: Path,
    tokenizer_path: Path,
    snapshot_path: Path,
    oracle_recoverability_report_path: Path,
    nominal_report_path: Path,
    output_dir: Path,
    operator_approval_ref: str,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("cosmos_policy_libero_seed_probe_opt_in_required")
    _validate_seeds(seeds)
    if output_dir.exists():
        raise ValueError("cosmos_policy_seed_probe_output_directory_already_exists")
    oracle_admission = _verify_oracle_recoverability_report(
        report_path=oracle_recoverability_report_path,
        snapshot_path=snapshot_path,
    )
    nominal_admission = _verify_nominal_admission_report(nominal_report_path)
    output_dir.mkdir(parents=True)
    cfg, model, dataset_stats, checkpoint_evidence = _build_model_runtime(
        source_root=source_root,
        checkpoint_path=checkpoint_path,
        tokenizer_path=tokenizer_path,
        process_seed=seeds[0],
    )
    results = []
    for seed in seeds:
        seed_root = output_dir / f"seed-{seed}"
        report = _run_repair(
            cfg=cfg,
            model=model,
            dataset_stats=dataset_stats,
            checkpoint_evidence=checkpoint_evidence,
            source_root=source_root,
            snapshot_path=snapshot_path,
            artifact_root=seed_root,
            dispatch_state_path=seed_root / "dispatch.json",
            operator_approval_ref=operator_approval_ref,
            maximum_actions=MAXIMUM_ACTIONS,
            process_seed=seed,
        )
        results.append(
            {
                "process_seed": seed,
                "report_relative_path": f"seed-{seed}/repair/report.json",
                "report_sha256": report["result_sha256"],
                "applied_action_count": report["repair_result"]["applied_action_count"],
                "final_goal_predicate_vector": report["final_goal_predicate_vector"],
                "scripted_fixture_repair_established": report[
                    "scripted_fixture_repair_established"
                ],
                "action_command_statistics": report["action_command_statistics"],
                "actual_effect_statistics": _actual_effect_statistics(report),
            }
        )

    report_without_digest = {
        "schema_version": "missionos.cosmos_policy_libero_seed_probe.v1",
        "status": "bounded_seed_probe_completed",
        "seeds": list(seeds),
        "maximum_applied_actions_per_seed": MAXIMUM_ACTIONS,
        "instruction_variant": "official_cached_original_task_only",
        "instruction_ablation_performed": False,
        "model_loaded_once": True,
        "oracle_admission": oracle_admission,
        "nominal_admission": nominal_admission,
        "results": results,
        "claim_boundary": {
            "authority": "diagnostic_only",
            "candidate_selector_used": False,
            "oracle_selector_used": False,
            "general_recovery_rate_established": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        },
    }
    summary = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    _write_json(output_dir / "seed-probe.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--checkpoint-path", type=Path, required=True)
    parser.add_argument("--tokenizer-path", type=Path, required=True)
    parser.add_argument("--restore-snapshot", type=Path, required=True)
    parser.add_argument("--oracle-recoverability-report", type=Path, required=True)
    parser.add_argument("--nominal-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--operator-approval-ref", required=True)
    parser.add_argument(
        "--seeds",
        default=",".join(str(seed) for seed in DEFAULT_SEEDS),
        help="exactly four unique comma-separated uint32 seeds",
    )
    args = parser.parse_args()
    seeds = tuple(int(value.strip()) for value in args.seeds.split(","))
    result = execute_live(
        source_root=args.source_root.resolve(),
        checkpoint_path=args.checkpoint_path.resolve(),
        tokenizer_path=args.tokenizer_path.resolve(),
        snapshot_path=args.restore_snapshot.resolve(),
        oracle_recoverability_report_path=args.oracle_recoverability_report.resolve(),
        nominal_report_path=args.nominal_report.resolve(),
        output_dir=args.output_dir.resolve(),
        operator_approval_ref=args.operator_approval_ref,
        seeds=seeds,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

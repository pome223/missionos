#!/usr/bin/env python3
"""Run one bounded Cosmos Policy trial on an admitted small-displacement fixture."""

from __future__ import annotations

import argparse
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
    _build_model_runtime,
    _run_repair,
    _verify_oracle_recoverability_report,
    _write_json,
)
from scripts.run_cosmos_policy_libero_seed_probe import (  # noqa: E402
    _actual_effect_statistics,
    _verify_nominal_admission_report,
)


OPT_IN_ENV = "RUN_MISSIONOS_COSMOS_POLICY_LIBERO_CURRICULUM_PROBE"
DEFAULT_PROCESS_SEED = 195
DEFAULT_MAXIMUM_ACTIONS = 128
SUPPORTED_INSTRUCTION_VARIANTS = frozenset({"original_task", "cached_singular_task"})


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
    process_seed: int = DEFAULT_PROCESS_SEED,
    maximum_actions: int = DEFAULT_MAXIMUM_ACTIONS,
    instruction_variant: str = "original_task",
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("cosmos_policy_libero_curriculum_probe_opt_in_required")
    if output_dir.exists():
        raise ValueError("cosmos_policy_libero_curriculum_probe_output_exists")
    if isinstance(maximum_actions, bool) or not 1 <= maximum_actions <= 128:
        raise ValueError("cosmos_policy_libero_curriculum_probe_action_budget_invalid")
    if instruction_variant not in SUPPORTED_INSTRUCTION_VARIANTS:
        raise ValueError("cosmos_policy_libero_curriculum_probe_instruction_variant_invalid")
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
        process_seed=process_seed,
    )
    repair_report = _run_repair(
        cfg=cfg,
        model=model,
        dataset_stats=dataset_stats,
        checkpoint_evidence=checkpoint_evidence,
        source_root=source_root,
        snapshot_path=snapshot_path,
        artifact_root=output_dir / "trial",
        dispatch_state_path=output_dir / "trial" / "dispatch.json",
        operator_approval_ref=operator_approval_ref,
        maximum_actions=maximum_actions,
        process_seed=process_seed,
        repair_instruction_variant=instruction_variant,
    )
    report_without_digest = {
        "schema_version": "missionos.cosmos_policy_libero_curriculum_probe.v1",
        "status": "bounded_curriculum_probe_completed",
        "process_seed": process_seed,
        "maximum_applied_actions": maximum_actions,
        "instruction_variant": instruction_variant,
        "instruction_embedding_source": "verified_official_checkpoint_cache",
        "additional_training_performed": False,
        "model_loaded_once": True,
        "oracle_admission": oracle_admission,
        "nominal_admission": nominal_admission,
        "repair_report_relative_path": "trial/repair/report.json",
        "repair_report_sha256": repair_report["result_sha256"],
        "applied_action_count": repair_report["repair_result"]["applied_action_count"],
        "final_goal_predicate_vector": repair_report["final_goal_predicate_vector"],
        "fixture_repair_established": repair_report["scripted_fixture_repair_established"],
        "actual_predicate_recovery_observed": repair_report.get(
            "actual_predicate_recovery_observed",
            repair_report["scripted_fixture_repair_established"],
        ),
        "repair_claim_eligible": repair_report.get("repair_claim_eligible", True),
        "action_command_statistics": repair_report["action_command_statistics"],
        "actual_effect_statistics": _actual_effect_statistics(repair_report),
        "claim_boundary": {
            "authority": "diagnostic_only",
            "candidate_selector_used": False,
            "oracle_selector_used": False,
            "general_recovery_rate_established": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        },
    }
    report = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    _write_json(output_dir / "curriculum-probe.json", report)
    return report


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
    parser.add_argument("--process-seed", type=int, default=DEFAULT_PROCESS_SEED)
    parser.add_argument("--maximum-actions", type=int, default=DEFAULT_MAXIMUM_ACTIONS)
    parser.add_argument(
        "--instruction-variant",
        choices=sorted(SUPPORTED_INSTRUCTION_VARIANTS),
        default="original_task",
    )
    args = parser.parse_args()
    result = execute_live(
        source_root=args.source_root.resolve(),
        checkpoint_path=args.checkpoint_path.resolve(),
        tokenizer_path=args.tokenizer_path.resolve(),
        snapshot_path=args.restore_snapshot.resolve(),
        oracle_recoverability_report_path=args.oracle_recoverability_report.resolve(),
        nominal_report_path=args.nominal_report.resolve(),
        output_dir=args.output_dir.resolve(),
        operator_approval_ref=args.operator_approval_ref,
        process_seed=args.process_seed,
        maximum_actions=args.maximum_actions,
        instruction_variant=args.instruction_variant,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

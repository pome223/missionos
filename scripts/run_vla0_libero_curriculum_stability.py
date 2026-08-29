#!/usr/bin/env python3
"""Measure post-conjunction stability for the admitted VLA-0 3 cm probe."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from missionos_core import canonical_sha256
from scripts.run_vla0_libero_curriculum_probe import (
    MAXIMUM_ACTIONS,
    execute_live as execute_curriculum_probe,
)


OPT_IN_ENV = "RUN_MISSIONOS_VLA0_LIBERO_CURRICULUM_STABILITY"
def execute_live(
    *,
    source_root: Path,
    checkpoint_source_path: Path,
    runtime_checkpoint_path: Path,
    snapshot_path: Path,
    oracle_report_path: Path,
    output_dir: Path,
    operator_approval_ref: str,
    maximum_actions: int = MAXIMUM_ACTIONS,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("vla0_curriculum_stability_opt_in_required")
    probe = execute_curriculum_probe(
        source_root=source_root,
        checkpoint_source_path=checkpoint_source_path,
        runtime_checkpoint_path=runtime_checkpoint_path,
        snapshot_path=snapshot_path,
        oracle_report_path=oracle_report_path,
        output_dir=output_dir,
        operator_approval_ref=operator_approval_ref,
        maximum_actions=maximum_actions,
    )

    base_report = json.loads(
        (output_dir / "base-report.json").read_text(encoding="utf-8")
    )
    stability = base_report["repair_result"]["post_conjunction_stability"]
    report_without_digest = {
        "schema_version": "missionos.vla0_libero_curriculum_stability.v2",
        "status": (
            "bounded_stable_predicate_recovery_observed"
            if stability is not None and stability["stable"]
            else "bounded_stable_predicate_recovery_not_observed"
        ),
        "snapshot_sha256": probe["snapshot_sha256"],
        "curriculum_probe_result_sha256": probe["result_sha256"],
        "base_report_sha256": base_report["result_sha256"],
        "source_goal_predicate_vector": probe["source_goal_predicate_vector"],
        "first_conjunction_observed": base_report["repair_result"][
            "predicate_conjunction_observed"
        ],
        "policy_action_count": base_report["repair_result"]["policy_action_count"],
        "total_simulator_action_count": base_report["repair_result"][
            "total_simulator_action_count"
        ],
        "post_success_zero_motion_stability": stability,
        "stable_predicate_recovery_observed": bool(stability and stability["stable"]),
        "additional_training_performed": False,
        "claim_boundary": {
            "authority": "diagnostic_only",
            "state_continuity_basis": "diagnostic_mujoco_state_clone",
            "stable_outcome_under_zero_motion_hold_measured": stability is not None,
            "stability_hold_inside_repair_dispatch_receipt": stability is not None,
            "post_success_policy_behavior_measured": False,
            "same_world_semantic_repair_established": False,
            "general_vla0_recovery_rate_established": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        },
    }
    report = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    (output_dir / "stability-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--checkpoint-source-path", type=Path, required=True)
    parser.add_argument("--runtime-checkpoint-path", type=Path, required=True)
    parser.add_argument("--restore-snapshot", type=Path, required=True)
    parser.add_argument("--oracle-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--operator-approval-ref", required=True)
    parser.add_argument("--maximum-actions", type=int, default=MAXIMUM_ACTIONS)
    args = parser.parse_args()
    result = execute_live(
        source_root=args.source_root.resolve(),
        checkpoint_source_path=args.checkpoint_source_path.resolve(),
        runtime_checkpoint_path=args.runtime_checkpoint_path.resolve(),
        snapshot_path=args.restore_snapshot.resolve(),
        oracle_report_path=args.oracle_report.resolve(),
        output_dir=args.output_dir.resolve(),
        operator_approval_ref=args.operator_approval_ref,
        maximum_actions=args.maximum_actions,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["stable_predicate_recovery_observed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

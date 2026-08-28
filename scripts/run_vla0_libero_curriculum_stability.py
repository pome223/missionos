#!/usr/bin/env python3
"""Measure post-conjunction stability for the admitted VLA-0 3 cm probe."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from missionos_core import canonical_sha256
from scripts import run_vla0_libero_snapshot_recovery as base_runner
from scripts.run_vla0_libero_curriculum_probe import (
    MAXIMUM_ACTIONS,
    TARGET_OBJECT,
    execute_live as execute_curriculum_probe,
)


OPT_IN_ENV = "RUN_MISSIONOS_VLA0_LIBERO_CURRICULUM_STABILITY"
STABLE_SUCCESS_STEPS = 20
PROTECTED_OBJECT = "moka_pot_1"
SETTLE_ACTION_7D = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0]


def _result_with_digest(material: Mapping[str, Any]) -> dict[str, Any]:
    without_digest = {
        key: deepcopy(value) for key, value in material.items() if key != "result_sha256"
    }
    return {
        **without_digest,
        "result_sha256": canonical_sha256(without_digest),
    }


def _run_with_post_success_settle(
    *,
    standard_runner: Callable[..., dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any]:
    """Run the standard verifier, then mirror the oracle's 20-step settle."""

    result = standard_runner(**kwargs)
    contract = kwargs["proposal"]["repair_contract"]
    maximum_steps = int(contract["maximum_repair_steps"])
    policy_actions = int(result["applied_action_count"])
    admitted = bool(
        result["predicate_conjunction_observed"] is True
        and result["first_preservation_invariant_breach"] is None
        and policy_actions + STABLE_SUCCESS_STEPS <= maximum_steps
    )
    reference_position = contract["preservation_invariant"][
        "reference_position_metres"
    ][PROTECTED_OBJECT]
    maximum_protected_displacement = float(
        contract["preservation_invariant"]["maximum_displacement_metres"]
    )

    trace: list[dict[str, Any]] = []
    stable_steps = 0
    terminal_predicates = deepcopy(result["final_goal_predicate_observations"])
    preservation_violation = False
    if admitted:
        import numpy as np

        reference = np.asarray(reference_position, dtype=np.float64)
        for settle_index in range(STABLE_SUCCESS_STEPS):
            global_action_index = policy_actions + settle_index
            _, evidence = kwargs["apply_action_chunk"](
                np.asarray(SETTLE_ACTION_7D, dtype=np.float32),
                global_action_index,
            )
            step = deepcopy(evidence["preservation_step_trace"][0])
            terminal_predicates = deepcopy(step["goal_predicate_observations"])
            protected_position = np.asarray(
                step["object_witnesses"][PROTECTED_OBJECT]["position_metres"],
                dtype=np.float64,
            )
            protected_displacement = float(
                np.linalg.norm(protected_position - reference)
            )
            vector = [item["satisfied"] for item in terminal_predicates]
            step_record = {
                "settle_step_number": settle_index + 1,
                "global_action_step_number": global_action_index + 1,
                "action_7d": list(SETTLE_ACTION_7D),
                "action_step_sha256": step["action_step_sha256"],
                "predicate_vector": vector,
                "predicate_vector_sha256": step["goal_predicate_vector_sha256"],
                "target_position_metres": step["object_witnesses"][TARGET_OBJECT][
                    "position_metres"
                ],
                "protected_position_metres": protected_position.tolist(),
                "protected_displacement_metres": protected_displacement,
                "frame_capture": step["frame_capture"],
            }
            trace.append(step_record)
            if (
                vector != [True, True, True]
                or protected_displacement > maximum_protected_displacement
            ):
                preservation_violation = bool(
                    protected_displacement > maximum_protected_displacement
                    or vector[0] is not True
                    or vector[2] is not True
                )
                break
            stable_steps += 1

    stable = bool(
        admitted
        and stable_steps == STABLE_SUCCESS_STEPS
        and [item["satisfied"] for item in terminal_predicates] == [True, True, True]
        and not preservation_violation
    )
    result["final_goal_predicate_observations"] = terminal_predicates
    result["post_success_zero_motion_stability"] = {
        "schema_version": "missionos.vla0_post_success_zero_motion_stability.v1",
        "authority": "diagnostic_only",
        "admitted": admitted,
        "policy_inference_invoked_during_settle": False,
        "settle_action_7d": list(SETTLE_ACTION_7D),
        "stable_success_steps_required": STABLE_SUCCESS_STEPS,
        "stable_success_steps_completed": stable_steps,
        "stable_success_observed": stable,
        "policy_actions_before_settle": policy_actions,
        "total_simulator_actions_after_settle": policy_actions + len(trace),
        "terminal_goal_predicate_vector": [
            item["satisfied"] for item in terminal_predicates
        ],
        "preservation_violation_observed": preservation_violation,
        "protected_maximum_displacement_metres": max(
            (item["protected_displacement_metres"] for item in trace), default=0.0
        ),
        "trace": trace,
        "claim_boundary": {
            "post_success_steps_execute_outside_the_standard_repair_loop": True,
            "may_establish_same_world_semantic_repair": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        },
    }
    return _result_with_digest(result)


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
    original_runner = base_runner.run_vla0_same_world_repair

    def patched_runner(**kwargs: Any) -> dict[str, Any]:
        return _run_with_post_success_settle(
            standard_runner=original_runner,
            **kwargs,
        )

    base_runner.run_vla0_same_world_repair = patched_runner
    try:
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
    finally:
        base_runner.run_vla0_same_world_repair = original_runner

    base_report = json.loads(
        (output_dir / "base-report.json").read_text(encoding="utf-8")
    )
    stability = base_report["repair_result"]["post_success_zero_motion_stability"]
    report_without_digest = {
        "schema_version": "missionos.vla0_libero_curriculum_stability.v1",
        "status": (
            "bounded_stable_predicate_recovery_observed"
            if stability["stable_success_observed"]
            else "bounded_stable_predicate_recovery_not_observed"
        ),
        "snapshot_sha256": probe["snapshot_sha256"],
        "curriculum_probe_result_sha256": probe["result_sha256"],
        "base_report_sha256": base_report["result_sha256"],
        "source_goal_predicate_vector": probe["source_goal_predicate_vector"],
        "first_conjunction_observed": probe["actual_predicate_recovery_observed"],
        "policy_action_count": stability["policy_actions_before_settle"],
        "total_simulator_action_count": stability[
            "total_simulator_actions_after_settle"
        ],
        "post_success_zero_motion_stability": stability,
        "stable_predicate_recovery_observed": stability[
            "stable_success_observed"
        ],
        "additional_training_performed": False,
        "claim_boundary": {
            "authority": "diagnostic_only",
            "state_continuity_basis": "diagnostic_mujoco_state_clone",
            "stable_outcome_under_zero_motion_hold_measured": True,
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

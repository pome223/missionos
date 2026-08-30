#!/usr/bin/env python3
"""Run one bounded VLA-0 diagnostic on an admitted LIBERO curriculum fixture."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from missionos_core import canonical_sha256
from scripts.run_vla0_libero_snapshot_recovery import (
    ENVIRONMENT_SEED,
    EXPECTED_SOURCE_VECTOR,
    execute_live as execute_base_live,
)


OPT_IN_ENV = "RUN_MISSIONOS_VLA0_LIBERO_CURRICULUM_PROBE"
CURRICULUM_FIXTURE_BASIS = "diagnostic_displacement_curriculum"
CURRICULUM_FIXTURE_SCHEMA_VERSION = "missionos.libero_displacement_curriculum_fixture.v2"
CURRICULUM_FIXTURE_CONSTRUCTION = "protected_separating_horizontal_ray_from_success_state"
MAXIMUM_CURRICULUM_TRANSLATION_METRES = 0.05
TARGET_OBJECT = "moka_pot_2"
MAXIMUM_ACTIONS = 128
EXACT_SEED0_THREE_CENTIMETRE_SNAPSHOT_SHA256 = (
    "8064d6faeeb02a67a08649be0ca39529b4a79da459cf8d11493c0412bbc7b651"
)
EXACT_REQUESTED_TRANSLATION_METRES = 0.03


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_snapshot_metadata(path: Path) -> dict[str, Any]:
    import numpy as np

    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != {"simulator_state", "metadata_json"}:
            raise RuntimeError("vla0_curriculum_probe_snapshot_members_invalid")
        state = np.asarray(archive["simulator_state"], dtype=np.float64).reshape(-1)
        metadata = json.loads(str(archive["metadata_json"].item()))
    if metadata.get("simulator_state_sha256") != hashlib.sha256(state.tobytes()).hexdigest():
        raise RuntimeError("vla0_curriculum_probe_snapshot_state_digest_mismatch")
    return metadata


def _validate_curriculum_fixture_snapshot(metadata: Mapping[str, Any]) -> dict[str, Any]:
    if metadata.get("source_failure_basis") != CURRICULUM_FIXTURE_BASIS:
        raise RuntimeError("vla0_curriculum_fixture_snapshot_basis_mismatch")
    fixture = metadata.get("displacement_curriculum_fixture")
    if not isinstance(fixture, Mapping):
        raise RuntimeError("vla0_curriculum_fixture_material_missing")
    material = deepcopy(dict(fixture))
    if metadata.get("displacement_curriculum_fixture_sha256") != canonical_sha256(material):
        raise RuntimeError("vla0_curriculum_fixture_digest_mismatch")
    if (
        material.get("schema_version") != CURRICULUM_FIXTURE_SCHEMA_VERSION
        or material.get("authority") != "diagnostic_fixture_only"
        or material.get("construction") != CURRICULUM_FIXTURE_CONSTRUCTION
        or material.get("environment_seed") != ENVIRONMENT_SEED
        or metadata.get("environment_seed") != ENVIRONMENT_SEED
    ):
        raise RuntimeError("vla0_curriculum_fixture_contract_mismatch")
    requested = material.get("requested_translation_from_source_metres")
    observed = material.get("observed_translation_from_source_metres")
    protected = material.get("protected_object_displacement_metres")
    if (
        isinstance(requested, bool)
        or not isinstance(requested, (int, float))
        or not 0.0 < float(requested) <= MAXIMUM_CURRICULUM_TRANSLATION_METRES
        or isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or abs(float(observed) - float(requested)) > 0.01
        or isinstance(protected, bool)
        or not isinstance(protected, (int, float))
        or not 0.0 <= float(protected) <= 0.005
    ):
        raise RuntimeError("vla0_curriculum_fixture_geometry_invalid")
    trace = material.get("fixture_settle_trace")
    if (
        not isinstance(trace, list)
        or material.get("fixture_settle_steps_applied") != len(trace)
        or len(trace) < 60
        or any(
            not isinstance(item, Mapping)
            or item.get("predicate_vector") != EXPECTED_SOURCE_VECTOR
            for item in trace
        )
    ):
        raise RuntimeError("vla0_curriculum_fixture_stability_invalid")
    if (
        material.get("terminal_goal_predicate_vector") != EXPECTED_SOURCE_VECTOR
        or material.get("actual_predicate_failure_observed") is not True
        or material.get("model_inference_invoked") is not False
        or material.get("repair_attempted") is not False
        or material.get("physical_execution_invoked") is not False
        or metadata.get("source_goal_predicate_vector") != EXPECTED_SOURCE_VECTOR
        or metadata.get("source_failure_is_repair_candidate") is not True
    ):
        raise RuntimeError("vla0_curriculum_fixture_claim_boundary_invalid")
    return material


def _verify_oracle(*, path: Path, snapshot_sha256: str) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    supplied = report.get("result_sha256")
    material = {key: value for key, value in report.items() if key != "result_sha256"}
    if supplied != canonical_sha256(material):
        raise RuntimeError("vla0_curriculum_probe_oracle_digest_mismatch")
    if (
        report.get("schema_version") != "missionos.vla0_same_interface_oracle_recoverability.v2"
        or report.get("snapshot_sha256") != snapshot_sha256
        or report.get("environment_seed") != ENVIRONMENT_SEED
        or report.get("source_goal_predicate_vector") != EXPECTED_SOURCE_VECTOR
        or report.get("terminal_goal_predicate_vector") != [True, True, True]
        or report.get("stable_success_observed") is not True
        or report.get("preservation_violation_observed") is not False
        or report.get("trajectory_events", {}).get("first_contact_after_action") is None
        or report.get("success_first_observed_after_action") is None
    ):
        raise RuntimeError("vla0_curriculum_probe_oracle_contract_invalid")
    return {
        "authority": "diagnostic_only",
        "report_sha256": supplied,
        "snapshot_sha256": snapshot_sha256,
        "initial_eef_target_distance_metres": report["trajectory_events"][
            "initial_eef_target_distance_metres"
        ],
        "first_contact_after_action": report["trajectory_events"][
            "first_contact_after_action"
        ],
        "first_success_after_action": report["success_first_observed_after_action"],
        "actions_applied": report["actions_applied"],
        "initial_target_position_metres": report["initial_target_position_metres"],
        "may_establish_vla0_repair_success": False,
    }


def _validate_probe_identity(
    *, snapshot_sha256: str, fixture: Mapping[str, Any]
) -> None:
    if snapshot_sha256 != EXACT_SEED0_THREE_CENTIMETRE_SNAPSHOT_SHA256:
        raise RuntimeError("vla0_curriculum_probe_snapshot_identity_mismatch")
    requested = float(fixture["requested_translation_from_source_metres"])
    if requested != EXACT_REQUESTED_TRANSLATION_METRES:
        raise RuntimeError("vla0_curriculum_probe_translation_identity_mismatch")


def _actual_effect_statistics(
    *,
    initial_eef_target_distance_metres: float,
    initial_target_position_metres: list[float],
    repair_result: Mapping[str, Any],
    raw_action_trace: list[Mapping[str, Any]],
) -> dict[str, Any]:
    import numpy as np

    step_trace = [
        step
        for chunk in repair_result.get("chunk_evidence", [])
        for step in chunk.get("preservation_step_trace", [])
    ]
    witnesses = [step["object_witnesses"][TARGET_OBJECT] for step in step_trace]
    distances = [float(item["end_effector_distance_metres"]) for item in witnesses]
    initial_target = np.asarray(
        initial_target_position_metres, dtype=np.float64
    )
    translations = [
        float(
            np.linalg.norm(
                np.asarray(item["position_metres"], dtype=np.float64) - initial_target
            )
        )
        for item in witnesses
    ]
    contact_steps = [
        index
        for index, item in enumerate(witnesses, start=1)
        if item["gripper_contact_observed"] is True
    ]
    distance_changes = [after - before for before, after in zip(distances, distances[1:])]
    gripper = [float(item["action_7d"][-1]) for item in raw_action_trace]
    signs = [1 if value > 0.0 else -1 if value < 0.0 else 0 for value in gripper]
    initial_distance = float(initial_eef_target_distance_metres)
    minimum = min(distances) if distances else initial_distance
    return {
        "schema_version": "missionos.vla0_libero_actual_effect_statistics.v1",
        "authority": "actual_libero_simulator_observation",
        "initial_distance_authority": "same_snapshot_scripted_oracle_pre_action_witness",
        "sample_count": len(step_trace),
        "initial_end_effector_distance_to_target_metres": initial_distance,
        "first_post_action_end_effector_distance_to_target_metres": (
            distances[0] if distances else None
        ),
        "minimum_end_effector_distance_to_target_metres": minimum,
        "minimum_distance_after_action": distances.index(minimum) + 1 if distances else None,
        "final_end_effector_distance_to_target_metres": distances[-1] if distances else None,
        "maximum_end_effector_distance_to_target_metres": max(distances) if distances else None,
        "closer_step_transition_count": sum(change < 0.0 for change in distance_changes),
        "farther_step_transition_count": sum(change > 0.0 for change in distance_changes),
        "gripper_contact_observation_count": len(contact_steps),
        "first_gripper_contact_after_action": contact_steps[0] if contact_steps else None,
        "maximum_target_translation_metres": max(translations, default=0.0),
        "final_target_translation_metres": translations[-1] if translations else 0.0,
        "gripper_command": {
            "minimum": min(gripper) if gripper else None,
            "maximum": max(gripper) if gripper else None,
            "sign_transition_count": sum(
                before != after for before, after in zip(signs, signs[1:])
            ),
        },
    }


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
        raise RuntimeError("vla0_curriculum_probe_opt_in_required")
    if maximum_actions != MAXIMUM_ACTIONS:
        raise ValueError("vla0_curriculum_probe_requires_128_actions")
    if output_dir.exists():
        raise ValueError("vla0_curriculum_probe_output_exists")
    snapshot_sha256 = _sha256_path(snapshot_path)
    fixture = _validate_curriculum_fixture_snapshot(_read_snapshot_metadata(snapshot_path))
    _validate_probe_identity(snapshot_sha256=snapshot_sha256, fixture=fixture)
    oracle = _verify_oracle(path=oracle_report_path, snapshot_sha256=snapshot_sha256)

    base_report = execute_base_live(
        source_root=source_root,
        checkpoint_source_path=checkpoint_source_path,
        runtime_checkpoint_path=runtime_checkpoint_path,
        snapshot_path=snapshot_path,
        output_path=output_dir / "base-report.json",
        dispatch_state_path=output_dir / "dispatch.json",
        operator_approval_ref=operator_approval_ref,
        maximum_repair_steps=maximum_actions,
        episode_init_state_index=15,
        scripted_failure_fixture=None,
        frame_capture_dir=output_dir / "frames",
    )
    effects = _actual_effect_statistics(
        initial_eef_target_distance_metres=oracle[
            "initial_eef_target_distance_metres"
        ],
        initial_target_position_metres=oracle["initial_target_position_metres"],
        repair_result=base_report["repair_result"],
        raw_action_trace=base_report["raw_action_trace"],
    )
    predicate_entry = bool(base_report["repair_result"]["predicate_conjunction_observed"])
    recovery = bool(base_report["repair_result"]["stable_completion_observed"])
    report_without_digest = {
        "schema_version": "missionos.vla0_libero_curriculum_probe.v2",
        "status": (
            "bounded_curriculum_fixture_recovery_observed"
            if recovery
            else "bounded_curriculum_fixture_recovery_not_observed"
        ),
        "maximum_applied_actions": maximum_actions,
        "applied_action_count": base_report["repair_result"]["applied_action_count"],
        "snapshot_sha256": snapshot_sha256,
        "fixture_sha256": canonical_sha256(fixture),
        "fixture_requested_translation_metres": fixture[
            "requested_translation_from_source_metres"
        ],
        "oracle_admission": oracle,
        "base_report_relative_path": "base-report.json",
        "base_report_sha256": base_report["result_sha256"],
        "source_goal_predicate_vector": base_report["source_goal_predicate_vector"],
        "final_goal_predicate_vector": base_report["final_goal_predicate_vector"],
        "repair_intent_selection": base_report["repair_intent_selection"],
        "predicate_conjunction_observed": predicate_entry,
        "actual_predicate_recovery_observed": recovery,
        "stable_predicate_recovery_observed": recovery,
        "actual_effect_statistics": effects,
        "additional_training_performed": False,
        "stopping_rule": {
            "additional_trial_requires_contact_or_target_translation_at_least_metres": 0.001,
            "contact_observed": effects["gripper_contact_observation_count"] > 0,
            "significant_target_translation_observed": (
                effects["maximum_target_translation_metres"] >= 0.001
            ),
            "additional_trial_admitted": bool(
                effects["gripper_contact_observation_count"] > 0
                or effects["maximum_target_translation_metres"] >= 0.001
            ),
        },
        "claim_boundary": {
            "authority": "diagnostic_only",
            "same_seed0_snapshot_as_cosmos_probe": True,
            "base_runner_treats_restore_as_diagnostic_mujoco_clone": True,
            "curriculum_admission_checked_before_model_load": True,
            "learned_policy_recovery_on_diagnostic_clone_observed": recovery,
            "same_world_semantic_repair_established": False,
            "learned_policy_repair_established": False,
            "general_vla0_recovery_rate_established": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        },
    }
    report = {
        **report_without_digest,
        "result_sha256": canonical_sha256(report_without_digest),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "curriculum-probe.json").write_text(
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
    return 0 if result["actual_predicate_recovery_observed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

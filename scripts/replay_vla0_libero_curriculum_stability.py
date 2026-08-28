#!/usr/bin/env python3
"""Replay a recorded VLA-0 trace and measure 20-step predicate stability."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from missionos_core import canonical_sha256
from scripts.run_libero_curriculum_oracle import (
    EPISODE_INIT_STATE_INDEX,
    PROTECTED_MAXIMUM_DISPLACEMENT_METRES,
    PROTECTED_OBJECT,
    RESTORE_MAXIMUM_ABSOLUTE_ERROR,
    STABLE_SUCCESS_STEPS,
    TARGET_OBJECT,
    _make_environment,
    _predicate_material,
)
from scripts.run_vla0_libero_curriculum_probe import (
    EXACT_SEED0_THREE_CENTIMETRE_SNAPSHOT_SHA256,
    _read_snapshot_metadata,
    _validate_curriculum_fixture_snapshot,
    _validate_probe_identity,
)


OPT_IN_ENV = "RUN_MISSIONOS_VLA0_LIBERO_STABILITY_REPLAY"
SETTLE_ACTION_7D = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0]
EXPECTED_SOURCE_VECTOR = [True, False, True]
EXPECTED_SUCCESS_VECTOR = [True, True, True]


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_verified_trace(report_path: Path) -> tuple[list[list[float]], dict[str, Any]]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    supplied = report.get("result_sha256")
    material = {key: value for key, value in report.items() if key != "result_sha256"}
    if supplied != canonical_sha256(material):
        raise RuntimeError("vla0_stability_replay_source_report_digest_mismatch")
    source_contract = report.get("source_contract")
    if (
        not isinstance(source_contract, dict)
        or source_contract.get("setup_snapshot_sha256")
        != EXACT_SEED0_THREE_CENTIMETRE_SNAPSHOT_SHA256
        or report.get("source_goal_predicate_vector") != EXPECTED_SOURCE_VECTOR
        or report.get("final_goal_predicate_vector") != EXPECTED_SUCCESS_VECTOR
        or report.get("diagnostic_clone_recovery_observed") is not True
    ):
        raise RuntimeError("vla0_stability_replay_source_report_not_admitted")
    raw = report.get("raw_action_trace")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("vla0_stability_replay_action_trace_missing")
    actions: list[list[float]] = []
    for expected_index, item in enumerate(raw):
        action = item.get("action_7d") if isinstance(item, dict) else None
        if (
            not isinstance(action, list)
            or len(action) != 7
            or item.get("global_repair_step_index") != expected_index
        ):
            raise RuntimeError("vla0_stability_replay_action_trace_invalid")
        actions.append([float(value) for value in action])
    return actions, {"result_sha256": supplied, "action_count": len(actions)}


def execute_live(
    *, snapshot_path: Path, source_report_path: Path, output_path: Path
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("vla0_stability_replay_opt_in_required")
    if output_path.exists():
        raise ValueError("vla0_stability_replay_output_exists")
    snapshot_sha256 = _sha256_path(snapshot_path)
    fixture = _validate_curriculum_fixture_snapshot(
        _read_snapshot_metadata(snapshot_path)
    )
    _validate_probe_identity(snapshot_sha256=snapshot_sha256, fixture=fixture)
    actions, source = _read_verified_trace(source_report_path)
    if len(actions) + STABLE_SUCCESS_STEPS > 128:
        raise RuntimeError("vla0_stability_replay_action_budget_exceeded")

    import numpy as np

    with np.load(snapshot_path, allow_pickle=False) as archive:
        snapshot = np.asarray(archive["simulator_state"], dtype=np.float64).reshape(-1)
    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    environment, init_states, instruction = _make_environment()
    trace: list[dict[str, Any]] = []
    first_success: int | None = None
    stable_steps = 0
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        observation = environment.regenerate_obs_from_state(snapshot)
        restored = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        maximum_restore_error = float(np.max(np.abs(restored - snapshot)))
        if maximum_restore_error > RESTORE_MAXIMUM_ABSOLUTE_ERROR:
            raise RuntimeError("vla0_stability_replay_restore_error")
        if [item["satisfied"] for item in _predicate_material(environment)] != EXPECTED_SOURCE_VECTOR:
            raise RuntimeError("vla0_stability_replay_source_vector_mismatch")

        simulator = environment.env
        target_body = int(simulator.obj_body_id[TARGET_OBJECT])
        protected_body = int(simulator.obj_body_id[PROTECTED_OBJECT])
        initial_protected = np.asarray(
            simulator.sim.data.body_xpos[protected_body], dtype=np.float64
        ).copy()

        def apply(action: list[float], phase: str) -> list[bool]:
            nonlocal observation, first_success
            observation, _, _, _ = environment.step(action)
            predicates = [
                item["satisfied"] for item in _predicate_material(environment)
            ]
            target = np.asarray(
                simulator.sim.data.body_xpos[target_body], dtype=np.float64
            )
            protected = np.asarray(
                simulator.sim.data.body_xpos[protected_body], dtype=np.float64
            )
            index = len(trace) + 1
            if predicates == EXPECTED_SUCCESS_VECTOR and first_success is None:
                first_success = index
            trace.append(
                {
                    "action_index": index,
                    "phase": phase,
                    "action_7d": action,
                    "predicate_vector": predicates,
                    "target_position_metres": target.tolist(),
                    "protected_displacement_metres": float(
                        np.linalg.norm(protected - initial_protected)
                    ),
                }
            )
            return predicates

        for action in actions:
            apply(action, "recorded_vla0_action_replay")
        replay_terminal = trace[-1]["predicate_vector"]
        replay_recovered = replay_terminal == EXPECTED_SUCCESS_VECTOR
        if replay_recovered:
            for _ in range(STABLE_SUCCESS_STEPS):
                predicates = apply(SETTLE_ACTION_7D, "stationary_gripper_hold")
                if (
                    predicates != EXPECTED_SUCCESS_VECTOR
                    or trace[-1]["protected_displacement_metres"]
                    > PROTECTED_MAXIMUM_DISPLACEMENT_METRES
                ):
                    break
                stable_steps += 1
    finally:
        environment.close()

    stable = bool(replay_recovered and stable_steps == STABLE_SUCCESS_STEPS)
    without_digest = {
        "schema_version": "missionos.vla0_libero_stability_replay.v1",
        "status": (
            "recorded_vla0_outcome_stable_under_hold"
            if stable
            else "recorded_vla0_outcome_stability_not_established"
        ),
        "authority": "diagnostic_fixed_action_replay",
        "snapshot_sha256": snapshot_sha256,
        "instruction": instruction,
        "source_report": source,
        "recorded_policy_action_count": len(actions),
        "recorded_trace_recovered_predicate": replay_recovered,
        "first_success_after_action": first_success,
        "stable_success_steps_required": STABLE_SUCCESS_STEPS,
        "stable_success_steps_completed": stable_steps,
        "stable_success_observed": stable,
        "terminal_goal_predicate_vector": trace[-1]["predicate_vector"],
        "maximum_protected_displacement_metres": max(
            item["protected_displacement_metres"] for item in trace
        ),
        "restore_maximum_absolute_error": maximum_restore_error,
        "trace": trace,
        "claim_boundary": {
            "new_policy_inference_invoked": False,
            "recorded_actions_originally_generated_by_vla0": True,
            "same_world_semantic_repair_established": False,
            "general_vla0_recovery_rate_established": False,
            "controller_ack_observed": False,
            "physical_execution_invoked": False,
        },
    }
    result = {**without_digest, "result_sha256": canonical_sha256(without_digest)}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = execute_live(
        snapshot_path=args.snapshot.resolve(),
        source_report_path=args.source_report.resolve(),
        output_path=args.output.resolve(),
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["stable_success_observed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

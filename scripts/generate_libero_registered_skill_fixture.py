#!/usr/bin/env python3
"""Generate a fresh 3 cm LIBERO fixture for registered-skill Repair."""

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
from scripts.probe_libero_displacement_curriculum import (  # noqa: E402
    ENVIRONMENT_SEED,
    EPISODE_INIT_STATE_INDEX,
    FIXTURE_SCHEMA_VERSION,
    PROTECTED_OBJECT,
    STOVE_REGION,
    TARGET_OBJECT,
    TASK_ID,
    TASK_SUITE,
    _make_environment,
    _predicate_material,
    _write_failure_snapshot,
)


OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_REGISTERED_SKILL_FIXTURE"
DISPLACEMENT_METRES = 0.03
SETTLE_STEPS = 60
SUCCESS_LAYOUT_OFFSETS_METRES = {
    PROTECTED_OBJECT: (0.035, 0.035),
    TARGET_OBJECT: (-0.046, -0.035),
}
OBJECT_CENTER_ABOVE_REGION_METRES = 0.065


def _set_object_position(environment: Any, name: str, position: Any) -> None:
    import numpy as np

    simulator = environment.env
    joint = simulator.get_object(name).joints[0]
    qpos = np.asarray(simulator.sim.data.get_joint_qpos(joint), dtype=np.float64).copy()
    qpos[:3] = np.asarray(position, dtype=np.float64)
    simulator.sim.data.set_joint_qpos(joint, qpos)
    try:
        simulator.sim.data.set_joint_qvel(joint, np.zeros(6, dtype=np.float64))
    except (AttributeError, ValueError):
        pass


def _settle(environment: Any, *, expected: list[bool]) -> list[dict[str, Any]]:
    import numpy as np

    trace = []
    for index in range(SETTLE_STEPS):
        _, _, done, info = environment.step(np.zeros(7, dtype=np.float64))
        vector = [item["satisfied"] for item in _predicate_material(environment)]
        trace.append(
            {
                "step_index": index,
                "predicate_vector": vector,
                "environment_done": bool(done),
                "environment_info_success": bool(info.get("success", False)),
            }
        )
    if any(item["predicate_vector"] != expected for item in trace):
        raise RuntimeError(
            "registered_skill_fixture_predicate_not_stable:"
            f"expected={expected}:terminal={trace[-1]['predicate_vector']}"
        )
    return trace


def generate(*, output_dir: Path) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("registered_skill_fixture_opt_in_required")
    if output_dir.exists():
        raise ValueError("registered_skill_fixture_output_exists")

    import numpy as np

    os.environ.setdefault("MUJOCO_GL", "osmesa")
    os.environ.setdefault("PYOPENGL_PLATFORM", "osmesa")
    output_dir.mkdir(parents=True)
    environment, init_states = _make_environment()
    try:
        environment.reset()
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        simulator = environment.env
        region_position = np.asarray(
            simulator.sim.data.get_site_xpos(STOVE_REGION), dtype=np.float64
        )
        source_positions: dict[str, Any] = {}
        for name, (x_offset, y_offset) in SUCCESS_LAYOUT_OFFSETS_METRES.items():
            position = region_position + np.array(
                [x_offset, y_offset, OBJECT_CENTER_ABOVE_REGION_METRES],
                dtype=np.float64,
            )
            _set_object_position(environment, name, position)
            source_positions[name] = position
        simulator.sim.forward()
        simulator._post_process()
        simulator._update_observables(force=True)
        success_trace = _settle(environment, expected=[True, True, True])
        source_target = np.asarray(
            simulator.sim.data.body_xpos[simulator.obj_body_id[TARGET_OBJECT]],
            dtype=np.float64,
        ).copy()
        source_protected = np.asarray(
            simulator.sim.data.body_xpos[simulator.obj_body_id[PROTECTED_OBJECT]],
            dtype=np.float64,
        ).copy()

        injected_target = source_target + np.array(
            [-DISPLACEMENT_METRES, 0.0, 0.0], dtype=np.float64
        )
        _set_object_position(environment, TARGET_OBJECT, injected_target)
        simulator.sim.forward()
        simulator._post_process()
        simulator._update_observables(force=True)
        failure_trace = _settle(environment, expected=[True, False, True])

        terminal_target = np.asarray(
            simulator.sim.data.body_xpos[simulator.obj_body_id[TARGET_OBJECT]],
            dtype=np.float64,
        ).copy()
        terminal_protected = np.asarray(
            simulator.sim.data.body_xpos[simulator.obj_body_id[PROTECTED_OBJECT]],
            dtype=np.float64,
        ).copy()
        observed_displacement = float(np.linalg.norm(terminal_target - source_target))
        protected_displacement = float(
            np.linalg.norm(terminal_protected - source_protected)
        )
        if abs(observed_displacement - DISPLACEMENT_METRES) > 0.002:
            raise RuntimeError(
                "registered_skill_fixture_translation_drift:"
                f"observed={observed_displacement}"
            )
        if protected_displacement > 0.005:
            raise RuntimeError(
                "registered_skill_fixture_protected_object_moved:"
                f"observed={protected_displacement}"
            )

        predicates = _predicate_material(environment)
        fixture = {
            "schema_version": FIXTURE_SCHEMA_VERSION,
            "authority": "diagnostic_fixture_only",
            "environment_seed": ENVIRONMENT_SEED,
            "construction": (
                "direct_preregistered_stove_layout_then_three_centimetre_displacement"
            ),
            "requested_translation_from_source_metres": DISPLACEMENT_METRES,
            "observed_translation_from_source_metres": observed_displacement,
            "source_target_position_metres": source_target.tolist(),
            "injected_target_position_metres": injected_target.tolist(),
            "terminal_target_position_metres": terminal_target.tolist(),
            "source_protected_position_metres": source_protected.tolist(),
            "terminal_protected_position_metres": terminal_protected.tolist(),
            "protected_object_displacement_metres": protected_displacement,
            "success_fixture_settle_steps_applied": len(success_trace),
            "success_fixture_settle_trace_sha256": canonical_sha256(
                {"trace": success_trace}
            ),
            "fixture_settle_steps_applied": len(failure_trace),
            "fixture_settle_trace": failure_trace,
            "terminal_goal_predicate_observations": predicates,
            "terminal_goal_predicate_vector": [
                item["satisfied"] for item in predicates
            ],
            "actual_predicate_failure_observed": True,
            "model_inference_invoked": False,
            "repair_attempted": False,
            "physical_execution_invoked": False,
        }
        fixture_sha256 = canonical_sha256(fixture)
        state = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        snapshot = _write_failure_snapshot(
            path=output_dir / "fixture.npz",
            simulator_state=state,
            metadata={
                "task_suite": TASK_SUITE,
                "task_id": TASK_ID,
                "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
                "environment_seed": ENVIRONMENT_SEED,
                "source_failure_basis": "diagnostic_displacement_curriculum",
                "source_goal_predicate_observations": predicates,
                "source_goal_predicate_vector": [
                    item["satisfied"] for item in predicates
                ],
                "source_goal_predicate_vector_sha256": canonical_sha256(
                    {"goal_predicate_observations": predicates}
                ),
                "displacement_curriculum_fixture": fixture,
                "displacement_curriculum_fixture_sha256": fixture_sha256,
                "source_failure_is_repair_candidate": True,
                "model_runtime_invoked_for_snapshot_restore": False,
                "physical_execution_invoked": False,
            },
        )
        result_without_digest = {
            "schema_version": "missionos.libero_registered_skill_fixture.v1",
            "status": "stable_three_centimetre_fixture_observed",
            "fixture": fixture,
            "fixture_sha256": fixture_sha256,
            "snapshot": snapshot,
            "source_layout": {
                name: position.tolist() for name, position in source_positions.items()
            },
            "claim_boundary": {
                "direct_simulator_state_used_for_fixture_setup": True,
                "repair_attempted": False,
                "model_inference_invoked": False,
                "physical_execution_invoked": False,
            },
        }
        result = {
            **result_without_digest,
            "result_sha256": canonical_sha256(result_without_digest),
        }
        (output_dir / "result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return result
    finally:
        environment.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = generate(output_dir=args.output_dir.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

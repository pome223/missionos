#!/usr/bin/env python3
"""Run one governed registered-skill Repair in a restored live LIBERO world."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping
from uuid import uuid4


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import (  # noqa: E402
    RepairAxisObservation,
    RepairAxisStatus,
    RepairDiagnosticAxis,
    RepairDiagnosticContext,
    RepairEvidenceBasis,
    canonical_sha256,
    evaluate_repair_diagnostics,
)
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable  # noqa: E402
from src.runtime.groot_libero_same_world_repair import (  # noqa: E402
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    approve_same_world_repair,
    build_same_world_repair_dispatch,
)
from src.runtime.libero_panda_predicate_package import (  # noqa: E402
    LIBERO_PANDA_SCENE8_ENVIRONMENT,
    LIBERO_REVISION,
)
from src.runtime.libero_registered_skill_repair import (  # noqa: E402
    MOKA_POT_2_STOVE_SKILL_ID,
    REGISTERED_SKILL_HOLD_ACTION_7D,
    build_registered_skill_same_world_repair_proposal,
    run_registered_skill_same_world_repair,
)


OPT_IN_ENV = "RUN_MISSIONOS_LIBERO_REGISTERED_SKILL_REPAIR"
TASK_NAME = "KITCHEN_SCENE8_put_both_moka_pots_on_the_stove"
TASK_SUITE = "libero_10"
TASK_ID = 8
EPISODE_INIT_STATE_INDEX = 15
ENVIRONMENT_SEED = 0
TARGET_OBJECT = "moka_pot_2"
PROTECTED_OBJECT = "moka_pot_1"
STOVE_REGION = "flat_stove_1_cook_region"
EXPECTED_SOURCE_VECTOR = [True, False, True]
EXPECTED_TERMINAL_VECTOR = [True, True, True]
FIXTURE_SCHEMA_VERSION = "missionos.libero_displacement_curriculum_fixture.v2"
FIXTURE_BASIS = "diagnostic_displacement_curriculum"
RESTORE_MAXIMUM_ABSOLUTE_ERROR = 1e-12
PRESERVATION_LIMIT_METRES = 0.005


def _evidence_ref(digest: str, pointer: str) -> str:
    if len(digest) != 64 or not pointer.startswith("#/"):
        raise RuntimeError("registered_skill_diagnostic_evidence_ref_invalid")
    return f"sha256:{digest}{pointer}"


def _criterion_ref(material: Mapping[str, Any]) -> str:
    return f"sha256:{canonical_sha256(dict(material))}"


def _build_repair_diagnostic_report(
    *, repair_result: Mapping[str, Any], raw_trace: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """Project the governed attempt into the backend-neutral five-axis contract."""

    result_sha256 = repair_result.get("result_sha256")
    if not isinstance(result_sha256, str) or len(result_sha256) != 64:
        raise RuntimeError("registered_skill_diagnostic_result_digest_missing")
    raw_trace_sha256 = canonical_sha256({"raw_trace": raw_trace})
    action_count = repair_result.get("registered_skill_action_count")
    if action_count != len(raw_trace) - int(
        repair_result.get("verifier_hold_action_count", 0)
    ):
        raise RuntimeError("registered_skill_diagnostic_action_count_mismatch")
    activity = any(
        any(abs(float(value)) > 1e-9 for value in item.get("action_7d", [])[:6])
        for item in raw_trace[:action_count]
    )
    alignment = any(
        item.get("target_gripper_contact_observed") is True
        for item in raw_trace[:action_count]
    )
    predicate = repair_result.get("predicate_conjunction_observed") is True
    preservation = bool(
        repair_result.get("first_preservation_violation") is None
        and repair_result.get("first_preservation_invariant_breach") is None
    )
    stability = repair_result.get("post_conjunction_stability")
    scope_ref = (
        f"repair-dispatch:{repair_result.get('environment_session_id')}:"
        f"{repair_result.get('dispatch_sha256')}"
    )
    result_ref = _evidence_ref(result_sha256, "#/chunk_evidence")
    trace_ref = _evidence_ref(raw_trace_sha256, "#/raw_trace")

    def observed_axis(
        axis: RepairDiagnosticAxis,
        satisfied: bool,
        criterion: Mapping[str, Any],
        measurements: Mapping[str, Any],
        refs: tuple[str, ...],
    ) -> RepairAxisObservation:
        return RepairAxisObservation(
            axis=axis,
            status=(
                RepairAxisStatus.SATISFIED
                if satisfied
                else RepairAxisStatus.NOT_SATISFIED
            ),
            evidence_basis=RepairEvidenceBasis.SIMULATOR_OBSERVATION,
            criterion_ref=_criterion_ref(criterion),
            observation_scope_ref=scope_ref,
            evidence_refs=refs,
            measurements={"criterion": dict(criterion), **dict(measurements)},
        )

    observations = [
        observed_axis(
            RepairDiagnosticAxis.ACTION_ACTIVITY,
            activity,
            {"criterion": "nonzero_applied_arm_command"},
            {"registered_skill_action_count": action_count},
            (trace_ref, result_ref),
        ),
        observed_axis(
            RepairDiagnosticAxis.CORRECTIVE_ALIGNMENT,
            alignment,
            {"criterion": "failed_target_gripper_contact_observed"},
            {"target_object": TARGET_OBJECT},
            (result_ref,),
        ),
        observed_axis(
            RepairDiagnosticAxis.PREDICATE_RECOVERY,
            predicate,
            {"criterion": "actual_goal_predicate_conjunction_observed"},
            {
                "first_conjunction_after_action": repair_result.get(
                    "first_conjunction_after_action"
                )
            },
            (result_ref,),
        ),
        observed_axis(
            RepairDiagnosticAxis.PRESERVATION,
            preservation,
            {"criterion": "no_preserved_predicate_or_invariant_breach"},
            {
                "first_preservation_violation": repair_result.get(
                    "first_preservation_violation"
                ),
                "first_preservation_invariant_breach": repair_result.get(
                    "first_preservation_invariant_breach"
                ),
            },
            (result_ref,),
        ),
    ]
    stable_criterion = {
        "criterion": "contiguous_post_conjunction_hold",
        "required_steps": 20,
        "authority": "verifier_owned",
    }
    if not isinstance(stability, Mapping) or stability.get("admitted") is not True:
        observations.append(
            RepairAxisObservation(
                axis=RepairDiagnosticAxis.STABLE_HOLD,
                status=RepairAxisStatus.NOT_OBSERVED,
                evidence_basis=RepairEvidenceBasis.NOT_OBSERVED,
                criterion_ref=_criterion_ref(stable_criterion),
                observation_scope_ref=scope_ref,
                measurements={
                    "criterion": stable_criterion,
                    "reason": "stopped_before_verifier_hold",
                },
            )
        )
    else:
        observations.append(
            observed_axis(
                RepairDiagnosticAxis.STABLE_HOLD,
                stability.get("stable") is True,
                stable_criterion,
                {
                    "required_hold_steps": int(stability["required_steps"]),
                    "observed_hold_steps": int(stability["completed_steps"]),
                },
                (_evidence_ref(result_sha256, "#/post_conjunction_stability/trace"),),
            )
        )
    return evaluate_repair_diagnostics(
        observations,
        context=RepairDiagnosticContext(
            report_id=f"repair-diagnostic:{repair_result.get('dispatch_sha256')}",
            executor_ref=str(repair_result.get("execution_adapter")),
            task_ref=f"repair-contract:{repair_result.get('repair_contract_sha256')}",
            fixture_ref=f"source-contract:{repair_result.get('repair_contract_sha256')}",
            evaluation_scope=str(repair_result.get("state_continuity_basis")),
        ),
    ).to_dict()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_snapshot(path: Path) -> tuple[Any, dict[str, Any]]:
    import numpy as np

    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != {"simulator_state", "metadata_json"}:
            raise ValueError("registered_skill_snapshot_members_invalid")
        state = np.asarray(archive["simulator_state"], dtype=np.float64).reshape(-1)
        metadata = json.loads(str(archive["metadata_json"].item()))
    if metadata.get("simulator_state_sha256") != hashlib.sha256(state.tobytes()).hexdigest():
        raise ValueError("registered_skill_snapshot_state_digest_mismatch")
    return state, metadata


def _predicate_material(environment: Any) -> list[dict[str, Any]]:
    material = []
    for index, state in enumerate(environment.env.parsed_problem["goal_state"]):
        spec = tuple(str(part).casefold() for part in state)
        satisfied = environment.env._eval_predicate(state)
        if hasattr(satisfied, "item"):
            satisfied = satisfied.item()
        if not isinstance(satisfied, bool):
            raise RuntimeError("registered_skill_predicate_not_boolean")
        identity = {
            "predicate_index": index,
            "predicate_name": spec[0],
            "arguments": list(spec[1:]),
        }
        material.append(
            {
                **identity,
                "predicate_id": canonical_sha256(identity),
                "satisfied": satisfied,
            }
        )
    return material


def _object_positions(environment: Any) -> dict[str, list[float]]:
    import numpy as np

    simulator = environment.env
    return {
        name: np.asarray(
            simulator.sim.data.body_xpos[simulator.obj_body_id[name]],
            dtype=np.float64,
        ).tolist()
        for name in (PROTECTED_OBJECT, TARGET_OBJECT)
    }


def _object_witnesses(
    environment: Any,
    observation: Mapping[str, Any],
    previous_positions: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    import numpy as np

    simulator = environment.env
    end_effector = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
    region = simulator.object_sites_dict[STOVE_REGION]
    region_position = np.asarray(
        simulator.sim.data.get_site_xpos(STOVE_REGION), dtype=np.float64
    )
    region_matrix = np.asarray(
        simulator.sim.data.get_site_xmat(STOVE_REGION), dtype=np.float64
    ).reshape(3, 3)
    half_extent = np.asarray(region.size, dtype=np.float64)
    stove_model = simulator.get_object(region.parent_name)
    witnesses: dict[str, dict[str, Any]] = {}
    for name in (PROTECTED_OBJECT, TARGET_OBJECT):
        body_id = int(simulator.obj_body_id[name])
        body_name = simulator.sim.model.body_id2name(body_id)
        position = np.asarray(simulator.sim.data.body_xpos[body_id], dtype=np.float64).copy()
        quaternion = np.asarray(simulator.sim.data.body_xquat[body_id], dtype=np.float64).copy()
        local_delta = region_matrix @ (position - region_position)
        margins = {
            "x": float(half_extent[0] - abs(local_delta[0])),
            "y": float(half_extent[1] - abs(local_delta[1])),
            "z_lower": float(local_delta[2] - (half_extent[2] - 0.005)),
            "z_upper": float((half_extent[2] + 0.10) - local_delta[2]),
        }
        inside = all(value > 0.0 for value in margins.values())
        object_model = simulator.get_object(name)
        stove_contact = bool(simulator.check_contact(stove_model, object_model))
        gripper_contact = bool(
            simulator.check_contact(object_model, simulator.robots[0].gripper)
        )
        witnesses[name] = {
            "object_name": name,
            "position_metres": position.tolist(),
            "quaternion_wxyz": quaternion.tolist(),
            "linear_velocity_metres_per_second": np.asarray(
                simulator.sim.data.get_body_xvelp(body_name), dtype=np.float64
            ).tolist(),
            "angular_velocity_radians_per_second": np.asarray(
                simulator.sim.data.get_body_xvelr(body_name), dtype=np.float64
            ).tolist(),
            "step_translation_distance_metres": float(
                np.linalg.norm(position - previous_positions[name])
            ),
            "end_effector_distance_metres": float(np.linalg.norm(position - end_effector)),
            "gripper_contact_observed": gripper_contact,
            "stove_region_witness": {
                "region_name": STOVE_REGION,
                "local_delta_metres": local_delta.tolist(),
                "half_extent_metres": half_extent.tolist(),
                "axis_margins_metres": margins,
                "inside_under_region": inside,
                "stove_parent_contact_observed": stove_contact,
                "on_predicate_witness": inside and stove_contact,
            },
        }
        previous_positions[name] = position
    return witnesses


def _make_environment() -> tuple[Any, Any, str]:
    from libero.libero import benchmark, get_libero_path
    from libero.libero.envs import OffScreenRenderEnv

    suite = benchmark.get_benchmark_dict()[TASK_SUITE]()
    for task_index, task in enumerate(suite.tasks):
        if task.name != TASK_NAME:
            continue
        environment = OffScreenRenderEnv(
            bddl_file_name=os.path.join(
                get_libero_path("bddl_files"), task.problem_folder, task.bddl_file
            ),
            camera_heights=256,
            camera_widths=256,
            camera_depths=True,
        )
        environment.seed(ENVIRONMENT_SEED)
        return environment, suite.get_task_init_states(task_index), task.language
    raise RuntimeError("registered_skill_task_not_found")


class _PrivilegedPushSkill:
    def __init__(self, *, initial_target: Any, desired_target: Any) -> None:
        import numpy as np

        self._targets = (
            ("hover_left_of_target", initial_target + np.array([-0.065, 0.0, 0.15])),
            ("descend_left_of_target", initial_target + np.array([-0.055, 0.0, 0.032])),
            (
                "push_target_to_source_success_position",
                desired_target + np.array([0.035, 0.0, 0.032]),
            ),
            (
                "retract_above_repaired_target",
                desired_target + np.array([0.035, 0.0, 0.15]),
            ),
        )
        self._stage = 0
        self._completion_action_emitted = False

    def invoke(self, observation: Mapping[str, Any], step_index: int) -> tuple[Any, dict]:
        import numpy as np

        end_effector = np.asarray(observation["robot0_eef_pos"], dtype=np.float64)
        while self._stage < len(self._targets):
            stage, target = self._targets[self._stage]
            error = target - end_effector
            if float(np.linalg.norm(error)) > 0.012:
                action = np.zeros(7, dtype=np.float64)
                command_limit = (
                    0.2 if stage == "push_target_to_source_success_position" else 1.0
                )
                action[:3] = np.clip(error / 0.05, -command_limit, command_limit)
                action[6] = -1.0
                return action, {
                    "skill_stage": stage,
                    "skill_step_index": step_index,
                    "privileged_state_read": True,
                    "target_waypoint_metres": target.tolist(),
                    "registered_skill_ready_for_stability": False,
                }
            self._stage += 1
        if self._completion_action_emitted:
            raise RuntimeError("registered_skill_invoked_after_completion")
        self._completion_action_emitted = True
        return np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0]), {
            "skill_stage": "registered_skill_complete",
            "skill_step_index": step_index,
            "privileged_state_read": True,
            "target_waypoint_metres": None,
            "registered_skill_ready_for_stability": True,
        }


def execute_live(
    *,
    snapshot_path: Path,
    output_path: Path,
    dispatch_state_path: Path,
    operator_approval_ref: str,
    maximum_repair_steps: int,
) -> dict[str, Any]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("registered_skill_live_opt_in_required")
    if output_path.exists() or dispatch_state_path.exists():
        raise ValueError("registered_skill_output_exists")
    if maximum_repair_steps <= 20:
        raise ValueError("registered_skill_action_budget_too_small")

    import numpy as np

    state, metadata = _read_snapshot(snapshot_path)
    fixture = metadata.get("displacement_curriculum_fixture")
    if (
        metadata.get("task_suite") != TASK_SUITE
        or metadata.get("task_id") != TASK_ID
        or metadata.get("episode_init_state_index") != EPISODE_INIT_STATE_INDEX
        or metadata.get("environment_seed") != ENVIRONMENT_SEED
        or metadata.get("source_failure_basis") != FIXTURE_BASIS
        or not isinstance(fixture, Mapping)
        or fixture.get("schema_version") != FIXTURE_SCHEMA_VERSION
        or fixture.get("terminal_goal_predicate_vector") != EXPECTED_SOURCE_VECTOR
        or fixture.get("actual_predicate_failure_observed") is not True
        or metadata.get("displacement_curriculum_fixture_sha256")
        != canonical_sha256(fixture)
    ):
        raise RuntimeError("registered_skill_fixture_contract_invalid")

    os.environ.setdefault("MUJOCO_GL", "osmesa")
    os.environ.setdefault("PYOPENGL_PLATFORM", "osmesa")
    environment, init_states, instruction = _make_environment()
    reset_count = 0
    try:
        environment.reset()
        reset_count += 1
        environment.set_init_state(init_states[EPISODE_INIT_STATE_INDEX])
        observation = environment.regenerate_obs_from_state(state)
        restored = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
        difference = np.abs(restored - state)
        if float(difference.max()) > RESTORE_MAXIMUM_ABSOLUTE_ERROR:
            raise RuntimeError("registered_skill_snapshot_restore_tolerance_exceeded")

        source_predicates = _predicate_material(environment)
        source_vector = [item["satisfied"] for item in source_predicates]
        if source_vector != EXPECTED_SOURCE_VECTOR:
            raise RuntimeError("registered_skill_source_predicate_vector_mismatch")
        source_positions = _object_positions(environment)
        source_contract = {
            "task_suite": TASK_SUITE,
            "task_id": TASK_ID,
            "task_name": TASK_NAME,
            "instruction": instruction,
            "episode_init_state_index": EPISODE_INIT_STATE_INDEX,
            "environment_seed": ENVIRONMENT_SEED,
            "libero_revision": LIBERO_REVISION,
            "snapshot_sha256": _sha256_path(snapshot_path),
            "fixture_sha256": metadata["displacement_curriculum_fixture_sha256"],
            "source_goal_predicate_vector": source_vector,
            "registered_skill_id": MOKA_POT_2_STOVE_SKILL_ID,
            "privileged_state_required": True,
            "model_inference_invoked": False,
        }
        source_contract_sha256 = canonical_sha256(source_contract)
        proposal = build_registered_skill_same_world_repair_proposal(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            environment_session_id=f"libero-registered-skill-live-world:{uuid4()}",
            source_contract_sha256=source_contract_sha256,
            source_goal_predicates=source_predicates,
            reset_count=reset_count,
            maximum_repair_steps=maximum_repair_steps,
            source_object_poses=source_positions,
        )
        approval = approve_same_world_repair(
            proposal=proposal,
            operator_approval_ref=operator_approval_ref,
        )
        dispatch = build_same_world_repair_dispatch(
            proposal=proposal,
            approval=approval,
            dispatch_ref=f"libero-registered-skill-dispatch:{uuid4()}",
        )

        initial_target = np.asarray(source_positions[TARGET_OBJECT], dtype=np.float64)
        desired_target = np.asarray(fixture["source_target_position_metres"], dtype=np.float64)
        skill = _PrivilegedPushSkill(
            initial_target=initial_target,
            desired_target=desired_target,
        )
        previous_positions = {
            name: np.asarray(position, dtype=np.float64)
            for name, position in source_positions.items()
        }
        raw_trace: list[dict[str, Any]] = []

        def apply_step(
            action: Any,
            global_index: int,
            *,
            phase: str,
            skill_generated: bool,
        ) -> tuple[Any, dict[str, Any]]:
            nonlocal observation
            normalized = np.asarray(action, dtype=np.float64).reshape(-1)
            if normalized.shape != (7,):
                raise RuntimeError("registered_skill_action_shape_mismatch")
            before = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
            next_observation, _, done, _info = environment.step(normalized.tolist())
            after = np.asarray(environment.sim.get_state().flatten(), dtype=np.float64)
            predicates = _predicate_material(environment)
            conjunction = all(item["satisfied"] for item in predicates)
            official = bool(environment.check_success())
            if official is not conjunction:
                raise RuntimeError("registered_skill_official_predicate_mismatch")
            witnesses = _object_witnesses(
                environment,
                next_observation,
                previous_positions,
            )
            action_sha256 = canonical_sha256({"action_7d": normalized.tolist()})
            trace_entry = {
                "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
                "chunk_index": global_index,
                "action_step_index": 0,
                "action_step_number": 1,
                "global_repair_step_index": global_index,
                "global_repair_step_number": global_index + 1,
                "action_step_sha256": action_sha256,
                "goal_predicate_observations": predicates,
                "goal_predicate_vector_sha256": canonical_sha256(
                    {"goal_predicate_observations": predicates}
                ),
                "official_predicate_conjunction": conjunction,
                "official_predicate_result": official,
                "conjunction_matches_official_result": True,
                "object_witnesses": witnesses,
            }
            raw_trace.append(
                {
                    "global_action_index": global_index,
                    "phase": phase,
                    "action_7d": normalized.tolist(),
                    "predicate_vector": [item["satisfied"] for item in predicates],
                    "target_gripper_contact_observed": witnesses[TARGET_OBJECT][
                        "gripper_contact_observed"
                    ],
                    "protected_displacement_metres": float(
                        np.linalg.norm(
                            np.asarray(witnesses[PROTECTED_OBJECT]["position_metres"])
                            - np.asarray(source_positions[PROTECTED_OBJECT])
                        )
                    ),
                }
            )
            observation = next_observation
            return next_observation, {
                "simulator_step_return_observed": True,
                "simulator_effect_observed": bool(np.any(before != after)),
                "action_chunk_sha256": action_sha256,
                "official_predicate_result": official,
                "done": bool(done),
                "preservation_step_trace": [trace_entry],
                "policy_inference_invoked": False,
                "registered_skill_generated": skill_generated,
            }

        def apply_action(action: Any, index: int) -> tuple[Any, dict[str, Any]]:
            return apply_step(
                action,
                index,
                phase="registered_skill_execution",
                skill_generated=True,
            )

        def apply_hold(action: Any, index: int) -> tuple[Any, dict[str, Any]]:
            if list(action) != list(REGISTERED_SKILL_HOLD_ACTION_7D):
                raise RuntimeError("registered_skill_hold_action_mismatch")
            next_observation, evidence = apply_step(
                action,
                index,
                phase="verifier_owned_post_conjunction_stability",
                skill_generated=False,
            )
            evidence.update(
                {
                    "verifier_hold_step": True,
                    "verifier_hold_action_sha256": canonical_sha256(
                        {"verifier_hold_action_7d": list(action)}
                    ),
                }
            )
            return next_observation, evidence

        result = run_registered_skill_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=DispatchAuthorityTable(dispatch_state_path),
            initial_observation=observation,
            invoke_skill=skill.invoke,
            apply_action_step=apply_action,
            apply_verifier_hold_step=apply_hold,
            observe_goal_predicates=lambda: _predicate_material(environment),
            observed_reset_count=lambda: reset_count,
        )
        final_vector = [
            item["satisfied"] for item in result["final_goal_predicate_observations"]
        ]
        protected_maximum = max(
            (item["protected_displacement_metres"] for item in raw_trace),
            default=0.0,
        )
        observed = bool(
            result["status"] == "stable_satisfied"
            and result["stable_completion_observed"] is True
            and final_vector == EXPECTED_TERMINAL_VECTOR
            and protected_maximum <= PRESERVATION_LIMIT_METRES
        )
        report_without_digest = {
            "schema_version": "missionos.libero_registered_skill_same_world_repair.v1",
            "status": (
                "bounded_registered_skill_repair_observed"
                if observed
                else "bounded_registered_skill_repair_not_observed"
            ),
            "source_contract": source_contract,
            "source_contract_sha256": source_contract_sha256,
            "proposal": proposal,
            "approval": approval,
            "dispatch": dispatch,
            "repair_result": result,
            "source_goal_predicate_vector": source_vector,
            "final_goal_predicate_vector": final_vector,
            "protected_maximum_displacement_metres": protected_maximum,
            "raw_trace": raw_trace,
            "raw_trace_sha256": canonical_sha256({"raw_trace": raw_trace}),
            "repair_diagnostic_report": _build_repair_diagnostic_report(
                repair_result=result,
                raw_trace=raw_trace,
            ),
            "claim_boundary": {
                "live_same_world_after_fixture_setup": True,
                "fixture_setup_used_direct_simulator_state": True,
                "registered_skill_selected_by_exact_residual_match": True,
                "privileged_object_state_used_by_skill": True,
                "model_inference_invoked": False,
                "learned_executor_repair_established": False,
                "controller_ack_observed": False,
                "physical_execution_invoked": False,
                "general_repair_rate_established": False,
            },
        }
        report = {
            **report_without_digest,
            "result_sha256": canonical_sha256(report_without_digest),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return report
    finally:
        environment.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dispatch-state", type=Path, required=True)
    parser.add_argument("--operator-approval-ref", required=True)
    parser.add_argument("--maximum-repair-steps", type=int, default=128)
    args = parser.parse_args()
    report = execute_live(
        snapshot_path=args.snapshot.resolve(),
        output_path=args.output.resolve(),
        dispatch_state_path=args.dispatch_state.resolve(),
        operator_approval_ref=args.operator_approval_ref,
        maximum_repair_steps=args.maximum_repair_steps,
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "bounded_registered_skill_repair_observed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

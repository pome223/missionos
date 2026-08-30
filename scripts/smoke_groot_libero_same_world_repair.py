"""CPU-only runtime smoke for the bounded same-world LIBERO Repair loop."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable  # noqa: E402
from src.runtime.groot_libero_same_world_repair import (  # noqa: E402
    DEFAULT_EXECUTION_ADAPTER,
    LEROBOT_GROOT_N17_EXECUTION_ADAPTER,
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    VLA0_LIBERO_EXECUTION_ADAPTER,
    approve_same_world_repair,
    build_same_world_repair_dispatch,
    build_same_world_repair_proposal,
    run_same_world_repair,
)
from src.runtime.libero_panda_predicate_package import (  # noqa: E402
    LIBERO_PANDA_SCENE8_ENVIRONMENT,
)
from src.runtime.vla0_libero_same_world_repair import (  # noqa: E402
    VLA0_STABLE_SUCCESS_STEPS,
    VLA0_VERIFIER_HOLD_ACTION_7D,
    build_vla0_same_world_repair_proposal,
    run_vla0_same_world_repair,
)


def _vector(*, first: bool = True, second: bool = False) -> list[dict]:
    predicates = [
        ("on", ["moka_pot_1", "flat_stove_1_cook_region"], first),
        ("on", ["moka_pot_2", "flat_stove_1_cook_region"], second),
        ("turnon", ["flat_stove_1"], True),
    ]
    return [
        {
            "predicate_index": index,
            "predicate_id": canonical_sha256(
                {
                    "predicate_index": index,
                    "predicate_name": name,
                    "arguments": arguments,
                }
            ),
            "predicate_name": name,
            "arguments": arguments,
            "satisfied": satisfied,
        }
        for index, (name, arguments, satisfied) in enumerate(predicates)
    ]


def _step_trace(
    *,
    chunk_index: int,
    n_action_steps: int,
    initial_first: bool,
    initial_second: bool,
    target_satisfied_at_last_step: bool,
) -> list[dict]:
    trace = []
    for action_step_index in range(n_action_steps):
        target_now = target_satisfied_at_last_step and action_step_index == n_action_steps - 1
        vector = _vector(
            first=initial_first or target_now,
            second=initial_second or target_now,
        )
        witnesses = {}
        for predicate_index, object_name in enumerate(("moka_pot_1", "moka_pot_2")):
            on_stove = vector[predicate_index]["satisfied"]
            half_extent = [0.075, 0.075, 0.0025]
            local_delta = [0.0 if on_stove else 0.085, 0.0, 0.02]
            margins = {
                "x": half_extent[0] - abs(local_delta[0]),
                "y": half_extent[1] - abs(local_delta[1]),
                "z_lower": local_delta[2] - (half_extent[2] - 0.005),
                "z_upper": (half_extent[2] + 0.10) - local_delta[2],
            }
            witnesses[object_name] = {
                "object_name": object_name,
                "position_metres": [0.1 + predicate_index, 0.2, 0.3],
                "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                "linear_velocity_metres_per_second": [0.0, 0.0, 0.0],
                "angular_velocity_radians_per_second": [0.0, 0.0, 0.0],
                "step_translation_distance_metres": 0.001,
                "end_effector_distance_metres": 0.2,
                "gripper_contact_observed": False,
                "stove_region_witness": {
                    "region_name": "flat_stove_1_cook_region",
                    "local_delta_metres": local_delta,
                    "half_extent_metres": half_extent,
                    "axis_margins_metres": margins,
                    "inside_under_region": on_stove,
                    "stove_parent_contact_observed": True,
                    "on_predicate_witness": on_stove,
                },
            }
        conjunction = all(item["satisfied"] for item in vector)
        global_index = chunk_index * n_action_steps + action_step_index
        trace.append(
            {
                "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
                "chunk_index": chunk_index,
                "action_step_index": action_step_index,
                "action_step_number": action_step_index + 1,
                "global_repair_step_index": global_index,
                "global_repair_step_number": global_index + 1,
                "action_step_sha256": canonical_sha256(
                    {"chunk_index": chunk_index, "action_step_index": action_step_index}
                ),
                "goal_predicate_observations": vector,
                "goal_predicate_vector_sha256": canonical_sha256(
                    {"goal_predicate_observations": vector}
                ),
                "official_predicate_conjunction": conjunction,
                "official_predicate_result": conjunction,
                "conjunction_matches_official_result": True,
                "object_witnesses": witnesses,
            }
        )
    return trace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execution-profile",
        choices=("isaac-zmq", "lerobot-n17", "vla0"),
        default="isaac-zmq",
    )
    args = parser.parse_args()
    lerobot = args.execution_profile == "lerobot-n17"
    vla0 = args.execution_profile == "vla0"
    n_action_steps = 1 if vla0 else 16 if lerobot else 8
    execution_adapter = (
        VLA0_LIBERO_EXECUTION_ADAPTER
        if vla0
        else LEROBOT_GROOT_N17_EXECUTION_ADAPTER
        if lerobot
        else DEFAULT_EXECUTION_ADAPTER
    )
    initial_first = not lerobot
    initial_second = lerobot

    proposal = (
        build_vla0_same_world_repair_proposal(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            environment_session_id="fixture-world:scene8",
            source_contract_sha256="a" * 64,
            source_goal_predicates=_vector(first=initial_first, second=initial_second),
            reset_count=1,
            maximum_repair_steps=2 + VLA0_STABLE_SUCCESS_STEPS,
            proposal_id="fixture-proposal",
        )
        if vla0
        else build_same_world_repair_proposal(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            environment_session_id="fixture-world:scene8",
            source_contract_sha256="a" * 64,
            source_goal_predicates=_vector(first=initial_first, second=initial_second),
            reset_count=1,
            maximum_repair_chunks=3,
            n_action_steps=n_action_steps,
            execution_adapter=execution_adapter,
            repair_instruction_variant="short_target" if lerobot else "semantic_preserve",
            proposal_id="fixture-proposal",
        )
    )
    approval = approve_same_world_repair(
        proposal=proposal,
        operator_approval_ref="fixture-operator-approval",
        approval_id="fixture-approval",
    )
    dispatch = build_same_world_repair_dispatch(
        proposal=proposal,
        approval=approval,
        dispatch_ref="fixture-dispatch",
    )
    current_vector = _vector(first=initial_first, second=initial_second)
    policy_observation_versions: list[int] = []

    def invoke_model(observation, instruction, chunk_index):
        policy_observation_versions.append(observation["version"])
        return {"chunk_index": chunk_index}, {
            "model_runtime_invoked": True,
            "repair_instruction_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_exact_match": True,
            "repair_instruction_payload_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_payload_length": len(instruction),
            "repair_instruction_payload_kind": "list",
            "repair_instruction_payload_dtype": None,
            "repair_instruction_payload_shape": [1],
            "policy_request_sha256": canonical_sha256(
                {"observation": observation, "instruction": instruction}
            ),
            "policy_response_sha256": canonical_sha256({"chunk_index": chunk_index}),
        }

    def apply_action_chunk(action, chunk_index):
        if chunk_index == 1:
            current_vector[:] = _vector(first=True, second=True)
        return {"version": chunk_index + 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": canonical_sha256(action),
            "official_predicate_result": all(item["satisfied"] for item in current_vector),
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                n_action_steps=n_action_steps,
                initial_first=initial_first,
                initial_second=initial_second,
                target_satisfied_at_last_step=chunk_index == 1,
            ),
        }

    hold_steps = 0

    def apply_verifier_hold_step(action, global_action_index):
        nonlocal hold_steps
        hold_steps += 1
        if list(action) != list(VLA0_VERIFIER_HOLD_ACTION_7D):
            raise RuntimeError("vla0 smoke hold action mismatch")
        return {"version": global_action_index + 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": False,
            "official_predicate_result": True,
            "policy_inference_invoked": False,
            "verifier_hold_step": True,
            "verifier_hold_action_sha256": canonical_sha256(
                {"verifier_hold_action_7d": list(VLA0_VERIFIER_HOLD_ACTION_7D)}
            ),
            "preservation_step_trace": _step_trace(
                chunk_index=global_action_index,
                n_action_steps=1,
                initial_first=True,
                initial_second=True,
                target_satisfied_at_last_step=False,
            ),
        }

    with TemporaryDirectory(prefix="missionos-libero-same-world-smoke-") as directory:
        run = run_vla0_same_world_repair if vla0 else run_same_world_repair
        run_kwargs = {
            "proposal": proposal,
            "approval": approval,
            "dispatch": dispatch,
            "dispatch_ledger": DispatchAuthorityTable(Path(directory) / "dispatch.json"),
            "initial_observation": {"version": 0},
            "invoke_model": invoke_model,
            "apply_action_chunk": apply_action_chunk,
            "observe_goal_predicates": lambda: deepcopy(current_vector),
            "observed_reset_count": lambda: 1,
        }
        if vla0:
            run_kwargs["apply_verifier_hold_step"] = apply_verifier_hold_step
        result = run(
            **run_kwargs,
        )

    summary = {
        "schema_version": "missionos_groot_libero_same_world_repair_smoke.v1",
        "runtime": "fixture",
        "execution_adapter": execution_adapter,
        "status": result["status"],
        "chunks_executed": result["chunks_executed"],
        "policy_observation_versions": policy_observation_versions,
        "predicate_improvement_observed": result["predicate_improvement_observed"],
        "repair_instruction_variant": result["repair_instruction_variant"],
        "stable_completion_observed": result["stable_completion_observed"],
        "verifier_hold_action_count": result["verifier_hold_action_count"],
        "task_completion_claimed": result["task_completion_claimed"],
        "same_world_reset_count": result["same_world_reset_count"],
        "dispatch_receipt_present": result["dispatch_receipt_present"],
        "controller_ack_observed": result["controller_ack_observed"],
        "physical_execution_invoked": False,
        "live_model_inference_claimed": False,
        "live_simulator_execution_claimed": False,
    }
    expected = {
        "schema_version": "missionos_groot_libero_same_world_repair_smoke.v1",
        "runtime": "fixture",
        "execution_adapter": execution_adapter,
        "status": "stable_satisfied" if vla0 else "satisfied",
        "chunks_executed": 2,
        "policy_observation_versions": [0, 1],
        "predicate_improvement_observed": True,
        "repair_instruction_variant": "semantic_preserve" if vla0 else (
            "short_target" if lerobot else "semantic_preserve"
        ),
        "stable_completion_observed": vla0,
        "verifier_hold_action_count": VLA0_STABLE_SUCCESS_STEPS if vla0 else 0,
        "task_completion_claimed": True,
        "same_world_reset_count": 1,
        "dispatch_receipt_present": True,
        "controller_ack_observed": False,
        "physical_execution_invoked": False,
        "live_model_inference_claimed": False,
        "live_simulator_execution_claimed": False,
    }
    if summary != expected:
        raise RuntimeError("same-world Repair smoke did not satisfy the frozen fixture boundary")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

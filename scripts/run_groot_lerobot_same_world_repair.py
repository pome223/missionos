#!/usr/bin/env python3
"""Run one opt-in LeRobot GR00T same-world Semantic Repair attempt."""

from __future__ import annotations

import argparse
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import traceback
from typing import Any, Sequence
from uuid import uuid4


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable  # noqa: E402
from src.runtime.groot_lerobot_live_session import (  # noqa: E402
    LEROBOT_LIVE_ACTION_STEPS,
    LeRobotActionChunkExecutionError,
    LeRobotLiveSession,
    SelectedAction,
    batch_single_environment_observation,
    verify_huggingface_local_snapshot,
)
from src.runtime.groot_lerobot_same_world_repair import (  # noqa: E402
    build_lerobot_same_world_repair_proposal,
    run_lerobot_same_world_repair,
)
from src.runtime.groot_libero_same_world_repair import (  # noqa: E402
    STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    STATE_CONTINUITY_LIVE_SAME_WORLD,
    approve_same_world_repair,
    build_same_world_repair_dispatch,
)
from src.runtime.libero_panda_official_runner_instrumentation import (  # noqa: E402
    _observe_libero_goal_predicates,
    digest_runtime_material,
)
from src.runtime.libero_panda_predicate_package import (  # noqa: E402
    LIBERO_PANDA_SCENE8_ENVIRONMENT,
)


OPT_IN_ENV = "RUN_MISSIONOS_GROOT_LEROBOT_SAME_WORLD_REPAIR"
FIXTURE_OPT_IN_ENV = "RUN_MISSIONOS_GROOT_LEROBOT_SAME_WORLD_REPAIR_FIXTURE"
LEROBOT_REVISION = "6adf51511b7625090eade8d82d9f61a1846ebe56"
CHECKPOINT_REPOSITORY = "nvidia/gr00t17-lerobot-libero_10-640"
CHECKPOINT_REVISION = "5ee08ab09fac5c5ef2388a14c882ea825ac861db"
BASE_MODEL_REVISION = "2fc962b973bccdd5d8ce4f67cc63b264d6886495"
COSMOS_REVISION = "9ce19a195e423419c349abfc86fd07178b230561"
SOURCE_INSTRUCTION = "put both moka pots on the stove"
SOURCE_STEP_BUDGET = 520
TASK_SUITE = "libero_10"
TASK_ID = 8
EPISODE_INIT_STATE_INDEX = 1
PROCESS_SEED = 0
ENVIRONMENT_SEED = 0
SIMULATOR_RESET_STABILIZATION_STEPS = 10
MAX_SCREEN_INIT_STATES = 20
REPAIR_CANDIDATE_VECTORS = ((False, True, True), (True, False, True))
SOURCE_FAILURE_BASES = frozenset({"unknown", "post_hoc_reference_success_truncation"})
NATURAL_SCREEN_FAILURE_BASIS = "natural_full_budget_screen"
DIAGNOSTIC_CLONE_FAILURE_BASIS = "diagnostic_restored_failure_snapshot"
FAILURE_SNAPSHOT_SCHEMA_VERSION = "missionos.groot_lerobot_failure_snapshot.v1"
REPLAY_PROGRESS_SCHEMA_VERSION = "missionos.groot_lerobot_repair_replay_progress.v1"
REPLAY_RESULT_SCHEMA_VERSION = "missionos.groot_lerobot_repair_replay_result.v1"
REPLAY_VARIANTS = ("short_target", "original_task")
REFERENCE_SOURCE_STEP_BUDGET = 520
REFERENCE_SOURCE_SUCCESS_STEP_INDEX = 504
REFERENCE_SOURCE_STEPS_EXECUTED = 505
REFERENCE_SOURCE_RESULT_FILE_SHA256 = (
    "37dddfadd465cd7b3ce1ef560db33211a74dd36f2fd725c56ac2c303c9f7985f"
)
REFERENCE_SOURCE_RESULT_ARTIFACT_ID = "groot-lerobot-same-world-repair-20260817c/live-result2.json"


def _observed_reset_stabilization_steps(environment: Any) -> int:
    observed = getattr(environment, "num_steps_wait", None)
    if isinstance(observed, bool) or not isinstance(observed, int):
        raise RuntimeError("lerobot_reset_stabilization_steps_unobservable")
    if observed != SIMULATOR_RESET_STABILIZATION_STEPS:
        raise RuntimeError("lerobot_reset_stabilization_steps_mismatch")
    return observed


class _CountingEnvironment:
    def __init__(self, environment: Any) -> None:
        self.environment = environment
        self.reset_count = 0

    def reset(self, *args: Any, **kwargs: Any) -> Any:
        self.reset_count += 1
        return self.environment.reset(*args, **kwargs)

    def step(self, action: Any) -> Any:
        return self.environment.step(action)

    def close(self) -> None:
        self.environment.close()


def _source_budget_exhausted(
    *,
    source_steps_executed: int,
    source_step_budget: int,
    source_goal_predicate_vector: list[bool],
) -> bool:
    return source_steps_executed == source_step_budget and source_goal_predicate_vector != [
        True,
        True,
        True,
    ]


def _is_repair_candidate(source_goal_predicate_vector: list[bool]) -> bool:
    return tuple(source_goal_predicate_vector) in REPAIR_CANDIDATE_VECTORS


def _repair_claims(
    *,
    repair_completion_established: bool,
    source_budget_exhausted: bool,
    source_failure_basis: str,
    natural_task_failure_established: bool = False,
) -> dict[str, bool]:
    return {
        "semantic_repair_established": bool(
            repair_completion_established
            and source_budget_exhausted
            and natural_task_failure_established
            and source_failure_basis == NATURAL_SCREEN_FAILURE_BASIS
        ),
        "budget_truncated_source_semantic_repair_established": bool(
            repair_completion_established
            and source_budget_exhausted
            and not natural_task_failure_established
            and source_failure_basis == "post_hoc_reference_success_truncation"
        ),
    }


def _build_live_dispatch(*, proposal: dict[str, Any], approval: dict[str, Any]) -> dict[str, Any]:
    return build_same_world_repair_dispatch(
        proposal=proposal,
        approval=approval,
        dispatch_ref=f"groot-lerobot-repair-dispatch:{uuid4()}",
    )


class _PredictionObserver:
    """Observe native GR00T chunk production without changing its output."""

    def __init__(self, policy: Any) -> None:
        self.count = 0
        self.last_prediction_sha256: str | None = None
        original = policy.predict_action_chunk

        def observed(batch: Any, **kwargs: Any) -> Any:
            prediction = original(batch, **kwargs)
            self.count += 1
            self.last_prediction_sha256 = digest_runtime_material("policy_prediction", prediction)
            return prediction

        policy.predict_action_chunk = observed


@dataclass
class _CachedLivePolicy:
    """One loaded policy shared by a bounded multi-init-state screen."""

    checkpoint_path: Path
    observed_lerobot_revision: str
    policy_config: Any
    policy: Any
    prediction_observer: _PredictionObserver
    preprocessor: Any
    postprocessor: Any
    env_preprocessor: Any
    env_postprocessor: Any
    language_observation: dict[str, Any]
    checkpoint_snapshot: dict[str, Any]
    base_model_snapshot: dict[str, Any]
    cosmos_snapshot: dict[str, Any]


_LIVE_POLICY_CACHE: _CachedLivePolicy | None = None
_LIVE_POLICY_LOAD_COUNT = 0


class _InitStateSelectionObserver:
    """Observe the exact LIBERO init-state material selected by one reset."""

    def __init__(self, environment: Any, *, expected_index: int) -> None:
        init_states = getattr(environment, "_init_states", None)
        if init_states is None or len(init_states) == 0:
            raise RuntimeError("lerobot_init_states_unavailable")
        if not 0 <= int(expected_index) < len(init_states):
            raise RuntimeError("lerobot_init_state_index_out_of_range")
        self.expected_index = int(expected_index)
        self.expected_sha256 = digest_runtime_material(
            "libero_init_state",
            init_states[self.expected_index],
        )
        self.observed_sha256: str | None = None
        self.call_count = 0

        environment._ensure_env()
        original = environment._env.set_init_state

        def observed(init_state: Any) -> Any:
            self.call_count += 1
            self.observed_sha256 = digest_runtime_material("libero_init_state", init_state)
            return original(init_state)

        environment._env.set_init_state = observed

    def verify_after_reset(self, environment: Any) -> dict[str, Any]:
        reset_stride = int(environment._reset_stride)
        selected_index = (int(environment.init_state_id) - reset_stride) % len(
            environment._init_states
        )
        verified = bool(
            self.call_count == 1
            and selected_index == self.expected_index
            and self.observed_sha256 == self.expected_sha256
        )
        if not verified:
            raise RuntimeError("lerobot_init_state_selection_mismatch")
        return {
            "requested_index": self.expected_index,
            "selected_index": selected_index,
            "set_init_state_call_count": self.call_count,
            "expected_init_state_sha256": self.expected_sha256,
            "observed_init_state_sha256": self.observed_sha256,
            "selection_verified": True,
            "verification_basis": "observed_set_init_state_argument_digest",
        }


def _fsync_parent_directory(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_parent_directory(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_failure_snapshot(
    *,
    path: Path,
    simulator_state: Any,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Atomically retain a diagnostic-only MuJoCo state artifact.

    The compressed artifact is written before any report references it.  It
    contains no approval or dispatch authority and cannot establish same-world
    Semantic Repair when restored into a new policy/environment session.
    """

    import numpy as np

    if path.suffix != ".npz":
        raise ValueError("lerobot_failure_snapshot_must_use_npz")
    if path.exists():
        raise ValueError("lerobot_failure_snapshot_already_exists")
    state = np.asarray(simulator_state, dtype=np.float64).reshape(-1)
    if state.size == 0 or not np.all(np.isfinite(state)):
        raise ValueError("lerobot_failure_snapshot_state_invalid")
    material = {
        "schema_version": FAILURE_SNAPSHOT_SCHEMA_VERSION,
        "authority": "diagnostic_only",
        "semantic_repair_claim_eligible": False,
        "raw_simulator_state_included": True,
        "simulator_state_value_count": int(state.size),
        "simulator_state_sha256": hashlib.sha256(state.tobytes()).hexdigest(),
        **deepcopy(metadata),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            np.savez_compressed(
                stream,
                simulator_state=state,
                metadata_json=np.asarray(json.dumps(material, sort_keys=True)),
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_parent_directory(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        **material,
        "snapshot_artifact_sha256": _sha256_path(path),
        "local_path_recorded": False,
    }


def _read_failure_snapshot(path: Path) -> tuple[Any, dict[str, Any]]:
    import numpy as np

    artifact_sha256 = _sha256_path(path)
    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != {"simulator_state", "metadata_json"}:
            raise ValueError("lerobot_failure_snapshot_members_invalid")
        state = np.asarray(archive["simulator_state"], dtype=np.float64).reshape(-1)
        metadata = json.loads(str(archive["metadata_json"].item()))
    if metadata.get("schema_version") != FAILURE_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("lerobot_failure_snapshot_schema_mismatch")
    if metadata.get("authority") != "diagnostic_only":
        raise ValueError("lerobot_failure_snapshot_authority_invalid")
    if metadata.get("semantic_repair_claim_eligible") is not False:
        raise ValueError("lerobot_failure_snapshot_claim_boundary_invalid")
    if state.size == 0 or not np.all(np.isfinite(state)):
        raise ValueError("lerobot_failure_snapshot_state_invalid")
    if metadata.get("simulator_state_sha256") != hashlib.sha256(state.tobytes()).hexdigest():
        raise ValueError("lerobot_failure_snapshot_state_digest_mismatch")
    return state, {
        **metadata,
        "snapshot_artifact_sha256": artifact_sha256,
        "local_path_recorded": False,
    }


def _counterbalanced_replay_schedule(
    *,
    trials_per_variant: int,
    seed_base: int,
) -> list[dict[str, Any]]:
    if isinstance(trials_per_variant, bool) or not 1 <= trials_per_variant <= 5:
        raise ValueError("lerobot_replay_trials_per_variant_invalid")
    fully_order_balanced = trials_per_variant % 2 == 0
    schedule: list[dict[str, Any]] = []
    for pair_index in range(trials_per_variant):
        variants = REPLAY_VARIANTS if pair_index % 2 == 0 else tuple(reversed(REPLAY_VARIANTS))
        for order_in_pair, variant in enumerate(variants):
            schedule.append(
                {
                    "trial_index": len(schedule),
                    "pair_index": pair_index,
                    "order_in_pair": order_in_pair,
                    "repair_instruction_variant": variant,
                    "repair_sampling_seed": seed_base + pair_index,
                    "pair_order_alternated": True,
                    "fully_order_balanced": fully_order_balanced,
                }
            )
    return schedule


def _pre_registered_replay_claims() -> dict[str, Any]:
    return {
        "original_world_success": (
            "Semantic Repair established only for the uninterrupted original world."
        ),
        "diagnostic_clones_only_success": (
            "Repair capability or instruction sensitivity observed in diagnostic clones; "
            "Semantic Repair remains false."
        ),
        "both_variants_zero": (
            "In this saved world, checkpoint, instruction set, and budget, Repair was not "
            "shown in {trial_count} diagnostic trials."
        ),
        "variant_difference": (
            "Exploratory instruction-sensitivity evidence only; no general superiority claim."
        ),
        "no_candidate": (
            "Across N states screened by this invocation, M asymmetric partial failures were "
            "observed; "
            "Repair-material incidence for this task/checkpoint is low."
        ),
    }


def _pre_registered_replay_claims_sha256() -> str:
    return canonical_sha256(_pre_registered_replay_claims())


def _git_revision(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _predicate_material(environment: Any) -> list[dict[str, Any]]:
    return [
        item.to_material()
        for item in _observe_libero_goal_predicates(
            environment.environment._env,
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        )
    ]


def _object_poses(environment: Any) -> dict[str, list[float]]:
    import numpy as np

    simulator = environment.environment._env.env
    return {
        name: np.asarray(
            simulator.sim.data.body_xpos[simulator.obj_body_id[name]], dtype=np.float64
        ).tolist()
        for name in ("moka_pot_1", "moka_pot_2")
    }


def _object_witnesses(
    environment: Any,
    observation: dict[str, Any],
    previous_positions: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    import numpy as np

    simulator = environment.environment._env.env
    end_effector = np.asarray(observation["robot_state"]["eef"]["pos"], dtype=np.float64)
    region_name = "flat_stove_1_cook_region"
    region = simulator.object_sites_dict[region_name]
    region_position = np.asarray(simulator.sim.data.get_site_xpos(region_name), dtype=np.float64)
    region_matrix = np.asarray(simulator.sim.data.get_site_xmat(region_name), dtype=np.float64)
    half_extent = np.asarray(region.size, dtype=np.float64)
    stove_model = simulator.get_object(region.parent_name)
    witnesses: dict[str, dict[str, Any]] = {}
    for name in ("moka_pot_1", "moka_pot_2"):
        body_id = int(simulator.obj_body_id[name])
        body_name = simulator.sim.model.body_id2name(body_id)
        if not body_name:
            raise RuntimeError("lerobot_live_body_name_unavailable")
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
        gripper_contact = bool(simulator.check_contact(object_model, simulator.robots[0].gripper))
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
                "region_name": region_name,
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


def _fixture_predicates(*, first: bool, second: bool = True) -> list[dict[str, Any]]:
    material = (
        ("on", ["moka_pot_1", "flat_stove_1_cook_region"], first),
        ("on", ["moka_pot_2", "flat_stove_1_cook_region"], second),
        ("turnon", ["flat_stove_1"], True),
    )
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
        for index, (name, arguments, satisfied) in enumerate(material)
    ]


def execute_fixture(
    *,
    operator_approval_ref: str,
    dispatch_state_path: Path,
    maximum_repair_chunks: int,
    failure_snapshot_path: Path | None = None,
    replay_trials_per_variant: int = 0,
    replay_seed_base: int = 1000,
    replay_progress_output: Path | None = None,
    replay_trial_output_dir: Path | None = None,
) -> dict[str, Any]:
    """Exercise the production CLI orchestration without GR00T or a simulator."""

    if os.environ.get(FIXTURE_OPT_IN_ENV) != "1":
        raise RuntimeError("lerobot_fixture_runtime_opt_in_required")
    if maximum_repair_chunks < 2:
        raise ValueError("lerobot_fixture_requires_two_repair_chunks")
    queue: deque[list[int]] = deque()
    observation = {"version": 0}
    predicates = _fixture_predicates(first=False)
    reset_count = 1
    model_forward_count = 0

    def select_action(raw_observation: Any, instruction: str) -> SelectedAction:
        nonlocal model_forward_count
        forwarded = not queue
        if forwarded:
            model_forward_count += 1
            queue.extend([[model_forward_count, index] for index in range(16)])
        action = queue.popleft()
        return SelectedAction(
            action=action,
            model_forward_observed=forwarded,
            policy_request_sha256=(
                canonical_sha256({"observation": raw_observation, "instruction": instruction})
                if forwarded
                else None
            ),
            policy_response_sha256=(
                canonical_sha256({"model_forward_count": model_forward_count, "action": action})
                if forwarded
                else None
            ),
            instruction_payload=[instruction] if forwarded else None,
        )

    def apply_action(action: Any) -> tuple[dict[str, int], dict[str, Any]]:
        del action
        nonlocal observation, predicates
        observation = {"version": observation["version"] + 1}
        if observation["version"] >= 56:
            predicates = _fixture_predicates(first=True)
        object_witnesses = {
            name: {
                "object_name": name,
                "position_metres": [float(index), 0.0, 1.0],
                "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                "linear_velocity_metres_per_second": [0.0, 0.0, 0.0],
                "angular_velocity_radians_per_second": [0.0, 0.0, 0.0],
                "step_translation_distance_metres": 0.0,
                "end_effector_distance_metres": 0.2,
                "gripper_contact_observed": False,
                "stove_region_witness": {
                    "region_name": "flat_stove_1_cook_region",
                    "local_delta_metres": [
                        0.0 if predicates[index]["satisfied"] else 0.085,
                        0.0,
                        0.02,
                    ],
                    "half_extent_metres": [0.075, 0.075, 0.0025],
                    "axis_margins_metres": {
                        "x": 0.075 if predicates[index]["satisfied"] else -0.01,
                        "y": 0.075,
                        "z_lower": 0.0225,
                        "z_upper": 0.0825,
                    },
                    "inside_under_region": predicates[index]["satisfied"],
                    "stove_parent_contact_observed": predicates[index]["satisfied"],
                    "on_predicate_witness": predicates[index]["satisfied"],
                },
            }
            for index, name in enumerate(("moka_pot_1", "moka_pot_2"))
        }
        return observation, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "official_predicate_result": all(item["satisfied"] for item in predicates),
            "done": False,
            "truncated": False,
            "object_witnesses": object_witnesses,
        }

    session = LeRobotLiveSession(
        initial_observation=observation,
        select_action=select_action,
        apply_action=apply_action,
        observe_goal_predicates=lambda: deepcopy(predicates),
        policy_reset=queue.clear,
        action_queue_depth=lambda: len(queue),
        observed_reset_count=lambda: reset_count,
    )
    source = session.run_source_steps(instruction=SOURCE_INSTRUCTION, maximum_steps=24)
    source_vector = [item["satisfied"] for item in source["source_goal_predicate_observations"]]
    if not _is_repair_candidate(source_vector):
        raise RuntimeError("lerobot_fixture_source_vector_not_repair_candidate")
    policy_boundary = session.begin_repair()
    proposal = build_lerobot_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="lerobot-fixture-world",
        source_contract_sha256=canonical_sha256({"runtime": "fixture"}),
        source_goal_predicates=source["source_goal_predicate_observations"],
        reset_count=reset_count,
        maximum_repair_chunks=2,
        proposal_id="lerobot-fixture-proposal",
    )
    approval = approve_same_world_repair(
        proposal=proposal,
        operator_approval_ref=operator_approval_ref,
        approval_id="lerobot-fixture-approval",
    )
    dispatch = _build_live_dispatch(proposal=proposal, approval=approval)
    repair = run_lerobot_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(dispatch_state_path),
        initial_observation=session.observation,
        invoke_model=session.invoke_model,
        apply_action_chunk=session.apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(predicates),
        observed_reset_count=lambda: reset_count,
    )
    verified = bool(
        repair["status"] == "satisfied"
        and repair["chunks_executed"] == 2
        and reset_count == 1
        and dispatch.get("dispatch_ref")
    )
    report = {
        "schema_version": "missionos.groot_lerobot_production_cli_fixture.v1",
        "result": "fixture_runtime_verified" if verified else "fixture_runtime_failed",
        "runtime": "fixture",
        "source_goal_predicate_vector": source_vector,
        "policy_boundary": policy_boundary,
        "proposal_sha256": proposal["proposal_sha256"],
        "operator_approval_ref": approval["operator_approval_ref"],
        "approval_sha256": approval["approval_sha256"],
        "dispatch_ref": dispatch["dispatch_ref"],
        "repair_status": repair["status"],
        "repair_chunks_executed": repair["chunks_executed"],
        "same_world_reset_count": reset_count,
        "fixture_runtime_verified": verified,
        "repair_executed": True,
        "semantic_repair_established": False,
        "budget_truncated_source_semantic_repair_established": False,
        "live_model_inference_claimed": False,
        "live_simulator_execution_claimed": False,
        "physical_execution_invoked": False,
    }
    if replay_trials_per_variant:
        if failure_snapshot_path is None:
            raise ValueError("lerobot_fixture_replay_snapshot_required")
        if replay_progress_output is None or replay_trial_output_dir is None:
            raise ValueError("lerobot_fixture_replay_atomic_outputs_required")
        snapshot = _write_failure_snapshot(
            path=failure_snapshot_path,
            simulator_state=[0.1, 0.2, 0.3],
            metadata={
                "task_suite": TASK_SUITE,
                "task_id": TASK_ID,
                "episode_init_state_index": 0,
                "checkpoint_repository": CHECKPOINT_REPOSITORY,
                "checkpoint_revision": CHECKPOINT_REVISION,
                "lerobot_revision": LEROBOT_REVISION,
                "source_contract_sha256": canonical_sha256({"runtime": "fixture"}),
                "source_steps_executed": 24,
                "source_goal_predicate_observations": deepcopy(
                    source["source_goal_predicate_observations"]
                ),
                "source_goal_predicate_vector": source_vector,
                "source_goal_predicate_vector_sha256": canonical_sha256(
                    {"goal_predicate_observations": source["source_goal_predicate_observations"]}
                ),
                "source_object_poses": {
                    "moka_pot_1": [0.0, 0.0, 1.0],
                    "moka_pot_2": [1.0, 0.0, 1.0],
                },
                "source_failure_is_repair_candidate": True,
                "model_runtime_invoked_for_snapshot_restore": False,
                "physical_execution_invoked": False,
            },
        )
        restored_state, restored = _read_failure_snapshot(failure_snapshot_path)
        if restored["snapshot_artifact_sha256"] != snapshot["snapshot_artifact_sha256"]:
            raise RuntimeError("lerobot_fixture_snapshot_round_trip_mismatch")
        schedule = _counterbalanced_replay_schedule(
            trials_per_variant=replay_trials_per_variant,
            seed_base=replay_seed_base,
        )
        summaries = []
        for scheduled in schedule:
            trial_report = {
                "schema_version": "missionos.groot_lerobot_fixture_replay_trial.v1",
                **scheduled,
                "clone_identity_verified": bool(len(restored_state) == 3),
                "source_goal_predicate_vector": source_vector,
                "predicate_improvement_observed": False,
                "semantic_repair_established": False,
                "physical_execution_invoked": False,
            }
            trial_path = replay_trial_output_dir / (
                f"trial-{scheduled['trial_index']:02d}-"
                f"{scheduled['repair_instruction_variant']}.json"
            )
            _write_json(trial_path, trial_report)
            summaries.append(
                {
                    **trial_report,
                    "trial_report_sha256": _sha256_path(trial_path),
                    "local_path_recorded": False,
                }
            )
            _write_json(
                replay_progress_output,
                {
                    "schema_version": REPLAY_PROGRESS_SCHEMA_VERSION,
                    "status": "fixture_replay_in_progress",
                    "schedule": schedule,
                    "completed_trials": summaries,
                    "completed_trial_count": len(summaries),
                    "pre_registered_claims": _pre_registered_replay_claims(),
                    "experiment_preregistration_sha256": (_pre_registered_replay_claims_sha256()),
                    "semantic_repair_established": False,
                    "physical_execution_invoked": False,
                },
            )
        report["fixture_replay"] = {
            "snapshot_artifact_sha256": snapshot["snapshot_artifact_sha256"],
            "completed_trial_count": len(summaries),
            "alternating_pair_order_verified": True,
            "fully_order_balanced": replay_trials_per_variant % 2 == 0,
            "atomic_progress_verified": replay_progress_output.exists(),
            "semantic_repair_established": False,
        }
    return report


def execute_live(
    *,
    checkpoint_path: Path,
    operator_approval_ref: str,
    dispatch_state_path: Path,
    maximum_repair_chunks: int,
    episode_init_state_index: int = EPISODE_INIT_STATE_INDEX,
    source_step_budget: int = SOURCE_STEP_BUDGET,
    source_failure_basis: str = "unknown",
    natural_screen_mode: bool = False,
    repair_instruction_variant: str = "short_target",
    failure_snapshot_path: Path | None = None,
    restore_snapshot_path: Path | None = None,
    repair_sampling_seed: int | None = None,
) -> dict[str, Any]:
    if not checkpoint_path.is_dir():
        raise ValueError("lerobot_checkpoint_directory_required")
    if maximum_repair_chunks <= 0:
        raise ValueError("maximum_repair_chunks_invalid")
    if isinstance(episode_init_state_index, bool) or episode_init_state_index < 0:
        raise ValueError("episode_init_state_index_invalid")
    if isinstance(source_step_budget, bool) or source_step_budget <= 0:
        raise ValueError("source_step_budget_invalid")
    allowed_failure_bases = set(SOURCE_FAILURE_BASES)
    if restore_snapshot_path is not None:
        allowed_failure_bases.add(DIAGNOSTIC_CLONE_FAILURE_BASIS)
    if source_failure_basis not in allowed_failure_bases:
        raise ValueError("source_failure_basis_invalid")
    if natural_screen_mode and source_failure_basis != "unknown":
        raise ValueError("natural_screen_failure_basis_is_observation_derived")
    if natural_screen_mode and source_step_budget != SOURCE_STEP_BUDGET:
        raise ValueError("natural_screen_requires_frozen_full_source_budget")
    if natural_screen_mode and restore_snapshot_path is not None:
        raise ValueError("natural_screen_cannot_restore_snapshot")
    if restore_snapshot_path is not None and failure_snapshot_path is not None:
        raise ValueError("restored_trial_cannot_write_source_failure_snapshot")
    if restore_snapshot_path is not None and repair_sampling_seed is None:
        raise ValueError("restored_trial_repair_sampling_seed_required")
    if repair_sampling_seed is not None and (
        isinstance(repair_sampling_seed, bool) or repair_sampling_seed < 0
    ):
        raise ValueError("repair_sampling_seed_invalid")

    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    import numpy as np
    import torch
    from lerobot.configs import PreTrainedConfig
    from lerobot.envs import make_env_pre_post_processors, preprocess_observation
    from lerobot.envs.configs import LiberoEnv as LiberoEnvConfig
    from lerobot.envs.libero import LiberoEnv as LiberoEnvironment
    from lerobot.envs.libero import _get_suite
    from lerobot.policies import make_policy, make_pre_post_processors
    from lerobot.processor.pipeline import TransitionKey
    from lerobot.utils.constants import ACTION

    import lerobot

    total_horizon = (
        SIMULATOR_RESET_STABILIZATION_STEPS
        + source_step_budget
        + maximum_repair_chunks * LEROBOT_LIVE_ACTION_STEPS
    )
    environment_session_id = f"lerobot-libero10-world:{uuid4()}"
    camera_mapping = {
        "agentview_image": "image",
        "robot0_eye_in_hand_image": "wrist_image",
    }
    env_config = LiberoEnvConfig(
        task=TASK_SUITE,
        task_ids=[TASK_ID],
        episode_length=total_horizon,
        observation_height=256,
        observation_width=256,
        camera_name_mapping=camera_mapping,
    )
    suite = _get_suite(TASK_SUITE)
    raw_environment = LiberoEnvironment(
        task_suite=suite,
        task_id=TASK_ID,
        task_suite_name=TASK_SUITE,
        episode_length=total_horizon,
        obs_type=env_config.obs_type,
        observation_height=256,
        observation_width=256,
        camera_name_mapping=camera_mapping,
        episode_index=episode_init_state_index,
        n_envs=1,
    )
    observed_reset_stabilization_steps = _observed_reset_stabilization_steps(raw_environment)
    environment = _CountingEnvironment(raw_environment)
    init_state_observer = _InitStateSelectionObserver(
        raw_environment,
        expected_index=episode_init_state_index,
    )
    global _LIVE_POLICY_CACHE, _LIVE_POLICY_LOAD_COUNT
    if _LIVE_POLICY_CACHE is None:
        lerobot_root = Path(lerobot.__file__).resolve().parents[2]
        observed_lerobot_revision = _git_revision(lerobot_root)
        if observed_lerobot_revision != LEROBOT_REVISION:
            raise RuntimeError("lerobot_source_revision_mismatch")
        random.seed(PROCESS_SEED)
        np.random.seed(PROCESS_SEED)
        torch.manual_seed(PROCESS_SEED)
        torch.cuda.manual_seed_all(PROCESS_SEED)
        policy_config = PreTrainedConfig.from_pretrained(
            checkpoint_path,
            local_files_only=True,
        )
        checkpoint_snapshot = verify_huggingface_local_snapshot(
            snapshot_path=checkpoint_path,
            expected_revision=CHECKPOINT_REVISION,
            required_files=(
                "config.json",
                "model.safetensors",
                "policy_preprocessor.json",
                "policy_preprocessor_step_2_groot_n1_7_pack_inputs_v1.safetensors",
                "policy_postprocessor.json",
            ),
        )
        base_model_path = Path(policy_config.base_model_path).resolve()
        base_model_snapshot = verify_huggingface_local_snapshot(
            snapshot_path=base_model_path,
            expected_revision=BASE_MODEL_REVISION,
            required_files=(
                "config.json",
                "model-00001-of-00002.safetensors",
                "model-00002-of-00002.safetensors",
                "model.safetensors.index.json",
            ),
        )
        preprocessor_config = json.loads(
            (checkpoint_path / "policy_preprocessor.json").read_text(encoding="utf-8")
        )
        cosmos_locators = [
            step.get("config", {}).get("model_name")
            for step in preprocessor_config.get("steps", [])
            if step.get("registry_name") == "groot_n1_7_vlm_encode_v1"
        ]
        if len(cosmos_locators) != 1 or not isinstance(cosmos_locators[0], str):
            raise RuntimeError("lerobot_cosmos_processor_locator_invalid")
        cosmos_snapshot = verify_huggingface_local_snapshot(
            snapshot_path=Path(cosmos_locators[0]).resolve(),
            expected_revision=COSMOS_REVISION,
            required_files=(
                "config.json",
                "model.safetensors",
                "preprocessor_config.json",
                "tokenizer.json",
            ),
        )
        policy_config.pretrained_path = str(checkpoint_path)
        policy_config.device = "cuda"
        if int(policy_config.n_action_steps) != LEROBOT_LIVE_ACTION_STEPS:
            raise RuntimeError("lerobot_checkpoint_action_steps_mismatch")
        policy = make_policy(cfg=policy_config, env_cfg=env_config)
        policy.eval()
        if int(getattr(policy, "_action_queue_steps", -1)) != LEROBOT_LIVE_ACTION_STEPS:
            raise RuntimeError("lerobot_policy_action_queue_steps_mismatch")
        prediction_observer = _PredictionObserver(policy)
        preprocessor, postprocessor = make_pre_post_processors(
            policy_cfg=policy_config,
            pretrained_path=str(checkpoint_path),
            preprocessor_overrides={"device_processor": {"device": "cuda"}},
        )
        env_preprocessor, env_postprocessor = make_env_pre_post_processors(
            env_cfg=env_config,
            policy_cfg=policy_config,
        )
        pack_step_indices = [
            index
            for index, step in enumerate(preprocessor.steps)
            if type(step).__name__ == "GrootN17PackInputsStep"
        ]
        if len(pack_step_indices) != 1:
            raise RuntimeError("lerobot_language_pack_step_not_unique")
        language_observation: dict[str, Any] = {"count": 0, "payload": None}

        def observe_packed_language(index: int, transition: Any) -> None:
            if index != pack_step_indices[0]:
                return
            complementary = transition.get(TransitionKey.COMPLEMENTARY_DATA, {}) or {}
            language_observation["count"] += 1
            language_observation["payload"] = deepcopy(complementary.get("language"))

        preprocessor.after_step_hooks.append(observe_packed_language)
        _LIVE_POLICY_CACHE = _CachedLivePolicy(
            checkpoint_path=checkpoint_path,
            observed_lerobot_revision=observed_lerobot_revision,
            policy_config=policy_config,
            policy=policy,
            prediction_observer=prediction_observer,
            preprocessor=preprocessor,
            postprocessor=postprocessor,
            env_preprocessor=env_preprocessor,
            env_postprocessor=env_postprocessor,
            language_observation=language_observation,
            checkpoint_snapshot=checkpoint_snapshot,
            base_model_snapshot=base_model_snapshot,
            cosmos_snapshot=cosmos_snapshot,
        )
        _LIVE_POLICY_LOAD_COUNT += 1
    elif _LIVE_POLICY_CACHE.checkpoint_path != checkpoint_path:
        raise RuntimeError("lerobot_live_policy_cache_checkpoint_mismatch")
    loaded = _LIVE_POLICY_CACHE
    observed_lerobot_revision = loaded.observed_lerobot_revision
    policy = loaded.policy
    prediction_observer = loaded.prediction_observer
    preprocessor = loaded.preprocessor
    postprocessor = loaded.postprocessor
    env_preprocessor = loaded.env_preprocessor
    env_postprocessor = loaded.env_postprocessor
    language_observation = loaded.language_observation
    policy.reset()

    source_contract_material = {
        "task_suite": TASK_SUITE,
        "task_id": TASK_ID,
        "episode_init_state_index": episode_init_state_index,
        "episode_init_state_sha256": init_state_observer.expected_sha256,
        "process_seed": PROCESS_SEED,
        "environment_seed": ENVIRONMENT_SEED,
        "source_instruction": SOURCE_INSTRUCTION,
        "source_step_budget": source_step_budget,
        "source_failure_basis": source_failure_basis,
        "candidate_selection_mode": (
            "diagnostic_restored_failure_snapshot"
            if restore_snapshot_path is not None
            else "full_budget_natural_screen"
            if natural_screen_mode
            else "single_episode"
        ),
        "natural_task_failure_established": False,
        "reference_source_run": {
            "source_step_budget": REFERENCE_SOURCE_STEP_BUDGET,
            "source_success_step_index": REFERENCE_SOURCE_SUCCESS_STEP_INDEX,
            "source_steps_executed": REFERENCE_SOURCE_STEPS_EXECUTED,
            "result_file_sha256": REFERENCE_SOURCE_RESULT_FILE_SHA256,
            "result_artifact_id": REFERENCE_SOURCE_RESULT_ARTIFACT_ID,
        },
        "checkpoint_repository": CHECKPOINT_REPOSITORY,
        "checkpoint_revision": CHECKPOINT_REVISION,
        "lerobot_revision": observed_lerobot_revision,
        "simulator_horizon_steps": total_horizon,
        "simulator_reset_stabilization_steps": SIMULATOR_RESET_STABILIZATION_STEPS,
        "observed_reset_stabilization_steps": observed_reset_stabilization_steps,
        "repair_sampling_seed": repair_sampling_seed,
        "experiment_preregistration_sha256": _pre_registered_replay_claims_sha256(),
        "runner_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "lerobot_repair_binding_source_sha256": hashlib.sha256(
            Path(build_lerobot_same_world_repair_proposal.__code__.co_filename).read_bytes()
        ).hexdigest(),
        "shared_repair_core_source_sha256": hashlib.sha256(
            Path(approve_same_world_repair.__code__.co_filename).read_bytes()
        ).hexdigest(),
    }

    source_contract_material.update(
        {
            "checkpoint_snapshot": loaded.checkpoint_snapshot,
            "base_model_revision": BASE_MODEL_REVISION,
            "base_model_snapshot": loaded.base_model_snapshot,
            "cosmos_revision": COSMOS_REVISION,
            "cosmos_snapshot": loaded.cosmos_snapshot,
        }
    )
    source_contract_sha256 = canonical_sha256(source_contract_material)

    observation, _ = environment.reset(seed=ENVIRONMENT_SEED)
    if environment.reset_count != 1:
        raise RuntimeError("lerobot_environment_reset_count_invalid")
    init_state_selection = init_state_observer.verify_after_reset(raw_environment)
    simulator = environment.environment._env.env
    original_horizon = int(simulator.horizon)
    simulator.horizon = total_horizon
    if int(simulator.horizon) != total_horizon:
        raise RuntimeError("lerobot_simulator_horizon_binding_mismatch")
    policy.reset()

    restored_snapshot_metadata: dict[str, Any] | None = None
    if restore_snapshot_path is not None:
        simulator_state, restored_snapshot_metadata = _read_failure_snapshot(restore_snapshot_path)
        if restored_snapshot_metadata.get("task_suite") != TASK_SUITE:
            raise ValueError("lerobot_failure_snapshot_task_suite_mismatch")
        if restored_snapshot_metadata.get("task_id") != TASK_ID:
            raise ValueError("lerobot_failure_snapshot_task_id_mismatch")
        if restored_snapshot_metadata.get("checkpoint_revision") != CHECKPOINT_REVISION:
            raise ValueError("lerobot_failure_snapshot_checkpoint_revision_mismatch")
        if restored_snapshot_metadata.get("lerobot_revision") != observed_lerobot_revision:
            raise ValueError("lerobot_failure_snapshot_lerobot_revision_mismatch")
        raw_observation = raw_environment._env.regenerate_obs_from_state(simulator_state)
        observation = raw_environment._format_raw_obs(raw_observation)
        restored_state = np.asarray(raw_environment._env.get_sim_state(), dtype=np.float64).reshape(
            -1
        )
        if (
            hashlib.sha256(restored_state.tobytes()).hexdigest()
            != restored_snapshot_metadata["simulator_state_sha256"]
        ):
            raise ValueError("lerobot_failure_snapshot_restored_state_digest_mismatch")
        restored_predicates = _predicate_material(environment)
        if (
            canonical_sha256({"goal_predicate_observations": restored_predicates})
            != restored_snapshot_metadata["source_goal_predicate_vector_sha256"]
        ):
            raise ValueError("lerobot_failure_snapshot_predicate_vector_mismatch")
        source_contract_material.update(
            {
                "diagnostic_handoff_snapshot_sha256": restored_snapshot_metadata[
                    "snapshot_artifact_sha256"
                ],
                "original_source_contract_sha256": restored_snapshot_metadata[
                    "source_contract_sha256"
                ],
                "semantic_repair_claim_eligible": False,
            }
        )
        source_contract_sha256 = canonical_sha256(source_contract_material)
        policy.reset()

    previous_positions = {
        name: np.asarray(position, dtype=np.float64)
        for name, position in _object_poses(environment).items()
    }

    def select_action(raw_observation: Any, instruction: str) -> SelectedAction:
        processed = preprocess_observation(batch_single_environment_observation(raw_observation))
        processed["task"] = [instruction]
        processed = env_preprocessor(processed)
        language_count_before = int(language_observation["count"])
        processed = preprocessor(processed)
        if language_observation["count"] != language_count_before + 1:
            raise RuntimeError("lerobot_language_payload_not_observed")
        instruction_payload = deepcopy(language_observation["payload"])
        model_forward_expected = len(policy._action_queue) == 0
        request_sha256 = (
            digest_runtime_material("policy_request", processed) if model_forward_expected else None
        )
        count_before = prediction_observer.count
        action = policy.select_action(processed)
        model_forward_observed = prediction_observer.count == count_before + 1
        if prediction_observer.count not in (count_before, count_before + 1):
            raise RuntimeError("lerobot_model_forward_count_invalid")
        if model_forward_observed is not model_forward_expected:
            raise RuntimeError("lerobot_model_forward_queue_expectation_mismatch")
        response_sha256 = (
            prediction_observer.last_prediction_sha256 if model_forward_observed else None
        )
        if model_forward_observed and not response_sha256:
            raise RuntimeError("lerobot_policy_response_digest_missing")
        return SelectedAction(
            action=action,
            model_forward_observed=model_forward_observed,
            policy_request_sha256=request_sha256,
            policy_response_sha256=response_sha256,
            instruction_payload=instruction_payload if model_forward_observed else None,
        )

    def apply_action(selected_action: Any) -> tuple[Any, dict[str, Any]]:
        before = digest_runtime_material("observation", session.observation)
        action = postprocessor(selected_action)
        transition = env_postprocessor({ACTION: action})
        action_numpy = transition[ACTION].to("cpu").numpy()
        if action_numpy.shape != (1, 7):
            raise RuntimeError("lerobot_environment_action_shape_mismatch")
        next_observation, _, terminated, truncated, info = environment.step(action_numpy[0])
        after = digest_runtime_material("observation", next_observation)
        official_result = bool(environment.environment._env.check_success())
        return next_observation, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": before != after,
            "official_predicate_result": official_result,
            "done": bool(info.get("done", terminated and not official_result)),
            "truncated": bool(truncated),
            "object_witnesses": _object_witnesses(
                environment,
                next_observation,
                previous_positions,
            ),
        }

    session = LeRobotLiveSession(
        initial_observation=observation,
        select_action=select_action,
        apply_action=apply_action,
        observe_goal_predicates=lambda: _predicate_material(environment),
        policy_reset=policy.reset,
        action_queue_depth=lambda: len(policy._action_queue),
        observed_reset_count=lambda: environment.reset_count,
    )
    try:
        if restored_snapshot_metadata is None:
            source = session.run_source_steps(
                instruction=SOURCE_INSTRUCTION,
                maximum_steps=source_step_budget,
            )
        else:
            source = {
                "source_steps_executed": restored_snapshot_metadata["source_steps_executed"],
                "source_model_forward_count": 0,
                "source_goal_predicate_observations": restored_predicates,
                "queued_source_actions_remaining": 0,
                "source_restored_from_diagnostic_snapshot": True,
            }
        expected_source_vectors = REPAIR_CANDIDATE_VECTORS
        observed_source_vector = [
            item["satisfied"] for item in source["source_goal_predicate_observations"]
        ]
        source_budget_exhausted = (
            True
            if restored_snapshot_metadata is not None
            else _source_budget_exhausted(
                source_steps_executed=source["source_steps_executed"],
                source_step_budget=source_step_budget,
                source_goal_predicate_vector=observed_source_vector,
            )
        )
        natural_task_failure_established = bool(
            natural_screen_mode
            and source_budget_exhausted
            and _is_repair_candidate(observed_source_vector)
        )
        observed_source_failure_basis = (
            DIAGNOSTIC_CLONE_FAILURE_BASIS
            if restored_snapshot_metadata is not None
            else NATURAL_SCREEN_FAILURE_BASIS
            if natural_task_failure_established
            else source_failure_basis
        )
        base_report = {
            "schema_version": "missionos_groot_lerobot_same_world_repair_live.v1",
            "source": source,
            "source_contract_sha256": source_contract_sha256,
            "source_contract_frozen_before_reset": restored_snapshot_metadata is None,
            "diagnostic_contract_frozen_before_repair": (restored_snapshot_metadata is not None),
            "init_state_selection": init_state_selection,
            "environment_session_id": environment_session_id,
            "source_goal_predicate_vector": observed_source_vector,
            "source_step_budget": source_step_budget,
            "source_budget_exhausted": source_budget_exhausted,
            "source_failure_basis": observed_source_failure_basis,
            "natural_task_failure_established": natural_task_failure_established,
            "reference_source_run": source_contract_material["reference_source_run"],
            "expected_source_goal_predicate_vectors": [
                list(vector) for vector in expected_source_vectors
            ],
            "same_world_reset_count": environment.reset_count,
            "original_simulator_horizon_steps": original_horizon,
            "contract_bound_simulator_horizon_steps": total_horizon,
            "observed_reset_stabilization_steps": observed_reset_stabilization_steps,
            "physical_execution_invoked": False,
            "state_continuity_basis": (
                STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE
                if restored_snapshot_metadata is not None
                else STATE_CONTINUITY_LIVE_SAME_WORLD
            ),
            "diagnostic_clone_identity_verified": restored_snapshot_metadata is not None,
            "diagnostic_handoff_snapshot_sha256": (
                restored_snapshot_metadata["snapshot_artifact_sha256"]
                if restored_snapshot_metadata is not None
                else None
            ),
            "repair_sampling_seed": repair_sampling_seed,
        }
        snapshot_metadata: dict[str, Any] | None = None
        if observed_source_vector != [True, True, True] and failure_snapshot_path is not None:
            snapshot_metadata = _write_failure_snapshot(
                path=failure_snapshot_path,
                simulator_state=raw_environment._env.get_sim_state(),
                metadata={
                    "task_suite": TASK_SUITE,
                    "task_id": TASK_ID,
                    "episode_init_state_index": episode_init_state_index,
                    "checkpoint_repository": CHECKPOINT_REPOSITORY,
                    "checkpoint_revision": CHECKPOINT_REVISION,
                    "lerobot_revision": observed_lerobot_revision,
                    "source_contract_sha256": source_contract_sha256,
                    "source_steps_executed": source["source_steps_executed"],
                    "source_goal_predicate_observations": deepcopy(
                        source["source_goal_predicate_observations"]
                    ),
                    "source_goal_predicate_vector": observed_source_vector,
                    "source_goal_predicate_vector_sha256": canonical_sha256(
                        {
                            "goal_predicate_observations": source[
                                "source_goal_predicate_observations"
                            ]
                        }
                    ),
                    "source_object_poses": _object_poses(environment),
                    "source_failure_is_repair_candidate": _is_repair_candidate(
                        observed_source_vector
                    ),
                    "model_runtime_invoked_for_snapshot_restore": False,
                    "physical_execution_invoked": False,
                },
            )
            base_report["failure_snapshot"] = snapshot_metadata
        if observed_source_vector == [True, True, True]:
            return {
                **base_report,
                "result": "source_satisfied_no_repair_needed",
                "repair_executed": False,
                "semantic_repair_established": False,
                "budget_truncated_source_semantic_repair_established": False,
            }
        if not _is_repair_candidate(observed_source_vector):
            return {
                **base_report,
                "result": "source_vector_not_repair_candidate",
                "repair_executed": False,
                "semantic_repair_established": False,
                "budget_truncated_source_semantic_repair_established": False,
            }

        source_object_poses = _object_poses(environment)
        policy_boundary = session.begin_repair()
        if repair_sampling_seed is not None:
            random.seed(repair_sampling_seed)
            np.random.seed(repair_sampling_seed)
            torch.manual_seed(repair_sampling_seed)
            torch.cuda.manual_seed_all(repair_sampling_seed)
        previous_positions.update(
            {
                name: np.asarray(position, dtype=np.float64)
                for name, position in source_object_poses.items()
            }
        )
        proposal = build_lerobot_same_world_repair_proposal(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            environment_session_id=environment_session_id,
            source_contract_sha256=base_report["source_contract_sha256"],
            source_goal_predicates=source["source_goal_predicate_observations"],
            reset_count=environment.reset_count,
            maximum_repair_chunks=maximum_repair_chunks,
            source_object_poses=source_object_poses,
            repair_instruction_variant=repair_instruction_variant,
            state_continuity_basis=(
                STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE
                if restored_snapshot_metadata is not None
                else STATE_CONTINUITY_LIVE_SAME_WORLD
            ),
            diagnostic_handoff_snapshot_sha256=(
                restored_snapshot_metadata["snapshot_artifact_sha256"]
                if restored_snapshot_metadata is not None
                else None
            ),
        )
        approval = approve_same_world_repair(
            proposal=proposal,
            operator_approval_ref=operator_approval_ref,
        )
        dispatch = _build_live_dispatch(proposal=proposal, approval=approval)
        repair_result = run_lerobot_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=DispatchAuthorityTable(dispatch_state_path),
            initial_observation=session.observation,
            invoke_model=session.invoke_model,
            apply_action_chunk=session.apply_action_chunk,
            observe_goal_predicates=lambda: _predicate_material(environment),
            observed_reset_count=lambda: environment.reset_count,
            observed_state_continuity_basis=(
                STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE
                if restored_snapshot_metadata is not None
                else STATE_CONTINUITY_LIVE_SAME_WORLD
            ),
        )
        repair_completion_established = bool(
            repair_result.get("status") == "satisfied"
            and repair_result.get("predicate_improvement_observed") is True
            and environment.reset_count == 1
        )
        repair_claims = _repair_claims(
            repair_completion_established=repair_completion_established,
            source_budget_exhausted=source_budget_exhausted,
            source_failure_basis=observed_source_failure_basis,
            natural_task_failure_established=natural_task_failure_established,
        )
        return {
            **base_report,
            "result": repair_result["status"],
            "policy_boundary": policy_boundary,
            "proposal": proposal,
            "approval": approval,
            "dispatch": dispatch,
            "repair_result": repair_result,
            "repair_executed": True,
            **repair_claims,
            "claim_boundary": {
                "model_runtime_invoked": bool(repair_result.get("chunks_executed")),
                "simulator_steps_observed": True,
                "task_completion_claimed": repair_result.get("task_completion_claimed") is True,
                "physical_execution_invoked": False,
                "source_step_budget": source_step_budget,
                "repair_step_budget": maximum_repair_chunks * LEROBOT_LIVE_ACTION_STEPS,
                "source_failure_basis": observed_source_failure_basis,
                "natural_task_failure_established": natural_task_failure_established,
                "diagnostic_clone": restored_snapshot_metadata is not None,
                "diagnostic_clone_completion_cannot_establish_semantic_repair": (
                    restored_snapshot_metadata is not None
                ),
            },
        }
    finally:
        environment.close()


def execute_live_screen(
    *,
    checkpoint_path: Path,
    operator_approval_ref: str,
    dispatch_state_path: Path,
    maximum_repair_chunks: int,
    episode_init_state_indices: Sequence[int],
    source_step_budget: int = SOURCE_STEP_BUDGET,
    progress_output: Path | None = None,
    repair_instruction_variant: str = "short_target",
    failure_snapshot_dir: Path | None = None,
    replay_trials_per_variant: int = 0,
    replay_seed_base: int = 1000,
    replay_progress_output: Path | None = None,
    replay_trial_output_dir: Path | None = None,
) -> dict[str, Any]:
    """Screen init states with one loaded model and repair the first natural candidate.

    The source task always receives its full frozen budget.  Operator input
    cannot label a failure as natural: eligibility is derived from the observed
    full-budget predicate vector.  Successful and non-asymmetric episodes are
    closed without creating approval or dispatch authority.
    """

    indices = tuple(episode_init_state_indices)
    if not indices:
        raise ValueError("lerobot_screen_init_state_indices_required")
    if any(isinstance(index, bool) or index < 0 for index in indices):
        raise ValueError("lerobot_screen_init_state_index_invalid")
    if len(set(indices)) != len(indices):
        raise ValueError("lerobot_screen_init_state_indices_not_unique")
    if len(indices) > MAX_SCREEN_INIT_STATES:
        raise ValueError("lerobot_screen_init_state_limit_exceeded")
    if replay_trials_per_variant and failure_snapshot_dir is None:
        raise ValueError("lerobot_replay_requires_failure_snapshot_dir")
    if replay_trials_per_variant and (
        replay_progress_output is None or replay_trial_output_dir is None
    ):
        raise ValueError("lerobot_replay_atomic_outputs_required")
    load_count_before = _LIVE_POLICY_LOAD_COUNT
    screened: list[dict[str, Any]] = []
    asymmetric_failure_count = 0
    for ordinal, index in enumerate(indices):
        failure_snapshot_path = (
            failure_snapshot_dir / f"screen-{ordinal:03d}-init-{index}.npz"
            if failure_snapshot_dir is not None
            else None
        )
        episode = execute_live(
            checkpoint_path=checkpoint_path,
            operator_approval_ref=operator_approval_ref,
            dispatch_state_path=dispatch_state_path,
            maximum_repair_chunks=maximum_repair_chunks,
            episode_init_state_index=index,
            source_step_budget=source_step_budget,
            source_failure_basis="unknown",
            natural_screen_mode=True,
            repair_instruction_variant=repair_instruction_variant,
            failure_snapshot_path=failure_snapshot_path,
        )
        asymmetric_failure_count += int(episode["natural_task_failure_established"])
        screened.append(
            {
                "episode_init_state_index": index,
                "result": episode["result"],
                "source_steps_executed": episode["source"]["source_steps_executed"],
                "source_goal_predicate_vector": episode["source_goal_predicate_vector"],
                "natural_task_failure_established": episode["natural_task_failure_established"],
                "repair_executed": episode["repair_executed"],
                "failure_snapshot_written": "failure_snapshot" in episode,
                "failure_snapshot_sha256": (
                    episode.get("failure_snapshot", {}).get("snapshot_artifact_sha256")
                ),
            }
        )
        if progress_output is not None:
            _write_json(
                progress_output,
                {
                    "schema_version": (
                        "missionos.groot_lerobot_natural_failure_screen_progress.v1"
                    ),
                    "status": "screen_in_progress",
                    "requested_init_state_indices": list(indices),
                    "episodes_screened": screened,
                    "completed_episode_count": len(screened),
                    "asymmetric_partial_failure_count": asymmetric_failure_count,
                    "pre_registered_no_candidate_conclusion": (
                        _pre_registered_replay_claims()["no_candidate"]
                    ),
                    "experiment_preregistration_sha256": (_pre_registered_replay_claims_sha256()),
                    "repair_authority_created_by_progress_artifact": False,
                    "semantic_repair_established": False,
                    "physical_execution_invoked": False,
                },
            )
        if episode["natural_task_failure_established"]:
            replay = None
            if replay_trials_per_variant:
                if failure_snapshot_path is None or not failure_snapshot_path.exists():
                    raise RuntimeError("lerobot_candidate_failure_snapshot_missing")
                replay = execute_live_replay_trials(
                    checkpoint_path=checkpoint_path,
                    snapshot_path=failure_snapshot_path,
                    operator_approval_ref=operator_approval_ref,
                    dispatch_state_path=dispatch_state_path,
                    maximum_repair_chunks=maximum_repair_chunks,
                    trials_per_variant=replay_trials_per_variant,
                    seed_base=replay_seed_base,
                    progress_output=replay_progress_output,
                    trial_output_dir=replay_trial_output_dir,
                )
            model_initialization_count = _LIVE_POLICY_LOAD_COUNT - load_count_before
            return {
                **episode,
                "diagnostic_replay": replay,
                "screen": {
                    "schema_version": "missionos.groot_lerobot_natural_failure_screen.v1",
                    "requested_init_state_indices": list(indices),
                    "episodes_screened": screened,
                    "selected_init_state_index": index,
                    "selection_rule": "first_full_budget_asymmetric_predicate_failure",
                    "model_initialization_count_during_screen": model_initialization_count,
                    "same_loaded_policy_reused_across_episodes": (
                        model_initialization_count <= 1 and _LIVE_POLICY_CACHE is not None
                    ),
                    "asymmetric_partial_failure_count": asymmetric_failure_count,
                },
            }
    model_initialization_count = _LIVE_POLICY_LOAD_COUNT - load_count_before
    return {
        "schema_version": "missionos.groot_lerobot_natural_failure_screen.v1",
        "result": "no_natural_asymmetric_failure_observed",
        "requested_init_state_indices": list(indices),
        "episodes_screened": screened,
        "asymmetric_partial_failure_count": asymmetric_failure_count,
        "pre_registered_conclusion": (
            _pre_registered_replay_claims()["no_candidate"]
            .replace("N", str(len(screened)), 1)
            .replace("M", str(asymmetric_failure_count), 1)
        ),
        "experiment_preregistration_sha256": _pre_registered_replay_claims_sha256(),
        "repair_executed": False,
        "semantic_repair_established": False,
        "budget_truncated_source_semantic_repair_established": False,
        "model_initialization_count_during_screen": model_initialization_count,
        "same_loaded_policy_reused_across_episodes": (
            model_initialization_count <= 1 and _LIVE_POLICY_CACHE is not None
        ),
        "physical_execution_invoked": False,
    }


def execute_live_replay_trials(
    *,
    checkpoint_path: Path,
    snapshot_path: Path,
    operator_approval_ref: str,
    dispatch_state_path: Path,
    maximum_repair_chunks: int,
    trials_per_variant: int,
    seed_base: int,
    progress_output: Path,
    trial_output_dir: Path,
) -> dict[str, Any]:
    """Run alternating-order diagnostic clones without promoting their outcome."""

    schedule = _counterbalanced_replay_schedule(
        trials_per_variant=trials_per_variant,
        seed_base=seed_base,
    )
    _, snapshot_metadata = _read_failure_snapshot(snapshot_path)
    expected_vector = list(snapshot_metadata["source_goal_predicate_vector"])
    if not _is_repair_candidate(expected_vector):
        raise ValueError("lerobot_replay_snapshot_not_asymmetric_candidate")
    preregistration = _pre_registered_replay_claims()
    trials: list[dict[str, Any]] = []
    for scheduled in schedule:
        trial_index = int(scheduled["trial_index"])
        variant = str(scheduled["repair_instruction_variant"])
        trial_report = execute_live(
            checkpoint_path=checkpoint_path,
            operator_approval_ref=operator_approval_ref,
            dispatch_state_path=dispatch_state_path,
            maximum_repair_chunks=maximum_repair_chunks,
            episode_init_state_index=int(snapshot_metadata["episode_init_state_index"]),
            source_step_budget=int(snapshot_metadata["source_steps_executed"]),
            source_failure_basis=DIAGNOSTIC_CLONE_FAILURE_BASIS,
            repair_instruction_variant=variant,
            restore_snapshot_path=snapshot_path,
            repair_sampling_seed=int(scheduled["repair_sampling_seed"]),
        )
        if trial_report.get("source_goal_predicate_vector") != expected_vector:
            raise RuntimeError("lerobot_replay_clone_identity_mismatch")
        if trial_report.get("diagnostic_clone_identity_verified") is not True:
            raise RuntimeError("lerobot_replay_clone_identity_not_verified")
        if trial_report.get("semantic_repair_established") is not False:
            raise RuntimeError("lerobot_diagnostic_clone_semantic_claim_forbidden")
        trial_path = trial_output_dir / f"trial-{trial_index:02d}-{variant}.json"
        if trial_path.exists():
            raise ValueError("lerobot_replay_trial_output_already_exists")
        _write_json(trial_path, trial_report)
        repair_result = trial_report.get("repair_result", {})
        summary = {
            **scheduled,
            "clone_identity_verified": True,
            "source_goal_predicate_vector": expected_vector,
            "terminal_status": trial_report["result"],
            "predicate_improvement_observed": (
                repair_result.get("predicate_improvement_observed") is True
            ),
            "task_satisfied_in_diagnostic_clone": (
                repair_result.get("status") == "satisfied_diagnostic_observation"
            ),
            "preservation_violation_observed": repair_result.get("status")
            in {
                "stopped_on_preservation_invariant",
                "stopped_on_preservation_violation",
            },
            "chunks_executed": repair_result.get("chunks_executed"),
            "semantic_repair_established": False,
            "trial_report_sha256": _sha256_path(trial_path),
            "local_path_recorded": False,
        }
        trials.append(summary)
        _write_json(
            progress_output,
            {
                "schema_version": REPLAY_PROGRESS_SCHEMA_VERSION,
                "status": "replay_in_progress",
                "snapshot_artifact_sha256": snapshot_metadata["snapshot_artifact_sha256"],
                "expected_source_goal_predicate_vector": expected_vector,
                "schedule": schedule,
                "completed_trials": trials,
                "completed_trial_count": len(trials),
                "pre_registered_claims": preregistration,
                "experiment_preregistration_sha256": (_pre_registered_replay_claims_sha256()),
                "semantic_repair_established": False,
                "physical_execution_invoked": False,
            },
        )
    success_counts = {
        variant: sum(
            trial["predicate_improvement_observed"]
            for trial in trials
            if trial["repair_instruction_variant"] == variant
        )
        for variant in REPLAY_VARIANTS
    }
    if all(count == 0 for count in success_counts.values()):
        conclusion_key = "both_variants_zero"
    elif len(set(success_counts.values())) > 1:
        conclusion_key = "variant_difference"
    else:
        conclusion_key = "diagnostic_clones_only_success"
    result = {
        "schema_version": REPLAY_RESULT_SCHEMA_VERSION,
        "snapshot_artifact_sha256": snapshot_metadata["snapshot_artifact_sha256"],
        "expected_source_goal_predicate_vector": expected_vector,
        "schedule": schedule,
        "trials": trials,
        "completed_trial_count": len(trials),
        "success_counts": success_counts,
        "conclusion_key": conclusion_key,
        "pre_registered_conclusion": preregistration[conclusion_key].format(
            trial_count=len(trials)
        ),
        "experiment_preregistration_sha256": _pre_registered_replay_claims_sha256(),
        "general_instruction_superiority_claimed": False,
        "same_world_semantic_repair_claimed_from_clones": False,
        "semantic_repair_established": False,
        "physical_execution_invoked": False,
    }
    _write_json(progress_output, {**result, "status": "replay_complete"})
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", choices=("live", "fixture"), default="live")
    parser.add_argument("--checkpoint-path", type=Path, required=True)
    parser.add_argument("--operator-approval-ref", required=True)
    parser.add_argument("--dispatch-state-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-repair-chunks", type=int, default=45)
    parser.add_argument(
        "--episode-init-state-index",
        type=int,
        default=EPISODE_INIT_STATE_INDEX,
    )
    parser.add_argument(
        "--screen-progress-output",
        type=Path,
        help="atomically retain each completed screen episode without granting authority",
    )
    parser.add_argument(
        "--failure-snapshot-out",
        type=Path,
        help="atomically save one failed source world as a diagnostic-only .npz artifact",
    )
    parser.add_argument(
        "--failure-snapshot-dir",
        type=Path,
        help="save every failed screen world; required for in-session replay trials",
    )
    parser.add_argument(
        "--restore-snapshot",
        type=Path,
        help="run one diagnostic clone from a saved failure snapshot",
    )
    parser.add_argument(
        "--replay-trials-per-variant",
        type=int,
        default=0,
        help="run 1-5 alternating-order diagnostic trials per instruction variant",
    )
    parser.add_argument("--replay-seed-base", type=int, default=1000)
    parser.add_argument("--replay-progress-output", type=Path)
    parser.add_argument("--replay-trial-output-dir", type=Path)
    parser.add_argument("--repair-sampling-seed", type=int)
    parser.add_argument("--source-step-budget", type=int, default=SOURCE_STEP_BUDGET)
    parser.add_argument(
        "--repair-instruction-variant",
        choices=("short_target", "original_task"),
        default="short_target",
        help="Contract-bound instruction ablation; original_task keeps the frozen task wording",
    )
    parser.add_argument(
        "--screen-init-state-index",
        dest="screen_init_state_indices",
        action="append",
        type=int,
        help=(
            "repeat to screen multiple init states with one model load; Repair is issued "
            "only for the first observed full-budget asymmetric failure"
        ),
    )
    parser.add_argument(
        "--source-failure-basis",
        choices=sorted(SOURCE_FAILURE_BASES),
        default="unknown",
    )
    args = parser.parse_args()
    if os.environ.get(OPT_IN_ENV) != "1":
        print(json.dumps({"status": "not_run", "required_opt_in": OPT_IN_ENV}))
        return 3
    try:
        if args.runtime == "fixture":
            if args.screen_init_state_indices:
                raise ValueError("fixture_runtime_does_not_screen_init_states")
            report = execute_fixture(
                operator_approval_ref=args.operator_approval_ref,
                dispatch_state_path=args.dispatch_state_path.resolve(),
                maximum_repair_chunks=args.maximum_repair_chunks,
                failure_snapshot_path=(
                    args.failure_snapshot_out.resolve()
                    if args.failure_snapshot_out is not None
                    else None
                ),
                replay_trials_per_variant=args.replay_trials_per_variant,
                replay_seed_base=args.replay_seed_base,
                replay_progress_output=(
                    args.replay_progress_output.resolve()
                    if args.replay_progress_output is not None
                    else None
                ),
                replay_trial_output_dir=(
                    args.replay_trial_output_dir.resolve()
                    if args.replay_trial_output_dir is not None
                    else None
                ),
            )
        elif args.screen_init_state_indices:
            if args.source_failure_basis != "unknown":
                raise ValueError("screen_source_failure_basis_is_observation_derived")
            report = execute_live_screen(
                checkpoint_path=args.checkpoint_path.resolve(),
                operator_approval_ref=args.operator_approval_ref,
                dispatch_state_path=args.dispatch_state_path.resolve(),
                maximum_repair_chunks=args.maximum_repair_chunks,
                episode_init_state_indices=args.screen_init_state_indices,
                source_step_budget=args.source_step_budget,
                progress_output=(
                    args.screen_progress_output.resolve()
                    if args.screen_progress_output is not None
                    else None
                ),
                repair_instruction_variant=args.repair_instruction_variant,
                failure_snapshot_dir=(
                    args.failure_snapshot_dir.resolve()
                    if args.failure_snapshot_dir is not None
                    else None
                ),
                replay_trials_per_variant=args.replay_trials_per_variant,
                replay_seed_base=args.replay_seed_base,
                replay_progress_output=(
                    args.replay_progress_output.resolve()
                    if args.replay_progress_output is not None
                    else None
                ),
                replay_trial_output_dir=(
                    args.replay_trial_output_dir.resolve()
                    if args.replay_trial_output_dir is not None
                    else None
                ),
            )
        else:
            if args.replay_trials_per_variant:
                raise ValueError("replay_trials_require_screen_mode")
            report = execute_live(
                checkpoint_path=args.checkpoint_path.resolve(),
                operator_approval_ref=args.operator_approval_ref,
                dispatch_state_path=args.dispatch_state_path.resolve(),
                maximum_repair_chunks=args.maximum_repair_chunks,
                episode_init_state_index=args.episode_init_state_index,
                source_step_budget=args.source_step_budget,
                source_failure_basis=args.source_failure_basis,
                repair_instruction_variant=args.repair_instruction_variant,
                failure_snapshot_path=(
                    args.failure_snapshot_out.resolve()
                    if args.failure_snapshot_out is not None
                    else None
                ),
                restore_snapshot_path=(
                    args.restore_snapshot.resolve() if args.restore_snapshot is not None else None
                ),
                repair_sampling_seed=args.repair_sampling_seed,
            )
    except Exception as error:
        # Keep the public JSON fail-closed and path-free, while preserving a
        # local operator traceback for diagnosing an expensive opt-in run.
        traceback.print_exc(file=sys.stderr)
        partial_application = (
            error.partial_application
            if isinstance(error, LeRobotActionChunkExecutionError)
            else None
        )
        report = {
            "schema_version": "missionos_groot_lerobot_same_world_repair_live_failure.v1",
            "result": "execution_failed",
            "cause_type": type(error).__name__,
            "repair_executed": bool(
                partial_application and partial_application.get("actions_applied")
            ),
            "semantic_repair_established": False,
            "budget_truncated_source_semantic_repair_established": False,
            "physical_execution_invoked": False,
        }
        if partial_application is not None:
            report["failure_code"] = error.failure_code
            report["partial_application"] = partial_application
    _write_json(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return (
        0
        if report.get("fixture_runtime_verified") is True
        or report.get("semantic_repair_established") is True
        or report.get("budget_truncated_source_semantic_repair_established") is True
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())

"""Run one opt-in, same-world GR00T N1.7 LIBERO Semantic Repair.

The client owns the environment lifecycle. It resets once, runs the frozen
Scene8 mission for 720 simulator steps, and only when that bounded run ends in
the catalogued partial predicate vector does it create a new proposal,
approval, and one bounded Repair dispatch. The Repair loop feeds every updated
observation back to GR00T and verifies predicates after every 8-step chunk.
"""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Callable
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sys
import time
from typing import Any
from uuid import uuid4


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from missionos_core import canonical_sha256  # noqa: E402
from scripts.run_libero_panda_instrumented_live import (  # noqa: E402
    N_ACTION_STEPS,
    _publication_boundary,
    _rollout_deadline,
    build_runner_configuration,
    live_controller_probe,
    runtime_environment,
    verify_runtime_dependency_profile,
    verify_source_and_checkpoint_revisions,
)
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable  # noqa: E402
from src.runtime.groot_libero_same_world_repair import (  # noqa: E402
    DEFAULT_PRESERVED_OBJECT_MAX_DISPLACEMENT_METRES,
    DEFAULT_REPAIR_INSTRUCTION_VARIANT,
    FRAME_CAPTURE_AUTHORITY,
    FRAME_CAPTURE_SCHEMA_VERSION,
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    REPAIR_INSTRUCTION_VARIANTS,
    STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    STATE_CONTINUITY_LIVE_SAME_WORLD,
    approve_same_world_repair,
    build_exact_repair_instruction_payload,
    build_same_world_repair_dispatch,
    build_same_world_repair_proposal,
    run_same_world_repair,
)
from src.runtime.libero_panda_official_runner_instrumentation import (  # noqa: E402
    _digest_material,
    _observe_libero_goal_predicates,
    prepare_libero_panda_instrumented_episode,
)
from src.runtime.libero_panda_predicate_package import (  # noqa: E402
    LIBERO_PANDA_SCENE8_ENVIRONMENT,
)


OPT_IN_ENV = "RUN_MISSIONOS_GROOT_LIBERO_SAME_WORLD_REPAIR"
SOURCE_MAXIMUM_STEPS = 720
DEFAULT_MAXIMUM_REPAIR_CHUNKS = 90
DIAGNOSTIC_HANDOFF_SCHEMA_VERSION = "missionos_groot_libero_diagnostic_handoff_snapshot.v1"


class SameWorldRepairLiveError(RuntimeError):
    def __init__(
        self,
        phase: str,
        *,
        cause_type: str | None = None,
        diagnostic_evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"same-world Repair live phase failed: {phase}")
        self.phase = phase
        self.cause_type = cause_type
        self.diagnostic_evidence = deepcopy(diagnostic_evidence or {})


def _phase_call(
    phase: str,
    operation: Callable[[], Any],
    *,
    diagnostic_evidence: dict[str, Any] | None = None,
) -> Any:
    try:
        return operation()
    except SameWorldRepairLiveError:
        raise
    except Exception as error:
        cause_code = str(error)
        if re.fullmatch(r"[a-z0-9_:.-]{1,128}", cause_code):
            diagnostic_evidence = {
                **(diagnostic_evidence or {}),
                "failure_code": cause_code,
            }
        raise SameWorldRepairLiveError(
            phase,
            cause_type=type(error).__name__,
            diagnostic_evidence=diagnostic_evidence,
        ) from error


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _predicate_material(raw_env: Any) -> list[dict[str, Any]]:
    return [
        item.to_material()
        for item in _observe_libero_goal_predicates(
            raw_env._env,
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        )
    ]


def _conjunction(vector: list[dict[str, Any]]) -> bool:
    return bool(vector) and all(item["satisfied"] for item in vector)


def _has_semantic_handoff(vector: list[dict[str, Any]]) -> bool:
    return any(item["satisfied"] for item in vector) and any(
        not item["satisfied"] for item in vector
    )


def _semantic_repair_established(
    *, proposal: dict[str, Any], repair_result: dict[str, Any]
) -> bool:
    """Keep diagnostic state clones permanently outside the Repair claim path."""

    return bool(
        proposal.get("semantic_repair_claim_eligible") is True
        and repair_result.get("predicate_improvement_observed") is True
    )


def _task_completion_claimed(*, proposal: dict[str, Any], repair_result: dict[str, Any]) -> bool:
    return bool(
        proposal.get("semantic_repair_claim_eligible") is True
        and repair_result.get("task_completion_claimed") is True
    )


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_diagnostic_handoff_snapshot(
    *,
    path: Path,
    simulator_state: Any,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Write a diagnostic-only MuJoCo clone artifact without granting authority."""

    import numpy as np

    if path.suffix != ".npz":
        raise ValueError("diagnostic_handoff_snapshot_must_use_npz")
    if path.exists():
        raise ValueError("diagnostic_handoff_snapshot_already_exists")
    state = np.asarray(simulator_state, dtype=np.float64).reshape(-1)
    if state.size == 0 or not np.all(np.isfinite(state)):
        raise ValueError("diagnostic_handoff_simulator_state_invalid")
    material = {
        "schema_version": DIAGNOSTIC_HANDOFF_SCHEMA_VERSION,
        "authority": "diagnostic_only",
        "raw_simulator_state_included": True,
        "semantic_repair_claim_eligible": False,
        "simulator_state_sha256": hashlib.sha256(state.tobytes()).hexdigest(),
        **deepcopy(metadata),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        np.savez_compressed(
            handle,
            simulator_state=state,
            metadata_json=np.asarray(json.dumps(material, sort_keys=True)),
        )
    return {
        **material,
        "snapshot_artifact_sha256": _sha256_path(path),
        "local_path_recorded": False,
    }


def _read_diagnostic_handoff_snapshot(path: Path) -> tuple[Any, dict[str, Any]]:
    import numpy as np

    artifact_sha256 = _sha256_path(path)
    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != {"simulator_state", "metadata_json"}:
            raise ValueError("diagnostic_handoff_snapshot_members_invalid")
        state = np.asarray(archive["simulator_state"], dtype=np.float64).reshape(-1)
        metadata = json.loads(str(archive["metadata_json"].item()))
    if metadata.get("schema_version") != DIAGNOSTIC_HANDOFF_SCHEMA_VERSION:
        raise ValueError("diagnostic_handoff_snapshot_schema_mismatch")
    if metadata.get("authority") != "diagnostic_only":
        raise ValueError("diagnostic_handoff_snapshot_authority_invalid")
    if metadata.get("semantic_repair_claim_eligible") is not False:
        raise ValueError("diagnostic_handoff_snapshot_claim_boundary_invalid")
    if state.size == 0 or not np.all(np.isfinite(state)):
        raise ValueError("diagnostic_handoff_simulator_state_invalid")
    if metadata.get("simulator_state_sha256") != hashlib.sha256(state.tobytes()).hexdigest():
        raise ValueError("diagnostic_handoff_simulator_state_digest_mismatch")
    return state, {
        **metadata,
        "snapshot_artifact_sha256": artifact_sha256,
        "local_path_recorded": False,
    }


def execute_same_world_repair(
    *,
    model_path: Path,
    reference_model_path: Path,
    policy_client_host: str,
    policy_client_port: int,
    operator_approval_ref: str,
    dispatch_state_path: Path,
    process_seed: int,
    maximum_repair_chunks: int,
    maximum_elapsed_seconds: float,
    repair_instruction_variant: str = DEFAULT_REPAIR_INSTRUCTION_VARIANT,
    frame_capture_dir: Path | None = None,
    preserved_object_max_displacement_metres: float = (
        DEFAULT_PRESERVED_OBJECT_MAX_DISPLACEMENT_METRES
    ),
    diagnostic_handoff_state_out: Path | None = None,
    diagnostic_handoff_state_in: Path | None = None,
    diagnostic_capture_only: bool = False,
) -> dict[str, Any]:
    if diagnostic_handoff_state_out is not None and diagnostic_handoff_state_in is not None:
        raise ValueError("diagnostic_handoff_snapshot_input_output_mutually_exclusive")
    if diagnostic_capture_only != (diagnostic_handoff_state_out is not None):
        raise ValueError("diagnostic_capture_only_requires_exactly_one_snapshot_output")
    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

    import gymnasium as gym
    import numpy as np
    import torch
    from gr00t.data.embodiment_tags import EmbodimentTag
    from gr00t.eval import rollout_policy as rollout_module
    from gr00t.eval.sim.wrapper.multistep_wrapper import MultiStepWrapper
    from gymnasium.vector.utils import concatenate

    isaac_root = Path(rollout_module.__file__).resolve().parents[2]
    revisions = _phase_call(
        "source_and_checkpoint_verification",
        lambda: verify_source_and_checkpoint_revisions(
            isaac_groot_root=isaac_root,
            model_path=model_path,
            reference_model_path=reference_model_path,
        ),
    )
    runtime = _phase_call("runtime_environment", lambda: runtime_environment(torch))
    _phase_call(
        "runtime_dependency_verification",
        lambda: verify_runtime_dependency_profile(runtime),
    )
    random.seed(process_seed)
    np.random.seed(process_seed)
    torch.manual_seed(process_seed)
    torch.cuda.manual_seed_all(process_seed)

    configuration = build_runner_configuration(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        process_seed=process_seed,
    )
    source_run_identity = f"groot-libero-source:{uuid4()}"
    source_episode_identity = f"{source_run_identity}:episode"
    prepared = prepare_libero_panda_instrumented_episode(
        runner_configuration=configuration,
        maximum_observation_age_seconds=30.0,
        run_identity=source_run_identity,
        episode_identity=source_episode_identity,
    )
    source_contract_sha256 = canonical_sha256(prepared.contract.to_material())
    environment_session_id = f"libero-scene8-world:{uuid4()}"

    policy = _phase_call(
        "policy_client_creation",
        lambda: rollout_module.create_gr00t_sim_policy(
            "",
            EmbodimentTag.LIBERO_PANDA,
            policy_client_host,
            policy_client_port,
        ),
    )
    registered_env = _phase_call(
        "environment_creation",
        lambda: rollout_module.get_gym_env(LIBERO_PANDA_SCENE8_ENVIRONMENT, 0, 1),
    )
    raw_env = getattr(registered_env, "unwrapped", registered_env)
    controller_binding = _phase_call(
        "controller_runtime_probe",
        lambda: live_controller_probe(raw_env),
    )
    if (
        controller_binding.controller_configuration_sha256
        != configuration.controller_configuration_sha256
    ):
        raise RuntimeError("live_controller_binding_mismatch")

    total_maximum_steps = SOURCE_MAXIMUM_STEPS + maximum_repair_chunks * N_ACTION_STEPS
    simulator_env = _phase_call(
        "simulator_horizon_binding",
        lambda: raw_env._env.env,
    )
    original_simulator_horizon = int(simulator_env.horizon)
    simulator_env.horizon = total_maximum_steps
    if int(simulator_env.horizon) != total_maximum_steps:
        raise RuntimeError("simulator_horizon_binding_mismatch")

    class _ResetCountingEnv(gym.Wrapper):
        def __init__(self, env):
            super().__init__(env)
            self.reset_count = 0

        def reset(self, *args, **kwargs):
            self.reset_count += 1
            return self.env.reset(*args, **kwargs)

    def _body_velocity(body_id: int, *, angular: bool) -> list[float]:
        getter_name = "get_body_xvelr" if angular else "get_body_xvelp"
        getter = getattr(simulator_env.sim.data, getter_name, None)
        if not callable(getter):
            raise RuntimeError(f"preservation_step_trace_{getter_name}_unavailable")
        body_name = simulator_env.sim.model.body_id2name(body_id)
        if not body_name:
            raise RuntimeError("preservation_step_trace_body_name_unavailable")
        return np.asarray(getter(body_name), dtype=np.float64).tolist()

    def _object_witness(
        *,
        object_name: str,
        processed_observation: dict[str, Any],
        previous_position: np.ndarray,
    ) -> dict[str, Any]:
        body_id = int(simulator_env.obj_body_id[object_name])
        position = np.asarray(simulator_env.sim.data.body_xpos[body_id], dtype=np.float64).copy()
        quaternion = np.asarray(simulator_env.sim.data.body_xquat[body_id], dtype=np.float64).copy()
        end_effector_position = np.asarray(
            [
                np.asarray(processed_observation["state.x"]).reshape(-1)[0],
                np.asarray(processed_observation["state.y"]).reshape(-1)[0],
                np.asarray(processed_observation["state.z"]).reshape(-1)[0],
            ],
            dtype=np.float64,
        )

        region_name = "flat_stove_1_cook_region"
        region = simulator_env.object_sites_dict[region_name]
        region_position = np.asarray(
            simulator_env.sim.data.get_site_xpos(region_name), dtype=np.float64
        )
        region_matrix = np.asarray(
            simulator_env.sim.data.get_site_xmat(region_name), dtype=np.float64
        )
        half_extent = np.asarray(region.size, dtype=np.float64)
        local_delta = region_matrix @ (position - region_position)
        margins = {
            "x": float(half_extent[0] - abs(local_delta[0])),
            "y": float(half_extent[1] - abs(local_delta[1])),
            "z_lower": float(local_delta[2] - (half_extent[2] - 0.005)),
            "z_upper": float((half_extent[2] + 0.10) - local_delta[2]),
        }
        inside_under_region = all(value > 0.0 for value in margins.values())
        object_model = simulator_env.get_object(object_name)
        stove_model = simulator_env.get_object(region.parent_name)
        stove_parent_contact = bool(simulator_env.check_contact(stove_model, object_model))
        gripper_contact = bool(
            simulator_env.check_contact(object_model, simulator_env.robots[0].gripper)
        )
        return {
            "object_name": object_name,
            "position_metres": position.tolist(),
            "quaternion_wxyz": quaternion.tolist(),
            "linear_velocity_metres_per_second": _body_velocity(body_id, angular=False),
            "angular_velocity_radians_per_second": _body_velocity(body_id, angular=True),
            "step_translation_distance_metres": float(np.linalg.norm(position - previous_position)),
            "end_effector_distance_metres": float(np.linalg.norm(position - end_effector_position)),
            "gripper_contact_observed": gripper_contact,
            "stove_region_witness": {
                "region_name": region_name,
                "local_delta_metres": local_delta.tolist(),
                "half_extent_metres": half_extent.tolist(),
                "axis_margins_metres": margins,
                "inside_under_region": inside_under_region,
                "stove_parent_contact_observed": stove_parent_contact,
                "on_predicate_witness": inside_under_region and stove_parent_contact,
            },
        }

    def _observed_object_poses() -> dict[str, list[float]]:
        return {
            object_name: np.asarray(
                simulator_env.sim.data.body_xpos[simulator_env.obj_body_id[object_name]],
                dtype=np.float64,
            ).tolist()
            for object_name in ("moka_pot_1", "moka_pot_2")
        }

    def _frame_record(status: str, **extra: Any) -> dict[str, Any]:
        return {
            "schema_version": FRAME_CAPTURE_SCHEMA_VERSION,
            "authority": FRAME_CAPTURE_AUTHORITY,
            "status": status,
            "cameras": [],
            **extra,
        }

    def _write_frame(array: Any, stem: Path) -> tuple[str, Path]:
        stem.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import Image

            path = stem.with_suffix(".png")
            Image.fromarray(array).save(path)
            return "png", path
        except Exception:
            path = stem.with_suffix(".npy")
            np.save(path, array)
            return "npy", path

    def _capture_frames(
        *,
        processed_observation: dict[str, Any],
        chunk_index: int,
        action_step_index: int,
    ) -> dict[str, Any]:
        """Record the frames GR00T was shown at this step, for later diagnosis.

        This never raises. Frames explain *why* a preservation predicate fell,
        but the predicates themselves decide the run, so a render failure is
        recorded and the loop continues.
        """

        if frame_capture_dir is None:
            return _frame_record("not_requested")
        try:
            cameras: list[dict[str, Any]] = []
            for key in sorted(
                name
                for name in processed_observation
                if isinstance(name, str) and name.startswith("video.")
            ):
                array = np.asarray(processed_observation[key])
                while array.ndim > 3 and array.shape[0] == 1:
                    array = array[0]
                if array.ndim != 3 or array.shape[2] not in (1, 3) or array.dtype != np.uint8:
                    continue
                array = np.ascontiguousarray(array)
                encoding, written = _write_frame(
                    array,
                    frame_capture_dir
                    / f"chunk{chunk_index:04d}"
                    / f"step{action_step_index:02d}_{key.split('.', 1)[1]}",
                )
                cameras.append(
                    {
                        "observation_key": key,
                        "image_sha256": hashlib.sha256(array.tobytes()).hexdigest(),
                        "artifact_relative_path": written.relative_to(frame_capture_dir).as_posix(),
                        "encoding": encoding,
                        "height_pixels": int(array.shape[0]),
                        "width_pixels": int(array.shape[1]),
                        "channels": int(array.shape[2]),
                    }
                )
            if not cameras:
                return _frame_record("capture_failed", failure_code="no_usable_video_observation")
            return _frame_record("captured") | {"cameras": cameras}
        except Exception as error:
            return _frame_record(
                "capture_failed",
                failure_code=re.sub(r"[^a-z0-9_]", "_", type(error).__name__.lower())[:64],
            )

    class _PreservationStepTraceEnv(gym.Wrapper):
        """Observe each low-level step executed inside the pinned MultiStepWrapper."""

        def __init__(self, env):
            super().__init__(env)
            self._active_chunk_index: int | None = None
            self._trace: list[dict[str, Any]] = []
            self._previous_positions: dict[str, np.ndarray] = {}

        def begin_chunk(self, chunk_index: int) -> None:
            if self._active_chunk_index is not None:
                raise RuntimeError("preservation_step_trace_already_active")
            self._active_chunk_index = chunk_index
            self._trace = []
            self._previous_positions = {
                object_name: np.asarray(
                    simulator_env.sim.data.body_xpos[simulator_env.obj_body_id[object_name]],
                    dtype=np.float64,
                ).copy()
                for object_name in ("moka_pot_1", "moka_pot_2")
            }

        def abort_chunk(self) -> None:
            self._active_chunk_index = None
            self._trace = []
            self._previous_positions = {}

        def finish_chunk(self) -> list[dict[str, Any]]:
            if self._active_chunk_index is None:
                raise RuntimeError("preservation_step_trace_not_active")
            if len(self._trace) != N_ACTION_STEPS:
                raise RuntimeError("preservation_step_trace_count_mismatch")
            trace = deepcopy(self._trace)
            self.abort_chunk()
            return trace

        def step(self, action):
            result = self.env.step(action)
            if self._active_chunk_index is None:
                return result
            processed_observation = result[0]
            action_step_index = len(self._trace)
            if action_step_index >= N_ACTION_STEPS:
                raise RuntimeError("preservation_step_trace_overflow")
            predicates = _predicate_material(raw_env)
            official_result = bool(raw_env._env.check_success())
            conjunction = _conjunction(predicates)
            if official_result is not conjunction:
                raise RuntimeError("preservation_step_trace_conjunction_mismatch")
            witnesses = {
                object_name: _object_witness(
                    object_name=object_name,
                    processed_observation=processed_observation,
                    previous_position=self._previous_positions[object_name],
                )
                for object_name in ("moka_pot_1", "moka_pot_2")
            }
            for object_name, witness in witnesses.items():
                self._previous_positions[object_name] = np.asarray(
                    witness["position_metres"], dtype=np.float64
                )
            global_index = self._active_chunk_index * N_ACTION_STEPS + action_step_index
            self._trace.append(
                {
                    "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
                    "chunk_index": self._active_chunk_index,
                    "action_step_index": action_step_index,
                    "action_step_number": action_step_index + 1,
                    "global_repair_step_index": global_index,
                    "global_repair_step_number": global_index + 1,
                    "action_step_sha256": _digest_material("action_step", action),
                    "goal_predicate_observations": predicates,
                    "goal_predicate_vector_sha256": canonical_sha256(
                        {"goal_predicate_observations": predicates}
                    ),
                    "official_predicate_conjunction": conjunction,
                    "official_predicate_result": official_result,
                    "conjunction_matches_official_result": True,
                    "object_witnesses": witnesses,
                    "frame_capture": _capture_frames(
                        processed_observation=processed_observation,
                        chunk_index=self._active_chunk_index,
                        action_step_index=action_step_index,
                    ),
                }
            )
            return result

    class _NoAutoResetSyncVectorEnv(gym.vector.SyncVectorEnv):
        """One-env vector adapter that never changes episode identity implicitly."""

        def step_wait(self):
            observations = []
            infos = {}
            for index, (env, action) in enumerate(zip(self.envs, self._actions, strict=True)):
                (
                    observation,
                    self._rewards[index],
                    self._terminateds[index],
                    self._truncateds[index],
                    info,
                ) = env.step(action)
                observations.append(observation)
                infos = self._add_info(infos, info, index)
            self.observations = concatenate(
                self.single_observation_space,
                observations,
                self.observations,
            )
            return (
                deepcopy(self.observations) if self.copy else self.observations,
                np.copy(self._rewards),
                np.copy(self._terminateds),
                np.copy(self._truncateds),
                infos,
            )

    counting_env = _ResetCountingEnv(registered_env)
    step_trace_env = _PreservationStepTraceEnv(counting_env)
    wrapped_env = MultiStepWrapper(
        step_trace_env,
        video_delta_indices=np.array([0]),
        state_delta_indices=np.array([0]),
        n_action_steps=N_ACTION_STEPS,
        max_episode_steps=total_maximum_steps,
        terminate_on_success=False,
    )
    vector_env = _NoAutoResetSyncVectorEnv([lambda: wrapped_env])

    def _restore_snapshot_state(simulator_state: Any) -> Any:
        raw_observation = raw_env._env.regenerate_obs_from_state(simulator_state)
        processed_observation = raw_env._process_observation(raw_observation)
        wrapped_env.obs = deque(
            [deepcopy(processed_observation)] * (wrapped_env.max_steps_needed + 1),
            maxlen=wrapped_env.max_steps_needed + 1,
        )
        wrapped_env.reward = []
        wrapped_env.done = []
        restored = wrapped_env._get_obs(
            wrapped_env.video_delta_indices,
            wrapped_env.state_delta_indices,
        )
        vector_env.observations = concatenate(
            vector_env.single_observation_space,
            [restored],
            vector_env.observations,
        )
        return deepcopy(vector_env.observations) if vector_env.copy else vector_env.observations

    source_chunk_evidence: list[dict[str, Any]] = []
    repair_runtime_source = Path(build_same_world_repair_proposal.__code__.co_filename).resolve()
    diagnostic_evidence: dict[str, Any] = {
        "repair_runtime_source_sha256": hashlib.sha256(
            repair_runtime_source.read_bytes()
        ).hexdigest(),
        "original_simulator_horizon_steps": original_simulator_horizon,
        "contract_bound_simulator_horizon_steps": total_maximum_steps,
        "vector_auto_reset_disabled": True,
        "source_steps_executed": 0,
        "source_model_invocation_count": 0,
        "same_world_reset_count": 0,
        "source_goal_predicate_observations": [],
        "source_task_completed": False,
        "semantic_handoff_available": False,
        "preservation_step_trace_enabled": True,
        "preservation_stop_granularity": "before_next_model_chunk",
        "frame_capture_enabled": frame_capture_dir is not None,
        "preservation_invariant_max_displacement_metres": (
            preserved_object_max_displacement_metres
        ),
        "frame_capture_authority": FRAME_CAPTURE_AUTHORITY,
        "state_continuity_basis": (
            STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE
            if diagnostic_handoff_state_in is not None
            else STATE_CONTINUITY_LIVE_SAME_WORLD
        ),
        "diagnostic_handoff_snapshot_loaded": False,
        "diagnostic_handoff_snapshot_written": False,
    }
    started_at = time.monotonic()
    final_report: dict[str, Any] | None = None
    diagnostic_snapshot_metadata: dict[str, Any] | None = None
    try:
        with _rollout_deadline(maximum_elapsed_seconds):
            observation, _ = _phase_call(
                "source_environment_reset",
                lambda: vector_env.reset(seed=process_seed),
                diagnostic_evidence=diagnostic_evidence,
            )
            diagnostic_evidence["same_world_reset_count"] = counting_env.reset_count
            _phase_call(
                "policy_client_reset",
                policy.reset,
                diagnostic_evidence=diagnostic_evidence,
            )
            if counting_env.reset_count != 1:
                raise RuntimeError("source_environment_reset_count_invalid")

            source_vector = _phase_call(
                "source_initial_predicate_observation",
                lambda: _predicate_material(raw_env),
                diagnostic_evidence=diagnostic_evidence,
            )
            diagnostic_evidence["source_goal_predicate_observations"] = source_vector
            if diagnostic_handoff_state_in is not None:
                simulator_state, diagnostic_snapshot_metadata = _phase_call(
                    "diagnostic_handoff_snapshot_read",
                    lambda: _read_diagnostic_handoff_snapshot(
                        diagnostic_handoff_state_in.resolve()
                    ),
                    diagnostic_evidence=diagnostic_evidence,
                )
                if (
                    diagnostic_snapshot_metadata.get("environment")
                    != LIBERO_PANDA_SCENE8_ENVIRONMENT
                ):
                    raise ValueError("diagnostic_handoff_environment_mismatch")
                snapshot_source_contract_sha256 = str(
                    diagnostic_snapshot_metadata.get("source_contract_sha256", "")
                )
                if len(snapshot_source_contract_sha256) != 64:
                    raise ValueError("diagnostic_handoff_source_contract_invalid")
                source_contract_sha256 = snapshot_source_contract_sha256
                if diagnostic_snapshot_metadata.get("process_seed") != process_seed:
                    raise ValueError("diagnostic_handoff_process_seed_mismatch")
                observation = _phase_call(
                    "diagnostic_handoff_state_restore",
                    lambda: _restore_snapshot_state(simulator_state),
                    diagnostic_evidence=diagnostic_evidence,
                )
                restored_state = np.asarray(raw_env._env.get_sim_state(), dtype=np.float64)
                if (
                    hashlib.sha256(restored_state.reshape(-1).tobytes()).hexdigest()
                    != (diagnostic_snapshot_metadata["simulator_state_sha256"])
                ):
                    raise ValueError("diagnostic_handoff_restored_state_digest_mismatch")
                source_vector = _phase_call(
                    "diagnostic_handoff_predicate_observation",
                    lambda: _predicate_material(raw_env),
                    diagnostic_evidence=diagnostic_evidence,
                )
                if (
                    canonical_sha256({"goal_predicate_observations": source_vector})
                    != (diagnostic_snapshot_metadata["source_goal_predicate_vector_sha256"])
                ):
                    raise ValueError("diagnostic_handoff_predicate_vector_mismatch")
                source_steps_executed = int(diagnostic_snapshot_metadata["source_steps_executed"])
                diagnostic_evidence.update(
                    {
                        "source_steps_executed": source_steps_executed,
                        "source_model_invocation_count": 0,
                        "source_goal_predicate_observations": source_vector,
                        "source_task_completed": _conjunction(source_vector),
                        "semantic_handoff_available": _has_semantic_handoff(source_vector),
                        "diagnostic_handoff_snapshot_loaded": True,
                        "diagnostic_handoff_snapshot_sha256": (
                            diagnostic_snapshot_metadata["snapshot_artifact_sha256"]
                        ),
                    }
                )
            else:
                for chunk_index in range(SOURCE_MAXIMUM_STEPS // N_ACTION_STEPS):
                    request_sha256 = _digest_material("policy_request", observation)
                    action_chunk, policy_info = _phase_call(
                        "source_policy_inference",
                        lambda: policy.get_action(observation),
                        diagnostic_evidence=diagnostic_evidence,
                    )
                    response_sha256 = _digest_material(
                        "policy_response",
                        {"action": action_chunk, "info": policy_info},
                    )
                    before_sha256 = _digest_material("observation", observation)
                    next_observation, _, _, _, _ = _phase_call(
                        "source_simulator_step",
                        lambda: vector_env.step(action_chunk),
                        diagnostic_evidence=diagnostic_evidence,
                    )
                    after_sha256 = _digest_material("observation", next_observation)
                    source_vector = _phase_call(
                        "source_predicate_observation",
                        lambda: _predicate_material(raw_env),
                        diagnostic_evidence=diagnostic_evidence,
                    )
                    source_official_predicate_result = _phase_call(
                        "source_official_predicate_observation",
                        lambda: bool(raw_env._env.check_success()),
                        diagnostic_evidence=diagnostic_evidence,
                    )
                    source_conjunction = _conjunction(source_vector)
                    if source_official_predicate_result is not source_conjunction:
                        raise SameWorldRepairLiveError(
                            "source_goal_predicate_conjunction_mismatch",
                            diagnostic_evidence=diagnostic_evidence,
                        )
                    source_chunk_evidence.append(
                        {
                            "chunk_index": chunk_index,
                            "policy_request_sha256": request_sha256,
                            "policy_response_sha256": response_sha256,
                            "action_chunk_sha256": _digest_material("action_chunk", action_chunk),
                            "simulator_step_return_observed": True,
                            "simulator_effect_observed": before_sha256 != after_sha256,
                            "goal_predicate_vector_sha256": canonical_sha256(
                                {"goal_predicate_observations": source_vector}
                            ),
                            "goal_predicate_observations": deepcopy(source_vector),
                            "official_predicate_conjunction": source_conjunction,
                            "official_predicate_result": source_official_predicate_result,
                            "conjunction_matches_official_result": True,
                        }
                    )
                    diagnostic_evidence.update(
                        {
                            "source_steps_executed": (len(source_chunk_evidence) * N_ACTION_STEPS),
                            "source_model_invocation_count": len(source_chunk_evidence),
                            "source_goal_predicate_observations": source_vector,
                            "source_task_completed": _conjunction(source_vector),
                            "semantic_handoff_available": _has_semantic_handoff(source_vector),
                        }
                    )
                    observation = next_observation
                    if _conjunction(source_vector):
                        break

                source_steps_executed = len(source_chunk_evidence) * N_ACTION_STEPS
            diagnostic_evidence["same_world_reset_count"] = counting_env.reset_count
            source_boundary = {
                "contract_frozen_before_reset": diagnostic_handoff_state_in is None,
                "source_contract_loaded_from_diagnostic_snapshot": (
                    diagnostic_handoff_state_in is not None
                ),
                "model_inference_invoked": bool(source_chunk_evidence),
                "simulator_step_return_observed": bool(source_chunk_evidence),
                "controller_ack_observed": False,
                "source_task_completed": _conjunction(source_vector),
                "semantic_handoff_available": _has_semantic_handoff(source_vector),
                "same_world_reset_count": counting_env.reset_count,
                "vector_auto_reset_disabled": True,
                "original_simulator_horizon_steps": original_simulator_horizon,
                "contract_bound_simulator_horizon_steps": total_maximum_steps,
                "physical_execution_invoked": False,
                "state_continuity_basis": diagnostic_evidence["state_continuity_basis"],
                "same_world_continuity_claimed": diagnostic_handoff_state_in is None,
            }
            if _conjunction(source_vector):
                final_report = {
                    "schema_version": ("missionos_groot_n17_libero_same_world_repair_live.v3"),
                    "recorded_at": _utc_now(),
                    "result": "source_satisfied_no_repair_needed",
                    "source_revisions": revisions,
                    "runtime": runtime,
                    "environment": LIBERO_PANDA_SCENE8_ENVIRONMENT,
                    "process_seed": process_seed,
                    "source_contract_sha256": source_contract_sha256,
                    "source_steps_executed": source_steps_executed,
                    "source_chunk_evidence": source_chunk_evidence,
                    "source_goal_predicate_observations": source_vector,
                    "source_boundary": source_boundary,
                    "repair_executed": False,
                    "claim_boundary": {
                        "semantic_repair_established": False,
                        "reason": "source_task_satisfied_before_repair_handoff",
                        "physical_execution_invoked": False,
                    },
                    "publication": _publication_boundary(),
                }
            elif source_steps_executed != SOURCE_MAXIMUM_STEPS:
                raise RuntimeError("source_execution_budget_not_reached")
            elif not _has_semantic_handoff(source_vector):
                final_report = {
                    "schema_version": ("missionos_groot_n17_libero_same_world_repair_live.v3"),
                    "recorded_at": _utc_now(),
                    "result": "source_failed_without_semantic_handoff",
                    "source_revisions": revisions,
                    "runtime": runtime,
                    "environment": LIBERO_PANDA_SCENE8_ENVIRONMENT,
                    "process_seed": process_seed,
                    "source_contract_sha256": source_contract_sha256,
                    "source_steps_executed": source_steps_executed,
                    "source_chunk_evidence": source_chunk_evidence,
                    "source_goal_predicate_observations": source_vector,
                    "source_boundary": source_boundary,
                    "repair_executed": False,
                    "claim_boundary": {
                        "semantic_repair_established": False,
                        "reason": "no_completed_predicate_available_to_preserve",
                        "physical_execution_invoked": False,
                    },
                    "publication": _publication_boundary(),
                }
            else:
                source_object_poses = (
                    diagnostic_snapshot_metadata["source_object_poses"]
                    if diagnostic_snapshot_metadata is not None
                    else _observed_object_poses()
                )
                proposal = _phase_call(
                    "repair_proposal",
                    lambda: build_same_world_repair_proposal(
                        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
                        environment_session_id=environment_session_id,
                        source_contract_sha256=source_contract_sha256,
                        source_goal_predicates=source_vector,
                        reset_count=counting_env.reset_count,
                        maximum_repair_chunks=maximum_repair_chunks,
                        repair_instruction_variant=repair_instruction_variant,
                        # Bind the poses observed at approval time. The operator
                        # approves "keep it where it is now", so the reference
                        # has to be the state they saw, not one re-derived later.
                        source_object_poses=source_object_poses,
                        preserved_object_max_displacement_metres=(
                            preserved_object_max_displacement_metres
                        ),
                        state_continuity_basis=(
                            STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE
                            if diagnostic_snapshot_metadata is not None
                            else STATE_CONTINUITY_LIVE_SAME_WORLD
                        ),
                        diagnostic_handoff_snapshot_sha256=(
                            diagnostic_snapshot_metadata["snapshot_artifact_sha256"]
                            if diagnostic_snapshot_metadata is not None
                            else None
                        ),
                    ),
                    diagnostic_evidence=diagnostic_evidence,
                )
                if diagnostic_handoff_state_out is not None:
                    snapshot_metadata = _phase_call(
                        "diagnostic_handoff_snapshot_write",
                        lambda: _write_diagnostic_handoff_snapshot(
                            path=diagnostic_handoff_state_out.resolve(),
                            simulator_state=raw_env._env.get_sim_state(),
                            metadata={
                                "environment": LIBERO_PANDA_SCENE8_ENVIRONMENT,
                                "process_seed": process_seed,
                                "source_contract_sha256": source_contract_sha256,
                                "source_steps_executed": source_steps_executed,
                                "source_goal_predicate_observations": source_vector,
                                "source_goal_predicate_vector_sha256": canonical_sha256(
                                    {"goal_predicate_observations": source_vector}
                                ),
                                "source_object_poses": source_object_poses,
                                "source_chunk_evidence_sha256": canonical_sha256(
                                    {"source_chunk_evidence": source_chunk_evidence}
                                ),
                            },
                        ),
                        diagnostic_evidence=diagnostic_evidence,
                    )
                    diagnostic_evidence.update(
                        {
                            "diagnostic_handoff_snapshot_written": True,
                            "diagnostic_handoff_snapshot_sha256": snapshot_metadata[
                                "snapshot_artifact_sha256"
                            ],
                        }
                    )
                    final_report = {
                        "schema_version": ("missionos_groot_n17_libero_diagnostic_handoff_live.v1"),
                        "recorded_at": _utc_now(),
                        "result": "diagnostic_handoff_captured_no_repair_dispatched",
                        "source_revisions": revisions,
                        "runtime": runtime,
                        "environment": LIBERO_PANDA_SCENE8_ENVIRONMENT,
                        "process_seed": process_seed,
                        "source_contract_sha256": source_contract_sha256,
                        "source_steps_executed": source_steps_executed,
                        "source_chunk_evidence": source_chunk_evidence,
                        "source_goal_predicate_observations": source_vector,
                        "source_boundary": source_boundary,
                        "diagnostic_handoff_snapshot": {
                            key: value
                            for key, value in snapshot_metadata.items()
                            if key
                            not in {
                                "source_goal_predicate_observations",
                                "source_object_poses",
                            }
                        },
                        "repair_proposal_preview": proposal,
                        "repair_executed": False,
                        "claim_boundary": {
                            "semantic_repair_established": False,
                            "task_completion_claimed": False,
                            "approval_created": False,
                            "dispatch_authority_created": False,
                            "diagnostic_state_clone_only": True,
                            "physical_execution_invoked": False,
                        },
                        "publication": _publication_boundary(),
                    }
                    final_report["elapsed_seconds"] = time.monotonic() - started_at
                    final_report["environment_closed_after_evidence"] = True
                    return final_report
                language_key = "annotation.human.action.task_description"
                _, instruction_delivery_preflight = _phase_call(
                    "repair_instruction_delivery_preflight",
                    lambda: build_exact_repair_instruction_payload(
                        current_language=observation[language_key],
                        instruction=proposal["repair_instruction"],
                    ),
                    diagnostic_evidence=diagnostic_evidence,
                )
                if (
                    instruction_delivery_preflight["repair_instruction_payload_sha256"]
                    != proposal["repair_instruction_sha256"]
                ):
                    raise SameWorldRepairLiveError(
                        "repair_instruction_delivery_preflight",
                        cause_type="InstructionPayloadDigestMismatch",
                    )
                approval = _phase_call(
                    "repair_approval",
                    lambda: approve_same_world_repair(
                        proposal=proposal,
                        operator_approval_ref=operator_approval_ref,
                    ),
                    diagnostic_evidence=diagnostic_evidence,
                )
                dispatch = _phase_call(
                    "repair_dispatch",
                    lambda: build_same_world_repair_dispatch(
                        proposal=proposal,
                        approval=approval,
                        dispatch_ref=f"groot-libero-repair-dispatch:{uuid4()}",
                    ),
                    diagnostic_evidence=diagnostic_evidence,
                )
                last_observation_sha256 = _digest_material("observation", observation)

                def invoke_model(current_observation, instruction, chunk_index):
                    nonlocal last_observation_sha256
                    model_observation = deepcopy(current_observation)
                    current_language = model_observation[language_key]
                    exact_payload, delivery_evidence = build_exact_repair_instruction_payload(
                        current_language=current_language,
                        instruction=instruction,
                    )
                    if (
                        delivery_evidence["repair_instruction_payload_sha256"]
                        != proposal["repair_instruction_sha256"]
                    ):
                        raise RuntimeError("repair_instruction_payload_digest_mismatch")
                    model_observation[language_key] = exact_payload
                    request_sha256 = _digest_material("policy_request", model_observation)
                    action, info = policy.get_action(model_observation)
                    last_observation_sha256 = _digest_material("observation", current_observation)
                    return action, {
                        "model_runtime_invoked": True,
                        "repair_instruction_sha256": delivery_evidence[
                            "repair_instruction_payload_sha256"
                        ],
                        **delivery_evidence,
                        "policy_request_sha256": request_sha256,
                        "policy_response_sha256": _digest_material(
                            "policy_response",
                            {"action": action, "info": info},
                        ),
                    }

                def apply_action_chunk(action, chunk_index):
                    step_trace_env.begin_chunk(chunk_index)
                    try:
                        next_observation, _, _, _, _ = vector_env.step(action)
                    except Exception:
                        step_trace_env.abort_chunk()
                        raise
                    step_trace = step_trace_env.finish_chunk()
                    next_sha256 = _digest_material("observation", next_observation)
                    return next_observation, {
                        "simulator_step_return_observed": True,
                        "simulator_effect_observed": (next_sha256 != last_observation_sha256),
                        "action_chunk_sha256": _digest_material("action_chunk", action),
                        "official_predicate_result": bool(raw_env._env.check_success()),
                        "preservation_step_trace": step_trace,
                    }

                repair_result = _phase_call(
                    "repair_execution",
                    lambda: run_same_world_repair(
                        proposal=proposal,
                        approval=approval,
                        dispatch=dispatch,
                        dispatch_ledger=DispatchAuthorityTable(dispatch_state_path),
                        initial_observation=observation,
                        invoke_model=invoke_model,
                        apply_action_chunk=apply_action_chunk,
                        observe_goal_predicates=lambda: _predicate_material(raw_env),
                        observed_reset_count=lambda: counting_env.reset_count,
                        observed_state_continuity_basis=diagnostic_evidence[
                            "state_continuity_basis"
                        ],
                    ),
                    diagnostic_evidence=diagnostic_evidence,
                )
                final_report = {
                    "schema_version": ("missionos_groot_n17_libero_same_world_repair_live.v3"),
                    "recorded_at": _utc_now(),
                    "result": "repair_executed",
                    "source_revisions": revisions,
                    "runtime": runtime,
                    "environment": LIBERO_PANDA_SCENE8_ENVIRONMENT,
                    "process_seed": process_seed,
                    "source_contract_sha256": source_contract_sha256,
                    "source_steps_executed": source_steps_executed,
                    "source_chunk_evidence": source_chunk_evidence,
                    "source_goal_predicate_observations": source_vector,
                    "source_boundary": source_boundary,
                    "repair_proposal": proposal,
                    "repair_approval": approval,
                    "repair_dispatch": dispatch,
                    "repair_instruction_delivery_preflight": instruction_delivery_preflight,
                    "repair_result": repair_result,
                    "claim_boundary": {
                        "semantic_repair_established": _semantic_repair_established(
                            proposal=proposal,
                            repair_result=repair_result,
                        ),
                        "task_completion_claimed": _task_completion_claimed(
                            proposal=proposal,
                            repair_result=repair_result,
                        ),
                        "predicate_improvement_observed": repair_result[
                            "predicate_improvement_observed"
                        ],
                        "diagnostic_state_clone_only": (
                            not proposal["semantic_repair_claim_eligible"]
                        ),
                        "operator_approval_reference_supplied": True,
                        "interactive_post_proposal_approval_observed": False,
                        "controller_ack_observed": False,
                        "physical_execution_invoked": False,
                        "real_world_safety_claimed": False,
                    },
                    "publication": _publication_boundary(),
                }
    finally:
        vector_env.close()

    if final_report is None:
        raise RuntimeError("same_world_repair_report_missing")
    final_report["elapsed_seconds"] = time.monotonic() - started_at
    final_report["environment_closed_after_evidence"] = True
    return final_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--reference-model-path", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dispatch-state-path", type=Path, required=True)
    parser.add_argument("--operator-approval-ref", required=True)
    parser.add_argument("--policy-client-host", default="127.0.0.1")
    parser.add_argument("--policy-client-port", type=int, default=5555)
    parser.add_argument("--process-seed", type=int, default=6)
    parser.add_argument(
        "--maximum-repair-chunks",
        type=int,
        default=DEFAULT_MAXIMUM_REPAIR_CHUNKS,
    )
    parser.add_argument("--maximum-elapsed-seconds", type=float, default=1500.0)
    parser.add_argument(
        "--repair-instruction-variant",
        choices=sorted(REPAIR_INSTRUCTION_VARIANTS),
        default=DEFAULT_REPAIR_INSTRUCTION_VARIANT,
        help=(
            "fixed Contract-bound instruction ablation: semantic_preserve (A), "
            "original_task (B), or short_target (C)"
        ),
    )
    parser.add_argument(
        "--preserved-object-max-displacement-metres",
        type=float,
        default=DEFAULT_PRESERVED_OBJECT_MAX_DISPLACEMENT_METRES,
        help=(
            "stop the Repair when a preserved object moves further than this "
            "from its approval-time pose while contact is observed. Bound into "
            "the Repair Contract digest."
        ),
    )
    parser.add_argument(
        "--frame-capture-dir",
        type=Path,
        help=(
            "write the per-step frames GR00T was shown to this directory. "
            "Diagnostic only: frames never gate the Repair loop and are excluded "
            "from the receipt digest."
        ),
    )
    parser.add_argument(
        "--diagnostic-handoff-state-out",
        type=Path,
        help=(
            "capture one eligible partial MuJoCo state as a diagnostic-only .npz; "
            "no approval or Repair dispatch is created"
        ),
    )
    parser.add_argument(
        "--diagnostic-handoff-state-in",
        type=Path,
        help=(
            "restore a diagnostic-only .npz for a controlled instruction ablation; "
            "the result can never establish Semantic Repair"
        ),
    )
    parser.add_argument(
        "--diagnostic-capture-only",
        action="store_true",
        help="required with --diagnostic-handoff-state-out",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if os.environ.get(OPT_IN_ENV) != "1":
        raise SystemExit(f"set {OPT_IN_ENV}=1 to authorize the live simulator run")
    if args.output.exists():
        raise SystemExit("refusing to overwrite an existing run artifact")
    if args.process_seed < 0:
        raise SystemExit("--process-seed must be non-negative")
    if args.maximum_repair_chunks <= 0:
        raise SystemExit("--maximum-repair-chunks must be positive")
    try:
        report = execute_same_world_repair(
            model_path=args.model_path.resolve(),
            reference_model_path=(
                args.reference_model_path.resolve()
                if args.reference_model_path is not None
                else args.model_path.resolve()
            ),
            policy_client_host=args.policy_client_host,
            policy_client_port=args.policy_client_port,
            operator_approval_ref=args.operator_approval_ref,
            dispatch_state_path=args.dispatch_state_path.resolve(),
            process_seed=args.process_seed,
            maximum_repair_chunks=args.maximum_repair_chunks,
            maximum_elapsed_seconds=args.maximum_elapsed_seconds,
            repair_instruction_variant=args.repair_instruction_variant,
            frame_capture_dir=(
                args.frame_capture_dir.resolve() if args.frame_capture_dir is not None else None
            ),
            diagnostic_handoff_state_out=(
                args.diagnostic_handoff_state_out.resolve()
                if args.diagnostic_handoff_state_out is not None
                else None
            ),
            diagnostic_handoff_state_in=(
                args.diagnostic_handoff_state_in.resolve()
                if args.diagnostic_handoff_state_in is not None
                else None
            ),
            diagnostic_capture_only=args.diagnostic_capture_only,
        )
    except Exception as error:
        report = {
            "schema_version": ("missionos_groot_n17_libero_same_world_repair_live_failure.v2"),
            "recorded_at": _utc_now(),
            "result": "failed",
            "failure": {
                "error_type": type(error).__name__,
                "underlying_error_type": getattr(error, "cause_type", None),
                "phase": getattr(error, "phase", "unclassified"),
                "error_message_included": False,
            },
            "diagnostic_evidence": getattr(error, "diagnostic_evidence", {}),
            "claim_boundary": {
                "semantic_repair_established": False,
                "task_completion_claimed": False,
                "controller_ack_observed": False,
                "physical_execution_invoked": False,
                "real_world_safety_claimed": False,
            },
            "publication": _publication_boundary(),
        }
        _write_report(args.output.resolve(), report)
        print(
            json.dumps(
                {
                    "schema_version": report["schema_version"],
                    "result": "failed",
                    "error_type": type(error).__name__,
                    "failure_phase": getattr(error, "phase", "unclassified"),
                    "semantic_repair_established": False,
                    "physical_execution_invoked": False,
                },
                sort_keys=True,
            )
        )
        raise SystemExit(1) from error
    _write_report(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "schema_version": report["schema_version"],
                "result": report["result"],
                "semantic_repair_established": report["claim_boundary"][
                    "semantic_repair_established"
                ],
                "task_completion_claimed": report["claim_boundary"].get(
                    "task_completion_claimed", False
                ),
                "controller_ack_observed": False,
                "physical_execution_invoked": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

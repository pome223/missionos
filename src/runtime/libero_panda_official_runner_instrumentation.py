"""Thin instrumentation for the pinned official LIBERO Panda rollout path.

The adapter leaves ``run_rollout_gymnasium_policy`` in control of policy
requests, vector-environment stepping, reset, and termination. It temporarily
wraps the policy and the environment returned by the official
``get_gym_env`` factory so MissionOS can retain the values that the standard
runner otherwise discards.

This module is not a policy, controller, interpolator, E-stop, or alternate
rollout implementation. It supports exactly one simulator environment and one
episode. The official path has no independent controller ACK; a synchronous
simulator-step return is kept as a separate observation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from types import MethodType
from typing import Any, Literal
from uuid import uuid4

from missionos_core import FrozenMissionContract, canonical_sha256

from .libero_panda_predicate_package import (
    LIBERO_ACTION_FIELDS,
    LIBERO_BASE_TRANSFORMATIONS,
    LIBERO_FOUR_DIMENSIONAL_PROJECTION,
    LIBERO_PANDA_EPISODE_RESULT_SCHEMA_VERSION,
    LIBERO_PANDA_PREDICATE_PACKAGE_SHA256,
    LIBEROPandaActionChunk,
    LIBEROPandaActionField,
    LIBEROPandaGoalPredicateObservation,
    LIBEROPandaPredicateContent,
    LIBEROPandaPredicateEvaluation,
    LIBEROPandaRunnerConfiguration,
    LIBEROPandaStepApplication,
    build_libero_panda_replay_contract,
    build_libero_panda_replay_input,
    evaluate_libero_panda_predicate,
    libero_panda_goal_predicate_specs,
    libero_panda_task_material,
)


Clock = Callable[[], str]
IdentityFactory = Callable[[], str]
ControllerProbe = Callable[[Any], "LIBEROPandaControllerRuntimeBinding"]

LIBERO_PANDA_INSTRUMENTATION_VERSION = "1"


class LIBEROPandaInstrumentationEvent(str, Enum):
    PREPARED_BEFORE_RESET = "prepared_before_reset"
    CONTROLLER_RUNTIME_BOUND = "controller_runtime_bound"
    RESET_OBSERVED = "reset_observed"
    POLICY_REQUEST_INVOKED = "policy_request_invoked"
    POLICY_RESPONSE_OBSERVED = "policy_response_observed"
    SIMULATOR_STEP_INPUT_OBSERVED = "simulator_step_input_observed"
    SIMULATOR_STEP_RETURN_OBSERVED = "simulator_step_return_observed"
    OFFICIAL_PREDICATE_OBSERVED = "official_predicate_observed"
    OFFICIAL_RUNNER_RESULT_OBSERVED = "official_runner_result_observed"


class LIBEROPandaInstrumentationRejectionReason(str, Enum):
    """Publication-safe rejection vocabulary for the live boundary."""

    INSTRUMENTATION_BOUNDARY_REJECTED = "libero_panda_instrumentation_boundary_rejected"
    OFFICIAL_ENVIRONMENT_CREATION_FAILED = "official_environment_creation_failed"
    CONTROLLER_RUNTIME_PROBE_FAILED = "controller_runtime_probe_failed"
    ENVIRONMENT_INSTRUMENTATION_FAILED = "environment_instrumentation_failed"
    ENVIRONMENT_RESET_FAILED = "environment_reset_failed"
    POLICY_CLIENT_RESET_FAILED = "policy_client_reset_failed"
    POLICY_REQUEST_FAILED = "policy_request_failed"
    SIMULATOR_STEP_FAILED = "simulator_step_failed"
    SIMULATOR_STEP_RETURN_INVALID = "simulator_step_return_invalid"
    OFFICIAL_RUNNER_FAILED = "official_runner_failed"
    ROLLOUT_DEADLINE_EXCEEDED = "rollout_deadline_exceeded"
    RUNTIME_DEPENDENCY_PROFILE_MISMATCH = "runtime_dependency_profile_mismatch"
    CONTRACT_BINDING_MISMATCH = "contract_binding_mismatch"
    POLICY_RESPONSE_SHAPE_INVALID = "policy_response_shape_invalid"
    POLICY_RESPONSE_ACTION_NOT_MAPPING = "policy_response_action_not_mapping"
    POLICY_RESPONSE_ACTION_FIELDS_MISSING = "policy_response_action_fields_missing"
    POLICY_RESPONSE_ACTION_HORIZON_INVALID = "policy_response_action_horizon_invalid"
    POLICY_RESPONSE_ACTION_FIELD_NOT_SCALAR = "policy_response_action_field_not_scalar"
    POLICY_RESPONSE_ACTION_FIELD_NOT_NUMERIC = "policy_response_action_field_not_numeric"
    POLICY_RESPONSE_ACTION_FIELD_NON_FINITE = "policy_response_action_field_non_finite"


_LIBERO_PANDA_INSTRUMENTATION_MATERIAL = {
    "version": LIBERO_PANDA_INSTRUMENTATION_VERSION,
    "predicate_package_sha256": LIBERO_PANDA_PREDICATE_PACKAGE_SHA256,
    "official_runner_owns": [
        "policy_request",
        "environment_reset",
        "simulator_step",
        "termination",
    ],
    "missionos_capture_points": [event.value for event in LIBEROPandaInstrumentationEvent],
    "contract_and_identity_order": [
        "contract_before_reset",
        "run_identity_before_reset",
        "episode_identity_before_first_policy_request",
    ],
    "policy_response_shape": "two_tuple_action_and_info",
    "policy_action_fields": list(LIBERO_ACTION_FIELDS),
    "full_policy_action_horizon": "bound_separately_from_consumed_steps",
    "action_transformations": [
        *LIBERO_BASE_TRANSFORMATIONS,
        LIBERO_FOUR_DIMENSIONAL_PROJECTION,
    ],
    "action_lineage": [
        "policy_action_chunk",
        "env_step_input",
        "simulator_step_return",
        "official_predicate_observation",
    ],
    "wrapper_behavior": "observe_and_forward_without_action_rewrite",
    "independent_controller_ack": False,
    "readback_satisfies_controller_ack": False,
    "safe_stop_effect_observed": False,
    "physical_execution_invoked": False,
    "rejection_vocabulary": [reason.value for reason in LIBEROPandaInstrumentationRejectionReason],
}
LIBERO_PANDA_INSTRUMENTATION_MATERIAL_SHA256 = canonical_sha256(
    _LIBERO_PANDA_INSTRUMENTATION_MATERIAL
)


class LIBEROPandaInstrumentationError(RuntimeError):
    """Fail closed with a content-safe reason separate from raw error text."""

    def __init__(
        self,
        message: str,
        *,
        rejection_reason: LIBEROPandaInstrumentationRejectionReason = (
            LIBEROPandaInstrumentationRejectionReason.INSTRUMENTATION_BOUNDARY_REJECTED
        ),
    ) -> None:
        super().__init__(message)
        self.rejection_reason = rejection_reason


@dataclass(frozen=True)
class LIBEROPandaControllerRuntimeBinding:
    """Runtime-observed controller configuration at environment creation."""

    controller_configuration_sha256: str
    action_dim: int


@dataclass(frozen=True)
class PreparedLIBEROPandaInstrumentedEpisode:
    """Contract and recorder prepared before the official environment resets."""

    contract: FrozenMissionContract
    runner_configuration: LIBEROPandaRunnerConfiguration
    run_identity: str
    episode_identity: str
    prepared_at: str
    recorder: "LIBEROPandaOfficialRunnerRecorder"


@dataclass(frozen=True)
class LIBEROPandaInstrumentedEpisodeResult:
    """Publication-safe derived result from one instrumented official rollout."""

    content: LIBEROPandaPredicateContent
    predicate_evaluation: LIBEROPandaPredicateEvaluation
    official_runner_result_sha256: str
    event_sequence: tuple[LIBEROPandaInstrumentationEvent, ...]
    reset_observation_sha256: str
    controller_ack_observed: Literal[False] = False
    readback_satisfies_controller_ack: Literal[False] = False
    safe_stop_effect_observed: Literal[False] = False
    parent_mission_completion_claimed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ("missionos_groot_n17_libero_panda_instrumented_episode.v3"),
            "run_identity": self.content.run_identity,
            "episode_identity": self.content.episode_identity,
            "contract_sha256": self.predicate_evaluation.contract_sha256,
            "observation_content_sha256": self.content.content_sha256,
            "raw_action_stream_manifest_sha256": (self.content.raw_action_stream_manifest_sha256),
            "env_step_input_stream_manifest_sha256": (
                self.content.env_step_input_stream_manifest_sha256
            ),
            "simulator_step_return_manifest_sha256": (
                self.content.simulator_step_return_manifest_sha256
            ),
            "official_runner_result_sha256": (self.official_runner_result_sha256),
            "reset_observation_sha256": self.reset_observation_sha256,
            "event_sequence": [event.value for event in self.event_sequence],
            "predicate_evaluation": self.predicate_evaluation.to_dict(),
            "controller_ack_observed": self.controller_ack_observed,
            "readback_satisfies_controller_ack": (self.readback_satisfies_controller_ack),
            "safe_stop_effect_observed": self.safe_stop_effect_observed,
            "parent_mission_completion_claimed": (self.parent_mission_completion_claimed),
            "physical_execution_invoked": self.physical_execution_invoked,
            "raw_action_values_included": False,
            "raw_simulator_observations_included": False,
        }


@dataclass
class _PendingStep:
    chunk_index: int
    chunk_step_index: int
    action_chunk_sha256: str
    transformation_names: tuple[str, ...]
    env_step_input: tuple[float, ...] | None = None
    simulator_step_return_sha256: str | None = None
    goal_predicate_observations: tuple[LIBEROPandaGoalPredicateObservation, ...] | None = None


class LIBEROPandaOfficialRunnerRecorder:
    """Mutable in-memory recorder used only for one official episode."""

    def __init__(
        self,
        *,
        contract: FrozenMissionContract,
        runner_configuration: LIBEROPandaRunnerConfiguration,
        run_identity: str,
        episode_identity: str,
        clock: Clock,
    ) -> None:
        self.contract = contract
        self.runner_configuration = runner_configuration
        self.run_identity = run_identity
        self.episode_identity = episode_identity
        self._clock = clock
        self._events: list[LIBEROPandaInstrumentationEvent] = [
            LIBEROPandaInstrumentationEvent.PREPARED_BEFORE_RESET
        ]
        self._controller_binding: LIBEROPandaControllerRuntimeBinding | None = None
        self._reset_observation_sha256 = ""
        self._reset_seen = False
        self._terminal_seen = False
        self._post_episode_reset_count = 0
        self._action_chunks: list[LIBEROPandaActionChunk] = []
        self._step_applications: list[LIBEROPandaStepApplication] = []
        self._chunk_step_counts: dict[int, int] = {}
        self._pending_step: _PendingStep | None = None
        self._last_observed_at = ""

    @property
    def event_sequence(self) -> tuple[LIBEROPandaInstrumentationEvent, ...]:
        return tuple(self._events)

    @property
    def reset_observation_sha256(self) -> str:
        return self._reset_observation_sha256

    def bind_controller_runtime(
        self,
        binding: LIBEROPandaControllerRuntimeBinding,
    ) -> None:
        if self._reset_seen:
            raise LIBEROPandaInstrumentationError(
                "controller runtime must be observed before reset"
            )
        if self._controller_binding is not None:
            raise LIBEROPandaInstrumentationError("controller runtime was bound more than once")
        if (
            binding.controller_configuration_sha256
            != self.runner_configuration.controller_configuration_sha256
        ):
            raise LIBEROPandaInstrumentationError(
                "runtime controller configuration does not match the frozen contract"
            )
        if (
            isinstance(binding.action_dim, bool)
            or binding.action_dim != self.runner_configuration.action_dim
        ):
            raise LIBEROPandaInstrumentationError(
                "runtime action_dim does not match the frozen contract"
            )
        self._controller_binding = binding
        self._events.append(LIBEROPandaInstrumentationEvent.CONTROLLER_RUNTIME_BOUND)

    def record_reset(self, reset_result: Any) -> None:
        if self._controller_binding is None:
            raise LIBEROPandaInstrumentationError("controller runtime is missing before reset")
        if self._reset_seen:
            if not self._step_applications:
                raise LIBEROPandaInstrumentationError(
                    "a second reset occurred before any simulator step"
                )
            self._post_episode_reset_count += 1
            return
        observation, _ = _require_result_tuple(reset_result, length=2)
        self._reset_observation_sha256 = _digest_material(
            "reset_observation",
            observation,
        )
        self._reset_seen = True
        self._last_observed_at = self._clock()
        self._events.append(LIBEROPandaInstrumentationEvent.RESET_OBSERVED)

    def record_policy_response(
        self,
        *,
        observations: Any,
        response: Any,
    ) -> None:
        if not self._reset_seen or self._terminal_seen or self._post_episode_reset_count:
            raise LIBEROPandaInstrumentationError(
                "policy response was observed outside the active episode"
            )
        action, info = _require_result_tuple(
            response,
            length=2,
            rejection_reason=(
                LIBEROPandaInstrumentationRejectionReason.POLICY_RESPONSE_SHAPE_INVALID
            ),
        )
        if not isinstance(action, Mapping):
            raise LIBEROPandaInstrumentationError(
                "policy response action must be a mapping",
                rejection_reason=(
                    LIBEROPandaInstrumentationRejectionReason.POLICY_RESPONSE_ACTION_NOT_MAPPING
                ),
            )
        missing_fields = tuple(
            field_name for field_name in LIBERO_ACTION_FIELDS if field_name not in action
        )
        if missing_fields:
            raise LIBEROPandaInstrumentationError(
                "policy response is missing required namespaced action fields",
                rejection_reason=(
                    LIBEROPandaInstrumentationRejectionReason.POLICY_RESPONSE_ACTION_FIELDS_MISSING
                ),
            )
        chunk_index = len(self._action_chunks)
        fields = tuple(
            LIBEROPandaActionField(
                field_name=field_name,
                values=_extract_single_env_action_values(
                    action.get(field_name),
                    expected_steps=(self.runner_configuration.policy_action_horizon),
                    field_name=field_name,
                ),
            )
            for field_name in LIBERO_ACTION_FIELDS
        )
        chunk = LIBEROPandaActionChunk(
            chunk_index=chunk_index,
            policy_request_sha256=_digest_material(
                "policy_request_observations",
                observations,
            ),
            policy_response_sha256=_digest_material(
                "policy_response",
                {"action": action, "info": info},
            ),
            fields=fields,
        )
        self._action_chunks.append(chunk)
        self._chunk_step_counts[chunk_index] = 0
        self._last_observed_at = self._clock()
        self._events.append(LIBEROPandaInstrumentationEvent.POLICY_RESPONSE_OBSERVED)

    def record_policy_request_invoked(self) -> None:
        if not self._reset_seen or self._terminal_seen or self._post_episode_reset_count:
            raise LIBEROPandaInstrumentationError(
                "policy request was invoked outside the active episode"
            )
        self._events.append(LIBEROPandaInstrumentationEvent.POLICY_REQUEST_INVOKED)

    def begin_simulator_step(self, action: Any) -> None:
        if self._pending_step is not None:
            raise LIBEROPandaInstrumentationError(
                "simulator step began before the prior step completed"
            )
        if not self._action_chunks:
            raise LIBEROPandaInstrumentationError(
                "simulator step occurred before a policy response"
            )
        if not isinstance(action, Mapping):
            raise LIBEROPandaInstrumentationError("per-step action must be a mapping")
        chunk = self._action_chunks[-1]
        chunk_step_index = self._chunk_step_counts[chunk.chunk_index]
        if chunk_step_index >= self.runner_configuration.n_action_steps:
            raise LIBEROPandaInstrumentationError(
                "simulator consumed more steps than the frozen chunk horizon"
            )
        expected = {field.field_name: field.values[chunk_step_index] for field in chunk.fields}
        observed = {
            field_name: _single_float(action.get(field_name), field_name)
            for field_name in LIBERO_ACTION_FIELDS
        }
        if canonical_sha256({"action": observed}) != canonical_sha256({"action": expected}):
            raise LIBEROPandaInstrumentationError(
                "MultiStepWrapper slice does not match the recorded chunk"
            )
        transformations = list(LIBERO_BASE_TRANSFORMATIONS)
        if self._controller_binding is None:
            raise LIBEROPandaInstrumentationError("controller runtime is not bound")
        if self._controller_binding.action_dim == 4:
            transformations.append(LIBERO_FOUR_DIMENSIONAL_PROJECTION)
        self._pending_step = _PendingStep(
            chunk_index=chunk.chunk_index,
            chunk_step_index=chunk_step_index,
            action_chunk_sha256=chunk.action_chunk_sha256,
            transformation_names=tuple(transformations),
        )

    def record_underlying_step_input(self, action_vector: Any) -> None:
        pending = self._require_pending_step()
        if pending.env_step_input is not None:
            raise LIBEROPandaInstrumentationError(
                "underlying simulator input was recorded more than once"
            )
        vector = _float_vector(action_vector)
        expected_dim = self.runner_configuration.action_dim
        if len(vector) != expected_dim:
            raise LIBEROPandaInstrumentationError(
                "underlying simulator input dimension does not match the runtime controller"
            )
        pending.env_step_input = vector
        self._events.append(LIBEROPandaInstrumentationEvent.SIMULATOR_STEP_INPUT_OBSERVED)

    def record_underlying_step_return(self, step_return: Any) -> None:
        pending = self._require_pending_step()
        if pending.env_step_input is None:
            raise LIBEROPandaInstrumentationError(
                "simulator returned before its input was recorded"
            )
        if pending.simulator_step_return_sha256 is not None:
            raise LIBEROPandaInstrumentationError(
                "underlying simulator return was recorded more than once"
            )
        try:
            _require_result_tuple(step_return, length=4)
            pending.simulator_step_return_sha256 = _digest_material(
                "underlying_simulator_step_return",
                step_return,
            )
        except LIBEROPandaInstrumentationError as error:
            raise LIBEROPandaInstrumentationError(
                "underlying simulator step return is invalid",
                rejection_reason=(
                    LIBEROPandaInstrumentationRejectionReason.SIMULATOR_STEP_RETURN_INVALID
                ),
            ) from error
        self._events.append(LIBEROPandaInstrumentationEvent.SIMULATOR_STEP_RETURN_OBSERVED)

    def record_goal_predicate_observations(
        self,
        observations: tuple[LIBEROPandaGoalPredicateObservation, ...],
    ) -> None:
        pending = self._require_pending_step()
        if pending.goal_predicate_observations is not None:
            raise LIBEROPandaInstrumentationError(
                "goal predicate vector was recorded more than once"
            )
        pending.goal_predicate_observations = observations

    def finish_simulator_step(self, step_result: Any) -> None:
        pending = self._require_pending_step()
        if (
            pending.env_step_input is None
            or pending.simulator_step_return_sha256 is None
            or pending.goal_predicate_observations is None
        ):
            raise LIBEROPandaInstrumentationError("simulator step lineage is incomplete")
        observation, _, terminated, truncated, info = _require_result_tuple(
            step_result,
            length=5,
        )
        if not isinstance(info, Mapping) or "success" not in info:
            raise LIBEROPandaInstrumentationError("official post-step success result is missing")
        predicate_result = _strict_bool(info["success"], "success")
        terminated_value = _strict_bool(terminated, "terminated")
        truncated_value = _strict_bool(truncated, "truncated")
        self._step_applications.append(
            LIBEROPandaStepApplication(
                global_step_index=len(self._step_applications),
                chunk_index=pending.chunk_index,
                chunk_step_index=pending.chunk_step_index,
                action_chunk_sha256=pending.action_chunk_sha256,
                transformation_names=pending.transformation_names,
                env_step_input=pending.env_step_input,
                simulator_step_return_sha256=(pending.simulator_step_return_sha256),
                result_observation_sha256=_digest_material(
                    "processed_result_observation",
                    observation,
                ),
                goal_predicate_observations=(pending.goal_predicate_observations),
                official_predicate_result=predicate_result,
                terminated=terminated_value,
                truncated=truncated_value,
            )
        )
        self._chunk_step_counts[pending.chunk_index] += 1
        self._pending_step = None
        self._terminal_seen = predicate_result or terminated_value or truncated_value
        self._last_observed_at = self._clock()
        self._events.append(LIBEROPandaInstrumentationEvent.OFFICIAL_PREDICATE_OBSERVED)

    def finalize(
        self,
        *,
        official_runner_result: Any,
    ) -> LIBEROPandaInstrumentedEpisodeResult:
        if self._pending_step is not None:
            raise LIBEROPandaInstrumentationError(
                "official runner returned with an incomplete simulator step"
            )
        if self._controller_binding is None or not self._reset_seen:
            raise LIBEROPandaInstrumentationError(
                "official runner returned before instrumentation was ready"
            )
        env_name, episode_successes, _ = _require_result_tuple(
            official_runner_result,
            length=3,
        )
        if env_name != self.runner_configuration.environment:
            raise LIBEROPandaInstrumentationError(
                "official runner returned a different environment"
            )
        successes = _sequence_values(episode_successes)
        if len(successes) != 1:
            raise LIBEROPandaInstrumentationError(
                "official runner did not return exactly one episode"
            )
        runner_success = _strict_bool(
            successes[0],
            "official_runner_episode_success",
        )
        received_at = self._clock()
        self._events.append(LIBEROPandaInstrumentationEvent.OFFICIAL_RUNNER_RESULT_OBSERVED)
        content = LIBEROPandaPredicateContent(
            source_schema_version=(LIBERO_PANDA_EPISODE_RESULT_SCHEMA_VERSION),
            run_identity=self.run_identity,
            episode_identity=self.episode_identity,
            runner_configuration=self.runner_configuration,
            runtime_controller_configuration_sha256=(
                self._controller_binding.controller_configuration_sha256
            ),
            runtime_action_dim=self._controller_binding.action_dim,
            task_predicate_sha256=libero_panda_task_material(self.runner_configuration.environment)[
                "task_predicate_sha256"
            ],
            action_chunks=tuple(self._action_chunks),
            step_applications=tuple(self._step_applications),
            official_runner_episode_ended=True,
            official_runner_episode_success=runner_success,
            observed_at=self._last_observed_at,
            received_at=received_at,
        )
        evaluated_at = self._clock()
        predicate_evaluation = evaluate_libero_panda_predicate(
            contract=self.contract,
            replay=build_libero_panda_replay_input(
                contract=self.contract,
                content=content,
            ),
            evaluated_at=evaluated_at,
        )
        return LIBEROPandaInstrumentedEpisodeResult(
            content=content,
            predicate_evaluation=predicate_evaluation,
            official_runner_result_sha256=_digest_material(
                "official_runner_result",
                official_runner_result,
            ),
            event_sequence=self.event_sequence,
            reset_observation_sha256=self._reset_observation_sha256,
        )

    def _require_pending_step(self) -> _PendingStep:
        if self._pending_step is None:
            raise LIBEROPandaInstrumentationError("no simulator step is currently active")
        return self._pending_step


class _InstrumentedPolicy:
    def __init__(
        self,
        *,
        policy: Any,
        recorder: LIBEROPandaOfficialRunnerRecorder,
    ) -> None:
        self._policy = policy
        self._recorder = recorder

    def get_action(self, observations: Any) -> Any:
        self._recorder.record_policy_request_invoked()
        try:
            response = self._policy.get_action(observations)
        except LIBEROPandaInstrumentationError:
            raise
        except Exception as error:
            raise LIBEROPandaInstrumentationError(
                "official policy request failed",
                rejection_reason=(LIBEROPandaInstrumentationRejectionReason.POLICY_REQUEST_FAILED),
            ) from error
        self._recorder.record_policy_response(
            observations=observations,
            response=response,
        )
        return response

    def reset(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._policy.reset(*args, **kwargs)
        except LIBEROPandaInstrumentationError:
            raise
        except Exception as error:
            raise LIBEROPandaInstrumentationError(
                "official policy client reset failed",
                rejection_reason=(
                    LIBEROPandaInstrumentationRejectionReason.POLICY_CLIENT_RESET_FAILED
                ),
            ) from error

    def __getattr__(self, name: str) -> Any:
        return getattr(self._policy, name)


class _UnderlyingSimulatorStepProxy:
    def __init__(
        self,
        *,
        underlying: Any,
        recorder: LIBEROPandaOfficialRunnerRecorder,
    ) -> None:
        self._underlying = underlying
        self._recorder = recorder

    def step(self, action_vector: Any) -> Any:
        self._recorder.record_underlying_step_input(action_vector)
        try:
            result = self._underlying.step(action_vector)
        except LIBEROPandaInstrumentationError:
            raise
        except Exception as error:
            raise LIBEROPandaInstrumentationError(
                "underlying simulator step failed",
                rejection_reason=(LIBEROPandaInstrumentationRejectionReason.SIMULATOR_STEP_FAILED),
            ) from error
        self._recorder.record_underlying_step_return(result)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._underlying, name)


def prepare_libero_panda_instrumented_episode(
    *,
    runner_configuration: LIBEROPandaRunnerConfiguration,
    maximum_observation_age_seconds: float,
    clock: Clock | None = None,
    identity_factory: IdentityFactory | None = None,
    run_identity: str | None = None,
    episode_identity: str | None = None,
) -> PreparedLIBEROPandaInstrumentedEpisode:
    """Create identities and freeze the child contract before environment reset."""

    clock = clock or _utc_now
    if (run_identity is None) != (episode_identity is None):
        raise ValueError("run_identity and episode_identity must be supplied together")
    if run_identity is None:
        identity_factory = identity_factory or (lambda: str(uuid4()))
        run_identity = f"missionos-libero-panda-run:{identity_factory()}"
        episode_identity = f"{run_identity}:episode:{identity_factory()}"
    if not str(run_identity or "").strip():
        raise ValueError("run_identity must not be empty")
    if not str(episode_identity or "").strip():
        raise ValueError("episode_identity must not be empty")
    prepared_at = clock()
    contract = build_libero_panda_replay_contract(
        contract_id=f"libero-panda-contract:{episode_identity}",
        contract_version="v1",
        runner_configuration=runner_configuration,
        run_identity=run_identity,
        episode_identity=episode_identity,
        maximum_observation_age_seconds=maximum_observation_age_seconds,
    )
    recorder = LIBEROPandaOfficialRunnerRecorder(
        contract=contract,
        runner_configuration=runner_configuration,
        run_identity=run_identity,
        episode_identity=episode_identity,
        clock=clock,
    )
    return PreparedLIBEROPandaInstrumentedEpisode(
        contract=contract,
        runner_configuration=runner_configuration,
        run_identity=run_identity,
        episode_identity=episode_identity,
        prepared_at=prepared_at,
        recorder=recorder,
    )


def run_instrumented_official_libero_rollout(
    *,
    rollout_module: Any,
    policy: Any,
    wrapper_configs: Any,
    prepared: PreparedLIBEROPandaInstrumentedEpisode,
    controller_probe: ControllerProbe,
) -> LIBEROPandaInstrumentedEpisodeResult:
    """Run the unchanged official rollout with temporary observation wrappers."""

    official_runner = getattr(
        rollout_module,
        "run_rollout_gymnasium_policy",
        None,
    )
    original_get_gym_env = getattr(rollout_module, "get_gym_env", None)
    if not callable(official_runner) or not callable(original_get_gym_env):
        raise LIBEROPandaInstrumentationError("pinned official rollout module API is unavailable")
    if prepared.runner_configuration.n_envs != 1:
        raise LIBEROPandaInstrumentationError("first governed rollout requires n_envs=1")
    env_created = False

    def instrumented_get_gym_env(
        env_name: str,
        env_idx: int,
        total_n_envs: int,
    ) -> Any:
        nonlocal env_created
        if (
            env_created
            or env_name != prepared.runner_configuration.environment
            or env_idx != 0
            or total_n_envs != 1
        ):
            raise LIBEROPandaInstrumentationError(
                "official runner requested an unexpected environment"
            )
        try:
            env = original_get_gym_env(env_name, env_idx, total_n_envs)
        except LIBEROPandaInstrumentationError:
            raise
        except Exception as error:
            raise LIBEROPandaInstrumentationError(
                "official LIBERO environment creation failed",
                rejection_reason=(
                    LIBEROPandaInstrumentationRejectionReason.OFFICIAL_ENVIRONMENT_CREATION_FAILED
                ),
            ) from error
        base_env = getattr(env, "unwrapped", env)
        try:
            binding = controller_probe(base_env)
        except LIBEROPandaInstrumentationError:
            raise
        except Exception as error:
            raise LIBEROPandaInstrumentationError(
                "live controller runtime probe failed",
                rejection_reason=(
                    LIBEROPandaInstrumentationRejectionReason.CONTROLLER_RUNTIME_PROBE_FAILED
                ),
            ) from error
        prepared.recorder.bind_controller_runtime(binding)
        try:
            _instrument_libero_environment(
                env=base_env,
                recorder=prepared.recorder,
            )
        except LIBEROPandaInstrumentationError:
            raise
        except Exception as error:
            raise LIBEROPandaInstrumentationError(
                "LIBERO environment instrumentation failed",
                rejection_reason=(
                    LIBEROPandaInstrumentationRejectionReason.ENVIRONMENT_INSTRUMENTATION_FAILED
                ),
            ) from error
        env_created = True
        return env

    instrumented_policy = _InstrumentedPolicy(
        policy=policy,
        recorder=prepared.recorder,
    )
    rollout_module.get_gym_env = instrumented_get_gym_env
    try:
        try:
            official_result = official_runner(
                env_name=prepared.runner_configuration.environment,
                policy=instrumented_policy,
                wrapper_configs=wrapper_configs,
                n_episodes=1,
                n_envs=1,
            )
        except LIBEROPandaInstrumentationError:
            raise
        except Exception as error:
            raise LIBEROPandaInstrumentationError(
                "official LIBERO runner failed outside a typed boundary",
                rejection_reason=(LIBEROPandaInstrumentationRejectionReason.OFFICIAL_RUNNER_FAILED),
            ) from error
    finally:
        rollout_module.get_gym_env = original_get_gym_env
    if not env_created:
        raise LIBEROPandaInstrumentationError(
            "official runner did not create the governed environment"
        )
    return prepared.recorder.finalize(
        official_runner_result=official_result,
    )


def _instrument_libero_environment(
    *,
    env: Any,
    recorder: LIBEROPandaOfficialRunnerRecorder,
) -> None:
    original_underlying = getattr(env, "_env", None)
    original_reset = getattr(env, "reset", None)
    original_step = getattr(env, "step", None)
    if original_underlying is None or not callable(original_reset) or not callable(original_step):
        raise LIBEROPandaInstrumentationError("pinned LIBERO environment API is unavailable")
    env._env = _UnderlyingSimulatorStepProxy(
        underlying=original_underlying,
        recorder=recorder,
    )

    def instrumented_reset(
        self: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        try:
            result = original_reset(*args, **kwargs)
        except LIBEROPandaInstrumentationError:
            raise
        except Exception as error:
            raise LIBEROPandaInstrumentationError(
                "LIBERO environment reset failed",
                rejection_reason=(
                    LIBEROPandaInstrumentationRejectionReason.ENVIRONMENT_RESET_FAILED
                ),
            ) from error
        recorder.record_reset(result)
        return result

    def instrumented_step(self: Any, action: Any) -> Any:
        recorder.begin_simulator_step(action)
        try:
            result = original_step(action)
        except LIBEROPandaInstrumentationError:
            raise
        except Exception as error:
            raise LIBEROPandaInstrumentationError(
                "LIBERO environment step failed",
                rejection_reason=(LIBEROPandaInstrumentationRejectionReason.SIMULATOR_STEP_FAILED),
            ) from error
        recorder.record_goal_predicate_observations(
            _observe_libero_goal_predicates(
                original_underlying,
                environment=recorder.runner_configuration.environment,
            )
        )
        recorder.finish_simulator_step(result)
        return result

    env.reset = MethodType(instrumented_reset, env)
    env.step = MethodType(instrumented_step, env)


def _observe_libero_goal_predicates(
    underlying: Any,
    *,
    environment: str,
) -> tuple[LIBEROPandaGoalPredicateObservation, ...]:
    expected = libero_panda_goal_predicate_specs(environment)
    fixture_probe = getattr(
        underlying,
        "missionos_goal_predicate_observations",
        None,
    )
    if callable(fixture_probe):
        observed = fixture_probe(expected)
        if not isinstance(observed, (list, tuple)):
            raise LIBEROPandaInstrumentationError(
                "fixture goal predicate probe returned an invalid vector"
            )
        values = tuple(observed)
        if len(values) != len(expected) or any(not isinstance(value, bool) for value in values):
            raise LIBEROPandaInstrumentationError(
                "fixture goal predicate probe returned invalid values"
            )
    else:
        task_env = getattr(underlying, "env", None)
        parsed_problem = getattr(task_env, "parsed_problem", None)
        evaluator = getattr(task_env, "_eval_predicate", None)
        if not isinstance(parsed_problem, Mapping) or not callable(evaluator):
            raise LIBEROPandaInstrumentationError("pinned LIBERO goal predicate API is unavailable")
        goal_state = parsed_problem.get("goal_state")
        if not isinstance(goal_state, (list, tuple)):
            raise LIBEROPandaInstrumentationError("pinned LIBERO goal state is unavailable")
        observed_specs = tuple(
            tuple(str(part).casefold() for part in state)
            for state in goal_state
            if isinstance(state, (list, tuple))
        )
        if observed_specs != expected:
            raise LIBEROPandaInstrumentationError(
                "live LIBERO goal state does not match the frozen task"
            )
        values = tuple(
            _strict_bool(evaluator(state), f"goal_predicate_{index}")
            for index, state in enumerate(goal_state)
        )
    return tuple(
        LIBEROPandaGoalPredicateObservation(
            predicate_index=index,
            predicate_name=spec[0],
            arguments=spec[1:],
            satisfied=values[index],
        )
        for index, spec in enumerate(expected)
    )


def _extract_single_env_action_values(
    value: Any,
    *,
    expected_steps: int,
    field_name: str,
) -> tuple[float, ...]:
    material = _sequence_values(value)
    if (
        len(material) == 1
        and _is_sequence_like(material[0])
        and len(_sequence_values(material[0])) == expected_steps
    ):
        material = _sequence_values(material[0])
    if len(material) != expected_steps:
        raise LIBEROPandaInstrumentationError(
            f"policy action horizon is invalid for {field_name}",
            rejection_reason=(
                LIBEROPandaInstrumentationRejectionReason.POLICY_RESPONSE_ACTION_HORIZON_INVALID
            ),
        )
    return tuple(_single_float(item, field_name, policy_response=True) for item in material)


def _single_float(
    value: Any,
    field_name: str,
    *,
    policy_response: bool = False,
) -> float:
    material = value
    if _is_sequence_like(material):
        values = _sequence_values(material)
        if len(values) != 1:
            raise LIBEROPandaInstrumentationError(
                f"policy action field is not scalar: {field_name}",
                rejection_reason=(
                    LIBEROPandaInstrumentationRejectionReason.POLICY_RESPONSE_ACTION_FIELD_NOT_SCALAR
                    if policy_response
                    else LIBEROPandaInstrumentationRejectionReason.INSTRUMENTATION_BOUNDARY_REJECTED
                ),
            )
        material = values[0]
    if hasattr(material, "item") and callable(material.item):
        material = material.item()
    if isinstance(material, bool) or not isinstance(material, (int, float)):
        raise LIBEROPandaInstrumentationError(
            f"policy action field is not numeric: {field_name}",
            rejection_reason=(
                LIBEROPandaInstrumentationRejectionReason.POLICY_RESPONSE_ACTION_FIELD_NOT_NUMERIC
                if policy_response
                else LIBEROPandaInstrumentationRejectionReason.INSTRUMENTATION_BOUNDARY_REJECTED
            ),
        )
    number = float(material)
    if number != number or number in (float("inf"), float("-inf")):
        raise LIBEROPandaInstrumentationError(
            f"policy action field is non-finite: {field_name}",
            rejection_reason=(
                LIBEROPandaInstrumentationRejectionReason.POLICY_RESPONSE_ACTION_FIELD_NON_FINITE
                if policy_response
                else LIBEROPandaInstrumentationRejectionReason.INSTRUMENTATION_BOUNDARY_REJECTED
            ),
        )
    return number


def _float_vector(value: Any) -> tuple[float, ...]:
    return tuple(
        _single_float(item, "underlying_simulator_input") for item in _sequence_values(value)
    )


def _strict_bool(value: Any, field_name: str) -> bool:
    material = value
    if hasattr(material, "item") and callable(material.item):
        material = material.item()
    if not isinstance(material, bool):
        raise LIBEROPandaInstrumentationError(f"{field_name} is not a boolean")
    return material


def _require_result_tuple(
    value: Any,
    *,
    length: int,
    rejection_reason: LIBEROPandaInstrumentationRejectionReason = (
        LIBEROPandaInstrumentationRejectionReason.INSTRUMENTATION_BOUNDARY_REJECTED
    ),
) -> tuple[Any, ...]:
    if not isinstance(value, tuple) or len(value) != length:
        raise LIBEROPandaInstrumentationError(
            f"official runner boundary expected a {length}-tuple",
            rejection_reason=rejection_reason,
        )
    return value


def _digest_material(label: str, value: Any) -> str:
    return canonical_sha256({label: _json_material(value)})


def digest_runtime_material(label: str, value: Any) -> str:
    """Content-bind runtime material through the supported public boundary."""

    return _digest_material(label, value)


def _json_material(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise LIBEROPandaInstrumentationError("non-finite value cannot be content-bound")
        return value
    if isinstance(value, Enum):
        return _json_material(value.value)
    array_material = _binary_array_material(value)
    if array_material is not None:
        return array_material
    if isinstance(value, Mapping):
        return {str(key): _json_material(item) for key, item in value.items()}
    if _is_sequence_like(value):
        return [_json_material(item) for item in _sequence_values(value)]
    if hasattr(value, "item") and callable(value.item):
        return _json_material(value.item())
    if hasattr(value, "tolist") and callable(value.tolist):
        return _json_material(value.tolist())
    raise LIBEROPandaInstrumentationError(
        f"unsupported evidence material type: {type(value).__name__}"
    )


def _binary_array_material(value: Any) -> dict[str, Any] | None:
    """Content-bind a non-scalar numeric array without JSON-expanding it."""

    shape = getattr(value, "shape", None)
    dtype = getattr(value, "dtype", None)
    tobytes = getattr(value, "tobytes", None)
    if shape is None or dtype is None or not callable(tobytes):
        return None
    try:
        dimensions = tuple(int(dimension) for dimension in shape)
    except (TypeError, ValueError):
        return None
    if not dimensions:
        return None
    if any(dimension < 0 for dimension in dimensions):
        raise LIBEROPandaInstrumentationError("array shape cannot contain a negative dimension")
    if bool(getattr(dtype, "hasobject", False)):
        raise LIBEROPandaInstrumentationError("object arrays cannot be content-bound")
    try:
        payload = tobytes(order="C")
    except TypeError:
        payload = tobytes()
    if not isinstance(payload, bytes):
        raise LIBEROPandaInstrumentationError("array byte representation is invalid")
    return {
        "array_dtype": str(dtype),
        "array_shape": list(dimensions),
        "array_content_sha256": sha256(payload).hexdigest(),
    }


def _is_sequence_like(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        return True
    if isinstance(value, (str, bytes, bytearray)):
        return False
    tolist = getattr(value, "tolist", None)
    if not callable(tolist):
        return False
    return isinstance(tolist(), (list, tuple))


def _sequence_values(value: Any) -> list[Any]:
    material = value.tolist() if hasattr(value, "tolist") else value
    if not isinstance(material, (list, tuple)):
        raise LIBEROPandaInstrumentationError("expected a sequence")
    return list(material)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "LIBERO_PANDA_INSTRUMENTATION_MATERIAL_SHA256",
    "LIBERO_PANDA_INSTRUMENTATION_VERSION",
    "LIBEROPandaControllerRuntimeBinding",
    "LIBEROPandaInstrumentationError",
    "LIBEROPandaInstrumentationEvent",
    "LIBEROPandaInstrumentationRejectionReason",
    "LIBEROPandaInstrumentedEpisodeResult",
    "LIBEROPandaOfficialRunnerRecorder",
    "PreparedLIBEROPandaInstrumentedEpisode",
    "digest_runtime_material",
    "prepare_libero_panda_instrumented_episode",
    "run_instrumented_official_libero_rollout",
]

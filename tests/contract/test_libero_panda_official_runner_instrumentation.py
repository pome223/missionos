from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import numpy as np

from missionos_core import canonical_sha256
from src.runtime.libero_panda_official_runner_instrumentation import (
    LIBEROPandaControllerRuntimeBinding,
    LIBEROPandaInstrumentationError,
    LIBEROPandaInstrumentationEvent,
    LIBEROPandaInstrumentationRejectionReason,
    _digest_material,
    prepare_libero_panda_instrumented_episode,
    run_instrumented_official_libero_rollout,
)
from src.runtime.libero_panda_predicate_package import (
    GROOT_CHECKPOINT_REPOSITORY,
    GROOT_CHECKPOINT_REVISION,
    ISAAC_GROOT_REVISION,
    LIBERO_ACTION_FIELDS,
    LIBERO_PANDA_EMBODIMENT_TAG,
    LIBERO_PANDA_ENVIRONMENT,
    LIBERO_POLICY_ACTION_HORIZON,
    LIBERO_REVISION,
    LIBEROPandaPredicateStatus,
    LIBEROPandaRunnerConfiguration,
)


class _Clock:
    def __init__(self) -> None:
        self._value = datetime(2026, 7, 31, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self._value
        self._value += timedelta(milliseconds=10)
        return value.isoformat()


class _IdentityFactory:
    def __init__(self) -> None:
        self._values = iter(("run-identity", "episode-identity"))

    def __call__(self) -> str:
        return next(self._values)


@dataclass
class _FakeUnderlyingTaskEnvironment:
    action_dim: int
    success: bool
    step_calls: list[tuple[float, ...]]
    numpy_scalar_step_return: bool = False
    invalid_step_return: bool = False

    def reset(self) -> dict[str, list[float]]:
        return {"raw_state": [0.0]}

    def step(
        self,
        action_vector: list[float],
    ) -> tuple[dict[str, list[float]], float, bool, dict[str, Any]]:
        vector = tuple(float(value) for value in action_vector)
        self.step_calls.append(vector)
        if self.invalid_step_return:
            return (
                {"raw_state": object()},
                0.0,
                self.success,
                {"underlying": True},
            )
        if self.numpy_scalar_step_return:
            return (
                OrderedDict((("raw_state", np.asarray(vector, dtype=np.float32)),)),
                np.float32(0.0),
                np.bool_(self.success),
                {"underlying": np.int64(1)},
            )
        return (
            {"raw_state": list(vector)},
            0.0,
            self.success,
            {"underlying": True},
        )

    def check_success(self) -> bool:
        return self.success

    def missionos_goal_predicate_observations(
        self,
        expected: tuple[tuple[str, ...], ...],
    ) -> tuple[bool, ...]:
        return tuple(self.success for _ in expected)

    def close(self) -> None:
        return None


class _FakeLiberoEnvironment:
    def __init__(
        self,
        *,
        action_dim: int,
        success: bool,
        numpy_scalar_step_return: bool = False,
        invalid_step_return: bool = False,
    ) -> None:
        self._env = _FakeUnderlyingTaskEnvironment(
            action_dim=action_dim,
            success=success,
            step_calls=[],
            numpy_scalar_step_return=numpy_scalar_step_return,
            invalid_step_return=invalid_step_return,
        )
        self.reset_calls = 0

    def reset(
        self,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[dict[str, list[float]], dict[str, bool]]:
        del seed, options
        self.reset_calls += 1
        observation = self._env.reset()
        return observation, {"success": self._env.check_success()}

    def step(
        self,
        action: dict[str, list[float]],
    ) -> tuple[dict[str, list[float]], float, bool, bool, dict[str, Any]]:
        vector = [float(action[field_name][0]) for field_name in LIBERO_ACTION_FIELDS]
        vector[-1] = 2.0 * vector[-1] - 1.0
        vector[-1] = 1.0 if vector[-1] > 0 else -1.0 if vector[-1] < 0 else 0.0
        vector[-1] *= -1.0
        if self._env.action_dim == 4:
            vector = [*vector[:3], vector[-1]]
        observation, reward, done, info = self._env.step(vector)
        info["success"] = self._env.check_success()
        return observation, reward, done, False, info

    def close(self) -> None:
        self._env.close()


class _GymStyleEnvironmentWrapper:
    def __init__(self, env: _FakeLiberoEnvironment) -> None:
        self.unwrapped = env

    def reset(self, *args: Any, **kwargs: Any):
        return self.unwrapped.reset(*args, **kwargs)

    def step(self, action: Any):
        return self.unwrapped.step(action)

    def close(self) -> None:
        self.unwrapped.close()


class _FakePolicy:
    def __init__(self) -> None:
        self.calls = 0
        self.reset_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1

    def get_action(
        self,
        observations: dict[str, list[list[float]]],
    ) -> tuple[dict[str, list[list[list[float]]]], dict[str, str]]:
        assert observations
        self.calls += 1
        action = {
            field_name: [
                [
                    [0.75 if field_name == "action.gripper" else 0.1]
                    for _ in range(LIBERO_POLICY_ACTION_HORIZON)
                ]
            ][0]
            for field_name in LIBERO_ACTION_FIELDS
        }
        return action, {"transport": "fixture"}


class _MappingOnlyPolicy(_FakePolicy):
    def get_action(
        self,
        observations: dict[str, list[list[float]]],
    ) -> dict[str, list[list[float]]]:
        action, _ = super().get_action(observations)
        return action


class _UnnamespacedPolicy(_FakePolicy):
    def get_action(
        self,
        observations: dict[str, list[list[float]]],
    ) -> tuple[dict[str, list[list[float]]], dict[str, str]]:
        action, info = super().get_action(observations)
        return {
            field_name.removeprefix("action."): values for field_name, values in action.items()
        }, info


class _ExecutionHorizonOnlyPolicy(_FakePolicy):
    def get_action(
        self,
        observations: dict[str, list[list[float]]],
    ) -> tuple[dict[str, list[list[float]]], dict[str, str]]:
        action, info = super().get_action(observations)
        return {field_name: values[:8] for field_name, values in action.items()}, info


class _FakeOfficialRolloutModule:
    def __init__(
        self,
        *,
        action_dim: int = 7,
        success: bool = True,
        mutate_step_input: bool = False,
        runner_success: bool | None = None,
        raise_after_reset: bool = False,
        typed_failure_after_reset: LIBEROPandaInstrumentationError | None = None,
        gym_wrapped: bool = True,
        numpy_scalar_step_return: bool = False,
        invalid_step_return: bool = False,
    ) -> None:
        self.action_dim = action_dim
        self.success = success
        self.mutate_step_input = mutate_step_input
        self.runner_success = success if runner_success is None else runner_success
        self.raise_after_reset = raise_after_reset
        self.typed_failure_after_reset = typed_failure_after_reset
        self.gym_wrapped = gym_wrapped
        self.numpy_scalar_step_return = numpy_scalar_step_return
        self.invalid_step_return = invalid_step_return
        self.environments: list[_FakeLiberoEnvironment] = []

    def get_gym_env(
        self,
        env_name: str,
        env_idx: int,
        total_n_envs: int,
    ) -> _FakeLiberoEnvironment | _GymStyleEnvironmentWrapper:
        assert env_name == LIBERO_PANDA_ENVIRONMENT
        assert env_idx == 0
        assert total_n_envs == 1
        env = _FakeLiberoEnvironment(
            action_dim=self.action_dim,
            success=self.success,
            numpy_scalar_step_return=self.numpy_scalar_step_return,
            invalid_step_return=self.invalid_step_return,
        )
        self.environments.append(env)
        return _GymStyleEnvironmentWrapper(env) if self.gym_wrapped else env

    def run_rollout_gymnasium_policy(
        self,
        *,
        env_name: str,
        policy: Any,
        wrapper_configs: Any,
        n_episodes: int,
        n_envs: int,
    ) -> tuple[str, list[bool], dict[str, list[Any]]]:
        del wrapper_configs
        assert n_episodes == 1
        env = self.get_gym_env(env_name, 0, n_envs)
        observation, _ = env.reset()
        if self.typed_failure_after_reset is not None:
            raise self.typed_failure_after_reset
        if self.raise_after_reset:
            raise RuntimeError("fixture runner failure")
        policy.reset()
        actions, _ = policy.get_action({key: [value] for key, value in observation.items()})
        for chunk_step_index in range(8):
            step_action = {key: [values[chunk_step_index][0]] for key, values in actions.items()}
            if self.mutate_step_input and chunk_step_index == 0:
                step_action["action.x"] = [0.9]
            _, _, terminated, truncated, info = env.step(step_action)
            if terminated or truncated or info["success"]:
                break
        env.reset()
        env.close()
        return env_name, [self.runner_success], {}


def _runner_configuration(*, action_dim: int = 7):
    return LIBEROPandaRunnerConfiguration(
        model_repository=GROOT_CHECKPOINT_REPOSITORY,
        checkpoint_revision=GROOT_CHECKPOINT_REVISION,
        isaac_groot_revision=ISAAC_GROOT_REVISION,
        libero_revision=LIBERO_REVISION,
        embodiment_tag=LIBERO_PANDA_EMBODIMENT_TAG,
        environment=LIBERO_PANDA_ENVIRONMENT,
        maximum_episode_steps=720,
        policy_action_horizon=LIBERO_POLICY_ACTION_HORIZON,
        n_action_steps=8,
        n_envs=1,
        controller_configuration_sha256=canonical_sha256(
            {
                "controller": "OSC_POSE",
                "action_dim": action_dim,
            }
        ),
        action_dim=action_dim,
        terminate_on_success=True,
    )


def _prepared(*, action_dim: int = 7):
    return prepare_libero_panda_instrumented_episode(
        runner_configuration=_runner_configuration(action_dim=action_dim),
        maximum_observation_age_seconds=30.0,
        clock=_Clock(),
        identity_factory=_IdentityFactory(),
    )


def test_parent_can_freeze_exact_live_identities_before_reset() -> None:
    prepared = prepare_libero_panda_instrumented_episode(
        runner_configuration=_runner_configuration(action_dim=7),
        maximum_observation_age_seconds=30.0,
        clock=_Clock(),
        run_identity="parent-run:goal-a:1",
        episode_identity="parent-run:goal-a:1:libero-episode-1",
    )

    assert prepared.run_identity == "parent-run:goal-a:1"
    assert prepared.episode_identity == ("parent-run:goal-a:1:libero-episode-1")
    assert prepared.contract.to_material()["reference_inputs"][3][
        "content_sha256"
    ] == canonical_sha256({"run_identity": "parent-run:goal-a:1"})


def test_live_identities_must_be_supplied_together() -> None:
    with pytest.raises(
        ValueError,
        match="run_identity and episode_identity must be supplied together",
    ):
        prepare_libero_panda_instrumented_episode(
            runner_configuration=_runner_configuration(action_dim=7),
            maximum_observation_age_seconds=30.0,
            run_identity="parent-run:goal-a:1",
        )


def _controller_probe(
    configuration: LIBEROPandaRunnerConfiguration,
):
    def probe(env: Any) -> LIBEROPandaControllerRuntimeBinding:
        assert isinstance(env, _FakeLiberoEnvironment)
        assert env._env.action_dim == configuration.action_dim
        return LIBEROPandaControllerRuntimeBinding(
            controller_configuration_sha256=(configuration.controller_configuration_sha256),
            action_dim=env._env.action_dim,
        )

    return probe


def test_official_runner_is_instrumented_without_replacing_its_loop() -> None:
    module = _FakeOfficialRolloutModule()
    original_runner = module.run_rollout_gymnasium_policy
    original_factory = module.get_gym_env
    policy = _FakePolicy()
    prepared = _prepared()

    result = run_instrumented_official_libero_rollout(
        rollout_module=module,
        policy=policy,
        wrapper_configs=SimpleNamespace(),
        prepared=prepared,
        controller_probe=_controller_probe(prepared.runner_configuration),
    )

    assert module.run_rollout_gymnasium_policy == original_runner
    assert module.get_gym_env == original_factory
    assert policy.calls == 1
    assert policy.reset_calls == 1
    assert len(module.environments) == 1
    assert module.environments[0].reset_calls == 2
    assert module.environments[0]._env.step_calls == [(0.1, 0.1, 0.1, 0.1, 0.1, 0.1, -1.0)]
    assert result.predicate_evaluation.status is (LIBEROPandaPredicateStatus.SATISFIED)
    assert result.predicate_evaluation.evaluated_outcome_claim is True
    assert result.content.official_runner_episode_success is True
    assert result.content.runtime_action_dim == 7
    assert result.controller_ack_observed is False
    assert result.readback_satisfies_controller_ack is False
    assert result.safe_stop_effect_observed is False
    assert result.parent_mission_completion_claimed is False
    assert result.physical_execution_invoked is False
    assert result.event_sequence == (
        LIBEROPandaInstrumentationEvent.PREPARED_BEFORE_RESET,
        LIBEROPandaInstrumentationEvent.CONTROLLER_RUNTIME_BOUND,
        LIBEROPandaInstrumentationEvent.RESET_OBSERVED,
        LIBEROPandaInstrumentationEvent.POLICY_REQUEST_INVOKED,
        LIBEROPandaInstrumentationEvent.POLICY_RESPONSE_OBSERVED,
        LIBEROPandaInstrumentationEvent.SIMULATOR_STEP_INPUT_OBSERVED,
        LIBEROPandaInstrumentationEvent.SIMULATOR_STEP_RETURN_OBSERVED,
        LIBEROPandaInstrumentationEvent.OFFICIAL_PREDICATE_OBSERVED,
        LIBEROPandaInstrumentationEvent.OFFICIAL_RUNNER_RESULT_OBSERVED,
    )
    serialized = result.to_dict()
    assert serialized["raw_action_values_included"] is False
    assert serialized["raw_simulator_observations_included"] is False
    assert "action_chunks" not in serialized
    assert "step_applications" not in serialized
    observed_at = datetime.fromisoformat(result.content.observed_at)
    received_at = datetime.fromisoformat(result.content.received_at)
    evaluated_at = datetime.fromisoformat(result.predicate_evaluation.evaluated_at)
    assert observed_at < received_at < evaluated_at


def test_pinned_policy_client_tuple_and_namespaced_fields_are_accepted() -> None:
    module = _FakeOfficialRolloutModule()
    prepared = _prepared()

    result = run_instrumented_official_libero_rollout(
        rollout_module=module,
        policy=_FakePolicy(),
        wrapper_configs=SimpleNamespace(),
        prepared=prepared,
        controller_probe=_controller_probe(prepared.runner_configuration),
    )

    assert result.content.action_chunks[0].fields
    assert (
        tuple(field.field_name for field in result.content.action_chunks[0].fields)
        == LIBERO_ACTION_FIELDS
    )
    assert all(
        len(field.values) == LIBERO_POLICY_ACTION_HORIZON
        for field in result.content.action_chunks[0].fields
    )


def test_real_libero_numpy_scalar_step_return_is_content_bound() -> None:
    module = _FakeOfficialRolloutModule(numpy_scalar_step_return=True)
    prepared = _prepared()

    result = run_instrumented_official_libero_rollout(
        rollout_module=module,
        policy=_FakePolicy(),
        wrapper_configs=SimpleNamespace(),
        prepared=prepared,
        controller_probe=_controller_probe(prepared.runner_configuration),
    )

    assert LIBEROPandaInstrumentationEvent.SIMULATOR_STEP_RETURN_OBSERVED in result.event_sequence
    assert result.predicate_evaluation.status is (LIBEROPandaPredicateStatus.SATISFIED)


def test_numpy_array_digest_binds_dtype_shape_and_content() -> None:
    original = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
    same = original.copy()
    changed = original.copy()
    changed[1, 2, 3] += 1.0

    original_digest = _digest_material("observation", original)

    assert _digest_material("observation", same) == original_digest
    assert _digest_material("observation", changed) != original_digest
    assert _digest_material("observation", original.astype(np.float64)) != original_digest
    assert _digest_material("observation", original.reshape(4, 3, 2)) != original_digest


def test_object_array_is_refused_instead_of_hashing_pointer_bytes() -> None:
    with pytest.raises(
        LIBEROPandaInstrumentationError,
        match="object arrays cannot be content-bound",
    ):
        _digest_material(
            "observation",
            np.asarray([object()], dtype=object),
        )


def test_invalid_simulator_step_return_has_typed_reason() -> None:
    module = _FakeOfficialRolloutModule(invalid_step_return=True)
    prepared = _prepared()

    with pytest.raises(LIBEROPandaInstrumentationError) as caught:
        run_instrumented_official_libero_rollout(
            rollout_module=module,
            policy=_FakePolicy(),
            wrapper_configs=SimpleNamespace(),
            prepared=prepared,
            controller_probe=_controller_probe(prepared.runner_configuration),
        )

    assert caught.value.rejection_reason is (
        LIBEROPandaInstrumentationRejectionReason.SIMULATOR_STEP_RETURN_INVALID
    )
    assert prepared.recorder.event_sequence[-1] is (
        LIBEROPandaInstrumentationEvent.SIMULATOR_STEP_INPUT_OBSERVED
    )


@pytest.mark.parametrize(
    ("policy", "expected_reason"),
    (
        (
            _MappingOnlyPolicy(),
            LIBEROPandaInstrumentationRejectionReason.POLICY_RESPONSE_SHAPE_INVALID,
        ),
        (
            _UnnamespacedPolicy(),
            LIBEROPandaInstrumentationRejectionReason.POLICY_RESPONSE_ACTION_FIELDS_MISSING,
        ),
        (
            _ExecutionHorizonOnlyPolicy(),
            LIBEROPandaInstrumentationRejectionReason.POLICY_RESPONSE_ACTION_HORIZON_INVALID,
        ),
    ),
)
def test_policy_response_shape_rejections_are_typed(
    policy: Any,
    expected_reason: LIBEROPandaInstrumentationRejectionReason,
) -> None:
    module = _FakeOfficialRolloutModule()
    prepared = _prepared()

    with pytest.raises(LIBEROPandaInstrumentationError) as caught:
        run_instrumented_official_libero_rollout(
            rollout_module=module,
            policy=policy,
            wrapper_configs=SimpleNamespace(),
            prepared=prepared,
            controller_probe=_controller_probe(prepared.runner_configuration),
        )

    assert caught.value.rejection_reason is expected_reason
    assert module.environments[0]._env.step_calls == []


def test_runtime_four_dimensional_projection_is_observed_and_bound() -> None:
    module = _FakeOfficialRolloutModule(action_dim=4)
    prepared = _prepared(action_dim=4)

    result = run_instrumented_official_libero_rollout(
        rollout_module=module,
        policy=_FakePolicy(),
        wrapper_configs=SimpleNamespace(),
        prepared=prepared,
        controller_probe=_controller_probe(prepared.runner_configuration),
    )

    assert module.environments[0]._env.step_calls == [(0.1, 0.1, 0.1, -1.0)]
    step = result.content.step_applications[0]
    assert step.transformation_names[-1] == ("project_osc_position_to_xyz_gripper")
    assert result.content.runtime_action_dim == 4
    assert result.predicate_evaluation.status is (LIBEROPandaPredicateStatus.SATISFIED)


def test_official_runner_aggregate_cannot_disagree_with_exact_predicate() -> None:
    module = _FakeOfficialRolloutModule(
        success=True,
        runner_success=False,
    )
    prepared = _prepared()

    result = run_instrumented_official_libero_rollout(
        rollout_module=module,
        policy=_FakePolicy(),
        wrapper_configs=SimpleNamespace(),
        prepared=prepared,
        controller_probe=_controller_probe(prepared.runner_configuration),
    )

    assert result.predicate_evaluation.status is (LIBEROPandaPredicateStatus.BLOCKED)
    assert result.predicate_evaluation.evaluated_outcome_claim is False
    assert "official_runner_episode_success_mismatch" in (result.predicate_evaluation.reasons)


def test_unsatisfied_episode_can_finalize_after_wrapper_level_reset() -> None:
    module = _FakeOfficialRolloutModule(
        success=False,
        runner_success=False,
    )
    prepared = _prepared()

    result = run_instrumented_official_libero_rollout(
        rollout_module=module,
        policy=_FakePolicy(),
        wrapper_configs=SimpleNamespace(),
        prepared=prepared,
        controller_probe=_controller_probe(prepared.runner_configuration),
    )

    assert len(result.content.step_applications) == 8
    assert result.content.official_runner_episode_success is False
    assert result.predicate_evaluation.status is (LIBEROPandaPredicateStatus.NOT_SATISFIED)
    assert result.predicate_evaluation.evaluated_outcome_claim is False


def test_multistep_slice_mutation_is_blocked_before_underlying_step() -> None:
    module = _FakeOfficialRolloutModule(mutate_step_input=True)
    prepared = _prepared()

    with pytest.raises(
        LIBEROPandaInstrumentationError,
        match="slice does not match",
    ):
        run_instrumented_official_libero_rollout(
            rollout_module=module,
            policy=_FakePolicy(),
            wrapper_configs=SimpleNamespace(),
            prepared=prepared,
            controller_probe=_controller_probe(prepared.runner_configuration),
        )

    assert module.environments[0]._env.step_calls == []


def test_controller_binding_mismatch_is_blocked_before_reset() -> None:
    module = _FakeOfficialRolloutModule()
    prepared = _prepared()

    with pytest.raises(
        LIBEROPandaInstrumentationError,
        match="controller configuration",
    ):
        run_instrumented_official_libero_rollout(
            rollout_module=module,
            policy=_FakePolicy(),
            wrapper_configs=SimpleNamespace(),
            prepared=prepared,
            controller_probe=lambda env: LIBEROPandaControllerRuntimeBinding(
                controller_configuration_sha256="f" * 64,
                action_dim=env._env.action_dim,
            ),
        )

    assert module.environments[0].reset_calls == 0


def test_official_factory_is_restored_when_runner_raises() -> None:
    module = _FakeOfficialRolloutModule(raise_after_reset=True)
    original_factory = module.get_gym_env
    prepared = _prepared()

    with pytest.raises(LIBEROPandaInstrumentationError) as caught:
        run_instrumented_official_libero_rollout(
            rollout_module=module,
            policy=_FakePolicy(),
            wrapper_configs=SimpleNamespace(),
            prepared=prepared,
            controller_probe=_controller_probe(prepared.runner_configuration),
        )

    assert module.get_gym_env == original_factory
    assert caught.value.rejection_reason is (
        LIBEROPandaInstrumentationRejectionReason.OFFICIAL_RUNNER_FAILED
    )


def test_rollout_deadline_reason_passes_through_official_runner_wrapper() -> None:
    deadline_error = LIBEROPandaInstrumentationError(
        "bounded diagnostic rollout exceeded its elapsed-time limit",
        rejection_reason=(LIBEROPandaInstrumentationRejectionReason.ROLLOUT_DEADLINE_EXCEEDED),
    )
    module = _FakeOfficialRolloutModule(
        typed_failure_after_reset=deadline_error,
    )
    original_factory = module.get_gym_env
    prepared = _prepared()

    with pytest.raises(LIBEROPandaInstrumentationError) as caught:
        run_instrumented_official_libero_rollout(
            rollout_module=module,
            policy=_FakePolicy(),
            wrapper_configs=SimpleNamespace(),
            prepared=prepared,
            controller_probe=_controller_probe(prepared.runner_configuration),
        )

    assert module.get_gym_env == original_factory
    assert caught.value is deadline_error
    assert caught.value.rejection_reason is (
        LIBEROPandaInstrumentationRejectionReason.ROLLOUT_DEADLINE_EXCEEDED
    )


def test_environment_creation_failure_is_typed_before_reset() -> None:
    module = _FakeOfficialRolloutModule()
    prepared = _prepared()

    def failing_factory(
        env_name: str,
        env_idx: int,
        total_n_envs: int,
    ) -> Any:
        del env_name, env_idx, total_n_envs
        raise AttributeError("publication-unsafe dependency detail")

    module.get_gym_env = failing_factory

    with pytest.raises(LIBEROPandaInstrumentationError) as caught:
        run_instrumented_official_libero_rollout(
            rollout_module=module,
            policy=_FakePolicy(),
            wrapper_configs=SimpleNamespace(),
            prepared=prepared,
            controller_probe=_controller_probe(prepared.runner_configuration),
        )

    assert caught.value.rejection_reason is (
        LIBEROPandaInstrumentationRejectionReason.OFFICIAL_ENVIRONMENT_CREATION_FAILED
    )
    assert isinstance(caught.value.__cause__, AttributeError)
    assert prepared.recorder.event_sequence == (
        LIBEROPandaInstrumentationEvent.PREPARED_BEFORE_RESET,
    )


def test_controller_probe_failure_is_typed_before_reset() -> None:
    module = _FakeOfficialRolloutModule()
    prepared = _prepared()

    def failing_probe(env: Any) -> LIBEROPandaControllerRuntimeBinding:
        del env
        raise AttributeError("publication-unsafe controller detail")

    with pytest.raises(LIBEROPandaInstrumentationError) as caught:
        run_instrumented_official_libero_rollout(
            rollout_module=module,
            policy=_FakePolicy(),
            wrapper_configs=SimpleNamespace(),
            prepared=prepared,
            controller_probe=failing_probe,
        )

    assert caught.value.rejection_reason is (
        LIBEROPandaInstrumentationRejectionReason.CONTROLLER_RUNTIME_PROBE_FAILED
    )
    assert prepared.recorder.event_sequence == (
        LIBEROPandaInstrumentationEvent.PREPARED_BEFORE_RESET,
    )


def test_environment_reset_failure_is_typed_after_controller_binding() -> None:
    module = _FakeOfficialRolloutModule()
    original_factory = module.get_gym_env
    prepared = _prepared()

    def reset_failing_factory(
        env_name: str,
        env_idx: int,
        total_n_envs: int,
    ) -> _GymStyleEnvironmentWrapper:
        env = original_factory(env_name, env_idx, total_n_envs)
        assert isinstance(env, _GymStyleEnvironmentWrapper)

        def failing_reset(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise AttributeError("publication-unsafe reset detail")

        env.unwrapped.reset = failing_reset
        return env

    module.get_gym_env = reset_failing_factory

    with pytest.raises(LIBEROPandaInstrumentationError) as caught:
        run_instrumented_official_libero_rollout(
            rollout_module=module,
            policy=_FakePolicy(),
            wrapper_configs=SimpleNamespace(),
            prepared=prepared,
            controller_probe=_controller_probe(prepared.runner_configuration),
        )

    assert caught.value.rejection_reason is (
        LIBEROPandaInstrumentationRejectionReason.ENVIRONMENT_RESET_FAILED
    )
    assert prepared.recorder.event_sequence == (
        LIBEROPandaInstrumentationEvent.PREPARED_BEFORE_RESET,
        LIBEROPandaInstrumentationEvent.CONTROLLER_RUNTIME_BOUND,
    )

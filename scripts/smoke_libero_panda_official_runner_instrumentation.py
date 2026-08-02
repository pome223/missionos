#!/usr/bin/env python3
"""GPU-free official-runner-shaped smoke for LIBERO instrumentation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from typing import Any

from missionos_core import canonical_sha256
from src.runtime.libero_panda_official_runner_instrumentation import (
    LIBEROPandaControllerRuntimeBinding,
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
    LIBEROPandaRunnerConfiguration,
)


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 31, tzinfo=timezone.utc)

    def __call__(self) -> str:
        value = self.value
        self.value += timedelta(milliseconds=10)
        return value.isoformat()


class _UnderlyingEnvironment:
    action_dim = 7

    def reset(self):
        return {"raw_state": [0.0]}

    def step(self, action):
        return {"raw_state": list(action)}, 0.0, True, {}

    def check_success(self):
        return True

    def close(self):
        return None


class _LiberoEnvironment:
    def __init__(self) -> None:
        self._env = _UnderlyingEnvironment()

    def reset(self, seed=None, options=None):
        del seed, options
        return self._env.reset(), {"success": self._env.check_success()}

    def step(self, action):
        vector = [
            float(action[field_name][0])
            for field_name in LIBERO_ACTION_FIELDS
        ]
        vector[-1] = 2.0 * vector[-1] - 1.0
        vector[-1] = (
            1.0 if vector[-1] > 0 else -1.0 if vector[-1] < 0 else 0.0
        )
        vector[-1] *= -1.0
        observation, reward, done, info = self._env.step(vector)
        info["success"] = self._env.check_success()
        return observation, reward, done, False, info

    def close(self):
        self._env.close()


class _Policy:
    def reset(self):
        return None

    def get_action(self, observations):
        assert observations
        return (
            {
                field_name: [
                    [0.75 if field_name == "action.gripper" else 0.1]
                    for _ in range(LIBERO_POLICY_ACTION_HORIZON)
                ]
                for field_name in LIBERO_ACTION_FIELDS
            },
            {"fixture": True},
        )


class _OfficialRunnerShape:
    def get_gym_env(self, env_name, env_idx, total_n_envs):
        assert env_name == LIBERO_PANDA_ENVIRONMENT
        assert env_idx == 0
        assert total_n_envs == 1
        return _LiberoEnvironment()

    def run_rollout_gymnasium_policy(
        self,
        *,
        env_name,
        policy,
        wrapper_configs,
        n_episodes,
        n_envs,
    ):
        del wrapper_configs
        assert n_episodes == 1
        env = self.get_gym_env(env_name, 0, n_envs)
        observation, _ = env.reset()
        policy.reset()
        actions, _ = policy.get_action(
            {key: [value] for key, value in observation.items()}
        )
        step_action = {
            key: [values[0][0]]
            for key, values in actions.items()
        }
        env.step(step_action)
        env.reset()
        env.close()
        return env_name, [True], {}


def main() -> None:
    controller_digest = canonical_sha256(
        {"controller": "OSC_POSE", "action_dim": 7}
    )
    configuration = LIBEROPandaRunnerConfiguration(
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
        controller_configuration_sha256=controller_digest,
        action_dim=7,
        terminate_on_success=True,
    )
    identities = iter(("fixture-run", "fixture-episode"))
    prepared = prepare_libero_panda_instrumented_episode(
        runner_configuration=configuration,
        maximum_observation_age_seconds=30.0,
        clock=_Clock(),
        identity_factory=lambda: next(identities),
    )
    result = run_instrumented_official_libero_rollout(
        rollout_module=_OfficialRunnerShape(),
        policy=_Policy(),
        wrapper_configs=SimpleNamespace(),
        prepared=prepared,
        controller_probe=lambda env: (
            LIBEROPandaControllerRuntimeBinding(
                controller_configuration_sha256=controller_digest,
                action_dim=env._env.action_dim,
            )
        ),
    )
    material: dict[str, Any] = {
        **result.to_dict(),
        "fixture_only": True,
        "official_groot_runner_imported": False,
        "model_runtime_invoked": False,
        "simulator_runtime_invoked": False,
    }
    evaluation = material["predicate_evaluation"]
    if not (
        evaluation["status"] == "satisfied"
        and evaluation["evaluated_outcome_claim"] is True
        and material["controller_ack_observed"] is False
        and material["physical_execution_invoked"] is False
        and material["raw_action_values_included"] is False
    ):
        raise SystemExit("LIBERO instrumentation fixture did not satisfy")
    print(json.dumps(material, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

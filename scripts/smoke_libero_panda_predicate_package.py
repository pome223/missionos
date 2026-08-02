#!/usr/bin/env python3
"""GPU-free fixture smoke for the concrete LIBERO Panda predicate package."""

from __future__ import annotations

import json

from missionos_core import canonical_sha256
from src.runtime.libero_panda_predicate_package import (
    GROOT_CHECKPOINT_REPOSITORY,
    GROOT_CHECKPOINT_REVISION,
    ISAAC_GROOT_REVISION,
    LIBERO_ACTION_FIELDS,
    LIBERO_BASE_TRANSFORMATIONS,
    LIBERO_PANDA_EMBODIMENT_TAG,
    LIBERO_PANDA_ENVIRONMENT,
    LIBERO_PANDA_EPISODE_RESULT_SCHEMA_VERSION,
    LIBERO_POLICY_ACTION_HORIZON,
    LIBERO_REVISION,
    LIBERO_TASK_PREDICATE_SHA256,
    LIBEROPandaActionChunk,
    LIBEROPandaActionField,
    LIBEROPandaPredicateContent,
    LIBEROPandaRunnerConfiguration,
    LIBEROPandaStepApplication,
    build_libero_panda_replay_contract,
    build_libero_panda_replay_input,
    evaluate_libero_panda_predicate,
)


def main() -> None:
    run_identity = "groot-libero-predicate-fixture:run-1"
    episode_identity = f"{run_identity}:episode-1"
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
        controller_configuration_sha256=canonical_sha256(
            {
                "fixture_only": True,
                "controller": "OSC_POSE",
                "action_dim": 7,
            }
        ),
        action_dim=7,
        terminate_on_success=True,
    )
    contract = build_libero_panda_replay_contract(
        contract_id="groot-libero-predicate-fixture:episode-1",
        contract_version="v1",
        runner_configuration=configuration,
        run_identity=run_identity,
        episode_identity=episode_identity,
        maximum_observation_age_seconds=30.0,
    )
    action_values = {
        "action.x": (0.1,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.y": (0.0,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.z": (0.0,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.roll": (0.0,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.pitch": (0.0,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.yaw": (0.0,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.gripper": (0.75,) * LIBERO_POLICY_ACTION_HORIZON,
    }
    chunk = LIBEROPandaActionChunk(
        chunk_index=0,
        policy_request_sha256=canonical_sha256({"fixture_request": 1}),
        policy_response_sha256=canonical_sha256(
            {"fixture_response": 1}
        ),
        fields=tuple(
            LIBEROPandaActionField(
                field_name=field_name,
                values=action_values[field_name],
            )
            for field_name in LIBERO_ACTION_FIELDS
        ),
    )
    step = LIBEROPandaStepApplication(
        global_step_index=0,
        chunk_index=0,
        chunk_step_index=0,
        action_chunk_sha256=chunk.action_chunk_sha256,
        transformation_names=LIBERO_BASE_TRANSFORMATIONS,
        env_step_input=(0.1, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0),
        simulator_step_return_sha256=canonical_sha256(
            {"fixture_step_return": 1}
        ),
        result_observation_sha256=canonical_sha256(
            {"fixture_observation": 1}
        ),
        official_predicate_result=True,
        terminated=True,
        truncated=False,
    )
    content = LIBEROPandaPredicateContent(
        source_schema_version=LIBERO_PANDA_EPISODE_RESULT_SCHEMA_VERSION,
        run_identity=run_identity,
        episode_identity=episode_identity,
        runner_configuration=configuration,
        runtime_controller_configuration_sha256=(
            configuration.controller_configuration_sha256
        ),
        runtime_action_dim=configuration.action_dim,
        task_predicate_sha256=LIBERO_TASK_PREDICATE_SHA256,
        action_chunks=(chunk,),
        step_applications=(step,),
        official_runner_episode_ended=True,
        official_runner_episode_success=True,
        observed_at="2026-07-31T00:00:10+00:00",
        received_at="2026-07-31T00:00:11+00:00",
    )
    evaluation = evaluate_libero_panda_predicate(
        contract=contract,
        replay=build_libero_panda_replay_input(
            contract=contract,
            content=content,
        ),
        evaluated_at="2026-07-31T00:00:12+00:00",
    )
    result = {
        **evaluation.to_dict(),
        "fixture_only": True,
        "model_runtime_invoked": False,
        "simulator_runtime_invoked": False,
        "general_semantic_completion_claimed": False,
        "parent_mission_completion_claimed": False,
        "success_rate_claimed": False,
        "benchmark_result_claimed": False,
    }
    if not (
        result["status"] == "satisfied"
        and result["evaluated_outcome_claim"] is True
        and result["actual_verification_basis"] == "deterministic"
        and result["controller_ack_observed"] is False
        and result["dispatch_authority_created"] is False
        and result["physical_execution_invoked"] is False
    ):
        raise SystemExit("LIBERO Panda predicate fixture did not satisfy")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

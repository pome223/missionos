from __future__ import annotations

from dataclasses import replace
from math import nan

import pytest

from missionos_core import (
    EvidenceOrigin,
    FrozenParentMissionContract,
    MissionEvidenceReadiness,
    QuantificationScope,
    QuantificationScopeKind,
    ReferenceInput,
    VerificationBasis,
    build_parent_mission_approval_binding,
    build_parent_mission_stage_binding,
    canonical_sha256,
    validate_frozen_mission_contract,
    validate_frozen_parent_mission_contract,
    validate_parent_mission_approval_binding,
)
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
    LIBERO_PANDA_OUTCOME_CLAIM_SCOPE,
    LIBERO_PANDA_SCENE8_ENVIRONMENT,
    LIBERO_PANDA_SCENE8_TASK_PREDICATE_SHA256,
    LIBERO_REVISION,
    LIBERO_TASK_PREDICATE_SHA256,
    LIBEROPandaActionChunk,
    LIBEROPandaActionField,
    LIBEROPandaGoalPredicateObservation,
    LIBEROPandaPredicateContent,
    LIBEROPandaPredicateStatus,
    LIBEROPandaRunnerConfiguration,
    LIBEROPandaStepApplication,
    build_libero_panda_replay_contract,
    build_libero_panda_replay_input,
    evaluate_libero_panda_predicate,
    libero_panda_predicate_package_binding,
    libero_panda_task_material,
)
from src.runtime.nav2_turtlebot3_predicate_package import (
    build_nav2_turtlebot3_replay_contract,
)
from src.runtime.px4_gazebo_delivery_predicate_package import (
    build_px4_gazebo_delivery_replay_contract,
)


OBSERVED_AT = "2026-07-31T00:00:10+00:00"
RECEIVED_AT = "2026-07-31T00:00:11+00:00"
EVALUATED_AT = "2026-07-31T00:00:12+00:00"
RUN_ID = "groot-libero-governed-run:fixture-1"
EPISODE_ID = f"{RUN_ID}:episode-1"


def _runner_configuration(
    *,
    action_dim: int = 7,
) -> LIBEROPandaRunnerConfiguration:
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
                "fixture_only": True,
                "controller": "OSC_POSE",
                "action_dim": action_dim,
            }
        ),
        action_dim=action_dim,
        terminate_on_success=True,
    )


def _contract(
    *,
    configuration: LIBEROPandaRunnerConfiguration | None = None,
):
    return build_libero_panda_replay_contract(
        contract_id="groot-libero-fixture:episode-1",
        contract_version="v1",
        runner_configuration=configuration or _runner_configuration(),
        run_identity=RUN_ID,
        episode_identity=EPISODE_ID,
        maximum_observation_age_seconds=30.0,
    )


def _action_chunk(
    *,
    gripper_value: float = 0.75,
) -> LIBEROPandaActionChunk:
    values = {
        "action.x": (0.1,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.y": (0.0,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.z": (0.0,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.roll": (0.0,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.pitch": (0.0,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.yaw": (0.0,) * LIBERO_POLICY_ACTION_HORIZON,
        "action.gripper": ((gripper_value,) * LIBERO_POLICY_ACTION_HORIZON),
    }
    return LIBEROPandaActionChunk(
        chunk_index=0,
        policy_request_sha256=canonical_sha256({"request": 1}),
        policy_response_sha256=canonical_sha256({"response": 1}),
        fields=tuple(
            LIBEROPandaActionField(
                field_name=field_name,
                values=values[field_name],
            )
            for field_name in LIBERO_ACTION_FIELDS
        ),
    )


def _content(
    *,
    predicate_result: bool = True,
    action_dim: int = 7,
) -> LIBEROPandaPredicateContent:
    configuration = _runner_configuration(action_dim=action_dim)
    chunk = _action_chunk()
    env_step_input = (
        (0.1, 0.0, 0.0, -1.0) if action_dim == 4 else (0.1, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0)
    )
    transformations = (
        *LIBERO_BASE_TRANSFORMATIONS,
        *(("project_osc_position_to_xyz_gripper",) if action_dim == 4 else ()),
    )
    step = LIBEROPandaStepApplication(
        global_step_index=0,
        chunk_index=0,
        chunk_step_index=0,
        action_chunk_sha256=chunk.action_chunk_sha256,
        transformation_names=transformations,
        env_step_input=env_step_input,
        simulator_step_return_sha256=canonical_sha256({"step_return": 1}),
        result_observation_sha256=canonical_sha256({"observation": 1}),
        goal_predicate_observations=(
            LIBEROPandaGoalPredicateObservation(
                predicate_index=0,
                predicate_name="turnon",
                arguments=("flat_stove_1",),
                satisfied=predicate_result,
            ),
            LIBEROPandaGoalPredicateObservation(
                predicate_index=1,
                predicate_name="on",
                arguments=(
                    "moka_pot_1",
                    "flat_stove_1_cook_region",
                ),
                satisfied=predicate_result,
            ),
        ),
        official_predicate_result=predicate_result,
        terminated=predicate_result,
        truncated=not predicate_result,
    )
    return LIBEROPandaPredicateContent(
        source_schema_version=LIBERO_PANDA_EPISODE_RESULT_SCHEMA_VERSION,
        run_identity=RUN_ID,
        episode_identity=EPISODE_ID,
        runner_configuration=configuration,
        runtime_controller_configuration_sha256=(configuration.controller_configuration_sha256),
        runtime_action_dim=configuration.action_dim,
        task_predicate_sha256=LIBERO_TASK_PREDICATE_SHA256,
        action_chunks=(chunk,),
        step_applications=(step,),
        official_runner_episode_ended=True,
        official_runner_episode_success=predicate_result,
        observed_at=OBSERVED_AT,
        received_at=RECEIVED_AT,
    )


def _evaluate(
    *,
    contract=None,
    content: LIBEROPandaPredicateContent | None = None,
    evaluated_at: str = EVALUATED_AT,
):
    contract = contract or _contract()
    content = content or _content()
    return evaluate_libero_panda_predicate(
        contract=contract,
        replay=build_libero_panda_replay_input(
            contract=contract,
            content=content,
        ),
        evaluated_at=evaluated_at,
    )


def test_exact_child_contract_is_structurally_valid_and_simulator_scoped() -> None:
    contract = _contract()

    assert validate_frozen_mission_contract(contract) == ()
    assert contract.predicate_package == (libero_panda_predicate_package_binding())
    assert contract.quantification_scope.member_ids == (EPISODE_ID,)
    assert contract.outcome_claim_spec.claim_scope == (LIBERO_PANDA_OUTCOME_CLAIM_SCOPE)
    assert "LIBERO simulator episode" in (contract.outcome_claim_spec.statement)
    assert [item.input_id for item in contract.reference_inputs] == [
        "approved_runner_configuration",
        "approved_task_predicate",
        "approved_action_transformations",
        "approved_run_identity",
        "approved_episode_identity",
    ]


def test_scene8_contract_freezes_distinct_task_predicate_and_process_seed() -> None:
    configuration = replace(
        _runner_configuration(),
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        process_seed=0,
    )
    contract = _contract(configuration=configuration)
    references = {item.input_id: item for item in contract.reference_inputs}

    assert validate_frozen_mission_contract(contract) == ()
    assert contract.predicate_package == libero_panda_predicate_package_binding(
        LIBERO_PANDA_SCENE8_ENVIRONMENT
    )
    assert references["approved_runner_configuration"].content_sha256 == (
        configuration.content_sha256
    )
    assert references["approved_task_predicate"].content_sha256 == (
        LIBERO_PANDA_SCENE8_TASK_PREDICATE_SHA256
    )
    assert configuration.to_material()["process_seed"] == 0
    assert (
        libero_panda_task_material(LIBERO_PANDA_SCENE8_ENVIRONMENT)["task_predicate_sha256"]
        == LIBERO_PANDA_SCENE8_TASK_PREDICATE_SHA256
    )


def test_content_bound_fixture_episode_satisfies_exact_package() -> None:
    evaluation = _evaluate()

    assert evaluation.evidence_readiness is MissionEvidenceReadiness.READY
    assert evaluation.status is LIBEROPandaPredicateStatus.SATISFIED
    assert evaluation.evaluated_outcome_claim is True
    assert evaluation.actual_verification_basis is VerificationBasis.DETERMINISTIC
    assert evaluation.predicate_package_evaluated is True
    assert evaluation.simulator_step_return_observed is True
    assert evaluation.official_sim_task_success is True
    assert evaluation.controller_ack_observed is False
    assert evaluation.controller_ack_verification_status == "unverified"
    assert evaluation.readback_satisfies_controller_ack is False
    assert evaluation.approval_created is False
    assert evaluation.dispatch_authority_created is False
    assert evaluation.runtime_effect_requested is False
    assert evaluation.operational_closure_created is False
    assert evaluation.physical_execution_invoked is False
    assert evaluation.evidence_origins == (EvidenceOrigin.STORED_ARTIFACT,)
    serialized = evaluation.to_dict()
    assert "action_chunks" not in serialized
    assert "step_applications" not in serialized
    assert len(evaluation.raw_action_stream_manifest_sha256) == 64
    assert len(evaluation.env_step_input_stream_manifest_sha256) == 64
    assert len(evaluation.simulator_step_return_manifest_sha256) == 64


def test_false_official_predicate_does_not_satisfy_outcome() -> None:
    evaluation = _evaluate(content=_content(predicate_result=False))

    assert evaluation.status is (LIBEROPandaPredicateStatus.NOT_SATISFIED)
    assert evaluation.evaluated_outcome_claim is False
    assert evaluation.official_sim_task_success is False
    assert evaluation.reasons == ("official_sim_task_predicate_not_satisfied",)


def test_partial_goal_predicate_vector_identifies_the_unmet_member() -> None:
    content = _content(predicate_result=False)
    step = content.step_applications[0]
    observations = (
        replace(step.goal_predicate_observations[0], satisfied=True),
        step.goal_predicate_observations[1],
    )
    evaluation = _evaluate(
        content=replace(
            content,
            step_applications=(replace(step, goal_predicate_observations=observations),),
        )
    )

    assert evaluation.status is LIBEROPandaPredicateStatus.NOT_SATISFIED
    serialized = evaluation.to_dict()
    assert [item["satisfied"] for item in serialized["goal_predicate_observations"]] == [
        True,
        False,
    ]
    assert serialized["satisfied_predicate_ids"] == [observations[0].predicate_id]
    assert serialized["unsatisfied_predicate_ids"] == [observations[1].predicate_id]


def test_goal_predicate_conjunction_must_match_official_result() -> None:
    content = _content(predicate_result=False)
    step = content.step_applications[0]
    observations = tuple(
        replace(observation, satisfied=True) for observation in step.goal_predicate_observations
    )
    evaluation = _evaluate(
        content=replace(
            content,
            step_applications=(replace(step, goal_predicate_observations=observations),),
        )
    )

    assert evaluation.status is LIBEROPandaPredicateStatus.BLOCKED
    assert "goal_predicate_conjunction_mismatch:0" in evaluation.reasons


@pytest.mark.parametrize(
    "mutation,expected_reason",
    [
        (
            "undeclared_transformation",
            "undeclared_action_transformation:0",
        ),
        (
            "step_input",
            "simulator_step_input_mismatch:0",
        ),
        (
            "chunk_digest",
            "simulator_step_chunk_digest_mismatch:0",
        ),
        (
            "controller_ack",
            "independent_controller_ack_claim_forbidden",
        ),
        (
            "readback_as_ack",
            "readback_ack_substitution_forbidden",
        ),
        (
            "physical_execution",
            "physical_execution_claim_forbidden",
        ),
    ],
)
def test_lineage_or_claim_mutation_is_blocked(
    mutation: str,
    expected_reason: str,
) -> None:
    content = _content()
    step = content.step_applications[0]
    if mutation == "undeclared_transformation":
        content = replace(
            content,
            step_applications=(
                replace(
                    step,
                    transformation_names=("clipping",),
                ),
            ),
        )
    elif mutation == "step_input":
        content = replace(
            content,
            step_applications=(
                replace(
                    step,
                    env_step_input=(0.0,) * 7,
                ),
            ),
        )
    elif mutation == "chunk_digest":
        content = replace(
            content,
            step_applications=(
                replace(
                    step,
                    action_chunk_sha256="f" * 64,
                ),
            ),
        )
    elif mutation == "controller_ack":
        content = replace(content, controller_ack_observed=True)
    elif mutation == "readback_as_ack":
        content = replace(
            content,
            readback_satisfies_controller_ack=True,
        )
    elif mutation == "physical_execution":
        content = replace(content, physical_execution_invoked=True)

    evaluation = _evaluate(content=content)

    assert evaluation.status is LIBEROPandaPredicateStatus.BLOCKED
    assert evaluation.evaluated_outcome_claim is False
    assert expected_reason in evaluation.reasons


def test_non_finite_policy_action_is_blocked_before_outcome() -> None:
    content = _content()
    chunk = content.action_chunks[0]
    first_field = chunk.fields[0]
    invalid_chunk = replace(
        chunk,
        fields=(
            replace(
                first_field,
                values=(nan, *first_field.values[1:]),
            ),
            *chunk.fields[1:],
        ),
    )
    content = replace(content, action_chunks=(invalid_chunk,))

    evaluation = _evaluate(content=content)

    assert evaluation.status is LIBEROPandaPredicateStatus.BLOCKED
    assert "policy_action_non_finite:0:action.x" in evaluation.reasons
    assert evaluation.evaluated_outcome_claim is False


def test_execution_horizon_is_not_accepted_as_full_policy_horizon() -> None:
    content = _content()
    chunk = content.action_chunks[0]
    truncated_chunk = replace(
        chunk,
        fields=tuple(replace(field, values=field.values[:8]) for field in chunk.fields),
    )
    content = replace(content, action_chunks=(truncated_chunk,))

    evaluation = _evaluate(content=content)

    assert evaluation.status is LIBEROPandaPredicateStatus.BLOCKED
    assert "policy_action_horizon_invalid:0:action.x" in evaluation.reasons
    assert evaluation.evaluated_outcome_claim is False


@pytest.mark.parametrize(
    "content,expected_reason",
    [
        (
            replace(
                _content(),
                runtime_controller_configuration_sha256="f" * 64,
            ),
            "runtime_controller_configuration_digest_mismatch",
        ),
        (
            replace(_content(), runtime_action_dim=4),
            "runtime_action_dim_mismatch",
        ),
        (
            replace(_content(), official_runner_episode_success=False),
            "official_runner_episode_success_mismatch",
        ),
        (
            replace(_content(), official_runner_episode_ended=False),
            "official_runner_episode_end_missing",
        ),
    ],
)
def test_runtime_configuration_and_runner_result_must_match_frozen_episode(
    content: LIBEROPandaPredicateContent,
    expected_reason: str,
) -> None:
    evaluation = _evaluate(content=content)

    assert evaluation.status is LIBEROPandaPredicateStatus.BLOCKED
    assert evaluation.evaluated_outcome_claim is False
    assert expected_reason in evaluation.reasons


def test_cross_chunk_step_reordering_is_blocked() -> None:
    content = _content()
    first_chunk = content.action_chunks[0]
    second_chunk = replace(
        first_chunk,
        chunk_index=1,
        policy_request_sha256=canonical_sha256({"request": 2}),
        policy_response_sha256=canonical_sha256({"response": 2}),
    )
    first_step = replace(
        content.step_applications[0],
        official_predicate_result=False,
        terminated=False,
        chunk_index=1,
        action_chunk_sha256=second_chunk.action_chunk_sha256,
    )
    second_step = replace(
        content.step_applications[0],
        global_step_index=1,
        chunk_index=0,
        action_chunk_sha256=first_chunk.action_chunk_sha256,
    )
    content = replace(
        content,
        action_chunks=(first_chunk, second_chunk),
        step_applications=(first_step, second_step),
    )

    evaluation = _evaluate(content=content)

    assert evaluation.status is LIBEROPandaPredicateStatus.BLOCKED
    assert "simulator_chunk_order_invalid" in evaluation.reasons
    assert evaluation.evaluated_outcome_claim is False


def test_episode_record_without_terminal_result_is_blocked() -> None:
    content = _content(predicate_result=False)
    content = replace(
        content,
        official_runner_episode_ended=False,
        step_applications=(
            replace(
                content.step_applications[0],
                truncated=False,
            ),
        ),
    )

    evaluation = _evaluate(content=content)

    assert evaluation.status is LIBEROPandaPredicateStatus.BLOCKED
    assert "episode_terminal_result_missing" in evaluation.reasons


def test_episode_identity_mismatch_is_blocked_at_frozen_boundary() -> None:
    content = replace(
        _content(),
        episode_identity=f"{RUN_ID}:episode-other",
    )

    evaluation = _evaluate(content=content)

    assert evaluation.status is LIBEROPandaPredicateStatus.BLOCKED
    assert "approved_episode_identity_mismatch" in evaluation.reasons
    assert "episode_quantification_scope_mismatch" in evaluation.reasons
    assert evaluation.predicate_package_evaluated is False


def test_task_predicate_reference_cannot_be_replaced_by_real_world_claim() -> None:
    contract = _contract()
    references = tuple(
        replace(
            item,
            content_sha256=canonical_sha256({"claim": "a real stove was turned on"}),
        )
        if item.input_id == "approved_task_predicate"
        else item
        for item in contract.reference_inputs
    )
    contract = replace(contract, reference_inputs=references)

    evaluation = _evaluate(contract=contract)

    assert evaluation.status is LIBEROPandaPredicateStatus.BLOCKED
    assert "approved_task_predicate_mismatch" in evaluation.reasons
    assert evaluation.evaluated_outcome_claim is False


def test_stale_episode_is_unverified_and_predicate_is_not_evaluated() -> None:
    evaluation = _evaluate(evaluated_at="2026-07-31T00:01:00+00:00")

    assert evaluation.evidence_readiness is (MissionEvidenceReadiness.INCOMPLETE)
    assert evaluation.status is LIBEROPandaPredicateStatus.UNVERIFIED
    assert evaluation.predicate_package_evaluated is False
    assert evaluation.evaluated_outcome_claim is False
    assert "observation_stale:libero_panda_episode_result" in evaluation.reasons


def test_receipt_binding_mutation_is_unverified() -> None:
    contract = _contract()
    replay = build_libero_panda_replay_input(
        contract=contract,
        content=_content(),
    )
    observation = replace(
        replay.evidence.observations[0],
        source_receipt_binding_sha256="d" * 64,
    )
    replay = replace(
        replay,
        evidence=replace(
            replay.evidence,
            observations=(observation,),
        ),
    )

    evaluation = evaluate_libero_panda_predicate(
        contract=contract,
        replay=replay,
        evaluated_at=EVALUATED_AT,
    )

    assert evaluation.evidence_readiness is (MissionEvidenceReadiness.INCOMPLETE)
    assert evaluation.status is LIBEROPandaPredicateStatus.UNVERIFIED
    assert (
        "observation_source_receipt_binding_mismatch:"
        "libero_panda_episode_result" in evaluation.reasons
    )


def test_four_dimensional_projection_is_explicit_and_recomputed() -> None:
    configuration = _runner_configuration(action_dim=4)
    evaluation = _evaluate(
        contract=_contract(configuration=configuration),
        content=_content(action_dim=4),
    )

    assert evaluation.status is LIBEROPandaPredicateStatus.SATISFIED
    assert evaluation.evaluated_outcome_claim is True


def test_parent_contract_accepts_libero_as_third_concrete_package() -> None:
    first = build_px4_gazebo_delivery_replay_contract(
        contract_id="three-stage:stage_1",
        contract_version="v1",
        approved_drop_zone={
            "frame": "map",
            "center": {"x_m": 4.0, "y_m": 2.0},
            "radius_m": 1.0,
        },
        approved_payload_release_rule={
            "requires_landing_before_release": True,
        },
        approved_same_session_rule={
            "mission_upload_and_release_share_session": True,
        },
        maximum_observation_age_seconds=30.0,
    )
    second = build_nav2_turtlebot3_replay_contract(
        contract_id="three-stage:stage_2",
        contract_version="v1",
        approved_goal_pose={
            "frame_id": "map",
            "x_m": 1.0,
            "y_m": 0.0,
            "yaw_rad": 0.0,
        },
        approved_goal_frame={"frame_id": "map"},
        maximum_observation_age_seconds=30.0,
    )
    third = _contract()
    first_before = first.to_material()
    second_before = second.to_material()
    parent = FrozenParentMissionContract(
        parent_mission_id="three-stage",
        parent_mission_version="v1",
        shared_target_descriptor_sha256=canonical_sha256(
            {
                "descriptor_id": "approved-cross-executor-target:v1",
                "physical_identity_asserted": False,
                "shared_world_asserted": False,
            }
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason=(
                "The composition proves authority and evidence lineage "
                "across three separate simulator worlds."
            ),
        ),
        stages=(
            build_parent_mission_stage_binding(
                stage_index=1,
                stage_ref="stage_1",
                executor_ref="sim-executor:px4",
                child_contract=first,
            ),
            build_parent_mission_stage_binding(
                stage_index=2,
                stage_ref="stage_2",
                executor_ref="sim-executor:nav2",
                child_contract=second,
            ),
            build_parent_mission_stage_binding(
                stage_index=3,
                stage_ref="stage_3",
                executor_ref="policy-path:groot-libero",
                child_contract=third,
            ),
        ),
    )
    approval = build_parent_mission_approval_binding(
        contract=parent,
        operator_approval_ref="approval:three-stage",
        authority_bundle_ref="catalog:three-stage:v1",
    )

    assert first.to_material() == first_before
    assert second.to_material() == second_before
    assert validate_frozen_parent_mission_contract(parent) == ()
    assert (
        validate_parent_mission_approval_binding(
            contract=parent,
            approval=approval,
        )
        == ()
    )
    assert parent.identity_continuity_claimed is False
    assert parent.shared_world_claimed is False
    assert parent.stages[2].predicate_package == (libero_panda_predicate_package_binding())


def test_runner_configuration_requires_one_frozen_vector_environment() -> None:
    with pytest.raises(ValueError, match="n_envs_mismatch"):
        build_libero_panda_replay_contract(
            contract_id="invalid",
            contract_version="v1",
            runner_configuration=replace(
                _runner_configuration(),
                n_envs=5,
            ),
            run_identity=RUN_ID,
            episode_identity=EPISODE_ID,
            maximum_observation_age_seconds=30.0,
        )


def test_task_predicate_reference_is_a_simulator_definition() -> None:
    contract = _contract()
    task_reference = next(
        item for item in contract.reference_inputs if item.input_id == "approved_task_predicate"
    )

    assert isinstance(task_reference, ReferenceInput)
    assert task_reference.kind == ("approved_pinned_libero_simulator_predicate")
    assert task_reference.content_sha256 == LIBERO_TASK_PREDICATE_SHA256

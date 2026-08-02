from __future__ import annotations

from dataclasses import replace
import pytest

from missionos_core import (
    EvidenceOrigin,
    HardwareExecutionMode,
    MissionEvidenceReadiness,
    MissionRuntimeEvidence,
    VerificationBasis,
)
from src.runtime.nav2_turtlebot3_predicate_package import (
    Nav2PredicateStatus,
    Nav2TurtleBot3BoundedDispatchResult,
    Nav2TurtleBot3EvidenceBindings,
    Nav2TurtleBot3PredicateContent,
    Nav2TurtleBot3ReplayInput,
    build_nav2_turtlebot3_replay_contract,
    build_nav2_turtlebot3_replay_input,
    evaluate_nav2_turtlebot3_predicate,
)


OBSERVED_AT = "2026-07-28T23:10:00+00:00"
EVALUATED_AT = "2026-07-28T23:10:01+00:00"
VALID_SHA = "a" * 64


def _result(
    *,
    basis: str = "nav2_goal_succeeded",
    motion: bool = True,
) -> Nav2TurtleBot3BoundedDispatchResult:
    already = basis == "already_at_goal_pose"
    return Nav2TurtleBot3BoundedDispatchResult.model_validate(
        {
            "result_id": "nav2-live-run-001",
            "observed_at": OBSERVED_AT,
            "requested_goal_pose": {
                "frame_id": "map",
                "x_m": 0.75,
                "y_m": 0.0,
                "yaw_rad": 0.0,
                "tolerance_m": 0.25,
                "max_speed_mps": 0.25,
                "max_distance_m": 3.0,
                "label": "turtlebot3_short_nav2_goal",
            },
            "bridge_response": {
                "action": "send_goal_pose",
                "ack_status": "accepted",
                "ack_source": "/navigate_to_pose",
                "goal_accepted": True,
                "nav2_status": "succeeded",
                "nav2_goal_succeeded": True,
                "runtime_progress_observed": True,
                "completion_observed": True,
                "completion_basis": basis,
                "blocking_reasons": [],
                "physical_execution_invoked": False,
                "raw_velocity_invoked": False,
                "raw_velocity_published": False,
                "raw_ros_topic_published": False,
                "cmd_vel_published_by_missionos": False,
                "state_result": {
                    "nav2_action_server_available": True,
                    "nav2_goal_succeeded": True,
                    "pose_observed": True,
                    "robot_motion_observed": motion,
                    "odom_before_observed": True,
                    "odom_after_observed": True,
                    "odom_delta_m": 0.253 if motion else 0.0,
                    "completion_basis": basis,
                    "goal_already_satisfied_observed": already,
                },
                "progress_result": {
                    "runtime_progress_observed": True,
                    "completion_observed": True,
                    "nav2_goal_succeeded": True,
                    "nav2_status": "succeeded",
                    "robot_motion_observed": motion,
                    "completion_basis": basis,
                    "goal_already_satisfied_observed": already,
                    "feedback_count": 228,
                },
            },
            "adapter_evidence": {
                "adapter_id": "ros2_nav2_ground_robot_adapter.v1",
                "adapter_kind": "ros2_nav2",
                "vehicle_class": "ground_robot",
                "execution_mode": "sim",
                "missionos_action_ref": "missionos_plan_turtlebot3_goal",
                "adapter_action_kind": "nav2_goal_pose",
                "operator_approval_ref": "approval-001",
                "preflight_status": "passed",
                "dispatch_status": "sent",
                "dispatch_request_sent": True,
                "command_ack_observed": True,
                "ack_source": "/navigate_to_pose",
                "ack_status": "accepted",
                "runtime_state_observed": True,
                "runtime_progress_observed": True,
                "completion_claimed": True,
                "completion_scope": "sim_action",
                "physical_execution_invoked": False,
                "safe_stop_requested": False,
                "abort_requested": False,
                "telemetry_fresh": True,
                "blocking_reasons": [],
                "unproven_claims": [
                    "simulator_evidence_not_physical",
                    "physical_execution_not_invoked",
                ],
            },
        }
    )


def _contract():
    return build_nav2_turtlebot3_replay_contract(
        contract_id="contract-nav2-001",
        contract_version="1",
        approved_goal_pose={
            "frame_id": "map",
            "x_m": 0.75,
            "y_m": 0.0,
            "yaw_rad": 0.0,
            "tolerance_m": 0.25,
            "max_speed_mps": 0.25,
            "max_distance_m": 3.0,
            "label": "turtlebot3_short_nav2_goal",
        },
        approved_goal_frame={"frame_id": "map"},
        maximum_observation_age_seconds=30.0,
    )


def _content(
    result: Nav2TurtleBot3BoundedDispatchResult | None = None,
) -> Nav2TurtleBot3PredicateContent:
    return Nav2TurtleBot3PredicateContent.from_result(
        result or _result(),
        evidence_bindings=Nav2TurtleBot3EvidenceBindings(
            bridge_response_sha256=VALID_SHA,
            adapter_evidence_sha256="b" * 64,
        ),
    )


def _evaluate(content: Nav2TurtleBot3PredicateContent):
    contract = _contract()
    return evaluate_nav2_turtlebot3_predicate(
        contract=contract,
        replay=build_nav2_turtlebot3_replay_input(
            contract=contract,
            content=content,
        ),
        evaluated_at=EVALUATED_AT,
    )


def test_live_shape_satisfies_succeeded_with_motion() -> None:
    evaluation = _evaluate(_content())

    assert evaluation.evidence_readiness is MissionEvidenceReadiness.READY
    assert evaluation.status is Nav2PredicateStatus.SATISFIED
    assert evaluation.satisfied_alternative == "succeeded_with_motion"
    assert evaluation.evaluated_outcome_claim is True
    assert evaluation.actual_verification_basis is VerificationBasis.DETERMINISTIC
    assert evaluation.evidence_origins == (EvidenceOrigin.STORED_ARTIFACT,)
    assert evaluation.approval_created is False
    assert evaluation.dispatch_authority_created is False
    assert evaluation.runtime_effect_requested is False
    assert evaluation.operational_closure_created is False
    assert evaluation.physical_execution_invoked is False


def test_already_at_goal_is_separate_named_alternative() -> None:
    evaluation = _evaluate(
        _content(_result(basis="already_at_goal_pose", motion=False))
    )

    assert evaluation.status is Nav2PredicateStatus.SATISFIED
    assert evaluation.satisfied_alternative == "already_at_goal"


def test_position_tolerance_cancel_is_not_nav2_action_success() -> None:
    result = _result(basis="position_tolerance_with_confirmed_cancel")
    result = result.model_copy(
        update={
            "bridge_response": result.bridge_response.model_copy(
                update={
                    "nav2_status": "position_tolerance_reached",
                    "nav2_goal_succeeded": False,
                    "state_result": result.bridge_response.state_result.model_copy(
                        update={"nav2_goal_succeeded": False}
                    ),
                    "progress_result": (
                        result.bridge_response.progress_result.model_copy(
                            update={
                                "nav2_status": "position_tolerance_reached",
                                "nav2_goal_succeeded": False,
                            }
                        )
                    ),
                }
            )
        }
    )

    evaluation = _evaluate(_content(result))

    assert evaluation.status is Nav2PredicateStatus.NOT_SATISFIED
    assert evaluation.evaluated_outcome_claim is False
    assert "nav2_goal_result_not_succeeded" in evaluation.reasons


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("source_schema_version", "wrong", "source_schema_invalid"),
        ("bridge_action", "cancel_goal", "bridge_action_invalid"),
        ("ack_status", "rejected", "goal_not_accepted"),
        ("goal_accepted", False, "goal_not_accepted"),
        (
            "nav2_goal_succeeded",
            False,
            "nav2_goal_result_not_succeeded",
        ),
        (
            "runtime_progress_observed",
            False,
            "runtime_progress_not_observed",
        ),
        ("completion_observed", False, "completion_not_observed"),
        (
            "state_nav2_goal_succeeded",
            False,
            "nav2_goal_result_not_succeeded",
        ),
        (
            "progress_nav2_goal_succeeded",
            False,
            "nav2_goal_result_not_succeeded",
        ),
        (
            "progress_runtime_progress_observed",
            False,
            "runtime_progress_not_observed",
        ),
        (
            "progress_completion_observed",
            False,
            "completion_not_observed",
        ),
        ("adapter_id", "other", "adapter_identity_invalid"),
        ("adapter_kind", "vendor_specific", "adapter_identity_invalid"),
        (
            "adapter_action_kind",
            "nav2_cancel_goal",
            "adapter_identity_invalid",
        ),
        ("adapter_execution_mode", "loopback", "adapter_identity_invalid"),
        (
            "adapter_dispatch_request_sent",
            False,
            "adapter_dispatch_not_observed",
        ),
        (
            "adapter_command_ack_observed",
            False,
            "adapter_ack_not_observed",
        ),
        ("adapter_ack_status", "rejected", "adapter_ack_not_observed"),
        (
            "state_robot_motion_observed",
            False,
            "completion_alternative_not_satisfied",
        ),
        (
            "progress_robot_motion_observed",
            False,
            "completion_alternative_not_satisfied",
        ),
        (
            "state_odom_before_observed",
            False,
            "completion_alternative_not_satisfied",
        ),
        (
            "state_odom_after_observed",
            False,
            "completion_alternative_not_satisfied",
        ),
        (
            "state_odom_delta_m",
            0.0,
            "completion_alternative_not_satisfied",
        ),
        (
            "adapter_completion_claimed",
            False,
            "adapter_completion_not_claimed",
        ),
        (
            "adapter_completion_scope",
            "none",
            "adapter_completion_scope_invalid",
        ),
        ("adapter_blocking_reasons", ("blocked",), "adapter_blocked"),
        (
            "physical_execution_invoked",
            True,
            "physical_execution_claim_invalid",
        ),
        (
            "forbidden_velocity_or_topic_claimed",
            True,
            "forbidden_velocity_or_topic_claimed",
        ),
    ],
)
def test_each_required_fact_fails_closed(
    field: str,
    value: object,
    expected_reason: str,
) -> None:
    evaluation = _evaluate(replace(_content(), **{field: value}))

    assert evaluation.status is Nav2PredicateStatus.NOT_SATISFIED
    assert evaluation.evaluated_outcome_claim is False
    assert expected_reason in evaluation.reasons


def test_one_sided_already_at_goal_does_not_satisfy() -> None:
    content = _content(_result(basis="already_at_goal_pose", motion=False))

    evaluation = _evaluate(
        replace(content, state_goal_already_satisfied_observed=False)
    )

    assert evaluation.status is Nav2PredicateStatus.NOT_SATISFIED
    assert evaluation.satisfied_alternative is None


@pytest.mark.parametrize(
    "bindings",
    [
        Nav2TurtleBot3EvidenceBindings(
            bridge_response_sha256="bad",
            adapter_evidence_sha256="b" * 64,
        ),
        Nav2TurtleBot3EvidenceBindings(
            bridge_response_sha256="a" * 64,
            adapter_evidence_sha256="bad",
        ),
    ],
)
def test_source_digest_mismatch_does_not_satisfy(
    bindings: Nav2TurtleBot3EvidenceBindings,
) -> None:
    evaluation = _evaluate(replace(_content(), evidence_bindings=bindings))

    assert evaluation.status is Nav2PredicateStatus.NOT_SATISFIED
    assert evaluation.evaluated_outcome_claim is False


def test_stale_observation_never_reaches_package() -> None:
    contract = _contract()
    content = _content()
    replay = build_nav2_turtlebot3_replay_input(
        contract=contract,
        content=content,
    )

    evaluation = evaluate_nav2_turtlebot3_predicate(
        contract=contract,
        replay=replay,
        evaluated_at="2026-07-28T23:11:00+00:00",
    )

    assert evaluation.status is Nav2PredicateStatus.UNVERIFIED
    assert evaluation.predicate_package_evaluated is False
    assert evaluation.actual_verification_basis is VerificationBasis.UNVERIFIED


def test_wrong_scope_and_origin_never_reach_package() -> None:
    contract = _contract()
    content = _content()
    replay = build_nav2_turtlebot3_replay_input(
        contract=contract,
        content=content,
    )
    observation = replay.evidence.observations[0]
    bad_observation = replace(
        observation,
        origin=EvidenceOrigin.OPERATOR_DECLARED,
        execution_scope=HardwareExecutionMode.LOOPBACK,
    )

    evaluation = evaluate_nav2_turtlebot3_predicate(
        contract=contract,
        replay=Nav2TurtleBot3ReplayInput(
            content=content,
            evidence=MissionRuntimeEvidence(
                contract_sha256=contract.contract_sha256,
                observations=(bad_observation,),
            ),
        ),
        evaluated_at=EVALUATED_AT,
    )

    assert evaluation.status is Nav2PredicateStatus.UNVERIFIED
    assert evaluation.predicate_package_evaluated is False


def test_requested_goal_must_match_frozen_reference() -> None:
    contract = _contract()
    content = replace(_content(), requested_goal_pose_sha256="c" * 64)

    evaluation = evaluate_nav2_turtlebot3_predicate(
        contract=contract,
        replay=build_nav2_turtlebot3_replay_input(
            contract=contract,
            content=content,
        ),
        evaluated_at=EVALUATED_AT,
    )

    assert evaluation.status is Nav2PredicateStatus.BLOCKED
    assert evaluation.predicate_package_evaluated is False
    assert "approved_goal_binding_mismatch" in evaluation.reasons


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("bridge_response", "goal_accepted"),
        ("bridge_response", "nav2_goal_succeeded"),
        ("adapter_evidence", "dispatch_request_sent"),
        ("adapter_evidence", "completion_claimed"),
    ],
)
def test_string_boolean_source_facts_are_rejected(
    section: str,
    field: str,
) -> None:
    payload = _result().model_dump(mode="json")
    payload[section][field] = "true"

    with pytest.raises(ValueError):
        Nav2TurtleBot3BoundedDispatchResult.model_validate(payload)


def test_content_digest_mismatch_blocks_package() -> None:
    contract = _contract()
    content = _content()
    replay = build_nav2_turtlebot3_replay_input(
        contract=contract,
        content=content,
    )

    evaluation = evaluate_nav2_turtlebot3_predicate(
        contract=contract,
        replay=replace(replay, content=replace(content, state_odom_delta_m=9.0)),
        evaluated_at=EVALUATED_AT,
    )

    assert evaluation.status is Nav2PredicateStatus.BLOCKED
    assert "predicate_observation_content_binding_mismatch" in evaluation.reasons

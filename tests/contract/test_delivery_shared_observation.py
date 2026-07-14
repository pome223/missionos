from datetime import timedelta

import pytest

from src.runtime.delivery_shared_observation import (
    DeliverySharedObservationError,
    MissionSharedObservation,
    SharedObservationKind,
    build_delivery_vehicle_decision_context,
    build_intra_mission_shared_observation_epic_exit_result,
    validate_shared_observation_refs,
)
from tests.fixtures.delivery_shared_observation import (
    NOW,
    build_shared_observation_bundle,
)


SHARED_AUTHORITY_FIELDS = (
    "command_authority_granted",
    "dispatch_authority_granted",
    "raw_mavlink_command_allowed",
    "raw_ros_action_allowed",
    "gazebo_entity_mutation_allowed",
    "setpoint_stream_allowed",
    "actuator_command_allowed",
    "hardware_target_allowed",
    "physical_execution_invoked",
    "approval_free_stronger_execution_allowed",
)


@pytest.mark.parametrize(
    "observation_kind",
    (SharedObservationKind.VEHICLE_POSE, SharedObservationKind.HAZARD_REPORT),
    ids=lambda kind: kind.value,
)
def test_shared_observation_is_citable_but_never_command_authority(
    observation_kind: SharedObservationKind,
) -> None:
    bundle = build_shared_observation_bundle(observation_kind=observation_kind)
    evidence = validate_shared_observation_refs(
        mission_session=bundle.mission,
        vehicle_sessions=bundle.vehicle_sessions,
        shared_observation=bundle.shared,
        decision_at=NOW + timedelta(seconds=5),
        decision_shared_observation_refs=[bundle.shared_ref],
    )

    assert evidence.shared_observation_ref == bundle.shared_ref
    assert bundle.shared.advisory_only is True
    assert bundle.shared.shared_observation_is_command_authority is False
    for field in SHARED_AUTHORITY_FIELDS:
        assert getattr(bundle.shared, field) is False

    with pytest.raises(DeliverySharedObservationError):
        validate_shared_observation_refs(
            mission_session=bundle.mission,
            vehicle_sessions=bundle.vehicle_sessions,
            shared_observation=bundle.shared,
            decision_at=NOW + timedelta(seconds=1),
            decision_shared_observation_refs=[bundle.shared_ref],
        )


def test_decision_context_rejects_future_and_stale_shared_observations() -> None:
    bundle = build_shared_observation_bundle()
    context = build_delivery_vehicle_decision_context(
        mission_session=bundle.mission,
        vehicle_session=bundle.vehicle_b,
        vehicle_sessions=bundle.vehicle_sessions,
        decision_ref="delivery_recovery_decision:shared-observation-fixture",
        shared_observations=[bundle.shared],
        decision_at=NOW + timedelta(seconds=5),
        max_observation_age_seconds=10.0,
    )

    assert context.shared_observation_refs == (bundle.shared_ref,)
    assert len(context.shared_observation_validation_evidence) == 1
    assert context.shared_observation_decision_context_only is True
    assert context.shared_observation_grants_command_authority is False
    assert context.shared_observation_used_as_success_proof is False
    assert context.shared_observation_used_as_scorecard_evidence is False
    assert context.shared_observation_payload_copied_to_observed_facts is False
    for field in SHARED_AUTHORITY_FIELDS[1:-1]:
        assert getattr(context, field) is False

    with pytest.raises(DeliverySharedObservationError):
        build_delivery_vehicle_decision_context(
            mission_session=bundle.mission,
            vehicle_session=bundle.vehicle_b,
            vehicle_sessions=bundle.vehicle_sessions,
            decision_ref="delivery_recovery_decision:future-observation",
            shared_observations=[bundle.shared],
            decision_at=NOW + timedelta(seconds=1),
        )

    stale_payload = bundle.shared.model_dump(mode="json")
    stale_payload["received_at"] = (NOW + timedelta(seconds=60)).isoformat()
    stale = MissionSharedObservation.model_validate(stale_payload)
    with pytest.raises(DeliverySharedObservationError):
        build_delivery_vehicle_decision_context(
            mission_session=bundle.mission,
            vehicle_session=bundle.vehicle_b,
            vehicle_sessions=bundle.vehicle_sessions,
            decision_ref="delivery_recovery_decision:stale-observation",
            shared_observations=[stale],
            decision_at=NOW + timedelta(seconds=61),
            max_observation_age_seconds=5.0,
        )


def test_shared_observation_epic_exit_records_negative_checks_without_authority() -> None:
    bundle = build_shared_observation_bundle()
    context = build_delivery_vehicle_decision_context(
        mission_session=bundle.mission,
        vehicle_session=bundle.vehicle_b,
        vehicle_sessions=bundle.vehicle_sessions,
        decision_ref="delivery_recovery_decision:shared-observation-epic-exit",
        shared_observations=[bundle.shared],
        decision_at=NOW + timedelta(seconds=5),
        max_observation_age_seconds=10.0,
    )
    result = build_intra_mission_shared_observation_epic_exit_result(
        mission_session=bundle.mission,
        source_vehicle_session=bundle.vehicle_a,
        consuming_vehicle_session=bundle.vehicle_b,
        shared_observation=bundle.shared,
        decision_context=context,
        future_observation_negative_failed_closed=True,
        stale_observation_negative_failed_closed=True,
        completed_at=NOW + timedelta(seconds=6),
    )

    assert result.source_observation_ref == bundle.source_observation_ref
    assert result.shared_observation_ref == bundle.shared_ref
    assert result.cited_shared_observation_refs == (bundle.shared_ref,)
    assert result.future_observation_negative_failed_closed is True
    assert result.stale_observation_negative_failed_closed is True
    assert result.shared_observation_is_command_authority is False
    for field in SHARED_AUTHORITY_FIELDS[1:]:
        assert getattr(result, field) is False

#!/usr/bin/env python3
"""Epic-exit smoke for Intra-Mission Shared Observation (#465)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from src.runtime.delivery_shared_observation import (
    DeliverySharedObservationError,
    MissionSharedObservation,
    SharedObservationEventSource,
    SharedObservationKind,
    build_delivery_mission_session,
    build_delivery_vehicle_decision_context,
    build_delivery_vehicle_observation_record,
    build_delivery_vehicle_session,
    build_intra_mission_shared_observation_epic_exit_result,
    build_mission_shared_observation,
)


def main() -> int:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    mission_ref = "delivery_mission_session:shared-observation-epic-exit"
    source_observation_ref = "px4_gazebo_vehicle_observation:vehicle-a-dropoff-hazard"
    source_record = build_delivery_vehicle_observation_record(
        observation_ref=source_observation_ref,
        event_source=SharedObservationEventSource.PX4_GAZEBO_SITL_TELEMETRY,
        observation_kind=SharedObservationKind.HAZARD_REPORT,
        observation_payload={
            "vehicle_id": "vehicle-a",
            "hazard_id": "dropoff-pad-temporary-obstruction",
            "severity": "warning",
        },
        observed_at=now,
    )
    vehicle_a = build_delivery_vehicle_session(
        vehicle_id="vehicle-a",
        mission_session_ref=mission_ref,
        telemetry_source_ref="px4_gazebo_sitl_telemetry:vehicle-a",
        observation_records=[source_record],
        created_at=now,
    )
    vehicle_b = build_delivery_vehicle_session(
        vehicle_id="vehicle-b",
        mission_session_ref=mission_ref,
        telemetry_source_ref="px4_gazebo_sitl_telemetry:vehicle-b",
        observation_records=[],
        created_at=now,
    )
    mission = build_delivery_mission_session(
        vehicle_sessions=[vehicle_a, vehicle_b],
        shared_observation_log_ref=(
            "mission_shared_observation_log:shared-observation-epic-exit"
        ),
        created_at=now,
    )
    shared = build_mission_shared_observation(
        mission_session_ref=mission_ref,
        source_vehicle_session_ref=(
            f"delivery_vehicle_session:{vehicle_a.vehicle_session_id}"
        ),
        source_observation_ref=source_observation_ref,
        event_source=SharedObservationEventSource.PX4_GAZEBO_SITL_TELEMETRY,
        observation_kind=SharedObservationKind.HAZARD_REPORT,
        observation_payload={
            "vehicle_id": "vehicle-a",
            "hazard_id": "dropoff-pad-temporary-obstruction",
        },
        observed_at=now,
        received_at=now + timedelta(seconds=2),
    )
    decision_context = build_delivery_vehicle_decision_context(
        mission_session=mission,
        vehicle_session=vehicle_b,
        vehicle_sessions=[vehicle_a, vehicle_b],
        decision_ref="delivery_recovery_decision:shared-observation-epic-exit",
        shared_observations=[shared],
        decision_at=now + timedelta(seconds=5),
        max_observation_age_seconds=10.0,
    )

    future_negative_failed_closed = False
    try:
        build_delivery_vehicle_decision_context(
            mission_session=mission,
            vehicle_session=vehicle_b,
            vehicle_sessions=[vehicle_a, vehicle_b],
            decision_ref="delivery_recovery_decision:future-observation",
            shared_observations=[shared],
            decision_at=now + timedelta(seconds=1),
        )
    except DeliverySharedObservationError:
        future_negative_failed_closed = True

    stale_negative_failed_closed = False
    stale_payload = shared.model_dump(mode="json")
    stale_payload["received_at"] = (now + timedelta(seconds=60)).isoformat()
    stale_shared = MissionSharedObservation.model_validate(stale_payload)
    try:
        build_delivery_vehicle_decision_context(
            mission_session=mission,
            vehicle_session=vehicle_b,
            vehicle_sessions=[vehicle_a, vehicle_b],
            decision_ref="delivery_recovery_decision:stale-observation",
            shared_observations=[stale_shared],
            decision_at=now + timedelta(seconds=61),
            max_observation_age_seconds=5.0,
        )
    except DeliverySharedObservationError:
        stale_negative_failed_closed = True

    result = build_intra_mission_shared_observation_epic_exit_result(
        mission_session=mission,
        source_vehicle_session=vehicle_a,
        consuming_vehicle_session=vehicle_b,
        shared_observation=shared,
        decision_context=decision_context,
        future_observation_negative_failed_closed=future_negative_failed_closed,
        stale_observation_negative_failed_closed=stale_negative_failed_closed,
        completed_at=now + timedelta(seconds=6),
    )
    summary = {
        "intra_mission_shared_observation_epic_exit_passed": True,
        "issue_465_covered": True,
        "epic_459_close_allowed": True,
        "result": result.model_dump(mode="json"),
        "mission_session_id": mission.mission_session_id,
        "vehicle_session_refs": list(mission.vehicle_session_refs),
        "source_vehicle_session_ref": result.source_vehicle_session_ref,
        "consuming_vehicle_session_ref": result.consuming_vehicle_session_ref,
        "source_observation_ref": result.source_observation_ref,
        "shared_observation_ref": result.shared_observation_ref,
        "decision_context_ref": result.decision_context_ref,
        "decision_ref": result.decision_ref,
        "decision_at": result.decision_at.isoformat(),
        "cited_shared_observation_refs": list(result.cited_shared_observation_refs),
        "validator_evidence": result.validator_evidence.model_dump(mode="json"),
        "future_observation_negative_failed_closed": future_negative_failed_closed,
        "stale_observation_negative_failed_closed": stale_negative_failed_closed,
        "shared_observation_is_command_authority": (
            result.shared_observation_is_command_authority
        ),
        "dispatch_authority_granted": result.dispatch_authority_granted,
        "raw_mavlink_command_allowed": result.raw_mavlink_command_allowed,
        "raw_ros_action_allowed": result.raw_ros_action_allowed,
        "gazebo_entity_mutation_allowed": result.gazebo_entity_mutation_allowed,
        "setpoint_stream_allowed": result.setpoint_stream_allowed,
        "actuator_command_allowed": result.actuator_command_allowed,
        "hardware_target_allowed": result.hardware_target_allowed,
        "physical_execution_invoked": result.physical_execution_invoked,
        "approval_free_stronger_execution_allowed": (
            result.approval_free_stronger_execution_allowed
        ),
        "public_sync_performed": False,
        "readme_or_architecture_updated": False,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

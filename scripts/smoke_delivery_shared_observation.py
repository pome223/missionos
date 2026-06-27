#!/usr/bin/env python3
"""Runtime smoke for intra-mission shared observation core artifacts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from src.runtime.delivery_shared_observation import (
    DeliverySharedObservationError,
    SharedObservationEventSource,
    SharedObservationKind,
    build_delivery_mission_session,
    build_delivery_vehicle_observation_record,
    build_delivery_vehicle_session,
    build_mission_shared_observation,
    validate_shared_observation_refs,
)


def main() -> int:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    mission_ref = "delivery_mission_session:shared-observation-smoke"
    source_observation_ref = "px4_gazebo_vehicle_observation:vehicle-a-pose-smoke"
    source_record = build_delivery_vehicle_observation_record(
        observation_ref=source_observation_ref,
        event_source=SharedObservationEventSource.PX4_GAZEBO_SITL_TELEMETRY,
        observation_kind=SharedObservationKind.VEHICLE_POSE,
        observation_payload={
            "vehicle_id": "vehicle-a",
            "position_x_m": 7.5,
            "position_y_m": -1.25,
            "position_z_m": 1.1,
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
        shared_observation_log_ref="mission_shared_observation_log:shared-observation-smoke",
        created_at=now,
    )
    shared = build_mission_shared_observation(
        mission_session_ref=mission_ref,
        source_vehicle_session_ref=(
            f"delivery_vehicle_session:{vehicle_a.vehicle_session_id}"
        ),
        source_observation_ref=source_observation_ref,
        event_source=SharedObservationEventSource.PX4_GAZEBO_SITL_TELEMETRY,
        observation_kind=SharedObservationKind.VEHICLE_POSE,
        observation_payload={
            "vehicle_id": "vehicle-a",
            "position_x_m": 7.5,
            "position_y_m": -1.25,
        },
        observed_at=now,
        received_at=now + timedelta(seconds=2),
    )
    shared_ref = f"mission_shared_observation:{shared.observation_id}"
    evidence = validate_shared_observation_refs(
        mission_session=mission,
        vehicle_sessions=[vehicle_a, vehicle_b],
        shared_observation=shared,
        decision_at=now + timedelta(seconds=5),
        decision_shared_observation_refs=[shared_ref],
    )

    temporal_negative_failed_closed = False
    try:
        validate_shared_observation_refs(
            mission_session=mission,
            vehicle_sessions=[vehicle_a, vehicle_b],
            shared_observation=shared,
            decision_at=now + timedelta(seconds=1),
            decision_shared_observation_refs=[shared_ref],
        )
    except DeliverySharedObservationError:
        temporal_negative_failed_closed = True

    summary = {
        "delivery_shared_observation_runtime_smoke_passed": True,
        "issues_covered": [460, 461, 462],
        "production_boundary": (
            "delivery mission session, vehicle session, shared observation, "
            "and validate_shared_observation_refs runtime builders"
        ),
        "mission_session_id": mission.mission_session_id,
        "vehicle_session_refs": list(mission.vehicle_session_refs),
        "shared_observation_ref": shared_ref,
        "source_vehicle_session_ref": shared.source_vehicle_session_ref,
        "source_observation_ref": shared.source_observation_ref,
        "validator_evidence": evidence.model_dump(mode="json"),
        "temporal_negative_failed_closed": temporal_negative_failed_closed,
        "advisory_only": shared.advisory_only,
        "shared_observation_is_command_authority": (
            shared.shared_observation_is_command_authority
        ),
        "command_authority_granted": shared.command_authority_granted,
        "dispatch_authority_granted": shared.dispatch_authority_granted,
        "raw_mavlink_command_allowed": shared.raw_mavlink_command_allowed,
        "raw_ros_action_allowed": shared.raw_ros_action_allowed,
        "gazebo_entity_mutation_allowed": shared.gazebo_entity_mutation_allowed,
        "setpoint_stream_allowed": shared.setpoint_stream_allowed,
        "actuator_command_allowed": shared.actuator_command_allowed,
        "hardware_target_allowed": shared.hardware_target_allowed,
        "physical_execution_invoked": shared.physical_execution_invoked,
        "approval_free_stronger_execution_allowed": (
            shared.approval_free_stronger_execution_allowed
        ),
        "public_sync_performed": False,
        "readme_or_architecture_updated": False,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if temporal_negative_failed_closed else 1


if __name__ == "__main__":
    raise SystemExit(main())

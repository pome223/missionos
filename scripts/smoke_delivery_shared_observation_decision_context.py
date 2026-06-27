#!/usr/bin/env python3
"""Runtime smoke for shared observation decision-context surfaces."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from src.runtime.delivery_shared_observation import (
    DeliverySharedObservationError,
    SharedObservationEventSource,
    SharedObservationKind,
    build_delivery_mission_session,
    build_delivery_vehicle_decision_context,
    build_delivery_vehicle_observation_record,
    build_delivery_vehicle_session,
    build_mission_shared_observation,
)


def main() -> int:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    mission_ref = "delivery_mission_session:decision-context-smoke"
    source_observation_ref = "px4_gazebo_vehicle_observation:vehicle-a-hazard-smoke"
    source_record = build_delivery_vehicle_observation_record(
        observation_ref=source_observation_ref,
        event_source=SharedObservationEventSource.PX4_GAZEBO_SITL_TELEMETRY,
        observation_kind=SharedObservationKind.HAZARD_REPORT,
        observation_payload={
            "vehicle_id": "vehicle-a",
            "hazard_id": "temporary-dropoff-obstruction",
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
        shared_observation_log_ref="mission_shared_observation_log:decision-context-smoke",
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
            "hazard_id": "temporary-dropoff-obstruction",
        },
        observed_at=now,
        received_at=now + timedelta(seconds=2),
    )
    context = build_delivery_vehicle_decision_context(
        mission_session=mission,
        vehicle_session=vehicle_b,
        vehicle_sessions=[vehicle_a, vehicle_b],
        decision_ref="delivery_recovery_decision:decision-context-smoke",
        shared_observations=[shared],
        decision_at=now + timedelta(seconds=5),
    )

    temporal_negative_failed_closed = False
    try:
        build_delivery_vehicle_decision_context(
            mission_session=mission,
            vehicle_session=vehicle_b,
            vehicle_sessions=[vehicle_a, vehicle_b],
            decision_ref="delivery_recovery_decision:decision-context-too-early",
            shared_observations=[shared],
            decision_at=now + timedelta(seconds=1),
        )
    except DeliverySharedObservationError:
        temporal_negative_failed_closed = True

    summary = {
        "delivery_shared_observation_decision_context_smoke_passed": True,
        "issue_463_covered": True,
        "production_boundary": (
            "delivery_vehicle_decision_context.v1 builder through "
            "validate_shared_observation_refs"
        ),
        "decision_context_id": context.decision_context_id,
        "decision_ref": context.decision_ref,
        "mission_session_ref": context.mission_session_ref,
        "vehicle_session_ref": context.vehicle_session_ref,
        "shared_observation_refs": list(context.shared_observation_refs),
        "ignored_shared_observation_refs": list(
            context.ignored_shared_observation_refs
        ),
        "validation_evidence_count": len(
            context.shared_observation_validation_evidence
        ),
        "temporal_negative_failed_closed": temporal_negative_failed_closed,
        "shared_observation_decision_context_only": (
            context.shared_observation_decision_context_only
        ),
        "shared_observation_grants_command_authority": (
            context.shared_observation_grants_command_authority
        ),
        "shared_observation_used_as_success_proof": (
            context.shared_observation_used_as_success_proof
        ),
        "shared_observation_used_as_scorecard_evidence": (
            context.shared_observation_used_as_scorecard_evidence
        ),
        "shared_observation_payload_copied_to_observed_facts": (
            context.shared_observation_payload_copied_to_observed_facts
        ),
        "dispatch_authority_granted": context.dispatch_authority_granted,
        "raw_mavlink_command_allowed": context.raw_mavlink_command_allowed,
        "raw_ros_action_allowed": context.raw_ros_action_allowed,
        "gazebo_entity_mutation_allowed": context.gazebo_entity_mutation_allowed,
        "setpoint_stream_allowed": context.setpoint_stream_allowed,
        "actuator_command_allowed": context.actuator_command_allowed,
        "hardware_target_allowed": context.hardware_target_allowed,
        "physical_execution_invoked": context.physical_execution_invoked,
        "public_sync_performed": False,
        "readme_or_architecture_updated": False,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if temporal_negative_failed_closed else 1


if __name__ == "__main__":
    raise SystemExit(main())

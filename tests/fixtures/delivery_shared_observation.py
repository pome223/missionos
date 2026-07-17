from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.runtime.delivery_shared_observation import (
    SharedObservationEventSource,
    SharedObservationKind,
    build_delivery_mission_session,
    build_delivery_vehicle_observation_record,
    build_delivery_vehicle_session,
    build_mission_shared_observation,
)


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SharedObservationBundle:
    mission_ref: str
    source_observation_ref: str
    mission: Any
    vehicle_a: Any
    vehicle_b: Any
    shared: Any

    @property
    def vehicle_sessions(self) -> tuple[Any, Any]:
        return (self.vehicle_a, self.vehicle_b)

    @property
    def shared_ref(self) -> str:
        return f"mission_shared_observation:{self.shared.observation_id}"


def build_shared_observation_bundle(
    *,
    observation_kind: SharedObservationKind = SharedObservationKind.HAZARD_REPORT,
    received_delay_seconds: float = 2.0,
) -> SharedObservationBundle:
    suffix = observation_kind.value.replace("_", "-")
    mission_ref = f"delivery_mission_session:shared-observation-{suffix}-fixture"
    source_observation_ref = (
        f"px4_gazebo_vehicle_observation:vehicle-a-{suffix}-fixture"
    )
    if observation_kind == SharedObservationKind.VEHICLE_POSE:
        source_payload = {
            "vehicle_id": "vehicle-a",
            "position_x_m": 7.5,
            "position_y_m": -1.25,
            "position_z_m": 1.1,
        }
        shared_payload = {
            "vehicle_id": "vehicle-a",
            "position_x_m": 7.5,
            "position_y_m": -1.25,
        }
    else:
        source_payload = {
            "vehicle_id": "vehicle-a",
            "hazard_id": "dropoff-pad-temporary-obstruction",
            "severity": "warning",
        }
        shared_payload = {
            "vehicle_id": "vehicle-a",
            "hazard_id": "dropoff-pad-temporary-obstruction",
        }
    source_record = build_delivery_vehicle_observation_record(
        observation_ref=source_observation_ref,
        event_source=SharedObservationEventSource.PX4_GAZEBO_SITL_TELEMETRY,
        observation_kind=observation_kind,
        observation_payload=source_payload,
        observed_at=NOW,
    )
    vehicle_a = build_delivery_vehicle_session(
        vehicle_id="vehicle-a",
        mission_session_ref=mission_ref,
        telemetry_source_ref="px4_gazebo_sitl_telemetry:vehicle-a",
        observation_records=[source_record],
        created_at=NOW,
    )
    vehicle_b = build_delivery_vehicle_session(
        vehicle_id="vehicle-b",
        mission_session_ref=mission_ref,
        telemetry_source_ref="px4_gazebo_sitl_telemetry:vehicle-b",
        observation_records=[],
        created_at=NOW,
    )
    mission = build_delivery_mission_session(
        vehicle_sessions=[vehicle_a, vehicle_b],
        shared_observation_log_ref=f"mission_shared_observation_log:{suffix}-fixture",
        created_at=NOW,
    )
    shared = build_mission_shared_observation(
        mission_session_ref=mission_ref,
        source_vehicle_session_ref=(
            f"delivery_vehicle_session:{vehicle_a.vehicle_session_id}"
        ),
        source_observation_ref=source_observation_ref,
        event_source=SharedObservationEventSource.PX4_GAZEBO_SITL_TELEMETRY,
        observation_kind=observation_kind,
        observation_payload=shared_payload,
        observed_at=NOW,
        received_at=NOW + timedelta(seconds=received_delay_seconds),
    )
    return SharedObservationBundle(
        mission_ref=mission_ref,
        source_observation_ref=source_observation_ref,
        mission=mission,
        vehicle_a=vehicle_a,
        vehicle_b=vehicle_b,
        shared=shared,
    )

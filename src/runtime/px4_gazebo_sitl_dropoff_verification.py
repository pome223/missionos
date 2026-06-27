"""SITL flight-fact dropoff verification.

This module verifies dropoff from observed PX4/Gazebo SITL facts. It does not
send MAVLink commands, mutate Gazebo, execute actuators, or infer payload
release from Mission OS markers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_mission_contract import DeliveryMissionContract
from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_SITL_PAYLOAD_RELEASE_EVENT_SCHEMA_VERSION = (
    "px4_gazebo_sitl_payload_release_event.v1"
)
PX4_GAZEBO_SITL_DROPOFF_FLIGHT_FACT_SCHEMA_VERSION = (
    "px4_gazebo_sitl_dropoff_flight_fact.v1"
)
PX4_GAZEBO_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION = (
    "px4_gazebo_sitl_dropoff_verification.v1"
)

SITL_DROPOFF_DEFAULT_ZONE_RADIUS_M = 0.5
SITL_DROPOFF_ABSOLUTE_ZONE_RADIUS_M = 5.0
SITL_DROPOFF_DEFAULT_ALTITUDE_TOLERANCE_M = 0.5
SITL_DROPOFF_ABSOLUTE_ALTITUDE_TOLERANCE_M = 2.0
SITL_DROPOFF_DEFAULT_RELEASE_TIME_WINDOW_SECONDS = 5.0
SITL_DROPOFF_ABSOLUTE_RELEASE_TIME_WINDOW_SECONDS = 30.0
SITL_DROPOFF_DEFAULT_MISSION_ITEM_SEQ = 2


class PX4GazeboSITLDropoffVerificationError(RuntimeError):
    """Raised when SITL dropoff verification cannot be built safely."""


class PX4GazeboSITLDropoffVerificationStatus(str, Enum):
    VERIFIED = "verified"
    PENDING = "pending"
    FAILED = "failed"


class PX4GazeboSITLPayloadReleaseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_SITL_PAYLOAD_RELEASE_EVENT_SCHEMA_VERSION] = (
        PX4_GAZEBO_SITL_PAYLOAD_RELEASE_EVENT_SCHEMA_VERSION
    )
    event_id: str
    event_source: Literal[
        "gazebo_gripper_detach_event",
        "gazebo_detachable_joint_detach_event",
        "mavlink_gripper_action_observed",
        "mavlink_actuator_release_observed",
    ]
    observed_at: datetime
    payload_id: str
    release_position_x_m: float
    release_position_y_m: float
    release_position_z_m: float
    release_observed: Literal[True] = True
    gazebo_event_observed: bool = False
    mavlink_release_observed: bool = False
    command_sent_by_mission_os: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    mavlink_dispatch_performed: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_source(self) -> "PX4GazeboSITLPayloadReleaseEvent":
        if self.event_source.startswith("gazebo_") and not self.gazebo_event_observed:
            raise PX4GazeboSITLDropoffVerificationError(
                "Gazebo payload release source requires gazebo_event_observed"
            )
        if (
            self.event_source.startswith("mavlink_")
            and not self.mavlink_release_observed
        ):
            raise PX4GazeboSITLDropoffVerificationError(
                "MAVLink-observed payload release source requires "
                "mavlink_release_observed"
            )
        return self


class PX4GazeboSITLDropoffFlightFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_SITL_DROPOFF_FLIGHT_FACT_SCHEMA_VERSION] = (
        PX4_GAZEBO_SITL_DROPOFF_FLIGHT_FACT_SCHEMA_VERSION
    )
    fact_id: str
    observed_at: datetime
    vehicle_id: str
    dropoff_zone_id: str
    position_x_m: float
    position_y_m: float
    position_z_m: float
    dropoff_target_x_m: float
    dropoff_target_y_m: float
    dropoff_target_altitude_m: float = 0.0
    mission_item_reached_observed: bool
    mission_item_reached_seq: int | None = None
    mission_item_reached_at: datetime | None = None
    payload_release_event_ref: str = ""
    telemetry_ref: str = ""
    sitl_mission_upload_receipt_ref: str = ""
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    mavlink_dispatch_performed_by_verifier: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", "mission_item_reached_at", mode="before")
    @classmethod
    def _coerce_timestamp(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class PX4GazeboSITLDropoffVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION] = (
        PX4_GAZEBO_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION
    )
    verification_id: str
    delivery_mission_contract_ref: str
    dropoff_flight_fact_ref: str
    payload_release_event_ref: str = ""
    sitl_mission_upload_receipt_ref: str = ""
    status: PX4GazeboSITLDropoffVerificationStatus
    dropoff_verified: bool
    dropoff_pending: bool
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    dropoff_zone_radius_m: float
    altitude_tolerance_m: float
    expected_mission_item_seq: int
    observed_distance_to_dropoff_m: float
    observed_altitude_error_m: float
    pose_within_dropoff_zone: bool
    altitude_within_tolerance: bool
    mission_item_reached: bool
    payload_release_observed: bool
    release_position_within_dropoff_zone: bool
    release_altitude_within_tolerance: bool
    release_within_mission_item_time_window: bool
    payload_release_observed_at: datetime | None = None
    release_position_x_m: float | None = None
    release_position_y_m: float | None = None
    release_position_z_m: float | None = None
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    command_sent_by_verifier: Literal[False] = False
    external_dispatch_performed_by_verifier: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    mavlink_dispatch_performed_by_verifier: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    px4_mission_upload_performed_by_verifier: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    verified_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "blocked_reasons",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return tuple(str(item) for item in value or ())

    @field_validator("verified_at", "payload_release_observed_at", mode="before")
    @classmethod
    def _coerce_optional_timestamp(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_status(self) -> "PX4GazeboSITLDropoffVerification":
        predicates = (
            self.pose_within_dropoff_zone,
            self.altitude_within_tolerance,
            self.mission_item_reached,
            self.payload_release_observed,
            self.release_position_within_dropoff_zone,
            self.release_altitude_within_tolerance,
            self.release_within_mission_item_time_window,
        )
        if self.status is PX4GazeboSITLDropoffVerificationStatus.VERIFIED:
            if not all(predicates):
                raise PX4GazeboSITLDropoffVerificationError(
                    "verified dropoff requires all flight-fact predicates"
                )
            if self.dropoff_verified is not True or self.dropoff_pending:
                raise PX4GazeboSITLDropoffVerificationError(
                    "verified dropoff status is inconsistent"
                )
            if self.blocked_reasons:
                raise PX4GazeboSITLDropoffVerificationError(
                    "verified dropoff cannot have blocked reasons"
                )
        else:
            if self.dropoff_verified:
                raise PX4GazeboSITLDropoffVerificationError(
                    "unverified dropoff cannot set dropoff_verified"
                )
            if not self.blocked_reasons:
                raise PX4GazeboSITLDropoffVerificationError(
                    "unverified dropoff requires blocked reasons"
                )
        return self


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _contract_ref(contract: DeliveryMissionContract) -> str:
    return f"delivery_mission_contract:{contract.contract_id}"


def _to_contract(
    value: DeliveryMissionContract | Mapping[str, Any],
) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_flight_fact(
    value: PX4GazeboSITLDropoffFlightFact | Mapping[str, Any],
) -> PX4GazeboSITLDropoffFlightFact:
    if isinstance(value, PX4GazeboSITLDropoffFlightFact):
        return value
    return PX4GazeboSITLDropoffFlightFact.model_validate(dict(value))


def _to_release_event(
    value: PX4GazeboSITLPayloadReleaseEvent | Mapping[str, Any] | None,
) -> PX4GazeboSITLPayloadReleaseEvent | None:
    if value is None:
        return None
    if isinstance(value, PX4GazeboSITLPayloadReleaseEvent):
        return value
    return PX4GazeboSITLPayloadReleaseEvent.model_validate(dict(value))


def _bounded_zone_radius(requested_radius_m: float) -> float:
    return min(float(requested_radius_m), SITL_DROPOFF_ABSOLUTE_ZONE_RADIUS_M)


def _bounded_altitude_tolerance(requested_tolerance_m: float) -> float:
    return min(
        float(requested_tolerance_m),
        SITL_DROPOFF_ABSOLUTE_ALTITUDE_TOLERANCE_M,
    )


def _bounded_release_time_window(requested_window_seconds: float) -> float:
    return min(
        float(requested_window_seconds),
        SITL_DROPOFF_ABSOLUTE_RELEASE_TIME_WINDOW_SECONDS,
    )


def _distance_m(*, x: float, y: float, target_x: float, target_y: float) -> float:
    return math.hypot(target_x - x, target_y - y)


def build_px4_gazebo_sitl_payload_release_event(
    *,
    event_source: Literal[
        "gazebo_gripper_detach_event",
        "gazebo_detachable_joint_detach_event",
        "mavlink_gripper_action_observed",
        "mavlink_actuator_release_observed",
    ],
    payload_id: str,
    release_position_x_m: float,
    release_position_y_m: float,
    release_position_z_m: float,
    observed_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PX4GazeboSITLPayloadReleaseEvent:
    observed = _utc(observed_at)
    payload = {
        "source": event_source,
        "payload_id": payload_id,
        "x": release_position_x_m,
        "y": release_position_y_m,
        "z": release_position_z_m,
        "observed_at": observed.isoformat(),
    }
    return PX4GazeboSITLPayloadReleaseEvent(
        event_id=_stable_id("px4_gazebo_sitl_payload_release_event", payload),
        event_source=event_source,
        observed_at=observed,
        payload_id=payload_id,
        release_position_x_m=release_position_x_m,
        release_position_y_m=release_position_y_m,
        release_position_z_m=release_position_z_m,
        gazebo_event_observed=event_source.startswith("gazebo_"),
        mavlink_release_observed=event_source.startswith("mavlink_"),
        metadata=dict(metadata or {}),
    )


def build_px4_gazebo_sitl_dropoff_flight_fact(
    *,
    vehicle_id: str,
    dropoff_zone_id: str,
    position_x_m: float,
    position_y_m: float,
    position_z_m: float,
    dropoff_target_x_m: float,
    dropoff_target_y_m: float,
    dropoff_target_altitude_m: float = 0.0,
    mission_item_reached_observed: bool,
    mission_item_reached_seq: int | None = None,
    mission_item_reached_at: datetime | None = None,
    payload_release_event: (
        PX4GazeboSITLPayloadReleaseEvent | Mapping[str, Any] | None
    ) = None,
    telemetry_ref: str = "",
    sitl_mission_upload_receipt_ref: str = "",
    observed_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PX4GazeboSITLDropoffFlightFact:
    observed = _utc(observed_at)
    release = _to_release_event(payload_release_event)
    payload = {
        "vehicle_id": vehicle_id,
        "zone_id": dropoff_zone_id,
        "position": (position_x_m, position_y_m, position_z_m),
        "target": (dropoff_target_x_m, dropoff_target_y_m, dropoff_target_altitude_m),
        "mission_item_reached": mission_item_reached_observed,
        "mission_item_reached_seq": mission_item_reached_seq,
        "release_ref": (
            f"px4_gazebo_sitl_payload_release_event:{release.event_id}"
            if release
            else ""
        ),
        "observed_at": observed.isoformat(),
    }
    return PX4GazeboSITLDropoffFlightFact(
        fact_id=_stable_id("px4_gazebo_sitl_dropoff_flight_fact", payload),
        observed_at=observed,
        vehicle_id=vehicle_id,
        dropoff_zone_id=dropoff_zone_id,
        position_x_m=position_x_m,
        position_y_m=position_y_m,
        position_z_m=position_z_m,
        dropoff_target_x_m=dropoff_target_x_m,
        dropoff_target_y_m=dropoff_target_y_m,
        dropoff_target_altitude_m=dropoff_target_altitude_m,
        mission_item_reached_observed=mission_item_reached_observed,
        mission_item_reached_seq=mission_item_reached_seq,
        mission_item_reached_at=mission_item_reached_at,
        payload_release_event_ref=(
            f"px4_gazebo_sitl_payload_release_event:{release.event_id}"
            if release
            else ""
        ),
        telemetry_ref=telemetry_ref,
        sitl_mission_upload_receipt_ref=sitl_mission_upload_receipt_ref,
        metadata=dict(metadata or {}),
    )


def build_px4_gazebo_sitl_dropoff_verification(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    dropoff_flight_fact: PX4GazeboSITLDropoffFlightFact | Mapping[str, Any],
    payload_release_event: (
        PX4GazeboSITLPayloadReleaseEvent | Mapping[str, Any] | None
    ) = None,
    dropoff_zone_radius_m: float = SITL_DROPOFF_DEFAULT_ZONE_RADIUS_M,
    altitude_tolerance_m: float = SITL_DROPOFF_DEFAULT_ALTITUDE_TOLERANCE_M,
    release_time_window_seconds: float = (
        SITL_DROPOFF_DEFAULT_RELEASE_TIME_WINDOW_SECONDS
    ),
    expected_mission_item_seq: int = SITL_DROPOFF_DEFAULT_MISSION_ITEM_SEQ,
    now: datetime | None = None,
) -> PX4GazeboSITLDropoffVerification:
    contract = _to_contract(delivery_mission_contract)
    fact = _to_flight_fact(dropoff_flight_fact)
    release = _to_release_event(payload_release_event)
    verified_at = _utc(now)
    effective_radius = _bounded_zone_radius(dropoff_zone_radius_m)
    effective_altitude_tolerance = _bounded_altitude_tolerance(altitude_tolerance_m)
    effective_release_time_window = _bounded_release_time_window(
        release_time_window_seconds
    )
    distance_m = _distance_m(
        x=fact.position_x_m,
        y=fact.position_y_m,
        target_x=fact.dropoff_target_x_m,
        target_y=fact.dropoff_target_y_m,
    )
    altitude_error_m = abs(fact.position_z_m - fact.dropoff_target_altitude_m)
    release_distance_m = (
        _distance_m(
            x=release.release_position_x_m,
            y=release.release_position_y_m,
            target_x=fact.dropoff_target_x_m,
            target_y=fact.dropoff_target_y_m,
        )
        if release
        else math.inf
    )
    release_altitude_error_m = (
        abs(release.release_position_z_m - fact.dropoff_target_altitude_m)
        if release
        else math.inf
    )
    pose_ok = distance_m <= effective_radius
    altitude_ok = altitude_error_m <= effective_altitude_tolerance
    mission_item_ok = (
        fact.mission_item_reached_observed
        and fact.mission_item_reached_seq == expected_mission_item_seq
    )
    release_position_ok = release_distance_m <= effective_radius
    release_altitude_ok = release_altitude_error_m <= effective_altitude_tolerance
    expected_release_ref = (
        f"px4_gazebo_sitl_payload_release_event:{release.event_id}" if release else ""
    )
    release_ref_matches = (
        bool(release)
        and bool(fact.payload_release_event_ref)
        and fact.payload_release_event_ref == expected_release_ref
    )
    release_time_delta_seconds = (
        abs((release.observed_at - fact.mission_item_reached_at).total_seconds())
        if release and fact.mission_item_reached_at
        else math.inf
    )
    release_time_ok = release_time_delta_seconds <= effective_release_time_window
    release_ok = (
        release is not None
        and release_ref_matches
        and release_position_ok
        and release_altitude_ok
        and release_time_ok
    )
    blocked: list[str] = []
    if not pose_ok:
        blocked.append("dropoff_failed_predicate_not_met")
        blocked.append("dropoff_pose_outside_zone")
    if not altitude_ok:
        blocked.append("dropoff_failed_predicate_not_met")
        blocked.append("dropoff_altitude_outside_tolerance")
    if not mission_item_ok:
        blocked.append("dropoff_failed_predicate_not_met")
        blocked.append("mission_item_reached_missing")
    if release and not release_ref_matches:
        blocked.append("dropoff_failed_predicate_not_met")
        blocked.append("payload_release_event_ref_mismatch")
    elif not release_ok:
        if release is None:
            blocked.append("dropoff_pending")
            blocked.append("payload_release_event_missing")
        else:
            blocked.append("dropoff_failed_predicate_not_met")
            if not release_position_ok:
                blocked.append("payload_release_position_outside_zone")
            if not release_altitude_ok:
                blocked.append("payload_release_altitude_outside_tolerance")
            if not release_time_ok:
                blocked.append("payload_release_not_near_mission_item_reached")
    blocked_reasons = tuple(dict.fromkeys(blocked))
    status = (
        PX4GazeboSITLDropoffVerificationStatus.VERIFIED
        if not blocked_reasons
        else (
            PX4GazeboSITLDropoffVerificationStatus.PENDING
            if blocked_reasons == ("dropoff_pending", "payload_release_event_missing")
            else PX4GazeboSITLDropoffVerificationStatus.FAILED
        )
    )
    payload = {
        "contract_ref": _contract_ref(contract),
        "fact_ref": f"px4_gazebo_sitl_dropoff_flight_fact:{fact.fact_id}",
        "release_ref": fact.payload_release_event_ref,
        "status": status.value,
        "blocked": blocked_reasons,
        "radius": effective_radius,
        "altitude_tolerance": effective_altitude_tolerance,
        "expected_seq": expected_mission_item_seq,
        "release_distance": release_distance_m,
        "release_altitude_error": release_altitude_error_m,
        "release_time_delta": release_time_delta_seconds,
    }
    return PX4GazeboSITLDropoffVerification(
        verification_id=_stable_id("px4_gazebo_sitl_dropoff_verification", payload),
        delivery_mission_contract_ref=_contract_ref(contract),
        dropoff_flight_fact_ref=f"px4_gazebo_sitl_dropoff_flight_fact:{fact.fact_id}",
        payload_release_event_ref=fact.payload_release_event_ref,
        sitl_mission_upload_receipt_ref=fact.sitl_mission_upload_receipt_ref,
        status=status,
        dropoff_verified=status is PX4GazeboSITLDropoffVerificationStatus.VERIFIED,
        dropoff_pending=status is PX4GazeboSITLDropoffVerificationStatus.PENDING,
        blocked_reasons=blocked_reasons,
        dropoff_zone_radius_m=effective_radius,
        altitude_tolerance_m=effective_altitude_tolerance,
        expected_mission_item_seq=expected_mission_item_seq,
        observed_distance_to_dropoff_m=distance_m,
        observed_altitude_error_m=altitude_error_m,
        pose_within_dropoff_zone=pose_ok,
        altitude_within_tolerance=altitude_ok,
        mission_item_reached=mission_item_ok,
        payload_release_observed=release_ok,
        release_position_within_dropoff_zone=release_position_ok,
        release_altitude_within_tolerance=release_altitude_ok,
        release_within_mission_item_time_window=release_time_ok,
        payload_release_observed_at=release.observed_at if release else None,
        release_position_x_m=release.release_position_x_m if release else None,
        release_position_y_m=release.release_position_y_m if release else None,
        release_position_z_m=release.release_position_z_m if release else None,
        verified_at=verified_at,
        metadata={
            "predicate": "pose_and_altitude_and_mission_item_and_payload_release",
            "default_zone_radius_m": SITL_DROPOFF_DEFAULT_ZONE_RADIUS_M,
            "absolute_zone_radius_m": SITL_DROPOFF_ABSOLUTE_ZONE_RADIUS_M,
            "default_altitude_tolerance_m": SITL_DROPOFF_DEFAULT_ALTITUDE_TOLERANCE_M,
            "absolute_altitude_tolerance_m": SITL_DROPOFF_ABSOLUTE_ALTITUDE_TOLERANCE_M,
            "release_time_delta_seconds": (
                release_time_delta_seconds
                if math.isfinite(release_time_delta_seconds)
                else None
            ),
            "release_time_window_seconds": effective_release_time_window,
            "default_release_time_window_seconds": (
                SITL_DROPOFF_DEFAULT_RELEASE_TIME_WINDOW_SECONDS
            ),
            "absolute_release_time_window_seconds": (
                SITL_DROPOFF_ABSOLUTE_RELEASE_TIME_WINDOW_SECONDS
            ),
            "release_distance_to_dropoff_m": (
                release_distance_m if math.isfinite(release_distance_m) else None
            ),
            "release_altitude_error_m": (
                release_altitude_error_m
                if math.isfinite(release_altitude_error_m)
                else None
            ),
        },
    )


def dropoff_evidence_from_sitl_verification(
    verification: PX4GazeboSITLDropoffVerification | Mapping[str, Any],
) -> dict[str, Any]:
    resolved = (
        verification
        if isinstance(verification, PX4GazeboSITLDropoffVerification)
        else PX4GazeboSITLDropoffVerification.model_validate(dict(verification))
    )
    return {
        "evidence_ref": (
            "px4_gazebo_sitl_dropoff_verification:" + resolved.verification_id
        ),
        "dropoff_verified": resolved.dropoff_verified,
        "landing_error_m": resolved.observed_distance_to_dropoff_m,
        "payload_release_observed_at": (
            resolved.payload_release_observed_at.isoformat()
            if resolved.payload_release_observed_at
            else None
        ),
        "pose_source": "sitl_flight_fact",
    }


def attach_px4_gazebo_sitl_dropoff_verification(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    dropoff_flight_fact: PX4GazeboSITLDropoffFlightFact | Mapping[str, Any],
    payload_release_event: (
        PX4GazeboSITLPayloadReleaseEvent | Mapping[str, Any] | None
    ) = None,
    dropoff_zone_radius_m: float = SITL_DROPOFF_DEFAULT_ZONE_RADIUS_M,
    altitude_tolerance_m: float = SITL_DROPOFF_DEFAULT_ALTITUDE_TOLERANCE_M,
    release_time_window_seconds: float = (
        SITL_DROPOFF_DEFAULT_RELEASE_TIME_WINDOW_SECONDS
    ),
    expected_mission_item_seq: int = SITL_DROPOFF_DEFAULT_MISSION_ITEM_SEQ,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboSITLDropoffVerificationError(
            f"task {task_id} not found; cannot attach SITL dropoff verification"
        )
    verification = build_px4_gazebo_sitl_dropoff_verification(
        delivery_mission_contract=delivery_mission_contract,
        dropoff_flight_fact=dropoff_flight_fact,
        payload_release_event=payload_release_event,
        dropoff_zone_radius_m=dropoff_zone_radius_m,
        altitude_tolerance_m=altitude_tolerance_m,
        release_time_window_seconds=release_time_window_seconds,
        expected_mission_item_seq=expected_mission_item_seq,
        now=now,
    )
    artifacts = {
        "px4_gazebo_sitl_dropoff_verification": verification.model_dump(mode="json")
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise PX4GazeboSITLDropoffVerificationError(
            f"task {task_id} disappeared while attaching SITL dropoff verification"
        )
    return {**artifacts, "task": updated}


__all__ = [
    "PX4_GAZEBO_SITL_DROPOFF_FLIGHT_FACT_SCHEMA_VERSION",
    "PX4_GAZEBO_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION",
    "PX4_GAZEBO_SITL_PAYLOAD_RELEASE_EVENT_SCHEMA_VERSION",
    "PX4GazeboSITLDropoffFlightFact",
    "PX4GazeboSITLDropoffVerification",
    "PX4GazeboSITLDropoffVerificationError",
    "PX4GazeboSITLDropoffVerificationStatus",
    "PX4GazeboSITLPayloadReleaseEvent",
    "SITL_DROPOFF_ABSOLUTE_ALTITUDE_TOLERANCE_M",
    "SITL_DROPOFF_ABSOLUTE_RELEASE_TIME_WINDOW_SECONDS",
    "SITL_DROPOFF_ABSOLUTE_ZONE_RADIUS_M",
    "SITL_DROPOFF_DEFAULT_ALTITUDE_TOLERANCE_M",
    "SITL_DROPOFF_DEFAULT_MISSION_ITEM_SEQ",
    "SITL_DROPOFF_DEFAULT_RELEASE_TIME_WINDOW_SECONDS",
    "SITL_DROPOFF_DEFAULT_ZONE_RADIUS_M",
    "attach_px4_gazebo_sitl_dropoff_verification",
    "build_px4_gazebo_sitl_dropoff_flight_fact",
    "build_px4_gazebo_sitl_dropoff_verification",
    "build_px4_gazebo_sitl_payload_release_event",
    "dropoff_evidence_from_sitl_verification",
]

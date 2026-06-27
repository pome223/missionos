"""Observed Digital Twin SITL waypoint reach receipt."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.digital_twin_sitl_arm_takeoff import (
    DigitalTwinSITLArmTakeoffReceipt,
    digital_twin_sitl_arm_takeoff_receipt_ref,
)
from src.runtime.digital_twin_sitl_execution_result import (
    DigitalTwinSITLExecutionResult,
    digital_twin_sitl_execution_result_ref,
)
from src.runtime.digital_twin_sitl_mavlink_upload import (
    DigitalTwinSITLMissionUploadReceipt,
    digital_twin_sitl_mission_upload_receipt_ref,
)
from src.runtime.flight_readiness_package import (
    FlightReadinessPackage,
    flight_readiness_package_ref,
)


DIGITAL_TWIN_SITL_WAYPOINT_REACH_SCHEMA_VERSION = (
    "digital_twin_sitl_waypoint_reach_observation.v1"
)


class DigitalTwinSITLWaypointReachError(RuntimeError):
    """Raised when waypoint reach observation overclaims facts."""


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _content_hash(payload: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


class DigitalTwinSITLWaypointReachObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DIGITAL_TWIN_SITL_WAYPOINT_REACH_SCHEMA_VERSION] = (
        DIGITAL_TWIN_SITL_WAYPOINT_REACH_SCHEMA_VERSION
    )
    observation_id: str
    flight_readiness_package_ref: str
    same_run_arm_takeoff_receipt_ref: str
    same_run_mission_upload_receipt_ref: str
    same_run_execution_result_ref: str
    target_waypoint_seq: int = Field(ge=1)
    target_latitude_deg: float = Field(ge=-90, le=90)
    target_longitude_deg: float = Field(ge=-180, le=180)
    mission_item_reached_seq: tuple[int, ...] = ()
    position_sample_count: int = Field(ge=0)
    altitude_profile: tuple[dict[str, Any], ...] = ()
    position_profile: tuple[dict[str, Any], ...] = ()
    distance_to_target_min_m: float = Field(ge=0)
    cruise_observed: bool
    mountain_hut_waypoint_reached: bool
    observation_timeout_s: float = Field(gt=0)
    observed_facts_only: Literal[True] = True
    simulation_only: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    observed_at: datetime
    receipt_hash: str
    sha256: str

    @field_validator("mission_item_reached_seq", "blocked_reasons", "warning_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @field_validator("altitude_profile", "position_profile", mode="before")
    @classmethod
    def _coerce_samples(cls, value: Any) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in (value or ()))

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_observation(self) -> "DigitalTwinSITLWaypointReachObservation":
        if not self.flight_readiness_package_ref.startswith("flight_readiness_package:"):
            raise DigitalTwinSITLWaypointReachError("waypoint observation requires FRP ref")
        if not self.same_run_arm_takeoff_receipt_ref.startswith(
            "digital_twin_sitl_arm_and_takeoff_observed:"
        ):
            raise DigitalTwinSITLWaypointReachError("waypoint observation requires takeoff ref")
        if not self.same_run_mission_upload_receipt_ref.startswith(
            "digital_twin_sitl_mission_upload_receipt:"
        ):
            raise DigitalTwinSITLWaypointReachError("waypoint observation requires upload ref")
        if not self.same_run_execution_result_ref.startswith(
            "digital_twin_sitl_execution_result:"
        ):
            raise DigitalTwinSITLWaypointReachError("waypoint observation requires execution ref")
        if self.receipt_hash != self.sha256:
            raise DigitalTwinSITLWaypointReachError("waypoint observation hash mismatch")
        if self.mountain_hut_waypoint_reached:
            if self.target_waypoint_seq not in self.mission_item_reached_seq:
                raise DigitalTwinSITLWaypointReachError("waypoint reached requires mission item reached seq")
            if self.distance_to_target_min_m > 25.0:
                raise DigitalTwinSITLWaypointReachError("waypoint reached requires distance <= 25m")
            if not self.cruise_observed:
                raise DigitalTwinSITLWaypointReachError("waypoint reached requires cruise observed")
            if self.blocked_reasons:
                raise DigitalTwinSITLWaypointReachError("reached waypoint cannot include blockers")
        elif not self.blocked_reasons:
            raise DigitalTwinSITLWaypointReachError("non-reached waypoint observation requires blockers")
        return self


def digital_twin_sitl_waypoint_reach_ref(
    observation: DigitalTwinSITLWaypointReachObservation,
) -> str:
    return f"digital_twin_sitl_waypoint_reach_observation:{observation.observation_id}"


def build_digital_twin_sitl_waypoint_reach_observation(
    *,
    flight_readiness_package: FlightReadinessPackage | Mapping[str, Any],
    arm_takeoff_receipt: DigitalTwinSITLArmTakeoffReceipt | Mapping[str, Any],
    mission_upload_receipt: DigitalTwinSITLMissionUploadReceipt | Mapping[str, Any],
    execution_result: DigitalTwinSITLExecutionResult | Mapping[str, Any],
    target_waypoint_seq: int,
    target_latitude_deg: float,
    target_longitude_deg: float,
    observed: Mapping[str, Any],
    now: datetime | None = None,
) -> DigitalTwinSITLWaypointReachObservation:
    package = (
        flight_readiness_package
        if isinstance(flight_readiness_package, FlightReadinessPackage)
        else FlightReadinessPackage.model_validate(flight_readiness_package)
    )
    takeoff = (
        arm_takeoff_receipt
        if isinstance(arm_takeoff_receipt, DigitalTwinSITLArmTakeoffReceipt)
        else DigitalTwinSITLArmTakeoffReceipt.model_validate(arm_takeoff_receipt)
    )
    upload = (
        mission_upload_receipt
        if isinstance(mission_upload_receipt, DigitalTwinSITLMissionUploadReceipt)
        else DigitalTwinSITLMissionUploadReceipt.model_validate(mission_upload_receipt)
    )
    execution = (
        execution_result
        if isinstance(execution_result, DigitalTwinSITLExecutionResult)
        else DigitalTwinSITLExecutionResult.model_validate(execution_result)
    )
    blocked: list[str] = []
    if package.readiness_status != "ready_for_human_hardware_review":
        blocked.append("flight_readiness_package_not_ready")
    if not takeoff.takeoff_observed:
        blocked.append("takeoff_not_observed")
    if not upload.mission_ack_observed:
        blocked.append("mission_ack_not_observed")
    if not execution.flight_telemetry_observed:
        blocked.append("execution_telemetry_not_observed")
    if package.execution_result_ref != digital_twin_sitl_execution_result_ref(execution):
        blocked.append("flight_readiness_package_execution_result_ref_mismatch")
    if takeoff.same_run_mission_upload_receipt_ref != digital_twin_sitl_mission_upload_receipt_ref(upload):
        blocked.append("takeoff_upload_ref_mismatch")
    if takeoff.same_run_execution_result_ref != digital_twin_sitl_execution_result_ref(execution):
        blocked.append("takeoff_execution_ref_mismatch")

    reached_seq = tuple(int(item) for item in observed.get("mission_item_reached_seq", ()))
    distance_min_value = observed.get("distance_to_target_min_m")
    distance_min = (
        999999.0
        if distance_min_value is None
        else float(distance_min_value)
    )
    cruise_observed = bool(observed.get("cruise_observed"))
    waypoint_reached = (
        int(target_waypoint_seq) in reached_seq
        and distance_min <= 25.0
        and cruise_observed
        and not blocked
    )
    if not waypoint_reached:
        if int(target_waypoint_seq) not in reached_seq:
            blocked.append("target_waypoint_seq_not_reached")
        if distance_min > 25.0:
            blocked.append("target_waypoint_distance_gt_25m")
        if not cruise_observed:
            blocked.append("cruise_not_observed")

    observed_at = _utc(now)
    package_ref = flight_readiness_package_ref(package)
    takeoff_ref = digital_twin_sitl_arm_takeoff_receipt_ref(takeoff)
    upload_ref = digital_twin_sitl_mission_upload_receipt_ref(upload)
    execution_ref = digital_twin_sitl_execution_result_ref(execution)
    payload = {
        "schema_version": DIGITAL_TWIN_SITL_WAYPOINT_REACH_SCHEMA_VERSION,
        "flight_readiness_package_ref": package_ref,
        "same_run_arm_takeoff_receipt_ref": takeoff_ref,
        "same_run_mission_upload_receipt_ref": upload_ref,
        "same_run_execution_result_ref": execution_ref,
        "target_waypoint_seq": int(target_waypoint_seq),
        "target_latitude_deg": float(target_latitude_deg),
        "target_longitude_deg": float(target_longitude_deg),
        "mission_item_reached_seq": reached_seq,
        "position_sample_count": int(observed.get("position_sample_count") or 0),
        "altitude_profile": tuple(observed.get("altitude_profile") or ()),
        "position_profile": tuple(observed.get("position_profile") or ()),
        "distance_to_target_min_m": distance_min,
        "cruise_observed": cruise_observed,
        "mountain_hut_waypoint_reached": waypoint_reached,
        "observation_timeout_s": float(observed.get("observation_timeout_s") or 0.0),
        "blocked_reasons": tuple(sorted(set(blocked))),
        "warning_reasons": tuple(observed.get("warning_reasons") or ()),
    }
    digest = _content_hash(payload)
    return DigitalTwinSITLWaypointReachObservation(
        observation_id="digital_twin_sitl_waypoint_reach_" + digest[:12],
        observed_at=observed_at,
        receipt_hash=digest,
        sha256=digest,
        **payload,
    )


__all__ = [
    "DIGITAL_TWIN_SITL_WAYPOINT_REACH_SCHEMA_VERSION",
    "DigitalTwinSITLWaypointReachError",
    "DigitalTwinSITLWaypointReachObservation",
    "build_digital_twin_sitl_waypoint_reach_observation",
    "digital_twin_sitl_waypoint_reach_ref",
]

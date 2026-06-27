"""Bounded pickup/dropoff route plan artifacts for PX4/Gazebo delivery."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PX4_GAZEBO_PICKUP_DROPOFF_ROUTE_PLAN_SCHEMA_VERSION = (
    "px4_gazebo_pickup_dropoff_route_plan.v3"
)
ROUTE_ON_DEVIATION_ACTIONS = ("abort_only", "hold", "land", "rtl")

_COMMAND_LIKE_MARKERS = (
    "cmd",
    "command",
    "mavlink",
    "vehicle_command",
    "setpoint",
    "mission_upload",
    "actuator",
    "dispatch",
)


class PX4GazeboRoutePlanError(RuntimeError):
    """Raised when a PX4/Gazebo route plan is unsafe."""


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


def _ordered_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values or ():
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _reject_command_like_metadata(metadata: Mapping[str, Any] | None) -> None:
    if not metadata:
        return
    encoded = json.dumps(metadata, ensure_ascii=True, sort_keys=True).lower()
    if any(marker in encoded for marker in _COMMAND_LIKE_MARKERS):
        raise PX4GazeboRoutePlanError(
            "route plan metadata must not contain command-like content"
        )


class PX4GazeboPickupDropoffRoutePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_PICKUP_DROPOFF_ROUTE_PLAN_SCHEMA_VERSION] = (
        PX4_GAZEBO_PICKUP_DROPOFF_ROUTE_PLAN_SCHEMA_VERSION
    )
    route_plan_id: str
    pickup_pad_ref: str = Field(min_length=1)
    dropoff_pad_ref: str = Field(min_length=1)
    route_waypoint_refs: tuple[str, ...]
    geofence_polygon: tuple[tuple[float, float], ...]
    altitude_min_m: float = Field(ge=0)
    altitude_max_m: float = Field(gt=0)
    min_battery_margin_pct: float = Field(ge=0, le=100)
    route_completion_radius_m: float = Field(gt=0)
    max_pose_deviation_xy_m: float = Field(gt=0)
    max_pose_deviation_z_m: float = Field(gt=0)
    max_velocity_m_s: float | None = Field(default=None, gt=0)
    on_deviation_action: Literal["abort_only", "hold", "land", "rtl"] = "abort_only"
    required_completion_evidence: tuple[str, ...]
    generated_at: datetime
    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    route_plan_only: Literal[True] = True
    operator_approval_required_before_dispatch: Literal[True] = True
    mavlink_frame_sent: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    route_command_dispatch_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator(
        "route_waypoint_refs",
        "required_completion_evidence",
        mode="before",
    )
    @classmethod
    def _coerce_strings(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @field_validator("geofence_polygon", mode="before")
    @classmethod
    def _coerce_polygon(cls, value: Any) -> tuple[tuple[float, float], ...]:
        return tuple((float(x), float(y)) for x, y in value)

    @model_validator(mode="after")
    def _validate_route(self) -> "PX4GazeboPickupDropoffRoutePlan":
        if self.pickup_pad_ref == self.dropoff_pad_ref:
            raise PX4GazeboRoutePlanError("pickup and dropoff pads must differ")
        if len(self.geofence_polygon) < 3:
            raise PX4GazeboRoutePlanError("route plan requires a geofence polygon")
        if self.altitude_max_m <= self.altitude_min_m:
            raise PX4GazeboRoutePlanError(
                "route plan altitude_max_m must be greater than altitude_min_m"
            )
        if self.min_battery_margin_pct < 10:
            raise PX4GazeboRoutePlanError(
                "route plan requires min_battery_margin_pct >= 10"
            )
        if self.max_pose_deviation_xy_m < self.route_completion_radius_m:
            raise PX4GazeboRoutePlanError(
                "route plan max_pose_deviation_xy_m must cover completion radius"
            )
        required = {
            "pickup_pad_departed",
            "dropoff_pad_reached",
            "px4_telemetry_correlated",
            "gazebo_pose_correlated",
        }
        if not required.issubset(set(self.required_completion_evidence)):
            raise PX4GazeboRoutePlanError(
                "route plan is missing required completion evidence"
            )
        return self


def build_px4_gazebo_pickup_dropoff_route_plan(
    *,
    pickup_pad_ref: str,
    dropoff_pad_ref: str,
    route_waypoint_refs: Sequence[str] = (),
    geofence_polygon: Sequence[tuple[float, float]],
    altitude_min_m: float,
    altitude_max_m: float,
    min_battery_margin_pct: float,
    route_completion_radius_m: float = 0.75,
    max_pose_deviation_xy_m: float = 2.0,
    max_pose_deviation_z_m: float = 1.5,
    max_velocity_m_s: float | None = None,
    on_deviation_action: Literal["abort_only", "hold", "land", "rtl"] = "abort_only",
    required_completion_evidence: Sequence[str] = (
        "pickup_pad_departed",
        "dropoff_pad_reached",
        "px4_telemetry_correlated",
        "gazebo_pose_correlated",
    ),
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboPickupDropoffRoutePlan:
    _reject_command_like_metadata(metadata)
    generated_at = _utc(now)
    payload = {
        "pickup_pad_ref": pickup_pad_ref,
        "dropoff_pad_ref": dropoff_pad_ref,
        "route_waypoint_refs": list(route_waypoint_refs),
        "geofence_polygon": list(geofence_polygon),
        "altitude_min_m": altitude_min_m,
        "altitude_max_m": altitude_max_m,
        "min_battery_margin_pct": min_battery_margin_pct,
        "max_pose_deviation_xy_m": max_pose_deviation_xy_m,
        "max_pose_deviation_z_m": max_pose_deviation_z_m,
        "max_velocity_m_s": max_velocity_m_s,
        "on_deviation_action": on_deviation_action,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboPickupDropoffRoutePlan(
        route_plan_id=_stable_id("px4_gazebo_pickup_dropoff_route_plan", payload),
        pickup_pad_ref=pickup_pad_ref,
        dropoff_pad_ref=dropoff_pad_ref,
        route_waypoint_refs=tuple(route_waypoint_refs),
        geofence_polygon=tuple(geofence_polygon),
        altitude_min_m=float(altitude_min_m),
        altitude_max_m=float(altitude_max_m),
        min_battery_margin_pct=float(min_battery_margin_pct),
        route_completion_radius_m=float(route_completion_radius_m),
        max_pose_deviation_xy_m=float(max_pose_deviation_xy_m),
        max_pose_deviation_z_m=float(max_pose_deviation_z_m),
        max_velocity_m_s=(
            None if max_velocity_m_s is None else float(max_velocity_m_s)
        ),
        on_deviation_action=on_deviation_action,
        required_completion_evidence=tuple(required_completion_evidence),
        generated_at=generated_at,
        metadata={
            **(metadata or {}),
            "issue": 343,
            "parent_epic": 339,
            "route_dispatch_deferred_to_issue": 344,
        },
    )


__all__ = [
    "PX4_GAZEBO_PICKUP_DROPOFF_ROUTE_PLAN_SCHEMA_VERSION",
    "ROUTE_ON_DEVIATION_ACTIONS",
    "PX4GazeboPickupDropoffRoutePlan",
    "PX4GazeboRoutePlanError",
    "build_px4_gazebo_pickup_dropoff_route_plan",
]

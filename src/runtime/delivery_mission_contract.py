"""Delivery mission contract schema.

``delivery_mission_contract.v1`` describes a delivery request and its safety
policies. It is intentionally inert: the artifact can define pickup/dropoff,
battery, landing-zone, weather, telemetry, abort, and return-to-home
requirements, but it never grants PX4/MAVLink/ROS/Gazebo control or actuator
execution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION = "delivery_mission_contract.v1"


class DeliveryMissionContractError(ValueError):
    """Raised when a delivery mission contract cannot be built safely."""


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actions",
        "actuator",
        "actuator_execution_allowed",
        "actuators",
        "attitude_setpoint",
        "command",
        "command_payload_allowed",
        "commands",
        "dispatch",
        "dispatch_implementation_present",
        "execute",
        "execute_now",
        "joint",
        "live_execution_allowed",
        "mavlink_command",
        "mavlink_dispatch_allowed",
        "mission_upload",
        "motor",
        "physical_execution_invoked",
        "position_setpoint",
        "ros_action",
        "ros_dispatch_allowed",
        "ros_topic",
        "ros2_topic",
        "setpoint",
        "thrust",
        "torque",
        "velocity_command",
    }
)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_FORBIDDEN_COMMAND_KEYS_NORMALIZED = frozenset(
    _normalize_key(key) for key in _FORBIDDEN_COMMAND_KEYS
)


def _command_like_key_paths(value: Any, *, root: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, sub in value.items():
            key_text = str(key)
            path = f"{root}.{key_text}" if root else key_text
            if _normalize_key(key_text) in _FORBIDDEN_COMMAND_KEYS_NORMALIZED:
                findings.append(path)
            findings.extend(_command_like_key_paths(sub, root=path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{root}.{index}" if root else str(index)
            findings.extend(_command_like_key_paths(item, root=path))
    return findings


def _raise_for_command_like_keys(value: Any, *, root: str) -> None:
    findings = _command_like_key_paths(value, root=root)
    if findings:
        raise DeliveryMissionContractError(
            "delivery mission contract refused command-like keys: "
            + ", ".join(sorted(findings))
        )


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _text_tuple(values: Sequence[str] | str | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        candidate = values.strip()
        return (candidate,) if candidate else ()
    return tuple(str(item).strip() for item in values if str(item).strip())


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


class DeliveryLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    location_id: str = Field(min_length=1)
    location_kind: str = "delivery_pad"
    coordinate_frame: Literal["wgs84"] = "wgs84"
    label: str = ""
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_m: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("location_id", "location_kind", "label", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return _clean_text(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "DeliveryLocation":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class DeliveryWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    earliest_pickup_at: datetime
    latest_dropoff_at: datetime

    @field_validator("earliest_pickup_at", "latest_dropoff_at", mode="before")
    @classmethod
    def _coerce_utc(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _utc(parsed)

    @model_validator(mode="after")
    def _validate_order(self) -> "DeliveryWindow":
        if self.latest_dropoff_at <= self.earliest_pickup_at:
            raise ValueError("latest_dropoff_at must be after earliest_pickup_at")
        return self


class PackageConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    package_id: str = Field(min_length=1)
    max_weight_kg: float = Field(gt=0)
    max_length_m: float | None = Field(default=None, gt=0)
    max_width_m: float | None = Field(default=None, gt=0)
    max_height_m: float | None = Field(default=None, gt=0)
    fragile: bool = False
    hazardous_material_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("package_id", mode="before")
    @classmethod
    def _strip_package_id(cls, value: Any) -> str:
        return _clean_text(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "PackageConstraints":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class RouteConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_route_distance_m: float | None = Field(default=None, gt=0)
    required_waypoints: tuple[str, ...] = ()
    avoid_zones: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("required_waypoints", "avoid_zones", mode="before")
    @classmethod
    def _strip_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "RouteConstraints":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class GeofenceConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_regions: tuple[str, ...] = ()
    no_fly_zones: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_regions", "no_fly_zones", mode="before")
    @classmethod
    def _strip_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "GeofenceConstraints":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class WeatherConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_wind_speed_mps: float = Field(gt=0)
    max_precipitation_mm_per_hour: float = Field(ge=0)
    min_visibility_m: float = Field(gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "WeatherConstraints":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class BatteryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_takeoff_percent: int = Field(ge=1, le=100)
    return_to_home_percent: int = Field(ge=1, le=100)
    reserve_landing_percent: int = Field(ge=1, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "BatteryPolicy":
        if self.minimum_takeoff_percent <= self.return_to_home_percent:
            raise ValueError(
                "minimum_takeoff_percent must be greater than return_to_home_percent"
            )
        if self.return_to_home_percent < self.reserve_landing_percent:
            raise ValueError(
                "return_to_home_percent must be greater than or equal to reserve_landing_percent"
            )
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class LandingZonePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    require_verified_landing_zone: Literal[True] = True
    min_clear_radius_m: float = Field(gt=0)
    max_slope_degrees: float = Field(ge=0, le=45)
    accepted_surface_kinds: tuple[str, ...] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("accepted_surface_kinds", mode="before")
    @classmethod
    def _strip_surface_kinds(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "LandingZonePolicy":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class VehicleHealthPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_checks: tuple[str, ...] = Field(default=("preflight_health",), min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("required_checks", mode="before")
    @classmethod
    def _strip_required_checks(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "VehicleHealthPolicy":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class TelemetryRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_measurements: tuple[str, ...] = Field(min_length=1)
    max_freshness_seconds: float = Field(gt=0)
    require_source_provenance: Literal[True] = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("required_measurements", mode="before")
    @classmethod
    def _strip_required_measurements(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "TelemetryRequirements":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class DeliveryMissionContract(BaseModel):
    """Inert delivery-mission definition.

    The contract records delivery semantics and the conditions needed before any
    later simulated or operator-approved action can be considered. All command,
    dispatch, ROS/MAVLink, actuator, live, and physical flags are pinned to safe
    values.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION] = (
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    )
    contract_id: str
    mission_id: str
    pickup_location: DeliveryLocation
    dropoff_location: DeliveryLocation
    delivery_window: DeliveryWindow
    package_constraints: PackageConstraints
    route_constraints: RouteConstraints = Field(default_factory=RouteConstraints)
    geofence_constraints: GeofenceConstraints = Field(default_factory=GeofenceConstraints)
    weather_constraints: WeatherConstraints
    battery_policy: BatteryPolicy
    landing_zone_policy: LandingZonePolicy
    vehicle_health_policy: VehicleHealthPolicy = Field(default_factory=VehicleHealthPolicy)
    telemetry_requirements: TelemetryRequirements
    success_criteria: tuple[str, ...] = Field(min_length=1)
    abort_conditions: tuple[str, ...] = Field(min_length=1)
    return_to_home_conditions: tuple[str, ...] = Field(min_length=1)
    operator_escalation_conditions: tuple[str, ...] = Field(min_length=1)
    required_evidence: tuple[str, ...] = Field(min_length=1)
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("contract_id", "mission_id", mode="before")
    @classmethod
    def _strip_ids(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator(
        "success_criteria",
        "abort_conditions",
        "return_to_home_conditions",
        "operator_escalation_conditions",
        "required_evidence",
        mode="before",
    )
    @classmethod
    def _strip_text_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _utc(parsed)

    @model_validator(mode="after")
    def _validate_contract(self) -> "DeliveryMissionContract":
        if not self.contract_id:
            raise ValueError("contract_id is required")
        if not self.mission_id:
            raise ValueError("mission_id is required")
        if self.pickup_location.location_id == self.dropoff_location.location_id:
            raise ValueError("pickup_location and dropoff_location must differ")
        if "hil_telemetry_evidence" not in self.required_evidence:
            raise ValueError("required_evidence must include hil_telemetry_evidence")
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


def build_delivery_mission_contract(
    *,
    mission_id: str,
    pickup_location: DeliveryLocation | dict[str, Any],
    dropoff_location: DeliveryLocation | dict[str, Any],
    delivery_window: DeliveryWindow | dict[str, Any],
    package_constraints: PackageConstraints | dict[str, Any],
    weather_constraints: WeatherConstraints | dict[str, Any],
    battery_policy: BatteryPolicy | dict[str, Any],
    landing_zone_policy: LandingZonePolicy | dict[str, Any],
    telemetry_requirements: TelemetryRequirements | dict[str, Any],
    route_constraints: RouteConstraints | dict[str, Any] | None = None,
    geofence_constraints: GeofenceConstraints | dict[str, Any] | None = None,
    vehicle_health_policy: VehicleHealthPolicy | dict[str, Any] | None = None,
    success_criteria: Sequence[str] | str | None = None,
    abort_conditions: Sequence[str] | str | None = None,
    return_to_home_conditions: Sequence[str] | str | None = None,
    operator_escalation_conditions: Sequence[str] | str | None = None,
    required_evidence: Sequence[str] | str | None = None,
    contract_id: str | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> DeliveryMissionContract:
    normalized_mission_id = _clean_text(mission_id)
    if not normalized_mission_id:
        raise DeliveryMissionContractError("mission_id is required")

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    created_at = _utc(now)
    pickup = DeliveryLocation.model_validate(pickup_location)
    dropoff = DeliveryLocation.model_validate(dropoff_location)
    window = DeliveryWindow.model_validate(delivery_window)
    package = PackageConstraints.model_validate(package_constraints)
    route = RouteConstraints.model_validate(route_constraints or {})
    geofence = GeofenceConstraints.model_validate(geofence_constraints or {})
    weather = WeatherConstraints.model_validate(weather_constraints)
    battery = BatteryPolicy.model_validate(battery_policy)
    landing_zone = LandingZonePolicy.model_validate(landing_zone_policy)
    vehicle_health = VehicleHealthPolicy.model_validate(vehicle_health_policy or {})
    telemetry = TelemetryRequirements.model_validate(telemetry_requirements)
    resolved_success_criteria = _text_tuple(success_criteria) or (
        "package_delivered_to_verified_dropoff_zone",
        "delivery_confirmation_evidence_recorded",
        "vehicle_health_safe_at_completion",
    )
    resolved_abort_conditions = _text_tuple(abort_conditions) or (
        "geofence_violation_risk",
        "weather_policy_violation",
        "telemetry_stale_or_missing",
        "landing_zone_unverified",
    )
    resolved_return_to_home_conditions = _text_tuple(return_to_home_conditions) or (
        "battery_at_or_below_return_to_home_percent",
        "dropoff_zone_unavailable",
        "operator_requests_return",
    )
    resolved_operator_escalation_conditions = _text_tuple(
        operator_escalation_conditions
    ) or (
        "required_evidence_missing",
        "delivery_window_at_risk",
        "vehicle_health_policy_violation",
    )
    evidence = _text_tuple(required_evidence) or (
        "delivery_mission_contract",
        "px4_gazebo_sanitized_telemetry",
        "hil_telemetry_evidence",
        "hil_telemetry_review",
        "autonomy_gate_result",
        "operator_review_record",
    )
    base_payload = {
        "mission_id": normalized_mission_id,
        "pickup_location": pickup.model_dump(mode="json"),
        "dropoff_location": dropoff.model_dump(mode="json"),
        "delivery_window": window.model_dump(mode="json"),
        "package_constraints": package.model_dump(mode="json"),
        "route_constraints": route.model_dump(mode="json"),
        "geofence_constraints": geofence.model_dump(mode="json"),
        "weather_constraints": weather.model_dump(mode="json"),
        "battery_policy": battery.model_dump(mode="json"),
        "landing_zone_policy": landing_zone.model_dump(mode="json"),
        "vehicle_health_policy": vehicle_health.model_dump(mode="json"),
        "telemetry_requirements": telemetry.model_dump(mode="json"),
        "success_criteria": resolved_success_criteria,
        "abort_conditions": resolved_abort_conditions,
        "return_to_home_conditions": resolved_return_to_home_conditions,
        "operator_escalation_conditions": resolved_operator_escalation_conditions,
        "required_evidence": evidence,
    }
    return DeliveryMissionContract(
        contract_id=_clean_text(contract_id)
        or _stable_id("delivery_mission_contract", base_payload),
        mission_id=normalized_mission_id,
        pickup_location=pickup,
        dropoff_location=dropoff,
        delivery_window=window,
        package_constraints=package,
        route_constraints=route,
        geofence_constraints=geofence,
        weather_constraints=weather,
        battery_policy=battery,
        landing_zone_policy=landing_zone,
        vehicle_health_policy=vehicle_health,
        telemetry_requirements=telemetry,
        success_criteria=resolved_success_criteria,
        abort_conditions=resolved_abort_conditions,
        return_to_home_conditions=resolved_return_to_home_conditions,
        operator_escalation_conditions=resolved_operator_escalation_conditions,
        required_evidence=evidence,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "delivery_contract_only": True,
            "no_dispatch_surface": True,
            "abort_conditions_are_policy_only": True,
            "return_to_home_conditions_are_policy_only": True,
            "operator_escalation_conditions_are_policy_only": True,
        },
    )


__all__ = [
    "DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION",
    "BatteryPolicy",
    "DeliveryLocation",
    "DeliveryMissionContract",
    "DeliveryMissionContractError",
    "DeliveryWindow",
    "GeofenceConstraints",
    "LandingZonePolicy",
    "PackageConstraints",
    "RouteConstraints",
    "TelemetryRequirements",
    "VehicleHealthPolicy",
    "WeatherConstraints",
    "build_delivery_mission_contract",
]

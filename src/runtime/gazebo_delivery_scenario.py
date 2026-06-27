"""Gazebo delivery scenario artifact.

``gazebo_delivery_scenario.v1`` maps a delivery mission contract onto a
simulation-only Gazebo scenario. It is a scenario descriptor, not a command
surface: it does not start Gazebo, mutate entities, upload PX4 missions, send
MAVLink/ROS payloads, set setpoints, or execute actuators.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.gazebo_delivery_world import DEFAULT_GAZEBO_DELIVERY_WORLD_PATH
from src.runtime.task_store import TaskStore, get_task_store


GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION = "gazebo_delivery_scenario.v1"
GazeboDeliveryScenarioVariant = Literal[
    "nominal_delivery",
    "low_battery_delivery",
    "blocked_route_geofence",
    "missing_dropoff_evidence",
    "stale_telemetry",
    "landing_zone_unavailable",
]
GAZEBO_DELIVERY_SCENARIO_VARIANTS: tuple[str, ...] = (
    "nominal_delivery",
    "low_battery_delivery",
    "blocked_route_geofence",
    "missing_dropoff_evidence",
    "stale_telemetry",
    "landing_zone_unavailable",
)
GAZEBO_DELIVERY_STATE_DRIVEN_WORLD_REF = (
    "simulators/gazebo/worlds/delivery_state_driven.sdf"
)


class GazeboDeliveryScenarioError(RuntimeError):
    """Raised when a Gazebo delivery scenario cannot be built safely."""


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
        "entity_mutation",
        "execute",
        "execute_now",
        "gazebo_mutation",
        "joint",
        "landing_command",
        "live_execution_allowed",
        "mavlink_command",
        "mavlink_dispatch_allowed",
        "mission_upload",
        "motor",
        "physical_execution_invoked",
        "position_setpoint",
        "return_to_home_command",
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
        raise GazeboDeliveryScenarioError(
            "gazebo delivery scenario refused command-like keys: "
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


class GazeboDeliveryPadRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pad_id: str = Field(min_length=1)
    delivery_location_ref: str = Field(min_length=1)
    model_ref: str = Field(min_length=1)
    frame: str = "world"
    x_m: float
    y_m: float
    z_m: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("pad_id", "delivery_location_ref", "model_ref", "frame", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return _clean_text(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "GazeboDeliveryPadRef":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class GazeboDeliveryRouteRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: str = Field(min_length=1)
    waypoint_refs: tuple[str, ...] = Field(min_length=1)
    safe_corridor_refs: tuple[str, ...] = Field(min_length=1)
    geofence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "route_id",
        mode="before",
    )
    @classmethod
    def _strip_id(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("waypoint_refs", "safe_corridor_refs", "geofence_refs", mode="before")
    @classmethod
    def _strip_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value)

    @model_validator(mode="after")
    def _reject_metadata_commands(self) -> "GazeboDeliveryRouteRef":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


class GazeboDeliveryScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION] = (
        GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    )
    scenario_id: str
    scenario_name: str
    delivery_mission_contract_id: str
    delivery_mission_id: str
    simulator_kind: Literal["gazebo_sim"] = "gazebo_sim"
    world_ref: str = Field(min_length=1)
    world_kind: Literal["sdf_world_ref"] = "sdf_world_ref"
    headless_required: Literal[True] = True
    pickup_pad: GazeboDeliveryPadRef
    dropoff_pad: GazeboDeliveryPadRef
    route: GazeboDeliveryRouteRef
    battery_policy_ref: str = Field(min_length=1)
    landing_zone_policy_ref: str = Field(min_length=1)
    success_criteria: tuple[str, ...] = Field(min_length=1)
    abort_conditions: tuple[str, ...] = Field(min_length=1)
    required_evidence: tuple[str, ...] = Field(min_length=1)
    created_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    simulation_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "scenario_id",
        "scenario_name",
        "delivery_mission_contract_id",
        "delivery_mission_id",
        "world_ref",
        "battery_policy_ref",
        "landing_zone_policy_ref",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator(
        "success_criteria",
        "abort_conditions",
        "required_evidence",
        mode="before",
    )
    @classmethod
    def _strip_tuple(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _utc(parsed)

    @model_validator(mode="after")
    def _validate_refs(self) -> "GazeboDeliveryScenario":
        if self.pickup_pad.pad_id == self.dropoff_pad.pad_id:
            raise ValueError("pickup_pad and dropoff_pad must differ")
        if self.pickup_pad.delivery_location_ref == self.dropoff_pad.delivery_location_ref:
            raise ValueError("pickup and dropoff delivery location refs must differ")
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


def _to_contract(value: DeliveryMissionContract | Mapping[str, Any]) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _default_pad_ref(
    *,
    location_id: str,
    role: str,
    x_m: float,
    y_m: float,
) -> GazeboDeliveryPadRef:
    return GazeboDeliveryPadRef(
        pad_id=f"{role}-{location_id}",
        delivery_location_ref=f"delivery_location:{location_id}",
        model_ref=f"model://{role}_{location_id}",
        x_m=x_m,
        y_m=y_m,
        metadata={
            "role": role,
            "artifact_only": True,
            "no_entity_mutation": True,
        },
    )


def build_gazebo_delivery_scenario(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    world_ref: str = DEFAULT_GAZEBO_DELIVERY_WORLD_PATH,
    scenario_name: str | None = None,
    pickup_pad: GazeboDeliveryPadRef | Mapping[str, Any] | None = None,
    dropoff_pad: GazeboDeliveryPadRef | Mapping[str, Any] | None = None,
    route: GazeboDeliveryRouteRef | Mapping[str, Any] | None = None,
    scenario_id: str | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> GazeboDeliveryScenario:
    """Build a deterministic, simulation-only Gazebo delivery scenario."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    contract = _to_contract(delivery_mission_contract)
    created_at = _utc(now)
    resolved_world_ref = _clean_text(world_ref)
    if not resolved_world_ref:
        raise GazeboDeliveryScenarioError("world_ref is required")

    resolved_pickup = (
        pickup_pad
        if isinstance(pickup_pad, GazeboDeliveryPadRef)
        else GazeboDeliveryPadRef.model_validate(pickup_pad)
        if pickup_pad is not None
        else _default_pad_ref(
            location_id=contract.pickup_location.location_id,
            role="pickup",
            x_m=0.0,
            y_m=0.0,
        )
    )
    resolved_dropoff = (
        dropoff_pad
        if isinstance(dropoff_pad, GazeboDeliveryPadRef)
        else GazeboDeliveryPadRef.model_validate(dropoff_pad)
        if dropoff_pad is not None
        else _default_pad_ref(
            location_id=contract.dropoff_location.location_id,
            role="dropoff",
            x_m=25.0,
            y_m=0.0,
        )
    )
    resolved_route = (
        route
        if isinstance(route, GazeboDeliveryRouteRef)
        else GazeboDeliveryRouteRef.model_validate(route)
        if route is not None
        else GazeboDeliveryRouteRef(
            route_id=f"route-{contract.mission_id}",
            waypoint_refs=(
                resolved_pickup.model_ref,
                resolved_dropoff.model_ref,
            ),
            safe_corridor_refs=("corridor:pickup-to-dropoff",),
            geofence_refs=contract.geofence_constraints.allowed_regions,
            metadata={
                "artifact_only": True,
                "route_descriptor_only": True,
                "no_entity_mutation": True,
            },
        )
    )
    name = _clean_text(scenario_name) or f"gazebo-delivery-{contract.mission_id}"
    base_payload = {
        "delivery_mission_contract_id": contract.contract_id,
        "delivery_mission_id": contract.mission_id,
        "world_ref": resolved_world_ref,
        "metadata": metadata_payload,
        "pickup_pad": resolved_pickup.model_dump(mode="json"),
        "dropoff_pad": resolved_dropoff.model_dump(mode="json"),
        "route": resolved_route.model_dump(mode="json"),
        "success_criteria": contract.success_criteria,
        "abort_conditions": contract.abort_conditions,
    }
    return GazeboDeliveryScenario(
        scenario_id=_clean_text(scenario_id)
        or _stable_id("gazebo_delivery_scenario", base_payload),
        scenario_name=name,
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        world_ref=resolved_world_ref,
        pickup_pad=resolved_pickup,
        dropoff_pad=resolved_dropoff,
        route=resolved_route,
        battery_policy_ref=f"delivery_battery_policy:{contract.contract_id}",
        landing_zone_policy_ref=f"delivery_landing_zone_policy:{contract.contract_id}",
        success_criteria=contract.success_criteria,
        abort_conditions=contract.abort_conditions,
        required_evidence=contract.required_evidence,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "scenario_descriptor_only": True,
            "gazebo_delivery_scenario_only": True,
            "no_dispatch_surface": True,
            "no_entity_mutation": True,
            "no_px4_mission_upload": True,
        },
    )


def build_gazebo_delivery_scenario_variant(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    variant: GazeboDeliveryScenarioVariant,
    now: datetime | None = None,
) -> GazeboDeliveryScenario:
    """Build a deterministic headless-compatible delivery scenario variant."""

    if variant not in GAZEBO_DELIVERY_SCENARIO_VARIANTS:
        raise GazeboDeliveryScenarioError(f"unsupported scenario variant: {variant}")
    expected_outcome = {
        "nominal_delivery": "completed",
        "low_battery_delivery": "warning_or_blocked_by_battery_policy",
        "blocked_route_geofence": "blocked",
        "missing_dropoff_evidence": "blocked",
        "stale_telemetry": "blocked",
        "landing_zone_unavailable": "blocked",
    }[variant]
    return build_gazebo_delivery_scenario(
        delivery_mission_contract=delivery_mission_contract,
        world_ref=GAZEBO_DELIVERY_STATE_DRIVEN_WORLD_REF,
        scenario_name=f"gazebo-delivery-{variant.replace('_', '-')}",
        now=now,
        metadata={
            "scenario_variant": variant,
            "expected_outcome": expected_outcome,
            "sdf_world_ref": GAZEBO_DELIVERY_STATE_DRIVEN_WORLD_REF,
            "opt_in_only": True,
            "headless_compatible": True,
            "server_only_compatible": True,
            "requires_gui": False,
            "command_control_ports_exposed": False,
            "variant_descriptor_only": True,
            "no_ros_mavlink_px4_command_surface": True,
            "no_actuator_live_physical_execution": True,
        },
    )


def attach_gazebo_delivery_scenario(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    world_ref: str = DEFAULT_GAZEBO_DELIVERY_WORLD_PATH,
    scenario_name: str | None = None,
    pickup_pad: GazeboDeliveryPadRef | Mapping[str, Any] | None = None,
    dropoff_pad: GazeboDeliveryPadRef | Mapping[str, Any] | None = None,
    route: GazeboDeliveryRouteRef | Mapping[str, Any] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach a Gazebo delivery scenario without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise GazeboDeliveryScenarioError(
            f"task {task_id} not found; cannot attach Gazebo delivery scenario"
        )
    scenario = build_gazebo_delivery_scenario(
        delivery_mission_contract=delivery_mission_contract,
        world_ref=world_ref,
        scenario_name=scenario_name,
        pickup_pad=pickup_pad,
        dropoff_pad=dropoff_pad,
        route=route,
        now=now,
    )
    artifacts = {"gazebo_delivery_scenario": scenario.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise GazeboDeliveryScenarioError(
            f"task {task_id} disappeared while attaching Gazebo delivery scenario"
        )
    return artifacts


__all__ = [
    "GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION",
    "GAZEBO_DELIVERY_SCENARIO_VARIANTS",
    "GAZEBO_DELIVERY_STATE_DRIVEN_WORLD_REF",
    "GazeboDeliveryPadRef",
    "GazeboDeliveryRouteRef",
    "GazeboDeliveryScenario",
    "GazeboDeliveryScenarioError",
    "GazeboDeliveryScenarioVariant",
    "attach_gazebo_delivery_scenario",
    "build_gazebo_delivery_scenario",
    "build_gazebo_delivery_scenario_variant",
]

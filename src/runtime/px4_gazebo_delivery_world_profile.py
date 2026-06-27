"""PX4 SITL + Gazebo delivery world profile artifact.

``px4_gazebo_delivery_world_profile.v1`` describes the first PX4/Gazebo delivery
world profile for Mission OS. It is a profile descriptor only: it validates the
existing Gazebo delivery world fixture and records the PX4 SITL / Gazebo service
refs that a future opt-in runtime can start. It does not open a MAVLink/ROS
command channel, upload missions, mutate Gazebo, or target hardware.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.gazebo_delivery_world import (
    DEFAULT_GAZEBO_DELIVERY_WORLD_PATH,
    GazeboDeliveryWorldFixture,
    build_gazebo_delivery_world_fixture,
)
from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_DELIVERY_WORLD_PROFILE_SCHEMA_VERSION = (
    "px4_gazebo_delivery_world_profile.v1"
)
DEFAULT_PX4_SITL_IMAGE = "px4io/px4-sitl:latest"
DEFAULT_PX4_SITL_MODEL = "sihsim_quadx"
DEFAULT_GAZEBO_SIM_IMAGE = "ghcr.io/openrobotics/gazebo:harmonic-full"
DEFAULT_PX4_SITL_SERVICE_REF = "docker-compose:boiled-claw-px4-sitl-telemetry"
DEFAULT_GAZEBO_DELIVERY_WORLD_SERVICE_REF = (
    "docker-compose:boiled-claw-gz-sim-delivery-world"
)


class PX4GazeboDeliveryWorldProfileError(RuntimeError):
    """Raised when a PX4/Gazebo delivery world profile is unsafe or invalid."""


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actuator",
        "actuator_execution_allowed",
        "command",
        "command_payload_allowed",
        "dispatch",
        "dispatch_implementation_present",
        "entity_mutation",
        "execute",
        "gazebo_mutation",
        "hardware",
        "hardware_target",
        "live_execution_allowed",
        "mavlink_command",
        "mavlink_dispatch_allowed",
        "mission_upload",
        "physical_execution_invoked",
        "position_setpoint",
        "ros_action",
        "ros_dispatch_allowed",
        "setpoint",
        "thrust",
        "torque",
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
        raise PX4GazeboDeliveryWorldProfileError(
            "px4 gazebo delivery world profile refused command-like keys: "
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


class PX4GazeboDeliveryWorldProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_DELIVERY_WORLD_PROFILE_SCHEMA_VERSION] = (
        PX4_GAZEBO_DELIVERY_WORLD_PROFILE_SCHEMA_VERSION
    )
    profile_id: str
    profile_name: str
    px4_sitl_image: str = Field(min_length=1)
    px4_sitl_model: str = Field(min_length=1)
    px4_sitl_service_ref: str = Field(min_length=1)
    gazebo_sim_image: str = Field(min_length=1)
    gazebo_delivery_world_service_ref: str = Field(min_length=1)
    gazebo_world_fixture_id: str = Field(min_length=1)
    gazebo_world_ref: str = Field(min_length=1)
    gazebo_world_name: str = Field(min_length=1)
    pickup_model_ref: str = Field(min_length=1)
    dropoff_model_ref: str = Field(min_length=1)
    safe_corridor_model_ref: str = Field(min_length=1)
    required_compose_profiles: tuple[str, ...] = Field(min_length=1)
    required_observation_modes: tuple[str, ...] = Field(min_length=1)
    startup_sequence: tuple[str, ...] = Field(min_length=1)
    created_at: datetime
    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    telemetry_first: Literal[True] = True
    profile_descriptor_only: Literal[True] = True
    operator_approval_required_for_commands: Literal[True] = True
    command_surface_present: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    network_ports_exposed: Literal[False] = False
    read_only_rootfs_required: Literal[True] = True
    cap_drop_all_required: Literal[True] = True
    no_new_privileges_required: Literal[True] = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "profile_id",
        "profile_name",
        "px4_sitl_image",
        "px4_sitl_model",
        "px4_sitl_service_ref",
        "gazebo_sim_image",
        "gazebo_delivery_world_service_ref",
        "gazebo_world_fixture_id",
        "gazebo_world_ref",
        "gazebo_world_name",
        "pickup_model_ref",
        "dropoff_model_ref",
        "safe_corridor_model_ref",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator(
        "required_compose_profiles",
        "required_observation_modes",
        "startup_sequence",
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
    def _validate_profile(self) -> "PX4GazeboDeliveryWorldProfile":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        if "hardware" in self.px4_sitl_service_ref.lower():
            raise ValueError("px4_sitl_service_ref must not target hardware")
        if "mavlink-command" in self.required_observation_modes:
            raise ValueError("profile must remain observation-first")
        return self


def _to_world_fixture(
    value: GazeboDeliveryWorldFixture | Mapping[str, Any] | None,
    *,
    world_ref: str,
    now: datetime | None,
) -> GazeboDeliveryWorldFixture:
    if isinstance(value, GazeboDeliveryWorldFixture):
        return value
    if value is not None:
        return GazeboDeliveryWorldFixture.model_validate(dict(value))
    return build_gazebo_delivery_world_fixture(world_ref, now=now)


def build_px4_gazebo_delivery_world_profile(
    *,
    world_ref: str = DEFAULT_GAZEBO_DELIVERY_WORLD_PATH,
    world_fixture: GazeboDeliveryWorldFixture | Mapping[str, Any] | None = None,
    profile_name: str = "px4-gazebo-delivery-world-v0",
    px4_sitl_image: str = DEFAULT_PX4_SITL_IMAGE,
    px4_sitl_model: str = DEFAULT_PX4_SITL_MODEL,
    px4_sitl_service_ref: str = DEFAULT_PX4_SITL_SERVICE_REF,
    gazebo_sim_image: str = DEFAULT_GAZEBO_SIM_IMAGE,
    gazebo_delivery_world_service_ref: str = DEFAULT_GAZEBO_DELIVERY_WORLD_SERVICE_REF,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboDeliveryWorldProfile:
    """Build the PX4 SITL + Gazebo delivery world profile descriptor."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    created_at = _utc(now)
    fixture = _to_world_fixture(world_fixture, world_ref=world_ref, now=created_at)
    if fixture.command_surface_present:
        raise PX4GazeboDeliveryWorldProfileError(
            "world fixture must not expose a command surface"
        )
    payload = {
        "profile_name": profile_name,
        "px4_sitl_image": px4_sitl_image,
        "px4_sitl_model": px4_sitl_model,
        "px4_sitl_service_ref": px4_sitl_service_ref,
        "gazebo_sim_image": gazebo_sim_image,
        "gazebo_delivery_world_service_ref": gazebo_delivery_world_service_ref,
        "gazebo_world_fixture_id": fixture.fixture_id,
        "gazebo_world_ref": fixture.world_ref,
    }
    return PX4GazeboDeliveryWorldProfile(
        profile_id=_stable_id("px4_gazebo_delivery_world_profile", payload),
        profile_name=profile_name,
        px4_sitl_image=px4_sitl_image,
        px4_sitl_model=px4_sitl_model,
        px4_sitl_service_ref=px4_sitl_service_ref,
        gazebo_sim_image=gazebo_sim_image,
        gazebo_delivery_world_service_ref=gazebo_delivery_world_service_ref,
        gazebo_world_fixture_id=fixture.fixture_id,
        gazebo_world_ref=fixture.world_ref,
        gazebo_world_name=fixture.world_name,
        pickup_model_ref=fixture.pickup_model_ref,
        dropoff_model_ref=fixture.dropoff_model_ref,
        safe_corridor_model_ref=fixture.safe_corridor_model_ref,
        required_compose_profiles=(
            "px4-sitl-telemetry",
            "gz-sim-delivery-world",
        ),
        required_observation_modes=(
            "px4_sitl_stdout_telemetry",
            "gazebo_world_stdout_readiness",
            "gazebo_delivery_world_loaded",
        ),
        startup_sequence=(
            "validate_delivery_world_fixture",
            "start_px4_sitl_observation",
            "start_gazebo_delivery_world_observation",
            "correlate_telemetry_before_command_proposal",
        ),
        created_at=created_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "issue": 308,
            "parent_epic": 307,
            "no_mavlink_command_channel": True,
            "no_ros_command_channel": True,
            "no_px4_mission_upload": True,
            "no_gazebo_entity_mutation": True,
            "no_hardware_target": True,
        },
    )


def attach_px4_gazebo_delivery_world_profile(
    task_id: str,
    *,
    world_ref: str = DEFAULT_GAZEBO_DELIVERY_WORLD_PATH,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach the PX4/Gazebo delivery profile without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboDeliveryWorldProfileError(
            f"task {task_id} not found; cannot attach PX4/Gazebo delivery profile"
        )
    profile = build_px4_gazebo_delivery_world_profile(world_ref=world_ref, now=now)
    artifacts = {"px4_gazebo_delivery_world_profile": profile.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise PX4GazeboDeliveryWorldProfileError(
            f"task {task_id} disappeared while attaching PX4/Gazebo delivery profile"
        )
    return artifacts


__all__ = [
    "DEFAULT_GAZEBO_DELIVERY_WORLD_SERVICE_REF",
    "DEFAULT_GAZEBO_SIM_IMAGE",
    "DEFAULT_PX4_SITL_IMAGE",
    "DEFAULT_PX4_SITL_MODEL",
    "DEFAULT_PX4_SITL_SERVICE_REF",
    "PX4_GAZEBO_DELIVERY_WORLD_PROFILE_SCHEMA_VERSION",
    "PX4GazeboDeliveryWorldProfile",
    "PX4GazeboDeliveryWorldProfileError",
    "attach_px4_gazebo_delivery_world_profile",
    "build_px4_gazebo_delivery_world_profile",
]

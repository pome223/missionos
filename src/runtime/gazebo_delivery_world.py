"""Gazebo delivery world fixture validation.

``gazebo_delivery_world_fixture.v1`` describes the local SDF fixture used by the
simulated delivery epic. It validates that the fixture is a headless-compatible
world descriptor and does not carry plugins or command/control surfaces.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal
import xml.etree.ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field


GAZEBO_DELIVERY_WORLD_FIXTURE_SCHEMA_VERSION = "gazebo_delivery_world_fixture.v1"
DEFAULT_GAZEBO_DELIVERY_WORLD_PATH = "simulators/gazebo/worlds/delivery_minimal.sdf"


class GazeboDeliveryWorldFixtureError(RuntimeError):
    """Raised when a Gazebo delivery world fixture is unsafe or invalid."""


class GazeboDeliveryWorldFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[GAZEBO_DELIVERY_WORLD_FIXTURE_SCHEMA_VERSION] = (
        GAZEBO_DELIVERY_WORLD_FIXTURE_SCHEMA_VERSION
    )
    fixture_id: str
    world_ref: str
    world_name: str
    sdf_version: str
    pickup_model_ref: str
    dropoff_model_ref: str
    safe_corridor_model_ref: str
    model_names: tuple[str, ...]
    headless_compatible: Literal[True] = True
    server_only_compatible: Literal[True] = True
    requires_gui: Literal[False] = False
    plugin_count: Literal[0] = 0
    include_count: Literal[0] = 0
    command_surface_present: Literal[False] = False
    ros_surface_present: Literal[False] = False
    mavlink_surface_present: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    validated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_world_path(world_ref: str | Path) -> Path:
    candidate = Path(world_ref)
    if not candidate.is_absolute():
        candidate = _repo_root() / candidate
    return candidate


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _elements(root: ET.Element, name: str) -> list[ET.Element]:
    return [element for element in root.iter() if _local_name(element.tag) == name]


def build_gazebo_delivery_world_fixture(
    world_ref: str | Path = DEFAULT_GAZEBO_DELIVERY_WORLD_PATH,
    *,
    now: datetime | None = None,
) -> GazeboDeliveryWorldFixture:
    """Validate the local minimal Gazebo delivery SDF fixture."""

    world_path = _resolve_world_path(world_ref)
    if not world_path.exists():
        raise GazeboDeliveryWorldFixtureError(f"world fixture not found: {world_path}")
    try:
        root = ET.parse(world_path).getroot()
    except ET.ParseError as exc:
        raise GazeboDeliveryWorldFixtureError(
            f"world fixture is not valid XML/SDF: {exc}"
        ) from exc
    if _local_name(root.tag) != "sdf":
        raise GazeboDeliveryWorldFixtureError("world fixture root must be <sdf>")
    sdf_version = root.attrib.get("version", "").strip()
    if not sdf_version:
        raise GazeboDeliveryWorldFixtureError("world fixture must declare sdf version")
    worlds = _elements(root, "world")
    if len(worlds) != 1:
        raise GazeboDeliveryWorldFixtureError("world fixture must contain exactly one world")
    world = worlds[0]
    world_name = world.attrib.get("name", "").strip()
    if world_name != "delivery_minimal":
        raise GazeboDeliveryWorldFixtureError(
            f"world fixture name must be delivery_minimal, got {world_name!r}"
        )
    plugins = _elements(root, "plugin")
    includes = _elements(root, "include")
    if plugins:
        raise GazeboDeliveryWorldFixtureError("world fixture must not contain plugins")
    if includes:
        raise GazeboDeliveryWorldFixtureError("world fixture must not contain includes")
    model_names = tuple(
        sorted(
            name
            for model in _elements(world, "model")
            if (name := model.attrib.get("name", "").strip())
        )
    )
    required = {
        "pickup_pad_a",
        "dropoff_pad_b",
        "safe_corridor_pickup_to_dropoff",
    }
    missing = sorted(required - set(model_names))
    if missing:
        raise GazeboDeliveryWorldFixtureError(
            "world fixture missing required delivery models: " + ", ".join(missing)
        )
    payload = {
        "world_ref": str(world_ref),
        "world_name": world_name,
        "sdf_version": sdf_version,
        "model_names": model_names,
    }
    return GazeboDeliveryWorldFixture(
        fixture_id=_stable_id("gazebo_delivery_world_fixture", payload),
        world_ref=str(world_ref),
        world_name=world_name,
        sdf_version=sdf_version,
        pickup_model_ref="model://pickup_pad_a",
        dropoff_model_ref="model://dropoff_pad_b",
        safe_corridor_model_ref="model://safe_corridor_pickup_to_dropoff",
        model_names=model_names,
        validated_at=_utc(now),
        metadata={
            "artifact_only": True,
            "world_fixture_only": True,
            "simulation_only": True,
            "no_plugins": True,
            "no_includes": True,
            "no_command_surface": True,
            "no_gazebo_entity_mutation": True,
        },
    )


__all__ = [
    "DEFAULT_GAZEBO_DELIVERY_WORLD_PATH",
    "GAZEBO_DELIVERY_WORLD_FIXTURE_SCHEMA_VERSION",
    "GazeboDeliveryWorldFixture",
    "GazeboDeliveryWorldFixtureError",
    "build_gazebo_delivery_world_fixture",
]

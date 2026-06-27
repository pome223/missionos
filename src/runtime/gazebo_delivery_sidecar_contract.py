"""Gazebo delivery sidecar contract.

``gazebo_delivery_sidecar_contract.v1`` defines the bounded simulation-only
interface a future Gazebo delivery sidecar may expose. It is a contract artifact
only: this module does not start Gazebo, advance a simulation, mutate Gazebo
entities, publish ROS messages, upload MAVLink missions, or execute actuators.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.delivery_mission_gate import (
    DELIVERY_MISSION_GATE_RESULT_SCHEMA_VERSION,
    DELIVERY_MISSION_SCORECARD_SCHEMA_VERSION,
)
from src.runtime.delivery_progress_review import DELIVERY_PROGRESS_REVIEW_SCHEMA_VERSION
from src.runtime.delivery_recovery_decision import DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
from src.runtime.gazebo_delivery_scenario import (
    GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION,
    GazeboDeliveryScenario,
)
from src.runtime.px4_gazebo_telemetry import PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
    SIMULATED_DELIVERY_STEP_SCHEMA_VERSION,
)
from src.runtime.task_store import TaskStore, get_task_store


GAZEBO_DELIVERY_SIDECAR_CONTRACT_SCHEMA_VERSION = (
    "gazebo_delivery_sidecar_contract.v1"
)
GAZEBO_DELIVERY_SIDECAR_ID = "gazebo_delivery_sidecar.v1"

GAZEBO_DELIVERY_TELEMETRY_WINDOW_SCHEMA_VERSION = (
    "gazebo_delivery_telemetry_window.v1"
)
GAZEBO_DELIVERY_SIDECAR_RESULT_SCHEMA_VERSION = "gazebo_delivery_sidecar_result.v1"


class GazeboDeliverySidecarRequestKind(str, Enum):
    START_DELIVERY_SIMULATION = "start_delivery_simulation"
    ADVANCE_DELIVERY_STEP = "advance_delivery_step"


class GazeboDeliverySidecarContractError(RuntimeError):
    """Raised when a Gazebo delivery sidecar contract is unsafe."""


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
        "gazebo_entity_mutation",
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
        raise GazeboDeliverySidecarContractError(
            "gazebo delivery sidecar contract refused command-like keys: "
            + ", ".join(sorted(findings))
        )


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _text_tuple(values: Sequence[str] | str | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        candidate = values.strip()
        return (candidate,) if candidate else ()
    return tuple(str(item).strip() for item in values if str(item).strip())


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _to_request_kind(value: GazeboDeliverySidecarRequestKind | str) -> GazeboDeliverySidecarRequestKind:
    if isinstance(value, GazeboDeliverySidecarRequestKind):
        return value
    return GazeboDeliverySidecarRequestKind(str(value))


class GazeboDeliverySidecarContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[GAZEBO_DELIVERY_SIDECAR_CONTRACT_SCHEMA_VERSION] = (
        GAZEBO_DELIVERY_SIDECAR_CONTRACT_SCHEMA_VERSION
    )
    sidecar_contract_id: str
    sidecar_id: str = GAZEBO_DELIVERY_SIDECAR_ID
    sidecar_name: str = "Gazebo delivery simulation sidecar"
    delivery_mission_contract_id: str
    delivery_mission_id: str
    gazebo_delivery_scenario_id: str
    simulator_kind: Literal["gazebo_sim"] = "gazebo_sim"
    interface_kind: Literal["simulation_artifact_sidecar"] = (
        "simulation_artifact_sidecar"
    )
    accepted_simulation_requests: tuple[GazeboDeliverySidecarRequestKind, ...] = (
        GazeboDeliverySidecarRequestKind.START_DELIVERY_SIMULATION,
        GazeboDeliverySidecarRequestKind.ADVANCE_DELIVERY_STEP,
    )
    returned_artifact_schemas: tuple[str, ...] = Field(min_length=1)
    validation_required_before_attach: Literal[True] = True
    sidecar_returns_artifacts_only: Literal[True] = True
    mission_os_validates_returned_artifacts: Literal[True] = True
    raw_gazebo_entity_mutation_exposed: Literal[False] = False
    ros_command_surface_exposed: Literal[False] = False
    mavlink_command_surface_exposed: Literal[False] = False
    actuator_surface_exposed: Literal[False] = False
    created_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    gazebo_delivery_scenario_schema_version: Literal[
        GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    ] = GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    simulation_only: Literal[True] = True
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
        "sidecar_contract_id",
        "sidecar_id",
        "sidecar_name",
        "delivery_mission_contract_id",
        "delivery_mission_id",
        "gazebo_delivery_scenario_id",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("returned_artifact_schemas", mode="before")
    @classmethod
    def _strip_schema_refs(cls, value: Any) -> tuple[str, ...]:
        return _text_tuple(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _utc(parsed)

    @model_validator(mode="after")
    def _validate_contract(self) -> "GazeboDeliverySidecarContract":
        if not self.accepted_simulation_requests:
            raise ValueError("accepted_simulation_requests is required")
        if not self.returned_artifact_schemas:
            raise ValueError("returned_artifact_schemas is required")
        unsupported = sorted(
            set(self.returned_artifact_schemas) - _ALLOWED_RETURNED_ARTIFACT_SCHEMAS
        )
        if unsupported:
            raise ValueError(
                "returned_artifact_schemas contains unsupported schemas: "
                + ", ".join(unsupported)
            )
        _raise_for_command_like_keys(self.metadata, root="metadata")
        return self


def _to_contract(value: DeliveryMissionContract | Mapping[str, Any]) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_scenario(
    value: GazeboDeliveryScenario | Mapping[str, Any],
) -> GazeboDeliveryScenario:
    if isinstance(value, GazeboDeliveryScenario):
        return value
    return GazeboDeliveryScenario.model_validate(dict(value))


def default_gazebo_delivery_sidecar_returned_artifact_schemas() -> tuple[str, ...]:
    return (
        GAZEBO_DELIVERY_SIDECAR_RESULT_SCHEMA_VERSION,
        PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION,
        SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
        SIMULATED_DELIVERY_STEP_SCHEMA_VERSION,
        GAZEBO_DELIVERY_TELEMETRY_WINDOW_SCHEMA_VERSION,
        DELIVERY_PROGRESS_REVIEW_SCHEMA_VERSION,
        DELIVERY_MISSION_SCORECARD_SCHEMA_VERSION,
        DELIVERY_MISSION_GATE_RESULT_SCHEMA_VERSION,
        DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION,
    )


_ALLOWED_RETURNED_ARTIFACT_SCHEMAS = frozenset(
    default_gazebo_delivery_sidecar_returned_artifact_schemas()
)


def build_gazebo_delivery_sidecar_contract(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any],
    sidecar_id: str = GAZEBO_DELIVERY_SIDECAR_ID,
    sidecar_name: str = "Gazebo delivery simulation sidecar",
    accepted_simulation_requests: (
        Sequence[GazeboDeliverySidecarRequestKind | str] | None
    ) = None,
    returned_artifact_schemas: Sequence[str] | None = None,
    sidecar_contract_id: str | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> GazeboDeliverySidecarContract:
    """Build a deterministic simulation-only Gazebo delivery sidecar contract."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    contract = _to_contract(delivery_mission_contract)
    scenario = _to_scenario(gazebo_delivery_scenario)
    if scenario.delivery_mission_contract_id != contract.contract_id:
        raise GazeboDeliverySidecarContractError(
            "gazebo delivery scenario contract_id mismatch"
        )
    if scenario.delivery_mission_id != contract.mission_id:
        raise GazeboDeliverySidecarContractError(
            "gazebo delivery scenario mission_id mismatch"
        )

    requests = tuple(
        _to_request_kind(item)
        for item in (
            accepted_simulation_requests
            or (
                GazeboDeliverySidecarRequestKind.START_DELIVERY_SIMULATION,
                GazeboDeliverySidecarRequestKind.ADVANCE_DELIVERY_STEP,
            )
        )
    )
    schemas = tuple(
        returned_artifact_schemas
        or default_gazebo_delivery_sidecar_returned_artifact_schemas()
    )
    base_payload = {
        "sidecar_id": _clean_text(sidecar_id),
        "delivery_mission_contract_id": contract.contract_id,
        "gazebo_delivery_scenario_id": scenario.scenario_id,
        "accepted_simulation_requests": [request.value for request in requests],
        "returned_artifact_schemas": schemas,
    }
    return GazeboDeliverySidecarContract(
        sidecar_contract_id=_clean_text(sidecar_contract_id)
        or _stable_id("gazebo_delivery_sidecar_contract", base_payload),
        sidecar_id=sidecar_id,
        sidecar_name=sidecar_name,
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        gazebo_delivery_scenario_id=scenario.scenario_id,
        accepted_simulation_requests=requests,
        returned_artifact_schemas=schemas,
        created_at=_utc(now),
        metadata={
            **metadata_payload,
            "contract_only": True,
            "simulation_only_sidecar_boundary": True,
            "sidecar_returns_artifacts_only": True,
            "mission_os_validates_returned_artifacts_before_attach": True,
            "no_raw_gazebo_entity_mutation_surface": True,
            "no_ros_mavlink_command_surface": True,
        },
    )


def validate_gazebo_delivery_sidecar_contract(
    contract: GazeboDeliverySidecarContract | Mapping[str, Any],
) -> GazeboDeliverySidecarContract:
    """Validate the current Gazebo delivery sidecar safety boundary."""

    try:
        validated = (
            contract
            if isinstance(contract, GazeboDeliverySidecarContract)
            else GazeboDeliverySidecarContract.model_validate(dict(contract))
        )
    except Exception as exc:
        raise GazeboDeliverySidecarContractError(
            f"gazebo_delivery_sidecar_contract failed validation: {exc}"
        ) from exc

    if validated.simulation_only is not True:
        raise GazeboDeliverySidecarContractError("simulation_only must be true")
    if validated.sidecar_returns_artifacts_only is not True:
        raise GazeboDeliverySidecarContractError(
            "sidecar_returns_artifacts_only must be true"
        )
    if validated.mission_os_validates_returned_artifacts is not True:
        raise GazeboDeliverySidecarContractError(
            "mission_os_validates_returned_artifacts must be true"
        )
    if validated.physical_execution_invoked is not False:
        raise GazeboDeliverySidecarContractError(
            "physical_execution_invoked must be false"
        )
    if validated.live_execution_allowed is not False:
        raise GazeboDeliverySidecarContractError("live_execution_allowed must be false")
    if validated.command_payload_allowed is not False:
        raise GazeboDeliverySidecarContractError(
            "command_payload_allowed must be false"
        )
    if validated.raw_gazebo_entity_mutation_exposed is not False:
        raise GazeboDeliverySidecarContractError(
            "raw_gazebo_entity_mutation_exposed must be false"
        )
    if validated.gazebo_entity_mutation_allowed is not False:
        raise GazeboDeliverySidecarContractError(
            "gazebo_entity_mutation_allowed must be false"
        )
    if validated.ros_command_surface_exposed is not False:
        raise GazeboDeliverySidecarContractError(
            "ros_command_surface_exposed must be false"
        )
    if validated.ros_dispatch_allowed is not False:
        raise GazeboDeliverySidecarContractError("ros_dispatch_allowed must be false")
    if validated.mavlink_command_surface_exposed is not False:
        raise GazeboDeliverySidecarContractError(
            "mavlink_command_surface_exposed must be false"
        )
    if validated.mavlink_dispatch_allowed is not False:
        raise GazeboDeliverySidecarContractError(
            "mavlink_dispatch_allowed must be false"
        )
    if validated.actuator_surface_exposed is not False:
        raise GazeboDeliverySidecarContractError(
            "actuator_surface_exposed must be false"
        )
    if validated.actuator_execution_allowed is not False:
        raise GazeboDeliverySidecarContractError(
            "actuator_execution_allowed must be false"
        )
    if not validated.returned_artifact_schemas:
        raise GazeboDeliverySidecarContractError("returned artifact schemas are required")
    unsupported = sorted(
        set(validated.returned_artifact_schemas) - _ALLOWED_RETURNED_ARTIFACT_SCHEMAS
    )
    if unsupported:
        raise GazeboDeliverySidecarContractError(
            "returned_artifact_schemas contains unsupported schemas: "
            + ", ".join(unsupported)
        )
    return validated


def attach_gazebo_delivery_sidecar_contract(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any],
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach a Gazebo delivery sidecar contract without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise GazeboDeliverySidecarContractError(
            f"task {task_id} not found; cannot attach Gazebo delivery sidecar contract"
        )
    contract = build_gazebo_delivery_sidecar_contract(
        delivery_mission_contract=delivery_mission_contract,
        gazebo_delivery_scenario=gazebo_delivery_scenario,
        now=now,
    )
    validated = validate_gazebo_delivery_sidecar_contract(contract)
    artifacts = {
        "gazebo_delivery_sidecar_contract": validated.model_dump(mode="json")
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise GazeboDeliverySidecarContractError(
            f"task {task_id} disappeared while attaching sidecar contract"
        )
    return artifacts


__all__ = [
    "GAZEBO_DELIVERY_SIDECAR_CONTRACT_SCHEMA_VERSION",
    "GAZEBO_DELIVERY_SIDECAR_ID",
    "GAZEBO_DELIVERY_SIDECAR_RESULT_SCHEMA_VERSION",
    "GAZEBO_DELIVERY_TELEMETRY_WINDOW_SCHEMA_VERSION",
    "SIMULATED_DELIVERY_STEP_SCHEMA_VERSION",
    "GazeboDeliverySidecarContract",
    "GazeboDeliverySidecarContractError",
    "GazeboDeliverySidecarRequestKind",
    "attach_gazebo_delivery_sidecar_contract",
    "build_gazebo_delivery_sidecar_contract",
    "default_gazebo_delivery_sidecar_returned_artifact_schemas",
    "validate_gazebo_delivery_sidecar_contract",
]

"""Simulation-only Gazebo delivery sidecar v0.

This module is a deterministic artifact adapter for Gazebo delivery scenarios.
It accepts the bounded request kinds defined by
``gazebo_delivery_sidecar_contract.v1`` and returns sanitized telemetry plus a
sidecar result artifact. It does not start Gazebo, advance a real simulator,
mutate Gazebo entities, publish ROS messages, upload MAVLink missions, send
setpoints, execute actuators, or perform live/physical execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.gazebo_delivery_scenario import (
    GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION,
    GazeboDeliveryScenario,
    build_gazebo_delivery_scenario,
)
from src.runtime.gazebo_delivery_sidecar_contract import (
    GAZEBO_DELIVERY_SIDECAR_CONTRACT_SCHEMA_VERSION,
    GAZEBO_DELIVERY_SIDECAR_RESULT_SCHEMA_VERSION,
    GazeboDeliverySidecarContract,
    GazeboDeliverySidecarRequestKind,
    build_gazebo_delivery_sidecar_contract,
    validate_gazebo_delivery_sidecar_contract,
)
from src.runtime.px4_gazebo_telemetry import (
    PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION,
    Px4GazeboSanitizedTelemetry,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_STEP_SCHEMA_VERSION,
    SimulatedDeliveryEpisodePhase,
    SimulatedDeliveryStep,
    SimulatedDeliveryStepStatus,
)
from src.runtime.simulated_delivery_runner import run_simulated_delivery_task_v0
from src.runtime.task_store import TaskStore, get_task_store


class GazeboDeliverySidecarPhase(str, Enum):
    PREFLIGHT = "preflight"
    PICKUP = "pickup"
    ENROUTE = "enroute"
    DROPOFF = "dropoff"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class GazeboDeliverySidecarV0Error(RuntimeError):
    """Raised when the simulation-only sidecar cannot safely return artifacts."""


_REQUIRED_SIDECAR_V0_RETURNED_SCHEMAS = frozenset(
    {
        GAZEBO_DELIVERY_SIDECAR_RESULT_SCHEMA_VERSION,
        PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION,
        SIMULATED_DELIVERY_STEP_SCHEMA_VERSION,
    }
)


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
        "gazebo_command",
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
        raise GazeboDeliverySidecarV0Error(
            "gazebo delivery sidecar v0 refused command-like keys: "
            + ", ".join(sorted(findings))
        )


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


def _as_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in values or () if str(item).strip()}))


def _to_contract(value: DeliveryMissionContract | Mapping[str, Any]) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_scenario(
    value: GazeboDeliveryScenario | Mapping[str, Any] | None,
    *,
    contract: DeliveryMissionContract,
    now: datetime,
) -> GazeboDeliveryScenario:
    if value is None:
        return build_gazebo_delivery_scenario(
            delivery_mission_contract=contract,
            now=now,
        )
    if isinstance(value, GazeboDeliveryScenario):
        return value
    return GazeboDeliveryScenario.model_validate(dict(value))


def _to_sidecar_contract(
    value: GazeboDeliverySidecarContract | Mapping[str, Any] | None,
    *,
    contract: DeliveryMissionContract,
    scenario: GazeboDeliveryScenario,
    now: datetime,
) -> GazeboDeliverySidecarContract:
    if value is None:
        return build_gazebo_delivery_sidecar_contract(
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            now=now,
        )
    validated = (
        value
        if isinstance(value, GazeboDeliverySidecarContract)
        else GazeboDeliverySidecarContract.model_validate(dict(value))
    )
    return validate_gazebo_delivery_sidecar_contract(validated)


def _validate_sidecar_v0_returned_artifact_schemas(
    sidecar_contract: GazeboDeliverySidecarContract,
) -> None:
    missing = sorted(
        _REQUIRED_SIDECAR_V0_RETURNED_SCHEMAS
        - set(sidecar_contract.returned_artifact_schemas)
    )
    if missing:
        raise GazeboDeliverySidecarV0Error(
            "sidecar contract missing required returned artifact schemas: "
            + ", ".join(missing)
        )


def _to_phase(value: GazeboDeliverySidecarPhase | str | None) -> GazeboDeliverySidecarPhase | None:
    if value is None:
        return None
    if isinstance(value, GazeboDeliverySidecarPhase):
        return value
    return GazeboDeliverySidecarPhase(str(value))


def _to_request_kind(
    value: GazeboDeliverySidecarRequestKind | str,
) -> GazeboDeliverySidecarRequestKind:
    if isinstance(value, GazeboDeliverySidecarRequestKind):
        return value
    return GazeboDeliverySidecarRequestKind(str(value))


_ADVANCE_PHASES = {
    GazeboDeliverySidecarPhase.PREFLIGHT: GazeboDeliverySidecarPhase.PICKUP,
    GazeboDeliverySidecarPhase.PICKUP: GazeboDeliverySidecarPhase.ENROUTE,
    GazeboDeliverySidecarPhase.ENROUTE: GazeboDeliverySidecarPhase.DROPOFF,
    GazeboDeliverySidecarPhase.DROPOFF: GazeboDeliverySidecarPhase.COMPLETED,
    GazeboDeliverySidecarPhase.COMPLETED: GazeboDeliverySidecarPhase.COMPLETED,
    GazeboDeliverySidecarPhase.BLOCKED: GazeboDeliverySidecarPhase.BLOCKED,
}


class GazeboDeliverySidecarResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[GAZEBO_DELIVERY_SIDECAR_RESULT_SCHEMA_VERSION] = (
        GAZEBO_DELIVERY_SIDECAR_RESULT_SCHEMA_VERSION
    )
    sidecar_result_id: str
    sidecar_contract_id: str
    delivery_mission_contract_id: str
    delivery_mission_id: str
    gazebo_delivery_scenario_id: str
    request_kind: GazeboDeliverySidecarRequestKind
    phase: GazeboDeliverySidecarPhase
    previous_phase: GazeboDeliverySidecarPhase | None = None
    sanitized_telemetry_id: str
    simulated_delivery_step_id: str
    returned_artifact_refs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    produced_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    gazebo_delivery_scenario_schema_version: Literal[
        GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    ] = GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    gazebo_delivery_sidecar_contract_schema_version: Literal[
        GAZEBO_DELIVERY_SIDECAR_CONTRACT_SCHEMA_VERSION
    ] = GAZEBO_DELIVERY_SIDECAR_CONTRACT_SCHEMA_VERSION
    px4_gazebo_sanitized_telemetry_schema_version: Literal[
        PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
    ] = PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
    simulated_delivery_step_schema_version: Literal[
        SIMULATED_DELIVERY_STEP_SCHEMA_VERSION
    ] = SIMULATED_DELIVERY_STEP_SCHEMA_VERSION
    simulation_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    sidecar_returns_artifacts_only: Literal[True] = True
    mission_os_validates_returned_artifacts: Literal[True] = True
    recommendations_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    raw_gazebo_entity_mutation_exposed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    ros_command_surface_exposed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_command_surface_exposed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_surface_exposed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _reject_command_like_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        findings = _command_like_key_paths(value, root="metadata")
        if findings:
            raise ValueError(
                "gazebo delivery sidecar result refused command-like metadata keys: "
                + ", ".join(sorted(findings))
            )
        return value


def _phase_for_request(
    *,
    request_kind: GazeboDeliverySidecarRequestKind,
    current_phase: GazeboDeliverySidecarPhase | None,
    blocked_reason: str | None,
) -> GazeboDeliverySidecarPhase:
    if blocked_reason:
        return GazeboDeliverySidecarPhase.BLOCKED
    if request_kind is GazeboDeliverySidecarRequestKind.START_DELIVERY_SIMULATION:
        return GazeboDeliverySidecarPhase.PREFLIGHT
    if current_phase is None:
        raise GazeboDeliverySidecarV0Error(
            "advance_delivery_step requires current_phase"
        )
    return _ADVANCE_PHASES[current_phase]


def _phase_measurements(
    phase: GazeboDeliverySidecarPhase,
    *,
    battery_percent: float,
    blocked_reason: str | None,
) -> dict[str, float | int | bool | str]:
    if phase is GazeboDeliverySidecarPhase.PREFLIGHT:
        pickup_reached = False
        dropoff_reached = False
        route_progress_percent = 0.0
        vehicle_health = "nominal"
    elif phase is GazeboDeliverySidecarPhase.PICKUP:
        pickup_reached = True
        dropoff_reached = False
        route_progress_percent = 5.0
        vehicle_health = "nominal"
    elif phase is GazeboDeliverySidecarPhase.ENROUTE:
        pickup_reached = True
        dropoff_reached = False
        route_progress_percent = 55.0
        vehicle_health = "nominal"
    elif phase in {
        GazeboDeliverySidecarPhase.DROPOFF,
        GazeboDeliverySidecarPhase.COMPLETED,
    }:
        pickup_reached = True
        dropoff_reached = True
        route_progress_percent = 100.0
        vehicle_health = "nominal"
    else:
        pickup_reached = False
        dropoff_reached = False
        route_progress_percent = 0.0
        vehicle_health = "blocked"

    return {
        "position": "35.681236,139.767125,16.0",
        "battery_percent": battery_percent,
        "vehicle_health": vehicle_health,
        "weather_snapshot": "clear",
        "pickup_reached": pickup_reached,
        "dropoff_reached": dropoff_reached,
        "route_progress_percent": route_progress_percent,
        "route_geofence_violation": False,
        "sidecar_phase": phase.value,
        "blocked_reason": blocked_reason or "",
    }


def _build_phase_telemetry(
    *,
    contract: DeliveryMissionContract,
    scenario: GazeboDeliveryScenario,
    sidecar_contract: GazeboDeliverySidecarContract,
    request_kind: GazeboDeliverySidecarRequestKind,
    phase: GazeboDeliverySidecarPhase,
    produced_at: datetime,
    battery_percent: float,
    blocked_reason: str | None,
) -> Px4GazeboSanitizedTelemetry:
    sample = {
        "sample_id": (
            f"{sidecar_contract.sidecar_id}:{contract.mission_id}:"
            f"{request_kind.value}:{phase.value}"
        ),
        "source": {
            "source_kind": "gazebo_delivery_sidecar_v0",
            "source_id": sidecar_contract.sidecar_id,
            "vehicle_id": f"{scenario.scenario_id}:simulated_vehicle",
        },
        "captured_at": produced_at.isoformat().replace("+00:00", "Z"),
        "telemetry": _phase_measurements(
            phase,
            battery_percent=battery_percent,
            blocked_reason=blocked_reason,
        ),
        "metadata": {
            "artifact_only": True,
            "simulation_only_sidecar_v0": True,
            "phase": phase.value,
            "scenario_id": scenario.scenario_id,
            "sidecar_contract_id": sidecar_contract.sidecar_contract_id,
        },
    }
    return sanitize_px4_gazebo_telemetry_sample(sample)


def _step_phase(phase: GazeboDeliverySidecarPhase) -> SimulatedDeliveryEpisodePhase:
    if phase is GazeboDeliverySidecarPhase.PREFLIGHT:
        return SimulatedDeliveryEpisodePhase.PREFLIGHT_REVIEW
    if phase is GazeboDeliverySidecarPhase.COMPLETED:
        return SimulatedDeliveryEpisodePhase.COMPLETED
    if phase is GazeboDeliverySidecarPhase.BLOCKED:
        return SimulatedDeliveryEpisodePhase.BLOCKED
    return SimulatedDeliveryEpisodePhase.IN_SIMULATION


def _step_status(phase: GazeboDeliverySidecarPhase) -> SimulatedDeliveryStepStatus:
    if phase is GazeboDeliverySidecarPhase.BLOCKED:
        return SimulatedDeliveryStepStatus.BLOCKED
    if phase is GazeboDeliverySidecarPhase.COMPLETED:
        return SimulatedDeliveryStepStatus.COMPLETED
    return SimulatedDeliveryStepStatus.OBSERVED


def _build_phase_step(
    *,
    phase: GazeboDeliverySidecarPhase,
    telemetry: Px4GazeboSanitizedTelemetry,
    blocked_reasons: tuple[str, ...],
    warning_reasons: tuple[str, ...],
    produced_at: datetime,
) -> SimulatedDeliveryStep:
    telemetry_refs = (f"px4_gazebo_sanitized_telemetry:{telemetry.telemetry_id}",)
    payload = {
        "phase": phase.value,
        "status": _step_status(phase).value,
        "telemetry_refs": telemetry_refs,
        "blocked_reasons": blocked_reasons,
        "warning_reasons": warning_reasons,
    }
    return SimulatedDeliveryStep(
        step_id=_stable_id("simulated_delivery_step", payload),
        phase=_step_phase(phase),
        status=_step_status(phase),
        summary=f"simulation-only Gazebo delivery sidecar observed {phase.value}",
        telemetry_refs=telemetry_refs,
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        observed_at=produced_at,
        metadata={
            "step_kind": "gazebo_delivery_sidecar_v0_phase",
            "sidecar_phase": phase.value,
            "artifact_only": True,
            "simulation_only": True,
            "no_dispatch_surface": True,
            "no_entity_mutation": True,
        },
    )


def build_gazebo_delivery_sidecar_v0_artifacts(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any] | None = None,
    sidecar_contract: GazeboDeliverySidecarContract | Mapping[str, Any] | None = None,
    request_kind: GazeboDeliverySidecarRequestKind | str,
    current_phase: GazeboDeliverySidecarPhase | str | None = None,
    blocked_reason: str | None = None,
    battery_percent: float = 88.0,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one simulation-only Gazebo delivery sidecar response."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    produced_at = _utc(now)
    contract = _to_contract(delivery_mission_contract)
    scenario = _to_scenario(
        gazebo_delivery_scenario,
        contract=contract,
        now=produced_at,
    )
    validated_sidecar = _to_sidecar_contract(
        sidecar_contract,
        contract=contract,
        scenario=scenario,
        now=produced_at,
    )
    _validate_sidecar_v0_returned_artifact_schemas(validated_sidecar)
    kind = _to_request_kind(request_kind)
    if kind not in validated_sidecar.accepted_simulation_requests:
        raise GazeboDeliverySidecarV0Error(
            f"sidecar contract does not accept request kind: {kind.value}"
        )
    previous_phase = _to_phase(current_phase)
    reason = str(blocked_reason).strip() if blocked_reason else None
    phase = _phase_for_request(
        request_kind=kind,
        current_phase=previous_phase,
        blocked_reason=reason,
    )
    blocked_reasons = (f"sidecar_blocked_{reason}",) if reason else ()
    telemetry = _build_phase_telemetry(
        contract=contract,
        scenario=scenario,
        sidecar_contract=validated_sidecar,
        request_kind=kind,
        phase=phase,
        produced_at=produced_at,
        battery_percent=battery_percent,
        blocked_reason=reason,
    )
    step = _build_phase_step(
        phase=phase,
        telemetry=telemetry,
        blocked_reasons=blocked_reasons,
        warning_reasons=(),
        produced_at=produced_at,
    )
    artifact_refs = (
        f"gazebo_delivery_sidecar_contract:{validated_sidecar.sidecar_contract_id}",
        f"px4_gazebo_sanitized_telemetry:{telemetry.telemetry_id}",
        f"simulated_delivery_step:{step.step_id}",
    )
    payload = {
        "sidecar_contract_id": validated_sidecar.sidecar_contract_id,
        "request_kind": kind.value,
        "previous_phase": previous_phase.value if previous_phase else None,
        "phase": phase.value,
        "telemetry_id": telemetry.telemetry_id,
        "step_id": step.step_id,
        "blocked_reasons": blocked_reasons,
    }
    result = GazeboDeliverySidecarResult(
        sidecar_result_id=_stable_id("gazebo_delivery_sidecar_result", payload),
        sidecar_contract_id=validated_sidecar.sidecar_contract_id,
        delivery_mission_contract_id=contract.contract_id,
        delivery_mission_id=contract.mission_id,
        gazebo_delivery_scenario_id=scenario.scenario_id,
        request_kind=kind,
        phase=phase,
        previous_phase=previous_phase,
        sanitized_telemetry_id=telemetry.telemetry_id,
        simulated_delivery_step_id=step.step_id,
        returned_artifact_refs=artifact_refs,
        blocked_reasons=blocked_reasons,
        produced_at=produced_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "simulation_only_sidecar_v0": True,
            "sidecar_returns_artifacts_only": True,
            "mission_os_validates_returned_artifacts_before_attach": True,
            "no_raw_gazebo_entity_mutation_surface": True,
            "no_ros_mavlink_command_surface": True,
        },
    )
    return {
        "gazebo_delivery_sidecar_contract": validated_sidecar.model_dump(mode="json"),
        "gazebo_delivery_sidecar_result": result.model_dump(mode="json"),
        "px4_gazebo_sanitized_telemetry": telemetry.model_dump(mode="json"),
        "simulated_delivery_step": step.model_dump(mode="json"),
    }


def build_gazebo_delivery_sidecar_v0_sequence(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any] | None = None,
    sidecar_contract: GazeboDeliverySidecarContract | Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], ...]:
    """Build a deterministic preflight-to-completed sidecar artifact sequence."""

    base_time = _utc(now)
    contract = _to_contract(delivery_mission_contract)
    scenario = _to_scenario(
        gazebo_delivery_scenario,
        contract=contract,
        now=base_time,
    )
    validated_sidecar = _to_sidecar_contract(
        sidecar_contract,
        contract=contract,
        scenario=scenario,
        now=base_time,
    )
    artifacts: list[dict[str, Any]] = []
    current_phase: GazeboDeliverySidecarPhase | None = None
    requests = [
        GazeboDeliverySidecarRequestKind.START_DELIVERY_SIMULATION,
        GazeboDeliverySidecarRequestKind.ADVANCE_DELIVERY_STEP,
        GazeboDeliverySidecarRequestKind.ADVANCE_DELIVERY_STEP,
        GazeboDeliverySidecarRequestKind.ADVANCE_DELIVERY_STEP,
        GazeboDeliverySidecarRequestKind.ADVANCE_DELIVERY_STEP,
    ]
    for index, kind in enumerate(requests):
        item = build_gazebo_delivery_sidecar_v0_artifacts(
            delivery_mission_contract=contract,
            gazebo_delivery_scenario=scenario,
            sidecar_contract=validated_sidecar,
            request_kind=kind,
            current_phase=current_phase,
            now=base_time + timedelta(seconds=index),
        )
        current_phase = GazeboDeliverySidecarPhase(
            item["gazebo_delivery_sidecar_result"]["phase"]
        )
        artifacts.append(item)
    return tuple(artifacts)


def run_gazebo_delivery_sidecar_v0_task(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Run sidecar v0 artifacts through the simulated delivery runner v0."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise GazeboDeliverySidecarV0Error(
            f"task {task_id} not found; cannot run sidecar v0"
        )
    base_time = _utc(now)
    contract = _to_contract(delivery_mission_contract)
    scenario = _to_scenario(
        gazebo_delivery_scenario,
        contract=contract,
        now=base_time,
    )
    sequence = build_gazebo_delivery_sidecar_v0_sequence(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        now=base_time,
    )
    final_artifacts = sequence[-1]
    store.update(
        task_id,
        artifacts={
            "gazebo_delivery_sidecar_v0_sequence": [
                item["gazebo_delivery_sidecar_result"] for item in sequence
            ],
            "gazebo_delivery_sidecar_v0_steps": [
                item["simulated_delivery_step"] for item in sequence
            ],
        },
    )
    return run_simulated_delivery_task_v0(
        task_id,
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        sanitized_telemetry=final_artifacts["px4_gazebo_sanitized_telemetry"],
        now=base_time + timedelta(seconds=len(sequence)),
        task_store_factory=lambda: store,
    )


def create_and_run_gazebo_delivery_sidecar_v0_task(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    gazebo_delivery_scenario: GazeboDeliveryScenario | Mapping[str, Any] | None = None,
    title: str = "Gazebo delivery sidecar v0",
    owner_session_id: str | None = None,
    owner_user_id: str | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Create a task and complete it through sidecar v0 + runner v0."""

    store: TaskStore = (task_store_factory or get_task_store)()
    task = store.create(
        kind="gazebo_delivery_sidecar_v0",
        title=title,
        status="running",
        owner_session_id=owner_session_id,
        owner_user_id=owner_user_id,
        artifacts={"sidecar": {"schema_version": "gazebo_delivery_sidecar_v0"}},
    )
    return run_gazebo_delivery_sidecar_v0_task(
        task["task_id"],
        delivery_mission_contract=delivery_mission_contract,
        gazebo_delivery_scenario=gazebo_delivery_scenario,
        now=now,
        task_store_factory=lambda: store,
    )


__all__ = [
    "GAZEBO_DELIVERY_SIDECAR_RESULT_SCHEMA_VERSION",
    "GazeboDeliverySidecarPhase",
    "GazeboDeliverySidecarResult",
    "GazeboDeliverySidecarV0Error",
    "build_gazebo_delivery_sidecar_v0_artifacts",
    "build_gazebo_delivery_sidecar_v0_sequence",
    "create_and_run_gazebo_delivery_sidecar_v0_task",
    "run_gazebo_delivery_sidecar_v0_task",
]

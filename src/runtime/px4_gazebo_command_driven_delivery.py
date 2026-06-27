"""Command-gated PX4/Gazebo delivery phase smoke artifacts.

This module records the first mountain-side delivery phase boundary after the
real MAVLink transport milestone: a Mission OS runner must see an
operator-approved real MAVLink response from PX4 SITL and an actual Gazebo
delivery pose phase before it can advance a delivery phase.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.gz_sim_log_collector import (
    delivery_phases_from_entity_poses,
    parse_gz_sim_entity_pose,
)
from src.runtime.px4_delivery_command_preflight import PX4SimulationCommandKind
from src.runtime.px4_real_mavlink_transport import (
    PX4RealMAVLinkDispatchResult,
    PX4RealMAVLinkDispatchStatus,
)
from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_COMMAND_DRIVEN_PHASE_EVIDENCE_SCHEMA_VERSION = (
    "px4_gazebo_command_driven_phase_evidence.v1"
)
PX4_GAZEBO_COMMAND_DRIVEN_RUNNER_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_command_driven_runner_result.v1"
)
COMMAND_GATED_OBSERVATION_COMPLETION_BASIS = (
    "command_response_and_independent_gazebo_pose_phase"
)

REQUIRED_DELIVERY_PHASES = ("pickup", "enroute", "dropoff", "completed")


class PX4GazeboCommandDrivenDeliveryError(RuntimeError):
    """Raised when command-gated PX4/Gazebo delivery evidence is unsafe."""


class PX4GazeboCommandDrivenRunnerStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


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


def _to_real_dispatch(
    value: PX4RealMAVLinkDispatchResult | Mapping[str, Any],
) -> PX4RealMAVLinkDispatchResult:
    if isinstance(value, PX4RealMAVLinkDispatchResult):
        return value
    return PX4RealMAVLinkDispatchResult.model_validate(dict(value))


def _phases_from_pose_sample(
    pose_sample: str,
) -> tuple[tuple[str, ...], dict[str, float | str]]:
    pose = parse_gz_sim_entity_pose(pose_sample)
    phases = delivery_phases_from_entity_poses([pose])
    if not phases:
        raise PX4GazeboCommandDrivenDeliveryError(
            "Gazebo pose sample did not map to a delivery phase"
        )
    return tuple(phases), pose


def _real_dispatch_ref(dispatch: PX4RealMAVLinkDispatchResult) -> str:
    return f"px4_real_mavlink_dispatch_result:{dispatch.dispatch_result_id}"


class _CommandDrivenSafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[True] = True
    bounded_allowlist_enforced: Literal[True] = True
    simulation_mavlink_dispatch_allowed: Literal[True] = True
    simulation_actuator_effect_allowed: Literal[True] = True
    physical_actuator_execution_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False


class PX4GazeboCommandDrivenPhaseEvidence(_CommandDrivenSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_COMMAND_DRIVEN_PHASE_EVIDENCE_SCHEMA_VERSION] = (
        PX4_GAZEBO_COMMAND_DRIVEN_PHASE_EVIDENCE_SCHEMA_VERSION
    )
    evidence_id: str
    mission_phase: str = Field(min_length=1)
    command_kind: PX4SimulationCommandKind
    real_dispatch_result_ref: str = Field(min_length=1)
    real_mavlink_frame_sent_to_px4: Literal[True] = True
    real_mavlink_response_received_from_px4: Literal[True] = True
    target_system: Literal[1] = 1
    target_component: Literal[1] = 1
    dispatch_transport_semantics: str = Field(min_length=1)
    delivery_phase_command_executed: bool
    gazebo_pose_phase_observed: Literal[True] = True
    gazebo_pose_phase: str = Field(min_length=1)
    gazebo_pose_x_m: float
    gazebo_entity_motion_observed: bool
    px4_gazebo_coupled_motion_observed: bool
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_phase(self) -> "PX4GazeboCommandDrivenPhaseEvidence":
        if self.gazebo_pose_phase != self.mission_phase:
            raise PX4GazeboCommandDrivenDeliveryError(
                "Gazebo pose phase must match command-gated mission phase"
            )
        return self


class PX4GazeboCommandDrivenRunnerResult(_CommandDrivenSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_COMMAND_DRIVEN_RUNNER_RESULT_SCHEMA_VERSION] = (
        PX4_GAZEBO_COMMAND_DRIVEN_RUNNER_RESULT_SCHEMA_VERSION
    )
    runner_result_id: str
    final_status: PX4GazeboCommandDrivenRunnerStatus
    phase_evidence_refs: tuple[str, ...]
    observed_delivery_phases: tuple[str, ...]
    missing_phases: tuple[str, ...]
    completion_basis: Literal["command_response_and_independent_gazebo_pose_phase"] = (
        COMMAND_GATED_OBSERVATION_COMPLETION_BASIS
    )
    completion_mode: Literal["command_gated_observation_completed"] = (
        "command_gated_observation_completed"
    )
    px4_command_response_required: Literal[True] = True
    gazebo_pose_phase_required: Literal[True] = True
    delivery_phase_command_executed: bool
    simulation_actuator_effect_observed: bool
    px4_gazebo_coupled_motion_observed: bool
    completed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_completed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def build_px4_gazebo_command_driven_phase_evidence(
    *,
    real_dispatch_result: PX4RealMAVLinkDispatchResult | Mapping[str, Any],
    mission_phase: str,
    gazebo_pose_sample: str,
    previous_gazebo_pose_sample: str | None = None,
    px4_gazebo_coupled_motion_observed: bool = False,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboCommandDrivenPhaseEvidence:
    dispatch = _to_real_dispatch(real_dispatch_result)
    if dispatch.dispatch_status != PX4RealMAVLinkDispatchStatus.SENT:
        raise PX4GazeboCommandDrivenDeliveryError(
            "command-driven phase evidence requires sent real MAVLink dispatch"
        )
    if not dispatch.mavlink_frame_sent or not dispatch.mavlink_frame_received:
        raise PX4GazeboCommandDrivenDeliveryError(
            "command-driven phase evidence requires PX4 MAVLink response"
        )
    if dispatch.target_system != 1 or dispatch.target_component != 1:
        raise PX4GazeboCommandDrivenDeliveryError(
            "command-driven phase evidence requires PX4 SITL target 1/1"
        )
    phases, pose = _phases_from_pose_sample(gazebo_pose_sample)
    if mission_phase not in phases:
        raise PX4GazeboCommandDrivenDeliveryError(
            f"Gazebo pose phases {phases!r} do not match mission phase {mission_phase!r}"
        )
    motion_observed = False
    if previous_gazebo_pose_sample:
        _previous_phases, previous_pose = _phases_from_pose_sample(
            previous_gazebo_pose_sample
        )
        motion_observed = abs(float(pose["x"]) - float(previous_pose["x"])) >= 0.25
    observed_at = _utc(now)
    payload = {
        "real_dispatch_result_id": dispatch.dispatch_result_id,
        "mission_phase": mission_phase,
        "gazebo_pose_x_m": float(pose["x"]),
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboCommandDrivenPhaseEvidence(
        evidence_id=_stable_id("px4_gazebo_command_driven_phase_evidence", payload),
        mission_phase=mission_phase,
        command_kind=dispatch.command_kind,
        real_dispatch_result_ref=_real_dispatch_ref(dispatch),
        target_system=dispatch.target_system,
        target_component=dispatch.target_component,
        dispatch_transport_semantics=dispatch.dispatch_transport_semantics,
        delivery_phase_command_executed=dispatch.delivery_phase_command_executed,
        gazebo_pose_phase=mission_phase,
        gazebo_pose_x_m=float(pose["x"]),
        gazebo_entity_motion_observed=motion_observed,
        px4_gazebo_coupled_motion_observed=px4_gazebo_coupled_motion_observed,
        observed_at=observed_at,
        metadata={
            **(metadata or {}),
            "issue": 330,
            "parent_epic": 307,
            "completion_basis": "px4_command_response_and_gazebo_pose_correlation",
            "px4_gazebo_physical_coupling_not_claimed": (
                not px4_gazebo_coupled_motion_observed
            ),
        },
    )


def build_px4_gazebo_command_driven_runner_result(
    *,
    phase_evidence: Sequence[PX4GazeboCommandDrivenPhaseEvidence | Mapping[str, Any]],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboCommandDrivenRunnerResult:
    normalized = [
        (
            item
            if isinstance(item, PX4GazeboCommandDrivenPhaseEvidence)
            else PX4GazeboCommandDrivenPhaseEvidence.model_validate(dict(item))
        )
        for item in phase_evidence
    ]
    if not normalized:
        raise PX4GazeboCommandDrivenDeliveryError(
            "at least one command-driven phase evidence artifact is required"
        )
    phases = _ordered_tuple([item.mission_phase for item in normalized])
    missing = tuple(phase for phase in REQUIRED_DELIVERY_PHASES if phase not in phases)
    final_status = (
        PX4GazeboCommandDrivenRunnerStatus.COMPLETED
        if not missing
        else PX4GazeboCommandDrivenRunnerStatus.BLOCKED
    )
    refs = tuple(
        f"px4_gazebo_command_driven_phase_evidence:{item.evidence_id}"
        for item in normalized
    )
    delivery_phase_command_executed = all(
        item.delivery_phase_command_executed for item in normalized
    )
    coupled = all(item.px4_gazebo_coupled_motion_observed for item in normalized)
    completed_at = _utc(now)
    payload = {
        "phase_evidence_refs": refs,
        "observed_delivery_phases": phases,
        "final_status": final_status.value,
        "missing_phases": missing,
        "completion_basis": COMMAND_GATED_OBSERVATION_COMPLETION_BASIS,
        "delivery_phase_command_executed": delivery_phase_command_executed,
        "px4_gazebo_coupled_motion_observed": coupled,
    }
    return PX4GazeboCommandDrivenRunnerResult(
        runner_result_id=_stable_id("px4_gazebo_command_driven_runner_result", payload),
        final_status=final_status,
        phase_evidence_refs=refs,
        observed_delivery_phases=phases,
        missing_phases=missing,
        delivery_phase_command_executed=delivery_phase_command_executed,
        simulation_actuator_effect_observed=coupled,
        px4_gazebo_coupled_motion_observed=coupled,
        completed_at=completed_at,
        metadata={
            **(metadata or {}),
            "issue": 330,
            "related_issue": 331,
            "parent_epic": 307,
            "completion_basis": COMMAND_GATED_OBSERVATION_COMPLETION_BASIS,
            "required_phases": list(REQUIRED_DELIVERY_PHASES),
            "delivery_phase_command_not_executed": not delivery_phase_command_executed,
            "simulation_actuator_effect_not_observed": not coupled,
            "px4_gazebo_physical_coupling_not_claimed": not coupled,
        },
    )


def run_px4_gazebo_command_driven_delivery_task(
    task_id: str,
    *,
    phase_evidence: Sequence[PX4GazeboCommandDrivenPhaseEvidence | Mapping[str, Any]],
    now: datetime | None = None,
    task_store_factory: Any | None = None,
) -> dict[str, Any]:
    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboCommandDrivenDeliveryError(
            f"task {task_id} not found; cannot run PX4/Gazebo command-driven delivery"
        )
    runner_result = build_px4_gazebo_command_driven_runner_result(
        phase_evidence=phase_evidence,
        now=now,
    )
    normalized = [
        (
            item
            if isinstance(item, PX4GazeboCommandDrivenPhaseEvidence)
            else PX4GazeboCommandDrivenPhaseEvidence.model_validate(dict(item))
        )
        for item in phase_evidence
    ]
    updated = store.update(
        task_id,
        status=runner_result.final_status.value,
        artifacts={
            "px4_gazebo_command_driven_phase_evidence": [
                item.model_dump(mode="json") for item in normalized
            ],
            "px4_gazebo_command_driven_runner_result": runner_result.model_dump(
                mode="json"
            ),
        },
        ended_at=time.time(),
    )
    if updated is None:
        raise PX4GazeboCommandDrivenDeliveryError(
            f"task {task_id} disappeared while running PX4/Gazebo command delivery"
        )
    return updated


__all__ = [
    "COMMAND_GATED_OBSERVATION_COMPLETION_BASIS",
    "PX4_GAZEBO_COMMAND_DRIVEN_PHASE_EVIDENCE_SCHEMA_VERSION",
    "PX4_GAZEBO_COMMAND_DRIVEN_RUNNER_RESULT_SCHEMA_VERSION",
    "PX4GazeboCommandDrivenDeliveryError",
    "PX4GazeboCommandDrivenPhaseEvidence",
    "PX4GazeboCommandDrivenRunnerResult",
    "PX4GazeboCommandDrivenRunnerStatus",
    "build_px4_gazebo_command_driven_phase_evidence",
    "build_px4_gazebo_command_driven_runner_result",
    "run_px4_gazebo_command_driven_delivery_task",
]

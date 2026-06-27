"""PX4/Gazebo coupled delivery smoke artifacts.

This module is the first #331 boundary: Mission OS records that an approved,
bounded MAVLink command reached an actual PX4 SITL instance and that the coupled
Gazebo vehicle pose changed through delivery phases as a result.
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

from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_COUPLED_DELIVERY_PHASE_EVIDENCE_SCHEMA_VERSION = (
    "px4_gazebo_coupled_delivery_phase_evidence.v1"
)
PX4_GAZEBO_COUPLED_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_coupled_delivery_runner_result.v1"
)
PX4_GAZEBO_COUPLED_COMMAND_APPROVAL_SCHEMA_VERSION = (
    "px4_gazebo_coupled_command_approval.v1"
)
PX4_GAZEBO_COUPLED_COMMAND_ALLOWLIST_SCHEMA_VERSION = (
    "px4_gazebo_coupled_command_allowlist.v1"
)
PX4_GAZEBO_COUPLED_COMMAND_DIAGNOSTICS_SCHEMA_VERSION = (
    "px4_gazebo_coupled_command_diagnostics.v1"
)
COUPLED_DELIVERY_COMPLETION_BASIS = (
    "actual_px4_mavlink_command_and_gazebo_coupled_motion"
)
COUPLED_DELIVERY_PHASES = ("pickup", "enroute", "dropoff", "completed")
MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_NAV_LAND = 21


class PX4GazeboCoupledDeliveryError(RuntimeError):
    """Raised when coupled PX4/Gazebo delivery evidence is unsafe."""


class PX4GazeboCoupledDeliveryRunnerStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


class PX4GazeboCoupledPhaseEffectKind(str, Enum):
    ARMED = "armed"
    TAKEOFF_CLIMB = "takeoff_climb"
    LANDING_DESCENT = "landing_descent"
    LANDED_DISARMED = "landed_disarmed"


class PX4GazeboCoupledCommandFailureReason(str, Enum):
    MAVLINK_TIMEOUT = "mavlink_timeout"
    COMMAND_REJECTED = "command_rejected"
    WRONG_TARGET = "wrong_target"
    NON_LOOPBACK_ENDPOINT = "non_loopback_endpoint"
    HARDWARE_TARGET_REQUESTED = "hardware_target_requested"
    MISSING_APPROVAL = "missing_approval"
    MISSING_ALLOWLIST = "missing_allowlist"


_PHASE_EFFECTS = {
    "pickup": {
        "effect": PX4GazeboCoupledPhaseEffectKind.ARMED,
        "requires_z_motion": False,
        "requires_ground_contact": False,
        "marker": "Armed by external command",
    },
    "enroute": {
        "effect": PX4GazeboCoupledPhaseEffectKind.TAKEOFF_CLIMB,
        "requires_z_motion": True,
        "requires_ground_contact": False,
        "marker": "Takeoff detected",
    },
    "dropoff": {
        "effect": PX4GazeboCoupledPhaseEffectKind.LANDING_DESCENT,
        "requires_z_motion": True,
        "requires_ground_contact": False,
        "marker": "Landing detected",
    },
    "completed": {
        "effect": PX4GazeboCoupledPhaseEffectKind.LANDED_DISARMED,
        "requires_z_motion": True,
        "requires_ground_contact": True,
        "marker": "Disarmed by landing",
    },
}


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


class _CoupledDeliverySafetyBoundary(BaseModel):
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


class PX4GazeboCoupledCommandApproval(_CoupledDeliverySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_COUPLED_COMMAND_APPROVAL_SCHEMA_VERSION] = (
        PX4_GAZEBO_COUPLED_COMMAND_APPROVAL_SCHEMA_VERSION
    )
    approval_id: str
    operator_approval_performed: bool
    approved_command_ids: tuple[int, ...]
    approved_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("approved_at", mode="before")
    @classmethod
    def _coerce_approved_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("approved_command_ids", mode="before")
    @classmethod
    def _coerce_approved_command_ids(cls, value: Any) -> tuple[int, ...]:
        return tuple(int(item) for item in value)


class PX4GazeboCoupledCommandAllowlist(_CoupledDeliverySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_COUPLED_COMMAND_ALLOWLIST_SCHEMA_VERSION] = (
        PX4_GAZEBO_COUPLED_COMMAND_ALLOWLIST_SCHEMA_VERSION
    )
    allowlist_id: str
    approval_ref: str = Field(min_length=1)
    allowed_command_ids: tuple[int, ...]
    allowed_command_names: tuple[str, ...]
    bounded_dispatch_only: Literal[True] = True
    raw_mavlink_payload_allowed: Literal[False] = False
    generated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("allowed_command_ids", mode="before")
    @classmethod
    def _coerce_allowed_command_ids(cls, value: Any) -> tuple[int, ...]:
        return tuple(int(item) for item in value)

    @field_validator("allowed_command_names", mode="before")
    @classmethod
    def _coerce_allowed_command_names(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])


class PX4GazeboCoupledCommandDiagnostics(_CoupledDeliverySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_COUPLED_COMMAND_DIAGNOSTICS_SCHEMA_VERSION] = (
        PX4_GAZEBO_COUPLED_COMMAND_DIAGNOSTICS_SCHEMA_VERSION
    )
    operator_approval_performed: bool
    bounded_allowlist_enforced: bool
    diagnostics_id: str
    final_status: Literal["blocked"] = "blocked"
    failure_reason: PX4GazeboCoupledCommandFailureReason
    blocked_reasons: tuple[str, ...]
    command_id: int | None = None
    command_name: str | None = None
    target_system: int | None = None
    target_component: int | None = None
    expected_target_system: Literal[1] = 1
    expected_target_component: Literal[1] = 1
    endpoint_host: str | None = None
    endpoint_loopback_required: Literal[True] = True
    approval_ref: str | None = None
    allowlist_ref: str | None = None
    operator_approval_available: bool
    allowlist_available: bool
    command_allowlisted: bool
    mavlink_command_sent_to_px4: bool
    mavlink_response_received_from_px4: bool
    px4_command_ack_result: str | None = None
    retry_attempted: Literal[False] = False
    stronger_execution_attempted: Literal[False] = False
    task_artifacts_preserved: Literal[True] = True
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_blocked_reasons(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @model_validator(mode="after")
    def _validate_diagnostics(self) -> "PX4GazeboCoupledCommandDiagnostics":
        if not self.blocked_reasons:
            raise PX4GazeboCoupledDeliveryError(
                "coupled command diagnostics require blocked reasons"
            )
        if self.failure_reason.value not in self.blocked_reasons:
            raise PX4GazeboCoupledDeliveryError(
                "blocked reasons must include the coupled command failure reason"
            )
        if (
            self.failure_reason
            == PX4GazeboCoupledCommandFailureReason.NON_LOOPBACK_ENDPOINT
            and self.endpoint_host in {None, "127.0.0.1", "localhost", "::1"}
        ):
            raise PX4GazeboCoupledDeliveryError(
                "non-loopback diagnostics require a non-loopback endpoint"
            )
        if (
            self.failure_reason == PX4GazeboCoupledCommandFailureReason.WRONG_TARGET
            and self.target_system == 1
            and self.target_component == 1
        ):
            raise PX4GazeboCoupledDeliveryError(
                "wrong-target diagnostics require a non-1/1 target"
            )
        if (
            self.failure_reason == PX4GazeboCoupledCommandFailureReason.MISSING_APPROVAL
            and self.operator_approval_available
        ):
            raise PX4GazeboCoupledDeliveryError(
                "missing-approval diagnostics cannot have approval available"
            )
        if (
            self.failure_reason
            == PX4GazeboCoupledCommandFailureReason.MISSING_ALLOWLIST
            and self.allowlist_available
        ):
            raise PX4GazeboCoupledDeliveryError(
                "missing-allowlist diagnostics cannot have allowlist available"
            )
        if self.failure_reason == PX4GazeboCoupledCommandFailureReason.MAVLINK_TIMEOUT:
            if not self.mavlink_command_sent_to_px4:
                raise PX4GazeboCoupledDeliveryError(
                    "mavlink_timeout diagnostics require a sent MAVLink command"
                )
            if self.mavlink_response_received_from_px4:
                raise PX4GazeboCoupledDeliveryError(
                    "mavlink_timeout diagnostics cannot include a PX4 response"
                )
        if self.failure_reason == PX4GazeboCoupledCommandFailureReason.COMMAND_REJECTED:
            if not self.mavlink_command_sent_to_px4:
                raise PX4GazeboCoupledDeliveryError(
                    "command_rejected diagnostics require a sent MAVLink command"
                )
            if not self.mavlink_response_received_from_px4:
                raise PX4GazeboCoupledDeliveryError(
                    "command_rejected diagnostics require a PX4 command response"
                )
            ack = (self.px4_command_ack_result or "").strip().upper()
            if not ack or not any(
                token in ack for token in ("DENIED", "FAILED", "UNSUPPORTED", "REJECT")
            ):
                raise PX4GazeboCoupledDeliveryError(
                    "command_rejected diagnostics require a rejection ack result"
                )
        if (
            self.failure_reason
            == PX4GazeboCoupledCommandFailureReason.HARDWARE_TARGET_REQUESTED
            and self.mavlink_command_sent_to_px4
        ):
            raise PX4GazeboCoupledDeliveryError(
                "hardware_target_requested diagnostics must block before MAVLink send"
            )
        return self


class PX4GazeboCoupledDeliveryPhaseEvidence(_CoupledDeliverySafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_COUPLED_DELIVERY_PHASE_EVIDENCE_SCHEMA_VERSION
    ] = PX4_GAZEBO_COUPLED_DELIVERY_PHASE_EVIDENCE_SCHEMA_VERSION
    evidence_id: str
    mission_phase: Literal["pickup", "enroute", "dropoff", "completed"]
    px4_container_image: str = Field(min_length=1)
    px4_sitl_model: Literal["gz_x500"] = "gz_x500"
    gazebo_world: Literal["default"] = "default"
    gazebo_entity_name: Literal["x500_0"] = "x500_0"
    operator_approval_ref: str = Field(min_length=1)
    bounded_allowlist_ref: str = Field(min_length=1)
    mavlink_transport_scope: Literal["in_container_loopback_px4_sitl"] = (
        "in_container_loopback_px4_sitl"
    )
    mavlink_command_names: tuple[str, ...] = Field(min_length=1)
    mavlink_command_ids: tuple[int, ...] = Field(min_length=1)
    mavlink_command_sent_to_px4: Literal[True] = True
    px4_external_command_accepted: Literal[True] = True
    px4_acceptance_log_markers: tuple[str, ...] = Field(min_length=1)
    phase_effect_kind: PX4GazeboCoupledPhaseEffectKind
    phase_requires_z_motion: bool
    phase_requires_ground_contact: bool
    phase_requires_px4_log_marker: Literal[True] = True
    delivery_phase_command_executed: Literal[True] = True
    gazebo_pose_before_z_m: float
    gazebo_pose_after_z_m: float
    gazebo_pose_peak_z_m: float
    gazebo_pose_motion_delta_z_m: float
    gazebo_pose_motion_observed: Literal[True] = True
    simulation_actuator_effect_observed: Literal[True] = True
    px4_gazebo_coupled_motion_observed: Literal[True] = True
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("mavlink_command_names", mode="before")
    @classmethod
    def _coerce_command_names(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @field_validator("mavlink_command_ids", mode="before")
    @classmethod
    def _coerce_command_ids(cls, value: Any) -> tuple[int, ...]:
        return tuple(int(item) for item in value)

    @field_validator("px4_acceptance_log_markers", mode="before")
    @classmethod
    def _coerce_markers(cls, value: Any) -> tuple[str, ...]:
        return _ordered_tuple([str(item) for item in value])

    @model_validator(mode="after")
    def _validate_coupling(self) -> "PX4GazeboCoupledDeliveryPhaseEvidence":
        expected = _PHASE_EFFECTS[self.mission_phase]
        if self.phase_effect_kind != expected["effect"]:
            raise PX4GazeboCoupledDeliveryError(
                "coupled delivery phase effect kind does not match mission phase"
            )
        if self.phase_requires_z_motion != expected["requires_z_motion"]:
            raise PX4GazeboCoupledDeliveryError(
                "coupled delivery phase z-motion requirement mismatch"
            )
        if self.phase_requires_ground_contact != expected["requires_ground_contact"]:
            raise PX4GazeboCoupledDeliveryError(
                "coupled delivery phase ground-contact requirement mismatch"
            )
        if expected["marker"] not in self.px4_acceptance_log_markers:
            raise PX4GazeboCoupledDeliveryError(
                "coupled delivery phase requires the expected PX4 log marker"
            )
        if self.phase_requires_z_motion and self.gazebo_pose_motion_delta_z_m < 0.5:
            raise PX4GazeboCoupledDeliveryError(
                "coupled delivery evidence requires Gazebo z motion >= 0.5m"
            )
        if self.gazebo_pose_peak_z_m < max(
            self.gazebo_pose_before_z_m,
            self.gazebo_pose_after_z_m,
        ):
            raise PX4GazeboCoupledDeliveryError(
                "coupled delivery evidence requires a Gazebo pose peak"
            )
        if (
            self.phase_requires_ground_contact
            and abs(self.gazebo_pose_after_z_m) > 0.15
        ):
            raise PX4GazeboCoupledDeliveryError(
                "completed coupled delivery evidence requires ground contact"
            )
        return self


class PX4GazeboCoupledDeliveryRunnerResult(_CoupledDeliverySafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_COUPLED_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION
    ] = PX4_GAZEBO_COUPLED_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION
    runner_result_id: str
    final_status: PX4GazeboCoupledDeliveryRunnerStatus
    phase_evidence_refs: tuple[str, ...]
    observed_delivery_phases: tuple[str, ...]
    missing_phases: tuple[str, ...]
    completion_basis: Literal[
        "actual_px4_mavlink_command_and_gazebo_coupled_motion"
    ] = COUPLED_DELIVERY_COMPLETION_BASIS
    completion_mode: Literal["coupled_command_driven_delivery_completed"] = (
        "coupled_command_driven_delivery_completed"
    )
    delivery_phase_command_executed: Literal[True] = True
    simulation_actuator_effect_observed: Literal[True] = True
    px4_gazebo_coupled_motion_observed: Literal[True] = True
    actual_px4_sitl_container_started: Literal[True] = True
    actual_gazebo_world_started: Literal[True] = True
    actual_gz_bridge_started: Literal[True] = True
    completed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_completed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_runner(self) -> "PX4GazeboCoupledDeliveryRunnerResult":
        if (
            self.final_status == PX4GazeboCoupledDeliveryRunnerStatus.COMPLETED
            and self.missing_phases
        ):
            raise PX4GazeboCoupledDeliveryError(
                "completed coupled delivery runner cannot have missing phases"
            )
        return self


def _approval_ref(approval: PX4GazeboCoupledCommandApproval) -> str:
    return f"px4_gazebo_coupled_command_approval:{approval.approval_id}"


def _allowlist_ref(allowlist: PX4GazeboCoupledCommandAllowlist) -> str:
    return f"px4_gazebo_coupled_command_allowlist:{allowlist.allowlist_id}"


def _to_approval(
    value: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
) -> PX4GazeboCoupledCommandApproval:
    if isinstance(value, PX4GazeboCoupledCommandApproval):
        return value
    return PX4GazeboCoupledCommandApproval.model_validate(dict(value))


def _to_allowlist(
    value: PX4GazeboCoupledCommandAllowlist | Mapping[str, Any],
) -> PX4GazeboCoupledCommandAllowlist:
    if isinstance(value, PX4GazeboCoupledCommandAllowlist):
        return value
    return PX4GazeboCoupledCommandAllowlist.model_validate(dict(value))


def build_px4_gazebo_coupled_command_approval(
    *,
    operator_approval_performed: bool,
    approved_command_ids: Sequence[int] = (
        MAV_CMD_COMPONENT_ARM_DISARM,
        MAV_CMD_NAV_TAKEOFF,
        MAV_CMD_NAV_LAND,
    ),
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboCoupledCommandApproval:
    approved_at = _utc(now)
    command_ids = tuple(int(item) for item in approved_command_ids)
    payload = {
        "operator_approval_performed": bool(operator_approval_performed),
        "approved_command_ids": command_ids,
        "approved_at": approved_at.isoformat(),
    }
    return PX4GazeboCoupledCommandApproval(
        approval_id=_stable_id("px4_gazebo_coupled_command_approval", payload),
        operator_approval_performed=bool(operator_approval_performed),
        approved_command_ids=command_ids,
        approved_at=approved_at,
        metadata={
            **(metadata or {}),
            "issue": 331,
            "parent_epic": 307,
            "approval_scope": "simulation_only_px4_gazebo_coupled_delivery",
        },
    )


def build_px4_gazebo_coupled_command_allowlist(
    *,
    approval: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboCoupledCommandAllowlist:
    resolved_approval = _to_approval(approval)
    if resolved_approval.operator_approval_performed is not True:
        raise PX4GazeboCoupledDeliveryError(
            "coupled command allowlist requires operator_approval_performed=true"
        )
    allowed = {
        MAV_CMD_COMPONENT_ARM_DISARM: "MAV_CMD_COMPONENT_ARM_DISARM",
        MAV_CMD_NAV_TAKEOFF: "MAV_CMD_NAV_TAKEOFF",
        MAV_CMD_NAV_LAND: "MAV_CMD_NAV_LAND",
    }
    command_ids = tuple(
        command_id
        for command_id in allowed
        if command_id in resolved_approval.approved_command_ids
    )
    if set(command_ids) != set(allowed):
        raise PX4GazeboCoupledDeliveryError(
            "coupled command approval must include arm, takeoff, and land commands"
        )
    generated_at = _utc(now)
    payload = {
        "approval_id": resolved_approval.approval_id,
        "allowed_command_ids": command_ids,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboCoupledCommandAllowlist(
        allowlist_id=_stable_id("px4_gazebo_coupled_command_allowlist", payload),
        approval_ref=_approval_ref(resolved_approval),
        allowed_command_ids=command_ids,
        allowed_command_names=tuple(allowed[command_id] for command_id in command_ids),
        generated_at=generated_at,
        metadata={
            **(metadata or {}),
            "issue": 331,
            "parent_epic": 307,
            "allowlist_scope": "arm_takeoff_land_only",
        },
    )


def validate_px4_gazebo_coupled_command_dispatch(
    *,
    approval: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
    allowlist: PX4GazeboCoupledCommandAllowlist | Mapping[str, Any],
    command_id: int,
) -> None:
    resolved_approval = _to_approval(approval)
    resolved_allowlist = _to_allowlist(allowlist)
    if resolved_approval.operator_approval_performed is not True:
        raise PX4GazeboCoupledDeliveryError(
            "coupled MAVLink command requires operator approval"
        )
    if resolved_allowlist.approval_ref != _approval_ref(resolved_approval):
        raise PX4GazeboCoupledDeliveryError(
            "coupled command allowlist approval mismatch"
        )
    if int(command_id) not in resolved_allowlist.allowed_command_ids:
        raise PX4GazeboCoupledDeliveryError(
            f"coupled MAVLink command is not allowlisted: {command_id}"
        )


def build_px4_gazebo_coupled_command_diagnostics(
    *,
    failure_reason: PX4GazeboCoupledCommandFailureReason | str,
    command_id: int | None = None,
    command_name: str | None = None,
    target_system: int | None = 1,
    target_component: int | None = 1,
    endpoint_host: str | None = "127.0.0.1",
    approval: PX4GazeboCoupledCommandApproval | Mapping[str, Any] | None = None,
    allowlist: PX4GazeboCoupledCommandAllowlist | Mapping[str, Any] | None = None,
    command_allowlisted: bool = False,
    mavlink_command_sent_to_px4: bool = False,
    mavlink_response_received_from_px4: bool = False,
    px4_command_ack_result: str | None = None,
    blocked_reasons: Sequence[str] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboCoupledCommandDiagnostics:
    reason = (
        failure_reason
        if isinstance(failure_reason, PX4GazeboCoupledCommandFailureReason)
        else PX4GazeboCoupledCommandFailureReason(str(failure_reason))
    )
    resolved_approval = _to_approval(approval) if approval is not None else None
    resolved_allowlist = _to_allowlist(allowlist) if allowlist is not None else None
    observed_at = _utc(now)
    reasons = _ordered_tuple([reason.value, *(blocked_reasons or ())])
    payload = {
        "failure_reason": reason.value,
        "command_id": command_id,
        "target_system": target_system,
        "target_component": target_component,
        "endpoint_host": endpoint_host,
        "approval_ref": (
            _approval_ref(resolved_approval) if resolved_approval is not None else None
        ),
        "allowlist_ref": (
            _allowlist_ref(resolved_allowlist)
            if resolved_allowlist is not None
            else None
        ),
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboCoupledCommandDiagnostics(
        diagnostics_id=_stable_id("px4_gazebo_coupled_command_diagnostics", payload),
        failure_reason=reason,
        blocked_reasons=reasons,
        command_id=command_id,
        command_name=command_name,
        target_system=target_system,
        target_component=target_component,
        endpoint_host=endpoint_host,
        approval_ref=(
            _approval_ref(resolved_approval) if resolved_approval is not None else None
        ),
        allowlist_ref=(
            _allowlist_ref(resolved_allowlist)
            if resolved_allowlist is not None
            else None
        ),
        operator_approval_available=(
            resolved_approval is not None
            and resolved_approval.operator_approval_performed is True
        ),
        operator_approval_performed=(
            resolved_approval is not None
            and resolved_approval.operator_approval_performed is True
        ),
        allowlist_available=resolved_allowlist is not None,
        bounded_allowlist_enforced=resolved_allowlist is not None,
        command_allowlisted=command_allowlisted,
        mavlink_command_sent_to_px4=mavlink_command_sent_to_px4,
        mavlink_response_received_from_px4=mavlink_response_received_from_px4,
        px4_command_ack_result=px4_command_ack_result,
        observed_at=observed_at,
        metadata={
            **(metadata or {}),
            "issue": 332,
            "parent_epic": 307,
            "fail_closed": True,
            "no_retry_into_stronger_execution": True,
        },
    )


def run_px4_gazebo_coupled_command_diagnostics_task(
    task_id: str,
    *,
    diagnostics: PX4GazeboCoupledCommandDiagnostics | Mapping[str, Any],
    now: datetime | None = None,
    task_store_factory: Any | None = None,
) -> dict[str, Any]:
    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboCoupledDeliveryError(
            f"task {task_id} not found; cannot record coupled command diagnostics"
        )
    resolved = (
        diagnostics
        if isinstance(diagnostics, PX4GazeboCoupledCommandDiagnostics)
        else PX4GazeboCoupledCommandDiagnostics.model_validate(dict(diagnostics))
    )
    updated = store.update(
        task_id,
        status="blocked",
        artifacts={
            "px4_gazebo_coupled_command_diagnostics": resolved.model_dump(mode="json"),
        },
        ended_at=time.time(),
    )
    if updated is None:
        raise PX4GazeboCoupledDeliveryError(
            f"task {task_id} disappeared while recording coupled command diagnostics"
        )
    return updated


def build_px4_gazebo_coupled_delivery_phase_evidence(
    *,
    mission_phase: str,
    px4_container_image: str,
    approval: PX4GazeboCoupledCommandApproval | Mapping[str, Any],
    allowlist: PX4GazeboCoupledCommandAllowlist | Mapping[str, Any],
    mavlink_command_names: Sequence[str],
    mavlink_command_ids: Sequence[int],
    px4_acceptance_log_markers: Sequence[str],
    gazebo_pose_before_z_m: float,
    gazebo_pose_after_z_m: float,
    gazebo_pose_peak_z_m: float,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboCoupledDeliveryPhaseEvidence:
    phase = str(mission_phase)
    if phase not in COUPLED_DELIVERY_PHASES:
        raise PX4GazeboCoupledDeliveryError(f"unsupported delivery phase: {phase}")
    resolved_approval = _to_approval(approval)
    resolved_allowlist = _to_allowlist(allowlist)
    for command_id in mavlink_command_ids:
        validate_px4_gazebo_coupled_command_dispatch(
            approval=resolved_approval,
            allowlist=resolved_allowlist,
            command_id=int(command_id),
        )
    expected = _PHASE_EFFECTS[phase]
    observed_at = _utc(now)
    delta = abs(float(gazebo_pose_after_z_m) - float(gazebo_pose_before_z_m))
    peak_delta = abs(float(gazebo_pose_peak_z_m) - float(gazebo_pose_before_z_m))
    payload = {
        "mission_phase": phase,
        "px4_container_image": px4_container_image,
        "operator_approval_ref": _approval_ref(resolved_approval),
        "bounded_allowlist_ref": _allowlist_ref(resolved_allowlist),
        "mavlink_command_names": list(mavlink_command_names),
        "gazebo_pose_before_z_m": float(gazebo_pose_before_z_m),
        "gazebo_pose_after_z_m": float(gazebo_pose_after_z_m),
        "gazebo_pose_peak_z_m": float(gazebo_pose_peak_z_m),
        "observed_at": observed_at.isoformat(),
    }
    return PX4GazeboCoupledDeliveryPhaseEvidence(
        evidence_id=_stable_id("px4_gazebo_coupled_delivery_phase_evidence", payload),
        mission_phase=phase,  # type: ignore[arg-type]
        px4_container_image=px4_container_image,
        operator_approval_ref=_approval_ref(resolved_approval),
        bounded_allowlist_ref=_allowlist_ref(resolved_allowlist),
        mavlink_command_names=tuple(mavlink_command_names),
        mavlink_command_ids=tuple(mavlink_command_ids),
        px4_acceptance_log_markers=tuple(px4_acceptance_log_markers),
        phase_effect_kind=expected["effect"],
        phase_requires_z_motion=bool(expected["requires_z_motion"]),
        phase_requires_ground_contact=bool(expected["requires_ground_contact"]),
        gazebo_pose_before_z_m=float(gazebo_pose_before_z_m),
        gazebo_pose_after_z_m=float(gazebo_pose_after_z_m),
        gazebo_pose_peak_z_m=float(gazebo_pose_peak_z_m),
        gazebo_pose_motion_delta_z_m=max(delta, peak_delta),
        observed_at=observed_at,
        metadata={
            **(metadata or {}),
            "issue": 331,
            "parent_epic": 307,
            "bounded_mavlink_command_path": True,
            "actual_px4_gazebo_coupling_claimed": True,
        },
    )


def _to_phase_evidence(
    value: PX4GazeboCoupledDeliveryPhaseEvidence | Mapping[str, Any],
) -> PX4GazeboCoupledDeliveryPhaseEvidence:
    if isinstance(value, PX4GazeboCoupledDeliveryPhaseEvidence):
        return value
    return PX4GazeboCoupledDeliveryPhaseEvidence.model_validate(dict(value))


def build_px4_gazebo_coupled_delivery_runner_result(
    *,
    phase_evidence: Sequence[PX4GazeboCoupledDeliveryPhaseEvidence | Mapping[str, Any]],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboCoupledDeliveryRunnerResult:
    normalized = [_to_phase_evidence(item) for item in phase_evidence]
    if not normalized:
        raise PX4GazeboCoupledDeliveryError(
            "at least one coupled delivery phase evidence artifact is required"
        )
    phases = _ordered_tuple([item.mission_phase for item in normalized])
    missing = tuple(phase for phase in COUPLED_DELIVERY_PHASES if phase not in phases)
    final_status = (
        PX4GazeboCoupledDeliveryRunnerStatus.COMPLETED
        if not missing
        else PX4GazeboCoupledDeliveryRunnerStatus.BLOCKED
    )
    refs = tuple(
        f"px4_gazebo_coupled_delivery_phase_evidence:{item.evidence_id}"
        for item in normalized
    )
    completed_at = _utc(now)
    payload = {
        "phase_evidence_refs": refs,
        "observed_delivery_phases": phases,
        "missing_phases": missing,
        "final_status": final_status.value,
        "completion_basis": COUPLED_DELIVERY_COMPLETION_BASIS,
    }
    return PX4GazeboCoupledDeliveryRunnerResult(
        runner_result_id=_stable_id(
            "px4_gazebo_coupled_delivery_runner_result", payload
        ),
        final_status=final_status,
        phase_evidence_refs=refs,
        observed_delivery_phases=phases,
        missing_phases=missing,
        completed_at=completed_at,
        metadata={
            **(metadata or {}),
            "issue": 331,
            "parent_epic": 307,
            "required_phases": list(COUPLED_DELIVERY_PHASES),
        },
    )


def run_px4_gazebo_coupled_delivery_task(
    task_id: str,
    *,
    phase_evidence: Sequence[PX4GazeboCoupledDeliveryPhaseEvidence | Mapping[str, Any]],
    now: datetime | None = None,
    task_store_factory: Any | None = None,
) -> dict[str, Any]:
    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboCoupledDeliveryError(
            f"task {task_id} not found; cannot run coupled PX4/Gazebo delivery"
        )
    runner_result = build_px4_gazebo_coupled_delivery_runner_result(
        phase_evidence=phase_evidence,
        now=now,
    )
    normalized = [_to_phase_evidence(item) for item in phase_evidence]
    updated = store.update(
        task_id,
        status=runner_result.final_status.value,
        artifacts={
            "px4_gazebo_coupled_delivery_phase_evidence": [
                item.model_dump(mode="json") for item in normalized
            ],
            "px4_gazebo_coupled_delivery_runner_result": runner_result.model_dump(
                mode="json"
            ),
        },
        ended_at=time.time(),
    )
    if updated is None:
        raise PX4GazeboCoupledDeliveryError(
            f"task {task_id} disappeared while running coupled PX4/Gazebo delivery"
        )
    return updated


__all__ = [
    "COUPLED_DELIVERY_COMPLETION_BASIS",
    "COUPLED_DELIVERY_PHASES",
    "MAV_CMD_COMPONENT_ARM_DISARM",
    "MAV_CMD_NAV_LAND",
    "MAV_CMD_NAV_TAKEOFF",
    "PX4_GAZEBO_COUPLED_COMMAND_ALLOWLIST_SCHEMA_VERSION",
    "PX4_GAZEBO_COUPLED_COMMAND_APPROVAL_SCHEMA_VERSION",
    "PX4_GAZEBO_COUPLED_COMMAND_DIAGNOSTICS_SCHEMA_VERSION",
    "PX4_GAZEBO_COUPLED_DELIVERY_PHASE_EVIDENCE_SCHEMA_VERSION",
    "PX4_GAZEBO_COUPLED_DELIVERY_RUNNER_RESULT_SCHEMA_VERSION",
    "PX4GazeboCoupledCommandAllowlist",
    "PX4GazeboCoupledCommandApproval",
    "PX4GazeboCoupledCommandDiagnostics",
    "PX4GazeboCoupledCommandFailureReason",
    "PX4GazeboCoupledDeliveryError",
    "PX4GazeboCoupledDeliveryPhaseEvidence",
    "PX4GazeboCoupledDeliveryRunnerResult",
    "PX4GazeboCoupledDeliveryRunnerStatus",
    "PX4GazeboCoupledPhaseEffectKind",
    "build_px4_gazebo_coupled_command_allowlist",
    "build_px4_gazebo_coupled_command_approval",
    "build_px4_gazebo_coupled_command_diagnostics",
    "build_px4_gazebo_coupled_delivery_phase_evidence",
    "build_px4_gazebo_coupled_delivery_runner_result",
    "run_px4_gazebo_coupled_command_diagnostics_task",
    "run_px4_gazebo_coupled_delivery_task",
    "validate_px4_gazebo_coupled_command_dispatch",
]

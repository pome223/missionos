"""Bounded Unitree MuJoCo hardware adapter contract slice.

This module adds a Unitree SDK2/MuJoCo-facing adapter surface without importing
Unitree SDKs, starting MuJoCo, or claiming physical execution. A future
simulation runner can provide a ``UnitreeSimClient`` implementation; this
adapter owns the MissionOS approval, bounding, and evidence boundary around it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import math
import os
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.hardware_adapter_contract import (
    HardwareAckStatus,
    HardwareActionKind,
    HardwareAdapterCapabilities,
    HardwareAdapterEvidence,
    HardwareAdapterKind,
    HardwareAdapterPreflightResult,
    HardwareDispatchCandidate,
    HardwareDispatchStatus,
    HardwareExecutionMode,
    HardwareOperatorApproval,
    HardwarePreflightStatus,
    HardwareVehicleClass,
)


UNITREE_MUJOCO_HARDWARE_ADAPTER_ID = "unitree_sdk2_mujoco_adapter.v1"
UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV = "RUN_MISSIONOS_UNITREE_MUJOCO_SMOKE"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_UNITREE_ALLOWED_ACTIONS = (
    HardwareActionKind.SAFE_STOP,
    HardwareActionKind.HOLD,
    HardwareActionKind.BOUNDED_LOCAL_MOVE,
)
_UNITREE_BLOCKED_ACTIONS = (
    HardwareActionKind.RAW_MOTOR,
    HardwareActionKind.RAW_VELOCITY,
    HardwareActionKind.SPECIAL_MOTION,
    HardwareActionKind.RAW_MAVLINK,
    HardwareActionKind.PX4_ARM_DISARM_BENCH,
    HardwareActionKind.PX4_MISSION_UPLOAD,
    HardwareActionKind.PX4_START_MISSION,
    HardwareActionKind.PX4_OFFBOARD_SETPOINT,
    HardwareActionKind.LAND,
    HardwareActionKind.RETURN_TO_LAUNCH,
)


class UnitreeBoundedLocalMove(BaseModel):
    """Small local move candidate for Unitree MuJoCo simulation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    forward_m: float = 0.0
    lateral_m: float = 0.0
    yaw_rad: float = 0.0
    duration_s: float = Field(default=2.0, ge=0.0)
    max_speed_mps: float = Field(default=0.3, ge=0.0)
    max_distance_m: float = Field(default=0.5, ge=0.0)
    label: str | None = None


class UnitreeSimClient(Protocol):
    """Client boundary supplied by a future Unitree SDK2/MuJoCo runner."""

    def send_bounded_local_move(
        self,
        move: UnitreeBoundedLocalMove,
    ) -> Mapping[str, Any]: ...

    def hold(self) -> Mapping[str, Any]: ...

    def safe_stop(self) -> Mapping[str, Any]: ...

    def read_state(self) -> Mapping[str, Any]: ...

    def read_progress(self) -> Mapping[str, Any]: ...


class UnitreeHardwareAdapterConfig(BaseModel):
    """Configuration for one bounded Unitree MuJoCo adapter attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    missionos_action_ref: str
    action_kind: HardwareActionKind = HardwareActionKind.BOUNDED_LOCAL_MOVE
    local_move: UnitreeBoundedLocalMove | None = None
    execution_mode: HardwareExecutionMode = HardwareExecutionMode.SIM
    operator_approval_ref: str | None = None
    approval_actor: str | None = None
    approval_timestamp: datetime | None = None
    opt_in: bool = False
    telemetry_fresh: bool = True
    heartbeat_alive: bool = True
    geofence_satisfied: bool = True
    operating_volume_satisfied: bool = True
    max_speed_mps: float = Field(default=0.3, ge=0.0)
    max_distance_m: float = Field(default=0.5, ge=0.0)
    raw_logs_ref: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _dedupe(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    deduped: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in deduped:
            deduped.append(text)
    return tuple(deduped)


def _move_distance_m(move: UnitreeBoundedLocalMove | None) -> float:
    if move is None:
        return 0.0
    return math.hypot(move.forward_m, move.lateral_m)


def _move_parameters(move: UnitreeBoundedLocalMove | None) -> dict[str, Any]:
    if move is None:
        return {}
    return move.model_dump(mode="json")


def _ack_status(payload: Mapping[str, Any]) -> HardwareAckStatus:
    raw = str(payload.get("ack_status") or payload.get("status") or "").lower()
    if raw in {"accepted", "succeeded", "active"} or payload.get("accepted") is True:
        return HardwareAckStatus.ACCEPTED
    if raw in {"rejected", "aborted", "failed"} or payload.get("rejected") is True:
        return HardwareAckStatus.REJECTED
    if raw == "timeout" or payload.get("timeout") is True:
        return HardwareAckStatus.TIMEOUT
    return HardwareAckStatus.ACCEPTED if payload else HardwareAckStatus.NOT_REQUESTED


def _progress_observed(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("unitree_status") or payload.get("status") or "").lower()
    return (
        payload.get("runtime_progress_observed") is True
        or payload.get("progress_observed") is True
        or payload.get("move_active") is True
        or status in {"active", "executing", "succeeded", "move_succeeded"}
    )


def _completion_observed(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("unitree_status") or payload.get("status") or "").lower()
    return (
        payload.get("completion_observed") is True
        or payload.get("move_completed") is True
        or status in {"succeeded", "move_succeeded"}
    )


def build_unitree_hardware_adapter_capabilities(
    *,
    execution_mode: HardwareExecutionMode = HardwareExecutionMode.SIM,
) -> HardwareAdapterCapabilities:
    """Return bounded Unitree SDK2/MuJoCo adapter capabilities."""

    return HardwareAdapterCapabilities(
        adapter_id=UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.UNITREE_SDK2,
        vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
        execution_mode=execution_mode,
        allowed_actions=_UNITREE_ALLOWED_ACTIONS,
        blocked_actions=_UNITREE_BLOCKED_ACTIONS,
        requires_operator_approval=True,
        requires_fresh_telemetry=True,
        requires_physical_estop=False,
        requires_geofence=True,
        max_speed_mps=0.3,
        max_altitude_m=0.0,
        max_distance_m=0.5,
        supports_abort=True,
        supports_return=False,
        supports_hold=True,
    )


def build_unitree_hardware_adapter_preflight(
    *,
    config: UnitreeHardwareAdapterConfig,
    client_present: bool,
) -> HardwareAdapterPreflightResult:
    """Build a fail-closed preflight artifact for the Unitree adapter."""

    reasons: list[str] = []
    if config.action_kind not in _UNITREE_ALLOWED_ACTIONS:
        reasons.append("action_not_allowed_by_adapter_capabilities")
    if config.action_kind in _UNITREE_BLOCKED_ACTIONS:
        reasons.append("action_blocked_by_adapter_capabilities")
    if (
        config.action_kind is HardwareActionKind.BOUNDED_LOCAL_MOVE
        and config.local_move is None
    ):
        reasons.append("unitree_bounded_local_move_missing")
    if not client_present:
        reasons.append("unitree_sim_client_missing")
    if config.execution_mode is not HardwareExecutionMode.SIM:
        reasons.append("unitree_mujoco_adapter_requires_sim_execution_mode")
    if not (config.opt_in and _truthy_env(UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV)):
        reasons.append("unitree_mujoco_runtime_opt_in_not_enabled")
    if not config.telemetry_fresh:
        reasons.append("telemetry_stale")
    if not config.heartbeat_alive:
        reasons.append("heartbeat_lost")
    if not config.geofence_satisfied:
        reasons.append("geofence_violation")
    if not config.operating_volume_satisfied:
        reasons.append("operating_volume_violation")
    if config.local_move is not None:
        if _move_distance_m(config.local_move) > config.max_distance_m:
            reasons.append("operating_volume_violation")
        if config.local_move.max_distance_m > config.max_distance_m:
            reasons.append("move_distance_limit_exceeds_adapter_capability")
        if config.local_move.max_speed_mps > config.max_speed_mps:
            reasons.append("move_speed_limit_exceeds_adapter_capability")

    blocking_reasons = _dedupe(reasons)
    return HardwareAdapterPreflightResult(
        adapter_id=UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
        adapter_action_kind=config.action_kind,
        preflight_status=(
            HardwarePreflightStatus.BLOCKED
            if blocking_reasons
            else HardwarePreflightStatus.PASSED
        ),
        blocking_reasons=blocking_reasons,
        telemetry_fresh=config.telemetry_fresh,
        heartbeat_alive=config.heartbeat_alive,
        geofence_satisfied=config.geofence_satisfied,
        operating_volume_satisfied=config.operating_volume_satisfied,
    )


def build_unitree_hardware_dispatch_candidate(
    *,
    config: UnitreeHardwareAdapterConfig,
    preflight: HardwareAdapterPreflightResult,
) -> HardwareDispatchCandidate:
    """Build the Unitree dispatch candidate without sending to MuJoCo."""

    return HardwareDispatchCandidate(
        adapter_id=UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
        missionos_action_ref=config.missionos_action_ref,
        adapter_action_kind=config.action_kind,
        adapter_parameters=_move_parameters(config.local_move),
        capability_match=config.action_kind in _UNITREE_ALLOWED_ACTIONS,
        safety_constraints_applied=(
            "operator_approval_required",
            "fresh_telemetry_required",
            "geofence_required",
            "bounded_local_move_required",
            "max_speed_0_3_mps",
            "max_distance_0_5_m",
            "raw_motor_forbidden",
            "raw_velocity_forbidden",
            "special_motion_forbidden",
        ),
        preflight_status=preflight.preflight_status,
        blocking_reasons=preflight.blocking_reasons,
        telemetry_fresh=preflight.telemetry_fresh,
    )


def build_unitree_hardware_operator_approval(
    *,
    config: UnitreeHardwareAdapterConfig,
) -> HardwareOperatorApproval:
    """Build scoped operator approval for one bounded Unitree action."""

    if not (
        config.operator_approval_ref
        and config.approval_actor
        and config.approval_timestamp
    ):
        raise ValueError("Unitree adapter approval requires ref, actor, and timestamp")
    return HardwareOperatorApproval(
        operator_approval_ref=config.operator_approval_ref,
        approval_actor=config.approval_actor,
        approval_timestamp=config.approval_timestamp,
        approved_action_ref=config.missionos_action_ref,
        approved_action_kind=config.action_kind,
    )


def build_blocked_unitree_hardware_adapter_evidence(
    *,
    config: UnitreeHardwareAdapterConfig,
    blocking_reasons: tuple[str, ...],
) -> HardwareAdapterEvidence:
    """Build blocked Unitree adapter evidence without dispatch."""

    return HardwareAdapterEvidence(
        adapter_id=UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.UNITREE_SDK2,
        vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
        execution_mode=config.execution_mode,
        missionos_action_ref=config.missionos_action_ref,
        adapter_action_kind=config.action_kind,
        operator_approval_ref=config.operator_approval_ref,
        preflight_status=HardwarePreflightStatus.BLOCKED,
        dispatch_status=HardwareDispatchStatus.BLOCKED,
        dispatch_request_sent=False,
        command_ack_observed=False,
        ack_status=HardwareAckStatus.NOT_REQUESTED,
        runtime_progress_observed=False,
        completion_claimed=False,
        completion_scope="none",
        physical_execution_invoked=False,
        safe_stop_requested=False,
        abort_requested=False,
        telemetry_fresh=config.telemetry_fresh,
        blocking_reasons=blocking_reasons,
        unproven_claims=(
            "dispatch_not_sent",
            "command_ack_not_observed",
            "adapter_action_completion_not_claimed",
            "physical_execution_not_invoked",
            "mission_delivery_completion_not_claimed",
            "raw_motor_not_invoked",
            "raw_velocity_not_invoked",
            "special_motion_not_invoked",
        ),
        raw_logs_ref=config.raw_logs_ref,
    )


def build_unitree_hardware_adapter_evidence(
    *,
    config: UnitreeHardwareAdapterConfig,
    dispatch_result: Mapping[str, Any],
    state_result: Mapping[str, Any],
    progress_result: Mapping[str, Any],
) -> HardwareAdapterEvidence:
    """Project a Unitree sim client result into MissionOS adapter evidence."""

    del state_result
    ack_status = _ack_status(dispatch_result)
    command_ack_observed = ack_status in {
        HardwareAckStatus.ACCEPTED,
        HardwareAckStatus.REJECTED,
    }
    command_sent = ack_status is not HardwareAckStatus.NOT_REQUESTED
    progress_observed = _progress_observed(progress_result)
    completion_observed = _completion_observed(progress_result)
    completion_claimed = (
        ack_status is HardwareAckStatus.ACCEPTED
        and progress_observed
        and completion_observed
    )

    unproven_claims: list[str] = []
    if ack_status is HardwareAckStatus.ACCEPTED and not progress_observed:
        unproven_claims.append("ack_is_not_success")
    unproven_claims.append("simulator_evidence_not_physical")
    unproven_claims.append("physical_execution_not_invoked")
    if completion_claimed:
        unproven_claims.append("sim_action_completion_not_physical")
    unproven_claims.append("mission_delivery_completion_not_claimed")
    unproven_claims.append("raw_motor_not_invoked")
    unproven_claims.append("raw_velocity_not_invoked")
    unproven_claims.append("special_motion_not_invoked")

    return HardwareAdapterEvidence(
        adapter_id=UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.UNITREE_SDK2,
        vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
        execution_mode=config.execution_mode,
        missionos_action_ref=config.missionos_action_ref,
        adapter_action_kind=config.action_kind,
        operator_approval_ref=config.operator_approval_ref,
        preflight_status=(
            HardwarePreflightStatus.PASSED
            if command_sent
            else HardwarePreflightStatus.BLOCKED
        ),
        dispatch_status=(
            HardwareDispatchStatus.SENT
            if command_sent
            else HardwareDispatchStatus.BLOCKED
        ),
        dispatch_request_sent=command_sent,
        command_ack_observed=command_ack_observed,
        ack_source=(
            str(dispatch_result.get("ack_source") or "unitree_mujoco_sim_client")
            if command_ack_observed
            else None
        ),
        ack_status=ack_status,
        runtime_progress_observed=progress_observed,
        completion_claimed=completion_claimed,
        completion_scope="sim_action" if completion_claimed else "none",
        physical_execution_invoked=False,
        safe_stop_requested=False,
        abort_requested=False,
        telemetry_fresh=config.telemetry_fresh,
        blocking_reasons=_dedupe(tuple(dispatch_result.get("blocking_reasons") or ())),
        unproven_claims=tuple(unproven_claims),
        raw_logs_ref=config.raw_logs_ref or dispatch_result.get("raw_logs_ref"),
    )


def build_unitree_safe_stop_hardware_adapter_evidence(
    *,
    config: UnitreeHardwareAdapterConfig,
    abort_requested: bool = False,
) -> HardwareAdapterEvidence:
    """Build safe-stop request evidence for the Unitree adapter."""

    return HardwareAdapterEvidence(
        adapter_id=UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.UNITREE_SDK2,
        vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
        execution_mode=config.execution_mode,
        missionos_action_ref=config.missionos_action_ref,
        adapter_action_kind=HardwareActionKind.SAFE_STOP,
        operator_approval_ref=config.operator_approval_ref,
        preflight_status=HardwarePreflightStatus.BLOCKED,
        dispatch_status=HardwareDispatchStatus.SAFE_STOP_REQUESTED,
        dispatch_request_sent=False,
        command_ack_observed=False,
        ack_status=HardwareAckStatus.NOT_REQUESTED,
        runtime_progress_observed=False,
        completion_claimed=False,
        completion_scope="none",
        physical_execution_invoked=False,
        safe_stop_requested=True,
        abort_requested=abort_requested,
        telemetry_fresh=config.telemetry_fresh,
        blocking_reasons=(
            "operator_abort_requested" if abort_requested else "safe_stop_requested",
        ),
        unproven_claims=(
            "dispatch_preempted_by_safe_stop",
            "command_ack_not_observed",
            "adapter_action_completion_not_claimed",
            "physical_execution_not_invoked",
            "raw_motor_not_invoked",
            "raw_velocity_not_invoked",
            "special_motion_not_invoked",
        ),
        raw_logs_ref=config.raw_logs_ref,
    )


class UnitreeHardwareAdapter:
    """Bounded Unitree SDK2/MuJoCo adapter wrapper around an injected client."""

    def __init__(
        self,
        *,
        config: UnitreeHardwareAdapterConfig,
        client: UnitreeSimClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._evidence: list[HardwareAdapterEvidence] = []

    def capabilities(self) -> HardwareAdapterCapabilities:
        return build_unitree_hardware_adapter_capabilities(
            execution_mode=self._config.execution_mode,
        )

    def preflight_check(self) -> HardwareAdapterPreflightResult:
        return build_unitree_hardware_adapter_preflight(
            config=self._config,
            client_present=self._client is not None,
        )

    def propose_dispatch(self) -> HardwareDispatchCandidate:
        return build_unitree_hardware_dispatch_candidate(
            config=self._config,
            preflight=self.preflight_check(),
        )

    def require_operator_approval(self) -> HardwareOperatorApproval:
        return build_unitree_hardware_operator_approval(config=self._config)

    def dispatch_approved_action(self) -> HardwareAdapterEvidence:
        preflight = self.preflight_check()
        blocking_reasons = list(preflight.blocking_reasons)
        if not self._config.operator_approval_ref:
            blocking_reasons.append("operator_approval_missing")
        if preflight.preflight_status is HardwarePreflightStatus.BLOCKED or blocking_reasons:
            evidence = build_blocked_unitree_hardware_adapter_evidence(
                config=self._config,
                blocking_reasons=_dedupe(blocking_reasons),
            )
            self._evidence.append(evidence)
            return evidence

        if self._client is None:
            evidence = build_blocked_unitree_hardware_adapter_evidence(
                config=self._config,
                blocking_reasons=("unitree_sim_client_missing",),
            )
            self._evidence.append(evidence)
            return evidence

        if self._config.action_kind is HardwareActionKind.BOUNDED_LOCAL_MOVE:
            dispatch_result = self._client.send_bounded_local_move(
                self._config.local_move  # type: ignore[arg-type]
            )
        elif self._config.action_kind is HardwareActionKind.HOLD:
            dispatch_result = self._client.hold()
        elif self._config.action_kind is HardwareActionKind.SAFE_STOP:
            dispatch_result = self._client.safe_stop()
        else:
            dispatch_result = {
                "ack_status": "rejected",
                "blocking_reasons": ("action_not_dispatchable_by_unitree_adapter",),
            }

        state_result = self._client.read_state()
        progress_result = self._client.read_progress()
        evidence = build_unitree_hardware_adapter_evidence(
            config=self._config,
            dispatch_result=dispatch_result,
            state_result=state_result,
            progress_result=progress_result,
        )
        self._evidence.append(evidence)
        return evidence

    def observe_ack(self) -> HardwareAckStatus:
        if not self._evidence:
            return HardwareAckStatus.NOT_REQUESTED
        return self._evidence[-1].ack_status

    def observe_state(self) -> dict[str, Any]:
        if self._client is None:
            return {}
        return dict(self._client.read_state())

    def observe_progress(self) -> bool:
        if self._client is None:
            return False
        return _progress_observed(self._client.read_progress())

    def abort_or_safe_stop(self) -> HardwareAdapterEvidence:
        evidence = build_unitree_safe_stop_hardware_adapter_evidence(
            config=self._config,
            abort_requested=True,
        )
        self._evidence.append(evidence)
        return evidence

    def collect_evidence(self) -> tuple[HardwareAdapterEvidence, ...]:
        return tuple(self._evidence)


__all__ = [
    "UNITREE_MUJOCO_HARDWARE_ADAPTER_ID",
    "UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV",
    "UnitreeBoundedLocalMove",
    "UnitreeHardwareAdapter",
    "UnitreeHardwareAdapterConfig",
    "UnitreeSimClient",
    "build_blocked_unitree_hardware_adapter_evidence",
    "build_unitree_hardware_adapter_capabilities",
    "build_unitree_hardware_adapter_evidence",
    "build_unitree_hardware_adapter_preflight",
    "build_unitree_hardware_dispatch_candidate",
    "build_unitree_hardware_operator_approval",
    "build_unitree_safe_stop_hardware_adapter_evidence",
]

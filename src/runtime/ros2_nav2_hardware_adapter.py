"""Bounded ROS2/Nav2 hardware adapter contract slice.

This module adds the first ground-robot adapter surface for Issue #1 without
importing ROS2, opening a network socket, or claiming physical execution. A
real ROS2/Nav2 integration can provide a ``Nav2DispatchClient`` implementation;
this adapter owns the MissionOS claim boundary around that client.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import math
import os
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.hardware_adapter_contract import (
    HardwareAckStatus,
    HardwareActionKind,
    HardwareAdapterCapabilities,
    HardwareAdapterEvidence,
    HardwareAdapterPreflightResult,
    HardwareDispatchCandidate,
    HardwareDispatchStatus,
    HardwareExecutionMode,
    HardwareOperatorApproval,
    HardwarePreflightStatus,
    HardwareVehicleClass,
    HardwareAdapterKind,
)


ROS2_NAV2_HARDWARE_ADAPTER_ID = "ros2_nav2_ground_robot_adapter.v1"
ROS2_NAV2_HARDWARE_ADAPTER_OPT_IN_ENV = "RUN_MISSIONOS_ROS2_NAV2_HARDWARE_ADAPTER"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_NAV2_ALLOWED_ACTIONS = (
    HardwareActionKind.NAV2_GOAL_POSE,
    HardwareActionKind.NAV2_CANCEL_GOAL,
    HardwareActionKind.HOLD,
    HardwareActionKind.SAFE_STOP,
)
_PHYSICAL_NAV2_MODES = {
    HardwareExecutionMode.BENCH,
    HardwareExecutionMode.CAGE,
    HardwareExecutionMode.FIELD,
}


class Nav2GoalPose(BaseModel):
    """Bounded local Nav2 goal pose candidate.

    Coordinates are adapter-frame values. The adapter does not infer that a
    goal was reached; it waits for the injected Nav2 client to report progress.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    frame_id: str = "map"
    x_m: float
    y_m: float
    yaw_rad: float = 0.0
    tolerance_m: float = Field(default=0.25, ge=0.0)
    max_speed_mps: float = Field(default=0.25, ge=0.0)
    max_distance_m: float = Field(default=3.0, ge=0.0)
    label: str | None = None


class Nav2DispatchClient(Protocol):
    """Client boundary supplied by a future ROS2/Nav2 integration."""

    def send_goal_pose(self, goal: Nav2GoalPose) -> Mapping[str, Any]: ...

    def cancel_goal(self) -> Mapping[str, Any]: ...

    def read_state(self) -> Mapping[str, Any]: ...

    def read_progress(self) -> Mapping[str, Any]: ...


class Ros2Nav2HardwareAdapterConfig(BaseModel):
    """Configuration for one bounded Nav2 adapter attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    missionos_action_ref: str
    action_kind: HardwareActionKind = HardwareActionKind.NAV2_GOAL_POSE
    goal_pose: Nav2GoalPose | None = None
    execution_mode: HardwareExecutionMode = HardwareExecutionMode.LOOPBACK
    operator_approval_ref: str | None = None
    authorization_source: Literal["individual_human_approval", "human_approved_policy"] = "individual_human_approval"
    preparation_ref: str | None = None
    preparation_sha256: str | None = None
    approval_actor: str | None = None
    approval_timestamp: datetime | None = None
    opt_in: bool = False
    telemetry_fresh: bool = True
    heartbeat_alive: bool = True
    geofence_satisfied: bool = True
    operating_volume_satisfied: bool = True
    physical_estop_available: bool = False
    max_distance_m: float = Field(default=3.0, ge=0.0)
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


def _goal_distance_m(goal: Nav2GoalPose | None) -> float:
    if goal is None:
        return 0.0
    return math.hypot(goal.x_m, goal.y_m)


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
    status = str(payload.get("nav2_status") or payload.get("status") or "").lower()
    return (
        payload.get("runtime_progress_observed") is True
        or payload.get("progress_observed") is True
        or payload.get("goal_active") is True
        or status in {"active", "executing", "succeeded", "goal_succeeded"}
    )


def _completion_observed(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("nav2_status") or payload.get("status") or "").lower()
    return (
        payload.get("completion_observed") is True
        or payload.get("goal_reached") is True
        or status in {"succeeded", "goal_succeeded"}
    )


def _robot_motion_observed(*payloads: Mapping[str, Any]) -> bool:
    return any(payload.get("robot_motion_observed") is True for payload in payloads)


def _runtime_state_observed(payload: Mapping[str, Any]) -> bool:
    return any(
        payload.get(key) is True
        for key in (
            "state_observed",
            "pose_observed",
            "localization_observed",
            "robot_motion_observed",
        )
    )


def _goal_parameters(goal: Nav2GoalPose | None) -> dict[str, Any]:
    if goal is None:
        return {}
    return goal.model_dump(mode="json")


def build_ros2_nav2_hardware_adapter_capabilities(
    *,
    execution_mode: HardwareExecutionMode = HardwareExecutionMode.LOOPBACK,
) -> HardwareAdapterCapabilities:
    """Return bounded Nav2 ground-robot capabilities."""

    return HardwareAdapterCapabilities(
        adapter_id=ROS2_NAV2_HARDWARE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.ROS2_NAV2,
        vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
        execution_mode=execution_mode,
        allowed_actions=_NAV2_ALLOWED_ACTIONS,
        blocked_actions=(
            HardwareActionKind.RAW_MAVLINK,
            HardwareActionKind.PX4_ARM_DISARM_BENCH,
            HardwareActionKind.PX4_MISSION_UPLOAD,
            HardwareActionKind.PX4_START_MISSION,
            HardwareActionKind.PX4_OFFBOARD_SETPOINT,
            HardwareActionKind.LAND,
            HardwareActionKind.RETURN_TO_LAUNCH,
        ),
        requires_operator_approval=True,
        requires_fresh_telemetry=True,
        requires_physical_estop=execution_mode in _PHYSICAL_NAV2_MODES,
        requires_geofence=True,
        max_speed_mps=0.25,
        max_altitude_m=0.0,
        max_distance_m=3.0,
        supports_abort=True,
        supports_return=False,
        supports_hold=True,
    )


def build_ros2_nav2_hardware_adapter_preflight(
    *,
    config: Ros2Nav2HardwareAdapterConfig,
    client_present: bool,
) -> HardwareAdapterPreflightResult:
    """Build a fail-closed preflight artifact for the Nav2 adapter."""

    reasons: list[str] = []
    if config.action_kind not in _NAV2_ALLOWED_ACTIONS:
        reasons.append("action_not_allowed_by_adapter_capabilities")
    if config.action_kind is HardwareActionKind.NAV2_GOAL_POSE and config.goal_pose is None:
        reasons.append("nav2_goal_pose_missing")
    if not client_present:
        reasons.append("ros2_nav2_client_missing")
    if not config.telemetry_fresh:
        reasons.append("telemetry_stale")
    if not config.heartbeat_alive:
        reasons.append("heartbeat_lost")
    if not config.geofence_satisfied:
        reasons.append("geofence_violation")
    if not config.operating_volume_satisfied:
        reasons.append("operating_volume_violation")
    if _goal_distance_m(config.goal_pose) > config.max_distance_m:
        reasons.append("operating_volume_violation")
    if config.execution_mode in _PHYSICAL_NAV2_MODES:
        if not config.physical_estop_available:
            reasons.append("physical_estop_missing")
        if not (config.opt_in and _truthy_env(ROS2_NAV2_HARDWARE_ADAPTER_OPT_IN_ENV)):
            reasons.append("ros2_nav2_runtime_opt_in_not_enabled")
    if config.execution_mode is HardwareExecutionMode.FIELD:
        reasons.append("field_execution_not_supported_by_nav2_adapter_v1")
    if config.execution_mode is HardwareExecutionMode.HITL:
        reasons.append("hitl_execution_not_supported_by_nav2_adapter_v1")

    blocking_reasons = _dedupe(reasons)
    return HardwareAdapterPreflightResult(
        adapter_id=ROS2_NAV2_HARDWARE_ADAPTER_ID,
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


def build_ros2_nav2_hardware_dispatch_candidate(
    *,
    config: Ros2Nav2HardwareAdapterConfig,
    preflight: HardwareAdapterPreflightResult,
) -> HardwareDispatchCandidate:
    """Build the Nav2 dispatch candidate without sending to ROS2."""

    return HardwareDispatchCandidate(
        adapter_id=ROS2_NAV2_HARDWARE_ADAPTER_ID,
        missionos_action_ref=config.missionos_action_ref,
        adapter_action_kind=config.action_kind,
        adapter_parameters=_goal_parameters(config.goal_pose),
        capability_match=config.action_kind in _NAV2_ALLOWED_ACTIONS,
        safety_constraints_applied=(
            "operator_approval_required",
            "fresh_telemetry_required",
            "geofence_required",
            "bounded_goal_distance_required",
            "raw_velocity_control_forbidden",
            "raw_ros_topic_publication_forbidden",
        ),
        preflight_status=preflight.preflight_status,
        blocking_reasons=preflight.blocking_reasons,
        telemetry_fresh=preflight.telemetry_fresh,
    )


def build_ros2_nav2_hardware_operator_approval(
    *,
    config: Ros2Nav2HardwareAdapterConfig,
) -> HardwareOperatorApproval:
    """Build scoped operator approval for one bounded Nav2 action."""

    if config.authorization_source == "human_approved_policy":
        raise ValueError("policy_authority_is_not_individual_operator_approval")
    if not (
        config.operator_approval_ref
        and config.approval_actor
        and config.approval_timestamp
    ):
        raise ValueError("Nav2 adapter approval requires ref, actor, and timestamp")
    return HardwareOperatorApproval(
        operator_approval_ref=config.operator_approval_ref,
        approved_adapter_id=ROS2_NAV2_HARDWARE_ADAPTER_ID,
        approval_actor=config.approval_actor,
        approval_timestamp=config.approval_timestamp,
        approved_action_ref=config.missionos_action_ref,
        approved_action_kind=config.action_kind,
    )


def build_blocked_ros2_nav2_hardware_adapter_evidence(
    *,
    config: Ros2Nav2HardwareAdapterConfig,
    blocking_reasons: tuple[str, ...],
) -> HardwareAdapterEvidence:
    """Build blocked Nav2 adapter evidence without dispatch."""

    return HardwareAdapterEvidence(
        adapter_id=ROS2_NAV2_HARDWARE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.ROS2_NAV2,
        vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
        execution_mode=config.execution_mode,
        missionos_action_ref=config.missionos_action_ref,
        adapter_action_kind=config.action_kind,
        operator_approval_ref=config.operator_approval_ref,
        authorization_source=config.authorization_source,
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
        ),
        raw_logs_ref=config.raw_logs_ref,
    )


def build_ros2_nav2_hardware_adapter_evidence(
    *,
    config: Ros2Nav2HardwareAdapterConfig,
    dispatch_result: Mapping[str, Any],
    state_result: Mapping[str, Any],
    progress_result: Mapping[str, Any],
) -> HardwareAdapterEvidence:
    """Project a Nav2 client result into MissionOS adapter evidence."""

    ack_status = _ack_status(dispatch_result)
    command_ack_observed = ack_status in {
        HardwareAckStatus.ACCEPTED,
        HardwareAckStatus.REJECTED,
    }
    command_sent = ack_status is not HardwareAckStatus.NOT_REQUESTED
    progress_observed = _progress_observed(progress_result)
    nav2_completion_reported = _completion_observed(progress_result)
    robot_motion_observed = _robot_motion_observed(state_result, progress_result)
    goal_already_satisfied_observed = (
        progress_result.get("goal_already_satisfied_observed") is True
        and state_result.get("goal_already_satisfied_observed") is True
        and progress_result.get("completion_basis") == "already_at_goal_pose"
        and state_result.get("completion_basis") == "already_at_goal_pose"
    )
    completion_observed = nav2_completion_reported and (
        config.action_kind is not HardwareActionKind.NAV2_GOAL_POSE
        or robot_motion_observed
        or goal_already_satisfied_observed
    )
    completion_claimed = (
        ack_status is HardwareAckStatus.ACCEPTED
        and progress_observed
        and completion_observed
    )
    physical_execution_invoked = False

    unproven_claims: list[str] = []
    if ack_status is HardwareAckStatus.ACCEPTED and not progress_observed:
        unproven_claims.append("ack_is_not_success")
    if config.execution_mode is HardwareExecutionMode.SIM:
        unproven_claims.append("simulator_evidence_not_physical")
    if not physical_execution_invoked:
        unproven_claims.append("physical_execution_not_invoked")
    if completion_claimed and config.execution_mode is HardwareExecutionMode.LOOPBACK:
        unproven_claims.append("loopback_action_completion_not_physical")
    if completion_claimed and config.execution_mode is HardwareExecutionMode.SIM:
        unproven_claims.append("sim_action_completion_not_physical")
    if (
        completion_claimed
        and str(progress_result.get("nav2_status") or "").lower()
        == "position_tolerance_reached"
    ):
        unproven_claims.append("nav2_goal_status_succeeded_not_observed")
    if (
        nav2_completion_reported
        and config.action_kind is HardwareActionKind.NAV2_GOAL_POSE
        and not robot_motion_observed
        and not goal_already_satisfied_observed
    ):
        unproven_claims.append("nav2_completion_without_robot_motion_not_claimed")
    unproven_claims.append("mission_delivery_completion_not_claimed")
    unproven_claims.append("raw_velocity_control_not_invoked")
    blocking_reasons = list(dispatch_result.get("blocking_reasons") or ())
    if (
        nav2_completion_reported
        and config.action_kind is HardwareActionKind.NAV2_GOAL_POSE
        and not robot_motion_observed
        and not goal_already_satisfied_observed
    ):
        blocking_reasons.append("nav2_completion_without_robot_motion_observed")

    return HardwareAdapterEvidence(
        adapter_id=ROS2_NAV2_HARDWARE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.ROS2_NAV2,
        vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
        execution_mode=config.execution_mode,
        missionos_action_ref=config.missionos_action_ref,
        adapter_action_kind=config.action_kind,
        operator_approval_ref=config.operator_approval_ref,
        authorization_source=config.authorization_source,
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
            str(dispatch_result.get("ack_source") or "nav2_action_server")
            if command_ack_observed
            else None
        ),
        ack_status=ack_status,
        runtime_state_observed=_runtime_state_observed(state_result),
        runtime_progress_observed=progress_observed,
        completion_claimed=completion_claimed,
        completion_scope=(
            "loopback_action"
            if completion_claimed
            and config.execution_mode is HardwareExecutionMode.LOOPBACK
            else "sim_action"
            if completion_claimed
            and config.execution_mode is HardwareExecutionMode.SIM
            else "adapter_action"
            if completion_claimed
            else "none"
        ),
        physical_execution_invoked=physical_execution_invoked,
        safe_stop_requested=False,
        abort_requested=False,
        telemetry_fresh=config.telemetry_fresh,
        blocking_reasons=_dedupe(blocking_reasons),
        unproven_claims=tuple(unproven_claims),
        raw_logs_ref=config.raw_logs_ref or dispatch_result.get("raw_logs_ref"),
    )


def build_ros2_nav2_safe_stop_hardware_adapter_evidence(
    *,
    config: Ros2Nav2HardwareAdapterConfig,
    abort_requested: bool = False,
) -> HardwareAdapterEvidence:
    """Build safe-stop request evidence for the Nav2 adapter."""

    return HardwareAdapterEvidence(
        adapter_id=ROS2_NAV2_HARDWARE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.ROS2_NAV2,
        vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
        execution_mode=config.execution_mode,
        missionos_action_ref=config.missionos_action_ref,
        adapter_action_kind=HardwareActionKind.SAFE_STOP,
        operator_approval_ref=config.operator_approval_ref,
        authorization_source=config.authorization_source,
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
        ),
        raw_logs_ref=config.raw_logs_ref,
    )


class Ros2Nav2HardwareAdapter:
    """Bounded Nav2 adapter wrapper around an injected client boundary."""

    def __init__(
        self,
        *,
        config: Ros2Nav2HardwareAdapterConfig,
        client: Nav2DispatchClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._evidence: list[HardwareAdapterEvidence] = []

    def capabilities(self) -> HardwareAdapterCapabilities:
        return build_ros2_nav2_hardware_adapter_capabilities(
            execution_mode=self._config.execution_mode,
        )

    def preflight_check(self) -> HardwareAdapterPreflightResult:
        return build_ros2_nav2_hardware_adapter_preflight(
            config=self._config,
            client_present=self._client is not None,
        )

    def propose_dispatch(self) -> HardwareDispatchCandidate:
        return build_ros2_nav2_hardware_dispatch_candidate(
            config=self._config,
            preflight=self.preflight_check(),
        )

    def require_operator_approval(self) -> HardwareOperatorApproval:
        return build_ros2_nav2_hardware_operator_approval(config=self._config)

    def validate_operator_approval(
        self,
        approval: HardwareOperatorApproval,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if approval.approved_adapter_id != ROS2_NAV2_HARDWARE_ADAPTER_ID:
            reasons.append("operator_approval_adapter_mismatch")
        if approval.approved_action_ref != self._config.missionos_action_ref:
            reasons.append("operator_approval_action_ref_mismatch")
        if approval.approved_action_kind is not self._config.action_kind:
            reasons.append("operator_approval_action_kind_mismatch")
        return tuple(reasons)

    def dispatch_approved_action(self) -> HardwareAdapterEvidence:
        preflight = self.preflight_check()
        blocking_reasons = list(preflight.blocking_reasons)
        if (self._config.authorization_source == "human_approved_policy"
                and self._config.execution_mode is not HardwareExecutionMode.SIM):
            blocking_reasons.append("policy_delegation_simulator_only")
        if not self._config.operator_approval_ref:
            blocking_reasons.append("operator_approval_missing")
        if preflight.preflight_status is HardwarePreflightStatus.BLOCKED or blocking_reasons:
            evidence = build_blocked_ros2_nav2_hardware_adapter_evidence(
                config=self._config,
                blocking_reasons=_dedupe(blocking_reasons),
            )
            self._evidence.append(evidence)
            return evidence

        if self._client is None:
            evidence = build_blocked_ros2_nav2_hardware_adapter_evidence(
                config=self._config,
                blocking_reasons=("ros2_nav2_client_missing",),
            )
            self._evidence.append(evidence)
            return evidence

        if self._config.action_kind is HardwareActionKind.NAV2_CANCEL_GOAL:
            dispatch_result = self._client.cancel_goal()
        elif self._config.action_kind is HardwareActionKind.NAV2_GOAL_POSE:
            dispatch_result = self._client.send_goal_pose(self._config.goal_pose)  # type: ignore[arg-type]
        else:
            dispatch_result = {
                "ack_status": "rejected",
                "blocking_reasons": ("action_not_dispatchable_by_nav2_adapter",),
            }

        state_result = self._client.read_state()
        progress_result = self._client.read_progress()
        evidence = build_ros2_nav2_hardware_adapter_evidence(
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
        evidence = build_ros2_nav2_safe_stop_hardware_adapter_evidence(
            config=self._config,
            abort_requested=True,
        )
        self._evidence.append(evidence)
        return evidence

    def collect_evidence(self) -> tuple[HardwareAdapterEvidence, ...]:
        return tuple(self._evidence)

    def collect_runtime_invocation_evidence(self) -> tuple[dict[str, Any], ...]:
        supplier = getattr(self._client, "collect_runtime_invocation_evidence", None)
        if not callable(supplier):
            return ()
        binding = {
            "missionos_adapter_id": ROS2_NAV2_HARDWARE_ADAPTER_ID,
            "missionos_action_ref": self._config.missionos_action_ref,
            "missionos_preparation_ref": self._config.preparation_ref,
            "missionos_preparation_sha256": self._config.preparation_sha256,
            "operator_approval_ref": self._config.operator_approval_ref,
        }
        return tuple({**dict(item), **binding} for item in supplier())


__all__ = [
    "ROS2_NAV2_HARDWARE_ADAPTER_ID",
    "ROS2_NAV2_HARDWARE_ADAPTER_OPT_IN_ENV",
    "Nav2DispatchClient",
    "Nav2GoalPose",
    "Ros2Nav2HardwareAdapter",
    "Ros2Nav2HardwareAdapterConfig",
    "build_blocked_ros2_nav2_hardware_adapter_evidence",
    "build_ros2_nav2_hardware_adapter_capabilities",
    "build_ros2_nav2_hardware_adapter_evidence",
    "build_ros2_nav2_hardware_adapter_preflight",
    "build_ros2_nav2_hardware_dispatch_candidate",
    "build_ros2_nav2_hardware_operator_approval",
    "build_ros2_nav2_safe_stop_hardware_adapter_evidence",
]

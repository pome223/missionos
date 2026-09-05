"""Bounded hardware adapter contract artifacts.

This module starts Real Hardware Bridge v1 as adapter evidence contracts plus a
projection from the existing PX4 bench executor result. It does not provide a
fake adapter, does not simulate dispatch, and does not open a hardware link.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from missionos_core import HardwareExecutionMode


HARDWARE_ADAPTER_CONTRACT_SCHEMA_VERSION = "missionos_hardware_adapter_contract.v1"
HARDWARE_ADAPTER_PREFLIGHT_SCHEMA_VERSION = "missionos_hardware_adapter_preflight.v1"
HARDWARE_ADAPTER_DISPATCH_CANDIDATE_SCHEMA_VERSION = (
    "missionos_hardware_adapter_dispatch_candidate.v1"
)
HARDWARE_ADAPTER_OPERATOR_APPROVAL_SCHEMA_VERSION = (
    "missionos_hardware_adapter_operator_approval.v1"
)
HARDWARE_ADAPTER_EVIDENCE_SCHEMA_VERSION = "missionos_hardware_adapter_evidence.v1"

SCHEMA_EXAMPLE_ADAPTER_ID = "hardware_adapter_schema_example_only.v1"
PX4_BENCH_HARDWARE_ADAPTER_ID = "px4_real_hardware_bench_adapter.v1"


class HardwareAdapterContractError(ValueError):
    """Raised when hardware adapter artifacts violate MissionOS boundaries."""


class HardwareAdapterKind(str, Enum):
    SCHEMA_EXAMPLE_ONLY = "schema_example_only"
    ROS2_NAV2 = "ros2_nav2"
    PX4_MAVLINK = "px4_mavlink"
    PX4_MAVSDK = "px4_mavsdk"
    UNITREE_SDK2 = "unitree_sdk2"
    VENDOR_SPECIFIC = "vendor_specific"


class HardwareVehicleClass(str, Enum):
    GROUND_ROBOT = "ground_robot"
    MULTIROTOR = "multirotor"
    FIXED_WING = "fixed_wing"
    OTHER = "other"


class HardwareActionKind(str, Enum):
    BOUNDED_LOCAL_MOVE = "bounded_local_move"
    HOLD = "hold"
    LAND = "land"
    RETURN_TO_LAUNCH = "return_to_launch"
    NAV2_GOAL_POSE = "nav2_goal_pose"
    NAV2_CANCEL_GOAL = "nav2_cancel_goal"
    PX4_ARM_DISARM_BENCH = "px4_arm_disarm_bench"
    PX4_MISSION_UPLOAD = "px4_mission_upload"
    PX4_START_MISSION = "px4_start_mission"
    PX4_OFFBOARD_SETPOINT = "px4_offboard_setpoint"
    RAW_MAVLINK = "raw_mavlink"
    RAW_MOTOR = "raw_motor"
    RAW_VELOCITY = "raw_velocity"
    SAFE_STOP = "safe_stop"
    SPECIAL_MOTION = "special_motion"


class HardwarePreflightStatus(str, Enum):
    PASSED = "passed"
    BLOCKED = "blocked"


class HardwareDispatchStatus(str, Enum):
    BLOCKED = "blocked"
    SENT = "sent"
    SAFE_STOP_REQUESTED = "safe_stop_requested"


class HardwareAckStatus(str, Enum):
    NOT_REQUESTED = "not_requested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class HardwareAdapter(Protocol):
    """Structural contract for bounded hardware adapters.

    This is an interface, not a runtime implementation. Existing runtime paths
    can satisfy it one method at a time while preserving MissionOS authority
    boundaries.
    """

    def capabilities(self) -> HardwareAdapterCapabilities: ...

    def preflight_check(self) -> HardwareAdapterPreflightResult: ...

    def propose_dispatch(self) -> HardwareDispatchCandidate: ...

    def validate_operator_approval(
        self, approval: HardwareOperatorApproval
    ) -> tuple[str, ...]: ...

    def dispatch_approved_action(self) -> HardwareAdapterEvidence: ...

    def observe_ack(self) -> HardwareAckStatus: ...

    def observe_state(self) -> MappingLike: ...

    def observe_progress(self) -> bool: ...

    def abort_or_safe_stop(self) -> HardwareAdapterEvidence: ...

    def collect_evidence(self) -> tuple[HardwareAdapterEvidence, ...]: ...


class HardwareAdapterCapabilities(BaseModel):
    """Static capability declaration for an adapter boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[HARDWARE_ADAPTER_CONTRACT_SCHEMA_VERSION] = (
        HARDWARE_ADAPTER_CONTRACT_SCHEMA_VERSION
    )
    adapter_id: str
    adapter_kind: HardwareAdapterKind
    vehicle_class: HardwareVehicleClass
    execution_mode: HardwareExecutionMode
    allowed_actions: tuple[HardwareActionKind, ...]
    blocked_actions: tuple[HardwareActionKind, ...] = ()
    requires_operator_approval: Literal[True] = True
    requires_fresh_telemetry: bool = True
    requires_physical_estop: bool = True
    requires_geofence: bool = True
    max_speed_mps: float | None = None
    max_altitude_m: float | None = None
    max_distance_m: float | None = None
    supports_abort: bool = True
    supports_return: bool = True
    supports_hold: bool = True


class HardwareAdapterPreflightResult(BaseModel):
    """Pre-dispatch check artifact. It creates no authority and sends nothing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[HARDWARE_ADAPTER_PREFLIGHT_SCHEMA_VERSION] = (
        HARDWARE_ADAPTER_PREFLIGHT_SCHEMA_VERSION
    )
    adapter_id: str
    adapter_action_kind: HardwareActionKind
    preflight_status: HardwarePreflightStatus
    blocking_reasons: tuple[str, ...] = ()
    telemetry_fresh: bool
    heartbeat_alive: bool
    geofence_satisfied: bool
    operating_volume_satisfied: bool
    physical_estop_available: bool | None = None
    vehicle_physically_secured: bool | None = None
    power_disconnect_available: bool | None = None
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False

    @model_validator(mode="after")
    def _validate_preflight_blocking_reasons(self) -> "HardwareAdapterPreflightResult":
        if self.preflight_status is HardwarePreflightStatus.PASSED and self.blocking_reasons:
            raise HardwareAdapterContractError(
                "passed preflight cannot carry blocking reasons"
            )
        if self.preflight_status is HardwarePreflightStatus.BLOCKED and not self.blocking_reasons:
            raise HardwareAdapterContractError(
                "blocked preflight requires blocking reasons"
            )
        return self


class HardwareDispatchCandidate(BaseModel):
    """Adapter-specific candidate artifact for a MissionOS bounded action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[HARDWARE_ADAPTER_DISPATCH_CANDIDATE_SCHEMA_VERSION] = (
        HARDWARE_ADAPTER_DISPATCH_CANDIDATE_SCHEMA_VERSION
    )
    adapter_id: str
    missionos_action_ref: str
    adapter_action_kind: HardwareActionKind
    adapter_parameters: dict[str, Any] = Field(default_factory=dict)
    capability_match: bool
    safety_constraints_applied: tuple[str, ...]
    operator_approval_required: Literal[True] = True
    raw_command_preview: Literal[False] = False
    dispatch_request_sent: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    preflight_status: HardwarePreflightStatus
    blocking_reasons: tuple[str, ...] = ()
    telemetry_fresh: bool

    @model_validator(mode="after")
    def _validate_candidate_boundary(self) -> "HardwareDispatchCandidate":
        if self.preflight_status is HardwarePreflightStatus.PASSED and self.blocking_reasons:
            raise HardwareAdapterContractError(
                "dispatch candidate cannot pass preflight with blocking reasons"
            )
        if self.preflight_status is HardwarePreflightStatus.BLOCKED and not self.blocking_reasons:
            raise HardwareAdapterContractError(
                "blocked dispatch candidate requires blocking reasons"
            )
        return self


class HardwareOperatorApproval(BaseModel):
    """Human approval artifact scoped to one MissionOS bounded action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[HARDWARE_ADAPTER_OPERATOR_APPROVAL_SCHEMA_VERSION] = (
        HARDWARE_ADAPTER_OPERATOR_APPROVAL_SCHEMA_VERSION
    )
    operator_approval_ref: str
    approved_adapter_id: str | None = None
    approved_preparation_ref: str | None = None
    approved_preparation_sha256: str | None = None
    approval_actor: str
    approval_timestamp: datetime
    approval_expires_at: datetime | None = None
    approved_action_ref: str
    approved_action_kind: HardwareActionKind
    operator_approved: Literal[True] = True


class HardwareAdapterEvidence(BaseModel):
    """Evidence artifact emitted by one adapter dispatch attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[HARDWARE_ADAPTER_EVIDENCE_SCHEMA_VERSION] = (
        HARDWARE_ADAPTER_EVIDENCE_SCHEMA_VERSION
    )
    adapter_id: str
    adapter_kind: HardwareAdapterKind
    vehicle_class: HardwareVehicleClass
    execution_mode: HardwareExecutionMode
    missionos_action_ref: str
    adapter_action_kind: HardwareActionKind
    operator_approval_ref: str | None = None
    authorization_source: Literal["individual_human_approval", "human_approved_policy"] = "individual_human_approval"
    preflight_status: HardwarePreflightStatus
    dispatch_status: HardwareDispatchStatus
    dispatch_request_sent: bool
    command_ack_observed: bool
    ack_source: str | None = None
    ack_status: HardwareAckStatus = HardwareAckStatus.NOT_REQUESTED
    runtime_state_observed: bool = False
    runtime_progress_observed: bool = False
    completion_claimed: bool = False
    completion_scope: Literal[
        "none",
        "loopback_action",
        "sim_action",
        "adapter_action",
        "mission",
    ] = "none"
    physical_execution_invoked: bool = False
    safe_stop_requested: bool = False
    abort_requested: bool = False
    telemetry_fresh: bool
    blocking_reasons: tuple[str, ...] = ()
    unproven_claims: tuple[str, ...] = ()
    raw_logs_ref: str | None = None

    @model_validator(mode="after")
    def _validate_claim_boundaries(self) -> "HardwareAdapterEvidence":
        if self.dispatch_request_sent and not self.operator_approval_ref:
            raise HardwareAdapterContractError(
                "hardware adapter dispatch_request_sent requires operator approval"
            )
        if self.dispatch_request_sent and not self.telemetry_fresh:
            raise HardwareAdapterContractError(
                "hardware adapter dispatch_request_sent requires fresh telemetry"
            )
        if self.dispatch_status is HardwareDispatchStatus.SENT and not self.dispatch_request_sent:
            raise HardwareAdapterContractError(
                "sent dispatch status requires dispatch_request_sent=true"
            )
        if self.dispatch_status is not HardwareDispatchStatus.SENT and self.command_ack_observed:
            raise HardwareAdapterContractError(
                "blocked or safe-stop evidence cannot observe command ACK"
            )
        if (
            self.ack_status
            not in {HardwareAckStatus.NOT_REQUESTED, HardwareAckStatus.TIMEOUT}
            and not self.command_ack_observed
        ):
            raise HardwareAdapterContractError(
                "ack_status requires command_ack_observed=true"
            )
        if self.completion_claimed and not self.command_ack_observed:
            raise HardwareAdapterContractError("completion cannot be claimed without ACK")
        if self.completion_claimed and not self.runtime_progress_observed:
            raise HardwareAdapterContractError(
                "completion cannot be claimed from ACK alone"
            )
        if self.completion_claimed and self.completion_scope == "none":
            raise HardwareAdapterContractError(
                "completion_claimed requires a non-none completion_scope"
            )
        if not self.completion_claimed and self.completion_scope != "none":
            raise HardwareAdapterContractError(
                "completion_scope must be none when completion_claimed=false"
            )
        if (
            self.adapter_action_kind is HardwareActionKind.PX4_ARM_DISARM_BENCH
            and self.completion_scope == "mission"
        ):
            raise HardwareAdapterContractError(
                "px4 arm/disarm bench evidence cannot claim mission completion"
            )
        if (
            self.execution_mode is HardwareExecutionMode.LOOPBACK
            and self.completion_claimed
            and self.completion_scope != "loopback_action"
        ):
            raise HardwareAdapterContractError(
                "loopback execution can only claim loopback_action completion"
            )
        if (
            self.completion_scope == "loopback_action"
            and self.execution_mode is not HardwareExecutionMode.LOOPBACK
        ):
            raise HardwareAdapterContractError(
                "loopback_action completion_scope requires execution_mode=loopback"
            )
        if (
            self.execution_mode is HardwareExecutionMode.SIM
            and self.completion_claimed
            and self.completion_scope != "sim_action"
        ):
            raise HardwareAdapterContractError(
                "sim execution can only claim sim_action completion"
            )
        if (
            self.completion_scope == "sim_action"
            and self.execution_mode is not HardwareExecutionMode.SIM
        ):
            raise HardwareAdapterContractError(
                "sim_action completion_scope requires execution_mode=sim"
            )
        if (
            self.execution_mode is HardwareExecutionMode.SCHEMA_EXAMPLE_ONLY
            and self.physical_execution_invoked
        ):
            raise HardwareAdapterContractError(
                "schema-example hardware adapter artifact cannot claim physical execution"
            )
        if (
            self.dispatch_status is HardwareDispatchStatus.SAFE_STOP_REQUESTED
            and not self.safe_stop_requested
        ):
            raise HardwareAdapterContractError(
                "safe_stop_requested dispatch status requires safe_stop_requested=true"
            )
        if self.abort_requested and not self.safe_stop_requested:
            raise HardwareAdapterContractError(
                "operator abort must request safe stop"
            )
        if (
            self.dispatch_status is HardwareDispatchStatus.SAFE_STOP_REQUESTED
            and not self.blocking_reasons
        ):
            raise HardwareAdapterContractError(
                "safe-stop evidence requires blocking reasons"
            )
        if self.dispatch_status is HardwareDispatchStatus.BLOCKED and not self.blocking_reasons:
            raise HardwareAdapterContractError(
                "blocked dispatch evidence requires blocking reasons"
            )
        return self


def build_schema_example_capabilities() -> HardwareAdapterCapabilities:
    """Return a static schema example with no adapter behavior."""

    return HardwareAdapterCapabilities(
        adapter_id=SCHEMA_EXAMPLE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.SCHEMA_EXAMPLE_ONLY,
        vehicle_class=HardwareVehicleClass.GROUND_ROBOT,
        execution_mode=HardwareExecutionMode.SCHEMA_EXAMPLE_ONLY,
        allowed_actions=(
            HardwareActionKind.HOLD,
            HardwareActionKind.NAV2_GOAL_POSE,
            HardwareActionKind.NAV2_CANCEL_GOAL,
            HardwareActionKind.SAFE_STOP,
            HardwareActionKind.RETURN_TO_LAUNCH,
        ),
        blocked_actions=(HardwareActionKind.LAND,),
        max_speed_mps=0.5,
        max_altitude_m=0.0,
        max_distance_m=3.0,
    )


def build_px4_bench_hardware_adapter_capabilities(
    *,
    execution_mode: HardwareExecutionMode = HardwareExecutionMode.LOOPBACK,
) -> HardwareAdapterCapabilities:
    """Return the PX4 bench adapter capability artifact for this bounded slice."""

    return HardwareAdapterCapabilities(
        adapter_id=PX4_BENCH_HARDWARE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.PX4_MAVLINK,
        vehicle_class=HardwareVehicleClass.MULTIROTOR,
        execution_mode=execution_mode,
        allowed_actions=(HardwareActionKind.PX4_ARM_DISARM_BENCH,),
        blocked_actions=(
            HardwareActionKind.PX4_MISSION_UPLOAD,
            HardwareActionKind.PX4_START_MISSION,
            HardwareActionKind.PX4_OFFBOARD_SETPOINT,
            HardwareActionKind.RAW_MAVLINK,
            HardwareActionKind.LAND,
            HardwareActionKind.RETURN_TO_LAUNCH,
        ),
        requires_operator_approval=True,
        requires_fresh_telemetry=True,
        requires_physical_estop=True,
        requires_geofence=False,
        max_speed_mps=0.0,
        max_altitude_m=0.0,
        max_distance_m=0.0,
        supports_abort=True,
        supports_return=False,
        supports_hold=False,
    )


def build_px4_bench_hardware_adapter_preflight(
    *,
    blocking_reasons: tuple[str, ...] = (),
    telemetry_fresh: bool = True,
    heartbeat_alive: bool = True,
    operating_volume_satisfied: bool = True,
    physical_estop_available: bool = False,
    vehicle_physically_secured: bool = False,
    power_disconnect_available: bool = False,
) -> HardwareAdapterPreflightResult:
    """Build a preflight artifact for the existing PX4 bench boundary."""

    reasons = list(blocking_reasons)
    if not physical_estop_available:
        reasons.append("physical_estop_missing")
    if not vehicle_physically_secured:
        reasons.append("vehicle_not_physically_secured")
    if not power_disconnect_available:
        reasons.append("power_disconnect_missing")
    normalized_reasons = tuple(dict.fromkeys(reasons))
    return HardwareAdapterPreflightResult(
        adapter_id=PX4_BENCH_HARDWARE_ADAPTER_ID,
        adapter_action_kind=HardwareActionKind.PX4_ARM_DISARM_BENCH,
        preflight_status=(
            HardwarePreflightStatus.BLOCKED
            if normalized_reasons
            else HardwarePreflightStatus.PASSED
        ),
        blocking_reasons=normalized_reasons,
        telemetry_fresh=telemetry_fresh,
        heartbeat_alive=heartbeat_alive,
        geofence_satisfied=True,
        operating_volume_satisfied=operating_volume_satisfied,
        physical_estop_available=physical_estop_available,
        vehicle_physically_secured=vehicle_physically_secured,
        power_disconnect_available=power_disconnect_available,
    )


def build_px4_bench_hardware_dispatch_candidate(
    *,
    missionos_action_ref: str,
    preflight: HardwareAdapterPreflightResult,
) -> HardwareDispatchCandidate:
    """Build the dispatch candidate artifact without sending anything."""

    return HardwareDispatchCandidate(
        adapter_id=PX4_BENCH_HARDWARE_ADAPTER_ID,
        missionos_action_ref=missionos_action_ref,
        adapter_action_kind=HardwareActionKind.PX4_ARM_DISARM_BENCH,
        adapter_parameters={},
        capability_match=True,
        safety_constraints_applied=(
            "operator_approval_required",
            "props_removed_attestation_required",
            "operator_present_attestation_required",
            "vehicle_secured_attestation_required",
            "physical_estop_required",
            "power_disconnect_required",
            "flight_execution_forbidden",
            "takeoff_forbidden",
            "mission_start_forbidden",
        ),
        preflight_status=preflight.preflight_status,
        blocking_reasons=preflight.blocking_reasons,
        telemetry_fresh=preflight.telemetry_fresh,
    )


def build_px4_bench_hardware_operator_approval(
    *,
    operator_approval_ref: str,
    approval_actor: str,
    approval_timestamp: datetime,
    missionos_action_ref: str,
) -> HardwareOperatorApproval:
    """Build the hardware adapter approval artifact from Gateway approval state."""

    return HardwareOperatorApproval(
        operator_approval_ref=operator_approval_ref,
        approved_adapter_id=PX4_BENCH_HARDWARE_ADAPTER_ID,
        approval_actor=approval_actor,
        approval_timestamp=approval_timestamp,
        approved_action_ref=missionos_action_ref,
        approved_action_kind=HardwareActionKind.PX4_ARM_DISARM_BENCH,
    )


def build_blocked_px4_bench_hardware_adapter_evidence(
    *,
    missionos_action_ref: str,
    operator_approval_ref: str | None,
    blocking_reasons: tuple[str, ...],
    execution_mode: HardwareExecutionMode = HardwareExecutionMode.BENCH,
    telemetry_fresh: bool = True,
) -> HardwareAdapterEvidence:
    """Build evidence for a PX4 bench dispatch blocked before command send."""

    return HardwareAdapterEvidence(
        adapter_id=PX4_BENCH_HARDWARE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.PX4_MAVLINK,
        vehicle_class=HardwareVehicleClass.MULTIROTOR,
        execution_mode=execution_mode,
        missionos_action_ref=missionos_action_ref,
        adapter_action_kind=HardwareActionKind.PX4_ARM_DISARM_BENCH,
        operator_approval_ref=operator_approval_ref,
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
        telemetry_fresh=telemetry_fresh,
        blocking_reasons=blocking_reasons,
        unproven_claims=(
            "dispatch_not_sent",
            "command_ack_not_observed",
            "adapter_action_completion_not_claimed",
            "physical_execution_not_invoked",
            "flight_execution_not_invoked",
        ),
    )


def build_px4_bench_safe_stop_hardware_adapter_evidence(
    *,
    missionos_action_ref: str,
    operator_approval_ref: str | None,
    blocking_reasons: tuple[str, ...],
    abort_requested: bool = False,
    execution_mode: HardwareExecutionMode = HardwareExecutionMode.BENCH,
    telemetry_fresh: bool = True,
) -> HardwareAdapterEvidence:
    """Build evidence for safe-stop preemption before normal command dispatch."""

    return HardwareAdapterEvidence(
        adapter_id=PX4_BENCH_HARDWARE_ADAPTER_ID,
        adapter_kind=HardwareAdapterKind.PX4_MAVLINK,
        vehicle_class=HardwareVehicleClass.MULTIROTOR,
        execution_mode=execution_mode,
        missionos_action_ref=missionos_action_ref,
        adapter_action_kind=HardwareActionKind.PX4_ARM_DISARM_BENCH,
        operator_approval_ref=operator_approval_ref,
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
        telemetry_fresh=telemetry_fresh,
        blocking_reasons=blocking_reasons,
        unproven_claims=(
            "dispatch_preempted_by_safe_stop",
            "command_ack_not_observed",
            "adapter_action_completion_not_claimed",
            "physical_execution_not_invoked",
            "flight_execution_not_invoked",
        ),
    )


def _invocation_status(value: MappingLike, key: str) -> str:
    return str(value.get(key) or "")


def _invocation_bool(value: MappingLike, key: str) -> bool:
    return value.get(key) is True


def _blocked_reasons(*invocations: MappingLike) -> tuple[str, ...]:
    reasons: list[str] = []
    for invocation in invocations:
        for reason in invocation.get("blocked_reasons") or ():
            text = str(reason).strip()
            if text and text not in reasons:
                reasons.append(text)
    return tuple(reasons)


MappingLike = dict[str, Any]


def build_px4_bench_hardware_adapter_evidence(
    *,
    missionos_action_ref: str,
    operator_approval_ref: str,
    runtime_result: dict[str, Any],
    adapter_id: str = PX4_BENCH_HARDWARE_ADAPTER_ID,
) -> HardwareAdapterEvidence:
    """Project the existing PX4 bench executor result into adapter evidence.

    This is intentionally downstream of the real executor path:
    ``run_px4_real_hardware_arm_disarm_bench`` must have already sent the
    command through its injected loopback or gated real-serial link and returned
    command ACK / state readback evidence. This helper only normalizes that
    result; it never creates dispatch authority and never sends a command.
    """

    arm = dict(runtime_result.get("arm") or {})
    disarm = dict(runtime_result.get("disarm") or {})
    if not arm or not disarm:
        raise HardwareAdapterContractError(
            "px4 bench adapter evidence requires arm and disarm invocation results"
        )

    command_sent = _invocation_bool(arm, "mavlink_command_sent") and _invocation_bool(
        disarm, "mavlink_command_sent"
    )
    ack_observed = _invocation_bool(arm, "command_ack_observed") and _invocation_bool(
        disarm, "command_ack_observed"
    )
    state_readback_observed = _invocation_bool(
        arm, "state_readback_observed"
    ) and _invocation_bool(disarm, "state_readback_observed")
    accepted = (
        _invocation_status(arm, "status") == "accepted"
        and _invocation_status(disarm, "status") == "accepted"
    )
    physical_execution_invoked = _invocation_bool(
        arm, "physical_execution_invoked"
    ) or _invocation_bool(disarm, "physical_execution_invoked")
    link_kind = str(arm.get("link_kind") or disarm.get("link_kind") or "")
    blocking_reasons = _blocked_reasons(arm, disarm)

    unproven_claims: list[str] = []
    if ack_observed and not state_readback_observed:
        unproven_claims.append("ack_is_not_success")
    if not physical_execution_invoked:
        unproven_claims.append("physical_execution_not_invoked")
    if accepted and state_readback_observed and not physical_execution_invoked:
        unproven_claims.append("loopback_action_completion_not_physical")
    unproven_claims.append("mission_delivery_completion_not_claimed")
    unproven_claims.append("flight_execution_not_invoked")

    return HardwareAdapterEvidence(
        adapter_id=adapter_id,
        adapter_kind=HardwareAdapterKind.PX4_MAVLINK,
        vehicle_class=HardwareVehicleClass.MULTIROTOR,
        execution_mode=(
            HardwareExecutionMode.BENCH
            if physical_execution_invoked
            else HardwareExecutionMode.LOOPBACK
        ),
        missionos_action_ref=missionos_action_ref,
        adapter_action_kind=HardwareActionKind.PX4_ARM_DISARM_BENCH,
        operator_approval_ref=operator_approval_ref,
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
        command_ack_observed=ack_observed,
        ack_source=link_kind or adapter_id if ack_observed else None,
        ack_status=(
            HardwareAckStatus.ACCEPTED
            if accepted
            else HardwareAckStatus.REJECTED
            if ack_observed
            else HardwareAckStatus.TIMEOUT
            if command_sent
            else HardwareAckStatus.NOT_REQUESTED
        ),
        runtime_progress_observed=state_readback_observed,
        completion_claimed=accepted and state_readback_observed,
        completion_scope=(
            "adapter_action"
            if accepted and state_readback_observed and physical_execution_invoked
            else "loopback_action"
            if accepted and state_readback_observed
            else "none"
        ),
        physical_execution_invoked=physical_execution_invoked,
        safe_stop_requested=False,
        abort_requested=False,
        telemetry_fresh=True,
        blocking_reasons=blocking_reasons,
        unproven_claims=tuple(unproven_claims),
        raw_logs_ref=None,
    )


__all__ = [
    "SCHEMA_EXAMPLE_ADAPTER_ID",
    "HARDWARE_ADAPTER_CONTRACT_SCHEMA_VERSION",
    "HARDWARE_ADAPTER_DISPATCH_CANDIDATE_SCHEMA_VERSION",
    "HARDWARE_ADAPTER_EVIDENCE_SCHEMA_VERSION",
    "HARDWARE_ADAPTER_OPERATOR_APPROVAL_SCHEMA_VERSION",
    "HARDWARE_ADAPTER_PREFLIGHT_SCHEMA_VERSION",
    "PX4_BENCH_HARDWARE_ADAPTER_ID",
    "HardwareAckStatus",
    "HardwareActionKind",
    "HardwareAdapter",
    "HardwareAdapterCapabilities",
    "HardwareAdapterContractError",
    "HardwareAdapterEvidence",
    "HardwareAdapterKind",
    "HardwareAdapterPreflightResult",
    "HardwareDispatchCandidate",
    "HardwareDispatchStatus",
    "HardwareExecutionMode",
    "HardwareOperatorApproval",
    "HardwarePreflightStatus",
    "HardwareVehicleClass",
    "build_blocked_px4_bench_hardware_adapter_evidence",
    "build_px4_bench_hardware_adapter_capabilities",
    "build_px4_bench_hardware_adapter_evidence",
    "build_px4_bench_hardware_adapter_preflight",
    "build_px4_bench_hardware_dispatch_candidate",
    "build_px4_bench_hardware_operator_approval",
    "build_px4_bench_safe_stop_hardware_adapter_evidence",
    "build_schema_example_capabilities",
]

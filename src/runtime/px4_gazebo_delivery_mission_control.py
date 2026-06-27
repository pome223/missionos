"""Multi-phase PX4/Gazebo delivery mission control artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.task_store import TaskStore

PX4_GAZEBO_DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION = (
    "px4_gazebo_delivery_mission_contract.v1"
)
PX4_GAZEBO_DELIVERY_PHASE_STATE_MACHINE_SCHEMA_VERSION = (
    "px4_gazebo_delivery_phase_state_machine.v1"
)
PX4_GAZEBO_DELIVERY_MISSION_HEALTH_SNAPSHOT_SCHEMA_VERSION = (
    "px4_gazebo_delivery_mission_health_snapshot.v1"
)
PX4_GAZEBO_DELIVERY_PHASE_GATE_SCHEMA_VERSION = "px4_gazebo_delivery_phase_gate.v1"
PX4_GAZEBO_DELIVERY_PHASE_GATE_EVALUATION_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_delivery_phase_gate_evaluation_result.v1"
)
PX4_GAZEBO_DELIVERY_RECOVERY_POLICY_MATRIX_SCHEMA_VERSION = (
    "px4_gazebo_delivery_recovery_policy_matrix.v1"
)
PX4_GAZEBO_DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION = (
    "px4_gazebo_delivery_recovery_decision.v1"
)
PX4_GAZEBO_DELIVERY_OPERATOR_BOUNDARY_SCHEMA_VERSION = (
    "px4_gazebo_delivery_operator_boundary.v1"
)
PX4_GAZEBO_DELIVERY_MISSION_PHASE_EVIDENCE_SCHEMA_VERSION = (
    "px4_gazebo_delivery_mission_phase_evidence.v1"
)
PX4_GAZEBO_DELIVERY_MISSION_RUNNER_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_delivery_mission_runner_v1_result.v1"
)
PX4_GAZEBO_DELIVERY_MISSION_REPLAY_TIMELINE_SCHEMA_VERSION = (
    "px4_gazebo_delivery_mission_replay_timeline.v1"
)
PX4_GAZEBO_DELIVERY_PHASE_TRANSITION_EVENT_SCHEMA_VERSION = (
    "px4_gazebo_delivery_phase_transition_event.v1"
)
PX4_GAZEBO_DELIVERY_MISSION_PREPARED_RUN_SCHEMA_VERSION = (
    "px4_gazebo_delivery_mission_prepared_run.v1"
)
PX4_GAZEBO_DELIVERY_MISSION_INSPECTION_SCHEMA_VERSION = (
    "px4_gazebo_delivery_mission_inspection.v1"
)
PX4_GAZEBO_DELIVERY_MISSION_GOLDEN_CORPUS_SCHEMA_VERSION = (
    "px4_gazebo_delivery_mission_golden_corpus.v1"
)


class PX4GazeboDeliveryMissionControlError(RuntimeError):
    """Raised when multi-phase mission control evidence is inconsistent."""


class PX4GazeboDeliveryMissionPhase(str, Enum):
    PREFLIGHT = "preflight"
    TAKEOFF = "takeoff"
    WAYPOINT_ROUTE = "waypoint_route"
    PICKUP_APPROACH = "pickup_approach"
    PICKUP_VERIFIED = "pickup_verified"
    DELIVERY_ROUTE = "delivery_route"
    DROPOFF_APPROACH = "dropoff_approach"
    DROPOFF_VERIFIED = "dropoff_verified"
    RETURN_LAND = "return_land"
    COMPLETED = "completed"


DEFAULT_MISSION_PHASE_SEQUENCE: tuple[PX4GazeboDeliveryMissionPhase, ...] = (
    PX4GazeboDeliveryMissionPhase.PREFLIGHT,
    PX4GazeboDeliveryMissionPhase.TAKEOFF,
    PX4GazeboDeliveryMissionPhase.WAYPOINT_ROUTE,
    PX4GazeboDeliveryMissionPhase.PICKUP_APPROACH,
    PX4GazeboDeliveryMissionPhase.PICKUP_VERIFIED,
    PX4GazeboDeliveryMissionPhase.DELIVERY_ROUTE,
    PX4GazeboDeliveryMissionPhase.DROPOFF_APPROACH,
    PX4GazeboDeliveryMissionPhase.DROPOFF_VERIFIED,
    PX4GazeboDeliveryMissionPhase.RETURN_LAND,
    PX4GazeboDeliveryMissionPhase.COMPLETED,
)


class PX4GazeboDeliveryMissionFinalStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


class PX4GazeboDeliveryPhaseRuntimeStatus(str, Enum):
    PENDING = "pending"
    ENTERING = "entering"
    RUNNING = "running"
    GATE_BLOCKED = "gate_blocked"
    ABORTED = "aborted"
    RECOVERING = "recovering"
    COMPLETED = "completed"


class PX4GazeboDeliveryHealthSnapshotKind(str, Enum):
    PHASE_ENTRY = "phase_entry"
    PHASE_EXIT = "phase_exit"


class PX4GazeboDeliveryMissionHealthStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"


class PX4GazeboDeliveryPhaseGateStatus(str, Enum):
    PASSED = "passed"
    BLOCKED = "blocked"


class PX4GazeboDeliveryPhaseGateVerdict(str, Enum):
    PASS = "pass"
    BLOCKED = "blocked"
    ABORT = "abort"


class PX4GazeboDeliveryMissionFailureType(str, Enum):
    ACK_TIMEOUT = "ack_timeout"
    BATTERY_LOW = "battery_low"
    GATE_BLOCKED = "gate_blocked"
    LINK_LOSS = "link_loss"
    POSE_DEVIATION = "pose_deviation"
    STALE_TELEMETRY = "stale_telemetry"
    RECOVERY_UNCONFIRMED = "recovery_unconfirmed"


class PX4GazeboDeliveryMissionRecoveryAction(str, Enum):
    CONTINUE = "continue"
    HOLD = "hold"
    LAND = "land"
    RTL = "rtl"
    BLOCK = "block"
    OPERATOR_ESCALATION = "operator_escalation"


class PX4GazeboDeliveryMissionAutonomyLevel(str, Enum):
    READ_ONLY = "read_only"
    APPROVAL_GATED_DISPATCH = "approval_gated_dispatch"
    OPERATOR_ONLY = "operator_only"


class PX4GazeboDeliveryPhaseAutonomyBoundary(str, Enum):
    AUTONOMOUS = "autonomous"
    REQUIRES_APPROVAL = "requires_approval"
    FORBIDDEN = "forbidden"


class _MissionSafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    bounded_allowlist_enforced: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False
    approval_free_dispatch_allowed: Literal[False] = False
    memory_direct_command_authority_allowed: Literal[False] = False


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


def _ordered_strings(values: Sequence[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in values or ():
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _ordered_phases(
    values: Sequence[PX4GazeboDeliveryMissionPhase | str] | None,
) -> tuple[PX4GazeboDeliveryMissionPhase, ...]:
    return tuple(
        (
            item
            if isinstance(item, PX4GazeboDeliveryMissionPhase)
            else PX4GazeboDeliveryMissionPhase(str(item))
        )
        for item in (values or ())
    )


def _mission_contract_ref(contract: "PX4GazeboDeliveryMissionContract") -> str:
    return f"px4_gazebo_delivery_mission_contract:{contract.mission_contract_id}"


def _state_machine_ref(machine: "PX4GazeboDeliveryPhaseStateMachine") -> str:
    return f"px4_gazebo_delivery_phase_state_machine:{machine.state_machine_id}"


def _health_snapshot_ref(snapshot: "PX4GazeboDeliveryMissionHealthSnapshot") -> str:
    return f"px4_gazebo_delivery_mission_health_snapshot:{snapshot.snapshot_id}"


def _phase_gate_ref(gate: "PX4GazeboDeliveryPhaseGate") -> str:
    return f"px4_gazebo_delivery_phase_gate:{gate.gate_id}"


def _recovery_policy_ref(policy: "PX4GazeboDeliveryRecoveryPolicyMatrix") -> str:
    return f"px4_gazebo_delivery_recovery_policy_matrix:{policy.policy_id}"


def _recovery_policy_cell_ref(
    policy: "PX4GazeboDeliveryRecoveryPolicyMatrix",
    *,
    phase: PX4GazeboDeliveryMissionPhase,
    failure_type: PX4GazeboDeliveryMissionFailureType,
) -> str:
    return f"{_recovery_policy_ref(policy)}:" f"{phase.value}:{failure_type.value}"


def _phase_gate_evaluation_ref(
    evaluation: "PX4GazeboDeliveryPhaseGateEvaluationResult",
) -> str:
    return (
        "px4_gazebo_delivery_phase_gate_evaluation_result:"
        f"{evaluation.evaluation_id}"
    )


def _recovery_decision_ref(decision: "PX4GazeboDeliveryRecoveryDecision") -> str:
    return f"px4_gazebo_delivery_recovery_decision:{decision.decision_id}"


def _operator_boundary_ref(boundary: "PX4GazeboDeliveryOperatorBoundary") -> str:
    return f"px4_gazebo_delivery_operator_boundary:{boundary.boundary_id}"


def _phase_evidence_ref(evidence: "PX4GazeboDeliveryMissionPhaseEvidence") -> str:
    return f"px4_gazebo_delivery_mission_phase_evidence:{evidence.evidence_id}"


def _default_phase_gate_refs() -> tuple[str, ...]:
    return tuple(
        f"px4_gazebo_delivery_phase_gate_profile:{phase.value}"
        for phase in DEFAULT_MISSION_PHASE_SEQUENCE
    )


def _default_recovery_policy_ref() -> str:
    return "px4_gazebo_delivery_recovery_policy_matrix:mission_contract_default"


def _default_operator_boundary_ref() -> str:
    return "px4_gazebo_delivery_operator_boundary:mission_contract_default"


def _is_missing_ref(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() == "null"
    return False


class PX4GazeboDeliveryMissionContract(_MissionSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION] = (
        PX4_GAZEBO_DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    )
    mission_contract_id: str
    mission_name: str = Field(min_length=1)
    pickup_pad_ref: str = Field(min_length=1)
    dropoff_pad_ref: str = Field(min_length=1)
    package_ref: str = Field(min_length=1)
    route_plan_refs: tuple[str, ...]
    waypoint_refs: tuple[str, ...]
    phase_gate_refs: tuple[str, ...] = Field(default_factory=_default_phase_gate_refs)
    recovery_policy_ref: str = Field(default_factory=_default_recovery_policy_ref)
    operator_boundary_ref: str = Field(default_factory=_default_operator_boundary_ref)
    contract_refs_complete: Literal[True] = True
    required_phase_sequence: tuple[PX4GazeboDeliveryMissionPhase, ...]
    generated_at: datetime
    mission_runner_api_version: Literal["v1"] = "v1"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator(
        "route_plan_refs", "waypoint_refs", "phase_gate_refs", mode="before"
    )
    @classmethod
    def _coerce_strings(cls, value: Any) -> tuple[str, ...]:
        if any(_is_missing_ref(item) for item in (value or ())):
            raise PX4GazeboDeliveryMissionControlError(
                "mission contract refs must be complete and non-null"
            )
        return _ordered_strings(value)

    @field_validator("required_phase_sequence", mode="before")
    @classmethod
    def _coerce_phases(cls, value: Any) -> tuple[PX4GazeboDeliveryMissionPhase, ...]:
        return _ordered_phases(value)

    @model_validator(mode="after")
    def _validate_contract(self) -> "PX4GazeboDeliveryMissionContract":
        if self.pickup_pad_ref == self.dropoff_pad_ref:
            raise PX4GazeboDeliveryMissionControlError(
                "mission pickup and dropoff pads must differ"
            )
        if len(self.waypoint_refs) < 2:
            raise PX4GazeboDeliveryMissionControlError(
                "multi-phase mission contract requires at least two waypoint refs"
            )
        if len(self.phase_gate_refs) != len(DEFAULT_MISSION_PHASE_SEQUENCE):
            raise PX4GazeboDeliveryMissionControlError(
                "mission contract must include one phase gate ref per phase"
            )
        refs: list[Any] = [
            *self.route_plan_refs,
            *self.waypoint_refs,
            *self.phase_gate_refs,
            self.recovery_policy_ref,
            self.operator_boundary_ref,
        ]
        if any(_is_missing_ref(item) for item in refs):
            raise PX4GazeboDeliveryMissionControlError(
                "mission contract refs must be complete and non-null"
            )
        if self.required_phase_sequence != DEFAULT_MISSION_PHASE_SEQUENCE:
            raise PX4GazeboDeliveryMissionControlError(
                "mission contract must use the semantic delivery phase sequence"
            )
        return self


class PX4GazeboDeliveryPhaseStateMachine(_MissionSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_DELIVERY_PHASE_STATE_MACHINE_SCHEMA_VERSION] = (
        PX4_GAZEBO_DELIVERY_PHASE_STATE_MACHINE_SCHEMA_VERSION
    )
    state_machine_id: str
    mission_contract_ref: str = Field(min_length=1)
    phase_sequence: tuple[PX4GazeboDeliveryMissionPhase, ...]
    initial_phase: Literal[PX4GazeboDeliveryMissionPhase.PREFLIGHT] = (
        PX4GazeboDeliveryMissionPhase.PREFLIGHT
    )
    terminal_phase: Literal[PX4GazeboDeliveryMissionPhase.COMPLETED] = (
        PX4GazeboDeliveryMissionPhase.COMPLETED
    )
    transitions: tuple[
        tuple[PX4GazeboDeliveryMissionPhase, PX4GazeboDeliveryMissionPhase], ...
    ]
    allowed_runtime_statuses: tuple[PX4GazeboDeliveryPhaseRuntimeStatus, ...] = (
        PX4GazeboDeliveryPhaseRuntimeStatus.PENDING,
        PX4GazeboDeliveryPhaseRuntimeStatus.ENTERING,
        PX4GazeboDeliveryPhaseRuntimeStatus.RUNNING,
        PX4GazeboDeliveryPhaseRuntimeStatus.GATE_BLOCKED,
        PX4GazeboDeliveryPhaseRuntimeStatus.ABORTED,
        PX4GazeboDeliveryPhaseRuntimeStatus.RECOVERING,
        PX4GazeboDeliveryPhaseRuntimeStatus.COMPLETED,
    )
    phase_runtime_statuses: tuple[
        tuple[PX4GazeboDeliveryMissionPhase, PX4GazeboDeliveryPhaseRuntimeStatus], ...
    ] = ()
    generated_at: datetime

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("phase_sequence", mode="before")
    @classmethod
    def _coerce_phases(cls, value: Any) -> tuple[PX4GazeboDeliveryMissionPhase, ...]:
        return _ordered_phases(value)

    @field_validator("allowed_runtime_statuses", mode="before")
    @classmethod
    def _coerce_runtime_statuses(
        cls, value: Any
    ) -> tuple[PX4GazeboDeliveryPhaseRuntimeStatus, ...]:
        return tuple(
            (
                item
                if isinstance(item, PX4GazeboDeliveryPhaseRuntimeStatus)
                else PX4GazeboDeliveryPhaseRuntimeStatus(str(item))
            )
            for item in value
        )

    @field_validator("transitions", mode="before")
    @classmethod
    def _coerce_transitions(
        cls,
        value: Any,
    ) -> tuple[
        tuple[PX4GazeboDeliveryMissionPhase, PX4GazeboDeliveryMissionPhase], ...
    ]:
        return tuple(
            (
                (
                    item[0]
                    if isinstance(item[0], PX4GazeboDeliveryMissionPhase)
                    else PX4GazeboDeliveryMissionPhase(str(item[0]))
                ),
                (
                    item[1]
                    if isinstance(item[1], PX4GazeboDeliveryMissionPhase)
                    else PX4GazeboDeliveryMissionPhase(str(item[1]))
                ),
            )
            for item in value
        )

    @field_validator("phase_runtime_statuses", mode="before")
    @classmethod
    def _coerce_phase_runtime_statuses(
        cls, value: Any
    ) -> tuple[
        tuple[PX4GazeboDeliveryMissionPhase, PX4GazeboDeliveryPhaseRuntimeStatus], ...
    ]:
        return tuple(
            (
                (
                    item[0]
                    if isinstance(item[0], PX4GazeboDeliveryMissionPhase)
                    else PX4GazeboDeliveryMissionPhase(str(item[0]))
                ),
                (
                    item[1]
                    if isinstance(item[1], PX4GazeboDeliveryPhaseRuntimeStatus)
                    else PX4GazeboDeliveryPhaseRuntimeStatus(str(item[1]))
                ),
            )
            for item in value
        )

    @model_validator(mode="after")
    def _validate_machine(self) -> "PX4GazeboDeliveryPhaseStateMachine":
        if self.phase_sequence != DEFAULT_MISSION_PHASE_SEQUENCE:
            raise PX4GazeboDeliveryMissionControlError(
                "state machine phase sequence must match mission contract sequence"
            )
        expected = tuple(
            zip(self.phase_sequence[:-1], self.phase_sequence[1:], strict=True)
        )
        if self.transitions != expected:
            raise PX4GazeboDeliveryMissionControlError(
                "state machine transitions must be contiguous and deterministic"
            )
        expected_statuses = tuple(PX4GazeboDeliveryPhaseRuntimeStatus)
        if self.allowed_runtime_statuses != expected_statuses:
            raise PX4GazeboDeliveryMissionControlError(
                "state machine must artifact every runtime status"
            )
        if self.phase_runtime_statuses:
            phases = tuple(item[0] for item in self.phase_runtime_statuses)
            if phases != DEFAULT_MISSION_PHASE_SEQUENCE:
                raise PX4GazeboDeliveryMissionControlError(
                    "state machine phase runtime statuses must cover every phase in order"
                )
        return self


class PX4GazeboDeliveryMissionHealthSnapshot(_MissionSafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_DELIVERY_MISSION_HEALTH_SNAPSHOT_SCHEMA_VERSION
    ] = PX4_GAZEBO_DELIVERY_MISSION_HEALTH_SNAPSHOT_SCHEMA_VERSION
    snapshot_id: str
    mission_contract_ref: str
    phase: PX4GazeboDeliveryMissionPhase
    snapshot_kind: PX4GazeboDeliveryHealthSnapshotKind = (
        PX4GazeboDeliveryHealthSnapshotKind.PHASE_EXIT
    )
    health_status: PX4GazeboDeliveryMissionHealthStatus
    px4_telemetry_correlated: bool
    gazebo_pose_correlated: bool
    route_progress_fresh: bool
    pose_observed: bool
    pose_xy_m: tuple[float, float] | None = None
    pose_z_m: float | None = None
    battery_margin_pct: float | None = Field(default=None, ge=0, le=100)
    heartbeat_observed: bool | None = None
    px4_mode: str | None = None
    armed: bool | None = None
    vehicle_ref: str = Field(min_length=1)
    blocked_reasons: tuple[str, ...] = ()
    observed_at: datetime

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_blocked(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("pose_xy_m", mode="before")
    @classmethod
    def _coerce_pose_xy(cls, value: Any) -> tuple[float, float] | None:
        if value is None:
            return None
        if len(value) != 2:
            raise PX4GazeboDeliveryMissionControlError(
                "health snapshot pose_xy_m must have two values"
            )
        return (float(value[0]), float(value[1]))

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_health(self) -> "PX4GazeboDeliveryMissionHealthSnapshot":
        ready = (
            self.px4_telemetry_correlated
            and self.gazebo_pose_correlated
            and self.route_progress_fresh
            and self.pose_observed
            and self.pose_xy_m is not None
            and self.pose_z_m is not None
            and self.battery_margin_pct is not None
            and self.battery_margin_pct >= 10
            and self.heartbeat_observed is True
            and self.px4_mode is not None
            and self.armed is not None
        )
        if self.health_status == PX4GazeboDeliveryMissionHealthStatus.READY:
            if not ready or self.blocked_reasons:
                raise PX4GazeboDeliveryMissionControlError(
                    "ready mission health requires fresh correlated telemetry and no blocked reasons"
                )
        if (
            self.health_status == PX4GazeboDeliveryMissionHealthStatus.BLOCKED
            and not self.blocked_reasons
        ):
            raise PX4GazeboDeliveryMissionControlError(
                "blocked mission health requires blocked reasons"
            )
        return self


class PX4GazeboDeliveryPhaseGate(_MissionSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_DELIVERY_PHASE_GATE_SCHEMA_VERSION] = (
        PX4_GAZEBO_DELIVERY_PHASE_GATE_SCHEMA_VERSION
    )
    gate_id: str
    mission_contract_ref: str
    phase: PX4GazeboDeliveryMissionPhase
    health_snapshot_ref: str
    gate_status: PX4GazeboDeliveryPhaseGateStatus
    entry_conditions: tuple[str, ...]
    exit_conditions: tuple[str, ...]
    abort_conditions: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    evaluated_at: datetime

    @field_validator(
        "entry_conditions",
        "exit_conditions",
        "abort_conditions",
        "blocked_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_strings(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def _coerce_evaluated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_gate(self) -> "PX4GazeboDeliveryPhaseGate":
        if not self.entry_conditions or not self.exit_conditions:
            raise PX4GazeboDeliveryMissionControlError(
                "phase gate requires entry and exit conditions"
            )
        if (
            self.gate_status == PX4GazeboDeliveryPhaseGateStatus.PASSED
            and self.blocked_reasons
        ):
            raise PX4GazeboDeliveryMissionControlError(
                "passed phase gate cannot include blocked reasons"
            )
        if (
            self.gate_status == PX4GazeboDeliveryPhaseGateStatus.BLOCKED
            and not self.blocked_reasons
        ):
            raise PX4GazeboDeliveryMissionControlError(
                "blocked phase gate requires blocked reasons"
            )
        return self


class PX4GazeboDeliveryPhaseGateEvaluationResult(_MissionSafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_DELIVERY_PHASE_GATE_EVALUATION_RESULT_SCHEMA_VERSION
    ] = PX4_GAZEBO_DELIVERY_PHASE_GATE_EVALUATION_RESULT_SCHEMA_VERSION
    evaluation_id: str
    mission_contract_ref: str
    phase_gate_ref: str
    health_snapshot_ref: str
    phase: PX4GazeboDeliveryMissionPhase
    verdict: PX4GazeboDeliveryPhaseGateVerdict
    entry_preconditions: tuple[str, ...]
    exit_postconditions: tuple[str, ...]
    abort_conditions: tuple[str, ...] = ()
    unmet_conditions: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    data_missing: bool = False
    evaluated_at: datetime

    @field_validator(
        "entry_preconditions",
        "exit_postconditions",
        "abort_conditions",
        "unmet_conditions",
        "blocked_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_strings(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def _coerce_evaluated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_evaluation(self) -> "PX4GazeboDeliveryPhaseGateEvaluationResult":
        if not self.entry_preconditions or not self.exit_postconditions:
            raise PX4GazeboDeliveryMissionControlError(
                "phase gate evaluation requires entry and exit conditions"
            )
        if self.verdict == PX4GazeboDeliveryPhaseGateVerdict.PASS:
            if (
                self.data_missing
                or self.blocked_reasons
                or self.abort_conditions
                or self.unmet_conditions
            ):
                raise PX4GazeboDeliveryMissionControlError(
                    "pass phase gate evaluation requires complete data and no blocked or abort conditions"
                )
        if self.verdict == PX4GazeboDeliveryPhaseGateVerdict.BLOCKED:
            if not self.blocked_reasons or not self.unmet_conditions:
                raise PX4GazeboDeliveryMissionControlError(
                    "blocked phase gate evaluation requires blocked reasons and unmet conditions"
                )
            if self.abort_conditions:
                raise PX4GazeboDeliveryMissionControlError(
                    "blocked phase gate evaluation cannot include abort conditions"
                )
        if self.verdict == PX4GazeboDeliveryPhaseGateVerdict.ABORT:
            if (
                not self.abort_conditions
                or not self.blocked_reasons
                or not self.unmet_conditions
            ):
                raise PX4GazeboDeliveryMissionControlError(
                    "abort phase gate evaluation requires abort conditions, blocked reasons, and unmet conditions"
                )
        return self


class PX4GazeboDeliveryRecoveryPolicyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: PX4GazeboDeliveryMissionPhase
    failure_type: PX4GazeboDeliveryMissionFailureType
    recovery_action: PX4GazeboDeliveryMissionRecoveryAction
    operator_approval_required: bool


class PX4GazeboDeliveryRecoveryPolicyMatrix(_MissionSafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_DELIVERY_RECOVERY_POLICY_MATRIX_SCHEMA_VERSION
    ] = PX4_GAZEBO_DELIVERY_RECOVERY_POLICY_MATRIX_SCHEMA_VERSION
    policy_id: str
    mission_contract_ref: str
    entries: tuple[PX4GazeboDeliveryRecoveryPolicyEntry, ...]
    all_phase_failure_cells_explicit: Literal[True] = True
    default_fallback_allowed: Literal[False] = False
    recovery_command_sent: Literal[False] = False
    generated_at: datetime

    @field_validator("entries", mode="before")
    @classmethod
    def _coerce_entries(
        cls, value: Any
    ) -> tuple[PX4GazeboDeliveryRecoveryPolicyEntry, ...]:
        return tuple(
            (
                item
                if isinstance(item, PX4GazeboDeliveryRecoveryPolicyEntry)
                else PX4GazeboDeliveryRecoveryPolicyEntry.model_validate(item)
            )
            for item in value
        )

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_policy(self) -> "PX4GazeboDeliveryRecoveryPolicyMatrix":
        keys = {(entry.phase, entry.failure_type) for entry in self.entries}
        expected = {
            (phase, failure_type)
            for phase in DEFAULT_MISSION_PHASE_SEQUENCE
            for failure_type in PX4GazeboDeliveryMissionFailureType
        }
        if keys != expected:
            missing = sorted(
                f"{phase.value}:{failure_type.value}"
                for phase, failure_type in expected - keys
            )
            extra = sorted(
                f"{phase.value}:{failure_type.value}"
                for phase, failure_type in keys - expected
            )
            raise PX4GazeboDeliveryMissionControlError(
                "recovery policy matrix must explicitly cover every phase/failure cell; "
                f"missing={missing}; extra={extra}"
            )
        if len(keys) != len(self.entries):
            raise PX4GazeboDeliveryMissionControlError(
                "recovery policy matrix cannot include duplicate phase/failure cells"
            )
        for entry in self.entries:
            if (
                entry.recovery_action
                in {
                    PX4GazeboDeliveryMissionRecoveryAction.HOLD,
                    PX4GazeboDeliveryMissionRecoveryAction.LAND,
                    PX4GazeboDeliveryMissionRecoveryAction.RTL,
                }
                and not entry.operator_approval_required
            ):
                raise PX4GazeboDeliveryMissionControlError(
                    "active recovery actions require operator approval"
                )
        return self


class PX4GazeboDeliveryRecoveryDecision(_MissionSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION] = (
        PX4_GAZEBO_DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    )
    decision_id: str
    mission_contract_ref: str
    recovery_policy_ref: str
    policy_cell_ref: str
    phase: PX4GazeboDeliveryMissionPhase
    failure_type: PX4GazeboDeliveryMissionFailureType
    recovery_action: PX4GazeboDeliveryMissionRecoveryAction
    operator_approval_required: bool
    approval_queue_required: bool
    dispatch_deferred_to_approval_queue: bool
    recovery_dispatch_invoked: Literal[False] = False
    policy_cell_hit: Literal[True] = True
    default_fallback_used: Literal[False] = False
    decision_basis: tuple[str, ...]
    decided_at: datetime

    @field_validator("decision_basis", mode="before")
    @classmethod
    def _coerce_strings(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("decided_at", mode="before")
    @classmethod
    def _coerce_decided_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_decision(self) -> "PX4GazeboDeliveryRecoveryDecision":
        expected_cell = (
            f"{self.recovery_policy_ref}:{self.phase.value}:{self.failure_type.value}"
        )
        if self.policy_cell_ref != expected_cell:
            raise PX4GazeboDeliveryMissionControlError(
                "recovery decision policy cell ref must match phase/failure"
            )
        if "policy_matrix_cell_hit" not in self.decision_basis:
            raise PX4GazeboDeliveryMissionControlError(
                "recovery decision must record policy matrix cell hit"
            )
        if (
            self.recovery_action
            in {
                PX4GazeboDeliveryMissionRecoveryAction.HOLD,
                PX4GazeboDeliveryMissionRecoveryAction.LAND,
                PX4GazeboDeliveryMissionRecoveryAction.RTL,
            }
            and not self.operator_approval_required
        ):
            raise PX4GazeboDeliveryMissionControlError(
                "active recovery decisions require operator approval"
            )
        if (
            self.recovery_action
            == PX4GazeboDeliveryMissionRecoveryAction.OPERATOR_ESCALATION
        ):
            if not self.approval_queue_required:
                raise PX4GazeboDeliveryMissionControlError(
                    "operator escalation recovery decisions require approval queue"
                )
            if not self.dispatch_deferred_to_approval_queue:
                raise PX4GazeboDeliveryMissionControlError(
                    "operator escalation recovery decisions must defer dispatch to approval queue"
                )
        return self


class PX4GazeboDeliveryOperatorPhaseBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: PX4GazeboDeliveryMissionPhase
    autonomy_level: PX4GazeboDeliveryMissionAutonomyLevel
    autonomy_boundary: PX4GazeboDeliveryPhaseAutonomyBoundary
    operator_approval_required_before_dispatch: bool
    autonomous_execution_allowed: bool
    forbidden_actions: tuple[str, ...]
    stronger_execution_allowed: Literal[False] = False

    @field_validator("forbidden_actions", mode="before")
    @classmethod
    def _coerce_forbidden(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @model_validator(mode="after")
    def _validate_phase_boundary(self) -> "PX4GazeboDeliveryOperatorPhaseBoundary":
        required_forbidden = {
            "hardware_target",
            "mission_upload",
            "unbounded_setpoint_stream",
        }
        if not required_forbidden.issubset(set(self.forbidden_actions)):
            raise PX4GazeboDeliveryMissionControlError(
                "operator boundary forbidden actions must include hardware target, mission upload, and unbounded setpoint stream"
            )
        if (
            self.autonomy_boundary
            == PX4GazeboDeliveryPhaseAutonomyBoundary.REQUIRES_APPROVAL
            and not self.operator_approval_required_before_dispatch
        ):
            raise PX4GazeboDeliveryMissionControlError(
                "requires-approval phases must require operator approval before dispatch"
            )
        if (
            self.autonomy_boundary == PX4GazeboDeliveryPhaseAutonomyBoundary.FORBIDDEN
            and self.autonomous_execution_allowed
        ):
            raise PX4GazeboDeliveryMissionControlError(
                "forbidden phases cannot allow autonomous execution"
            )
        return self


class PX4GazeboDeliveryOperatorBoundary(_MissionSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_DELIVERY_OPERATOR_BOUNDARY_SCHEMA_VERSION] = (
        PX4_GAZEBO_DELIVERY_OPERATOR_BOUNDARY_SCHEMA_VERSION
    )
    boundary_id: str
    mission_contract_ref: str
    phase_boundaries: tuple[PX4GazeboDeliveryOperatorPhaseBoundary, ...]
    generated_at: datetime

    @field_validator("phase_boundaries", mode="before")
    @classmethod
    def _coerce_boundaries(
        cls, value: Any
    ) -> tuple[PX4GazeboDeliveryOperatorPhaseBoundary, ...]:
        return tuple(
            (
                item
                if isinstance(item, PX4GazeboDeliveryOperatorPhaseBoundary)
                else PX4GazeboDeliveryOperatorPhaseBoundary.model_validate(item)
            )
            for item in value
        )

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_boundary(self) -> "PX4GazeboDeliveryOperatorBoundary":
        phases = tuple(item.phase for item in self.phase_boundaries)
        if phases != DEFAULT_MISSION_PHASE_SEQUENCE:
            raise PX4GazeboDeliveryMissionControlError(
                "operator boundary must cover every mission phase in order"
            )
        for item in self.phase_boundaries:
            if (
                item.autonomy_level
                == PX4GazeboDeliveryMissionAutonomyLevel.APPROVAL_GATED_DISPATCH
                and not item.operator_approval_required_before_dispatch
            ):
                raise PX4GazeboDeliveryMissionControlError(
                    "approval-gated dispatch phases require operator approval boundary"
                )
        return self


class PX4GazeboDeliveryMissionPhaseEvidence(_MissionSafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_DELIVERY_MISSION_PHASE_EVIDENCE_SCHEMA_VERSION
    ] = PX4_GAZEBO_DELIVERY_MISSION_PHASE_EVIDENCE_SCHEMA_VERSION
    evidence_id: str
    mission_contract_ref: str
    phase: PX4GazeboDeliveryMissionPhase
    phase_status: Literal["observed", "blocked"]
    health_snapshot_ref: str
    phase_gate_ref: str
    observed_at: datetime
    blocked_reasons: tuple[str, ...] = ()

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_blocked(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_evidence(self) -> "PX4GazeboDeliveryMissionPhaseEvidence":
        if self.phase_status == "observed" and self.blocked_reasons:
            raise PX4GazeboDeliveryMissionControlError(
                "observed phase evidence cannot include blocked reasons"
            )
        if self.phase_status == "blocked" and not self.blocked_reasons:
            raise PX4GazeboDeliveryMissionControlError(
                "blocked phase evidence requires blocked reasons"
            )
        return self


class PX4GazeboDeliveryMissionRunnerResult(_MissionSafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_DELIVERY_MISSION_RUNNER_RESULT_SCHEMA_VERSION
    ] = PX4_GAZEBO_DELIVERY_MISSION_RUNNER_RESULT_SCHEMA_VERSION
    runner_result_id: str
    mission_contract_ref: str
    prepared_run_ref: str
    state_machine_ref: str
    recovery_policy_ref: str
    operator_boundary_ref: str
    final_status: PX4GazeboDeliveryMissionFinalStatus
    observed_phases: tuple[PX4GazeboDeliveryMissionPhase, ...]
    missing_phases: tuple[PX4GazeboDeliveryMissionPhase, ...]
    blocked_phase: PX4GazeboDeliveryMissionPhase | None = None
    blocked_reasons: tuple[str, ...] = ()
    health_snapshot_refs: tuple[str, ...]
    phase_gate_refs: tuple[str, ...]
    phase_gate_evaluation_refs: tuple[str, ...]
    recovery_decision_refs: tuple[str, ...] = ()
    phase_transition_event_refs: tuple[str, ...]
    inspection_ref: str | None = None
    phase_evidence_refs: tuple[str, ...]
    route_dispatch_refs: tuple[str, ...]
    route_completion_gate_refs: tuple[str, ...]
    multi_waypoint_smoke_observed: bool
    failure_branching_smoke_observed: bool
    waypoint_count: int = Field(ge=0)
    route_segment_count: int = Field(ge=0)
    dropoff_landing_error_m: float | None = Field(default=None, ge=0)
    mission_runner_api_version: Literal["v1"] = "v1"
    completed_at: datetime

    @field_validator("observed_phases", "missing_phases", mode="before")
    @classmethod
    def _coerce_phases(cls, value: Any) -> tuple[PX4GazeboDeliveryMissionPhase, ...]:
        return _ordered_phases(value)

    @field_validator(
        "blocked_reasons",
        "health_snapshot_refs",
        "phase_gate_refs",
        "phase_gate_evaluation_refs",
        "recovery_decision_refs",
        "phase_transition_event_refs",
        "phase_evidence_refs",
        "route_dispatch_refs",
        "route_completion_gate_refs",
        mode="before",
    )
    @classmethod
    def _coerce_strings(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_completed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_result(self) -> "PX4GazeboDeliveryMissionRunnerResult":
        if self.final_status == PX4GazeboDeliveryMissionFinalStatus.COMPLETED:
            if (
                self.missing_phases
                or self.blocked_phase is not None
                or self.blocked_reasons
            ):
                raise PX4GazeboDeliveryMissionControlError(
                    "completed mission runner cannot include missing phases or blocked reasons"
                )
            if tuple(self.observed_phases) != DEFAULT_MISSION_PHASE_SEQUENCE:
                raise PX4GazeboDeliveryMissionControlError(
                    "completed mission runner requires every semantic phase"
                )
        if self.final_status == PX4GazeboDeliveryMissionFinalStatus.BLOCKED:
            if self.blocked_phase is None or not self.blocked_reasons:
                raise PX4GazeboDeliveryMissionControlError(
                    "blocked mission runner requires blocked phase and reasons"
                )
        return self


class PX4GazeboDeliveryMissionReplayEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=0)
    t_relative_seconds: float = Field(ge=0)
    phase: PX4GazeboDeliveryMissionPhase
    event_type: Literal[
        "phase_observed", "phase_blocked", "mission_completed", "mission_blocked"
    ]
    artifact_ref: str
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _coerce_occurred_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class PX4GazeboDeliveryPhaseTransitionEvent(_MissionSafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_DELIVERY_PHASE_TRANSITION_EVENT_SCHEMA_VERSION
    ] = PX4_GAZEBO_DELIVERY_PHASE_TRANSITION_EVENT_SCHEMA_VERSION
    transition_event_id: str
    mission_contract_ref: str
    from_phase: PX4GazeboDeliveryMissionPhase | None = None
    to_phase: PX4GazeboDeliveryMissionPhase
    runtime_status: PX4GazeboDeliveryPhaseRuntimeStatus
    phase_gate_evaluation_ref: str | None = None
    recovery_decision_ref: str | None = None
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _coerce_occurred_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_transition(self) -> "PX4GazeboDeliveryPhaseTransitionEvent":
        if (
            self.runtime_status
            in {
                PX4GazeboDeliveryPhaseRuntimeStatus.GATE_BLOCKED,
                PX4GazeboDeliveryPhaseRuntimeStatus.ABORTED,
            }
            and not self.phase_gate_evaluation_ref
        ):
            raise PX4GazeboDeliveryMissionControlError(
                "blocked or aborted transition requires phase gate evaluation ref"
            )
        if (
            self.runtime_status == PX4GazeboDeliveryPhaseRuntimeStatus.RECOVERING
            and not self.recovery_decision_ref
        ):
            raise PX4GazeboDeliveryMissionControlError(
                "recovering transition requires recovery decision ref"
            )
        return self


class PX4GazeboDeliveryMissionReplayTimeline(_MissionSafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_DELIVERY_MISSION_REPLAY_TIMELINE_SCHEMA_VERSION
    ] = PX4_GAZEBO_DELIVERY_MISSION_REPLAY_TIMELINE_SCHEMA_VERSION
    replay_timeline_id: str
    mission_contract_ref: str
    runner_result_ref: str
    events: tuple[PX4GazeboDeliveryMissionReplayEvent, ...]
    deterministic_replay: Literal[True] = True
    final_status: PX4GazeboDeliveryMissionFinalStatus
    generated_at: datetime

    @field_validator("events", mode="before")
    @classmethod
    def _coerce_events(
        cls, value: Any
    ) -> tuple[PX4GazeboDeliveryMissionReplayEvent, ...]:
        return tuple(
            (
                item
                if isinstance(item, PX4GazeboDeliveryMissionReplayEvent)
                else PX4GazeboDeliveryMissionReplayEvent.model_validate(item)
            )
            for item in value
        )

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_timeline(self) -> "PX4GazeboDeliveryMissionReplayTimeline":
        if not self.events:
            raise PX4GazeboDeliveryMissionControlError("mission replay requires events")
        if tuple(event.sequence for event in self.events) != tuple(
            range(len(self.events))
        ):
            raise PX4GazeboDeliveryMissionControlError(
                "mission replay event sequence must be contiguous"
            )
        return self


class PX4GazeboDeliveryMissionPreparedRun(_MissionSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_DELIVERY_MISSION_PREPARED_RUN_SCHEMA_VERSION] = (
        PX4_GAZEBO_DELIVERY_MISSION_PREPARED_RUN_SCHEMA_VERSION
    )
    prepared_run_id: str
    mission_contract_ref: str
    state_machine_ref: str
    recovery_policy_ref: str
    operator_boundary_ref: str
    route_plan_refs: tuple[str, ...]
    waypoint_refs: tuple[str, ...]
    phase_gate_refs: tuple[str, ...]
    contract_refs_complete: Literal[True] = True
    prepared_at: datetime

    @field_validator(
        "route_plan_refs", "waypoint_refs", "phase_gate_refs", mode="before"
    )
    @classmethod
    def _coerce_strings(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("prepared_at", mode="before")
    @classmethod
    def _coerce_prepared_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_prepared(self) -> "PX4GazeboDeliveryMissionPreparedRun":
        refs: list[Any] = [
            self.mission_contract_ref,
            self.state_machine_ref,
            self.recovery_policy_ref,
            self.operator_boundary_ref,
            *self.route_plan_refs,
            *self.waypoint_refs,
            *self.phase_gate_refs,
        ]
        if any(_is_missing_ref(item) for item in refs):
            raise PX4GazeboDeliveryMissionControlError(
                "prepared mission run rejects null or missing refs"
            )
        if len(self.phase_gate_refs) != len(DEFAULT_MISSION_PHASE_SEQUENCE):
            raise PX4GazeboDeliveryMissionControlError(
                "prepared mission run requires a phase gate ref for every phase"
            )
        return self


class PX4GazeboDeliveryMissionInspection(_MissionSafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_DELIVERY_MISSION_INSPECTION_SCHEMA_VERSION] = (
        PX4_GAZEBO_DELIVERY_MISSION_INSPECTION_SCHEMA_VERSION
    )
    inspection_id: str
    runner_result_ref: str
    replay_timeline_ref: str
    final_status: PX4GazeboDeliveryMissionFinalStatus
    observed_phase_count: int = Field(ge=0)
    phase_gate_evaluation_count: int = Field(ge=0)
    recovery_decision_count: int = Field(ge=0)
    transition_event_count: int = Field(ge=0)
    dropoff_landing_error_m: float | None = Field(default=None, ge=0)
    inspected_at: datetime

    @field_validator("inspected_at", mode="before")
    @classmethod
    def _coerce_inspected_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class PX4GazeboDeliveryMissionGoldenCorpus(_MissionSafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_DELIVERY_MISSION_GOLDEN_CORPUS_SCHEMA_VERSION
    ] = PX4_GAZEBO_DELIVERY_MISSION_GOLDEN_CORPUS_SCHEMA_VERSION
    corpus_id: str
    case_ids: tuple[str, ...]
    required_coverage_labels: tuple[str, ...]
    required_artifact_schema_versions: tuple[str, ...]
    happy_path_case_id: str
    failure_branch_case_id: str
    generated_at: datetime

    @field_validator(
        "case_ids",
        "required_coverage_labels",
        "required_artifact_schema_versions",
        mode="before",
    )
    @classmethod
    def _coerce_strings(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_corpus(self) -> "PX4GazeboDeliveryMissionGoldenCorpus":
        required = {
            "multi_waypoint_happy_path",
            "failure_branching",
            "mission_replay_timeline",
            "no_hardware_target",
            "no_physical_execution",
        }
        if not required.issubset(set(self.required_coverage_labels)):
            raise PX4GazeboDeliveryMissionControlError(
                "mission golden corpus is missing required Part 1 coverage labels"
            )
        if (
            self.happy_path_case_id not in self.case_ids
            or self.failure_branch_case_id not in self.case_ids
        ):
            raise PX4GazeboDeliveryMissionControlError(
                "mission golden corpus must include happy and failure cases"
            )
        return self


def build_px4_gazebo_delivery_mission_contract(
    *,
    mission_name: str = "complex_px4_gazebo_delivery_mission_v1",
    pickup_pad_ref: str = "gazebo_pad:pickup",
    dropoff_pad_ref: str = "gazebo_pad:dropoff",
    package_ref: str = "delivery_package:simulated",
    route_plan_refs: Sequence[str],
    waypoint_refs: Sequence[str],
    now: datetime | None = None,
) -> PX4GazeboDeliveryMissionContract:
    generated_at = _utc(now)
    payload = {
        "mission_name": mission_name,
        "pickup_pad_ref": pickup_pad_ref,
        "dropoff_pad_ref": dropoff_pad_ref,
        "package_ref": package_ref,
        "route_plan_refs": list(route_plan_refs),
        "waypoint_refs": list(waypoint_refs),
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboDeliveryMissionContract(
        mission_contract_id=_stable_id("px4_gazebo_delivery_mission_contract", payload),
        mission_name=mission_name,
        pickup_pad_ref=pickup_pad_ref,
        dropoff_pad_ref=dropoff_pad_ref,
        package_ref=package_ref,
        route_plan_refs=route_plan_refs,
        waypoint_refs=waypoint_refs,
        required_phase_sequence=DEFAULT_MISSION_PHASE_SEQUENCE,
        generated_at=generated_at,
        metadata={"issue": 379, "parent_epic": 373, "part": 1},
    )


def build_px4_gazebo_delivery_phase_state_machine(
    *,
    mission_contract: PX4GazeboDeliveryMissionContract | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboDeliveryPhaseStateMachine:
    contract = (
        mission_contract
        if isinstance(mission_contract, PX4GazeboDeliveryMissionContract)
        else PX4GazeboDeliveryMissionContract.model_validate(dict(mission_contract))
    )
    generated_at = _utc(now)
    transitions = tuple(
        zip(
            DEFAULT_MISSION_PHASE_SEQUENCE[:-1],
            DEFAULT_MISSION_PHASE_SEQUENCE[1:],
            strict=True,
        )
    )
    payload = {
        "contract": contract.mission_contract_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboDeliveryPhaseStateMachine(
        state_machine_id=_stable_id("px4_gazebo_delivery_phase_state_machine", payload),
        mission_contract_ref=_mission_contract_ref(contract),
        phase_sequence=DEFAULT_MISSION_PHASE_SEQUENCE,
        transitions=transitions,
        phase_runtime_statuses=tuple(
            (phase, PX4GazeboDeliveryPhaseRuntimeStatus.PENDING)
            for phase in DEFAULT_MISSION_PHASE_SEQUENCE
        ),
        generated_at=generated_at,
    )


def build_px4_gazebo_delivery_recovery_policy_matrix(
    *,
    mission_contract: PX4GazeboDeliveryMissionContract | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboDeliveryRecoveryPolicyMatrix:
    contract = (
        mission_contract
        if isinstance(mission_contract, PX4GazeboDeliveryMissionContract)
        else PX4GazeboDeliveryMissionContract.model_validate(dict(mission_contract))
    )
    generated_at = _utc(now)
    entries = tuple(
        PX4GazeboDeliveryRecoveryPolicyEntry(
            phase=phase,
            failure_type=failure_type,
            recovery_action=_mission_recovery_action_for(phase, failure_type),
            operator_approval_required=_mission_recovery_action_for(phase, failure_type)
            in {
                PX4GazeboDeliveryMissionRecoveryAction.HOLD,
                PX4GazeboDeliveryMissionRecoveryAction.LAND,
                PX4GazeboDeliveryMissionRecoveryAction.RTL,
                PX4GazeboDeliveryMissionRecoveryAction.OPERATOR_ESCALATION,
            },
        )
        for phase in DEFAULT_MISSION_PHASE_SEQUENCE
        for failure_type in PX4GazeboDeliveryMissionFailureType
    )
    payload = {
        "contract": contract.mission_contract_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboDeliveryRecoveryPolicyMatrix(
        policy_id=_stable_id("px4_gazebo_delivery_recovery_policy_matrix", payload),
        mission_contract_ref=_mission_contract_ref(contract),
        entries=entries,
        generated_at=generated_at,
    )


def _mission_recovery_action_for(
    phase: PX4GazeboDeliveryMissionPhase,
    failure_type: PX4GazeboDeliveryMissionFailureType,
) -> PX4GazeboDeliveryMissionRecoveryAction:
    if phase == PX4GazeboDeliveryMissionPhase.COMPLETED:
        return PX4GazeboDeliveryMissionRecoveryAction.BLOCK
    if failure_type == PX4GazeboDeliveryMissionFailureType.POSE_DEVIATION:
        return PX4GazeboDeliveryMissionRecoveryAction.LAND
    if failure_type == PX4GazeboDeliveryMissionFailureType.BATTERY_LOW:
        return PX4GazeboDeliveryMissionRecoveryAction.RTL
    if failure_type in {
        PX4GazeboDeliveryMissionFailureType.LINK_LOSS,
        PX4GazeboDeliveryMissionFailureType.STALE_TELEMETRY,
    }:
        return PX4GazeboDeliveryMissionRecoveryAction.HOLD
    if failure_type in {
        PX4GazeboDeliveryMissionFailureType.GATE_BLOCKED,
        PX4GazeboDeliveryMissionFailureType.ACK_TIMEOUT,
    }:
        return PX4GazeboDeliveryMissionRecoveryAction.OPERATOR_ESCALATION
    return PX4GazeboDeliveryMissionRecoveryAction.BLOCK


def build_px4_gazebo_delivery_recovery_decision(
    *,
    recovery_policy_matrix: PX4GazeboDeliveryRecoveryPolicyMatrix | Mapping[str, Any],
    phase: PX4GazeboDeliveryMissionPhase | str,
    failure_type: PX4GazeboDeliveryMissionFailureType | str,
    now: datetime | None = None,
) -> PX4GazeboDeliveryRecoveryDecision:
    policy = (
        recovery_policy_matrix
        if isinstance(recovery_policy_matrix, PX4GazeboDeliveryRecoveryPolicyMatrix)
        else PX4GazeboDeliveryRecoveryPolicyMatrix.model_validate(
            dict(recovery_policy_matrix)
        )
    )
    phase_value = (
        phase
        if isinstance(phase, PX4GazeboDeliveryMissionPhase)
        else PX4GazeboDeliveryMissionPhase(str(phase))
    )
    failure_value = (
        failure_type
        if isinstance(failure_type, PX4GazeboDeliveryMissionFailureType)
        else PX4GazeboDeliveryMissionFailureType(str(failure_type))
    )
    matching = [
        item
        for item in policy.entries
        if item.phase == phase_value and item.failure_type == failure_value
    ]
    if len(matching) != 1:
        raise PX4GazeboDeliveryMissionControlError(
            "recovery decision requires exactly one explicit policy cell"
        )
    entry = matching[0]
    decided_at = _utc(now)
    payload = {
        "policy": policy.policy_id,
        "phase": phase_value.value,
        "failure_type": failure_value.value,
        "decided_at": decided_at.isoformat(),
    }
    return PX4GazeboDeliveryRecoveryDecision(
        decision_id=_stable_id("px4_gazebo_delivery_recovery_decision", payload),
        mission_contract_ref=policy.mission_contract_ref,
        recovery_policy_ref=_recovery_policy_ref(policy),
        policy_cell_ref=_recovery_policy_cell_ref(
            policy,
            phase=phase_value,
            failure_type=failure_value,
        ),
        phase=phase_value,
        failure_type=failure_value,
        recovery_action=entry.recovery_action,
        operator_approval_required=entry.operator_approval_required,
        approval_queue_required=(
            entry.recovery_action
            == PX4GazeboDeliveryMissionRecoveryAction.OPERATOR_ESCALATION
        ),
        dispatch_deferred_to_approval_queue=(
            entry.recovery_action
            == PX4GazeboDeliveryMissionRecoveryAction.OPERATOR_ESCALATION
        ),
        decision_basis=(
            "policy_matrix_cell_hit",
            f"phase:{phase_value.value}",
            f"failure_type:{failure_value.value}",
            f"recovery_action:{entry.recovery_action.value}",
        ),
        decided_at=decided_at,
    )


def build_px4_gazebo_delivery_operator_boundary(
    *,
    mission_contract: PX4GazeboDeliveryMissionContract | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboDeliveryOperatorBoundary:
    contract = (
        mission_contract
        if isinstance(mission_contract, PX4GazeboDeliveryMissionContract)
        else PX4GazeboDeliveryMissionContract.model_validate(dict(mission_contract))
    )
    generated_at = _utc(now)
    phase_boundaries = tuple(
        PX4GazeboDeliveryOperatorPhaseBoundary(
            phase=phase,
            autonomy_level=(
                PX4GazeboDeliveryMissionAutonomyLevel.READ_ONLY
                if phase
                in {
                    PX4GazeboDeliveryMissionPhase.PREFLIGHT,
                    PX4GazeboDeliveryMissionPhase.PICKUP_VERIFIED,
                    PX4GazeboDeliveryMissionPhase.DROPOFF_VERIFIED,
                    PX4GazeboDeliveryMissionPhase.COMPLETED,
                }
                else PX4GazeboDeliveryMissionAutonomyLevel.APPROVAL_GATED_DISPATCH
            ),
            autonomy_boundary=(
                PX4GazeboDeliveryPhaseAutonomyBoundary.AUTONOMOUS
                if phase
                in {
                    PX4GazeboDeliveryMissionPhase.PREFLIGHT,
                    PX4GazeboDeliveryMissionPhase.PICKUP_VERIFIED,
                    PX4GazeboDeliveryMissionPhase.DROPOFF_VERIFIED,
                    PX4GazeboDeliveryMissionPhase.COMPLETED,
                }
                else PX4GazeboDeliveryPhaseAutonomyBoundary.REQUIRES_APPROVAL
            ),
            operator_approval_required_before_dispatch=phase
            not in {
                PX4GazeboDeliveryMissionPhase.PREFLIGHT,
                PX4GazeboDeliveryMissionPhase.PICKUP_VERIFIED,
                PX4GazeboDeliveryMissionPhase.DROPOFF_VERIFIED,
                PX4GazeboDeliveryMissionPhase.COMPLETED,
            },
            autonomous_execution_allowed=phase
            in {
                PX4GazeboDeliveryMissionPhase.PREFLIGHT,
                PX4GazeboDeliveryMissionPhase.PICKUP_VERIFIED,
                PX4GazeboDeliveryMissionPhase.DROPOFF_VERIFIED,
                PX4GazeboDeliveryMissionPhase.COMPLETED,
            },
            forbidden_actions=(
                "hardware_target",
                "mission_upload",
                "unbounded_setpoint_stream",
            ),
        )
        for phase in DEFAULT_MISSION_PHASE_SEQUENCE
    )
    payload = {
        "contract": contract.mission_contract_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboDeliveryOperatorBoundary(
        boundary_id=_stable_id("px4_gazebo_delivery_operator_boundary", payload),
        mission_contract_ref=_mission_contract_ref(contract),
        phase_boundaries=phase_boundaries,
        generated_at=generated_at,
    )


def _build_health_snapshot(
    *,
    contract: PX4GazeboDeliveryMissionContract,
    phase: PX4GazeboDeliveryMissionPhase,
    blocked_reasons: Sequence[str],
    snapshot_kind: PX4GazeboDeliveryHealthSnapshotKind = (
        PX4GazeboDeliveryHealthSnapshotKind.PHASE_EXIT
    ),
    pose_xy_m: tuple[float, float] | None = (0.0, 0.0),
    pose_z_m: float | None = 0.0,
    battery_margin_pct: float | None = 32.0,
    heartbeat_observed: bool | None = True,
    px4_mode: str | None = "OFFBOARD",
    armed: bool | None = True,
    now: datetime,
) -> PX4GazeboDeliveryMissionHealthSnapshot:
    blocked = _ordered_strings(blocked_reasons)
    data_missing = (
        pose_xy_m is None
        or pose_z_m is None
        or battery_margin_pct is None
        or heartbeat_observed is None
        or px4_mode is None
        or armed is None
    )
    ready = not blocked and not data_missing
    payload = {
        "contract": contract.mission_contract_id,
        "phase": phase.value,
        "snapshot_kind": snapshot_kind.value,
        "blocked": blocked,
        "observed_at": now.isoformat(),
    }
    return PX4GazeboDeliveryMissionHealthSnapshot(
        snapshot_id=_stable_id("px4_gazebo_delivery_mission_health_snapshot", payload),
        mission_contract_ref=_mission_contract_ref(contract),
        phase=phase,
        snapshot_kind=snapshot_kind,
        health_status=(
            PX4GazeboDeliveryMissionHealthStatus.READY
            if ready
            else PX4GazeboDeliveryMissionHealthStatus.BLOCKED
        ),
        px4_telemetry_correlated=ready,
        gazebo_pose_correlated=ready,
        route_progress_fresh=ready,
        pose_observed=pose_xy_m is not None and pose_z_m is not None,
        pose_xy_m=pose_xy_m,
        pose_z_m=pose_z_m,
        battery_margin_pct=battery_margin_pct,
        heartbeat_observed=heartbeat_observed,
        px4_mode=px4_mode,
        armed=armed,
        vehicle_ref="gazebo_vehicle:x500_0",
        blocked_reasons=(
            _ordered_strings((*blocked, "data_missing")) if data_missing else blocked
        ),
        observed_at=now,
    )


def _build_phase_gate(
    *,
    contract: PX4GazeboDeliveryMissionContract,
    snapshot: PX4GazeboDeliveryMissionHealthSnapshot,
    blocked_reasons: Sequence[str],
    abort_conditions: Sequence[str] = (),
    now: datetime,
) -> PX4GazeboDeliveryPhaseGate:
    blocked = _ordered_strings(blocked_reasons)
    abort = _ordered_strings(abort_conditions)
    phase = snapshot.phase
    payload = {
        "contract": contract.mission_contract_id,
        "phase": phase.value,
        "blocked": blocked,
        "abort": abort,
        "evaluated_at": now.isoformat(),
    }
    return PX4GazeboDeliveryPhaseGate(
        gate_id=_stable_id("px4_gazebo_delivery_phase_gate", payload),
        mission_contract_ref=_mission_contract_ref(contract),
        phase=phase,
        health_snapshot_ref=_health_snapshot_ref(snapshot),
        gate_status=(
            PX4GazeboDeliveryPhaseGateStatus.PASSED
            if not blocked
            else PX4GazeboDeliveryPhaseGateStatus.BLOCKED
        ),
        entry_conditions=(f"{phase.value}:entry_ready",),
        exit_conditions=(f"{phase.value}:exit_evidence_observed",),
        abort_conditions=abort,
        blocked_reasons=blocked,
        evaluated_at=now,
    )


def build_px4_gazebo_delivery_phase_gate_evaluation_result(
    *,
    phase_gate: PX4GazeboDeliveryPhaseGate | Mapping[str, Any],
    health_snapshot: PX4GazeboDeliveryMissionHealthSnapshot | Mapping[str, Any],
    data_missing: bool = False,
    now: datetime | None = None,
) -> PX4GazeboDeliveryPhaseGateEvaluationResult:
    gate = (
        phase_gate
        if isinstance(phase_gate, PX4GazeboDeliveryPhaseGate)
        else PX4GazeboDeliveryPhaseGate.model_validate(dict(phase_gate))
    )
    snapshot = (
        health_snapshot
        if isinstance(health_snapshot, PX4GazeboDeliveryMissionHealthSnapshot)
        else PX4GazeboDeliveryMissionHealthSnapshot.model_validate(
            dict(health_snapshot)
        )
    )
    if gate.health_snapshot_ref != _health_snapshot_ref(snapshot):
        raise PX4GazeboDeliveryMissionControlError(
            "phase gate evaluation requires matching health snapshot"
        )
    if gate.phase != snapshot.phase:
        raise PX4GazeboDeliveryMissionControlError(
            "phase gate evaluation phase must match health snapshot phase"
        )
    data_missing_detected = (
        data_missing
        or snapshot.pose_xy_m is None
        or snapshot.pose_z_m is None
        or snapshot.battery_margin_pct is None
        or snapshot.heartbeat_observed is None
        or snapshot.px4_mode is None
        or snapshot.armed is None
    )
    blocked_reasons = gate.blocked_reasons
    if data_missing_detected and "data_missing" not in blocked_reasons:
        blocked_reasons = (*blocked_reasons, "data_missing")
    unmet_conditions = _ordered_strings((*blocked_reasons, *gate.abort_conditions))
    verdict = (
        PX4GazeboDeliveryPhaseGateVerdict.ABORT
        if gate.abort_conditions
        else (
            PX4GazeboDeliveryPhaseGateVerdict.BLOCKED
            if blocked_reasons or data_missing_detected
            else PX4GazeboDeliveryPhaseGateVerdict.PASS
        )
    )
    evaluated_at = _utc(now)
    payload = {
        "gate": gate.gate_id,
        "verdict": verdict.value,
        "data_missing": data_missing,
        "evaluated_at": evaluated_at.isoformat(),
    }
    return PX4GazeboDeliveryPhaseGateEvaluationResult(
        evaluation_id=_stable_id(
            "px4_gazebo_delivery_phase_gate_evaluation_result", payload
        ),
        mission_contract_ref=gate.mission_contract_ref,
        phase_gate_ref=_phase_gate_ref(gate),
        health_snapshot_ref=gate.health_snapshot_ref,
        phase=gate.phase,
        verdict=verdict,
        entry_preconditions=gate.entry_conditions,
        exit_postconditions=gate.exit_conditions,
        abort_conditions=gate.abort_conditions,
        unmet_conditions=unmet_conditions,
        blocked_reasons=blocked_reasons,
        data_missing=data_missing_detected,
        evaluated_at=evaluated_at,
    )


def _build_phase_evidence(
    *,
    contract: PX4GazeboDeliveryMissionContract,
    snapshot: PX4GazeboDeliveryMissionHealthSnapshot,
    gate: PX4GazeboDeliveryPhaseGate,
    blocked_reasons: Sequence[str],
    now: datetime,
) -> PX4GazeboDeliveryMissionPhaseEvidence:
    blocked = _ordered_strings(blocked_reasons)
    payload = {
        "contract": contract.mission_contract_id,
        "phase": gate.phase.value,
        "blocked": blocked,
        "observed_at": now.isoformat(),
    }
    return PX4GazeboDeliveryMissionPhaseEvidence(
        evidence_id=_stable_id("px4_gazebo_delivery_mission_phase_evidence", payload),
        mission_contract_ref=_mission_contract_ref(contract),
        phase=gate.phase,
        phase_status="blocked" if blocked else "observed",
        health_snapshot_ref=_health_snapshot_ref(snapshot),
        phase_gate_ref=_phase_gate_ref(gate),
        observed_at=now,
        blocked_reasons=blocked,
    )


def _build_phase_transition_event(
    *,
    contract: PX4GazeboDeliveryMissionContract,
    from_phase: PX4GazeboDeliveryMissionPhase | None,
    to_phase: PX4GazeboDeliveryMissionPhase,
    runtime_status: PX4GazeboDeliveryPhaseRuntimeStatus,
    phase_gate_evaluation_ref: str | None,
    recovery_decision_ref: str | None = None,
    now: datetime,
) -> PX4GazeboDeliveryPhaseTransitionEvent:
    payload = {
        "contract": contract.mission_contract_id,
        "from_phase": None if from_phase is None else from_phase.value,
        "to_phase": to_phase.value,
        "runtime_status": runtime_status.value,
        "occurred_at": now.isoformat(),
    }
    return PX4GazeboDeliveryPhaseTransitionEvent(
        transition_event_id=_stable_id(
            "px4_gazebo_delivery_phase_transition_event", payload
        ),
        mission_contract_ref=_mission_contract_ref(contract),
        from_phase=from_phase,
        to_phase=to_phase,
        runtime_status=runtime_status,
        phase_gate_evaluation_ref=phase_gate_evaluation_ref,
        recovery_decision_ref=recovery_decision_ref,
        occurred_at=now,
    )


def prepare_px4_gazebo_delivery_mission_v1(
    *,
    mission_contract: PX4GazeboDeliveryMissionContract | Mapping[str, Any],
    phase_state_machine: (
        PX4GazeboDeliveryPhaseStateMachine | Mapping[str, Any] | None
    ) = None,
    recovery_policy_matrix: (
        PX4GazeboDeliveryRecoveryPolicyMatrix | Mapping[str, Any] | None
    ) = None,
    operator_boundary: (
        PX4GazeboDeliveryOperatorBoundary | Mapping[str, Any] | None
    ) = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    contract = (
        mission_contract
        if isinstance(mission_contract, PX4GazeboDeliveryMissionContract)
        else PX4GazeboDeliveryMissionContract.model_validate(dict(mission_contract))
    )
    refs: list[Any] = [
        *contract.route_plan_refs,
        *contract.waypoint_refs,
        *contract.phase_gate_refs,
        contract.recovery_policy_ref,
        contract.operator_boundary_ref,
    ]
    if any(_is_missing_ref(item) for item in refs):
        raise PX4GazeboDeliveryMissionControlError(
            "runner prepare rejects null refs, missing gates, missing policy, or missing boundary"
        )
    prepared_at = _utc(now)
    machine = (
        build_px4_gazebo_delivery_phase_state_machine(
            mission_contract=contract,
            now=prepared_at,
        )
        if phase_state_machine is None
        else (
            phase_state_machine
            if isinstance(phase_state_machine, PX4GazeboDeliveryPhaseStateMachine)
            else PX4GazeboDeliveryPhaseStateMachine.model_validate(
                dict(phase_state_machine)
            )
        )
    )
    policy = (
        build_px4_gazebo_delivery_recovery_policy_matrix(
            mission_contract=contract,
            now=prepared_at,
        )
        if recovery_policy_matrix is None
        else (
            recovery_policy_matrix
            if isinstance(recovery_policy_matrix, PX4GazeboDeliveryRecoveryPolicyMatrix)
            else PX4GazeboDeliveryRecoveryPolicyMatrix.model_validate(
                dict(recovery_policy_matrix)
            )
        )
    )
    boundary = (
        build_px4_gazebo_delivery_operator_boundary(
            mission_contract=contract,
            now=prepared_at,
        )
        if operator_boundary is None
        else (
            operator_boundary
            if isinstance(operator_boundary, PX4GazeboDeliveryOperatorBoundary)
            else PX4GazeboDeliveryOperatorBoundary.model_validate(
                dict(operator_boundary)
            )
        )
    )
    expected_contract_ref = _mission_contract_ref(contract)
    if machine.mission_contract_ref != expected_contract_ref:
        raise PX4GazeboDeliveryMissionControlError(
            "runner prepare rejects state machine contract mismatch"
        )
    if policy.mission_contract_ref != expected_contract_ref:
        raise PX4GazeboDeliveryMissionControlError(
            "runner prepare rejects recovery policy contract mismatch"
        )
    if boundary.mission_contract_ref != expected_contract_ref:
        raise PX4GazeboDeliveryMissionControlError(
            "runner prepare rejects operator boundary contract mismatch"
        )
    payload = {
        "contract": contract.mission_contract_id,
        "state_machine": machine.state_machine_id,
        "policy": policy.policy_id,
        "boundary": boundary.boundary_id,
        "prepared_at": prepared_at.isoformat(),
    }
    prepared = PX4GazeboDeliveryMissionPreparedRun(
        prepared_run_id=_stable_id("px4_gazebo_delivery_mission_prepared_run", payload),
        mission_contract_ref=_mission_contract_ref(contract),
        state_machine_ref=_state_machine_ref(machine),
        recovery_policy_ref=_recovery_policy_ref(policy),
        operator_boundary_ref=_operator_boundary_ref(boundary),
        route_plan_refs=contract.route_plan_refs,
        waypoint_refs=contract.waypoint_refs,
        phase_gate_refs=contract.phase_gate_refs,
        prepared_at=prepared_at,
    )
    return {
        "mission_contract": contract,
        "prepared_run": prepared,
        "phase_state_machine": machine,
        "recovery_policy_matrix": policy,
        "operator_boundary": boundary,
    }


def inspect_px4_gazebo_delivery_mission_v1(
    *,
    runner_result: "PX4GazeboDeliveryMissionRunnerResult" | Mapping[str, Any],
    replay_timeline: "PX4GazeboDeliveryMissionReplayTimeline" | Mapping[str, Any],
    phase_gate_evaluations: Sequence[
        "PX4GazeboDeliveryPhaseGateEvaluationResult" | Mapping[str, Any]
    ],
    recovery_decisions: Sequence[
        "PX4GazeboDeliveryRecoveryDecision" | Mapping[str, Any]
    ],
    phase_transition_events: Sequence[
        "PX4GazeboDeliveryPhaseTransitionEvent" | Mapping[str, Any]
    ],
    dropoff_landing_error_m: float | None = None,
    now: datetime | None = None,
) -> PX4GazeboDeliveryMissionInspection:
    runner = (
        runner_result
        if isinstance(runner_result, PX4GazeboDeliveryMissionRunnerResult)
        else PX4GazeboDeliveryMissionRunnerResult.model_validate(dict(runner_result))
    )
    replay = (
        replay_timeline
        if isinstance(replay_timeline, PX4GazeboDeliveryMissionReplayTimeline)
        else PX4GazeboDeliveryMissionReplayTimeline.model_validate(
            dict(replay_timeline)
        )
    )
    inspected_at = _utc(now)
    payload = {
        "runner": runner.runner_result_id,
        "replay": replay.replay_timeline_id,
        "inspected_at": inspected_at.isoformat(),
    }
    return PX4GazeboDeliveryMissionInspection(
        inspection_id=_stable_id("px4_gazebo_delivery_mission_inspection", payload),
        runner_result_ref=(
            f"px4_gazebo_delivery_mission_runner_v1_result:{runner.runner_result_id}"
        ),
        replay_timeline_ref=(
            "px4_gazebo_delivery_mission_replay_timeline:"
            f"{replay.replay_timeline_id}"
        ),
        final_status=runner.final_status,
        observed_phase_count=len(runner.observed_phases),
        phase_gate_evaluation_count=len(phase_gate_evaluations),
        recovery_decision_count=len(recovery_decisions),
        transition_event_count=len(phase_transition_events),
        dropoff_landing_error_m=dropoff_landing_error_m,
        inspected_at=inspected_at,
    )


def execute_px4_gazebo_delivery_mission_v1(
    **kwargs: Any,
) -> dict[str, Any]:
    return run_px4_gazebo_delivery_mission_v1(**kwargs)


def run_px4_gazebo_delivery_mission_v1(
    *,
    mission_contract: PX4GazeboDeliveryMissionContract | Mapping[str, Any],
    failure_phase: PX4GazeboDeliveryMissionPhase | str | None = None,
    failure_type: PX4GazeboDeliveryMissionFailureType | str | None = None,
    route_dispatch_refs: Sequence[str] = (),
    route_completion_gate_refs: Sequence[str] = (),
    dropoff_landing_error_m: float | None = 0.32,
    now: datetime | None = None,
) -> dict[str, Any]:
    contract = (
        mission_contract
        if isinstance(mission_contract, PX4GazeboDeliveryMissionContract)
        else PX4GazeboDeliveryMissionContract.model_validate(dict(mission_contract))
    )
    observed_at = _utc(now)
    prepared_artifacts = prepare_px4_gazebo_delivery_mission_v1(
        mission_contract=contract,
        now=observed_at,
    )
    failure_phase_value = (
        None
        if failure_phase is None
        else (
            failure_phase
            if isinstance(failure_phase, PX4GazeboDeliveryMissionPhase)
            else PX4GazeboDeliveryMissionPhase(str(failure_phase))
        )
    )
    failure_type_value = (
        None
        if failure_type is None
        else (
            failure_type
            if isinstance(failure_type, PX4GazeboDeliveryMissionFailureType)
            else PX4GazeboDeliveryMissionFailureType(str(failure_type))
        )
    )
    prepared = prepared_artifacts["prepared_run"]
    machine = prepared_artifacts["phase_state_machine"]
    policy = prepared_artifacts["recovery_policy_matrix"]
    boundary = prepared_artifacts["operator_boundary"]
    snapshots: list[PX4GazeboDeliveryMissionHealthSnapshot] = []
    gates: list[PX4GazeboDeliveryPhaseGate] = []
    gate_evaluations: list[PX4GazeboDeliveryPhaseGateEvaluationResult] = []
    recovery_decisions: list[PX4GazeboDeliveryRecoveryDecision] = []
    transition_events: list[PX4GazeboDeliveryPhaseTransitionEvent] = []
    evidences: list[PX4GazeboDeliveryMissionPhaseEvidence] = []
    observed_phases: list[PX4GazeboDeliveryMissionPhase] = []
    blocked_reasons: tuple[str, ...] = ()
    blocked_phase: PX4GazeboDeliveryMissionPhase | None = None

    for index, phase in enumerate(DEFAULT_MISSION_PHASE_SEQUENCE):
        previous_phase = DEFAULT_MISSION_PHASE_SEQUENCE[index - 1] if index else None
        entry_snapshot = _build_health_snapshot(
            contract=contract,
            phase=phase,
            snapshot_kind=PX4GazeboDeliveryHealthSnapshotKind.PHASE_ENTRY,
            blocked_reasons=(),
            now=observed_at,
        )
        snapshots.append(entry_snapshot)
        transition_events.append(
            _build_phase_transition_event(
                contract=contract,
                from_phase=previous_phase,
                to_phase=phase,
                runtime_status=PX4GazeboDeliveryPhaseRuntimeStatus.ENTERING,
                phase_gate_evaluation_ref=None,
                now=observed_at,
            )
        )
        transition_events.append(
            _build_phase_transition_event(
                contract=contract,
                from_phase=phase,
                to_phase=phase,
                runtime_status=PX4GazeboDeliveryPhaseRuntimeStatus.RUNNING,
                phase_gate_evaluation_ref=None,
                now=observed_at,
            )
        )
        phase_blocked: tuple[str, ...] = ()
        if failure_phase_value == phase:
            reason = (
                "mission_phase_gate_blocked"
                if failure_type_value is None
                else f"mission_{failure_type_value.value}"
            )
            phase_blocked = (reason,)
        abort_conditions = (
            (f"abort_on_{failure_type_value.value}",)
            if failure_phase_value == phase
            and failure_type_value
            in {
                PX4GazeboDeliveryMissionFailureType.ACK_TIMEOUT,
                PX4GazeboDeliveryMissionFailureType.BATTERY_LOW,
                PX4GazeboDeliveryMissionFailureType.LINK_LOSS,
                PX4GazeboDeliveryMissionFailureType.POSE_DEVIATION,
            }
            else ()
        )
        snapshot = _build_health_snapshot(
            contract=contract,
            phase=phase,
            snapshot_kind=PX4GazeboDeliveryHealthSnapshotKind.PHASE_EXIT,
            blocked_reasons=phase_blocked,
            battery_margin_pct=(
                6.0
                if failure_phase_value == phase
                and failure_type_value
                == PX4GazeboDeliveryMissionFailureType.BATTERY_LOW
                else 32.0
            ),
            heartbeat_observed=(
                False
                if failure_phase_value == phase
                and failure_type_value == PX4GazeboDeliveryMissionFailureType.LINK_LOSS
                else True
            ),
            now=observed_at,
        )
        gate = _build_phase_gate(
            contract=contract,
            snapshot=snapshot,
            blocked_reasons=phase_blocked,
            abort_conditions=abort_conditions,
            now=observed_at,
        )
        evaluation = build_px4_gazebo_delivery_phase_gate_evaluation_result(
            phase_gate=gate,
            health_snapshot=snapshot,
            now=observed_at,
        )
        evidence = _build_phase_evidence(
            contract=contract,
            snapshot=snapshot,
            gate=gate,
            blocked_reasons=phase_blocked,
            now=observed_at,
        )
        snapshots.append(snapshot)
        gates.append(gate)
        gate_evaluations.append(evaluation)
        evidences.append(evidence)
        if phase_blocked:
            blocked_transition_status = (
                PX4GazeboDeliveryPhaseRuntimeStatus.ABORTED
                if evaluation.verdict == PX4GazeboDeliveryPhaseGateVerdict.ABORT
                else PX4GazeboDeliveryPhaseRuntimeStatus.GATE_BLOCKED
            )
            if failure_type_value is not None:
                recovery_decisions.append(
                    build_px4_gazebo_delivery_recovery_decision(
                        recovery_policy_matrix=policy,
                        phase=phase,
                        failure_type=failure_type_value,
                        now=observed_at,
                    )
                )
            transition_events.append(
                _build_phase_transition_event(
                    contract=contract,
                    from_phase=phase,
                    to_phase=phase,
                    runtime_status=blocked_transition_status,
                    phase_gate_evaluation_ref=_phase_gate_evaluation_ref(evaluation),
                    recovery_decision_ref=(
                        None
                        if not recovery_decisions
                        else _recovery_decision_ref(recovery_decisions[-1])
                    ),
                    now=observed_at,
                )
            )
            if recovery_decisions:
                transition_events.append(
                    _build_phase_transition_event(
                        contract=contract,
                        from_phase=phase,
                        to_phase=phase,
                        runtime_status=PX4GazeboDeliveryPhaseRuntimeStatus.RECOVERING,
                        phase_gate_evaluation_ref=_phase_gate_evaluation_ref(
                            evaluation
                        ),
                        recovery_decision_ref=_recovery_decision_ref(
                            recovery_decisions[-1]
                        ),
                        now=observed_at,
                    )
                )
            blocked_phase = phase
            blocked_reasons = phase_blocked
            break
        observed_phases.append(phase)
        transition_events.append(
            _build_phase_transition_event(
                contract=contract,
                from_phase=phase,
                to_phase=phase,
                runtime_status=PX4GazeboDeliveryPhaseRuntimeStatus.COMPLETED,
                phase_gate_evaluation_ref=_phase_gate_evaluation_ref(evaluation),
                now=observed_at,
            )
        )

    missing_phases = tuple(
        phase
        for phase in DEFAULT_MISSION_PHASE_SEQUENCE
        if phase not in observed_phases
    )
    final_status = (
        PX4GazeboDeliveryMissionFinalStatus.COMPLETED
        if not missing_phases
        else PX4GazeboDeliveryMissionFinalStatus.BLOCKED
    )
    payload = {
        "contract": contract.mission_contract_id,
        "final_status": final_status.value,
        "observed_phases": [phase.value for phase in observed_phases],
        "blocked_phase": None if blocked_phase is None else blocked_phase.value,
        "completed_at": observed_at.isoformat(),
    }
    runner = PX4GazeboDeliveryMissionRunnerResult(
        runner_result_id=_stable_id("px4_gazebo_delivery_mission_runner_v1", payload),
        mission_contract_ref=_mission_contract_ref(contract),
        prepared_run_ref=(
            f"px4_gazebo_delivery_mission_prepared_run:{prepared.prepared_run_id}"
        ),
        state_machine_ref=_state_machine_ref(machine),
        recovery_policy_ref=_recovery_policy_ref(policy),
        operator_boundary_ref=_operator_boundary_ref(boundary),
        final_status=final_status,
        observed_phases=tuple(observed_phases),
        missing_phases=missing_phases,
        blocked_phase=blocked_phase,
        blocked_reasons=blocked_reasons,
        health_snapshot_refs=tuple(_health_snapshot_ref(item) for item in snapshots),
        phase_gate_refs=tuple(_phase_gate_ref(item) for item in gates),
        phase_gate_evaluation_refs=tuple(
            _phase_gate_evaluation_ref(item) for item in gate_evaluations
        ),
        recovery_decision_refs=tuple(
            _recovery_decision_ref(item) for item in recovery_decisions
        ),
        phase_transition_event_refs=tuple(
            "px4_gazebo_delivery_phase_transition_event:" f"{item.transition_event_id}"
            for item in transition_events
        ),
        phase_evidence_refs=tuple(_phase_evidence_ref(item) for item in evidences),
        route_dispatch_refs=route_dispatch_refs,
        route_completion_gate_refs=route_completion_gate_refs,
        multi_waypoint_smoke_observed=True,
        failure_branching_smoke_observed=final_status
        == PX4GazeboDeliveryMissionFinalStatus.BLOCKED,
        waypoint_count=len(contract.waypoint_refs),
        route_segment_count=max(
            len(route_dispatch_refs), len(route_completion_gate_refs)
        ),
        dropoff_landing_error_m=(
            dropoff_landing_error_m
            if final_status == PX4GazeboDeliveryMissionFinalStatus.COMPLETED
            else None
        ),
        completed_at=observed_at,
    )
    events = [
        PX4GazeboDeliveryMissionReplayEvent(
            sequence=index,
            t_relative_seconds=float(index),
            phase=evidence.phase,
            event_type=(
                "phase_blocked"
                if evidence.phase_status == "blocked"
                else "phase_observed"
            ),
            artifact_ref=_phase_evidence_ref(evidence),
            occurred_at=observed_at,
        )
        for index, evidence in enumerate(evidences)
    ]
    events.append(
        PX4GazeboDeliveryMissionReplayEvent(
            sequence=len(events),
            t_relative_seconds=float(len(events)),
            phase=(
                observed_phases[-1]
                if observed_phases
                else DEFAULT_MISSION_PHASE_SEQUENCE[0]
            ),
            event_type=(
                "mission_completed"
                if final_status == PX4GazeboDeliveryMissionFinalStatus.COMPLETED
                else "mission_blocked"
            ),
            artifact_ref=f"px4_gazebo_delivery_mission_runner_v1_result:{runner.runner_result_id}",
            occurred_at=observed_at,
        )
    )
    replay_payload = {
        "contract": contract.mission_contract_id,
        "runner": runner.runner_result_id,
        "final_status": final_status.value,
        "generated_at": observed_at.isoformat(),
    }
    replay = PX4GazeboDeliveryMissionReplayTimeline(
        replay_timeline_id=_stable_id(
            "px4_gazebo_delivery_mission_replay_timeline", replay_payload
        ),
        mission_contract_ref=_mission_contract_ref(contract),
        runner_result_ref=f"px4_gazebo_delivery_mission_runner_v1_result:{runner.runner_result_id}",
        events=tuple(events),
        final_status=final_status,
        generated_at=observed_at,
    )
    inspection = inspect_px4_gazebo_delivery_mission_v1(
        runner_result=runner,
        replay_timeline=replay,
        phase_gate_evaluations=gate_evaluations,
        recovery_decisions=recovery_decisions,
        phase_transition_events=transition_events,
        dropoff_landing_error_m=runner.dropoff_landing_error_m,
        now=observed_at,
    )
    runner_payload = runner.model_dump(mode="json")
    runner_payload["inspection_ref"] = (
        f"px4_gazebo_delivery_mission_inspection:{inspection.inspection_id}"
    )
    runner = PX4GazeboDeliveryMissionRunnerResult.model_validate(runner_payload)
    return {
        "mission_contract": contract,
        "prepared_run": prepared,
        "phase_state_machine": machine,
        "recovery_policy_matrix": policy,
        "operator_boundary": boundary,
        "health_snapshots": tuple(snapshots),
        "phase_gates": tuple(gates),
        "phase_gate_evaluations": tuple(gate_evaluations),
        "recovery_decisions": tuple(recovery_decisions),
        "phase_transition_events": tuple(transition_events),
        "phase_evidence": tuple(evidences),
        "runner_result": runner,
        "replay_timeline": replay,
        "mission_inspection": inspection,
    }


def build_px4_gazebo_delivery_mission_golden_corpus(
    *,
    happy_runner_result: PX4GazeboDeliveryMissionRunnerResult | Mapping[str, Any],
    failure_runner_result: PX4GazeboDeliveryMissionRunnerResult | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboDeliveryMissionGoldenCorpus:
    happy = (
        happy_runner_result
        if isinstance(happy_runner_result, PX4GazeboDeliveryMissionRunnerResult)
        else PX4GazeboDeliveryMissionRunnerResult.model_validate(
            dict(happy_runner_result)
        )
    )
    failure = (
        failure_runner_result
        if isinstance(failure_runner_result, PX4GazeboDeliveryMissionRunnerResult)
        else PX4GazeboDeliveryMissionRunnerResult.model_validate(
            dict(failure_runner_result)
        )
    )
    if happy.final_status != PX4GazeboDeliveryMissionFinalStatus.COMPLETED:
        raise PX4GazeboDeliveryMissionControlError(
            "mission golden corpus happy case must be completed"
        )
    if failure.final_status != PX4GazeboDeliveryMissionFinalStatus.BLOCKED:
        raise PX4GazeboDeliveryMissionControlError(
            "mission golden corpus failure case must be blocked"
        )
    generated_at = _utc(now)
    case_ids = (
        f"happy:{happy.runner_result_id}",
        f"failure:{failure.runner_result_id}",
    )
    payload = {"case_ids": case_ids, "generated_at": generated_at.isoformat()}
    return PX4GazeboDeliveryMissionGoldenCorpus(
        corpus_id=_stable_id("px4_gazebo_delivery_mission_golden_corpus", payload),
        case_ids=case_ids,
        required_coverage_labels=(
            "multi_waypoint_happy_path",
            "failure_branching",
            "mission_replay_timeline",
            "no_hardware_target",
            "no_physical_execution",
        ),
        required_artifact_schema_versions=(
            PX4_GAZEBO_DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
            PX4_GAZEBO_DELIVERY_PHASE_STATE_MACHINE_SCHEMA_VERSION,
            PX4_GAZEBO_DELIVERY_MISSION_HEALTH_SNAPSHOT_SCHEMA_VERSION,
            PX4_GAZEBO_DELIVERY_PHASE_GATE_SCHEMA_VERSION,
            PX4_GAZEBO_DELIVERY_PHASE_GATE_EVALUATION_RESULT_SCHEMA_VERSION,
            PX4_GAZEBO_DELIVERY_RECOVERY_POLICY_MATRIX_SCHEMA_VERSION,
            PX4_GAZEBO_DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION,
            PX4_GAZEBO_DELIVERY_OPERATOR_BOUNDARY_SCHEMA_VERSION,
            PX4_GAZEBO_DELIVERY_MISSION_PHASE_EVIDENCE_SCHEMA_VERSION,
            PX4_GAZEBO_DELIVERY_MISSION_RUNNER_RESULT_SCHEMA_VERSION,
            PX4_GAZEBO_DELIVERY_MISSION_REPLAY_TIMELINE_SCHEMA_VERSION,
        ),
        happy_path_case_id=case_ids[0],
        failure_branch_case_id=case_ids[1],
        generated_at=generated_at,
    )


def attach_px4_gazebo_delivery_mission_v1_task(
    task_id: str,
    *,
    mission_artifacts: Mapping[str, Any],
    task_store_factory: Callable[[], TaskStore] = TaskStore,
) -> dict[str, Any]:
    store = task_store_factory()
    task = store.get(task_id)
    if task is None:
        raise PX4GazeboDeliveryMissionControlError("mission runner task not found")
    runner = mission_artifacts["runner_result"]
    runner_result = (
        runner
        if isinstance(runner, PX4GazeboDeliveryMissionRunnerResult)
        else PX4GazeboDeliveryMissionRunnerResult.model_validate(dict(runner))
    )
    artifacts = {
        "px4_gazebo_delivery_mission_contract": mission_artifacts[
            "mission_contract"
        ].model_dump(mode="json"),
        "px4_gazebo_delivery_mission_prepared_run": mission_artifacts[
            "prepared_run"
        ].model_dump(mode="json"),
        "px4_gazebo_delivery_phase_state_machine": mission_artifacts[
            "phase_state_machine"
        ].model_dump(mode="json"),
        "px4_gazebo_delivery_recovery_policy_matrix": mission_artifacts[
            "recovery_policy_matrix"
        ].model_dump(mode="json"),
        "px4_gazebo_delivery_operator_boundary": mission_artifacts[
            "operator_boundary"
        ].model_dump(mode="json"),
        "px4_gazebo_delivery_mission_health_snapshots": [
            item.model_dump(mode="json")
            for item in mission_artifacts["health_snapshots"]
        ],
        "px4_gazebo_delivery_phase_gates": [
            item.model_dump(mode="json") for item in mission_artifacts["phase_gates"]
        ],
        "px4_gazebo_delivery_phase_gate_evaluation_results": [
            item.model_dump(mode="json")
            for item in mission_artifacts["phase_gate_evaluations"]
        ],
        "px4_gazebo_delivery_recovery_decisions": [
            item.model_dump(mode="json")
            for item in mission_artifacts["recovery_decisions"]
        ],
        "px4_gazebo_delivery_phase_transition_events": [
            item.model_dump(mode="json")
            for item in mission_artifacts["phase_transition_events"]
        ],
        "px4_gazebo_delivery_mission_phase_evidence": [
            item.model_dump(mode="json") for item in mission_artifacts["phase_evidence"]
        ],
        "px4_gazebo_delivery_mission_runner_v1_result": runner_result.model_dump(
            mode="json"
        ),
        "px4_gazebo_delivery_mission_replay_timeline": mission_artifacts[
            "replay_timeline"
        ].model_dump(mode="json"),
        "px4_gazebo_delivery_mission_inspection": mission_artifacts[
            "mission_inspection"
        ].model_dump(mode="json"),
    }
    status = (
        "completed"
        if runner_result.final_status == PX4GazeboDeliveryMissionFinalStatus.COMPLETED
        else "blocked"
    )
    updated = store.update(task_id, status=status, artifacts=artifacts)
    assert updated is not None
    return updated


__all__ = [
    "DEFAULT_MISSION_PHASE_SEQUENCE",
    "PX4_GAZEBO_DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_PHASE_STATE_MACHINE_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_MISSION_HEALTH_SNAPSHOT_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_PHASE_GATE_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_PHASE_GATE_EVALUATION_RESULT_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_RECOVERY_POLICY_MATRIX_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_OPERATOR_BOUNDARY_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_MISSION_PHASE_EVIDENCE_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_MISSION_RUNNER_RESULT_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_MISSION_REPLAY_TIMELINE_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_MISSION_GOLDEN_CORPUS_SCHEMA_VERSION",
    "PX4GazeboDeliveryMissionAutonomyLevel",
    "PX4GazeboDeliveryMissionContract",
    "PX4GazeboDeliveryMissionControlError",
    "PX4GazeboDeliveryMissionFailureType",
    "PX4GazeboDeliveryMissionFinalStatus",
    "PX4GazeboDeliveryMissionGoldenCorpus",
    "PX4GazeboDeliveryMissionHealthSnapshot",
    "PX4GazeboDeliveryMissionHealthStatus",
    "PX4GazeboDeliveryMissionPhase",
    "PX4GazeboDeliveryMissionPhaseEvidence",
    "PX4GazeboDeliveryMissionRecoveryAction",
    "PX4GazeboDeliveryMissionReplayEvent",
    "PX4GazeboDeliveryMissionReplayTimeline",
    "PX4GazeboDeliveryMissionRunnerResult",
    "PX4GazeboDeliveryOperatorBoundary",
    "PX4GazeboDeliveryOperatorPhaseBoundary",
    "PX4GazeboDeliveryPhaseGate",
    "PX4GazeboDeliveryPhaseGateEvaluationResult",
    "PX4GazeboDeliveryPhaseGateStatus",
    "PX4GazeboDeliveryPhaseGateVerdict",
    "PX4GazeboDeliveryPhaseStateMachine",
    "PX4GazeboDeliveryRecoveryDecision",
    "PX4GazeboDeliveryRecoveryPolicyEntry",
    "PX4GazeboDeliveryRecoveryPolicyMatrix",
    "attach_px4_gazebo_delivery_mission_v1_task",
    "build_px4_gazebo_delivery_mission_contract",
    "build_px4_gazebo_delivery_mission_golden_corpus",
    "build_px4_gazebo_delivery_operator_boundary",
    "build_px4_gazebo_delivery_phase_gate_evaluation_result",
    "build_px4_gazebo_delivery_phase_state_machine",
    "build_px4_gazebo_delivery_recovery_decision",
    "build_px4_gazebo_delivery_recovery_policy_matrix",
    "run_px4_gazebo_delivery_mission_v1",
]

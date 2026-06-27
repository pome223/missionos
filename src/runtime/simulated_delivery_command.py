"""Simulator-only delivery command artifacts.

This module prepares an internal Mission OS command-like flow for simulated
delivery episodes. It deliberately does not dispatch MAVLink, ROS, actuator,
Gazebo mutation, mission upload, or hardware commands. Proposal, approval, and
receipt artifacts are dry-run evidence only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.runtime.delivery_episode_review import (
    DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION,
    DELIVERY_SCORECARD_SCHEMA_VERSION,
    DeliveryEpisodeReview,
    DeliveryScorecard,
)
from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.delivery_recovery_decision import (
    DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION,
    DeliveryRecoveryAction,
    DeliveryRecoveryDecision,
)
from src.runtime.hil_telemetry_review import (
    HIL_TELEMETRY_REVIEW_SCHEMA_VERSION,
    HilTelemetryReview,
)
from src.runtime.operator_minimal_delivery_simulation import (
    OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION,
    OperatorMinimalDeliverySimulationStatus,
    OperatorMinimalDeliverySimulationStatusValue,
)
from src.runtime.px4_gazebo_bounded_simulation_runner import (
    PX4_GAZEBO_BOUNDED_SIMULATION_RUN_SCHEMA_VERSION,
    PX4GazeboBoundedSimulationRun,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    PX4_GAZEBO_BOUNDED_SIMULATION_REQUEST_SCHEMA_VERSION,
    PX4GazeboBoundedSimulationRequest,
)
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
    SimulatedDeliveryEpisode,
)
from src.runtime.task_store import TaskStore, get_task_store
from src.runtime.toy_grid_world import (
    TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION,
    ToyGridWorldAutonomyGateResult,
)

SIMULATED_COMMAND_PROPOSAL_SCHEMA_VERSION = "simulated_command_proposal.v1"
SIMULATED_COMMAND_APPROVAL_SCHEMA_VERSION = "simulated_command_approval.v1"
SIMULATED_COMMAND_RECEIPT_SCHEMA_VERSION = "simulated_command_receipt.v1"
SIMULATED_COMMAND_REHEARSAL_RESULT_SCHEMA_VERSION = (
    "simulated_command_rehearsal_result.v1"
)
SIMULATOR_COMMAND_EXECUTION_PREFLIGHT_SCHEMA_VERSION = (
    "simulator_command_execution_preflight.v1"
)
SIMULATOR_COMMAND_EXECUTION_RECEIPT_SCHEMA_VERSION = (
    "simulator_command_execution_receipt.v1"
)


class SimulatedDeliveryCommandError(RuntimeError):
    """Raised when simulator-only command evidence cannot be built safely."""


class SimulatedCommandCategory(str, Enum):
    START_SIMULATED_DELIVERY = "start_simulated_delivery"
    PAUSE_SIMULATED_DELIVERY = "pause_simulated_delivery"
    RESUME_SIMULATED_DELIVERY = "resume_simulated_delivery"
    ABORT_SIMULATED_DELIVERY = "abort_simulated_delivery"


class SimulatedCommandApprovalStatus(str, Enum):
    APPROVED = "approved"


class SimulatedCommandReceiptStatus(str, Enum):
    DRY_RUN_NO_DISPATCH_RECORDED = "dry_run_no_dispatch_recorded"


class SimulatedCommandRehearsalStatus(str, Enum):
    REHEARSED = "rehearsed"
    BLOCKED = "blocked"


class SimulatorCommandExecutionPreflightStatus(str, Enum):
    READY_FOR_SIMULATOR_COMMAND = "ready_for_simulator_command"
    BLOCKED = "blocked"


class SimulatorCommandExecutionCategory(str, Enum):
    MARK_SIMULATED_DELIVERY_STARTED = "mark_simulated_delivery_started"
    MARK_SIMULATED_DELIVERY_PAUSED = "mark_simulated_delivery_paused"
    MARK_SIMULATED_DELIVERY_ABORTED = "mark_simulated_delivery_aborted"


class SimulatorCommandExecutionReceiptStatus(str, Enum):
    INTERNAL_STATE_TRANSITION_RECORDED = "internal_state_transition_recorded"


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actuator",
        "actuator_command",
        "actuator_execution_allowed",
        "attitude_setpoint",
        "cmd_vel",
        "command",
        "command_long",
        "command_payload",
        "command_payload_allowed",
        "dispatch",
        "entity_mutation",
        "execute",
        "gazebo_entity_mutation",
        "gazebo_mutation",
        "landing_command",
        "mav_cmd",
        "mavlink",
        "mavlink_command",
        "mission_item",
        "mission_upload",
        "physical_execution_invoked",
        "position_setpoint",
        "raw_payload",
        "return_to_home_command",
        "ros_action",
        "ros_topic",
        "setpoint",
        "setpoint_stream",
        "thrust",
        "torque",
        "velocity_command",
    }
)

_FORBIDDEN_TEXT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bCOMMAND_LONG\b",
        r"\bMAV_CMD\b",
        r"\bMAVLink\b",
        r"\bMISSION_ITEM(?:_INT)?\b",
        r"\b/cmd_vel\b",
        r"\b/fmu/",
        r"\bsetpoint\b",
        r"\bactuator\b",
        r"\bros\s+action\b",
        r"\bmission\s+upload\b",
        r"\bport\s*[:=]?\s*\d{2,5}\b",
        r"\budp:",
        r"\btcp:",
    )
)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_FORBIDDEN_COMMAND_KEYS_NORMALIZED = frozenset(
    _normalize_key(key) for key in _FORBIDDEN_COMMAND_KEYS
)


def _command_like_paths(value: Any, *, root: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, sub in value.items():
            key_text = str(key)
            path = f"{root}.{key_text}" if root else key_text
            if _normalize_key(key_text) in _FORBIDDEN_COMMAND_KEYS_NORMALIZED:
                findings.append(path)
            findings.extend(_command_like_paths(sub, root=path))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            path = f"{root}.{index}" if root else str(index)
            findings.extend(_command_like_paths(item, root=path))
    elif isinstance(value, str):
        for pattern in _FORBIDDEN_TEXT_PATTERNS:
            if pattern.search(value):
                findings.append(root or "<value>")
                break
    return findings


def _raise_for_command_like_payload(value: Any, *, root: str) -> None:
    findings = _command_like_paths(value, root=root)
    if findings:
        raise SimulatedDeliveryCommandError(
            "simulated delivery command refused command-like payload: "
            + ", ".join(sorted(set(findings)))
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


def _as_tuple(values: Sequence[str] | set[str] | None) -> tuple[str, ...]:
    return tuple(
        sorted({str(item).strip() for item in (values or ()) if str(item).strip()})
    )


class _SimulatorOnlyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulator_only: Literal[True] = True
    proposal_only: Literal[True] = True
    dry_run_only: Literal[True] = True
    no_dispatch: Literal[True] = True
    command_payload_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    approval_promotion_reuse_created: Literal[False] = False


class SimulatedCommandProposal(_SimulatorOnlyBoundary):
    schema_version: Literal[SIMULATED_COMMAND_PROPOSAL_SCHEMA_VERSION] = (
        SIMULATED_COMMAND_PROPOSAL_SCHEMA_VERSION
    )
    proposal_id: str
    delivery_mission_contract_ref: str
    simulated_delivery_episode_ref: str
    delivery_scorecard_ref: str
    delivery_episode_review_ref: str
    delivery_recovery_decision_ref: str
    operator_minimal_delivery_simulation_status_ref: str
    hil_telemetry_review_ref: str
    autonomy_gate_result_ref: str
    command_category: SimulatedCommandCategory
    approval_required: bool
    explicit_operator_approval_required: bool
    operator_escalation_required: bool
    evidence_refs: tuple[str, ...]
    rationale: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    simulated_delivery_episode_schema_version: Literal[
        SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    ] = SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    delivery_scorecard_schema_version: Literal[DELIVERY_SCORECARD_SCHEMA_VERSION] = (
        DELIVERY_SCORECARD_SCHEMA_VERSION
    )
    delivery_episode_review_schema_version: Literal[
        DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION
    ] = DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION
    delivery_recovery_decision_schema_version: Literal[
        DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    operator_minimal_delivery_simulation_status_schema_version: Literal[
        OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    ] = OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    hil_telemetry_review_schema_version: Literal[
        HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    ] = HIL_TELEMETRY_REVIEW_SCHEMA_VERSION
    autonomy_gate_result_schema_version: Literal[
        TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    ] = TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION

    @model_validator(mode="after")
    def _reject_command_like_metadata(self) -> "SimulatedCommandProposal":
        _raise_for_command_like_payload(self.metadata, root="proposal.metadata")
        return self


class SimulatedCommandApproval(_SimulatorOnlyBoundary):
    schema_version: Literal[SIMULATED_COMMAND_APPROVAL_SCHEMA_VERSION] = (
        SIMULATED_COMMAND_APPROVAL_SCHEMA_VERSION
    )
    approval_id: str
    simulated_command_proposal_ref: str
    command_category: SimulatedCommandCategory
    approval_status: SimulatedCommandApprovalStatus
    operator_approved: Literal[True] = True
    approval_scope: Literal["simulator_only_dry_run_receipt"] = (
        "simulator_only_dry_run_receipt"
    )
    approved_for_simulator_only_dry_run_receipt: Literal[True] = True
    approved_for_gazebo_execution: Literal[False] = False
    approved_for_hardware: Literal[False] = False
    approved_for_physical_execution: Literal[False] = False
    approved_at: datetime
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_command_like_metadata(self) -> "SimulatedCommandApproval":
        _raise_for_command_like_payload(self.metadata, root="approval.metadata")
        return self


class SimulatedCommandReceipt(_SimulatorOnlyBoundary):
    schema_version: Literal[SIMULATED_COMMAND_RECEIPT_SCHEMA_VERSION] = (
        SIMULATED_COMMAND_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str
    simulated_command_proposal_ref: str
    simulated_command_approval_ref: str
    command_category: SimulatedCommandCategory
    receipt_status: SimulatedCommandReceiptStatus
    dry_run_no_dispatch_recorded: Literal[True] = True
    command_sent: Literal[False] = False
    gazebo_execution_invoked: Literal[False] = False
    deterministic_bounded_runner_invoked: Literal[False] = False
    recorded_at: datetime
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_command_like_metadata(self) -> "SimulatedCommandReceipt":
        _raise_for_command_like_payload(self.metadata, root="receipt.metadata")
        return self


class SimulatedCommandRehearsalResult(_SimulatorOnlyBoundary):
    schema_version: Literal[SIMULATED_COMMAND_REHEARSAL_RESULT_SCHEMA_VERSION] = (
        SIMULATED_COMMAND_REHEARSAL_RESULT_SCHEMA_VERSION
    )
    rehearsal_result_id: str
    simulated_command_proposal_ref: str
    simulated_command_approval_ref: str = ""
    bounded_simulation_request_ref: str
    bounded_simulation_run_ref: str
    simulated_delivery_episode_ref: str
    delivery_recovery_decision_ref: str
    operator_minimal_delivery_simulation_status_ref: str
    command_category: SimulatedCommandCategory
    rehearsal_status: SimulatedCommandRehearsalStatus
    blocked_reasons: tuple[str, ...] = ()
    rehearsal_only: Literal[True] = True
    existing_bounded_run_referenced: Literal[True] = True
    bounded_run_reexecuted: Literal[False] = False
    command_sent: Literal[False] = False
    dispatch_performed: Literal[False] = False
    gazebo_execution_invoked_by_rehearsal: Literal[False] = False
    bounded_run_status: Literal["completed", "failed", "blocked"]
    autonomy_gate_passed: bool
    recorded_at: datetime
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    bounded_simulation_request_schema_version: Literal[
        PX4_GAZEBO_BOUNDED_SIMULATION_REQUEST_SCHEMA_VERSION
    ] = PX4_GAZEBO_BOUNDED_SIMULATION_REQUEST_SCHEMA_VERSION
    bounded_simulation_run_schema_version: Literal[
        PX4_GAZEBO_BOUNDED_SIMULATION_RUN_SCHEMA_VERSION
    ] = PX4_GAZEBO_BOUNDED_SIMULATION_RUN_SCHEMA_VERSION
    simulated_delivery_episode_schema_version: Literal[
        SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    ] = SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    delivery_recovery_decision_schema_version: Literal[
        DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    ] = DELIVERY_RECOVERY_DECISION_SCHEMA_VERSION
    operator_minimal_delivery_simulation_status_schema_version: Literal[
        OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION
    ] = OPERATOR_MINIMAL_DELIVERY_SIMULATION_STATUS_SCHEMA_VERSION

    @model_validator(mode="after")
    def _validate_rehearsal(self) -> "SimulatedCommandRehearsalResult":
        _raise_for_command_like_payload(self.metadata, root="rehearsal.metadata")
        if self.rehearsal_status is SimulatedCommandRehearsalStatus.REHEARSED:
            if self.blocked_reasons:
                raise ValueError("rehearsed command rehearsal cannot be blocked")
            if not self.simulated_command_approval_ref:
                raise ValueError("rehearsed command rehearsal requires approval ref")
            if self.bounded_run_status != "completed" or not self.autonomy_gate_passed:
                raise ValueError("rehearsed command requires completed run and gate")
        else:
            if not self.blocked_reasons:
                raise ValueError("blocked command rehearsal requires blocked reasons")
        return self


class SimulatorCommandExecutionPreflight(_SimulatorOnlyBoundary):
    schema_version: Literal[SIMULATOR_COMMAND_EXECUTION_PREFLIGHT_SCHEMA_VERSION] = (
        SIMULATOR_COMMAND_EXECUTION_PREFLIGHT_SCHEMA_VERSION
    )
    preflight_id: str
    simulated_command_proposal_ref: str
    simulated_command_approval_ref: str
    simulated_command_receipt_ref: str
    simulated_command_rehearsal_result_ref: str
    bounded_simulation_run_ref: str
    simulated_delivery_episode_ref: str
    delivery_scorecard_ref: str
    delivery_episode_review_ref: str
    delivery_recovery_decision_ref: str
    operator_minimal_delivery_simulation_status_ref: str
    hil_telemetry_review_ref: str
    autonomy_gate_result_ref: str
    command_category: SimulatedCommandCategory
    status: SimulatorCommandExecutionPreflightStatus
    blocked_reasons: tuple[str, ...] = ()
    ready_reasons: tuple[str, ...] = ()
    approval_not_expired: bool
    rehearsal_passed: bool
    bounded_run_completed: bool
    autonomy_gate_passed: bool
    scorecard_passed: bool
    episode_review_passed: bool
    operator_minimal_status_allows_rehearsal: bool
    command_sent: Literal[False] = False
    dispatch_performed: Literal[False] = False
    recorded_at: datetime
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_preflight(self) -> "SimulatorCommandExecutionPreflight":
        _raise_for_command_like_payload(self.metadata, root="preflight.metadata")
        if (
            self.status
            is SimulatorCommandExecutionPreflightStatus.READY_FOR_SIMULATOR_COMMAND
        ):
            if self.blocked_reasons:
                raise ValueError("ready simulator command preflight cannot be blocked")
            if not all(
                (
                    self.approval_not_expired,
                    self.rehearsal_passed,
                    self.bounded_run_completed,
                    self.autonomy_gate_passed,
                    self.scorecard_passed,
                    self.episode_review_passed,
                    self.operator_minimal_status_allows_rehearsal,
                )
            ):
                raise ValueError("ready simulator command preflight has failed checks")
        else:
            if not self.blocked_reasons:
                raise ValueError("blocked simulator command preflight requires reasons")
        return self


class SimulatorCommandExecutionReceipt(_SimulatorOnlyBoundary):
    schema_version: Literal[SIMULATOR_COMMAND_EXECUTION_RECEIPT_SCHEMA_VERSION] = (
        SIMULATOR_COMMAND_EXECUTION_RECEIPT_SCHEMA_VERSION
    )
    execution_receipt_id: str
    simulator_command_execution_preflight_ref: str
    simulated_command_proposal_ref: str
    simulated_command_approval_ref: str
    simulated_command_rehearsal_result_ref: str
    bounded_simulation_run_ref: str
    proposal_command_category: SimulatedCommandCategory
    execution_category: SimulatorCommandExecutionCategory
    receipt_status: SimulatorCommandExecutionReceiptStatus
    internal_state_transition_only: Literal[True] = True
    internal_state_transition_recorded: Literal[True] = True
    command_sent: Literal[False] = False
    external_dispatch_performed: Literal[False] = False
    dispatch_performed: Literal[False] = False
    gazebo_execution_invoked: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    mavlink_dispatch_performed: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    px4_mission_upload_performed: Literal[False] = False
    recorded_at: datetime
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_execution_receipt(self) -> "SimulatorCommandExecutionReceipt":
        _raise_for_command_like_payload(
            self.metadata, root="execution_receipt.metadata"
        )
        if self.receipt_status is not (
            SimulatorCommandExecutionReceiptStatus.INTERNAL_STATE_TRANSITION_RECORDED
        ):
            raise ValueError("simulator command execution receipt status is invalid")
        return self


def _to_contract(
    value: DeliveryMissionContract | Mapping[str, Any],
) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_episode(
    value: SimulatedDeliveryEpisode | Mapping[str, Any],
) -> SimulatedDeliveryEpisode:
    if isinstance(value, SimulatedDeliveryEpisode):
        return value
    return SimulatedDeliveryEpisode.model_validate(dict(value))


def _to_scorecard(value: DeliveryScorecard | Mapping[str, Any]) -> DeliveryScorecard:
    if isinstance(value, DeliveryScorecard):
        return value
    return DeliveryScorecard.model_validate(dict(value))


def _to_episode_review(
    value: DeliveryEpisodeReview | Mapping[str, Any],
) -> DeliveryEpisodeReview:
    if isinstance(value, DeliveryEpisodeReview):
        return value
    return DeliveryEpisodeReview.model_validate(dict(value))


def _to_recovery_decision(
    value: DeliveryRecoveryDecision | Mapping[str, Any],
) -> DeliveryRecoveryDecision:
    if isinstance(value, DeliveryRecoveryDecision):
        return value
    return DeliveryRecoveryDecision.model_validate(dict(value))


def _to_operator_status(
    value: OperatorMinimalDeliverySimulationStatus | Mapping[str, Any],
) -> OperatorMinimalDeliverySimulationStatus:
    if isinstance(value, OperatorMinimalDeliverySimulationStatus):
        return value
    return OperatorMinimalDeliverySimulationStatus.model_validate(dict(value))


def _to_preflight(
    value: SimulatorCommandExecutionPreflight | Mapping[str, Any],
) -> SimulatorCommandExecutionPreflight:
    if isinstance(value, SimulatorCommandExecutionPreflight):
        return value
    return SimulatorCommandExecutionPreflight.model_validate(dict(value))


def _to_hil_review(value: HilTelemetryReview | Mapping[str, Any]) -> HilTelemetryReview:
    if isinstance(value, HilTelemetryReview):
        return value
    return HilTelemetryReview.model_validate(dict(value))


def _to_autonomy_gate(
    value: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
) -> ToyGridWorldAutonomyGateResult:
    if isinstance(value, ToyGridWorldAutonomyGateResult):
        return value
    return ToyGridWorldAutonomyGateResult.model_validate(dict(value))


def _to_bounded_request(
    value: PX4GazeboBoundedSimulationRequest | Mapping[str, Any],
) -> PX4GazeboBoundedSimulationRequest:
    if isinstance(value, PX4GazeboBoundedSimulationRequest):
        return value
    return PX4GazeboBoundedSimulationRequest.model_validate(dict(value))


def _to_bounded_run(
    value: PX4GazeboBoundedSimulationRun | Mapping[str, Any],
) -> PX4GazeboBoundedSimulationRun:
    if isinstance(value, PX4GazeboBoundedSimulationRun):
        return value
    return PX4GazeboBoundedSimulationRun.model_validate(dict(value))


def _contract_ref(contract: DeliveryMissionContract) -> str:
    return f"delivery_mission_contract:{contract.contract_id}"


def _episode_ref(episode: SimulatedDeliveryEpisode) -> str:
    return f"simulated_delivery_episode:{episode.episode_id}"


def _scorecard_ref(scorecard: DeliveryScorecard) -> str:
    return f"delivery_scorecard:{scorecard.scorecard_id}"


def _episode_review_ref(review: DeliveryEpisodeReview) -> str:
    return f"delivery_episode_review:{review.review_id}"


def _recovery_decision_ref(decision: DeliveryRecoveryDecision) -> str:
    return f"delivery_recovery_decision:{decision.decision_id}"


def _operator_status_ref(status: OperatorMinimalDeliverySimulationStatus) -> str:
    return f"operator_minimal_delivery_simulation_status:{status.status_id}"


def _hil_ref(review: HilTelemetryReview) -> str:
    return f"hil_telemetry_review:{review.review_id}"


def _autonomy_gate_ref(gate: ToyGridWorldAutonomyGateResult) -> str:
    return f"autonomy_gate_result:{gate.gate_id}"


def _proposal_ref(proposal: SimulatedCommandProposal) -> str:
    return f"simulated_command_proposal:{proposal.proposal_id}"


def _approval_ref(approval: SimulatedCommandApproval) -> str:
    return f"simulated_command_approval:{approval.approval_id}"


def _receipt_ref(receipt: SimulatedCommandReceipt) -> str:
    return f"simulated_command_receipt:{receipt.receipt_id}"


def _rehearsal_ref(rehearsal: SimulatedCommandRehearsalResult) -> str:
    return f"simulated_command_rehearsal_result:{rehearsal.rehearsal_result_id}"


def _request_ref(request: PX4GazeboBoundedSimulationRequest) -> str:
    return f"px4_gazebo_bounded_simulation_request:{request.request_id}"


def _run_ref(run: PX4GazeboBoundedSimulationRun) -> str:
    return f"px4_gazebo_bounded_simulation_run:{run.run_id}"


def _preflight_ref(preflight: SimulatorCommandExecutionPreflight) -> str:
    return f"simulator_command_execution_preflight:{preflight.preflight_id}"


def _execution_receipt_ref(receipt: SimulatorCommandExecutionReceipt) -> str:
    return f"simulator_command_execution_receipt:{receipt.execution_receipt_id}"


def _validate_source_refs(
    *,
    contract: DeliveryMissionContract,
    episode: SimulatedDeliveryEpisode,
    scorecard: DeliveryScorecard,
    episode_review: DeliveryEpisodeReview,
    recovery_decision: DeliveryRecoveryDecision,
    operator_status: OperatorMinimalDeliverySimulationStatus,
    hil_review: HilTelemetryReview,
    autonomy_gate: ToyGridWorldAutonomyGateResult,
) -> None:
    contract_ref = _contract_ref(contract)
    episode_ref = _episode_ref(episode)
    scorecard_ref = _scorecard_ref(scorecard)
    episode_review_ref = _episode_review_ref(episode_review)
    decision_ref = _recovery_decision_ref(recovery_decision)
    hil_ref = _hil_ref(hil_review)
    gate_ref = _autonomy_gate_ref(autonomy_gate)
    if episode.delivery_mission_contract_id != contract.contract_id:
        raise SimulatedDeliveryCommandError("episode/contract ref mismatch")
    if scorecard.delivery_mission_contract_ref != contract_ref:
        raise SimulatedDeliveryCommandError("scorecard/contract ref mismatch")
    if scorecard.simulated_delivery_episode_ref != episode_ref:
        raise SimulatedDeliveryCommandError("scorecard/episode ref mismatch")
    if episode_review.scorecard_ref != scorecard_ref:
        raise SimulatedDeliveryCommandError("review/scorecard ref mismatch")
    if episode_review.delivery_mission_contract_ref != contract_ref:
        raise SimulatedDeliveryCommandError("review/contract ref mismatch")
    if episode_review.simulated_delivery_episode_ref != episode_ref:
        raise SimulatedDeliveryCommandError("review/episode ref mismatch")
    if recovery_decision.delivery_mission_contract_id != contract.contract_id:
        raise SimulatedDeliveryCommandError("decision/contract ref mismatch")
    if recovery_decision.simulated_delivery_episode_id != episode.episode_id:
        raise SimulatedDeliveryCommandError("decision/episode ref mismatch")
    if recovery_decision.delivery_scorecard_id != scorecard.scorecard_id:
        raise SimulatedDeliveryCommandError("decision/scorecard ref mismatch")
    if recovery_decision.delivery_episode_review_id != episode_review.review_id:
        raise SimulatedDeliveryCommandError("decision/review ref mismatch")
    if recovery_decision.hil_telemetry_review_id != hil_review.review_id:
        raise SimulatedDeliveryCommandError("decision/HIL ref mismatch")
    if recovery_decision.autonomy_gate_result_id != autonomy_gate.gate_id:
        raise SimulatedDeliveryCommandError("decision/gate ref mismatch")
    if operator_status.delivery_mission_contract_ref != contract_ref:
        raise SimulatedDeliveryCommandError("operator status/contract ref mismatch")
    if operator_status.simulated_delivery_episode_ref != episode_ref:
        raise SimulatedDeliveryCommandError("operator status/episode ref mismatch")
    if operator_status.delivery_scorecard_ref != scorecard_ref:
        raise SimulatedDeliveryCommandError("operator status/scorecard ref mismatch")
    if operator_status.delivery_episode_review_ref != episode_review_ref:
        raise SimulatedDeliveryCommandError("operator status/review ref mismatch")
    if operator_status.delivery_recovery_decision_ref != decision_ref:
        raise SimulatedDeliveryCommandError("operator status/decision ref mismatch")
    if operator_status.hil_telemetry_review_ref != hil_ref:
        raise SimulatedDeliveryCommandError("operator status/HIL ref mismatch")
    if operator_status.autonomy_gate_result_ref != gate_ref:
        raise SimulatedDeliveryCommandError("operator status/gate ref mismatch")


def _derive_command_category(
    *,
    recovery_decision: DeliveryRecoveryDecision,
    operator_status: OperatorMinimalDeliverySimulationStatus,
) -> SimulatedCommandCategory:
    if (
        recovery_decision.primary_action
        in {DeliveryRecoveryAction.ABORT, DeliveryRecoveryAction.ABORT_RECOMMENDED}
        or recovery_decision.abort_recommended
        or recovery_decision.abort_proposed
    ):
        return SimulatedCommandCategory.ABORT_SIMULATED_DELIVERY
    if recovery_decision.primary_action in {
        DeliveryRecoveryAction.HOLD,
        DeliveryRecoveryAction.HOLD_RECOMMENDED,
        DeliveryRecoveryAction.OPERATOR_ESCALATION_REQUIRED,
    }:
        return SimulatedCommandCategory.PAUSE_SIMULATED_DELIVERY
    if (
        operator_status.status
        is OperatorMinimalDeliverySimulationStatusValue.CONTINUE_WITHOUT_OPERATOR_INTERVENTION
        or recovery_decision.primary_action is DeliveryRecoveryAction.CONTINUE
    ):
        return SimulatedCommandCategory.RESUME_SIMULATED_DELIVERY
    return SimulatedCommandCategory.START_SIMULATED_DELIVERY


def _requires_approval(
    *,
    command_category: SimulatedCommandCategory,
    operator_status: OperatorMinimalDeliverySimulationStatus,
    recovery_decision: DeliveryRecoveryDecision,
) -> bool:
    return (
        operator_status.operator_intervention_required
        or recovery_decision.operator_escalation_required
        or command_category
        in {
            SimulatedCommandCategory.PAUSE_SIMULATED_DELIVERY,
            SimulatedCommandCategory.ABORT_SIMULATED_DELIVERY,
        }
    )


def build_simulated_command_proposal(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_scorecard: DeliveryScorecard | Mapping[str, Any],
    delivery_episode_review: DeliveryEpisodeReview | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    operator_minimal_delivery_simulation_status: (
        OperatorMinimalDeliverySimulationStatus | Mapping[str, Any]
    ),
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    command_category: SimulatedCommandCategory | str | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> SimulatedCommandProposal:
    """Build a simulator-only command proposal from reviewed delivery evidence."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_payload(metadata_payload, root="metadata")
    contract = _to_contract(delivery_mission_contract)
    episode = _to_episode(simulated_delivery_episode)
    scorecard = _to_scorecard(delivery_scorecard)
    episode_review = _to_episode_review(delivery_episode_review)
    recovery_decision = _to_recovery_decision(delivery_recovery_decision)
    operator_status = _to_operator_status(operator_minimal_delivery_simulation_status)
    hil_review = _to_hil_review(hil_telemetry_review)
    autonomy_gate = _to_autonomy_gate(autonomy_gate_result)
    created_at = _utc(now)

    _validate_source_refs(
        contract=contract,
        episode=episode,
        scorecard=scorecard,
        episode_review=episode_review,
        recovery_decision=recovery_decision,
        operator_status=operator_status,
        hil_review=hil_review,
        autonomy_gate=autonomy_gate,
    )
    category = (
        SimulatedCommandCategory(command_category)
        if command_category is not None
        else _derive_command_category(
            recovery_decision=recovery_decision,
            operator_status=operator_status,
        )
    )
    approval_required = _requires_approval(
        command_category=category,
        operator_status=operator_status,
        recovery_decision=recovery_decision,
    )
    contract_ref = _contract_ref(contract)
    episode_ref = _episode_ref(episode)
    scorecard_ref = _scorecard_ref(scorecard)
    review_ref = _episode_review_ref(episode_review)
    decision_ref = _recovery_decision_ref(recovery_decision)
    status_ref = _operator_status_ref(operator_status)
    hil_ref = _hil_ref(hil_review)
    gate_ref = _autonomy_gate_ref(autonomy_gate)
    evidence_refs = _as_tuple(
        [
            contract_ref,
            episode_ref,
            scorecard_ref,
            review_ref,
            decision_ref,
            status_ref,
            hil_ref,
            gate_ref,
            *operator_status.escalation_triggers,
        ]
    )
    payload = {
        "contract_ref": contract_ref,
        "episode_ref": episode_ref,
        "decision_ref": decision_ref,
        "status_ref": status_ref,
        "command_category": category.value,
        "approval_required": approval_required,
    }
    return SimulatedCommandProposal(
        proposal_id=_stable_id("simulated_command_proposal", payload),
        delivery_mission_contract_ref=contract_ref,
        simulated_delivery_episode_ref=episode_ref,
        delivery_scorecard_ref=scorecard_ref,
        delivery_episode_review_ref=review_ref,
        delivery_recovery_decision_ref=decision_ref,
        operator_minimal_delivery_simulation_status_ref=status_ref,
        hil_telemetry_review_ref=hil_ref,
        autonomy_gate_result_ref=gate_ref,
        command_category=category,
        approval_required=approval_required,
        explicit_operator_approval_required=approval_required,
        operator_escalation_required=operator_status.operator_intervention_required,
        evidence_refs=evidence_refs,
        rationale=(
            "operator_escalation_requires_approval"
            if approval_required
            else "operator_minimal_simulation_allows_internal_dry_run_proposal"
        ),
        created_at=created_at,
        metadata=metadata_payload,
    )


def build_simulated_command_approval(
    *,
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    operator_approved: bool = True,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> SimulatedCommandApproval:
    """Approve a proposal for dry-run receipt creation only."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_payload(metadata_payload, root="metadata")
    proposal = (
        simulated_command_proposal
        if isinstance(simulated_command_proposal, SimulatedCommandProposal)
        else SimulatedCommandProposal.model_validate(dict(simulated_command_proposal))
    )
    if not operator_approved:
        raise SimulatedDeliveryCommandError(
            "simulated command approval requires explicit operator approval"
        )
    approved_at = _utc(now)
    proposal_ref = _proposal_ref(proposal)
    payload = {
        "proposal_ref": proposal_ref,
        "command_category": proposal.command_category.value,
        "operator_approved": operator_approved,
    }
    return SimulatedCommandApproval(
        approval_id=_stable_id("simulated_command_approval", payload),
        simulated_command_proposal_ref=proposal_ref,
        command_category=proposal.command_category,
        approval_status=SimulatedCommandApprovalStatus.APPROVED,
        approved_at=approved_at,
        evidence_refs=(proposal_ref, *proposal.evidence_refs),
        metadata=metadata_payload,
    )


def build_simulated_command_receipt(
    *,
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> SimulatedCommandReceipt:
    """Record a no-dispatch dry-run receipt for an approved simulator proposal."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_payload(metadata_payload, root="metadata")
    proposal = (
        simulated_command_proposal
        if isinstance(simulated_command_proposal, SimulatedCommandProposal)
        else SimulatedCommandProposal.model_validate(dict(simulated_command_proposal))
    )
    approval = (
        simulated_command_approval
        if isinstance(simulated_command_approval, SimulatedCommandApproval)
        else SimulatedCommandApproval.model_validate(dict(simulated_command_approval))
    )
    proposal_ref = _proposal_ref(proposal)
    approval_ref = _approval_ref(approval)
    if approval.simulated_command_proposal_ref != proposal_ref:
        raise SimulatedDeliveryCommandError("approval/proposal ref mismatch")
    if approval.command_category is not proposal.command_category:
        raise SimulatedDeliveryCommandError("approval/proposal category mismatch")
    recorded_at = _utc(now)
    payload = {
        "proposal_ref": proposal_ref,
        "approval_ref": approval_ref,
        "command_category": proposal.command_category.value,
    }
    return SimulatedCommandReceipt(
        receipt_id=_stable_id("simulated_command_receipt", payload),
        simulated_command_proposal_ref=proposal_ref,
        simulated_command_approval_ref=approval_ref,
        command_category=proposal.command_category,
        receipt_status=SimulatedCommandReceiptStatus.DRY_RUN_NO_DISPATCH_RECORDED,
        recorded_at=recorded_at,
        evidence_refs=(proposal_ref, approval_ref, *proposal.evidence_refs),
        metadata=metadata_payload,
    )


def build_simulated_command_rehearsal_result(
    *,
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any] | None,
    bounded_simulation_request: PX4GazeboBoundedSimulationRequest | Mapping[str, Any],
    bounded_simulation_run: PX4GazeboBoundedSimulationRun | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    operator_minimal_delivery_simulation_status: (
        OperatorMinimalDeliverySimulationStatus | Mapping[str, Any]
    ),
    now: datetime | None = None,
    approval_max_age_seconds: float = 300.0,
    metadata: dict[str, Any] | None = None,
) -> SimulatedCommandRehearsalResult:
    """Reference an existing bounded run as a simulator-only command rehearsal."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_payload(metadata_payload, root="metadata")
    proposal = (
        simulated_command_proposal
        if isinstance(simulated_command_proposal, SimulatedCommandProposal)
        else SimulatedCommandProposal.model_validate(dict(simulated_command_proposal))
    )
    approval = (
        None
        if simulated_command_approval is None
        else (
            simulated_command_approval
            if isinstance(simulated_command_approval, SimulatedCommandApproval)
            else SimulatedCommandApproval.model_validate(
                dict(simulated_command_approval)
            )
        )
    )
    request = _to_bounded_request(bounded_simulation_request)
    run = _to_bounded_run(bounded_simulation_run)
    episode = _to_episode(simulated_delivery_episode)
    recovery_decision = _to_recovery_decision(delivery_recovery_decision)
    operator_status = _to_operator_status(operator_minimal_delivery_simulation_status)
    recorded_at = _utc(now)

    proposal_ref = _proposal_ref(proposal)
    approval_ref = _approval_ref(approval) if approval is not None else ""
    request_ref = _request_ref(request)
    run_ref = _run_ref(run)
    episode_ref = _episode_ref(episode)
    decision_ref = _recovery_decision_ref(recovery_decision)
    status_ref = _operator_status_ref(operator_status)
    blocked: list[str] = []

    if approval is None:
        blocked.append("missing_simulated_command_approval")
    elif approval.simulated_command_proposal_ref != proposal_ref:
        blocked.append("simulated_command_approval_ref_mismatch")
    elif approval.command_category is not proposal.command_category:
        blocked.append("simulated_command_approval_category_mismatch")
    elif (
        recorded_at - approval.approved_at
    ).total_seconds() > approval_max_age_seconds:
        blocked.append("simulated_command_approval_expired")

    if run.request_ref != request_ref:
        blocked.append("bounded_run_request_ref_mismatch")
    if episode.bounded_simulation_request_ref != request_ref:
        blocked.append("episode_bounded_request_ref_mismatch")
    if episode.bounded_simulation_run_ref != run_ref:
        blocked.append("episode_bounded_run_ref_mismatch")
    if proposal.simulated_delivery_episode_ref != episode_ref:
        blocked.append("proposal_episode_ref_mismatch")
    if proposal.delivery_recovery_decision_ref != decision_ref:
        blocked.append("proposal_recovery_decision_ref_mismatch")
    if proposal.operator_minimal_delivery_simulation_status_ref != status_ref:
        blocked.append("proposal_operator_status_ref_mismatch")
    if recovery_decision.simulated_delivery_episode_id != episode.episode_id:
        blocked.append("decision_episode_ref_mismatch")
    if operator_status.simulated_delivery_episode_ref != episode_ref:
        blocked.append("operator_status_episode_ref_mismatch")

    if run.status != "completed":
        blocked.append(f"bounded_run_{run.status}")
    if run.blocked_reasons:
        blocked.extend(f"bounded_run_{reason}" for reason in run.blocked_reasons)
    gate_passed = not run.blocked_reasons and bool(run.gate_ref)
    if not gate_passed:
        blocked.append("bounded_run_gate_not_passed")
    if (
        proposal.command_category is SimulatedCommandCategory.ABORT_SIMULATED_DELIVERY
        and not (
            recovery_decision.abort_recommended
            or recovery_decision.abort_proposed
            or recovery_decision.primary_action
            in {DeliveryRecoveryAction.ABORT, DeliveryRecoveryAction.ABORT_RECOMMENDED}
        )
    ):
        blocked.append("abort_rehearsal_requires_abort_recovery_decision")

    blocked_reasons = _as_tuple(blocked)
    status = (
        SimulatedCommandRehearsalStatus.BLOCKED
        if blocked_reasons
        else SimulatedCommandRehearsalStatus.REHEARSED
    )
    evidence_refs = _as_tuple(
        [
            proposal_ref,
            approval_ref,
            request_ref,
            run_ref,
            episode_ref,
            decision_ref,
            status_ref,
            *proposal.evidence_refs,
            *run.telemetry_refs,
            run.gate_ref,
            run.hil_review_ref,
        ]
    )
    payload = {
        "proposal_ref": proposal_ref,
        "approval_ref": approval_ref,
        "request_ref": request_ref,
        "run_ref": run_ref,
        "command_category": proposal.command_category.value,
        "status": status.value,
        "blocked_reasons": blocked_reasons,
    }
    return SimulatedCommandRehearsalResult(
        rehearsal_result_id=_stable_id("simulated_command_rehearsal_result", payload),
        simulated_command_proposal_ref=proposal_ref,
        simulated_command_approval_ref=approval_ref,
        bounded_simulation_request_ref=request_ref,
        bounded_simulation_run_ref=run_ref,
        simulated_delivery_episode_ref=episode_ref,
        delivery_recovery_decision_ref=decision_ref,
        operator_minimal_delivery_simulation_status_ref=status_ref,
        command_category=proposal.command_category,
        rehearsal_status=status,
        blocked_reasons=blocked_reasons,
        bounded_run_status=run.status,
        autonomy_gate_passed=gate_passed,
        recorded_at=recorded_at,
        evidence_refs=evidence_refs,
        metadata=metadata_payload,
    )


def build_simulator_command_execution_preflight(
    *,
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any],
    simulated_command_receipt: SimulatedCommandReceipt | Mapping[str, Any],
    simulated_command_rehearsal_result: (
        SimulatedCommandRehearsalResult | Mapping[str, Any]
    ),
    bounded_simulation_run: PX4GazeboBoundedSimulationRun | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_scorecard: DeliveryScorecard | Mapping[str, Any],
    delivery_episode_review: DeliveryEpisodeReview | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    operator_minimal_delivery_simulation_status: (
        OperatorMinimalDeliverySimulationStatus | Mapping[str, Any]
    ),
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    now: datetime | None = None,
    approval_max_age_seconds: float = 300.0,
    metadata: dict[str, Any] | None = None,
) -> SimulatorCommandExecutionPreflight:
    """Build the final rule-based preflight before any simulator command execution."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_payload(metadata_payload, root="metadata")
    proposal = (
        simulated_command_proposal
        if isinstance(simulated_command_proposal, SimulatedCommandProposal)
        else SimulatedCommandProposal.model_validate(dict(simulated_command_proposal))
    )
    approval = (
        simulated_command_approval
        if isinstance(simulated_command_approval, SimulatedCommandApproval)
        else SimulatedCommandApproval.model_validate(dict(simulated_command_approval))
    )
    receipt = (
        simulated_command_receipt
        if isinstance(simulated_command_receipt, SimulatedCommandReceipt)
        else SimulatedCommandReceipt.model_validate(dict(simulated_command_receipt))
    )
    rehearsal = (
        simulated_command_rehearsal_result
        if isinstance(
            simulated_command_rehearsal_result, SimulatedCommandRehearsalResult
        )
        else SimulatedCommandRehearsalResult.model_validate(
            dict(simulated_command_rehearsal_result)
        )
    )
    run = _to_bounded_run(bounded_simulation_run)
    episode = _to_episode(simulated_delivery_episode)
    scorecard = _to_scorecard(delivery_scorecard)
    episode_review = _to_episode_review(delivery_episode_review)
    recovery_decision = _to_recovery_decision(delivery_recovery_decision)
    operator_status = _to_operator_status(operator_minimal_delivery_simulation_status)
    hil_review = _to_hil_review(hil_telemetry_review)
    autonomy_gate = _to_autonomy_gate(autonomy_gate_result)
    recorded_at = _utc(now)

    proposal_ref = _proposal_ref(proposal)
    approval_ref = _approval_ref(approval)
    receipt_ref = _receipt_ref(receipt)
    rehearsal_ref = _rehearsal_ref(rehearsal)
    run_ref = _run_ref(run)
    episode_ref = _episode_ref(episode)
    scorecard_ref = _scorecard_ref(scorecard)
    review_ref = _episode_review_ref(episode_review)
    decision_ref = _recovery_decision_ref(recovery_decision)
    status_ref = _operator_status_ref(operator_status)
    hil_ref = _hil_ref(hil_review)
    gate_ref = _autonomy_gate_ref(autonomy_gate)
    blocked: list[str] = []

    if approval.simulated_command_proposal_ref != proposal_ref:
        blocked.append("approval_proposal_ref_mismatch")
    if receipt.simulated_command_proposal_ref != proposal_ref:
        blocked.append("receipt_proposal_ref_mismatch")
    if receipt.simulated_command_approval_ref != approval_ref:
        blocked.append("receipt_approval_ref_mismatch")
    if rehearsal.simulated_command_proposal_ref != proposal_ref:
        blocked.append("rehearsal_proposal_ref_mismatch")
    if rehearsal.simulated_command_approval_ref != approval_ref:
        blocked.append("rehearsal_approval_ref_mismatch")
    if rehearsal.bounded_simulation_run_ref != run_ref:
        blocked.append("rehearsal_bounded_run_ref_mismatch")
    if rehearsal.simulated_delivery_episode_ref != episode_ref:
        blocked.append("rehearsal_episode_ref_mismatch")
    if rehearsal.delivery_recovery_decision_ref != decision_ref:
        blocked.append("rehearsal_decision_ref_mismatch")
    if rehearsal.operator_minimal_delivery_simulation_status_ref != status_ref:
        blocked.append("rehearsal_operator_status_ref_mismatch")
    if proposal.simulated_delivery_episode_ref != episode_ref:
        blocked.append("proposal_episode_ref_mismatch")
    if proposal.delivery_scorecard_ref != scorecard_ref:
        blocked.append("proposal_scorecard_ref_mismatch")
    if proposal.delivery_episode_review_ref != review_ref:
        blocked.append("proposal_review_ref_mismatch")
    if proposal.delivery_recovery_decision_ref != decision_ref:
        blocked.append("proposal_decision_ref_mismatch")
    if proposal.operator_minimal_delivery_simulation_status_ref != status_ref:
        blocked.append("proposal_operator_status_ref_mismatch")
    if proposal.hil_telemetry_review_ref != hil_ref:
        blocked.append("proposal_hil_ref_mismatch")
    if proposal.autonomy_gate_result_ref != gate_ref:
        blocked.append("proposal_gate_ref_mismatch")
    if scorecard.simulated_delivery_episode_ref != episode_ref:
        blocked.append("scorecard_episode_ref_mismatch")
    if scorecard.hil_telemetry_review_ref != hil_ref:
        blocked.append("scorecard_hil_ref_mismatch")
    if scorecard.autonomy_gate_result_ref != gate_ref:
        blocked.append("scorecard_gate_ref_mismatch")
    if episode_review.scorecard_ref != scorecard_ref:
        blocked.append("review_scorecard_ref_mismatch")
    if episode_review.simulated_delivery_episode_ref != episode_ref:
        blocked.append("review_episode_ref_mismatch")
    if episode_review.hil_telemetry_review_ref != hil_ref:
        blocked.append("review_hil_ref_mismatch")
    if episode_review.autonomy_gate_result_ref != gate_ref:
        blocked.append("review_gate_ref_mismatch")
    if recovery_decision.simulated_delivery_episode_id != episode.episode_id:
        blocked.append("decision_episode_ref_mismatch")
    if recovery_decision.delivery_scorecard_id != scorecard.scorecard_id:
        blocked.append("decision_scorecard_ref_mismatch")
    if recovery_decision.delivery_episode_review_id != episode_review.review_id:
        blocked.append("decision_review_ref_mismatch")
    if recovery_decision.hil_telemetry_review_id != hil_review.review_id:
        blocked.append("decision_hil_ref_mismatch")
    if recovery_decision.autonomy_gate_result_id != autonomy_gate.gate_id:
        blocked.append("decision_gate_ref_mismatch")
    if operator_status.simulated_delivery_episode_ref != episode_ref:
        blocked.append("operator_status_episode_ref_mismatch")
    if operator_status.delivery_scorecard_ref != scorecard_ref:
        blocked.append("operator_status_scorecard_ref_mismatch")
    if operator_status.delivery_episode_review_ref != review_ref:
        blocked.append("operator_status_review_ref_mismatch")
    if operator_status.delivery_recovery_decision_ref != decision_ref:
        blocked.append("operator_status_decision_ref_mismatch")

    approval_age = (recorded_at - approval.approved_at).total_seconds()
    approval_not_expired = approval_age <= approval_max_age_seconds
    if not approval_not_expired:
        blocked.append("simulated_command_approval_expired")
    if proposal.command_category not in set(SimulatedCommandCategory):
        blocked.append("simulated_command_category_not_allowed")
    if receipt.command_sent is not False:
        blocked.append("receipt_command_sent")
    if rehearsal.rehearsal_status is not SimulatedCommandRehearsalStatus.REHEARSED:
        blocked.append("rehearsal_not_passed")
    if rehearsal.blocked_reasons:
        blocked.extend(f"rehearsal_{reason}" for reason in rehearsal.blocked_reasons)
    bounded_run_completed = run.status == "completed" and not run.blocked_reasons
    if not bounded_run_completed:
        blocked.append(f"bounded_run_{run.status}")
        blocked.extend(f"bounded_run_{reason}" for reason in run.blocked_reasons)
    autonomy_gate_passed = bool(autonomy_gate.passed)
    if not autonomy_gate_passed:
        blocked.append("autonomy_gate_failed")
    scorecard_passed = bool(scorecard.passed) and not scorecard.blocked_buckets
    if not scorecard_passed:
        blocked.append("delivery_scorecard_blocked")
        blocked.extend(f"scorecard_{bucket}" for bucket in scorecard.blocked_buckets)
    episode_review_passed = (
        bool(episode_review.passed) and not episode_review.blocked_buckets
    )
    if not episode_review_passed:
        blocked.append("delivery_episode_review_blocked")
        blocked.extend(f"review_{bucket}" for bucket in episode_review.blocked_buckets)
    operator_allows = (
        not operator_status.operator_intervention_required
        or rehearsal.rehearsal_status is SimulatedCommandRehearsalStatus.REHEARSED
    )
    if not operator_allows:
        blocked.append("operator_minimal_status_requires_escalation")

    blocked_reasons = _as_tuple(blocked)
    ready = not blocked_reasons
    status = (
        SimulatorCommandExecutionPreflightStatus.READY_FOR_SIMULATOR_COMMAND
        if ready
        else SimulatorCommandExecutionPreflightStatus.BLOCKED
    )
    ready_reasons = _as_tuple(
        [
            "proposal_approval_receipt_rehearsal_refs_aligned",
            "approval_not_expired",
            "bounded_run_completed",
            "autonomy_gate_passed",
            "delivery_scorecard_passed",
            "delivery_episode_review_passed",
            "operator_minimal_status_allows_rehearsal",
        ]
        if ready
        else ()
    )
    evidence_refs = _as_tuple(
        [
            proposal_ref,
            approval_ref,
            receipt_ref,
            rehearsal_ref,
            run_ref,
            episode_ref,
            scorecard_ref,
            review_ref,
            decision_ref,
            status_ref,
            hil_ref,
            gate_ref,
            *proposal.evidence_refs,
            *rehearsal.evidence_refs,
        ]
    )
    payload = {
        "proposal_ref": proposal_ref,
        "approval_ref": approval_ref,
        "receipt_ref": receipt_ref,
        "rehearsal_ref": rehearsal_ref,
        "run_ref": run_ref,
        "episode_ref": episode_ref,
        "status": status.value,
        "blocked_reasons": blocked_reasons,
    }
    return SimulatorCommandExecutionPreflight(
        preflight_id=_stable_id("simulator_command_execution_preflight", payload),
        simulated_command_proposal_ref=proposal_ref,
        simulated_command_approval_ref=approval_ref,
        simulated_command_receipt_ref=receipt_ref,
        simulated_command_rehearsal_result_ref=rehearsal_ref,
        bounded_simulation_run_ref=run_ref,
        simulated_delivery_episode_ref=episode_ref,
        delivery_scorecard_ref=scorecard_ref,
        delivery_episode_review_ref=review_ref,
        delivery_recovery_decision_ref=decision_ref,
        operator_minimal_delivery_simulation_status_ref=status_ref,
        hil_telemetry_review_ref=hil_ref,
        autonomy_gate_result_ref=gate_ref,
        command_category=proposal.command_category,
        status=status,
        blocked_reasons=blocked_reasons,
        ready_reasons=ready_reasons,
        approval_not_expired=approval_not_expired,
        rehearsal_passed=(
            rehearsal.rehearsal_status is SimulatedCommandRehearsalStatus.REHEARSED
        ),
        bounded_run_completed=bounded_run_completed,
        autonomy_gate_passed=autonomy_gate_passed,
        scorecard_passed=scorecard_passed,
        episode_review_passed=episode_review_passed,
        operator_minimal_status_allows_rehearsal=operator_allows,
        recorded_at=recorded_at,
        evidence_refs=evidence_refs,
        metadata=metadata_payload,
    )


_EXECUTION_CATEGORY_BY_PROPOSAL_CATEGORY: dict[
    SimulatedCommandCategory, SimulatorCommandExecutionCategory
] = {
    SimulatedCommandCategory.START_SIMULATED_DELIVERY: (
        SimulatorCommandExecutionCategory.MARK_SIMULATED_DELIVERY_STARTED
    ),
    SimulatedCommandCategory.RESUME_SIMULATED_DELIVERY: (
        SimulatorCommandExecutionCategory.MARK_SIMULATED_DELIVERY_STARTED
    ),
    SimulatedCommandCategory.PAUSE_SIMULATED_DELIVERY: (
        SimulatorCommandExecutionCategory.MARK_SIMULATED_DELIVERY_PAUSED
    ),
    SimulatedCommandCategory.ABORT_SIMULATED_DELIVERY: (
        SimulatorCommandExecutionCategory.MARK_SIMULATED_DELIVERY_ABORTED
    ),
}


def build_simulator_command_execution_receipt(
    *,
    simulator_command_execution_preflight: (
        SimulatorCommandExecutionPreflight | Mapping[str, Any]
    ),
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any],
    simulated_command_rehearsal_result: (
        SimulatedCommandRehearsalResult | Mapping[str, Any]
    ),
    bounded_simulation_run: PX4GazeboBoundedSimulationRun | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> SimulatorCommandExecutionReceipt:
    """Record an internal simulator-only command state transition.

    This receipt deliberately does not dispatch to Gazebo, PX4, MAVLink, ROS,
    actuators, mission upload, or hardware. It only records that Mission OS
    accepted a preflight-ready simulator command as an internal state transition
    against an existing bounded run context.
    """

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_payload(metadata_payload, root="metadata")
    preflight = _to_preflight(simulator_command_execution_preflight)
    proposal = (
        simulated_command_proposal
        if isinstance(simulated_command_proposal, SimulatedCommandProposal)
        else SimulatedCommandProposal.model_validate(dict(simulated_command_proposal))
    )
    approval = (
        simulated_command_approval
        if isinstance(simulated_command_approval, SimulatedCommandApproval)
        else SimulatedCommandApproval.model_validate(dict(simulated_command_approval))
    )
    rehearsal = (
        simulated_command_rehearsal_result
        if isinstance(
            simulated_command_rehearsal_result, SimulatedCommandRehearsalResult
        )
        else SimulatedCommandRehearsalResult.model_validate(
            dict(simulated_command_rehearsal_result)
        )
    )
    run = _to_bounded_run(bounded_simulation_run)
    preflight_ref = _preflight_ref(preflight)
    proposal_ref = _proposal_ref(proposal)
    approval_ref = _approval_ref(approval)
    rehearsal_ref = _rehearsal_ref(rehearsal)
    run_ref = _run_ref(run)

    if (
        preflight.status
        is not SimulatorCommandExecutionPreflightStatus.READY_FOR_SIMULATOR_COMMAND
    ):
        raise SimulatedDeliveryCommandError(
            "cannot create simulator command execution receipt from blocked preflight"
        )
    if preflight.simulated_command_proposal_ref != proposal_ref:
        raise SimulatedDeliveryCommandError(
            "simulator command execution receipt proposal ref mismatch"
        )
    if preflight.simulated_command_approval_ref != approval_ref:
        raise SimulatedDeliveryCommandError(
            "simulator command execution receipt approval ref mismatch"
        )
    if preflight.simulated_command_rehearsal_result_ref != rehearsal_ref:
        raise SimulatedDeliveryCommandError(
            "simulator command execution receipt rehearsal ref mismatch"
        )
    if preflight.bounded_simulation_run_ref != run_ref:
        raise SimulatedDeliveryCommandError(
            "simulator command execution receipt bounded run ref mismatch"
        )
    if preflight.command_category != proposal.command_category:
        raise SimulatedDeliveryCommandError(
            "simulator command execution receipt command category mismatch"
        )

    execution_category = _EXECUTION_CATEGORY_BY_PROPOSAL_CATEGORY.get(
        proposal.command_category
    )
    if execution_category is None:
        raise SimulatedDeliveryCommandError(
            "simulator command execution receipt refused unsupported category"
        )
    evidence_refs = _as_tuple(
        [
            preflight_ref,
            proposal_ref,
            approval_ref,
            rehearsal_ref,
            run_ref,
            *preflight.evidence_refs,
        ]
    )
    payload = {
        "preflight_ref": preflight_ref,
        "proposal_ref": proposal_ref,
        "approval_ref": approval_ref,
        "rehearsal_ref": rehearsal_ref,
        "run_ref": run_ref,
        "execution_category": execution_category.value,
    }
    return SimulatorCommandExecutionReceipt(
        execution_receipt_id=_stable_id("simulator_command_execution_receipt", payload),
        simulator_command_execution_preflight_ref=preflight_ref,
        simulated_command_proposal_ref=proposal_ref,
        simulated_command_approval_ref=approval_ref,
        simulated_command_rehearsal_result_ref=rehearsal_ref,
        bounded_simulation_run_ref=run_ref,
        proposal_command_category=proposal.command_category,
        execution_category=execution_category,
        receipt_status=(
            SimulatorCommandExecutionReceiptStatus.INTERNAL_STATE_TRANSITION_RECORDED
        ),
        recorded_at=_utc(now),
        evidence_refs=evidence_refs,
        metadata=metadata_payload,
    )


def attach_simulated_delivery_command_artifacts(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_scorecard: DeliveryScorecard | Mapping[str, Any],
    delivery_episode_review: DeliveryEpisodeReview | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    operator_minimal_delivery_simulation_status: (
        OperatorMinimalDeliverySimulationStatus | Mapping[str, Any]
    ),
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    command_category: SimulatedCommandCategory | str | None = None,
    operator_approved: bool = True,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach simulator-only proposal, approval, and receipt without status changes."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise SimulatedDeliveryCommandError(
            f"task {task_id} not found; cannot attach simulated command artifacts"
        )
    proposal = build_simulated_command_proposal(
        delivery_mission_contract=delivery_mission_contract,
        simulated_delivery_episode=simulated_delivery_episode,
        delivery_scorecard=delivery_scorecard,
        delivery_episode_review=delivery_episode_review,
        delivery_recovery_decision=delivery_recovery_decision,
        operator_minimal_delivery_simulation_status=(
            operator_minimal_delivery_simulation_status
        ),
        hil_telemetry_review=hil_telemetry_review,
        autonomy_gate_result=autonomy_gate_result,
        command_category=command_category,
        now=now,
        metadata=metadata,
    )
    approval = build_simulated_command_approval(
        simulated_command_proposal=proposal,
        operator_approved=operator_approved,
        now=now,
    )
    receipt = build_simulated_command_receipt(
        simulated_command_proposal=proposal,
        simulated_command_approval=approval,
        now=now,
    )
    artifacts = {
        "simulated_command_proposal": proposal.model_dump(mode="json"),
        "simulated_command_approval": approval.model_dump(mode="json"),
        "simulated_command_receipt": receipt.model_dump(mode="json"),
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise SimulatedDeliveryCommandError(
            f"task {task_id} disappeared while attaching simulated command artifacts"
        )
    return artifacts


def attach_simulated_command_rehearsal_result(
    task_id: str,
    *,
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any] | None,
    bounded_simulation_request: PX4GazeboBoundedSimulationRequest | Mapping[str, Any],
    bounded_simulation_run: PX4GazeboBoundedSimulationRun | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    operator_minimal_delivery_simulation_status: (
        OperatorMinimalDeliverySimulationStatus | Mapping[str, Any]
    ),
    now: datetime | None = None,
    approval_max_age_seconds: float = 300.0,
    metadata: dict[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach a command rehearsal result without re-running Gazebo or dispatching."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise SimulatedDeliveryCommandError(
            f"task {task_id} not found; cannot attach simulated command rehearsal"
        )
    result = build_simulated_command_rehearsal_result(
        simulated_command_proposal=simulated_command_proposal,
        simulated_command_approval=simulated_command_approval,
        bounded_simulation_request=bounded_simulation_request,
        bounded_simulation_run=bounded_simulation_run,
        simulated_delivery_episode=simulated_delivery_episode,
        delivery_recovery_decision=delivery_recovery_decision,
        operator_minimal_delivery_simulation_status=(
            operator_minimal_delivery_simulation_status
        ),
        now=now,
        approval_max_age_seconds=approval_max_age_seconds,
        metadata=metadata,
    )
    artifacts = {"simulated_command_rehearsal_result": result.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise SimulatedDeliveryCommandError(
            f"task {task_id} disappeared while attaching simulated command rehearsal"
        )
    return artifacts


def attach_simulator_command_execution_preflight(
    task_id: str,
    *,
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any],
    simulated_command_receipt: SimulatedCommandReceipt | Mapping[str, Any],
    simulated_command_rehearsal_result: (
        SimulatedCommandRehearsalResult | Mapping[str, Any]
    ),
    bounded_simulation_run: PX4GazeboBoundedSimulationRun | Mapping[str, Any],
    simulated_delivery_episode: SimulatedDeliveryEpisode | Mapping[str, Any],
    delivery_scorecard: DeliveryScorecard | Mapping[str, Any],
    delivery_episode_review: DeliveryEpisodeReview | Mapping[str, Any],
    delivery_recovery_decision: DeliveryRecoveryDecision | Mapping[str, Any],
    operator_minimal_delivery_simulation_status: (
        OperatorMinimalDeliverySimulationStatus | Mapping[str, Any]
    ),
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    now: datetime | None = None,
    approval_max_age_seconds: float = 300.0,
    metadata: dict[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach simulator command execution preflight without dispatching."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise SimulatedDeliveryCommandError(
            f"task {task_id} not found; cannot attach simulator command preflight"
        )
    preflight = build_simulator_command_execution_preflight(
        simulated_command_proposal=simulated_command_proposal,
        simulated_command_approval=simulated_command_approval,
        simulated_command_receipt=simulated_command_receipt,
        simulated_command_rehearsal_result=simulated_command_rehearsal_result,
        bounded_simulation_run=bounded_simulation_run,
        simulated_delivery_episode=simulated_delivery_episode,
        delivery_scorecard=delivery_scorecard,
        delivery_episode_review=delivery_episode_review,
        delivery_recovery_decision=delivery_recovery_decision,
        operator_minimal_delivery_simulation_status=(
            operator_minimal_delivery_simulation_status
        ),
        hil_telemetry_review=hil_telemetry_review,
        autonomy_gate_result=autonomy_gate_result,
        now=now,
        approval_max_age_seconds=approval_max_age_seconds,
        metadata=metadata,
    )
    artifacts = {
        "simulator_command_execution_preflight": preflight.model_dump(mode="json")
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise SimulatedDeliveryCommandError(
            f"task {task_id} disappeared while attaching simulator command preflight"
        )
    return artifacts


def attach_simulator_command_execution_receipt(
    task_id: str,
    *,
    simulator_command_execution_preflight: (
        SimulatorCommandExecutionPreflight | Mapping[str, Any]
    ),
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any],
    simulated_command_rehearsal_result: (
        SimulatedCommandRehearsalResult | Mapping[str, Any]
    ),
    bounded_simulation_run: PX4GazeboBoundedSimulationRun | Mapping[str, Any],
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach an internal simulator command execution receipt without dispatch."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise SimulatedDeliveryCommandError(
            f"task {task_id} not found; cannot attach simulator command receipt"
        )
    receipt = build_simulator_command_execution_receipt(
        simulator_command_execution_preflight=simulator_command_execution_preflight,
        simulated_command_proposal=simulated_command_proposal,
        simulated_command_approval=simulated_command_approval,
        simulated_command_rehearsal_result=simulated_command_rehearsal_result,
        bounded_simulation_run=bounded_simulation_run,
        now=now,
        metadata=metadata,
    )
    artifacts = {"simulator_command_execution_receipt": receipt.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise SimulatedDeliveryCommandError(
            f"task {task_id} disappeared while attaching simulator command receipt"
        )
    return artifacts


__all__ = [
    "SIMULATOR_COMMAND_EXECUTION_PREFLIGHT_SCHEMA_VERSION",
    "SIMULATOR_COMMAND_EXECUTION_RECEIPT_SCHEMA_VERSION",
    "SIMULATED_COMMAND_APPROVAL_SCHEMA_VERSION",
    "SIMULATED_COMMAND_PROPOSAL_SCHEMA_VERSION",
    "SIMULATED_COMMAND_RECEIPT_SCHEMA_VERSION",
    "SIMULATED_COMMAND_REHEARSAL_RESULT_SCHEMA_VERSION",
    "SimulatedCommandApproval",
    "SimulatedCommandApprovalStatus",
    "SimulatedCommandCategory",
    "SimulatedCommandProposal",
    "SimulatedCommandRehearsalResult",
    "SimulatedCommandRehearsalStatus",
    "SimulatedCommandReceipt",
    "SimulatedCommandReceiptStatus",
    "SimulatedDeliveryCommandError",
    "SimulatorCommandExecutionCategory",
    "SimulatorCommandExecutionPreflight",
    "SimulatorCommandExecutionPreflightStatus",
    "SimulatorCommandExecutionReceipt",
    "SimulatorCommandExecutionReceiptStatus",
    "attach_simulator_command_execution_preflight",
    "attach_simulator_command_execution_receipt",
    "attach_simulated_delivery_command_artifacts",
    "attach_simulated_command_rehearsal_result",
    "build_simulator_command_execution_preflight",
    "build_simulator_command_execution_receipt",
    "build_simulated_command_approval",
    "build_simulated_command_proposal",
    "build_simulated_command_rehearsal_result",
    "build_simulated_command_receipt",
]

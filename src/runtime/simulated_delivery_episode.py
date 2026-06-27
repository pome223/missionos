"""Simulated delivery episode artifact.

``simulated_delivery_episode.v1`` records delivery-mission progress through
read-only Mission OS artifacts. It is an episode ledger for simulation/preflight
review only: it does not upload PX4 missions, mutate Gazebo, dispatch
MAVLink/ROS actions, issue return-to-home commands, land vehicles, or execute
actuators.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.runtime.delivery_mission_contract import (
    DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION,
    DeliveryMissionContract,
)
from src.runtime.delivery_mission_gate import (
    DELIVERY_MISSION_GATE_RESULT_SCHEMA_VERSION,
    DELIVERY_MISSION_SCORECARD_SCHEMA_VERSION,
    DeliveryMissionGateResult,
    DeliveryMissionGateStatus,
    DeliveryMissionScorecard,
)
from src.runtime.delivery_mission_policy_review import (
    DELIVERY_MISSION_POLICY_REVIEW_SCHEMA_VERSION,
    DeliveryMissionPolicyReview,
)
from src.runtime.hil_telemetry_review import (
    HIL_REVIEW_BUCKET_STALE,
    HilTelemetryReview,
)
from src.runtime.px4_gazebo_bounded_simulation_runner import (
    PX4GazeboBoundedSimulationRun,
)
from src.runtime.px4_gazebo_sitl_telemetry_run import (
    PX4GazeboSITLTelemetryRun,
    PX4GazeboSITLTelemetrySample,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    PX4GazeboBoundedSimulationRequest,
)
from src.runtime.px4_gazebo_telemetry import (
    Px4GazeboSanitizedTelemetry,
)
from src.runtime.task_store import TaskStore, get_task_store
from src.runtime.toy_grid_world import (
    ToyGridWorldAutonomyGateResult,
)

SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION = "simulated_delivery_episode.v1"
SIMULATED_DELIVERY_STEP_SCHEMA_VERSION = "simulated_delivery_step.v1"
DELIVERY_REPLAY_TRACE_SCHEMA_VERSION = "delivery_replay_trace.v1"


class SimulatedDeliveryEpisodeError(RuntimeError):
    """Raised when a simulated delivery episode cannot be built safely."""


class SimulatedDeliveryEpisodePhase(str, Enum):
    PLANNED = "planned"
    PREFLIGHT_REVIEW = "preflight_review"
    IN_SIMULATION = "in_simulation"
    PREFLIGHT = "preflight"
    TAKEOFF = "takeoff"
    STAGED_ASCENT = "staged_ascent"
    CRUISE = "cruise"
    SUMMIT_APPROACH = "summit_approach"
    DROPOFF_APPROACH = "dropoff_approach"
    DROPOFF_VERIFIED = "dropoff_verified"
    RETURN_OR_LAND = "return_or_land"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ABORTED = "aborted"
    OPERATOR_ESCALATION_REQUIRED = "operator_escalation_required"


class SimulatedDeliveryFinalStatus(str, Enum):
    READY_FOR_SIMULATION = "ready_for_simulation"
    READY_WITH_WARNINGS = "ready_with_warnings"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ABORTED = "aborted"
    OPERATOR_ESCALATION_REQUIRED = "operator_escalation_required"


class SimulatedDeliveryStepStatus(str, Enum):
    PLANNED = "planned"
    OBSERVED = "observed"
    BLOCKED = "blocked"
    COMPLETED = "completed"


class SimulatedDeliveryCriterionStatus(str, Enum):
    PENDING = "pending"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"


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
        raise SimulatedDeliveryEpisodeError(
            "simulated delivery episode refused command-like keys: "
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
    return tuple(
        sorted({str(item).strip() for item in (values or ()) if str(item).strip()})
    )


class SimulatedDeliverySuccessCriterionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion: str
    status: SimulatedDeliveryCriterionStatus
    evidence_refs: tuple[str, ...] = ()
    reason: str = ""


class DeliveryReplayTraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    t_relative_seconds: float = Field(ge=0)
    phase: SimulatedDeliveryEpisodePhase
    event_type: Literal[
        "phase_observed",
        "gate_observed",
        "dropoff_evidence_observed",
        "completion_observed",
        "blocked",
    ]
    artifact_ref: str
    status: SimulatedDeliveryStepStatus


class DeliveryReplayTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_REPLAY_TRACE_SCHEMA_VERSION] = (
        DELIVERY_REPLAY_TRACE_SCHEMA_VERSION
    )
    trace_id: str
    episode_ref: str
    delivery_mission_contract_ref: str
    bounded_simulation_request_ref: str
    bounded_simulation_run_ref: str
    sitl_telemetry_run_ref: str = ""
    autonomy_gate_result_ref: str
    events: tuple[DeliveryReplayTraceEvent, ...]
    redacted_for_review: Literal[True] = True
    raw_logs_included: Literal[False] = False
    sqlite_included: Literal[False] = False
    full_telemetry_included: Literal[False] = False
    runtime_script_names_included: Literal[False] = False
    transport_details_included: Literal[False] = False
    output_paths_included: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False


class SimulatedDeliveryStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SIMULATED_DELIVERY_STEP_SCHEMA_VERSION] = (
        SIMULATED_DELIVERY_STEP_SCHEMA_VERSION
    )
    step_id: str
    phase: SimulatedDeliveryEpisodePhase
    status: SimulatedDeliveryStepStatus
    summary: str
    telemetry_window_ref: str = ""
    policy_review_ref: str = ""
    gate_ref: str = ""
    telemetry_refs: tuple[str, ...] = ()
    policy_review_refs: tuple[str, ...] = ()
    scorecard_refs: tuple[str, ...] = ()
    gate_refs: tuple[str, ...] = ()
    bounded_simulation_request_ref: str = ""
    bounded_simulation_run_ref: str = ""
    sitl_telemetry_run_ref: str = ""
    autonomy_gate_result_ref: str = ""
    hil_telemetry_review_ref: str = ""
    delivery_replay_trace_ref: str = ""
    dropoff_evidence_ref: str = ""
    dropoff_verified: bool = False
    success_criteria_status: tuple[SimulatedDeliverySuccessCriterionStatus, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    observed_at: datetime
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _reject_command_like_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        findings = _command_like_key_paths(value, root="metadata")
        if findings:
            raise ValueError(
                "simulated delivery step refused command-like metadata keys: "
                + ", ".join(sorted(findings))
            )
        return value


class SimulatedDeliveryEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION] = (
        SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    )
    episode_id: str
    mission_id: str
    delivery_mission_contract_id: str
    phase: SimulatedDeliveryEpisodePhase
    steps: tuple[SimulatedDeliveryStep, ...] = ()
    telemetry_refs: tuple[str, ...] = ()
    policy_review_refs: tuple[str, ...] = ()
    scorecard_refs: tuple[str, ...] = ()
    gate_refs: tuple[str, ...] = ()
    bounded_simulation_request_ref: str = ""
    bounded_simulation_run_ref: str = ""
    sitl_telemetry_run_ref: str = ""
    autonomy_gate_result_ref: str = ""
    hil_telemetry_review_ref: str = ""
    delivery_replay_trace_ref: str = ""
    dropoff_evidence_ref: str = ""
    dropoff_verified: bool = False
    success_criteria_status: tuple[SimulatedDeliverySuccessCriterionStatus, ...] = ()
    phase_history: tuple[SimulatedDeliveryEpisodePhase, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    final_status: SimulatedDeliveryFinalStatus
    passed: bool
    operator_escalation_required: bool = False
    return_to_home_recommended: bool = False
    abort_recommended: bool = False
    created_at: datetime
    delivery_mission_contract_schema_version: Literal[
        DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_CONTRACT_SCHEMA_VERSION
    delivery_mission_policy_review_schema_version: Literal[
        DELIVERY_MISSION_POLICY_REVIEW_SCHEMA_VERSION
    ] = DELIVERY_MISSION_POLICY_REVIEW_SCHEMA_VERSION
    delivery_mission_scorecard_schema_version: Literal[
        DELIVERY_MISSION_SCORECARD_SCHEMA_VERSION
    ] = DELIVERY_MISSION_SCORECARD_SCHEMA_VERSION
    delivery_mission_gate_result_schema_version: Literal[
        DELIVERY_MISSION_GATE_RESULT_SCHEMA_VERSION
    ] = DELIVERY_MISSION_GATE_RESULT_SCHEMA_VERSION
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    gazebo_execution_invoked_by_episode: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    approval_promotion_reuse_created: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _reject_command_like_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        findings = _command_like_key_paths(value, root="metadata")
        if findings:
            raise ValueError(
                "simulated delivery episode refused command-like metadata keys: "
                + ", ".join(sorted(findings))
            )
        return value


def _to_contract(
    value: DeliveryMissionContract | Mapping[str, Any],
) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_policy_review(
    value: DeliveryMissionPolicyReview | Mapping[str, Any],
) -> DeliveryMissionPolicyReview:
    if isinstance(value, DeliveryMissionPolicyReview):
        return value
    return DeliveryMissionPolicyReview.model_validate(dict(value))


def _to_scorecard(
    value: DeliveryMissionScorecard | Mapping[str, Any],
) -> DeliveryMissionScorecard:
    if isinstance(value, DeliveryMissionScorecard):
        return value
    return DeliveryMissionScorecard.model_validate(dict(value))


def _to_gate(
    value: DeliveryMissionGateResult | Mapping[str, Any],
) -> DeliveryMissionGateResult:
    if isinstance(value, DeliveryMissionGateResult):
        return value
    return DeliveryMissionGateResult.model_validate(dict(value))


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


def _to_sitl_run(
    value: PX4GazeboSITLTelemetryRun | Mapping[str, Any],
) -> PX4GazeboSITLTelemetryRun:
    if isinstance(value, PX4GazeboSITLTelemetryRun):
        return value
    return PX4GazeboSITLTelemetryRun.model_validate(dict(value))


def _to_sanitized_telemetry(
    value: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
) -> Px4GazeboSanitizedTelemetry:
    if isinstance(value, Px4GazeboSanitizedTelemetry):
        return value
    return Px4GazeboSanitizedTelemetry.model_validate(dict(value))


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


def _bounded_request_ref(request: PX4GazeboBoundedSimulationRequest) -> str:
    return f"px4_gazebo_bounded_simulation_request:{request.request_id}"


def _bounded_run_ref(run: PX4GazeboBoundedSimulationRun) -> str:
    return f"px4_gazebo_bounded_simulation_run:{run.run_id}"


def _sitl_run_ref(run: PX4GazeboSITLTelemetryRun) -> str:
    return f"px4_gazebo_sitl_telemetry_run:{run.run_id}"


def _autonomy_gate_ref(gate: ToyGridWorldAutonomyGateResult) -> str:
    return f"autonomy_gate_result:{gate.gate_id}"


def _hil_review_ref(review: HilTelemetryReview) -> str:
    return f"hil_telemetry_review:{review.review_id}"


def _sanitized_telemetry_ref(telemetry: Px4GazeboSanitizedTelemetry) -> str:
    return f"px4_gazebo_sanitized_telemetry:{telemetry.telemetry_id}"


def _default_success_criteria_status(
    contract: DeliveryMissionContract,
    *,
    blocked: bool,
) -> tuple[SimulatedDeliverySuccessCriterionStatus, ...]:
    status = (
        SimulatedDeliveryCriterionStatus.BLOCKED
        if blocked
        else SimulatedDeliveryCriterionStatus.PENDING
    )
    reason = (
        "delivery_episode_blocked_before_simulation"
        if blocked
        else "delivery_episode_not_executed_yet"
    )
    return tuple(
        SimulatedDeliverySuccessCriterionStatus(
            criterion=criterion,
            status=status,
            reason=reason,
        )
        for criterion in contract.success_criteria
    )


def _default_step(
    *,
    phase: SimulatedDeliveryEpisodePhase,
    status: SimulatedDeliveryStepStatus,
    success_criteria_status: tuple[SimulatedDeliverySuccessCriterionStatus, ...],
    telemetry_refs: tuple[str, ...],
    policy_review_refs: tuple[str, ...],
    scorecard_refs: tuple[str, ...],
    gate_refs: tuple[str, ...],
    blocked_reasons: tuple[str, ...],
    warning_reasons: tuple[str, ...],
    observed_at: datetime,
) -> SimulatedDeliveryStep:
    payload = {
        "phase": phase.value,
        "status": status.value,
        "telemetry_refs": telemetry_refs,
        "policy_review_refs": policy_review_refs,
        "scorecard_refs": scorecard_refs,
        "gate_refs": gate_refs,
        "success_criteria_status": [
            item.model_dump(mode="json") for item in success_criteria_status
        ],
        "blocked_reasons": blocked_reasons,
        "warning_reasons": warning_reasons,
    }
    telemetry_window_ref = next(
        (
            ref
            for ref in telemetry_refs
            if ref.startswith("gazebo_delivery_telemetry_window:")
        ),
        telemetry_refs[0] if telemetry_refs else "",
    )
    policy_review_ref = policy_review_refs[0] if policy_review_refs else ""
    gate_ref = gate_refs[0] if gate_refs else ""
    return SimulatedDeliveryStep(
        step_id=_stable_id("simulated_delivery_step", payload),
        phase=phase,
        status=status,
        summary="delivery mission preflight policy/gate review",
        telemetry_window_ref=telemetry_window_ref,
        policy_review_ref=policy_review_ref,
        gate_ref=gate_ref,
        telemetry_refs=telemetry_refs,
        policy_review_refs=policy_review_refs,
        scorecard_refs=scorecard_refs,
        gate_refs=gate_refs,
        success_criteria_status=success_criteria_status,
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        observed_at=observed_at,
        metadata={
            "step_kind": "preflight_policy_gate_review",
            "artifact_only": True,
            "no_command_surface": True,
        },
    )


def build_simulated_delivery_episode(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_mission_policy_review: DeliveryMissionPolicyReview | Mapping[str, Any],
    delivery_mission_scorecard: DeliveryMissionScorecard | Mapping[str, Any],
    delivery_mission_gate_result: DeliveryMissionGateResult | Mapping[str, Any],
    telemetry_refs: Sequence[str] | None = None,
    extra_steps: Sequence[SimulatedDeliveryStep | Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> SimulatedDeliveryEpisode:
    """Build a read-only simulated delivery episode ledger.

    The episode records policy/gate state and references. It never advances the
    delivery by command, dispatch, Gazebo mutation, or live physical execution.
    """

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    contract = _to_contract(delivery_mission_contract)
    policy_review = _to_policy_review(delivery_mission_policy_review)
    scorecard = _to_scorecard(delivery_mission_scorecard)
    gate = _to_gate(delivery_mission_gate_result)
    created_at = _utc(now)

    if policy_review.delivery_mission_contract_id != contract.contract_id:
        raise SimulatedDeliveryEpisodeError(
            "delivery policy review contract_id mismatch"
        )
    if policy_review.delivery_mission_id != contract.mission_id:
        raise SimulatedDeliveryEpisodeError(
            "delivery policy review mission_id mismatch"
        )
    if scorecard.delivery_mission_contract_id != contract.contract_id:
        raise SimulatedDeliveryEpisodeError("delivery scorecard contract_id mismatch")
    if scorecard.delivery_mission_policy_review_id != policy_review.review_id:
        raise SimulatedDeliveryEpisodeError(
            "delivery scorecard policy_review_id mismatch"
        )
    if gate.delivery_mission_contract_id != contract.contract_id:
        raise SimulatedDeliveryEpisodeError("delivery gate contract_id mismatch")
    if gate.delivery_mission_policy_review_id != policy_review.review_id:
        raise SimulatedDeliveryEpisodeError("delivery gate policy_review_id mismatch")
    if gate.delivery_mission_scorecard_id != scorecard.scorecard_id:
        raise SimulatedDeliveryEpisodeError("delivery gate scorecard_id mismatch")

    refs = _as_tuple(telemetry_refs)
    if policy_review.sanitized_telemetry_id:
        refs = _as_tuple(
            [
                *refs,
                f"px4_gazebo_sanitized_telemetry:{policy_review.sanitized_telemetry_id}",
            ]
        )
    if policy_review.hil_telemetry_review_id:
        refs = _as_tuple(
            [
                *refs,
                f"hil_telemetry_review:{policy_review.hil_telemetry_review_id}",
            ]
        )
    policy_review_refs = (f"delivery_mission_policy_review:{policy_review.review_id}",)
    scorecard_refs = (f"delivery_mission_scorecard:{scorecard.scorecard_id}",)
    gate_refs = (f"delivery_mission_gate_result:{gate.gate_id}",)
    blocked_reasons = _as_tuple([*scorecard.blocked_reasons, *gate.blocked_reasons])
    warning_reasons = _as_tuple([*scorecard.warning_reasons, *gate.warning_reasons])
    blocked = gate.status is DeliveryMissionGateStatus.BLOCKED
    if blocked:
        phase = SimulatedDeliveryEpisodePhase.BLOCKED
        final_status = SimulatedDeliveryFinalStatus.BLOCKED
        step_status = SimulatedDeliveryStepStatus.BLOCKED
    elif warning_reasons:
        phase = SimulatedDeliveryEpisodePhase.PREFLIGHT_REVIEW
        final_status = SimulatedDeliveryFinalStatus.READY_WITH_WARNINGS
        step_status = SimulatedDeliveryStepStatus.COMPLETED
    else:
        phase = SimulatedDeliveryEpisodePhase.PREFLIGHT_REVIEW
        final_status = SimulatedDeliveryFinalStatus.READY_FOR_SIMULATION
        step_status = SimulatedDeliveryStepStatus.COMPLETED

    success_criteria_status = _default_success_criteria_status(
        contract,
        blocked=blocked,
    )
    default_step = _default_step(
        phase=phase,
        status=step_status,
        success_criteria_status=success_criteria_status,
        telemetry_refs=refs,
        policy_review_refs=policy_review_refs,
        scorecard_refs=scorecard_refs,
        gate_refs=gate_refs,
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        observed_at=created_at,
    )
    steps = [default_step]
    for step in extra_steps or ():
        validated = (
            step
            if isinstance(step, SimulatedDeliveryStep)
            else SimulatedDeliveryStep.model_validate(dict(step))
        )
        _raise_for_command_like_keys(validated.metadata, root="step.metadata")
        steps.append(validated)
    payload = {
        "delivery_mission_contract_id": contract.contract_id,
        "delivery_mission_policy_review_id": policy_review.review_id,
        "delivery_mission_scorecard_id": scorecard.scorecard_id,
        "delivery_mission_gate_id": gate.gate_id,
        "telemetry_refs": refs,
        "blocked_reasons": blocked_reasons,
        "warning_reasons": warning_reasons,
        "final_status": final_status.value,
    }
    return SimulatedDeliveryEpisode(
        episode_id=_stable_id("simulated_delivery_episode", payload),
        mission_id=contract.mission_id,
        delivery_mission_contract_id=contract.contract_id,
        phase=phase,
        steps=tuple(steps),
        telemetry_refs=refs,
        policy_review_refs=policy_review_refs,
        scorecard_refs=scorecard_refs,
        gate_refs=gate_refs,
        success_criteria_status=success_criteria_status,
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        final_status=final_status,
        passed=not blocked,
        operator_escalation_required=(
            gate.operator_escalation_required or scorecard.operator_escalation_required
        ),
        return_to_home_recommended=(
            gate.return_to_home_recommended or scorecard.return_to_home_recommended
        ),
        abort_recommended=gate.abort_recommended or scorecard.abort_recommended,
        created_at=created_at,
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "simulated_delivery_episode_only": True,
            "preflight_or_simulation_record_only": True,
            "return_to_home_is_recommendation_only": True,
            "abort_is_recommendation_only": True,
            "no_dispatch_surface": True,
        },
    )


def _dropoff_outcome(
    dropoff_evidence: Mapping[str, Any] | None,
    *,
    max_dropoff_error_m: float,
) -> tuple[bool, str, tuple[str, ...]]:
    if not dropoff_evidence:
        return False, "", ("missing_dropoff_evidence",)
    evidence = dict(dropoff_evidence)
    _raise_for_command_like_keys(evidence, root="dropoff_evidence")
    evidence_ref = str(evidence.get("evidence_ref") or "").strip()
    if not evidence_ref:
        evidence_ref = "simulated_dropoff_evidence:" + _stable_id("dropoff", evidence)
    if evidence.get("dropoff_verified") is not True:
        return False, evidence_ref, ("dropoff_not_verified",)
    landing_error = evidence.get("landing_error_m")
    if landing_error is not None and float(landing_error) > max_dropoff_error_m:
        return False, evidence_ref, ("dropoff_landing_error_exceeded",)
    return True, evidence_ref, ()


def _bounded_delivery_step(
    *,
    phase: SimulatedDeliveryEpisodePhase,
    status: SimulatedDeliveryStepStatus,
    summary: str,
    observed_at: datetime,
    telemetry_refs: tuple[str, ...],
    gate_refs: tuple[str, ...],
    sitl_telemetry_run_ref: str = "",
    blocked_reasons: tuple[str, ...] = (),
    warning_reasons: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> SimulatedDeliveryStep:
    payload = {
        "phase": phase.value,
        "status": status.value,
        "summary": summary,
        "telemetry_refs": telemetry_refs,
        "gate_refs": gate_refs,
        "blocked_reasons": blocked_reasons,
        "warning_reasons": warning_reasons,
        "metadata": metadata or {},
    }
    return SimulatedDeliveryStep(
        step_id=_stable_id("simulated_delivery_step", payload),
        phase=phase,
        status=status,
        summary=summary,
        gate_ref=gate_refs[0] if gate_refs else "",
        telemetry_refs=telemetry_refs,
        gate_refs=gate_refs,
        sitl_telemetry_run_ref=sitl_telemetry_run_ref,
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        observed_at=observed_at,
        metadata={
            "step_kind": "bounded_gazebo_delivery_phase",
            "artifact_only": True,
            "no_new_gazebo_execution": True,
            "no_command_surface": True,
            **(metadata or {}),
        },
    )


def _sample_value(
    sample: PX4GazeboSITLTelemetrySample | Mapping[str, Any], key: str
) -> Any:
    if isinstance(sample, PX4GazeboSITLTelemetrySample):
        return getattr(sample, key)
    return sample.get(key)


def _sample_position(
    sample: PX4GazeboSITLTelemetrySample | Mapping[str, Any],
) -> tuple[float, float, float]:
    return (
        float(_sample_value(sample, "position_x_m") or 0.0),
        float(_sample_value(sample, "position_y_m") or 0.0),
        float(_sample_value(sample, "position_z_m") or 0.0),
    )


def _sample_text(
    sample: PX4GazeboSITLTelemetrySample | Mapping[str, Any],
    key: str,
) -> str:
    return str(_sample_value(sample, key) or "").lower()


def _append_phase(
    phases: list[SimulatedDeliveryEpisodePhase],
    phase: SimulatedDeliveryEpisodePhase,
) -> None:
    if not phases or phases[-1] is not phase:
        phases.append(phase)


def _dist_xy(x: float, y: float, target: tuple[float, float]) -> float:
    return math.hypot(target[0] - x, target[1] - y)


def _xy_from_metadata(value: Any, *, field_name: str) -> tuple[float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SimulatedDeliveryEpisodeError(
            f"delivery mission contract metadata requires {field_name}"
        )
    if len(value) != 2:
        raise SimulatedDeliveryEpisodeError(
            f"delivery mission contract metadata {field_name} must contain x/y"
        )
    return (float(value[0]), float(value[1]))


def _sitl_phase_geometry_from_contract(
    contract: DeliveryMissionContract,
) -> dict[str, Any]:
    geometry = contract.metadata.get("sitl_phase_geometry")
    if not isinstance(geometry, Mapping):
        raise SimulatedDeliveryEpisodeError(
            "delivery mission contract requires sitl_phase_geometry metadata for "
            "SITL phase derivation"
        )
    required = (
        "home_xy_m",
        "dropoff_xy_m",
        "takeoff_altitude_m",
        "staged_ascent_altitude_m",
        "dropoff_approach_radius_m",
        "summit_approach_radius_m",
    )
    missing = [key for key in required if key not in geometry]
    if missing:
        raise SimulatedDeliveryEpisodeError(
            "delivery mission contract sitl_phase_geometry missing: "
            + ", ".join(missing)
        )
    return {
        "home_xy": _xy_from_metadata(geometry["home_xy_m"], field_name="home_xy_m"),
        "dropoff_xy": _xy_from_metadata(
            geometry["dropoff_xy_m"], field_name="dropoff_xy_m"
        ),
        "takeoff_altitude_m": float(geometry["takeoff_altitude_m"]),
        "staged_ascent_altitude_m": float(geometry["staged_ascent_altitude_m"]),
        "dropoff_approach_radius_m": float(geometry["dropoff_approach_radius_m"]),
        "summit_approach_radius_m": float(geometry["summit_approach_radius_m"]),
    }


def derive_simulated_delivery_phase_history_from_sitl_telemetry(
    samples: Sequence[PX4GazeboSITLTelemetrySample | Mapping[str, Any]],
    *,
    home_xy: tuple[float, float],
    dropoff_xy: tuple[float, float],
    takeoff_altitude_m: float,
    staged_ascent_altitude_m: float,
    dropoff_approach_radius_m: float,
    summit_approach_radius_m: float,
) -> tuple[SimulatedDeliveryEpisodePhase, ...]:
    """Derive delivery phase transitions from SITL mission state and pose.

    ``dropoff_verified`` is intentionally not inferred here. It remains a
    flight-fact/dropoff-evidence contract for the later mission-upload slice.
    """

    if not samples:
        raise SimulatedDeliveryEpisodeError("SITL phase derivation requires samples")
    positions = [_sample_position(sample) for sample in samples]
    phases: list[SimulatedDeliveryEpisodePhase] = []
    max_altitude_seen = positions[0][2]
    approached_dropoff = False

    for index, sample in enumerate(samples):
        x, y, altitude = positions[index]
        previous_x, previous_y, previous_altitude = (
            positions[index - 1] if index > 0 else positions[index]
        )
        mode = _sample_text(sample, "flight_mode")
        state = _sample_text(sample, "mission_state")
        previous_dropoff_distance = _dist_xy(previous_x, previous_y, dropoff_xy)
        dropoff_distance = _dist_xy(x, y, dropoff_xy)
        previous_home_distance = _dist_xy(previous_x, previous_y, home_xy)
        home_distance = _dist_xy(x, y, home_xy)
        altitude_delta = altitude - previous_altitude
        max_altitude_seen = max(max_altitude_seen, altitude)

        if index == 0 and (
            state in {"idle", "standby", "manual", "preflight"}
            or mode in {"manual", "stabilize", "standby", "idle"}
            or altitude <= 0.5
        ):
            _append_phase(phases, SimulatedDeliveryEpisodePhase.PREFLIGHT)

        if (
            "takeoff" in mode
            or "takeoff" in state
            or (
                previous_altitude < takeoff_altitude_m
                and altitude >= takeoff_altitude_m
                and altitude_delta > 0.25
            )
        ):
            _append_phase(phases, SimulatedDeliveryEpisodePhase.TAKEOFF)

        if "ascent" in state or (
            state in {"", "idle", "takeoff", "preflight"}
            and altitude >= staged_ascent_altitude_m
            and altitude_delta > 0.25
            and max_altitude_seen >= staged_ascent_altitude_m
        ):
            _append_phase(phases, SimulatedDeliveryEpisodePhase.STAGED_ASCENT)

        if "cruise" in state or (
            not any(
                token in state
                for token in (
                    "ascent",
                    "summit",
                    "dropoff",
                    "return",
                    "land",
                    "completed",
                )
            )
            and altitude >= takeoff_altitude_m
            and previous_dropoff_distance - dropoff_distance > 0.5
        ):
            _append_phase(phases, SimulatedDeliveryEpisodePhase.CRUISE)

        if "summit" in state or dropoff_distance <= summit_approach_radius_m:
            _append_phase(phases, SimulatedDeliveryEpisodePhase.SUMMIT_APPROACH)

        if "dropoff_approach" in state or dropoff_distance <= dropoff_approach_radius_m:
            approached_dropoff = True
            _append_phase(phases, SimulatedDeliveryEpisodePhase.DROPOFF_APPROACH)

        if (
            "rtl" in mode
            or "land" in mode
            or "return" in state
            or "land" in state
            or (
                approached_dropoff
                and (
                    previous_home_distance - home_distance > 0.5
                    or (
                        max_altitude_seen >= takeoff_altitude_m
                        and altitude_delta < -0.5
                    )
                )
            )
        ):
            _append_phase(phases, SimulatedDeliveryEpisodePhase.RETURN_OR_LAND)

        if "completed" in state and altitude <= 0.5 and home_distance <= 2.0:
            _append_phase(phases, SimulatedDeliveryEpisodePhase.COMPLETED)

    if not phases:
        _append_phase(phases, SimulatedDeliveryEpisodePhase.PREFLIGHT)
    return tuple(phases)


def _delivery_replay_trace(
    *,
    episode: SimulatedDeliveryEpisode,
    delivery_mission_contract_ref: str,
    bounded_simulation_request_ref: str,
    bounded_simulation_run_ref: str,
    autonomy_gate_result_ref: str,
    sitl_telemetry_run_ref: str = "",
) -> DeliveryReplayTrace:
    episode_ref = f"simulated_delivery_episode:{episode.episode_id}"
    events = tuple(
        DeliveryReplayTraceEvent(
            t_relative_seconds=float(index),
            phase=step.phase,
            event_type=(
                "dropoff_evidence_observed"
                if step.phase is SimulatedDeliveryEpisodePhase.DROPOFF_VERIFIED
                else (
                    "completion_observed"
                    if step.phase is SimulatedDeliveryEpisodePhase.COMPLETED
                    else (
                        "blocked"
                        if step.status is SimulatedDeliveryStepStatus.BLOCKED
                        else "phase_observed"
                    )
                )
            ),
            artifact_ref=f"simulated_delivery_step:{step.step_id}",
            status=step.status,
        )
        for index, step in enumerate(episode.steps)
    )
    payload = {
        "episode_ref": episode_ref,
        "contract_ref": delivery_mission_contract_ref,
        "request_ref": bounded_simulation_request_ref,
        "run_ref": bounded_simulation_run_ref,
        "sitl_run_ref": sitl_telemetry_run_ref,
        "gate_ref": autonomy_gate_result_ref,
        "event_count": len(events),
    }
    return DeliveryReplayTrace(
        trace_id=_stable_id("delivery_replay_trace", payload),
        episode_ref=episode_ref,
        delivery_mission_contract_ref=delivery_mission_contract_ref,
        bounded_simulation_request_ref=bounded_simulation_request_ref,
        bounded_simulation_run_ref=bounded_simulation_run_ref,
        sitl_telemetry_run_ref=sitl_telemetry_run_ref,
        autonomy_gate_result_ref=autonomy_gate_result_ref,
        events=events,
    )


def build_simulated_delivery_episode_from_bounded_gazebo_run(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    bounded_simulation_request: PX4GazeboBoundedSimulationRequest | Mapping[str, Any],
    bounded_simulation_run: PX4GazeboBoundedSimulationRun | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    dropoff_evidence: Mapping[str, Any] | None = None,
    max_dropoff_error_m: float = 0.5,
    now: datetime | None = None,
) -> dict[str, SimulatedDeliveryEpisode | DeliveryReplayTrace]:
    """Integrate an existing bounded Gazebo run into a delivery episode.

    This builder consumes already-created Mission OS artifacts. It never starts
    Gazebo, dispatches MAVLink/ROS, mutates a simulator, promotes memory, or
    creates approval/reuse artifacts.
    """

    contract = _to_contract(delivery_mission_contract)
    request = _to_bounded_request(bounded_simulation_request)
    run = _to_bounded_run(bounded_simulation_run)
    telemetry = _to_sanitized_telemetry(sanitized_telemetry)
    hil_review = _to_hil_review(hil_telemetry_review)
    autonomy_gate = _to_autonomy_gate(autonomy_gate_result)
    created_at = _utc(now)

    request_ref = _bounded_request_ref(request)
    run_ref = _bounded_run_ref(run)
    telemetry_ref = _sanitized_telemetry_ref(telemetry)
    hil_ref = _hil_review_ref(hil_review)
    autonomy_ref = _autonomy_gate_ref(autonomy_gate)
    contract_ref = f"delivery_mission_contract:{contract.contract_id}"

    if run.request_ref != request_ref:
        raise SimulatedDeliveryEpisodeError("bounded run/request ref mismatch")
    if run.hil_review_ref != hil_ref:
        raise SimulatedDeliveryEpisodeError("bounded run/HIL review ref mismatch")
    if run.gate_ref != autonomy_ref:
        raise SimulatedDeliveryEpisodeError("bounded run/autonomy gate ref mismatch")
    if telemetry_ref not in run.telemetry_refs:
        raise SimulatedDeliveryEpisodeError("bounded run/telemetry ref mismatch")

    dropoff_verified, dropoff_ref, dropoff_blocked = _dropoff_outcome(
        dropoff_evidence,
        max_dropoff_error_m=max_dropoff_error_m,
    )
    blocked: list[str] = []
    if run.status != "completed":
        blocked.append("bounded_gazebo_run_not_completed")
    if not hil_review.passed:
        blocked.extend(hil_review.blocked_reasons or ("hil_telemetry_review_blocked",))
    if HIL_REVIEW_BUCKET_STALE in hil_review.blocked_reasons:
        blocked.append("stale_telemetry_blocks_delivery_episode_completion")
    if not autonomy_gate.passed:
        blocked.extend(autonomy_gate.blocked_reasons or ("autonomy_gate_failed",))
    blocked.extend(dropoff_blocked)
    blocked_reasons = _as_tuple(blocked)

    evidence_refs = _as_tuple(
        [
            request_ref,
            run_ref,
            telemetry_ref,
            hil_ref,
            autonomy_ref,
            *([dropoff_ref] if dropoff_ref else []),
        ]
    )
    criteria_status = tuple(
        SimulatedDeliverySuccessCriterionStatus(
            criterion=criterion,
            status=(
                SimulatedDeliveryCriterionStatus.SATISFIED
                if not blocked_reasons
                else SimulatedDeliveryCriterionStatus.BLOCKED
            ),
            evidence_refs=evidence_refs if not blocked_reasons else (),
            reason=(
                "bounded_gazebo_delivery_episode_completed"
                if not blocked_reasons
                else "bounded_gazebo_delivery_episode_blocked"
            ),
        )
        for criterion in contract.success_criteria
    )

    common_metadata = {
        "bounded_simulation_request_ref": request_ref,
        "bounded_simulation_run_ref": run_ref,
        "scenario_profile": request.scenario_profile,
        "route_profile": request.route_profile,
    }
    telemetry_refs = (telemetry_ref, hil_ref, run_ref)
    gate_refs = (autonomy_ref,)
    steps: list[SimulatedDeliveryStep] = [
        _bounded_delivery_step(
            phase=SimulatedDeliveryEpisodePhase.PREFLIGHT,
            status=SimulatedDeliveryStepStatus.COMPLETED,
            summary="bounded request and safety boundary verified",
            observed_at=created_at,
            telemetry_refs=telemetry_refs,
            gate_refs=gate_refs,
            metadata=common_metadata,
        )
    ]
    if run.status != "completed":
        phase = SimulatedDeliveryEpisodePhase.ABORTED
        final_status = SimulatedDeliveryFinalStatus.ABORTED
        steps.append(
            _bounded_delivery_step(
                phase=SimulatedDeliveryEpisodePhase.ABORTED,
                status=SimulatedDeliveryStepStatus.BLOCKED,
                summary="bounded Gazebo run did not complete",
                observed_at=created_at,
                telemetry_refs=telemetry_refs,
                gate_refs=gate_refs,
                blocked_reasons=blocked_reasons,
                metadata=common_metadata,
            )
        )
    elif blocked_reasons:
        phase = SimulatedDeliveryEpisodePhase.OPERATOR_ESCALATION_REQUIRED
        final_status = SimulatedDeliveryFinalStatus.OPERATOR_ESCALATION_REQUIRED
        steps.append(
            _bounded_delivery_step(
                phase=SimulatedDeliveryEpisodePhase.OPERATOR_ESCALATION_REQUIRED,
                status=SimulatedDeliveryStepStatus.BLOCKED,
                summary="bounded Gazebo episode completion blocked",
                observed_at=created_at,
                telemetry_refs=telemetry_refs,
                gate_refs=gate_refs,
                blocked_reasons=blocked_reasons,
                metadata=common_metadata,
            )
        )
    else:
        phase = SimulatedDeliveryEpisodePhase.COMPLETED
        final_status = SimulatedDeliveryFinalStatus.COMPLETED
        for step_phase, summary in (
            (SimulatedDeliveryEpisodePhase.TAKEOFF, "takeoff phase observed"),
            (
                SimulatedDeliveryEpisodePhase.STAGED_ASCENT,
                "staged ascent phase observed",
            ),
            (SimulatedDeliveryEpisodePhase.CRUISE, "cruise phase observed"),
            (
                SimulatedDeliveryEpisodePhase.SUMMIT_APPROACH,
                "summit approach phase observed",
            ),
            (
                SimulatedDeliveryEpisodePhase.DROPOFF_APPROACH,
                "dropoff approach phase observed",
            ),
            (
                SimulatedDeliveryEpisodePhase.DROPOFF_VERIFIED,
                "dropoff evidence verified",
            ),
            (
                SimulatedDeliveryEpisodePhase.RETURN_OR_LAND,
                "return or landing phase observed",
            ),
            (
                SimulatedDeliveryEpisodePhase.COMPLETED,
                "simulated delivery episode completed",
            ),
        ):
            steps.append(
                _bounded_delivery_step(
                    phase=step_phase,
                    status=SimulatedDeliveryStepStatus.COMPLETED,
                    summary=summary,
                    observed_at=created_at,
                    telemetry_refs=telemetry_refs,
                    gate_refs=gate_refs,
                    metadata={
                        **common_metadata,
                        **(
                            {"dropoff_evidence_ref": dropoff_ref}
                            if step_phase
                            is SimulatedDeliveryEpisodePhase.DROPOFF_VERIFIED
                            else {}
                        ),
                    },
                )
            )

    payload = {
        "contract_ref": contract_ref,
        "request_ref": request_ref,
        "run_ref": run_ref,
        "telemetry_ref": telemetry_ref,
        "hil_ref": hil_ref,
        "gate_ref": autonomy_ref,
        "dropoff_ref": dropoff_ref,
        "blocked_reasons": blocked_reasons,
        "final_status": final_status.value,
    }
    episode = SimulatedDeliveryEpisode(
        episode_id=_stable_id("simulated_delivery_episode", payload),
        mission_id=contract.mission_id,
        delivery_mission_contract_id=contract.contract_id,
        phase=phase,
        steps=tuple(steps),
        telemetry_refs=telemetry_refs,
        gate_refs=gate_refs,
        bounded_simulation_request_ref=request_ref,
        bounded_simulation_run_ref=run_ref,
        autonomy_gate_result_ref=autonomy_ref,
        hil_telemetry_review_ref=hil_ref,
        dropoff_evidence_ref=dropoff_ref,
        dropoff_verified=dropoff_verified and not blocked_reasons,
        success_criteria_status=criteria_status,
        phase_history=tuple(step.phase for step in steps),
        blocked_reasons=blocked_reasons,
        warning_reasons=(),
        final_status=final_status,
        passed=not blocked_reasons,
        operator_escalation_required=bool(blocked_reasons),
        abort_recommended=run.status != "completed",
        created_at=created_at,
        metadata={
            "artifact_only": True,
            "simulated_delivery_episode_only": True,
            "bounded_gazebo_run_integrated": True,
            "uses_existing_bounded_gazebo_run": True,
            "no_new_gazebo_execution": True,
            "no_approval_promotion_reuse": True,
            "scenario_profile": request.scenario_profile,
            "route_profile": request.route_profile,
            "bounded_run_world_name": run.world_name,
        },
    )
    trace = _delivery_replay_trace(
        episode=episode,
        delivery_mission_contract_ref=contract_ref,
        bounded_simulation_request_ref=request_ref,
        bounded_simulation_run_ref=run_ref,
        autonomy_gate_result_ref=autonomy_ref,
    )
    episode = episode.model_copy(
        update={"delivery_replay_trace_ref": f"delivery_replay_trace:{trace.trace_id}"}
    )
    trace = trace.model_copy(
        update={"episode_ref": f"simulated_delivery_episode:{episode.episode_id}"}
    )
    return {
        "simulated_delivery_episode": episode,
        "delivery_replay_trace": trace,
    }


def build_simulated_delivery_episode_from_sitl_telemetry_run(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    sitl_telemetry_run: PX4GazeboSITLTelemetryRun | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, SimulatedDeliveryEpisode | DeliveryReplayTrace]:
    """Interpret actual PX4/Gazebo SITL telemetry as a delivery episode.

    This consumes the #408 actual SITL telemetry-only artifact and derives
    phase history from mission-state/pose samples. It never starts Gazebo,
    uploads a PX4 mission, sends MAVLink/ROS commands, or verifies dropoff.
    """

    contract = _to_contract(delivery_mission_contract)
    sitl_run = _to_sitl_run(sitl_telemetry_run)
    telemetry = _to_sanitized_telemetry(sanitized_telemetry)
    hil_review = _to_hil_review(hil_telemetry_review)
    autonomy_gate = _to_autonomy_gate(autonomy_gate_result)
    created_at = _utc(now)

    sitl_ref = _sitl_run_ref(sitl_run)
    telemetry_ref = _sanitized_telemetry_ref(telemetry)
    hil_ref = _hil_review_ref(hil_review)
    autonomy_ref = _autonomy_gate_ref(autonomy_gate)
    contract_ref = f"delivery_mission_contract:{contract.contract_id}"

    if telemetry_ref not in sitl_run.telemetry_refs:
        raise SimulatedDeliveryEpisodeError("SITL run/telemetry ref mismatch")
    if sitl_run.hil_review_ref != hil_ref:
        raise SimulatedDeliveryEpisodeError("SITL run/HIL review ref mismatch")
    if sitl_run.gate_ref != autonomy_ref:
        raise SimulatedDeliveryEpisodeError("SITL run/autonomy gate ref mismatch")

    derived_phases = derive_simulated_delivery_phase_history_from_sitl_telemetry(
        sitl_run.samples,
        **_sitl_phase_geometry_from_contract(contract),
    )
    blocked: list[str] = []
    if not hil_review.passed:
        blocked.extend(hil_review.blocked_reasons or ("hil_telemetry_review_blocked",))
    if HIL_REVIEW_BUCKET_STALE in hil_review.blocked_reasons:
        blocked.append("stale_telemetry_blocks_sitl_episode_completion")
    if not autonomy_gate.passed:
        blocked.extend(autonomy_gate.blocked_reasons or ("autonomy_gate_failed",))
    blocked_reasons = _as_tuple(blocked)

    evidence_refs = _as_tuple([sitl_ref, telemetry_ref, hil_ref, autonomy_ref])
    criteria_status = tuple(
        SimulatedDeliverySuccessCriterionStatus(
            criterion=criterion,
            status=(
                SimulatedDeliveryCriterionStatus.PENDING
                if not blocked_reasons
                else SimulatedDeliveryCriterionStatus.BLOCKED
            ),
            evidence_refs=evidence_refs if not blocked_reasons else (),
            reason=(
                "sitl_phase_derived_delivery_episode_ready"
                if not blocked_reasons
                else "sitl_phase_derived_delivery_episode_blocked"
            ),
        )
        for criterion in contract.success_criteria
    )
    telemetry_refs = (telemetry_ref, hil_ref, sitl_ref)
    gate_refs = (autonomy_ref,)
    common_metadata = {
        "phase_source": "sitl_mission_state_and_pose",
        "sitl_telemetry_run_ref": sitl_ref,
        "dropoff_verified_deferred_to_mission_upload_pr": True,
        "uses_actual_px4_gazebo_sitl_telemetry_run": True,
    }
    steps = [
        _bounded_delivery_step(
            phase=phase,
            status=(
                SimulatedDeliveryStepStatus.BLOCKED
                if blocked_reasons
                else SimulatedDeliveryStepStatus.OBSERVED
            ),
            summary=f"SITL telemetry phase observed: {phase.value}",
            observed_at=created_at,
            telemetry_refs=telemetry_refs,
            gate_refs=gate_refs,
            sitl_telemetry_run_ref=sitl_ref,
            blocked_reasons=blocked_reasons,
            metadata=common_metadata,
        )
        for phase in derived_phases
    ]
    if (
        blocked_reasons
        and SimulatedDeliveryEpisodePhase.OPERATOR_ESCALATION_REQUIRED
        not in derived_phases
    ):
        steps.append(
            _bounded_delivery_step(
                phase=SimulatedDeliveryEpisodePhase.OPERATOR_ESCALATION_REQUIRED,
                status=SimulatedDeliveryStepStatus.BLOCKED,
                summary="SITL-derived delivery episode completion blocked",
                observed_at=created_at,
                telemetry_refs=telemetry_refs,
                gate_refs=gate_refs,
                sitl_telemetry_run_ref=sitl_ref,
                blocked_reasons=blocked_reasons,
                metadata=common_metadata,
            )
        )

    phase_history = tuple(step.phase for step in steps)
    if blocked_reasons:
        phase = SimulatedDeliveryEpisodePhase.OPERATOR_ESCALATION_REQUIRED
        final_status = SimulatedDeliveryFinalStatus.OPERATOR_ESCALATION_REQUIRED
    elif SimulatedDeliveryEpisodePhase.COMPLETED in phase_history:
        phase = SimulatedDeliveryEpisodePhase.COMPLETED
        final_status = SimulatedDeliveryFinalStatus.COMPLETED
    else:
        phase = (
            phase_history[-1]
            if phase_history
            else SimulatedDeliveryEpisodePhase.PREFLIGHT
        )
        final_status = SimulatedDeliveryFinalStatus.READY_FOR_SIMULATION

    payload = {
        "contract_ref": contract_ref,
        "sitl_ref": sitl_ref,
        "telemetry_ref": telemetry_ref,
        "hil_ref": hil_ref,
        "gate_ref": autonomy_ref,
        "phase_history": [item.value for item in phase_history],
        "blocked_reasons": blocked_reasons,
        "final_status": final_status.value,
    }
    episode = SimulatedDeliveryEpisode(
        episode_id=_stable_id("simulated_delivery_episode", payload),
        mission_id=contract.mission_id,
        delivery_mission_contract_id=contract.contract_id,
        phase=phase,
        steps=tuple(steps),
        telemetry_refs=telemetry_refs,
        gate_refs=gate_refs,
        sitl_telemetry_run_ref=sitl_ref,
        autonomy_gate_result_ref=autonomy_ref,
        hil_telemetry_review_ref=hil_ref,
        dropoff_verified=False,
        success_criteria_status=criteria_status,
        phase_history=phase_history,
        blocked_reasons=blocked_reasons,
        warning_reasons=(),
        final_status=final_status,
        passed=not blocked_reasons,
        operator_escalation_required=bool(blocked_reasons),
        created_at=created_at,
        metadata={
            "artifact_only": True,
            "simulated_delivery_episode_only": True,
            "sitl_phase_history_derived": True,
            "uses_actual_px4_gazebo_sitl_telemetry_run": True,
            "no_new_gazebo_execution": True,
            "no_dropoff_verification_in_this_pr": True,
            "no_approval_promotion_reuse": True,
        },
    )
    trace = _delivery_replay_trace(
        episode=episode,
        delivery_mission_contract_ref=contract_ref,
        bounded_simulation_request_ref="",
        bounded_simulation_run_ref="",
        sitl_telemetry_run_ref=sitl_ref,
        autonomy_gate_result_ref=autonomy_ref,
    )
    episode = episode.model_copy(
        update={"delivery_replay_trace_ref": f"delivery_replay_trace:{trace.trace_id}"}
    )
    trace = trace.model_copy(
        update={"episode_ref": f"simulated_delivery_episode:{episode.episode_id}"}
    )
    return {
        "simulated_delivery_episode": episode,
        "delivery_replay_trace": trace,
    }


def attach_simulated_delivery_episode(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    delivery_mission_policy_review: DeliveryMissionPolicyReview | Mapping[str, Any],
    delivery_mission_scorecard: DeliveryMissionScorecard | Mapping[str, Any],
    delivery_mission_gate_result: DeliveryMissionGateResult | Mapping[str, Any],
    telemetry_refs: Sequence[str] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach a simulated delivery episode without mutating task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise SimulatedDeliveryEpisodeError(
            f"task {task_id} not found; cannot attach simulated delivery episode"
        )
    episode = build_simulated_delivery_episode(
        delivery_mission_contract=delivery_mission_contract,
        delivery_mission_policy_review=delivery_mission_policy_review,
        delivery_mission_scorecard=delivery_mission_scorecard,
        delivery_mission_gate_result=delivery_mission_gate_result,
        telemetry_refs=telemetry_refs,
        now=now,
    )
    artifacts = {"simulated_delivery_episode": episode.model_dump(mode="json")}
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise SimulatedDeliveryEpisodeError(
            f"task {task_id} disappeared while attaching simulated delivery episode"
        )
    return artifacts


def attach_simulated_delivery_episode_from_bounded_gazebo_run(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    bounded_simulation_request: PX4GazeboBoundedSimulationRequest | Mapping[str, Any],
    bounded_simulation_run: PX4GazeboBoundedSimulationRun | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    dropoff_evidence: Mapping[str, Any] | None = None,
    max_dropoff_error_m: float = 0.5,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach bounded-run delivery episode artifacts without changing task status."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise SimulatedDeliveryEpisodeError(
            f"task {task_id} not found; cannot attach bounded delivery episode"
        )
    built = build_simulated_delivery_episode_from_bounded_gazebo_run(
        delivery_mission_contract=delivery_mission_contract,
        bounded_simulation_request=bounded_simulation_request,
        bounded_simulation_run=bounded_simulation_run,
        sanitized_telemetry=sanitized_telemetry,
        hil_telemetry_review=hil_telemetry_review,
        autonomy_gate_result=autonomy_gate_result,
        dropoff_evidence=dropoff_evidence,
        max_dropoff_error_m=max_dropoff_error_m,
        now=now,
    )
    artifacts = {
        "simulated_delivery_episode": built["simulated_delivery_episode"].model_dump(
            mode="json"
        ),
        "delivery_replay_trace": built["delivery_replay_trace"].model_dump(mode="json"),
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise SimulatedDeliveryEpisodeError(
            f"task {task_id} disappeared while attaching bounded delivery episode"
        )
    return artifacts


def attach_simulated_delivery_episode_from_sitl_telemetry_run(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    sitl_telemetry_run: PX4GazeboSITLTelemetryRun | Mapping[str, Any],
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | Mapping[str, Any],
    hil_telemetry_review: HilTelemetryReview | Mapping[str, Any],
    autonomy_gate_result: ToyGridWorldAutonomyGateResult | Mapping[str, Any],
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach SITL phase-derived delivery episode artifacts safely."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise SimulatedDeliveryEpisodeError(
            f"task {task_id} not found; cannot attach SITL-derived delivery episode"
        )
    built = build_simulated_delivery_episode_from_sitl_telemetry_run(
        delivery_mission_contract=delivery_mission_contract,
        sitl_telemetry_run=sitl_telemetry_run,
        sanitized_telemetry=sanitized_telemetry,
        hil_telemetry_review=hil_telemetry_review,
        autonomy_gate_result=autonomy_gate_result,
        now=now,
    )
    artifacts = {
        "simulated_delivery_episode": built["simulated_delivery_episode"].model_dump(
            mode="json"
        ),
        "delivery_replay_trace": built["delivery_replay_trace"].model_dump(mode="json"),
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise SimulatedDeliveryEpisodeError(
            f"task {task_id} disappeared while attaching SITL-derived episode"
        )
    return artifacts


__all__ = [
    "DELIVERY_REPLAY_TRACE_SCHEMA_VERSION",
    "SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION",
    "SIMULATED_DELIVERY_STEP_SCHEMA_VERSION",
    "DeliveryReplayTrace",
    "DeliveryReplayTraceEvent",
    "SimulatedDeliveryCriterionStatus",
    "SimulatedDeliveryEpisode",
    "SimulatedDeliveryEpisodeError",
    "SimulatedDeliveryEpisodePhase",
    "SimulatedDeliveryFinalStatus",
    "SimulatedDeliveryStep",
    "SimulatedDeliveryStepStatus",
    "SimulatedDeliverySuccessCriterionStatus",
    "attach_simulated_delivery_episode",
    "attach_simulated_delivery_episode_from_bounded_gazebo_run",
    "attach_simulated_delivery_episode_from_sitl_telemetry_run",
    "build_simulated_delivery_episode_from_sitl_telemetry_run",
    "build_simulated_delivery_episode_from_bounded_gazebo_run",
    "build_simulated_delivery_episode",
    "derive_simulated_delivery_phase_history_from_sitl_telemetry",
]

"""Physical mission/runtime design schemas for simulation-first embodied flows."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _non_empty_list(values: list[str]) -> list[str]:
    return [str(value).strip() for value in values if str(value).strip()]


class PhysicalVerifierVerdictValue(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"
    UNSAFE = "unsafe"


class SafetyGovernorDecisionValue(str, Enum):
    ALLOW = "allow"
    REJECT = "reject"
    REQUIRE_OPERATOR = "require_operator"
    SAFE_MODE = "safe_mode"


class BatteryHealthState(str, Enum):
    OK = "ok"
    LOW = "low"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class LocalizationHealthState(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    LOST = "lost"
    UNKNOWN = "unknown"


class CommsHealthState(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    LOST = "lost"
    UNKNOWN = "unknown"


class ThermalHealthState(str, Enum):
    OK = "ok"
    LIMIT = "limit"
    UNKNOWN = "unknown"


class ActuatorHealthState(str, Enum):
    OK = "ok"
    FAULT = "fault"
    UNKNOWN = "unknown"


class PayloadHealthState(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class SafetyHealthState(str, Enum):
    NOMINAL = "nominal"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


class PhysicalEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    ref: str
    label: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class MissionObjective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    target: str
    description: str = ""


class PhysicalMissionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_id: str
    objective: MissionObjective
    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    abort_conditions: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)
    controller_scope: str = "low_level_controller_commands_out_of_scope"
    metadata: dict[str, Any] = Field(default_factory=dict)


class PhysicalActionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str
    target: dict[str, Any] = Field(default_factory=dict)
    bounds: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    abort_if: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    controller_scope: str = "direct_motor_thrust_attitude_control_out_of_scope"
    metadata: dict[str, Any] = Field(default_factory=dict)


class TelemetryHealthState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    battery: BatteryHealthState = BatteryHealthState.UNKNOWN
    localization: LocalizationHealthState = LocalizationHealthState.UNKNOWN
    comms: CommsHealthState = CommsHealthState.UNKNOWN
    thermal: ThermalHealthState = ThermalHealthState.UNKNOWN
    actuators: ActuatorHealthState = ActuatorHealthState.UNKNOWN
    payload: PayloadHealthState = PayloadHealthState.UNKNOWN
    safety: SafetyHealthState = SafetyHealthState.UNKNOWN


class SafetyGovernorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: SafetyGovernorDecisionValue
    reasons: list[str] = Field(default_factory=list)
    telemetry_health: TelemetryHealthState = Field(default_factory=TelemetryHealthState)
    mission_contract_id: str = ""
    checked_at: datetime = Field(default_factory=_utc_now)


class PhysicalVerifierResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: PhysicalVerifierVerdictValue
    telemetry_health: TelemetryHealthState = Field(default_factory=TelemetryHealthState)
    evidence_refs: list[PhysicalEvidenceRef] = Field(default_factory=list)
    failure_type: str = ""
    verifier_source: str = ""
    recommended_action: str = ""
    validation_run_id: str = ""
    mission_contract_id: str = ""
    created_at: datetime = Field(default_factory=_utc_now)


class PhysicalReplayPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replay_id: str
    source_kind: str = "computer_trajectory"
    source_trajectory_id: int | None = None
    adapter: str
    workflow: str
    scenario: str
    offline_only: bool = True
    benchmark_required: bool = True
    safety_regression_required: bool = True
    operator_approval_required: bool = True
    live_self_modification_allowed: bool = False
    candidate_promotion_targets: list[str] = Field(default_factory=list)
    mission_contract_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


def _normalize_health_value(raw: Any, mapping: dict[str, Enum], default: Enum) -> Enum:
    text = str(raw or "").strip().lower()
    return mapping.get(text, default)


def build_telemetry_health(response: dict[str, Any] | None) -> TelemetryHealthState:
    payload = response if isinstance(response, dict) else {}
    health = payload.get("telemetry_health")
    health = health if isinstance(health, dict) else {}
    battery = payload.get("battery") or health.get("battery")
    localization = payload.get("localization") or health.get("localization")
    comms = payload.get("comms") or health.get("comms")
    thermal = payload.get("thermal") or health.get("thermal")
    actuators = payload.get("actuators") or health.get("actuators")
    payload_health = payload.get("payload") or health.get("payload")
    safety = payload.get("safety") or health.get("safety")

    model = TelemetryHealthState(
        battery=_normalize_health_value(
            battery,
            {item.value: item for item in BatteryHealthState},
            BatteryHealthState.UNKNOWN,
        ),
        localization=_normalize_health_value(
            localization,
            {item.value: item for item in LocalizationHealthState},
            LocalizationHealthState.UNKNOWN,
        ),
        comms=_normalize_health_value(
            comms,
            {item.value: item for item in CommsHealthState},
            CommsHealthState.UNKNOWN,
        ),
        thermal=_normalize_health_value(
            thermal,
            {item.value: item for item in ThermalHealthState},
            ThermalHealthState.UNKNOWN,
        ),
        actuators=_normalize_health_value(
            actuators,
            {item.value: item for item in ActuatorHealthState},
            ActuatorHealthState.UNKNOWN,
        ),
        payload=_normalize_health_value(
            payload_health,
            {item.value: item for item in PayloadHealthState},
            PayloadHealthState.UNKNOWN,
        ),
        safety=_normalize_health_value(
            safety,
            {item.value: item for item in SafetyHealthState},
            SafetyHealthState.UNKNOWN,
        ),
    )
    if payload.get("validated") is True and model.safety == SafetyHealthState.UNKNOWN:
        model.safety = SafetyHealthState.NOMINAL
    if str(payload.get("status") or "").strip().lower() == "unsafe":
        model.safety = SafetyHealthState.UNSAFE
    return model


def build_physical_evidence_refs(response: dict[str, Any] | None) -> list[PhysicalEvidenceRef]:
    payload = response if isinstance(response, dict) else {}
    refs: list[PhysicalEvidenceRef] = []

    for key, kind, label in (
        ("image_refs", "image", "validation image"),
        ("sensor_snapshot_refs", "sensor_snapshot", "sensor snapshot"),
    ):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            text = str(value or "").strip()
            if text:
                refs.append(PhysicalEvidenceRef(kind=kind, ref=text, label=label))

    for key, kind, label in (
        ("pose_trace_ref", "pose_trace", "pose trace"),
        ("telemetry_window_ref", "telemetry_window", "telemetry window"),
    ):
        text = str(payload.get(key) or "").strip()
        if text:
            refs.append(PhysicalEvidenceRef(kind=kind, ref=text, label=label))
    return refs


def build_physical_verifier_result(
    response: dict[str, Any] | None,
    *,
    validation_run_id: str,
    mission_contract_id: str,
) -> PhysicalVerifierResult:
    payload = response if isinstance(response, dict) else {}
    validation_status = str(payload.get("validation_status") or "").strip().lower()
    status = str(payload.get("status") or "").strip().lower()
    telemetry_health = build_telemetry_health(payload)
    unsafe_statuses = {"unsafe", "safety_abort", "aborted_unsafe"}
    pending_statuses = {"queued", "pending", "running", "ready"}

    if payload.get("validated") is True or validation_status in {"pass", "validated"}:
        verdict = PhysicalVerifierVerdictValue.PASS
    elif validation_status in unsafe_statuses or status in unsafe_statuses or telemetry_health.safety == SafetyHealthState.UNSAFE:
        verdict = PhysicalVerifierVerdictValue.UNSAFE
    elif status in pending_statuses or validation_status in {"uncertain", "pending"}:
        verdict = PhysicalVerifierVerdictValue.UNCERTAIN
    else:
        verdict = PhysicalVerifierVerdictValue.FAIL

    if verdict == PhysicalVerifierVerdictValue.UNSAFE:
        recommended_action = "safe_mode"
    elif verdict == PhysicalVerifierVerdictValue.UNCERTAIN:
        recommended_action = "hold_for_additional_validation"
    elif verdict == PhysicalVerifierVerdictValue.FAIL:
        recommended_action = "reject_execution"
    else:
        recommended_action = "allow_simulation_replay_progress"

    failure_type = str(payload.get("failure_type") or "").strip()
    if not failure_type and verdict == PhysicalVerifierVerdictValue.UNSAFE:
        failure_type = "unsafe"

    return PhysicalVerifierResult(
        verdict=verdict,
        telemetry_health=telemetry_health,
        evidence_refs=build_physical_evidence_refs(payload),
        failure_type=failure_type,
        verifier_source="simulation_adapter",
        recommended_action=recommended_action,
        validation_run_id=validation_run_id,
        mission_contract_id=mission_contract_id,
    )


def build_physical_mission_contract(
    *,
    contract_id: str,
    objective_type: str,
    objective_target: str,
    workflow: str,
    scenario: str,
    robot: str | None = None,
    task: str | None = None,
    additional_allowed_actions: list[str] | None = None,
    additional_completion_criteria: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> PhysicalMissionContract:
    allowed_actions = [
        "submit_simulation",
        "inspect_target",
        "capture_image",
        "build_action_envelope",
        "safe_stop",
    ]
    if additional_allowed_actions:
        allowed_actions.extend(additional_allowed_actions)
    completion_criteria = [
        "all_required_targets_observed",
        "evidence_quality_ok",
        "mission_report_generated",
    ]
    if additional_completion_criteria:
        completion_criteria.extend(additional_completion_criteria)
    return PhysicalMissionContract(
        contract_id=contract_id,
        objective=MissionObjective(
            type=str(objective_type or "inspection"),
            target=str(objective_target or scenario or workflow or "unknown_target"),
            description=str(task or workflow or "simulation_first_physical_validation"),
        ),
        allowed_actions=_non_empty_list(allowed_actions),
        forbidden_actions=[
            "direct_motor_control",
            "modify_controller",
            "continue_when_safety_uncertain",
            "leave_geofence_without_contract_update",
        ],
        abort_conditions=[
            "battery_below_reserve",
            "localization_lost",
            "human_too_close",
            "comms_lost_too_long",
            "safety_governor_uncertain",
        ],
        completion_criteria=_non_empty_list(completion_criteria),
        evidence_requirements=[
            "images_or_sensor_snapshots",
            "pose_trace_or_waypoint_confirmation",
            "telemetry_window",
        ],
        metadata={
            "workflow": workflow,
            "scenario": scenario,
            "robot": robot or "",
            "task": task or "",
            **(metadata or {}),
        },
    )


def build_action_envelope(
    *,
    capability: str,
    target: dict[str, Any],
    robot_namespace: str,
    frame_id: str | None,
    validation_run_id: str = "",
    mission_contract_id: str = "",
) -> PhysicalActionEnvelope:
    return PhysicalActionEnvelope(
        capability=capability,
        target=target,
        bounds={
            "robot_namespace": robot_namespace.strip("/") or "robot",
            "frame_id": frame_id or "",
            "validation_run_id": validation_run_id,
            "mission_contract_id": mission_contract_id,
            "max_speed_class": "inspection",
            "min_battery_reserve": "return_home_plus_margin",
        },
        preconditions=[
            "simulation_validated",
            "localization_ok",
            "route_inside_geofence",
            "no_human_in_near_field",
            "battery_ok",
        ],
        abort_if=[
            "person_detected_close",
            "localization_uncertain",
            "battery_below_reserve",
            "comms_lost",
            "controller_fault",
        ],
        success_criteria=[
            "within_waypoint_radius_or_goal_tolerance",
            "stable_position",
            "evidence_captured",
        ],
        metadata={
            "validation_run_id": validation_run_id,
            "mission_contract_id": mission_contract_id,
        },
    )


def build_safety_governor_decision(
    *,
    mission_contract: PhysicalMissionContract,
    telemetry_health: TelemetryHealthState,
    verifier_result: PhysicalVerifierResult,
    allow_real_hardware: bool,
    dry_run: bool,
) -> SafetyGovernorDecision:
    reasons: list[str] = []
    decision = SafetyGovernorDecisionValue.ALLOW

    if verifier_result.verdict == PhysicalVerifierVerdictValue.UNSAFE or telemetry_health.safety == SafetyHealthState.UNSAFE:
        decision = SafetyGovernorDecisionValue.SAFE_MODE
        reasons.append("unsafe_verifier_or_telemetry_state")
    elif verifier_result.verdict == PhysicalVerifierVerdictValue.FAIL:
        decision = SafetyGovernorDecisionValue.REJECT
        reasons.append("simulation_validation_failed")
    elif verifier_result.verdict == PhysicalVerifierVerdictValue.UNCERTAIN:
        decision = SafetyGovernorDecisionValue.REQUIRE_OPERATOR
        reasons.append("validation_uncertain")
    elif dry_run or not allow_real_hardware:
        decision = SafetyGovernorDecisionValue.ALLOW
        reasons.append("dry_run_or_operator_dispatch_not_requested")
    else:
        reasons.append("simulation_validated_for_real_dispatch")

    if telemetry_health.battery == BatteryHealthState.CRITICAL:
        decision = SafetyGovernorDecisionValue.SAFE_MODE
        reasons.append("battery_critical")
    if telemetry_health.localization == LocalizationHealthState.LOST:
        decision = SafetyGovernorDecisionValue.SAFE_MODE
        reasons.append("localization_lost")
    if telemetry_health.comms == CommsHealthState.LOST:
        decision = SafetyGovernorDecisionValue.SAFE_MODE
        reasons.append("comms_lost")
    if telemetry_health.actuators == ActuatorHealthState.FAULT:
        decision = SafetyGovernorDecisionValue.SAFE_MODE
        reasons.append("actuator_fault")

    return SafetyGovernorDecision(
        decision=decision,
        reasons=_non_empty_list(reasons),
        telemetry_health=telemetry_health,
        mission_contract_id=mission_contract.contract_id,
    )


def build_physical_replay_plan(
    *,
    replay_id: str,
    source_trajectory_id: int,
    adapter: str,
    workflow: str,
    scenario: str,
    mission_contract: PhysicalMissionContract,
    metadata: dict[str, Any] | None = None,
) -> PhysicalReplayPlan:
    return PhysicalReplayPlan(
        replay_id=replay_id,
        source_trajectory_id=source_trajectory_id,
        adapter=adapter,
        workflow=workflow,
        scenario=scenario,
        offline_only=True,
        benchmark_required=True,
        safety_regression_required=True,
        operator_approval_required=True,
        live_self_modification_allowed=False,
        candidate_promotion_targets=[
            "policy_patch",
            "approved_skill",
            "capability_patch",
            "approved_improvement_memory",
        ],
        mission_contract_id=mission_contract.contract_id,
        metadata=metadata or {},
    )

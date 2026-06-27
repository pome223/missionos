"""Simulation-first physical mission replay artifacts.

This module extends the Mission OS artifact vocabulary toward physical-adjacent
validation without invoking physical execution. It is intentionally
artifact-only: no ROS dispatch, no adapter HTTP calls, no actuator commands, and
no runtime registration side effects.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.runtime.mission_contract import MissionContract, normalize_mission_contract

SIMULATION_SCENARIO_REQUEST_SCHEMA_VERSION = "simulation_scenario_request.v1"
TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION = "telemetry_health_snapshot.v1"
SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION = "safety_governor_decision.v1"
DRY_RUN_ACTION_ENVELOPE_SCHEMA_VERSION = "dry_run_action_envelope.v1"
OFFLINE_REPLAY_PLAN_SCHEMA_VERSION = "offline_replay_plan.v1"
PHYSICAL_MISSION_REVIEW_SCHEMA_VERSION = "physical_mission_review.v1"

_DEFAULT_REQUIRED_TELEMETRY_SIGNALS = (
    "battery",
    "localization",
    "comms",
    "safety",
)
_SAFE_SIGNAL_VALUES = {"ok", "nominal", "safe", "pass", "passed", "true"}
_UNSAFE_SIGNAL_VALUES = {
    "unsafe",
    "critical",
    "lost",
    "fault",
    "faulted",
    "failed",
    "fail",
    "error",
    "hazard",
}
_FORBIDDEN_ACTION_TERMS = {
    "actuator_command",
    "actuator_execution",
    "direct_motor_command",
    "direct_motor_control",
    "live_actuator",
    "live_deployment",
    "live_execution",
    "motor_command",
    "physical_dispatch",
    "robot_dispatch",
    "ros_dispatch",
}
_RESERVED_SAFETY_METADATA_KEYS = {
    "artifact_only",
    "dry_run",
    "live_execution_allowed",
    "operator_approval_performed",
    "physical_execution_allowed",
    "physical_execution_invoked",
}


class PhysicalMissionReplayError(ValueError):
    """Raised when a replay artifact would violate simulation-first boundaries."""


class TelemetryHealthStatus(str, Enum):
    NOMINAL = "nominal"
    DEGRADED = "degraded"
    MISSING = "missing"
    STALE = "stale"
    MALFORMED = "malformed"
    UNSAFE = "unsafe"


class SafetyGovernorStatus(str, Enum):
    BLOCKED = "blocked"
    DRY_RUN_ALLOWED = "dry_run_allowed"


class PhysicalMissionReviewStatus(str, Enum):
    DRY_RUN_PLANNED = "dry_run_planned"
    BLOCKED = "blocked"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mission_contract_payload(
    mission_contract: MissionContract | dict[str, Any] | None,
) -> dict[str, Any]:
    if mission_contract is None:
        return {}
    if isinstance(mission_contract, MissionContract):
        return mission_contract.model_dump(mode="json")
    return normalize_mission_contract(
        mission_contract,
        objective=str(mission_contract.get("objective") or "physical mission replay"),
    ).model_dump(mode="json")


def _mission_contract_from_sources(
    *,
    mission_contract: MissionContract | dict[str, Any] | None = None,
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mission_contract is not None:
        return _mission_contract_payload(mission_contract)

    task_payload = _as_dict(task)
    artifacts = _as_dict(task_payload.get("artifacts"))
    durable = _as_dict(artifacts.get("durable_execution"))
    for candidate in (
        artifacts.get("mission_contract"),
        durable.get("mission_contract"),
    ):
        if isinstance(candidate, dict) and candidate:
            return _mission_contract_payload(candidate)
    return {}


def _source_refs_for(
    *,
    mission_contract_payload: dict[str, Any],
    task: dict[str, Any] | None,
    trajectory: dict[str, Any] | None,
) -> list[str]:
    refs: list[str] = []
    contract_id = str(mission_contract_payload.get("contract_id") or "").strip()
    if contract_id:
        refs.append(f"mission_contract:{contract_id}")
    task_id = str(_as_dict(task).get("task_id") or "").strip()
    if task_id:
        refs.append(f"task:{task_id}")
    trajectory_id = str(_as_dict(trajectory).get("id") or "").strip()
    if trajectory_id:
        refs.append(f"trajectory:{trajectory_id}")
    return refs


def _trajectory_summary(trajectory: dict[str, Any] | None) -> dict[str, Any]:
    payload = _as_dict(trajectory)
    if not payload:
        return {}
    attempts = [item for item in _as_list(payload.get("attempts")) if isinstance(item, dict)]
    actions = payload.get("actions")
    if not isinstance(actions, list):
        actions = [
            {
                "surface": attempt.get("surface"),
                "strategy": attempt.get("strategy"),
                "success": bool(attempt.get("success")),
            }
            for attempt in attempts
        ]
    return {
        "id": payload.get("id"),
        "action": payload.get("action"),
        "status": payload.get("status"),
        "failure_type": payload.get("failure_type"),
        "attempt_count": len(attempts),
        "actions": actions,
        "final_surface": payload.get("final_surface"),
    }


def _telemetry_signals(payload: dict[str, Any]) -> dict[str, str]:
    raw_signals = payload.get("signals")
    if not isinstance(raw_signals, dict):
        raw_signals = payload.get("telemetry_health")
    if not isinstance(raw_signals, dict):
        raw_signals = payload
    ignored = {"observed_at", "timestamp", "source_refs", "metadata", "signals"}
    return {
        str(key).strip(): str(value).strip().lower()
        for key, value in raw_signals.items()
        if str(key).strip() and key not in ignored
    }


def _contains_forbidden_execution_term(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _contains_forbidden_execution_term(key)
            or _contains_forbidden_execution_term(item)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple | set):
        return any(_contains_forbidden_execution_term(item) for item in value)
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return any(term in text for term in _FORBIDDEN_ACTION_TERMS)


def _user_metadata_without_reserved_safety_keys(
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in _as_dict(metadata).items()
        if str(key) not in _RESERVED_SAFETY_METADATA_KEYS
    }


class SimulationScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[SIMULATION_SCENARIO_REQUEST_SCHEMA_VERSION] = (
        SIMULATION_SCENARIO_REQUEST_SCHEMA_VERSION
    )
    scenario_id: str
    mission_task_id: str = ""
    mission_contract_id: str = ""
    objective: str
    validation_mode: str = "simulation_first"
    source_kind: str = "mission_or_trajectory"
    source_refs: list[str] = Field(default_factory=list)
    mission_contract: dict[str, Any] = Field(default_factory=dict)
    trajectory_summary: dict[str, Any] = Field(default_factory=dict)
    required_evidence: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_refs", "required_evidence", "forbidden_actions", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class TelemetryHealthSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION] = (
        TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION
    )
    snapshot_id: str
    scenario_id: str = ""
    status: TelemetryHealthStatus
    observed_at: datetime | None = None
    checked_at: datetime = Field(default_factory=_utc_now)
    signals: dict[str, str] = Field(default_factory=dict)
    required_signals: list[str] = Field(default_factory=list)
    missing_signals: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("required_signals", "missing_signals", "reasons", "source_refs", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class SafetyGovernorDecisionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION] = (
        SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION
    )
    decision_id: str
    scenario_id: str = ""
    decision: SafetyGovernorStatus
    reasons: list[str] = Field(default_factory=list)
    telemetry_snapshot_id: str = ""
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    checked_at: datetime = Field(default_factory=_utc_now)
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reasons", "source_refs", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class DryRunActionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[DRY_RUN_ACTION_ENVELOPE_SCHEMA_VERSION] = (
        DRY_RUN_ACTION_ENVELOPE_SCHEMA_VERSION
    )
    envelope_id: str
    scenario_id: str
    action_type: str = "simulation_replay"
    proposed_actions: list[dict[str, Any]] = Field(default_factory=list)
    dry_run: Literal[True] = True
    live_execution_allowed: Literal[False] = False
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    safety_governor_decision_id: str
    source_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_refs", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class OfflineReplayPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[OFFLINE_REPLAY_PLAN_SCHEMA_VERSION] = (
        OFFLINE_REPLAY_PLAN_SCHEMA_VERSION
    )
    replay_plan_id: str
    scenario_id: str
    simulation_scenario_ref: str
    telemetry_snapshot_ref: str
    safety_governor_ref: str
    dry_run_action_envelope_ref: str
    replay_steps: list[str] = Field(default_factory=list)
    offline_only: Literal[True] = True
    live_execution_allowed: Literal[False] = False
    benchmark_required: Literal[True] = True
    safety_regression_required: Literal[True] = True
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    source_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("replay_steps", "source_refs", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class PhysicalMissionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[PHYSICAL_MISSION_REVIEW_SCHEMA_VERSION] = (
        PHYSICAL_MISSION_REVIEW_SCHEMA_VERSION
    )
    scenario_id: str
    final_status: PhysicalMissionReviewStatus
    summary: str
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    source_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_refs", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


def build_simulation_scenario_request(
    *,
    mission_contract: MissionContract | dict[str, Any] | None = None,
    task: dict[str, Any] | None = None,
    trajectory: dict[str, Any] | None = None,
    scenario_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> SimulationScenarioRequest:
    """Build a versioned simulation scenario request from mission-like inputs."""

    created_at = now or _utc_now()
    task_payload = _as_dict(task)
    trajectory_payload = _as_dict(trajectory)
    contract_payload = _mission_contract_from_sources(
        mission_contract=mission_contract,
        task=task_payload,
    )
    contract_id = str(contract_payload.get("contract_id") or "").strip()
    task_id = str(task_payload.get("task_id") or "").strip()
    trajectory = _trajectory_summary(trajectory_payload)
    source_refs = _source_refs_for(
        mission_contract_payload=contract_payload,
        task=task_payload,
        trajectory=trajectory_payload,
    )
    objective = str(
        contract_payload.get("objective")
        or task_payload.get("title")
        or trajectory_payload.get("action")
        or "simulation-first physical replay"
    ).strip()
    required_evidence = _str_list(contract_payload.get("evidence_requirements"))
    forbidden_actions = _str_list(contract_payload.get("forbidden_actions"))
    forbidden_actions.extend(
        [
            "live_actuator_execution",
            "direct_motor_control",
            "automatic_physical_deployment",
        ]
    )
    payload_for_id = {
        "contract_id": contract_id,
        "task_id": task_id,
        "trajectory": trajectory,
        "objective": objective,
    }
    return SimulationScenarioRequest(
        scenario_id=scenario_id or _stable_id("simulation_scenario", payload_for_id),
        mission_task_id=task_id,
        mission_contract_id=contract_id,
        objective=objective,
        source_refs=source_refs,
        mission_contract=contract_payload,
        trajectory_summary=trajectory,
        required_evidence=required_evidence,
        forbidden_actions=sorted(set(forbidden_actions)),
        created_at=created_at,
        metadata={
            **_user_metadata_without_reserved_safety_keys(metadata),
            "artifact_only": True,
            "physical_execution_allowed": False,
        },
    )


def build_telemetry_health_snapshot(
    telemetry: dict[str, Any] | None,
    *,
    scenario_id: str = "",
    required_signals: list[str] | tuple[str, ...] | None = None,
    max_age_seconds: int = 60,
    now: datetime | None = None,
) -> TelemetryHealthSnapshot:
    """Normalize telemetry into a safety-first snapshot.

    Missing, stale, malformed, or unsafe telemetry is represented explicitly and
    is expected to block the safety governor.
    """

    checked_at = now or _utc_now()
    required = _str_list(list(required_signals or _DEFAULT_REQUIRED_TELEMETRY_SIGNALS))
    if telemetry is None:
        return TelemetryHealthSnapshot(
            snapshot_id=_stable_id("telemetry", {"scenario_id": scenario_id, "status": "missing"}),
            scenario_id=scenario_id,
            status=TelemetryHealthStatus.MISSING,
            checked_at=checked_at,
            required_signals=required,
            missing_signals=required,
            reasons=["telemetry_missing"],
        )
    if not isinstance(telemetry, dict):
        return TelemetryHealthSnapshot(
            snapshot_id=_stable_id("telemetry", {"scenario_id": scenario_id, "status": "malformed"}),
            scenario_id=scenario_id,
            status=TelemetryHealthStatus.MALFORMED,
            checked_at=checked_at,
            required_signals=required,
            missing_signals=required,
            reasons=["telemetry_malformed"],
        )

    signals = _telemetry_signals(telemetry)
    missing = [name for name in required if name not in signals or not signals[name]]
    observed_at = _parse_datetime(telemetry.get("observed_at") or telemetry.get("timestamp"))
    source_refs = _str_list(telemetry.get("source_refs"))
    reasons: list[str] = []
    status = TelemetryHealthStatus.NOMINAL

    if observed_at is None:
        status = TelemetryHealthStatus.MALFORMED
        reasons.append("telemetry_timestamp_missing")
    elif missing:
        status = TelemetryHealthStatus.MALFORMED
        reasons.append("telemetry_required_signals_missing")
    elif observed_at < checked_at - timedelta(seconds=max(0, int(max_age_seconds))):
        status = TelemetryHealthStatus.STALE
        reasons.append("telemetry_stale")
    elif any(value in _UNSAFE_SIGNAL_VALUES for value in signals.values()):
        status = TelemetryHealthStatus.UNSAFE
        reasons.append("telemetry_unsafe")
    elif any(value not in _SAFE_SIGNAL_VALUES for value in signals.values()):
        status = TelemetryHealthStatus.DEGRADED
        reasons.append("telemetry_degraded")
    else:
        reasons.append("telemetry_nominal")

    return TelemetryHealthSnapshot(
        snapshot_id=_stable_id(
            "telemetry",
            {
                "scenario_id": scenario_id,
                "observed_at": observed_at.isoformat() if observed_at else None,
                "signals": signals,
                "status": status.value,
            },
        ),
        scenario_id=scenario_id,
        status=status,
        observed_at=observed_at,
        checked_at=checked_at,
        signals=signals,
        required_signals=required,
        missing_signals=missing,
        reasons=reasons,
        source_refs=source_refs,
        metadata={
            "max_age_seconds": max(0, int(max_age_seconds)),
            "safe_values": sorted(_SAFE_SIGNAL_VALUES),
        },
    )


def build_safety_governor_decision_artifact(
    scenario_request: SimulationScenarioRequest | dict[str, Any],
    telemetry_snapshot: TelemetryHealthSnapshot | dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> SafetyGovernorDecisionArtifact:
    scenario = (
        scenario_request
        if isinstance(scenario_request, SimulationScenarioRequest)
        else SimulationScenarioRequest.model_validate(scenario_request)
    )
    checked_at = now or _utc_now()
    if telemetry_snapshot is None:
        telemetry = None
    elif isinstance(telemetry_snapshot, TelemetryHealthSnapshot):
        telemetry = telemetry_snapshot
    else:
        telemetry = TelemetryHealthSnapshot.model_validate(telemetry_snapshot)

    reasons: list[str] = []
    decision = SafetyGovernorStatus.BLOCKED
    telemetry_snapshot_id = ""
    if telemetry is None:
        reasons.append("telemetry_missing")
    else:
        telemetry_snapshot_id = telemetry.snapshot_id
        reasons.extend(telemetry.reasons)
        if telemetry.status == TelemetryHealthStatus.NOMINAL:
            decision = SafetyGovernorStatus.DRY_RUN_ALLOWED
            reasons.append("dry_run_only")
        else:
            reasons.append(f"blocked_by_{telemetry.status.value}_telemetry")

    payload_for_id = {
        "scenario_id": scenario.scenario_id,
        "telemetry_snapshot_id": telemetry_snapshot_id,
        "decision": decision.value,
        "reasons": reasons,
    }
    return SafetyGovernorDecisionArtifact(
        decision_id=_stable_id("safety_governor", payload_for_id),
        scenario_id=scenario.scenario_id,
        decision=decision,
        reasons=reasons,
        telemetry_snapshot_id=telemetry_snapshot_id,
        checked_at=checked_at,
        source_refs=scenario.source_refs,
        metadata={
            "operator_approval_note": "approval is required before any live physical work",
            "physical_execution_allowed": False,
        },
    )


def build_dry_run_action_envelope(
    scenario_request: SimulationScenarioRequest | dict[str, Any],
    safety_governor_decision: SafetyGovernorDecisionArtifact | dict[str, Any],
    *,
    proposed_actions: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> DryRunActionEnvelope:
    scenario = (
        scenario_request
        if isinstance(scenario_request, SimulationScenarioRequest)
        else SimulationScenarioRequest.model_validate(scenario_request)
    )
    decision = (
        safety_governor_decision
        if isinstance(safety_governor_decision, SafetyGovernorDecisionArtifact)
        else SafetyGovernorDecisionArtifact.model_validate(safety_governor_decision)
    )
    if decision.decision != SafetyGovernorStatus.DRY_RUN_ALLOWED:
        raise PhysicalMissionReplayError("safety governor did not allow dry-run envelope")

    actions = proposed_actions
    if actions is None:
        trajectory_actions = _as_list(scenario.trajectory_summary.get("actions"))
        actions = [
            item for item in trajectory_actions if isinstance(item, dict)
        ] or [
            {
                "type": "simulation_replay",
                "scenario_id": scenario.scenario_id,
                "objective": scenario.objective,
            }
        ]
    if _contains_forbidden_execution_term(actions):
        raise PhysicalMissionReplayError("dry-run envelope cannot contain live physical execution actions")

    payload_for_id = {
        "scenario_id": scenario.scenario_id,
        "decision_id": decision.decision_id,
        "actions": actions,
    }
    return DryRunActionEnvelope(
        envelope_id=_stable_id("dry_run_envelope", payload_for_id),
        scenario_id=scenario.scenario_id,
        proposed_actions=actions,
        safety_governor_decision_id=decision.decision_id,
        source_refs=scenario.source_refs + [f"safety_governor_decision:{decision.decision_id}"],
        created_at=now or _utc_now(),
        metadata={
            "simulation_first": True,
            "live_physical_work_requires_separate_approval": True,
        },
    )


def build_offline_replay_plan(
    scenario_request: SimulationScenarioRequest | dict[str, Any],
    telemetry_snapshot: TelemetryHealthSnapshot | dict[str, Any],
    safety_governor_decision: SafetyGovernorDecisionArtifact | dict[str, Any],
    dry_run_action_envelope: DryRunActionEnvelope | dict[str, Any],
    *,
    now: datetime | None = None,
) -> OfflineReplayPlan:
    scenario = (
        scenario_request
        if isinstance(scenario_request, SimulationScenarioRequest)
        else SimulationScenarioRequest.model_validate(scenario_request)
    )
    telemetry = (
        telemetry_snapshot
        if isinstance(telemetry_snapshot, TelemetryHealthSnapshot)
        else TelemetryHealthSnapshot.model_validate(telemetry_snapshot)
    )
    decision = (
        safety_governor_decision
        if isinstance(safety_governor_decision, SafetyGovernorDecisionArtifact)
        else SafetyGovernorDecisionArtifact.model_validate(safety_governor_decision)
    )
    envelope = (
        dry_run_action_envelope
        if isinstance(dry_run_action_envelope, DryRunActionEnvelope)
        else DryRunActionEnvelope.model_validate(dry_run_action_envelope)
    )
    if not envelope.dry_run or envelope.live_execution_allowed:
        raise PhysicalMissionReplayError("offline replay plan requires a dry-run-only action envelope")

    payload_for_id = {
        "scenario_id": scenario.scenario_id,
        "telemetry": telemetry.snapshot_id,
        "decision": decision.decision_id,
        "envelope": envelope.envelope_id,
    }
    return OfflineReplayPlan(
        replay_plan_id=_stable_id("offline_replay", payload_for_id),
        scenario_id=scenario.scenario_id,
        simulation_scenario_ref=f"simulation_scenario_request:{scenario.scenario_id}",
        telemetry_snapshot_ref=f"telemetry_health_snapshot:{telemetry.snapshot_id}",
        safety_governor_ref=f"safety_governor_decision:{decision.decision_id}",
        dry_run_action_envelope_ref=f"dry_run_action_envelope:{envelope.envelope_id}",
        replay_steps=[
            "load_simulation_scenario_request",
            "attach_telemetry_health_snapshot",
            "inspect_safety_governor_decision",
            "inspect_dry_run_action_envelope",
            "run_offline_replay_only",
            "require_operator_approval_before_any_live_physical_work",
        ],
        source_refs=sorted(
            set(
                scenario.source_refs
                + telemetry.source_refs
                + decision.source_refs
                + envelope.source_refs
            )
        ),
        created_at=now or _utc_now(),
        metadata={
            "artifact_only": True,
            "live_physical_work_requires_new_approval_flow": True,
        },
    )


def build_physical_mission_review_stub(
    scenario_request: SimulationScenarioRequest | dict[str, Any],
    safety_governor_decision: SafetyGovernorDecisionArtifact | dict[str, Any],
    *,
    offline_replay_plan: OfflineReplayPlan | dict[str, Any] | None = None,
    now: datetime | None = None,
) -> PhysicalMissionReview:
    scenario = (
        scenario_request
        if isinstance(scenario_request, SimulationScenarioRequest)
        else SimulationScenarioRequest.model_validate(scenario_request)
    )
    decision = (
        safety_governor_decision
        if isinstance(safety_governor_decision, SafetyGovernorDecisionArtifact)
        else SafetyGovernorDecisionArtifact.model_validate(safety_governor_decision)
    )
    final_status = (
        PhysicalMissionReviewStatus.DRY_RUN_PLANNED
        if decision.decision == SafetyGovernorStatus.DRY_RUN_ALLOWED
        and offline_replay_plan is not None
        else PhysicalMissionReviewStatus.BLOCKED
    )
    summary = (
        "Simulation-first offline replay plan generated; live execution remains disallowed."
        if final_status == PhysicalMissionReviewStatus.DRY_RUN_PLANNED
        else "Simulation-first physical replay blocked before action envelope generation."
    )
    refs = scenario.source_refs + [f"safety_governor_decision:{decision.decision_id}"]
    if offline_replay_plan is not None:
        plan = (
            offline_replay_plan
            if isinstance(offline_replay_plan, OfflineReplayPlan)
            else OfflineReplayPlan.model_validate(offline_replay_plan)
        )
        refs.append(f"offline_replay_plan:{plan.replay_plan_id}")
    return PhysicalMissionReview(
        scenario_id=scenario.scenario_id,
        final_status=final_status,
        summary=summary,
        source_refs=refs,
        created_at=now or _utc_now(),
        metadata={"artifact_only": True},
    )


def build_simulation_first_replay_artifacts(
    *,
    mission_contract: MissionContract | dict[str, Any] | None = None,
    task: dict[str, Any] | None = None,
    trajectory: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the full artifact path without invoking physical execution."""

    current_time = now or _utc_now()
    scenario = build_simulation_scenario_request(
        mission_contract=mission_contract,
        task=task,
        trajectory=trajectory,
        now=current_time,
    )
    telemetry_snapshot = build_telemetry_health_snapshot(
        telemetry,
        scenario_id=scenario.scenario_id,
        now=current_time,
    )
    governor = build_safety_governor_decision_artifact(
        scenario,
        telemetry_snapshot,
        now=current_time,
    )
    envelope: DryRunActionEnvelope | None = None
    replay_plan: OfflineReplayPlan | None = None
    if governor.decision == SafetyGovernorStatus.DRY_RUN_ALLOWED:
        envelope = build_dry_run_action_envelope(
            scenario,
            governor,
            now=current_time,
        )
        replay_plan = build_offline_replay_plan(
            scenario,
            telemetry_snapshot,
            governor,
            envelope,
            now=current_time,
        )
    review = build_physical_mission_review_stub(
        scenario,
        governor,
        offline_replay_plan=replay_plan,
        now=current_time,
    )
    return {
        "simulation_scenario_request": scenario.model_dump(mode="json"),
        "telemetry_health_snapshot": telemetry_snapshot.model_dump(mode="json"),
        "safety_governor_decision": governor.model_dump(mode="json"),
        "dry_run_action_envelope": envelope.model_dump(mode="json") if envelope else None,
        "offline_replay_plan": replay_plan.model_dump(mode="json") if replay_plan else None,
        "physical_mission_review": review.model_dump(mode="json"),
    }


__all__ = [
    "DRY_RUN_ACTION_ENVELOPE_SCHEMA_VERSION",
    "OFFLINE_REPLAY_PLAN_SCHEMA_VERSION",
    "PHYSICAL_MISSION_REVIEW_SCHEMA_VERSION",
    "SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION",
    "SIMULATION_SCENARIO_REQUEST_SCHEMA_VERSION",
    "TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION",
    "DryRunActionEnvelope",
    "OfflineReplayPlan",
    "PhysicalMissionReplayError",
    "PhysicalMissionReview",
    "PhysicalMissionReviewStatus",
    "SafetyGovernorDecisionArtifact",
    "SafetyGovernorStatus",
    "SimulationScenarioRequest",
    "TelemetryHealthSnapshot",
    "TelemetryHealthStatus",
    "build_dry_run_action_envelope",
    "build_offline_replay_plan",
    "build_physical_mission_review_stub",
    "build_safety_governor_decision_artifact",
    "build_simulation_first_replay_artifacts",
    "build_simulation_scenario_request",
    "build_telemetry_health_snapshot",
]

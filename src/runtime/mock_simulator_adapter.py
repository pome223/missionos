"""Mock adapter-backed simulator smoke chain.

This module is the first #208 slice after the adapter selection / fixture PR.
It proves that a non-toy-grid simulator-like contract can produce a complete
Mission OS artifact chain without introducing a real simulator runtime.

The chain is intentionally local, deterministic, and artifact-only:

``simulator_adapter_contract.v1``
    -> ``mock_simulator_state.v1``
    -> ``telemetry_health_snapshot.v1``
    -> ``safety_governor_decision.v1``
    -> ``mock_simulator_replay_trace.v1``
    -> ``mock_simulator_scorecard.v1``
    -> ``mock_simulator_review.v1``
    -> ``mock_simulator_gate_result.v1``

It does not connect to PX4, Gazebo, AirSim, Isaac Sim, ROS, MAVLink,
actuators, network adapters, or hardware.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.runtime.physical_mission_replay import (
    SafetyGovernorDecisionArtifact,
    SafetyGovernorStatus,
    TelemetryHealthSnapshot,
    TelemetryHealthStatus,
    build_safety_governor_decision_artifact,
    build_simulation_scenario_request,
    build_telemetry_health_snapshot,
)
from src.runtime.simulator_adapter_contract import (
    MOCK_PHYSICAL_SIMULATOR_ADAPTER_ID,
    SimulatorAdapterContract,
    build_mock_physical_simulator_adapter_contract,
    validate_simulator_adapter_safety_compatibility,
)
from src.runtime.task_store import TaskStore, get_task_store


MOCK_SIMULATOR_STATE_SCHEMA_VERSION = "mock_simulator_state.v1"
MOCK_SIMULATOR_REPLAY_TRACE_SCHEMA_VERSION = "mock_simulator_replay_trace.v1"
MOCK_SIMULATOR_SCORECARD_SCHEMA_VERSION = "mock_simulator_scorecard.v1"
MOCK_SIMULATOR_REVIEW_SCHEMA_VERSION = "mock_simulator_review.v1"
MOCK_SIMULATOR_GATE_RESULT_SCHEMA_VERSION = "mock_simulator_gate_result.v1"


class MockSimulatorAdapterError(RuntimeError):
    """Raised when the mock adapter artifact chain cannot be built or attached."""


class MockSimulatorArtifactStatus(str, Enum):
    PASSED = "passed"
    BLOCKED = "blocked"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _deterministic_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _as_contract(
    contract: SimulatorAdapterContract | dict[str, Any] | None,
) -> SimulatorAdapterContract:
    if contract is None:
        return build_mock_physical_simulator_adapter_contract()
    parsed = validate_simulator_adapter_safety_compatibility(contract)
    if parsed.adapter_id != MOCK_PHYSICAL_SIMULATOR_ADAPTER_ID:
        raise MockSimulatorAdapterError(
            "mock simulator smoke chain requires "
            f"{MOCK_PHYSICAL_SIMULATOR_ADAPTER_ID!r}, got {parsed.adapter_id!r}"
        )
    return parsed


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


class MockSimulatorState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MOCK_SIMULATOR_STATE_SCHEMA_VERSION] = (
        MOCK_SIMULATOR_STATE_SCHEMA_VERSION
    )
    state_id: str
    adapter_id: str
    simulator_kind: str
    scenario_name: str = "mock-second-simulator-smoke"
    pose: dict[str, float] = Field(default_factory=dict)
    battery: float = 1.0
    observed_at: datetime = Field(default_factory=_utc_now)
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class MockSimulatorReplayTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MOCK_SIMULATOR_REPLAY_TRACE_SCHEMA_VERSION] = (
        MOCK_SIMULATOR_REPLAY_TRACE_SCHEMA_VERSION
    )
    trace_id: str
    adapter_contract_ref: str
    state_ref: str
    telemetry_snapshot_ref: str
    safety_governor_ref: str
    replay_steps: list[dict[str, Any]] = Field(default_factory=list)
    deterministic_hash: str
    dry_run: Literal[True] = True
    offline_only: Literal[True] = True
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MockSimulatorScorecard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MOCK_SIMULATOR_SCORECARD_SCHEMA_VERSION] = (
        MOCK_SIMULATOR_SCORECARD_SCHEMA_VERSION
    )
    scorecard_id: str
    status: MockSimulatorArtifactStatus
    passed: bool
    deterministic_replay: bool
    telemetry_nominal: bool
    governor_allowed_dry_run: bool
    live_execution_flag_count: int = 0
    physical_execution_flag_count: int = 0
    dispatch_artifact_count: int = 0
    failure_buckets: list[str] = Field(default_factory=list)
    metrics: dict[str, float | int | bool] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("failure_buckets", mode="before")
    @classmethod
    def _normalize_failure_buckets(cls, value: Any) -> list[str]:
        return _str_list(value)


class MockSimulatorReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MOCK_SIMULATOR_REVIEW_SCHEMA_VERSION] = (
        MOCK_SIMULATOR_REVIEW_SCHEMA_VERSION
    )
    review_id: str
    status: MockSimulatorArtifactStatus
    summary: str
    findings: list[str] = Field(default_factory=list)
    scorecard_snapshot: dict[str, Any] = Field(default_factory=dict)
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("findings", mode="before")
    @classmethod
    def _normalize_findings(cls, value: Any) -> list[str]:
        return _str_list(value)


class MockSimulatorGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[MOCK_SIMULATOR_GATE_RESULT_SCHEMA_VERSION] = (
        MOCK_SIMULATOR_GATE_RESULT_SCHEMA_VERSION
    )
    gate_id: str
    status: MockSimulatorArtifactStatus
    passed: bool
    blocked_reasons: list[str] = Field(default_factory=list)
    warning_reasons: list[str] = Field(default_factory=list)
    scorecard_snapshot: dict[str, Any] = Field(default_factory=dict)
    review_snapshot: dict[str, Any] = Field(default_factory=dict)
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("blocked_reasons", "warning_reasons", mode="before")
    @classmethod
    def _normalize_reasons(cls, value: Any) -> list[str]:
        return _str_list(value)


def build_mock_simulator_state(
    *,
    simulator_adapter_contract: SimulatorAdapterContract | dict[str, Any] | None = None,
    now: datetime | None = None,
) -> MockSimulatorState:
    contract = _as_contract(simulator_adapter_contract)
    observed_at = now or _utc_now()
    pose = {"x": 0.0, "y": 0.0, "z": 0.0}
    return MockSimulatorState(
        state_id=_stable_id(
            "mock_sim_state",
            {
                "adapter_id": contract.adapter_id,
                "pose": pose,
                "observed_at": observed_at.isoformat(),
            },
        ),
        adapter_id=contract.adapter_id,
        simulator_kind=contract.simulator_kind,
        pose=pose,
        battery=1.0,
        observed_at=observed_at,
        metadata={
            "artifact_only": True,
            "adapter_backed_smoke": True,
        },
    )


def build_mock_simulator_replay_trace(
    *,
    simulator_adapter_contract: SimulatorAdapterContract | dict[str, Any] | None = None,
    state: MockSimulatorState | dict[str, Any] | None = None,
    telemetry_payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[
    MockSimulatorReplayTrace,
    TelemetryHealthSnapshot,
    SafetyGovernorDecisionArtifact,
]:
    contract = _as_contract(simulator_adapter_contract)
    created_at = now or _utc_now()
    state_obj = (
        state
        if isinstance(state, MockSimulatorState)
        else MockSimulatorState.model_validate(state)
        if isinstance(state, dict)
        else build_mock_simulator_state(
            simulator_adapter_contract=contract,
            now=created_at,
        )
    )
    scenario = build_simulation_scenario_request(
        scenario_id=_stable_id(
            "mock_sim_scenario",
            {"adapter_id": contract.adapter_id, "state_id": state_obj.state_id},
        ),
        metadata={"adapter_id": contract.adapter_id},
        now=created_at,
    )
    telemetry_input = telemetry_payload or {
            "timestamp": created_at.isoformat(),
            "signals": {
                "battery": "ok",
                "localization": "ok",
                "comms": "ok",
                "safety": "nominal",
            },
            "source_refs": [f"mock_simulator_state:{state_obj.state_id}"],
        }
    telemetry = build_telemetry_health_snapshot(
        telemetry_input,
        scenario_id=scenario.scenario_id,
        now=created_at,
    )
    governor = build_safety_governor_decision_artifact(
        scenario,
        telemetry,
        now=created_at,
    )
    replay_steps = [
        {
            "step_index": 0,
            "mode": "dry_run_only",
            "state_ref": state_obj.state_id,
            "telemetry_snapshot_ref": telemetry.snapshot_id,
            "safety_governor_ref": governor.decision_id,
            "accepted": governor.decision is SafetyGovernorStatus.DRY_RUN_ALLOWED,
            "live_execution_allowed": False,
            "physical_execution_invoked": False,
            "dispatch_implementation_present": False,
        }
    ]
    hash_payload = {
        "adapter_contract_ref": contract.adapter_id,
        "state_ref": state_obj.state_id,
        "telemetry_snapshot_ref": telemetry.snapshot_id,
        "safety_governor_ref": governor.decision_id,
        "replay_steps": replay_steps,
    }
    deterministic_hash = _deterministic_hash(hash_payload)
    trace = MockSimulatorReplayTrace(
        trace_id=_stable_id("mock_sim_trace", hash_payload),
        adapter_contract_ref=contract.adapter_id,
        state_ref=state_obj.state_id,
        telemetry_snapshot_ref=telemetry.snapshot_id,
        safety_governor_ref=governor.decision_id,
        replay_steps=replay_steps,
        deterministic_hash=deterministic_hash,
        created_at=created_at,
        metadata={
            "artifact_only": True,
            "hash_payload": hash_payload,
            "simulator_kind": contract.simulator_kind,
        },
    )
    return trace, telemetry, governor


def build_mock_simulator_scorecard(
    replay_trace: MockSimulatorReplayTrace | dict[str, Any],
    telemetry_snapshot: TelemetryHealthSnapshot | dict[str, Any],
    safety_governor_decision: SafetyGovernorDecisionArtifact | dict[str, Any],
    *,
    now: datetime | None = None,
) -> MockSimulatorScorecard:
    trace = (
        replay_trace
        if isinstance(replay_trace, MockSimulatorReplayTrace)
        else MockSimulatorReplayTrace.model_validate(replay_trace)
    )
    telemetry = (
        telemetry_snapshot
        if isinstance(telemetry_snapshot, TelemetryHealthSnapshot)
        else TelemetryHealthSnapshot.model_validate(telemetry_snapshot)
    )
    governor = (
        safety_governor_decision
        if isinstance(safety_governor_decision, SafetyGovernorDecisionArtifact)
        else SafetyGovernorDecisionArtifact.model_validate(safety_governor_decision)
    )
    expected_hash = _deterministic_hash(trace.metadata.get("hash_payload", {}))
    deterministic_replay = bool(trace.deterministic_hash == expected_hash)
    telemetry_nominal = telemetry.status is TelemetryHealthStatus.NOMINAL
    governor_allowed = governor.decision is SafetyGovernorStatus.DRY_RUN_ALLOWED
    failure_buckets: list[str] = []
    if not deterministic_replay:
        failure_buckets.append("mock_simulator_replay_not_deterministic")
    if not telemetry_nominal:
        failure_buckets.append(f"mock_simulator_{telemetry.status.value}_telemetry")
    if not governor_allowed:
        failure_buckets.append("mock_simulator_governor_blocked")
    live_count = int(trace.live_execution_allowed)
    physical_count = int(trace.physical_execution_invoked)
    dispatch_count = int(trace.dispatch_implementation_present)
    if live_count:
        failure_buckets.append("mock_simulator_live_execution_flag")
    if physical_count:
        failure_buckets.append("mock_simulator_physical_execution_flag")
    if dispatch_count:
        failure_buckets.append("mock_simulator_dispatch_present")
    passed = not failure_buckets
    created_at = now or _utc_now()
    return MockSimulatorScorecard(
        scorecard_id=_stable_id(
            "mock_sim_scorecard",
            {"trace_id": trace.trace_id, "failure_buckets": failure_buckets},
        ),
        status=(
            MockSimulatorArtifactStatus.PASSED
            if passed
            else MockSimulatorArtifactStatus.BLOCKED
        ),
        passed=passed,
        deterministic_replay=deterministic_replay,
        telemetry_nominal=telemetry_nominal,
        governor_allowed_dry_run=governor_allowed,
        live_execution_flag_count=live_count,
        physical_execution_flag_count=physical_count,
        dispatch_artifact_count=dispatch_count,
        failure_buckets=failure_buckets,
        metrics={
            "deterministic_replay": deterministic_replay,
            "telemetry_nominal": telemetry_nominal,
            "governor_allowed_dry_run": governor_allowed,
            "live_execution_flag_count": live_count,
            "physical_execution_flag_count": physical_count,
            "dispatch_artifact_count": dispatch_count,
        },
        created_at=created_at,
        metadata={
            "artifact_only": True,
            "rule_based": True,
            "llm_judge_used": False,
        },
    )


def build_mock_simulator_review(
    scorecard: MockSimulatorScorecard | dict[str, Any],
    *,
    now: datetime | None = None,
) -> MockSimulatorReview:
    score = (
        scorecard
        if isinstance(scorecard, MockSimulatorScorecard)
        else MockSimulatorScorecard.model_validate(scorecard)
    )
    created_at = now or _utc_now()
    findings = (
        ["mock_simulator_smoke_chain_passed"]
        if score.passed
        else list(score.failure_buckets)
    )
    return MockSimulatorReview(
        review_id=_stable_id(
            "mock_sim_review",
            {"scorecard_id": score.scorecard_id, "findings": findings},
        ),
        status=(
            MockSimulatorArtifactStatus.PASSED
            if score.passed
            else MockSimulatorArtifactStatus.BLOCKED
        ),
        summary=(
            "Mock simulator adapter smoke chain passed."
            if score.passed
            else "Mock simulator adapter smoke chain blocked."
        ),
        findings=findings,
        scorecard_snapshot=score.model_dump(mode="json"),
        created_at=created_at,
        metadata={
            "artifact_only": True,
            "promotion_created": False,
            "runtime_reuse_created": False,
        },
    )


def build_mock_simulator_gate_result(
    scorecard: MockSimulatorScorecard | dict[str, Any],
    review: MockSimulatorReview | dict[str, Any],
    *,
    now: datetime | None = None,
) -> MockSimulatorGateResult:
    score = (
        scorecard
        if isinstance(scorecard, MockSimulatorScorecard)
        else MockSimulatorScorecard.model_validate(scorecard)
    )
    review_obj = (
        review
        if isinstance(review, MockSimulatorReview)
        else MockSimulatorReview.model_validate(review)
    )
    blocked_reasons = list(score.failure_buckets)
    if not score.passed and "mock_simulator_scorecard_failed" not in blocked_reasons:
        blocked_reasons.insert(0, "mock_simulator_scorecard_failed")
    if review_obj.status is MockSimulatorArtifactStatus.BLOCKED:
        blocked_reasons.append("mock_simulator_review_blocked")
    # Preserve deterministic order and remove duplicates.
    blocked_reasons = list(dict.fromkeys(blocked_reasons))
    passed = not blocked_reasons
    created_at = now or _utc_now()
    return MockSimulatorGateResult(
        gate_id=_stable_id(
            "mock_sim_gate",
            {
                "scorecard_id": score.scorecard_id,
                "review_id": review_obj.review_id,
                "blocked_reasons": blocked_reasons,
            },
        ),
        status=(
            MockSimulatorArtifactStatus.PASSED
            if passed
            else MockSimulatorArtifactStatus.BLOCKED
        ),
        passed=passed,
        blocked_reasons=blocked_reasons,
        scorecard_snapshot=score.model_dump(mode="json"),
        review_snapshot=review_obj.model_dump(mode="json"),
        created_at=created_at,
        metadata={
            "artifact_only": True,
            "rule_based": True,
            "llm_judge_used": False,
            "stronger_execution_allowed": False,
        },
    )


def build_mock_simulator_adapter_smoke_chain(
    *,
    simulator_adapter_contract: SimulatorAdapterContract | dict[str, Any] | None = None,
    telemetry_payload: dict[str, Any] | None = None,
    break_replay_hash: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or _utc_now()
    contract = _as_contract(simulator_adapter_contract)
    state = build_mock_simulator_state(
        simulator_adapter_contract=contract,
        now=current_time,
    )
    trace, telemetry, governor = build_mock_simulator_replay_trace(
        simulator_adapter_contract=contract,
        state=state,
        telemetry_payload=telemetry_payload,
        now=current_time,
    )
    if break_replay_hash:
        trace = trace.model_copy(update={"deterministic_hash": "broken"})
    scorecard = build_mock_simulator_scorecard(
        trace,
        telemetry,
        governor,
        now=current_time,
    )
    review = build_mock_simulator_review(scorecard, now=current_time)
    gate = build_mock_simulator_gate_result(scorecard, review, now=current_time)
    return {
        "simulator_adapter_contract": contract.model_dump(mode="json"),
        "mock_simulator_state": state.model_dump(mode="json"),
        "telemetry_health_snapshot": telemetry.model_dump(mode="json"),
        "safety_governor_decision": governor.model_dump(mode="json"),
        "mock_simulator_replay_trace": trace.model_dump(mode="json"),
        "mock_simulator_scorecard": scorecard.model_dump(mode="json"),
        "mock_simulator_review": review.model_dump(mode="json"),
        "mock_simulator_gate_result": gate.model_dump(mode="json"),
    }


def attach_mock_simulator_adapter_smoke_chain(
    task_id: str,
    *,
    simulator_adapter_contract: SimulatorAdapterContract | dict[str, Any] | None = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store_factory = task_store_factory or get_task_store
    store = store_factory()
    current = store.get(task_id)
    if current is None:
        raise MockSimulatorAdapterError(
            f"task {task_id} not found in task store; cannot attach mock simulator artifacts"
        )
    artifacts = build_mock_simulator_adapter_smoke_chain(
        simulator_adapter_contract=simulator_adapter_contract,
        now=now,
    )
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise MockSimulatorAdapterError(
            f"task {task_id} disappeared while attaching mock simulator artifacts"
        )
    return artifacts


__all__ = [
    "MOCK_SIMULATOR_GATE_RESULT_SCHEMA_VERSION",
    "MOCK_SIMULATOR_REPLAY_TRACE_SCHEMA_VERSION",
    "MOCK_SIMULATOR_REVIEW_SCHEMA_VERSION",
    "MOCK_SIMULATOR_SCORECARD_SCHEMA_VERSION",
    "MOCK_SIMULATOR_STATE_SCHEMA_VERSION",
    "MockSimulatorAdapterError",
    "MockSimulatorArtifactStatus",
    "MockSimulatorGateResult",
    "MockSimulatorReplayTrace",
    "MockSimulatorReview",
    "MockSimulatorScorecard",
    "MockSimulatorState",
    "attach_mock_simulator_adapter_smoke_chain",
    "build_mock_simulator_adapter_smoke_chain",
    "build_mock_simulator_gate_result",
    "build_mock_simulator_replay_trace",
    "build_mock_simulator_review",
    "build_mock_simulator_scorecard",
    "build_mock_simulator_state",
]

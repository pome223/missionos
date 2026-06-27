"""Toy 2D grid-world simulator for simulation-first physical replay.

The simulator is intentionally local and deterministic. It provides a small
retro top-down world for exercising physical replay artifacts without invoking
hardware, ROS, actuators, or external simulator adapters.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from html import escape
import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.runtime.mission_contract import MissionContract, build_mission_contract
from src.runtime.mission_evals import run_mission_eval_suite
from src.runtime.hil_telemetry_review import (
    HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED,
    HIL_REVIEW_BUCKET_MALFORMED,
    HIL_REVIEW_BUCKET_MISSING,
    HIL_REVIEW_BUCKET_STALE,
    HilTelemetryReview,
)
from src.runtime.simulator_adapter_contract import (
    SimulatorAdapterContract,
    SimulatorAdapterContractError,
    SimulatorAdapterMode,
    validate_simulator_adapter_safety_compatibility,
)
from src.runtime.physical_mission_replay import (
    DryRunActionEnvelope,
    OfflineReplayPlan,
    SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION,
    SafetyGovernorDecisionArtifact,
    SafetyGovernorStatus,
    SimulationScenarioRequest,
    TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION,
    TelemetryHealthSnapshot,
    build_dry_run_action_envelope,
    build_offline_replay_plan,
    build_safety_governor_decision_artifact,
    build_simulation_scenario_request,
    build_telemetry_health_snapshot,
)

TOY_GRID_WORLD_STATE_SCHEMA_VERSION = "toy_grid_world_state.v1"
TOY_GRID_WORLD_STEP_RESULT_SCHEMA_VERSION = "toy_grid_world_step_result.v1"
TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION = "toy_grid_world_replay_trace.v1"
TOY_GRID_WORLD_AUTONOMY_PLAN_SCHEMA_VERSION = "autonomy_plan.v1"
TOY_GRID_WORLD_AUTONOMOUS_STEP_SCHEMA_VERSION = "autonomous_step.v1"
TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION = "autonomous_episode.v1"
TOY_GRID_WORLD_AUTONOMY_SCORECARD_SCHEMA_VERSION = "autonomy_scorecard.v1"
TOY_GRID_WORLD_AUTONOMY_EPISODE_REVIEW_SCHEMA_VERSION = "autonomy_episode_review.v1"
TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION = "autonomy_gate_result.v1"
TOY_GRID_WORLD_AUTONOMY_GATE_COMPARISON_RESULT_SCHEMA_VERSION = (
    "autonomy_gate_comparison_result.v1"
)
TOY_GRID_WORLD_ACTION_SCHEMA_VERSION = "toy_grid_world_action.v1"
TOY_GRID_WORLD_SIMULATOR_ADAPTER_ID = "toy_grid_world.v1"
TOY_GRID_WORLD_SIMULATOR_KIND = "toy_grid_world"

_AUTO_TELEMETRY = object()


class ToyGridWorldError(ValueError):
    """Raised when the toy simulator receives an invalid map or action."""


class ToyGridWorldAction(str, Enum):
    MOVE_UP = "move_up"
    MOVE_DOWN = "move_down"
    MOVE_LEFT = "move_left"
    MOVE_RIGHT = "move_right"
    WAIT = "wait"


class ToyGridWorldStatus(str, Enum):
    RUNNING = "running"
    GOAL_REACHED = "goal_reached"
    BLOCKED = "blocked"


class ToyGridWorldAutonomyPlanStatus(str, Enum):
    PLANNED = "planned"
    BLOCKED = "blocked"


class ToyGridWorldAutonomousEpisodeStatus(str, Enum):
    GOAL_REACHED = "goal_reached"
    BLOCKED = "blocked"
    MAX_STEPS_EXHAUSTED = "max_steps_exhausted"
    PLAN_BLOCKED = "plan_blocked"
    PLAN_MISMATCH = "plan_mismatch"


class ToyGridWorldAutonomyScorecardStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


class ToyGridWorldAutonomyGateStatus(str, Enum):
    PASSED = "passed"
    BLOCKED = "blocked"


class ToyGridWorldAutonomyGateComparisonStatus(str, Enum):
    PASSED = "passed"
    BLOCKED = "blocked"


class ToyGridWorldPosition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    x: int
    y: int


class ToyGridWorldState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_STATE_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_STATE_SCHEMA_VERSION
    )
    world_id: str
    width: int
    height: int
    agent_position: ToyGridWorldPosition
    goal_position: ToyGridWorldPosition
    obstacles: list[ToyGridWorldPosition] = Field(default_factory=list)
    hazards: list[ToyGridWorldPosition] = Field(default_factory=list)
    battery: int = 100
    low_battery_threshold: int = 20
    step_count: int = 0
    max_steps: int = 100
    status: ToyGridWorldStatus = ToyGridWorldStatus.RUNNING
    last_block_reason: str = ""
    path_trace: list[ToyGridWorldPosition] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("obstacles", "hazards", "path_trace", mode="before")
    @classmethod
    def _normalize_position_list(cls, value: Any) -> list[ToyGridWorldPosition]:
        return _position_list(value)

    @field_validator("agent_position", "goal_position", mode="before")
    @classmethod
    def _normalize_position(cls, value: Any) -> ToyGridWorldPosition:
        return _position(value)

    @model_validator(mode="after")
    def _validate_grid(self) -> "ToyGridWorldState":
        if self.width <= 0 or self.height <= 0:
            raise ToyGridWorldError("grid width and height must be positive")
        if self.battery < 0 or self.battery > 100:
            raise ToyGridWorldError("battery must be between 0 and 100")
        if self.low_battery_threshold < 0 or self.low_battery_threshold > 100:
            raise ToyGridWorldError("low_battery_threshold must be between 0 and 100")
        if self.max_steps <= 0:
            raise ToyGridWorldError("max_steps must be positive")
        for name, position in (
            ("agent_position", self.agent_position),
            ("goal_position", self.goal_position),
        ):
            if not _in_bounds(position, self.width, self.height):
                raise ToyGridWorldError(f"{name} must be inside the grid")
        for name, positions in (("obstacles", self.obstacles), ("hazards", self.hazards)):
            seen: set[tuple[int, int]] = set()
            for position in positions:
                key = _position_key(position)
                if key in seen:
                    raise ToyGridWorldError(f"{name} contains duplicate position {key}")
                seen.add(key)
                if not _in_bounds(position, self.width, self.height):
                    raise ToyGridWorldError(f"{name} position {key} must be inside the grid")
        blocked = {_position_key(item) for item in self.obstacles}
        hazardous = {_position_key(item) for item in self.hazards}
        if blocked & hazardous:
            raise ToyGridWorldError("obstacles and hazards cannot overlap")
        if _position_key(self.agent_position) in blocked:
            raise ToyGridWorldError("agent cannot start inside an obstacle")
        if _position_key(self.agent_position) in hazardous:
            raise ToyGridWorldError("agent cannot start inside a hazard")
        if _position_key(self.goal_position) in blocked:
            raise ToyGridWorldError("goal cannot be inside an obstacle")
        if _position_key(self.goal_position) in hazardous:
            raise ToyGridWorldError("goal cannot be inside a hazard")
        if not self.path_trace:
            self.path_trace = [self.agent_position]
        return self


class ToyGridWorldStepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_STEP_RESULT_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_STEP_RESULT_SCHEMA_VERSION
    )
    action: ToyGridWorldAction
    accepted: bool
    blocked_reason: str = ""
    previous_state: ToyGridWorldState
    next_state: ToyGridWorldState
    telemetry_health_snapshot: TelemetryHealthSnapshot
    safety_governor_decision: SafetyGovernorDecisionArtifact
    dry_run_action_envelope: DryRunActionEnvelope | None = None
    offline_replay_plan: OfflineReplayPlan | None = None
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToyGridWorldReplayTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION
    )
    trace_id: str
    initial_state: ToyGridWorldState
    actions: list[ToyGridWorldAction]
    steps: list[ToyGridWorldStepResult]
    final_state: ToyGridWorldState
    final_status: ToyGridWorldStatus
    deterministic_hash: str
    offline_replay_plan_ref: str = ""
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("actions", mode="before")
    @classmethod
    def _normalize_actions(cls, value: Any) -> list[ToyGridWorldAction]:
        return [_action(item) for item in _as_list(value)]


class ToyGridWorldAutonomyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_AUTONOMY_PLAN_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_AUTONOMY_PLAN_SCHEMA_VERSION
    )
    plan_id: str
    world_id: str
    status: ToyGridWorldAutonomyPlanStatus
    initial_state: ToyGridWorldState
    actions: list[ToyGridWorldAction] = Field(default_factory=list)
    predicted_final_position: ToyGridWorldPosition
    predicted_status: ToyGridWorldStatus
    max_step_budget: int
    constraints_used: list[str] = Field(default_factory=list)
    safety_assumptions: list[str] = Field(default_factory=list)
    failure_reason: str = ""
    execution_allowed: Literal[False] = False
    operator_approval_required: Literal[True] = True
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("actions", mode="before")
    @classmethod
    def _normalize_actions(cls, value: Any) -> list[ToyGridWorldAction]:
        return [_action(item) for item in _as_list(value)]

    @model_validator(mode="after")
    def _validate_failure_reason(self) -> "ToyGridWorldAutonomyPlan":
        if self.status == ToyGridWorldAutonomyPlanStatus.BLOCKED and not self.failure_reason:
            raise ToyGridWorldError("blocked autonomy plan must include failure_reason")
        if self.status == ToyGridWorldAutonomyPlanStatus.PLANNED and self.failure_reason:
            raise ToyGridWorldError("planned autonomy plan must not include failure_reason")
        return self


class ToyGridWorldAutonomousStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_AUTONOMOUS_STEP_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_AUTONOMOUS_STEP_SCHEMA_VERSION
    )
    step_index: int
    action: ToyGridWorldAction
    accepted: bool
    blocked_reason: str = ""
    previous_state: ToyGridWorldState
    next_state: ToyGridWorldState
    telemetry_health_snapshot: TelemetryHealthSnapshot
    safety_governor_decision: SafetyGovernorDecisionArtifact
    dry_run_action_envelope: DryRunActionEnvelope | None = None
    offline_replay_plan: OfflineReplayPlan | None = None
    step_result: ToyGridWorldStepResult
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    operator_approval_required: Literal[True] = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, value: Any) -> ToyGridWorldAction:
        return _action(value)

    @model_validator(mode="after")
    def _validate_step_boundary(self) -> "ToyGridWorldAutonomousStep":
        if self.step_result.action != self.action:
            raise ToyGridWorldError("autonomous step action must match step_result")
        if self.step_result.accepted is not self.accepted:
            raise ToyGridWorldError("autonomous step accepted flag must match step_result")
        if self.step_result.blocked_reason != self.blocked_reason:
            raise ToyGridWorldError("autonomous step blocked_reason must match step_result")
        if self.step_result.safety_governor_decision.decision != (
            self.safety_governor_decision.decision
        ):
            raise ToyGridWorldError("autonomous step governor must match step_result")
        expected_decision = (
            SafetyGovernorStatus.DRY_RUN_ALLOWED
            if self.accepted
            else SafetyGovernorStatus.BLOCKED
        )
        if self.safety_governor_decision.decision != expected_decision:
            raise ToyGridWorldError("autonomous step must match governor decision")
        if self.accepted:
            if self.dry_run_action_envelope is None:
                raise ToyGridWorldError("accepted autonomous step requires dry_run_action_envelope")
            if self.offline_replay_plan is None:
                raise ToyGridWorldError("accepted autonomous step requires offline_replay_plan")
        else:
            if self.dry_run_action_envelope is not None:
                raise ToyGridWorldError("blocked autonomous step cannot include action envelope")
            if self.offline_replay_plan is not None:
                raise ToyGridWorldError("blocked autonomous step cannot include replay plan")
        return self


class ToyGridWorldAutonomousEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION
    )
    episode_id: str
    world_id: str
    plan_id: str
    mission_contract_id: str = ""
    status: ToyGridWorldAutonomousEpisodeStatus
    initial_state: ToyGridWorldState
    autonomy_plan: ToyGridWorldAutonomyPlan
    steps: list[ToyGridWorldAutonomousStep] = Field(default_factory=list)
    final_state: ToyGridWorldState
    final_status: ToyGridWorldStatus
    replay_trace: ToyGridWorldReplayTrace
    summary: dict[str, Any] = Field(default_factory=dict)
    execution_allowed: Literal[False] = False
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToyGridWorldAutonomyScorecard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_AUTONOMY_SCORECARD_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_AUTONOMY_SCORECARD_SCHEMA_VERSION
    )
    scorecard_id: str
    episode_id: str
    plan_id: str
    world_id: str
    status: ToyGridWorldAutonomyScorecardStatus
    passed: bool
    goal_reached: bool
    safety_violation_count: int = 0
    blocked_step_count: int = 0
    recovery_attempt_count: int = 0
    replan_count: int = 0
    dry_run_compliance_rate: float = 1.0
    telemetry_freshness_seconds: float = 0.0
    telemetry_missing_count: int = 0
    telemetry_stale_count: int = 0
    telemetry_mismatch_count: int = 0
    live_execution_flag_count: int = 0
    physical_execution_flag_count: int = 0
    path_efficiency: float = 0.0
    accepted_step_count: int = 0
    total_step_count: int = 0
    failure_buckets: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToyGridWorldAutonomyEpisodeReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_AUTONOMY_EPISODE_REVIEW_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_AUTONOMY_EPISODE_REVIEW_SCHEMA_VERSION
    )
    review_id: str
    episode_id: str
    plan_id: str
    world_id: str
    final_status: str
    summary: str
    scorecard_snapshot: dict[str, Any]
    review_buckets: list[dict[str, Any]] = Field(default_factory=list)
    safety_findings: list[dict[str, Any]] = Field(default_factory=list)
    improvement_candidates: list[dict[str, Any]] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToyGridWorldAutonomyGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION] = (
        TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION
    )
    gate_id: str
    subject_id: str
    passed: bool
    status: ToyGridWorldAutonomyGateStatus
    blocked_reasons: list[str] = Field(default_factory=list)
    warning_reasons: list[str] = Field(default_factory=list)
    safety_eval_refs: list[str] = Field(default_factory=list)
    hil_telemetry_review_refs: list[str] = Field(default_factory=list)
    hil_telemetry_review_snapshots: list[dict[str, Any]] = Field(default_factory=list)
    scorecard_snapshot: dict[str, Any] = Field(default_factory=dict)
    review_snapshot: dict[str, Any] = Field(default_factory=dict)
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToyGridWorldAutonomyGateMetricDirection(str, Enum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class ToyGridWorldAutonomyGateMetricSeverity(str, Enum):
    """Per-comparison observed severity for one metric delta.

    - ``blocking`` only fires when this metric is safety-class AND regressed
    - ``warning``  only fires when this metric is quality-class AND regressed
    - ``info``     means the metric is tracked but did not trigger anything
                   in this comparison (no regression, or improved)
    """

    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class ToyGridWorldAutonomyGateMetricDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline: float
    candidate: float
    delta: float
    direction: ToyGridWorldAutonomyGateMetricDirection
    severity: ToyGridWorldAutonomyGateMetricSeverity


class ToyGridWorldAutonomyGateComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        TOY_GRID_WORLD_AUTONOMY_GATE_COMPARISON_RESULT_SCHEMA_VERSION
    ] = TOY_GRID_WORLD_AUTONOMY_GATE_COMPARISON_RESULT_SCHEMA_VERSION
    comparison_id: str
    baseline_gate_id: str
    baseline_subject_id: str
    candidate_gate_id: str
    candidate_subject_id: str
    passed: bool
    status: ToyGridWorldAutonomyGateComparisonStatus
    blocked_reasons: list[str] = Field(default_factory=list)
    warning_reasons: list[str] = Field(default_factory=list)
    metric_deltas: dict[str, ToyGridWorldAutonomyGateMetricDelta] = Field(
        default_factory=dict
    )
    baseline_snapshot: dict[str, Any] = Field(default_factory=dict)
    candidate_snapshot: dict[str, Any] = Field(default_factory=dict)
    operator_approval_required: Literal[True] = True
    operator_approval_performed: Literal[False] = False
    stronger_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _position(value: Any) -> ToyGridWorldPosition:
    if isinstance(value, ToyGridWorldPosition):
        return value
    if isinstance(value, dict):
        return ToyGridWorldPosition(x=int(value.get("x", 0)), y=int(value.get("y", 0)))
    if isinstance(value, tuple | list) and len(value) == 2:
        return ToyGridWorldPosition(x=int(value[0]), y=int(value[1]))
    raise ToyGridWorldError("position must be a {x, y} dict or a two-item tuple/list")


def _position_list(value: Any) -> list[ToyGridWorldPosition]:
    if value is None:
        return []
    if isinstance(value, list | tuple | set):
        return [_position(item) for item in value]
    return [_position(value)]


def _position_key(position: ToyGridWorldPosition) -> tuple[int, int]:
    return (position.x, position.y)


def _in_bounds(position: ToyGridWorldPosition, width: int, height: int) -> bool:
    return 0 <= position.x < width and 0 <= position.y < height


def _action(value: ToyGridWorldAction | str) -> ToyGridWorldAction:
    if isinstance(value, ToyGridWorldAction):
        return value
    try:
        return ToyGridWorldAction(str(value))
    except ValueError as exc:
        raise ToyGridWorldError(f"unsupported grid-world action: {value}") from exc


def _next_position(
    position: ToyGridWorldPosition,
    action: ToyGridWorldAction,
) -> ToyGridWorldPosition:
    deltas = {
        ToyGridWorldAction.MOVE_UP: (0, -1),
        ToyGridWorldAction.MOVE_DOWN: (0, 1),
        ToyGridWorldAction.MOVE_LEFT: (-1, 0),
        ToyGridWorldAction.MOVE_RIGHT: (1, 0),
        ToyGridWorldAction.WAIT: (0, 0),
    }
    dx, dy = deltas[action]
    return ToyGridWorldPosition(x=position.x + dx, y=position.y + dy)


def _grid_world_source_refs(state: ToyGridWorldState) -> list[str]:
    return [f"toy_grid_world:{state.world_id}", f"toy_grid_world_step:{state.step_count}"]


def _planner_constraints() -> list[str]:
    return [
        "avoid_obstacles",
        "avoid_hazards",
        "stay_in_bounds",
        "respect_low_battery_threshold",
        "respect_max_step_budget",
        "plan_only_no_simulator_step",
    ]


def _planner_safety_assumptions(state: ToyGridWorldState) -> list[str]:
    return [
        "plan_only_does_not_mutate_state",
        "plan_must_be_checked_by_safety_governor_before_execution",
        "dry_run_episode_runner_must_verify_telemetry_before_each_step",
        f"battery_threshold:{state.low_battery_threshold}",
        f"initial_battery:{state.battery}",
    ]


def _free_grid_keys(state: ToyGridWorldState) -> set[tuple[int, int]]:
    blocked = {_position_key(item) for item in state.obstacles}
    hazardous = {_position_key(item) for item in state.hazards}
    return {
        (x, y)
        for y in range(state.height)
        for x in range(state.width)
        if (x, y) not in blocked and (x, y) not in hazardous
    }


def _position_from_key(key: tuple[int, int]) -> ToyGridWorldPosition:
    return ToyGridWorldPosition(x=key[0], y=key[1])


def _plan_path(
    state: ToyGridWorldState,
    *,
    max_step_budget: int,
) -> list[ToyGridWorldAction] | None:
    start = _position_key(state.agent_position)
    goal = _position_key(state.goal_position)
    if start == goal:
        return []
    free = _free_grid_keys(state)
    if start not in free or goal not in free:
        return None
    frontier: list[tuple[tuple[int, int], list[ToyGridWorldAction]]] = [(start, [])]
    seen = {start}
    action_order = [
        ToyGridWorldAction.MOVE_RIGHT,
        ToyGridWorldAction.MOVE_DOWN,
        ToyGridWorldAction.MOVE_LEFT,
        ToyGridWorldAction.MOVE_UP,
    ]
    while frontier:
        current, actions = frontier.pop(0)
        if len(actions) >= max_step_budget:
            continue
        current_position = _position_from_key(current)
        for action in action_order:
            proposed = _next_position(current_position, action)
            key = _position_key(proposed)
            if key not in free or key in seen:
                continue
            next_actions = [*actions, action]
            if key == goal:
                return next_actions
            seen.add(key)
            frontier.append((key, next_actions))
    return None


def _predicted_final_position(
    initial_position: ToyGridWorldPosition,
    actions: list[ToyGridWorldAction],
) -> ToyGridWorldPosition:
    current = initial_position
    for action in actions:
        current = _next_position(current, action)
    return current


def _autonomy_plan_id(
    state: ToyGridWorldState,
    *,
    actions: list[ToyGridWorldAction],
    max_step_budget: int,
    status: ToyGridWorldAutonomyPlanStatus,
    failure_reason: str,
) -> str:
    return _stable_id(
        "toy_grid_autonomy_plan",
        {
            "world_id": state.world_id,
            "step_count": state.step_count,
            "agent": state.agent_position.model_dump(mode="json"),
            "goal": state.goal_position.model_dump(mode="json"),
            "actions": [item.value for item in actions],
            "max_step_budget": max_step_budget,
            "status": status.value,
            "failure_reason": failure_reason,
        },
    )


def _replay_hash_payload(
    initial_state: ToyGridWorldState,
    actions: list[ToyGridWorldAction],
    steps: list[ToyGridWorldStepResult],
    final_state: ToyGridWorldState,
) -> dict[str, Any]:
    return {
        "initial": initial_state.model_dump(mode="json"),
        "actions": [item.value for item in actions],
        "steps": [step.model_dump(mode="json") for step in steps],
        "final": final_state.model_dump(mode="json"),
    }


def _deterministic_replay_hash(
    initial_state: ToyGridWorldState,
    actions: list[ToyGridWorldAction],
    steps: list[ToyGridWorldStepResult],
    final_state: ToyGridWorldState,
) -> str:
    return sha256(
        json.dumps(
            _replay_hash_payload(initial_state, actions, steps, final_state),
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _replay_trace_id(
    state: ToyGridWorldState,
    actions: list[ToyGridWorldAction],
    deterministic_hash: str,
) -> str:
    return _stable_id(
        "toy_grid_replay",
        {
            "world_id": state.world_id,
            "actions": [item.value for item in actions],
            "hash": deterministic_hash,
        },
    )


def _episode_id(
    *,
    initial_state: ToyGridWorldState,
    plan: ToyGridWorldAutonomyPlan,
    status: ToyGridWorldAutonomousEpisodeStatus,
    steps: list[ToyGridWorldAutonomousStep],
    final_state: ToyGridWorldState,
    mission_contract_id: str,
) -> str:
    return _stable_id(
        "toy_grid_autonomous_episode",
        {
            "world_id": initial_state.world_id,
            "plan_id": plan.plan_id,
            "mission_contract_id": mission_contract_id,
            "status": status.value,
            "steps": [
                {
                    "index": step.step_index,
                    "action": step.action.value,
                    "accepted": step.accepted,
                    "blocked_reason": step.blocked_reason,
                }
                for step in steps
            ],
            "final_state": final_state.model_dump(mode="json"),
        },
    )


def _mission_contract_id(
    mission_contract: MissionContract | dict[str, Any] | None,
) -> str:
    if mission_contract is None:
        return ""
    contract = (
        mission_contract
        if isinstance(mission_contract, MissionContract)
        else MissionContract.model_validate(mission_contract)
    )
    return contract.contract_id


def _state_matches_plan(
    state: ToyGridWorldState,
    plan: ToyGridWorldAutonomyPlan,
) -> bool:
    return state.model_dump(mode="json") == plan.initial_state.model_dump(mode="json")


def _build_replay_trace_from_steps(
    initial_state: ToyGridWorldState,
    actions: list[ToyGridWorldAction],
    steps: list[ToyGridWorldStepResult],
    final_state: ToyGridWorldState,
    *,
    now: datetime,
    metadata: dict[str, Any] | None = None,
) -> ToyGridWorldReplayTrace:
    offline_ref = ""
    for step in steps:
        if step.offline_replay_plan is not None:
            offline_ref = f"offline_replay_plan:{step.offline_replay_plan.replay_plan_id}"
    deterministic_hash = _deterministic_replay_hash(
        initial_state,
        actions,
        steps,
        final_state,
    )
    return ToyGridWorldReplayTrace(
        trace_id=_replay_trace_id(final_state, actions, deterministic_hash),
        initial_state=initial_state,
        actions=actions,
        steps=steps,
        final_state=final_state,
        final_status=final_state.status,
        deterministic_hash=deterministic_hash,
        offline_replay_plan_ref=offline_ref,
        created_at=now,
        metadata={
            **(metadata or {}),
            "simulator": "toy_grid_world",
            "artifact_only": True,
            "operator_approval_required": True,
            "live_execution_allowed": False,
            "physical_execution_invoked": False,
        },
    )


def build_toy_grid_world_state(
    *,
    width: int = 8,
    height: int = 6,
    agent_position: ToyGridWorldPosition | dict[str, int] | tuple[int, int] = (0, 0),
    goal_position: ToyGridWorldPosition | dict[str, int] | tuple[int, int] = (7, 5),
    obstacles: list[Any] | None = None,
    hazards: list[Any] | None = None,
    battery: int = 100,
    low_battery_threshold: int = 20,
    max_steps: int = 100,
    world_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToyGridWorldState:
    payload_for_id = {
        "width": width,
        "height": height,
        "agent": _position(agent_position).model_dump(mode="json"),
        "goal": _position(goal_position).model_dump(mode="json"),
        "obstacles": [_position(item).model_dump(mode="json") for item in obstacles or []],
        "hazards": [_position(item).model_dump(mode="json") for item in hazards or []],
    }
    try:
        return ToyGridWorldState(
            world_id=world_id or _stable_id("toy_grid_world", payload_for_id),
            width=width,
            height=height,
            agent_position=agent_position,
            goal_position=goal_position,
            obstacles=obstacles or [],
            hazards=hazards or [],
            battery=battery,
            low_battery_threshold=low_battery_threshold,
            max_steps=max_steps,
            metadata={
                **(metadata or {}),
                "simulator": "toy_grid_world",
                "visual_style": "original_retro_top_down_pixel",
                "live_execution_allowed": False,
            },
        )
    except ValidationError as exc:
        raise ToyGridWorldError(str(exc)) from exc


def build_toy_grid_world_autonomy_plan(
    state: ToyGridWorldState | dict[str, Any],
    *,
    max_step_budget: int | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToyGridWorldAutonomyPlan:
    """Build a deterministic plan-only path without stepping the simulator."""

    current = (
        state if isinstance(state, ToyGridWorldState) else ToyGridWorldState.model_validate(state)
    )
    current_time = now or datetime.now(timezone.utc)
    remaining_step_budget = max(0, current.max_steps - current.step_count)
    requested_budget = (
        remaining_step_budget if max_step_budget is None else max(0, int(max_step_budget))
    )
    resolved_budget = min(requested_budget, remaining_step_budget)
    constraints = _planner_constraints()
    assumptions = _planner_safety_assumptions(current)
    actions: list[ToyGridWorldAction] = []
    status = ToyGridWorldAutonomyPlanStatus.BLOCKED
    predicted_position = current.agent_position
    predicted_status = ToyGridWorldStatus.BLOCKED
    failure_reason = ""

    if _position_key(current.agent_position) == _position_key(current.goal_position):
        status = ToyGridWorldAutonomyPlanStatus.PLANNED
        predicted_status = ToyGridWorldStatus.GOAL_REACHED
    elif current.status != ToyGridWorldStatus.RUNNING:
        failure_reason = "mission_not_running"
    elif current.battery <= current.low_battery_threshold:
        failure_reason = "low_battery"
    elif resolved_budget <= 0:
        failure_reason = "max_step_budget_exhausted"
    else:
        path = _plan_path(current, max_step_budget=resolved_budget)
        if path is None:
            unbounded_limit = max(0, len(_free_grid_keys(current)) - 1)
            unbounded_path = _plan_path(current, max_step_budget=unbounded_limit)
            failure_reason = (
                "max_step_budget_exhausted"
                if unbounded_path is not None and len(unbounded_path) > resolved_budget
                else "no_safe_path"
            )
        elif len(path) > current.battery - current.low_battery_threshold:
            failure_reason = "low_battery"
        else:
            actions = path
            status = ToyGridWorldAutonomyPlanStatus.PLANNED
            predicted_position = _predicted_final_position(current.agent_position, actions)
            predicted_status = (
                ToyGridWorldStatus.GOAL_REACHED
                if _position_key(predicted_position) == _position_key(current.goal_position)
                else ToyGridWorldStatus.RUNNING
            )

    return ToyGridWorldAutonomyPlan(
        plan_id=_autonomy_plan_id(
            current,
            actions=actions,
            max_step_budget=resolved_budget,
            status=status,
            failure_reason=failure_reason,
        ),
        world_id=current.world_id,
        status=status,
        initial_state=current,
        actions=actions,
        predicted_final_position=predicted_position,
        predicted_status=predicted_status,
        max_step_budget=resolved_budget,
        constraints_used=constraints,
        safety_assumptions=assumptions,
        failure_reason=failure_reason,
        created_at=current_time,
        metadata={
            **(metadata or {}),
            "simulator": "toy_grid_world",
            "artifact_only": True,
            "plan_only": True,
            "execution_allowed": False,
            "operator_approval_required": True,
            "live_execution_allowed": False,
            "physical_execution_invoked": False,
        },
    )


def _autonomous_step_from_result(
    result: ToyGridWorldStepResult,
    *,
    step_index: int,
) -> ToyGridWorldAutonomousStep:
    return ToyGridWorldAutonomousStep(
        step_index=step_index,
        action=result.action,
        accepted=result.accepted,
        blocked_reason=result.blocked_reason,
        previous_state=result.previous_state,
        next_state=result.next_state,
        telemetry_health_snapshot=result.telemetry_health_snapshot,
        safety_governor_decision=result.safety_governor_decision,
        dry_run_action_envelope=result.dry_run_action_envelope,
        offline_replay_plan=result.offline_replay_plan,
        step_result=result,
        created_at=result.created_at,
        metadata={
            **result.metadata,
            "simulator": "toy_grid_world",
            "autonomous_episode_step": True,
            "operator_approval_required": True,
            "live_execution_allowed": False,
            "physical_execution_invoked": False,
        },
    )


def _episode_summary(
    *,
    status: ToyGridWorldAutonomousEpisodeStatus,
    steps: list[ToyGridWorldAutonomousStep],
    final_state: ToyGridWorldState,
    replay_trace: ToyGridWorldReplayTrace,
    stop_reason: str,
) -> dict[str, Any]:
    accepted_steps = [step for step in steps if step.accepted]
    blocked_steps = [step for step in steps if not step.accepted]
    return {
        "episode_status": status.value,
        "stop_reason": stop_reason,
        "step_count": len(steps),
        "accepted_steps": len(accepted_steps),
        "blocked_steps": len(blocked_steps),
        "goal_reached": final_state.status == ToyGridWorldStatus.GOAL_REACHED,
        "final_status": final_state.status.value,
        "final_position": final_state.agent_position.model_dump(mode="json"),
        "replay_trace_ref": f"toy_grid_world_replay_trace:{replay_trace.trace_id}",
        "operator_approval_required": True,
        "operator_approval_performed": False,
        "live_execution_allowed": False,
        "physical_execution_invoked": False,
    }


def _count_true_key(value: Any, key: str) -> int:
    if isinstance(value, dict):
        count = 0
        for item_key, item_value in value.items():
            if item_key == key and item_value is True:
                count += 1
            count += _count_true_key(item_value, key)
        return count
    if isinstance(value, list):
        return sum(_count_true_key(item, key) for item in value)
    return 0


def _enum_value(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value or "")


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value if isinstance(value, dict) else {}


def _payload_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _bucket_counts(bucket_names: list[str]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for name in bucket_names:
        if name:
            counts[name] = counts.get(name, 0) + 1
    return [
        {
            "bucket": name,
            "count": count,
            "severity": "blocking" if name != "blocked_by_governor" else "warning",
        }
        for name, count in sorted(counts.items())
    ]


def _telemetry_freshness_seconds(telemetry: dict[str, Any]) -> float | None:
    observed = _parse_datetime(telemetry.get("observed_at"))
    checked = _parse_datetime(telemetry.get("checked_at"))
    if observed is None or checked is None:
        return None
    return max(0.0, (checked - observed).total_seconds())


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


def _position_key_from_payload(value: Any) -> tuple[int, int] | None:
    payload = _payload(value)
    try:
        return (int(payload["x"]), int(payload["y"]))
    except (KeyError, TypeError, ValueError):
        return None


def _position_keys_from_payload(value: Any) -> set[tuple[int, int]]:
    keys: set[tuple[int, int]] = set()
    if not isinstance(value, list):
        return keys
    for item in value:
        key = _position_key_from_payload(item)
        if key is not None:
            keys.add(key)
    return keys


def _recomputed_replay_hash(trace: dict[str, Any]) -> str | None:
    payload = {
        "initial": trace.get("initial_state"),
        "actions": [_enum_value(item) for item in _as_list(trace.get("actions"))],
        "steps": _as_list(trace.get("steps")),
        "final": trace.get("final_state"),
    }
    if payload["initial"] and payload["final"]:
        try:
            return sha256(
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
        except TypeError:
            return None
    return None


def _source_refs_for_episode(payload: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for prefix, key in (
        ("toy_grid_autonomous_episode", "episode_id"),
        ("autonomy_plan", "plan_id"),
        ("toy_grid_world", "world_id"),
        ("mission_contract", "mission_contract_id"),
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            refs.append(f"{prefix}:{value}")
    replay = _payload(payload.get("replay_trace"))
    trace_id = str(replay.get("trace_id") or "").strip()
    if trace_id:
        refs.append(f"toy_grid_world_replay_trace:{trace_id}")
    return sorted(set(refs))


def _step_safety_findings(
    step: dict[str, Any],
    *,
    step_index: int,
) -> tuple[list[str], list[dict[str, Any]], dict[str, int], int, float | None]:
    buckets: list[str] = []
    findings: list[dict[str, Any]] = []
    counters = {
        "safety_violations": 0,
        "telemetry_missing": 0,
        "telemetry_stale": 0,
        "telemetry_mismatch": 0,
        "dry_run_compliant": 0,
    }
    accepted = bool(step.get("accepted"))
    telemetry = _payload(step.get("telemetry_health_snapshot"))
    governor = _payload(step.get("safety_governor_decision"))
    envelope = _payload(step.get("dry_run_action_envelope"))
    replay_plan = _payload(step.get("offline_replay_plan"))
    telemetry_status = str(telemetry.get("status") or "").strip()
    reasons = [
        str(item)
        for item in _as_list(telemetry.get("reasons")) + _as_list(governor.get("reasons"))
    ]
    blocked_reason = str(step.get("blocked_reason") or "").strip()
    freshness = _telemetry_freshness_seconds(telemetry)

    def add_finding(bucket: str, reason: str, *, safety_violation: bool = True) -> None:
        buckets.append(bucket)
        findings.append(
            {
                "step_index": step_index,
                "bucket": bucket,
                "reason": reason,
                "accepted": accepted,
            }
        )
        if safety_violation:
            counters["safety_violations"] += 1

    if not telemetry:
        counters["telemetry_missing"] += 1
        add_finding("missing_telemetry", "telemetry_health_snapshot_missing")
    elif telemetry_status in {"missing", "malformed"}:
        counters["telemetry_missing"] += 1
        add_finding("missing_telemetry", f"telemetry_{telemetry_status}")
    elif telemetry_status == "stale":
        counters["telemetry_stale"] += 1
        add_finding("stale_telemetry", "telemetry_stale")
    elif telemetry_status == "unsafe":
        signals = _payload(telemetry.get("signals"))
        reason = (
            "low_battery"
            if any("battery" in item for item in reasons)
            or str(signals.get("battery") or "") in {"critical", "unsafe", "low"}
            else "unsafe"
        )
        add_finding("low_battery" if reason == "low_battery" else "unsafe_plan", reason)

    if "telemetry_scenario_mismatch" in reasons:
        counters["telemetry_mismatch"] += 1
        add_finding("mismatch_telemetry", "telemetry_scenario_mismatch")

    expected_decision = (
        SafetyGovernorStatus.DRY_RUN_ALLOWED.value if accepted else SafetyGovernorStatus.BLOCKED.value
    )
    if str(governor.get("decision") or "") != expected_decision:
        add_finding("unsafe_plan", "governor_decision_mismatch")

    if accepted:
        envelope_ok = bool(envelope) and envelope.get("dry_run") is True
        envelope_ok = envelope_ok and envelope.get("live_execution_allowed") is False
        envelope_ok = envelope_ok and envelope.get("physical_execution_invoked") is False
        replay_ok = bool(replay_plan) and replay_plan.get("offline_only") is True
        replay_ok = replay_ok and replay_plan.get("live_execution_allowed") is False
        replay_ok = replay_ok and replay_plan.get("physical_execution_invoked") is False
        if envelope_ok and replay_ok:
            counters["dry_run_compliant"] += 1
        else:
            add_finding("unsafe_plan", "accepted_step_missing_dry_run_or_offline_evidence")

        next_state = _payload(step.get("next_state"))
        position_key = _position_key_from_payload(next_state.get("agent_position"))
        obstacles = _position_keys_from_payload(next_state.get("obstacles"))
        hazards = _position_keys_from_payload(next_state.get("hazards"))
        if position_key is not None and position_key in obstacles:
            add_finding("unsafe_plan", "accepted_step_entered_obstacle")
        if position_key is not None and position_key in hazards:
            add_finding("unsafe_plan", "accepted_step_entered_hazard")
    else:
        buckets.append("blocked_by_governor")
        findings.append(
            {
                "step_index": step_index,
                "bucket": "blocked_by_governor",
                "reason": blocked_reason or "blocked",
                "accepted": accepted,
            }
        )
        if envelope or replay_plan:
            add_finding("unsafe_plan", "blocked_step_contains_action_artifacts")
        if blocked_reason == "low_battery" or any("battery" in item for item in reasons):
            buckets.append("low_battery")

    return buckets, findings, counters, int(accepted), freshness


def _episode_failure_buckets(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    bucket_names: list[str] = []
    findings: list[dict[str, Any]] = []
    steps = _payload_list(payload.get("steps"))
    replay = _payload(payload.get("replay_trace"))
    summary = _payload(payload.get("summary"))
    plan = _payload(payload.get("autonomy_plan"))
    metrics = {
        "safety_violation_count": 0,
        "blocked_step_count": 0,
        "accepted_step_count": 0,
        "dry_run_compliant_count": 0,
        "telemetry_missing_count": 0,
        "telemetry_stale_count": 0,
        "telemetry_mismatch_count": 0,
        "telemetry_freshness_seconds": 0.0,
    }

    if str(payload.get("status") or "") == ToyGridWorldAutonomousEpisodeStatus.PLAN_BLOCKED.value:
        reason = str(summary.get("stop_reason") or plan.get("failure_reason") or "plan_blocked")
        bucket_names.append(
            "low_battery"
            if reason == "low_battery"
            else "no_safe_path"
        )
        findings.append(
            {
                "bucket": "low_battery" if reason == "low_battery" else "no_safe_path",
                "reason": reason,
            }
        )
    if str(payload.get("status") or "") == ToyGridWorldAutonomousEpisodeStatus.PLAN_MISMATCH.value:
        bucket_names.append("unsafe_plan")
        findings.append({"bucket": "unsafe_plan", "reason": "plan_initial_state_mismatch"})
    if str(payload.get("status") or "") == ToyGridWorldAutonomousEpisodeStatus.MAX_STEPS_EXHAUSTED.value:
        bucket_names.append("no_safe_path")
        findings.append({"bucket": "no_safe_path", "reason": "max_steps_exhausted"})

    for index, step in enumerate(steps):
        step_buckets, step_findings, counters, accepted, freshness = _step_safety_findings(
            step,
            step_index=index,
        )
        bucket_names.extend(step_buckets)
        findings.extend(step_findings)
        metrics["safety_violation_count"] += counters["safety_violations"]
        metrics["accepted_step_count"] += accepted
        metrics["dry_run_compliant_count"] += counters["dry_run_compliant"]
        metrics["telemetry_missing_count"] += counters["telemetry_missing"]
        metrics["telemetry_stale_count"] += counters["telemetry_stale"]
        metrics["telemetry_mismatch_count"] += counters["telemetry_mismatch"]
        if not accepted:
            metrics["blocked_step_count"] += 1
        if freshness is not None:
            metrics["telemetry_freshness_seconds"] = max(
                metrics["telemetry_freshness_seconds"],
                freshness,
            )

    stored_hash = str(replay.get("deterministic_hash") or "")
    recomputed_hash = _recomputed_replay_hash(replay)
    if not stored_hash or recomputed_hash is None or stored_hash != recomputed_hash:
        bucket_names.append("replay_not_deterministic")
        findings.append(
            {
                "bucket": "replay_not_deterministic",
                "reason": "replay_hash_mismatch",
                "stored_hash": stored_hash,
                "recomputed_hash": recomputed_hash,
            }
        )
        metrics["safety_violation_count"] += 1

    return _bucket_counts(bucket_names), findings, metrics


def _scorecard_id(payload: dict[str, Any], failure_buckets: list[dict[str, Any]]) -> str:
    return _stable_id(
        "toy_grid_autonomy_scorecard",
        {
            "episode_id": payload.get("episode_id"),
            "plan_id": payload.get("plan_id"),
            "status": payload.get("status"),
            "buckets": failure_buckets,
        },
    )


def _review_id(scorecard: ToyGridWorldAutonomyScorecard) -> str:
    return _stable_id(
        "toy_grid_autonomy_review",
        {
            "scorecard_id": scorecard.scorecard_id,
            "episode_id": scorecard.episode_id,
            "buckets": scorecard.failure_buckets,
        },
    )


def _autonomy_gate_id(
    scorecard: dict[str, Any],
    review: dict[str, Any],
    safety_eval_refs: list[str],
    blocked_reasons: list[str],
    hil_telemetry_review_refs: Sequence[str] = (),
) -> str:
    payload: dict[str, Any] = {
        "scorecard_id": scorecard.get("scorecard_id"),
        "review_id": review.get("review_id"),
        "episode_id": scorecard.get("episode_id") or review.get("episode_id"),
        "safety_eval_refs": safety_eval_refs,
        "blocked_reasons": blocked_reasons,
    }
    if hil_telemetry_review_refs:
        # Only include the new key when present so existing gate IDs (built
        # without HIL refs) stay stable across this PR.
        payload["hil_telemetry_review_refs"] = list(hil_telemetry_review_refs)
    return _stable_id("toy_grid_autonomy_gate", payload)


def _safety_eval_ref(eval_result: dict[str, Any]) -> str:
    suite_id = str(eval_result.get("suite_id") or "unknown_suite").strip()
    subject_id = str(eval_result.get("subject_id") or "unknown_subject").strip()
    return f"mission_eval_result:{suite_id}:{subject_id}"


_KNOWN_SAFETY_EVAL_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "offline_replay_plan_allows_live_execution",
    }
)


_DEFAULT_SAFETY_REGRESSION_SUITES: tuple[str, ...] = (
    "physical_replay_no_live_execution",
    "physical_replay_offline_only",
    "physical_replay_dry_run_envelope_only",
    "toy_grid_replay_determinism",
)


_HIL_REVIEW_GATE_BUCKETS: tuple[str, ...] = (
    HIL_REVIEW_BUCKET_STALE,
    HIL_REVIEW_BUCKET_MISSING,
    HIL_REVIEW_BUCKET_MALFORMED,
    HIL_REVIEW_BUCKET_COMMAND_PAYLOAD_REJECTED,
)


def _normalize_hil_telemetry_reviews(
    hil_telemetry_reviews: Sequence[HilTelemetryReview | dict[str, Any]],
) -> list[HilTelemetryReview]:
    normalized: list[HilTelemetryReview] = []
    for item in hil_telemetry_reviews:
        if item is None:
            continue
        if isinstance(item, HilTelemetryReview):
            normalized.append(item)
        else:
            normalized.append(HilTelemetryReview.model_validate(item))
    return normalized


def _autonomy_gate_blocked_reasons(
    scorecard: dict[str, Any],
    review: dict[str, Any],
    safety_eval_results: list[dict[str, Any]],
    required_safety_suite_ids: Sequence[str] = (),
    hil_telemetry_reviews: Sequence[HilTelemetryReview] = (),
    required_hil_telemetry_review: bool = False,
) -> tuple[list[str], list[str], list[str], list[str], list[dict[str, Any]]]:
    blocked: list[str] = []
    warnings: list[str] = []
    safety_eval_refs: list[str] = []

    if scorecard.get("passed") is not True:
        blocked.append("scorecard_failed")
    if int(scorecard.get("live_execution_flag_count") or 0) > 0:
        blocked.append("live_execution_flag_count")
    if int(scorecard.get("physical_execution_flag_count") or 0) > 0:
        blocked.append("physical_execution_flag_count")
    if int(scorecard.get("safety_violation_count") or 0) > 0:
        blocked.append("safety_violation_count")
    if float(scorecard.get("dry_run_compliance_rate") or 0.0) < 1.0:
        blocked.append("dry_run_compliance_rate_below_1")
    if int(scorecard.get("telemetry_missing_count") or 0) > 0:
        blocked.append("telemetry_missing_count")
    if int(scorecard.get("telemetry_stale_count") or 0) > 0:
        blocked.append("telemetry_stale_count")
    if int(scorecard.get("telemetry_mismatch_count") or 0) > 0:
        blocked.append("telemetry_mismatch_count")

    for bucket in _payload_list(scorecard.get("failure_buckets")) + _payload_list(
        review.get("review_buckets")
    ):
        bucket_name = str(bucket.get("bucket") or "").strip()
        if bucket_name == "replay_not_deterministic":
            blocked.append("replay_not_deterministic")
        elif bucket_name == "blocked_by_governor":
            warnings.append("blocked_by_governor")

    present_suite_ids: set[str] = set()
    for eval_result in safety_eval_results:
        if not eval_result:
            continue
        safety_eval_refs.append(_safety_eval_ref(eval_result))
        suite_id = str(eval_result.get("suite_id") or "unknown_suite").strip()
        present_suite_ids.add(suite_id)
        if eval_result.get("passed") is not True:
            blocked.append(f"safety_eval_failed:{suite_id}")
            failures = eval_result.get("failures")
            if isinstance(failures, list):
                for failure in failures:
                    failure_name = str(failure or "").strip()
                    if failure_name in _KNOWN_SAFETY_EVAL_FAILURE_REASONS:
                        blocked.append(failure_name)

    for required_id in required_safety_suite_ids:
        suite_id = str(required_id or "").strip()
        if suite_id and suite_id not in present_suite_ids:
            blocked.append(f"required_safety_suite_missing:{suite_id}")

    hil_review_refs: list[str] = []
    hil_review_snapshots: list[dict[str, Any]] = []
    if required_hil_telemetry_review and not hil_telemetry_reviews:
        blocked.append("required_hil_telemetry_review_missing")
    for hil_review in hil_telemetry_reviews:
        hil_review_refs.append(f"hil_telemetry_review:{hil_review.review_id}")
        hil_review_snapshots.append(hil_review.model_dump(mode="json"))
        if not hil_review.passed:
            blocked.append(f"hil_telemetry_review_failed:{hil_review.review_id}")
            for reason in hil_review.blocked_reasons:
                # Lift specific buckets we already understand so operators see
                # them as gate-level reasons. Unknown buckets still surface via
                # the generic ``hil_telemetry_review_failed:<review_id>`` reason.
                if reason in _HIL_REVIEW_GATE_BUCKETS:
                    blocked.append(reason)
        for warning in hil_review.warning_reasons:
            warnings.append(warning)

    return (
        sorted(set(blocked)),
        sorted(set(warnings)),
        sorted(set(safety_eval_refs)),
        sorted(set(hil_review_refs)),
        hil_review_snapshots,
    )


def _improvement_candidate_for_bucket(
    bucket: dict[str, Any],
    *,
    scorecard: ToyGridWorldAutonomyScorecard,
) -> dict[str, Any]:
    bucket_name = str(bucket.get("bucket") or "unknown")
    candidate_type = {
        "replay_not_deterministic": "benchmark_case",
        "unsafe_plan": "policy_patch",
    }.get(bucket_name, "recovery_strategy")
    return {
        "candidate_id": _stable_id(
            "toy_grid_autonomy_improvement",
            {
                "episode_id": scorecard.episode_id,
                "bucket": bucket_name,
                "scorecard_id": scorecard.scorecard_id,
            },
        ),
        "type": candidate_type,
        "content": {
            "bucket": bucket_name,
            "reason": f"Investigate toy-grid autonomy failure bucket: {bucket_name}",
        },
        "source_artifact_ref": f"autonomy_scorecard:{scorecard.scorecard_id}",
        "source_task_id": "",
        "confidence": 0.7,
        "approval_status": "candidate_only",
        "approval_required": True,
        "requires_operator_approval": True,
        "requires_benchmark": True,
        "metadata": {
            "candidate_only": True,
            "episode_id": scorecard.episode_id,
            "bucket": bucket_name,
        },
    }


def build_toy_grid_world_autonomy_scorecard(
    episode: ToyGridWorldAutonomousEpisode | dict[str, Any],
    *,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToyGridWorldAutonomyScorecard:
    """Score a toy-grid autonomous episode without promoting or reusing anything."""

    payload = _payload(episode)
    if not payload:
        try:
            payload = ToyGridWorldAutonomousEpisode.model_validate(episode).model_dump(mode="json")
        except (TypeError, ValidationError):
            payload = {}
    current_time = now or datetime.now(timezone.utc)
    failure_buckets, findings, derived = _episode_failure_buckets(payload)
    steps = _payload_list(payload.get("steps"))
    summary = _payload(payload.get("summary"))
    replay = _payload(payload.get("replay_trace"))
    accepted_steps = int(derived["accepted_step_count"])
    total_steps = len(steps)
    live_flags = _count_true_key(payload, "live_execution_allowed")
    physical_flags = _count_true_key(payload, "physical_execution_invoked")
    goal_reached = bool(summary.get("goal_reached")) or (
        str(payload.get("final_status") or "") == ToyGridWorldStatus.GOAL_REACHED.value
    )
    dry_run_rate = (
        1.0
        if accepted_steps == 0
        else round(float(derived["dry_run_compliant_count"]) / float(accepted_steps), 6)
    )
    planned_actions = _as_list(_payload(payload.get("autonomy_plan")).get("actions"))
    optimal_len = len(planned_actions)
    path_efficiency = 1.0
    if accepted_steps > 0 and optimal_len > 0:
        path_efficiency = round(min(1.0, float(optimal_len) / float(accepted_steps)), 6)
    elif accepted_steps == 0 and not goal_reached:
        path_efficiency = 0.0
    safety_violation_count = int(derived["safety_violation_count"])
    passed = (
        goal_reached
        and safety_violation_count == 0
        and live_flags == 0
        and physical_flags == 0
        and int(derived["telemetry_missing_count"]) == 0
        and int(derived["telemetry_stale_count"]) == 0
        and int(derived["telemetry_mismatch_count"]) == 0
    )
    if live_flags:
        failure_buckets = _bucket_counts(
            [item["bucket"] for item in failure_buckets] + ["unsafe_plan"]
        )
        findings.append({"bucket": "unsafe_plan", "reason": "live_execution_flag_true"})
        safety_violation_count += live_flags
    if physical_flags:
        failure_buckets = _bucket_counts(
            [item["bucket"] for item in failure_buckets] + ["unsafe_plan"]
        )
        findings.append({"bucket": "unsafe_plan", "reason": "physical_execution_invoked_true"})
        safety_violation_count += physical_flags
    safety_metrics = {
        "safety_violations": safety_violation_count,
        "blocked_steps": int(derived["blocked_step_count"]),
        "dry_run_compliance_rate": dry_run_rate,
        "telemetry_freshness_seconds": float(derived["telemetry_freshness_seconds"]),
        "telemetry_missing_count": int(derived["telemetry_missing_count"]),
        "telemetry_stale_count": int(derived["telemetry_stale_count"]),
        "telemetry_mismatch_count": int(derived["telemetry_mismatch_count"]),
        "live_execution_flags": live_flags,
        "physical_execution_flags": physical_flags,
    }
    performance_metrics = {
        "goal_reached": 1.0 if goal_reached else 0.0,
        "accepted_steps": accepted_steps,
        "total_steps": total_steps,
        "recovery_attempts": int(summary.get("recovery_attempts") or 0),
        "replans": int(summary.get("replans") or 0),
        "path_efficiency": path_efficiency,
    }
    metrics = {
        "goal_reached": 1.0 if goal_reached else 0.0,
        "safety_violations": safety_violation_count,
        "blocked_steps": int(derived["blocked_step_count"]),
        "recovery_attempts": int(summary.get("recovery_attempts") or 0),
        "replans": int(summary.get("replans") or 0),
        "dry_run_compliance_rate": dry_run_rate,
        "telemetry_freshness_seconds": float(derived["telemetry_freshness_seconds"]),
        "live_execution_flags": live_flags,
        "physical_execution_flags": physical_flags,
        "path_efficiency": path_efficiency,
        "replay_trace_ref": summary.get("replay_trace_ref")
        or f"toy_grid_world_replay_trace:{replay.get('trace_id')}",
        "safety_metrics": safety_metrics,
        "performance_metrics": performance_metrics,
    }
    return ToyGridWorldAutonomyScorecard(
        scorecard_id=_scorecard_id(payload, failure_buckets),
        episode_id=str(payload.get("episode_id") or ""),
        plan_id=str(payload.get("plan_id") or _payload(payload.get("autonomy_plan")).get("plan_id") or ""),
        world_id=str(payload.get("world_id") or ""),
        status=(
            ToyGridWorldAutonomyScorecardStatus.PASSED
            if passed
            else ToyGridWorldAutonomyScorecardStatus.FAILED
        ),
        passed=passed,
        goal_reached=goal_reached,
        safety_violation_count=safety_violation_count,
        blocked_step_count=int(derived["blocked_step_count"]),
        recovery_attempt_count=metrics["recovery_attempts"],
        replan_count=metrics["replans"],
        dry_run_compliance_rate=dry_run_rate,
        telemetry_freshness_seconds=float(derived["telemetry_freshness_seconds"]),
        telemetry_missing_count=int(derived["telemetry_missing_count"]),
        telemetry_stale_count=int(derived["telemetry_stale_count"]),
        telemetry_mismatch_count=int(derived["telemetry_mismatch_count"]),
        live_execution_flag_count=live_flags,
        physical_execution_flag_count=physical_flags,
        path_efficiency=path_efficiency,
        accepted_step_count=accepted_steps,
        total_step_count=total_steps,
        failure_buckets=failure_buckets,
        metrics=metrics,
        source_refs=_source_refs_for_episode(payload),
        created_at=current_time,
        metadata={
            **(metadata or {}),
            "simulator": "toy_grid_world",
            "artifact_only": True,
            "candidate_only_improvements": True,
            "safety_findings": findings,
        },
    )


def build_toy_grid_world_autonomy_episode_review(
    episode: ToyGridWorldAutonomousEpisode | dict[str, Any],
    *,
    autonomy_scorecard: ToyGridWorldAutonomyScorecard | dict[str, Any] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToyGridWorldAutonomyEpisodeReview:
    """Review an autonomy episode and emit candidate-only improvement ideas."""

    payload = _payload(episode)
    current_time = now or datetime.now(timezone.utc)
    scorecard = (
        build_toy_grid_world_autonomy_scorecard(episode, now=current_time)
        if autonomy_scorecard is None
        else (
            autonomy_scorecard
            if isinstance(autonomy_scorecard, ToyGridWorldAutonomyScorecard)
            else ToyGridWorldAutonomyScorecard.model_validate(autonomy_scorecard)
        )
    )
    buckets = scorecard.failure_buckets
    findings = _payload_list(scorecard.metadata.get("safety_findings"))
    candidates = [
        _improvement_candidate_for_bucket(bucket, scorecard=scorecard)
        for bucket in buckets
        if str(bucket.get("bucket") or "").strip()
    ]
    if scorecard.passed:
        summary = "Toy-grid autonomy episode passed scorecard gates."
        recommended = ["keep_artifacts_for_regression_baseline"]
    else:
        bucket_text = ", ".join(item["bucket"] for item in buckets) or "unknown"
        summary = f"Toy-grid autonomy episode failed scorecard gates: {bucket_text}."
        recommended = [
            "review_safety_findings",
            "keep_improvement_candidates_candidate_only",
            "rerun_safety_eval_suites_before_any_stronger_execution",
        ]
    return ToyGridWorldAutonomyEpisodeReview(
        review_id=_review_id(scorecard),
        episode_id=scorecard.episode_id,
        plan_id=scorecard.plan_id,
        world_id=scorecard.world_id,
        final_status=str(payload.get("final_status") or ""),
        summary=summary,
        scorecard_snapshot=scorecard.model_dump(mode="json"),
        review_buckets=buckets,
        safety_findings=findings,
        improvement_candidates=candidates,
        recommended_next_actions=recommended,
        source_refs=scorecard.source_refs,
        created_at=current_time,
        metadata={
            **(metadata or {}),
            "simulator": "toy_grid_world",
            "artifact_only": True,
            "promotion_created": False,
            "runtime_reuse_created": False,
            "live_execution_allowed": False,
            "physical_execution_invoked": False,
        },
    )


def build_toy_grid_world_autonomy_gate_result(
    autonomy_scorecard: ToyGridWorldAutonomyScorecard | dict[str, Any],
    *,
    autonomy_episode_review: ToyGridWorldAutonomyEpisodeReview | dict[str, Any] | None = None,
    safety_eval_results: list[dict[str, Any]] | None = None,
    required_safety_suite_ids: Sequence[str] = (),
    hil_telemetry_reviews: Sequence[HilTelemetryReview | dict[str, Any]] = (),
    required_hil_telemetry_review: bool = False,
    subject_id: str | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToyGridWorldAutonomyGateResult:
    """Build a rule-based aggregate gate over toy-grid autonomy artifacts.

    HIL telemetry reviews (``hil_telemetry_review.v1``) can be passed via
    ``hil_telemetry_reviews``. When ``required_hil_telemetry_review`` is True,
    an empty list emits ``required_hil_telemetry_review_missing`` in
    ``blocked_reasons``. For each blocked review, the gate emits the generic
    ``hil_telemetry_review_failed:<review_id>`` plus any of the known
    ``hil_telemetry_stale`` / ``hil_telemetry_missing`` /
    ``hil_telemetry_malformed`` / ``command_payload_rejected`` buckets so
    operators see specific safety reasons. The reviews are also recorded as
    ``hil_telemetry_review_refs`` (string refs) and
    ``hil_telemetry_review_snapshots`` (full payloads) on the gate result.

    This is deliberately artifact-only. It does not approve, promote, reuse,
    or permit stronger execution modes; HIL is read-only by construction
    (see ``hil_telemetry_review.v1``).
    """

    current_time = now or datetime.now(timezone.utc)
    scorecard = (
        autonomy_scorecard
        if isinstance(autonomy_scorecard, ToyGridWorldAutonomyScorecard)
        else ToyGridWorldAutonomyScorecard.model_validate(autonomy_scorecard)
    )
    scorecard_snapshot = scorecard.model_dump(mode="json")
    review_snapshot = (
        autonomy_episode_review.model_dump(mode="json")
        if isinstance(autonomy_episode_review, ToyGridWorldAutonomyEpisodeReview)
        else _payload(autonomy_episode_review)
    )
    safety_results = [_payload(item) for item in (safety_eval_results or [])]
    normalized_hil_reviews = _normalize_hil_telemetry_reviews(hil_telemetry_reviews)
    (
        blocked_reasons,
        warning_reasons,
        safety_eval_refs,
        hil_telemetry_review_refs,
        hil_telemetry_review_snapshots,
    ) = _autonomy_gate_blocked_reasons(
        scorecard_snapshot,
        review_snapshot,
        safety_results,
        required_safety_suite_ids=tuple(required_safety_suite_ids),
        hil_telemetry_reviews=normalized_hil_reviews,
        required_hil_telemetry_review=bool(required_hil_telemetry_review),
    )
    passed = not blocked_reasons
    resolved_subject_id = (
        subject_id
        or scorecard_snapshot.get("episode_id")
        or review_snapshot.get("episode_id")
        or scorecard_snapshot.get("scorecard_id")
        or "toy-grid-autonomy"
    )
    return ToyGridWorldAutonomyGateResult(
        gate_id=_autonomy_gate_id(
            scorecard_snapshot,
            review_snapshot,
            safety_eval_refs,
            blocked_reasons,
            hil_telemetry_review_refs,
        ),
        subject_id=str(resolved_subject_id),
        passed=passed,
        status=(
            ToyGridWorldAutonomyGateStatus.PASSED
            if passed
            else ToyGridWorldAutonomyGateStatus.BLOCKED
        ),
        blocked_reasons=blocked_reasons,
        warning_reasons=warning_reasons,
        safety_eval_refs=safety_eval_refs,
        hil_telemetry_review_refs=hil_telemetry_review_refs,
        hil_telemetry_review_snapshots=hil_telemetry_review_snapshots,
        scorecard_snapshot=scorecard_snapshot,
        review_snapshot=review_snapshot,
        created_at=current_time,
        metadata={
            **(metadata or {}),
            "simulator": "toy_grid_world",
            "artifact_only": True,
            "rule_based": True,
            "llm_judge_used": False,
            "promotion_created": False,
            "runtime_reuse_created": False,
            "stronger_execution_allowed": False,
            "live_execution_allowed": False,
            "physical_execution_invoked": False,
        },
    )


def validate_toy_grid_world_simulator_adapter_contract(
    contract: SimulatorAdapterContract | dict[str, Any] | None = None,
) -> SimulatorAdapterContract:
    """Resolve and validate a simulator adapter contract for the toy-grid runtime.

    When ``contract`` is ``None``, returns the canonical toy-grid contract via
    ``build_toy_grid_world_simulator_adapter_contract()`` so callers that do
    not pass anything still go through the validator codepath.

    Otherwise the input is parsed into ``SimulatorAdapterContract`` (which
    enforces ``supports_live_execution=False`` etc. at the type level via
    Pydantic ``Literal``) and then re-checked against the toy-grid expected
    schema refs / mode / kind. Mismatches raise
    ``SimulatorAdapterContractError`` so the runtime fails closed instead of
    silently routing through an unexpected adapter.
    """

    if contract is None:
        return build_toy_grid_world_simulator_adapter_contract()
    validated = validate_simulator_adapter_safety_compatibility(contract)

    if validated.simulator_kind != TOY_GRID_WORLD_SIMULATOR_KIND:
        raise SimulatorAdapterContractError(
            "simulator_adapter_contract.simulator_kind must be "
            f"{TOY_GRID_WORLD_SIMULATOR_KIND!r}, got {validated.simulator_kind!r}"
        )
    if validated.adapter_mode is not SimulatorAdapterMode.DRY_RUN_ONLY:
        raise SimulatorAdapterContractError(
            "simulator_adapter_contract.adapter_mode must be "
            f"{SimulatorAdapterMode.DRY_RUN_ONLY.value!r}, got "
            f"{validated.adapter_mode.value!r}"
        )

    expected_refs = {
        "state_schema": TOY_GRID_WORLD_STATE_SCHEMA_VERSION,
        "action_schema": TOY_GRID_WORLD_ACTION_SCHEMA_VERSION,
        "telemetry_schema": TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION,
        "governor_schema": SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION,
        "episode_schema": TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION,
        "replay_trace_schema": TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION,
    }
    for field_name, expected_value in expected_refs.items():
        actual_value = getattr(validated, field_name)
        if actual_value != expected_value:
            raise SimulatorAdapterContractError(
                f"simulator_adapter_contract.{field_name} must be "
                f"{expected_value!r}, got {actual_value!r}"
            )
    return validated


def _autonomy_adapter_contract_metadata(
    contract: SimulatorAdapterContract,
) -> dict[str, Any]:
    return {
        "adapter_contract_id": contract.adapter_id,
        "adapter_contract_schema_version": contract.schema_version,
        "adapter_contract_simulator_kind": contract.simulator_kind,
        "adapter_contract_mode": contract.adapter_mode.value,
    }


def build_toy_grid_world_simulator_adapter_contract() -> SimulatorAdapterContract:
    """Static contract describing the toy-grid-world simulator adapter.

    The contract is a pure declaration: it does not start, step, or otherwise
    interact with the simulator. It exists so any future component (mission
    runtime, gate aggregator, UI, second simulator) can read which schema
    versions this adapter speaks and which execution capabilities it does
    NOT support, in one place.
    """

    return SimulatorAdapterContract(
        adapter_id=TOY_GRID_WORLD_SIMULATOR_ADAPTER_ID,
        simulator_kind=TOY_GRID_WORLD_SIMULATOR_KIND,
        state_schema=TOY_GRID_WORLD_STATE_SCHEMA_VERSION,
        action_schema=TOY_GRID_WORLD_ACTION_SCHEMA_VERSION,
        telemetry_schema=TELEMETRY_HEALTH_SNAPSHOT_SCHEMA_VERSION,
        governor_schema=SAFETY_GOVERNOR_DECISION_SCHEMA_VERSION,
        episode_schema=TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION,
        replay_trace_schema=TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION,
        adapter_mode=SimulatorAdapterMode.DRY_RUN_ONLY,
    )


def build_toy_grid_world_autonomy_safety_regression_gate(
    episode: ToyGridWorldAutonomousEpisode | dict[str, Any],
    *,
    required_safety_suite_ids: Sequence[str] = _DEFAULT_SAFETY_REGRESSION_SUITES,
    safety_eval_results: list[dict[str, Any]] | None = None,
    simulator_adapter_contract: SimulatorAdapterContract | dict[str, Any] | None = None,
    hil_telemetry_reviews: Sequence[HilTelemetryReview | dict[str, Any]] = (),
    required_hil_telemetry_review: bool = False,
    subject_id: str | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToyGridWorldAutonomyGateResult:
    """Aggregate safety regression gate entry-point for a single autonomy episode.

    Builds the autonomy_scorecard.v1, autonomy_episode_review.v1 and runs the
    required safety eval suites against the episode's replay trace, then defers
    to ``build_toy_grid_world_autonomy_gate_result`` for the autonomy_gate_result.v1
    aggregation. The gate blocks when any required safety suite is absent from
    the input (``required_safety_suite_missing:<suite_id>``), distinct from the
    existing ``safety_eval_failed:<suite_id>`` reason which fires when a present
    suite reports ``passed=False``.

    The optional ``simulator_adapter_contract`` is validated via
    ``validate_toy_grid_world_simulator_adapter_contract`` before the gate is
    aggregated. Mismatches raise ``SimulatorAdapterContractError`` (fail
    closed). When unset, the canonical toy-grid contract is used and the
    resulting adapter_id is recorded in gate metadata.

    This entry-point is artifact-only. It does not approve, promote, reuse, or
    permit live / physical / stronger execution.
    """

    adapter_contract = validate_toy_grid_world_simulator_adapter_contract(
        simulator_adapter_contract
    )
    current_time = now or datetime.now(timezone.utc)
    if isinstance(episode, ToyGridWorldAutonomousEpisode):
        replay_trace_payload = episode.replay_trace.model_dump(mode="json")
    else:
        episode_dict = episode if isinstance(episode, dict) else {}
        replay_trace_payload = _payload(episode_dict.get("replay_trace"))
    scorecard = build_toy_grid_world_autonomy_scorecard(
        episode,
        now=current_time,
    )
    review = build_toy_grid_world_autonomy_episode_review(
        episode,
        autonomy_scorecard=scorecard,
        now=current_time,
    )
    required_ids = tuple(
        str(item or "").strip() for item in required_safety_suite_ids if str(item or "").strip()
    )
    if safety_eval_results is None:
        replay_artifacts = {
            "toy_grid_world_replay_trace": replay_trace_payload,
        }
        eval_subject_id = subject_id or scorecard.episode_id or scorecard.scorecard_id or "toy-grid-autonomy"
        computed_results: list[dict[str, Any]] = []
        for suite_id in required_ids:
            result = run_mission_eval_suite(
                suite_id,
                replay_artifacts,
                subject_id=eval_subject_id,
                created_at=current_time,
            )
            computed_results.append(result.model_dump(mode="json"))
        eval_inputs = computed_results
    else:
        eval_inputs = list(safety_eval_results)
    return build_toy_grid_world_autonomy_gate_result(
        scorecard,
        autonomy_episode_review=review,
        safety_eval_results=eval_inputs,
        required_safety_suite_ids=required_ids,
        hil_telemetry_reviews=hil_telemetry_reviews,
        required_hil_telemetry_review=required_hil_telemetry_review,
        subject_id=subject_id,
        now=current_time,
        metadata={
            **(metadata or {}),
            "entry_point": "safety_regression_gate",
            "default_required_safety_suite_ids": list(required_ids),
            **_autonomy_adapter_contract_metadata(adapter_contract),
        },
    )


_AUTONOMY_GATE_COMPARISON_SAFETY_METRICS_LOWER_IS_BETTER: tuple[str, ...] = (
    "safety_violation_count",
    "live_execution_flag_count",
    "physical_execution_flag_count",
    "telemetry_missing_count",
    "telemetry_stale_count",
    "telemetry_mismatch_count",
    "blocked_step_count",
)
_AUTONOMY_GATE_COMPARISON_SAFETY_METRICS_HIGHER_IS_BETTER: tuple[str, ...] = (
    "dry_run_compliance_rate",
)
_AUTONOMY_GATE_COMPARISON_QUALITY_METRICS_HIGHER_IS_BETTER: tuple[str, ...] = (
    "path_efficiency",
)
_AUTONOMY_GATE_COMPARISON_QUALITY_METRICS_LOWER_IS_BETTER: tuple[str, ...] = (
    "recovery_attempt_count",
    "replan_count",
)
_AUTONOMY_GATE_COMPARISON_SAFETY_METRIC_DEFAULTS: dict[str, float] = {
    "dry_run_compliance_rate": 1.0,
}
_AUTONOMY_GATE_COMPARISON_QUALITY_METRIC_DEFAULTS: dict[str, float] = {
    "path_efficiency": 1.0,
}


def _comparison_metric_value(
    scorecard: dict[str, Any], metric_name: str, defaults: dict[str, float]
) -> float:
    raw = scorecard.get(metric_name)
    if raw is None:
        return float(defaults.get(metric_name, 0.0))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(defaults.get(metric_name, 0.0))


def _gate_snapshot(
    gate: ToyGridWorldAutonomyGateResult | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(gate, ToyGridWorldAutonomyGateResult):
        return gate.model_dump(mode="json")
    return _payload(gate)


def compare_toy_grid_world_autonomy_gate_results(
    baseline: ToyGridWorldAutonomyGateResult | dict[str, Any],
    candidate: ToyGridWorldAutonomyGateResult | dict[str, Any],
    *,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToyGridWorldAutonomyGateComparisonResult:
    """Rule-based, deterministic comparison of two autonomy_gate_result.v1 artifacts.

    Emits autonomy_gate_comparison_result.v1. Blocks the comparison gate when
    any of the following holds:

    - candidate gate is blocked (``candidate_gate_blocked``)
    - baseline passed but candidate failed (``regression_from_passing_baseline``)
    - any safety metric regressed beyond its baseline (``metric_regressed:<name>``)

    Quality-only regressions (path efficiency, replans, recovery attempts) are
    surfaced as ``quality_metric_regressed:<name>`` in ``warning_reasons`` and
    do not block the gate. Determinism: identical inputs produce identical
    ``comparison_id`` / reasons / metric_deltas, so the result is suitable for
    CI regression diffing.

    No LLM judge. No promotion. No runtime reuse. No live, physical, or
    stronger execution. Operator approval is always required even when passed.
    """

    current_time = now or datetime.now(timezone.utc)
    baseline_snapshot = _gate_snapshot(baseline)
    candidate_snapshot = _gate_snapshot(candidate)
    baseline_scorecard = _payload(baseline_snapshot.get("scorecard_snapshot"))
    candidate_scorecard = _payload(candidate_snapshot.get("scorecard_snapshot"))

    blocked: list[str] = []
    warnings: list[str] = []
    metric_deltas: dict[str, ToyGridWorldAutonomyGateMetricDelta] = {}

    baseline_passed = bool(baseline_snapshot.get("passed"))
    candidate_passed = bool(candidate_snapshot.get("passed"))

    if not candidate_passed:
        blocked.append("candidate_gate_blocked")
    if baseline_passed and not candidate_passed:
        blocked.append("regression_from_passing_baseline")

    def _record(
        metric_name: str,
        defaults: dict[str, float],
        direction: ToyGridWorldAutonomyGateMetricDirection,
        regressed_severity: ToyGridWorldAutonomyGateMetricSeverity,
    ) -> tuple[float, float, bool]:
        baseline_value = _comparison_metric_value(
            baseline_scorecard, metric_name, defaults
        )
        candidate_value = _comparison_metric_value(
            candidate_scorecard, metric_name, defaults
        )
        delta_value = round(candidate_value - baseline_value, 6)
        if direction is ToyGridWorldAutonomyGateMetricDirection.LOWER_IS_BETTER:
            regressed = candidate_value > baseline_value
        else:
            regressed = candidate_value < baseline_value
        observed_severity = (
            regressed_severity
            if regressed
            else ToyGridWorldAutonomyGateMetricSeverity.INFO
        )
        metric_deltas[metric_name] = ToyGridWorldAutonomyGateMetricDelta(
            baseline=baseline_value,
            candidate=candidate_value,
            delta=delta_value,
            direction=direction,
            severity=observed_severity,
        )
        return baseline_value, candidate_value, regressed

    for metric in _AUTONOMY_GATE_COMPARISON_SAFETY_METRICS_LOWER_IS_BETTER:
        _b, _c, regressed = _record(
            metric,
            _AUTONOMY_GATE_COMPARISON_SAFETY_METRIC_DEFAULTS,
            ToyGridWorldAutonomyGateMetricDirection.LOWER_IS_BETTER,
            ToyGridWorldAutonomyGateMetricSeverity.BLOCKING,
        )
        if regressed:
            blocked.append(f"metric_regressed:{metric}")
    for metric in _AUTONOMY_GATE_COMPARISON_SAFETY_METRICS_HIGHER_IS_BETTER:
        _b, _c, regressed = _record(
            metric,
            _AUTONOMY_GATE_COMPARISON_SAFETY_METRIC_DEFAULTS,
            ToyGridWorldAutonomyGateMetricDirection.HIGHER_IS_BETTER,
            ToyGridWorldAutonomyGateMetricSeverity.BLOCKING,
        )
        if regressed:
            blocked.append(f"metric_regressed:{metric}")
    for metric in _AUTONOMY_GATE_COMPARISON_QUALITY_METRICS_HIGHER_IS_BETTER:
        _b, _c, regressed = _record(
            metric,
            _AUTONOMY_GATE_COMPARISON_QUALITY_METRIC_DEFAULTS,
            ToyGridWorldAutonomyGateMetricDirection.HIGHER_IS_BETTER,
            ToyGridWorldAutonomyGateMetricSeverity.WARNING,
        )
        if regressed:
            warnings.append(f"quality_metric_regressed:{metric}")
    for metric in _AUTONOMY_GATE_COMPARISON_QUALITY_METRICS_LOWER_IS_BETTER:
        _b, _c, regressed = _record(
            metric,
            _AUTONOMY_GATE_COMPARISON_QUALITY_METRIC_DEFAULTS,
            ToyGridWorldAutonomyGateMetricDirection.LOWER_IS_BETTER,
            ToyGridWorldAutonomyGateMetricSeverity.WARNING,
        )
        if regressed:
            warnings.append(f"quality_metric_regressed:{metric}")

    blocked_sorted = sorted(set(blocked))
    warnings_sorted = sorted(set(warnings))
    passed = not blocked_sorted

    comparison_id = _stable_id(
        "toy_grid_autonomy_gate_comparison",
        {
            "baseline_gate_id": baseline_snapshot.get("gate_id"),
            "candidate_gate_id": candidate_snapshot.get("gate_id"),
            "blocked_reasons": blocked_sorted,
            "warning_reasons": warnings_sorted,
        },
    )

    return ToyGridWorldAutonomyGateComparisonResult(
        comparison_id=comparison_id,
        baseline_gate_id=str(baseline_snapshot.get("gate_id") or ""),
        baseline_subject_id=str(baseline_snapshot.get("subject_id") or ""),
        candidate_gate_id=str(candidate_snapshot.get("gate_id") or ""),
        candidate_subject_id=str(candidate_snapshot.get("subject_id") or ""),
        passed=passed,
        status=(
            ToyGridWorldAutonomyGateComparisonStatus.PASSED
            if passed
            else ToyGridWorldAutonomyGateComparisonStatus.BLOCKED
        ),
        blocked_reasons=blocked_sorted,
        warning_reasons=warnings_sorted,
        metric_deltas=metric_deltas,
        baseline_snapshot=baseline_snapshot,
        candidate_snapshot=candidate_snapshot,
        created_at=current_time,
        metadata={
            **(metadata or {}),
            "simulator": "toy_grid_world",
            "artifact_only": True,
            "rule_based": True,
            "llm_judge_used": False,
            "promotion_created": False,
            "runtime_reuse_created": False,
            "stronger_execution_allowed": False,
            "live_execution_allowed": False,
            "physical_execution_invoked": False,
        },
    )


def build_toy_grid_world_autonomy_review_artifacts(
    episode: ToyGridWorldAutonomousEpisode | dict[str, Any],
    *,
    safety_eval_results: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or datetime.now(timezone.utc)
    scorecard = build_toy_grid_world_autonomy_scorecard(episode, now=current_time)
    review = build_toy_grid_world_autonomy_episode_review(
        episode,
        autonomy_scorecard=scorecard,
        now=current_time,
    )
    gate = build_toy_grid_world_autonomy_gate_result(
        scorecard,
        autonomy_episode_review=review,
        safety_eval_results=safety_eval_results,
        now=current_time,
    )
    return {
        "autonomy_scorecard": scorecard.model_dump(mode="json"),
        "autonomy_episode_review": review.model_dump(mode="json"),
        "autonomy_gate_result": gate.model_dump(mode="json"),
    }


def run_toy_grid_world_autonomous_episode(
    initial_state: ToyGridWorldState | dict[str, Any],
    autonomy_plan: ToyGridWorldAutonomyPlan | dict[str, Any],
    *,
    mission_contract: MissionContract | dict[str, Any] | None = None,
    max_steps: int | None = None,
    telemetry_sequence: list[TelemetryHealthSnapshot | dict[str, Any] | None] | None = None,
    simulator_adapter_contract: SimulatorAdapterContract | dict[str, Any] | None = None,
    now: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToyGridWorldAutonomousEpisode:
    """Run a bounded autonomy plan inside the toy simulator only.

    This consumes a plan and steps the local grid-world through the same
    dry-run safety governor used by replay. It never enables live execution,
    physical dispatch, ROS, or actuator control.

    The optional ``simulator_adapter_contract`` is validated via
    ``validate_toy_grid_world_simulator_adapter_contract`` before any step
    is attempted. Mismatching simulator_kind / adapter_mode / schema refs,
    or a contract that advertises stronger execution capabilities, raise
    ``SimulatorAdapterContractError`` and the episode is not run (fail
    closed). When unset, the canonical toy-grid contract is used.
    """

    adapter_contract = validate_toy_grid_world_simulator_adapter_contract(
        simulator_adapter_contract
    )

    current = (
        initial_state
        if isinstance(initial_state, ToyGridWorldState)
        else ToyGridWorldState.model_validate(initial_state)
    )
    plan = (
        autonomy_plan
        if isinstance(autonomy_plan, ToyGridWorldAutonomyPlan)
        else ToyGridWorldAutonomyPlan.model_validate(autonomy_plan)
    )
    current_time = now or datetime.now(timezone.utc)
    contract_id = _mission_contract_id(mission_contract)
    telemetry_items = telemetry_sequence or []
    steps: list[ToyGridWorldAutonomousStep] = []
    executed_actions: list[ToyGridWorldAction] = []
    final_state = current
    status = ToyGridWorldAutonomousEpisodeStatus.MAX_STEPS_EXHAUSTED
    stop_reason = "max_steps_exhausted"

    if plan.status == ToyGridWorldAutonomyPlanStatus.BLOCKED:
        status = ToyGridWorldAutonomousEpisodeStatus.PLAN_BLOCKED
        stop_reason = plan.failure_reason or "plan_blocked"
    elif not _state_matches_plan(current, plan):
        status = ToyGridWorldAutonomousEpisodeStatus.PLAN_MISMATCH
        stop_reason = "plan_initial_state_mismatch"
    else:
        resolved_max_steps = len(plan.actions)
        if max_steps is not None:
            resolved_max_steps = min(resolved_max_steps, max(0, int(max_steps)))
        resolved_max_steps = min(resolved_max_steps, max(0, int(plan.max_step_budget)))
        if not plan.actions and current.status == ToyGridWorldStatus.GOAL_REACHED:
            status = ToyGridWorldAutonomousEpisodeStatus.GOAL_REACHED
            stop_reason = "goal_reached"
        for index, action in enumerate(plan.actions[:resolved_max_steps]):
            telemetry = telemetry_items[index] if index < len(telemetry_items) else _AUTO_TELEMETRY
            result = step_toy_grid_world(
                final_state,
                action,
                telemetry=telemetry,
                now=current_time + timedelta(seconds=index),
            )
            autonomous_step = _autonomous_step_from_result(result, step_index=index)
            steps.append(autonomous_step)
            executed_actions.append(action)
            final_state = result.next_state
            if not result.accepted:
                status = ToyGridWorldAutonomousEpisodeStatus.BLOCKED
                stop_reason = result.blocked_reason or "safety_governor_blocked"
                break
            if final_state.status == ToyGridWorldStatus.GOAL_REACHED:
                status = ToyGridWorldAutonomousEpisodeStatus.GOAL_REACHED
                stop_reason = "goal_reached"
                break
        else:
            if len(steps) < len(plan.actions):
                status = ToyGridWorldAutonomousEpisodeStatus.MAX_STEPS_EXHAUSTED
                stop_reason = "max_steps_exhausted"
            elif final_state.status == ToyGridWorldStatus.GOAL_REACHED:
                status = ToyGridWorldAutonomousEpisodeStatus.GOAL_REACHED
                stop_reason = "goal_reached"
            else:
                status = ToyGridWorldAutonomousEpisodeStatus.MAX_STEPS_EXHAUSTED
                stop_reason = "plan_actions_exhausted_before_goal"

    step_results = [step.step_result for step in steps]
    replay_trace = _build_replay_trace_from_steps(
        current,
        executed_actions,
        step_results,
        final_state,
        now=current_time,
        metadata={
            "autonomous_episode": True,
            "plan_id": plan.plan_id,
            "mission_contract_id": contract_id,
        },
    )
    summary = _episode_summary(
        status=status,
        steps=steps,
        final_state=final_state,
        replay_trace=replay_trace,
        stop_reason=stop_reason,
    )
    episode_id = _episode_id(
        initial_state=current,
        plan=plan,
        status=status,
        steps=steps,
        final_state=final_state,
        mission_contract_id=contract_id,
    )
    return ToyGridWorldAutonomousEpisode(
        episode_id=episode_id,
        world_id=current.world_id,
        plan_id=plan.plan_id,
        mission_contract_id=contract_id,
        status=status,
        initial_state=current,
        autonomy_plan=plan,
        steps=steps,
        final_state=final_state,
        final_status=final_state.status,
        replay_trace=replay_trace,
        summary=summary,
        created_at=current_time,
        metadata={
            **(metadata or {}),
            "simulator": "toy_grid_world",
            "artifact_only": True,
            "simulator_only": True,
            "dry_run_only": True,
            "execution_allowed": False,
            "operator_approval_required": True,
            "operator_approval_performed": False,
            "live_execution_allowed": False,
            "physical_execution_invoked": False,
            **_autonomy_adapter_contract_metadata(adapter_contract),
        },
    )


def build_grid_world_simulation_scenario_request(
    state: ToyGridWorldState | dict[str, Any],
    *,
    action: ToyGridWorldAction | str | None = None,
    now: datetime | None = None,
) -> SimulationScenarioRequest:
    current = (
        state if isinstance(state, ToyGridWorldState) else ToyGridWorldState.model_validate(state)
    )
    selected_action = _action(action) if action is not None else ToyGridWorldAction.WAIT
    contract = build_mission_contract(
        contract_id=f"toy-grid-world-{current.world_id}",
        objective=(
            "Navigate the toy grid-world agent to the goal without entering "
            "obstacles, hazards, or low-battery unsafe states."
        ),
        allowed_actions=[item.value for item in ToyGridWorldAction],
        forbidden_actions=[
            "enter_obstacle",
            "enter_hazard",
            "leave_grid",
            "continue_with_low_battery",
            "live_actuator_execution",
            "direct_motor_control",
        ],
        completion_criteria=["agent_position_equals_goal"],
        evidence_requirements=[
            "grid_position",
            "path_trace",
            "telemetry_health_snapshot",
            "safety_governor_decision",
        ],
        metadata={"simulator": "toy_grid_world", "world_id": current.world_id},
    )
    return build_simulation_scenario_request(
        mission_contract=contract,
        trajectory={
            "id": f"{current.world_id}:{current.step_count}:{selected_action.value}",
            "action": selected_action.value,
            "status": current.status.value,
            "actions": [
                {
                    "type": "toy_grid_world_action",
                    "action": selected_action.value,
                    "agent_position": current.agent_position.model_dump(mode="json"),
                    "goal_position": current.goal_position.model_dump(mode="json"),
                }
            ],
        },
        metadata={
            "simulator": "toy_grid_world",
            "world_id": current.world_id,
            "step_count": current.step_count,
        },
        now=now,
    )


def build_grid_world_telemetry_snapshot(
    state: ToyGridWorldState | dict[str, Any],
    *,
    scenario_id: str = "",
    observed_at: datetime | None = None,
    now: datetime | None = None,
) -> TelemetryHealthSnapshot:
    current = (
        state if isinstance(state, ToyGridWorldState) else ToyGridWorldState.model_validate(state)
    )
    current_time = now or datetime.now(timezone.utc)
    unsafe = current.status == ToyGridWorldStatus.BLOCKED
    low_battery = current.battery <= current.low_battery_threshold
    telemetry = {
        "observed_at": (observed_at or current_time).isoformat(),
        "signals": {
            "battery": "critical" if low_battery else "ok",
            "localization": "ok",
            "comms": "ok",
            "safety": "unsafe" if unsafe else "nominal",
        },
        "source_refs": _grid_world_source_refs(current),
    }
    snapshot = build_telemetry_health_snapshot(
        telemetry,
        scenario_id=scenario_id,
        now=current_time,
    )
    metadata = {
        **snapshot.metadata,
        "simulator": "toy_grid_world",
        "world_id": current.world_id,
        "position": current.agent_position.model_dump(mode="json"),
        "goal_position": current.goal_position.model_dump(mode="json"),
        "battery": current.battery,
        "step_count": current.step_count,
    }
    return snapshot.model_copy(update={"metadata": metadata})


def _action_block_reason(
    state: ToyGridWorldState,
    action: ToyGridWorldAction,
) -> str:
    if state.status != ToyGridWorldStatus.RUNNING:
        return "mission_not_running"
    if state.battery <= state.low_battery_threshold:
        return "low_battery"
    if state.step_count >= state.max_steps:
        return "max_steps_exhausted"
    proposed = _next_position(state.agent_position, action)
    if not _in_bounds(proposed, state.width, state.height):
        return "out_of_bounds"
    obstacle_keys = {_position_key(item) for item in state.obstacles}
    hazard_keys = {_position_key(item) for item in state.hazards}
    if _position_key(proposed) in obstacle_keys:
        return "obstacle"
    if _position_key(proposed) in hazard_keys:
        return "hazard"
    return ""


def build_grid_world_safety_governor_decision(
    state: ToyGridWorldState | dict[str, Any],
    action: ToyGridWorldAction | str,
    scenario_request: SimulationScenarioRequest | dict[str, Any],
    telemetry_snapshot: TelemetryHealthSnapshot | dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> SafetyGovernorDecisionArtifact:
    current = (
        state if isinstance(state, ToyGridWorldState) else ToyGridWorldState.model_validate(state)
    )
    selected_action = _action(action)
    scenario = (
        scenario_request
        if isinstance(scenario_request, SimulationScenarioRequest)
        else SimulationScenarioRequest.model_validate(scenario_request)
    )
    if telemetry_snapshot is None:
        return build_safety_governor_decision_artifact(
            scenario,
            None,
            now=now,
        )
    telemetry = (
        telemetry_snapshot
        if isinstance(telemetry_snapshot, TelemetryHealthSnapshot)
        else TelemetryHealthSnapshot.model_validate(telemetry_snapshot)
    )
    if telemetry.scenario_id != scenario.scenario_id:
        reasons = [*telemetry.reasons, "telemetry_scenario_mismatch"]
        return SafetyGovernorDecisionArtifact(
            decision_id=_stable_id(
                "toy_grid_governor",
                {
                    "world_id": current.world_id,
                    "step": current.step_count,
                    "action": selected_action.value,
                    "scenario_id": scenario.scenario_id,
                    "telemetry_scenario_id": telemetry.scenario_id,
                    "reason": "telemetry_scenario_mismatch",
                    "telemetry": telemetry.snapshot_id,
                },
            ),
            scenario_id=scenario.scenario_id,
            decision=SafetyGovernorStatus.BLOCKED,
            reasons=reasons,
            telemetry_snapshot_id=telemetry.snapshot_id,
            checked_at=now or datetime.now(timezone.utc),
            source_refs=sorted(set(scenario.source_refs + telemetry.source_refs)),
            metadata={
                "simulator": "toy_grid_world",
                "world_id": current.world_id,
                "action": selected_action.value,
                "blocked_reason": "telemetry_scenario_mismatch",
                "telemetry_scenario_id": telemetry.scenario_id,
                "expected_scenario_id": scenario.scenario_id,
                "physical_execution_allowed": False,
            },
        )
    base_decision = build_safety_governor_decision_artifact(
        scenario,
        telemetry,
        now=now,
    )
    if base_decision.decision == SafetyGovernorStatus.BLOCKED:
        return base_decision

    block_reason = _action_block_reason(current, selected_action)
    if not block_reason:
        return base_decision

    reasons = [*telemetry.reasons, f"blocked_by_{block_reason}"]
    return SafetyGovernorDecisionArtifact(
        decision_id=_stable_id(
            "toy_grid_governor",
            {
                "world_id": current.world_id,
                "step": current.step_count,
                "action": selected_action.value,
                "reason": block_reason,
                "telemetry": telemetry.snapshot_id,
            },
        ),
        scenario_id=scenario.scenario_id,
        decision=SafetyGovernorStatus.BLOCKED,
        reasons=reasons,
        telemetry_snapshot_id=telemetry.snapshot_id,
        checked_at=now or datetime.now(timezone.utc),
        source_refs=sorted(set(scenario.source_refs + telemetry.source_refs)),
        metadata={
            "simulator": "toy_grid_world",
            "world_id": current.world_id,
            "action": selected_action.value,
            "blocked_reason": block_reason,
            "physical_execution_allowed": False,
        },
    )


def _apply_grid_world_action(
    state: ToyGridWorldState,
    action: ToyGridWorldAction,
) -> ToyGridWorldState:
    proposed = _next_position(state.agent_position, action)
    battery = max(0, state.battery - 1)
    status = (
        ToyGridWorldStatus.GOAL_REACHED
        if _position_key(proposed) == _position_key(state.goal_position)
        else ToyGridWorldStatus.RUNNING
    )
    return state.model_copy(
        update={
            "agent_position": proposed,
            "battery": battery,
            "step_count": state.step_count + 1,
            "status": status,
            "last_block_reason": "",
            "path_trace": [*state.path_trace, proposed],
        }
    )


def _blocked_state(
    state: ToyGridWorldState,
    reason: str,
) -> ToyGridWorldState:
    return state.model_copy(
        update={
            "status": ToyGridWorldStatus.BLOCKED,
            "last_block_reason": reason,
            "path_trace": list(state.path_trace),
        }
    )


def _blocked_reason_from_decision(decision: SafetyGovernorDecisionArtifact) -> str:
    for reason in decision.reasons:
        if reason.startswith("blocked_by_"):
            return reason.removeprefix("blocked_by_")
    return "safety_governor_blocked"


def step_toy_grid_world(
    state: ToyGridWorldState | dict[str, Any],
    action: ToyGridWorldAction | str,
    *,
    telemetry: TelemetryHealthSnapshot | dict[str, Any] | None | object = _AUTO_TELEMETRY,
    now: datetime | None = None,
) -> ToyGridWorldStepResult:
    current = (
        state if isinstance(state, ToyGridWorldState) else ToyGridWorldState.model_validate(state)
    )
    selected_action = _action(action)
    current_time = now or datetime.now(timezone.utc)
    scenario = build_grid_world_simulation_scenario_request(
        current,
        action=selected_action,
        now=current_time,
    )
    if telemetry is _AUTO_TELEMETRY:
        telemetry_snapshot = build_grid_world_telemetry_snapshot(
            current,
            scenario_id=scenario.scenario_id,
            now=current_time,
        )
    elif telemetry is None:
        telemetry_snapshot = build_telemetry_health_snapshot(
            None,
            scenario_id=scenario.scenario_id,
            now=current_time,
        )
    elif isinstance(telemetry, TelemetryHealthSnapshot):
        telemetry_snapshot = telemetry
    else:
        telemetry_snapshot = build_telemetry_health_snapshot(
            telemetry,
            scenario_id=scenario.scenario_id,
            now=current_time,
        )
    governor = build_grid_world_safety_governor_decision(
        current,
        selected_action,
        scenario,
        telemetry_snapshot,
        now=current_time,
    )
    if governor.decision == SafetyGovernorStatus.BLOCKED:
        reason = _blocked_reason_from_decision(governor)
        return ToyGridWorldStepResult(
            action=selected_action,
            accepted=False,
            blocked_reason=reason,
            previous_state=current,
            next_state=_blocked_state(current, reason),
            telemetry_health_snapshot=telemetry_snapshot,
            safety_governor_decision=governor,
            created_at=current_time,
            metadata={"simulator": "toy_grid_world", "operator_approval_required": True},
        )

    proposed = _next_position(current.agent_position, selected_action)
    envelope = build_dry_run_action_envelope(
        scenario,
        governor,
        proposed_actions=[
            {
                "type": "toy_grid_world_action",
                "action": selected_action.value,
                "from": current.agent_position.model_dump(mode="json"),
                "to": proposed.model_dump(mode="json"),
                "dry_run": True,
            }
        ],
        now=current_time,
    )
    next_state = _apply_grid_world_action(current, selected_action)
    replay_plan = build_offline_replay_plan(
        scenario,
        telemetry_snapshot,
        governor,
        envelope,
        now=current_time,
    )
    return ToyGridWorldStepResult(
        action=selected_action,
        accepted=True,
        previous_state=current,
        next_state=next_state,
        telemetry_health_snapshot=telemetry_snapshot,
        safety_governor_decision=governor,
        dry_run_action_envelope=envelope,
        offline_replay_plan=replay_plan,
        created_at=current_time,
        metadata={
            "simulator": "toy_grid_world",
            "operator_approval_required": True,
            "physical_execution_allowed": False,
        },
    )


def run_toy_grid_world_replay(
    initial_state: ToyGridWorldState | dict[str, Any],
    actions: list[ToyGridWorldAction | str],
    *,
    now: datetime | None = None,
) -> ToyGridWorldReplayTrace:
    current = (
        initial_state
        if isinstance(initial_state, ToyGridWorldState)
        else ToyGridWorldState.model_validate(initial_state)
    )
    current_time = now or datetime.now(timezone.utc)
    selected_actions = [_action(item) for item in actions]
    steps: list[ToyGridWorldStepResult] = []
    offline_ref = ""
    for index, action in enumerate(selected_actions):
        step = step_toy_grid_world(
            current,
            action,
            now=current_time + timedelta(seconds=index),
        )
        steps.append(step)
        if step.offline_replay_plan is not None:
            offline_ref = f"offline_replay_plan:{step.offline_replay_plan.replay_plan_id}"
        current = step.next_state
        if current.status in {ToyGridWorldStatus.BLOCKED, ToyGridWorldStatus.GOAL_REACHED}:
            break

    resolved_initial = (
        initial_state
        if isinstance(initial_state, ToyGridWorldState)
        else ToyGridWorldState.model_validate(initial_state)
    )
    deterministic_hash = _deterministic_replay_hash(
        resolved_initial,
        selected_actions,
        steps,
        current,
    )
    return ToyGridWorldReplayTrace(
        trace_id=_replay_trace_id(current, selected_actions, deterministic_hash),
        initial_state=resolved_initial,
        actions=selected_actions,
        steps=steps,
        final_state=current,
        final_status=current.status,
        deterministic_hash=deterministic_hash,
        offline_replay_plan_ref=offline_ref,
        created_at=current_time,
        metadata={
            "simulator": "toy_grid_world",
            "artifact_only": True,
            "operator_approval_required": True,
        },
    )


def render_toy_grid_world_svg(
    state: ToyGridWorldState | dict[str, Any],
    *,
    tile_size: int = 32,
) -> str:
    """Render an original retro top-down SVG view of the grid world.

    The renderer intentionally uses generated geometric tiles and no third-party
    game assets or franchise-specific characters.
    """

    current = (
        state if isinstance(state, ToyGridWorldState) else ToyGridWorldState.model_validate(state)
    )
    size = max(16, int(tile_size))
    width_px = current.width * size
    height_px = current.height * size
    obstacle_keys = {_position_key(item) for item in current.obstacles}
    hazard_keys = {_position_key(item) for item in current.hazards}
    path_keys = {_position_key(item) for item in current.path_trace}
    agent_key = _position_key(current.agent_position)
    goal_key = _position_key(current.goal_position)
    cells: list[str] = []
    for y in range(current.height):
        for x in range(current.width):
            key = (x, y)
            fill = "#8fd16a"
            accent = "#a9df84"
            label = ""
            if key in path_keys:
                fill = "#b7dd7a"
            if key == goal_key:
                fill = "#f6d365"
                accent = "#fda085"
                label = "G"
            if key in hazard_keys:
                fill = "#b85c6b"
                accent = "#e88873"
                label = "!"
            if key in obstacle_keys:
                fill = "#52606d"
                accent = "#36454f"
                label = ""
            px = x * size
            py = y * size
            cells.append(
                f'<rect x="{px}" y="{py}" width="{size}" height="{size}" '
                f'fill="{fill}" stroke="#29422a" stroke-width="1"/>'
            )
            cells.append(
                f'<rect x="{px + 4}" y="{py + 4}" width="{max(2, size - 8)}" '
                f'height="{max(2, size - 8)}" fill="{accent}" opacity="0.28"/>'
            )
            if label:
                cells.append(
                    f'<text x="{px + size / 2:.1f}" y="{py + size * 0.66:.1f}" '
                    'font-family="monospace" font-size="16" text-anchor="middle" '
                    'fill="#263238">'
                    f"{escape(label)}</text>"
                )
    ax, ay = agent_key
    agent_x = ax * size
    agent_y = ay * size
    cells.append(
        f'<rect x="{agent_x + 8}" y="{agent_y + 8}" width="{size - 16}" '
        f'height="{size - 16}" fill="#2f80ed" stroke="#12355b" stroke-width="2"/>'
    )
    cells.append(
        f'<rect x="{agent_x + size / 2 - 4:.1f}" y="{agent_y + 4}" width="8" '
        'height="6" fill="#f5f5f5" stroke="#12355b" stroke-width="1"/>'
    )
    title = escape(f"Toy grid world {current.world_id}")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_px}" height="{height_px}" '
        f'viewBox="0 0 {width_px} {height_px}" role="img" aria-label="{title}" '
        'shape-rendering="crispEdges">'
        '<rect width="100%" height="100%" fill="#223322"/>'
        f"{''.join(cells)}"
        "</svg>"
    )


__all__ = [
    "TOY_GRID_WORLD_ACTION_SCHEMA_VERSION",
    "TOY_GRID_WORLD_AUTONOMOUS_EPISODE_SCHEMA_VERSION",
    "TOY_GRID_WORLD_AUTONOMOUS_STEP_SCHEMA_VERSION",
    "TOY_GRID_WORLD_AUTONOMY_EPISODE_REVIEW_SCHEMA_VERSION",
    "TOY_GRID_WORLD_AUTONOMY_GATE_COMPARISON_RESULT_SCHEMA_VERSION",
    "TOY_GRID_WORLD_AUTONOMY_GATE_RESULT_SCHEMA_VERSION",
    "TOY_GRID_WORLD_AUTONOMY_PLAN_SCHEMA_VERSION",
    "TOY_GRID_WORLD_AUTONOMY_SCORECARD_SCHEMA_VERSION",
    "TOY_GRID_WORLD_REPLAY_TRACE_SCHEMA_VERSION",
    "TOY_GRID_WORLD_STATE_SCHEMA_VERSION",
    "TOY_GRID_WORLD_STEP_RESULT_SCHEMA_VERSION",
    "ToyGridWorldAction",
    "ToyGridWorldAutonomousEpisode",
    "ToyGridWorldAutonomousEpisodeStatus",
    "ToyGridWorldAutonomousStep",
    "ToyGridWorldAutonomyEpisodeReview",
    "ToyGridWorldAutonomyGateComparisonResult",
    "ToyGridWorldAutonomyGateComparisonStatus",
    "ToyGridWorldAutonomyGateMetricDelta",
    "ToyGridWorldAutonomyGateMetricDirection",
    "ToyGridWorldAutonomyGateMetricSeverity",
    "ToyGridWorldAutonomyGateResult",
    "ToyGridWorldAutonomyGateStatus",
    "ToyGridWorldAutonomyPlan",
    "ToyGridWorldAutonomyPlanStatus",
    "ToyGridWorldAutonomyScorecard",
    "ToyGridWorldAutonomyScorecardStatus",
    "ToyGridWorldError",
    "ToyGridWorldPosition",
    "ToyGridWorldReplayTrace",
    "ToyGridWorldState",
    "ToyGridWorldStatus",
    "ToyGridWorldStepResult",
    "build_grid_world_safety_governor_decision",
    "build_grid_world_simulation_scenario_request",
    "build_grid_world_telemetry_snapshot",
    "build_toy_grid_world_autonomy_plan",
    "build_toy_grid_world_autonomy_episode_review",
    "build_toy_grid_world_autonomy_gate_result",
    "build_toy_grid_world_autonomy_review_artifacts",
    "build_toy_grid_world_autonomy_safety_regression_gate",
    "build_toy_grid_world_simulator_adapter_contract",
    "validate_toy_grid_world_simulator_adapter_contract",
    "compare_toy_grid_world_autonomy_gate_results",
    "build_toy_grid_world_autonomy_scorecard",
    "build_toy_grid_world_state",
    "render_toy_grid_world_svg",
    "run_toy_grid_world_autonomous_episode",
    "run_toy_grid_world_replay",
    "step_toy_grid_world",
]

"""Fleet-memory artifacts for PX4/Gazebo delivery mission feedback."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_delivery_mission_control import (
    DEFAULT_MISSION_PHASE_SEQUENCE,
    PX4GazeboDeliveryMissionReplayTimeline,
    PX4GazeboDeliveryMissionRunnerResult,
)

PX4_GAZEBO_DELIVERY_MISSION_TRAJECTORY_SUMMARY_SCHEMA_VERSION = (
    "px4_gazebo_delivery_mission_trajectory_summary.v1"
)
PX4_GAZEBO_ROUTE_SEGMENT_OBSERVATION_SCHEMA_VERSION = (
    "px4_gazebo_route_segment_observation.v1"
)
PX4_GAZEBO_ROUTE_SEGMENT_MEMORY_SCHEMA_VERSION = "px4_gazebo_route_segment_memory.v1"
PX4_GAZEBO_DELIVERY_ZONE_MEMORY_SCHEMA_VERSION = "px4_gazebo_delivery_zone_memory.v1"
PX4_GAZEBO_FLEET_MEMORY_SNAPSHOT_SCHEMA_VERSION = "px4_gazebo_fleet_memory_snapshot.v1"
PX4_GAZEBO_FLEET_FEEDBACK_CANDIDATE_SCHEMA_VERSION = (
    "px4_gazebo_fleet_feedback_candidate.v1"
)
PX4_GAZEBO_FLEET_FEEDBACK_PROMOTION_GATE_SCHEMA_VERSION = (
    "px4_gazebo_fleet_feedback_promotion_gate.v1"
)
PX4_GAZEBO_MEMORY_INFORMED_MISSION_PLAN_SCHEMA_VERSION = (
    "px4_gazebo_memory_informed_mission_plan.v1"
)
PX4_GAZEBO_LEAD_DRONE_OBSERVATION_SCHEMA_VERSION = (
    "px4_gazebo_lead_drone_observation.v1"
)
PX4_GAZEBO_FOLLOWUP_MISSION_FEEDBACK_SCHEMA_VERSION = (
    "px4_gazebo_followup_mission_feedback.v1"
)
PX4_GAZEBO_FLEET_LEARNING_REPLAY_SCHEMA_VERSION = "px4_gazebo_fleet_learning_replay.v1"
PX4_GAZEBO_FLEET_LEARNING_CORPUS_SCHEMA_VERSION = "px4_gazebo_fleet_learning_corpus.v1"
PX4_GAZEBO_FLEET_MEMORY_PART2_FINALIZATION_SCHEMA_VERSION = (
    "px4_gazebo_fleet_memory_part2_finalization.v1"
)


class PX4GazeboFleetMemoryError(RuntimeError):
    """Raised when fleet-memory evidence would become unsafe or inconsistent."""


class PX4GazeboFleetMemoryMissionOutcome(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


class PX4GazeboFleetFeedbackCandidateStatus(str, Enum):
    PROPOSED = "proposed"
    STALE_IGNORED = "stale_ignored"
    CONTRADICTORY_BLOCKED = "contradictory_blocked"
    OUTLIER_NOT_ADOPTED = "outlier_not_adopted"
    UNSAFE_REJECTED = "unsafe_rejected"


class PX4GazeboFleetFeedbackPromotionStatus(str, Enum):
    PROMOTED = "promoted"
    BLOCKED = "blocked"


class PX4GazeboFleetLearningReplayCaseStatus(str, Enum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class _FleetMemorySafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    fleet_memory_is_evidence_not_authority: Literal[True] = True
    memory_direct_command_authority_allowed: Literal[False] = False
    memory_grants_dispatch_authority: Literal[False] = False
    approval_free_dispatch_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False


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


def _runner_ref(runner: PX4GazeboDeliveryMissionRunnerResult) -> str:
    return f"px4_gazebo_delivery_mission_runner_v1_result:{runner.runner_result_id}"


def _replay_ref(replay: PX4GazeboDeliveryMissionReplayTimeline) -> str:
    return f"px4_gazebo_delivery_mission_replay_timeline:{replay.replay_timeline_id}"


def _summary_ref(summary: "PX4GazeboDeliveryMissionTrajectorySummary") -> str:
    return f"px4_gazebo_delivery_mission_trajectory_summary:{summary.summary_id}"


def _route_segment_memory_ref(memory: "PX4GazeboRouteSegmentMemory") -> str:
    return f"px4_gazebo_route_segment_memory:{memory.memory_id}"


def _delivery_zone_memory_ref(memory: "PX4GazeboDeliveryZoneMemory") -> str:
    return f"px4_gazebo_delivery_zone_memory:{memory.memory_id}"


def _fleet_snapshot_ref(snapshot: "PX4GazeboFleetMemorySnapshot") -> str:
    return f"px4_gazebo_fleet_memory_snapshot:{snapshot.snapshot_id}"


def _candidate_ref(candidate: "PX4GazeboFleetFeedbackCandidate") -> str:
    return f"px4_gazebo_fleet_feedback_candidate:{candidate.candidate_id}"


def _promotion_gate_ref(gate: "PX4GazeboFleetFeedbackPromotionGate") -> str:
    return f"px4_gazebo_fleet_feedback_promotion_gate:{gate.gate_id}"


def _memory_plan_ref(plan: "PX4GazeboMemoryInformedMissionPlan") -> str:
    return f"px4_gazebo_memory_informed_mission_plan:{plan.plan_id}"


def _lead_observation_ref(observation: "PX4GazeboLeadDroneObservation") -> str:
    return f"px4_gazebo_lead_drone_observation:{observation.observation_id}"


def _followup_feedback_ref(feedback: "PX4GazeboFollowupMissionFeedback") -> str:
    return f"px4_gazebo_followup_mission_feedback:{feedback.feedback_id}"


def _fleet_replay_ref(replay: "PX4GazeboFleetLearningReplay") -> str:
    return f"px4_gazebo_fleet_learning_replay:{replay.replay_id}"


def _fleet_corpus_ref(corpus: "PX4GazeboFleetLearningCorpus") -> str:
    return f"px4_gazebo_fleet_learning_corpus:{corpus.corpus_id}"


def _coerce_runner(
    value: PX4GazeboDeliveryMissionRunnerResult | Mapping[str, Any],
) -> PX4GazeboDeliveryMissionRunnerResult:
    if isinstance(value, PX4GazeboDeliveryMissionRunnerResult):
        return value
    return PX4GazeboDeliveryMissionRunnerResult.model_validate(dict(value))


def _coerce_replay(
    value: PX4GazeboDeliveryMissionReplayTimeline | Mapping[str, Any],
) -> PX4GazeboDeliveryMissionReplayTimeline:
    if isinstance(value, PX4GazeboDeliveryMissionReplayTimeline):
        return value
    return PX4GazeboDeliveryMissionReplayTimeline.model_validate(dict(value))


class PX4GazeboDeliveryMissionTrajectorySummary(_FleetMemorySafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_DELIVERY_MISSION_TRAJECTORY_SUMMARY_SCHEMA_VERSION
    ] = PX4_GAZEBO_DELIVERY_MISSION_TRAJECTORY_SUMMARY_SCHEMA_VERSION
    summary_id: str
    runner_result_ref: str = Field(min_length=1)
    replay_timeline_ref: str = Field(min_length=1)
    mission_outcome: PX4GazeboFleetMemoryMissionOutcome
    final_status: str = Field(min_length=1)
    observed_phases: tuple[str, ...]
    blocked_phase: str | None = None
    blocked_reasons: tuple[str, ...] = ()
    replay_event_count: int = Field(ge=1)
    route_segment_count: int = Field(ge=1)
    delivery_zone_refs: tuple[str, ...]
    compact_summary_only: Literal[True] = True
    raw_logs_included: Literal[False] = False
    sqlite_included: Literal[False] = False
    full_telemetry_included: Literal[False] = False
    generated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "observed_phases",
        "blocked_reasons",
        "delivery_zone_refs",
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
    def _validate_summary(self) -> "PX4GazeboDeliveryMissionTrajectorySummary":
        if self.mission_outcome == PX4GazeboFleetMemoryMissionOutcome.COMPLETED:
            if self.blocked_phase is not None or self.blocked_reasons:
                raise PX4GazeboFleetMemoryError(
                    "completed trajectory summary cannot include blocked evidence"
                )
            if tuple(self.observed_phases) != tuple(
                phase.value for phase in DEFAULT_MISSION_PHASE_SEQUENCE
            ):
                raise PX4GazeboFleetMemoryError(
                    "completed trajectory summary requires every mission phase"
                )
        if self.mission_outcome == PX4GazeboFleetMemoryMissionOutcome.BLOCKED:
            if self.blocked_phase is None or not self.blocked_reasons:
                raise PX4GazeboFleetMemoryError(
                    "blocked trajectory summary requires blocked phase and reasons"
                )
        return self


class PX4GazeboRouteSegmentObservation(_FleetMemorySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_SEGMENT_OBSERVATION_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_SEGMENT_OBSERVATION_SCHEMA_VERSION
    )
    observation_id: str
    trajectory_summary_ref: str = Field(min_length=1)
    segment_ref: str = Field(min_length=1)
    source_zone_ref: str = Field(min_length=1)
    target_zone_ref: str = Field(min_length=1)
    mission_outcome: PX4GazeboFleetMemoryMissionOutcome
    risk_labels: tuple[str, ...]
    observed_success: bool
    generated_at: datetime

    @field_validator("risk_labels", mode="before")
    @classmethod
    def _coerce_labels(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class PX4GazeboRouteSegmentMemory(_FleetMemorySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_ROUTE_SEGMENT_MEMORY_SCHEMA_VERSION] = (
        PX4_GAZEBO_ROUTE_SEGMENT_MEMORY_SCHEMA_VERSION
    )
    memory_id: str
    segment_ref: str = Field(min_length=1)
    observation_refs: tuple[str, ...]
    success_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    risk_labels: tuple[str, ...]
    planning_hint: str = Field(min_length=1)
    generated_at: datetime

    @field_validator("observation_refs", "risk_labels", mode="before")
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
    def _validate_memory(self) -> "PX4GazeboRouteSegmentMemory":
        if self.success_count + self.blocked_count != len(self.observation_refs):
            raise PX4GazeboFleetMemoryError(
                "route segment memory counts must match observations"
            )
        return self


class PX4GazeboDeliveryZoneMemory(_FleetMemorySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_DELIVERY_ZONE_MEMORY_SCHEMA_VERSION] = (
        PX4_GAZEBO_DELIVERY_ZONE_MEMORY_SCHEMA_VERSION
    )
    memory_id: str
    zone_ref: str = Field(min_length=1)
    trajectory_summary_refs: tuple[str, ...]
    success_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    gate_hint: str = Field(min_length=1)
    generated_at: datetime

    @field_validator("trajectory_summary_refs", mode="before")
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_zone(self) -> "PX4GazeboDeliveryZoneMemory":
        if self.success_count + self.blocked_count != len(self.trajectory_summary_refs):
            raise PX4GazeboFleetMemoryError(
                "delivery zone memory counts must match trajectory summaries"
            )
        return self


class PX4GazeboFleetMemorySnapshot(_FleetMemorySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_FLEET_MEMORY_SNAPSHOT_SCHEMA_VERSION] = (
        PX4_GAZEBO_FLEET_MEMORY_SNAPSHOT_SCHEMA_VERSION
    )
    snapshot_id: str
    trajectory_summary_refs: tuple[str, ...]
    route_segment_memory_refs: tuple[str, ...]
    delivery_zone_memory_refs: tuple[str, ...]
    read_only_at_execution_time: Literal[True] = True
    generated_at: datetime

    @field_validator(
        "trajectory_summary_refs",
        "route_segment_memory_refs",
        "delivery_zone_memory_refs",
        mode="before",
    )
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "PX4GazeboFleetMemorySnapshot":
        if (
            not self.trajectory_summary_refs
            or not self.route_segment_memory_refs
            or not self.delivery_zone_memory_refs
        ):
            raise PX4GazeboFleetMemoryError(
                "fleet memory snapshot requires trajectory, segment, and zone memory refs"
            )
        return self


class PX4GazeboFleetFeedbackCandidate(_FleetMemorySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_FLEET_FEEDBACK_CANDIDATE_SCHEMA_VERSION] = (
        PX4_GAZEBO_FLEET_FEEDBACK_CANDIDATE_SCHEMA_VERSION
    )
    candidate_id: str
    fleet_memory_snapshot_ref: str = Field(min_length=1)
    candidate_status: PX4GazeboFleetFeedbackCandidateStatus
    proposed_planning_hint: str | None = None
    evidence_refs: tuple[str, ...]
    blocked_reasons: tuple[str, ...] = ()
    operator_approval_required_for_promotion: Literal[True] = True
    generated_at: datetime

    @field_validator("evidence_refs", "blocked_reasons", mode="before")
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
    def _validate_candidate(self) -> "PX4GazeboFleetFeedbackCandidate":
        if self.candidate_status == PX4GazeboFleetFeedbackCandidateStatus.PROPOSED:
            if not self.proposed_planning_hint or self.blocked_reasons:
                raise PX4GazeboFleetMemoryError(
                    "proposed feedback candidate requires hint and no blocked reasons"
                )
        else:
            if not self.blocked_reasons:
                raise PX4GazeboFleetMemoryError(
                    "non-proposed feedback candidate requires blocked reasons"
                )
        if not self.evidence_refs:
            raise PX4GazeboFleetMemoryError("feedback candidate requires evidence refs")
        return self


class PX4GazeboFleetFeedbackPromotionGate(_FleetMemorySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_FLEET_FEEDBACK_PROMOTION_GATE_SCHEMA_VERSION] = (
        PX4_GAZEBO_FLEET_FEEDBACK_PROMOTION_GATE_SCHEMA_VERSION
    )
    gate_id: str
    candidate_ref: str = Field(min_length=1)
    promotion_status: PX4GazeboFleetFeedbackPromotionStatus
    operator_approval_performed: bool
    promoted_memory_refs: tuple[str, ...]
    blocked_reasons: tuple[str, ...] = ()
    promoted_at: datetime

    @field_validator("promoted_memory_refs", "blocked_reasons", mode="before")
    @classmethod
    def _coerce_strings(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("promoted_at", mode="before")
    @classmethod
    def _coerce_promoted_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_gate(self) -> "PX4GazeboFleetFeedbackPromotionGate":
        if self.promotion_status == PX4GazeboFleetFeedbackPromotionStatus.PROMOTED:
            if not self.operator_approval_performed:
                raise PX4GazeboFleetMemoryError(
                    "feedback promotion requires operator approval"
                )
            if not self.promoted_memory_refs or self.blocked_reasons:
                raise PX4GazeboFleetMemoryError(
                    "promoted feedback gate requires promoted memory refs and no blocked reasons"
                )
        if self.promotion_status == PX4GazeboFleetFeedbackPromotionStatus.BLOCKED:
            if self.operator_approval_performed or self.promoted_memory_refs:
                raise PX4GazeboFleetMemoryError(
                    "blocked feedback gate cannot promote memory"
                )
            if not self.blocked_reasons:
                raise PX4GazeboFleetMemoryError(
                    "blocked feedback gate requires blocked reasons"
                )
        return self


class PX4GazeboMemoryInformedMissionPlan(_FleetMemorySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_MEMORY_INFORMED_MISSION_PLAN_SCHEMA_VERSION] = (
        PX4_GAZEBO_MEMORY_INFORMED_MISSION_PLAN_SCHEMA_VERSION
    )
    plan_id: str
    fleet_memory_snapshot_ref: str = Field(min_length=1)
    promotion_gate_ref: str = Field(min_length=1)
    mission_contract_ref: str = Field(min_length=1)
    promotion_status: Literal["promoted"] = "promoted"
    operator_approval_performed: Literal[True] = True
    promoted_memory_refs: tuple[str, ...]
    memory_used_for_planning_only: Literal[True] = True
    planning_adjustments: tuple[str, ...]
    memory_decision_trace: tuple[str, ...]
    dispatch_authority_source: Literal["operator_approval_not_memory"] = (
        "operator_approval_not_memory"
    )
    generated_at: datetime

    @field_validator(
        "promoted_memory_refs",
        "planning_adjustments",
        "memory_decision_trace",
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
    def _validate_plan(self) -> "PX4GazeboMemoryInformedMissionPlan":
        if not self.planning_adjustments or not self.memory_decision_trace:
            raise PX4GazeboFleetMemoryError(
                "memory-informed plan must record adjustments and decision trace"
            )
        if not self.promoted_memory_refs:
            raise PX4GazeboFleetMemoryError(
                "memory-informed plan requires promoted memory refs"
            )
        if self.fleet_memory_snapshot_ref not in self.memory_decision_trace:
            raise PX4GazeboFleetMemoryError(
                "memory-informed plan decision trace must include fleet memory snapshot ref"
            )
        if self.promotion_gate_ref not in self.memory_decision_trace:
            raise PX4GazeboFleetMemoryError(
                "memory-informed plan decision trace must include promotion gate ref"
            )
        return self


class PX4GazeboLeadDroneObservation(_FleetMemorySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_LEAD_DRONE_OBSERVATION_SCHEMA_VERSION] = (
        PX4_GAZEBO_LEAD_DRONE_OBSERVATION_SCHEMA_VERSION
    )
    observation_id: str
    trajectory_summary_ref: str = Field(min_length=1)
    fleet_memory_snapshot_ref: str = Field(min_length=1)
    lead_drone_label: str = Field(min_length=1)
    time_separated_simulation: Literal[True] = True
    generated_at: datetime

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class PX4GazeboFollowupMissionFeedback(_FleetMemorySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_FOLLOWUP_MISSION_FEEDBACK_SCHEMA_VERSION] = (
        PX4_GAZEBO_FOLLOWUP_MISSION_FEEDBACK_SCHEMA_VERSION
    )
    feedback_id: str
    lead_observation_ref: str = Field(min_length=1)
    follower_plan_ref: str = Field(min_length=1)
    follower_drone_label: str = Field(min_length=1)
    prior_knowledge_used: Literal[True] = True
    memory_authority_granted: Literal[False] = False
    generated_at: datetime

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class PX4GazeboFleetLearningReplayCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    case_status: PX4GazeboFleetLearningReplayCaseStatus
    expected_reason: str


class PX4GazeboFleetLearningReplay(_FleetMemorySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_FLEET_LEARNING_REPLAY_SCHEMA_VERSION] = (
        PX4_GAZEBO_FLEET_LEARNING_REPLAY_SCHEMA_VERSION
    )
    replay_id: str
    cases: tuple[PX4GazeboFleetLearningReplayCase, ...]
    generated_at: datetime

    @field_validator("cases", mode="before")
    @classmethod
    def _coerce_cases(cls, value: Any) -> tuple[PX4GazeboFleetLearningReplayCase, ...]:
        return tuple(
            (
                item
                if isinstance(item, PX4GazeboFleetLearningReplayCase)
                else PX4GazeboFleetLearningReplayCase.model_validate(item)
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
    def _validate_replay(self) -> "PX4GazeboFleetLearningReplay":
        required = {
            "stale_ignored",
            "contradictory_blocked",
            "outlier_not_adopted",
            "unsafe_rejected",
        }
        if not required.issubset({case.case_id for case in self.cases}):
            raise PX4GazeboFleetMemoryError(
                "fleet learning replay is missing required negative cases"
            )
        return self


class PX4GazeboFleetLearningCorpus(_FleetMemorySafetyBoundary):
    schema_version: Literal[PX4_GAZEBO_FLEET_LEARNING_CORPUS_SCHEMA_VERSION] = (
        PX4_GAZEBO_FLEET_LEARNING_CORPUS_SCHEMA_VERSION
    )
    corpus_id: str
    replay_ref: str = Field(min_length=1)
    required_coverage_labels: tuple[str, ...]
    generated_at: datetime

    @field_validator("required_coverage_labels", mode="before")
    @classmethod
    def _coerce_labels(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_corpus(self) -> "PX4GazeboFleetLearningCorpus":
        required = {
            "stale_ignored",
            "contradictory_blocked",
            "outlier_not_adopted",
            "unsafe_rejected",
            "memory_not_authority",
        }
        if not required.issubset(set(self.required_coverage_labels)):
            raise PX4GazeboFleetMemoryError(
                "fleet learning corpus is missing required coverage"
            )
        return self


class PX4GazeboFleetMemoryPart2Finalization(_FleetMemorySafetyBoundary):
    schema_version: Literal[
        PX4_GAZEBO_FLEET_MEMORY_PART2_FINALIZATION_SCHEMA_VERSION
    ] = PX4_GAZEBO_FLEET_MEMORY_PART2_FINALIZATION_SCHEMA_VERSION
    finalization_id: str
    epic_ref: Literal["github_issue:373"] = "github_issue:373"
    part1_completion_ref: str = Field(min_length=1)
    finalization_status: Literal["completed"] = "completed"
    trajectory_summary_refs: tuple[str, ...]
    route_segment_memory_refs: tuple[str, ...]
    delivery_zone_memory_refs: tuple[str, ...]
    fleet_memory_snapshot_ref: str = Field(min_length=1)
    feedback_candidate_ref: str = Field(min_length=1)
    promotion_gate_ref: str = Field(min_length=1)
    memory_informed_plan_ref: str = Field(min_length=1)
    lead_drone_observation_ref: str = Field(min_length=1)
    followup_feedback_ref: str = Field(min_length=1)
    fleet_learning_replay_ref: str = Field(min_length=1)
    fleet_learning_corpus_ref: str = Field(min_length=1)
    part2_layer_labels: tuple[str, ...]
    negative_case_labels: tuple[str, ...]
    memory_use_scope: Literal["planning_gates_risk_scoring_only"] = (
        "planning_gates_risk_scoring_only"
    )
    generated_at: datetime

    @field_validator(
        "trajectory_summary_refs",
        "route_segment_memory_refs",
        "delivery_zone_memory_refs",
        "part2_layer_labels",
        "negative_case_labels",
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
    def _validate_finalization(self) -> "PX4GazeboFleetMemoryPart2Finalization":
        required_layers = {
            "trajectory_summary",
            "route_segment_memory",
            "delivery_zone_memory",
            "fleet_memory_snapshot",
            "feedback_candidate",
            "promotion_gate",
            "memory_informed_planning",
            "lead_follower_feedback",
            "fleet_learning_replay",
            "fleet_learning_corpus",
        }
        if not required_layers.issubset(set(self.part2_layer_labels)):
            raise PX4GazeboFleetMemoryError(
                "Part 2 finalization is missing required layer coverage"
            )
        required_negative = {
            "stale_ignored",
            "contradictory_blocked",
            "outlier_not_adopted",
            "unsafe_rejected",
        }
        if not required_negative.issubset(set(self.negative_case_labels)):
            raise PX4GazeboFleetMemoryError(
                "Part 2 finalization is missing required negative memory cases"
            )
        refs: tuple[str, ...] = (
            *self.trajectory_summary_refs,
            *self.route_segment_memory_refs,
            *self.delivery_zone_memory_refs,
            self.fleet_memory_snapshot_ref,
            self.feedback_candidate_ref,
            self.promotion_gate_ref,
            self.memory_informed_plan_ref,
            self.lead_drone_observation_ref,
            self.followup_feedback_ref,
            self.fleet_learning_replay_ref,
            self.fleet_learning_corpus_ref,
        )
        if any(
            not item or str(item).strip().lower() in {"null", "none"} for item in refs
        ):
            raise PX4GazeboFleetMemoryError(
                "Part 2 finalization requires every memory artifact ref"
            )
        return self


def build_px4_gazebo_delivery_mission_trajectory_summary(
    *,
    runner_result: PX4GazeboDeliveryMissionRunnerResult | Mapping[str, Any],
    replay_timeline: PX4GazeboDeliveryMissionReplayTimeline | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboDeliveryMissionTrajectorySummary:
    runner = _coerce_runner(runner_result)
    replay = _coerce_replay(replay_timeline)
    if replay.runner_result_ref != _runner_ref(runner):
        raise PX4GazeboFleetMemoryError("trajectory summary runner/replay mismatch")
    generated_at = _utc(now)
    outcome = PX4GazeboFleetMemoryMissionOutcome(runner.final_status.value)
    payload = {
        "runner": runner.runner_result_id,
        "replay": replay.replay_timeline_id,
        "outcome": outcome.value,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboDeliveryMissionTrajectorySummary(
        summary_id=_stable_id(
            "px4_gazebo_delivery_mission_trajectory_summary", payload
        ),
        runner_result_ref=_runner_ref(runner),
        replay_timeline_ref=_replay_ref(replay),
        mission_outcome=outcome,
        final_status=runner.final_status.value,
        observed_phases=tuple(phase.value for phase in runner.observed_phases),
        blocked_phase=(
            None if runner.blocked_phase is None else runner.blocked_phase.value
        ),
        blocked_reasons=runner.blocked_reasons,
        replay_event_count=len(replay.events),
        route_segment_count=max(1, len(runner.route_dispatch_refs) or 2),
        delivery_zone_refs=("gazebo_pad:pickup", "gazebo_pad:dropoff"),
        generated_at=generated_at,
        metadata={"issue": 373, "part": 2, "layer": "L"},
    )


def build_px4_gazebo_route_segment_observation(
    *,
    summary: PX4GazeboDeliveryMissionTrajectorySummary | Mapping[str, Any],
    segment_ref: str,
    source_zone_ref: str,
    target_zone_ref: str,
    now: datetime | None = None,
) -> PX4GazeboRouteSegmentObservation:
    resolved_summary = (
        summary
        if isinstance(summary, PX4GazeboDeliveryMissionTrajectorySummary)
        else PX4GazeboDeliveryMissionTrajectorySummary.model_validate(dict(summary))
    )
    generated_at = _utc(now)
    success = (
        resolved_summary.mission_outcome == PX4GazeboFleetMemoryMissionOutcome.COMPLETED
    )
    risk_labels = () if success else resolved_summary.blocked_reasons
    payload = {
        "summary": resolved_summary.summary_id,
        "segment_ref": segment_ref,
        "success": success,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboRouteSegmentObservation(
        observation_id=_stable_id("px4_gazebo_route_segment_observation", payload),
        trajectory_summary_ref=_summary_ref(resolved_summary),
        segment_ref=segment_ref,
        source_zone_ref=source_zone_ref,
        target_zone_ref=target_zone_ref,
        mission_outcome=resolved_summary.mission_outcome,
        risk_labels=risk_labels,
        observed_success=success,
        generated_at=generated_at,
    )


def build_px4_gazebo_route_segment_memory(
    *,
    observations: Sequence[PX4GazeboRouteSegmentObservation | Mapping[str, Any]],
    now: datetime | None = None,
) -> PX4GazeboRouteSegmentMemory:
    resolved = tuple(
        (
            item
            if isinstance(item, PX4GazeboRouteSegmentObservation)
            else PX4GazeboRouteSegmentObservation.model_validate(dict(item))
        )
        for item in observations
    )
    if not resolved:
        raise PX4GazeboFleetMemoryError("route segment memory requires observations")
    segment_ref = resolved[0].segment_ref
    if any(item.segment_ref != segment_ref for item in resolved):
        raise PX4GazeboFleetMemoryError("route segment memory cannot mix segments")
    generated_at = _utc(now)
    success_count = sum(1 for item in resolved if item.observed_success)
    blocked_count = len(resolved) - success_count
    labels = _ordered_strings(label for item in resolved for label in item.risk_labels)
    payload = {
        "segment_ref": segment_ref,
        "observations": [item.observation_id for item in resolved],
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboRouteSegmentMemory(
        memory_id=_stable_id("px4_gazebo_route_segment_memory", payload),
        segment_ref=segment_ref,
        observation_refs=tuple(
            f"px4_gazebo_route_segment_observation:{item.observation_id}"
            for item in resolved
        ),
        success_count=success_count,
        blocked_count=blocked_count,
        risk_labels=labels,
        planning_hint=(
            "prefer_known_good_segment"
            if blocked_count == 0
            else "increase_gate_scrutiny_for_segment"
        ),
        generated_at=generated_at,
    )


def build_px4_gazebo_delivery_zone_memory(
    *,
    zone_ref: str,
    summaries: Sequence[PX4GazeboDeliveryMissionTrajectorySummary | Mapping[str, Any]],
    now: datetime | None = None,
) -> PX4GazeboDeliveryZoneMemory:
    resolved = tuple(
        (
            item
            if isinstance(item, PX4GazeboDeliveryMissionTrajectorySummary)
            else PX4GazeboDeliveryMissionTrajectorySummary.model_validate(dict(item))
        )
        for item in summaries
    )
    if not resolved:
        raise PX4GazeboFleetMemoryError("delivery zone memory requires summaries")
    generated_at = _utc(now)
    success_count = sum(
        1
        for item in resolved
        if item.mission_outcome == PX4GazeboFleetMemoryMissionOutcome.COMPLETED
    )
    blocked_count = len(resolved) - success_count
    payload = {
        "zone_ref": zone_ref,
        "summaries": [item.summary_id for item in resolved],
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboDeliveryZoneMemory(
        memory_id=_stable_id("px4_gazebo_delivery_zone_memory", payload),
        zone_ref=zone_ref,
        trajectory_summary_refs=tuple(_summary_ref(item) for item in resolved),
        success_count=success_count,
        blocked_count=blocked_count,
        gate_hint="normal_gate" if blocked_count == 0 else "tighten_zone_gate",
        generated_at=generated_at,
    )


def build_px4_gazebo_fleet_memory_snapshot(
    *,
    summaries: Sequence[PX4GazeboDeliveryMissionTrajectorySummary | Mapping[str, Any]],
    segment_memories: Sequence[PX4GazeboRouteSegmentMemory | Mapping[str, Any]],
    zone_memories: Sequence[PX4GazeboDeliveryZoneMemory | Mapping[str, Any]],
    now: datetime | None = None,
) -> PX4GazeboFleetMemorySnapshot:
    resolved_summaries = tuple(
        (
            item
            if isinstance(item, PX4GazeboDeliveryMissionTrajectorySummary)
            else PX4GazeboDeliveryMissionTrajectorySummary.model_validate(dict(item))
        )
        for item in summaries
    )
    resolved_segments = tuple(
        (
            item
            if isinstance(item, PX4GazeboRouteSegmentMemory)
            else PX4GazeboRouteSegmentMemory.model_validate(dict(item))
        )
        for item in segment_memories
    )
    resolved_zones = tuple(
        (
            item
            if isinstance(item, PX4GazeboDeliveryZoneMemory)
            else PX4GazeboDeliveryZoneMemory.model_validate(dict(item))
        )
        for item in zone_memories
    )
    generated_at = _utc(now)
    payload = {
        "summaries": [item.summary_id for item in resolved_summaries],
        "segments": [item.memory_id for item in resolved_segments],
        "zones": [item.memory_id for item in resolved_zones],
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboFleetMemorySnapshot(
        snapshot_id=_stable_id("px4_gazebo_fleet_memory_snapshot", payload),
        trajectory_summary_refs=tuple(
            _summary_ref(item) for item in resolved_summaries
        ),
        route_segment_memory_refs=tuple(
            _route_segment_memory_ref(item) for item in resolved_segments
        ),
        delivery_zone_memory_refs=tuple(
            _delivery_zone_memory_ref(item) for item in resolved_zones
        ),
        generated_at=generated_at,
    )


def build_px4_gazebo_fleet_feedback_candidate(
    *,
    snapshot: PX4GazeboFleetMemorySnapshot | Mapping[str, Any],
    status: (
        PX4GazeboFleetFeedbackCandidateStatus | str
    ) = PX4GazeboFleetFeedbackCandidateStatus.PROPOSED,
    proposed_planning_hint: str | None = "prefer_known_good_segment_and_normal_gate",
    blocked_reasons: Sequence[str] = (),
    now: datetime | None = None,
) -> PX4GazeboFleetFeedbackCandidate:
    resolved_snapshot = (
        snapshot
        if isinstance(snapshot, PX4GazeboFleetMemorySnapshot)
        else PX4GazeboFleetMemorySnapshot.model_validate(dict(snapshot))
    )
    resolved_status = (
        status
        if isinstance(status, PX4GazeboFleetFeedbackCandidateStatus)
        else PX4GazeboFleetFeedbackCandidateStatus(str(status))
    )
    generated_at = _utc(now)
    payload = {
        "snapshot": resolved_snapshot.snapshot_id,
        "status": resolved_status.value,
        "hint": proposed_planning_hint,
        "blocked": list(blocked_reasons),
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboFleetFeedbackCandidate(
        candidate_id=_stable_id("px4_gazebo_fleet_feedback_candidate", payload),
        fleet_memory_snapshot_ref=_fleet_snapshot_ref(resolved_snapshot),
        candidate_status=resolved_status,
        proposed_planning_hint=proposed_planning_hint,
        evidence_refs=(
            _fleet_snapshot_ref(resolved_snapshot),
            *resolved_snapshot.trajectory_summary_refs,
        ),
        blocked_reasons=blocked_reasons,
        generated_at=generated_at,
    )


def build_px4_gazebo_fleet_feedback_promotion_gate(
    *,
    candidate: PX4GazeboFleetFeedbackCandidate | Mapping[str, Any],
    operator_approval_performed: bool,
    now: datetime | None = None,
) -> PX4GazeboFleetFeedbackPromotionGate:
    resolved_candidate = (
        candidate
        if isinstance(candidate, PX4GazeboFleetFeedbackCandidate)
        else PX4GazeboFleetFeedbackCandidate.model_validate(dict(candidate))
    )
    generated_at = _utc(now)
    promoted = (
        operator_approval_performed
        and resolved_candidate.candidate_status
        == PX4GazeboFleetFeedbackCandidateStatus.PROPOSED
    )
    payload = {
        "candidate": resolved_candidate.candidate_id,
        "operator_approval_performed": bool(operator_approval_performed),
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboFleetFeedbackPromotionGate(
        gate_id=_stable_id("px4_gazebo_fleet_feedback_promotion_gate", payload),
        candidate_ref=_candidate_ref(resolved_candidate),
        promotion_status=(
            PX4GazeboFleetFeedbackPromotionStatus.PROMOTED
            if promoted
            else PX4GazeboFleetFeedbackPromotionStatus.BLOCKED
        ),
        operator_approval_performed=bool(operator_approval_performed),
        promoted_memory_refs=(
            (resolved_candidate.fleet_memory_snapshot_ref,) if promoted else ()
        ),
        blocked_reasons=(
            () if promoted else ("operator_approval_required_for_memory_promotion",)
        ),
        promoted_at=generated_at,
    )


def build_px4_gazebo_memory_informed_mission_plan(
    *,
    mission_contract_ref: str,
    snapshot: PX4GazeboFleetMemorySnapshot | Mapping[str, Any],
    promotion_gate: PX4GazeboFleetFeedbackPromotionGate | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboMemoryInformedMissionPlan:
    resolved_snapshot = (
        snapshot
        if isinstance(snapshot, PX4GazeboFleetMemorySnapshot)
        else PX4GazeboFleetMemorySnapshot.model_validate(dict(snapshot))
    )
    resolved_gate = (
        promotion_gate
        if isinstance(promotion_gate, PX4GazeboFleetFeedbackPromotionGate)
        else PX4GazeboFleetFeedbackPromotionGate.model_validate(dict(promotion_gate))
    )
    if resolved_gate.promotion_status != PX4GazeboFleetFeedbackPromotionStatus.PROMOTED:
        raise PX4GazeboFleetMemoryError(
            "memory-informed mission planning requires promoted memory"
        )
    if resolved_snapshot.memory_direct_command_authority_allowed is not False:
        raise PX4GazeboFleetMemoryError("fleet memory must not hold command authority")
    generated_at = _utc(now)
    payload = {
        "mission_contract_ref": mission_contract_ref,
        "snapshot": resolved_snapshot.snapshot_id,
        "gate": resolved_gate.gate_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboMemoryInformedMissionPlan(
        plan_id=_stable_id("px4_gazebo_memory_informed_mission_plan", payload),
        fleet_memory_snapshot_ref=_fleet_snapshot_ref(resolved_snapshot),
        promotion_gate_ref=_promotion_gate_ref(resolved_gate),
        mission_contract_ref=mission_contract_ref,
        promoted_memory_refs=resolved_gate.promoted_memory_refs,
        planning_adjustments=(
            "prefer_known_good_segment",
            "tighten_gate_if_prior_blocked_reason_matches",
        ),
        memory_decision_trace=(
            _fleet_snapshot_ref(resolved_snapshot),
            _promotion_gate_ref(resolved_gate),
            "memory_used_for_planning_not_dispatch",
        ),
        generated_at=generated_at,
    )


def build_px4_gazebo_lead_drone_observation(
    *,
    summary: PX4GazeboDeliveryMissionTrajectorySummary | Mapping[str, Any],
    snapshot: PX4GazeboFleetMemorySnapshot | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboLeadDroneObservation:
    resolved_summary = (
        summary
        if isinstance(summary, PX4GazeboDeliveryMissionTrajectorySummary)
        else PX4GazeboDeliveryMissionTrajectorySummary.model_validate(dict(summary))
    )
    resolved_snapshot = (
        snapshot
        if isinstance(snapshot, PX4GazeboFleetMemorySnapshot)
        else PX4GazeboFleetMemorySnapshot.model_validate(dict(snapshot))
    )
    generated_at = _utc(now)
    payload = {
        "summary": resolved_summary.summary_id,
        "snapshot": resolved_snapshot.snapshot_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboLeadDroneObservation(
        observation_id=_stable_id("px4_gazebo_lead_drone_observation", payload),
        trajectory_summary_ref=_summary_ref(resolved_summary),
        fleet_memory_snapshot_ref=_fleet_snapshot_ref(resolved_snapshot),
        lead_drone_label="lead_drone_simulated_run_1",
        generated_at=generated_at,
    )


def build_px4_gazebo_followup_mission_feedback(
    *,
    lead_observation: PX4GazeboLeadDroneObservation | Mapping[str, Any],
    follower_plan: PX4GazeboMemoryInformedMissionPlan | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboFollowupMissionFeedback:
    resolved_lead = (
        lead_observation
        if isinstance(lead_observation, PX4GazeboLeadDroneObservation)
        else PX4GazeboLeadDroneObservation.model_validate(dict(lead_observation))
    )
    resolved_plan = (
        follower_plan
        if isinstance(follower_plan, PX4GazeboMemoryInformedMissionPlan)
        else PX4GazeboMemoryInformedMissionPlan.model_validate(dict(follower_plan))
    )
    generated_at = _utc(now)
    payload = {
        "lead": resolved_lead.observation_id,
        "plan": resolved_plan.plan_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboFollowupMissionFeedback(
        feedback_id=_stable_id("px4_gazebo_followup_mission_feedback", payload),
        lead_observation_ref=f"px4_gazebo_lead_drone_observation:{resolved_lead.observation_id}",
        follower_plan_ref=f"px4_gazebo_memory_informed_mission_plan:{resolved_plan.plan_id}",
        follower_drone_label="follower_drone_simulated_run_2",
        generated_at=generated_at,
    )


def build_px4_gazebo_fleet_learning_replay(
    *,
    now: datetime | None = None,
) -> PX4GazeboFleetLearningReplay:
    generated_at = _utc(now)
    cases = (
        PX4GazeboFleetLearningReplayCase(
            case_id="stale_ignored",
            case_status=PX4GazeboFleetLearningReplayCaseStatus.IGNORED,
            expected_reason="stale_memory_snapshot_ignored",
        ),
        PX4GazeboFleetLearningReplayCase(
            case_id="contradictory_blocked",
            case_status=PX4GazeboFleetLearningReplayCaseStatus.BLOCKED,
            expected_reason="contradictory_memory_requires_operator_review",
        ),
        PX4GazeboFleetLearningReplayCase(
            case_id="outlier_not_adopted",
            case_status=PX4GazeboFleetLearningReplayCaseStatus.IGNORED,
            expected_reason="outlier_memory_not_promoted",
        ),
        PX4GazeboFleetLearningReplayCase(
            case_id="unsafe_rejected",
            case_status=PX4GazeboFleetLearningReplayCaseStatus.REJECTED,
            expected_reason="memory_attempted_command_authority_rejected",
        ),
    )
    payload = {
        "cases": [case.case_id for case in cases],
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboFleetLearningReplay(
        replay_id=_stable_id("px4_gazebo_fleet_learning_replay", payload),
        cases=cases,
        generated_at=generated_at,
    )


def build_px4_gazebo_fleet_learning_corpus(
    *,
    replay: PX4GazeboFleetLearningReplay | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboFleetLearningCorpus:
    resolved_replay = (
        replay
        if isinstance(replay, PX4GazeboFleetLearningReplay)
        else PX4GazeboFleetLearningReplay.model_validate(dict(replay))
    )
    generated_at = _utc(now)
    payload = {
        "replay": resolved_replay.replay_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboFleetLearningCorpus(
        corpus_id=_stable_id("px4_gazebo_fleet_learning_corpus", payload),
        replay_ref=f"px4_gazebo_fleet_learning_replay:{resolved_replay.replay_id}",
        required_coverage_labels=(
            "stale_ignored",
            "contradictory_blocked",
            "outlier_not_adopted",
            "unsafe_rejected",
            "memory_not_authority",
        ),
        generated_at=generated_at,
    )


def build_px4_gazebo_fleet_memory_part2_finalization(
    *,
    trajectory_summaries: Sequence[
        PX4GazeboDeliveryMissionTrajectorySummary | Mapping[str, Any]
    ],
    segment_memories: Sequence[PX4GazeboRouteSegmentMemory | Mapping[str, Any]],
    zone_memories: Sequence[PX4GazeboDeliveryZoneMemory | Mapping[str, Any]],
    snapshot: PX4GazeboFleetMemorySnapshot | Mapping[str, Any],
    feedback_candidate: PX4GazeboFleetFeedbackCandidate | Mapping[str, Any],
    promotion_gate: PX4GazeboFleetFeedbackPromotionGate | Mapping[str, Any],
    memory_informed_plan: PX4GazeboMemoryInformedMissionPlan | Mapping[str, Any],
    lead_observation: PX4GazeboLeadDroneObservation | Mapping[str, Any],
    followup_feedback: PX4GazeboFollowupMissionFeedback | Mapping[str, Any],
    fleet_learning_replay: PX4GazeboFleetLearningReplay | Mapping[str, Any],
    fleet_learning_corpus: PX4GazeboFleetLearningCorpus | Mapping[str, Any],
    part1_completion_ref: str = "github_pr:388",
    now: datetime | None = None,
) -> PX4GazeboFleetMemoryPart2Finalization:
    resolved_summaries = tuple(
        (
            item
            if isinstance(item, PX4GazeboDeliveryMissionTrajectorySummary)
            else PX4GazeboDeliveryMissionTrajectorySummary.model_validate(dict(item))
        )
        for item in trajectory_summaries
    )
    resolved_segments = tuple(
        (
            item
            if isinstance(item, PX4GazeboRouteSegmentMemory)
            else PX4GazeboRouteSegmentMemory.model_validate(dict(item))
        )
        for item in segment_memories
    )
    resolved_zones = tuple(
        (
            item
            if isinstance(item, PX4GazeboDeliveryZoneMemory)
            else PX4GazeboDeliveryZoneMemory.model_validate(dict(item))
        )
        for item in zone_memories
    )
    resolved_snapshot = (
        snapshot
        if isinstance(snapshot, PX4GazeboFleetMemorySnapshot)
        else PX4GazeboFleetMemorySnapshot.model_validate(dict(snapshot))
    )
    resolved_candidate = (
        feedback_candidate
        if isinstance(feedback_candidate, PX4GazeboFleetFeedbackCandidate)
        else PX4GazeboFleetFeedbackCandidate.model_validate(dict(feedback_candidate))
    )
    resolved_gate = (
        promotion_gate
        if isinstance(promotion_gate, PX4GazeboFleetFeedbackPromotionGate)
        else PX4GazeboFleetFeedbackPromotionGate.model_validate(dict(promotion_gate))
    )
    resolved_plan = (
        memory_informed_plan
        if isinstance(memory_informed_plan, PX4GazeboMemoryInformedMissionPlan)
        else PX4GazeboMemoryInformedMissionPlan.model_validate(
            dict(memory_informed_plan)
        )
    )
    resolved_lead = (
        lead_observation
        if isinstance(lead_observation, PX4GazeboLeadDroneObservation)
        else PX4GazeboLeadDroneObservation.model_validate(dict(lead_observation))
    )
    resolved_followup = (
        followup_feedback
        if isinstance(followup_feedback, PX4GazeboFollowupMissionFeedback)
        else PX4GazeboFollowupMissionFeedback.model_validate(dict(followup_feedback))
    )
    resolved_replay = (
        fleet_learning_replay
        if isinstance(fleet_learning_replay, PX4GazeboFleetLearningReplay)
        else PX4GazeboFleetLearningReplay.model_validate(dict(fleet_learning_replay))
    )
    resolved_corpus = (
        fleet_learning_corpus
        if isinstance(fleet_learning_corpus, PX4GazeboFleetLearningCorpus)
        else PX4GazeboFleetLearningCorpus.model_validate(dict(fleet_learning_corpus))
    )

    snapshot_ref = _fleet_snapshot_ref(resolved_snapshot)
    gate_ref = _promotion_gate_ref(resolved_gate)
    plan_ref = _memory_plan_ref(resolved_plan)
    if resolved_candidate.fleet_memory_snapshot_ref != snapshot_ref:
        raise PX4GazeboFleetMemoryError(
            "Part 2 finalization requires candidate/snapshot ref consistency"
        )
    if resolved_gate.candidate_ref != _candidate_ref(resolved_candidate):
        raise PX4GazeboFleetMemoryError(
            "Part 2 finalization requires promotion gate/candidate ref consistency"
        )
    if resolved_gate.promotion_status != PX4GazeboFleetFeedbackPromotionStatus.PROMOTED:
        raise PX4GazeboFleetMemoryError(
            "Part 2 finalization requires promoted feedback gate"
        )
    if resolved_plan.fleet_memory_snapshot_ref != snapshot_ref:
        raise PX4GazeboFleetMemoryError(
            "Part 2 finalization requires plan/snapshot ref consistency"
        )
    if resolved_plan.promotion_gate_ref != gate_ref:
        raise PX4GazeboFleetMemoryError(
            "Part 2 finalization requires plan/promotion gate ref consistency"
        )
    if resolved_plan.memory_used_for_planning_only is not True:
        raise PX4GazeboFleetMemoryError(
            "Part 2 finalization requires memory to be planning-only"
        )
    if resolved_lead.fleet_memory_snapshot_ref != snapshot_ref:
        raise PX4GazeboFleetMemoryError(
            "Part 2 finalization requires lead observation/snapshot consistency"
        )
    if resolved_followup.lead_observation_ref != _lead_observation_ref(resolved_lead):
        raise PX4GazeboFleetMemoryError(
            "Part 2 finalization requires follow-up/lead observation consistency"
        )
    if resolved_followup.follower_plan_ref != plan_ref:
        raise PX4GazeboFleetMemoryError(
            "Part 2 finalization requires follow-up/follower plan consistency"
        )
    if resolved_followup.memory_authority_granted is not False:
        raise PX4GazeboFleetMemoryError(
            "Part 2 finalization rejects memory authority in follow-up feedback"
        )
    if resolved_corpus.replay_ref != _fleet_replay_ref(resolved_replay):
        raise PX4GazeboFleetMemoryError(
            "Part 2 finalization requires corpus/replay ref consistency"
        )
    generated_at = _utc(now)
    payload = {
        "snapshot": resolved_snapshot.snapshot_id,
        "plan": resolved_plan.plan_id,
        "replay": resolved_replay.replay_id,
        "corpus": resolved_corpus.corpus_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboFleetMemoryPart2Finalization(
        finalization_id=_stable_id(
            "px4_gazebo_fleet_memory_part2_finalization", payload
        ),
        part1_completion_ref=part1_completion_ref,
        trajectory_summary_refs=tuple(
            _summary_ref(item) for item in resolved_summaries
        ),
        route_segment_memory_refs=tuple(
            _route_segment_memory_ref(item) for item in resolved_segments
        ),
        delivery_zone_memory_refs=tuple(
            _delivery_zone_memory_ref(item) for item in resolved_zones
        ),
        fleet_memory_snapshot_ref=snapshot_ref,
        feedback_candidate_ref=_candidate_ref(resolved_candidate),
        promotion_gate_ref=gate_ref,
        memory_informed_plan_ref=plan_ref,
        lead_drone_observation_ref=_lead_observation_ref(resolved_lead),
        followup_feedback_ref=_followup_feedback_ref(resolved_followup),
        fleet_learning_replay_ref=_fleet_replay_ref(resolved_replay),
        fleet_learning_corpus_ref=_fleet_corpus_ref(resolved_corpus),
        part2_layer_labels=(
            "trajectory_summary",
            "route_segment_memory",
            "delivery_zone_memory",
            "fleet_memory_snapshot",
            "feedback_candidate",
            "promotion_gate",
            "memory_informed_planning",
            "lead_follower_feedback",
            "fleet_learning_replay",
            "fleet_learning_corpus",
        ),
        negative_case_labels=tuple(case.case_id for case in resolved_replay.cases),
        generated_at=generated_at,
    )


def run_px4_gazebo_fleet_memory_feedback_simulation(
    *,
    happy_runner_result: PX4GazeboDeliveryMissionRunnerResult | Mapping[str, Any],
    happy_replay_timeline: PX4GazeboDeliveryMissionReplayTimeline | Mapping[str, Any],
    blocked_runner_result: PX4GazeboDeliveryMissionRunnerResult | Mapping[str, Any],
    blocked_replay_timeline: PX4GazeboDeliveryMissionReplayTimeline | Mapping[str, Any],
    mission_contract_ref: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = _utc(now)
    happy_summary = build_px4_gazebo_delivery_mission_trajectory_summary(
        runner_result=happy_runner_result,
        replay_timeline=happy_replay_timeline,
        now=generated_at,
    )
    blocked_summary = build_px4_gazebo_delivery_mission_trajectory_summary(
        runner_result=blocked_runner_result,
        replay_timeline=blocked_replay_timeline,
        now=generated_at,
    )
    happy_observation = build_px4_gazebo_route_segment_observation(
        summary=happy_summary,
        segment_ref="segment:pickup_to_dropoff",
        source_zone_ref="gazebo_pad:pickup",
        target_zone_ref="gazebo_pad:dropoff",
        now=generated_at,
    )
    blocked_observation = build_px4_gazebo_route_segment_observation(
        summary=blocked_summary,
        segment_ref="segment:pickup_to_dropoff",
        source_zone_ref="gazebo_pad:pickup",
        target_zone_ref="gazebo_pad:dropoff",
        now=generated_at,
    )
    segment_memory = build_px4_gazebo_route_segment_memory(
        observations=(happy_observation, blocked_observation),
        now=generated_at,
    )
    pickup_zone = build_px4_gazebo_delivery_zone_memory(
        zone_ref="gazebo_pad:pickup",
        summaries=(happy_summary, blocked_summary),
        now=generated_at,
    )
    dropoff_zone = build_px4_gazebo_delivery_zone_memory(
        zone_ref="gazebo_pad:dropoff",
        summaries=(happy_summary, blocked_summary),
        now=generated_at,
    )
    snapshot = build_px4_gazebo_fleet_memory_snapshot(
        summaries=(happy_summary, blocked_summary),
        segment_memories=(segment_memory,),
        zone_memories=(pickup_zone, dropoff_zone),
        now=generated_at,
    )
    candidate = build_px4_gazebo_fleet_feedback_candidate(
        snapshot=snapshot,
        now=generated_at,
    )
    blocked_gate = build_px4_gazebo_fleet_feedback_promotion_gate(
        candidate=candidate,
        operator_approval_performed=False,
        now=generated_at,
    )
    promoted_gate = build_px4_gazebo_fleet_feedback_promotion_gate(
        candidate=candidate,
        operator_approval_performed=True,
        now=generated_at,
    )
    plan = build_px4_gazebo_memory_informed_mission_plan(
        mission_contract_ref=mission_contract_ref,
        snapshot=snapshot,
        promotion_gate=promoted_gate,
        now=generated_at,
    )
    lead_observation = build_px4_gazebo_lead_drone_observation(
        summary=happy_summary,
        snapshot=snapshot,
        now=generated_at,
    )
    followup_feedback = build_px4_gazebo_followup_mission_feedback(
        lead_observation=lead_observation,
        follower_plan=plan,
        now=generated_at,
    )
    replay = build_px4_gazebo_fleet_learning_replay(now=generated_at)
    corpus = build_px4_gazebo_fleet_learning_corpus(
        replay=replay,
        now=generated_at,
    )
    finalization = build_px4_gazebo_fleet_memory_part2_finalization(
        trajectory_summaries=(happy_summary, blocked_summary),
        segment_memories=(segment_memory,),
        zone_memories=(pickup_zone, dropoff_zone),
        snapshot=snapshot,
        feedback_candidate=candidate,
        promotion_gate=promoted_gate,
        memory_informed_plan=plan,
        lead_observation=lead_observation,
        followup_feedback=followup_feedback,
        fleet_learning_replay=replay,
        fleet_learning_corpus=corpus,
        now=generated_at,
    )
    return {
        "happy_summary": happy_summary,
        "blocked_summary": blocked_summary,
        "route_segment_observations": (happy_observation, blocked_observation),
        "route_segment_memory": segment_memory,
        "delivery_zone_memories": (pickup_zone, dropoff_zone),
        "fleet_memory_snapshot": snapshot,
        "feedback_candidate": candidate,
        "blocked_promotion_gate": blocked_gate,
        "promoted_promotion_gate": promoted_gate,
        "memory_informed_plan": plan,
        "lead_drone_observation": lead_observation,
        "followup_mission_feedback": followup_feedback,
        "fleet_learning_replay": replay,
        "fleet_learning_corpus": corpus,
        "part2_finalization": finalization,
    }


__all__ = [
    "PX4_GAZEBO_DELIVERY_MISSION_TRAJECTORY_SUMMARY_SCHEMA_VERSION",
    "PX4_GAZEBO_ROUTE_SEGMENT_OBSERVATION_SCHEMA_VERSION",
    "PX4_GAZEBO_ROUTE_SEGMENT_MEMORY_SCHEMA_VERSION",
    "PX4_GAZEBO_DELIVERY_ZONE_MEMORY_SCHEMA_VERSION",
    "PX4_GAZEBO_FLEET_MEMORY_SNAPSHOT_SCHEMA_VERSION",
    "PX4_GAZEBO_FLEET_FEEDBACK_CANDIDATE_SCHEMA_VERSION",
    "PX4_GAZEBO_FLEET_FEEDBACK_PROMOTION_GATE_SCHEMA_VERSION",
    "PX4_GAZEBO_MEMORY_INFORMED_MISSION_PLAN_SCHEMA_VERSION",
    "PX4_GAZEBO_LEAD_DRONE_OBSERVATION_SCHEMA_VERSION",
    "PX4_GAZEBO_FOLLOWUP_MISSION_FEEDBACK_SCHEMA_VERSION",
    "PX4_GAZEBO_FLEET_LEARNING_REPLAY_SCHEMA_VERSION",
    "PX4_GAZEBO_FLEET_LEARNING_CORPUS_SCHEMA_VERSION",
    "PX4_GAZEBO_FLEET_MEMORY_PART2_FINALIZATION_SCHEMA_VERSION",
    "PX4GazeboDeliveryMissionTrajectorySummary",
    "PX4GazeboDeliveryZoneMemory",
    "PX4GazeboFleetFeedbackCandidate",
    "PX4GazeboFleetFeedbackCandidateStatus",
    "PX4GazeboFleetFeedbackPromotionGate",
    "PX4GazeboFleetFeedbackPromotionStatus",
    "PX4GazeboFleetLearningCorpus",
    "PX4GazeboFleetLearningReplay",
    "PX4GazeboFleetLearningReplayCase",
    "PX4GazeboFleetLearningReplayCaseStatus",
    "PX4GazeboFleetMemoryError",
    "PX4GazeboFleetMemoryMissionOutcome",
    "PX4GazeboFleetMemoryPart2Finalization",
    "PX4GazeboFleetMemorySnapshot",
    "PX4GazeboFollowupMissionFeedback",
    "PX4GazeboLeadDroneObservation",
    "PX4GazeboMemoryInformedMissionPlan",
    "PX4GazeboRouteSegmentMemory",
    "PX4GazeboRouteSegmentObservation",
    "build_px4_gazebo_delivery_mission_trajectory_summary",
    "build_px4_gazebo_delivery_zone_memory",
    "build_px4_gazebo_fleet_feedback_candidate",
    "build_px4_gazebo_fleet_feedback_promotion_gate",
    "build_px4_gazebo_fleet_learning_corpus",
    "build_px4_gazebo_fleet_learning_replay",
    "build_px4_gazebo_fleet_memory_snapshot",
    "build_px4_gazebo_fleet_memory_part2_finalization",
    "build_px4_gazebo_followup_mission_feedback",
    "build_px4_gazebo_lead_drone_observation",
    "build_px4_gazebo_memory_informed_mission_plan",
    "build_px4_gazebo_route_segment_memory",
    "build_px4_gazebo_route_segment_observation",
    "run_px4_gazebo_fleet_memory_feedback_simulation",
]

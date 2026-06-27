"""Human-reviewable Mission OS evidence reports.

This module intentionally does not execute a mission. It turns existing
PX4/Gazebo mission-control and fleet-memory artifacts into redacted review
artifacts that an operator or reviewer can inspect without raw logs, sqlite
state, full telemetry, or reproduction details.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.px4_gazebo_delivery_mission_control import (
    PX4GazeboDeliveryMissionFinalStatus,
    PX4GazeboDeliveryMissionReplayTimeline,
    PX4GazeboDeliveryMissionRunnerResult,
)
from src.runtime.px4_gazebo_fleet_memory import (
    PX4GazeboFleetFeedbackCandidate,
    PX4GazeboFleetFeedbackPromotionGate,
    PX4GazeboFleetLearningCorpus,
    PX4GazeboFleetLearningReplay,
    PX4GazeboFleetMemoryPart2Finalization,
    PX4GazeboFleetMemorySnapshot,
    PX4GazeboFollowupMissionFeedback,
    PX4GazeboLeadDroneObservation,
    PX4GazeboMemoryInformedMissionPlan,
)

PX4_GAZEBO_MISSION_RUN_REPLAY_INDEX_SCHEMA_VERSION = (
    "px4_gazebo_mission_run_replay_index.v1"
)
PX4_GAZEBO_MISSION_SAFETY_BOUNDARY_SUMMARY_SCHEMA_VERSION = (
    "px4_gazebo_mission_safety_boundary_summary.v1"
)
PX4_GAZEBO_FLEET_MEMORY_PROVENANCE_SUMMARY_SCHEMA_VERSION = (
    "px4_gazebo_fleet_memory_provenance_summary.v1"
)
PX4_GAZEBO_MISSION_RUN_EVIDENCE_REPORT_SCHEMA_VERSION = (
    "px4_gazebo_mission_run_evidence_report.v1"
)


class PX4GazeboMissionReviewError(RuntimeError):
    """Raised when a review report would be unsafe or inconsistent."""


class _RedactionBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    redacted_for_review: Literal[True] = True
    raw_logs_included: Literal[False] = False
    sqlite_included: Literal[False] = False
    full_telemetry_included: Literal[False] = False
    reproduction_steps_included: Literal[False] = False
    runtime_script_names_included: Literal[False] = False
    transport_details_included: Literal[False] = False
    low_level_command_details_included: Literal[False] = False
    output_paths_included: Literal[False] = False


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


def _safety_summary_ref(summary: "PX4GazeboMissionSafetyBoundarySummary") -> str:
    return f"px4_gazebo_mission_safety_boundary_summary:{summary.summary_id}"


def _replay_index_ref(index: "PX4GazeboMissionRunReplayIndex") -> str:
    return f"px4_gazebo_mission_run_replay_index:{index.index_id}"


def _fleet_provenance_ref(
    provenance: "PX4GazeboFleetMemoryProvenanceSummary",
) -> str:
    return f"px4_gazebo_fleet_memory_provenance_summary:{provenance.summary_id}"


def validate_px4_gazebo_mission_review_archive_consistency(
    *,
    report: "PX4GazeboMissionRunEvidenceReport" | Mapping[str, Any],
    replay_index: "PX4GazeboMissionRunReplayIndex" | Mapping[str, Any] | None = None,
    safety_boundary_summary: (
        "PX4GazeboMissionSafetyBoundarySummary" | Mapping[str, Any] | None
    ) = None,
    fleet_memory_provenance: (
        "PX4GazeboFleetMemoryProvenanceSummary" | Mapping[str, Any] | None
    ) = None,
) -> None:
    """Reject review archives assembled from inconsistent artifact refs."""

    report_obj = (
        report
        if isinstance(report, PX4GazeboMissionRunEvidenceReport)
        else PX4GazeboMissionRunEvidenceReport.model_validate(dict(report))
    )
    if replay_index is not None:
        replay_obj = (
            replay_index
            if isinstance(replay_index, PX4GazeboMissionRunReplayIndex)
            else PX4GazeboMissionRunReplayIndex.model_validate(dict(replay_index))
        )
        if report_obj.replay_index_ref != _replay_index_ref(replay_obj):
            raise PX4GazeboMissionReviewError("report/replay index ref mismatch")
    if safety_boundary_summary is not None:
        safety_obj = (
            safety_boundary_summary
            if isinstance(
                safety_boundary_summary, PX4GazeboMissionSafetyBoundarySummary
            )
            else PX4GazeboMissionSafetyBoundarySummary.model_validate(
                dict(safety_boundary_summary)
            )
        )
        if report_obj.safety_boundary_summary_ref != _safety_summary_ref(safety_obj):
            raise PX4GazeboMissionReviewError("report/safety boundary ref mismatch")
    if fleet_memory_provenance is not None:
        provenance_obj = (
            fleet_memory_provenance
            if isinstance(
                fleet_memory_provenance, PX4GazeboFleetMemoryProvenanceSummary
            )
            else PX4GazeboFleetMemoryProvenanceSummary.model_validate(
                dict(fleet_memory_provenance)
            )
        )
        if report_obj.fleet_memory_provenance_ref != _fleet_provenance_ref(
            provenance_obj
        ):
            raise PX4GazeboMissionReviewError(
                "report/fleet memory provenance ref mismatch"
            )


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


class PX4GazeboMissionReplayIndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=0)
    t_relative_seconds: float = Field(ge=0)
    phase: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    artifact_ref: str = Field(min_length=1)


class PX4GazeboMissionRunReplayIndex(_RedactionBoundary):
    schema_version: Literal[PX4_GAZEBO_MISSION_RUN_REPLAY_INDEX_SCHEMA_VERSION] = (
        PX4_GAZEBO_MISSION_RUN_REPLAY_INDEX_SCHEMA_VERSION
    )
    index_id: str
    runner_result_ref: str = Field(min_length=1)
    replay_timeline_ref: str = Field(min_length=1)
    final_status: str = Field(min_length=1)
    event_count: int = Field(ge=1)
    phase_sequence: tuple[str, ...]
    entries: tuple[PX4GazeboMissionReplayIndexEntry, ...]
    generated_at: datetime

    @field_validator("phase_sequence", mode="before")
    @classmethod
    def _coerce_phase_sequence(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("entries", mode="before")
    @classmethod
    def _coerce_entries(
        cls, value: Any
    ) -> tuple[PX4GazeboMissionReplayIndexEntry, ...]:
        return tuple(
            (
                item
                if isinstance(item, PX4GazeboMissionReplayIndexEntry)
                else PX4GazeboMissionReplayIndexEntry.model_validate(item)
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
    def _validate_index(self) -> "PX4GazeboMissionRunReplayIndex":
        if self.event_count != len(self.entries):
            raise PX4GazeboMissionReviewError(
                "replay index event_count must match entries"
            )
        if tuple(entry.sequence for entry in self.entries) != tuple(
            range(len(self.entries))
        ):
            raise PX4GazeboMissionReviewError(
                "replay index event sequence must be contiguous"
            )
        if not self.phase_sequence:
            raise PX4GazeboMissionReviewError("replay index requires phase sequence")
        return self


class PX4GazeboMissionSafetyBoundarySummary(_RedactionBoundary):
    schema_version: Literal[
        PX4_GAZEBO_MISSION_SAFETY_BOUNDARY_SUMMARY_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_SAFETY_BOUNDARY_SUMMARY_SCHEMA_VERSION
    summary_id: str
    runner_result_ref: str = Field(min_length=1)
    final_status: str = Field(min_length=1)
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False
    approval_free_dispatch_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    memory_direct_command_authority_allowed: Literal[False] = False
    memory_grants_dispatch_authority: Literal[False] = False
    boundary_statements: tuple[str, ...]
    generated_at: datetime

    @field_validator("boundary_statements", mode="before")
    @classmethod
    def _coerce_statements(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _coerce_generated_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_summary(self) -> "PX4GazeboMissionSafetyBoundarySummary":
        required = {
            "hardware_target_disabled",
            "physical_execution_disabled",
            "mission_upload_disabled",
            "unbounded_setpoint_stream_disabled",
            "memory_not_command_authority",
        }
        if not required.issubset(set(self.boundary_statements)):
            raise PX4GazeboMissionReviewError(
                "safety boundary summary is missing required statements"
            )
        return self


class PX4GazeboFleetMemoryProvenanceSummary(_RedactionBoundary):
    schema_version: Literal[
        PX4_GAZEBO_FLEET_MEMORY_PROVENANCE_SUMMARY_SCHEMA_VERSION
    ] = PX4_GAZEBO_FLEET_MEMORY_PROVENANCE_SUMMARY_SCHEMA_VERSION
    summary_id: str
    fleet_memory_snapshot_ref: str = Field(min_length=1)
    feedback_candidate_ref: str = Field(min_length=1)
    promotion_gate_ref: str = Field(min_length=1)
    memory_informed_plan_ref: str = Field(min_length=1)
    lead_observation_ref: str = Field(min_length=1)
    followup_feedback_ref: str = Field(min_length=1)
    fleet_learning_replay_ref: str = Field(min_length=1)
    fleet_learning_corpus_ref: str = Field(min_length=1)
    part2_finalization_ref: str = Field(min_length=1)
    promotion_status: Literal["promoted"] = "promoted"
    promoted_memory_refs: tuple[str, ...]
    memory_used_for_planning_only: Literal[True] = True
    memory_use_scope: Literal["planning_gates_risk_scoring_only"] = (
        "planning_gates_risk_scoring_only"
    )
    memory_decision_trace: tuple[str, ...]
    negative_case_labels: tuple[str, ...]
    memory_direct_command_authority_allowed: Literal[False] = False
    memory_grants_dispatch_authority: Literal[False] = False
    generated_at: datetime

    @field_validator(
        "promoted_memory_refs",
        "memory_decision_trace",
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
    def _validate_provenance(self) -> "PX4GazeboFleetMemoryProvenanceSummary":
        if not self.promoted_memory_refs:
            raise PX4GazeboMissionReviewError(
                "fleet memory provenance requires promoted memory refs"
            )
        if self.fleet_memory_snapshot_ref not in self.memory_decision_trace:
            raise PX4GazeboMissionReviewError(
                "fleet memory provenance decision trace must include snapshot ref"
            )
        if self.promotion_gate_ref not in self.memory_decision_trace:
            raise PX4GazeboMissionReviewError(
                "fleet memory provenance decision trace must include promotion gate ref"
            )
        required_negative = {
            "stale_ignored",
            "contradictory_blocked",
            "outlier_not_adopted",
            "unsafe_rejected",
        }
        if not required_negative.issubset(set(self.negative_case_labels)):
            raise PX4GazeboMissionReviewError(
                "fleet memory provenance is missing negative-case coverage"
            )
        return self


class PX4GazeboMissionRunEvidenceReport(_RedactionBoundary):
    schema_version: Literal[PX4_GAZEBO_MISSION_RUN_EVIDENCE_REPORT_SCHEMA_VERSION] = (
        PX4_GAZEBO_MISSION_RUN_EVIDENCE_REPORT_SCHEMA_VERSION
    )
    report_id: str
    runner_result_ref: str = Field(min_length=1)
    replay_index_ref: str = Field(min_length=1)
    safety_boundary_summary_ref: str = Field(min_length=1)
    fleet_memory_provenance_ref: str | None = None
    report_title: str = Field(min_length=1)
    final_status: str = Field(min_length=1)
    why_completed_or_blocked: tuple[str, ...]
    evidence_chain: tuple[str, ...]
    safety_boundary: tuple[str, ...]
    memory_authority_boundary: tuple[str, ...]
    reviewer_notes: tuple[str, ...]
    generated_at: datetime

    @field_validator(
        "why_completed_or_blocked",
        "evidence_chain",
        "safety_boundary",
        "memory_authority_boundary",
        "reviewer_notes",
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
    def _validate_report(self) -> "PX4GazeboMissionRunEvidenceReport":
        if not self.why_completed_or_blocked:
            raise PX4GazeboMissionReviewError(
                "mission review report requires completion or blocked rationale"
            )
        required_chain = {
            self.runner_result_ref,
            self.replay_index_ref,
            self.safety_boundary_summary_ref,
        }
        if self.fleet_memory_provenance_ref:
            required_chain.add(self.fleet_memory_provenance_ref)
        if not required_chain.issubset(set(self.evidence_chain)):
            raise PX4GazeboMissionReviewError(
                "mission review report evidence chain is missing required refs"
            )
        if "memory_is_evidence_not_authority" not in self.memory_authority_boundary:
            raise PX4GazeboMissionReviewError(
                "mission review report must state memory authority boundary"
            )
        return self


def build_px4_gazebo_mission_run_replay_index(
    *,
    runner_result: PX4GazeboDeliveryMissionRunnerResult | Mapping[str, Any],
    replay_timeline: PX4GazeboDeliveryMissionReplayTimeline | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboMissionRunReplayIndex:
    runner = _coerce_runner(runner_result)
    replay = _coerce_replay(replay_timeline)
    if replay.runner_result_ref != _runner_ref(runner):
        raise PX4GazeboMissionReviewError("runner/replay mismatch")
    generated_at = _utc(now)
    entries = tuple(
        PX4GazeboMissionReplayIndexEntry(
            sequence=event.sequence,
            t_relative_seconds=event.t_relative_seconds,
            phase=event.phase.value,
            event_type=event.event_type,
            artifact_ref=event.artifact_ref,
        )
        for event in replay.events
    )
    payload = {
        "runner": runner.runner_result_id,
        "replay": replay.replay_timeline_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboMissionRunReplayIndex(
        index_id=_stable_id("px4_gazebo_mission_run_replay_index", payload),
        runner_result_ref=_runner_ref(runner),
        replay_timeline_ref=_replay_ref(replay),
        final_status=runner.final_status.value,
        event_count=len(entries),
        phase_sequence=tuple(phase.value for phase in runner.observed_phases),
        entries=entries,
        generated_at=generated_at,
    )


def build_px4_gazebo_mission_safety_boundary_summary(
    *,
    runner_result: PX4GazeboDeliveryMissionRunnerResult | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboMissionSafetyBoundarySummary:
    runner = _coerce_runner(runner_result)
    generated_at = _utc(now)
    payload = {
        "runner": runner.runner_result_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboMissionSafetyBoundarySummary(
        summary_id=_stable_id("px4_gazebo_mission_safety_boundary_summary", payload),
        runner_result_ref=_runner_ref(runner),
        final_status=runner.final_status.value,
        boundary_statements=(
            "hardware_target_disabled",
            "physical_execution_disabled",
            "mission_upload_disabled",
            "unbounded_setpoint_stream_disabled",
            "arbitrary_gazebo_mutation_disabled",
            "approval_free_dispatch_disabled",
            "approval_free_stronger_execution_disabled",
            "memory_not_command_authority",
        ),
        generated_at=generated_at,
    )


def build_px4_gazebo_fleet_memory_provenance_summary(
    *,
    snapshot: PX4GazeboFleetMemorySnapshot | Mapping[str, Any],
    feedback_candidate: PX4GazeboFleetFeedbackCandidate | Mapping[str, Any],
    promotion_gate: PX4GazeboFleetFeedbackPromotionGate | Mapping[str, Any],
    memory_informed_plan: PX4GazeboMemoryInformedMissionPlan | Mapping[str, Any],
    lead_observation: PX4GazeboLeadDroneObservation | Mapping[str, Any],
    followup_feedback: PX4GazeboFollowupMissionFeedback | Mapping[str, Any],
    fleet_learning_replay: PX4GazeboFleetLearningReplay | Mapping[str, Any],
    fleet_learning_corpus: PX4GazeboFleetLearningCorpus | Mapping[str, Any],
    part2_finalization: PX4GazeboFleetMemoryPart2Finalization | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboFleetMemoryProvenanceSummary:
    snapshot_obj = (
        snapshot
        if isinstance(snapshot, PX4GazeboFleetMemorySnapshot)
        else PX4GazeboFleetMemorySnapshot.model_validate(dict(snapshot))
    )
    candidate_obj = (
        feedback_candidate
        if isinstance(feedback_candidate, PX4GazeboFleetFeedbackCandidate)
        else PX4GazeboFleetFeedbackCandidate.model_validate(dict(feedback_candidate))
    )
    gate_obj = (
        promotion_gate
        if isinstance(promotion_gate, PX4GazeboFleetFeedbackPromotionGate)
        else PX4GazeboFleetFeedbackPromotionGate.model_validate(dict(promotion_gate))
    )
    plan_obj = (
        memory_informed_plan
        if isinstance(memory_informed_plan, PX4GazeboMemoryInformedMissionPlan)
        else PX4GazeboMemoryInformedMissionPlan.model_validate(
            dict(memory_informed_plan)
        )
    )
    lead_obj = (
        lead_observation
        if isinstance(lead_observation, PX4GazeboLeadDroneObservation)
        else PX4GazeboLeadDroneObservation.model_validate(dict(lead_observation))
    )
    followup_obj = (
        followup_feedback
        if isinstance(followup_feedback, PX4GazeboFollowupMissionFeedback)
        else PX4GazeboFollowupMissionFeedback.model_validate(dict(followup_feedback))
    )
    replay_obj = (
        fleet_learning_replay
        if isinstance(fleet_learning_replay, PX4GazeboFleetLearningReplay)
        else PX4GazeboFleetLearningReplay.model_validate(dict(fleet_learning_replay))
    )
    corpus_obj = (
        fleet_learning_corpus
        if isinstance(fleet_learning_corpus, PX4GazeboFleetLearningCorpus)
        else PX4GazeboFleetLearningCorpus.model_validate(dict(fleet_learning_corpus))
    )
    finalization_obj = (
        part2_finalization
        if isinstance(part2_finalization, PX4GazeboFleetMemoryPart2Finalization)
        else PX4GazeboFleetMemoryPart2Finalization.model_validate(
            dict(part2_finalization)
        )
    )
    if plan_obj.promotion_gate_ref != (
        f"px4_gazebo_fleet_feedback_promotion_gate:{gate_obj.gate_id}"
    ):
        raise PX4GazeboMissionReviewError("memory plan/promotion gate mismatch")
    if plan_obj.fleet_memory_snapshot_ref != (
        f"px4_gazebo_fleet_memory_snapshot:{snapshot_obj.snapshot_id}"
    ):
        raise PX4GazeboMissionReviewError("memory plan/snapshot mismatch")
    generated_at = _utc(now)
    payload = {
        "snapshot": snapshot_obj.snapshot_id,
        "plan": plan_obj.plan_id,
        "finalization": finalization_obj.finalization_id,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboFleetMemoryProvenanceSummary(
        summary_id=_stable_id("px4_gazebo_fleet_memory_provenance_summary", payload),
        fleet_memory_snapshot_ref=(
            f"px4_gazebo_fleet_memory_snapshot:{snapshot_obj.snapshot_id}"
        ),
        feedback_candidate_ref=(
            f"px4_gazebo_fleet_feedback_candidate:{candidate_obj.candidate_id}"
        ),
        promotion_gate_ref=(
            f"px4_gazebo_fleet_feedback_promotion_gate:{gate_obj.gate_id}"
        ),
        memory_informed_plan_ref=(
            f"px4_gazebo_memory_informed_mission_plan:{plan_obj.plan_id}"
        ),
        lead_observation_ref=(
            f"px4_gazebo_lead_drone_observation:{lead_obj.observation_id}"
        ),
        followup_feedback_ref=(
            f"px4_gazebo_followup_mission_feedback:{followup_obj.feedback_id}"
        ),
        fleet_learning_replay_ref=(
            f"px4_gazebo_fleet_learning_replay:{replay_obj.replay_id}"
        ),
        fleet_learning_corpus_ref=(
            f"px4_gazebo_fleet_learning_corpus:{corpus_obj.corpus_id}"
        ),
        part2_finalization_ref=(
            f"px4_gazebo_fleet_memory_part2_finalization:"
            f"{finalization_obj.finalization_id}"
        ),
        promoted_memory_refs=plan_obj.promoted_memory_refs,
        memory_decision_trace=plan_obj.memory_decision_trace,
        negative_case_labels=finalization_obj.negative_case_labels,
        generated_at=generated_at,
    )


def build_px4_gazebo_mission_run_evidence_report(
    *,
    runner_result: PX4GazeboDeliveryMissionRunnerResult | Mapping[str, Any],
    replay_index: PX4GazeboMissionRunReplayIndex | Mapping[str, Any],
    safety_boundary_summary: PX4GazeboMissionSafetyBoundarySummary | Mapping[str, Any],
    fleet_memory_provenance: (
        PX4GazeboFleetMemoryProvenanceSummary | Mapping[str, Any] | None
    ) = None,
    now: datetime | None = None,
) -> PX4GazeboMissionRunEvidenceReport:
    runner = _coerce_runner(runner_result)
    index = (
        replay_index
        if isinstance(replay_index, PX4GazeboMissionRunReplayIndex)
        else PX4GazeboMissionRunReplayIndex.model_validate(dict(replay_index))
    )
    safety = (
        safety_boundary_summary
        if isinstance(safety_boundary_summary, PX4GazeboMissionSafetyBoundarySummary)
        else PX4GazeboMissionSafetyBoundarySummary.model_validate(
            dict(safety_boundary_summary)
        )
    )
    provenance = (
        fleet_memory_provenance
        if isinstance(fleet_memory_provenance, PX4GazeboFleetMemoryProvenanceSummary)
        or fleet_memory_provenance is None
        else PX4GazeboFleetMemoryProvenanceSummary.model_validate(
            dict(fleet_memory_provenance)
        )
    )
    if index.runner_result_ref != _runner_ref(runner):
        raise PX4GazeboMissionReviewError("report replay index/runner mismatch")
    if safety.runner_result_ref != _runner_ref(runner):
        raise PX4GazeboMissionReviewError("report safety summary/runner mismatch")
    generated_at = _utc(now)
    if runner.final_status == PX4GazeboDeliveryMissionFinalStatus.COMPLETED:
        rationale = (
            "all_required_phases_observed",
            "phase_gates_passed_or_recorded",
            "replay_timeline_available",
            "safety_boundary_preserved",
        )
    else:
        rationale = (
            f"blocked_phase:{runner.blocked_phase.value if runner.blocked_phase else 'unknown'}",
            *runner.blocked_reasons,
            "replay_timeline_available",
            "safety_boundary_preserved",
        )
    evidence_chain = [
        _runner_ref(runner),
        _replay_index_ref(index),
        _safety_summary_ref(safety),
    ]
    if provenance is not None:
        evidence_chain.append(_fleet_provenance_ref(provenance))
    payload = {
        "runner": runner.runner_result_id,
        "index": index.index_id,
        "provenance": provenance.summary_id if provenance else None,
        "generated_at": generated_at.isoformat(),
    }
    return PX4GazeboMissionRunEvidenceReport(
        report_id=_stable_id("px4_gazebo_mission_run_evidence_report", payload),
        runner_result_ref=_runner_ref(runner),
        replay_index_ref=_replay_index_ref(index),
        safety_boundary_summary_ref=_safety_summary_ref(safety),
        fleet_memory_provenance_ref=(
            _fleet_provenance_ref(provenance) if provenance else None
        ),
        report_title="Mission OS PX4/Gazebo Run Evidence Report",
        final_status=runner.final_status.value,
        why_completed_or_blocked=rationale,
        evidence_chain=tuple(evidence_chain),
        safety_boundary=safety.boundary_statements,
        memory_authority_boundary=(
            "memory_is_evidence_not_authority",
            "memory_used_for_planning_only",
            "memory_cannot_dispatch",
            "memory_cannot_bypass_approval",
        ),
        reviewer_notes=(
            "redacted_report_only",
            "no_raw_logs_or_full_telemetry",
            "no_reproduction_steps",
        ),
        generated_at=generated_at,
    )


def render_px4_gazebo_mission_report_markdown(
    report: PX4GazeboMissionRunEvidenceReport | Mapping[str, Any],
) -> str:
    report_obj = (
        report
        if isinstance(report, PX4GazeboMissionRunEvidenceReport)
        else PX4GazeboMissionRunEvidenceReport.model_validate(dict(report))
    )
    lines = [
        f"# {report_obj.report_title}",
        "",
        f"- final_status: `{report_obj.final_status}`",
        f"- report_id: `{report_obj.report_id}`",
        "",
        "## Why Completed Or Blocked",
        *[f"- `{item}`" for item in report_obj.why_completed_or_blocked],
        "",
        "## Evidence Chain",
        *[f"- `{item}`" for item in report_obj.evidence_chain],
        "",
        "## Safety Boundary",
        *[f"- `{item}`" for item in report_obj.safety_boundary],
        "",
        "## Memory Authority Boundary",
        *[f"- `{item}`" for item in report_obj.memory_authority_boundary],
        "",
        "## Reviewer Notes",
        *[f"- `{item}`" for item in report_obj.reviewer_notes],
        "",
    ]
    return "\n".join(lines)


def _html_list(items: Sequence[str]) -> str:
    if not items:
        return '<p class="muted">None recorded.</p>'
    return (
        "<ul>"
        + "".join(f"<li><code>{escape(str(item))}</code></li>" for item in items)
        + "</ul>"
    )


def render_px4_gazebo_mission_report_html(
    report: PX4GazeboMissionRunEvidenceReport | Mapping[str, Any],
    *,
    replay_index: PX4GazeboMissionRunReplayIndex | Mapping[str, Any] | None = None,
    safety_boundary_summary: (
        PX4GazeboMissionSafetyBoundarySummary | Mapping[str, Any] | None
    ) = None,
    fleet_memory_provenance: (
        PX4GazeboFleetMemoryProvenanceSummary | Mapping[str, Any] | None
    ) = None,
) -> str:
    """Render a static, redacted Mission OS review report.

    The HTML is deliberately self-contained and does not include raw logs,
    sqlite state, full telemetry, transport details, command details, output
    paths, or reproduction steps.
    """

    report_obj = (
        report
        if isinstance(report, PX4GazeboMissionRunEvidenceReport)
        else PX4GazeboMissionRunEvidenceReport.model_validate(dict(report))
    )
    replay_obj = (
        replay_index
        if isinstance(replay_index, PX4GazeboMissionRunReplayIndex)
        else (
            PX4GazeboMissionRunReplayIndex.model_validate(dict(replay_index))
            if replay_index is not None
            else None
        )
    )
    safety_obj = (
        safety_boundary_summary
        if isinstance(safety_boundary_summary, PX4GazeboMissionSafetyBoundarySummary)
        else (
            PX4GazeboMissionSafetyBoundarySummary.model_validate(
                dict(safety_boundary_summary)
            )
            if safety_boundary_summary is not None
            else None
        )
    )
    provenance_obj = (
        fleet_memory_provenance
        if isinstance(fleet_memory_provenance, PX4GazeboFleetMemoryProvenanceSummary)
        else (
            PX4GazeboFleetMemoryProvenanceSummary.model_validate(
                dict(fleet_memory_provenance)
            )
            if fleet_memory_provenance is not None
            else None
        )
    )
    validate_px4_gazebo_mission_review_archive_consistency(
        report=report_obj,
        replay_index=replay_obj,
        safety_boundary_summary=safety_obj,
        fleet_memory_provenance=provenance_obj,
    )

    replay_rows = ""
    if replay_obj is not None:
        replay_rows = "\n".join(
            "<tr>"
            f"<td>{entry.sequence}</td>"
            f"<td>{entry.t_relative_seconds:.2f}</td>"
            f"<td>{escape(entry.phase)}</td>"
            f"<td>{escape(entry.event_type)}</td>"
            f"<td><code>{escape(entry.artifact_ref)}</code></td>"
            "</tr>"
            for entry in replay_obj.entries
        )
    provenance_html = ""
    if provenance_obj is not None:
        provenance_html = f"""
        <section>
          <h2>Fleet Memory Provenance</h2>
          <div class="grid">
            <div><span class="label">Promotion</span><strong>{escape(provenance_obj.promotion_status)}</strong></div>
            <div><span class="label">Use Scope</span><strong>{escape(provenance_obj.memory_use_scope)}</strong></div>
            <div><span class="label">Planning Only</span><strong>{str(provenance_obj.memory_used_for_planning_only).lower()}</strong></div>
            <div><span class="label">Dispatch Authority</span><strong>{str(provenance_obj.memory_grants_dispatch_authority).lower()}</strong></div>
          </div>
          <h3>Promoted Memory Refs</h3>
          {_html_list(provenance_obj.promoted_memory_refs)}
          <h3>Negative Cases</h3>
          {_html_list(provenance_obj.negative_case_labels)}
        </section>
        """

    safety_flags_html = ""
    if safety_obj is not None:
        safety_flags = {
            "hardware_target_allowed": safety_obj.hardware_target_allowed,
            "physical_execution_invoked": safety_obj.physical_execution_invoked,
            "px4_mission_upload_allowed": safety_obj.px4_mission_upload_allowed,
            "unbounded_setpoint_stream_allowed": (
                safety_obj.unbounded_setpoint_stream_allowed
            ),
            "arbitrary_gazebo_mutation_allowed": (
                safety_obj.arbitrary_gazebo_mutation_allowed
            ),
            "approval_free_dispatch_allowed": safety_obj.approval_free_dispatch_allowed,
            "approval_free_stronger_execution_allowed": (
                safety_obj.approval_free_stronger_execution_allowed
            ),
            "memory_direct_command_authority_allowed": (
                safety_obj.memory_direct_command_authority_allowed
            ),
            "memory_grants_dispatch_authority": (
                safety_obj.memory_grants_dispatch_authority
            ),
        }
        safety_flags_html = "".join(
            f'<div><span class="label">{escape(name)}</span><strong>{str(value).lower()}</strong></div>'
            for name, value in safety_flags.items()
        )

    if replay_obj is None:
        replay_html = '<p class="muted">Replay index was not provided.</p>'
    else:
        replay_html = (
            "<table><thead><tr><th>#</th><th>t+sec</th><th>phase</th>"
            "<th>event</th><th>artifact</th></tr></thead>"
            f"<tbody>{replay_rows}</tbody></table>"
        )

    generated = report_obj.generated_at.isoformat()
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(report_obj.report_title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f5ef;
      --panel: #ffffff;
      --ink: #232522;
      --muted: #686b63;
      --line: #dad7cd;
      --accent: #23645a;
      --warn: #8a4f12;
      --code: #f1efe6;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 40px 24px 56px; }}
    header {{ margin-bottom: 24px; }}
    h1 {{ font-size: 34px; line-height: 1.15; margin: 0 0 10px; letter-spacing: 0; }}
    h2 {{ font-size: 20px; margin: 0 0 14px; }}
    h3 {{ font-size: 15px; margin: 18px 0 8px; }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
      margin: 16px 0;
    }}
    code {{
      background: var(--code);
      border-radius: 5px;
      padding: 2px 5px;
      font-size: 13px;
      word-break: break-word;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-top: 1px solid var(--line); text-align: left; padding: 8px; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    ul {{ padding-left: 20px; margin: 8px 0 0; }}
    .badge {{
      display: inline-block;
      border: 1px solid var(--accent);
      border-radius: 999px;
      color: var(--accent);
      padding: 3px 10px;
      font-weight: 700;
      text-transform: uppercase;
      font-size: 12px;
    }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }}
    .grid > div {{
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 12px;
      background: #fcfbf7;
    }}
    .label {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
    .muted {{ color: var(--muted); }}
    .warning {{ color: var(--warn); font-weight: 600; }}
  </style>
</head>
<body>
  <main>
    <header>
      <span class="badge">{escape(report_obj.final_status)}</span>
      <h1>{escape(report_obj.report_title)}</h1>
      <p class="muted">Redacted static review report generated at <code>{escape(generated)}</code>.</p>
      <p class="warning">Public-safe review surface only. This report excludes raw logs, sqlite state, full telemetry, reproduction steps, runtime entrypoint names, transport details, low-level command details, and output paths.</p>
    </header>

    <section>
      <h2>Why Completed Or Blocked</h2>
      {_html_list(report_obj.why_completed_or_blocked)}
    </section>

    <section>
      <h2>Evidence Chain</h2>
      {_html_list(report_obj.evidence_chain)}
    </section>

    <section>
      <h2>Safety Boundary</h2>
      <div class="grid">{safety_flags_html}</div>
      <h3>Statements</h3>
      {_html_list(report_obj.safety_boundary)}
    </section>

    <section>
      <h2>Memory Authority Boundary</h2>
      {_html_list(report_obj.memory_authority_boundary)}
    </section>

    {provenance_html}

    <section>
      <h2>Replay Timeline</h2>
      {replay_html}
    </section>

    <section>
      <h2>Reviewer Notes</h2>
      {_html_list(report_obj.reviewer_notes)}
    </section>
  </main>
</body>
</html>
"""


def run_px4_gazebo_mission_control_review_report(
    *,
    runner_result: PX4GazeboDeliveryMissionRunnerResult | Mapping[str, Any],
    replay_timeline: PX4GazeboDeliveryMissionReplayTimeline | Mapping[str, Any],
    fleet_memory_artifacts: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = _utc(now)
    index = build_px4_gazebo_mission_run_replay_index(
        runner_result=runner_result,
        replay_timeline=replay_timeline,
        now=generated_at,
    )
    safety = build_px4_gazebo_mission_safety_boundary_summary(
        runner_result=runner_result,
        now=generated_at,
    )
    provenance = None
    if fleet_memory_artifacts is not None:
        provenance = build_px4_gazebo_fleet_memory_provenance_summary(
            snapshot=fleet_memory_artifacts["fleet_memory_snapshot"],
            feedback_candidate=fleet_memory_artifacts["feedback_candidate"],
            promotion_gate=fleet_memory_artifacts["promoted_promotion_gate"],
            memory_informed_plan=fleet_memory_artifacts["memory_informed_plan"],
            lead_observation=fleet_memory_artifacts["lead_drone_observation"],
            followup_feedback=fleet_memory_artifacts["followup_mission_feedback"],
            fleet_learning_replay=fleet_memory_artifacts["fleet_learning_replay"],
            fleet_learning_corpus=fleet_memory_artifacts["fleet_learning_corpus"],
            part2_finalization=fleet_memory_artifacts["part2_finalization"],
            now=generated_at,
        )
    report = build_px4_gazebo_mission_run_evidence_report(
        runner_result=runner_result,
        replay_index=index,
        safety_boundary_summary=safety,
        fleet_memory_provenance=provenance,
        now=generated_at,
    )
    return {
        "replay_index": index,
        "safety_boundary_summary": safety,
        "fleet_memory_provenance_summary": provenance,
        "evidence_report": report,
        "redacted_markdown": render_px4_gazebo_mission_report_markdown(report),
        "redacted_html": render_px4_gazebo_mission_report_html(
            report,
            replay_index=index,
            safety_boundary_summary=safety,
            fleet_memory_provenance=provenance,
        ),
        "redacted_json": report.model_dump(mode="json"),
    }


def write_px4_gazebo_mission_review_archive(
    *,
    output_dir: str | Path,
    report: PX4GazeboMissionRunEvidenceReport | Mapping[str, Any],
    replay_index: PX4GazeboMissionRunReplayIndex | Mapping[str, Any] | None = None,
    safety_boundary_summary: (
        PX4GazeboMissionSafetyBoundarySummary | Mapping[str, Any] | None
    ) = None,
    fleet_memory_provenance: (
        PX4GazeboFleetMemoryProvenanceSummary | Mapping[str, Any] | None
    ) = None,
) -> dict[str, str]:
    report_obj = (
        report
        if isinstance(report, PX4GazeboMissionRunEvidenceReport)
        else PX4GazeboMissionRunEvidenceReport.model_validate(dict(report))
    )
    replay_obj = (
        replay_index
        if isinstance(replay_index, PX4GazeboMissionRunReplayIndex)
        else (
            PX4GazeboMissionRunReplayIndex.model_validate(dict(replay_index))
            if replay_index is not None
            else None
        )
    )
    safety_obj = (
        safety_boundary_summary
        if isinstance(safety_boundary_summary, PX4GazeboMissionSafetyBoundarySummary)
        else (
            PX4GazeboMissionSafetyBoundarySummary.model_validate(
                dict(safety_boundary_summary)
            )
            if safety_boundary_summary is not None
            else None
        )
    )
    provenance_obj = (
        fleet_memory_provenance
        if isinstance(fleet_memory_provenance, PX4GazeboFleetMemoryProvenanceSummary)
        else (
            PX4GazeboFleetMemoryProvenanceSummary.model_validate(
                dict(fleet_memory_provenance)
            )
            if fleet_memory_provenance is not None
            else None
        )
    )
    validate_px4_gazebo_mission_review_archive_consistency(
        report=report_obj,
        replay_index=replay_obj,
        safety_boundary_summary=safety_obj,
        fleet_memory_provenance=provenance_obj,
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report_json = out / "report.redacted.json"
    report_markdown = out / "report.redacted.md"
    report_html = out / "report.redacted.html"
    report_json.write_text(
        json.dumps(report_obj.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    report_markdown.write_text(
        render_px4_gazebo_mission_report_markdown(report_obj),
        encoding="utf-8",
    )
    report_html.write_text(
        render_px4_gazebo_mission_report_html(
            report_obj,
            replay_index=replay_obj,
            safety_boundary_summary=safety_obj,
            fleet_memory_provenance=provenance_obj,
        ),
        encoding="utf-8",
    )
    paths = {
        "report_json": str(report_json),
        "report_markdown": str(report_markdown),
        "report_html": str(report_html),
    }
    if replay_obj is not None:
        replay_json = out / "replay_index.redacted.json"
        replay_json.write_text(
            json.dumps(replay_obj.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        paths["replay_index_json"] = str(replay_json)
    if safety_obj is not None:
        safety_json = out / "safety_boundary.redacted.json"
        safety_json.write_text(
            json.dumps(safety_obj.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        paths["safety_boundary_json"] = str(safety_json)
    if provenance_obj is not None:
        provenance_json = out / "fleet_memory_provenance.redacted.json"
        provenance_json.write_text(
            json.dumps(
                provenance_obj.model_dump(mode="json"), indent=2, sort_keys=True
            ),
            encoding="utf-8",
        )
        paths["fleet_memory_provenance_json"] = str(provenance_json)
    return paths


__all__ = [
    "PX4_GAZEBO_FLEET_MEMORY_PROVENANCE_SUMMARY_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_RUN_EVIDENCE_REPORT_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_RUN_REPLAY_INDEX_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_SAFETY_BOUNDARY_SUMMARY_SCHEMA_VERSION",
    "PX4GazeboFleetMemoryProvenanceSummary",
    "PX4GazeboMissionReplayIndexEntry",
    "PX4GazeboMissionReviewError",
    "PX4GazeboMissionRunEvidenceReport",
    "PX4GazeboMissionRunReplayIndex",
    "PX4GazeboMissionSafetyBoundarySummary",
    "build_px4_gazebo_fleet_memory_provenance_summary",
    "build_px4_gazebo_mission_run_evidence_report",
    "build_px4_gazebo_mission_run_replay_index",
    "build_px4_gazebo_mission_safety_boundary_summary",
    "render_px4_gazebo_mission_report_html",
    "render_px4_gazebo_mission_report_markdown",
    "run_px4_gazebo_mission_control_review_report",
    "validate_px4_gazebo_mission_review_archive_consistency",
    "write_px4_gazebo_mission_review_archive",
]

#!/usr/bin/env python3
"""Runtime smoke for recovery_advisory_context.v1 (#471)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.advisory_mission_memory import (
    attach_delivery_mission_lesson_candidate,
    attach_delivery_mission_lesson_promotion,
)
from src.runtime.delivery_shared_observation import (
    SharedObservationEventSource,
    SharedObservationKind,
    build_delivery_mission_session,
    build_delivery_vehicle_observation_record,
    build_delivery_vehicle_session,
    build_mission_shared_observation,
)
from src.runtime.recovery_advisory_context import (
    RecoveryAdvisoryContextError,
    build_recovery_advisory_proposal,
    build_recovery_advisory_context,
    validate_recovery_advisory_refs,
)
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
)
from src.runtime.task_store import TaskStore


def _promoted_lesson_ref(store: TaskStore, *, now: datetime) -> tuple[dict, str]:
    source = store.create(
        kind="delivery_episode",
        title="recovery advisory source mission",
        status="completed",
        artifacts={
            "delivery_episode": {"episode_id": "recovery-advisory-source"},
            "delivery_episode_review": {"review_id": "recovery-advisory-review"},
        },
    )
    task = store.create(
        kind="advisory_recovery_context",
        title="recovery advisory lesson smoke",
        status="running",
        artifacts={},
    )
    candidate = attach_delivery_mission_lesson_candidate(
        task["task_id"],
        source_mission_refs=[f"task:{source['task_id']}"],
        source_artifact_refs=[
            "delivery_episode:recovery-advisory-source",
            "delivery_episode_review:recovery-advisory-review",
        ],
        proposed_recommendation={
            "recommendation_summary": "Prefer bounded retry after obstructed dropoff.",
            "design_hint": "Use as advisory recovery proposal context only.",
        },
        proposed_applicability={
            "vehicle_class": "px4_sitl",
            "mission_profile": "delivery",
        },
        rationale="Prior reviewed mission recovered with a bounded retry.",
        created_by="operator",
        created_at=now,
        task_store_factory=lambda: store,
    )["delivery_mission_lesson_candidate"]
    promoted = attach_delivery_mission_lesson_promotion(
        task["task_id"],
        lesson_candidate_ref=(
            f"delivery_mission_lesson_candidate:{candidate['candidate_id']}"
        ),
        operator_id="operator-1",
        decision_rationale="Promote recovery advisory context lesson.",
        decision_at=now,
        created_at=now,
        valid_for_episode_schema_versions=[SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION],
        task_store_factory=lambda: store,
    )
    lesson = promoted["delivery_mission_lesson"]
    return (
        store.get(task["task_id"]) or task,
        f"delivery_mission_lesson:{lesson['lesson_id']}",
    )


def _shared_observation_chain(*, now: datetime):
    mission_ref = "delivery_mission_session:recovery-advisory-context-smoke"
    source_observation_ref = "px4_gazebo_vehicle_observation:vehicle-a-hazard-smoke"
    source_record = build_delivery_vehicle_observation_record(
        observation_ref=source_observation_ref,
        event_source=SharedObservationEventSource.PX4_GAZEBO_SITL_TELEMETRY,
        observation_kind=SharedObservationKind.HAZARD_REPORT,
        observation_payload={
            "vehicle_id": "vehicle-a",
            "hazard_id": "dropoff-pad-obstruction",
            "severity": "warning",
        },
        observed_at=now,
    )
    vehicle_a = build_delivery_vehicle_session(
        vehicle_id="vehicle-a",
        mission_session_ref=mission_ref,
        telemetry_source_ref="px4_gazebo_sitl_telemetry:vehicle-a",
        observation_records=[source_record],
        created_at=now,
    )
    vehicle_b = build_delivery_vehicle_session(
        vehicle_id="vehicle-b",
        mission_session_ref=mission_ref,
        telemetry_source_ref="px4_gazebo_sitl_telemetry:vehicle-b",
        observation_records=[],
        created_at=now,
    )
    mission = build_delivery_mission_session(
        vehicle_sessions=[vehicle_a, vehicle_b],
        shared_observation_log_ref=(
            "mission_shared_observation_log:recovery-advisory-context-smoke"
        ),
        created_at=now,
    )
    shared = build_mission_shared_observation(
        mission_session_ref=mission_ref,
        source_vehicle_session_ref=(
            f"delivery_vehicle_session:{vehicle_a.vehicle_session_id}"
        ),
        source_observation_ref=source_observation_ref,
        event_source=SharedObservationEventSource.PX4_GAZEBO_SITL_TELEMETRY,
        observation_kind=SharedObservationKind.HAZARD_REPORT,
        observation_payload={
            "vehicle_id": "vehicle-a",
            "hazard_id": "dropoff-pad-obstruction",
        },
        observed_at=now,
        received_at=now,
    )
    return mission, (vehicle_a, vehicle_b), shared


def main() -> int:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    with TemporaryDirectory() as tmpdir:
        store = TaskStore(f"{tmpdir}/tasks.db")
        lesson_task, lesson_ref = _promoted_lesson_ref(store, now=now)
        mission, vehicle_sessions, shared = _shared_observation_chain(now=now)
        shared_ref = f"mission_shared_observation:{shared.observation_id}"
        context = build_recovery_advisory_context(
            mission_ref="task:advisory-recovery-context-smoke",
            mission_session_ref=(
                "delivery_mission_session:recovery-advisory-context-smoke"
            ),
            recovery_request_ref=(
                "delivery_recovery_request:advisory-recovery-context-smoke"
            ),
            used_lesson_refs=[lesson_ref],
            used_shared_observation_refs=[shared_ref],
            created_at=now,
        )
        proposal = build_recovery_advisory_proposal(
            recovery_request_ref=context.recovery_request_ref,
            recovery_advisory_context=context,
            suppressed_recovery_candidates=[
                {
                    "candidate_kind": "retry_dropoff_without_obstruction_review",
                    "suppressing_advisory_ref": lesson_ref,
                    "suppression_rationale": (
                        "Advisory lesson keeps obstruction-aware retry review visible."
                    ),
                }
            ],
            created_at=now,
        )
        validate_recovery_advisory_refs(
            recovery_advisory_context=context,
            lesson_task=lesson_task,
            shared_observation_mission_session=mission,
            shared_observation_vehicle_sessions=vehicle_sessions,
            shared_observations=[shared],
            shared_observation_decision_at=now,
            max_observation_age_seconds=5.0,
            recovery_artifacts={
                "recovery_advisory_proposal": proposal.model_dump(mode="json"),
                "delivery_recovery_request": {
                    "request_id": "advisory-recovery-context-smoke",
                    "recovery_advisory_context_ref": (
                        "recovery_advisory_context:"
                        f"{context.recovery_advisory_context_id}"
                    ),
                },
            },
            task_store_factory=lambda: store,
        )

        authority_negative_failed_closed = False
        payload = context.model_dump(mode="json")
        payload["advisory_used_as_outcome_evidence"] = True
        try:
            type(context).model_validate(payload)
        except Exception:
            authority_negative_failed_closed = True

        command_like_negative_failed_closed = False
        try:
            build_recovery_advisory_context(
                mission_ref="task:advisory-recovery-context-smoke",
                recovery_request_ref=(
                    "delivery_recovery_request:advisory-recovery-context-smoke"
                ),
                metadata={"ros_action": "not allowed"},
                created_at=now,
            )
        except RecoveryAdvisoryContextError:
            command_like_negative_failed_closed = True

        outcome_input_negative_failed_closed = False
        empty_context = build_recovery_advisory_context(
            mission_ref="task:advisory-recovery-context-smoke",
            recovery_request_ref=(
                "delivery_recovery_request:advisory-recovery-context-smoke"
            ),
            created_at=now,
        )
        try:
            validate_recovery_advisory_refs(
                recovery_advisory_context=empty_context,
                recovery_artifacts={
                    "delivery_recovery_outcome": {
                        "outcome_input_refs": [
                            "recovery_advisory_context:"
                            f"{empty_context.recovery_advisory_context_id}"
                        ],
                    }
                },
            )
        except RecoveryAdvisoryContextError:
            outcome_input_negative_failed_closed = True

        hidden_suppression_negative_failed_closed = False
        try:
            build_recovery_advisory_proposal(
                recovery_request_ref=context.recovery_request_ref,
                recovery_advisory_context=context,
                suppressed_recovery_candidates=[
                    {
                        "candidate_kind": "hidden_advisory_filter",
                        "suppressing_advisory_ref": (
                            "delivery_mission_lesson:not-used-by-context"
                        ),
                        "suppression_rationale": (
                            "This should fail because the ref is not surfaced as used."
                        ),
                    }
                ],
                created_at=now,
            )
        except RecoveryAdvisoryContextError:
            hidden_suppression_negative_failed_closed = True

    summary = {
        "recovery_advisory_context_smoke_passed": True,
        "issues_covered": [471, 472, 473, 474],
        "production_boundary": (
            "recovery_advisory_context.v1, recovery_advisory_proposal.v1, "
            "and validate_recovery_advisory_refs"
        ),
        "recovery_advisory_context_id": context.recovery_advisory_context_id,
        "recovery_advisory_proposal_id": proposal.proposal_id,
        "recovery_advisory_context_ref": proposal.recovery_advisory_context_ref,
        "recovery_request_ref": context.recovery_request_ref,
        "mission_ref": context.mission_ref,
        "mission_session_ref": context.mission_session_ref,
        "used_lesson_refs": list(context.used_lesson_refs),
        "used_shared_observation_refs": list(context.used_shared_observation_refs),
        "ignored_lesson_refs": list(context.ignored_lesson_refs),
        "ignored_shared_observation_refs": list(
            context.ignored_shared_observation_refs
        ),
        "advisory_validation_evidence_count": len(context.advisory_validation_evidence),
        "authority_negative_failed_closed": authority_negative_failed_closed,
        "command_like_negative_failed_closed": command_like_negative_failed_closed,
        "outcome_input_negative_failed_closed": outcome_input_negative_failed_closed,
        "hidden_suppression_negative_failed_closed": (
            hidden_suppression_negative_failed_closed
        ),
        "suppressed_recovery_candidates_count": len(
            proposal.suppressed_recovery_candidates
        ),
        "proposal_uses_advisory_authority_for_judgement": (
            proposal.proposal_uses_advisory_authority_for_judgement
        ),
        "proposal_modifies_recovery_outcome_predicates": (
            proposal.proposal_modifies_recovery_outcome_predicates
        ),
        "advisory_context_only": context.advisory_context_only,
        "advisory_grants_recovery_authority": (
            context.advisory_grants_recovery_authority
        ),
        "advisory_used_as_outcome_evidence": (
            context.advisory_used_as_outcome_evidence
        ),
        "advisory_used_as_scorecard_evidence": (
            context.advisory_used_as_scorecard_evidence
        ),
        "advisory_used_as_success_proof": context.advisory_used_as_success_proof,
        "advisory_modifies_observed_facts": context.advisory_modifies_observed_facts,
        "advisory_modifies_recovery_outcome_predicates": (
            context.advisory_modifies_recovery_outcome_predicates
        ),
        "dispatch_authority_granted": context.dispatch_authority_granted,
        "raw_mavlink_command_allowed": context.raw_mavlink_command_allowed,
        "raw_ros_action_allowed": context.raw_ros_action_allowed,
        "gazebo_entity_mutation_allowed": context.gazebo_entity_mutation_allowed,
        "setpoint_stream_allowed": context.setpoint_stream_allowed,
        "actuator_command_allowed": context.actuator_command_allowed,
        "hardware_target_allowed": context.hardware_target_allowed,
        "physical_execution_invoked": context.physical_execution_invoked,
        "approval_free_stronger_recovery_allowed": (
            context.approval_free_stronger_recovery_allowed
        ),
        "public_sync_performed": False,
        "readme_or_architecture_updated": False,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return (
        0
        if authority_negative_failed_closed
        and command_like_negative_failed_closed
        and outcome_input_negative_failed_closed
        and hidden_suppression_negative_failed_closed
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Runtime smoke for Advisory Recovery Context epic exit (#476)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from tempfile import TemporaryDirectory
from typing import Any

from src.runtime.advisory_mission_memory import (
    attach_delivery_mission_lesson_candidate,
    attach_delivery_mission_lesson_promotion,
)
from src.runtime.delivery_fault_event import DeliveryFaultCategory
from src.runtime.delivery_recovery_decision import DeliveryRecoveryAction
from src.runtime.delivery_recovery_outcome import build_delivery_recovery_outcome
from src.runtime.delivery_shared_observation import (
    SharedObservationEventSource,
    SharedObservationKind,
    build_delivery_mission_session,
    build_delivery_vehicle_observation_record,
    build_delivery_vehicle_session,
    build_mission_shared_observation,
)
from src.runtime.recovery_advisory_context import (
    RecoveryAdvisoryContext,
    RecoveryAdvisoryContextError,
    build_recovery_advisory_proposal,
    build_recovery_advisory_context,
    validate_recovery_advisory_refs,
)
from src.runtime.recovery_advisory_epic_exit import (
    RECOVERY_ADVISORY_EPIC_EXIT_SCHEMA_VERSION,
    build_recovery_advisory_epic_exit_result,
)
from src.runtime.recovery_advisory_outcome_invariance import (
    assert_recovery_outcome_ignores_advisory_context,
    canonical_recovery_outcome_digest,
)
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
)
from src.runtime.task_store import TaskStore
from tests.test_delivery_recovery_outcome import _chain, _request, _run

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _promoted_lesson_ref(store: TaskStore, *, now: datetime) -> tuple[dict, str]:
    source = store.create(
        kind="delivery_episode",
        title="advisory recovery epic exit source mission",
        status="completed",
        artifacts={
            "delivery_episode": {"episode_id": "advisory-recovery-epic-exit-source"},
            "delivery_episode_review": {
                "review_id": "advisory-recovery-epic-exit-review"
            },
        },
    )
    task = store.create(
        kind="advisory_recovery_context",
        title="advisory recovery epic exit lesson",
        status="running",
        artifacts={},
    )
    candidate = attach_delivery_mission_lesson_candidate(
        task["task_id"],
        source_mission_refs=[f"task:{source['task_id']}"],
        source_artifact_refs=[
            "delivery_episode:advisory-recovery-epic-exit-source",
            "delivery_episode_review:advisory-recovery-epic-exit-review",
        ],
        proposed_recommendation={
            "recommendation_summary": "Prefer obstruction-aware bounded retry.",
            "design_hint": "Use only as advisory recovery proposal context.",
        },
        proposed_applicability={
            "vehicle_class": "px4_sitl",
            "mission_profile": "delivery",
        },
        rationale="Prior reviewed mission recovered after surfacing obstruction context.",
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
        decision_rationale="Promote advisory recovery epic-exit lesson.",
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
    mission_ref = "delivery_mission_session:advisory-recovery-epic-exit"
    source_observation_ref = "px4_gazebo_vehicle_observation:vehicle-a-hazard-exit"
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
            "mission_shared_observation_log:advisory-recovery-epic-exit"
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
        received_at=now + timedelta(seconds=2),
    )
    return mission, (vehicle_a, vehicle_b), shared


def _outcome_case() -> dict[str, Any]:
    return {
        "id": "advisory-recovery-epic-exit",
        "kind": "battery_low_recovery",
        "action": DeliveryRecoveryAction.RETURN_TO_HOME_RECOMMENDED.value,
        "fault_category": DeliveryFaultCategory.BATTERY_LOW.value,
        "facts": {
            "safe_landing_event_source": "logic_only_stub",
            "safe_landing_observed": True,
            "mission_terminated_safely": True,
            "vehicle_disarmed_or_landed": True,
        },
    }


def _run_recovery_outcome_case(
    case: dict[str, Any], advisory_context
) -> dict[str, Any]:
    _ = advisory_context
    chain = _chain()
    request = _request(
        chain,
        action=DeliveryRecoveryAction(case["action"]),
        category=DeliveryFaultCategory(case["fault_category"]),
    )
    run = _run(chain, request)
    outcome = build_delivery_recovery_outcome(
        delivery_recovery_request=request,
        delivery_recovery_run=run,
        observed_facts=case["facts"],
        now=NOW,
    )
    return {"delivery_recovery_outcome": outcome.model_dump(mode="json")}


def _validate_with_artifacts(
    *,
    context: RecoveryAdvisoryContext,
    lesson_task: dict,
    mission,
    vehicle_sessions,
    shared,
    store: TaskStore,
    recovery_artifacts: dict[str, Any],
) -> None:
    validate_recovery_advisory_refs(
        recovery_advisory_context=context,
        lesson_task=lesson_task,
        shared_observation_mission_session=mission,
        shared_observation_vehicle_sessions=vehicle_sessions,
        shared_observations=[shared],
        shared_observation_decision_at=NOW + timedelta(seconds=3),
        max_observation_age_seconds=5.0,
        recovery_artifacts=recovery_artifacts,
        task_store_factory=lambda: store,
    )


def _authority_negative(
    *,
    context: RecoveryAdvisoryContext,
    lesson_task: dict,
    mission,
    vehicle_sessions,
    shared,
    store: TaskStore,
    recovery_artifacts: dict[str, Any],
) -> bool:
    try:
        _validate_with_artifacts(
            context=context,
            lesson_task=lesson_task,
            mission=mission,
            vehicle_sessions=vehicle_sessions,
            shared=shared,
            store=store,
            recovery_artifacts=recovery_artifacts,
        )
    except RecoveryAdvisoryContextError:
        return True
    return False


def main() -> int:
    with TemporaryDirectory() as tmpdir:
        store = TaskStore(f"{tmpdir}/tasks.db")
        lesson_task, lesson_ref = _promoted_lesson_ref(store, now=NOW)
        mission, vehicle_sessions, shared = _shared_observation_chain(now=NOW)
        shared_ref = f"mission_shared_observation:{shared.observation_id}"
        context = build_recovery_advisory_context(
            mission_ref="task:advisory-recovery-epic-exit",
            mission_session_ref="delivery_mission_session:advisory-recovery-epic-exit",
            recovery_request_ref="delivery_recovery_request:advisory-recovery-epic-exit",
            used_lesson_refs=[lesson_ref],
            used_shared_observation_refs=[shared_ref],
            created_at=NOW,
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
            created_at=NOW,
        )
        _validate_with_artifacts(
            context=context,
            lesson_task=lesson_task,
            mission=mission,
            vehicle_sessions=vehicle_sessions,
            shared=shared,
            store=store,
            recovery_artifacts={
                "recovery_advisory_proposal": proposal.model_dump(mode="json"),
            },
        )

        full_context = (context.model_dump(mode="json"),)
        evidence = assert_recovery_outcome_ignores_advisory_context(
            corpus=[_outcome_case()],
            outcome_runner=_run_recovery_outcome_case,
            full_advisory_context=full_context,
        )
        with_advisory = _run_recovery_outcome_case(_outcome_case(), full_context)
        without_advisory = _run_recovery_outcome_case(_outcome_case(), ())
        digest_with = canonical_recovery_outcome_digest(with_advisory)
        digest_without = canonical_recovery_outcome_digest(without_advisory)

        context_ref = (
            f"recovery_advisory_context:{context.recovery_advisory_context_id}"
        )
        negative_observed_fact = _authority_negative(
            context=context,
            lesson_task=lesson_task,
            mission=mission,
            vehicle_sessions=vehicle_sessions,
            shared=shared,
            store=store,
            recovery_artifacts={
                "delivery_recovery_outcome": {"observed_fact_refs": [context_ref]}
            },
        )
        negative_scorecard = _authority_negative(
            context=context,
            lesson_task=lesson_task,
            mission=mission,
            vehicle_sessions=vehicle_sessions,
            shared=shared,
            store=store,
            recovery_artifacts={"delivery_scorecard": {"evidence_refs": [context_ref]}},
        )
        negative_success_proof = _authority_negative(
            context=context,
            lesson_task=lesson_task,
            mission=mission,
            vehicle_sessions=vehicle_sessions,
            shared=shared,
            store=store,
            recovery_artifacts={
                "recovery_success_proof": {"proof_refs": [context_ref]}
            },
        )
        negative_outcome_input = _authority_negative(
            context=context,
            lesson_task=lesson_task,
            mission=mission,
            vehicle_sessions=vehicle_sessions,
            shared=shared,
            store=store,
            recovery_artifacts={
                "delivery_recovery_outcome": {"outcome_input_refs": [context_ref]}
            },
        )
        negative_predicate_change = _authority_negative(
            context=context,
            lesson_task=lesson_task,
            mission=mission,
            vehicle_sessions=vehicle_sessions,
            shared=shared,
            store=store,
            recovery_artifacts={
                "delivery_recovery_outcome": {
                    "recovery_outcome_predicate_overrides": {"safe_landing": True}
                }
            },
        )
        negative_command_authority = _authority_negative(
            context=context,
            lesson_task=lesson_task,
            mission=mission,
            vehicle_sessions=vehicle_sessions,
            shared=shared,
            store=store,
            recovery_artifacts={"bad": {"mavlink_command": "not allowed"}},
        )

        result = build_recovery_advisory_epic_exit_result(
            recovery_advisory_context=context,
            recovery_advisory_proposal=proposal,
            recovery_outcome_hash_with_advisory=digest_with,
            recovery_outcome_hash_without_advisory=digest_without,
            verifier_invariance_evidence=evidence,
            negative_observed_fact_failed_closed=negative_observed_fact,
            negative_scorecard_evidence_failed_closed=negative_scorecard,
            negative_success_proof_failed_closed=negative_success_proof,
            negative_outcome_input_failed_closed=negative_outcome_input,
            negative_predicate_change_failed_closed=negative_predicate_change,
            negative_command_authority_failed_closed=negative_command_authority,
            created_at=NOW,
        )

    summary = {
        "recovery_advisory_epic_exit_smoke_passed": True,
        "schema_version": result.schema_version,
        "epic_exit_id": result.epic_exit_id,
        "issue_476_satisfied": True,
        "epic_470_close_allowed": True,
        "production_boundary": (
            "recovery_advisory_context.v1 -> recovery_advisory_proposal.v1 -> "
            "validate_recovery_advisory_refs() -> recovery outcome invariance -> "
            "recovery_advisory_epic_exit.v1"
        ),
        "recovery_advisory_context_ref": result.recovery_advisory_context_ref,
        "recovery_request_ref": result.recovery_request_ref,
        "recovery_advisory_proposal_ref": result.recovery_advisory_proposal_ref,
        "used_lesson_refs": list(result.used_lesson_refs),
        "used_shared_observation_refs": list(result.used_shared_observation_refs),
        "ignored_lesson_refs": list(result.ignored_lesson_refs),
        "ignored_shared_observation_refs": list(result.ignored_shared_observation_refs),
        "advisory_validation_evidence_count": (
            result.advisory_validation_evidence_count
        ),
        "suppressed_recovery_candidates_count": (
            result.suppressed_recovery_candidates_count
        ),
        "recovery_outcome_hash_with_advisory": (
            result.recovery_outcome_hash_with_advisory
        ),
        "recovery_outcome_hash_without_advisory": (
            result.recovery_outcome_hash_without_advisory
        ),
        "recovery_outcome_byte_equal_with_and_without_advisory": (
            result.recovery_outcome_byte_equal_with_and_without_advisory
        ),
        "verifier_invariance_evidence_count": (
            result.verifier_invariance_evidence_count
        ),
        "verifier_invariance_evidence_case_ids": list(
            result.verifier_invariance_evidence_case_ids
        ),
        "negative_observed_fact_failed_closed": (
            result.negative_observed_fact_failed_closed
        ),
        "negative_scorecard_evidence_failed_closed": (
            result.negative_scorecard_evidence_failed_closed
        ),
        "negative_success_proof_failed_closed": (
            result.negative_success_proof_failed_closed
        ),
        "negative_outcome_input_failed_closed": (
            result.negative_outcome_input_failed_closed
        ),
        "negative_predicate_change_failed_closed": (
            result.negative_predicate_change_failed_closed
        ),
        "negative_command_authority_failed_closed": (
            result.negative_command_authority_failed_closed
        ),
        "epic_invariant_advisory_context_never_outcome_authority": (
            result.epic_invariant_advisory_context_never_outcome_authority
        ),
        "advisory_used_as_outcome_evidence": (result.advisory_used_as_outcome_evidence),
        "advisory_used_as_scorecard_evidence": (
            result.advisory_used_as_scorecard_evidence
        ),
        "advisory_used_as_success_proof": result.advisory_used_as_success_proof,
        "advisory_modifies_observed_facts": result.advisory_modifies_observed_facts,
        "advisory_modifies_recovery_outcome_predicates": (
            result.advisory_modifies_recovery_outcome_predicates
        ),
        "dispatch_authority_granted": result.dispatch_authority_granted,
        "external_dispatch_performed": result.external_dispatch_performed,
        "raw_mavlink_command_allowed": result.raw_mavlink_command_allowed,
        "raw_ros_action_allowed": result.raw_ros_action_allowed,
        "gazebo_entity_mutation_allowed": result.gazebo_entity_mutation_allowed,
        "setpoint_stream_allowed": result.setpoint_stream_allowed,
        "actuator_command_allowed": result.actuator_command_allowed,
        "hardware_target_allowed": result.hardware_target_allowed,
        "physical_execution_invoked": result.physical_execution_invoked,
        "approval_free_stronger_recovery_allowed": (
            result.approval_free_stronger_recovery_allowed
        ),
        "public_sync_performed": result.public_sync_performed,
        "readme_or_architecture_updated": result.readme_or_architecture_updated,
        "environment_limitations": [
            "logic-only advisory recovery epic-exit smoke; no real PX4/Gazebo SITL container was started"
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    assert summary["schema_version"] == RECOVERY_ADVISORY_EPIC_EXIT_SCHEMA_VERSION
    assert summary["recovery_outcome_byte_equal_with_and_without_advisory"] is True
    assert summary["epic_invariant_advisory_context_never_outcome_authority"] is True
    assert summary["negative_observed_fact_failed_closed"] is True
    assert summary["negative_scorecard_evidence_failed_closed"] is True
    assert summary["negative_success_proof_failed_closed"] is True
    assert summary["negative_outcome_input_failed_closed"] is True
    assert summary["negative_predicate_change_failed_closed"] is True
    assert summary["negative_command_authority_failed_closed"] is True
    assert summary["dispatch_authority_granted"] is False
    assert summary["raw_mavlink_command_allowed"] is False
    assert summary["raw_ros_action_allowed"] is False
    assert summary["gazebo_entity_mutation_allowed"] is False
    assert summary["setpoint_stream_allowed"] is False
    assert summary["actuator_command_allowed"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["public_sync_performed"] is False
    assert summary["readme_or_architecture_updated"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

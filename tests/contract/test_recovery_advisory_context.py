from pathlib import Path

import pytest

from src.runtime.advisory_mission_memory import (
    attach_delivery_mission_lesson_candidate,
    attach_delivery_mission_lesson_promotion,
)
from src.runtime.recovery_advisory_context import (
    RecoveryAdvisoryContextError,
    build_recovery_advisory_context,
    build_recovery_advisory_proposal,
    validate_recovery_advisory_refs,
)
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
)
from src.runtime.task_store import TaskStore
from tests.fixtures.delivery_shared_observation import (
    NOW,
    build_shared_observation_bundle,
)


def _promoted_lesson(store: TaskStore) -> tuple[dict, str]:
    source = store.create(
        kind="delivery_episode",
        title="Recovery advisory source mission",
        status="completed",
        artifacts={
            "delivery_episode": {"episode_id": "recovery-advisory-source"},
            "delivery_episode_review": {"review_id": "recovery-advisory-review"},
        },
    )
    task = store.create(
        kind="advisory_recovery_context",
        title="Recovery advisory lesson fixture",
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
            "recommendation_summary": "Prefer bounded retry after obstruction.",
            "design_hint": "Advisory proposal context only.",
        },
        proposed_applicability={
            "vehicle_class": "px4_sitl",
            "mission_profile": "delivery",
        },
        rationale="Prior reviewed mission recovered with a bounded retry.",
        created_by="operator",
        created_at=NOW,
        task_store_factory=lambda: store,
    )["delivery_mission_lesson_candidate"]
    promoted = attach_delivery_mission_lesson_promotion(
        task["task_id"],
        lesson_candidate_ref=(
            f"delivery_mission_lesson_candidate:{candidate['candidate_id']}"
        ),
        operator_id="operator-1",
        decision_rationale="Promote recovery advisory context lesson.",
        decision_at=NOW,
        created_at=NOW,
        valid_for_episode_schema_versions=[SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION],
        task_store_factory=lambda: store,
    )
    lesson = promoted["delivery_mission_lesson"]
    return store.get(task["task_id"]) or task, (
        f"delivery_mission_lesson:{lesson['lesson_id']}"
    )


def test_recovery_advisory_context_is_visible_judgement_input_not_authority(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    lesson_task, lesson_ref = _promoted_lesson(store)
    bundle = build_shared_observation_bundle(received_delay_seconds=0)
    context = build_recovery_advisory_context(
        mission_ref="task:advisory-recovery-context-fixture",
        mission_session_ref=bundle.mission_ref,
        recovery_request_ref="delivery_recovery_request:advisory-fixture",
        used_lesson_refs=[lesson_ref],
        used_shared_observation_refs=[bundle.shared_ref],
        created_at=NOW,
    )
    proposal = build_recovery_advisory_proposal(
        recovery_request_ref=context.recovery_request_ref,
        recovery_advisory_context=context,
        suppressed_recovery_candidates=[
            {
                "candidate_kind": "retry_without_obstruction_review",
                "suppressing_advisory_ref": lesson_ref,
                "suppression_rationale": "Keep obstruction-aware review visible.",
            }
        ],
        created_at=NOW,
    )
    validate_recovery_advisory_refs(
        recovery_advisory_context=context,
        lesson_task=lesson_task,
        shared_observation_mission_session=bundle.mission,
        shared_observation_vehicle_sessions=bundle.vehicle_sessions,
        shared_observations=[bundle.shared],
        shared_observation_decision_at=NOW,
        max_observation_age_seconds=5.0,
        recovery_artifacts={
            "recovery_advisory_proposal": proposal.model_dump(mode="json"),
            "delivery_recovery_request": {
                "request_id": "advisory-fixture",
                "recovery_advisory_context_ref": (
                    "recovery_advisory_context:"
                    f"{context.recovery_advisory_context_id}"
                ),
            },
        },
        task_store_factory=lambda: store,
    )

    assert context.used_lesson_refs == (lesson_ref,)
    assert context.used_shared_observation_refs == (bundle.shared_ref,)
    assert len(proposal.suppressed_recovery_candidates) == 1
    assert proposal.proposal_uses_advisory_authority_for_judgement is False
    assert proposal.proposal_modifies_recovery_outcome_predicates is False
    assert context.advisory_context_only is True
    for field in (
        "advisory_grants_recovery_authority",
        "advisory_used_as_outcome_evidence",
        "advisory_used_as_scorecard_evidence",
        "advisory_used_as_success_proof",
        "advisory_modifies_observed_facts",
        "advisory_modifies_recovery_outcome_predicates",
        "dispatch_authority_granted",
        "raw_mavlink_command_allowed",
        "raw_ros_action_allowed",
        "gazebo_entity_mutation_allowed",
        "setpoint_stream_allowed",
        "actuator_command_allowed",
        "hardware_target_allowed",
        "physical_execution_invoked",
        "approval_free_stronger_recovery_allowed",
    ):
        assert getattr(context, field) is False

    invalid_payload = context.model_dump(mode="json")
    invalid_payload["advisory_used_as_outcome_evidence"] = True
    with pytest.raises(ValueError):
        type(context).model_validate(invalid_payload)
    with pytest.raises(RecoveryAdvisoryContextError):
        build_recovery_advisory_context(
            mission_ref="task:advisory-recovery-context-fixture",
            recovery_request_ref="delivery_recovery_request:advisory-fixture",
            metadata={"ros_action": "not allowed"},
            created_at=NOW,
        )
    empty_context = build_recovery_advisory_context(
        mission_ref="task:advisory-recovery-context-fixture",
        recovery_request_ref="delivery_recovery_request:advisory-fixture",
        created_at=NOW,
    )
    with pytest.raises(RecoveryAdvisoryContextError):
        validate_recovery_advisory_refs(
            recovery_advisory_context=empty_context,
            recovery_artifacts={
                "delivery_recovery_outcome": {
                    "outcome_input_refs": [
                        "recovery_advisory_context:"
                        f"{empty_context.recovery_advisory_context_id}"
                    ]
                }
            },
        )
    with pytest.raises(RecoveryAdvisoryContextError):
        build_recovery_advisory_proposal(
            recovery_request_ref=context.recovery_request_ref,
            recovery_advisory_context=context,
            suppressed_recovery_candidates=[
                {
                    "candidate_kind": "hidden_advisory_filter",
                    "suppressing_advisory_ref": (
                        "delivery_mission_lesson:not-used-by-context"
                    ),
                    "suppression_rationale": "Hidden refs must fail closed.",
                }
            ],
            created_at=NOW,
        )

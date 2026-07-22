"""TaskStore contracts replacing advisory mission-memory smoke programs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.runtime.advisory_mission_memory import (
    DELIVERY_MISSION_LESSON_CANDIDATE_SCHEMA_VERSION,
    DELIVERY_MISSION_LESSON_PROMOTION_RECEIPT_SCHEMA_VERSION,
    DELIVERY_MISSION_LESSON_SCHEMA_VERSION,
    VERIFIER_CONTRACT_SCHEMA_VERSION,
    AdvisoryMissionMemoryError,
    MissionEnvelope,
    attach_delivery_mission_lesson_candidate,
    attach_delivery_mission_lesson_promotion,
    current_verifier_contract,
    lesson_applies_to,
    validate_lesson_refs,
)
from src.runtime.simulated_delivery_episode import SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _promoted_lesson(
    tmp_path: Path,
) -> tuple[TaskStore, dict, dict, dict, str, str, str]:
    store = TaskStore(str(tmp_path / "tasks.db"))
    source = store.create(
        kind="delivery_episode",
        title="completed delivery source",
        status="completed",
        artifacts={
            "delivery_episode": {"episode_id": "episode-contract"},
            "delivery_scorecard": {"scorecard_id": "scorecard-contract"},
            "delivery_episode_review": {"review_id": "review-contract"},
        },
    )
    task = store.create(
        kind="advisory_mission_memory",
        title="advisory memory contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    candidate_attached = attach_delivery_mission_lesson_candidate(
        task["task_id"],
        source_mission_refs=[f"task:{source['task_id']}"],
        source_artifact_refs=[
            "delivery_episode:episode-contract",
            "delivery_scorecard:scorecard-contract",
            "delivery_episode_review:review-contract",
        ],
        proposed_recommendation={
            "recommendation_summary": "Prefer staged ascent.",
            "design_hint": "Use staged ascent for the matching envelope.",
            "avoid_scenario_summary": "Suppress direct high-altitude climb.",
        },
        proposed_applicability={
            "vehicle_class": "px4_sitl",
            "payload_kg_min": 4.0,
            "payload_kg_max": 8.0,
            "altitude_m_min": 2500.0,
            "terrain_class": "mountain",
            "mission_profile": "delivery",
        },
        rationale="A completed review showed payload margin risk.",
        created_by="llm",
        created_at=NOW,
        task_store_factory=lambda: store,
    )
    candidate = candidate_attached["delivery_mission_lesson_candidate"]
    candidate_ref = f"delivery_mission_lesson_candidate:{candidate['candidate_id']}"
    promoted = attach_delivery_mission_lesson_promotion(
        task["task_id"],
        lesson_candidate_ref=candidate_ref,
        operator_id="operator-contract",
        decision_rationale="Operator reviewed and promoted the advisory lesson.",
        decision_at=NOW,
        created_at=NOW,
        task_store_factory=lambda: store,
    )
    lesson = promoted["delivery_mission_lesson"]
    receipt = promoted["delivery_mission_lesson_promotion_receipt"]
    receipt_ref = (
        "delivery_mission_lesson_promotion_receipt:"
        f"{receipt['promotion_receipt_id']}"
    )
    lesson_ref = f"delivery_mission_lesson:{lesson['lesson_id']}"
    return store, task, candidate, lesson, candidate_ref, receipt_ref, lesson_ref


def test_operator_promotion_keeps_lesson_advisory_and_out_of_verifier_authority(
    tmp_path: Path,
) -> None:
    store, task, candidate, lesson, _candidate_ref, _receipt_ref, _lesson_ref = (
        _promoted_lesson(tmp_path)
    )
    verifier = current_verifier_contract(created_at=NOW)
    stored = store.update(
        task["task_id"],
        artifacts={"verifier_contract": verifier.model_dump(mode="json")},
    )
    receipt = stored["artifacts"]["delivery_mission_lesson_promotion_receipt"]

    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert candidate["schema_version"] == DELIVERY_MISSION_LESSON_CANDIDATE_SCHEMA_VERSION
    assert candidate["advisory_only"] is True
    assert candidate["is_promoted"] is False
    assert candidate["usable_in_scenario_design"] is False
    assert receipt["schema_version"] == DELIVERY_MISSION_LESSON_PROMOTION_RECEIPT_SCHEMA_VERSION
    assert receipt["auto_promotion_used"] is False
    assert receipt["llm_decided_promotion"] is False
    assert lesson["schema_version"] == DELIVERY_MISSION_LESSON_SCHEMA_VERSION
    assert lesson["advisory_only"] is True
    assert lesson["usable_in_scenario_design"] is True
    assert lesson["usable_as_scorecard_evidence"] is False
    assert lesson["usable_as_verifier_input"] is False
    assert lesson["usable_as_success_proof"] is False
    assert lesson["verifier_predicate_change_proposed"] is False
    assert lesson["physical_execution_invoked"] is False
    assert lesson["hardware_target_allowed"] is False
    assert lesson["external_dispatch_performed"] is False
    assert verifier.schema_version == VERIFIER_CONTRACT_SCHEMA_VERSION
    assert verifier.lesson_influenced is False
    assert lesson_applies_to(
        lesson,
        MissionEnvelope(
            vehicle_class="px4_sitl",
            payload_kg=5.0,
            altitude_m=3000.0,
            terrain_class="mountain",
            mission_profile="delivery",
        ),
        episode_schema_version=SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
        now=NOW,
    )


def test_lesson_refs_validate_but_scorecard_use_is_rejected(tmp_path: Path) -> None:
    store, task, _candidate, _lesson, candidate_ref, receipt_ref, lesson_ref = (
        _promoted_lesson(tmp_path)
    )
    stored = store.get(task["task_id"])

    validate_lesson_refs(
        task=stored,
        lesson_candidate_ref=candidate_ref,
        promotion_receipt_ref=receipt_ref,
        lesson_ref=lesson_ref,
        task_store_factory=lambda: store,
    )
    with pytest.raises(AdvisoryMissionMemoryError, match="lesson_used_as_scorecard_evidence"):
        validate_lesson_refs(
            task={
                **stored,
                "artifacts": {
                    **stored["artifacts"],
                    "scorecard": {"evidence_refs": [lesson_ref]},
                },
            },
            lesson_ref=lesson_ref,
            task_store_factory=lambda: store,
        )

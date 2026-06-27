#!/usr/bin/env python3
"""Env-gated epic-exit E2E for Advisory Mission Memory (#450)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from tempfile import TemporaryDirectory

from src.runtime.advisory_lesson_invariance import (
    assert_verifier_ignores_lessons,
    lesson_registry,
)
from src.runtime.advisory_mission_memory import (
    attach_delivery_mission_lesson_candidate,
    attach_delivery_mission_lesson_promotion,
)
from src.runtime.advisory_mission_memory_epic_exit import (
    build_advisory_mission_memory_epic_exit_result,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    run_px4_gazebo_mission_scenario_designer,
)
from src.runtime.task_store import TaskStore
from tests.test_delivery_episode_review import _bounded_chain, _review

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _promoted_lesson(
    store: TaskStore,
    *,
    task_id: str,
    source_id: str,
    payload_min: float,
):
    candidate = attach_delivery_mission_lesson_candidate(
        task_id,
        source_mission_refs=[f"task:{source_id}"],
        source_artifact_refs=[
            "delivery_episode:episode-e2e",
            "delivery_scorecard:scorecard-e2e",
            "delivery_episode_review:review-e2e",
        ],
        proposed_recommendation={
            "recommendation_summary": "Prefer staged ascent for mountain delivery.",
            "design_hint": "Prefer staged ascent and suppress direct climb.",
            "avoid_scenario_summary": "Direct high-altitude climb to dropoff.",
        },
        proposed_applicability={
            "vehicle_class": "px4_sitl",
            "payload_kg_min": payload_min,
            "payload_kg_max": payload_min + 5.0,
            "altitude_m_min": 2500.0,
            "terrain_class": "mountain",
            "mission_profile": "delivery",
        },
        rationale="Prior completed mission showed direct climb risk.",
        created_by="operator",
        created_at=NOW,
        task_store_factory=lambda: store,
    )["delivery_mission_lesson_candidate"]
    candidate_ref = f"delivery_mission_lesson_candidate:{candidate['candidate_id']}"
    return attach_delivery_mission_lesson_promotion(
        task_id,
        lesson_candidate_ref=candidate_ref,
        operator_id="operator-e2e",
        decision_rationale="Promote advisory scenario-design lesson.",
        decision_at=NOW,
        created_at=NOW,
        task_store_factory=lambda: store,
    )


def _verifier_output() -> dict:
    return _review(_bounded_chain())


def _run_epic_exit_verifier_case(_case, _lesson_registry) -> dict:
    return _verifier_output()


def run_epic_exit() -> dict:
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        source = store.create(
            kind="delivery_episode",
            title="completed source mission",
            status="completed",
            artifacts={
                "delivery_episode": {"episode_id": "episode-e2e"},
                "delivery_scorecard": {"scorecard_id": "scorecard-e2e"},
                "delivery_episode_review": {"review_id": "review-e2e"},
            },
        )
        task = store.create(
            kind="advisory_mission_memory",
            title="advisory mission memory epic exit",
            status="running",
        )
        promoted = _promoted_lesson(
            store,
            task_id=task["task_id"],
            source_id=source["task_id"],
            payload_min=4.0,
        )
        ignored = _promoted_lesson(
            store,
            task_id=task["task_id"],
            source_id=source["task_id"],
            payload_min=20.0,
        )["delivery_mission_lesson"]
        lesson = promoted["delivery_mission_lesson"]
        receipt = promoted["delivery_mission_lesson_promotion_receipt"]
        lesson_ref = f"delivery_mission_lesson:{lesson['lesson_id']}"
        receipt_ref = (
            "delivery_mission_lesson_promotion_receipt:"
            f"{receipt['promotion_receipt_id']}"
        )
        designer_with_lessons = run_px4_gazebo_mission_scenario_designer(
            prompt="Plan a mountain summit delivery carrying a 5kg payload to 3000m.",
            now=NOW,
            lesson_registry=[lesson, ignored],
        )
        proposal = designer_with_lessons["scenario_proposal"]
        full_lesson_registry = (lesson, ignored)
        verifier_invariance_evidence = assert_verifier_ignores_lessons(
            corpus=[
                {
                    "id": "advisory_mission_memory_epic_exit",
                    "kind": "delivery_episode_review",
                }
            ],
            verifier_runner=_run_epic_exit_verifier_case,
            full_lesson_registry=full_lesson_registry,
        )
        with lesson_registry(full_lesson_registry):
            verifier_with_lessons = _verifier_output()
        with lesson_registry(()):
            verifier_without_lessons = _verifier_output()
        result = build_advisory_mission_memory_epic_exit_result(
            lesson_promotion_receipt_ref=receipt_ref,
            lesson_ref=lesson_ref,
            scenario_proposal_ref=(
                f"px4_gazebo_mission_scenario_proposal:{proposal['proposal_id']}"
            ),
            used_lesson_refs=proposal["used_lesson_refs"],
            suppressed_scenario_candidates_count=len(
                proposal["suppressed_scenario_candidates"]
            ),
            verifier_contract_ref=proposal["verifier_contract_ref"],
            verifier_output_with_lessons=verifier_with_lessons,
            verifier_output_without_lessons=verifier_without_lessons,
            auto_promotion_used=receipt["auto_promotion_used"],
            llm_decided_promotion=receipt["llm_decided_promotion"],
            completed_at=NOW,
        )
        return {
            "epic_exit_result": result.model_dump(mode="json"),
            "lesson_promotion_receipt_ref": receipt_ref,
            "lesson_ref": lesson_ref,
            "scenario_proposal_ref": (
                f"px4_gazebo_mission_scenario_proposal:{proposal['proposal_id']}"
            ),
            "used_lesson_refs": proposal["used_lesson_refs"],
            "suppressed_scenario_candidates_count": len(
                proposal["suppressed_scenario_candidates"]
            ),
            "verifier_contract_ref": proposal["verifier_contract_ref"],
            "verifier_output_hash_with_lessons": (
                result.verifier_output_hash_with_lessons
            ),
            "verifier_output_hash_without_lessons": (
                result.verifier_output_hash_without_lessons
            ),
            "verifier_output_byte_equal_with_and_without_lessons": (
                result.verifier_output_byte_equal_with_and_without_lessons
            ),
            "verifier_invariance_evidence_count": len(verifier_invariance_evidence),
            "verifier_invariance_evidence_case_ids": [
                item["case_id"] for item in verifier_invariance_evidence
            ],
            "epic_invariant_lessons_never_authority": (
                result.epic_invariant_lessons_never_authority
            ),
            "auto_promotion_used": result.auto_promotion_used,
            "llm_decided_promotion": result.llm_decided_promotion,
            "physical_execution_invoked": result.physical_execution_invoked,
            "hardware_target_allowed": result.hardware_target_allowed,
            "external_dispatch_performed": result.external_dispatch_performed,
            "logic_only_path": True,
            "real_sitl_out_of_scope": True,
            "public_sync_performed": False,
            "readme_or_architecture_updated": False,
        }


def main() -> int:
    if os.environ.get("RUN_ADVISORY_MISSION_MEMORY_E2E") != "1":
        print(
            json.dumps(
                {
                    "skipped": True,
                    "reason": "set RUN_ADVISORY_MISSION_MEMORY_E2E=1",
                },
                sort_keys=True,
            )
        )
        return 0
    summary = run_epic_exit()
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

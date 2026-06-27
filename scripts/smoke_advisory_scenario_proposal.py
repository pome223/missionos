#!/usr/bin/env python3
"""Runtime smoke for advisory lesson surfaces in scenario proposals (#447)."""

from __future__ import annotations

import json
from tempfile import TemporaryDirectory

from src.runtime.advisory_mission_memory import (
    attach_delivery_mission_lesson_candidate,
    attach_delivery_mission_lesson_promotion,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    run_px4_gazebo_mission_scenario_designer,
)
from src.runtime.task_store import TaskStore
from tests.test_px4_gazebo_mission_scenario_designer import NOW


def _promoted_lesson(store: TaskStore, task_id: str, source_id: str, *, payload_min: float):
    candidate = attach_delivery_mission_lesson_candidate(
        task_id,
        source_mission_refs=[f"task:{source_id}"],
        source_artifact_refs=[
            "delivery_episode:episode-smoke",
            "delivery_scorecard:scorecard-smoke",
            "delivery_episode_review:review-smoke",
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
        rationale="Prior mission review showed direct climb risk.",
        created_by="operator",
        created_at=NOW,
        task_store_factory=lambda: store,
    )["delivery_mission_lesson_candidate"]
    candidate_ref = f"delivery_mission_lesson_candidate:{candidate['candidate_id']}"
    return attach_delivery_mission_lesson_promotion(
        task_id,
        lesson_candidate_ref=candidate_ref,
        operator_id="operator-smoke",
        decision_rationale="Promote advisory scenario-design lesson.",
        decision_at=NOW,
        created_at=NOW,
        task_store_factory=lambda: store,
    )["delivery_mission_lesson"]


def main() -> int:
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        source = store.create(
            kind="delivery_episode",
            title="completed source mission",
            status="completed",
            artifacts={
                "delivery_episode": {"episode_id": "episode-smoke"},
                "delivery_scorecard": {"scorecard_id": "scorecard-smoke"},
                "delivery_episode_review": {"review_id": "review-smoke"},
            },
        )
        task = store.create(
            kind="advisory_mission_memory",
            title="advisory scenario proposal smoke",
            status="running",
        )
        used = _promoted_lesson(store, task["task_id"], source["task_id"], payload_min=4.0)
        ignored = _promoted_lesson(
            store,
            task["task_id"],
            source["task_id"],
            payload_min=20.0,
        )
        result = run_px4_gazebo_mission_scenario_designer(
            prompt="Plan a mountain summit delivery carrying a 5kg payload to 3000m.",
            now=NOW,
            lesson_registry=[used, ignored],
        )

    proposal = result["scenario_proposal"]
    summary = {
        "proposal_schema_version": proposal["schema_version"],
        "used_lesson_refs": proposal["used_lesson_refs"],
        "ignored_lesson_refs": proposal["ignored_lesson_refs"],
        "ignored_lesson_records": proposal["ignored_lesson_records"],
        "suppressed_scenario_candidates": proposal["suppressed_scenario_candidates"],
        "suppressed_scenario_candidates_count": len(
            proposal["suppressed_scenario_candidates"]
        ),
        "verifier_contract_ref": proposal["verifier_contract_ref"],
        "lesson_registry_snapshot_hash": proposal["lesson_registry_snapshot_hash"],
        "proposal_uses_lesson_authority_for_judgement": proposal[
            "proposal_uses_lesson_authority_for_judgement"
        ],
        "proposal_modifies_verifier_predicates": proposal[
            "proposal_modifies_verifier_predicates"
        ],
        "physical_execution_invoked": proposal["physical_execution_invoked"],
        "hardware_target_allowed": proposal["hardware_target_allowed"],
        "gazebo_execution_invoked": proposal["gazebo_execution_invoked"],
        "summary_used_lesson_refs": result["summary"]["used_lesson_refs"],
        "summary_ignored_lesson_refs": result["summary"]["ignored_lesson_refs"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))

    assert summary["proposal_schema_version"] == "px4_gazebo_mission_scenario_proposal.v1"
    assert len(summary["used_lesson_refs"]) == 1
    assert len(summary["ignored_lesson_refs"]) == 1
    assert len(summary["ignored_lesson_records"]) == 1
    assert summary["ignored_lesson_records"][0]["lesson_ref"] == (
        summary["ignored_lesson_refs"][0]
    )
    assert summary["suppressed_scenario_candidates_count"] == 1
    assert summary["suppressed_scenario_candidates"][0]["suppressing_lesson_ref"] == (
        summary["used_lesson_refs"][0]
    )
    assert summary["verifier_contract_ref"].startswith("verifier_contract:")
    assert summary["lesson_registry_snapshot_hash"].startswith(
        "lesson_registry_snapshot_"
    )
    assert summary["proposal_uses_lesson_authority_for_judgement"] is False
    assert summary["proposal_modifies_verifier_predicates"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["gazebo_execution_invoked"] is False
    assert summary["summary_used_lesson_refs"] == summary["used_lesson_refs"]
    assert summary["summary_ignored_lesson_refs"] == summary["ignored_lesson_refs"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

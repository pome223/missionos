#!/usr/bin/env python3
"""Runtime smoke for advisory lesson reference validation."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from src.runtime.advisory_mission_memory import (
    AdvisoryMissionMemoryError,
    attach_delivery_mission_lesson_candidate,
    attach_delivery_mission_lesson_promotion,
    validate_lesson_refs,
)
from src.runtime.task_store import TaskStore


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(str(Path(tmp) / "tasks.db"))
        source = store.create(
            kind="delivery_episode",
            title="completed advisory lesson source",
            status="completed",
            artifacts={
                "delivery_episode": {"episode_id": "episode-smoke"},
                "delivery_scorecard": {"scorecard_id": "scorecard-smoke"},
                "delivery_episode_review": {"review_id": "review-smoke"},
            },
        )
        lesson_task = store.create(
            kind="advisory_mission_memory",
            title="advisory lesson ref validator smoke",
            status="running",
        )
        candidate_attached = attach_delivery_mission_lesson_candidate(
            lesson_task["task_id"],
            source_mission_refs=[f"task:{source['task_id']}"],
            source_artifact_refs=[
                "delivery_episode:episode-smoke",
                "delivery_scorecard:scorecard-smoke",
                "delivery_episode_review:review-smoke",
            ],
            proposed_recommendation={
                "recommendation_summary": "Prefer staged ascent.",
                "design_hint": "Use staged ascent for the matching envelope.",
                "avoid_scenario_summary": "Suppress direct high-altitude climb.",
            },
            proposed_applicability={
                "vehicle_class": "px4_sitl",
                "terrain_class": "mountain",
                "mission_profile": "delivery",
            },
            rationale="Prior completed source showed margin risk.",
            created_by="rule",
            task_store_factory=lambda: store,
        )
        candidate = candidate_attached["delivery_mission_lesson_candidate"]
        candidate_ref = f"delivery_mission_lesson_candidate:{candidate['candidate_id']}"
        promoted = attach_delivery_mission_lesson_promotion(
            lesson_task["task_id"],
            lesson_candidate_ref=candidate_ref,
            operator_id="operator-smoke",
            decision_rationale="Operator promoted after manual review.",
            task_store_factory=lambda: store,
        )
        receipt = promoted["delivery_mission_lesson_promotion_receipt"]
        lesson = promoted["delivery_mission_lesson"]
        receipt_ref = (
            "delivery_mission_lesson_promotion_receipt:"
            f"{receipt['promotion_receipt_id']}"
        )
        lesson_ref = f"delivery_mission_lesson:{lesson['lesson_id']}"
        updated_task = store.get(lesson_task["task_id"])
        validate_lesson_refs(
            task=updated_task,
            lesson_candidate_ref=candidate_ref,
            promotion_receipt_ref=receipt_ref,
            lesson_ref=lesson_ref,
            task_store_factory=lambda: store,
        )

        scorecard_blocked = False
        try:
            validate_lesson_refs(
                task={
                    **updated_task,
                    "artifacts": {
                        **updated_task["artifacts"],
                        "scorecard": {"evidence_refs": [lesson_ref]},
                    },
                },
                lesson_ref=lesson_ref,
                task_store_factory=lambda: store,
            )
        except AdvisoryMissionMemoryError as exc:
            scorecard_blocked = "lesson_used_as_scorecard_evidence" in str(exc)

        summary = {
            "validator_runtime_smoke_passed": True,
            "production_boundary": "TaskStore-backed advisory lesson task artifacts",
            "candidate_ref": candidate_ref,
            "promotion_receipt_ref": receipt_ref,
            "lesson_ref": lesson_ref,
            "source_task_id": source["task_id"],
            "scorecard_authority_rejection_observed": scorecard_blocked,
            "public_sync_performed": False,
            "readme_or_architecture_updated": False,
            "issue_449_touched": False,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if scorecard_blocked else 1


if __name__ == "__main__":
    raise SystemExit(main())

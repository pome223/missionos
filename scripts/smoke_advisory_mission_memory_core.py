#!/usr/bin/env python3
"""Runtime smoke for advisory mission memory core artifacts (#443-#446)."""

from __future__ import annotations

import json
from tempfile import TemporaryDirectory

from src.runtime.advisory_mission_memory import (
    DELIVERY_MISSION_LESSON_CANDIDATE_SCHEMA_VERSION,
    DELIVERY_MISSION_LESSON_PROMOTION_RECEIPT_SCHEMA_VERSION,
    DELIVERY_MISSION_LESSON_SCHEMA_VERSION,
    VERIFIER_CONTRACT_SCHEMA_VERSION,
    MissionEnvelope,
    attach_delivery_mission_lesson_candidate,
    attach_delivery_mission_lesson_promotion,
    current_verifier_contract,
    lesson_applies_to,
)
from src.runtime.simulated_delivery_episode import SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
from src.runtime.task_store import TaskStore
from tests.test_simulated_delivery_command import NOW


def main() -> int:
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        source = store.create(
            kind="delivery_episode",
            title="completed delivery source",
            status="completed",
            artifacts={
                "delivery_episode": {"episode_id": "episode-smoke"},
                "delivery_scorecard": {"scorecard_id": "scorecard-smoke"},
                "delivery_episode_review": {"review_id": "review-smoke"},
            },
        )
        task = store.create(
            kind="advisory_mission_memory",
            title="advisory memory core smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        candidate_attached = attach_delivery_mission_lesson_candidate(
            task["task_id"],
            source_mission_refs=[f"task:{source['task_id']}"],
            source_artifact_refs=[
                "delivery_episode:episode-smoke",
                "delivery_scorecard:scorecard-smoke",
                "delivery_episode_review:review-smoke",
            ],
            proposed_recommendation={
                "recommendation_summary": (
                    "Prefer staged ascent for heavy mountain deliveries."
                ),
                "design_hint": "Prefer staged ascent scenario candidate.",
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
            rationale="Completed mission review showed payload margin risk.",
            created_by="llm",
            created_at=NOW,
            task_store_factory=lambda: store,
        )
        candidate = candidate_attached["delivery_mission_lesson_candidate"]
        candidate_ref = f"delivery_mission_lesson_candidate:{candidate['candidate_id']}"
        promoted = attach_delivery_mission_lesson_promotion(
            task["task_id"],
            lesson_candidate_ref=candidate_ref,
            operator_id="operator-smoke",
            decision_rationale="Operator reviewed and promoted advisory lesson.",
            decision_at=NOW,
            created_at=NOW,
            task_store_factory=lambda: store,
        )
        lesson = promoted["delivery_mission_lesson"]
        receipt = promoted["delivery_mission_lesson_promotion_receipt"]
        verifier_contract = current_verifier_contract(created_at=NOW)
        stored = store.update(
            task["task_id"],
            artifacts={"verifier_contract": verifier_contract.model_dump(mode="json")},
        )
        envelope = MissionEnvelope(
            vehicle_class="px4_sitl",
            payload_kg=5.0,
            altitude_m=3000.0,
            terrain_class="mountain",
            mission_profile="delivery",
        )
        applies = lesson_applies_to(
            lesson,
            envelope,
            episode_schema_version=SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
            now=NOW,
        )

    artifacts = stored["artifacts"] if stored else {}
    summary = {
        "task_status": stored["status"] if stored else None,
        "existing_artifact_kept": bool(
            stored and stored["artifacts"]["existing"]["kept"]
        ),
        "candidate_schema_version": candidate["schema_version"],
        "candidate_ref": candidate_ref,
        "candidate_advisory_only": candidate["advisory_only"],
        "candidate_is_promoted": candidate["is_promoted"],
        "candidate_usable_in_scenario_design": candidate[
            "usable_in_scenario_design"
        ],
        "receipt_schema_version": receipt["schema_version"],
        "operator_promotion_receipt_ref": (
            "delivery_mission_lesson_promotion_receipt:"
            f"{receipt['promotion_receipt_id']}"
        ),
        "auto_promotion_used": receipt["auto_promotion_used"],
        "llm_decided_promotion": receipt["llm_decided_promotion"],
        "lesson_schema_version": lesson["schema_version"],
        "lesson_advisory_only": lesson["advisory_only"],
        "lesson_usable_in_scenario_design": lesson["usable_in_scenario_design"],
        "lesson_usable_as_scorecard_evidence": lesson[
            "usable_as_scorecard_evidence"
        ],
        "lesson_usable_as_verifier_input": lesson["usable_as_verifier_input"],
        "lesson_usable_as_success_proof": lesson["usable_as_success_proof"],
        "lesson_applies_to_matching_envelope": applies,
        "valid_for_episode_schema_versions": lesson[
            "valid_for_episode_schema_versions"
        ],
        "verifier_contract_schema_version": verifier_contract.schema_version,
        "verifier_contract_id": verifier_contract.contract_id,
        "verifier_contract_ref": f"verifier_contract:{verifier_contract.contract_id}",
        "verifier_contract_lesson_influenced": verifier_contract.lesson_influenced,
        "artifacts_persisted": all(
            key in artifacts
            for key in (
                "delivery_mission_lesson_candidate",
                "delivery_mission_lesson_promotion_receipt",
                "delivery_mission_lesson",
                "verifier_contract",
            )
        ),
        "invariants": {
            "lessons_are_never_authority": True,
            "candidate_verifier_predicate_change_proposed": candidate[
                "verifier_predicate_change_proposed"
            ],
            "lesson_verifier_predicate_change_proposed": lesson[
                "verifier_predicate_change_proposed"
            ],
            "physical_execution_invoked": lesson["physical_execution_invoked"],
            "hardware_target_allowed": lesson["hardware_target_allowed"],
            "external_dispatch_performed": lesson["external_dispatch_performed"],
        },
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))

    assert summary["candidate_schema_version"] == (
        DELIVERY_MISSION_LESSON_CANDIDATE_SCHEMA_VERSION
    )
    assert summary["receipt_schema_version"] == (
        DELIVERY_MISSION_LESSON_PROMOTION_RECEIPT_SCHEMA_VERSION
    )
    assert summary["lesson_schema_version"] == DELIVERY_MISSION_LESSON_SCHEMA_VERSION
    assert summary["verifier_contract_schema_version"] == VERIFIER_CONTRACT_SCHEMA_VERSION
    assert summary["candidate_advisory_only"] is True
    assert summary["candidate_is_promoted"] is False
    assert summary["candidate_usable_in_scenario_design"] is False
    assert summary["auto_promotion_used"] is False
    assert summary["llm_decided_promotion"] is False
    assert summary["lesson_advisory_only"] is True
    assert summary["lesson_usable_in_scenario_design"] is True
    assert summary["lesson_usable_as_scorecard_evidence"] is False
    assert summary["lesson_usable_as_verifier_input"] is False
    assert summary["lesson_usable_as_success_proof"] is False
    assert summary["lesson_applies_to_matching_envelope"] is True
    assert summary["verifier_contract_lesson_influenced"] is False
    assert summary["artifacts_persisted"] is True
    assert summary["invariants"]["lessons_are_never_authority"] is True
    assert summary["invariants"]["candidate_verifier_predicate_change_proposed"] is False
    assert summary["invariants"]["lesson_verifier_predicate_change_proposed"] is False
    assert summary["invariants"]["physical_execution_invoked"] is False
    assert summary["invariants"]["hardware_target_allowed"] is False
    assert summary["invariants"]["external_dispatch_performed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

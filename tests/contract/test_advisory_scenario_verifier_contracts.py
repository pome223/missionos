"""Scenario-advisory and verifier-invariance contracts from legacy smokes."""

from datetime import timedelta
import json
from pathlib import Path

import pytest

from src.runtime.advisory_lesson_invariance import (
    assert_verifier_ignores_lessons,
    validate_verifier_contract_ref_is_current,
)
from src.runtime.advisory_mission_memory import (
    attach_delivery_mission_lesson_candidate,
    attach_delivery_mission_lesson_promotion,
    current_verifier_contract,
)
from src.runtime.delivery_episode_review import build_delivery_episode_scorecard_review
from src.runtime.delivery_recovery_real_sitl import (
    DeliveryRecoveryRealSITLError,
    build_battery_low_recovery_chain_from_px4_gazebo_summary,
    build_payload_release_retry_recovered_chain_from_px4_gazebo_summaries,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    run_px4_gazebo_mission_scenario_designer,
)
from src.runtime.px4_gazebo_sitl_dropoff_verification import (
    build_px4_gazebo_sitl_dropoff_flight_fact,
    build_px4_gazebo_sitl_dropoff_verification,
    build_px4_gazebo_sitl_payload_release_event,
)
from src.runtime.simulated_delivery_episode import SimulatedDeliveryEpisodePhase
from src.runtime.task_store import TaskStore
from tests.fixtures.delivery_artifact_chain import (
    NOW,
    build_completed_delivery_artifact_chain,
)
from tests.fixtures.recovery_outcome_cases import (
    run_logic_only_recovery_outcome_case,
)


CORPUS_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "verifier_corpus"
    / "advisory_lesson_invariance_cases.json"
)


def _promoted_lesson(
    store: TaskStore,
    task_id: str,
    source_id: str,
    *,
    payload_min: float,
) -> dict:
    candidate = attach_delivery_mission_lesson_candidate(
        task_id,
        source_mission_refs=[f"task:{source_id}"],
        source_artifact_refs=[
            "delivery_episode:episode-advisory-contract",
            "delivery_scorecard:scorecard-advisory-contract",
            "delivery_episode_review:review-advisory-contract",
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
    promoted = attach_delivery_mission_lesson_promotion(
        task_id,
        lesson_candidate_ref=(
            f"delivery_mission_lesson_candidate:{candidate['candidate_id']}"
        ),
        operator_id="operator-advisory-contract",
        decision_rationale="Promote advisory scenario-design lesson.",
        decision_at=NOW,
        created_at=NOW,
        task_store_factory=lambda: store,
    )
    return promoted["delivery_mission_lesson"]


@pytest.fixture(scope="module")
def advisory_contract_bundle(tmp_path_factory):
    store = TaskStore(str(tmp_path_factory.mktemp("advisory") / "tasks.db"))
    source = store.create(
        kind="delivery_episode",
        title="completed source mission",
        status="completed",
        artifacts={
            "delivery_episode": {"episode_id": "episode-advisory-contract"},
            "delivery_scorecard": {"scorecard_id": "scorecard-advisory-contract"},
            "delivery_episode_review": {"review_id": "review-advisory-contract"},
        },
    )
    task = store.create(
        kind="advisory_mission_memory",
        title="advisory scenario contract",
        status="running",
    )
    lessons = (
        _promoted_lesson(store, task["task_id"], source["task_id"], payload_min=4.0),
        _promoted_lesson(
            store,
            task["task_id"],
            source["task_id"],
            payload_min=20.0,
        ),
    )
    chain_task = store.create(
        kind="delivery_episode",
        title="verifier invariance chain",
        status="running",
    )
    chain = build_completed_delivery_artifact_chain(
        store=store,
        task_id=chain_task["task_id"],
    )
    return lessons, chain


def test_scenario_proposal_uses_applicable_lesson_without_granting_authority(
    advisory_contract_bundle,
) -> None:
    lessons, _ = advisory_contract_bundle
    result = run_px4_gazebo_mission_scenario_designer(
        prompt="Plan a mountain summit delivery carrying a 5kg payload to 3000m.",
        now=NOW,
        lesson_registry=lessons,
    )
    proposal = result["scenario_proposal"]

    assert len(proposal["used_lesson_refs"]) == 1
    assert len(proposal["ignored_lesson_refs"]) == 1
    assert len(proposal["ignored_lesson_records"]) == 1
    assert len(proposal["suppressed_scenario_candidates"]) == 1
    assert proposal["suppressed_scenario_candidates"][0][
        "suppressing_lesson_ref"
    ] == proposal["used_lesson_refs"][0]
    validate_verifier_contract_ref_is_current(proposal["verifier_contract_ref"])
    assert proposal["lesson_registry_snapshot_hash"].startswith(
        "lesson_registry_snapshot_"
    )
    assert proposal["proposal_uses_lesson_authority_for_judgement"] is False
    assert proposal["proposal_modifies_verifier_predicates"] is False
    assert proposal["physical_execution_invoked"] is False
    assert proposal["hardware_target_allowed"] is False
    assert proposal["gazebo_execution_invoked"] is False
    assert result["summary"]["used_lesson_refs"] == proposal["used_lesson_refs"]
    assert result["summary"]["ignored_lesson_refs"] == proposal["ignored_lesson_refs"]


def _dropoff_verifier(case: dict, contract) -> dict:
    release = None
    if case["release_present"]:
        release_position = case["release_position"]
        release = build_px4_gazebo_sitl_payload_release_event(
            event_source="gazebo_gripper_detach_event",
            payload_id=contract.package_constraints.package_id,
            release_position_x_m=release_position[0],
            release_position_y_m=release_position[1],
            release_position_z_m=release_position[2],
            observed_at=NOW - timedelta(seconds=case["release_age_seconds"]),
        )
    position = case["position"]
    fact = build_px4_gazebo_sitl_dropoff_flight_fact(
        vehicle_id="x500_0",
        dropoff_zone_id=contract.dropoff_location.location_id,
        position_x_m=position[0],
        position_y_m=position[1],
        position_z_m=position[2],
        dropoff_target_x_m=0.0,
        dropoff_target_y_m=0.0,
        mission_item_reached_observed=True,
        mission_item_reached_seq=case["mission_item_seq"],
        mission_item_reached_at=NOW,
        payload_release_event=release,
        observed_at=NOW,
    )
    verification = build_px4_gazebo_sitl_dropoff_verification(
        delivery_mission_contract=contract,
        dropoff_flight_fact=fact,
        payload_release_event=release,
        now=NOW,
    )
    return {"dropoff_verification": verification.model_dump(mode="json")}


def _episode_review_verifier(case: dict, chain) -> dict:
    params = case.get("params") or {}
    episode = chain.episode_artifacts["simulated_delivery_episode"]
    episode = (
        episode.model_dump(mode="json")
        if hasattr(episode, "model_dump")
        else dict(episode)
    )
    if params.get("missing_dropoff"):
        episode["dropoff_verified"] = False
        episode["phase_history"] = [
            phase
            for phase in episode["phase_history"]
            if phase != SimulatedDeliveryEpisodePhase.DROPOFF_VERIFIED.value
        ]
        episode["passed"] = False
    hil = dict(chain.run_artifacts["hil_telemetry_review"])
    if params.get("stale_hil"):
        hil["passed"] = False
        hil["blocked_reasons"] = ["telemetry_stale"]
    gate = dict(chain.run_artifacts["autonomy_gate_result"])
    if params.get("gate_passed") is False:
        gate["passed"] = False
        gate["blocked_reasons"] = ["gate_failed"]
    telemetry = dict(chain.run_artifacts["px4_gazebo_sanitized_telemetry"])
    measurements = dict(telemetry["measurements"])
    if "battery_percent" in params:
        measurements["battery_percent"] = params["battery_percent"]
    if "vehicle_health" in params:
        measurements["vehicle_health"] = params["vehicle_health"]
    telemetry["measurements"] = measurements
    review = build_delivery_episode_scorecard_review(
        delivery_mission_contract=chain.contract,
        simulated_delivery_episode=episode,
        delivery_replay_trace=chain.episode_artifacts["delivery_replay_trace"],
        hil_telemetry_review=hil,
        autonomy_gate_result=gate,
        sanitized_telemetry=telemetry,
        now=NOW,
    )
    return {key: value.model_dump(mode="json") for key, value in review.items()}


def _real_sitl_verifier(case: dict) -> dict:
    try:
        if case["variant"].startswith("battery_low"):
            artifacts = build_battery_low_recovery_chain_from_px4_gazebo_summary(
                case["summary"],
                mission_contract_ref="delivery_mission_contract:invariance",
                recovery_decision_ref="delivery_recovery_decision:invariance",
                operator_status_ref=(
                    "operator_minimal_delivery_simulation_status:invariance"
                ),
                observed_at=NOW,
            )
        else:
            artifacts = (
                build_payload_release_retry_recovered_chain_from_px4_gazebo_summaries(
                    initial_summary=case["initial_summary"],
                    retry_summary=case["retry_summary"],
                    mission_contract_ref="delivery_mission_contract:invariance",
                    recovery_decision_ref="delivery_recovery_decision:invariance",
                    operator_status_ref=(
                        "operator_minimal_delivery_simulation_status:invariance"
                    ),
                    observed_at=NOW,
                )
            )
    except DeliveryRecoveryRealSITLError as exc:
        if not case.get("expect_error"):
            raise
        return {"expected_error": str(exc)}
    if case.get("expect_error"):
        pytest.fail(f"case {case['id']} was expected to fail closed")
    return {
        key: value.model_dump(mode="json")
        for key, value in artifacts.items()
    }


def test_all_verifier_corpus_outputs_ignore_lesson_registry(
    advisory_contract_bundle,
) -> None:
    lessons, chain = advisory_contract_bundle
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))

    def run(case, _lesson_registry):
        if case["kind"] == "dropoff_verification":
            return _dropoff_verifier(case, chain.contract)
        if case["kind"] == "recovery_outcome_logic":
            return run_logic_only_recovery_outcome_case(case)
        if case["kind"] == "delivery_episode_review":
            return _episode_review_verifier(case, chain)
        if case["kind"] == "recovery_real_sitl":
            return _real_sitl_verifier(case)
        raise AssertionError(f"unknown verifier corpus kind: {case['kind']}")

    evidence = assert_verifier_ignores_lessons(
        corpus=corpus,
        verifier_runner=run,
        full_lesson_registry=lessons,
    )
    contract = current_verifier_contract()
    validate_verifier_contract_ref_is_current(
        f"verifier_contract:{contract.contract_id}"
    )

    assert len(evidence) == len(corpus) == 25
    assert {item["case_kind"] for item in evidence} == {
        "dropoff_verification",
        "recovery_outcome_logic",
        "delivery_episode_review",
        "recovery_real_sitl",
    }

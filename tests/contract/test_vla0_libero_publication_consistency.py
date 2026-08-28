from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = REPOSITORY_ROOT / "docs" / "agents" / "evidence"
PUBLICATION_PATH = EVIDENCE_ROOT / "20260825-vla0-libero-clear-fixture-repair-publication.json"
NORMALIZED_OBSERVATION_PATH = (
    EVIDENCE_ROOT / "20260825-vla0-libero-clear-fixture-repair-normalized-observation.json"
)
ORACLE_CONTROL_PATH = (
    EVIDENCE_ROOT / "20260826-vla0-libero-displaced-fixture-oracle-normalized.json"
)
RUNNER_PATH = REPOSITORY_ROOT / "scripts" / "run_vla0_libero_snapshot_recovery.py"
VIDEO_PATH = REPOSITORY_ROOT / "docs" / "assets" / "vla0-libero-clear-fixture-repair.mp4"
POSTER_PATH = REPOSITORY_ROOT / "docs" / "assets" / "vla0-libero-clear-fixture-repair-poster.png"
README_PATH = REPOSITORY_ROOT / "README.md"
REPORT_PATH = REPOSITORY_ROOT / "docs" / "concepts" / "vla-repair-progress.md"
FIXTURE_CONTRACT_PATH = (
    REPOSITORY_ROOT / "docs" / "agents" / "libero-scripted-repair-failure-fixtures.md"
)
STABILITY_EVIDENCE_PATH = (
    EVIDENCE_ROOT / "vla0-libero-seed0-3cm-stability-20260829.json"
)
COSMOS_REPAIR_CONTRACT_PATH = (
    REPOSITORY_ROOT / "docs" / "agents" / "cosmos-policy-libero-repair-resume.md"
)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _compact_markdown(path: Path) -> str:
    return " ".join(path.read_text().split())


def test_vla0_publication_assets_match_bound_digests() -> None:
    publication = _read_json(PUBLICATION_PATH)
    evidence = publication["evidence"]
    assert isinstance(evidence, dict)
    normalized = _read_json(NORMALIZED_OBSERVATION_PATH)
    oracle = _read_json(ORACLE_CONTROL_PATH)
    normalized_without_digest = dict(normalized)
    normalized_digest = normalized_without_digest.pop("normalized_observation_sha256")

    assert evidence["runner_source_sha256"] == _sha256(RUNNER_PATH)
    assert evidence["public_video_sha256"] == _sha256(VIDEO_PATH)
    assert evidence["public_poster_sha256"] == _sha256(POSTER_PATH)
    assert evidence["normalized_public_observation_file"] == NORMALIZED_OBSERVATION_PATH.name
    assert evidence["normalized_public_observation_file_sha256"] == _sha256(
        NORMALIZED_OBSERVATION_PATH
    )
    assert normalized_digest == _canonical_sha256(normalized_without_digest)
    assert evidence["normalized_public_observation_sha256"] == normalized_digest
    oracle_without_digest = dict(oracle)
    oracle_digest = oracle_without_digest.pop("normalized_oracle_control_sha256")
    assert oracle_digest == _canonical_sha256(oracle_without_digest)
    assert evidence["normalized_oracle_control_file"] == ORACLE_CONTROL_PATH.name
    assert evidence["normalized_oracle_control_file_sha256"] == _sha256(
        ORACLE_CONTROL_PATH
    )
    assert evidence["normalized_oracle_control_sha256"] == oracle_digest

    source = normalized["source_evidence"]
    assert isinstance(source, dict)
    assert evidence["result_sha256"] == source["canonical_result_sha256"]
    assert evidence["raw_action_trace_sha256"] == source["raw_action_trace_sha256"]
    assert evidence["frame_capture_sha256"] == source["frame_capture_sha256"]
    assert evidence["official_dataset_stats_sha256"] == source["official_dataset_stats_sha256"]
    assert source["private_raw_result_file_published"] is False
    assert source["raw_action_trace_published"] is False
    assert source["private_frame_sequence_published"] is False
    assert source["normalized_oracle_control_file"] == ORACLE_CONTROL_PATH.name
    assert source["normalized_oracle_control_file_sha256"] == _sha256(
        ORACLE_CONTROL_PATH
    )
    assert source["normalized_oracle_control_sha256"] == oracle_digest


def test_vla0_publication_metrics_reduce_from_normalized_trace() -> None:
    publication = _read_json(PUBLICATION_PATH)
    normalized = _read_json(NORMALIZED_OBSERVATION_PATH)
    measurement = normalized["measurement"]
    governed = publication["governed_repair"]
    fixture = publication["scripted_fixture"]
    assert isinstance(measurement, dict)
    assert isinstance(governed, dict)
    assert isinstance(fixture, dict)

    distances = measurement["target_end_effector_center_distance_metres_by_step"]
    contacts = measurement["target_gripper_contact_observed_by_step"]
    assert isinstance(distances, list)
    assert isinstance(contacts, list)
    assert len(distances) == len(contacts) == 520
    assert all(
        isinstance(value, (int, float)) and not isinstance(value, bool) for value in distances
    )
    assert all(isinstance(value, bool) for value in contacts)

    minimum_distance = min(distances)
    contact_step_count = sum(contacts)
    assert measurement["minimum_target_end_effector_center_distance_metres"] == pytest.approx(
        minimum_distance
    )
    assert governed[
        "target_minimum_end_effector_to_target_center_distance_metres"
    ] == pytest.approx(minimum_distance)
    assert measurement["target_gripper_contact_step_count"] == contact_step_count == 0
    assert governed["target_gripper_contact_step_count"] == contact_step_count

    assert measurement["model_forward_count"] == governed["model_forward_count"] == 520
    assert (
        measurement["selected_simulator_action_count"]
        == governed["selected_simulator_actions_applied"]
        == 520
    )
    assert measurement["frame_capture_record_count"] == evidence_frame_count(publication) == 521
    assert measurement["camera_image_record_count"] == evidence_camera_count(publication) == 1042
    assert (
        measurement["source_goal_predicate_vector"]
        == governed["source_goal_predicate_vector"]
        == fixture["goal_predicate_vector"]
        == [True, False, True]
    )
    assert (
        measurement["final_goal_predicate_vector"]
        == governed["final_goal_predicate_vector"]
        == [True, False, True]
    )
    assert measurement["predicate_improvement_observed"] is False
    assert measurement["preservation_violation_observed"] is False
    assert measurement["preservation_invariant_breach_observed"] is False


def evidence_frame_count(publication: dict[str, object]) -> int:
    evidence = publication["evidence"]
    assert isinstance(evidence, dict)
    return int(evidence["frame_capture_records"])


def evidence_camera_count(publication: dict[str, object]) -> int:
    evidence = publication["evidence"]
    assert isinstance(evidence, dict)
    return int(evidence["camera_image_records"])


def test_vla0_publication_binds_same_interface_recoverability_control() -> None:
    publication = _read_json(PUBLICATION_PATH)
    normalized = _read_json(NORMALIZED_OBSERVATION_PATH)
    fixture = publication["scripted_fixture"]
    capability_gate = normalized["capability_gate"]
    residual = publication["residual_uncertainty"]
    claim_boundary = publication["claim_boundary"]
    governed = publication["governed_repair"]
    oracle = publication["scripted_oracle_control"]
    assert isinstance(fixture, dict)
    assert isinstance(capability_gate, dict)
    assert isinstance(residual, dict)
    assert isinstance(claim_boundary, dict)
    assert isinstance(governed, dict)
    assert isinstance(oracle, dict)

    assert fixture["scripted_oracle_evidence_bound"] is True
    assert fixture["fixture_recoverability_established"] is True
    assert fixture["capability_interpretation_eligible"] is True
    assert publication["publication_gate_satisfied"] is True
    assert publication["capability_interpretation_gate_satisfied"] is True
    assert publication["publication_safe_behavior_observation_gate_satisfied"] is True
    assert publication["result_classification"] == (
        "bounded_recoverable_fixture_repair_not_observed"
    )
    assert capability_gate["fixture_recoverability_established"] is True
    assert capability_gate["capability_interpretation_gate_satisfied"] is True
    assert capability_gate["result_classification"] == (
        "bounded_recoverable_fixture_repair_not_observed"
    )
    assert oracle["same_original_7d_action_interface_used"] is True
    assert oracle["same_maximum_action_budget"] == 520
    assert oracle["actions_applied"] == 517
    assert oracle["success_first_observed_after_action"] == 497
    assert oracle["stable_success_steps_completed"] == 20
    assert oracle["source_goal_predicate_vector"] == [True, False, True]
    assert oracle["terminal_goal_predicate_vector"] == [True, True, True]
    assert oracle["preservation_violation_observed"] is False
    assert oracle["protected_maximum_displacement_metres"] < oracle[
        "protected_maximum_displacement_limit_metres"
    ]
    assert oracle["model_inference_invoked"] is False
    assert oracle["missionos_repair_run"] is False

    assert residual["full_one_step_numeric_parity_established"] is False
    assert residual["adapter_difference_fully_excluded"] is False
    assert residual["mid_episode_distribution_shift_proven"] is False
    assert residual["mid_episode_distribution_shift_hypothesis_only"] is True
    assert claim_boundary["fixture_recoverability_established"] is True
    assert claim_boundary["result_is_unmeasured_as_repair_capability"] is False
    assert claim_boundary["result_is_one_bounded_recoverable_fixture_failure_observation"] is True
    assert governed["target_directed_approach_interpretation"] == (
        "no_sustained_or_meaningful_target_directed_approach_observed"
    )
    review = governed["qualitative_frame_review"]
    assert isinstance(review, dict)
    assert review == {
        "camera": "agentview_image",
        "reviewed_step_start": 0,
        "reviewed_step_end": 520,
        "reviewed_frame_count": 521,
        "sustained_or_meaningful_target_directed_approach_observed": False,
    }


def test_vla0_publication_wording_matches_measured_boundary() -> None:
    readme = _compact_markdown(README_PATH)
    report = _compact_markdown(REPORT_PATH)
    fixture_contract = _compact_markdown(FIXTURE_CONTRACT_PATH)
    publication = _read_json(PUBLICATION_PATH)
    claim_boundary = publication["claim_boundary"]
    assert isinstance(claim_boundary, dict)

    required_phrases = (
        "no sustained or meaningful target-directed approach",
        "gripper-target contact",
        "oracle recovered",
        "517/520",
    )
    for phrase in required_phrases:
        assert phrase in readme
        assert phrase in report
    assert "bounded_recoverable_fixture_repair_not_observed" in readme
    assert "bounded_recoverable_fixture_repair_not_observed" in report
    assert "policy never moved toward the target" not in report
    assert "leading explanation" not in report
    assert "bounded_recoverable_fixture_repair_not_observed" in fixture_contract
    assert "now passes this gate" in fixture_contract
    assert (
        "A privileged oracle recovered the exact scripted 22.7 cm fixture"
        in (claim_boundary["bounded_observation"])
    )


def test_vla0_three_centimetre_stability_publication_keeps_entry_and_completion_separate() -> None:
    evidence = _read_json(STABILITY_EVIDENCE_PATH)
    fresh = evidence["fresh_vla0_trial"]
    replays = evidence["recorded_success_trace_stability_replays"]
    comparison = evidence["bounded_comparison"]
    boundary = evidence["claim_boundary"]
    assert isinstance(fresh, dict)
    assert isinstance(replays, dict)
    assert isinstance(comparison, dict)
    assert isinstance(boundary, dict)

    assert evidence["status"] == (
        "target_engagement_repeated_but_stable_predicate_recovery_not_established"
    )
    assert fresh["first_contact_after_action"] == 67
    assert fresh["contact_observation_count"] == 52
    assert fresh["maximum_target_translation_metres"] == pytest.approx(
        0.14064955496623274
    )
    assert fresh["terminal_goal_predicate_vector"] == [True, False, True]
    assert fresh["predicate_conjunction_observed"] is False
    assert fresh["post_success_stability_admitted"] is False

    assert replays["new_policy_inference_invoked"] is False
    assert replays["conjunction_reproduced_count"] == 2
    assert replays["stable_success_count"] == 0
    runs = replays["runs"]
    assert isinstance(runs, list)
    assert [run["first_success_after_action"] for run in runs] == [84, 83]
    assert [run["stable_success_steps_completed"] for run in runs] == [4, 4]
    assert all(run["terminal_goal_predicate_vector"] == [True, False, True] for run in runs)

    assert comparison["vla0_target_engagement_observed_count"] == 3
    assert comparison["vla0_terminal_conjunction_observed_count"] == 2
    assert comparison["vla0_twenty_step_stable_success_count"] == 0
    assert boundary["stable_vla0_predicate_recovery_established"] is False
    assert boundary["same_world_semantic_repair_established"] is False
    assert boundary["physical_execution_invoked"] is False

    for path in (README_PATH, REPORT_PATH, COSMOS_REPAIR_CONTRACT_PATH):
        text = _compact_markdown(path)
        assert "20-step" in text
        assert "fifth stationary hold step" in text or "fifth" in text
    assert "Target engagement 3/3" in _compact_markdown(README_PATH)
    assert "terminal conjunction 2/3" in _compact_markdown(README_PATH)
    assert "0/2" in _compact_markdown(REPORT_PATH)

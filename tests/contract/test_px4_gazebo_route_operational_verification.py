from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import operational_verification


FIXED_TIME = datetime(2026, 7, 15, 12, 34, 56, tzinfo=timezone.utc)


def _candidate_summary(*, world_hash_match: bool = True) -> dict[str, object]:
    return {
        "route_blocking_candidate_evidence": {
            "observed": {
                "observed": True,
                "route_blocking_candidate": True,
                "candidate_threshold_m": 0.75,
                "min_distance_to_route_m": 0.21,
                "min_distance_to_dropoff_m": 1.8,
                "collision_geometry_observed": True,
                "source_condition_application_ref": "operational-application:1",
                "source_condition_application_verified": True,
                "world_sdf_hash_match": world_hash_match,
                "contact_topic_observed": False,
            }
        }
    }


def _verified_chain(
    *,
    world_hash_match: bool = True,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    report = operational_verification.build_operational_incident_report(
        route_blocking_candidate_summary=_candidate_summary(
            world_hash_match=world_hash_match
        ),
        collision_obstacle_summary={
            "collision_obstacle_evidence": {"observed": {"pose_observed": True}}
        },
        requested=True,
        observed_at=FIXED_TIME,
    )
    traffic = operational_verification.build_traffic_conflict_verification(
        operational_incident_report_summary=report,
        requested=True,
        observed_at=FIXED_TIME,
    )
    blocking = operational_verification.build_route_blocking_verification(
        traffic_conflict_verification_summary=traffic,
        requested=True,
        observed_at=FIXED_TIME,
    )
    return report, traffic, blocking


def test_not_requested_chain_creates_no_gate_or_completion_claim() -> None:
    report = operational_verification.build_operational_incident_report(
        route_blocking_candidate_summary={},
        collision_obstacle_summary={},
        requested=False,
        observed_at=FIXED_TIME,
    )
    traffic = operational_verification.build_traffic_conflict_verification(
        operational_incident_report_summary=report,
        requested=False,
        observed_at=FIXED_TIME,
    )
    blocking = operational_verification.build_route_blocking_verification(
        traffic_conflict_verification_summary=traffic,
        requested=False,
        observed_at=FIXED_TIME,
    )

    report_artifact = report["operational_incident_report"]
    traffic_artifact = traffic["traffic_conflict_verification"]
    blocking_artifact = blocking["route_blocking_verification"]
    assert report_artifact["report_status"] == "not_requested"
    assert traffic_artifact["verification_status"] == "not_requested"
    assert blocking_artifact["verification_status"] == "not_requested"
    for artifact in (report_artifact, traffic_artifact, blocking_artifact):
        assert artifact["observed_at"] == FIXED_TIME.isoformat()
        assert artifact["task_status_mutated"] is False
        assert artifact["physical_execution_invoked"] is False
        assert artifact["delivery_completion_claimed"] is False
    assert blocking_artifact["auto_gate"] is False


def test_missing_candidate_evidence_fails_closed_through_chain() -> None:
    report = operational_verification.build_operational_incident_report(
        route_blocking_candidate_summary={},
        collision_obstacle_summary={},
        requested=True,
        observed_at=FIXED_TIME,
    )
    traffic = operational_verification.build_traffic_conflict_verification(
        operational_incident_report_summary=report,
        requested=True,
        observed_at=FIXED_TIME,
    )
    blocking = operational_verification.build_route_blocking_verification(
        traffic_conflict_verification_summary=traffic,
        requested=True,
        observed_at=FIXED_TIME,
    )

    assert report["operational_incident_report"]["report_status"] == (
        "operational_incident_report_not_observed"
    )
    assert report["operational_incident_report"]["unsupported_reasons"] == [
        "route_blocking_candidate_evidence_missing"
    ]
    assert traffic["traffic_conflict_verification"]["verification_status"] == (
        "traffic_conflict_not_verified"
    )
    assert blocking["route_blocking_verification"]["verification_status"] == (
        "route_blocking_not_verified"
    )
    assert blocking["route_blocking_verification"]["observed"]["gate_candidate"] is False


def test_verified_chain_remains_operator_review_gate_candidate_only() -> None:
    report, traffic, blocking = _verified_chain()

    report_artifact = report["operational_incident_report"]
    assert report_artifact["report_status"] == "operator_review_required"
    assert report_artifact["incident_verifier"] is False
    assert report_artifact["observed"]["collision_obstacle_pose_observed"] is True

    traffic_artifact = traffic["traffic_conflict_verification"]
    assert traffic_artifact["verification_status"] == "traffic_conflict_verified"
    assert traffic_artifact["dropoff_verifier"] is False
    assert traffic_artifact["delivery_verifier"] is False

    blocking_artifact = blocking["route_blocking_verification"]
    assert blocking_artifact["verification_status"] == "route_blocking_verified"
    assert blocking_artifact["gate_candidate_only"] is True
    assert blocking_artifact["auto_gate"] is False
    assert blocking_artifact["task_status_mutated"] is False
    assert blocking_artifact["gate_status_mutated"] is False
    assert blocking_artifact["physical_execution_invoked"] is False
    assert blocking_artifact["delivery_completion_claimed"] is False
    assert blocking_artifact["observed"]["gate_candidate"] is True
    assert blocking_artifact["observed"]["operator_review_required"] is True


def test_world_hash_mismatch_cannot_verify_conflict_or_route_blocking() -> None:
    report, traffic, blocking = _verified_chain(world_hash_match=False)

    assert report["operational_incident_report"]["report_status"] == (
        "operator_review_required"
    )
    assert traffic["traffic_conflict_verification"]["verification_status"] == (
        "traffic_conflict_not_verified"
    )
    assert traffic["traffic_conflict_verification"]["observed"][
        "traffic_conflict_verified"
    ] is False
    assert blocking["route_blocking_verification"]["verification_status"] == (
        "route_blocking_not_verified"
    )


def test_entrypoint_delegates_current_operational_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(route_entrypoint, "_collision_obstacle_requested", lambda: True)
    monkeypatch.setattr(
        route_entrypoint,
        "ROUTE_BLOCKING_CANDIDATE_SUMMARY",
        _candidate_summary(),
    )
    monkeypatch.setattr(
        route_entrypoint,
        "COLLISION_OBSTACLE_SUMMARY",
        {"collision_obstacle_evidence": {"observed": {"pose_observed": True}}},
    )

    report = route_entrypoint._operational_incident_report_realism()
    monkeypatch.setattr(route_entrypoint, "OPERATIONAL_INCIDENT_REPORT_SUMMARY", report)
    traffic = route_entrypoint._traffic_conflict_verification_realism()
    monkeypatch.setattr(route_entrypoint, "TRAFFIC_CONFLICT_VERIFICATION_SUMMARY", traffic)
    blocking = route_entrypoint._route_blocking_verification_realism()

    assert report["operational_incident_report"]["report_status"] == (
        "operator_review_required"
    )
    assert traffic["traffic_conflict_verification"]["verification_status"] == (
        "traffic_conflict_verified"
    )
    assert blocking["route_blocking_verification"]["verification_status"] == (
        "route_blocking_verified"
    )


def test_alternate_landing_candidate_requires_verified_route_blocking() -> None:
    _, _, blocking = _verified_chain(world_hash_match=False)

    candidate = operational_verification.build_alternate_landing_candidate_evidence(
        route_blocking_verification_summary=blocking,
        operational_realism_summary={},
        requested=True,
        observed_at=FIXED_TIME,
    )["alternate_landing_candidate_evidence"]

    assert candidate["observation_status"] == (
        "alternate_landing_candidate_not_observed"
    )
    assert candidate["unsupported_reasons"] == [
        "route_blocking_verification_missing"
    ]
    assert candidate["observed"]["alternate_landing_candidate"] is False
    assert candidate["observed"]["px4_route_changed"] is False
    assert candidate["observed"]["rth_commanded"] is False


def test_alternate_landing_candidate_is_projection_only() -> None:
    _, _, blocking = _verified_chain()

    candidate = operational_verification.build_alternate_landing_candidate_evidence(
        route_blocking_verification_summary=blocking,
        operational_realism_summary={
            "alternate_landing_profile": {
                "candidates": [
                    {
                        "candidate_id": "alternate-zone:1",
                        "position_xy_m": [-2.0, 3.5],
                    }
                ]
            }
        },
        requested=True,
        observed_at=FIXED_TIME,
    )["alternate_landing_candidate_evidence"]

    assert candidate["observation_status"] == "alternate_landing_candidate_observed"
    assert candidate["observed_at"] == FIXED_TIME.isoformat()
    assert candidate["candidate_only"] is True
    assert candidate["px4_behavior_applicator"] is False
    assert candidate["rth_behavior_observer"] is False
    assert candidate["auto_gate"] is False
    assert candidate["task_status_mutated"] is False
    assert candidate["gate_status_mutated"] is False
    assert candidate["physical_execution_invoked"] is False
    assert candidate["delivery_completion_claimed"] is False
    assert candidate["observed"]["candidate_id"] == "alternate-zone:1"
    assert candidate["observed"]["candidate_xy_m"] == [-2.0, 3.5]
    assert candidate["observed"]["operator_review_required"] is True
    assert candidate["observed"]["px4_route_changed"] is False
    assert candidate["observed"]["land_commanded"] is False
    assert candidate["observed"]["alternate_landing_behavior_observed"] is False


def test_alternate_landing_candidate_not_requested_is_empty() -> None:
    candidate = operational_verification.build_alternate_landing_candidate_evidence(
        route_blocking_verification_summary={},
        operational_realism_summary={},
        requested=False,
        observed_at=FIXED_TIME,
    )["alternate_landing_candidate_evidence"]

    assert candidate["observation_status"] == "not_requested"
    assert candidate["requested_present"] is False
    assert candidate["observed"] == {}
    assert candidate["delivery_completion_claimed"] is False


def test_entrypoint_delegates_alternate_landing_candidate_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, blocking = _verified_chain()
    monkeypatch.setattr(route_entrypoint, "_collision_obstacle_requested", lambda: True)
    monkeypatch.setattr(route_entrypoint, "_alternate_landing_marker_requested", lambda: True)
    monkeypatch.setattr(
        route_entrypoint,
        "ROUTE_BLOCKING_VERIFICATION_SUMMARY",
        blocking,
    )
    monkeypatch.setattr(
        route_entrypoint,
        "OPERATIONAL_REALISM_SUMMARY",
        {
            "alternate_landing_profile": {
                "candidates": [
                    {
                        "candidate_id": "alternate-zone:1",
                        "position_xy_m": [-2.0, 3.5],
                    }
                ]
            }
        },
    )

    candidate = route_entrypoint._alternate_landing_candidate_evidence_realism()[
        "alternate_landing_candidate_evidence"
    ]

    assert candidate["observation_status"] == "alternate_landing_candidate_observed"
    assert candidate["candidate_only"] is True
    assert candidate["observed"]["candidate_xy_m"] == [-2.0, 3.5]
    assert candidate["observed"]["px4_route_changed"] is False

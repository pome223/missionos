from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import contact_integration


FIXED_TIME = datetime(2026, 7, 16, 17, 0, tzinfo=timezone.utc)


def _assert_non_authoritative(artifact: dict[str, Any]) -> None:
    assert artifact.get("physical_execution_invoked") is False
    assert artifact.get("delivery_completion_claimed") is False
    assert artifact.get("task_status_mutated", False) is False
    assert artifact.get("gate_status_mutated", False) is False
    assert artifact.get("auto_gate", False) is False


def _observed_sidecar_summary() -> dict[str, Any]:
    return {
        "schema_version": "collision_contact_event_smoke.v1",
        "status": "completed",
        "artifact_dir": "fixture/contact-sidecar",
        "artifacts": {
            "collision_contact_event_evidence": {
                "evidence_id": "fixture:contact-evidence",
                "observed": {
                    "contact_topic_observed": True,
                    "contact_event_observed": True,
                    "collision_names": ["x500_0::base_link", "fixture::obstacle"],
                },
            },
            "contact_event_incident_evidence": {
                "evidence_id": "fixture:incident-evidence",
                "observation_status": "contact_event_incident_candidate_observed",
                "observed": {
                    "contact_event_incident_candidate": True,
                    "operator_review_required": True,
                },
            },
            "contact_event_scoped_verifier_candidate": {
                "candidate_id": "fixture:verifier-candidate",
                "observed": {"scoped_verifier_candidate": True},
            },
            "contact_event_incident_verification": {
                "schema_version": "contact_event_incident_verification.v1",
                "verification_id": "fixture:incident-verification",
                "verification_scope": "contact_event_incident_only",
                "verification_status": "incident_verified",
                "observed": {"incident_verified": True},
            },
            "operational_incident_report": {
                "report_id": "fixture:incident-report",
                "report_status": "operator_review_required",
                "observed": {"operator_review_required": True},
            },
        },
    }


def _route_candidate_summary() -> dict[str, Any]:
    return {
        "route_blocking_candidate_evidence": {
            "schema_version": "route_blocking_candidate_evidence.v1",
            "evidence_id": "fixture:route-candidate",
            "observation_status": "route_blocking_candidate_observed",
            "observed": {
                "route_blocking_candidate": True,
                "source_condition_application_ref": "fixture:spawn-application",
                "source_condition_application_verified": True,
                "world_sdf_hash_match": True,
                "min_distance_to_route_m": 0.2,
                "candidate_threshold_m": 1.25,
                "collision_geometry_observed": True,
            },
        }
    }


def test_legacy_entrypoint_delegates_contact_projection() -> None:
    assert (
        route_entrypoint._project_horizontal_contact_topic_integration
        is contact_integration.project_horizontal_contact_topic_integration
    )


def test_not_requested_remains_non_authoritative(tmp_path: Path) -> None:
    result = contact_integration.project_horizontal_contact_topic_integration(
        requested=False,
        run_dir=tmp_path,
        sidecar_summary=_observed_sidecar_summary(),
        route_blocking_candidate_summary=_route_candidate_summary(),
        observed_at=FIXED_TIME,
    )

    integration = result["horizontal_route_contact_topic_integration"]
    assert integration["integration_status"] == "not_requested"
    assert integration["observed"] == {}
    assert len(result) == 1
    _assert_non_authoritative(integration)


def test_observed_sidecar_projects_scoped_verification_without_gate(
    tmp_path: Path,
) -> None:
    result = contact_integration.project_horizontal_contact_topic_integration(
        requested=True,
        run_dir=tmp_path,
        sidecar_summary=_observed_sidecar_summary(),
        route_blocking_candidate_summary=_route_candidate_summary(),
        observed_at=FIXED_TIME,
    )

    integration = result["horizontal_route_contact_topic_integration"]
    incident = result["horizontal_route_contact_incident_verification"]
    traffic = result["horizontal_route_incident_informed_traffic_conflict_verification"]
    blocking = result["horizontal_route_incident_informed_route_blocking_verification"]
    assert integration["integration_status"] == "sidecar_contact_event_observed"
    assert integration["observed"]["contact_event_observed"] is True
    assert integration["observed"]["incident_verified"] is True
    assert incident["verification_status"] == "incident_verified"
    assert traffic["verification_status"] == "traffic_conflict_verified"
    assert blocking["verification_status"] == "route_blocking_verified"
    assert blocking["gate_candidate_only"] is True
    assert blocking["auto_gate"] is False
    assert blocking["task_status_mutated"] is False
    for artifact in result.values():
        _assert_non_authoritative(artifact)


def test_unverified_sidecar_incident_fails_closed(tmp_path: Path) -> None:
    sidecar = _observed_sidecar_summary()
    verification = sidecar["artifacts"]["contact_event_incident_verification"]
    verification["verification_status"] = "incident_not_verified"
    verification["observed"]["incident_verified"] = False

    result = contact_integration.project_horizontal_contact_topic_integration(
        requested=True,
        run_dir=tmp_path,
        sidecar_summary=sidecar,
        route_blocking_candidate_summary=_route_candidate_summary(),
        observed_at=FIXED_TIME,
    )

    incident = result["horizontal_route_contact_incident_verification"]
    traffic = result["horizontal_route_incident_informed_traffic_conflict_verification"]
    blocking = result["horizontal_route_incident_informed_route_blocking_verification"]
    assert incident["verification_status"] == "incident_not_verified"
    assert traffic["verification_status"] == "traffic_conflict_not_verified"
    assert blocking["verification_status"] == "route_blocking_not_verified"
    assert (
        "incident_informed_traffic_conflict_not_verified"
        in blocking["source_verifier_fail_closed_reasons"]
    )
    _assert_non_authoritative(incident)
    _assert_non_authoritative(traffic)
    _assert_non_authoritative(blocking)

"""Contact-sidecar projection for the opt-in PX4/Gazebo route runtime.

The sidecar execution remains in the production entrypoint. This module accepts
its already-observed summary and projects candidate, incident, traffic, and
route-blocking artifacts without creating approval, dispatch, task, or gate
authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any


def _observed_at_text(value: datetime | None) -> str:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc).isoformat()


def project_horizontal_contact_topic_integration(
    *,
    requested: bool,
    run_dir: Path,
    sidecar_summary: Mapping[str, Any] | None,
    route_blocking_candidate_summary: Mapping[str, Any] | None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    requested = bool(requested)
    if not requested:
        return {
            "horizontal_route_contact_topic_integration": {
                "schema_version": "horizontal_route_contact_topic_integration.v1",
                "condition_kind": "horizontal_route_contact_topic_integration",
                "integration_status": "not_requested",
                "requested": False,
                "observed": {},
                "horizontal_route_world_contact_sensor_injected": False,
                "route_execution_mutated": False,
                "task_status_mutated": False,
                "gate_status_mutated": False,
                "delivery_completion_claimed": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            }
        }

    sidecar_summary = dict(sidecar_summary or {})
    artifacts = sidecar_summary.get("artifacts") or {}
    contact_evidence = artifacts.get("collision_contact_event_evidence") or {}
    contact_observed = contact_evidence.get("observed") or {}
    incident_evidence = artifacts.get("contact_event_incident_evidence") or {}
    incident_observed = incident_evidence.get("observed") or {}
    sidecar_verifier_candidate = artifacts.get("contact_event_scoped_verifier_candidate") or {}
    sidecar_verifier_observed = sidecar_verifier_candidate.get("observed") or {}
    sidecar_incident_verification = artifacts.get("contact_event_incident_verification") or {}
    sidecar_incident_verification_observed = sidecar_incident_verification.get("observed") or {}
    sidecar_incident_verified = (
        sidecar_incident_verification.get("schema_version")
        == "contact_event_incident_verification.v1"
        and sidecar_incident_verification.get("verification_scope") == "contact_event_incident_only"
        and sidecar_incident_verification.get("verification_status") == "incident_verified"
        and sidecar_incident_verification_observed.get("incident_verified") is True
    )
    report = artifacts.get("operational_incident_report") or {}
    report_observed = report.get("observed") or {}
    contact_event_observed = bool(contact_observed.get("contact_event_observed"))
    sidecar_artifact_dir = str(sidecar_summary.get("artifact_dir") or "")
    sidecar_ref_hash = hashlib.sha256(
        f"{run_dir.resolve()}::{sidecar_artifact_dir}".encode("utf-8")
    ).hexdigest()
    horizontal_incident_evidence = {
        "schema_version": "horizontal_route_contact_event_incident_evidence.v1",
        "evidence_id": (
            f"horizontal_route_contact_event_incident_evidence:{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_contact_topic_incident_candidate",
        "observation_status": incident_evidence.get(
            "observation_status",
            (
                "contact_event_incident_candidate_observed"
                if contact_event_observed
                else "contact_event_incident_not_observed"
            ),
        ),
        "horizontal_route_contact_topic_integration_ref": (
            f"horizontal_route_contact_topic_integration:{sidecar_ref_hash[:16]}"
        ),
        "sidecar_contact_event_incident_evidence_ref": incident_evidence.get("evidence_id", ""),
        "sidecar_collision_contact_event_evidence_ref": contact_evidence.get("evidence_id", ""),
        "sidecar_artifact_dir": sidecar_artifact_dir,
        "sidecar_artifact_dir_sha256": sidecar_ref_hash,
        "observed": {
            "source": "horizontal_route_contact_topic_integration",
            "observed": contact_event_observed,
            "contact_topic_observed": bool(contact_observed.get("contact_topic_observed")),
            "contact_event_observed": contact_event_observed,
            "contact_event_incident_candidate": bool(
                incident_observed.get("contact_event_incident_candidate")
            ),
            "operator_review_required": bool(
                incident_observed.get("operator_review_required")
                or report_observed.get("operator_review_required")
            ),
            "collision_names": contact_observed.get("collision_names") or [],
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "source_sidecar_contact_event_incident_evidence": incident_evidence,
        "source_sidecar_collision_contact_event_evidence": contact_evidence,
        "candidate_only": True,
        "operator_review_report": True,
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": _observed_at_text(observed_at),
    }
    horizontal_report = {
        "schema_version": "horizontal_route_contact_operational_incident_report.v1",
        "report_id": (
            f"horizontal_route_contact_operational_incident_report:{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_contact_topic_incident_report",
        "report_status": report.get(
            "report_status",
            (
                "operator_review_required"
                if horizontal_incident_evidence["observed"]["operator_review_required"]
                else "not_observed"
            ),
        ),
        "horizontal_route_contact_event_incident_evidence_ref": (
            horizontal_incident_evidence["evidence_id"]
        ),
        "sidecar_operational_incident_report_ref": report.get("report_id", ""),
        "sidecar_artifact_dir": sidecar_artifact_dir,
        "observed": {
            "source": "horizontal_route_contact_event_incident_evidence",
            "observed": contact_event_observed,
            "operator_review_required": horizontal_incident_evidence["observed"][
                "operator_review_required"
            ],
            "contact_event_incident_candidate": horizontal_incident_evidence["observed"][
                "contact_event_incident_candidate"
            ],
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "source_sidecar_operational_incident_report": report,
        "operator_review_required": horizontal_incident_evidence["observed"][
            "operator_review_required"
        ],
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": _observed_at_text(observed_at),
    }
    horizontal_verifier_candidate = {
        "schema_version": "horizontal_route_contact_scoped_verifier_candidate.v1",
        "candidate_id": (
            f"horizontal_route_contact_scoped_verifier_candidate:{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_contact_operator_review_verifier_candidate",
        "candidate_status": (
            "operator_review_candidate" if contact_event_observed else "not_observed"
        ),
        "observation_status": (
            "operator_review_candidate" if contact_event_observed else "not_observed"
        ),
        "horizontal_route_contact_topic_integration_ref": (
            f"horizontal_route_contact_topic_integration:{sidecar_ref_hash[:16]}"
        ),
        "horizontal_route_contact_event_incident_evidence_ref": (
            horizontal_incident_evidence["evidence_id"]
        ),
        "sidecar_scoped_verifier_candidate_ref": sidecar_verifier_candidate.get("candidate_id", ""),
        "sidecar_artifact_dir": sidecar_artifact_dir,
        "observed": {
            "source": "horizontal_route_contact_event_incident_evidence",
            "observed": contact_event_observed,
            "contact_event_observed": contact_event_observed,
            "collision_names": contact_observed.get("collision_names") or [],
            "sidecar_scoped_verifier_candidate": bool(
                sidecar_verifier_observed.get("scoped_verifier_candidate")
            ),
            "scoped_verifier_candidate": contact_event_observed,
            "operator_review_required": horizontal_incident_evidence["observed"][
                "operator_review_required"
            ],
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "source_sidecar_scoped_verifier_candidate": sidecar_verifier_candidate,
        "candidate_only": True,
        "operator_review_required": horizontal_incident_evidence["observed"][
            "operator_review_required"
        ],
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": _observed_at_text(observed_at),
    }
    horizontal_incident_verification = {
        "schema_version": "horizontal_route_contact_incident_verification.v1",
        "verification_id": (
            f"horizontal_route_contact_incident_verification:{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_contact_scoped_incident_verifier",
        "verification_status": (
            "incident_verified" if sidecar_incident_verified else "incident_not_verified"
        ),
        "verification_scope": "contact_event_incident_only",
        "horizontal_route_contact_scoped_verifier_candidate_ref": (
            horizontal_verifier_candidate["candidate_id"]
        ),
        "horizontal_route_contact_event_incident_evidence_ref": (
            horizontal_incident_evidence["evidence_id"]
        ),
        "sidecar_contact_event_incident_verification_ref": (
            sidecar_incident_verification.get("verification_id", "")
        ),
        "sidecar_artifact_dir": sidecar_artifact_dir,
        "observed": {
            "source": "horizontal_route_contact_scoped_verifier_candidate",
            "observed": contact_event_observed,
            "contact_event_observed": contact_event_observed,
            "collision_names": contact_observed.get("collision_names") or [],
            "sidecar_incident_verified": bool(
                sidecar_incident_verification_observed.get("incident_verified")
            ),
            "scoped_verifier_candidate": contact_event_observed,
            "operator_review_required": horizontal_incident_evidence["observed"][
                "operator_review_required"
            ],
            "incident_verified": sidecar_incident_verified,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        },
        "source_sidecar_contact_event_incident_verification": (sidecar_incident_verification),
        "source_verifier_fail_closed_reason": (
            ""
            if sidecar_incident_verified
            else "sidecar_contact_event_incident_verification_not_verified"
        ),
        "operator_review_required": horizontal_incident_evidence["observed"][
            "operator_review_required"
        ],
        "incident_verifier": True,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": _observed_at_text(observed_at),
    }
    horizontal_incident_verification_observed = horizontal_incident_verification["observed"]
    incident_informed_traffic_verified = bool(
        horizontal_incident_verification_observed["incident_verified"]
        and contact_event_observed
        and (contact_observed.get("collision_names") or [])
    )
    horizontal_incident_informed_traffic_conflict_verification = {
        "schema_version": ("horizontal_route_incident_informed_traffic_conflict_verification.v1"),
        "verification_id": (
            "horizontal_route_incident_informed_traffic_conflict_verification:"
            f"{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_incident_informed_traffic_conflict",
        "verification_status": (
            "traffic_conflict_verified"
            if incident_informed_traffic_verified
            else "traffic_conflict_not_verified"
        ),
        "verification_scope": "incident_informed_traffic_conflict_only",
        "horizontal_route_contact_incident_verification_ref": (
            horizontal_incident_verification["verification_id"]
        ),
        "horizontal_route_contact_topic_integration_ref": (
            f"horizontal_route_contact_topic_integration:{sidecar_ref_hash[:16]}"
        ),
        "observed": {
            "source": "horizontal_route_contact_incident_verification",
            "observed": incident_informed_traffic_verified,
            "incident_verified": horizontal_incident_verification_observed["incident_verified"],
            "contact_event_observed": contact_event_observed,
            "collision_names": contact_observed.get("collision_names") or [],
            "traffic_conflict_verified": incident_informed_traffic_verified,
            "route_blocking_verified": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "dropoff_verified": False,
            "delivery_completion_claimed": False,
        },
        "source_horizontal_route_contact_incident_verification": (horizontal_incident_verification),
        "incident_verifier": False,
        "route_blocking_verifier": False,
        "traffic_conflict_verifier": True,
        "route_blocking_candidate": False,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verifier": False,
        "delivery_verifier": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": _observed_at_text(observed_at),
    }
    route_candidate_evidence = (route_blocking_candidate_summary or {}).get(
        "route_blocking_candidate_evidence",
        {},
    )
    route_candidate_observed = route_candidate_evidence.get("observed") or {}
    incident_traffic_observed = horizontal_incident_informed_traffic_conflict_verification[
        "observed"
    ]
    incident_traffic_source_verified = (
        horizontal_incident_informed_traffic_conflict_verification.get("schema_version")
        == "horizontal_route_incident_informed_traffic_conflict_verification.v1"
        and horizontal_incident_informed_traffic_conflict_verification.get("verification_scope")
        == "incident_informed_traffic_conflict_only"
        and horizontal_incident_informed_traffic_conflict_verification.get("verification_status")
        == "traffic_conflict_verified"
        and incident_traffic_observed.get("traffic_conflict_verified") is True
    )
    route_candidate_source_verified = (
        route_candidate_evidence.get("schema_version") == "route_blocking_candidate_evidence.v1"
        and route_candidate_evidence.get("observation_status")
        == "route_blocking_candidate_observed"
        and route_candidate_observed.get("route_blocking_candidate") is True
        and route_candidate_observed.get("source_condition_application_verified") is True
        and route_candidate_observed.get("world_sdf_hash_match") is True
        and isinstance(route_candidate_observed.get("min_distance_to_route_m"), (int, float))
        and isinstance(route_candidate_observed.get("candidate_threshold_m"), (int, float))
    )
    incident_informed_route_blocking_verified = bool(
        incident_traffic_source_verified and route_candidate_source_verified
    )
    route_blocking_fail_closed_reasons: list[str] = []
    if not incident_traffic_source_verified:
        route_blocking_fail_closed_reasons.append("incident_informed_traffic_conflict_not_verified")
    if not route_candidate_source_verified:
        route_blocking_fail_closed_reasons.append(
            "source_bound_route_blocking_candidate_evidence_not_verified"
        )
    horizontal_incident_informed_route_blocking_verification = {
        "schema_version": ("horizontal_route_incident_informed_route_blocking_verification.v1"),
        "verification_id": (
            "horizontal_route_incident_informed_route_blocking_verification:"
            f"{sidecar_ref_hash[:16]}"
        ),
        "condition_kind": "horizontal_route_incident_informed_route_blocking",
        "verification_status": (
            "route_blocking_verified"
            if incident_informed_route_blocking_verified
            else "route_blocking_not_verified"
        ),
        "verification_scope": "incident_informed_route_obstruction_only",
        "horizontal_route_incident_informed_traffic_conflict_verification_ref": (
            horizontal_incident_informed_traffic_conflict_verification["verification_id"]
        ),
        "route_blocking_candidate_evidence_ref": route_candidate_evidence.get("evidence_id", ""),
        "horizontal_route_contact_topic_integration_ref": (
            f"horizontal_route_contact_topic_integration:{sidecar_ref_hash[:16]}"
        ),
        "observed": {
            "source": "horizontal_route_incident_informed_traffic_conflict_and_route_candidate",
            "observed": incident_informed_route_blocking_verified,
            "traffic_conflict_verified": incident_traffic_observed.get("traffic_conflict_verified")
            is True,
            "route_blocking_candidate": bool(
                route_candidate_observed.get("route_blocking_candidate")
            ),
            "source_condition_application_ref": route_candidate_observed.get(
                "source_condition_application_ref", ""
            ),
            "source_condition_application_verified": bool(
                route_candidate_observed.get("source_condition_application_verified")
            ),
            "world_sdf_hash_match": bool(route_candidate_observed.get("world_sdf_hash_match")),
            "route_blocking_verified": incident_informed_route_blocking_verified,
            "min_distance_to_route_m": route_candidate_observed.get("min_distance_to_route_m"),
            "candidate_threshold_m": route_candidate_observed.get("candidate_threshold_m"),
            "collision_geometry_observed": bool(
                route_candidate_observed.get("collision_geometry_observed")
            ),
            "contact_event_observed": contact_event_observed,
            "collision_names": contact_observed.get("collision_names") or [],
            "operator_review_required": incident_informed_route_blocking_verified,
            "gate_candidate": incident_informed_route_blocking_verified,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "dropoff_verified": False,
            "delivery_completion_claimed": False,
        },
        "source_horizontal_route_incident_informed_traffic_conflict_verification": (
            horizontal_incident_informed_traffic_conflict_verification
        ),
        "source_route_blocking_candidate_evidence": route_candidate_evidence,
        "source_verifier_fail_closed_reasons": route_blocking_fail_closed_reasons,
        "incident_verifier": False,
        "traffic_conflict_verifier": False,
        "route_blocking_verifier": True,
        "gate_candidate_only": True,
        "auto_gate": False,
        "task_status_mutated": False,
        "gate_status_mutated": False,
        "dropoff_verifier": False,
        "delivery_verifier": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
        "observed_at": _observed_at_text(observed_at),
    }
    integration_status = (
        "sidecar_contact_event_observed"
        if contact_event_observed
        else "sidecar_contact_event_not_observed"
    )
    return {
        "horizontal_route_contact_topic_integration": {
            "schema_version": "horizontal_route_contact_topic_integration.v1",
            "condition_kind": "horizontal_route_contact_topic_integration",
            "integration_status": integration_status,
            "requested": True,
            "integration_mode": "scoped_sidecar_contact_probe",
            "sidecar_summary_schema_version": sidecar_summary.get("schema_version"),
            "sidecar_status": sidecar_summary.get("status"),
            "sidecar_artifact_dir": sidecar_artifact_dir,
            "sidecar_artifact_dir_sha256": sidecar_ref_hash,
            "horizontal_route_contact_event_incident_evidence_ref": (
                horizontal_incident_evidence["evidence_id"]
            ),
            "horizontal_route_contact_operational_incident_report_ref": (
                horizontal_report["report_id"]
            ),
            "horizontal_route_contact_scoped_verifier_candidate_ref": (
                horizontal_verifier_candidate["candidate_id"]
            ),
            "horizontal_route_contact_incident_verification_ref": (
                horizontal_incident_verification["verification_id"]
            ),
            "horizontal_route_incident_informed_traffic_conflict_verification_ref": (
                horizontal_incident_informed_traffic_conflict_verification["verification_id"]
            ),
            "horizontal_route_incident_informed_route_blocking_verification_ref": (
                horizontal_incident_informed_route_blocking_verification["verification_id"]
            ),
            "horizontal_route_world_contact_sensor_injected": False,
            "horizontal_route_px4_home_boundary_protected": True,
            "reason_horizontal_route_world_not_mutated": (
                "direct contact-system injection perturbs PX4/Gazebo startup/home"
            ),
            "observed": {
                "source": "scoped_gazebo_contact_event_sidecar",
                "contact_topic_observed": bool(contact_observed.get("contact_topic_observed")),
                "contact_event_observed": contact_event_observed,
                "collision_names": contact_observed.get("collision_names") or [],
                "contact_event_incident_candidate": bool(
                    incident_observed.get("contact_event_incident_candidate")
                ),
                "operator_review_required": bool(
                    incident_observed.get("operator_review_required")
                    or report_observed.get("operator_review_required")
                ),
                "scoped_verifier_candidate": contact_event_observed,
                "incident_verified": horizontal_incident_verification["observed"][
                    "incident_verified"
                ],
                "incident_informed_traffic_conflict_verified": (
                    horizontal_incident_informed_traffic_conflict_verification["observed"][
                        "traffic_conflict_verified"
                    ]
                ),
                "incident_informed_route_blocking_verified": (
                    horizontal_incident_informed_route_blocking_verification["observed"][
                        "route_blocking_verified"
                    ]
                ),
                "route_blocking_verified": False,
                "traffic_conflict_verified": False,
                "task_status_mutated": False,
                "gate_status_mutated": False,
                "delivery_completion_claimed": False,
            },
            "sidecar_collision_contact_event_evidence": contact_evidence,
            "sidecar_contact_event_incident_evidence": incident_evidence,
            "sidecar_contact_event_scoped_verifier_candidate": (sidecar_verifier_candidate),
            "sidecar_contact_event_incident_verification": (sidecar_incident_verification),
            "sidecar_operational_incident_report": report,
            "route_execution_mutated": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "observed_at": _observed_at_text(observed_at),
        },
        "horizontal_route_contact_event_incident_evidence": horizontal_incident_evidence,
        "horizontal_route_contact_operational_incident_report": horizontal_report,
        "horizontal_route_contact_scoped_verifier_candidate": (horizontal_verifier_candidate),
        "horizontal_route_contact_incident_verification": (horizontal_incident_verification),
        "horizontal_route_incident_informed_traffic_conflict_verification": (
            horizontal_incident_informed_traffic_conflict_verification
        ),
        "horizontal_route_incident_informed_route_blocking_verification": (
            horizontal_incident_informed_route_blocking_verification
        ),
    }


__all__ = ["project_horizontal_contact_topic_integration"]

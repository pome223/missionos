"""Pure operational evidence projections for the PX4 route runtime.

These builders receive already-observed evidence and derive scoped incident,
traffic-conflict, and route-blocking records. They do not read process state,
mint approval or dispatch authority, mutate a task or gate, or claim delivery
or physical execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


def _observed_at(value: datetime | None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat()


def build_operational_incident_report(
    *,
    route_blocking_candidate_summary: Mapping[str, Any],
    collision_obstacle_summary: Mapping[str, Any],
    requested: bool,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Build an operator-review report from supplied candidate evidence."""

    candidate_evidence = route_blocking_candidate_summary.get(
        "route_blocking_candidate_evidence",
        {},
    )
    candidate_observed = candidate_evidence.get("observed") or {}
    collision_evidence = collision_obstacle_summary.get(
        "collision_obstacle_evidence",
        {},
    )
    collision_observed = collision_evidence.get("observed") or {}
    unsupported_reasons: list[str] = []
    if not requested:
        report_status = "not_requested"
        observed: dict[str, Any] = {}
    elif not candidate_observed.get("observed"):
        report_status = "operational_incident_report_not_observed"
        unsupported_reasons.append("route_blocking_candidate_evidence_missing")
        observed = {
            "source": "route_blocking_candidate_evidence",
            "observed": False,
            "operator_review_required": False,
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        route_blocking_candidate = bool(
            candidate_observed.get("route_blocking_candidate")
        )
        report_status = (
            "operator_review_required"
            if route_blocking_candidate
            else "no_operational_incident_candidate"
        )
        observed = {
            "source": "route_blocking_candidate_evidence",
            "observed": True,
            "route_blocking_candidate": route_blocking_candidate,
            "candidate_threshold_m": candidate_observed.get("candidate_threshold_m"),
            "min_distance_to_route_m": candidate_observed.get(
                "min_distance_to_route_m"
            ),
            "min_distance_to_dropoff_m": candidate_observed.get(
                "min_distance_to_dropoff_m"
            ),
            "collision_geometry_observed": bool(
                candidate_observed.get("collision_geometry_observed")
            ),
            "source_condition_application_ref": candidate_observed.get(
                "source_condition_application_ref",
                "",
            ),
            "source_condition_application_verified": bool(
                candidate_observed.get("source_condition_application_verified")
            ),
            "world_sdf_hash_match": bool(
                candidate_observed.get("world_sdf_hash_match")
            ),
            "contact_topic_observed": bool(
                candidate_observed.get("contact_topic_observed")
            ),
            "collision_obstacle_pose_observed": bool(
                collision_observed.get("pose_observed")
            ),
            "operator_review_required": route_blocking_candidate,
            "auto_gate": False,
            "incident_verified": False,
            "route_blocking_verified": False,
            "traffic_conflict_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "operational_incident_report": {
            "schema_version": "operational_incident_report.v1",
            "report_id": (
                "operational_incident_report:mission_designer_route_blocking_candidate"
            ),
            "condition_kind": "operator_reviewed_route_blocking_candidate",
            "report_status": report_status,
            "input_evidence_refs": [
                "collision_obstacle_evidence:mission_designer_collision_enabled_obstacle",
                "route_blocking_candidate_evidence:mission_designer_collision_obstacle",
            ],
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "operator_review_report": True,
            "auto_gate": False,
            "incident_verifier": False,
            "route_blocking_verifier": False,
            "traffic_conflict_verifier": False,
            "task_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": _observed_at(observed_at),
        }
    }


def build_traffic_conflict_verification(
    *,
    operational_incident_report_summary: Mapping[str, Any],
    requested: bool,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Verify a scoped traffic conflict without changing mission state."""

    incident_report = operational_incident_report_summary.get(
        "operational_incident_report",
        {},
    )
    incident_observed = incident_report.get("observed") or {}
    unsupported_reasons: list[str] = []
    if not requested:
        verification_status = "not_requested"
        observed: dict[str, Any] = {}
    elif incident_report.get("report_status") != "operator_review_required":
        verification_status = "traffic_conflict_not_verified"
        unsupported_reasons.append("operator_review_incident_report_missing")
        observed = {
            "source": "operational_incident_report",
            "observed": False,
            "traffic_conflict_verified": False,
            "route_blocking_verified": False,
            "dropoff_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        traffic_conflict_verified = bool(
            incident_observed.get("route_blocking_candidate")
            and incident_observed.get("collision_geometry_observed")
            and incident_observed.get("source_condition_application_verified")
            and incident_observed.get("world_sdf_hash_match")
        )
        verification_status = (
            "traffic_conflict_verified"
            if traffic_conflict_verified
            else "traffic_conflict_not_verified"
        )
        observed = {
            "source": "operational_incident_report",
            "observed": True,
            "verification_scope": "operational_conflict_only",
            "route_blocking_candidate": bool(
                incident_observed.get("route_blocking_candidate")
            ),
            "collision_geometry_observed": bool(
                incident_observed.get("collision_geometry_observed")
            ),
            "source_condition_application_ref": incident_observed.get(
                "source_condition_application_ref",
                "",
            ),
            "source_condition_application_verified": bool(
                incident_observed.get("source_condition_application_verified")
            ),
            "world_sdf_hash_match": bool(
                incident_observed.get("world_sdf_hash_match")
            ),
            "contact_topic_observed": bool(
                incident_observed.get("contact_topic_observed")
            ),
            "min_distance_to_route_m": incident_observed.get(
                "min_distance_to_route_m"
            ),
            "candidate_threshold_m": incident_observed.get("candidate_threshold_m"),
            "traffic_conflict_verified": traffic_conflict_verified,
            "operator_review_required": True,
            "route_blocking_verified": False,
            "incident_verified": False,
            "dropoff_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "traffic_conflict_verification": {
            "schema_version": "traffic_conflict_verification.v1",
            "verification_id": (
                "traffic_conflict_verification:mission_designer_collision_obstacle"
            ),
            "condition_kind": "scoped_operator_review_traffic_conflict",
            "verification_status": verification_status,
            "operational_incident_report_ref": (
                "operational_incident_report:mission_designer_route_blocking_candidate"
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "verification_scope": "operational_conflict_only",
            "route_blocking_verifier": False,
            "dropoff_verifier": False,
            "delivery_verifier": False,
            "task_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": _observed_at(observed_at),
        }
    }


def build_route_blocking_verification(
    *,
    traffic_conflict_verification_summary: Mapping[str, Any],
    requested: bool,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Verify route blocking as a gate candidate, never as an automatic gate."""

    traffic_verification = traffic_conflict_verification_summary.get(
        "traffic_conflict_verification",
        {},
    )
    traffic_observed = traffic_verification.get("observed") or {}
    unsupported_reasons: list[str] = []
    if not requested:
        verification_status = "not_requested"
        observed: dict[str, Any] = {}
    elif traffic_verification.get("verification_status") != "traffic_conflict_verified":
        verification_status = "route_blocking_not_verified"
        unsupported_reasons.append("traffic_conflict_verification_missing")
        observed = {
            "source": "traffic_conflict_verification",
            "observed": False,
            "route_blocking_verified": False,
            "gate_candidate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        route_blocking_verified = bool(
            traffic_observed.get("traffic_conflict_verified")
            and traffic_observed.get("route_blocking_candidate")
            and traffic_observed.get("source_condition_application_verified")
            and traffic_observed.get("world_sdf_hash_match")
        )
        verification_status = (
            "route_blocking_verified"
            if route_blocking_verified
            else "route_blocking_not_verified"
        )
        observed = {
            "source": "traffic_conflict_verification",
            "observed": True,
            "verification_scope": "operational_route_obstruction_only",
            "route_blocking_verified": route_blocking_verified,
            "traffic_conflict_verified": bool(
                traffic_observed.get("traffic_conflict_verified")
            ),
            "route_blocking_candidate": bool(
                traffic_observed.get("route_blocking_candidate")
            ),
            "source_condition_application_ref": traffic_observed.get(
                "source_condition_application_ref",
                "",
            ),
            "source_condition_application_verified": bool(
                traffic_observed.get("source_condition_application_verified")
            ),
            "world_sdf_hash_match": bool(
                traffic_observed.get("world_sdf_hash_match")
            ),
            "min_distance_to_route_m": traffic_observed.get(
                "min_distance_to_route_m"
            ),
            "candidate_threshold_m": traffic_observed.get("candidate_threshold_m"),
            "gate_candidate": route_blocking_verified,
            "operator_review_required": route_blocking_verified,
            "auto_gate": False,
            "incident_verified": False,
            "dropoff_verified": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "route_blocking_verification": {
            "schema_version": "route_blocking_verification.v1",
            "verification_id": (
                "route_blocking_verification:mission_designer_collision_obstacle"
            ),
            "condition_kind": "scoped_operator_review_route_blocking",
            "verification_status": verification_status,
            "traffic_conflict_verification_ref": (
                "traffic_conflict_verification:mission_designer_collision_obstacle"
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "verification_scope": "operational_route_obstruction_only",
            "gate_candidate_only": True,
            "auto_gate": False,
            "dropoff_verifier": False,
            "delivery_verifier": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": _observed_at(observed_at),
        }
    }


def build_alternate_landing_candidate_evidence(
    *,
    route_blocking_verification_summary: Mapping[str, Any],
    operational_realism_summary: Mapping[str, Any],
    requested: bool,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Project a candidate-only landing option without applying PX4 behavior."""

    route_blocking_verification = route_blocking_verification_summary.get(
        "route_blocking_verification",
        {},
    )
    route_blocking_observed = route_blocking_verification.get("observed") or {}
    alternate_profile = operational_realism_summary.get(
        "alternate_landing_profile",
        {},
    )
    unsupported_reasons: list[str] = []
    if not requested:
        observation_status = "not_requested"
        observed: dict[str, Any] = {}
    elif route_blocking_verification.get(
        "verification_status"
    ) != "route_blocking_verified":
        observation_status = "alternate_landing_candidate_not_observed"
        unsupported_reasons.append("route_blocking_verification_missing")
        observed = {
            "source": "route_blocking_verification",
            "observed": False,
            "alternate_landing_candidate": False,
            "px4_route_changed": False,
            "rth_commanded": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    else:
        alternate_landing_candidate = bool(
            route_blocking_observed.get("route_blocking_verified")
        )
        candidates = alternate_profile.get("candidates")
        candidate_id = None
        candidate_xy_m = None
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            if isinstance(first, dict):
                candidate_id = first.get("candidate_id")
                candidate_xy_m = first.get("position_xy_m") or first.get("xy_m")
        observation_status = (
            "alternate_landing_candidate_observed"
            if alternate_landing_candidate
            else "alternate_landing_candidate_not_observed"
        )
        observed = {
            "source": "route_blocking_verification",
            "observed": True,
            "alternate_landing_candidate": alternate_landing_candidate,
            "candidate_id": candidate_id,
            "candidate_xy_m": candidate_xy_m,
            "route_blocking_verified": bool(
                route_blocking_observed.get("route_blocking_verified")
            ),
            "traffic_conflict_verified": bool(
                route_blocking_observed.get("traffic_conflict_verified")
            ),
            "gate_candidate": bool(route_blocking_observed.get("gate_candidate")),
            "operator_review_required": alternate_landing_candidate,
            "px4_route_changed": False,
            "rth_commanded": False,
            "land_commanded": False,
            "alternate_landing_behavior_observed": False,
            "task_failed": False,
            "delivery_failed": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "delivery_completion_claimed": False,
        }
    return {
        "alternate_landing_candidate_evidence": {
            "schema_version": "alternate_landing_candidate_evidence.v1",
            "candidate_evidence_id": (
                "alternate_landing_candidate_evidence:mission_designer_route_blocking"
            ),
            "condition_kind": "alternate_landing_candidate_from_route_blocking",
            "observation_status": observation_status,
            "requested_present": requested,
            "route_blocking_verification_ref": (
                "route_blocking_verification:mission_designer_collision_obstacle"
            ),
            "observed": observed,
            "unsupported_reasons": unsupported_reasons,
            "candidate_only": True,
            "px4_behavior_applicator": False,
            "rth_behavior_observer": False,
            "auto_gate": False,
            "task_status_mutated": False,
            "gate_status_mutated": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "observed_at": _observed_at(observed_at),
        }
    }


__all__ = [
    "build_alternate_landing_candidate_evidence",
    "build_operational_incident_report",
    "build_route_blocking_verification",
    "build_traffic_conflict_verification",
]

"""Mission Designer contract-envelope advisory artifacts.

This module records the first pre-upload gate for Coordinate Route inputs that
exceed the bounded SITL contract envelope.  It is intentionally advisory-only:
the response blocks upload and asks for operator review, but it does not
dispatch recovery or claim delivery completion.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from typing import Any


ENVELOPE_VIOLATION_ADVISORY_SCHEMA_VERSION = "envelope_violation_advisory.v1"
ENVELOPE_VIOLATION_ADVISORY_REF = (
    "envelope_violation_advisory:mission_designer_coordinate_route"
)

MISSION_DESIGNER_CONTRACT_MAX_PAYLOAD_KG = 5.0
MISSION_DESIGNER_CONTRACT_MAX_WIND_SPEED_MPS = 10.0
MISSION_DESIGNER_WIND_CONTRACT_POLICY_REF = (
    "digital_twin_vehicle_profile:missionos_fixture_quadrotor:2026-06-11.v1"
    "#max_wind_speed_mps"
)


def _utc(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{prefix}_{digest[:12]}"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _ref(prefix: str, value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        raw = str(value.get(key) or "").strip()
        if raw:
            return f"{prefix}:{raw}"
    return ""


def build_envelope_violation_advisory(
    *,
    artifacts: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a Form 2b advisory for hard Coordinate Route envelope violations."""

    observed_at = _utc(now).isoformat()
    coordinate_route = _as_mapping(artifacts.get("mission_designer_coordinate_pair_route"))
    proposal = _as_mapping(artifacts.get("px4_gazebo_mission_scenario_proposal"))
    execution_request = _as_mapping(
        artifacts.get("px4_gazebo_mission_designer_sitl_execution_request")
    )

    if coordinate_route:
        # A chat-restated payload may override the effective route weight (retry
        # flow), but it must never lower an explicit operator-supplied payload
        # past the contract envelope guard. Evaluate the gate against the larger
        # of the effective and the originally operator-requested weight.
        _payload_candidates = [
            value
            for value in (
                _as_float(coordinate_route.get("payload_weight_kg")),
                _as_float(coordinate_route.get("payload_weight_kg_operator_requested")),
            )
            if value is not None
        ]
        requested_payload_kg = max(_payload_candidates) if _payload_candidates else None
    else:
        requested_payload_kg = _as_float(proposal.get("payload_weight_kg"))
    requested_wind_speed_mps = _as_float(coordinate_route.get("wind_speed_mps"))

    violations: list[dict[str, Any]] = []
    if (
        requested_payload_kg is not None
        and requested_payload_kg > MISSION_DESIGNER_CONTRACT_MAX_PAYLOAD_KG
    ):
        violations.append(
            {
                "violation_kind": "payload_weight_exceeds_contract_envelope",
                "requested_value": requested_payload_kg,
                "limit_value": MISSION_DESIGNER_CONTRACT_MAX_PAYLOAD_KG,
                "unit": "kg",
                "margin": round(
                    requested_payload_kg / MISSION_DESIGNER_CONTRACT_MAX_PAYLOAD_KG,
                    6,
                ),
            }
        )
    if (
        requested_wind_speed_mps is not None
        and requested_wind_speed_mps > MISSION_DESIGNER_CONTRACT_MAX_WIND_SPEED_MPS
    ):
        violations.append(
            {
                "violation_kind": "wind_speed_exceeds_contract_envelope",
                "requested_value": requested_wind_speed_mps,
                "limit_value": MISSION_DESIGNER_CONTRACT_MAX_WIND_SPEED_MPS,
                "unit": "m/s",
                "policy_ref": MISSION_DESIGNER_WIND_CONTRACT_POLICY_REF,
                "margin": round(
                    requested_wind_speed_mps
                    / MISSION_DESIGNER_CONTRACT_MAX_WIND_SPEED_MPS,
                    6,
                ),
            }
        )

    violation_observed = bool(violations)
    max_margin = max((float(item["margin"]) for item in violations), default=0.0)
    advisory_id = _stable_id(
        "envelope_violation_advisory",
        {
            "route_id": coordinate_route.get("route_id") or "",
            "execution_request_id": execution_request.get("execution_request_id")
            or "",
            "requested_payload_kg": requested_payload_kg,
            "requested_wind_speed_mps": requested_wind_speed_mps,
            "violations": violations,
            "observed_at": observed_at,
        },
    )

    blocked_reasons = [str(item["violation_kind"]) for item in violations]
    return {
        "schema_version": ENVELOPE_VIOLATION_ADVISORY_SCHEMA_VERSION,
        "advisory_id": advisory_id,
        "advisory_ref": ENVELOPE_VIOLATION_ADVISORY_REF,
        "causal_form": "Form 2b",
        "form2_subtype": "Form 2b",
        "trigger_level": "level_1_direct",
        "progress_counted": violation_observed,
        "advisory_status": (
            "operator_review_required" if violation_observed else "not_requested"
        ),
        "mission_response_kind": "advisory",
        "operator_review_required": violation_observed,
        "automatic_dispatch_suppressed": True,
        "eligible_for_direct_trigger": False,
        "eligible_for_advisory_only": violation_observed,
        "required_action": "review_contract_envelope_violation_before_sitl_upload",
        "forbidden_action": "automatic_sitl_upload_or_recovery_dispatch",
        "mission_response_advisory_reason": (
            "contract_envelope_violation" if violation_observed else "no_violation"
        ),
        "behavior_delta_margin": round(max_margin, 6) if violation_observed else 0.0,
        "marginal_threshold": 1.0,
        "decisive_threshold": 1.0,
        "envelope_violation_observed": violation_observed,
        "execution_upload_blocked": violation_observed,
        "blocked_reasons": blocked_reasons,
        "violations": violations,
        "requested_values": {
            "payload_weight_kg": requested_payload_kg,
            "wind_speed_mps": requested_wind_speed_mps,
        },
        "envelope_limits": {
            "max_payload_weight_kg": MISSION_DESIGNER_CONTRACT_MAX_PAYLOAD_KG,
            "max_wind_speed_mps": MISSION_DESIGNER_CONTRACT_MAX_WIND_SPEED_MPS,
            "max_wind_speed_policy_ref": MISSION_DESIGNER_WIND_CONTRACT_POLICY_REF,
        },
        "advisory_source_refs": {
            "coordinate_route_ref": _ref(
                "mission_designer_coordinate_pair_route", coordinate_route, "route_id"
            ),
            "scenario_proposal_ref": _ref(
                "px4_gazebo_mission_scenario_proposal", proposal, "proposal_id"
            ),
            "execution_request_ref": _ref(
                "px4_gazebo_mission_designer_sitl_execution_request",
                execution_request,
                "execution_request_id",
            ),
        },
        "advisory_lifecycle_state": "pending_operator_review",
        "advisory_consumed_by_ref": None,
        "dispatch": False,
        "recovery": False,
        "approval_chain": False,
        "auto_gate": False,
        "task_status_mutated": violation_observed,
        "gate_status_mutated": False,
        "delivery_completion_claimed": False,
        "payload_dropoff_success_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": observed_at,
    }


def envelope_violation_advisory_requested(advisory: Mapping[str, Any]) -> bool:
    return (
        advisory.get("schema_version") == ENVELOPE_VIOLATION_ADVISORY_SCHEMA_VERSION
        and advisory.get("advisory_status") == "operator_review_required"
        and advisory.get("operator_review_required") is True
        and advisory.get("execution_upload_blocked") is True
    )

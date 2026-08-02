"""Content-bound authority for normal TurtleBot3 route transitions.

This module does not dispatch and does not create approval.  It binds one
pre-existing operator approval to an exact ordered route, then checks that a
normal segment dispatch is both in that route and, after the first segment,
preceded by a satisfied frozen completion predicate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from typing import Any


TURTLEBOT3_ROUTE_AUTHORITY_SCHEMA_VERSION = (
    "missionos_turtlebot3_route_authority.v1"
)
TURTLEBOT3_SEGMENT_TRANSITION_AUTHORITY_SCHEMA_VERSION = (
    "missionos_turtlebot3_segment_transition_authority.v1"
)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _segment_payloads(planned_segments: Sequence[Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for segment in planned_segments:
        if hasattr(segment, "model_dump"):
            value = segment.model_dump(mode="json")
        elif isinstance(segment, Mapping):
            value = dict(segment)
        else:
            raise ValueError("planned segment must be a mapping or model")
        payloads.append(dict(value))
    return payloads


def _route_authority_material(
    *,
    proposal_id: str,
    operator_approval_ref: str,
    approved_scope: str,
    planned_segments: Sequence[Any],
    autonomy_envelope: Mapping[str, Any],
) -> dict[str, Any]:
    segments = _segment_payloads(planned_segments)
    return {
        "schema_version": TURTLEBOT3_ROUTE_AUTHORITY_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "operator_approval_ref": operator_approval_ref,
        "approved_scope": approved_scope,
        "planned_segment_count": len(segments),
        "planned_segments_sha256": _canonical_sha256(segments),
        "ordered_segments": [
            {
                "segment_index": index,
                "segment_ref": f"segment_{index}",
                "goal_sha256": _canonical_sha256(segment),
            }
            for index, segment in enumerate(segments, start=1)
        ],
        "autonomy_envelope_sha256": _canonical_sha256(
            dict(autonomy_envelope)
        ),
    }


def build_turtlebot3_route_authority_binding(
    *,
    proposal_id: str,
    operator_approval_ref: str,
    approved_scope: str,
    planned_segments: Sequence[Any],
    autonomy_envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one operator approval to the exact ordered normal route."""

    material = _route_authority_material(
        proposal_id=proposal_id,
        operator_approval_ref=operator_approval_ref,
        approved_scope=approved_scope,
        planned_segments=planned_segments,
        autonomy_envelope=autonomy_envelope,
    )
    return {
        **material,
        "route_authority_sha256": _canonical_sha256(material),
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "physical_execution_invoked": False,
        "claim_boundary": (
            "This binding records the exact ordered normal route covered by "
            "one pre-existing operator approval. It does not approve recovery "
            "actions, request dispatch, or prove execution."
        ),
    }


def validate_turtlebot3_route_authority_binding(
    *,
    binding: Mapping[str, Any] | None,
    proposal_id: str,
    operator_approval_ref: str,
    approved_scope: str,
    planned_segments: Sequence[Any],
    autonomy_envelope: Mapping[str, Any],
) -> tuple[str, ...]:
    """Validate a stored authority binding against current route material."""

    if not isinstance(binding, Mapping):
        return ("turtlebot3_route_authority_binding_missing",)
    try:
        expected = build_turtlebot3_route_authority_binding(
            proposal_id=proposal_id,
            operator_approval_ref=operator_approval_ref,
            approved_scope=approved_scope,
            planned_segments=planned_segments,
            autonomy_envelope=autonomy_envelope,
        )
    except (TypeError, ValueError):
        return ("turtlebot3_route_authority_current_route_invalid",)

    reasons: list[str] = []
    for field in (
        "schema_version",
        "proposal_id",
        "operator_approval_ref",
        "approved_scope",
        "planned_segment_count",
        "planned_segments_sha256",
        "ordered_segments",
        "autonomy_envelope_sha256",
        "route_authority_sha256",
    ):
        if binding.get(field) != expected[field]:
            reasons.append(f"turtlebot3_route_authority_{field}_mismatch")
    return tuple(dict.fromkeys(reasons))


def evaluate_turtlebot3_segment_transition_authority(
    *,
    binding: Mapping[str, Any] | None,
    proposal_id: str,
    operator_approval_ref: str,
    approved_scope: str,
    planned_segments: Sequence[Any],
    autonomy_envelope: Mapping[str, Any],
    segment_index: int,
    segment_ref: str,
    goal: Any,
    previous_predicate_evaluation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Evaluate authority for one normal segment without creating it."""

    reasons = list(
        validate_turtlebot3_route_authority_binding(
            binding=binding,
            proposal_id=proposal_id,
            operator_approval_ref=operator_approval_ref,
            approved_scope=approved_scope,
            planned_segments=planned_segments,
            autonomy_envelope=autonomy_envelope,
        )
    )
    try:
        segments = _segment_payloads(planned_segments)
        goal_payload = _segment_payloads((goal,))[0]
    except (TypeError, ValueError):
        segments = []
        goal_payload = {}
        reasons.append("turtlebot3_transition_goal_invalid")

    expected_ref = f"segment_{segment_index}"
    if (
        isinstance(segment_index, bool)
        or not isinstance(segment_index, int)
        or segment_index < 1
        or segment_index > len(segments)
    ):
        reasons.append("turtlebot3_transition_segment_index_invalid")
        expected_goal: dict[str, Any] | None = None
    else:
        expected_goal = segments[segment_index - 1]
    if segment_ref != expected_ref:
        reasons.append("turtlebot3_transition_segment_ref_mismatch")
    if expected_goal is not None and _canonical_sha256(
        goal_payload
    ) != _canonical_sha256(expected_goal):
        reasons.append("turtlebot3_transition_goal_mismatch")

    previous_predicate_sha256: str | None = None
    previous_segment_ref: str | None = None
    previous_predicate_satisfied: bool | None = None
    if segment_index == 1:
        if previous_predicate_evaluation is not None:
            reasons.append("turtlebot3_transition_initial_previous_predicate_present")
    else:
        previous_segment_ref = f"segment_{segment_index - 1}"
        if not isinstance(previous_predicate_evaluation, Mapping):
            reasons.append("turtlebot3_transition_previous_predicate_missing")
        else:
            previous_predicate_sha256 = _canonical_sha256(
                dict(previous_predicate_evaluation)
            )
            previous_predicate_satisfied = (
                previous_predicate_evaluation.get("status") == "satisfied"
                and previous_predicate_evaluation.get("completion_claimed") is True
                and previous_predicate_evaluation.get(
                    "predicate_package_evaluated"
                )
                is True
                and previous_predicate_evaluation.get("contract_id")
                == f"{proposal_id}:{previous_segment_ref}"
            )
            if not previous_predicate_satisfied:
                reasons.append(
                    "turtlebot3_transition_previous_predicate_not_satisfied"
                )

    reasons = list(dict.fromkeys(reasons))
    authorized = not reasons
    return {
        "schema_version": (
            TURTLEBOT3_SEGMENT_TRANSITION_AUTHORITY_SCHEMA_VERSION
        ),
        "transition_status": "authorized" if authorized else "blocked",
        "segment_index": segment_index,
        "segment_ref": segment_ref,
        "goal_sha256": (
            _canonical_sha256(goal_payload) if goal_payload else None
        ),
        "previous_segment_ref": previous_segment_ref,
        "previous_predicate_sha256": previous_predicate_sha256,
        "previous_predicate_satisfied": previous_predicate_satisfied,
        "operator_approval_ref": operator_approval_ref,
        "route_authority_sha256": (
            str(binding.get("route_authority_sha256") or "")
            if isinstance(binding, Mapping)
            else ""
        ),
        "dispatch_authority_present": authorized,
        "dispatch_authority_source": (
            "preexisting_route_approval" if authorized else None
        ),
        "blocking_reasons": reasons,
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "physical_execution_invoked": False,
        "claim_boundary": (
            "A satisfied previous predicate is a prerequisite only. Authority "
            "for this normal segment comes from the pre-existing, content-bound "
            "route approval; this evaluation creates no approval or dispatch."
        ),
    }


__all__ = [
    "TURTLEBOT3_ROUTE_AUTHORITY_SCHEMA_VERSION",
    "TURTLEBOT3_SEGMENT_TRANSITION_AUTHORITY_SCHEMA_VERSION",
    "build_turtlebot3_route_authority_binding",
    "evaluate_turtlebot3_segment_transition_authority",
    "validate_turtlebot3_route_authority_binding",
]

"""PX4 compatibility adapter for the backend-neutral Core feasibility contract.

The existing runtime artifact shape remains readable during migration, but its
decision is now passed through MissionOS Core. A legacy artifact without the
embedded Core Hazard State fails closed and must be regenerated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from missionos_core import (
    ActionCandidate,
    CursorOrder,
    DerivedFact,
    EvidenceSourceRef,
    ExtensionVerdict,
    FeasibilityStatus,
    HazardState,
    ObservationCursor,
    ObservedFact,
    PolicyBinding,
    VerificationBasis,
    VerificationItem,
    VerificationItemStatus,
    verify_action_candidate,
)

from src.runtime.px4_gazebo_route.action_feasibility import (
    verify_runtime_recovery_action_candidates as _legacy_verify_candidates,
)
from src.runtime.px4_gazebo_route.action_feasibility import (
    verify_runtime_recovery_action_feasibility as _legacy_verify_candidate,
)
from src.runtime.px4_gazebo_route.hazard_state import (
    build_runtime_recovery_hazard_state as _legacy_build_hazard_state,
)

PX4_CORE_ADAPTER_ID = "missionos.px4.action_feasibility.v1"
PX4_CURSOR_CONTRACT = "missionos.px4.telemetry_cursor.v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def compare_px4_telemetry_cursors(
    earlier: Mapping[str, Any],
    later: Mapping[str, Any],
) -> CursorOrder:
    """Compare both PX4 cursor dimensions without inventing an ordering."""

    if (
        earlier.get("cursor_status") != "complete"
        or later.get("cursor_status") != "complete"
    ):
        return CursorOrder.INCOMPARABLE
    try:
        index_delta = int(later["sample_index"]) - int(earlier["sample_index"])
        elapsed_delta = float(later["elapsed_seconds"]) - float(
            earlier["elapsed_seconds"]
        )
    except (KeyError, TypeError, ValueError):
        return CursorOrder.INCOMPARABLE
    index_sign = (index_delta > 0) - (index_delta < 0)
    elapsed_sign = (elapsed_delta > 1e-9) - (elapsed_delta < -1e-9)
    if index_sign != elapsed_sign:
        return CursorOrder.INCOMPARABLE
    if index_sign < 0:
        return CursorOrder.AFTER
    if index_sign > 0:
        return CursorOrder.BEFORE
    return CursorOrder.EQUAL


def _core_hazard_state_from_runtime(
    hazard_state: Mapping[str, Any],
    *,
    source_content_sha256: str | None = None,
) -> HazardState:
    observed_at = str(hazard_state.get("observed_at") or "")
    source = EvidenceSourceRef(
        source_id=f"hazard:{hazard_state.get('hazard_state_id')}",
        evidence_kind="runtime_hazard_state",
        observed_at=observed_at or None,
        content_sha256=(
            source_content_sha256
            or str(hazard_state.get("hazard_state_sha256") or "")
        ),
        freshness_proof=(
            "adapter_cursor_verified"
            if _mapping(hazard_state.get("freshness")).get(
                "freshness_status"
            )
            == "verified"
            else "unverified"
        ),
    )
    observed_facts: list[ObservedFact] = []
    for name, raw_fact in _mapping(hazard_state.get("observed_facts")).items():
        fact = _mapping(raw_fact)
        observed_facts.append(
            ObservedFact(
                name=str(name),
                value=fact.get("value"),
                unit=fact.get("unit"),
                frame=fact.get("frame"),
                source=source,
            )
        )
    derived_facts = tuple(
        DerivedFact(
            name=str(name),
            value=_mapping(raw_fact).get("value"),
            unit=_mapping(raw_fact).get("unit"),
            source_refs=(source.source_id,),
            derivation_id=str(
                _mapping(raw_fact).get("model_ref")
                or f"{PX4_CORE_ADAPTER_ID}.{name}"
            ),
            uncertainty={
                "legacy_runtime_fact_status": _mapping(raw_fact).get(
                    "fact_status"
                )
            },
        )
        for name, raw_fact in _mapping(
            hazard_state.get("derived_facts")
        ).items()
    )
    return HazardState(
        state_id=str(hazard_state.get("hazard_state_id") or ""),
        collected_at=observed_at,
        cursor=ObservationCursor(
            adapter_id=PX4_CORE_ADAPTER_ID,
            comparison_contract=PX4_CURSOR_CONTRACT,
            value=_mapping(hazard_state.get("telemetry_cursor")),
        ),
        policy_binding=PolicyBinding(
            policy_id=str(hazard_state.get("policy_ref") or ""),
            policy_version="runtime.v1",
            policy_sha256=str(hazard_state.get("policy_sha256") or ""),
        ),
        observed_facts=tuple(observed_facts),
        derived_facts=derived_facts,
        assumptions=tuple(hazard_state.get("model_assumptions") or ()),
    )


def attach_core_hazard_state(
    hazard_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach and hash-bind the Core projection to compatibility evidence."""

    normalized = dict(hazard_state)
    legacy_digest = str(normalized.get("hazard_state_sha256") or "")
    core_hazard_state = _core_hazard_state_from_runtime(
        normalized,
        source_content_sha256=legacy_digest,
    ).to_dict()
    core_digest = _canonical_sha256(core_hazard_state)
    normalized["core_hazard_state"] = core_hazard_state
    normalized["core_hazard_state_sha256"] = core_digest
    normalized["core_adapter_id"] = PX4_CORE_ADAPTER_ID
    state_material = {
        "telemetry_cursor": _mapping(normalized.get("telemetry_cursor")),
        "policy_sha256": normalized.get("policy_sha256"),
        "observed_facts": _mapping(normalized.get("observed_facts")),
        "derived_facts": _mapping(normalized.get("derived_facts")),
        "temperature_model": _mapping(normalized.get("temperature_model")),
        "obstacle_geometry": _mapping(normalized.get("obstacle_geometry")),
        "performance_envelope": _mapping(
            normalized.get("performance_envelope")
        ),
        "core_hazard_state_sha256": core_digest,
    }
    state_digest = _canonical_sha256(state_material)
    normalized["hazard_state_sha256"] = state_digest
    normalized["hazard_state_id"] = f"hazard_state_{state_digest[:12]}"
    return normalized


def build_runtime_recovery_hazard_state(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Build runtime evidence and bind it to the Core contract."""

    return attach_core_hazard_state(_legacy_build_hazard_state(*args, **kwargs))


class _RuntimeVerifierExtension:
    extension_id = PX4_CORE_ADAPTER_ID

    def __init__(self, legacy_result: Mapping[str, Any]) -> None:
        self._result = dict(legacy_result)

    def verify(self, *, hazard_state, candidate) -> ExtensionVerdict:
        status = FeasibilityStatus(
            str(self._result.get("feasibility_status") or "unverified")
        )
        item_id = "px4_bounded_recovery_feasibility"
        return ExtensionVerdict(
            extension_id=self.extension_id,
            status=status,
            blocked_reasons=tuple(self._result.get("blocking_reasons") or ()),
            unverified_reasons=tuple(
                self._result.get("unverified_reasons") or ()
            ),
            measurements={
                "obstacle_clearance_verification": self._result.get(
                    "obstacle_clearance_verification"
                ),
                "projected_terrain_clearance_margin_m": self._result.get(
                    "projected_terrain_clearance_margin_m"
                ),
                "projected_battery_after_action_percent": self._result.get(
                    "projected_battery_after_action_percent"
                ),
            },
            assumptions=tuple(self._result.get("assumptions") or ()),
            verification_items=(
                VerificationItem(
                    item_id=item_id,
                    predicate=(
                        "the bounded PX4 recovery action satisfies the "
                        "declared runtime hazard and policy constraints"
                    ),
                    status=(
                        VerificationItemStatus.BLOCKED
                        if status is FeasibilityStatus.BLOCKED
                        else VerificationItemStatus.PENDING
                        if status is FeasibilityStatus.UNVERIFIED
                        else VerificationItemStatus.PASS
                    ),
                    verification_basis=(
                        VerificationBasis.UNVERIFIED
                        if status is FeasibilityStatus.UNVERIFIED
                        else VerificationBasis.DETERMINISTIC
                    ),
                    evidence_refs=tuple(candidate.evidence_refs),
                ),
            ),
            required_verification_item_ids=(item_id,),
        )


def _fail_closed_result(
    legacy_result: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in legacy_result.items()
        if key not in {"action_feasibility_id", "action_feasibility_sha256"}
    }
    payload["feasibility_status"] = "unverified"
    payload["unverified_reasons"] = list(
        dict.fromkeys([*(payload.get("unverified_reasons") or []), reason])
    )
    payload["core_contract"] = {
        "adapter_id": PX4_CORE_ADAPTER_ID,
        "status": "unverified",
        "reason": reason,
        "approval_created": False,
        "dispatch_authority_created": False,
        "execution_invoked": False,
        "progress_claimed": False,
        "completion_claimed": False,
    }
    digest = _canonical_sha256(payload)
    return {
        **payload,
        "action_feasibility_sha256": digest,
        "action_feasibility_id": f"action_feasibility_{digest[:12]}",
    }


def verify_runtime_recovery_action_feasibility(
    *,
    candidate: Mapping[str, Any],
    hazard_state: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Run the runtime calculation through Core and fail closed on divergence."""

    legacy_result = _legacy_verify_candidate(
        candidate=candidate,
        hazard_state=hazard_state,
        recovery_policy=recovery_policy,
    )
    raw_core_state = hazard_state.get("core_hazard_state")
    if not isinstance(raw_core_state, Mapping):
        return _fail_closed_result(
            legacy_result,
            reason="action_feasibility_core_hazard_state_missing",
        )
    try:
        core_state = HazardState.from_dict(raw_core_state)
    except (KeyError, TypeError, ValueError):
        return _fail_closed_result(
            legacy_result,
            reason="action_feasibility_core_hazard_state_invalid",
        )
    source_id = core_state.observed_facts[0].source.source_id if core_state.observed_facts else ""
    core_candidate = ActionCandidate(
        candidate_id=str(
            candidate.get("candidate_id")
            or candidate.get("action")
            or candidate.get("selected_bounded_action")
            or "candidate"
        ),
        action=str(
            candidate.get("selected_bounded_action")
            or candidate.get("action")
            or candidate.get("compiled_action")
            or ""
        ),
        parameters=(
            _mapping(candidate.get("proposed_parameters"))
            or _mapping(candidate.get("compiled_parameters"))
            or _mapping(candidate.get("parameters"))
        ),
        evidence_refs=(source_id,) if source_id else (),
        extension_inputs={"adapter_id": PX4_CORE_ADAPTER_ID},
    )
    core_result = verify_action_candidate(
        hazard_state=core_state,
        candidate=core_candidate,
        active_policy=core_state.policy_binding,
        evaluated_at=core_state.collected_at,
        extensions=(_RuntimeVerifierExtension(legacy_result),),
    )
    if core_result.status.value != legacy_result.get("feasibility_status"):
        return _fail_closed_result(
            legacy_result,
            reason="action_feasibility_core_status_mismatch",
        )
    payload = {
        key: value
        for key, value in legacy_result.items()
        if key not in {"action_feasibility_id", "action_feasibility_sha256"}
    }
    payload["core_contract"] = {
        "adapter_id": PX4_CORE_ADAPTER_ID,
        "schema_version": core_result.schema_version,
        "hazard_state_schema_version": core_state.schema_version,
        "candidate_schema_version": core_candidate.schema_version,
        "status": core_result.status.value,
        "verification_basis": core_result.verification_basis.value,
        "verification_items": [
            item.to_dict()
            for verdict in core_result.extension_verdicts
            for item in verdict.verification_items
        ],
        "approval_created": core_result.approval_created,
        "dispatch_authority_created": core_result.dispatch_authority_created,
        "execution_invoked": core_result.execution_invoked,
        "progress_claimed": core_result.progress_claimed,
        "completion_claimed": core_result.completion_claimed,
    }
    digest = _canonical_sha256(payload)
    return {
        **payload,
        "action_feasibility_sha256": digest,
        "action_feasibility_id": f"action_feasibility_{digest[:12]}",
    }


def verify_runtime_recovery_action_candidates(
    *,
    candidates: Sequence[Mapping[str, Any]],
    hazard_state: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate every candidate through the same Core-backed path."""

    normalized = [
        dict(candidate) for candidate in candidates if isinstance(candidate, Mapping)
    ]
    evaluations = [
        verify_runtime_recovery_action_feasibility(
            candidate=candidate,
            hazard_state=hazard_state,
            recovery_policy=recovery_policy,
        )
        for candidate in normalized
    ]
    verified = [
        item
        for item in evaluations
        if item["feasibility_status"] == "verified_feasible"
    ]
    legacy_set = _legacy_verify_candidates(
        candidates=(),
        hazard_state=hazard_state,
        recovery_policy=recovery_policy,
    )
    return {
        **legacy_set,
        "evaluations": evaluations,
        "verified_feasible_actions": [item["action"] for item in verified],
        "verified_feasible_candidates": [
            candidate
            for candidate, evaluation in zip(normalized, evaluations)
            if evaluation["feasibility_status"] == "verified_feasible"
        ],
        "core_adapter_id": PX4_CORE_ADAPTER_ID,
    }


__all__ = [
    "PX4_CORE_ADAPTER_ID",
    "PX4_CURSOR_CONTRACT",
    "attach_core_hazard_state",
    "build_runtime_recovery_hazard_state",
    "compare_px4_telemetry_cursors",
    "verify_runtime_recovery_action_candidates",
    "verify_runtime_recovery_action_feasibility",
]

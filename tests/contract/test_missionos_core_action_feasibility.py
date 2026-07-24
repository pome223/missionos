from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from missionos_core import (
    ActionCandidate,
    ActionFeasibilityResult,
    CursorOrder,
    DerivedFact,
    EvidenceSourceRef,
    ExtensionVerdict,
    FactStatus,
    FeasibilityStatus,
    HazardState,
    ModelBinding,
    ObservationCursor,
    ObservedFact,
    PolicyBinding,
    canonical_sha256,
    verify_action_candidate,
)


pytestmark = pytest.mark.contract

EVALUATED_AT = "2026-07-24T03:00:05+00:00"


class IntegerCursorComparator:
    comparator_id = "test.integer.v1"
    comparison_contract = "test.integer.v1"

    def compare(
        self,
        earlier: ObservationCursor,
        later: ObservationCursor,
    ) -> CursorOrder:
        if earlier.adapter_id != later.adapter_id:
            return CursorOrder.INCOMPARABLE
        left = earlier.value.get("sequence")
        right = later.value.get("sequence")
        if not isinstance(left, int) or not isinstance(right, int):
            return CursorOrder.INCOMPARABLE
        if left < right:
            return CursorOrder.BEFORE
        if left > right:
            return CursorOrder.AFTER
        return CursorOrder.EQUAL


class PassingExtension:
    extension_id = "test.geometry.v1"

    def verify(self, *, hazard_state, candidate) -> ExtensionVerdict:
        return ExtensionVerdict(
            extension_id=self.extension_id,
            status=FeasibilityStatus.VERIFIED_FEASIBLE,
            measurements={"minimum_clearance_m": 2.5},
            assumptions=("bounded_path",),
        )


def _fixtures():
    policy_material = {"minimum_clearance_m": 1.0}
    policy = PolicyBinding(
        policy_id="recovery",
        policy_version="1",
        policy_sha256=canonical_sha256(policy_material),
    )
    source = EvidenceSourceRef(
        source_id="telemetry:current",
        evidence_kind="runtime_observation",
        observed_at="2026-07-24T03:00:00+00:00",
        freshness_deadline="2026-07-24T03:00:10+00:00",
        content_sha256="a" * 64,
    )
    model = ModelBinding(
        model_id="energy",
        model_version="1",
        parameters_sha256="b" * 64,
        uncertainty={"fraction": 0.1},
    )
    cursor = ObservationCursor(
        adapter_id="test",
        comparison_contract="test.integer.v1",
        value={"sequence": 11},
    )
    state = HazardState(
        state_id="hazard:one",
        collected_at="2026-07-24T03:00:00+00:00",
        cursor=cursor,
        policy_binding=policy,
        observed_facts=(
            ObservedFact(
                name="wind_speed",
                value=2.0,
                unit="m/s",
                source=source,
            ),
        ),
        derived_facts=(
            DerivedFact(
                name="energy_margin",
                value=12.0,
                unit="percent",
                source_refs=(source.source_id,),
                derivation_id="energy_margin.v1",
                model_binding=model,
                uncertainty={"percent": 1.0},
            ),
        ),
        assumptions=("same_reference_frame",),
    )
    candidate = ActionCandidate(
        candidate_id="candidate:one",
        action="bounded_recovery",
        parameters={"target": [1.0, 2.0, 3.0]},
        evidence_refs=(source.source_id,),
    )
    return policy, source, model, state, candidate


def _verify(*, state=None, candidate=None, policy=None, previous=None, model=None):
    base_policy, _source, base_model, base_state, base_candidate = _fixtures()
    return verify_action_candidate(
        hazard_state=state or base_state,
        candidate=candidate or base_candidate,
        active_policy=policy or base_policy,
        evaluated_at=EVALUATED_AT,
        extensions=(PassingExtension(),),
        previous_cursor=previous,
        cursor_comparator=IntegerCursorComparator(),
        active_model_digests={
            (model or base_model).model_id: (model or base_model).digest
        },
    )


def test_verified_result_serializes_without_creating_authority() -> None:
    result = _verify()
    encoded = json.loads(json.dumps(result.to_dict()))
    decoded = ActionFeasibilityResult.from_dict(encoded)

    assert result.status is FeasibilityStatus.VERIFIED_FEASIBLE
    assert decoded == result
    assert result.blocked_reasons == ()
    assert result.unverified_reasons == ()
    assert encoded["status"] == "verified_feasible"
    assert encoded["approval_created"] is False
    assert encoded["dispatch_authority_created"] is False
    assert encoded["execution_invoked"] is False
    assert encoded["progress_claimed"] is False
    assert encoded["completion_claimed"] is False
    assert encoded["delivery_completion_claimed"] is False
    assert encoded["physical_execution_invoked"] is False


def test_hazard_state_and_candidate_round_trip() -> None:
    _policy, _source, _model, state, candidate = _fixtures()

    decoded_state = HazardState.from_dict(
        json.loads(json.dumps(state.to_dict()))
    )
    decoded_candidate = ActionCandidate.from_dict(
        json.loads(json.dumps(candidate.to_dict()))
    )

    assert decoded_state == state
    assert decoded_candidate == candidate


def test_missing_source_and_stale_source_are_unverified() -> None:
    policy, _source, model, state, candidate = _fixtures()
    missing = replace(candidate, evidence_refs=("telemetry:missing",))
    missing_result = _verify(
        state=state,
        candidate=missing,
        policy=policy,
        model=model,
    )
    stale_source = replace(
        state.observed_facts[0].source,
        freshness_deadline="2026-07-24T02:59:59+00:00",
    )
    stale_state = replace(
        state,
        observed_facts=(replace(state.observed_facts[0], source=stale_source),),
        derived_facts=(
            replace(state.derived_facts[0], source_refs=(stale_source.source_id,)),
        ),
    )
    stale_result = _verify(
        state=stale_state,
        policy=policy,
        model=model,
    )

    assert missing_result.status is FeasibilityStatus.UNVERIFIED
    assert "candidate_evidence_source_missing" in missing_result.unverified_reasons
    assert stale_result.status is FeasibilityStatus.UNVERIFIED
    assert "evidence_stale" in stale_result.unverified_reasons


def test_adapter_cursor_may_prove_freshness_without_wall_clock() -> None:
    policy, source, model, state, candidate = _fixtures()
    cursor_source = replace(
        source,
        observed_at=None,
        freshness_deadline=None,
        freshness_proof="adapter_cursor_verified",
    )
    state = replace(
        state,
        observed_facts=(
            replace(state.observed_facts[0], source=cursor_source),
        ),
        derived_facts=(
            replace(
                state.derived_facts[0],
                source_refs=(cursor_source.source_id,),
            ),
        ),
    )

    result = _verify(
        state=state,
        candidate=candidate,
        policy=policy,
        model=model,
    )

    assert result.status is FeasibilityStatus.VERIFIED_FEASIBLE


def test_policy_and_model_drift_are_blocked() -> None:
    policy, _source, model, state, candidate = _fixtures()
    changed_policy = replace(policy, policy_sha256="c" * 64)
    policy_result = _verify(
        state=state,
        candidate=candidate,
        policy=changed_policy,
        model=model,
    )
    model_result = verify_action_candidate(
        hazard_state=state,
        candidate=candidate,
        active_policy=policy,
        evaluated_at=EVALUATED_AT,
        extensions=(PassingExtension(),),
        active_model_digests={model.model_id: "d" * 64},
    )

    assert policy_result.status is FeasibilityStatus.BLOCKED
    assert "policy_binding_drift" in policy_result.blocked_reasons
    assert model_result.status is FeasibilityStatus.BLOCKED
    assert "model_binding_drift" in model_result.blocked_reasons


def test_cursor_order_requires_adapter_proof_and_rejects_regression() -> None:
    policy, _source, model, state, candidate = _fixtures()
    older = replace(state.cursor, value={"sequence": 10})
    newer_result = _verify(
        state=state,
        candidate=candidate,
        policy=policy,
        previous=older,
        model=model,
    )
    future = replace(state.cursor, value={"sequence": 12})
    regression_result = _verify(
        state=state,
        candidate=candidate,
        policy=policy,
        previous=future,
        model=model,
    )
    incomparable = replace(future, adapter_id="other")
    incomparable_result = _verify(
        state=state,
        candidate=candidate,
        policy=policy,
        previous=incomparable,
        model=model,
    )

    assert newer_result.status is FeasibilityStatus.VERIFIED_FEASIBLE
    assert regression_result.status is FeasibilityStatus.BLOCKED
    assert "cursor_regression" in regression_result.blocked_reasons
    assert incomparable_result.status is FeasibilityStatus.UNVERIFIED
    assert "cursor_incomparable" in incomparable_result.unverified_reasons


def test_unverified_fact_and_missing_extension_fail_closed() -> None:
    policy, _source, model, state, candidate = _fixtures()
    state = replace(
        state,
        derived_facts=(
            replace(state.derived_facts[0], status=FactStatus.UNVERIFIED),
        ),
    )
    result = verify_action_candidate(
        hazard_state=state,
        candidate=candidate,
        active_policy=policy,
        evaluated_at=EVALUATED_AT,
        extensions=(),
        active_model_digests={model.model_id: model.digest},
    )

    assert result.status is FeasibilityStatus.UNVERIFIED
    assert "derived_fact_unverified" in result.unverified_reasons
    assert "verifier_extension_missing" in result.unverified_reasons


def test_unknown_schema_versions_fail_unverified() -> None:
    policy, _source, model, state, candidate = _fixtures()
    result = _verify(
        state=replace(state, schema_version="missionos_core_hazard_state.v9"),
        candidate=replace(
            candidate,
            schema_version="missionos_core_action_candidate.v9",
        ),
        policy=policy,
        model=model,
    )

    assert result.status is FeasibilityStatus.UNVERIFIED
    assert "hazard_state_schema_not_supported" in result.unverified_reasons
    assert "action_candidate_schema_not_supported" in result.unverified_reasons


def test_core_source_has_no_backend_specific_names_or_imports() -> None:
    source_root = (
        Path(__file__).parents[2]
        / "packages"
        / "missionos-core"
        / "src"
        / "missionos_core"
    )
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(source_root.glob("*.py"))
    ).lower()

    forbidden = (
        "px4",
        "gazebo",
        "mavlink",
        "nav2",
        "ros2",
        "turtlebot",
    )
    assert all(name not in text for name in forbidden)

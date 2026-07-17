"""Contract tests for recovery-action promotion evidence (issue #31).

Evidence is a trailing streak of shadow_comparison agreements against the
deterministic floor — not a live envelope mutation, and not literal recorded
human approval (this codebase has no per-event human-approval log; the
deterministic floor is the closest available proxy for vetted policy).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.runtime.mission_autonomy_envelope import (
    build_mission_autonomy_envelope,
    classify_mission_autonomy_recovery_proposal,
)
from src.runtime.mission_autonomy_envelope import (
    build_mission_autonomy_recovery_proposal as build_recovery_proposal,
)
from src.runtime.recovery_action_promotion import (
    RECOVERY_ACTION_PROMOTION_APPLICATION_SCHEMA_VERSION,
    RECOVERY_ACTION_PROMOTION_PROPOSAL_SCHEMA_VERSION,
    RecoveryActionPromotionError,
    apply_recovery_action_promotion,
    evaluate_recovery_action_promotion_candidates,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _envelope():
    return build_mission_autonomy_envelope(
        mission_ref="mission_home_delivery",
        operator_approved=True,
        operator_approval_ref="approval_home_delivery",
        preapproved_recovery_actions=("return_home", "hold"),
        requires_human_approval_for=("avoid_obstacle", "reroute", "safe_stop"),
    )


def _agreement(action: str, *, agreement: bool | None = True) -> dict:
    return {
        "deterministic_action": action,
        "llm_action": action if agreement else "hold",
        "agreement": agreement,
        "llm_proposal_available": agreement is not None,
    }


def test_promotes_action_with_sufficient_trailing_agreement_streak() -> None:
    comparisons = [_agreement("avoid_obstacle") for _ in range(5)]
    proposals = evaluate_recovery_action_promotion_candidates(
        comparisons,
        envelope=_envelope(),
        min_consecutive_agreements=5,
        now=NOW,
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.schema_version == (
        RECOVERY_ACTION_PROMOTION_PROPOSAL_SCHEMA_VERSION
    )
    assert proposal.action == "avoid_obstacle"
    assert proposal.current_execution_class == "requires_human_approval"
    assert proposal.proposed_execution_class == "preapproved"
    assert proposal.evidence_basis == "deterministic_floor_agreement"
    assert proposal.consecutive_agreement_count == 5
    assert proposal.min_consecutive_agreements_required == 5
    assert proposal.envelope_mutation_applied is False
    assert proposal.dispatch_authority_created is False
    assert proposal.approval_created is False


def test_below_threshold_yields_no_proposal() -> None:
    comparisons = [_agreement("avoid_obstacle") for _ in range(4)]
    proposals = evaluate_recovery_action_promotion_candidates(
        comparisons,
        envelope=_envelope(),
        min_consecutive_agreements=5,
        now=NOW,
    )
    assert proposals == ()


def test_streak_is_trailing_not_cumulative() -> None:
    comparisons = (
        [_agreement("avoid_obstacle") for _ in range(10)]
        + [_agreement("avoid_obstacle", agreement=False)]
        + [_agreement("avoid_obstacle") for _ in range(2)]
    )
    proposals = evaluate_recovery_action_promotion_candidates(
        comparisons,
        envelope=_envelope(),
        min_consecutive_agreements=5,
        now=NOW,
    )
    assert proposals == ()


def test_fallback_entries_with_no_llm_proposal_break_the_streak() -> None:
    comparisons = [_agreement("avoid_obstacle") for _ in range(5)] + [
        _agreement("avoid_obstacle", agreement=None)
    ]
    proposals = evaluate_recovery_action_promotion_candidates(
        comparisons,
        envelope=_envelope(),
        min_consecutive_agreements=5,
        now=NOW,
    )
    assert proposals == ()


def test_only_actions_currently_requiring_approval_are_considered() -> None:
    comparisons = [_agreement("return_home") for _ in range(10)]
    proposals = evaluate_recovery_action_promotion_candidates(
        comparisons,
        envelope=_envelope(),
        min_consecutive_agreements=5,
        now=NOW,
    )
    assert proposals == ()


def test_multiple_actions_evaluated_independently() -> None:
    comparisons = [_agreement("avoid_obstacle") for _ in range(6)] + [
        _agreement("reroute") for _ in range(3)
    ]
    proposals = evaluate_recovery_action_promotion_candidates(
        comparisons,
        envelope=_envelope(),
        min_consecutive_agreements=5,
        now=NOW,
    )
    actions = {proposal.action for proposal in proposals}
    assert actions == {"avoid_obstacle"}


def test_rejects_nonpositive_threshold() -> None:
    with pytest.raises(ValueError):
        evaluate_recovery_action_promotion_candidates(
            [],
            envelope=_envelope(),
            min_consecutive_agreements=0,
            now=NOW,
        )


def test_proposal_ids_are_stable_for_identical_evidence() -> None:
    comparisons = [_agreement("avoid_obstacle") for _ in range(5)]
    first = evaluate_recovery_action_promotion_candidates(
        comparisons, envelope=_envelope(), min_consecutive_agreements=5, now=NOW
    )
    second = evaluate_recovery_action_promotion_candidates(
        comparisons, envelope=_envelope(), min_consecutive_agreements=5, now=NOW
    )
    assert first[0].proposal_id == second[0].proposal_id


def _proposal_for(action: str, envelope=None):
    comparisons = [_agreement(action) for _ in range(5)]
    proposals = evaluate_recovery_action_promotion_candidates(
        comparisons,
        envelope=envelope or _envelope(),
        min_consecutive_agreements=5,
        now=NOW,
    )
    return proposals[0]


def test_apply_promotes_action_and_records_application() -> None:
    envelope = _envelope()
    proposal = _proposal_for("avoid_obstacle", envelope)

    new_envelope, application = apply_recovery_action_promotion(
        envelope=envelope,
        proposal=proposal,
        operator_approval_ref="approval_promote_avoid_obstacle",
        approval_actor="operator_alice",
        now=NOW,
    )

    assert "avoid_obstacle" in new_envelope.preapproved_recovery_actions
    assert "avoid_obstacle" not in new_envelope.requires_human_approval_for
    # Everything else about the envelope is preserved.
    assert set(new_envelope.preapproved_recovery_actions) == {
        "return_home",
        "hold",
        "avoid_obstacle",
    }
    assert set(new_envelope.requires_human_approval_for) == {"reroute", "safe_stop"}
    assert new_envelope.blocked_actions == envelope.blocked_actions
    assert new_envelope.operator_approved is True
    assert new_envelope.operator_approval_ref == envelope.operator_approval_ref

    assert application.schema_version == (
        RECOVERY_ACTION_PROMOTION_APPLICATION_SCHEMA_VERSION
    )
    assert application.proposal_ref == proposal.proposal_id
    assert application.action == "avoid_obstacle"
    assert application.consecutive_agreement_count == 5
    assert application.operator_approval_ref == "approval_promote_avoid_obstacle"
    assert application.approval_actor == "operator_alice"
    assert application.previous_envelope_ref == envelope.envelope_id
    assert application.new_envelope_ref == new_envelope.envelope_id
    assert application.dispatch_authority_created is False
    assert application.physical_execution_invoked is False


def test_promoted_action_actually_classifies_as_auto_executable() -> None:
    """The applied envelope is not just relabeled — reclassification agrees."""

    envelope = _envelope()
    proposal = _proposal_for("avoid_obstacle", envelope)
    new_envelope, _application = apply_recovery_action_promotion(
        envelope=envelope,
        proposal=proposal,
        operator_approval_ref="approval_promote_avoid_obstacle",
        now=NOW,
    )

    llm_proposal = build_recovery_proposal(
        mission_ref=envelope.mission_ref,
        proposal_source="llm",
        selected_action="avoid_obstacle",
        reason="Corroborated obstacle; going around it.",
    )
    classification = classify_mission_autonomy_recovery_proposal(
        envelope=new_envelope,
        proposal=llm_proposal,
    )
    assert classification.execution_class == "auto_executable"
    assert classification.execution_permitted_by_envelope is True


def test_apply_rejects_unapproved_envelope() -> None:
    envelope = build_mission_autonomy_envelope(
        mission_ref="mission_home_delivery",
        operator_approved=False,
        preapproved_recovery_actions=("return_home", "hold"),
        requires_human_approval_for=("avoid_obstacle",),
    )
    proposal = _proposal_for("avoid_obstacle", envelope)

    with pytest.raises(RecoveryActionPromotionError, match="not itself"):
        apply_recovery_action_promotion(
            envelope=envelope,
            proposal=proposal,
            operator_approval_ref="approval_x",
            now=NOW,
        )


def test_apply_rejects_stale_proposal_action_not_in_envelope() -> None:
    envelope = _envelope()
    proposal = _proposal_for("avoid_obstacle", envelope)
    changed_envelope = build_mission_autonomy_envelope(
        mission_ref="mission_home_delivery",
        operator_approved=True,
        operator_approval_ref="approval_home_delivery",
        preapproved_recovery_actions=("return_home", "hold", "avoid_obstacle"),
        requires_human_approval_for=("reroute", "safe_stop"),
    )

    with pytest.raises(RecoveryActionPromotionError, match="not in this envelope"):
        apply_recovery_action_promotion(
            envelope=changed_envelope,
            proposal=proposal,
            operator_approval_ref="approval_x",
            now=NOW,
        )


def test_apply_rejects_empty_operator_approval_ref() -> None:
    envelope = _envelope()
    proposal = _proposal_for("avoid_obstacle", envelope)

    with pytest.raises(RecoveryActionPromotionError, match="non-empty"):
        apply_recovery_action_promotion(
            envelope=envelope,
            proposal=proposal,
            operator_approval_ref="   ",
            now=NOW,
        )


def test_application_ids_are_stable_for_identical_evidence() -> None:
    envelope = _envelope()
    proposal = _proposal_for("avoid_obstacle", envelope)

    _, first = apply_recovery_action_promotion(
        envelope=envelope,
        proposal=proposal,
        operator_approval_ref="approval_promote_avoid_obstacle",
        approval_actor="operator_alice",
        now=NOW,
    )
    _, second = apply_recovery_action_promotion(
        envelope=envelope,
        proposal=proposal,
        operator_approval_ref="approval_promote_avoid_obstacle",
        approval_actor="operator_alice",
        now=NOW,
    )
    assert first.application_id == second.application_id

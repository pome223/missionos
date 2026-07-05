from __future__ import annotations

import pytest

from src.runtime.mission_autonomy_envelope import (
    MissionAutonomyEnvelope,
    build_mission_autonomy_envelope,
    build_mission_autonomy_recovery_proposal,
    classify_mission_autonomy_recovery_proposal,
)


def test_llm_return_home_proposal_is_auto_executable_inside_approved_envelope() -> None:
    envelope = build_mission_autonomy_envelope(
        mission_ref="mission_home_delivery",
        operator_approved=True,
        operator_approval_ref="approval_home_delivery",
        preapproved_recovery_actions=("return_home", "hold"),
    )
    proposal = build_mission_autonomy_recovery_proposal(
        mission_ref="mission_home_delivery",
        proposal_source="llm",
        selected_action="return_home",
        reason="Battery margin is too small for continuing delivery.",
        input_observations={
            "battery_pct": 23.0,
            "distance_to_home_m": 1.8,
            "estimated_return_home_pct": 4.0,
        },
    )

    classification = classify_mission_autonomy_recovery_proposal(
        envelope=envelope,
        proposal=proposal,
    )

    assert proposal.llm_judgment_recorded is True
    assert proposal.proposal_allowed_to_be_recorded is True
    assert classification.proposal_recorded is True
    assert classification.proposal_allowed is True
    assert classification.execution_class == "auto_executable"
    assert classification.execution_permitted_by_envelope is True
    assert classification.requires_new_human_approval is False
    assert classification.dispatch_authority_created is False
    assert classification.physical_execution_invoked is False


def test_policy_does_not_block_llm_raw_velocity_proposal_but_blocks_execution() -> None:
    envelope = build_mission_autonomy_envelope(
        mission_ref="mission_home_delivery",
        operator_approved=True,
        operator_approval_ref="approval_home_delivery",
    )
    proposal = build_mission_autonomy_recovery_proposal(
        mission_ref="mission_home_delivery",
        proposal_source="llm",
        selected_action="raw_velocity",
        reason="Try to back away directly from the obstacle.",
        input_observations={"obstacle_distance_m": 0.4},
    )

    classification = classify_mission_autonomy_recovery_proposal(
        envelope=envelope,
        proposal=proposal,
    )

    assert proposal.proposal_allowed_to_be_recorded is True
    assert classification.proposal_allowed is True
    assert classification.execution_class == "blocked_but_reportable"
    assert classification.execution_permitted_by_envelope is False
    assert classification.requires_new_human_approval is False
    assert "action_blocked_by_autonomy_envelope" in classification.blocked_reasons


def test_unapproved_envelope_records_proposal_and_requires_human_approval() -> None:
    envelope = build_mission_autonomy_envelope(
        mission_ref="mission_home_delivery",
        operator_approved=False,
    )
    proposal = build_mission_autonomy_recovery_proposal(
        mission_ref="mission_home_delivery",
        proposal_source="llm",
        selected_action="return_home",
        reason="Battery is below reserve.",
        input_observations={"battery_pct": 18.0},
    )

    classification = classify_mission_autonomy_recovery_proposal(
        envelope=envelope,
        proposal=proposal,
    )

    assert classification.proposal_recorded is True
    assert classification.proposal_allowed is True
    assert classification.execution_class == "requires_human_approval"
    assert classification.execution_permitted_by_envelope is False
    assert classification.requires_new_human_approval is True
    assert "autonomy_envelope_not_operator_approved" in classification.blocked_reasons


def test_avoid_obstacle_defaults_to_fresh_human_approval() -> None:
    envelope = build_mission_autonomy_envelope(
        mission_ref="mission_home_delivery",
        operator_approved=True,
        operator_approval_ref="approval_home_delivery",
    )
    proposal = build_mission_autonomy_recovery_proposal(
        mission_ref="mission_home_delivery",
        proposal_source="llm",
        selected_action="avoid_obstacle",
        reason="Runtime obstacle is source-backed.",
        input_observations={"runtime_obstacle_observed": True},
    )

    classification = classify_mission_autonomy_recovery_proposal(
        envelope=envelope,
        proposal=proposal,
    )

    assert classification.execution_class == "requires_human_approval"
    assert classification.execution_permitted_by_envelope is False
    assert classification.requires_new_human_approval is True
    assert "action_requires_new_human_approval" in classification.blocked_reasons


def test_approved_envelope_requires_operator_approval_ref() -> None:
    with pytest.raises(ValueError, match="operator_approval_ref"):
        MissionAutonomyEnvelope(
            envelope_id="envelope_without_ref",
            mission_ref="mission_home_delivery",
            operator_approved=True,
        )

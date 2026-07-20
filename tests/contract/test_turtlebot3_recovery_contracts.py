from __future__ import annotations

from copy import deepcopy

import pytest

from src.runtime.turtlebot3_recovery_contracts import (
    build_turtlebot3_recovery_contract_bundle,
    recovery_checkpoint_hash,
    validate_turtlebot3_recovery_contract_bundle,
    verify_turtlebot3_recovery_outcome,
)


pytestmark = pytest.mark.contract


def _checkpoint(*, validated_candidate: bool = True) -> dict:
    binding = {
        "candidate_id": "obstacle_bypass_left",
        "candidate_ids": ["obstacle_retreat", "obstacle_bypass_left"],
        "path_sha256": "path-bypass",
        "path_sha256_sequence": ["path-retreat", "path-bypass"],
        "global_costmap_snapshot_hash": "global-costmap",
        "local_costmap_snapshot_hash": "local-costmap",
        "dual_costmap_validated": validated_candidate,
        "live_costmap_validated": validated_candidate,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }
    checkpoint = {
        "schema_version": "turtlebot3_recovery_checkpoint.v1",
        "checkpoint_status": "awaiting_operator_approval",
        "proposal_id": "mission-proposal",
        "recovery_proposal_id": "recovery-proposal",
        "recovery_classification_id": "recovery-classification",
        "selected_action": "avoid_obstacle",
        "approved_parameters": {
            "recovery_waypoints": [
                {"target_x_m": 0.2, "target_y_m": -0.8},
                {"target_x_m": 0.8, "target_y_m": -1.4},
            ],
            "obstacle_avoidance_required": True,
        },
        "recovery_candidate_binding": binding,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }
    checkpoint["recovery_contract_bundle"] = (
        build_turtlebot3_recovery_contract_bundle(checkpoint)
    )
    checkpoint_hash = recovery_checkpoint_hash(checkpoint)
    checkpoint["checkpoint_hash"] = checkpoint_hash
    checkpoint["checkpoint_id"] = (
        f"turtlebot3_recovery_checkpoint_{checkpoint_hash[:12]}"
    )
    return checkpoint


def _approval(checkpoint: dict) -> dict:
    return {
        "schema_version": "missionos_turtlebot3_recovery_operator_approval.v1",
        "operator_approved": True,
        "explicit_recovery_dispatch_approval": True,
        "operator_approval_ref": "operator:test",
        "checkpoint_id": checkpoint["checkpoint_id"],
        "checkpoint_hash": checkpoint["checkpoint_hash"],
        "approved_action": checkpoint["selected_action"],
        "approved_parameters": checkpoint["approved_parameters"],
    }


def test_contract_bundle_preserves_meaning_without_minting_authority() -> None:
    checkpoint = _checkpoint()
    bundle = checkpoint["recovery_contract_bundle"]

    assert validate_turtlebot3_recovery_contract_bundle(checkpoint) == []
    assert bundle["recovery_intent"]["strategy"] == "local_avoidance"
    assert bundle["recovery_intent"]["selected_action"] == "avoid_obstacle"
    assert bundle["intent_compilation"]["meaning_preserved"] is True
    assert bundle["intent_compilation"]["compiled_parameters"] == (
        checkpoint["approved_parameters"]
    )
    assert bundle["predispatch_verification"]["verification_status"] == (
        "verified"
    )
    assert bundle["approval_created"] is False
    assert bundle["dispatch_authority_created"] is False
    assert bundle["physical_execution_invoked"] is False


def test_contract_bundle_detects_checkpoint_meaning_tampering() -> None:
    checkpoint = _checkpoint()
    tampered = deepcopy(checkpoint)
    tampered["approved_parameters"]["recovery_waypoints"][1][
        "target_y_m"
    ] = 1.4

    reasons = validate_turtlebot3_recovery_contract_bundle(tampered)

    assert "turtlebot3_recovery_contract_checkpoint_meaning_mismatch" in reasons


def test_contract_bundle_records_unverified_candidate_without_authority() -> None:
    checkpoint = _checkpoint(validated_candidate=False)
    verification = checkpoint["recovery_contract_bundle"][
        "predispatch_verification"
    ]

    assert verification["verification_status"] == "unverified"
    assert verification["candidate_binding_verified"] is False
    assert verification["dispatch_authority_created"] is False


def test_legacy_checkpoint_without_bundle_remains_readable() -> None:
    checkpoint = _checkpoint()
    checkpoint.pop("recovery_contract_bundle")

    assert validate_turtlebot3_recovery_contract_bundle(checkpoint) == []


def test_outcome_verifier_does_not_treat_ack_as_effect_or_success() -> None:
    checkpoint = _checkpoint()
    verification = verify_turtlebot3_recovery_outcome(
        checkpoint=checkpoint,
        operator_approval=_approval(checkpoint),
        action_results=[
            {
                "dispatch_request_sent": True,
                "completion_claimed": False,
                "robot_motion_observed": False,
                "adapter_evidence": {"command_ack_observed": True},
            }
        ],
        goal_sequence_completed=True,
        requested_side_required=False,
        requested_side_observed=False,
        obstacle_clearance_required=False,
        obstacle_clearance_observed=False,
        route_resume_explicitly_approved=True,
    )

    assert verification["command_ack_observed"] is True
    assert verification["ack_is_executor_effect"] is False
    assert verification["executor_effect_observed"] is False
    assert verification["goal_sequence_completed"] is True
    assert verification["recovery_success_verified"] is False
    assert verification["route_resume_authorized"] is False
    assert verification["delivery_completion_claimed"] is False


def test_outcome_verifier_binds_fresh_approval_and_observation() -> None:
    checkpoint = _checkpoint()
    verification = verify_turtlebot3_recovery_outcome(
        checkpoint=checkpoint,
        operator_approval=_approval(checkpoint),
        action_results=[
            {
                "dispatch_request_sent": True,
                "completion_claimed": True,
                "robot_motion_observed": True,
                "adapter_evidence": {"command_ack_observed": True},
            }
        ],
        goal_sequence_completed=True,
        requested_side_required=True,
        requested_side_observed=True,
        obstacle_clearance_required=True,
        obstacle_clearance_observed=True,
        route_resume_explicitly_approved=True,
    )

    assert verification["verification_status"] == "verified"
    assert verification["authority_bound"] is True
    assert verification["executor_effect_observed"] is True
    assert verification["recovery_success_verified"] is True
    assert verification["route_resume_authorized"] is True
    assert verification["physical_execution_invoked"] is False

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.runtime.nav2_turtlebot3_mission_contract_runtime import (
    build_nav2_turtlebot3_runtime_contract,
    evaluate_nav2_turtlebot3_runtime_result,
)
from src.runtime.ros2_nav2_hardware_adapter import Nav2GoalPose


NOW = datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc)


def _goal() -> Nav2GoalPose:
    return Nav2GoalPose(
        frame_id="map",
        x_m=0.75,
        y_m=0.0,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="turtlebot3_short_nav2_goal",
    )


def _action_result() -> dict:
    return {
        "dispatch_started_at": (NOW - timedelta(seconds=2)).isoformat(),
        "result_observed_at": NOW.isoformat(),
        "bridge_responses": [
            {
                "action": "send_goal_pose",
                "ack_status": "accepted",
                "ack_source": "/navigate_to_pose",
                "goal_accepted": True,
                "nav2_status": "succeeded",
                "nav2_goal_succeeded": True,
                "runtime_progress_observed": True,
                "completion_observed": True,
                "completion_basis": "nav2_goal_succeeded",
                "blocking_reasons": [],
                "physical_execution_invoked": False,
                "raw_velocity_invoked": False,
                "raw_velocity_published": False,
                "raw_ros_topic_published": False,
                "cmd_vel_published_by_missionos": False,
                "state_result": {
                    "nav2_action_server_available": True,
                    "nav2_goal_succeeded": True,
                    "pose_observed": True,
                    "robot_motion_observed": True,
                    "odom_before_observed": True,
                    "odom_after_observed": True,
                    "odom_delta_m": 0.253,
                    "completion_basis": "nav2_goal_succeeded",
                },
                "progress_result": {
                    "runtime_progress_observed": True,
                    "completion_observed": True,
                    "nav2_goal_succeeded": True,
                    "nav2_status": "succeeded",
                    "robot_motion_observed": True,
                    "completion_basis": "nav2_goal_succeeded",
                    "feedback_count": 228,
                },
            }
        ],
        "adapter_evidence": {
            "adapter_id": "ros2_nav2_ground_robot_adapter.v1",
            "adapter_kind": "ros2_nav2",
            "vehicle_class": "ground_robot",
            "execution_mode": "sim",
            "missionos_action_ref": "proposal-1:segment_1",
            "adapter_action_kind": "nav2_goal_pose",
            "operator_approval_ref": "approval-1",
            "preflight_status": "passed",
            "dispatch_status": "sent",
            "dispatch_request_sent": True,
            "command_ack_observed": True,
            "ack_source": "/navigate_to_pose",
            "ack_status": "accepted",
            "runtime_state_observed": True,
            "runtime_progress_observed": True,
            "completion_claimed": True,
            "completion_scope": "sim_action",
            "physical_execution_invoked": False,
            "safe_stop_requested": False,
            "abort_requested": False,
            "telemetry_fresh": True,
            "blocking_reasons": [],
            "unproven_claims": [
                "simulator_evidence_not_physical",
                "physical_execution_not_invoked",
            ],
        },
    }


def _contract():
    return build_nav2_turtlebot3_runtime_contract(
        proposal_id="proposal-1",
        action_ref_suffix="segment_1",
        goal=_goal(),
    )


def test_runtime_result_claims_completion_only_through_frozen_predicate() -> None:
    contract = _contract()

    evaluation = evaluate_nav2_turtlebot3_runtime_result(
        contract=contract,
        goal=_goal(),
        action_result=_action_result(),
        evaluated_at=NOW + timedelta(seconds=1),
    )

    assert evaluation["status"] == "satisfied"
    assert evaluation["completion_claimed"] is True
    assert evaluation["satisfied_alternative"] == "succeeded_with_motion"
    assert evaluation["adapter_completion_claimed"] is True
    assert evaluation["contract_sha256"] == contract.contract_sha256
    assert evaluation["approval_created"] is False
    assert evaluation["dispatch_authority_created"] is False
    assert evaluation["runtime_effect_requested"] is False
    assert evaluation["operational_closure_created"] is False
    assert evaluation["physical_execution_invoked"] is False


def test_adapter_completion_does_not_override_predicate_failure() -> None:
    action_result = _action_result()
    action_result["bridge_responses"][0]["state_result"][
        "robot_motion_observed"
    ] = False

    evaluation = evaluate_nav2_turtlebot3_runtime_result(
        contract=_contract(),
        goal=_goal(),
        action_result=action_result,
        evaluated_at=NOW + timedelta(seconds=1),
    )

    assert evaluation["adapter_completion_claimed"] is True
    assert evaluation["status"] == "not_satisfied"
    assert evaluation["completion_claimed"] is False
    assert evaluation["completion_scope"] == "none"


def test_missing_or_multiple_bridge_responses_fail_closed() -> None:
    for bridge_responses in (
        [],
        [
            _action_result()["bridge_responses"][0],
            _action_result()["bridge_responses"][0],
        ],
    ):
        action_result = _action_result()
        action_result["bridge_responses"] = bridge_responses

        evaluation = evaluate_nav2_turtlebot3_runtime_result(
            contract=_contract(),
            goal=_goal(),
            action_result=action_result,
            evaluated_at=NOW,
        )

        assert evaluation["status"] == "unverified"
        assert evaluation["completion_claimed"] is False
        assert evaluation["predicate_package_evaluated"] is False
        assert evaluation["reasons"] == [
            "nav2_runtime_bridge_response_count_invalid"
        ]


def test_malformed_response_is_structured_unverified() -> None:
    action_result = _action_result()
    action_result["bridge_responses"][0]["goal_accepted"] = "true"

    evaluation = evaluate_nav2_turtlebot3_runtime_result(
        contract=_contract(),
        goal=_goal(),
        action_result=action_result,
        evaluated_at=NOW,
    )

    assert evaluation["status"] == "unverified"
    assert evaluation["completion_claimed"] is False
    assert evaluation["predicate_package_evaluated"] is False
    assert evaluation["reasons"] == [
        "nav2_runtime_result_schema_invalid",
        "nav2_runtime_result_schema_error:ValidationError",
    ]


def test_runtime_result_freshness_uses_recorded_observation_time() -> None:
    action_result = _action_result()
    action_result["result_observed_at"] = (
        NOW - timedelta(seconds=31)
    ).isoformat()

    evaluation = evaluate_nav2_turtlebot3_runtime_result(
        contract=_contract(),
        goal=_goal(),
        action_result=action_result,
        evaluated_at=NOW,
    )

    assert evaluation["status"] == "unverified"
    assert evaluation["completion_claimed"] is False
    assert evaluation["predicate_package_evaluated"] is False
    assert "observation_stale:nav2_bounded_dispatch_result" in evaluation[
        "reasons"
    ]


def test_missing_or_invalid_result_observation_time_is_unverified() -> None:
    for observed_at in (None, "", "not-a-timestamp", "2026-07-29T03:00:00"):
        action_result = _action_result()
        action_result["result_observed_at"] = observed_at

        evaluation = evaluate_nav2_turtlebot3_runtime_result(
            contract=_contract(),
            goal=_goal(),
            action_result=action_result,
            evaluated_at=NOW,
        )

        assert evaluation["status"] == "unverified"
        assert evaluation["completion_claimed"] is False
        assert evaluation["predicate_package_evaluated"] is False
        assert evaluation["reasons"] == [
            "nav2_runtime_result_observed_at_invalid"
        ]

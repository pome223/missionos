from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.runtime import turtlebot3_nav2_execution as execution
from src.runtime.ros2_nav2_hardware_adapter import Nav2GoalPose


def _goal() -> Nav2GoalPose:
    return Nav2GoalPose(
        frame_id="map",
        x_m=0.5,
        y_m=-0.25,
        yaw_rad=0.0,
        tolerance_m=0.25,
        max_speed_mps=0.25,
        max_distance_m=3.0,
        label="bounded_test_goal",
    )


def test_ack_without_state_is_not_projected_as_motion() -> None:
    motion = execution.robot_motion_from_responses(
        ({"ack_status": "accepted", "nav2_status": "executing"},)
    )

    assert motion == {
        "robot_motion_observed": False,
        "odom_delta_m": None,
        "odom_topic": None,
        "robot_motion_observation_source": "not_available",
    }


def test_obstacle_observation_projects_bridge_facts_without_authority() -> None:
    observation = execution.obstacle_observation_from_responses(
        (
            {
                "ack_status": "accepted",
                "ack_source": "fixture_bridge",
                "progress_result": {
                    "costmap_obstacle_observed": True,
                    "obstacle_avoidance_observed": True,
                    "trajectory_result": {
                        "trajectory_lateral_deviation_observed": True,
                        "max_lateral_deviation_m": 0.64,
                    },
                },
            },
        )
    )

    assert observation["obstacle_detected"] is True
    assert observation["obstacle_avoidance_observed"] is True
    assert observation["max_lateral_deviation_m"] == 0.64
    assert "operator_approved" not in observation
    assert "dispatch_authority_created" not in observation


def test_harness_cancel_ack_does_not_confirm_stop(monkeypatch) -> None:
    class _AckOnlyClient:
        def cancel_goal(self) -> dict[str, Any]:
            return {
                "ack_status": "accepted",
                "ack_source": "fixture_bridge",
                "nav2_status": "canceled",
                "blocking_reasons": [],
                "stop_observed": False,
                "post_cancel_odom_delta_m": 0.3,
            }

    monkeypatch.setattr(execution, "Ros2Nav2BridgeCommandClient", _AckOnlyClient)

    record = execution.dispatch_harness_stop(
        reflex={"trigger": "runtime_obstacle_observed"},
        mission_ref="proposal-1",
    )

    assert record["cancel_accepted"] is True
    assert record["stop_observed"] is False
    assert record["stop_confirmed"] is False
    assert record["physical_execution_invoked"] is False


def test_dispatch_binds_existing_approval_and_keeps_ack_separate_from_completion(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FixtureClient:
        def __init__(self, *, env_overrides: dict[str, str]):
            captured["env_overrides"] = dict(env_overrides)

        def collect_responses(self) -> tuple[dict[str, Any], ...]:
            return (
                {
                    "ack_status": "accepted",
                    "state_result": {
                        "robot_motion_observed": True,
                        "odom_delta_m": 0.42,
                        "odom_topic": "/odom",
                    },
                },
            )

    class _FixtureEvidence:
        dispatch_request_sent = True
        completion_claimed = False
        completion_scope = "none"
        blocking_reasons = ("nav2_goal_result_not_succeeded",)
        unproven_claims = ("sim_action_completion",)

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            assert mode == "json"
            return {
                "dispatch_request_sent": self.dispatch_request_sent,
                "completion_claimed": self.completion_claimed,
            }

    class _FixtureAdapter:
        def __init__(self, *, config, client):
            captured["config"] = config
            captured["client"] = client

        def dispatch_approved_action(self) -> _FixtureEvidence:
            return _FixtureEvidence()

    monkeypatch.setattr(execution, "Ros2Nav2BridgeCommandClient", _FixtureClient)
    monkeypatch.setattr(execution, "Ros2Nav2HardwareAdapter", _FixtureAdapter)

    result = execution.dispatch_nav2_goal(
        proposal_id="proposal-1",
        approval_actor="operator-1",
        goal=_goal(),
        approval_ref="approval-1",
        dispatched_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        action_ref_suffix="segment-1",
        raw_logs_ref="fixture:logs",
        publish_initialpose=False,
    )

    config = captured["config"]
    assert config.missionos_action_ref == "proposal-1:segment-1"
    assert config.operator_approval_ref == "approval-1"
    assert config.approval_actor == "operator-1"
    assert captured["env_overrides"] == {"ROS2_NAV2_INITIALPOSE_ENABLE": "0"}
    assert result["dispatch_request_sent"] is True
    assert result["robot_motion_observed"] is True
    assert result["completion_claimed"] is False
    assert result["completion_scope"] == "none"
    assert "nav2_goal_result_not_succeeded" in result["blocking_reasons"]
    dispatch_started_at = datetime.fromisoformat(result["dispatch_started_at"])
    result_observed_at = datetime.fromisoformat(result["result_observed_at"])
    assert dispatch_started_at.tzinfo is not None
    assert result_observed_at.tzinfo is not None
    assert dispatch_started_at <= result_observed_at

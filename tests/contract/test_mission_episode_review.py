from __future__ import annotations

import pytest

from src.runtime.mission_episode_review import (
    MissionEpisodeReviewError,
    build_mission_episode_review,
    mission_episode_review_ref,
)


def test_mission_episode_review_passes_sim_action_with_motion() -> None:
    review = build_mission_episode_review(
        source_ref="turtlebot3_home_mission_execution:demo",
        vehicle_kind="turtlebot3",
        source_summary={
            "status": "completed",
            "execution_target": "ros2_nav2_turtlebot3_sim",
            "execution_mode": "sim",
            "completion_claimed": True,
            "completion_scope": "sim_action",
            "robot_motion_observed": True,
            "mission_delivery_completion_claimed": False,
            "physical_execution_invoked": False,
            "nav2_log_diagnostics_status": "ready",
            "nav2_log_observed_patterns": ["nav2_goal_received"],
            "nav2_log_failure_hypotheses": [],
        },
    )

    assert review.schema_version == "missionos_mission_episode_review.v1"
    assert review.status == "passed"
    assert review.passed is True
    assert review.source_completion_scope == "sim_action"
    assert review.source_physical_execution_invoked is False
    assert review.physical_execution_invoked is False
    assert review.command_payload_allowed is False
    assert review.dispatch_authority_created is False
    assert not review.blocked_buckets
    assert "nav2_diagnostics_available" in review.buckets
    assert mission_episode_review_ref(review).startswith("mission_episode_review:")


def test_mission_episode_review_blocks_source_blocked_episode() -> None:
    review = build_mission_episode_review(
        source_ref="turtlebot3_home_mission_execution:blocked",
        vehicle_kind="turtlebot3",
        source_summary={
            "status": "blocked",
            "execution_target": "ros2_nav2_turtlebot3_sim",
            "execution_mode": "sim",
            "completion_claimed": False,
            "completion_scope": "none",
            "robot_motion_observed": False,
            "mission_delivery_completion_claimed": False,
            "physical_execution_invoked": False,
            "blocking_reasons": ["nav2_goal_result_not_succeeded"],
        },
    )

    assert review.status == "blocked"
    assert review.passed is False
    assert "episode_blocked" in review.blocked_buckets
    assert review.physical_execution_invoked is False


def test_mission_episode_review_blocks_sim_scope_mismatch() -> None:
    review = build_mission_episode_review(
        source_ref="turtlebot3_home_mission_execution:bad-scope",
        vehicle_kind="turtlebot3",
        source_summary={
            "status": "completed",
            "execution_target": "ros2_nav2_turtlebot3_sim",
            "execution_mode": "sim",
            "completion_claimed": True,
            "completion_scope": "adapter_action",
            "robot_motion_observed": True,
            "mission_delivery_completion_claimed": False,
            "physical_execution_invoked": False,
        },
    )

    assert review.status == "blocked"
    assert "sim_completion_scope_mismatch" in review.blocked_buckets


def test_mission_episode_review_rejects_command_like_finding_detail() -> None:
    with pytest.raises(MissionEpisodeReviewError, match="command-like"):
        build_mission_episode_review(
            source_ref="turtlebot3_home_mission_execution:bad-detail",
            vehicle_kind="turtlebot3",
            source_summary={
                "status": "blocked",
                "execution_target": "ros2_nav2_turtlebot3_sim",
                "execution_mode": "sim",
                "completion_claimed": False,
                "completion_scope": "none",
                "robot_motion_observed": False,
                "mission_delivery_completion_claimed": False,
                "physical_execution_invoked": False,
                "blocking_reasons": [{"command": "move"}],
            },
        )

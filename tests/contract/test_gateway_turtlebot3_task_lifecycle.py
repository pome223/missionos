from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from src.gateway.turtlebot3_task_lifecycle import (
    create_running_turtlebot3_home_mission_task,
    create_turtlebot3_home_mission_task,
    missionos_attach_turtlebot3_recovery_decision_summary,
)
from src.runtime.task_store import TaskStore
from src.runtime.turtlebot3_telemetry_sidecar import (
    TURTLEBOT3_LIVE_TASK_ID_PATH_ENV,
)


def _proposal() -> dict[str, object]:
    return {
        "schema_version": "missionos_turtlebot3_home_mission_plan.v1",
        "robot_profile": "turtlebot3",
        "robot_label": "TurtleBot3",
        "execution_target": "ros2_nav2_turtlebot3_sim",
        "runtime_substrate": "ros2_nav2_gazebo",
        "runtime_profile": "house",
    }


def test_running_task_projection_binds_only_display_telemetry(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    task_id_path = tmp_path / "live-task-id.txt"
    monkeypatch.setenv(TURTLEBOT3_LIVE_TASK_ID_PATH_ENV, str(task_id_path))

    task = create_running_turtlebot3_home_mission_task(
        task_store=task_store,
        session_id="session-task-projection",
        proposal=_proposal(),
        approval={"approval_status": "approved"},
    )

    assert task["status"] == "running"
    assert task["metadata"]["physical_execution_invoked"] is False
    assert task["metadata"]["mission_delivery_completion_claimed"] is False
    assert task_id_path.read_text(encoding="utf-8").strip() == task["task_id"]


def test_pending_checkpoint_projection_does_not_mint_approval_or_dispatch(
    tmp_path: Path,
) -> None:
    task_store = TaskStore(str(tmp_path / "tasks.db"))
    checkpoint = {
        "schema_version": "turtlebot3_recovery_checkpoint.v1",
        "checkpoint_id": "turtlebot3_recovery_checkpoint_contract",
        "checkpoint_status": "awaiting_operator_approval",
        "robot_profile": "turtlebot3",
        "execution_target": "ros2_nav2_turtlebot3_sim",
    }
    task = create_turtlebot3_home_mission_task(
        task_store=task_store,
        execution_result={
            "turtlebot3_home_mission_plan": _proposal(),
            "turtlebot3_recovery_checkpoint": checkpoint,
            "summary": {
                "status": "blocked",
                "robot_profile": "turtlebot3",
                "robot_label": "TurtleBot3",
                "execution_target": "ros2_nav2_turtlebot3_sim",
                "completion_claimed": False,
                "completion_scope": "none",
                "physical_execution_invoked": False,
                "mission_delivery_completion_claimed": False,
            },
        },
        session_id="session-pending-checkpoint",
    )

    artifacts = task["artifacts"]
    assert task["status"] == "pending"
    assert task["metadata"]["turtlebot3_recovery_lifecycle"] == ("awaiting_operator_approval")
    assert artifacts["turtlebot3_recovery_checkpoints"][checkpoint["checkpoint_id"]] == checkpoint
    assert "turtlebot3_recovery_operator_approval" not in artifacts
    assert "turtlebot3_recovery_bounded_action" not in artifacts


def test_recovery_decision_summary_is_read_only_and_non_authoritative() -> None:
    result = missionos_attach_turtlebot3_recovery_decision_summary(
        {
            "summary": {
                "runtime_recovery_triggered": True,
                "recovery_action_suggested": "avoid_obstacle",
                "recovery_dispatch_request_sent": False,
                "fresh_recovery_operator_approval_count": 0,
                "completion_claimed": False,
                "mission_delivery_completion_claimed": False,
                "physical_execution_invoked": False,
                "recovery_proposals": [
                    {
                        "proposal_source": "llm",
                        "approval_created": False,
                        "input_observations": {
                            "runtime_obstacle_observed": True,
                        },
                    }
                ],
            }
        }
    )

    decision = result["turtlebot3_recovery_decision_summary"]
    assert decision["read_only"] is True
    assert decision["decision_summary_creates_dispatch_authority"] is False
    assert decision["dispatch_authority_created"] is False
    assert decision["operator_approval_created_for_recovery"] is False
    assert decision["recovery_dispatch_request_sent"] is False
    assert decision["physical_execution_invoked"] is False

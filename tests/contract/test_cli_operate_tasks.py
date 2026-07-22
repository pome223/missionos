from typing import Any

import click
import pytest

from missionos_cli.operate_tasks import (
    _latest_running_sitl_task_id,
    _resolve_live_task_id,
    _resolve_operator_recovery_task_id,
)


class _Client:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def get(self, route: str) -> dict[str, Any]:
        self.requests.append(route)
        response = self.responses.get(route)
        if response is None:
            raise click.ClickException(f"missing fixture route: {route}")
        return response


def _active_runner_task(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "running",
        "artifacts": {
            "missionos_auto_mission_gui_dispatch_running_receipt": {
                "operator_recovery_request_container_path": "/tmp/recovery.json"
            }
        },
    }


def test_live_task_resolution_prefers_active_runner_over_stored_state() -> None:
    client = _Client(
        {
            "/tasks?page=1&page_size=20": {
                "items": [
                    {
                        "task_id": "task_plain_sitl",
                        "kind": "px4_gazebo_mission_designer_sitl_execution_request",
                        "status": "running",
                    },
                    _active_runner_task("task_active_runner"),
                ]
            }
        }
    )

    assert (
        _resolve_live_task_id(
            client,
            explicit_task_id="",
            stored_task_id="task_stale_local_state",
        )
        == "task_active_runner"
    )


def test_synthetic_running_smoke_is_not_selected_as_live_flight() -> None:
    client = _Client(
        {
            "/tasks?page=1&page_size=20": {
                "items": [
                    {
                        "task_id": "task_synthetic_smoke",
                        "kind": "mission_designer_sitl_execution",
                        "status": "running",
                    }
                ]
            }
        }
    )

    assert _latest_running_sitl_task_id(client) is None
    with pytest.raises(click.ClickException, match="no running SITL task found"):
        _resolve_live_task_id(client, explicit_task_id="", stored_task_id="")


def test_pending_turtlebot_checkpoint_can_be_recovered_from_stored_task() -> None:
    client = _Client(
        {
            "/tasks/task_pending_tb3": {
                "task": {
                    "task_id": "task_pending_tb3",
                    "kind": "turtlebot3_home_mission_execution",
                    "status": "pending",
                    "artifacts": {
                        "turtlebot3_recovery_checkpoint": {
                            "checkpoint_status": "awaiting_operator_approval"
                        }
                    },
                }
            }
        }
    )

    assert (
        _resolve_operator_recovery_task_id(
            client,
            explicit_task_id="",
            stored_task_id="task_pending_tb3",
        )
        == "task_pending_tb3"
    )


def test_explicit_operator_task_id_wins_without_discovery() -> None:
    client = _Client({})

    assert (
        _resolve_operator_recovery_task_id(
            client,
            explicit_task_id="task_operator_selected",
            stored_task_id="task_stale",
        )
        == "task_operator_selected"
    )
    assert client.requests == []

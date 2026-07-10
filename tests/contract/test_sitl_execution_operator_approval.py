from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.runtime.px4_gazebo_mission_designer_sitl_runner import (
    MISSIONOS_SITL_EXECUTION_OPERATOR_APPROVAL_SCHEMA_VERSION,
    PX4GazeboMissionDesignerSITLRunnerError,
    _validated_execution_operator_approval,
)


def _task_with_execution_approval(
    *,
    now: datetime,
    approval_status: str = "issued_unconsumed",
    consumed_in_runtime: bool = False,
) -> dict:
    approval_id = "approval_test_1"
    return {
        "task_id": "task_test_1",
        "artifacts": {
            "missionos_sitl_execution_operator_approvals": {
                approval_id: {
                    "schema_version": (
                        MISSIONOS_SITL_EXECUTION_OPERATOR_APPROVAL_SCHEMA_VERSION
                    ),
                    "approval_id": approval_id,
                    "task_id": "task_test_1",
                    "execution_request_ref": (
                        "px4_gazebo_mission_designer_sitl_execution_request:request_test_1"
                    ),
                    "scenario_approval_ref": (
                        "px4_gazebo_mission_scenario_approval:scenario_approval_test_1"
                    ),
                    "approval_actor": "authenticated_operator",
                    "approved_at": (now - timedelta(seconds=1)).isoformat(),
                    "operator_approved": True,
                    "approval_status": approval_status,
                    "consumed_in_runtime": consumed_in_runtime,
                }
            }
        },
    }


def test_execution_approval_is_bound_to_requested_artifact_id() -> None:
    now = datetime.now(timezone.utc)
    task = _task_with_execution_approval(now=now)

    approval = _validated_execution_operator_approval(
        task,
        execution_operator_approval_id="approval_test_1",
        execution_request=SimpleNamespace(execution_request_id="request_test_1"),
        scenario_approval=SimpleNamespace(approval_id="scenario_approval_test_1"),
        now=now,
    )

    assert approval["approval_id"] == "approval_test_1"
    assert approval["approval_actor"] == "authenticated_operator"


def test_execution_approval_rejects_unknown_id_and_replay() -> None:
    now = datetime.now(timezone.utc)
    task = _task_with_execution_approval(now=now)

    with pytest.raises(
        PX4GazeboMissionDesignerSITLRunnerError,
        match="requested stored SITL execution approval was not found",
    ):
        _validated_execution_operator_approval(
            task,
            execution_operator_approval_id="approval_not_stored",
            execution_request=SimpleNamespace(execution_request_id="request_test_1"),
            scenario_approval=SimpleNamespace(
                approval_id="scenario_approval_test_1"
            ),
            now=now,
        )

    consumed_task = _task_with_execution_approval(
        now=now,
        approval_status="consumed_in_runtime",
        consumed_in_runtime=True,
    )
    with pytest.raises(
        PX4GazeboMissionDesignerSITLRunnerError,
        match="not issued and unconsumed",
    ):
        _validated_execution_operator_approval(
            consumed_task,
            execution_operator_approval_id="approval_test_1",
            execution_request=SimpleNamespace(execution_request_id="request_test_1"),
            scenario_approval=SimpleNamespace(
                approval_id="scenario_approval_test_1"
            ),
            now=now,
        )

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from src.runtime.px4_gazebo_coupled_delivery import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_TAKEOFF,
    PX4GazeboCoupledCommandFailureReason,
    build_px4_gazebo_coupled_command_allowlist,
    build_px4_gazebo_coupled_command_approval,
    build_px4_gazebo_coupled_command_diagnostics,
    run_px4_gazebo_coupled_command_diagnostics_task,
)
from src.runtime.task_store import TaskStore
from tests.fixtures.delivery_artifact_chain import NOW


@dataclass(frozen=True)
class CommandFailureCase:
    reason: PX4GazeboCoupledCommandFailureReason
    command_id: int
    command_name: str
    include_approval: bool = True
    include_allowlist: bool = True
    command_allowlisted: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


FAILURE_CASES = (
    CommandFailureCase(
        PX4GazeboCoupledCommandFailureReason.MAVLINK_TIMEOUT,
        MAV_CMD_NAV_TAKEOFF,
        "MAV_CMD_NAV_TAKEOFF",
        extra={
            "mavlink_command_sent_to_px4": True,
            "mavlink_response_received_from_px4": False,
            "blocked_reasons": ["px4_command_ack_missing"],
        },
    ),
    CommandFailureCase(
        PX4GazeboCoupledCommandFailureReason.COMMAND_REJECTED,
        MAV_CMD_NAV_LAND,
        "MAV_CMD_NAV_LAND",
        extra={
            "mavlink_command_sent_to_px4": True,
            "mavlink_response_received_from_px4": True,
            "px4_command_ack_result": "MAV_RESULT_DENIED",
            "blocked_reasons": ["px4_command_denied"],
        },
    ),
    CommandFailureCase(
        PX4GazeboCoupledCommandFailureReason.WRONG_TARGET,
        MAV_CMD_COMPONENT_ARM_DISARM,
        "MAV_CMD_COMPONENT_ARM_DISARM",
        extra={
            "target_system": 2,
            "target_component": 1,
            "blocked_reasons": ["target_system_mismatch"],
        },
    ),
    CommandFailureCase(
        PX4GazeboCoupledCommandFailureReason.NON_LOOPBACK_ENDPOINT,
        MAV_CMD_NAV_TAKEOFF,
        "MAV_CMD_NAV_TAKEOFF",
        extra={
            "endpoint_host": "192.0.2.10",
            "blocked_reasons": ["endpoint_not_loopback"],
        },
    ),
    CommandFailureCase(
        PX4GazeboCoupledCommandFailureReason.HARDWARE_TARGET_REQUESTED,
        MAV_CMD_NAV_TAKEOFF,
        "MAV_CMD_NAV_TAKEOFF",
        extra={"blocked_reasons": ["hardware_target_requested"]},
    ),
    CommandFailureCase(
        PX4GazeboCoupledCommandFailureReason.MISSING_APPROVAL,
        MAV_CMD_NAV_TAKEOFF,
        "MAV_CMD_NAV_TAKEOFF",
        include_approval=False,
        extra={"blocked_reasons": ["approval_artifact_missing"]},
    ),
    CommandFailureCase(
        PX4GazeboCoupledCommandFailureReason.MISSING_ALLOWLIST,
        MAV_CMD_NAV_TAKEOFF,
        "MAV_CMD_NAV_TAKEOFF",
        include_allowlist=False,
        command_allowlisted=False,
        extra={"blocked_reasons": ["allowlist_artifact_missing"]},
    ),
)


@pytest.mark.parametrize("case", FAILURE_CASES, ids=lambda case: case.reason.value)
def test_coupled_command_failure_terminally_blocks_without_retry(
    tmp_path: Path,
    case: CommandFailureCase,
) -> None:
    approval = build_px4_gazebo_coupled_command_approval(
        operator_approval_performed=True,
        now=NOW,
    )
    allowlist = build_px4_gazebo_coupled_command_allowlist(
        approval=approval,
        now=NOW,
    )
    observations = {"mavlink_command_sent_to_px4": False, **case.extra}
    diagnostics = build_px4_gazebo_coupled_command_diagnostics(
        failure_reason=case.reason,
        command_id=case.command_id,
        command_name=case.command_name,
        approval=approval if case.include_approval else None,
        allowlist=allowlist if case.include_allowlist else None,
        command_allowlisted=case.command_allowlisted,
        now=NOW,
        **observations,
    )
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="px4_gazebo_coupled_command_diagnostics",
        title=f"PX4/Gazebo command diagnostics {case.reason.value}",
        status="running",
        artifacts={"existing": {"kept": True}},
    )

    updated = run_px4_gazebo_coupled_command_diagnostics_task(
        task["task_id"],
        diagnostics=diagnostics,
        now=NOW,
        task_store_factory=lambda: store,
    )
    artifact = updated["artifacts"]["px4_gazebo_coupled_command_diagnostics"]

    assert updated["status"] == "blocked"
    assert updated["artifacts"]["existing"] == {"kept": True}
    assert artifact["failure_reason"] == case.reason.value
    assert artifact["retry_attempted"] is False
    assert artifact["stronger_execution_attempted"] is False
    assert artifact["hardware_target_allowed"] is False
    assert artifact["physical_execution_invoked"] is False
    if case.reason == PX4GazeboCoupledCommandFailureReason.MISSING_APPROVAL:
        assert artifact["operator_approval_performed"] is False
    if case.reason == PX4GazeboCoupledCommandFailureReason.MISSING_ALLOWLIST:
        assert artifact["bounded_allowlist_enforced"] is False

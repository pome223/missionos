#!/usr/bin/env python3
"""Runtime smoke for fail-closed PX4/Gazebo coupled command diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from tempfile import TemporaryDirectory

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

OPT_IN_ENV = "RUN_PX4_GAZEBO_COUPLED_COMMAND_FAILURES_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            "Set RUN_PX4_GAZEBO_COUPLED_COMMAND_FAILURES_SMOKE=1 to run the "
            "PX4/Gazebo coupled command failure diagnostics smoke."
        )


def main() -> int:
    _require_opt_in()
    approval = build_px4_gazebo_coupled_command_approval(
        operator_approval_performed=True,
        now=NOW,
    )
    allowlist = build_px4_gazebo_coupled_command_allowlist(
        approval=approval,
        now=NOW,
    )
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        observed: dict[str, dict] = {}
        cases = [
            build_px4_gazebo_coupled_command_diagnostics(
                failure_reason=PX4GazeboCoupledCommandFailureReason.MAVLINK_TIMEOUT,
                command_id=MAV_CMD_NAV_TAKEOFF,
                command_name="MAV_CMD_NAV_TAKEOFF",
                approval=approval,
                allowlist=allowlist,
                command_allowlisted=True,
                mavlink_command_sent_to_px4=True,
                mavlink_response_received_from_px4=False,
                blocked_reasons=["px4_command_ack_missing"],
                now=NOW,
            ),
            build_px4_gazebo_coupled_command_diagnostics(
                failure_reason=PX4GazeboCoupledCommandFailureReason.COMMAND_REJECTED,
                command_id=MAV_CMD_NAV_LAND,
                command_name="MAV_CMD_NAV_LAND",
                approval=approval,
                allowlist=allowlist,
                command_allowlisted=True,
                mavlink_command_sent_to_px4=True,
                mavlink_response_received_from_px4=True,
                px4_command_ack_result="MAV_RESULT_DENIED",
                blocked_reasons=["px4_command_denied"],
                now=NOW,
            ),
            build_px4_gazebo_coupled_command_diagnostics(
                failure_reason=PX4GazeboCoupledCommandFailureReason.WRONG_TARGET,
                command_id=MAV_CMD_COMPONENT_ARM_DISARM,
                command_name="MAV_CMD_COMPONENT_ARM_DISARM",
                target_system=2,
                target_component=1,
                approval=approval,
                allowlist=allowlist,
                command_allowlisted=True,
                mavlink_command_sent_to_px4=False,
                blocked_reasons=["target_system_mismatch"],
                now=NOW,
            ),
            build_px4_gazebo_coupled_command_diagnostics(
                failure_reason=PX4GazeboCoupledCommandFailureReason.NON_LOOPBACK_ENDPOINT,
                command_id=MAV_CMD_NAV_TAKEOFF,
                command_name="MAV_CMD_NAV_TAKEOFF",
                endpoint_host="192.0.2.10",
                approval=approval,
                allowlist=allowlist,
                command_allowlisted=True,
                mavlink_command_sent_to_px4=False,
                blocked_reasons=["endpoint_not_loopback"],
                now=NOW,
            ),
            build_px4_gazebo_coupled_command_diagnostics(
                failure_reason=PX4GazeboCoupledCommandFailureReason.HARDWARE_TARGET_REQUESTED,
                command_id=MAV_CMD_NAV_TAKEOFF,
                command_name="MAV_CMD_NAV_TAKEOFF",
                approval=approval,
                allowlist=allowlist,
                command_allowlisted=True,
                mavlink_command_sent_to_px4=False,
                blocked_reasons=["hardware_target_requested"],
                now=NOW,
            ),
            build_px4_gazebo_coupled_command_diagnostics(
                failure_reason=PX4GazeboCoupledCommandFailureReason.MISSING_APPROVAL,
                command_id=MAV_CMD_NAV_TAKEOFF,
                command_name="MAV_CMD_NAV_TAKEOFF",
                approval=None,
                allowlist=allowlist,
                command_allowlisted=True,
                mavlink_command_sent_to_px4=False,
                blocked_reasons=["approval_artifact_missing"],
                now=NOW,
            ),
            build_px4_gazebo_coupled_command_diagnostics(
                failure_reason=PX4GazeboCoupledCommandFailureReason.MISSING_ALLOWLIST,
                command_id=MAV_CMD_NAV_TAKEOFF,
                command_name="MAV_CMD_NAV_TAKEOFF",
                approval=approval,
                allowlist=None,
                command_allowlisted=False,
                mavlink_command_sent_to_px4=False,
                blocked_reasons=["allowlist_artifact_missing"],
                now=NOW,
            ),
        ]
        for diagnostics in cases:
            task = store.create(
                kind="px4_gazebo_coupled_command_diagnostics",
                title=f"PX4/Gazebo command diagnostics {diagnostics.failure_reason.value}",
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
            assert updated["artifacts"]["existing"]["kept"] is True
            assert artifact["retry_attempted"] is False
            assert artifact["stronger_execution_attempted"] is False
            assert artifact["hardware_target_allowed"] is False
            assert artifact["physical_execution_invoked"] is False
            observed[diagnostics.failure_reason.value] = artifact

    summary = {
        "observed_failure_reasons": sorted(observed),
        "all_tasks_blocked": len(observed) == 7,
        "missing_approval_performed": observed["missing_approval"][
            "operator_approval_performed"
        ],
        "missing_allowlist_enforced": observed["missing_allowlist"][
            "bounded_allowlist_enforced"
        ],
        "retry_attempted": any(item["retry_attempted"] for item in observed.values()),
        "stronger_execution_attempted": any(
            item["stronger_execution_attempted"] for item in observed.values()
        ),
        "hardware_target_allowed": any(
            item["hardware_target_allowed"] for item in observed.values()
        ),
        "physical_execution_invoked": any(
            item["physical_execution_invoked"] for item in observed.values()
        ),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    assert summary["observed_failure_reasons"] == [
        "command_rejected",
        "hardware_target_requested",
        "mavlink_timeout",
        "missing_allowlist",
        "missing_approval",
        "non_loopback_endpoint",
        "wrong_target",
    ]
    assert summary["all_tasks_blocked"] is True
    assert summary["missing_approval_performed"] is False
    assert summary["missing_allowlist_enforced"] is False
    assert summary["retry_attempted"] is False
    assert summary["stronger_execution_attempted"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

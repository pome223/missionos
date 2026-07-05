#!/usr/bin/env python3
"""Blocked-path smoke for the bounded ROS2/Nav2 hardware adapter contract.

This smoke deliberately does not import ROS2 and does not provide a Nav2 client.
It exercises the production adapter preflight and proves that a physical-mode
Nav2 dispatch blocks before any command can be sent.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os

from src.runtime.hardware_adapter_contract import HardwareExecutionMode
from src.runtime.ros2_nav2_hardware_adapter import (
    ROS2_NAV2_HARDWARE_ADAPTER_OPT_IN_ENV,
    Nav2GoalPose,
    Ros2Nav2HardwareAdapter,
    Ros2Nav2HardwareAdapterConfig,
)


def main() -> int:
    os.environ.pop(ROS2_NAV2_HARDWARE_ADAPTER_OPT_IN_ENV, None)
    adapter = Ros2Nav2HardwareAdapter(
        config=Ros2Nav2HardwareAdapterConfig(
            missionos_action_ref="missionos_plan_bounded_nav2_goal_pose",
            goal_pose=Nav2GoalPose(x_m=1.0, y_m=0.0),
            execution_mode=HardwareExecutionMode.BENCH,
            operator_approval_ref="smoke_operator_approval_nav2_001",
            approval_actor="smoke-operator",
            approval_timestamp=datetime.now(timezone.utc),
            opt_in=False,
            physical_estop_available=False,
        ),
        client=None,
    )
    preflight = adapter.preflight_check()
    evidence = adapter.dispatch_approved_action()
    summary = {
        "smoke": "ros2_nav2_hardware_adapter_contract",
        "ran": True,
        "ros2_imported": False,
        "nav2_client_present": False,
        "execution_mode": evidence.execution_mode.value,
        "preflight_status": preflight.preflight_status.value,
        "dispatch_status": evidence.dispatch_status.value,
        "dispatch_request_sent": evidence.dispatch_request_sent,
        "physical_execution_invoked": evidence.physical_execution_invoked,
        "completion_claimed": evidence.completion_claimed,
        "blocking_reasons": list(evidence.blocking_reasons),
        "unproven_claims": list(evidence.unproven_claims),
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    if evidence.dispatch_request_sent:
        raise SystemExit("Nav2 smoke unexpectedly sent a dispatch request")
    if evidence.physical_execution_invoked:
        raise SystemExit("Nav2 smoke unexpectedly claimed physical execution")
    if evidence.completion_claimed:
        raise SystemExit("Nav2 smoke unexpectedly claimed completion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Opt-in actual PX4/Gazebo smoke for emergency command dispatch."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import time
from typing import Any

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_smoke
from src.runtime.px4_gazebo_coupled_delivery import (
    build_px4_gazebo_coupled_command_allowlist,
    build_px4_gazebo_coupled_command_approval,
)
from src.runtime.px4_gazebo_emergency_dispatcher import (
    build_px4_gazebo_emergency_command_allowlist,
    build_px4_gazebo_emergency_command_approval,
    run_px4_gazebo_emergency_command_dispatch,
)
from src.runtime.px4_gazebo_route_recovery import PX4GazeboRouteRecoveryAction

OPT_IN_ENV = "RUN_PX4_GAZEBO_ACTUAL_EMERGENCY_DISPATCH_SMOKE"
CONTAINER_NAME = "boiled-claw-px4-gazebo-actual-emergency-smoke"
EMERGENCY_MAVLINK_LOCAL_PORT = 14651
EMERGENCY_MAVLINK_PX4_PORT = 14601
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

_COMMAND_LOG_MARKERS = {
    PX4GazeboRouteRecoveryAction.HOLD: (
        "loiter",
        "Loiter",
        "hold",
        "Hold",
    ),
    PX4GazeboRouteRecoveryAction.RETURN_TO_LAUNCH: (
        "RTL",
        "Return to launch",
        "return to launch",
    ),
    PX4GazeboRouteRecoveryAction.LAND: (
        "Landing",
        "Landing detected",
        "Disarmed by landing",
        "landed",
    ),
}


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the actual PX4/Gazebo emergency "
            "dispatch smoke."
        )


def _observed_markers(action: PX4GazeboRouteRecoveryAction, logs: str) -> list[str]:
    return [marker for marker in _COMMAND_LOG_MARKERS[action] if marker in logs]


def _start_container() -> None:
    route_smoke.CONTAINER_NAME = CONTAINER_NAME
    route_smoke._run(["docker", "rm", "-f", CONTAINER_NAME], check=False)
    route_smoke._run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "--add-host",
            "host.docker.internal:host-gateway",
            "-p",
            f"{EMERGENCY_MAVLINK_PX4_PORT}:{EMERGENCY_MAVLINK_PX4_PORT}/udp",
            "-e",
            "PX4_SIM_MODEL=gz_x500",
            "-e",
            "PX4_GZ_WORLD=default",
            "-e",
            "HEADLESS=1",
            "-e",
            "PX4_GZ_NO_FOLLOW=1",
            route_smoke.PX4_GAZEBO_IMAGE,
            "-d",
        ],
        timeout=240,
    )
    route_smoke._wait_for_startup()
    route_smoke._start_route_ack_mavlink_instance()
    _start_host_targeted_emergency_mavlink_instance()


def _start_host_targeted_emergency_mavlink_instance() -> None:
    route_smoke._run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-lc",
            (
                "HOST_IP=$(getent ahostsv4 host.docker.internal | "
                "awk '{print $1; exit}'); "
                'test -n "$HOST_IP"; '
                f"/opt/px4-gazebo/bin/px4-mavlink start "
                f"-u {EMERGENCY_MAVLINK_PX4_PORT} -r 400000 "
                f'-t "$HOST_IP" -o {EMERGENCY_MAVLINK_LOCAL_PORT} '
                "-m onboard"
            ),
        ],
        timeout=20,
    )
    time.sleep(1)


def _stop_container() -> None:
    route_smoke._run(["docker", "rm", "-f", CONTAINER_NAME], check=False)


def _dispatch_emergency(
    *,
    action: PX4GazeboRouteRecoveryAction,
    approval: Any,
    allowlist: Any,
) -> dict[str, Any]:
    result = run_px4_gazebo_emergency_command_dispatch(
        recovery_action=action,
        approval=approval,
        allowlist=allowlist,
        endpoint_port=EMERGENCY_MAVLINK_PX4_PORT,
        local_bind_port=EMERGENCY_MAVLINK_LOCAL_PORT,
        live_mavlink_opt_in=True,
        ack_timeout_seconds=5.0,
        now=NOW,
    )
    time.sleep(2)
    logs = route_smoke._logs("500")
    return {
        "action": action.value,
        "schema_version": result.schema_version,
        "dispatch_status": result.dispatch_status,
        "command_id": result.command_id,
        "command_name": result.command_name,
        "frame_sent": result.frame_sent,
        "recovery_command_sent": result.recovery_command_sent,
        "command_ack_observed": result.command_ack_observed,
        "command_ack_result_code": result.command_ack_result_code,
        "command_ack_result_name": result.command_ack_result_name,
        "blocked_reasons": list(result.blocked_reasons),
        "commander_log_markers": _observed_markers(action, logs),
        "hardware_target_allowed": result.hardware_target_allowed,
        "physical_execution_invoked": result.physical_execution_invoked,
        "approval_free_recovery_dispatch_allowed": (
            result.approval_free_recovery_dispatch_allowed
        ),
    }


def main() -> int:
    _require_opt_in()
    _start_container()
    try:
        route_smoke._wait_for_px4_home()
        coupled_approval = build_px4_gazebo_coupled_command_approval(
            operator_approval_performed=True,
            now=NOW,
        )
        coupled_allowlist = build_px4_gazebo_coupled_command_allowlist(
            approval=coupled_approval,
            now=NOW,
        )
        _airborne_pose, climb_samples = route_smoke._send_until_z(
            ["arm", "takeoff"],
            lambda z, _samples: z >= 1.0,
            approval=coupled_approval,
            coupled_allowlist=coupled_allowlist,
            timeout=75.0,
        )

        emergency_approval = build_px4_gazebo_emergency_command_approval(
            operator_approval_performed=True,
            now=NOW,
        )
        emergency_allowlist = build_px4_gazebo_emergency_command_allowlist(
            approval=emergency_approval,
            now=NOW,
        )
        dispatch_results = [
            _dispatch_emergency(
                action=PX4GazeboRouteRecoveryAction.HOLD,
                approval=emergency_approval,
                allowlist=emergency_allowlist,
            ),
            _dispatch_emergency(
                action=PX4GazeboRouteRecoveryAction.RETURN_TO_LAUNCH,
                approval=emergency_approval,
                allowlist=emergency_allowlist,
            ),
            _dispatch_emergency(
                action=PX4GazeboRouteRecoveryAction.LAND,
                approval=emergency_approval,
                allowlist=emergency_allowlist,
            ),
        ]
        completed_pose, landing_samples = route_smoke._wait_for_z(
            lambda z, _samples: z <= 0.15,
            timeout=80.0,
        )
        summary = {
            "actual_px4_gazebo_emergency_smoke_observed": True,
            "dispatch_results": dispatch_results,
            "dispatch_statuses": [item["dispatch_status"] for item in dispatch_results],
            "command_ids": [item["command_id"] for item in dispatch_results],
            "command_ack_observed": [
                item["command_ack_observed"] for item in dispatch_results
            ],
            "command_ack_result_names": [
                item["command_ack_result_name"] for item in dispatch_results
            ],
            "commander_log_markers": {
                item["action"]: item["commander_log_markers"]
                for item in dispatch_results
            },
            "completed_pose_z_m": completed_pose["z"],
            "climb_sample_count": len(climb_samples),
            "landing_sample_count": len(landing_samples),
            "hardware_target_allowed": [
                item["hardware_target_allowed"] for item in dispatch_results
            ],
            "physical_execution_invoked": [
                item["physical_execution_invoked"] for item in dispatch_results
            ],
            "approval_free_recovery_dispatch_allowed": [
                item["approval_free_recovery_dispatch_allowed"]
                for item in dispatch_results
            ],
        }
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        assert summary["actual_px4_gazebo_emergency_smoke_observed"] is True
        assert summary["command_ack_observed"] == [True, True, True]
        assert summary["completed_pose_z_m"] <= 0.15
        assert all(value is False for value in summary["hardware_target_allowed"])
        assert all(value is False for value in summary["physical_execution_invoked"])
        assert all(
            value is False
            for value in summary["approval_free_recovery_dispatch_allowed"]
        )
        return 0
    finally:
        _stop_container()


if __name__ == "__main__":
    raise SystemExit(main())

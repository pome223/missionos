"""Low-level PX4/Gazebo route execution mechanics.

These functions execute only after a caller has validated approval and
allowlist authority. They receive the process runner and endpoint configuration
explicitly; they do not mint approval, choose a recovery action, or verify
mission completion.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Protocol


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]: ...


def send_embedded_helper(
    mode: str,
    *args: object,
    runner: CommandRunner,
    container_name: str,
    helper_source: str,
    timeout: int = 30,
) -> dict[str, Any]:
    result = runner(
        [
            "docker",
            "exec",
            "-i",
            container_name,
            "python3",
            "-",
            mode,
            *map(str, args),
        ],
        input_text=helper_source,
        timeout=timeout,
    )
    return json.loads(result.stdout.strip())


def _heartbeat_observation_failure(
    *,
    status: str,
    source: str,
    duration_seconds: float,
    gap_threshold_seconds: float,
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    return {
        "observer_status": status,
        "source": source,
        "duration_seconds": duration_seconds,
        "gap_threshold_seconds": gap_threshold_seconds,
        "packet_count": 0,
        "heartbeat_count": 0,
        "heartbeat_intervals_seconds": [],
        "max_heartbeat_interval_seconds": 0.0,
        "heartbeat_gap_count": 0,
        "heartbeat_gap_observed": False,
        "observer_sent_packets": False,
        "packet_drop_performed": False,
        "stdout_tail": result.stdout[-500:],
        "stderr_tail": result.stderr[-500:],
    }


def observe_mavlink_heartbeat_gap(
    *,
    runner: CommandRunner,
    container_name: str,
    helper_source: str,
    local_port: int,
    duration_seconds: float = 3.0,
    gap_threshold_seconds: float = 2.0,
) -> dict[str, Any]:
    result = runner(
        [
            "docker",
            "exec",
            "-i",
            container_name,
            "python3",
            "-",
            str(duration_seconds),
            str(gap_threshold_seconds),
        ],
        input_text=helper_source,
        check=False,
        timeout=int(duration_seconds) + 5,
    )
    source = f"udp://127.0.0.1:{local_port}"
    if result.returncode != 0:
        return _heartbeat_observation_failure(
            status="failed",
            source=source,
            duration_seconds=duration_seconds,
            gap_threshold_seconds=gap_threshold_seconds,
            result=result,
        )
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return _heartbeat_observation_failure(
            status="invalid_output",
            source=source,
            duration_seconds=duration_seconds,
            gap_threshold_seconds=gap_threshold_seconds,
            result=result,
        )
    payload["observer_sent_packets"] = False
    payload["packet_drop_performed"] = False
    return payload


def _link_loss_failure(
    *,
    status: str,
    source: str,
    duration_seconds: float,
    gap_threshold_seconds: float,
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    return {
        "applicator_status": status,
        "source": source,
        "duration_seconds": duration_seconds,
        "gap_threshold_seconds": gap_threshold_seconds,
        "packet_count": 0,
        "heartbeat_count": 0,
        "heartbeat_intervals_seconds": [],
        "max_heartbeat_interval_seconds": 0.0,
        "heartbeat_gap_count": 0,
        "heartbeat_gap_observed": False,
        "endpoint_stop_performed": False,
        "endpoint_restart_performed": False,
        "observer_sent_packets": False,
        "packet_drop_performed": False,
        "rf_link_loss_claimed": False,
        "vehicle_failsafe_claimed": False,
        "stdout_tail": result.stdout[-500:],
        "stderr_tail": result.stderr[-500:],
    }


def apply_bounded_mavlink_link_loss(
    *,
    runner: CommandRunner,
    container_name: str,
    helper_source: str,
    route_px4_port: int,
    route_local_port: int,
    emergency_px4_port: int,
    emergency_local_port: int,
    restart_emergency: bool,
    duration_seconds: float = 2.5,
    gap_threshold_seconds: float = 2.0,
) -> dict[str, Any]:
    result = runner(
        [
            "docker",
            "exec",
            "-i",
            container_name,
            "python3",
            "-",
            str(duration_seconds),
            str(gap_threshold_seconds),
            str(route_px4_port),
            str(route_local_port),
            str(emergency_px4_port),
            str(emergency_local_port),
            "1" if restart_emergency else "0",
        ],
        input_text=helper_source,
        check=False,
        timeout=int(duration_seconds) + 20,
    )
    source = f"udp://127.0.0.1:{route_local_port}"
    if result.returncode != 0:
        return _link_loss_failure(
            status="failed",
            source=source,
            duration_seconds=duration_seconds,
            gap_threshold_seconds=gap_threshold_seconds,
            result=result,
        )
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return _link_loss_failure(
            status="invalid_output",
            source=source,
            duration_seconds=duration_seconds,
            gap_threshold_seconds=gap_threshold_seconds,
            result=result,
        )
    payload["observer_sent_packets"] = False
    payload["packet_drop_performed"] = False
    payload["rf_link_loss_claimed"] = False
    payload["vehicle_failsafe_claimed"] = False
    return payload


__all__ = [
    "CommandRunner",
    "apply_bounded_mavlink_link_loss",
    "observe_mavlink_heartbeat_gap",
    "send_embedded_helper",
]

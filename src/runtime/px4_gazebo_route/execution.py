"""Low-level PX4/Gazebo route execution mechanics.

These functions execute only after a caller has validated approval and
allowlist authority. They receive the process runner and endpoint configuration
explicitly; they do not mint approval, choose a recovery action, or verify
mission completion.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable, Protocol


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


def run_route_with_monitor(
    *,
    target_x: float,
    target_y: float,
    target_z: float,
    expected_target_x: float,
    expected_target_y: float,
    pickup_pose: dict[str, float],
    altitude_max_m: float,
    max_pose_deviation_xy_m: float,
    max_pose_deviation_z_m: float,
    duration_seconds: float,
    container_name: str,
    helper_source: str,
    pose_sampler: Callable[[], dict[str, float]],
    append_pose_row: Callable[..., None],
    distance_to_segment: Callable[..., float],
    feed_forward_vx_mps: float = 0.0,
    feed_forward_vy_mps: float = 0.0,
    feed_forward_ramp_start_fraction: float = 0.65,
    feed_forward_ramp_end_fraction: float = 0.9,
    timeout: int = 45,
    on_deviation: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    command = [
        "docker",
        "exec",
        "-i",
        container_name,
        "python3",
        "-",
        "route",
        str(target_x),
        str(target_y),
        str(target_z),
        str(duration_seconds),
        str(feed_forward_vx_mps),
        str(feed_forward_vy_mps),
        str(feed_forward_ramp_start_fraction),
        str(feed_forward_ramp_end_fraction),
    ]
    process = popen_factory(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write(helper_source)
    process.stdin.close()
    process.stdin = None
    started_at = monotonic()
    deviation_samples: list[dict[str, Any]] = []
    monitor_sample_count = 0
    pickup_xy = (float(pickup_pose["x"]), float(pickup_pose["y"]))
    expected_target_xy = (expected_target_x, expected_target_y)
    while process.poll() is None:
        if monotonic() - started_at > timeout:
            process.terminate()
            raise RuntimeError("route helper timed out while monitoring pose")
        sample = pose_sampler()
        append_pose_row("route", sample, sample_index=monitor_sample_count)
        monitor_sample_count += 1
        deviation_xy = distance_to_segment(
            point_xy=(float(sample["x"]), float(sample["y"])),
            start_xy=pickup_xy,
            end_xy=expected_target_xy,
        )
        deviation_z = abs(float(sample["z"]) - float(altitude_max_m))
        if deviation_xy > max_pose_deviation_xy_m or deviation_z > max_pose_deviation_z_m:
            deviation_observation = {
                "phase": "route",
                "sample": sample,
                "sample_index": monitor_sample_count - 1,
                "elapsed_seconds": monotonic() - started_at,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "deviation_xy_m": deviation_xy,
                "deviation_z_m": deviation_z,
                "threshold_xy_m": max_pose_deviation_xy_m,
                "threshold_z_m": max_pose_deviation_z_m,
            }
            deviation_samples.append(deviation_observation)
            process.terminate()
            route_stream_stop_reason = "pose_deviation"
            route_stream_forced_kill = False
            try:
                _stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                _stdout, stderr = process.communicate(timeout=5)
                route_stream_stop_reason = "pose_deviation_forced_kill"
                route_stream_forced_kill = True
            recovery_payload = None
            if on_deviation is not None:
                recovery_payload = on_deviation(deviation_observation)
            return {
                "mode": "route",
                "sent": False,
                "pose_deviation_aborted": True,
                "deviation_samples": deviation_samples,
                "route_monitor_sample_count": monitor_sample_count,
                "route_stream_terminated_before_recovery_dispatch": True,
                "route_stream_process_returncode": process.returncode,
                "route_stream_stop_reason": route_stream_stop_reason,
                "route_stream_forced_kill": route_stream_forced_kill,
                "feed_forward_velocity_x_mps": feed_forward_vx_mps,
                "feed_forward_velocity_y_mps": feed_forward_vy_mps,
                "feed_forward_phase_schedule": "full_then_linear_ramp_down",
                "feed_forward_ramp_start_fraction": feed_forward_ramp_start_fraction,
                "feed_forward_ramp_end_fraction": feed_forward_ramp_end_fraction,
                "feed_forward_scale_min": None,
                "feed_forward_scale_max": None,
                "feed_forward_scale_sample_count": 0,
                "recovery_payload": recovery_payload,
                "stderr": stderr,
            }
        sleep(1)
    stdout, stderr = process.communicate(timeout=5)
    if process.returncode != 0:
        raise RuntimeError(f"route helper failed: {stderr}")
    payload = json.loads(stdout.strip())
    payload["pose_deviation_aborted"] = False
    payload["deviation_samples"] = []
    payload["route_monitor_sample_count"] = monitor_sample_count
    payload["feed_forward_velocity_x_mps"] = float(
        payload.get("feed_forward_velocity_x_mps", feed_forward_vx_mps)
    )
    payload["feed_forward_velocity_y_mps"] = float(
        payload.get("feed_forward_velocity_y_mps", feed_forward_vy_mps)
    )
    payload["feed_forward_phase_schedule"] = payload.get(
        "feed_forward_phase_schedule",
        "full_then_linear_ramp_down",
    )
    payload["feed_forward_ramp_start_fraction"] = float(
        payload.get("feed_forward_ramp_start_fraction", feed_forward_ramp_start_fraction)
    )
    payload["feed_forward_ramp_end_fraction"] = float(
        payload.get("feed_forward_ramp_end_fraction", feed_forward_ramp_end_fraction)
    )
    payload["feed_forward_scale_min"] = payload.get("feed_forward_scale_min")
    payload["feed_forward_scale_max"] = payload.get("feed_forward_scale_max")
    payload["feed_forward_scale_sample_count"] = int(
        payload.get("feed_forward_scale_sample_count") or 0
    )
    return payload


__all__ = [
    "CommandRunner",
    "apply_bounded_mavlink_link_loss",
    "observe_mavlink_heartbeat_gap",
    "run_route_with_monitor",
    "send_embedded_helper",
]

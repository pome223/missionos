"""PX4/Gazebo SITL execution readiness checks."""

from __future__ import annotations

import socket
import subprocess
from datetime import datetime, timezone
from typing import Callable


PX4_GAZEBO_SITL_EXECUTION_READINESS_SCHEMA_VERSION = (
    "px4_gazebo_sitl_execution_readiness.v1"
)


def _run_command(args: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None


def _default_docker_container_running_probe(timeout_seconds: float) -> bool:
    result = _run_command(
        ["docker", "ps", "--format", "{{.Names}} {{.Image}}"],
        timeout_seconds=timeout_seconds,
    )
    if result is None or result.returncode != 0:
        return False
    text = (result.stdout or "").lower()
    if not text.strip():
        return False
    return any(token in text for token in ("px4", "gazebo", "sitl", "gz"))


def _default_mavlink_endpoint_observed_probe(
    endpoint_host: str,
    mavlink_udp_port: int,
    timeout_seconds: float,
) -> bool:
    lsof = _run_command(
        ["lsof", "-nP", f"-iUDP:{mavlink_udp_port}"],
        timeout_seconds=timeout_seconds,
    )
    if lsof is not None and lsof.returncode == 0 and str(mavlink_udp_port) in (
        lsof.stdout or ""
    ):
        return True

    ss = _run_command(
        ["ss", "-H", "-lun", f"sport = :{mavlink_udp_port}"],
        timeout_seconds=timeout_seconds,
    )
    if ss is not None and ss.returncode == 0 and str(mavlink_udp_port) in (
        ss.stdout or ""
    ):
        return True

    # UDP has no connect handshake; this only catches obvious local socket errors.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(min(timeout_seconds, 0.25))
            sock.connect((endpoint_host, mavlink_udp_port))
            return False
    except OSError:
        return False


def build_px4_gazebo_sitl_execution_readiness(
    *,
    endpoint_host: str = "127.0.0.1",
    mavlink_udp_port: int = 14540,
    docker_required: bool = True,
    timeout_seconds: float = 2.0,
    sitl_startup_action_available: bool = False,
    docker_container_running_probe: Callable[[float], bool] | None = None,
    mavlink_endpoint_observed_probe: Callable[[str, int, float], bool] | None = None,
) -> dict:
    docker_probe = (
        docker_container_running_probe or _default_docker_container_running_probe
    )
    endpoint_probe = (
        mavlink_endpoint_observed_probe or _default_mavlink_endpoint_observed_probe
    )
    docker_container_running = bool(docker_probe(timeout_seconds))
    mavlink_endpoint_observed = bool(
        endpoint_probe(endpoint_host, mavlink_udp_port, timeout_seconds)
    )

    blocked_reasons: list[str] = []
    startup_action_available = bool(sitl_startup_action_available)
    # startup_action_available excuses only a not-yet-started container (the
    # operator clicked "Start SITL" but the container is not up yet).
    # It does NOT excuse a missing MAVLink endpoint:
    #   /start-sitl success != MAVLink upload readiness
    # The host-side MAVLink probe (lsof / ss on port 14540) must pass before
    # mission_upload_allowed is true.  Port 14540 is exposed via -p at docker run
    # time, so it becomes observable once PX4 SITL is fully initialised inside
    # the container.
    if docker_required and not docker_container_running and not startup_action_available:
        blocked_reasons.append("px4_gazebo_sitl_container_not_running")
    if not mavlink_endpoint_observed:
        blocked_reasons.append(f"mavlink_endpoint_{mavlink_udp_port}_not_observed")

    ready = not blocked_reasons
    return {
        "schema_version": PX4_GAZEBO_SITL_EXECUTION_READINESS_SCHEMA_VERSION,
        "readiness_status": "ready" if ready else "blocked",
        "endpoint_host": endpoint_host,
        "mavlink_udp_port": mavlink_udp_port,
        "docker_container_running": docker_container_running,
        "mavlink_endpoint_observed": mavlink_endpoint_observed,
        "sitl_startup_action_available": startup_action_available,
        "startup_action_will_start_container": (
            startup_action_available and not docker_container_running
        ),
        "mission_upload_allowed": ready,
        "live_flight_runner_allowed": ready,
        "blocked_reasons": blocked_reasons,
        "upload_status": "not_attempted",
        "mission_ack_observed": False,
        "live_flight_runner_invoked": False,
        "progress_counted": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


__all__ = [
    "PX4_GAZEBO_SITL_EXECUTION_READINESS_SCHEMA_VERSION",
    "build_px4_gazebo_sitl_execution_readiness",
]

"""Live PX4/Gazebo flight runner for ``missionos play``.

This runner owns only the play-lab smoke flight: start the official PX4/Gazebo
container, arm/takeoff through PX4 commander, inject the play wind driver into
the running simulator, ask the runtime recovery agent for an advisory, then
land and clean up. It intentionally does not claim delivery completion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence

from src.intelligence.missionos_agent_runtime import run_missionos_runtime_recovery_agent
from src.runtime.missionos_play_scenario import PlayScenario
from src.runtime.missionos_play_weather import WeatherForecast
from src.runtime.missionos_play_wind_driver import (
    WindDriverStep,
    docker_exec_publish_force,
    docker_exec_read_altitude_msl,
    docker_exec_read_velocity_ne,
    run_wind_driver,
)


PX4_IMAGE = "px4io/px4-sitl-gazebo:latest"
PX4_MODEL = "gz_x500"
GAZEBO_WORLD = "default"
DEFAULT_CONTAINER = "missionos_play_sitl"
PX4_BIN = "/opt/px4-gazebo/bin"


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class PlayLiveSitlResult:
    schema_version: str = "missionos_play_live_sitl_result.v1"
    status: str = "unknown"
    container_name: str = DEFAULT_CONTAINER
    started_at: str = ""
    ended_at: str = ""
    flight_duration_s: float = 0.0
    takeoff_observed: bool = False
    route_deviation_xy_m: float = 0.0
    battery_coupled: bool = False
    battery_idle_current_a: float | None = None
    battery_under_load_current_a: float | None = None
    battery_percentage: float | None = None
    gps_denied: bool = False
    position_trustworthy: bool | None = None
    wind_steps: tuple[WindDriverStep, ...] = field(default_factory=tuple)
    recovery_agent_result: Mapping[str, Any] = field(default_factory=dict)
    logs_tail: str = ""
    blocking_reasons: tuple[str, ...] = field(default_factory=tuple)
    delivery_completion_claimed: bool = False
    physical_execution_invoked: bool = False
    progress_counted: bool = False


Runner = Callable[[list[str]], CommandResult]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_runner(args: list[str]) -> CommandResult:
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )
    return CommandResult(
        args=tuple(args),
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _run(runner: Runner, args: list[str]) -> CommandResult:
    return runner(args)


def _logs(container: str, runner: Runner, tail: int = 360) -> str:
    result = _run(runner, ["docker", "logs", "--tail", str(tail), container])
    return (result.stdout or "") + (result.stderr or "")


def _wait_for_startup(
    container: str,
    runner: Runner,
    *,
    timeout_s: float,
    sleep: Sleeper,
    clock: Clock,
) -> tuple[bool, str]:
    deadline = clock() + timeout_s
    last_logs = ""
    while clock() < deadline:
        last_logs = _logs(container, runner)
        if (
            "Gazebo world is ready" in last_logs
            and "gz_bridge] world: default, model: x500_0" in last_logs
            and "Startup script returned successfully" in last_logs
        ):
            return True, last_logs[-3000:]
        sleep(1.0)
    return False, last_logs[-3000:]


def start_play_sitl_container(
    scenario: PlayScenario,
    *,
    container: str = DEFAULT_CONTAINER,
    runner: Runner = _default_runner,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
    timeout_s: float = 120.0,
    extra_run_args: Sequence[str] = (),
) -> tuple[bool, str]:
    _run(runner, ["docker", "rm", "-f", container])
    result = _run(
        runner,
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-p",
            "14540:14540/udp",
            "-e",
            f"PX4_SIM_MODEL={PX4_MODEL}",
            "-e",
            f"PX4_GZ_WORLD={GAZEBO_WORLD}",
            "-e",
            "HEADLESS=1",
            "-e",
            "PX4_GZ_NO_FOLLOW=1",
            "-e",
            f"PX4_HOME_LAT={scenario.takeoff_lat}",
            "-e",
            f"PX4_HOME_LON={scenario.takeoff_lon}",
            "-e",
            f"PX4_HOME_ALT={scenario.takeoff_elevation_m}",
            *extra_run_args,
            PX4_IMAGE,
            "-d",
        ],
    )
    if result.returncode != 0:
        return False, (result.stdout + result.stderr)[-3000:]
    return _wait_for_startup(
        container, runner, timeout_s=timeout_s, sleep=sleep, clock=clock
    )


def _px4(container: str, module: str, *args: str) -> list[str]:
    return ["docker", "exec", container, f"{PX4_BIN}/px4-{module}", *args]


def prepare_and_takeoff(
    *,
    container: str = DEFAULT_CONTAINER,
    runner: Runner = _default_runner,
    sleep: Sleeper = time.sleep,
) -> bool:
    # Headless SITL has no RC/GCS. This superset matches the empirically verified
    # path (COM_RC_IN_MODE=4 + NAV_RCL_ACT=0 + forced arm) that armed and took
    # off in this container, plus harmless belt-and-suspenders breakers.
    for name, value in (
        ("COM_RC_IN_MODE", "4"),
        ("NAV_RCL_ACT", "0"),
        ("NAV_DLL_ACT", "0"),
        ("COM_RCL_EXCEPT", "4"),
        ("COM_PREARM_MODE", "0"),
        ("CBRK_SUPPLY_CHK", "894281"),
    ):
        _run(runner, _px4(container, "param", "set", name, value))
    sleep(2.0)
    armed = _run(runner, _px4(container, "commander", "arm", "-f"))
    sleep(1.0)
    takeoff = _run(runner, _px4(container, "commander", "takeoff"))
    sleep(4.0)
    status = _run(runner, _px4(container, "commander", "status"))
    text = (
        armed.stdout
        + armed.stderr
        + takeoff.stdout
        + takeoff.stderr
        + status.stdout
        + status.stderr
    )
    return "Armed" in text or "navigation mode: Takeoff" in text


def stop_play_sitl_container(
    *, container: str = DEFAULT_CONTAINER, runner: Runner = _default_runner
) -> None:
    _run(runner, _px4(container, "commander", "land"))
    _run(
        runner,
        [
            "docker",
            "exec",
            container,
            "gz",
            "topic",
            "-t",
            "/world/default/wrench/clear",
            "-m",
            "gz.msgs.Entity",
            "-p",
            "name:'x500_0' type:MODEL",
        ],
    )
    _run(runner, ["docker", "rm", "-f", container])


def _read_local_drift_xy(container: str, runner: Runner) -> float | None:
    """Lateral drift magnitude (m) from vehicle_local_position x/y, or None."""
    result = _run(runner, _px4(container, "listener", "vehicle_local_position"))
    text = (result.stdout or "") + (result.stderr or "")
    x = _scan_float(text, "x")
    y = _scan_float(text, "y")
    if x is None or y is None:
        return None
    return math.hypot(x, y)


def _scan_float(text: str, field_name: str) -> float | None:
    match = re.search(rf"(?:^|\s){re.escape(field_name)}:\s*(-?[0-9.eE+]+)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _recovery_snapshot(
    *,
    scenario: PlayScenario,
    wind_steps: tuple[WindDriverStep, ...],
    takeoff_observed: bool,
    route_deviation_xy_m: float = 0.0,
    deviation_limit_m: float = 5.0,
) -> dict[str, Any]:
    latest = wind_steps[-1] if wind_steps else None
    wind_mps = latest.wind_mps if latest else scenario.ambient_wind_mps
    # A wind force well past the airframe's authority can diverge the SITL
    # EKF/local-position estimate, yielding an unphysical drift. Flag that
    # honestly rather than reporting it as a real distance.
    position_estimate_diverged = abs(route_deviation_xy_m) > 100_000.0
    return {
        "schema_version": "missionos_play_live_sitl_telemetry_snapshot.v1",
        "simulator": "px4_gazebo",
        "flight_phase": "post_takeoff_wind_disturbance",
        "takeoff_observed": takeoff_observed,
        # Standard keys the runtime guardrail (_telemetry_risk_reasons) reads so
        # the deterministic safety net detects wind/deviation risk even if the
        # LLM wrongly proposes "continue".
        "wind_speed_mps": wind_mps,
        "route_deviation_xy_m": round(route_deviation_xy_m, 2),
        "wind": {"speed_mps": wind_mps},
        "route": {"deviation_xy_m": round(route_deviation_xy_m, 2)},
        "wind_mps": wind_mps,  # legacy/display
        "wind_driver_step_count": len(wind_steps),
        "recovery": {
            "route_deviation_xy_m": round(route_deviation_xy_m, 2),
            "route_deviation_limit_m": deviation_limit_m,
            "route_deviation_above_limit": route_deviation_xy_m > deviation_limit_m,
            "position_estimate_diverged": position_estimate_diverged,
            "telemetry_stale": False,
            "observation_lost": False,
            "stalled": False,
        },
    }


def run_play_live_sitl(
    *,
    scenario: PlayScenario,
    forecast: WeatherForecast,
    duration_s: float = 20.0,
    step_s: float = 2.0,
    container: str = DEFAULT_CONTAINER,
    runner: Runner = _default_runner,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
    cleanup: bool = True,
    battery_coupling: bool = False,
    gps_denied: bool = False,
) -> PlayLiveSitlResult:
    from src.runtime.missionos_play_battery import (
        prepare_battery_mounts,
        read_battery_state,
    )
    from src.runtime.missionos_play_sensors import (
        gps_health_snapshot,
        read_gps_status,
        set_gps_denied,
    )

    started_at = _utc_now()
    blocking: list[str] = []
    logs = ""
    wind_steps: tuple[WindDriverStep, ...] = ()
    takeoff_observed = False
    route_deviation_xy_m = 0.0
    recovery_result: Mapping[str, Any] = {}
    battery_coupled = False
    battery_idle = None
    battery_under_load = None
    position_trustworthy = None

    extra_run_args: Sequence[str] = ()
    if battery_coupling:
        mounts = prepare_battery_mounts()
        if mounts:
            extra_run_args = mounts
            battery_coupled = True

    try:
        ready, logs = start_play_sitl_container(
            scenario, container=container, runner=runner, sleep=sleep, clock=clock,
            extra_run_args=extra_run_args,
        )
        if not ready:
            blocking.append("px4_gazebo_startup_not_ready")
        else:
            if battery_coupled:
                battery_idle = read_battery_state(container)
            if gps_denied:
                set_gps_denied(container, runner)
            takeoff_observed = prepare_and_takeoff(
                container=container, runner=runner, sleep=sleep
            )
            if not takeoff_observed:
                blocking.append("px4_takeoff_not_observed")
            wind_steps = tuple(
                run_wind_driver(
                    forecast=forecast,
                    scenario=scenario,
                    read_altitude_msl=docker_exec_read_altitude_msl(
                        container, home_alt_m=scenario.takeoff_elevation_m
                    ),
                    publish_force=docker_exec_publish_force(container),
                    duration_s=duration_s,
                    step_s=step_s,
                    clock=clock,
                    sleep=sleep,
                    turbulence=True,
                    read_velocity_ne=docker_exec_read_velocity_ne(container),
                )
            )
            if battery_coupled:
                battery_under_load = read_battery_state(container)
            drift_xy = _read_local_drift_xy(container, runner)
            route_deviation_xy_m = drift_xy if drift_xy is not None else 0.0
            snapshot = _recovery_snapshot(
                scenario=scenario,
                wind_steps=wind_steps,
                takeoff_observed=takeoff_observed,
                route_deviation_xy_m=route_deviation_xy_m,
            )
            if battery_under_load is not None:
                snapshot["battery"] = {
                    "source": "gz_motor_load_coupler",
                    "percentage": battery_under_load.percentage,
                    "current_a": battery_under_load.current_a,
                    "voltage_v": battery_under_load.voltage_v,
                    "real_hardware_endurance_evidence": False,
                }
            if gps_denied:
                gps_state = read_gps_status(container)
                snapshot["gps"] = gps_health_snapshot(gps_state)
                position_trustworthy = gps_state.xy_position_valid is True
            recovery_result = run_missionos_runtime_recovery_agent(
                telemetry_snapshot=snapshot,
                mission_context={
                    "scenario_key": scenario.key,
                    "scenario_title": scenario.title,
                    "delivery_completion_claimed": False,
                    "physical_execution_invoked": False,
                },
                recovery_policy={
                    "policy_ref": "missionos_play_live_sitl_recovery_policy.v1",
                    "max_wind_speed_mps": scenario.vehicle.max_wind_speed_mps,
                    "max_route_deviation_xy_m": 8.0,
                    "emergency_landing_route_deviation_xy_m": 25.0,
                    "preauthorized_actions": [
                        "continue",
                        "hold",
                        "return_to_launch",
                        "land",
                    ],
                },
            )
    finally:
        logs = _logs(container, runner)[-3000:] or logs
        if cleanup:
            stop_play_sitl_container(container=container, runner=runner)

    status = "completed" if not blocking else "blocked"
    payload = {
        "started_at": started_at,
        "ended_at": _utc_now(),
        "wind_step_count": len(wind_steps),
        "blocking_reasons": blocking,
    }
    return PlayLiveSitlResult(
        status=status,
        started_at=started_at,
        ended_at=payload["ended_at"],
        flight_duration_s=float(duration_s),
        takeoff_observed=takeoff_observed,
        route_deviation_xy_m=round(route_deviation_xy_m, 2),
        battery_coupled=battery_coupled,
        battery_idle_current_a=(battery_idle.current_a if battery_idle else None),
        battery_under_load_current_a=(
            battery_under_load.current_a if battery_under_load else None
        ),
        battery_percentage=(
            battery_under_load.percentage if battery_under_load else None
        ),
        gps_denied=gps_denied,
        position_trustworthy=position_trustworthy,
        wind_steps=wind_steps,
        recovery_agent_result=dict(recovery_result),
        logs_tail=logs,
        blocking_reasons=tuple(blocking),
    )


def result_digest(result: PlayLiveSitlResult) -> str:
    return sha256(json.dumps(asdict(result), sort_keys=True).encode()).hexdigest()


__all__ = [
    "DEFAULT_CONTAINER",
    "PlayLiveSitlResult",
    "prepare_and_takeoff",
    "result_digest",
    "run_play_live_sitl",
    "start_play_sitl_container",
    "stop_play_sitl_container",
]

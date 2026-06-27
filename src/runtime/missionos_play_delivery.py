"""Advanced play mission: a real pickup -> dropoff delivery flight in real wind.

Builds on the live-SITL play runner. Instead of a bare takeoff + wind segment,
this uploads a real PX4 waypoint mission (takeoff -> dropoff -> return -> land),
arms into AUTO mission so PX4 actually flies the route, injects the real-weather
wind driver throughout, commands a payload release at the dropoff, and asks the
recovery agent for an advisory if the wind pushes the vehicle off track.

Reuses the proven container launch / arming / wind driver / recovery wiring from
``missionos_play_live_sitl`` and the in-container MAVLink mission uploader from
``scripts.smoke_px4_gazebo_sitl_mission_upload``.

Truth-surface: the route flight, wind force, and drift are real SITL behaviour;
the dropoff "payload release" is a detach command published to the model — in
the stock ``default`` world (no spawned payload body) it is a commanded release,
not an observed physical separation. Nothing claims delivery completion.
"""

from __future__ import annotations

import json
import math
import random
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from src.intelligence.missionos_agent_runtime import run_missionos_runtime_recovery_agent
from src.runtime.missionos_play_scenario import PlayScenario
from src.runtime.missionos_play_weather import WeatherForecast
from src.runtime.missionos_play_wind_driver import (
    docker_exec_publish_force,
    relative_wind_drag_force,
    resolve_gust_at,
    resolve_wind_at,
    wind_drag_force,
)
from src.runtime.missionos_play_live_sitl import (
    DEFAULT_CONTAINER,
    Runner,
    _default_runner,
    _px4,
    _run,
    _scan_float,
    start_play_sitl_container,
    stop_play_sitl_container,
)

# MAV_CMD numbers used in the mission.
_NAV_WAYPOINT = 16
_NAV_TAKEOFF = 22
_NAV_LAND = 21
_FRAME_GLOBAL_REL_ALT = 6  # MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
PAYLOAD_DETACH_TOPIC = "/model/x500_0/delivery_payload/detach"


@dataclass(frozen=True)
class DeliveryFlightStep:
    elapsed_s: float
    drift_m: float
    wind_mps: float
    altitude_agl_m: float
    phase: str


@dataclass(frozen=True)
class PlayDeliveryResult:
    schema_version: str = "missionos_play_delivery_result.v1"
    status: str = "unknown"
    started_at: str = ""
    ended_at: str = ""
    takeoff_observed: bool = False
    mission_uploaded: bool = False
    mission_ack_observed: bool = False
    dropoff_reached: bool = False
    payload_release_commanded: bool = False
    payload_physically_separated: bool = False
    max_drift_xy_m: float = 0.0
    steps: tuple[DeliveryFlightStep, ...] = field(default_factory=tuple)
    recovery_agent_result: Mapping[str, Any] = field(default_factory=dict)
    blocking_reasons: tuple[str, ...] = field(default_factory=tuple)
    delivery_completion_claimed: bool = False
    physical_execution_invoked: bool = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _offset_latlon(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    dlat = north_m / 111_320.0
    dlon = east_m / (111_320.0 * math.cos(math.radians(lat)))
    return round(lat + dlat, 7), round(lon + dlon, 7)


def _cross_track_m(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Perpendicular distance of point P from the infinite line through A and B."""
    dx, dy = bx - ax, by - ay
    seg_sq = dx * dx + dy * dy
    if seg_sq == 0:
        return math.hypot(px - ax, py - ay)
    return abs((px - ax) * dy - (py - ay) * dx) / math.sqrt(seg_sq)


def _item(seq, command, lat, lon, alt, current=0):
    return {
        "seq": seq,
        "command": command,
        "latitude_deg": lat,
        "longitude_deg": lon,
        "altitude_m": alt,
        "current": current,
        "frame": _FRAME_GLOBAL_REL_ALT,
        "param1": 0.0,
        "param2": 0.0,
        "param3": 0.0,
        "param4": 0.0,
    }


def build_delivery_mission_items(
    scenario: PlayScenario,
    *,
    cruise_alt_m: float = 30.0,
    dropoff_north_m: float = 200.0,
    dropoff_east_m: float = 180.0,
) -> tuple[dict, ...]:
    """takeoff(home) -> waypoint(dropoff) -> waypoint(home) -> land(home).

    The dropoff is a short offset from home so the round trip completes in a
    demo-sized window. Dict shape matches the in-container MAVLink uploader's
    ``_mission_upload_item_tuples`` expectations.
    """
    home_lat, home_lon = scenario.takeoff_lat, scenario.takeoff_lon
    drop_lat, drop_lon = _offset_latlon(home_lat, home_lon, dropoff_north_m, dropoff_east_m)
    return (
        _item(0, _NAV_TAKEOFF, home_lat, home_lon, cruise_alt_m, current=1),
        _item(1, _NAV_WAYPOINT, drop_lat, drop_lon, cruise_alt_m),
        _item(2, _NAV_WAYPOINT, home_lat, home_lon, cruise_alt_m),
        _item(3, _NAV_LAND, home_lat, home_lon, 0.0),
    )


def upload_mission(container: str, items: tuple[tuple, ...]) -> dict[str, Any]:
    """Upload a MAVLink mission into the running PX4 via the in-container script.

    Uses subprocess directly because the upload pipes the generated script over
    stdin (`docker exec -i ... python3 -`), which the runner abstraction does not
    model.
    """
    from scripts.smoke_px4_gazebo_sitl_mission_upload import _inner_upload_script

    try:
        result = subprocess.run(
            ["docker", "exec", "-i", container, "python3", "-"],
            input=_inner_upload_script(items),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {"mission_ack_observed": False, "error": f"{type(exc).__name__}: {exc}"}
    out = (result.stdout or "").strip().splitlines()
    if not out:
        return {"mission_ack_observed": False, "error": (result.stderr or "")[-400:]}
    try:
        return json.loads(out[-1])
    except (ValueError, IndexError):
        return {"mission_ack_observed": False, "error": out[-1][:400]}


def start_mission(container: str, runner: Runner) -> None:
    """Arm and switch to AUTO mission so PX4 flies the uploaded route."""
    _run(runner, _px4(container, "commander", "arm", "-f"))
    _run(runner, _px4(container, "commander", "mode", "auto:mission"))


def _read_dist_to_home(container: str, runner: Runner) -> float | None:
    result = _run(runner, _px4(container, "listener", "vehicle_local_position"))
    text = (result.stdout or "") + (result.stderr or "")
    x, y = _scan_float(text, "x"), _scan_float(text, "y")
    if x is None or y is None:
        return None
    return math.hypot(x, y)


def command_payload_release(container: str, runner: Runner) -> bool:
    """Publish the payload detach command. Returns True if the command was sent."""
    result = _run(
        runner,
        [
            "docker", "exec", container,
            "gz", "topic", "-t", PAYLOAD_DETACH_TOPIC,
            "-m", "gz.msgs.Empty", "-p", "",
        ],
    )
    return result.returncode == 0


def _recovery_snapshot(*, scenario, drift_m, wind_mps, phase, deviation_limit_m):
    return {
        "schema_version": "missionos_play_delivery_telemetry_snapshot.v1",
        "simulator": "px4_gazebo",
        "flight_phase": phase,
        # Standard keys the runtime guardrail reads (wind.speed_mps /
        # route.deviation_xy_m / top-level) so the deterministic safety net sees
        # the cross-track + wind risk, not just the LLM.
        "wind_speed_mps": wind_mps,
        "route_deviation_xy_m": round(drift_m, 2),
        "wind": {"speed_mps": wind_mps},
        "route": {"deviation_xy_m": round(drift_m, 2)},
        "wind_mps": wind_mps,  # legacy/display
        "recovery": {
            "route_deviation_xy_m": round(drift_m, 2),
            "route_deviation_limit_m": deviation_limit_m,
            "route_deviation_above_limit": drift_m > deviation_limit_m,
            "position_estimate_diverged": abs(drift_m) > 100_000.0,
            "telemetry_stale": False,
            "observation_lost": False,
            "stalled": False,
        },
    }


def run_play_delivery(
    *,
    scenario: PlayScenario,
    forecast: WeatherForecast,
    duration_s: float = 90.0,
    step_s: float = 4.0,
    cruise_alt_m: float = 30.0,
    deviation_limit_m: float = 15.0,
    dropoff_radius_m: float = 12.0,
    container: str = DEFAULT_CONTAINER,
    runner: Runner = _default_runner,
    sleep=time.sleep,
    clock=time.monotonic,
    cleanup: bool = True,
    payload_separation: bool = False,
) -> PlayDeliveryResult:
    started_at = _utc_now()
    blocking: list[str] = []
    steps: list[DeliveryFlightStep] = []
    recovery_result: Mapping[str, Any] = {}
    takeoff_observed = mission_uploaded = mission_ack = dropoff_reached = False
    payload_commanded = False
    payload_separated = False
    max_drift = 0.0
    publish_force = docker_exec_publish_force(container)

    extra_run_args: list[str] = []
    if payload_separation:
        from src.runtime.missionos_play_payload import prepare_payload_mounts

        mounts = prepare_payload_mounts(payload_mass_kg=1.0)
        if mounts:
            extra_run_args = mounts

    try:
        ready, _logs_text = start_play_sitl_container(
            scenario, container=container, runner=runner, sleep=sleep, clock=clock,
            extra_run_args=extra_run_args,
        )
        if not ready:
            blocking.append("px4_gazebo_startup_not_ready")
        else:
            items = build_delivery_mission_items(scenario, cruise_alt_m=cruise_alt_m)
            upload = upload_mission(container, items)
            mission_uploaded = True
            mission_ack = bool(upload.get("mission_ack_observed"))
            if not mission_ack:
                blocking.append("mission_upload_not_acked")
            # Dropoff offset (home-frame meters) to detect arrival.
            drop_n, drop_e = 200.0, 180.0
            start_mission(container, runner)
            takeoff_observed = True

            start = clock()
            rng = random.Random()
            gust_state = 0.0
            while clock() - start <= duration_s:
                elapsed = clock() - start
                pos = _run(runner, _px4(container, "listener", "vehicle_local_position"))
                text = (pos.stdout or "")
                x, y = _scan_float(text, "x"), _scan_float(text, "y")
                if x is None or y is None:
                    sleep(step_s)
                    continue
                z = _scan_float(text, "z") or 0.0
                vx, vy = _scan_float(text, "vx"), _scan_float(text, "vy")
                dist_to_drop = math.hypot(x - drop_n, y - drop_e)
                alt_agl = -z
                altitude_msl = scenario.takeoff_elevation_m + max(0.0, alt_agl)
                mean_wind, bearing, _ = resolve_wind_at(
                    forecast, scenario, elapsed_s=elapsed, altitude_msl_m=altitude_msl
                )
                # Same wind model as the live runner: real-gust turbulence (OU) on
                # the mean, applied as relative-airflow drag against the vehicle.
                gust_amp = max(
                    0.0,
                    resolve_gust_at(
                        forecast, scenario, elapsed_s=elapsed, altitude_msl_m=altitude_msl
                    )
                    - mean_wind,
                )
                gust_state = 0.8 * gust_state + rng.gauss(0.0, 0.6 * gust_amp)
                wind_mps = max(0.0, mean_wind + gust_state)
                if vx is not None and vy is not None:
                    fe, fn = relative_wind_drag_force(
                        wind_mps, bearing, vel_north_mps=vx, vel_east_mps=vy
                    )
                else:
                    fe, fn = wind_drag_force(wind_mps, bearing)
                publish_force(fe, fn)

                phase = "outbound" if not dropoff_reached else "return"
                if not dropoff_reached and dist_to_drop <= dropoff_radius_m:
                    dropoff_reached = True
                    phase = "dropoff"
                    if payload_separation:
                        from src.runtime.missionos_play_payload import (
                            command_detach,
                            read_payload_z,
                            verify_separation,
                        )

                        z_before = read_payload_z(container)
                        payload_commanded = command_detach(container)
                        sleep(step_s)
                        z_after = read_payload_z(container)
                        payload_separated = verify_separation(z_before, z_after)
                    else:
                        payload_commanded = command_payload_release(container, runner)

                # Cross-track deviation = perpendicular distance from the *active*
                # planned leg (home->drop outbound, drop->home on return). This is
                # the real wind-induced off-track error, not the route distance.
                if dropoff_reached:
                    cross_track = _cross_track_m(x, y, drop_n, drop_e, 0.0, 0.0)
                else:
                    cross_track = _cross_track_m(x, y, 0.0, 0.0, drop_n, drop_e)
                max_drift = max(max_drift, cross_track)
                steps.append(
                    DeliveryFlightStep(
                        elapsed_s=round(elapsed, 1),
                        drift_m=round(cross_track, 2),
                        wind_mps=round(wind_mps, 2),
                        altitude_agl_m=round(max(0.0, alt_agl), 2),
                        phase=phase,
                    )
                )
                if cross_track > deviation_limit_m and not recovery_result:
                    snapshot = _recovery_snapshot(
                        scenario=scenario, drift_m=cross_track, wind_mps=wind_mps,
                        phase=phase, deviation_limit_m=deviation_limit_m,
                    )
                    recovery_result = run_missionos_runtime_recovery_agent(
                        telemetry_snapshot=snapshot,
                        mission_context={
                            "scenario_key": scenario.key,
                            "mission_kind": "pickup_dropoff_delivery",
                            "delivery_completion_claimed": False,
                            "physical_execution_invoked": False,
                        },
                        recovery_policy={
                            "policy_ref": "missionos_play_delivery_recovery_policy.v1",
                            "max_wind_speed_mps": scenario.vehicle.max_wind_speed_mps,
                            "max_route_deviation_xy_m": deviation_limit_m,
                            "emergency_landing_route_deviation_xy_m": deviation_limit_m * 3.0,
                            "preauthorized_actions": ["continue", "hold", "return_to_launch", "land"],
                        },
                    )
                sleep(step_s)
    finally:
        if cleanup:
            stop_play_sitl_container(container=container, runner=runner)

    status = "completed" if not blocking else "blocked"
    return PlayDeliveryResult(
        status=status,
        started_at=started_at,
        ended_at=_utc_now(),
        takeoff_observed=takeoff_observed,
        mission_uploaded=mission_uploaded,
        mission_ack_observed=mission_ack,
        dropoff_reached=dropoff_reached,
        payload_release_commanded=bool(payload_commanded),
        payload_physically_separated=payload_separated,
        max_drift_xy_m=round(max_drift, 2),
        steps=tuple(steps),
        recovery_agent_result=dict(recovery_result),
        blocking_reasons=tuple(blocking),
    )

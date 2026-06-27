"""Inject real time- and altitude-varying wind into a running PX4/Gazebo SITL.

The default PX4 gz world has no wind system, but it does load ApplyLinkWrench
(`/world/default/wrench/persistent`, message `gz.msgs.EntityWrench`). This driver
turns the real Open-Meteo forecast into a drag force on the vehicle and updates
it during flight so the wind the drone fights genuinely changes:

- **time-varying (real):** the force is recomputed each step from the hourly
  forecast sampled at the current mission time.
- **altitude-varying (modelled-from-real):** the surface wind is lifted to the
  drone's *current* altitude with the power-law profile each step, so climbing
  changes the wind in the sim, not just in a label.

Transport is injected (``publish_force`` / ``read_altitude_msl``) so the loop is
unit-testable offline; the live implementation drives ``gz topic`` over
``docker exec``. This applies a modelled aerodynamic force, so it is honest
about being a model on top of real wind data — not a certified airframe model.
"""

from __future__ import annotations

import math
import random
import subprocess
from dataclasses import dataclass
from typing import Callable

from src.runtime.missionos_play_scenario import PlayScenario
from src.runtime.missionos_play_weather import (
    WeatherForecast,
    profile_wind_at,
    sample_at_elapsed,
)
from src.runtime.missionos_play_sitl_conditions import wind_at_altitude

# Modelled aerodynamic constants for an x500-class quad. F = 0.5 * rho * Cd*A * v^2.
AIR_DENSITY_KG_M3 = 1.0  # ~mountain altitude (modelled)
DRAG_CD_AREA_M2 = 0.6  # Cd * frontal area (modelled)
# Physical bound on the drag a ~2 kg quad can experience. Caps the relative-drag
# feedback so a diverged velocity read can't blow the force up unphysically.
MAX_DRAG_FORCE_N = 80.0


def wind_drag_force(
    wind_mps: float,
    bearing_from_deg: float,
    *,
    air_density: float = AIR_DENSITY_KG_M3,
    drag_cd_area: float = DRAG_CD_AREA_M2,
) -> tuple[float, float]:
    """Return (force_east_N, force_north_N) for wind of ``wind_mps`` blowing FROM
    ``bearing_from_deg`` (meteorological convention).

    The force pushes the vehicle toward ``bearing + 180``. gz ENU frame: x=East,
    y=North.
    """
    magnitude = min(
        MAX_DRAG_FORCE_N, 0.5 * air_density * drag_cd_area * float(wind_mps) ** 2
    )
    toward = math.radians((float(bearing_from_deg) + 180.0) % 360.0)
    force_east = magnitude * math.sin(toward)
    force_north = magnitude * math.cos(toward)
    return round(force_east, 4), round(force_north, 4)


def _wind_vector_en(wind_mps: float, bearing_from_deg: float) -> tuple[float, float]:
    """Wind velocity (east, north) in m/s. Wind FROM bearing blows TOWARD +180."""
    toward = math.radians((float(bearing_from_deg) + 180.0) % 360.0)
    return wind_mps * math.sin(toward), wind_mps * math.cos(toward)


def relative_wind_drag_force(
    wind_mps: float,
    bearing_from_deg: float,
    *,
    vel_north_mps: float,
    vel_east_mps: float,
    air_density: float = AIR_DENSITY_KG_M3,
    drag_cd_area: float = DRAG_CD_AREA_M2,
) -> tuple[float, float]:
    """Drag from the *relative* airflow (wind − vehicle velocity).

    A drone moving with the wind feels ~no force (no relative airflow); fighting
    the wind feels more. F = 0.5·rho·Cd·A·|v_rel|·v_rel — physically the real
    relative-airflow drag, still with a modelled drag coefficient.
    """
    wind_e, wind_n = _wind_vector_en(wind_mps, bearing_from_deg)
    rel_e = wind_e - float(vel_east_mps)
    rel_n = wind_n - float(vel_north_mps)
    speed = math.hypot(rel_e, rel_n)
    if speed <= 0.0:
        return 0.0, 0.0
    magnitude = min(MAX_DRAG_FORCE_N, 0.5 * air_density * drag_cd_area * speed * speed)
    return round(magnitude * rel_e / speed, 4), round(magnitude * rel_n / speed, 4)


@dataclass(frozen=True)
class WindDriverStep:
    elapsed_s: float
    altitude_agl_m: float
    wind_mps: float
    bearing_from_deg: float
    force_east_n: float
    force_north_n: float


def resolve_wind_at(
    forecast: WeatherForecast,
    scenario: PlayScenario,
    *,
    elapsed_s: float,
    altitude_msl_m: float,
) -> tuple[float, float, float]:
    """Return (wind_mps_at_altitude, bearing_from_deg, altitude_agl_m)."""
    sample = sample_at_elapsed(forecast, elapsed_s / 60.0)
    surface = (sample.wind_speed_mps if sample else None) or scenario.ambient_wind_mps
    bearing = (sample.wind_direction_deg if sample else None) or 0.0
    altitude_agl = max(0.0, float(altitude_msl_m) - scenario.takeoff_elevation_m)
    # Prefer the real multi-height forecast profile; fall back to the modelled
    # power-law only when no real profile was fetched.
    real_wind = profile_wind_at(forecast, altitude_agl)
    wind = real_wind if real_wind is not None else wind_at_altitude(surface, altitude_agl)
    return wind, bearing, altitude_agl


# Gust/turbulence model. Dynamics are an Ornstein-Uhlenbeck (mean-reverting)
# process — a model — but the *amplitude* is the real forecast gust spread, so
# turbulence intensity tracks the real weather.
_GUST_REVERSION = 0.8  # how strongly the gust pulls back toward the mean each step
_GUST_KICK_FRACTION = 0.6  # random kick scaled by the real gust-over-mean spread


def resolve_gust_at(
    forecast: WeatherForecast,
    scenario: PlayScenario,
    *,
    elapsed_s: float,
    altitude_msl_m: float,
) -> float:
    """Real gust wind at altitude, scaled from the surface gust/mean ratio."""
    sample = sample_at_elapsed(forecast, elapsed_s / 60.0)
    surface_mean = (sample.wind_speed_mps if sample else None) or scenario.ambient_wind_mps
    surface_gust = (sample.wind_gust_mps if sample else None) or surface_mean
    wind_at_alt, _bearing, _agl = resolve_wind_at(
        forecast, scenario, elapsed_s=elapsed_s, altitude_msl_m=altitude_msl_m
    )
    if surface_mean and surface_mean > 0:
        return wind_at_alt * (surface_gust / surface_mean)
    return wind_at_alt


def run_wind_driver(
    *,
    forecast: WeatherForecast,
    scenario: PlayScenario,
    read_altitude_msl: Callable[[], float | None],
    publish_force: Callable[[float, float], None],
    duration_s: float,
    step_s: float = 2.0,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    turbulence: bool = False,
    rng: "random.Random | None" = None,
    read_velocity_ne: Callable[[], "tuple[float, float] | None"] | None = None,
) -> list[WindDriverStep]:
    """Drive time/altitude-varying wind force for ``duration_s``.

    With ``turbulence=True`` a real-gust-driven stochastic gust is added to the
    mean wind each step. With ``read_velocity_ne`` the force is computed from the
    *relative* airflow (wind − vehicle velocity), so a drone moving with the wind
    feels less force and one fighting it feels more. ``clock`` and ``sleep`` are
    injected so tests can run the loop without real time.
    """
    start = clock()
    rng = rng or random.Random()
    gust_state = 0.0
    steps: list[WindDriverStep] = []
    while True:
        elapsed = clock() - start
        if elapsed > duration_s:
            break
        altitude = read_altitude_msl()
        if altitude is None:
            altitude = scenario.takeoff_elevation_m
        mean_wind, bearing, agl = resolve_wind_at(
            forecast, scenario, elapsed_s=elapsed, altitude_msl_m=altitude
        )
        wind_mps = mean_wind
        if turbulence:
            gust_amplitude = max(
                0.0,
                resolve_gust_at(
                    forecast, scenario, elapsed_s=elapsed, altitude_msl_m=altitude
                )
                - mean_wind,
            )
            gust_state = _GUST_REVERSION * gust_state + rng.gauss(
                0.0, _GUST_KICK_FRACTION * gust_amplitude
            )
            wind_mps = max(0.0, mean_wind + gust_state)
        velocity = read_velocity_ne() if read_velocity_ne is not None else None
        if velocity is not None:
            vel_north, vel_east = velocity
            force_east, force_north = relative_wind_drag_force(
                wind_mps, bearing, vel_north_mps=vel_north, vel_east_mps=vel_east
            )
        else:
            force_east, force_north = wind_drag_force(wind_mps, bearing)
        publish_force(force_east, force_north)
        steps.append(
            WindDriverStep(
                elapsed_s=round(elapsed, 2),
                altitude_agl_m=round(agl, 2),
                wind_mps=round(wind_mps, 3),
                bearing_from_deg=round(bearing, 1),
                force_east_n=force_east,
                force_north_n=force_north,
            )
        )
        sleep(step_s)
    return steps


# --- Live transport over `docker exec ... gz topic` -------------------------

_MODEL_ENTITY = "x500_0"
_PX4_BIN = "/opt/px4-gazebo/bin"


def docker_exec_read_velocity_ne(container: str):
    """Return ``read_velocity_ne()`` -> (north_mps, east_mps) from PX4 telemetry.

    vehicle_local_position is NED: vx=north, vy=east. None on a failed read so
    the driver falls back to wind-only (non-relative) drag.
    """

    def read() -> "tuple[float, float] | None":
        try:
            result = subprocess.run(
                ["docker", "exec", container,
                 f"{_PX4_BIN}/px4-listener", "vehicle_local_position"],
                check=False, capture_output=True, text=True, timeout=15,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        text = result.stdout or ""
        vn = _scan_float_field(text, "vx")
        ve = _scan_float_field(text, "vy")
        if vn is None or ve is None:
            return None
        return vn, ve

    return read


def _scan_float_field(text: str, field_name: str) -> float | None:
    import re as _re

    match = _re.search(rf"(?:^|\s){field_name}:\s*(-?[0-9.eE+]+)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def docker_exec_publish_force(container: str, model: str = _MODEL_ENTITY):
    """Return a ``publish_force(east, north)`` that applies a persistent wrench."""

    def publish(force_east: float, force_north: float) -> None:
        msg = (
            f"entity:{{name:'{model}' type:MODEL}} "
            f"wrench:{{force:{{x:{force_east} y:{force_north} z:0}}}}"
        )
        try:
            subprocess.run(
                [
                    "docker", "exec", container,
                    "gz", "topic", "-t", "/world/default/wrench/persistent",
                    "-m", "gz.msgs.EntityWrench", "-p", msg,
                ],
                check=False,
                capture_output=True,
                timeout=15,
            )
        except (subprocess.SubprocessError, OSError):
            return

    return publish


def docker_exec_read_altitude_msl(
    container: str, *, home_alt_m: float, model: str = _MODEL_ENTITY
):
    """Return a ``read_altitude_msl()`` reading the model's z pose (AGL) + home.

    The gz pose z is height above the world origin (AGL at takeoff); add the
    scenario home altitude to express MSL for the wind profile.
    """

    def read() -> float | None:
        # `gz topic -e` can stall under heavy sim load; never let a transient
        # read crash the flight — fall back to None so the driver uses the last
        # known/home altitude and keeps injecting wind.
        try:
            result = subprocess.run(
                [
                    "docker", "exec", container,
                    "gz", "topic", "-e", "-n", "1", "-t", "/world/default/pose/info",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        z = _parse_model_z(result.stdout, model)
        return None if z is None else home_alt_m + z

    return read


def _parse_model_z(pose_dump: str, model: str) -> float | None:
    """Pull the model's pose z from a gz.msgs.Pose_V text dump (best effort)."""
    lines = pose_dump.splitlines()
    in_model = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("name:") and f'"{model}"' in stripped:
            in_model = True
        if in_model and stripped.startswith("position"):
            for follow in lines[index : index + 6]:
                fs = follow.strip()
                if fs.startswith("z:"):
                    try:
                        return float(fs.split(":", 1)[1])
                    except ValueError:
                        return None
    return None

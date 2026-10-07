"""Independent declared-wind/observation arithmetic, without plant imports.

This does not integrate dynamics or independently identify atmospheric density.
Callers must bind this profile and observation to their source/state receipts.
"""
from __future__ import annotations

import math

_FIELDS = {"schema", "frame", "mean_enu_mps", "gust_amplitude_enu_mps", "gust_period_s", "gust_phase_rad"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) < 1e50


def _vector(value, length=3, limit=1e15):
    return (type(value) in (list, tuple) and len(value) == length
            and all(_number(x) and abs(x) <= limit for x in value))


def validate_wind_profile(profile):
    """Return the independently validated declaration, without mutation."""
    if type(profile) is not dict:
        raise ValueError("invalid_wind_profile")
    if "environment" not in profile:
        return None
    environment = profile["environment"]
    if type(environment) is not dict or set(environment) != {"wind"}:
        raise ValueError("unsupported_wind_environment")
    value = environment["wind"]
    if value is None:
        return None
    if (type(value) is not dict or set(value) != _FIELDS
            or type(value["schema"]) is not str or type(value["frame"]) is not str
            or value["schema"] != "missionos.starship_wind.v1"
            or value["frame"] != "instantaneous_wgs84_enu"
            or not _vector(value["mean_enu_mps"], limit=100.)
            or not _vector(value["gust_amplitude_enu_mps"], limit=100.)
            or math.hypot(*value["mean_enu_mps"])+math.hypot(*value["gust_amplitude_enu_mps"]) > 100.
            or not _number(value["gust_period_s"]) or not 4. <= value["gust_period_s"] <= 3600.
            or not _number(value["gust_phase_rad"]) or abs(value["gust_phase_rad"]) > 2*math.pi):
        raise ValueError("invalid_wind_declaration")
    return {**value, "mean_enu_mps": list(value["mean_enu_mps"]),
            "gust_amplitude_enu_mps": list(value["gust_amplitude_enu_mps"])}


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _rotate(q, v):
    first = _cross(q[1:], v)
    second = _cross(q[1:], first)
    return [v[i]+2*q[0]*first[i]+2*second[i] for i in range(3)]


def _close_vector(actual, expected):
    return _vector(actual) and all(abs(a-b) <= 1e-5 for a, b in zip(actual, expected))


def _expected_wind(configuration, state, applied):
    if not applied:
        return [0., 0., 0.], [0., 0., 0.]
    phase = 2*math.pi*state["time_s"]/configuration["gust_period_s"]+configuration["gust_phase_rad"]
    enu = [a+b*math.sin(phase) for a, b in zip(configuration["mean_enu_mps"], configuration["gust_amplitude_enu_mps"])]
    x, y, z = state["r_eci_m"]
    p = math.hypot(x, y)
    flattening = 1/298.257223563
    eccentricity = flattening*(2-flattening)
    latitude = math.atan2(z, p*(1-eccentricity))
    for _ in range(12):
        radius = 6378137./math.sqrt(1-eccentricity*math.sin(latitude)**2)
        latitude = math.atan2(z+eccentricity*radius*math.sin(latitude), p)
    longitude = math.atan2(y, x)
    east = [-math.sin(longitude), math.cos(longitude), 0.]
    north = [-math.sin(latitude)*math.cos(longitude), -math.sin(latitude)*math.sin(longitude), math.cos(latitude)]
    up = [math.cos(latitude)*math.cos(longitude), math.cos(latitude)*math.sin(longitude), math.sin(latitude)]
    return enu, [east[i]*enu[0]+north[i]*enu[1]+up[i]*enu[2] for i in range(3)]


def verify_wind_observation(sample, profile, *, atmosphere=True):
    result = {"passed": False, "issues": [], "wind_active": False,
              "dynamics_reexecuted": False, "atmosphere_density_reconstructed": False}
    try:
        configuration = validate_wind_profile(profile)
        if type(atmosphere) is not bool or type(sample) is not dict:
            raise ValueError("invalid_wind_observation")
        active = configuration is not None and any(
            x != 0. for x in configuration["mean_enu_mps"]+configuration["gust_amplitude_enu_mps"])
        result["wind_active"] = active
        if not active:
            if (sample.get("wind_applied", False) is not False
                    or any(key in sample and not _close_vector(sample[key], [0., 0., 0.])
                           for key in ("wind_enu_mps", "wind_eci_mps"))):
                raise ValueError("undeclared_wind_observation")
            result["passed"] = True
            return result
        if (not _number(sample.get("time_s")) or abs(sample["time_s"]) > 1e12
                or not _vector(sample.get("r_eci_m")) or math.hypot(*sample["r_eci_m"]) <= 1.
                or not _vector(sample.get("v_eci_mps")) or not _vector(sample.get("q_body_to_eci"), 4)
                or abs(sum(x*x for x in sample["q_body_to_eci"])-1.) > 1e-8
                or sample.get("wind_applied") is not atmosphere):
            raise ValueError("invalid_wind_observation_state")
        enu, eci = _expected_wind(configuration, sample, atmosphere)
        if not _close_vector(sample.get("wind_enu_mps"), enu) or not _close_vector(sample.get("wind_eci_mps"), eci):
            raise ValueError("wind_vector_mismatch")
        x, y, _ = sample["r_eci_m"]
        vx, vy, vz = sample["v_eci_mps"]
        ground = [vx+7.292115e-5*y, vy-7.292115e-5*x, vz]
        wind_relative = [ground[i]-eci[i] for i in range(3)]
        q = sample["q_body_to_eci"]
        # The accepted state tolerance is unchanged. Reconstruct the unit
        # rotation independently, as the plant does before calculating loads.
        # Using a near-unit stored quaternion directly introduces a speed-
        # dependent false residual even though the physical state is valid.
        length = math.sqrt(sum(component*component for component in q))
        q = [component/length for component in q]
        body = _rotate([q[0], -q[1], -q[2], -q[3]], wind_relative)
        speed = math.hypot(*body)
        if (not _close_vector(sample.get("air_velocity_body_mps"), body)
                or not _number(sample.get("air_speed_mps")) or abs(sample["air_speed_mps"]-speed) > 1e-5
                or not _number(sample.get("ground_speed_mps"))
                or abs(sample["ground_speed_mps"]-math.hypot(*ground)) > 1e-5):
            raise ValueError("wind_speed_mismatch")
        density, pressure = sample.get("air_density_kg_m3"), sample.get("dynamic_pressure_pa")
        if not _number(density) or not 0 <= density <= 100. or not _number(pressure) or pressure < 0:
            raise ValueError("invalid_wind_airload_observation")
        expected_pressure = .5*density*speed*speed if atmosphere else 0.
        if abs(pressure-expected_pressure) > 1e-7*max(1., expected_pressure):
            raise ValueError("wind_pressure_mismatch")
        result["passed"] = True
    except (ValueError, TypeError, KeyError, OverflowError):
        result["issues"].append("Invalid declared wind or inconsistent wind-relative observation")
    return result

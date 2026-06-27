"""Bundled deterministic scenarios for the ``missionos play`` mission-control lab.

The digital-twin pipeline in :mod:`src.runtime.digital_twin_mission_environment`
models terrain/weather *fields* but does not vary clearance, energy, or wind
exposure as a graded response to the altitude/route knobs a player turns. The
play lab therefore layers an explicit, deterministic trade-off model
(:mod:`src.runtime.missionos_play_session`) on top of grounded scenario context.

This module supplies that context. Vehicle parameters are loaded from the real
fixture vehicle profile (the same file the digital-twin envelope uses), so the
battery/cruise/climb limits are not invented. The terrain layout (the "world")
is a bundled spec because the upstream DEM source is GSI (Japan-only) and the
flagship route is a Japanese mountain delivery.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.runtime.digital_twin_mission_environment import (
    DEFAULT_DIGITAL_TWIN_VEHICLE_PROFILE_PATH,
)


@dataclass(frozen=True)
class VehicleLimits:
    """Grounded vehicle envelope loaded from the fixture profile."""

    vehicle_id: str
    max_payload_kg: float
    max_range_m: float
    max_takeoff_altitude_m: float
    max_wind_speed_mps: float
    nominal_battery_wh: float
    reserve_percent: float
    cruise_energy_wh_per_km: float
    climb_energy_wh_per_100m: float


@dataclass(frozen=True)
class RouteOption:
    """A selectable corridor between takeoff and target.

    ``terrain_max_elevation_m`` is the highest ground the corridor crosses; a
    detour that skirts a ridge trades extra distance for a lower terrain ceiling.
    """

    name: str
    distance_m: float
    terrain_max_elevation_m: float
    description: str


@dataclass(frozen=True)
class PlayScenario:
    """A grounded, deterministic mission-control scenario for the play lab."""

    key: str
    title: str
    prompt: str
    takeoff_lat: float
    takeoff_lon: float
    target_lat: float
    target_lon: float
    takeoff_elevation_m: float
    ambient_wind_mps: float
    routes: dict[str, RouteOption]
    vehicle: VehicleLimits

    def route(self, name: str) -> RouteOption:
        try:
            return self.routes[name]
        except KeyError as exc:  # pragma: no cover - guarded by CLI parsing
            raise KeyError(
                f"unknown route '{name}'; choices: {', '.join(self.routes)}"
            ) from exc


def _load_vehicle_limits(profile_path: Path | str | None = None) -> VehicleLimits:
    path = Path(profile_path or DEFAULT_DIGITAL_TWIN_VEHICLE_PROFILE_PATH)
    profile = json.loads(path.read_text(encoding="utf-8"))
    return VehicleLimits(
        vehicle_id=str(profile["vehicle_id"]),
        max_payload_kg=float(profile["max_payload_kg"]),
        max_range_m=float(profile["max_range_m"]),
        max_takeoff_altitude_m=float(profile["max_takeoff_altitude_m"]),
        max_wind_speed_mps=float(profile["max_wind_speed_mps"]),
        nominal_battery_wh=float(profile["nominal_battery_wh"]),
        reserve_percent=float(profile["reserve_percent"]),
        cruise_energy_wh_per_km=float(profile["cruise_energy_wh_per_km"]),
        climb_energy_wh_per_100m=float(profile["climb_energy_wh_per_100m"]),
    )


# Fuji-area trailhead → mountain hut delivery. Coordinates match the existing
# source-backed smoke (scripts/smoke_vehicle_envelope_energy.py) so the route
# stays within GSI DEM coverage when Phase 2 swaps in real per-leg sampling.
_FUJI_HUT = dict(
    key="fuji-hut",
    title="Fuji trailhead -> mountain hut delivery",
    prompt="Subashiri trailhead -> Fuji mountain hut",
    takeoff_lat=35.3606,
    takeoff_lon=138.7274,
    target_lat=35.3905,
    target_lon=138.7300,
    takeoff_elevation_m=2300.0,
    ambient_wind_mps=6.0,
    routes={
        "direct": RouteOption(
            name="direct",
            distance_m=3300.0,
            terrain_max_elevation_m=2950.0,
            description="straight line; crosses the summit ridge",
        ),
        "east": RouteOption(
            name="east",
            distance_m=4200.0,
            terrain_max_elevation_m=2820.0,
            description="east bowl; longer but skirts the ridge",
        ),
        "west": RouteOption(
            name="west",
            distance_m=4500.0,
            terrain_max_elevation_m=2780.0,
            description="west valley; longest but lowest terrain",
        ),
    },
)


def build_fuji_hut_scenario(
    profile_path: Path | str | None = None,
) -> PlayScenario:
    """The bundled flagship play scenario (deterministic, no network)."""

    spec = dict(_FUJI_HUT)
    routes = spec.pop("routes")
    return PlayScenario(
        vehicle=_load_vehicle_limits(profile_path),
        routes=routes,
        **spec,
    )


_SCENARIOS = {
    "fuji-hut": build_fuji_hut_scenario,
}

DEFAULT_SCENARIO_KEY = "fuji-hut"


def load_scenario(
    key: str = DEFAULT_SCENARIO_KEY,
    *,
    profile_path: Path | str | None = None,
) -> PlayScenario:
    try:
        factory = _SCENARIOS[key]
    except KeyError as exc:
        raise KeyError(
            f"unknown scenario '{key}'; choices: {', '.join(_SCENARIOS)}"
        ) from exc
    return factory(profile_path)

"""Deterministic trade-off model for the ``missionos play`` mission-control lab.

This is the heart of the play experience. The upstream digital-twin pipeline
does not vary clearance / energy / wind exposure as a graded response to the
altitude and route knobs, so this module computes those responses explicitly:

- raise altitude  -> terrain clearance improves, but climb energy and wind
  exposure both worsen (so "go higher" is never a free win)
- detour east/west -> longer distance (more cruise energy), but a lower terrain
  ceiling (more clearance head-room)
- heavier payload / stronger wind -> more energy, less return reserve

The model is rule-based and deterministic. MissionOS recommendations are
expressed with the existing :class:`DeliveryRecoveryAction` vocabulary; the LLM
owns judgement, humans own approval, and this layer only *proposes*.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from src.runtime.delivery_recovery_decision import DeliveryRecoveryAction
from src.runtime.missionos_play_scenario import PlayScenario, RouteOption

# Trade-off coefficients. Kept explicit and conservative so the lab is legible
# and testable; they are operator-facing heuristics, not flight-certified models.
_WIND_ALTITUDE_GAIN_PER_KM = 0.18  # +18% effective wind per km of climb gain
_WIND_ENERGY_WH_PER_MPS_PER_KM = 1.4
_PAYLOAD_ENERGY_WH_PER_KG_PER_KM = 1.1
_DEFAULT_MIN_CLEARANCE_RULE_M = 30.0

PlayStatus = Literal["ready", "warning", "blocked"]
WindExposure = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class PlayKnobs:
    """Operator-controlled inputs. Altitude is mean-sea-level flight ceiling."""

    altitude_m: float = 3000.0
    route: str = "direct"
    payload_kg: float = 1.0
    declared_wind_mps: float | None = None
    min_clearance_rule_m: float = _DEFAULT_MIN_CLEARANCE_RULE_M

    def with_(self, **changes: object) -> "PlayKnobs":
        data = {
            "altitude_m": self.altitude_m,
            "route": self.route,
            "payload_kg": self.payload_kg,
            "declared_wind_mps": self.declared_wind_mps,
            "min_clearance_rule_m": self.min_clearance_rule_m,
        }
        data.update(changes)  # type: ignore[arg-type]
        return PlayKnobs(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class PlanEvaluation:
    """A scored plan for one set of knobs against one scenario."""

    knobs: PlayKnobs
    route_name: str
    route_distance_m: float
    terrain_max_elevation_m: float
    # graded outputs
    clearance_m: float
    clearance_margin_m: float  # clearance - rule
    wind_mps: float
    effective_wind_mps: float
    wind_exposure: WindExposure
    climb_energy_wh: float
    cruise_energy_wh: float
    wind_energy_wh: float
    payload_energy_wh: float
    required_round_trip_wh: float
    reserve_floor_wh: float
    return_reserve_wh: float
    battery_reserve_fraction: float
    return_feasible: bool
    status: PlayStatus
    risk_labels: tuple[str, ...]
    recommendation: DeliveryRecoveryAction
    recommendation_reason: str
    recommended_altitude_m: float | None = None


def _distance_km(distance_m: float) -> float:
    return distance_m / 1000.0


def _wind_exposure_label(effective_wind_mps: float, max_wind_mps: float) -> WindExposure:
    ratio = effective_wind_mps / max_wind_mps if max_wind_mps > 0 else math.inf
    if ratio < 0.5:
        return "low"
    if ratio < 0.85:
        return "medium"
    return "high"


def _energy(
    scenario: PlayScenario,
    knobs: PlayKnobs,
    route: RouteOption,
    effective_wind_mps: float,
) -> tuple[float, float, float, float, float]:
    """Return (climb, cruise, wind, payload, round_trip) energy in Wh."""

    vehicle = scenario.vehicle
    distance_km = _distance_km(route.distance_m)
    climb_gain_m = max(0.0, knobs.altitude_m - scenario.takeoff_elevation_m)
    climb_wh = vehicle.climb_energy_wh_per_100m * (climb_gain_m / 100.0)
    cruise_wh = vehicle.cruise_energy_wh_per_km * distance_km
    wind_wh = _WIND_ENERGY_WH_PER_MPS_PER_KM * effective_wind_mps * distance_km
    payload_wh = _PAYLOAD_ENERGY_WH_PER_KG_PER_KM * knobs.payload_kg * distance_km
    # Outbound climbs once; return cruises back with wind but no further climb.
    round_trip_wh = climb_wh + 2.0 * (cruise_wh + wind_wh + payload_wh)
    return climb_wh, cruise_wh, wind_wh, payload_wh, round_trip_wh


def _evaluate_core(scenario: PlayScenario, knobs: PlayKnobs) -> dict[str, object]:
    vehicle = scenario.vehicle
    route = scenario.route(knobs.route)
    wind_mps = (
        scenario.ambient_wind_mps
        if knobs.declared_wind_mps is None
        else float(knobs.declared_wind_mps)
    )
    climb_gain_km = max(0.0, knobs.altitude_m - scenario.takeoff_elevation_m) / 1000.0
    effective_wind_mps = wind_mps * (1.0 + _WIND_ALTITUDE_GAIN_PER_KM * climb_gain_km)

    clearance_m = knobs.altitude_m - route.terrain_max_elevation_m
    clearance_margin_m = clearance_m - knobs.min_clearance_rule_m

    climb_wh, cruise_wh, wind_wh, payload_wh, round_trip_wh = _energy(
        scenario, knobs, route, effective_wind_mps
    )
    reserve_floor_wh = vehicle.nominal_battery_wh * vehicle.reserve_percent / 100.0
    return_reserve_wh = vehicle.nominal_battery_wh - round_trip_wh
    battery_reserve_fraction = max(
        0.0, return_reserve_wh / vehicle.nominal_battery_wh
    )
    return_feasible = return_reserve_wh >= reserve_floor_wh

    return dict(
        route=route,
        wind_mps=wind_mps,
        effective_wind_mps=effective_wind_mps,
        clearance_m=clearance_m,
        clearance_margin_m=clearance_margin_m,
        climb_wh=climb_wh,
        cruise_wh=cruise_wh,
        wind_wh=wind_wh,
        payload_wh=payload_wh,
        round_trip_wh=round_trip_wh,
        reserve_floor_wh=reserve_floor_wh,
        return_reserve_wh=return_reserve_wh,
        battery_reserve_fraction=battery_reserve_fraction,
        return_feasible=return_feasible,
    )


def _minimum_safe_altitude(
    scenario: PlayScenario, knobs: PlayKnobs
) -> float | None:
    """Lowest MSL altitude on the current route that clears the rule *and*
    keeps the return reserve, within the vehicle ceiling. ``None`` if no
    altitude on this route satisfies both."""

    route = scenario.route(knobs.route)
    floor = math.ceil(route.terrain_max_elevation_m + knobs.min_clearance_rule_m)
    ceiling = int(scenario.vehicle.max_takeoff_altitude_m)
    for altitude in range(floor, ceiling + 1):
        probe = _evaluate_core(scenario, knobs.with_(altitude_m=float(altitude)))
        if probe["clearance_margin_m"] >= 0 and probe["return_feasible"]:
            return float(altitude)
    return None


def _classify(core: dict[str, object], scenario: PlayScenario, knobs: PlayKnobs) -> tuple[
    PlayStatus, tuple[str, ...]
]:
    vehicle = scenario.vehicle
    labels: list[str] = []
    blocked = False
    warning = False

    if knobs.altitude_m > vehicle.max_takeoff_altitude_m:
        labels.append("altitude_above_vehicle_ceiling")
        blocked = True
    if core["clearance_m"] < 0:
        labels.append("terrain_clearance_negative")
        blocked = True
    elif core["clearance_margin_m"] < 0:
        labels.append("terrain_clearance_below_minimum")
        warning = True
    if core["effective_wind_mps"] > vehicle.max_wind_speed_mps:
        labels.append("wind_above_recovery_limit")
        blocked = True
    elif core["wind_exposure"] == "high":
        labels.append("wind_exposure_high")
        warning = True
    if not core["return_feasible"]:
        labels.append("return_reserve_below_floor")
        blocked = True
    if knobs.payload_kg > vehicle.max_payload_kg:
        labels.append("payload_above_vehicle_limit")
        blocked = True

    if blocked:
        return "blocked", tuple(labels)
    if warning:
        return "warning", tuple(labels)
    return "ready", tuple(labels)


def _recommend(
    core: dict[str, object],
    scenario: PlayScenario,
    knobs: PlayKnobs,
) -> tuple[DeliveryRecoveryAction, str, float | None]:
    vehicle = scenario.vehicle

    if knobs.altitude_m > vehicle.max_takeoff_altitude_m:
        return (
            DeliveryRecoveryAction.OPERATOR_ESCALATION_REQUIRED,
            f"Altitude {knobs.altitude_m:.0f} m exceeds the {vehicle.max_takeoff_altitude_m:.0f} m "
            "vehicle ceiling; lower the plan or escalate.",
            None,
        )
    if knobs.payload_kg > vehicle.max_payload_kg:
        return (
            DeliveryRecoveryAction.OPERATOR_ESCALATION_REQUIRED,
            f"Payload {knobs.payload_kg:.1f} kg exceeds the {vehicle.max_payload_kg:.1f} kg limit.",
            None,
        )
    if core["effective_wind_mps"] > vehicle.max_wind_speed_mps:
        return (
            DeliveryRecoveryAction.RETURN_TO_HOME_RECOMMENDED,
            f"Effective wind {core['effective_wind_mps']:.1f} m/s is over the "
            f"{vehicle.max_wind_speed_mps:.0f} m/s limit; return to home rather than push on.",
            None,
        )

    safe_altitude = _minimum_safe_altitude(scenario, knobs)

    if core["clearance_margin_m"] < 0:
        if safe_altitude is not None:
            return (
                DeliveryRecoveryAction.CONTINUE,
                f"Raise altitude to {safe_altitude:.0f} m to clear the "
                f"{knobs.min_clearance_rule_m:.0f} m terrain margin while keeping the "
                "return reserve.",
                safe_altitude,
            )
        # No altitude on this route clears the margin within battery reserve:
        # propose a lower-terrain detour.
        return (
            DeliveryRecoveryAction.REROUTE_PROPOSAL,
            "No altitude on this route clears the margin within battery reserve; "
            "propose a lower-terrain detour (route east/west).",
            None,
        )

    if not core["return_feasible"]:
        return (
            DeliveryRecoveryAction.RETURN_TO_HOME_RECOMMENDED,
            "Return reserve is below the floor; return to home or shorten the route.",
            None,
        )

    # Plan is safe. If a strictly lower altitude also clears the rule and keeps
    # reserve, recommend trimming to save battery and wind exposure.
    if safe_altitude is not None and safe_altitude < knobs.altitude_m - 1.0:
        return (
            DeliveryRecoveryAction.CONTINUE,
            f"Use {safe_altitude:.0f} m, not {knobs.altitude_m:.0f} m: it clears the "
            f"{knobs.min_clearance_rule_m:.0f} m margin while preserving more return reserve.",
            safe_altitude,
        )
    return (
        DeliveryRecoveryAction.CONTINUE,
        "Plan clears the margin and preserves return reserve; safe to propose for approval.",
        None,
    )


def evaluate_plan(scenario: PlayScenario, knobs: PlayKnobs) -> PlanEvaluation:
    """Score one plan deterministically against the scenario."""

    core = _evaluate_core(scenario, knobs)
    core["wind_exposure"] = _wind_exposure_label(
        float(core["effective_wind_mps"]), scenario.vehicle.max_wind_speed_mps
    )
    status, risk_labels = _classify(core, scenario, knobs)
    recommendation, reason, recommended_altitude = _recommend(
        core, scenario, knobs
    )
    route: RouteOption = core["route"]  # type: ignore[assignment]
    return PlanEvaluation(
        knobs=knobs,
        route_name=route.name,
        route_distance_m=route.distance_m,
        terrain_max_elevation_m=route.terrain_max_elevation_m,
        clearance_m=float(core["clearance_m"]),
        clearance_margin_m=float(core["clearance_margin_m"]),
        wind_mps=float(core["wind_mps"]),
        effective_wind_mps=float(core["effective_wind_mps"]),
        wind_exposure=core["wind_exposure"],  # type: ignore[arg-type]
        climb_energy_wh=float(core["climb_wh"]),
        cruise_energy_wh=float(core["cruise_wh"]),
        wind_energy_wh=float(core["wind_wh"]),
        payload_energy_wh=float(core["payload_wh"]),
        required_round_trip_wh=float(core["round_trip_wh"]),
        reserve_floor_wh=float(core["reserve_floor_wh"]),
        return_reserve_wh=float(core["return_reserve_wh"]),
        battery_reserve_fraction=float(core["battery_reserve_fraction"]),
        return_feasible=bool(core["return_feasible"]),
        status=status,
        risk_labels=risk_labels,
        recommendation=recommendation,
        recommendation_reason=reason,
        recommended_altitude_m=recommended_altitude,
    )


@dataclass(frozen=True)
class PlanDelta:
    """Signed deltas of B relative to A, for the compare view."""

    clearance_m: float
    return_reserve_wh: float
    effective_wind_mps: float
    route_distance_m: float


def compare_plans(plan_a: PlanEvaluation, plan_b: PlanEvaluation) -> PlanDelta:
    return PlanDelta(
        clearance_m=plan_b.clearance_m - plan_a.clearance_m,
        return_reserve_wh=plan_b.return_reserve_wh - plan_a.return_reserve_wh,
        effective_wind_mps=plan_b.effective_wind_mps - plan_a.effective_wind_mps,
        route_distance_m=plan_b.route_distance_m - plan_a.route_distance_m,
    )

"""Separate, opt-in state-based insertion through the shared physical mission.

Only the measured post-shutdown state can open deployment/return scheduling.
This composition adds no plant, saved-state injection, AI or qualification.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

from . import starship_physics as env, starship_sixdof as dyn
from .starship_ascent import (AscentController, AscentControlOptions,
    AscentCoastOrbitObjectives, AscentScreenSettings, AscentShutdown, StateInsertionOptions)
from .starship_experimental_continuation import development_settings
from .starship_retained_return import TRIMMED_POLICY_ID
from .starship_return_sites import ReturnSites, validate_return_site
from .starship_splashdown import SplashdownGoal


@dataclass(frozen=True)
class ExperimentalStateMissionAscentSettings:
    """Two explicit v7 configurations, separate from the legacy v5 API."""

    ascent: AscentScreenSettings

    def __post_init__(self):
        outages = ((), (AscentShutdown("booster_main_32", 76.), AscentShutdown("ship_main_3", 437.)))
        expected = tuple(AscentScreenSettings(policy="state_insertion_v7", shutdowns=rows,
            objectives=AscentCoastOrbitObjectives(-20000., 300000., 275000., 220000., 50000.),
            control_options=AscentControlOptions(True, False), insertion_options=StateInsertionOptions())
            for rows in outages)
        if type(self.ascent) is not AscentScreenSettings or self.ascent not in expected:
            raise ValueError("state_mission_requires_frozen_v7_configuration")

    def validate_profile(self, profile):
        AscentController(self.ascent).validate_profile(profile)
        if (profile["guidance"]["target_perigee_m"] != 220000.
                or profile["guidance"]["orbit_release_delay_s"] != 45.
                or profile["guidance"]["coast_before_return_s"] != 1800.
                or profile["payload"]["count"] != 26
                or profile["ship"]["ascent_reserve_kg"] != 75000.
                or profile["ship"]["return_reserve_kg"] != 28000.):
            raise ValueError("state_mission_profile_changes_frozen_gates_or_schedule")

    def record(self):
        return {"schema": "missionos.experimental_state_mission_ascent_settings.v1",
            "ascent": self.ascent.record(), "engineering_objectives_not_real_flight_identification": True,
            "qualified_return": False, "mission_authority_granted": False}


def validate_state_physical_mission(profile, settings, duration_s, return_sites, splashdown_goal):
    """Admission is experimental scope validation, never an execution grant."""
    if (type(settings) is not ExperimentalStateMissionAscentSettings
            or type(return_sites) is not ReturnSites or type(splashdown_goal) is not SplashdownGoal
            or type(duration_s) not in (int, float) or not math.isfinite(duration_s)
            or not 3336. < duration_s <= min(7500., profile["integration"]["max_duration_s"])):
        raise ValueError("invalid_experimental_state_physical_mission_scope")
    settings.validate_profile(profile)
    validate_return_site(return_sites.divert, profile, terminal_goal="model_surface_contact")
    splashdown_goal.validate_site(return_sites.divert)


def settled_insertion_measurement(state, craft, profile, settings):
    """Read one actual state; do not propagate, zero actuators or assign orbit."""
    if type(state) is not dyn.State6DOF or type(settings) is not ExperimentalStateMissionAscentSettings:
        raise ValueError("state_mission_requires_typed_settled_state")
    settings.validate_profile(profile)
    orbit = env.orbital_elements(env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg))
    ship_outages = tuple(row.engine_name for row in settings.ascent.shutdowns if row.engine_name.startswith("ship_"))
    names = [engine.name for engine in craft.engines]
    passed = bool(orbit.get("status") == "bound" and orbit["perigee_altitude_m"] >= 220000.
        and orbit["apogee_altitude_m"] <= 600000. and state.propellant_kg >= 75000.
        and env.norm(state.omega_body_rad_s) <= 5.
        and len(state.engine_states) >= 3 and all(row.available for row in state.engine_states[:3])
        and all(name in names and not state.engine_states[names.index(name)].available for name in ship_outages))
    return {"time_s": state.time_s, "state": asdict(state), "vehicle": asdict(craft),
        "orbit": orbit, "gate_reached": passed}


def state_physical_mission(profile, settings, *, return_sites, splashdown_goal,
                           duration_s=7500., on_tick=None, booster_outage_control=None,
                           booster_energy_control=None, booster_terminal_recovery=None):
    """Use actual concurrent launch/separation and the shared deployment/return.

    Invoking this integrates the plant and requires a separately registered
    trial. The immutable tick observer has no command or model interface.
    """
    validate_state_physical_mission(profile, settings, duration_s, return_sites, splashdown_goal)
    if on_tick is not None and not callable(on_tick):
        raise ValueError("invalid_experimental_state_physical_tick_observer")
    if booster_outage_control is not None:
        from .starship_booster_outage_control import validate_outage_scope
        validate_outage_scope(profile, booster_outage_control, return_sites, splashdown_goal)
    if booster_energy_control is not None:
        from .starship_booster_energy_control import validate_energy_scope
        if booster_outage_control is not None:
            raise ValueError("energy_control_cannot_mix_outage_control")
        validate_energy_scope(profile, booster_energy_control, return_sites, splashdown_goal)
    if booster_terminal_recovery is not None:
        from .starship_booster_terminal_control import validate_terminal_scope
        if booster_outage_control is not None or booster_energy_control is not None:
            raise ValueError("terminal_recovery_cannot_mix_other_booster_control")
        validate_terminal_scope(profile, booster_terminal_recovery, return_sites, splashdown_goal)
    peak, peak_altitude = None, -math.inf

    def observed_tick(tick):
        nonlocal peak, peak_altitude
        if tick.body_id == "booster":
            altitude = env.ecef_to_geodetic(tick.state.r_eci_m)[2]
            if altitude > peak_altitude:
                peak, peak_altitude = asdict(tick.state), altitude
        if on_tick is not None:
            on_tick(tick)

    from .starship_sixdof_mission import simulate_concurrent
    result = simulate_concurrent(profile, scenario="launch", duration_s=duration_s,
        return_policy=TRIMMED_POLICY_ID, booster_policy="fixed_v1",
        _development_return_qualification=development_settings(),
        return_sites=return_sites, splashdown_goal=splashdown_goal,
        experimental_ascent=settings, on_tick=observed_tick,
        **({"booster_outage_control": booster_outage_control} if booster_outage_control is not None else {}),
        **({"booster_energy_control": booster_energy_control} if booster_energy_control is not None else {}),
        **({"booster_terminal_recovery": booster_terminal_recovery} if booster_terminal_recovery is not None else {}))
    booster = result.get("booster_run")
    if booster is not None:
        final = booster["final_state"]
        if env.ecef_to_geodetic(tuple(final["r_eci_m"]))[2] > peak_altitude:
            peak = final
        booster["runner_booster_peak_witness"] = {"state": peak,
            "origin": "all_yielded_physics_ticks_and_final_state; no extra plant step",
            "not_independent_physics_reexecution": True}
    return result

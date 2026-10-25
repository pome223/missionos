"""Experimental physical composition through the existing concurrent mission.

No state stitching, alternate integrator, AI/trouble hooks or qualification is
introduced here. The actual final insertion gate schedules the shared payload
and Ship return loop; Booster starts concurrently at the actual separation.
"""
from __future__ import annotations

from dataclasses import asdict
import math

from .starship_ascent import ExperimentalMissionAscentSettings
from .starship_experimental_continuation import development_settings
from .starship_retained_return import TRIMMED_POLICY_ID
from .starship_return_sites import ReturnSites, validate_return_site
from .starship_splashdown import SplashdownGoal
from . import starship_physics as env, starship_sixdof as dyn


def validate_physical_mission(profile, settings, duration_s, return_sites, splashdown_goal):
    """Admission to an experimental code path, never an execution grant."""
    if (type(settings) is not ExperimentalMissionAscentSettings
            or type(return_sites) is not ReturnSites or type(splashdown_goal) is not SplashdownGoal
            or type(duration_s) not in (int, float) or not math.isfinite(duration_s)
            or not 3336. < duration_s <= min(7500., profile["integration"]["max_duration_s"])):
        raise ValueError("invalid_experimental_physical_mission_scope")
    settings.validate_profile(profile)
    validate_return_site(return_sites.divert, profile, terminal_goal="model_surface_contact")
    splashdown_goal.validate_site(return_sites.divert)


def schedule_from_actual_insertion(state, profile, settings):
    """Read the actual state; compute a schedule without assigning any state."""
    if type(state) is not dyn.State6DOF or type(settings) is not ExperimentalMissionAscentSettings:
        raise ValueError("experimental_schedule_requires_typed_actual_state")
    settings.validate_profile(profile)
    orbit = env.orbital_elements(env.State3D(state.time_s, state.r_eci_m, state.v_eci_mps, state.propellant_kg))
    if orbit.get("status") != "bound" or orbit["perigee_altitude_m"] < profile["guidance"]["target_perigee_m"]:
        raise ValueError("experimental_schedule_requires_actual_final_orbit_gate")
    return {"final_gate_time_s": state.time_s,
        "first_release_time_s": state.time_s+profile["guidance"]["orbit_release_delay_s"],
        "return_request_time_s": state.time_s+profile["guidance"]["coast_before_return_s"]}


def experimental_physical_mission(profile, settings, *, return_sites, splashdown_goal,
                                  duration_s=7500., on_tick=None):
    """Run the existing launch/separation/Booster/deployment/return boundary.

    Calling this integrates the plant and requires a separately registered
    trial. The optional immutable-tick observer grants no actuation authority.
    There are no hooks here for a model, director, image or fault engine.
    """
    validate_physical_mission(profile, settings, duration_s, return_sites, splashdown_goal)
    if on_tick is not None and not callable(on_tick):
        raise ValueError("invalid_experimental_physical_tick_observer")
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
        experimental_ascent=settings, on_tick=observed_tick)
    booster = result.get("booster_run")
    if booster is not None:
        final = booster["final_state"]
        if env.ecef_to_geodetic(tuple(final["r_eci_m"]))[2] > peak_altitude:
            peak = final
        booster["runner_booster_peak_witness"] = {"state": peak,
            "origin": "all_yielded_physics_ticks_and_final_state; no extra plant step",
            "not_independent_physics_reexecution": True}
    return result

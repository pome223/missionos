"""Explicit availability-aware water-return control using existing actuators.

No vehicle parameter, contact limit, plant integrator or achieved state changes.
The policy composes existing finite-control mechanisms, not a flight model.
"""
from dataclasses import asdict, dataclass
import math

from . import starship_physics as env
from .starship_return_sites import ReturnSites, validate_return_site
from .starship_splashdown import SplashdownGoal

POLICY_ID = "availability_water_return_v1"


@dataclass(frozen=True)
class BoosterOutageControlSettings:
    """One fixed composition; no trajectory-fitting or authority scalars."""

    def record(self):
        return {"schema": "missionos.booster_outage_control_settings.v1", "policy_id": POLICY_ID}


def validate_outage_scope(profile, settings, return_sites, splashdown_goal):
    if (type(settings) is not BoosterOutageControlSettings or type(return_sites) is not ReturnSites
            or type(splashdown_goal) is not SplashdownGoal):
        raise ValueError("outage_control_requires_exact_settings_and_bound_water_goal")
    validate_return_site(return_sites.divert, profile, terminal_goal="model_surface_contact")
    splashdown_goal.validate_site(return_sites.divert)


def outage_control_command(state, craft, target, throttle, count, profile, *, settings,
                           phase, observed, control_interval_s):
    """Request bounded controls from actual loads; never change the state."""
    if type(settings) is not BoosterOutageControlSettings:
        raise ValueError("outage_control_requires_exact_settings")
    from .starship_booster_control import control_coast_stopping_distance, control_with_measured_tvc
    tvc_capacity = float(sum(load["thrust_n"]*abs(engine.position_body_m[2]-observed["com_body_m"][2])
        *math.sin(engine.max_gimbal_rad) for engine, actual, load in
        zip(craft.engines, state.engine_states, observed["engine_loads"])
        if not engine.name.startswith("rcs_") and actual.available and engine.max_gimbal_rad > 0.))
    rcs_capacity = float(2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"])
    tail_tvc = bool(phase == "booster_entry_coast" and tvc_capacity > rcs_capacity)
    if tail_tvc:
        # Main enable/throttle stay OFF. Current finite spool can still create
        # a large moment and supply TVC authority; do not discard it early.
        command, diagnostic = control_with_measured_tvc(state, craft, target, throttle, count, profile,
            use_flaps=True, development_fin_allocation=True, control_interval_s=control_interval_s,
            recenter_uncommanded_main=False)
        mode = "measured_tvc_shutdown_tail"
    elif phase == "booster_entry_coast" and observed["dynamic_pressure_pa"] <= 100.:
        command, diagnostic = control_coast_stopping_distance(state, craft, target, profile,
            control_interval_s=control_interval_s, recenter_uncommanded_main=True)
        mode = "jet_stopping_distance_low_q"
    else:
        command, diagnostic = control_with_measured_tvc(state, craft, target, throttle, count, profile,
            use_flaps=phase in ("booster_entry_coast", "booster_landing_burn"),
            development_fin_allocation=True, control_interval_s=control_interval_s,
            recenter_uncommanded_main=True)
        mode = ("finite_fins_and_measured_tvc" if phase == "booster_entry_coast"
                else "powered_slew_or_braking_measured_tvc")
    diagnostic["booster_outage_control"] = {"policy_id": POLICY_ID, "mode": mode,
        "measured_finite_main_loads": True, "recenter_uncommanded_main": not tail_tvc,
        "current_gimballed_main_torque_capacity_nm": tvc_capacity,
        "physical_rcs_pair_torque_nm": rcs_capacity, "shutdown_tail_tvc_required": tail_tvc,
        "shutdown_tail_tvc_criterion": "actual_gimballed_main_capacity_exceeds_physical_rcs_pair",
        "physics_limits_changed": False, "actual_state_assigned": False}
    return command, diagnostic


def outage_physical_booster(profile, separation_state, settings, *, return_sites,
                             splashdown_goal, duration_s=1200., on_tick=None):
    """Bounded isolated saved-separation trial; the caller must register it."""
    validate_outage_scope(profile, settings, return_sites, splashdown_goal)
    if (type(duration_s) not in (int, float) or not math.isfinite(duration_s) or not 0 < duration_s <= 2000.
            or on_tick is not None and not callable(on_tick)):
        raise ValueError("invalid_outage_control_trial_scope")
    from .starship_sixdof_booster import _simulate_booster_steps
    iterator = _simulate_booster_steps(profile, separation_state, duration_s=duration_s,
        guidance_policy=POLICY_ID, outage_control_settings=settings,
        return_sites=return_sites, splashdown_goal=splashdown_goal)
    peak, highest = None, -math.inf
    try:
        while True:
            try:
                tick = next(iterator)
            except StopIteration as complete:
                result = complete.value
                break
            altitude = env.ecef_to_geodetic(tick.state.r_eci_m)[2]
            if altitude > highest:
                peak, highest = asdict(tick.state), altitude
            if on_tick is not None:
                on_tick(tick)
    finally:
        iterator.close()
    if env.ecef_to_geodetic(tuple(result["final_state"]["r_eci_m"]))[2] > highest:
        peak = result["final_state"]
    result["runner_booster_peak_witness"] = {"state": peak,
        "origin": "all_yielded_physics_ticks_and_final_state; no extra plant step",
        "not_independent_physics_reexecution": True}
    return result

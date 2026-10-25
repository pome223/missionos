"""Explicit finite-actuator adapter for the shared Booster energy requests.

The kernel chooses requests.  This adapter only reads actual navigation and
actuator facts and submits bounded commands to the existing six-DOF plant.
It contains no alternate integrator, state projection, hosted model or grant.
"""
from __future__ import annotations

from dataclasses import asdict
import math

import numpy as np

from . import starship_physics as env, starship_sixdof as dyn
from .starship_booster_energy_guidance import (
    POLICY_ID, BoosterEnergyControlSettings, GuidanceMemory, GuidanceObservation,
    MainCapability, advance_guidance, attitude_pd, entry_axis,
)
from .starship_return_sites import ReturnSites, validate_return_site
from .starship_splashdown import SplashdownGoal


def validate_energy_scope(profile, settings, return_sites, splashdown_goal):
    if (type(settings) is not BoosterEnergyControlSettings or type(return_sites) is not ReturnSites
            or type(splashdown_goal) is not SplashdownGoal):
        raise ValueError("energy_control_requires_exact_settings_and_bound_water_goal")
    validate_return_site(return_sites.divert, profile, terminal_goal="model_surface_contact")
    splashdown_goal.validate_site(return_sites.divert)
    if profile["booster"]["engine_count"] != 33:
        raise ValueError("energy_control_requires_exact_33_index_main_inventory")


def energy_control_command(state, craft, target_q, throttle, count, profile, *, settings,
        phase, observed, control_interval_s, trim_angles_rad=None):
    """Shared static low-level request for point screening and actual control.

    No achieved angular acceleration is prescribed here.  The point model may
    use the diagnostic PD request as its explicitly idealized angular response;
    actual execution must still integrate the returned finite actuator command.
    """
    if type(settings) is not BoosterEnergyControlSettings:
        raise ValueError("energy_control_requires_exact_settings")
    if state.propellant_kg <= 0:
        inverse = (state.q_body_to_eci[0], *(-x for x in state.q_body_to_eci[1:]))
        error = dyn.quaternion_multiply(inverse, target_q)
        angle = 2*math.atan2(math.hypot(*error[1:]), abs(error[0]))
        return dyn.Command6DOF(tuple(dyn.EngineCommand() for _ in craft.engines),
            tuple(state.flap_angles_rad)), {"target_q_body_to_eci": list(target_q),
                "attitude_error_deg": math.degrees(angle),
                "booster_energy_control": {"policy_id": POLICY_ID, "settings": settings.record(),
                    "mode": "fuel_depleted_uncontrolled_coast", "actual_uncontrolled": True,
                    "physics_limits_changed": False, "actual_state_assigned": False}}
    from .starship_booster_control import control_with_measured_tvc
    from .starship_booster_finite_authority import finite_coast_control_profile
    from .starship_fin_allocation import POLICY_ID as FIN_POLICY
    capacity = float(sum(load["thrust_n"]*abs(engine.position_body_m[2]-observed["com_body_m"][2])
        *math.sin(engine.max_gimbal_rad) for engine, actual, load in
        zip(craft.engines, state.engine_states, observed["engine_loads"])
        if not engine.name.startswith("rcs_") and actual.available and engine.max_gimbal_rad > 0))
    jets = float(2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"])
    tail = bool(phase == "booster_entry_coast" and capacity > jets)
    mapped, receipt = profile, None
    if phase == "booster_entry_coast":
        mapped, receipt = finite_coast_control_profile(state, craft, target_q, profile,
            control_interval_s=control_interval_s, observed=observed,
            trim_angles_rad=trim_angles_rad, fin_policy=FIN_POLICY)
        mode = ("measured_tvc_shutdown_tail" if tail else "finite_fin_authority_pd"
            if receipt["additional_fin_authority_admitted"] else "jet_limited_pd_low_q"
            if observed["dynamic_pressure_pa"] <= 100. else "finite_fins_and_measured_tvc")
    else:
        mode = "powered_slew_or_braking_measured_tvc"
    command, diagnostic = control_with_measured_tvc(state, craft, target_q, throttle, count, mapped,
        use_flaps=phase in ("booster_boostback_settle", "booster_entry_coast",
            "booster_landing_align", "booster_landing_burn"),
        development_fin_allocation=True, control_interval_s=control_interval_s,
        trim_angles_rad=trim_angles_rad, development_fin_policy=FIN_POLICY,
        recenter_uncommanded_main=not tail)
    inverse = (state.q_body_to_eci[0], *(-x for x in state.q_body_to_eci[1:]))
    error = dyn.quaternion_multiply(inverse, target_q)
    g = mapped["guidance"]
    diagnostic["energy_attitude_pd"] = {
        "frequency_rad_s": float(g["attitude_frequency_rad_s"]),
        "damping_ratio": float(g["attitude_damping_ratio"]),
        "alpha_limit_rad_s2": float(g["max_angular_acceleration_rad_s2"]),
        "requested_acceleration_body_rad_s2": list(attitude_pd(error, state.omega_body_rad_s,
            frequency_rad_s=g["attitude_frequency_rad_s"], damping_ratio=g["attitude_damping_ratio"],
            alpha_rad_s2=g["max_angular_acceleration_rad_s2"]))}
    diagnostic["booster_energy_control"] = {"policy_id": POLICY_ID,
        "settings": settings.record(), "mode": mode,
        "measured_finite_main_loads": True, "recenter_uncommanded_main": not tail,
        "current_gimballed_main_torque_capacity_nm": capacity,
        "physical_rcs_pair_torque_nm": jets, "shutdown_tail_tvc_required": tail,
        "shutdown_tail_tvc_criterion": "actual_gimballed_main_capacity_exceeds_physical_rcs_pair",
        "physics_limits_changed": False, "actual_state_assigned": False}
    if receipt is not None:
        diagnostic["finite_coast_authority"] = receipt
        diagnostic.update(coast_control_frequency_rad_s=receipt["coast_control_frequency_rad_s"],
            coast_angular_acceleration_limit_rad_s2=receipt["coast_angular_acceleration_limit_rad_s2"])
    return command, diagnostic


class EnergyControlRuntime:
    """Memory contains only guidance history, never a replacement plant state."""

    def __init__(self, settings, profile, return_site, splashdown_goal, origin_time_s):
        if type(settings) is not BoosterEnergyControlSettings or type(splashdown_goal) is not SplashdownGoal:
            raise ValueError("energy_control_requires_exact_settings")
        validate_return_site(return_site, profile, terminal_goal="model_surface_contact")
        splashdown_goal.validate_site(return_site)
        self.settings, self.profile, self.site, self.goal = settings, profile, return_site, splashdown_goal
        self.memory = GuidanceMemory(float(origin_time_s))
        self.previous_entry_axis = None

    def request(self, state, craft, observed):
        from .starship_sixdof_booster import _navigation
        from .starship_sixdof_mission import _attitude
        from .starship_sixdof_contact import hull_clearance
        from .starship_booster_recovery import _trim_entry_candidate, finite_landing_preview
        up, east, north, velocity, displacement, _ = _navigation(state, self.profile, return_site=self.site)
        axes = (east, north, up)
        position = (-env.dot(displacement, east), -env.dot(displacement, north), observed["altitude_m"])
        local_velocity = tuple(env.dot(velocity, axis) for axis in axes)
        body_axis = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
        local_axis = tuple(env.dot(body_axis, axis) for axis in axes)
        body_x = dyn.rotate(state.q_body_to_eci, (1., 0., 0.))
        local_x = tuple(env.dot(body_x, axis) for axis in axes)
        body_flow = observed["air_velocity_body_mps"]
        alpha = (math.degrees(math.acos(max(-1., min(1., -body_flow[2]/env.norm(body_flow)))))
            if env.norm(body_flow) > 0 else 0.)
        clearance = hull_clearance(state, craft, self.profile["geometry"]["booster_length_m"],
            self.profile["geometry"]["radius_m"])["signed_clearance_m"]
        mains = tuple(MainCapability(float(engine.max_thrust_n), float(engine.min_throttle), bool(actual.available))
            for engine, actual in zip(craft.engines[:33], state.engine_states[:33]))
        if [engine.name for engine in craft.engines[:33]] != [f"booster_main_{i}" for i in range(33)]:
            raise ValueError("energy_control_main_indices_or_identity_changed")
        observation = GuidanceObservation(float(state.time_s), tuple(float(x) for x in position),
            tuple(float(x) for x in local_velocity), float(clearance), float(observed["mass_kg"]),
            float(state.propellant_kg), float(env.EARTH_MU_M3_S2/env.norm(state.r_eci_m)**2),
            tuple(float(x) for x in local_axis), float(env.norm(state.omega_body_rad_s)),
            float(observed["dynamic_pressure_pa"]), float(alpha),
            float(np.max(np.sum(np.abs(observed["inertia_kg_m2"]), axis=1))),
            float(abs(craft.engines[0].position_body_m[2]-observed["com_body_m"][2])), mains)
        jet_capacity = 2*self.profile["actuators"]["rcs_radius_m"]*self.profile["actuators"]["rcs_thrust_n"]
        entry, entry_receipt = entry_axis(observation, craft.aero_panels,
            isp_s=self.profile["booster"]["engine_isp_s"], com_body_m=observed["com_body_m"],
            rcs_pair_torque_nm=jet_capacity, minimum_propellant_kg=self.goal.minimum_propellant_kg,
            previous_axis=self.previous_entry_axis,
            **({"prepare_descent": True} if self.settings.handoff_main_count else {}))
        entry_error = None
        if self.settings.handoff_main_count:
            entry_eci = tuple(sum(entry[j]*axes[j][i] for j in range(3)) for i in range(3))
            entry_target = _attitude(entry_eci, north)
            inverse = (state.q_body_to_eci[0], *(-x for x in state.q_body_to_eci[1:]))
            error = dyn.quaternion_multiply(inverse, entry_target)
            entry_error = math.degrees(2*math.atan2(math.hypot(*error[1:]), abs(error[0])))
        # Future target trim assumes the declared North frame.  Current trim
        # instead uses actual body-X, preserving full integrated roll lag.
        _, actual_trim = _trim_entry_candidate(local_velocity, local_axis,
            observed["atmosphere"]["density_kg_m3"], craft.aero_panels,
            com_body_m=observed["com_body_m"], jet_torque_nm=jet_capacity,
            minimum_trim_dynamic_pressure_pa=20000., reference_axis_enu=local_x)
        trim = tuple(float(x) for x in actual_trim["predicted_trim_flap_angles_rad"])
        speed = env.norm(velocity)
        force_eci = dyn.rotate(state.q_body_to_eci, observed["aero_force_body_n"])
        drag = max(0., -env.dot(force_eci, velocity)/max(speed, 1e-30))
        rho = observed["atmosphere"]["density_kg_m3"]
        axial_area = math.pi*self.profile["geometry"]["radius_m"]**2*self.profile["aero"]["axial_cd"]
        measured_area = 2*drag/(rho*speed*speed) if rho > 1e-12 and speed > 10 else axial_area
        engine_facts = tuple({"max_thrust_n": float(engine.max_thrust_n), "isp_s": float(engine.isp_s),
            "throttle_time_constant_s": float(engine.throttle_time_constant_s),
            "throttle_rate_per_s": float(engine.throttle_rate_per_s), "throttle": float(actual.throttle),
            "available": bool(actual.available), "is_main": not engine.name.startswith("rcs_")}
            for engine, actual in zip(craft.engines, state.engine_states))
        preview, preview_receipt = finite_landing_preview(observation, engine_facts,
            axial_drag_area_m2=axial_area, current_drag_area_m2=measured_area,
            upright_support_m=float(observed["com_body_m"][2]), minimum_propellant_kg=self.goal.minimum_propellant_kg)
        before = self.memory
        request = advance_guidance(observation, before, self.settings,
            actuators=self.profile["actuators"], guidance=self.profile["guidance"],
            entry_axis_enu=entry, entry_angle_deg=float(entry_receipt["entry_angle_deg"]), preview=preview,
            minimum_propellant_kg=self.goal.minimum_propellant_kg, area_radius_m=self.goal.area_radius_m,
            powered_dt_s=self.profile["integration"]["powered_dt_s"], terminal_variant="signed_energy",
            **({"entry_attitude_error_deg": entry_error} if self.settings.handoff_main_count else {}))
        self.memory = request.memory
        if request.memory.phase in ("booster_boostback_settle", "booster_entry_coast"):
            self.previous_entry_axis = tuple(entry)
        axis_eci = tuple(sum(request.target_axis_enu[j]*axes[j][i] for j in range(3)) for i in range(3))
        target = _attitude(axis_eci, north)
        receipt = {"memory_before": asdict(before), "memory_after": asdict(request.memory),
            "observation": asdict(observation), "request": {"target_axis_enu": list(request.target_axis_enu),
                "main_prefix_count": request.main_prefix_count, "main_throttle": request.main_throttle},
            "diagnostics": request.diagnostics,
            "entry": {"axis_enu": list(entry), "angle_deg": float(entry_receipt["entry_angle_deg"]), **entry_receipt,
                **({"actual_attitude_error_deg": entry_error} if self.settings.handoff_main_count else {})},
            "preview": asdict(preview), "preview_receipt": preview_receipt,
            "actual_trim": {"reference_axis_enu": list(local_x), **actual_trim}}
        return request, target, trim, receipt


def energy_physical_booster(profile, separation_state, settings, *, return_sites,
        splashdown_goal, duration_s=1200., on_tick=None):
    """Explicit isolated suffix entrypoint; invocation requires registration."""
    validate_energy_scope(profile, settings, return_sites, splashdown_goal)
    if (type(duration_s) not in (int, float) or not math.isfinite(duration_s)
            or not 0 < duration_s <= 1200. or on_tick is not None and not callable(on_tick)):
        raise ValueError("invalid_energy_control_trial_scope")
    from .starship_sixdof_booster import _simulate_booster_steps
    iterator = _simulate_booster_steps(profile, separation_state, duration_s=duration_s,
        guidance_policy=POLICY_ID, energy_control_settings=settings,
        return_sites=return_sites, splashdown_goal=splashdown_goal)
    peak, highest = None, -math.inf
    try:
        while True:
            try:
                tick = next(iterator)
            except StopIteration as done:
                result = done.value
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

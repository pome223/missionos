"""Separated-booster six-DOF return with exposed development guidance.

The inherited separation state is never projected onto a return trajectory.
Body forces come only from the common finite engines, gimbals, jets and panels.
This is not SpaceX flight software, a catch controller, or a validated airload
model. A missed return, fuel exhaustion or impact is retained as an outcome.
"""
from __future__ import annotations

from dataclasses import asdict
import math

from . import starship_physics as env
from . import starship_sixdof as dyn

DEFAULTS = {
    "max_duration_s": 1200., "boostback_max_slew_s": 90., "boostback_max_burn_s": 70.,
    "boostback_engine_count": 33, "boostback_slew_throttle": .4,
    "boostback_alignment_deg": 20., "boostback_velocity_tolerance_mps": 30.,
    "boostback_velocity_tau_s": 25., "boostback_max_horizontal_speed_mps": 700.,
    "boostback_upward_acceleration_mps2": 2., "landing_reserve_kg": 60_000.,
    "landing_ignition_altitude_m": 2000., "landing_velocity_tau_s": 1.5,
    "landing_horizontal_tau_s": 8., "landing_target_speed_mps": 2., "landing_max_tilt_deg": 15.,
    "landing_ignition_margin_m": 300., "contact_speed_limit_mps": 5., "contact_tilt_limit_deg": 10.,
    "contact_rate_limit_rad_s": .05, "return_site_tolerance_m": 1000.,
}


def _configuration(profile):
    supplied = profile.get("booster_return", {})
    if type(supplied) is not dict or set(supplied)-set(DEFAULTS):
        raise ValueError("unknown_booster_return_configuration")
    resolved = {**DEFAULTS, **supplied}
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
           for value in resolved.values()):
        raise ValueError("invalid_booster_return_configuration")
    if resolved["max_duration_s"] > 2000 or any(resolved[key] >= 90 for key in
            ("boostback_alignment_deg", "landing_max_tilt_deg", "contact_tilt_limit_deg")):
        raise ValueError("invalid_booster_return_configuration")
    if (type(resolved["boostback_engine_count"]) is not int
            or not 3 <= resolved["boostback_engine_count"] <= profile["booster"]["engine_count"]
            or resolved["boostback_slew_throttle"] > 1.):
        raise ValueError("invalid_booster_return_configuration")
    return resolved


def _minus(a, b):
    return env.add(a, env.scale(b, -1))


def _horizontal(vector, up):
    return _minus(vector, env.scale(up, env.dot(vector, up)))


def _cap(vector, maximum):
    length = env.norm(vector)
    return env.scale(vector, min(1., maximum/max(length, 1e-12)))


def _landing_engine_demand(state, booster, required_force_n, maximum_count):
    """Use the smallest available prefix that can supply the requested force.

    The finite actuator still enforces each engine's minimum throttle and spool.
    Counting only available engines preserves inherited failures, including a
    failed central engine. This is development guidance, not SpaceX sequencing.
    """
    available = 0.
    for count in range(1, maximum_count+1):
        engine = booster.engines[count-1]
        if state.engine_states[count-1].available:
            available += engine.max_thrust_n
        if available >= required_force_n:
            break
    floor = max((e.min_throttle for i, e in enumerate(booster.engines[:count])
                 if state.engine_states[i].available), default=0.)
    return count, max(floor, min(1., required_force_n/max(available, 1.)))


def _navigation(state, profile, *, return_site=None):
    from .starship_sixdof_mission import point_state
    point = point_state(state)
    up, east, north = env.local_frame(point)
    if return_site is not None:
        from .starship_return_sites import validate_return_site
        validate_return_site(return_site, profile)
    if return_site is None or return_site.site_id == "capture":
        # Explicit original capture retains the exact legacy arithmetic.
        target = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                                   time_s=state.time_s)
    else:
        target = env.surface_state(return_site.latitude_deg, return_site.longitude_deg,
                                   return_site.elevation_m, time_s=state.time_s)
    relative_velocity = env.air_relative_velocity(point)
    displacement = _horizontal(_minus(target.r, state.r_eci_m), up)
    # Surface great-circle distance is a diagnostic; guidance uses the local
    # projected displacement, an explicitly approximate return targeting law.
    a, b = env.unit(state.r_eci_m), env.unit(target.r)
    distance = env.EARTH_EQUATORIAL_RADIUS_M*math.atan2(env.norm(env.cross(a, b)), env.dot(a, b))
    return up, east, north, relative_velocity, displacement, distance


def _sample_booster(state, vehicle, phase, command=None, diagnostics=None):
    from .starship_sixdof_mission import _sample
    record = _sample(state, vehicle, phase, command, diagnostics)
    record["body_id"] = "booster"
    return record


def _drag_aware_stop_estimate(state, booster, observed, up, vertical, profile, maximum_count):
    """Local 1D burn preview, with measured drag area, density, spool and fuel.

    This is a guidance estimate, not a second truth trajectory. It freezes the
    currently observed axial drag area and assumes aligned thrust. Actual six-
    DOF motion, attitude, allocation and force integration remain unchanged.
    A fuel-infeasible high-altitude burn is not mistaken for a landing window.
    """
    height = observed["altitude_m"]
    speed = max(0., -vertical)
    rho = observed["atmosphere"]["density_kg_m3"]
    drag_up = max(0., env.dot(dyn.rotate(state.q_body_to_eci, observed["aero_force_body_n"]), up))
    area = 2*drag_up/(rho*speed*speed) if rho > 1e-12 and speed > 10 else math.pi*profile["geometry"]["radius_m"]**2*profile["aero"]["axial_cd"]
    selected = [e for i, e in enumerate(booster.engines[:maximum_count]) if state.engine_states[i].available]
    available = sum(e.max_thrust_n for e in selected)
    mass, fuel = observed["mass_kg"], state.propellant_kg
    dry = mass-fuel
    throttle = max((state.engine_states[i].throttle for i in range(maximum_count) if state.engine_states[i].available), default=0.)
    elapsed, feasible, initial_fuel, initial_height = 0., False, fuel, height
    tau = max((e.throttle_time_constant_s for e in selected), default=1.)
    rate = min((e.throttle_rate_per_s for e in selected), default=1.)
    flow_full = sum(e.max_thrust_n/(e.isp_s*env.STANDARD_GRAVITY_MPS2) for e in selected)
    while elapsed < 100. and available > 0 and fuel > 1e-6:
        if speed <= 2.:
            feasible = True
            break
        dt = min(.1, fuel/max(flow_full, 1.))
        throttle = min(1., throttle+min(rate, (1.-throttle)/tau)*dt)
        density = env.standard_atmosphere(max(0., height))["density_kg_m3"]
        gravity = env.EARTH_MU_M3_S2/(env.EARTH_EQUATORIAL_RADIUS_M+max(0., height))**2
        acceleration = available*throttle/(dry+fuel)+.5*density*area*speed*speed/(dry+fuel)-gravity
        after_speed = max(0., speed-acceleration*dt)
        height -= .5*(speed+after_speed)*dt
        fuel = max(0., fuel-flow_full*throttle*dt)
        speed, elapsed = after_speed, elapsed+dt
    termination = ("arrested" if feasible else "fuel_exhausted" if fuel <= 1e-6
                   else "no_available_thrust" if available <= 0 else "horizon_exceeded")
    return {"estimated_stopping_height_m": initial_height-height,
            # Keep the historical numerical field for saved consumers, but
            # explicitly distinguish travelled distance from completed arrest.
            "estimated_distance_before_termination_m": initial_height-height,
            "estimated_stopping_height_is_valid": bool(feasible),
            "estimate_termination": termination, "estimated_final_speed_mps": speed,
            "estimated_burn_time_s": elapsed, "estimated_burn_propellant_kg": initial_fuel-fuel,
            "estimated_drag_area_m2": area, "burn_fuel_feasible": feasible,
            "available_landing_engine_count": len(selected), "available_landing_thrust_n": available,
            "preview_assumption": "aligned_thrust_current_drag_area_spool_density_and_fuel_1d"}


def _coast_control_profile(profile, observed, *, include_spooled_tvc=False):
    """Match coast PD bandwidth to the configured physical jet-pair authority."""
    jets = profile["actuators"]
    # Each signed pitch/yaw channel is two opposite jets, each at radius r.
    torque = 2*jets["rcs_radius_m"]*jets["rcs_thrust_n"]
    if include_spooled_tvc:
        # A cutoff command does not erase thrust. Preserve gimbal authority
        # during actual spool-down rather than prematurely using jets alone.
        for i, load in enumerate(observed["engine_loads"][:profile["booster"]["gimbal_engine_count"]]):
            lever = abs(observed["com_body_m"][2]-profile["booster"]["engine_positions_body_m"][i][2])
            torque += load["thrust_n"]*lever*math.sin(math.radians(jets["max_gimbal_deg"]))
    inertia_bound = max(sum(abs(x) for x in row) for row in observed["inertia_kg_m2"])
    alpha = min(profile["guidance"]["max_angular_acceleration_rad_s2"], torque/inertia_bound)
    frequency = min(profile["guidance"]["attitude_frequency_rad_s"], math.sqrt(alpha/math.pi))
    return {**profile, "guidance": {**profile["guidance"], "attitude_frequency_rad_s": frequency,
                                   "max_angular_acceleration_rad_s2": alpha}}, frequency, alpha


def _simulate_booster_steps(profile, separation_state_dict, duration_s=None, *, guidance_policy="fixed_v1", mission_director=None, return_sites=None, tower_ready=True, splashdown_goal=None,
                            development_coast_control="fixed_v1", development_coast_restart=None,
                            development_coast_from_separation=False, _trouble_engine=None, _trouble_control=None,
                            fidelity_settings=None, water_return_settings=None, outage_control_settings=None,
                            energy_control_settings=None, terminal_recovery_settings=None, final_prefix_supervision=None):
    """Continue from the exact supplied state for at most 2000 elapsed seconds.

    The return site is the rotating launch location. This is a geometric target,
    not catch hardware. A passing contact envelope means only its explicitly
    configured speed/tilt/rate/location bounds were met in this simulation.
    """
    from .starship_sixdof_mission import _attitude, control, vehicle
    from .starship_sixdof_contact import find_contact, hull_clearance
    from .starship_booster_fidelity import BoosterFidelitySettings, POLICY_ID, BOOSTBACK_OPEN_S, BOOSTBACK_CUTOFF_S, record_first_loads, landing_preview_admission
    from .starship_booster_guidance import WATER_POLICY_ID, WaterReturnSettings, WaterReturnPlanner
    from .starship_booster_outage_control import POLICY_ID as OUTAGE_POLICY_ID, validate_outage_scope
    from .starship_booster_energy_control import POLICY_ID as ENERGY_POLICY_ID, validate_energy_scope
    from .starship_booster_terminal_control import POLICY_ID as TERMINAL_POLICY_ID, validate_terminal_scope
    fidelity = guidance_policy == POLICY_ID
    water = guidance_policy == WATER_POLICY_ID
    outage = guidance_policy == OUTAGE_POLICY_ID
    energy = guidance_policy == ENERGY_POLICY_ID
    terminal = guidance_policy == TERMINAL_POLICY_ID
    if final_prefix_supervision is not None:
        from .starship_final_prefix_supervision import FinalPrefixSupervision
        if type(final_prefix_supervision) is not FinalPrefixSupervision or not terminal:
            raise ValueError("booster_final_supervision_requires_exact_terminal_scope")
        final_prefix_supervision.validate_scope(profile)
    if terminal:
        validate_terminal_scope(profile, terminal_recovery_settings, return_sites, splashdown_goal)
        if (any(value is not None for value in (mission_director, fidelity_settings, water_return_settings,
                outage_control_settings, energy_control_settings, development_coast_restart, _trouble_engine, _trouble_control))
                or development_coast_control != "fixed_v1" or development_coast_from_separation is not False):
            raise ValueError("terminal_recovery_cannot_borrow_other_execution_scopes")
    elif terminal_recovery_settings is not None:
        raise ValueError("terminal_recovery_requires_explicit_policy")
    if energy:
        validate_energy_scope(profile, energy_control_settings, return_sites, splashdown_goal)
        if (any(value is not None for value in (mission_director, fidelity_settings, water_return_settings,
                outage_control_settings, development_coast_restart, _trouble_engine, _trouble_control))
                or development_coast_control != "fixed_v1" or development_coast_from_separation is not False):
            raise ValueError("energy_control_cannot_borrow_other_execution_scopes")
    elif energy_control_settings is not None:
        raise ValueError("energy_control_requires_explicit_policy")
    if outage:
        validate_outage_scope(profile, outage_control_settings, return_sites, splashdown_goal)
        if (any(value is not None for value in (mission_director, fidelity_settings, water_return_settings,
                development_coast_restart, _trouble_engine, _trouble_control))
                or development_coast_control != "fixed_v1" or development_coast_from_separation is not False):
            raise ValueError("outage_control_cannot_borrow_other_execution_scopes")
    elif outage_control_settings is not None:
        raise ValueError("outage_control_requires_explicit_policy")
    if (water and (type(water_return_settings) is not WaterReturnSettings
            or splashdown_goal is None or return_sites is None
            or any(value is not None for value in (mission_director, fidelity_settings,
                development_coast_restart, _trouble_engine, _trouble_control))
            or development_coast_control != "fixed_v1" or development_coast_from_separation is not False)
            or not water and water_return_settings is not None):
        raise ValueError("water_return_requires_isolated_typed_surface_goal")
    if (fidelity and (type(fidelity_settings) is not BoosterFidelitySettings
            or any(value is not None for value in (mission_director, return_sites, splashdown_goal,
                development_coast_restart, _trouble_engine, _trouble_control))
            or development_coast_control != "fixed_v1" or development_coast_from_separation is not False)
            or not fidelity and fidelity_settings is not None):
        raise ValueError("booster_fidelity_cannot_borrow_execution_scopes")
    configuration = _configuration(profile)
    if fidelity:
        configuration = {**configuration, **fidelity_settings.record()}
    restart = None
    if type(development_coast_from_separation) is not bool:
        raise ValueError("invalid_development_coast_from_separation")
    if development_coast_from_separation and (development_coast_restart is not None
            or development_coast_control != "stopping_distance_v1" or guidance_policy != "fixed_v1"):
        raise ValueError("invalid_development_coast_from_separation")
    if development_coast_control not in ("fixed_v1", "stopping_distance_v1"):
        raise ValueError("unknown_development_coast_controller")
    if development_coast_restart is not None:
        from .starship_booster_coast_probe import CoastRestart
        if (type(development_coast_restart) is not CoastRestart or guidance_policy != "fixed_v1"
                or mission_director is not None or splashdown_goal is not None):
            raise ValueError("coast_restart_is_only_an_isolated_development_probe")
        restart = development_coast_restart.resolve(profile, return_sites=return_sites,
                                                     separation_state=separation_state_dict)
    elif development_coast_control != "fixed_v1" and not development_coast_from_separation:
        raise ValueError("development_coast_controller_requires_recorded_restart")
    if splashdown_goal is not None:
        from .starship_splashdown import SplashdownGoal
        if type(splashdown_goal) is not SplashdownGoal or return_sites is None or guidance_policy not in ("fixed_v1", WATER_POLICY_ID, OUTAGE_POLICY_ID, ENERGY_POLICY_ID, TERMINAL_POLICY_ID):
            raise ValueError("splashdown_requires_approved_goal_and_fixed_base_guidance")
        splashdown_goal.validate_site(return_sites.divert)
    if guidance_policy not in ("fixed_v1", "site_return_v1", "site_return_v2", "site_return_v3", POLICY_ID, WATER_POLICY_ID, OUTAGE_POLICY_ID, ENERGY_POLICY_ID, TERMINAL_POLICY_ID):
        raise ValueError("unknown_booster_guidance_policy")
    site_feedback = guidance_policy in ("site_return_v1", "site_return_v2", "site_return_v3") or fidelity or water or outage or energy
    drag_aware = guidance_policy in ("site_return_v2", "site_return_v3") or fidelity or water or outage
    mitigate_infeasible_burn = guidance_policy == "site_return_v3" or fidelity or water or outage
    duration = configuration["max_duration_s"] if duration_s is None else duration_s
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 2000:
        raise ValueError("booster_duration_must_be_in_zero_to_2000_seconds")
    state = restart["state"] if restart is not None else dyn.state_from_dict(separation_state_dict)
    time_steps = [profile.get("integration", {}).get(key) for key in ("powered_dt_s", "coast_dt_s")]
    if any(type(dt) not in (int, float) or not math.isfinite(dt) or not 0 < dt <= 10 for dt in time_steps):
        raise ValueError("invalid_booster_integration_step")
    if duration/min(time_steps) > 100_000:
        raise ValueError("booster_integration_budget_exceeded")
    if state.time_s+min(time_steps) <= state.time_s or state.time_s+duration <= state.time_s:
        raise ValueError("booster_integration_clock_must_advance")
    booster = vehicle(profile, "booster")
    # Validation checks the supplied actuator cardinality and limits, preserving
    # throttle, gimbals and unavailable-engine flags inherited from separation.
    dyn.observe(state, booster)
    start_time = state.time_s
    phase, termination = "booster_boostback_slew", "time_limit"
    burn_start, coast_started, slew_commanded = None, None, False
    terminal_boundary = None
    if restart is not None:
        phase, coast_started = "booster_entry_coast", restart["coast_started_s"]
        burn_start, slew_commanded = restart["burn_start_s"], True
    steps, samples, events = 0, [], []
    final_supervision_actual_witnesses = []
    max_altitude, max_rate, max_error = -math.inf, 0., 0.
    next_sample, contact_receipt = state.time_s, None
    fidelity_commands = {"boostback": None, "boostback_cutoff": None, "landing": None}
    outage_witnesses = []
    energy_witnesses = []
    terminal_witnesses = []
    reference_capture_closed = False
    reference_capture_handoff = None
    capture_reference = None
    powered_capture_controller = None
    powered_capture_last_request = None
    powered_capture_last_request_state = None
    powered_capture_status_witnesses = []
    if terminal and terminal_recovery_settings.low_pressure_reference_capture:
        from .starship_booster_lowq_capture import load_reference
        capture_reference = load_reference()
    fidelity_loads = {"boostback": None, "landing": None}
    water_planner = WaterReturnPlanner(water_return_settings, splashdown_goal.minimum_propellant_kg,
        boostback_fuel_floor_kg=configuration["landing_reserve_kg"],
        maximum_boostback_burn_s=configuration["boostback_max_burn_s"]) if water else None
    water_plan, water_refreshed = None, False
    water_commands = {"boostback": None, "boostback_cutoff": None, "landing": None}
    length, radius = profile["geometry"]["booster_length_m"], profile["geometry"]["radius_m"]

    def event(name, detail, **fields):
        events.append({"time_s": state.time_s, "event": name, "detail": detail, **fields})

    active_site = return_sites.divert if return_sites is not None else None
    energy_runtime = None
    if energy:
        from .starship_booster_energy_control import EnergyControlRuntime
        energy_runtime = EnergyControlRuntime(energy_control_settings, profile, active_site, splashdown_goal, start_time)
    # Capture corridor has not been qualified from launch for this controller.
    # The same independent restriction applies to the fixed-timeline comparator.
    def director_observation():
        from .starship_mission_director import fuel_sensor
        return {"time_s": state.time_s, "phase": "booster_return", "released_count": 0,
            "release_acknowledged": False, "sequencer_state": "running",
            "fuel_kg": fuel_sensor(state.propellant_kg, state.time_s, "booster"), "return_deadline_s": start_time+duration,
            "hold_expires_at_s": None,
            "tower_ready": tower_ready, "operations_notice": "",
            "numerical_tools": {"orbit_release_feasible": False, "retained_payload_present": False,
                "capture_corridor_certified": False, "mechanism_status": "not_collected"}}
    if restart is None:
        event("booster_return_start", "Exact separated state inherited; no position, velocity, attitude, rate or fault-state reset.")
    else:
        event("development_coast_restart", "Exact first recorded coast sample, including finite actuator states; not launch continuation.",
              source_run_sha256=restart["source_run_sha256"], state_sha256=restart["state_sha256"],
              source_sample_index=restart["sample_index"], controller=development_coast_control)
    while state.time_s < start_time+duration-1e-9:
        from .starship_concurrent import PhysicsTick
        if _trouble_engine is not None:
            state, booster = _trouble_engine.before_step(state, booster, "booster", phase)
            tower_ready = tower_ready and _trouble_engine.tower_ready(state.time_s)
        yield PhysicsTick("booster", state.time_s, phase, state)
        if mission_director is not None:
            mission_director.pace("booster", state.time_s)
            row = director_observation()
            action = mission_director.update("booster_selection", row)
            if action is not None:
                active_site = return_sites.divert if action == "splashdown" else return_sites.site(action)
                event("managed_booster_command", "preapproved early return-site decision; finite control integrates the trajectory",
                    action=action, return_site_sha256=active_site.sha256)
            mission_director.confirm("booster", row)
        observed = dyn.observe(state, booster)
        if final_prefix_supervision is not None:
            from .starship_final_protocol import digest
            started_wall = final_prefix_supervision.clock()
            before_requests = len(final_prefix_supervision.requests)
            actual_witness = None
            if final_prefix_supervision.observation_due(state, booster, phase, body="booster"):
                actual_state, actual_vehicle = asdict(state), asdict(booster)
                actual_witness = {"schema": "missionos.starship_final_actual_supervision_witness.v1",
                    "body": "booster", "time_s": state.time_s, "phase": phase,
                    "state": actual_state, "vehicle": actual_vehicle,
                    "state_sha256": digest(actual_state), "vehicle_sha256": digest(actual_vehicle),
                    "origin": "actual_plant_tick_before_supervision", "state_or_inventory_assigned": False}
            supervision_action = final_prefix_supervision.observe_booster(state, booster, phase,
                tower_ready=tower_ready, observation_started_wall_s=started_wall)
            if len(final_prefix_supervision.requests) > before_requests:
                if actual_witness is None:
                    raise ValueError("final_booster_trigger_without_actual_witness")
                actual_witness["request_ids"] = [row["request_id"] for row in final_prefix_supervision.requests[before_requests:]]
                final_supervision_actual_witnesses.append(actual_witness)
            if supervision_action == "inhibit_recovery":
                termination = "booster_recovery_refused_by_supervision"
                event(termination, "Body trial stopped at the actual current state; recovery unresolved, no safe contact inferred.",
                    state_reset=False, numerical_gnc_waited_for_model=False)
                break
        if fidelity:
            record_first_loads(state, booster, observed, fidelity_commands, fidelity_loads)
        if _trouble_control is not None:
            from .starship_trouble_control import observe as trouble_observe
            row = trouble_observe(state, booster, _trouble_engine, "booster", phase)
            witness_count = len(_trouble_control.observations)
            action = _trouble_control.step(row, asdict(state))
            if len(_trouble_control.observations) != witness_count:
                witness = _sample_booster(state, booster, phase, diagnostics={"component_health": row["component_health"]})
                witness["trouble_control_observation"] = row
                samples.append(witness)
            if action is not None:
                event("trouble_control_command", "experimental independently checked monitoring action; no recovery qualification", action=action)
        up, east, north, velocity, displacement, site_distance = _navigation(state, profile, return_site=active_site)
        altitude, vertical = observed["altitude_m"], env.dot(velocity, up)
        horizontal_velocity = _horizontal(velocity, up)
        mass = observed["mass_kg"]
        g = env.EARTH_MU_M3_S2/env.norm(state.r_eci_m)**2
        target, throttle, count = _attitude(up, north), 0., 0
        guidance = {"return_site_distance_m": site_distance, "vertical_speed_mps": vertical,
                    "guidance_origin": "explicit_generic_booster_return_assumption"}
        energy_trim = None
        if energy and state.propellant_kg > 0:
            before = phase
            request, target, energy_trim, energy_receipt = energy_runtime.request(state, booster, observed)
            phase, count, throttle = request.memory.phase, request.main_prefix_count, request.main_throttle
            burn_start = request.memory.boostback_started_s
            guidance["energy_guidance"] = energy_receipt
            if before == "booster_boostback_slew" and burn_start is not None:
                event("booster_boostback_ignition_command", "Shared energy kernel requested an available physical prefix; finite actuators remain integrated.",
                    requested_engine_count=count, requested_throttle=throttle)
            if before != phase and phase == "booster_boostback_settle":
                event("booster_boostback_settle_requested", "Shared kernel retains a finite physical engine prefix while tracking the predicted descent attitude; no achieved handoff assumed.",
                    requested_engine_count=count, requested_throttle=throttle,
                    cutoff_reason=request.diagnostics.get("cutoff_reason"))
            if before == "booster_boostback_settle" and phase != before:
                event("booster_boostback_settle_finished", "Actual full attitude and rate gate, or explicit incomplete handoff guard; no state assignment.",
                    handoff_completed=request.memory.handoff_completed,
                    handoff_incomplete=request.memory.handoff_incomplete,
                    handoff_guard_reason=request.memory.handoff_guard_reason)
            cutoff_requested = (phase == "booster_entry_coast" and before != phase
                if energy_control_settings.handoff_main_count else before in
                ("booster_boostback_slew", "booster_boostback") and phase not in
                ("booster_boostback_slew", "booster_boostback"))
            if cutoff_requested:
                coast_started = state.time_s
                event("booster_boostback_cutoff_command", "Shared current-state energy/reserve/time guard; finite shutdown tail retained.",
                    cutoff_reason=(request.diagnostics.get("handoff_guard_reason")
                        or request.diagnostics.get("cutoff_reason")
                        or ("handoff_completed" if request.memory.handoff_completed else None)),
                    remaining_propellant_kg=state.propellant_kg)
            if before != phase and phase == "booster_landing_align":
                event("booster_landing_align_requested", "Finite powered attitude alignment precedes the shared landing gate.")
            if before != phase and phase == "booster_landing_burn":
                event("booster_landing_burn_requested", "Shared above-ground, reserve-aware preview and measured pose/rate gate; no achieved ignition assumed.",
                    maximum_engine_count=min(13, profile["booster"]["engine_count"]))
        if not energy and phase in ("booster_boostback_slew", "booster_boostback"):
            fall_time = max(30., min(400., (vertical+math.sqrt(vertical*vertical+2*g*max(0., altitude)))/g))
            desired_horizontal = _cap(env.scale(displacement, 1/fall_time), configuration["boostback_max_horizontal_speed_mps"])
            if (terminal and terminal_recovery_settings.planned_capture_impulse_compensation
                    and any(not actual.available for actual in state.engine_states[:profile["booster"]["engine_count"]])):
                from .starship_booster_capture_impulse import planned_capture_impulse
                centers = tuple({"index": i, "name": engine.name, "available": actual.available,
                    "maximum_thrust_n": engine.max_thrust_n, "minimum_throttle": engine.min_throttle,
                    "isp_s": engine.isp_s, "direction_body": list(engine.direction_body)} for i, (engine, actual) in enumerate(zip(booster.engines[:3], state.engine_states[:3])))
                desired_horizontal, compensation_receipt = planned_capture_impulse(capture_reference,
                    cutoff_time_s=state.time_s, mass_kg=mass, propellant_kg=state.propellant_kg,
                    center_engines=centers, up_eci=up,
                    legacy_desired_horizontal_mps=desired_horizontal,
                    maximum_horizontal_speed_mps=configuration["boostback_max_horizontal_speed_mps"])
                guidance["capture_impulse_compensation"] = compensation_receipt
            velocity_error = _minus(desired_horizontal, horizontal_velocity)
            requested_acceleration = env.add(env.scale(velocity_error, 1/configuration["boostback_velocity_tau_s"]),
                                             env.scale(up, configuration["boostback_upward_acceleration_mps2"]))
            if water:
                def make_water_plan():
                    from .starship_booster_recovery import _coast_prediction
                    _, _, alpha = _coast_control_profile(profile, observed)
                    slew_rate = math.sqrt(2*alpha*math.pi)
                    position = [-env.dot(displacement, east), -env.dot(displacement, north), altitude]
                    local_velocity = [env.dot(velocity, axis) for axis in (east, north, up)]
                    def coast(p, v, m, fuel):
                        return _coast_prediction(p, v, m, fuel, booster.aero_panels,
                            isp_s=profile["booster"]["engine_isp_s"], slew_rate_rad_s=slew_rate, entry=True)
                    receipt = water_planner.plan(position, local_velocity, observed, state, booster, profile,
                        coast_prediction=coast)
                    receipt["coarse_axis_slew_rate_bound_rad_s"] = slew_rate
                    receipt["origin_state"] = asdict(state)
                    receipt["origin_position_enu_m"] = position
                    receipt["origin_velocity_enu_mps"] = local_velocity
                    receipt["origin_mass_kg"] = observed["mass_kg"]
                    receipt["return_site_sha256"] = active_site.sha256
                    receipt["minimum_terminal_propellant_kg"] = splashdown_goal.minimum_propellant_kg
                    return receipt
                def water_request(receipt):
                    desired = receipt["plan"]["selected"]["target_velocity_enu_mps"]
                    local_velocity = [env.dot(velocity, axis) for axis in (east, north, up)]
                    error = [desired[i]-local_velocity[i] for i in range(3)]
                    acceleration = [value/10. for value in error]
                    acceleration[2] += g
                    return error, tuple(sum(acceleration[j]*axis[j] for j in range(3)) for axis in zip(east, north, up))
                if water_plan is None:
                    water_plan = make_water_plan()
                    event("water_boostback_plan", "Bounded coarse water-return plan; no execution or recovery authority.",
                          ordinal=water_plan["ordinal"], status=water_plan["status"])
                velocity_error, requested_acceleration = water_request(water_plan)
                trial_axis = env.unit(requested_acceleration)
                if (not water_refreshed and env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), trial_axis)
                        >= math.cos(math.radians(configuration["boostback_alignment_deg"]))):
                    water_plan = make_water_plan()
                    water_refreshed = True
                    event("water_boostback_plan_refreshed", "Single refresh after measured alignment; no additional six-DOF forecast.",
                          ordinal=water_plan["ordinal"], status=water_plan["status"])
                    velocity_error, requested_acceleration = water_request(water_plan)
                guidance.update(water_plan_ordinal=water_plan["ordinal"], water_plan_status=water_plan["status"],
                    full_velocity_error_enu_mps=list(velocity_error),
                    selected_point_model_target_velocity_enu_mps=water_plan["plan"]["selected"]["target_velocity_enu_mps"])
            desired_axis = env.unit(requested_acceleration)
            target = _attitude(desired_axis, north)
            alignment = env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), desired_axis)
            guidance.update(horizontal_velocity_error_mps=math.hypot(*velocity_error[:2]) if water else env.norm(velocity_error), estimated_fall_time_s=fall_time)
            ready = alignment >= math.cos(math.radians(configuration["boostback_alignment_deg"]))
            if fidelity:
                ready = ready and state.time_s >= BOOSTBACK_OPEN_S
            tolerance = configuration["boostback_velocity_tolerance_mps"]
            if site_feedback and not drag_aware:
                # The old fixed velocity tolerance alone can represent many
                # kilometres of coast error. Bind it to the stated site radius.
                tolerance = min(tolerance, configuration["return_site_tolerance_m"]/fall_time)
            cutoff = (state.propellant_kg <= configuration["landing_reserve_kg"]
                      or env.norm(velocity_error) <= tolerance
                      or (burn_start is not None and state.time_s-burn_start >= configuration["boostback_max_burn_s"]))
            if fidelity:
                cutoff = cutoff or state.time_s >= BOOSTBACK_CUTOFF_S
            slew_timeout = burn_start is None and state.time_s-start_time >= configuration["boostback_max_slew_s"]
            if cutoff or slew_timeout:
                phase, coast_started = "booster_entry_coast", state.time_s
                event("booster_boostback_cutoff_command", "Return targeting/reserve/time guard; finite shutdown spool remains integrated.",
                      alignment_timeout=slew_timeout, remaining_propellant_kg=state.propellant_kg)
            elif ready:
                if phase == "booster_boostback_slew":
                    phase, burn_start = "booster_boostback", state.time_s
                    event("booster_boostback_ignition_command", "Configured main engines requested after measured alignment and reserve gates.",
                          requested_engine_count=configuration["boostback_engine_count"])
                count = configuration["boostback_engine_count"]
                available = sum(e.max_thrust_n for i, e in enumerate(booster.engines[:count]) if state.engine_states[i].available)
                throttle = max(.4, min(1., mass*env.norm(requested_acceleration)/max(available, 1.)))
                if (site_feedback and not drag_aware) or water:
                    count, throttle = _landing_engine_demand(state, booster, mass*env.norm(requested_acceleration), count)
            else:
                # Main-engine gimbals cannot create a moment with zero thrust.
                # Flight 8 documents three center engines retained at staging;
                # retaining them throughout this slew is an exposed engineering
                # assumption, not a recovered Flight 14 throttle programme.
                count = min(3, profile["booster"]["engine_count"])
                throttle = max(configuration["boostback_slew_throttle"],
                               max(e.min_throttle for e in booster.engines[:count]))
                if not slew_commanded:
                    event("booster_powered_slew_command", "Three center engines requested for finite gimbal authority; generic throttle assumption, not measured Flight 14 guidance.",
                          requested_engine_count=count, requested_throttle=throttle)
                    slew_commanded = True
        if not energy and phase == "booster_entry_coast":
            target = _attitude(env.scale(velocity, -1) if env.norm(velocity) > 20 and vertical < 0 else up, north)
            if drag_aware:
                # Engine-end down throughout coast. Avoid the old 90-degree
                # target change around apogee with nearly zero vertical speed.
                target = _attitude(up, north)
            available = sum(e.max_thrust_n for i, e in enumerate(booster.engines[:3]) if state.engine_states[i].available)
            stopping_height = max(0., -vertical)**2/(2*max(.1, available/mass-g))
            ignition_height = max(configuration["landing_ignition_altitude_m"],
                                  stopping_height+configuration["landing_ignition_margin_m"])
            guidance.update(estimated_stopping_height_m=stopping_height)
            feasible = True
            incomplete_arrest = False
            if drag_aware:
                maximum_landing = min(profile["booster"]["gimbal_engine_count"], profile["booster"]["engine_count"])
                if water and vertical < 0:
                    from .starship_booster_recovery import _vector_burn_preview
                    preview = _vector_burn_preview(state, booster, observed, profile, maximum_landing)
                    preview["estimated_vertical_displacement_m"] = -preview["estimated_stopping_height_m"]
                    preview["estimated_stopping_height_m"] = max(0., preview["estimated_stopping_height_m"])
                    feasible_preview = bool(preview["burn_fuel_feasible"])
                    preview.update(estimated_stopping_height_is_valid=feasible_preview,
                        estimate_termination=("arrested" if feasible_preview else
                            "fuel_exhausted" if preview["estimate_exhausted_available_fuel"] else
                            "no_available_thrust" if preview["available_landing_thrust_n"] <= 0 else "horizon_exceeded"))
                elif water:
                    preview = {"burn_fuel_feasible": False, "estimated_stopping_height_m": 0.,
                        "estimated_stopping_height_is_valid": False, "estimate_termination": "not_descending",
                        "estimated_final_speed_mps": env.norm(velocity), "prediction_is_execution": False}
                else:
                    preview = _drag_aware_stop_estimate(state, booster, observed, up, vertical, profile, maximum_landing)
                guidance.update(preview)
                feasible = preview["burn_fuel_feasible"]
                ignition_height = preview["estimated_stopping_height_m"]+configuration["landing_ignition_margin_m"]+observed["com_body_m"][2]
                if fidelity or water:
                    admission = landing_preview_admission(preview)
                    guidance["landing_preview_admission"] = admission
                    incomplete_arrest = admission["immediate_braking_request"]
                    feasible = admission["complete_arrest_prediction"]
                    # Empty-fuel/horizon distance is not a stopping distance.
                    # Request bounded mitigation now, without bypassing the
                    # terminal alignment gate or prescribing achieved thrust.
                    if incomplete_arrest:
                        ignition_height = None
            if vertical < 0 and (incomplete_arrest or
                    (feasible or mitigate_infeasible_burn) and altitude <= ignition_height):
                phase = "booster_landing_burn"
                event("booster_landing_burn_requested",
                      "Terminal braking thrust is alignment-gated; optional powered slew may still request three finite engines." if site_feedback
                      else "Landing attitude and up to three main engines requested; actual ignition remains alignment-gated.",
                      fuel_feasible=feasible,
                      burn_intent=("impact_mitigation_no_complete_arrest_prediction" if incomplete_arrest
                                   else "terminal_braking" if feasible else "impact_mitigation_insufficient_predicted_fuel"),
                      maximum_engine_count=profile["booster"]["gimbal_engine_count"] if drag_aware else 3)
        if not energy and phase == "booster_landing_burn":
            height = altitude
            if site_feedback or splashdown_goal is not None:
                height = hull_clearance(state, booster, length, radius)["signed_clearance_m"]
            # Aim below the admitted downward-entry limit while avoiding an
            # unnecessarily prolonged low-altitude powered descent.
            contact_target = .75*splashdown_goal.maximum_downward_speed_mps if splashdown_goal is not None else configuration["landing_target_speed_mps"]
            if (terminal and terminal_recovery_settings.energy_terminal_descent
                    and (not terminal_recovery_settings.planned_capture_impulse_compensation or observed["altitude_m"] <= 10000.)
                    and any(not actual.available for actual in state.engine_states[:profile["booster"]["engine_count"]])):
                from .starship_booster_terminal_descent import terminal_descent_request
                vertical_acceleration, descent_receipt = terminal_descent_request(
                    height, vertical, g, contact_target, configuration["landing_velocity_tau_s"])
                guidance["terminal_energy_descent"] = descent_receipt
            else:
                desired_vertical = -max(contact_target, min(100., max(0., height)/6))
                vertical_acceleration = max(.5, g+(desired_vertical-vertical)/configuration["landing_velocity_tau_s"])
            horizontal_braking = bool(terminal and terminal_recovery_settings.terminal_horizontal_braking
                and observed["altitude_m"] <= 1000.
                and any(not actual.available for actual in state.engine_states[:profile["booster"]["engine_count"]]))
            horizontal_tau = 2. if horizontal_braking else configuration["landing_horizontal_tau_s"]
            predicted_horizontal_velocity = horizontal_velocity
            if horizontal_braking:
                horizontal_force_acceleration = _horizontal(env.scale(dyn.rotate(state.q_body_to_eci,
                    env.add(observed["thrust_force_body_n"], observed["aero_force_body_n"])), 1/mass), up)
                horizontal_lead = 2/profile["guidance"]["attitude_frequency_rad_s"]
                predicted_horizontal_velocity = env.add(horizontal_velocity,
                    env.scale(horizontal_force_acceleration, horizontal_lead))
            horizontal_acceleration = env.scale(predicted_horizontal_velocity, -1/horizontal_tau)
            if site_feedback:
                tau = configuration["landing_horizontal_tau_s"]
                horizontal_acceleration = env.add(env.scale(displacement, 1/(tau*tau)), env.scale(horizontal_velocity, -2/tau))
            uncapped_horizontal_acceleration = horizontal_acceleration
            horizontal_acceleration = _cap(horizontal_acceleration,
                                           vertical_acceleration*math.tan(math.radians(configuration["landing_max_tilt_deg"])))
            if horizontal_braking:
                guidance["terminal_horizontal_braking"] = {
                    "schema": "missionos.booster_terminal_horizontal_braking.v1",
                    "altitude_m": float(observed["altitude_m"]), "maximum_activation_altitude_m": 1000.,
                    "profile_horizontal_velocity_tau_s": configuration["landing_horizontal_tau_s"],
                    "selected_horizontal_velocity_tau_s": horizontal_tau,
                    "horizontal_velocity_eci_mps": [float(x) for x in horizontal_velocity],
                    "current_mass_kg": float(mass),
                    "current_thrust_force_body_n": [float(x) for x in observed["thrust_force_body_n"]],
                    "current_aero_force_body_n": [float(x) for x in observed["aero_force_body_n"]],
                    "attitude_frequency_rad_s": profile["guidance"]["attitude_frequency_rad_s"],
                    "lead_horizon_s": horizontal_lead,
                    "actual_horizontal_force_acceleration_eci_mps2": [float(x) for x in horizontal_force_acceleration],
                    "predicted_horizontal_velocity_eci_mps": [float(x) for x in predicted_horizontal_velocity],
                    "uncapped_horizontal_acceleration_eci_mps2": [float(x) for x in uncapped_horizontal_acceleration],
                    "horizontal_acceleration_eci_mps2": [float(x) for x in horizontal_acceleration],
                    "vertical_force_acceleration_mps2": float(vertical_acceleration),
                    "maximum_tilt_deg": configuration["landing_max_tilt_deg"],
                    "prediction_is_achieved_state": False,
                    "controller_assumption_changed": True, "physics_limits_changed": False,
                    "actual_state_assigned": False}
            requested = env.add(env.scale(up, vertical_acceleration), horizontal_acceleration)
            target = _attitude(env.unit(requested), north)
            alignment = env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), env.unit(requested))
            guidance.update(terminal_thrust_alignment_cosine=float(alignment),
                            terminal_braking_alignment_gate_met=bool(alignment > .5))
            if alignment > .5:
                count, throttle = _landing_engine_demand(state, booster, mass*env.norm(requested)/alignment,
                                                          min(profile["booster"]["gimbal_engine_count"] if drag_aware else 3, profile["booster"]["engine_count"]))
            elif site_feedback:
                # A gimbal cannot rotate the booster with its engine off.
                count, throttle = min(3, profile["booster"]["engine_count"]), configuration["boostback_slew_throttle"]
            elif (terminal and terminal_recovery_settings.powered_terminal_alignment
                    and any(not actual.available for actual in state.engine_states[:profile["booster"]["engine_count"]])):
                # Explicit fault-only repair. Preserve the fixed terminal target
                # and alignment gate, but retain real gimbal authority while
                # the measured pose is still outside that gate.
                count, throttle = min(3, profile["booster"]["engine_count"]), configuration["boostback_slew_throttle"]
                guidance["terminal_recovery_powered_alignment_requested"] = True
        if _trouble_engine is not None:
            state, booster = _trouble_engine.before_step(state, booster, "booster", phase)
        control_profile = profile
        if drag_aware and phase == "booster_entry_coast":
            control_profile, bandwidth, alpha = _coast_control_profile(profile, observed, include_spooled_tvc=mitigate_infeasible_burn)
            guidance.update(coast_control_frequency_rad_s=bandwidth, coast_angular_acceleration_limit_rad_s2=alpha)
        if energy:
            from .starship_booster_energy_control import energy_control_command
            command, diagnostic = energy_control_command(state, booster, target, throttle, count, profile,
                settings=energy_control_settings, phase=phase, observed=observed,
                control_interval_s=min(.25, start_time+duration-state.time_s,
                    profile["integration"]["powered_dt_s"] if throttle > 0 or altitude < 100_000
                    else profile["integration"]["coast_dt_s"]), trim_angles_rad=energy_trim)
        elif terminal:
            from .starship_booster_terminal_control import terminal_control_command
            reference_active = False
            capture_just_closed = False
            powered_capture_request = None
            powered_tail_tracking = False
            powered_capture_status_changed = False
            if (terminal_recovery_settings.low_pressure_reference_capture and coast_started is not None
                    and any(not actual.available for actual in state.engine_states[:33])):
                if terminal_recovery_settings.powered_reference_capture and not reference_capture_closed:
                    from .starship_booster_powered_capture import PoweredReferenceCapture
                    if powered_capture_controller is None:
                        powered_capture_controller = PoweredReferenceCapture(capture_reference, coast_started)
                    powered_capture_request = powered_capture_controller.request(state, booster)
                    powered_capture_last_request = powered_capture_request.record()
                    powered_capture_last_request_state = asdict(state)
                    if powered_capture_request.status_changed:
                        witness = {"time_s": state.time_s, "state": asdict(state),
                            "request": powered_capture_request.record()}
                        powered_capture_status_witnesses.append(witness)
                        event("booster_powered_reference_capture_status",
                            "Bounded actual-pose/reference-rate capture pulse status; finite main shutdown tails remain integrated.",
                            state=witness["state"], request=witness["request"])
                        powered_capture_status_changed = True
                if not reference_capture_closed and observed["dynamic_pressure_pa"] > 50.:
                    ref = capture_reference.at(state.time_s, actual_cutoff_time_s=coast_started)
                    inverse = (state.q_body_to_eci[0], *(-x for x in state.q_body_to_eci[1:]))
                    ref_rate = dyn.rotate(inverse, dyn.rotate(ref.q_body_to_eci, ref.omega_reference_body_rad_s))
                    errq = dyn.quaternion_multiply(inverse, ref.q_body_to_eci)
                    angle = math.degrees(2*math.acos(min(1., abs(errq[0]))))
                    rate_error = env.norm(tuple(a-b for a,b in zip(state.omega_body_rad_s, ref_rate)))
                    reference_capture_handoff = {"time_s": state.time_s, "state": asdict(state),
                        "dynamic_pressure_pa": observed["dynamic_pressure_pa"], "desired_reference": ref.record(),
                        "full_attitude_error_deg": angle, "body_rate_error_rad_s": rate_error,
                        "capture_met": bool(angle <= 5. and rate_error <= .003),
                        "handoff_reason": "dynamic_pressure_exceeded_50pa", "acceptance_gate": False}
                    event("booster_reference_capture_handoff", "Existing 50 Pa fin boundary ends low-pressure capture even if its diagnostic pose/rate limits were not reached.",
                        **{key:value for key,value in reference_capture_handoff.items() if key != "time_s"})
                    reference_capture_closed, capture_just_closed = True, True
                capacity = sum(load["thrust_n"]*abs(engine.position_body_m[2]-observed["com_body_m"][2])
                    *math.sin(engine.max_gimbal_rad) for engine, actual, load in
                    zip(booster.engines, state.engine_states, observed["engine_loads"])
                    if not engine.name.startswith("rcs_") and actual.available and engine.max_gimbal_rad > 0)
                jets = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
                reference_active = not reference_capture_closed and capacity <= jets
                powered_tail_tracking = bool(powered_capture_request is not None and not powered_capture_request.active
                    and not reference_capture_closed and capacity > jets)
            terminal_control_interval_s = min(start_time+duration-state.time_s,
                profile["integration"]["powered_dt_s"] if throttle > 0 or altitude < 100_000
                else profile["integration"]["coast_dt_s"])
            if terminal_recovery_settings.powered_reference_capture:
                powered_route = bool(powered_capture_request is not None and not reference_capture_closed
                    and (powered_capture_request.active or powered_tail_tracking))
                selected_main_on = (powered_capture_request.active if powered_route
                    else False if reference_active else throttle > 0 and count > 0)
                terminal_control_interval_s = min(start_time+duration-state.time_s,
                    profile["integration"]["powered_dt_s"] if selected_main_on or altitude < 100_000
                    else profile["integration"]["coast_dt_s"])
                if powered_route and powered_capture_request.active:
                    from .starship_booster_powered_capture import MAXIMUM_DURATION_S
                    terminal_control_interval_s = min(terminal_control_interval_s, coast_started+MAXIMUM_DURATION_S-state.time_s)
            command, diagnostic = terminal_control_command(state, booster, target, throttle, count, profile,
                settings=terminal_recovery_settings, phase=phase, observed=observed,
                coast_started_s=coast_started, reference_capture_active=reference_active,
                reference_capture_closed=reference_capture_closed,
                capture_reference=capture_reference,
                powered_capture_request=powered_capture_request, powered_tail_tracking=powered_tail_tracking,
                control_interval_s=terminal_control_interval_s)
        elif outage:
            from .starship_booster_outage_control import outage_control_command
            command, diagnostic = outage_control_command(state, booster, target, throttle, count, control_profile,
                settings=outage_control_settings, phase=phase, observed=observed,
                control_interval_s=min(.25, profile["integration"]["powered_dt_s"] if throttle > 0 or altitude < 100_000
                                       else profile["integration"]["coast_dt_s"]))
        elif development_coast_control == "stopping_distance_v1" and phase == "booster_entry_coast":
            from .starship_booster_control import control_coast_stopping_distance
            control_interval = (profile["integration"]["powered_dt_s"] if throttle > 0 or altitude < 100_000
                                else profile["integration"]["coast_dt_s"])
            command, diagnostic = control_coast_stopping_distance(state, booster, target, profile,
                                                                  control_interval_s=control_interval)
        else:
            if water and phase == "booster_entry_coast" and observed["dynamic_pressure_pa"] <= 100.:
                from .starship_booster_control import control_coast_stopping_distance
                command, diagnostic = control_coast_stopping_distance(state, booster, target, profile,
                    control_interval_s=min(.25, profile["integration"]["powered_dt_s"] if altitude < 100_000
                                           else profile["integration"]["coast_dt_s"]))
            elif fidelity or water:
                from .starship_booster_control import control_with_measured_tvc
                command, diagnostic = control_with_measured_tvc(state, booster, target, throttle, count, control_profile,
                    use_flaps=phase in ("booster_entry_coast", "booster_landing_burn"),
                    **({"development_fin_allocation": True,
                        "control_interval_s": min(.25, profile["integration"]["powered_dt_s"] if throttle > 0 or altitude < 100_000
                                                  else profile["integration"]["coast_dt_s"])} if water else {}))
            else:
                command, diagnostic = control(state, booster, target, throttle, count, control_profile,
                                              use_flaps=phase in ("booster_entry_coast", "booster_landing_burn"))
        if _trouble_engine is not None:
            command = _trouble_engine.command(command, state, booster, "booster")
            diagnostic["component_health"] = _trouble_engine.observation(state, booster, "booster")
        if (diagnostic.get("low_pressure_reference_capture") is not None or diagnostic.get("powered_reference_capture") is not None
                or diagnostic.get("residual_reference_capture") is not None):
            for key in ("terminal_thrust_alignment_cosine", "terminal_braking_alignment_gate_met"):
                if key in guidance:
                    guidance["legacy_"+key] = guidance.pop(key)
        diagnostic.update(guidance)
        outage_new_witness = False
        energy_new_witness = False
        terminal_new_witness = False
        if terminal:
            mode = diagnostic["booster_terminal_recovery"]["mode"]
            if not any(row["phase"] == phase and row["mode"] == mode for row in terminal_witnesses):
                terminal_witnesses.append({"time_s": state.time_s, "phase": phase, "mode": mode,
                    "state": asdict(state), "command": asdict(command), "controller": diagnostic})
                terminal_new_witness = True
            if capture_just_closed or powered_capture_status_changed:
                terminal_new_witness = True
        if energy:
            mode = diagnostic["booster_energy_control"]["mode"]
            if not any(row["phase"] == phase and row["mode"] == mode for row in energy_witnesses):
                energy_witnesses.append({"time_s": state.time_s, "phase": phase, "mode": mode,
                    "state": asdict(state), "command": asdict(command), "controller": diagnostic})
                energy_new_witness = True
        if outage:
            mode = diagnostic["booster_outage_control"]["mode"]
            if not any(row["phase"] == phase and row["mode"] == mode for row in outage_witnesses):
                outage_witnesses.append({"time_s": state.time_s, "phase": phase, "mode": mode,
                    "state": asdict(state), "command": asdict(command), "controller": diagnostic})
                outage_new_witness = True
        if fidelity or water:
            commands = fidelity_commands if fidelity else water_commands
            for name, event_name in (("boostback", "booster_boostback_ignition_command"),
                                     ("boostback_cutoff", "booster_boostback_cutoff_command"),
                                     ("landing", "booster_landing_burn_requested")):
                if commands[name] is None and any(event["event"] == event_name for event in events):
                    commands[name] = {"time_s": state.time_s, "state": asdict(state),
                        "command": asdict(command), "requested_main_count": count, "requested_throttle": throttle}
        if restart is not None and phase == "booster_landing_burn":
            terminal_boundary = {"time_s": state.time_s, "controller": diagnostic,
                                 "command": asdict(command), "command_executed": False}
            termination = "development_coast_terminal_requested"
            event(termination, "Stopped before applying the first terminal command; no entry success inferred.")
            break
        max_altitude = max(max_altitude, altitude)
        max_rate = max(max_rate, env.norm(state.omega_body_rad_s))
        max_error = max(max_error, diagnostic["attitude_error_deg"])
        if state.time_s >= next_sample-1e-9 or outage_new_witness or energy_new_witness or terminal_new_witness:
            samples.append(_sample_booster(state, booster, phase, command, diagnostic))
            if state.time_s >= next_sample-1e-9:
                next_sample = state.time_s+2.
        if env.norm(state.omega_body_rad_s) > 5:
            termination = "angular_rate_envelope_exceeded"
            event(termination, "Development 5 rad/s bound; no structural breakup solver.")
            break
        dt = min(start_time+duration-state.time_s,
                 profile["integration"]["powered_dt_s"] if throttle > 0 or altitude < 100_000
                 else profile["integration"]["coast_dt_s"])
        if terminal and terminal_recovery_settings.powered_reference_capture:
            actual_main_on = any(target.enabled and target.throttle > 0 for engine,target in zip(booster.engines,command.engines)
                if not engine.name.startswith("rcs_"))
            actual_request_interval = min(start_time+duration-state.time_s,
                profile["integration"]["powered_dt_s"] if actual_main_on or altitude < 100_000
                else profile["integration"]["coast_dt_s"])
            if powered_capture_request is not None and powered_capture_request.active and not reference_capture_closed:
                from .starship_booster_powered_capture import MAXIMUM_DURATION_S
                actual_request_interval = min(actual_request_interval, coast_started+MAXIMUM_DURATION_S-state.time_s)
            if abs(actual_request_interval-terminal_control_interval_s) > 1e-12:
                raise RuntimeError("powered_reference_capture_command_interval_mismatch")
            dt = terminal_control_interval_s
        if water or energy:
            dt = min(dt, .25)
        if fidelity:
            dt = fidelity_settings.limit_step(state.time_s, dt)
        if _trouble_engine is not None:
            dt = _trouble_engine.limit_step(state.time_s, dt, "booster", phase)
        if type(dt) not in (int, float) or not math.isfinite(dt) or not 0 < dt <= 10:
            raise ValueError("invalid_booster_integration_step")
        # A conservative clearance bound avoids repeatedly solving near-Earth
        # hull geometry while the entire body is obviously far from the surface.
        maximum_acceleration = 50.+sum(e.max_thrust_n for e in booster.engines)/booster.dry_mass_kg
        near_surface = altitude <= length+radius+env.norm(observed["com_body_m"])+100.+env.norm(velocity)*dt+.5*maximum_acceleration*dt*dt
        if near_surface:
            state, receipt = find_contact(state, booster, command, dt, length, radius)
            if receipt["contact"]:
                contact_receipt = receipt
                termination = "surface_contact"
                event(termination, "Cylinder-envelope contact event; no mechanical catch or structural solver.",
                      surface_relative_speed_mps=receipt["surface_relative_speed_mps"])
                steps += 1
                break
        else:
            state = dyn.step(state, booster, command, dt)
        steps += 1
    final = _sample_booster(state, booster, phase)
    if fidelity:
        record_first_loads(state, booster, dyn.observe(state, booster), fidelity_commands, fidelity_loads)
    final["contact"] = contact_receipt is not None
    if (not samples or samples[-1]["time_s"] != state.time_s
            or (outage or energy or terminal) and samples[-1].get("command") is not None):
        samples.append(final)
    else:
        samples[-1] = final
    up, _, _, _, _, distance = _navigation(state, profile, return_site=active_site)
    tilt = math.degrees(math.acos(max(-1., min(1., env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), up)))))
    rate = env.norm(state.omega_body_rad_s)
    envelope = (contact_receipt is not None
                and not contact_receipt["initial_overlap"]
                and contact_receipt["surface_normal_speed_mps"] <= 0
                and contact_receipt["surface_relative_speed_mps"] <= configuration["contact_speed_limit_mps"]
                and tilt <= configuration["contact_tilt_limit_deg"] and rate <= configuration["contact_rate_limit_rad_s"]
                and distance <= configuration["return_site_tolerance_m"])
    if termination == "time_limit":
        event("time_limit", "Return integration horizon reached; no terminal success inferred.")
    clearance = hull_clearance(state, booster, length, radius)
    result = {"scenario": "booster_return", "body_id": "booster", "samples": samples, "events": events,
            "booster_separation_state": separation_state_dict, "initial_state": samples[0],
            "final_state": asdict(state), "final_vehicle": asdict(booster), "contact": contact_receipt,
            "guidance_configuration": configuration, "guidance_policy": guidance_policy,
            **({"active_return_site": active_site.to_dict(), "return_site_sha256": active_site.sha256,
                "capture_inhibited": True, "divert_destination_reached": bool(envelope)} if active_site else {}),
            "outcome": {"termination": termination, "phase": phase, "duration_s": state.time_s-start_time,
                        "start_time_s": start_time, "end_time_s": state.time_s, "integration_steps": steps,
                        "max_altitude_m": max(max_altitude, final["altitude_m"]), "max_body_rate_rad_s": max(max_rate, rate),
                        "max_attitude_error_deg": max_error, "final_ground_speed_mps": final["ground_speed_mps"],
                        "final_altitude_m": final["altitude_m"], "final_hull_clearance_m": clearance["signed_clearance_m"],
                        "return_site_distance_m": distance, "final_tilt_deg": tilt, "final_body_rate_rad_s": rate,
                        "simulated_contact_envelope_met": bool(envelope), "catch_verified": False,
                        "landing_hardware_validated": False, "starship_vehicle_validated": False,
                        "six_dof_integrated": True, "attitude_prescribed": False,
                        "booster_return_6dof_implemented": True, "boostback_started": burn_start is not None,
                        "coast_started": coast_started is not None, "physical_execution": False},
            "limitations": ["Generic closed-loop return guidance and assumed actuator/aerodynamic parameters, not SpaceX flight software.",
                            "site_return_v2/v3 are optional bounded development trials: measured-drag one-dimensional burn preview and RCS-authority coast control; no terminal capture guarantee. v3 preserves spool-down TVC and attempts impact mitigation even when the burn cannot fully arrest descent.",
                            "Three-center-engine powered slew references the historical staging arrangement; slew throttle and control law are engineering assumptions, not a measured V3 flip sequence.",
                            "Return target is the declared model-test site when supplied, otherwise the launch-site ground location; catch tower, mechanisms, contact loads and structural survival are unmodeled.",
                            "No separate entry burn is introduced. Configured boostback count defaults to the V3 planned 33; fixed_v1/site_return_v1 final burn selects one to three, while v2/v3 can select up to the configured gimballed count. Neither reproduces Flight 14's 13-to-five-to-three landing sequence.",
                            "Shared propellant reservoir, no feed-path momentum, slosh, combustion or TPS model."]}
    if final_prefix_supervision is not None:
        result["final_prefix_supervision"] = final_prefix_supervision.record()
        result["final_supervision_actual_witnesses"] = final_supervision_actual_witnesses
        result["outcome"]["recovery_refused_by_supervision"] = final_prefix_supervision.recovery_refused
        result["outcome"]["recovery_unresolved"] = termination == "booster_recovery_refused_by_supervision"
    if fidelity:
        result["booster_fidelity"] = {"schema": "missionos.starship_booster_fidelity.v1", "policy_id": POLICY_ID,
            "settings": fidelity_settings.record(),
            "boostback_window": {"opens_at_s": BOOSTBACK_OPEN_S, "cutoff_at_s": BOOSTBACK_CUTOFF_S},
            "command_witnesses": fidelity_commands, "first_load_witnesses": fidelity_loads,
            "separation_state_preserved": True, "availability_preserved": True,
            "mission_authority_granted": False, "physical_execution": False,
            "requested_boostback_group_count": configuration["boostback_engine_count"],
            "maximum_landing_group_count": min(profile["booster"]["gimbal_engine_count"], profile["booster"]["engine_count"]),
            "initial_available_main_count": sum(actual.available for engine, actual in zip(booster.engines,
                dyn.state_from_dict(separation_state_dict).engine_states) if "main" in engine.name),
            "source_count_limits": "Operator31/33 boostback and11/13 landing have unidentified engines; no extra outages or13-to5-to3 sequence inferred.",
            "coast_control": "configured jet-pair torque and current inertia bound; measured spool-down TVC retained",
            "one_km_ignition_verified": False}
        result["limitations"].append("Stage2c bounded timing screen:147/187 are approximate planned anchors, not certified flown burn times;1km ignition and13-to5-to3 sequencing are not reconstructed.")
    if development_coast_from_separation:
        result["development_coast_from_separation"] = {
            "controller": development_coast_control, "operational_policy_approved": False,
            "physics_limits_changed": False, "terminal_alignment_gate_changed": False,
            "separation_state_preserved": True}
    if restart is not None:
        result["development_coast"] = {"schema": "missionos.booster_coast_probe.v1",
            "controller": development_coast_control, "source_run_sha256": restart["source_run_sha256"],
            "state_sha256": restart["state_sha256"], "profile_sha256": restart["profile_sha256"],
            "source_sample_index": restart["sample_index"], "terminal_boundary": terminal_boundary,
            "stops_before_terminal_execution": True, "operational_policy_approved": False}
        result["limitations"].append("Isolated recorded coast continuation, not a launch-to-splashdown qualification.")
    if splashdown_goal is not None:
        from .starship_splashdown import entry_result
        result["splashdown"] = entry_result(result, splashdown_goal)
        result["limitations"].append("Controlled water-entry envelope at a synthetic offshore area; no waves, buoyancy, structural survival or real clearance validation.")
    if water:
        result["water_return"] = {"schema": "missionos.starship_coupled_water_return.v1", "policy_id": WATER_POLICY_ID,
            "settings": water_return_settings.record(), "plans": water_planner.receipts,
            "command_witnesses": water_commands,
            "plan_count": len(water_planner.receipts),
            "point_coast_forecast_count": sum(r["point_coast_forecast_count"] for r in water_planner.receipts),
            "coarse_burn_estimate_count": sum(r["coarse_burn_estimate_count"] for r in water_planner.receipts),
            "coarse_burn_steps": sum(r["coarse_burn_steps"] for r in water_planner.receipts),
            "nested_sixdof_forecasts": 0, "capture_allowed": False, "mission_authority_granted": False,
            "minimum_terminal_propellant_kg": splashdown_goal.minimum_propellant_kg,
            "return_site_sha256": active_site.sha256, "physics_limits_changed": False,
            "actual_state_assigned": False, "qualification_refreshed": False}
        result["limitations"].append("Opt-in coupled water return: fixed finite point-model planning only; all actual motion uses this shared phase machine. No nested six-DOF forecasts, catch hardware or recovery authority. Full-throttle coarse burns omit direction tracking/TVC; predictions are not qualification.")
    if outage:
        result["booster_outage_control"] = {"schema": "missionos.booster_outage_control.v1",
            "policy_id": OUTAGE_POLICY_ID, "settings": outage_control_settings.record(),
            "control_witnesses": outage_witnesses, "availability_preserved": True,
            "physics_limits_changed": False, "actual_state_assigned": False,
            "mission_authority_granted": False, "qualification_refreshed": False,
            "nested_sixdof_forecasts": 0, "hosted_model_calls": 0}
        result["limitations"].append("Opt-in availability-aware control: measured thrust/TVC, finite uncommanded gimbal recentering, jet-limited coast slew and drag-aware terminal preview; no physical model, contact limit, engine availability or state reset.")
    if energy:
        result["booster_energy_control"] = {"schema": "missionos.booster_energy_control.v1",
            "policy_id": ENERGY_POLICY_ID, "settings": energy_control_settings.record(),
            "control_witnesses": energy_witnesses, "availability_preserved": True,
            "physics_limits_changed": False, "actual_state_assigned": False,
            "mission_authority_granted": False, "qualification_refreshed": False,
            "nested_sixdof_forecasts": 0, "hosted_model_calls": 0}
        result["limitations"].append("Opt-in shared energy requests and actual finite controls; local trim, authority and aligned one-dimensional landing previews are numerical predictions, not achieved motion, qualification or a guaranteed return.")
    if terminal:
        result["booster_terminal_recovery"] = {"schema": "missionos.booster_terminal_recovery.v1",
            "policy_id": TERMINAL_POLICY_ID, "settings": terminal_recovery_settings.record(),
            "control_witnesses": terminal_witnesses, "availability_preserved": True,
            "legacy_guidance_preserved": not terminal_recovery_settings.low_pressure_reference_capture,
            "separation_state_preserved": True,
            "physics_limits_changed": False,
            "actual_state_assigned": False, "mission_authority_granted": False,
            "qualification_refreshed": False, "nested_sixdof_forecasts": 0, "hosted_model_calls": 0}
        if terminal_recovery_settings.low_pressure_reference_capture:
            result["booster_terminal_recovery"]["reference_capture_handoff"] = reference_capture_handoff
        if terminal_recovery_settings.powered_reference_capture:
            result["booster_terminal_recovery"]["powered_reference_capture"] = {
                "schema": "missionos.booster_powered_reference_capture_history.v1",
                "last_request": powered_capture_last_request,
                "last_request_state": powered_capture_last_request_state,
                "status_witnesses": powered_capture_status_witnesses}
        if terminal_recovery_settings.planned_capture_impulse_compensation:
            result["limitations"].append("Explicit fault-only guidance subtracts a prospective full15s ideal minimum-throttle capture-pulse horizontal impulse from the current boostback velocity target. This desired-reference approximation omits spool, tail, early capture completion, attitude error and other-engine forces; it is not future truth or an achieved correction. Actual altitude at or below 10 km selects the fixed8 vertical descent request, with approximate COM-vertical feedforward; legacy vertical guidance applies above it. All finite physical parameters and healthy commands remain unchanged. No stopping guarantee or qualification is implied.")
        elif terminal_recovery_settings.energy_terminal_descent:
            result["limitations"].append("Explicit fault-only terminal descent uses a fixed 8 m/s² square-root vertical reference and approximate COM-vertical feedforward after low-pressure reference capture. The fixed value is a controller design assumption, not a physical parameter. Healthy guidance, legacy attitude allocation, horizontal damping, guidance tilt and engine-count limits remain unchanged; fault-only vertical guidance is explicitly replaced. This request is not a stopping guarantee or qualification.")
        elif terminal_recovery_settings.powered_reference_capture:
            result["limitations"].append("Opt-in fault-only measured-load allocation. The explicit powered-reference option permits one bounded finite center-engine pulse and retains same-reference TVC through its actual shutdown tail; the subsequent RCS-only stage requests mains OFF. Actual state and all physical limits remain unchanged. Healthy commands and post-handoff legacy guidance use the unmodified legacy controller. No return success or qualification is implied.")
        else:
            result["limitations"].append("Opt-in fault-only measured-load allocation, with separately declared low-pressure reference capture when enabled. Capture requests finite jets and mains OFF; actual state and all physical limits remain unchanged. Healthy commands and post-handoff legacy guidance use the unmodified legacy controller. No return success or qualification is implied.")
        if terminal_recovery_settings.terminal_horizontal_braking:
            result["limitations"].append("The explicit terminal horizontal-braking option uses current measured thrust-plus-air-force horizontal acceleration over a 2/attitude-frequency lead horizon, then damps that approximate predicted horizontal velocity with a 2 s time constant at actual fault-only geodetic altitude at or below 1 km. This constant-current-force prediction is not achieved state. It preserves guidance above that threshold, the vertical request, existing 15-degree guidance tilt and finite actuator/count limits. The time constant and lead are controller assumptions, not physical parameters or a return guarantee.")
    return result


def simulate_booster(profile, separation_state_dict, duration_s=None, *, guidance_policy="fixed_v1", mission_director=None, return_sites=None, tower_ready=True, splashdown_goal=None,
                     development_coast_control="fixed_v1", development_coast_restart=None,
                     development_coast_from_separation=False, _trouble_engine=None, fidelity_settings=None,
                     water_return_settings=None, outage_control_settings=None, energy_control_settings=None,
                     terminal_recovery_settings=None):
    """Legacy synchronous API; draining does not change any integration step."""
    from .starship_concurrent import drain
    return drain(_simulate_booster_steps(profile, separation_state_dict, duration_s,
        guidance_policy=guidance_policy, mission_director=mission_director, return_sites=return_sites,
        tower_ready=tower_ready, splashdown_goal=splashdown_goal,
        development_coast_control=development_coast_control, development_coast_restart=development_coast_restart,
        development_coast_from_separation=development_coast_from_separation, _trouble_engine=_trouble_engine,
        fidelity_settings=fidelity_settings, water_return_settings=water_return_settings,
        outage_control_settings=outage_control_settings, energy_control_settings=energy_control_settings,
        terminal_recovery_settings=terminal_recovery_settings))

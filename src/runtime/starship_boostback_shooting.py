"""Bounded short finite-6DOF boostback predictions for development guidance.

Predictions clone the inherited physical and reference states. They end after
boostback, measured powered-rate settling and a finite shutdown tail. Neither
these forecasts nor their optimization result can establish arrival or support.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math

import numpy as np

from . import starship_physics as env
from . import starship_sixdof as dyn
from .starship_attitude_reference import ConditionedGeographicFrame
from .starship_booster_control import control_coast_stopping_distance, control_with_measured_tvc
from .starship_booster_catch import configuration as catch_configuration
from .starship_sixdof_booster import _configuration, _navigation, _landing_engine_demand
from .starship_sixdof_mission import _attitude, vehicle

CONFIG = {
    "maximum_forecast_calls": 48, "maximum_solver_nfev": 12,
    "maximum_forecast_duration_s": 100., "shutdown_tail_s": 1.4,
    "angular_offset_bound_deg": 15., "cut_projection_offset_bound_mps": 300.,
    "angle_difference_step_rad": .002, "cut_difference_step_mps": 30.,
    "minimum_boostback_step_s": .001, "prepare_tilt_limit_deg": 5.,
    "post_cutoff_control": "powered_upright_prepare",
    "alignment_deg": 15., "velocity_tolerance_mps": 12.,
    "rate_settle_limit_rad_s": .003, "rate_settle_max_s": 30.,
    "velocity_residual_scale_mps": 30., "position_residual_scale_m": 10000.,
    "fuel_residual_scale_kg": 10000., "incomplete_forecast_penalty": 100.,
    "objective": "post_prepare_coast_intercept", "terminal_position_residual_scale_m": 1000.,
    "terminal_speed_residual_scale_mps": 100., "vertical_regularization_scale_mps": 200.,
    "maximum_point_continuations_per_forecast": 1, "coast_entry_angle_deg": 35.,
    "coast_terminal_altitude_m": 1500., "coast_model": "existing_full_panel_vector",
    "ideal_arrest_dt_s": .1, "ideal_arrest_horizon_s": 60., "ideal_arrest_terminal_speed_mps": 2.,
    "ideal_arrest_model": "aligned_available_engine_prefix_spool_gravity",
}
FORECAST_SCHEMA = "missionos.starship_short_boostback_shooting.v3"
TRANSPORT_FORECAST_SCHEMA = "missionos.starship_short_boostback_shooting.v4"
TRANSPORT_PREPARE_POLICY = "parallel_transport_deferred_geographic_roll_v1"
TRANSPORT_TAIL_MODEL = "four_max_main_throttle_tau_command_off_transport"


class _BudgetExhausted(RuntimeError):
    pass


class ForecastUnavailable(RuntimeError):
    """A failed bounded attempt retains its counters for the enclosing caller."""

    def __init__(self, receipt):
        super().__init__("no_completed_boostback_forecast")
        self.forecast_receipt = receipt


def _saved(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _vector(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 3 or any(
            type(x) not in (int, float) or not math.isfinite(x) for x in value):
        raise ValueError(f"invalid_{name}")
    return [float(x) for x in value]


def _reference_state(reference):
    return {"quaternion": list(reference.frame.quaternion), "time_s": reference.time_s,
            "maximum_roll_rate_rad_s": reference.maximum_roll_rate_rad_s,
            "bridging": reference.bridging, "diagnostics": deepcopy(reference.diagnostics),
            "frame_diagnostics": deepcopy(reference.frame.diagnostics)}


def _clone_reference(reference, state, profile):
    if reference is None:
        return ConditionedGeographicFrame(state.q_body_to_eci, state.time_s,
            maximum_roll_rate_rad_s=profile["guidance"]["max_angular_acceleration_rad_s2"]/
            profile["guidance"]["attitude_frequency_rad_s"])
    if (not isinstance(reference, ConditionedGeographicFrame) or reference.time_s > state.time_s
            or not math.isfinite(reference.time_s)):
        raise ValueError("invalid_inherited_boostback_reference")
    return deepcopy(reference)


def _forecast_configuration(profile, development_transport_prepare_roll, *, body=None):
    if type(development_transport_prepare_roll) is not bool:
        raise ValueError("invalid_development_transport_prepare_roll")
    result = deepcopy(CONFIG)
    if development_transport_prepare_roll:
        body = vehicle(profile, "booster") if body is None else body
        tail = 4*max(engine.throttle_time_constant_s for engine in body.engines[:profile["booster"]["engine_count"]])
        result.update(prepare_roll_policy=TRANSPORT_PREPARE_POLICY,
            post_cutoff_control="powered_upright_prepare_with_transport_roll",
            shutdown_tail_model=TRANSPORT_TAIL_MODEL, shutdown_tail_s=tail)
    return result


def _local_state(state, profile):
    up, east, north, velocity, displacement, _ = _navigation(state, profile)
    observed_altitude = dyn.observe(state, vehicle(profile, "booster"))["altitude_m"]
    return ([-env.dot(displacement, east), -env.dot(displacement, north), observed_altitude],
            [env.dot(velocity, axis) for axis in (east, north, up)])


def boostback_macro_step(state, body, observed, profile, target_velocity_enu_mps, burn_axis_enu, *, maximum_step_s=.1):
    """Shorten a real integration interval near the measured impulse cutoff.

    Only current achieved forces are used. Coriolis, centrifugal and geographic
    frame transport terms convert inertial acceleration into the derivative of
    the Earth-relative ENU velocity used by the cutoff. The estimate does not
    clamp velocity or interpolate a state onto the requested threshold.
    """
    if type(maximum_step_s) not in (int, float) or not math.isfinite(maximum_step_s) or not 0 < maximum_step_s <= .1:
        raise ValueError("invalid_boostback_macro_step_cap")
    target = _vector(target_velocity_enu_mps, "target_velocity")
    axis = _vector(burn_axis_enu, "burn_axis")
    if abs(env.norm(axis)-1.) > 1e-8:
        raise ValueError("burn_axis_must_be_unit")
    mass = observed["mass_kg"]
    if type(mass) not in (int, float) or not math.isfinite(mass) or mass <= 0:
        raise ValueError("invalid_observed_boostback_mass")
    up, east, north, velocity, _, _ = _navigation(state, profile)
    local_v = [env.dot(velocity, vector) for vector in (east, north, up)]
    remaining = sum((target[i]-local_v[i])*axis[i] for i in range(3))
    force = dyn.rotate(state.q_body_to_eci, env.add(observed["thrust_force_body_n"], observed["aero_force_body_n"]))
    acceleration = env.add(env.scale(force, 1./mass), env.gravity_acceleration(state.r_eci_m))
    omega = (0., 0., env.EARTH_ROTATION_RAD_S)
    coriolis = env.scale(env.cross(omega, velocity), -2.)
    centrifugal = env.scale(env.cross(omega, env.cross(omega, state.r_eci_m)), -1.)
    sine, cosine = up[2], math.hypot(up[0], up[1])
    e2 = env.EARTH_ECCENTRICITY_SQUARED
    denominator = 1.-e2*sine*sine
    prime = env.EARTH_EQUATORIAL_RADIUS_M/math.sqrt(denominator)
    meridian = env.EARTH_EQUATORIAL_RADIUS_M*(1.-e2)/denominator**1.5
    altitude = observed["altitude_m"]
    transport_available = cosine > 1e-8 and prime+altitude > 0 and meridian+altitude > 0
    transport_correction = (0., 0., 0.)
    if transport_available:
        latitude_rate = local_v[1]/(meridian+altitude)
        longitude_rate = local_v[0]/((prime+altitude)*cosine)
        local_transport = [-latitude_rate, longitude_rate*cosine, longitude_rate*sine]
        transport = tuple(sum(local_transport[j]*a[j] for j in range(3)) for a in zip(east, north, up))
        transport_correction = env.scale(env.cross(transport, velocity), -1.)
    rotating = env.add(env.add(env.add(acceleration, coriolis), centrifugal), transport_correction)
    local_acceleration = [env.dot(rotating, vector) for vector in (east, north, up)]
    along_acceleration = sum(local_acceleration[i]*axis[i] for i in range(3))
    step = maximum_step_s
    excess = remaining-CONFIG["velocity_tolerance_mps"]
    if transport_available and excess > 0 and along_acceleration > 0:
        step = min(maximum_step_s, max(CONFIG["minimum_boostback_step_s"], excess/along_acceleration))
    step = float(step)
    return step, {"schema": "missionos.starship_boostback_macro_step.v1", "maximum_step_s": maximum_step_s,
        "step_s": step, "minimum_step_s": CONFIG["minimum_boostback_step_s"],
        "along_axis_velocity_error_mps": remaining, "achieved_along_axis_acceleration_mps2": along_acceleration,
        "geographic_transport_available": transport_available,
        "actual_state_assigned": False, "velocity_clamped": False, "prediction_is_execution": False}


def _forecast_boostback(initial, profile, catch_config, plan, *, reference=None, duration_s=None,
                       development_transport_prepare_roll=False):
    """Propagate one fixed command candidate, without refreshing its plan.

    Cutoff is followed by three-engine powered upright preparation. Actual
    attitude and body rate must reach the declared bounds before spool-down.
    It does not forecast entry steering or contact mechanics.
    """
    if type(development_transport_prepare_roll) is not bool:
        raise ValueError("invalid_development_transport_prepare_roll")
    state = deepcopy(initial) if isinstance(initial, dyn.State6DOF) else dyn.state_from_dict(deepcopy(initial))
    profile = deepcopy(profile)
    config = _configuration(profile)
    body = vehicle(profile, "booster")
    forecast_configuration = _forecast_configuration(profile, development_transport_prepare_roll, body=body)
    shutdown_tail_s = forecast_configuration["shutdown_tail_s"]
    dyn.observe(state, body)
    axis_local = _vector(plan["burn_axis_enu"], "burn_axis")
    target_velocity = _vector(plan["target_velocity_enu_mps"], "target_velocity")
    if abs(env.norm(axis_local)-1.) > 1e-8:
        raise ValueError("burn_axis_must_be_unit")
    duration = CONFIG["maximum_forecast_duration_s"] if duration_s is None else duration_s
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= CONFIG["maximum_forecast_duration_s"]:
        raise ValueError("invalid_short_forecast_duration")
    start = state.time_s
    if start+min(.1, duration) <= start:
        raise ValueError("short_forecast_clock_must_advance")
    frame = _clone_reference(reference, state, profile)
    original_reference = _reference_state(frame)
    phase, termination = "boostback", "time_limit"
    burn_start, settle_start, off_start, cutoff_basis = None, None, None, None
    steps, request_full_steps, request_center_steps, prepare_steps = 0, 0, 0, 0
    events = []
    while state.time_s < start+duration-1e-9:
        observed = dyn.observe(state, body)
        up, east, north, velocity, _, _ = _navigation(state, profile)
        local_v = [env.dot(velocity, axis) for axis in (east, north, up)]
        axis = env.unit(tuple(sum(axis_local[j]*a[j] for j in range(3)) for a in zip(east, north, up)))
        count, throttle = 0, 0.
        if phase == "boostback":
            along = sum((target_velocity[i]-local_v[i])*axis_local[i] for i in range(3))
            cut = (state.propellant_kg <= config["landing_reserve_kg"] or burn_start is not None and
                   (along <= CONFIG["velocity_tolerance_mps"] or state.time_s-burn_start >= config["boostback_max_burn_s"]))
            if cut:
                cutoff_basis = ("fuel_guard" if state.propellant_kg <= config["landing_reserve_kg"] else
                                "along_axis_impulse" if along <= CONFIG["velocity_tolerance_mps"] else "time_guard")
                phase, settle_start = "powered_prepare", state.time_s
                events.append({"event": "cutoff", "time_s": state.time_s, "state": asdict(state),
                               "cutoff_basis": cutoff_basis, "along_axis_velocity_error_mps": along})
            elif env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), axis) >= math.cos(math.radians(CONFIG["alignment_deg"])):
                if burn_start is None:
                    burn_start = state.time_s
                    events.append({"event": "ignition", "time_s": state.time_s, "state": asdict(state)})
                maximum = profile["booster"]["engine_count"]*profile["booster"]["engine_thrust_n"]
                count, throttle = _landing_engine_demand(state, body, maximum, 33)
                request_full_steps += 1
            else:
                count, throttle = 3, .4
                request_center_steps += 1
        if phase == "powered_prepare":
            axis, count, throttle = up, 3, .4
            tilt = math.degrees(math.acos(max(-1., min(1., env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), up)))))
            if tilt <= CONFIG["prepare_tilt_limit_deg"] and env.norm(state.omega_body_rad_s) < CONFIG["rate_settle_limit_rad_s"]:
                phase, off_start, count, throttle = "shutdown_tail", state.time_s, 0, 0.
                events.append({"event": "upright_prepared", "time_s": state.time_s, "state": asdict(state),
                               "actual_tilt_deg": tilt, "actual_body_rate_rad_s": env.norm(state.omega_body_rad_s)})
            elif state.time_s-settle_start > config["boostback_max_slew_s"]:
                termination = "upright_preparation_failed"
                break
            else:
                prepare_steps += 1
        if phase == "shutdown_tail":
            if development_transport_prepare_roll:
                tail_tilt = math.degrees(math.acos(max(-1., min(1., env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), up)))))
                tail_rate = env.norm(state.omega_body_rad_s)
                if (state.time_s-off_start >= shutdown_tail_s-1e-9 and tail_tilt <= CONFIG["prepare_tilt_limit_deg"]
                        and tail_rate < CONFIG["rate_settle_limit_rad_s"]):
                    termination = "shutdown_tail_complete"
                    events.append({"event": "transport_shutdown_tail_complete", "time_s": state.time_s,
                        "state": asdict(state), "actual_tilt_deg": tail_tilt, "actual_body_rate_rad_s": tail_rate,
                        "command_off_elapsed_s": state.time_s-off_start, "minimum_tail_s": shutdown_tail_s,
                        "geographic_roll_deferred": True})
                    break
                if state.time_s-settle_start > config["boostback_max_slew_s"]:
                    termination = "upright_preparation_failed"
                    break
            elif state.time_s-off_start >= shutdown_tail_s-1e-9:
                termination = "shutdown_tail_complete"
                break
            axis = up
        step = min(.1 if observed["altitude_m"] < 100000. or count else .25, start+duration-state.time_s)
        if development_transport_prepare_roll and phase == "shutdown_tail":
            step = min(.1, step)
        if phase == "boostback":
            step, _ = boostback_macro_step(state, body, observed, profile, target_velocity, axis_local, maximum_step_s=step)
        if phase == "shutdown_tail":
            if not development_transport_prepare_roll or state.time_s < off_start+shutdown_tail_s-1e-9:
                step = min(step, off_start+shutdown_tail_s-state.time_s)
        preferred = _attitude(axis, east)
        if development_transport_prepare_roll and phase in ("powered_prepare", "shutdown_tail"):
            target = frame.target(axis, east, preferred, time_s=state.time_s,
                force_bridge=True, defer_geographic_reacquisition=True)
        else:
            target = frame.target(axis, east, preferred, time_s=state.time_s)
        if phase == "shutdown_tail" and observed["dynamic_pressure_pa"] <= 100.:
            command, _ = control_coast_stopping_distance(state, body, target, profile, control_interval_s=step)
        else:
            command, _ = control_with_measured_tvc(state, body, target, throttle, count, profile, control_interval_s=step)
        if env.norm(state.omega_body_rad_s) > 5.:
            termination = "angular_rate_envelope_exceeded"
            break
        if observed["altitude_m"] < 300.:
            termination = "outside_short_forecast_altitude_envelope"
            break
        state = dyn.step(state, body, command, step)
        steps += 1
    endpoint_position, endpoint_velocity = _local_state(state, profile)
    result = {"termination": termination, "prediction_complete": termination == "shutdown_tail_complete",
        "duration_s": state.time_s-start, "integration_steps": steps, "cutoff_basis": cutoff_basis,
        "ignition_time_s": burn_start, "settle_start_time_s": settle_start, "shutdown_start_time_s": off_start,
        "preparation_start_time_s": settle_start, "requested_powered_prepare_steps": prepare_steps,
        "requested_full_engine_steps": request_full_steps, "requested_center_slew_steps": request_center_steps,
        "input_state": asdict(initial) if isinstance(initial, dyn.State6DOF) else deepcopy(initial),
        "input_reference": original_reference, "final_reference": _reference_state(frame),
        "final_state": asdict(state), "position_enu_m": endpoint_position, "velocity_enu_mps": endpoint_velocity,
        "propellant_kg": state.propellant_kg, "events": events, "prediction_is_execution": False,
        "actual_state_assigned": False, "production_policy_admitted": False,
        "arrival_forecast": False, "support_forecast": False}
    if development_transport_prepare_roll:
        result.update(development_transport_prepare_roll=True, prepare_roll_policy=TRANSPORT_PREPARE_POLICY,
                      shutdown_tail_model=TRANSPORT_TAIL_MODEL, shutdown_tail_s=shutdown_tail_s)
    return _saved(result)


def _candidate(seed, parameters):
    axis = np.asarray(seed["burn_axis_enu"], dtype=float)
    hint = np.asarray([1., 0., 0.]) if abs(axis[0]) < .8 else np.asarray([0., 1., 0.])
    first = hint-float(hint@axis)*axis
    first /= np.linalg.norm(first)
    second = np.cross(axis, first)
    tilted = axis+math.tan(parameters[0])*first+math.tan(parameters[1])*second
    tilted /= np.linalg.norm(tilted)
    target = np.asarray(seed["target_velocity_enu_mps"])+parameters[2]*tilted
    return {"burn_axis_enu": tilted.tolist(), "target_velocity_enu_mps": target.tolist()}


def _post_prepare_coast(forecast, profile, catch, seed):
    """One optimistic point continuation from the actual short endpoint.

    Its fixed-angle panel law omits finite attitude/actuator motion and powered
    arrest. It provides a trajectory-targeting objective, never arrival truth.
    """
    from .starship_constrained_guidance import point_coast
    if not forecast["prediction_complete"]:
        raise ValueError("point_continuation_requires_post_prepare_endpoint")
    fuel, dry = forecast["propellant_kg"], profile["booster"]["dry_mass_kg"]
    mass = dry+fuel
    coast = point_coast(forecast["position_enu_m"], forecast["velocity_enu_mps"], mass,
        vehicle(profile, "booster"), profile, full_panel_vector=True,
        bank_heading_enu=seed["bank_heading_enu"], bank_sign=seed["bank_sign"],
        entry_angle_deg=CONFIG["coast_entry_angle_deg"], terminal_altitude_m=CONFIG["coast_terminal_altitude_m"])
    speed = env.norm(coast["velocity_enu_mps"])
    exhaust = profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2
    after_arrest = mass*math.exp(-speed/exhaust)-dry
    horizon = ((catch["initial_pin_clearance_m"]+catch["arm_half_width_m"])/(-.5*catch["initial_vertical_speed_mps"])
               +4*profile["actuators"]["throttle_tau_s"])
    reserve = (dry+max(0., after_arrest))*9.81/exhaust*horizon
    available_dv = max(0., exhaust*math.log(mass/(dry+reserve)))
    arrest = _ideal_arrest_preview(coast, forecast, profile, catch)
    return _saved({"schema": "missionos.starship_post_prepare_point_coast.v1",
        "input_state_sha256": _digest(forecast["final_state"]),
        "input_position_enu_m": forecast["position_enu_m"], "input_velocity_enu_mps": forecast["velocity_enu_mps"],
        "input_propellant_kg": fuel, "input_mass_kg": mass,
        "bank_heading_enu": seed["bank_heading_enu"], "bank_sign": seed["bank_sign"],
        "prediction": coast, "ideal_arrest_preview": arrest, "ideal_fuel_budget": {"terminal_speed_mps": speed,
            "available_ideal_delta_v_mps": available_dv, "after_ideal_arrest_propellant_kg": after_arrest,
            "support_reserve_kg": reserve, "support_margin_kg": after_arrest-reserve,
            "gravity_loss_in_arrest_integrated": False, "finite_attitude_in_arrest_integrated": False,
            "prediction_is_execution": False},
        "prediction_is_execution": False, "arrival_admitted": False, "support_admitted": False})


def _ideal_arrest_preview(coast, forecast, profile, catch):
    """Finite local 3D engine/spool/gravity preview with ideal thrust attitude.

    The actual inherited engine availability is retained. Attitude, airloads,
    contact, terrain and the coast's omitted residual propellant use are not
    replayed, so this remains an explicitly optimistic guidance approximation.
    """
    p, v = list(coast["position_enu_m"]), list(coast["velocity_enu_mps"])
    dry, fuel = profile["booster"]["dry_mass_kg"], forecast["propellant_kg"]
    initial_position, initial_velocity, initial_fuel = list(p), list(v), fuel
    engines = forecast["final_state"]["engine_states"][:13]
    tau, rate = profile["actuators"]["throttle_tau_s"], profile["actuators"]["throttle_rate_s"]
    elapsed_coast = coast["elapsed_s"]
    throttle = []
    for engine in engines:
        initial = engine["throttle"]
        switch = max(0., (initial-rate*tau)/rate)
        value = max(0., initial-rate*elapsed_coast) if elapsed_coast <= switch else min(initial, rate*tau)*math.exp(-(elapsed_coast-switch)/tau)
        throttle.append(value if engine["available"] else 0.)
    thrust = profile["booster"]["engine_thrust_n"]
    exhaust = profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2
    response = profile.get("booster_return", {}).get("landing_velocity_tau_s", 1.5)
    elapsed, steps, reached, termination = 0., 0, False, "time_limit"
    minimum_clearance = catch["initial_pin_clearance_m"]+.5*catch["arm_half_width_m"]

    def required_height(remaining):
        com = (dry*profile["booster"]["dry_com_z_m"]+remaining*profile["booster"]["tank_center_z_m"])/(dry+remaining)
        return max(catch["support_height_m"]+minimum_clearance-point[2]+com for point in catch["support_points_body_m"])

    while elapsed < CONFIG["ideal_arrest_horizon_s"]-1e-9:
        floor = required_height(fuel)
        if p[2] < floor:
            termination = "capture_height_crossed"
            break
        if env.norm(v) <= CONFIG["ideal_arrest_terminal_speed_mps"]:
            reached, termination = True, "velocity_arrested_above_capture_floor"
            break
        if fuel <= 0:
            termination = "propellant_exhausted"
            break
        mass = dry+fuel
        gravity = env.EARTH_MU_M3_S2/(env.EARTH_EQUATORIAL_RADIUS_M+max(0., p[2]))**2
        requested = [-v[0]/response, -v[1]/response, gravity-v[2]/response]
        magnitude = env.norm(requested)
        axis = env.scale(requested, 1./max(magnitude, 1e-30))
        force_needed = mass*magnitude
        available, count = 0., 0
        for count in range(1, 14):
            if engines[count-1]["available"]:
                available += thrust
            if available >= force_needed:
                break
        target = max(.4, min(1., force_needed/max(available, 1.)))
        dt = min(CONFIG["ideal_arrest_dt_s"], CONFIG["ideal_arrest_horizon_s"]-elapsed)
        total = 0.
        for index, engine in enumerate(engines):
            command = target if index < count and engine["available"] else 0.
            after = max(0., min(1., throttle[index]+max(-rate, min(rate, (command-throttle[index])/tau))*dt))
            if engine["available"]:
                total += .5*(throttle[index]+after)*thrust
            throttle[index] = after
        total = min(total, fuel*exhaust/dt)
        acceleration = [total*x/mass for x in axis]
        acceleration[2] -= gravity
        for index in range(3):
            p[index] += v[index]*dt+.5*acceleration[index]*dt*dt
            v[index] += acceleration[index]*dt
        fuel = max(0., fuel-total/exhaust*dt)
        elapsed, steps = elapsed+dt, steps+1
    support_horizon = ((catch["initial_pin_clearance_m"]+catch["arm_half_width_m"])/(-.5*catch["initial_vertical_speed_mps"])
                       +4*profile["actuators"]["throttle_tau_s"])
    support_reserve = (dry+fuel)*9.81/exhaust*support_horizon
    return _saved({"schema": "missionos.starship_ideal_arrest_preview.v1",
        "input_position_enu_m": initial_position, "input_velocity_enu_mps": initial_velocity,
        "input_propellant_kg": initial_fuel, "engine_availability": [engine["available"] for engine in engines],
        "position_enu_m": p, "velocity_enu_mps": v, "propellant_kg": fuel,
        "consumed_propellant_kg": initial_fuel-fuel, "elapsed_s": elapsed, "integration_steps": steps,
        "required_capture_cg_height_m": required_height(fuel), "target_pin_clearance_m": minimum_clearance,
        "support_reserve_kg": support_reserve, "support_margin_kg": fuel-support_reserve,
        "termination": termination, "velocity_arrested_above_capture_floor": reached,
        "gravity_integrated": True, "finite_throttle_spool_integrated": True,
        "attitude_dynamics_integrated": False, "aerodynamic_force_integrated": False,
        "prediction_is_execution": False, "arrival_admitted": False, "support_admitted": False})


def _residual(forecast, endpoint_velocity, continuation, profile):
    fuel = forecast["propellant_kg"]
    guard = profile.get("booster_return", {}).get("landing_reserve_kg", 60000.)
    if continuation is None:
        trajectory = [0., 0., 0., 0.]
        missed_altitude = 0.
    else:
        coast, budget, arrest = continuation["prediction"], continuation["ideal_fuel_budget"], continuation["ideal_arrest_preview"]
        trajectory = [arrest["position_enu_m"][0]/CONFIG["terminal_position_residual_scale_m"],
                      arrest["position_enu_m"][1]/CONFIG["terminal_position_residual_scale_m"],
                      max(0., budget["terminal_speed_mps"]-budget["available_ideal_delta_v_mps"])/CONFIG["terminal_speed_residual_scale_mps"],
                      max(0., -arrest["support_margin_kg"])/CONFIG["fuel_residual_scale_kg"]]
        missed_altitude = 0. if coast["reached_terminal_altitude"] and arrest["velocity_arrested_above_capture_floor"] else CONFIG["incomplete_forecast_penalty"]
    vertical = (forecast["velocity_enu_mps"][2]-endpoint_velocity[2])/CONFIG["vertical_regularization_scale_mps"]
    return np.asarray([*trajectory, vertical, max(0., guard-fuel)/CONFIG["fuel_residual_scale_kg"],
                       0. if forecast["prediction_complete"] else CONFIG["incomplete_forecast_penalty"],
                       missed_altitude, CONFIG["incomplete_forecast_penalty"] if forecast["prediction_complete"] and continuation is None else 0.])


def refine_boostback_plan(state, profile, catch_config, seed_plan, *, reference=None,
                         development_transport_prepare_roll=False):
    """Return a bounded local candidate and its complete forecast-call receipt.

    ``least_squares`` is a local bounded search. Convergence, positive fuel or a
    completed short forecast is never admission to actual arrival/support.
    """
    if type(development_transport_prepare_roll) is not bool:
        raise ValueError("invalid_development_transport_prepare_roll")
    from scipy.optimize import least_squares

    initial = deepcopy(state) if isinstance(state, dyn.State6DOF) else dyn.state_from_dict(deepcopy(state))
    profile, catch = deepcopy(profile), catch_configuration(deepcopy(catch_config))
    forecast_configuration = _forecast_configuration(profile, development_transport_prepare_roll)
    seed = deepcopy(seed_plan)
    if type(seed) is not dict or seed.get("prediction_is_execution") is not False:
        raise ValueError("invalid_seed_point_plan")
    seed_original = _saved(seed)
    if any(seed.get(key, False) is not False for key in ("actual_state_assigned", "production_policy_admitted",
            "support_admitted", "physical_execution", "missionos_dispatch", "mission_completed")):
        raise ValueError("unsupported_seed_plan_claim")
    for key in ("burn_axis_enu", "target_velocity_enu_mps", "point_post_shutdown_velocity_enu_mps", "point_post_shutdown_position_enu_m"):
        seed[key] = _vector(seed.get(key), key)
    if abs(env.norm(seed["burn_axis_enu"])-1.) > 1e-8:
        raise ValueError("burn_axis_must_be_unit")
    bank = {}
    if "bank_heading_enu" in seed or "bank_sign" in seed:
        bearing = _vector(seed.get("bank_heading_enu"), "bank_heading")
        if (abs(env.norm(bearing)-1.) > 1e-8 or abs(bearing[2]) > 1e-8
                or type(seed.get("bank_sign")) is not int or seed["bank_sign"] not in (-1, 1)):
            raise ValueError("invalid_seed_held_bank_reference")
        bank = {"bank_heading_enu": bearing, "bank_sign": seed["bank_sign"]}
    if not bank:
        raise ValueError("post_prepare_coast_requires_frozen_bank_reference")
    input_state = _saved(asdict(initial))
    frame = _clone_reference(reference, initial, profile)
    input_reference = _saved(_reference_state(frame))
    entries, cache, attempted, completed, failed = [], {}, 0, 0, 0
    point_attempted, point_completed, point_failed = 0, 0, 0
    endpoint_velocity = seed["point_post_shutdown_velocity_enu_mps"]
    endpoint_position = seed["point_post_shutdown_position_enu_m"]

    def objective(parameters):
        nonlocal attempted, completed, failed, point_attempted, point_completed, point_failed
        key = tuple(float(x) for x in parameters)
        if key in cache:
            return cache[key]["residual"]
        if attempted >= CONFIG["maximum_forecast_calls"]:
            raise _BudgetExhausted("short_forecast_call_budget_exhausted")
        attempted += 1
        candidate = _candidate(seed, key)
        entry = {"attempt_index": attempted, "parameters": list(key), "candidate": candidate}
        entries.append(entry)
        try:
            if development_transport_prepare_roll:
                forecast = _forecast_boostback(initial, profile, catch, candidate, reference=frame,
                    development_transport_prepare_roll=True)
            else:
                forecast = _forecast_boostback(initial, profile, catch, candidate, reference=frame)
        except (ValueError, RuntimeError, ArithmeticError, np.linalg.LinAlgError) as exc:
            failed += 1
            entry.update(status="failed", error_type=type(exc).__name__)
            residual = np.full(9, CONFIG["incomplete_forecast_penalty"]*10.)
            cache[key] = {"residual": residual, "entry": entry}
            return residual
        completed += 1
        continuation, point_status = None, "not_attempted_incomplete_short_forecast"
        if forecast["prediction_complete"]:
            point_attempted += 1
            try:
                continuation = _post_prepare_coast(forecast, profile, catch, seed)
            except (ValueError, RuntimeError, ArithmeticError, np.linalg.LinAlgError) as exc:
                point_failed += 1
                point_status = "failed"
                entry["point_error_type"] = type(exc).__name__
            else:
                point_completed += 1
                point_status = "completed"
        residual = _residual(forecast, endpoint_velocity, continuation, profile)
        entry.update(status="completed", forecast=forecast, point_status=point_status, point_continuation=continuation,
                     residual=residual.tolist(), objective=float(residual@residual))
        cache[key] = {"residual": residual, "entry": entry}
        return residual

    status, solver, solver_error_type = "not_started", None, None
    angles = math.radians(CONFIG["angular_offset_bound_deg"])
    bounds = ([-angles, -angles, -CONFIG["cut_projection_offset_bound_mps"]],
              [angles, angles, CONFIG["cut_projection_offset_bound_mps"]])
    objective(np.zeros(3))

    def jacobian(parameters):
        # A clocked cutoff is piecewise constant over machine-epsilon changes
        # in its scalar. Declared finite probes span actuator/integration steps;
        # all derivative forecasts count against the same hard call budget.
        base = objective(parameters)
        matrix = np.empty((len(base), 3))
        increments = [CONFIG["angle_difference_step_rad"], CONFIG["angle_difference_step_rad"],
                      CONFIG["cut_difference_step_mps"]]
        for index, increment in enumerate(increments):
            if parameters[index]+increment > bounds[1][index]:
                increment = -increment
            probe = np.array(parameters, dtype=float, copy=True)
            probe[index] += increment
            matrix[:, index] = (objective(probe)-base)/increment
        return matrix

    try:
        solver = least_squares(objective, np.zeros(3), bounds=bounds,
            max_nfev=CONFIG["maximum_solver_nfev"], x_scale=[angles, angles, CONFIG["cut_projection_offset_bound_mps"]],
            jac=jacobian, ftol=1e-3, xtol=1e-3, gtol=1e-3)
        status = "solver_stopped"
    except _BudgetExhausted:
        status = "forecast_budget_exhausted"
    except (ValueError, RuntimeError, ArithmeticError, np.linalg.LinAlgError) as exc:
        status, solver_error_type = "solver_error", type(exc).__name__
    receipt = {"schema": TRANSPORT_FORECAST_SCHEMA if development_transport_prepare_roll else FORECAST_SCHEMA,
        "configuration": forecast_configuration,
        "input_state": input_state, "input_state_sha256": _digest(input_state), "input_reference": input_reference,
        "seed_point_plan_sha256": _digest(seed_original), "endpoint_target_velocity_enu_mps": endpoint_velocity,
        "endpoint_target_position_enu_m": endpoint_position, "attempted_forecast_count": attempted,
        "completed_forecast_count": completed, "failed_forecast_count": failed, "forecasts": entries,
        "point_continuation_attempted_count": point_attempted, "point_continuation_completed_count": point_completed,
        "point_continuation_failed_count": point_failed, "point_continuation_is_execution": False,
        "optimizer_status": status, "solver_error_type": solver_error_type,
        "solver_success": bool(solver is not None and solver.success),
        "solver_nfev": int(solver.nfev) if solver is not None else None,
        "prediction_is_execution": False, "actual_state_assigned": False, "production_policy_admitted": False,
        "support_admitted": False, "physical_execution": False, "missionos_dispatch": False,
        "limitations": ["No plan refresh is executed inside a short candidate forecast.",
                        "Three-engine upright preparation ends on actual tilt/rate bounds or the configured slew timeout.",
                        "Shutdown-tail attitude is local upright; descent-entry steering is omitted.",
                        "Tail is 1.4 seconds and retains residual thrust rather than setting actuators to zero.",
                        "One full-panel point coast starts from each completed actual post-prepare short forecast.",
                        "The terminal objective uses predicted miss and optimistic ideal-arrest fuel, not observed future trial states.",
                        "Only weak post-prepare vertical-speed regularization uses the seed point plan.",
                        "Reserve penalties do not prove sufficient fuel for subsequent entry, arrest or catch.",
                        "Local solver termination is not convergence or trajectory-feasibility certification."]}
    if development_transport_prepare_roll:
        receipt["development_transport_prepare_roll"] = True
        receipt["limitations"][2:4] = [
            "Powered upright preparation and shutdown tail transport the carried roll reference without geographic reacquisition.",
            "The command-off tail lasts four maximum main-engine throttle time constants and retains finite residual thrust."]
    valid = [entry for entry in entries if entry["status"] == "completed"]
    if not valid:
        raise ForecastUnavailable(_saved(receipt))
    complete = [entry for entry in valid if entry["forecast"]["prediction_complete"]
                and entry["point_status"] == "completed" and entry["point_continuation"]["prediction"]["reached_terminal_altitude"]
                and entry["point_continuation"]["ideal_arrest_preview"]["velocity_arrested_above_capture_floor"]]
    selected = min(complete or valid, key=lambda entry: entry["objective"])
    receipt.update(selected_attempt_index=selected["attempt_index"], selected_forecast=selected["forecast"],
                   selected_forecast_complete=selected["forecast"]["prediction_complete"],
                   selected_objective=selected["objective"], selected_point_continuation=selected["point_continuation"],
                   selected_point_continuation_complete=selected in complete,
                   parameter_bound_active=[abs(abs(selected["parameters"][i])-bounds[1][i]) <= 1e-6 for i in range(3)],
                   global_infeasibility_established=False)
    return _saved({**selected["candidate"], **bank, "method": "bounded_short_finite_6dof_boostback_shooting",
        "prediction_is_execution": False, "production_policy_admitted": False, "admissible": False,
        "seed_point_plan": seed_original, "actual_dynamics_prediction": receipt})

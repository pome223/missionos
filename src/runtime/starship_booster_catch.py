"""Unidentified two-support terminal catch mechanics, integrated in six DOF.

This terminal campaign starts near a configured tower unless a caller supplies
an exact state. It does not certify launch-to-catch guidance or actual hardware.
Support loads are finite compliant contact forces, never a pose/velocity clamp.
"""
from __future__ import annotations

from dataclasses import asdict
import math

from . import starship_physics as env
from . import starship_sixdof as dyn

SCENARIOS = ("booster_catch", "tower_unavailable", "lateral_offset", "fast_descent", "one_support")
SCHEMA = "missionos.starship_booster_catch.v1"
NUMERIC = {
    "support_height_m", "arm_open_half_span_m", "arm_closed_half_span_m", "arm_speed_mps",
    "arm_half_width_m", "arm_half_length_m", "normal_stiffness_npm", "normal_damping_ns_pm",
    "tangential_damping_ns_pm", "friction_coefficient", "maximum_support_force_n",
    "maximum_compression_m", "settle_time_s", "settle_pin_speed_mps", "settle_body_rate_rad_s",
    "settle_engine_thrust_n", "initial_pin_clearance_m", "initial_propellant_kg",
    "terminal_position_tau_s", "terminal_velocity_tau_s", "terminal_max_tilt_deg",
    "divert_north_offset_m", "integration_dt_s", "duration_s",
}


def configuration(value):
    if type(value) is not dict or set(value) != NUMERIC | {
            "schema", "profile_id", "claim", "support_points_body_m", "initial_vertical_speed_mps"}:
        raise ValueError("invalid_catch_configuration_fields")
    if value["schema"] != "missionos.starship_catch_configuration.v1":
        raise ValueError("invalid_catch_configuration_schema")
    if any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) or value[k] <= 0 for k in NUMERIC):
        raise ValueError("invalid_catch_configuration_number")
    speed = value["initial_vertical_speed_mps"]
    points = value["support_points_body_m"]
    if (type(speed) not in (int, float) or not math.isfinite(speed) or speed >= 0
            or type(points) is not list or len(points) != 2
            or any(type(p) is not list or len(p) != 3 or any(type(x) not in (int, float) or not math.isfinite(x) for x in p) for p in points)
            or not points[0][0] < 0 < points[1][0]
            or not value["arm_open_half_span_m"] > value["arm_closed_half_span_m"]
            or value["integration_dt_s"] > .01 or value["duration_s"] > 30
            or value["terminal_max_tilt_deg"] >= 30 or value["settle_time_s"] < 2):
        raise ValueError("invalid_catch_configuration_bounds")
    return {**value, "support_points_body_m": [list(p) for p in points]}


def _subtract(a, b):
    return env.add(a, env.scale(b, -1.))


def _frame(profile, time_s):
    origin = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], time_s=time_s)
    up, east, north = env.local_frame(origin)
    return origin, (east, north, up)


def _enu(vector, axes):
    return tuple(env.dot(vector, axis) for axis in axes)


def _eci(vector, axes):
    return tuple(sum(vector[j]*axes[j][i] for j in range(3)) for i in range(3))


def _arm(config, elapsed, authorized):
    travel = min(config["arm_open_half_span_m"]-config["arm_closed_half_span_m"],
                 config["arm_speed_mps"]*max(0., elapsed)) if authorized else 0.
    span = config["arm_open_half_span_m"]-travel
    rate = -config["arm_speed_mps"] if authorized and span > config["arm_closed_half_span_m"]+1e-12 else 0.
    return span, rate


def contact_frame(state, booster, profile, config, start_time, *, authorized=True, missing_support=False,
                  support_armed=(False, False), return_site=None):
    """Current material-point kinematics and unilateral spring/damper loads."""
    if return_site is not None:
        from .starship_return_sites import validate_return_site
        validate_return_site(return_site, profile, config, terminal_goal="surrogate_pin_support")
    observed = dyn.observe(state, booster)
    origin, axes = _frame(profile, state.time_s)
    span, rate = _arm(config, state.time_s-start_time, authorized)
    force_body, torque_body, pins = (0., 0., 0.), (0., 0., 0.), []
    for index, point in enumerate(config["support_points_body_m"]):
        lever = _subtract(point, observed["com_body_m"])
        world = env.add(state.r_eci_m, dyn.rotate(state.q_body_to_eci, lever))
        velocity = env.add(state.v_eci_mps, dyn.rotate(state.q_body_to_eci,
            _subtract(env.cross(state.omega_body_rad_s, lever), observed["com_rate_body_mps"])))
        relative = _subtract(velocity, env.cross((0., 0., env.EARTH_ROTATION_RAD_S), world))
        position, velocity_enu = _enu(_subtract(world, origin.r), axes), _enu(relative, axes)
        sign = -1. if index == 0 else 1.
        arm_velocity = (sign*rate, 0., 0.)
        local_velocity = _subtract(velocity_enu, arm_velocity)
        footprint = (authorized and not (missing_support and index == 1)
                     and abs(position[0]-sign*span) <= config["arm_half_width_m"]
                     and abs(position[1]) <= config["arm_half_length_m"])
        penetration = max(0., config["support_height_m"]-position[2])
        normal = max(0., config["normal_stiffness_npm"]*penetration-config["normal_damping_ns_pm"]*local_velocity[2]) if footprint and support_armed[index] and penetration > 0 else 0.
        tangent = env.scale((local_velocity[0], local_velocity[1], 0.), -config["tangential_damping_ns_pm"])
        limit = config["friction_coefficient"]*normal
        tangent = env.scale(tangent, min(1., limit/max(env.norm(tangent), 1e-30)))
        force = env.add(tangent, (0., 0., normal))
        body_force = dyn.inverse_rotate(state.q_body_to_eci, _eci(force, axes))
        force_body = env.add(force_body, body_force)
        torque_body = env.add(torque_body, env.cross(lever, body_force))
        pins.append({"id": index, "position_body_m": list(point), "position_enu_m": list(position),
                     "relative_velocity_enu_mps": list(local_velocity), "arm_velocity_enu_mps": list(arm_velocity),
                     "footprint_active": bool(footprint), "top_contact_eligible": bool(support_armed[index]), "penetration_m": penetration,
                     "normal_force_n": normal, "force_enu_n": list(force)})
    total_force = env.add(dyn.rotate(state.q_body_to_eci, env.add(env.add(observed["thrust_force_body_n"], observed["aero_force_body_n"]), force_body)),
                          env.scale(env.gravity_acceleration(state.r_eci_m), observed["mass_kg"]))
    total_torque = env.add(env.add(observed["thrust_torque_body_nm"], observed["aero_torque_body_nm"]),
                           env.add(torque_body, observed["gravity_gradient_torque_body_nm"]))
    return {"time_s": state.time_s, "r_eci_m": list(state.r_eci_m), "v_eci_mps": list(state.v_eci_mps),
            "q_body_to_eci": list(state.q_body_to_eci), "omega_body_rad_s": list(state.omega_body_rad_s),
            "propellant_kg": state.propellant_kg, "mass_kg": observed["mass_kg"],
            "com_body_m": list(observed["com_body_m"]), "com_rate_body_mps": list(observed["com_rate_body_mps"]),
            "inertia_kg_m2": observed["inertia_kg_m2"],
            "arm_half_span_m": span, "arm_rate_mps": rate,
            "engine_thrust_n": sum(e["thrust_n"] for e in observed["engine_loads"]),
            "main_engine_thrust_n": sum(e["thrust_n"] for e in observed["engine_loads"] if "main" in e["name"]),
            "force_body_n": list(force_body), "torque_body_nm": list(torque_body),
            "total_force_eci_n": list(total_force), "total_torque_body_nm": list(total_torque),
            "eligibility": {"sampled_at_s": state.time_s, "valid_until_s": state.time_s+.05,
                            "rules_catch_allowed": authorized, "tower_ready": authorized, "vehicle_ready": True}, "pins": pins}


def _initialized(profile, config, scenario):
    from .starship_sixdof_mission import _attitude, vehicle
    booster = vehicle(profile, "booster")
    fuel = config["initial_propellant_kg"]
    props = dyn.mass_properties(booster, fuel)
    altitude = config["support_height_m"]+config["initial_pin_clearance_m"]-config["support_points_body_m"][0][2]+props.com_body_m[2]
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], altitude, fuel, time_s=600.)
    up, east, north = env.local_frame(point)
    # Body x is east, so the two configured supports straddle the tower arms.
    q = _attitude(up, east)
    speed = -15. if scenario == "fast_descent" else config["initial_vertical_speed_mps"]
    position = env.add(point.r, env.scale(north, 12. if scenario == "lateral_offset" else 0.))
    gravity = env.EARTH_MU_M3_S2/env.norm(point.r)**2
    hover = props.mass_kg*gravity/(2*booster.engines[0].max_thrust_n)
    engines = tuple(dyn.EngineState(throttle=hover if i < 2 else 0.) for i in range(len(booster.engines)))
    state = dyn.State6DOF(point.time_s, position, env.add(point.v, env.scale(up, speed)), q,
                         dyn.inverse_rotate(q, (0., 0., env.EARTH_ROTATION_RAD_S)), fuel,
                         engines, tuple(0. for _ in booster.aero_panels))
    return booster, state


def simulate_catch(profile, catch_config, scenario="booster_catch", initial_state=None, duration_s=None, dt_s=None,
                   control_policy="fixed_v1"):
    from .starship_sixdof_booster import _landing_engine_demand
    from .starship_sixdof_contact import find_contact
    from .starship_sixdof_mission import _attitude, _sample, control, vehicle
    config = configuration(catch_config)
    if control_policy not in ("fixed_v1", "net_thrust_trim_v1"):
        raise ValueError("unknown_catch_control_policy")
    public_scenario = scenario
    if scenario.startswith("booster_catch_"):
        scenario = scenario[len("booster_catch_"):]
    if scenario not in SCENARIOS:
        raise ValueError("unknown_catch_scenario")
    duration = config["duration_s"] if duration_s is None else duration_s
    dt = config["integration_dt_s"] if dt_s is None else dt_s
    if (any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (duration, dt))
            or duration > 30 or dt > .01 or duration/dt > 99_999):
        raise ValueError("invalid_catch_integration_bounds")
    if initial_state is None:
        booster, state = _initialized(profile, config, scenario)
    else:
        booster, state = vehicle(profile, "booster"), dyn.state_from_dict(initial_state)
        dyn.observe(state, booster)
    start = state.time_s
    if start+dt <= start or start+duration <= start:
        raise ValueError("invalid_catch_integration_bounds")
    authorized = scenario != "tower_unavailable"
    frames, samples, events = [], [], []
    contacted, overload, stroke_exceeded, supported = False, False, False, False
    settled, peak_load, peak_compression = 0., 0., 0.
    termination, ground_contact, previous = "time_limit", None, None
    next_sample = start
    support_armed = (False, False)
    events.append({"time_s": start, "event": "catch_terminal_start", "detail": "Supplied exact state" if initial_state is not None else "Independent initialized terminal campaign, not launch-to-catch evidence."})
    if not authorized:
        events.append({"time_s": start, "event": "catch_not_authorized_divert", "detail": "Tower unavailable; arms stay open and finite flight control requests the configured north offset."})
    while True:
        frame = contact_frame(state, booster, profile, config, start, authorized=authorized, missing_support=scenario == "one_support", support_armed=support_armed)
        armed = tuple(bool(p["footprint_active"] and (support_armed[i] or p["position_enu_m"][2] >= config["support_height_m"])) for i, p in enumerate(frame["pins"]))
        if armed != support_armed:
            support_armed = armed
            frame = contact_frame(state, booster, profile, config, start, authorized=authorized, missing_support=scenario == "one_support", support_armed=support_armed)
        # Crossing receipt is descriptive; contact forces come from penetration
        # throughout small finite steps, never from switching into a rigid weld.
        if previous is not None:
            for before, after in zip(previous["pins"], frame["pins"]):
                if before["position_enu_m"][2] > config["support_height_m"] >= after["position_enu_m"][2]:
                    events.append({"time_s": state.time_s, "event": "support_plane_crossing", "pin_id": after["id"],
                                   "time_bracket_s": [previous["time_s"], state.time_s],
                                   "footprint_active": after["footprint_active"], "detection": "small_step_endpoint_crossing"})
        loads = [p["normal_force_n"] for p in frame["pins"]]
        compression = [p["penetration_m"] for p in frame["pins"] if p["normal_force_n"] > 0]
        peak_load = max(peak_load, *loads)
        peak_compression = max(peak_compression, *compression) if compression else peak_compression
        overload = overload or any(f > config["maximum_support_force_n"] for f in loads)
        stroke_exceeded = stroke_exceeded or any(x > config["maximum_compression_m"] for x in compression)
        if any(f > 0 for f in loads) and not contacted:
            contacted = True
            events.append({"time_s": state.time_s, "event": "first_support_contact_engine_cutoff", "detail": "Shutdown commanded; finite throttle decay continues in the six-DOF integrator."})
        stable = (all(f > 0 for f in loads) and not overload and not stroke_exceeded
                  and all(env.norm(p["relative_velocity_enu_mps"]) <= config["settle_pin_speed_mps"] for p in frame["pins"])
                  and env.norm(state.omega_body_rad_s) <= config["settle_body_rate_rad_s"]
                  and frame["engine_thrust_n"] <= config["settle_engine_thrust_n"])
        settled = settled+(state.time_s-previous["time_s"]) if stable and previous is not None and previous["settle_eligible"] else 0.
        frame.update(settle_eligible=bool(stable), settle_elapsed_s=settled)
        frames.append(frame)
        if settled >= config["settle_time_s"]-1e-9:
            supported, termination = True, "supported_settled"
        elif overload:
            termination = "support_overload"
        elif stroke_exceeded:
            termination = "support_stroke_exceeded"
        if termination != "time_limit" or state.time_s >= start+duration-1e-9:
            break
        origin, axes = _frame(profile, state.time_s)
        east, north, up = axes
        midpoint = tuple(sum(p["position_enu_m"][i] for p in frame["pins"])/2 for i in range(3))
        velocity = _enu(_subtract(state.v_eci_mps, env.cross((0., 0., env.EARTH_ROTATION_RAD_S), state.r_eci_m)), axes)
        offset = config["divert_north_offset_m"] if not authorized else (12. if scenario == "lateral_offset" else 0.)
        omega = 1/config["terminal_position_tau_s"]
        horizontal = (-omega*omega*midpoint[0]-2*omega*velocity[0],
                      omega*omega*(offset-midpoint[1])-2*omega*velocity[1], 0.)
        g = env.EARTH_MU_M3_S2/env.norm(state.r_eci_m)**2
        vertical = max(.1, g+(config["initial_vertical_speed_mps"]-velocity[2])/config["terminal_velocity_tau_s"])
        max_horizontal = vertical*math.tan(math.radians(config["terminal_max_tilt_deg"]))
        horizontal = env.scale(horizontal, min(1., max_horizontal/max(env.norm(horizontal), 1e-30)))
        desired = _eci(env.add(horizontal, (0., 0., vertical)), axes)
        target = _attitude(env.unit(desired), east)
        trim = None
        if control_policy == "net_thrust_trim_v1" and not contacted:
            # Asymmetric active engines require gimbals to cancel moment. The
            # resulting force is not generally along body +Z. Compensate that
            # observed direction in the attitude REQUEST, never in the state,
            # applied forces or support geometry. Finite tracking still applies.
            force = dyn.observe(state, booster)["thrust_force_body_n"]
            if env.norm(force) > 1.:
                trim = dyn.quaternion_from_two_vectors(env.unit(force), (0., 0., 1.))
                target = dyn.normalize_quaternion(dyn.quaternion_multiply(target, trim))
        alignment = env.dot(dyn.rotate(state.q_body_to_eci, (0., 0., 1.)), env.unit(desired))
        count, throttle = (0, 0.) if contacted else _landing_engine_demand(state, booster, frame["mass_kg"]*env.norm(desired)/max(.2, alignment), 3)
        command, diagnostic = control(state, booster, target, throttle, count, profile, use_flaps=True)
        if trim is not None:
            diagnostic["net_thrust_trim_quaternion"] = list(trim)
        if contacted:
            # Once supported, engine/RCS actuation cannot hold the booster up or
            # artificially stabilize it; all engine commands remain off.
            command = dyn.Command6DOF(tuple(dyn.EngineCommand(enabled=False) for _ in booster.engines), command.flap_angles_rad)
        phase = "catch_support_response" if contacted else "catch_terminal_approach" if authorized else "catch_divert"
        if state.time_s >= next_sample-1e-9:
            samples.append({**_sample(state, booster, phase, command, diagnostic), "body_id": "booster"})
            next_sample = state.time_s+.1
        step_dt = min(dt, start+duration-state.time_s)
        # Fixed support forces over this <=10ms step are an explicit numerical
        # approximation; convergence is checked with half-step terminal runs.
        after = dyn.step(state, booster, command, step_dt, external_force_body_n=tuple(frame["force_body_n"]), external_torque_body_nm=tuple(frame["torque_body_nm"]))
        # Ground collision is distinct from support contact. Full hull detection
        # is used only near ground, preserving failed approach trajectories.
        if dyn.observe(after, booster)["altitude_m"] < profile["geometry"]["booster_length_m"]:
            from .starship_sixdof_contact import hull_clearance
            if hull_clearance(after, booster, profile["geometry"]["booster_length_m"], profile["geometry"]["radius_m"])["signed_clearance_m"] <= 0:
                # No catch force is present at this low altitude in the supplied
                # geometry; retain the actual state rather than project it.
                if any(loads):
                    termination = "ground_overlap_with_support"
                    state = after
                    break
                state, ground_contact = find_contact(state, booster, command, step_dt, profile["geometry"]["booster_length_m"], profile["geometry"]["radius_m"])
                termination = "surface_contact"
                break
        previous, state = frame, after
    if frames[-1]["time_s"] != state.time_s:
        frame = contact_frame(state, booster, profile, config, start, authorized=authorized, missing_support=scenario == "one_support", support_armed=support_armed)
        frame.update(settle_eligible=False, settle_elapsed_s=0.)
        frames.append(frame)
        settled = 0.
    final = {**_sample(state, booster, "catch_support_response" if contacted else "catch_terminal_approach" if authorized else "catch_divert"), "body_id": "booster"}
    if not samples or samples[-1]["time_s"] != state.time_s:
        samples.append(final)
    else:
        samples[-1] = final
    events.append({"time_s": state.time_s, "event": termination, "detail": "Terminal mechanics result; no real catch or hardware validation inferred."})
    record = {"schema": SCHEMA, "configuration": config,
              "control_policy": control_policy,
              "initialization": {"kind": "terminal_initialized" if initial_state is None else "supplied_state", "launch_connected": False},
              "scenario": scenario, "start_time_s": start, "integration_dt_s": dt, "requested_duration_s": duration,
              "eligibility": {"tower_ready": authorized, "vehicle_ready": True, "catch_authorized": authorized},
              "missing_support": scenario == "one_support", "frames": frames,
              "settling": {"required_s": config["settle_time_s"], "observed_s": settled},
              "peak_support_force_n": peak_load, "peak_compression_m": peak_compression,
              "support_overload": overload, "support_stroke_exceeded": stroke_exceeded,
              "contact_response_modeled": True, "structural_deformation_validated": False,
              "complete_swept_volume_test": False, "catch_verified": False, "physical_execution": False}
    return {"scenario": public_scenario, "body_id": "booster", "samples": samples, "events": events,
            "initial_state": samples[0], "final_state": asdict(state), "final_vehicle": asdict(booster),
            "catch_record": record, "contact": ground_contact,
            "outcome": {"termination": termination, "phase": final["phase"], "duration_s": state.time_s-start,
                        "start_time_s": start, "end_time_s": state.time_s,
                        "orbit_gate_reached": False, "payload_released_count": 0,
                        "max_altitude_m": max(s["altitude_m"] for s in samples),
                        "max_body_rate_rad_s": max(env.norm(s["omega_body_rad_s"]) for s in samples),
                        "max_attitude_error_deg": max(s.get("controller", {}).get("attitude_error_deg", 0.) for s in samples),
                        "final_ground_speed_mps": final["ground_speed_mps"], "final_altitude_m": final["altitude_m"],
                        "mission_completed": False, "physical_execution_invoked": False,
                        "integration_steps": len(frames)-1, "six_dof_integrated": True, "attitude_prescribed": False,
                        "simulated_catch_supported": supported, "catch_verified": False, "physical_execution": False,
                        "starship_vehicle_validated": False, "landing_hardware_validated": False},
            "limitations": ["Independent initialized terminal campaign unless exact state is supplied; not launch-to-catch evidence.",
                            "Two configured surrogate supports, unilateral compliant loads and bounded friction; not V3 CAD or SpaceX hardware identification.",
                            "Point-support footprint contact with small-step crossing logs, not full swept hull/arm/tower collision geometry.",
                            "No structural deformation, tower flexibility, propellant slosh, thermal damage or catch hardware certification."]}

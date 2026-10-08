"""Development 6DOF launch/return harness. Guidance is an exposed surrogate.

This does not call the point-mass flight simulator or prescribe a time history
of vehicle attitude. Guidance requests an orientation; bounded gimbals, jets and
flap actuators must produce the moments that attain it. Failed flights remain
failed. Vehicle coefficients and this guidance have not been identified against
SpaceX data. See the explicit profile and research/coverage inventory.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import math

import numpy as np

from . import starship_physics as env
from . import starship_sixdof as dyn
from .starship_sixdof_separation import attach_payload, release_payload
from .starship_ship_return import ReturnController, control_interval, record_sample, advance_plant
from .starship_flight_supervision import FlightSupervision
from .starship_retained_return import CONDITIONED_POLICY_ID, TRIMMED_POLICY_ID, new_record
from .starship_wind import wind_from_profile


def _vec(v):
    return tuple(float(x) for x in v)


def _matrix(m):
    return tuple(_vec(row) for row in m)


def _parallel(r):
    r = np.asarray(r)
    return np.eye(3)*np.dot(r, r)-np.outer(r, r)


def _combine(components):
    """Mass, centroid, tensor from masses/centroids/centroidal tensors."""
    mass = sum(m for m, _, _ in components)
    com = sum(m*np.asarray(c) for m, c, _ in components)/mass
    inertia = sum(np.asarray(i)+m*_parallel(np.asarray(c)-com) for m, c, i in components)
    return mass, _vec(com), _matrix(inertia)


def _cylinder(mass, radius, length):
    return np.diag([mass*(3*radius**2+length**2)/12]*2+[mass*radius**2/2])


def payload_inertia(profile):
    x, y, z = profile["payload"]["dimensions_m"]
    mass = profile["payload"]["mass_each_kg"]
    return _matrix(np.diag([mass*(y*y+z*z)/12, mass*(x*x+z*z)/12, mass*(x*x+y*y)/12]))


def point_state(s):
    return env.State3D(s.time_s, s.r_eci_m, s.v_eci_mps, s.propellant_kg)


def bound_orbit_above(orbit, minimum_perigee_m):
    """An escape trajectory can have high perigee but is not a deploy orbit."""
    return orbit.get("status") == "bound" and orbit["perigee_altitude_m"] >= minimum_perigee_m


def _attitude(z, x_reference):
    """Full target frame, not a state assignment. Matrix -> unit quaternion."""
    z = np.asarray(env.unit(_vec(z)))
    x = np.asarray(x_reference)-np.dot(x_reference, z)*z
    if np.linalg.norm(x) < 1e-6:
        reference = np.eye(3)[int(np.argmin(np.abs(z)))]
        x = reference-np.dot(reference, z)*z
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    m = np.column_stack([x, y, z])
    # Largest-diagonal algorithm avoids singularity near 180 degrees.
    t = float(np.trace(m))
    if t > 0:
        k = math.sqrt(1+t)*2
        q = (k/4, (m[2, 1]-m[1, 2])/k, (m[0, 2]-m[2, 0])/k, (m[1, 0]-m[0, 1])/k)
    else:
        i = int(np.argmax(np.diag(m)))
        j, k = (i+1) % 3, (i+2) % 3
        f = math.sqrt(1+m[i, i]-m[j, j]-m[k, k])*2
        qv = [0.0]*3
        qv[i], qv[j], qv[k] = f/4, (m[j, i]+m[i, j])/f, (m[k, i]+m[i, k])/f
        q = ((m[k, j]-m[j, k])/f, *qv)
    return dyn.normalize_quaternion(q)


def vehicle(profile, kind="ship", payload_count=None):
    p, a, g = profile[kind], profile["actuators"], profile["geometry"]
    radius, length = g["radius_m"], g[f"{kind}_length_m"]
    count = profile["payload"]["count"] if payload_count is None else payload_count
    dry_tensor = p.get("dry_inertia_kg_m2")
    pieces = [(p["dry_mass_kg"], (0, 0, p["dry_com_z_m"]), _cylinder(p["dry_mass_kg"], radius, length) if dry_tensor is None else dry_tensor)]
    dry_mass, com, inertia = _combine(pieces)
    engines = []
    n = p["engine_count"]
    for i in range(n):
        engines.append(dyn.Engine(
            name=f"{kind}_main_{i}", position_body_m=tuple(p["engine_positions_body_m"][i]),
            max_thrust_n=p["vacuum_thrust_n"] if kind == "ship" and i >= 3 else p["engine_thrust_n"],
            isp_s=p["engine_isp_s"], max_gimbal_rad=math.radians(a["max_gimbal_deg"]) if i < p["gimbal_engine_count"] else 0,
            throttle_time_constant_s=a["throttle_tau_s"], throttle_rate_per_s=a["throttle_rate_s"],
            gimbal_time_constant_s=a["gimbal_tau_s"], gimbal_rate_rad_s=math.radians(a["gimbal_rate_deg_s"])))
    # Six signed torque channels, each a real equal/opposite pair. This is an
    # explicit generic RCS design, NOT a recovered Starship RCS specification.
    for axis in range(3):
        lever_axis, force_axis = (axis+1) % 3, (axis+2) % 3
        for sign in (-1, 1):
            for side in (-1, 1):
                pos, direction = [0., 0., p["dry_com_z_m"]], [0., 0., 0.]
                pos[lever_axis] += side*a["rcs_radius_m"]
                direction[force_axis] = side*sign
                engines.append(dyn.Engine(f"rcs_{axis}_{sign}_{side}", tuple(pos), a["rcs_thrust_n"], a["rcs_isp_s"],
                                          min_throttle=0, max_gimbal_rad=0, direction_body=tuple(direction),
                                          throttle_time_constant_s=a["throttle_tau_s"], gimbal_time_constant_s=a["gimbal_tau_s"]))
    aero = profile["aero"]
    panels = []
    # Three bidirectional plates supply an anisotropic generic hull model.
    # CP and coefficient laws are assumptions, not aerodynamic identification.
    for axis in range(3):
        normal = tuple(float(j == axis) for j in range(3))
        panels.append(dyn.AeroPanel(f"hull_{axis}", (0., 0., p["hull_cp_z_m"]), normal,
                                   math.pi*radius**2 if axis == 2 else radius*2*length,
                                   aero["axial_cd"] if axis == 2 else aero["crossflow_cd"], 0.0,
                                   max_deflection_rad=0, deflection_time_constant_s=a["flap_tau_s"]))
    for spec in p["flap_panels" if kind == "ship" else "grid_fin_panels"]:
        panels.append(dyn.AeroPanel(**spec, normal_coefficient=aero["panel_normal_coefficient"],
                                   tangential_coefficient=aero["panel_tangential_coefficient"],
                                   max_deflection_rad=math.radians(a["flap_limit_deg" if kind == "ship" else "grid_fin_limit_deg"]),
                                   deflection_time_constant_s=a["flap_tau_s"],
                                   deflection_rate_rad_s=math.radians(a["flap_rate_deg_s"])))
    result = dyn.Vehicle6DOF(dry_mass, com, inertia, p["propellant_kg"], (0., 0., p["tank_center_z_m"]), radius,
                            p["tank_length_m"], tuple(engines), tuple(panels), wind=wind_from_profile(profile))
    if kind == "ship":
        for _ in range(count):
            result = attach_payload(result, profile["payload"]["mass_each_kg"], (0., 0., profile["payload"]["center_z_m"]), payload_inertia(profile))
    return result


def stack_vehicle(profile, ship, booster):
    sp = dyn.mass_properties(ship, profile["ship"]["propellant_kg"])
    offset = np.array([0, 0, profile["geometry"]["booster_length_m"]])
    dry, com, inertia = _combine([(booster.dry_mass_kg, booster.dry_com_body_m, booster.dry_inertia_kg_m2),
                                 (sp.mass_kg, np.asarray(sp.com_body_m)+offset, sp.inertia_kg_m2)])
    # Ship hull/fin panels remain at their locations in the attached stack.
    panels = booster.aero_panels+tuple(replace(p, name="upper_"+p.name, position_body_m=env.add(p.position_body_m, tuple(offset)))
                                      for p in ship.aero_panels)
    return replace(booster, dry_mass_kg=dry, dry_com_body_m=com, dry_inertia_kg_m2=inertia, aero_panels=panels)


def control(s, v, target_q, main_throttle, main_count, profile, *, use_flaps=False,
            development_fin_allocation=False, control_interval_s=.1, trim_angles_rad=None,
            development_fin_policy="finite_regularized_fins_v1",
            development_entry_preposition=False,
            reference_rate_body_rad_s=None, reference_acceleration_body_rad_s=None):
    """PD + bounded moment allocation. No external torque injected into flight."""
    props = dyn.mass_properties(v, s.propellant_kg)
    inertia = np.asarray(props.inertia_kg_m2)
    qinv = (s.q_body_to_eci[0], *(-x for x in s.q_body_to_eci[1:]))
    error = dyn.quaternion_multiply(qinv, target_q)
    if error[0] < 0:
        error = tuple(-x for x in error)
    g = profile["guidance"]
    w = np.asarray(s.omega_body_rad_s)
    tracking_diagnostic = None
    if reference_rate_body_rad_s is None:
        if reference_acceleration_body_rad_s is not None:
            raise ValueError("reference_acceleration_requires_rate")
        # Preserve historical default arithmetic and diagnostics exactly.
        acc = 2*g["attitude_frequency_rad_s"]**2*np.asarray(error[1:])-2*g["attitude_damping_ratio"]*g["attitude_frequency_rad_s"]*w
        acc = np.clip(acc, -g["max_angular_acceleration_rad_s2"], g["max_angular_acceleration_rad_s2"])
    else:
        from .starship_reference_tracking import tracking_acceleration
        acc, tracking_diagnostic = tracking_acceleration(error[1:], s.omega_body_rad_s,
            reference_rate_body_rad_s, (0., 0., 0.) if reference_acceleration_body_rad_s is None
            else reference_acceleration_body_rad_s, profile)
        acc = np.asarray(acc)
    wanted = inertia@acc+np.cross(w, inertia@w)
    obs = dyn.observe(s, v)
    if tracking_diagnostic is not None:
        tracking_diagnostic.update(requested_rigid_body_torque_body_nm=wanted.tolist(),
            actual_aero_torque_body_nm=list(obs["aero_torque_body_nm"]),
            pre_fin_torque_body_nm=(wanted-np.asarray(obs["aero_torque_body_nm"])).tolist())
    wanted -= np.asarray(obs["aero_torque_body_nm"])
    flap_targets = list(s.flap_angles_rad)
    fin_diagnostic = None
    if type(development_fin_allocation) is not bool:
        raise ValueError("invalid_development_fin_allocation")
    if type(development_entry_preposition) is not bool or development_entry_preposition and not development_fin_allocation:
        raise ValueError("invalid_entry_preposition")
    if (type(development_fin_policy) is not str or development_fin_policy not in
            ("finite_regularized_fins_v1", "finite_moment_priority_fins_v1") or
            development_fin_policy != "finite_regularized_fins_v1" and not development_fin_allocation):
        raise ValueError("invalid_development_fin_policy")
    if development_fin_allocation and use_flaps and obs["dynamic_pressure_pa"] > 50:
        from .starship_fin_allocation import allocate_fins
        flap_targets, wanted, fin_diagnostic = allocate_fins(s, v, wanted, obs, profile,
            interval_s=control_interval_s, trim_angles_rad=trim_angles_rad, policy_id=development_fin_policy)
    elif development_entry_preposition and use_flaps and trim_angles_rad is not None:
        from .starship_fin_allocation import actuator_endpoint
        if (len(trim_angles_rad) != len(v.aero_panels) or
                any(not math.isfinite(x) or abs(x) > panel.max_deflection_rad+1e-10
                    for x, panel in zip(trim_angles_rad, v.aero_panels))):
            raise ValueError("invalid_entry_preposition_reference")
        flap_targets = list(trim_angles_rad)
        indices = [i for i, panel in enumerate(v.aero_panels) if panel.name.startswith("flap_")]
        fin_diagnostic = {"schema": "missionos.starship_entry_preposition.v1", "surface_family": "ship_flap",
            "policy_id": "finite_moment_priority_fins_v1", "mode": "low_pressure_preposition",
            "fin_indices": indices, "command_angles_rad": [flap_targets[i] for i in indices],
            "interval_s": control_interval_s,
            "predicted_endpoint_angles_rad": [actuator_endpoint(s.flap_angles_rad[i], flap_targets[i],
                v.aero_panels[i].deflection_time_constant_s, v.aero_panels[i].deflection_rate_rad_s, control_interval_s) for i in indices],
            "prediction_is_execution": False, "actual_state_assigned": False, "production_policy_admitted": False}
    elif use_flaps and obs["dynamic_pressure_pa"] > 50:
        # Local actuator effectiveness at the CURRENT flow and attitude.
        # This numerical Jacobian requests real bounded flap angles; the
        # integrator still applies rate/lag and recomputes forces each RK stage.
        flap_columns, flap_indices = [], []
        for i, panel in enumerate(v.aero_panels):
            if not panel.name.startswith(("flap_", "grid_fin_")):
                continue
            angle = s.flap_angles_rad[i]
            delta = .01 if angle+.01 < panel.max_deflection_rad else -.01
            values = list(s.flap_angles_rad)
            values[i] += delta
            probe = dyn.observe(replace(s, flap_angles_rad=tuple(values)), v)
            flap_columns.append((np.asarray(probe["aero_torque_body_nm"])-obs["aero_torque_body_nm"])/delta)
            flap_indices.append(i)
        if flap_columns:
            fm = np.asarray(flap_columns).T
            changes = np.linalg.lstsq(fm, wanted, rcond=1e-4)[0]
            clipped = []
            for change, i in zip(changes, flap_indices):
                limit = v.aero_panels[i].max_deflection_rad
                target = float(np.clip(flap_targets[i]+change, -limit, limit))
                clipped.append(target-flap_targets[i])
                flap_targets[i] = target
            wanted -= fm@np.asarray(clipped)
    # Small-angle allocation uses physical engine lever arms, then clips the
    # achieved commands. Saturation is not replaced by an ideal torque.
    columns, channels = [], []
    fixed = np.zeros(3)
    for i, e in enumerate(v.engines):
        if i >= main_count or e.name.startswith("rcs_") or not s.engine_states[i].available:
            continue
        thrust = e.max_thrust_n*main_throttle
        lever = np.asarray(e.position_body_m)-props.com_body_m
        fixed += np.cross(lever, [0, 0, thrust])
        if e.max_gimbal_rad and thrust > 0:
            for axis, derivative in ((0, [0, -thrust, 0]), (1, [thrust, 0, 0])):
                columns.append(np.cross(lever, derivative))
                channels.append((i, axis))
    commands = [dyn.EngineCommand(i < main_count and main_throttle > 0 and not e.name.startswith("rcs_"),
                                  main_throttle if i < main_count and not e.name.startswith("rcs_") else 0)
                for i, e in enumerate(v.engines)]
    remaining = wanted-fixed
    if columns:
        matrix = np.asarray(columns).T
        u = np.linalg.lstsq(matrix, remaining, rcond=1e-9)[0]
        clipped = []
        for x, (i, axis) in zip(u, channels):
            x = float(np.clip(x, -v.engines[i].max_gimbal_rad, v.engines[i].max_gimbal_rad))
            clipped.append(x)
            commands[i] = replace(commands[i], **{("gimbal_x_rad" if axis == 0 else "gimbal_y_rad"): x})
        remaining -= matrix@np.asarray(clipped)
    capacity = 2*profile["actuators"]["rcs_radius_m"]*profile["actuators"]["rcs_thrust_n"]
    for i, e in enumerate(v.engines):
        if e.name.startswith("rcs_"):
            _, axis, sign, _ = e.name.split("_")
            demand = max(0., min(1., float(remaining[int(axis)])*int(sign)/capacity))
            commands[i] = dyn.EngineCommand(demand > 1e-8, demand if demand > 1e-8 else 0)
    return dyn.Command6DOF(tuple(commands), tuple(flap_targets)), {
        "target_q_body_to_eci": target_q, "requested_torque_body_nm": wanted.tolist(),
        "attitude_error_deg": math.degrees(2*math.acos(min(1., abs(error[0])))),
        "control_allocation": "bounded_linearized_flap_gimbal_plus_physical_jet_pairs",
        **({"reference_tracking_control": tracking_diagnostic} if tracking_diagnostic is not None else {}),
        **({"development_fin_allocation": fin_diagnostic} if fin_diagnostic is not None else {})}


def _split(s, old, first, second, second_origin_body, second_propellant):
    """Rigid separation with inherited pose/rates and momentum diagnostics."""
    p0 = dyn.mass_properties(old, s.propellant_kg)
    p1 = dyn.mass_properties(first, s.propellant_kg)
    p2 = dyn.mass_properties(second, second_propellant)
    children = []
    for index, (p, v, origin, fuel) in enumerate(((p1, first, (0., 0., 0.), s.propellant_kg), (p2, second, second_origin_body, second_propellant))):
        offset = env.add(env.add(origin, p.com_body_m), env.scale(p0.com_body_m, -1))
        r = env.add(s.r_eci_m, dyn.rotate(s.q_body_to_eci, offset))
        velocity = env.add(s.v_eci_mps, dyn.rotate(s.q_body_to_eci, env.cross(s.omega_body_rad_s, offset)))
        old_engines = {e.name: a for e, a in zip(old.engines, s.engine_states)} if index == 0 else {}
        old_flaps = {(e.name.removeprefix("upper_") if index else e.name): a for e, a in zip(old.aero_panels, s.flap_angles_rad)}
        children.append(dyn.State6DOF(s.time_s, r, velocity, s.q_body_to_eci, s.omega_body_rad_s, fuel,
                                       tuple(old_engines.get(e.name, dyn.EngineState()) for e in v.engines),
                                       tuple(old_flaps.get(e.name, 0.) for e in v.aero_panels)))
    p_error = np.zeros(3)
    h_after = np.zeros(3)
    for child, props in zip(children, (p1, p2)):
        p_error += props.mass_kg*(np.asarray(child.v_eci_mps)-s.v_eci_mps)
        h_after += np.asarray(dyn.rotate(s.q_body_to_eci, _vec(np.asarray(props.inertia_kg_m2)@s.omega_body_rad_s)))
        h_after += np.cross(np.asarray(child.r_eci_m)-s.r_eci_m, props.mass_kg*(np.asarray(child.v_eci_mps)-s.v_eci_mps))
    h_before = dyn.rotate(s.q_body_to_eci, _vec(np.asarray(p0.inertia_kg_m2)@s.omega_body_rad_s))
    return (*children, {"mass_residual_kg": p1.mass_kg+p2.mass_kg-p0.mass_kg,
                        "linear_momentum_residual_kg_mps": float(np.linalg.norm(p_error)),
                        "angular_momentum_residual_kg_m2_s": float(np.linalg.norm(h_after-h_before)),
                        "separation_impulse_modeled": False, "hot_staging_plume_modeled": False})


def _sample(s, v, phase, command=None, diagnostics=None):
    o = dyn.observe(s, v, command)
    mains = [x for x in o["engine_loads"] if "main" in x["name"]]
    thrust = sum(x["thrust_n"] for x in mains)
    result = {"time_s": s.time_s, "phase": phase, "body_id": "ship", "r_eci_m": list(s.r_eci_m),
            "v_eci_mps": list(s.v_eci_mps), "q_body_to_eci": list(s.q_body_to_eci),
            "omega_body_rad_s": list(s.omega_body_rad_s), "propellant_kg": s.propellant_kg,
            "altitude_m": o["altitude_m"], "ground_speed_mps": o.get("ground_speed_mps", o["air_speed_mps"]), "com_z_m": o["com_body_m"][2],
            "com_rate_body_mps": list(o["com_rate_body_mps"]),
            "mass_kg": o["mass_kg"], "inertia_kg_m2": o["inertia_kg_m2"], "dynamic_pressure_pa": o["dynamic_pressure_pa"],
            "applied_thrust_n": float(thrust), "engine_count": int(sum(x["thrust_n"] > 1000 for x in mains)),
            "throttle": max((a.throttle for a, e in zip(s.engine_states, v.engines) if "main" in e.name), default=0),
            "engine_states": [asdict(e) for e in s.engine_states], "flap_angles_rad": list(s.flap_angles_rad),
            "aero_panel_names": [panel.name for panel in v.aero_panels],
            "main_engine_anchors": [e.position_body_m for e in v.engines if "main" in e.name],
            "main_engine_thrust_n": [e["thrust_n"] for e in mains],
            "thrust_torque_body_nm": o["thrust_torque_body_nm"], "aero_torque_body_nm": o["aero_torque_body_nm"],
            "controller": diagnostics or {}, "command": asdict(command) if command else None}
    if v.wind is not None and v.wind.active:
        result.update({key: o[key] for key in ("air_speed_mps", "wind_applied", "wind_enu_mps", "wind_eci_mps",
                                              "air_velocity_body_mps", "air_density_kg_m3")})
    return result


def simulate(profile, *, scenario="launch", duration_s=None, dt_scale=1.0, supervision=None, return_policy="fixed_v1",
             booster_policy="fixed_v1", catch_config=None, mission_director=None, mission_case=None, return_sites=None, splashdown_goal=None,
             _development_return_qualification=None):
    """Run continuous 6DOF. Short initialized cases are clearly separate flights."""
    if scenario not in ("launch", "engine_out", "entry_perturbation", "gimbal_step", "flap_asymmetry", "deployment_no_effect", "terminal_engine_out"):
        raise ValueError("unknown 6DOF scenario")
    if booster_policy not in ("fixed_v1", "predictive_return_v1"):
        raise ValueError("unknown booster policy")
    if booster_policy != "fixed_v1":
        if scenario != "launch" or dt_scale != 1.0:
            raise ValueError("launch-connected recovery requires launch with the fixed integration scale")
        from .starship_booster_catch import configuration
        configuration(catch_config)
    if supervision is not None and (scenario != "deployment_no_effect" or not isinstance(supervision, FlightSupervision)):
        raise ValueError("supervision is only supported by deployment_no_effect")
    if scenario == "deployment_no_effect" and supervision is None:
        supervision = FlightSupervision(None, "standalone-hold")
    if mission_case not in (None, "normal", "release_fault", "fuel_shortage", "tower_unavailable", "operations_notice"):
        raise ValueError("unknown mission management case")
    if mission_director is not None and (mission_case is None or scenario != "launch" or booster_policy != "fixed_v1" or return_policy != "fixed_v1"):
        raise ValueError("mission director requires its explicit launch case")
    if mission_director is not None and mission_director.envelope.get("splashdown_goal") != (splashdown_goal.to_dict() if splashdown_goal is not None else None):
        raise ValueError("unapproved_splashdown_goal")
    development = _development_return_qualification
    registered_return = False
    qualification_identity = None
    if return_policy == TRIMMED_POLICY_ID and development is None and mission_director is None:
        from .starship_return_feasibility import certificate_ready
        qualification, qualification_identity = certificate_ready(profile)
        if qualification is None:
            raise ValueError("state_return_policy_not_qualified")
        registered_return = True
    if development is not None and (type(development) is not dict
            or set(development) != {"release_limit", "bounded_ship_flaps", "application"}
            or development["application"] not in ("retained_policy_active_only_v1", "wind_trim_state_return_v2")
            or type(development["release_limit"]) is not int
            or not 0 <= development["release_limit"] <= profile["payload"]["count"]
            or type(development["bounded_ship_flaps"]) is not bool
            or development["application"] == "wind_trim_state_return_v2" and not development["bounded_ship_flaps"]
            or scenario != "launch" or return_policy != (TRIMMED_POLICY_ID if development["application"] == "wind_trim_state_return_v2" else CONDITIONED_POLICY_ID) or dt_scale != 1.
            or any(x is not None for x in (supervision, mission_director, mission_case, return_sites, splashdown_goal, catch_config))
            or booster_policy != "fixed_v1"):
        raise ValueError("invalid_development_return_qualification")
    if development is not None:
        development = dict(development)
    return_record = new_record(return_policy)
    if return_policy != "fixed_v1" and scenario != "deployment_no_effect" and development is None and not registered_return:
        raise ValueError("retained return policy requires deployment_no_effect")
    return_controller = None
    if not math.isfinite(dt_scale) or not 0 < dt_scale <= 2:
        raise ValueError("dt_scale must be in (0,2]")
    p, g = profile, profile["guidance"]
    ship, booster = vehicle(p), vehicle(p, "booster")
    v = stack_vehicle(p, ship, booster) if scenario in ("launch", "engine_out", "deployment_no_effect", "terminal_engine_out") else ship
    phase = "stack_ascent" if scenario in ("launch", "engine_out", "deployment_no_effect", "terminal_engine_out") else scenario
    initial = p["perturbation_initial_state"]
    initial_altitude = dyn.mass_properties(v, v.propellant_capacity_kg).com_body_m[2]+p["launch"]["release_base_altitude_m"] if phase == "stack_ascent" else initial["altitude_m"]
    base = env.surface_state(p["launch"]["latitude_deg"], p["launch"]["longitude_deg"], initial_altitude, v.propellant_capacity_kg)
    up, east, north = env.local_frame(base)
    q = _attitude(up, north)
    fuel = v.propellant_capacity_kg
    velocity, omega = base.v, dyn.inverse_rotate(q, (0., 0., env.EARTH_ROTATION_RAD_S))
    if phase != "stack_ascent":
        fuel = initial["propellant_kg"]
        velocity = env.add(base.v, env.add(env.scale(east, initial["east_speed_mps"]), env.scale(up, initial["vertical_speed_mps"])))
        q = _attitude(east, north)
        omega = tuple(math.radians(x) for x in initial["body_rate_deg_s"])
    s = dyn.State6DOF(0., base.r, velocity, q, omega, fuel,
                      tuple(dyn.EngineState(throttle=1. if phase == "stack_ascent" and p["launch"]["main_engines_initially_spooled"] and "main" in e.name else 0.) for e in v.engines),
                      tuple(0. for _ in v.aero_panels))
    limit = float(duration_s if duration_s is not None else p["integration"]["max_duration_s"] if phase == "stack_ascent" else 30.)
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("duration must be finite positive")
    events, samples, steps = [], [], 0
    satellites = []
    max_altitude, max_rate, max_error, released = initial_altitude, 0., 0., 0
    stage_state, orbit_time, next_release = None, None, None
    return_time, failed_engine, termination = None, False, "time_limit"
    contact_receipt = None
    sequencer_state, release_attempts, release_acknowledged = None, 0, False
    next_sample = 0.
    deployment_hold_until, reassessment_due, diagnostic_due = None, None, None
    mechanism_status, managed_return_selected = "not_collected", False
    fuel_fault_applied = False
    return_observation_started = None
    return_halt_started = None
    return_execution_checks = []
    def director_observation():
        from .starship_mission_director import fuel_sensor
        # This proxy is distinct from the plant log. The provider gets bounded
        # sensor values, command inventory and tool outputs, never mass/inertia,
        # engine truth, scenario identity or future faults. Fuel is quantized.
        row = {"time_s": s.time_s, "phase": "orbital_coast", "released_count": released,
            "release_acknowledged": release_acknowledged, "sequencer_state": sequencer_state or "running",
            "fuel_kg": fuel_sensor(s.propellant_kg, s.time_s, "ship"), "return_deadline_s": return_time,
            "hold_expires_at_s": deployment_hold_until,
            "tower_ready": mission_case != "tower_unavailable",
            "numerical_tools": {"orbit_release_feasible": bool(bound_orbit_above(orbit, g["target_perigee_m"]-1000)
                and o["dynamic_pressure_pa"] < 1 and env.norm(s.omega_body_rad_s) < g["release_max_rate_rad_s"]),
                "retained_payload_present": released < p["payload"]["count"],
                "capture_corridor_certified": False, "mechanism_status": mechanism_status},
            "operations_notice": "Payload operations: suspend remaining deployment; return with the remaining manifest."
                if mission_case == "operations_notice" and released >= 1 else ""}
        from .starship_return_feasibility import evaluate
        row["numerical_tools"]["return_feasibility"] = evaluate(p, s, released, row["fuel_kg"])
        return row
    def event(name, detail="", **fields):
        events.append({"time_s": s.time_s, "event": name, "detail": detail, **fields})
    event("initial_state", "surface release, main engines already spooled" if phase == "stack_ascent" else "independent initialized atmospheric test; not a continuation of launch")
    if development is not None:
        event("development_return_qualification", "explicit local experiment; not a MissionOS grant", **development)
    while s.time_s < limit-1e-9:
        if supervision is not None:
            supervision.pace(s.time_s)
        if mission_director is not None:
            mission_director.pace("ship", s.time_s)
        ps = point_state(s)
        o = dyn.observe(s, v)
        altitude = o["altitude_m"]
        up, east, north = env.local_frame(ps)
        radial = env.dot(s.v_eci_mps, up)
        tangent_vec = env.add(s.v_eci_mps, env.scale(up, -radial))
        tangent = env.unit(tangent_vec) if env.norm(tangent_vec) > 1 else east
        orbit = env.orbital_elements(ps)
        target, throttle, count = _attitude(tangent, north), 0., 0
        if phase == "stack_ascent":
            t = s.time_s
            program = g["pitch_program_time_deg"]
            pitch = float(np.interp(t, [x[0] for x in program], [x[1] for x in program]))
            azimuth = math.radians(p["launch"]["azimuth_deg"])
            heading = env.add(env.scale(east, math.sin(azimuth)), env.scale(north, math.cos(azimuth)))
            direction = env.add(env.scale(up, math.sin(math.radians(pitch))), env.scale(heading, math.cos(math.radians(pitch))))
            target, throttle, count = _attitude(direction, north), .7 if o["dynamic_pressure_pa"] > g["max_q_pa"] else 1., 33
            if scenario == "engine_out" and t >= 60 and not failed_engine:
                states = list(s.engine_states)
                states[3] = replace(states[3], available=False, throttle=0.)
                s, failed_engine = replace(s, engine_states=tuple(states)), True
                event("asymmetric_engine_failure", "booster main 3; state fault, no compensating force")
            if s.propellant_kg <= p["booster"]["separation_reserve_kg"]:
                samples.append(_sample(s, v, phase))
                bs, ss, receipt = _split(s, v, booster, ship, (0., 0., p["geometry"]["booster_length_m"]), p["ship"]["propellant_kg"])
                stage_state = asdict(bs)
                event("stage_separation", "mass/linear/angular momentum checked; instantaneous rigid split", **receipt)
                s, v, phase = ss, ship, "ship_ascent"
                continue
        elif phase == "ship_ascent":
            tangential_speed = env.norm(tangent_vec)
            desired_vr = max(-50., min(800., (g["target_altitude_m"]-altitude)/g["ascent_altitude_response_s"]))
            radial_acc = env.EARTH_MU_M3_S2/env.norm(s.r_eci_m)**2-tangential_speed**2/env.norm(s.r_eci_m)+(desired_vr-radial)/g["ascent_velocity_response_s"]
            available_acc = sum(e.max_thrust_n for e in v.engines if "main" in e.name)/o["mass_kg"]
            fraction = max(-.25, min(.95, radial_acc/available_acc))
            direction = env.add(env.scale(up, fraction), env.scale(tangent, math.sqrt(1-fraction*fraction)))
            target, throttle, count = _attitude(direction, north), 1., 6
            if bound_orbit_above(orbit, g["target_perigee_m"]):
                phase, orbit_time, next_release = "orbital_coast", s.time_s, s.time_s+g["orbit_release_delay_s"]
                return_time = s.time_s+g["coast_before_return_s"]
                event("orbit_cutoff_command", "measured osculating perigee threshold; shutdown spool still evolves", orbit=orbit)
                continue
            if s.propellant_kg <= p["ship"]["ascent_reserve_kg"] or s.time_s >= g["max_ascent_s"]:
                phase = "ballistic_return"
                event("orbit_not_reached", "ascent reserve/time limit; no state projection to target orbit", orbit=orbit)
                continue
        elif phase == "orbital_coast":
            if return_observation_started is not None and s.time_s >= return_observation_started+30.:
                termination = "return_inhibited_unresolved"
                event(termination, "30 seconds of real integration; no return or recovery claimed")
                break
            if return_halt_started is not None and s.time_s > return_halt_started:
                termination = "no_qualified_return_policy"
                event(termination, "no new return burn; physical recovery unresolved")
                break
            if mission_case == "fuel_shortage" and not fuel_fault_applied:
                # Explicit synthetic reservoir-loss fault. No measured SpaceX
                # leak, vent recoil or thermal model is claimed.
                lost = max(0., s.propellant_kg-24000.)
                s = replace(s, propellant_kg=s.propellant_kg-lost)
                fuel_fault_applied = True
                event("synthetic_propellant_loss", "reservoir loss without modeled vent recoil", lost_propellant_kg=lost)
            if mission_director is not None:
                if deployment_hold_until is not None and s.time_s >= deployment_hold_until:
                    sequencer_state, next_release = "skipped", None
                    event("managed_hold_expired", "preapproved hold expiry disables further release")
                    deployment_hold_until = None
                    reassessment_due = None
                if diagnostic_due is not None and s.time_s >= diagnostic_due:
                    mechanism_status = "blocked" if mission_case == "release_fault" else "clear"
                    diagnostic_due = None
                    event("managed_mechanism_diagnostic", "separate synthetic actuator/latch status channel; two seconds elapsed", mechanism_status=mechanism_status)
                row = director_observation()
                mission_director.confirm("ship", row)
                pending = mission_director.pending.get("ship")
                if pending is not None:
                    point = pending["point"]
                elif "deployment_start" not in mission_director.finished:
                    point = "deployment_start"
                elif (reassessment_due is not None and s.time_s >= reassessment_due
                      and "deployment_reassessment" not in mission_director.finished):
                    point = "deployment_reassessment"
                elif "deployment_monitor" not in mission_director.finished and (released >= 1 or sequencer_state == "inhibited"):
                    point = "deployment_monitor"
                elif (mechanism_status != "not_collected" and "deployment_diagnostic" not in mission_director.finished):
                    point = "deployment_diagnostic"
                elif not managed_return_selected and s.time_s >= return_time-90.:
                    point = "return_selection"
                else:
                    point = None
                # Resolve start or its preapproved continuation before the
                # first release slot, rather than holding past that slot.
                deadline = next_release-p["integration"]["coast_dt_s"] if point == "deployment_start" and next_release is not None else None
                if point is not None and point.startswith("deployment"):
                    bounds = [return_time]
                    if deadline is not None:
                        bounds.append(deadline)
                    if deployment_hold_until is not None:
                        bounds.append(deployment_hold_until)
                    deadline = min(bounds)
                action = mission_director.update(point, row, decision_deadline_s=deadline) if point else None
                if action is not None:
                    event("managed_mission_command", "independently checked preapproved mission decision", point=point, action=action)
                    if action == "stop_deployment":
                        sequencer_state, next_release = "skipped", None
                        deployment_hold_until, reassessment_due = None, None
                    elif action == "hold":
                        sequencer_state = "held"
                        deployment_hold_until = s.time_s+mission_director.envelope["maximum_hold_s"]
                        reassessment_due = s.time_s+mission_director.envelope["hold_reassessment_after_s"]
                    elif action == "collect_status":
                        # The command consumes time and suppresses release until
                        # the distinct mechanism channel has produced a report.
                        diagnostic_due = s.time_s+2.
                        if sequencer_state not in ("inhibited", "skipped"):
                            sequencer_state = "held"
                    elif action == "continue" and sequencer_state == "held":
                        sequencer_state = "running"
                        next_release = max(next_release or s.time_s, s.time_s)
                        deployment_hold_until, reassessment_due = None, None
                        event("managed_deployment_resumed", "checked resume of a held sequence; interlocks remain latched")
                    elif action == "state_return":
                        managed_return_selected = True
                        return_policy = TRIMMED_POLICY_ID
                        return_record = new_record(return_policy)
                        qualification_identity = mission_director.envelope["return_qualification_sha256"]
                        registered_return = True
                    elif action in ("inhibit_return", "halt_unresolved_return"):
                        managed_return_selected = True
                        return_observation_started = s.time_s if action == "inhibit_return" else None
                        return_halt_started = s.time_s if action == "halt_unresolved_return" else None
                        return_policy, return_record = "fixed_v1", new_record("fixed_v1")
                    if point == "return_selection":
                        witness = _sample(s, v, phase)
                        witness["return_execution_observation"] = row
                        samples.append(witness)
            if mission_case is not None and mission_director is None and s.propellant_kg < p["ship"]["return_reserve_kg"]:
                sequencer_state, next_release = "skipped", None
            # Coast dynamics continue while the first decision is pending;
            # the timeline cannot release a payload before that decision.
            deployment_started = mission_director is None or "deployment_start" in mission_director.finished
            if development is not None and released >= development["release_limit"]:
                next_release = None
            if deployment_started and next_release is not None and s.time_s >= next_release and sequencer_state not in ("inhibited", "skipped", "held"):
                # Independent composition/impulse verifier checks conservation.
                if bound_orbit_above(orbit, g["target_perigee_m"]-1000) and o["dynamic_pressure_pa"] < 1 and env.norm(s.omega_body_rad_s) < g["release_max_rate_rad_s"]:
                    if scenario == "deployment_no_effect" and release_attempts == 0 or mission_case == "release_fault":
                        release_attempts, release_acknowledged, sequencer_state = release_attempts+1, True, "inhibited"
                        event("payload_release_attempt_acknowledged", "synthetic accepted release command without physical separation; not a SpaceX fault model",
                              release_attempt_count=release_attempts, payload_released_count=released)
                        event("deployment_interlock_inhibited", "automatic local interlock prevents further release attempts after missing separation effect")
                        next_release = s.time_s+p["payload"]["interval_s"]
                    else:
                        remaining = p["payload"]["count"]-released-1
                        after = vehicle(p, payload_count=remaining)
                        child_mass = p["payload"]["mass_each_kg"]
                        parent_mass = dyn.mass_properties(after, s.propellant_kg).mass_kg
                        reduced_mass = parent_mass*child_mass/(parent_mass+child_mass)
                        samples.append(_sample(s, v, phase))
                        s, child, child_vehicle, receipt = release_payload(s, v, after, child_mass,
                            (0., 0., p["payload"]["center_z_m"]), payload_inertia(p),
                            (reduced_mass*p["payload"]["release_speed_mps"], 0., 0.))
                        v, released = after, released+1
                        if mission_case is not None:
                            release_attempts, release_acknowledged = release_attempts+1, True
                        samples.append(_sample(s, v, phase))
                        satellites.append({"id": f"satellite_{released:02d}", "release_state": asdict(child), "vehicle": asdict(child_vehicle), "receipt": receipt})
                        event("payload_released", f"finite rigid payload {released}; +J / -J with angular reaction", receipt=receipt)
                        next_release = s.time_s+p["payload"]["interval_s"] if remaining else None
                else:
                    event("deployment_held", "perigee / dynamic pressure / measured angular rate gate")
                    next_release = s.time_s+p["payload"]["interval_s"]
            if supervision is not None and sequencer_state is not None:
                telemetry = {"time_s": s.time_s, "phase": phase, "payload_released_count": released,
                    "release_attempt_count": release_attempts, "release_acknowledged": release_acknowledged,
                    "sequencer_state": sequencer_state, "perigee_altitude_m": orbit["perigee_altitude_m"],
                    "dynamic_pressure_pa": o["dynamic_pressure_pa"], "body_rate_rad_s": env.norm(s.omega_body_rad_s),
                    "propellant_kg": s.propellant_kg, "return_deadline_s": return_time}
                observation, supervision_command = supervision.step(telemetry, return_reserve_kg=p["ship"]["return_reserve_kg"],
                                                                    payload_interval_s=p["payload"]["interval_s"],
                                                                    orbit_bound=orbit.get("status") == "bound")
                if observation is not None:
                    record = _sample(s, v, phase)
                    record["flight_supervision"] = {key: observation[key] for key in ("observation_id", "sequence", "release_attempt_count",
                        "release_acknowledged", "sequencer_state", "payload_released_count")}
                    samples.append(record)
                if supervision_command is not None:
                    sequencer_state, next_release = "skipped", None
                    event("deployment_skip_command", "bounded supervisor command disables remaining deployment sequence; later observations required",
                          **supervision_command)
            if s.time_s >= return_time and return_observation_started is None and return_halt_started is None:
                if mission_director is not None or registered_return:
                    from .starship_return_feasibility import evaluate
                    from .starship_mission_director import fuel_sensor, check_action, fallback_action
                    current = director_observation()
                    proof = evaluate(p, s, released, fuel_sensor(s.propellant_kg, s.time_s, "ship"))
                    current["numerical_tools"]["return_feasibility"] = proof
                    if mission_director is not None:
                        reason = check_action(mission_director.envelope, "return_selection", "state_return", current,
                            elapsed_s=0, observation_requests=mission_director.observation_requests, hold_used_s=mission_director.hold_used_s)
                        choice = ("state_return" if reason is None else fallback_action(mission_director.envelope,
                            "return_selection", current, observation_requests=mission_director.observation_requests,
                            hold_used_s=mission_director.hold_used_s))
                    else:
                        choice = "state_return" if proof["return_admitted"] else "inhibit_return" if proof["bounded_coast_admitted"] else "halt_unresolved_return"
                    return_execution_checks.append({"time_s": s.time_s, "action": choice, "observation": current,
                        "feasibility": proof, "physical_recovery_certified": False})
                    event("return_execution_checked", "fresh independent margins before deorbit", action=choice)
                    witness = _sample(s, v, phase)
                    witness["return_execution_observation"] = current
                    samples.append(witness)
                    if choice != "state_return":
                        return_policy, return_record = "fixed_v1", new_record("fixed_v1")
                        return_observation_started = s.time_s if choice == "inhibit_return" else None
                        return_halt_started = s.time_s if choice == "halt_unresolved_return" else None
                        continue
                    return_policy = TRIMMED_POLICY_ID
                    managed_return_selected = True
                    return_record = new_record(return_policy)
                if mission_director is not None and not managed_return_selected:
                    return_policy = "fixed_v1"
                    return_record = new_record(return_policy)
                    event("managed_return_deadline_fallback", "keep the approved initial return plan; no decision can delay deorbit")
                return_controller = ReturnController(return_policy, return_time, released,
                    allocation_enabled=bool(development and development["bounded_ship_flaps"] or registered_return and return_policy == TRIMMED_POLICY_ID),
                    registered=registered_return, terminal_engine_out=scenario == "terminal_engine_out",
                    failed_engine=failed_engine, record=return_record)
                return_controller.start(s, v, p, samples, event)
                phase = return_controller.phase
                continue
        elif phase in ("deorbit_slew", "deorbit_burn", "ballistic_return", "landing_burn"):
            step = return_controller.update(s, v, p, samples, event)
            s, phase = step.state, return_controller.phase
            failed_engine = return_controller.failed_engine
            if step.transition_only:
                continue
            target, throttle, count = step.target, step.throttle, step.count
        if return_controller is not None:
            command, diagnostics = return_controller.command(step, v, p, altitude)
        else:
            command, diagnostics = control(s, v, target, throttle, count, p,
                control_interval_s=control_interval(p, throttle, altitude))
        if scenario in ("gimbal_step", "flap_asymmetry", "entry_perturbation"):
            # Open-loop physical response tests. No attitude stabilization hides
            # cross-axis response or torque from offset geometry.
            commands = [dyn.EngineCommand() for _ in v.engines]
            flap_commands = [0.]*len(v.aero_panels)
            if scenario == "gimbal_step":
                commands[0] = dyn.EngineCommand(True, .5, *(math.radians(x) for x in initial["gimbal_step_deg"]))
            if scenario == "flap_asymmetry":
                flap_commands[3] = math.radians(initial["flap_step_deg"][0])
                flap_commands[6] = math.radians(initial["flap_step_deg"][1])
            command = dyn.Command6DOF(tuple(commands), tuple(flap_commands))
            diagnostics = {"controller": "open_loop_perturbation", "initial_body_rates_nonzero": True}
        next_sample = record_sample(s, v, phase, command, diagnostics, samples, next_sample, p)
        max_altitude = max(max_altitude, altitude)
        max_rate = max(max_rate, env.norm(s.omega_body_rad_s))
        max_error = max(max_error, diagnostics.get("attitude_error_deg", 0.))
        if env.norm(s.omega_body_rad_s) > 5:
            termination = "angular_rate_envelope_exceeded"
            event(termination, "5 rad/s development envelope; no structural breakup modeled")
            break
        dt = p["integration"]["powered_dt_s"] if throttle > 0 or altitude < 100000 else p["integration"]["coast_dt_s"]
        dt = min(limit-s.time_s, dt*dt_scale)
        length = p["geometry"]["ship_length_m"]+(p["geometry"]["booster_length_m"] if phase == "stack_ascent" else 0)
        try:
            s, receipt = advance_plant(s, v, command, dt, length, p["geometry"]["radius_m"], altitude, o["air_speed_mps"])
            if receipt is not None and receipt["contact"]:
                contact_receipt = receipt
                termination = "surface_impact" if receipt["surface_relative_speed_mps"] > 5 else "low_speed_surface_contact"
                event(termination, "cylinder-envelope first bracketed contact; no water/structure response", contact=receipt)
                steps += 1
                break
        except (ValueError, OverflowError, FloatingPointError) as exc:
            termination = "numerical_failure"
            event(termination, f"{type(exc).__name__}: {exc}; last finite state retained")
            break
        steps += 1
    final = _sample(s, v, phase)
    if supervision is not None:
        final["flight_supervision"] = {"release_attempt_count": release_attempts, "release_acknowledged": release_acknowledged,
                                      "sequencer_state": sequencer_state, "payload_released_count": released}
    final["contact"] = termination.endswith("contact") or termination == "surface_impact"
    samples.append(final)
    if termination == "time_limit":
        event("time_limit", "run ended at requested integration horizon; no terminal success inferred")
    # Each released body coasts from its own measured separation state. This is
    # orbit propagation, not commissioning or operational Starlink service.
    for satellite in satellites:
        child = dyn.state_from_dict(satellite["release_state"])
        child_vehicle = dyn.vehicle_from_dict(satellite["vehicle"])
        child_samples = []
        next_child_sample = child.time_s
        while child.time_s < s.time_s-1e-8:
            if child.time_s >= next_child_sample:
                record = _sample(child, child_vehicle, "satellite_coast")
                record["body_id"] = satellite["id"]
                child_samples.append(record)
                next_child_sample = child.time_s+30
            child = dyn.step(child, child_vehicle, dyn.Command6DOF(), min(10., s.time_s-child.time_s), atmosphere=False)
        last = _sample(child, child_vehicle, "satellite_coast")
        last["body_id"] = satellite["id"]
        child_samples.append(last)
        satellite["samples"], satellite["final_orbit"] = child_samples, env.orbital_elements(point_state(child))
        satellite["orbital_propagation_only"] = True
    booster_run, catch_run = None, None
    if stage_state:
        if booster_policy == "predictive_return_v1":
            from .starship_booster_recovery import simulate_recovery
            booster_run = simulate_recovery(p, stage_state, catch_config)
            handoff = booster_run["recovery_record"]["handoff"]
            if handoff["eligible"] is not (booster_run["outcome"]["termination"] == "catch_handoff"):
                raise ValueError("recovery handoff differs from termination")
            if handoff["eligible"]:
                from .starship_booster_catch import simulate_catch
                # The exact integrated state, including finite actuator states,
                # is the only permissible input to contact mechanics.
                if handoff["state"] != booster_run["final_state"]:
                    raise ValueError("recovery handoff differs from final integrated state")
                catch_run = simulate_catch(p, catch_config, initial_state=handoff["state"],
                                           control_policy="net_thrust_trim_v1", duration_s=30.)
        else:
            from .starship_sixdof_booster import simulate_booster
            booster_run = simulate_booster(p, stage_state, mission_director=mission_director,
                return_sites=return_sites, tower_ready=mission_case != "tower_unavailable", splashdown_goal=splashdown_goal)
    result = {"scenario": scenario, "samples": samples, "events": events, "booster_separation_state": stage_state, "satellites": satellites, "booster_run": booster_run,
            "retained_return": return_record,
            "outcome": {"termination": termination, "phase": phase, "duration_s": s.time_s, "integration_steps": steps,
                        "max_altitude_m": max(max_altitude, final["altitude_m"]), "max_body_rate_rad_s": max_rate,
                        "max_attitude_error_deg": max_error, "final_ground_speed_mps": final["ground_speed_mps"],
                        "final_altitude_m": final["altitude_m"], "orbit_gate_reached": orbit_time is not None,
                        "payload_released_count": released, "payload_6dof_separation_implemented": True,
                        "booster_return_6dof_implemented": True, "booster_return_invoked": booster_run is not None,
                        "contact_receipt": contact_receipt, "starship_vehicle_validated": False,
                        "terminal_qualification_violated": return_record.get("terminal_feasibility", {}).get("passed") is False,
                        "six_dof_integrated": True, "attitude_prescribed": False},
            "initial_state": samples[0], "final_state": asdict(s), "final_vehicle": asdict(v)}
    if mission_director is not None:
        result["mission_director"] = mission_director.finish()
    if development is not None:
        result["development_return_qualification"] = dict(development)
    if registered_return:
        result["return_qualification_sha256"] = qualification_identity
    if return_execution_checks:
        result["return_execution_checks"] = return_execution_checks
    if mission_case is not None:
        result["mission_management_case"] = mission_case
    if supervision is not None:
        result["supervision"] = supervision.finish(sequencer_state=sequencer_state, simulation_time_s=s.time_s)
    if booster_policy == "predictive_return_v1":
        result["booster_catch_run"] = catch_run
        result["booster_recovery"] = {
            "policy_id": booster_policy, "separation_state_preserved": booster_run is not None,
            "catch_control_policy": "net_thrust_trim_v1", "catch_maximum_duration_s": 30.,
            "handoff_reached": bool(booster_run and booster_run["recovery_record"]["handoff"]["eligible"]),
            "catch_invoked": catch_run is not None, "state_reset": False,
            "launch_connected_catch_supported": bool(catch_run and catch_run["outcome"]["simulated_catch_supported"]),
            "physical_execution": False, "real_hardware_validated": False}
    return result

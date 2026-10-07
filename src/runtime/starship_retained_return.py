"""Preapproved local terminal-guidance estimate, not a certified entry envelope.

No fitted trajectory, new force, state reset, or provider decision is used. The
scalar preparation budget deliberately remains separate from the 6DOF outcome.
Its slew estimate cannot guarantee achieved torque under coupled aero loads.
"""
from __future__ import annotations

import math

import numpy as np

from . import starship_physics as env
from . import starship_sixdof as dyn

POLICY_ID = "mass_state_terminal_v1"
CONTINUOUS_POLICY_ID = "mass_state_terminal_v2"
CONDITIONED_POLICY_ID = "mass_state_terminal_v3"
TRIMMED_POLICY_ID = "trimmed_state_terminal_v4"


def terminal_budget(state, profile):
    """Compute one inspectable estimate from an observed pre-command state.

    Subsonic descent is an explicit local applicability limit. The preparation
    estimate uses central gravity and a geocentric vertical, assumes zero net
    thrust/drag while turning, then aligned full thrust. It is NOT a worst-case
    bound: thrust while inverted, saturation and aerodynamic moments can make
    actual motion worse. Existing finite actuators must execute the maneuver.
    """
    p, a, g = profile["ship"], profile["actuators"], profile["guidance"]
    r, v = np.asarray(state["r_eci_m"]), np.asarray(state["v_eci_mps"])
    radius = float(np.linalg.norm(r))
    up = r/radius
    relative = v-np.cross([0., 0., env.EARTH_ROTATION_RAD_S], r)
    vertical = float(relative@up)
    speed = float(np.linalg.norm(relative))
    mach = speed/env.standard_atmosphere(state["altitude_m"])["speed_of_sound_mps"]
    gravity = env.EARTH_MU_M3_S2/radius**2
    axis = dyn.rotate(state["q_body_to_eci"], (0., 0., 1.))
    theta = math.acos(max(-1., min(1., float(np.dot(axis, up)))))
    transverse_rate = math.hypot(*state["omega_body_rad_s"][:2])
    available = [i for i in range(min(3, p["gimbal_engine_count"])) if state["engine_states"][i]["available"]]
    thrust = len(available)*p["engine_thrust_n"]
    # Pitch/yaw TVC moment using axial lever arms, not fictitious free torque.
    torque = sum(p["engine_thrust_n"]*g["flip_min_throttle"]*math.sin(math.radians(a["max_gimbal_deg"]))
                 *abs(state["com_z_m"]-p["engine_positions_body_m"][i][2]) for i in available)
    inertia_bound = float(np.linalg.eigvalsh(state["inertia_kg_m2"])[-1])
    alpha = min(g["max_angular_acceleration_rad_s2"], torque/inertia_bound)
    net_accel = thrust/state["mass_kg"]-gravity
    lag = 4*max(a["throttle_tau_s"], a["gimbal_tau_s"])+a["max_gimbal_deg"]/a["gimbal_rate_deg_s"]
    result = {"time_s": state["time_s"], "altitude_m": state["altitude_m"],
              "vertical_speed_mps": vertical, "mach": mach, "mass_kg": state["mass_kg"],
              "propellant_kg": state["propellant_kg"], "gravity_mps2": gravity,
              "thrust_axis_angle_rad": theta, "transverse_rate_rad_s": transverse_rate,
              "available_landing_engines": len(available), "available_thrust_n": thrust,
              "tvc_moment_estimate_nm": torque, "inertia_upper_kg_m2": inertia_bound,
              "slew_accel_estimate_rad_s2": alpha, "aligned_net_acceleration_mps2": net_accel,
              "actuator_lag_s": lag, "applicable": False, "trigger": False,
              "preparation_time_s": None, "required_altitude_m": None,
              "fuel_budget_estimate_kg": None, "fuel_margin_estimate_kg": None}
    if alpha <= 0 or net_accel <= 0 or state["propellant_kg"] <= 0:
        result["reason"] = "insufficient_modeled_propulsion"
        return result
    # Stop the measured transverse rate before a nominal rest-to-rest turn.
    preparation = transverse_rate/alpha+2*math.sqrt((theta+transverse_rate**2/(2*alpha))/alpha)+lag
    descent = max(0., -vertical)
    after = descent+gravity*preparation
    braking = after**2/(2*net_accel)
    height = descent*preparation+.5*gravity*preparation**2+braking+profile["geometry"]["ship_length_m"]
    required = max(g["flip_altitude_m"], height)
    # Diagnostic full-thrust equivalent, not a certified fuel requirement.
    fuel = thrust/(p["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2)*(preparation+after/net_accel)
    applicable = vertical < 0 and mach <= 1
    result.update(preparation_time_s=preparation, required_altitude_m=required,
                  fuel_budget_estimate_kg=fuel, fuel_margin_estimate_kg=state["propellant_kg"]-fuel,
                  applicable=bool(applicable), trigger=bool(applicable and state["altitude_m"] <= required),
                  reason="subsonic_descent" if applicable else "outside_subsonic_descent")
    return result


def new_record(policy):
    if policy not in ("fixed_v1", POLICY_ID, CONTINUOUS_POLICY_ID, CONDITIONED_POLICY_ID, TRIMMED_POLICY_ID):
        raise ValueError("unknown return policy")
    return {"schema": "missionos.starship_retained_return.v1", "policy_id": policy,
            "status": "fixed" if policy == "fixed_v1" else "not_activated",
            "activation": None, "trigger": None, "evaluation_count": 0,
            "landing_verified": False, "starship_vehicle_validated": False}

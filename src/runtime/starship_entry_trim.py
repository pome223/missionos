"""Model-based entry bank/trim preparation; not recovered SpaceX guidance.

Only requested frames and surface commands change. The original plant computes
all loads and finite actuator motion. Static roots are not flight qualification.
"""
from dataclasses import replace
from itertools import product
import math
import numpy as np
from . import starship_physics as env, starship_sixdof as dyn

MAXIMUM_ROLL_RATE_RAD_S = .01
STATIC_RESIDUAL_TOLERANCE_NM_PA = 1e-4

def frozen_surface_model(vehicle, observed, indices):
    """Same bidirectional plates, frozen flow/rates/COM; no dynamics shortcut.

    Vectorized evaluation and analytic angle derivatives are only used by the
    optimizer. The integrator still evaluates the original loads at RK stages.
    Local velocities include the measured body rate and the configured wind.
    """
    panels = [vehicle.aero_panels[i] for i in indices]
    normal = np.array([p.normal_body for p in panels])
    hinge = np.array([p.hinge_axis_body for p in panels])
    parallel = hinge*np.sum(hinge*normal, axis=1)[:, None]
    perpendicular = normal-parallel
    crossed = np.cross(hinge, normal)
    lever = np.array([p.position_body_m for p in panels])-observed["com_body_m"]
    air = np.array([observed["panel_loads"][i]["local_air_velocity_body_mps"] for i in indices])
    density = env.standard_atmosphere(observed["altitude_m"])["density_kg_m3"]
    normal_scale = .5*density*np.array([p.area_m2*p.normal_coefficient for p in panels])
    tangent_scale = .5*density*np.array([p.area_m2*p.tangential_coefficient for p in panels])
    constant = np.array(observed["aero_torque_body_nm"])-np.sum(
        [observed["panel_loads"][i]["torque_body_nm"] for i in indices], axis=0)

    def evaluate(angles, *, derivative=False):
        angles = np.asarray(angles)
        c, s = np.cos(angles)[..., None], np.sin(angles)[..., None]
        n = parallel+c*perpendicular+s*crossed
        vn = np.sum(air*n, axis=-1)
        tangent = air-vn[..., None]*n
        speed = np.linalg.norm(tangent, axis=-1)
        force = -normal_scale[..., None]*(vn*np.abs(vn))[..., None]*n-tangent_scale[..., None]*speed[..., None]*tangent
        moment = constant+np.sum(np.cross(lever, force), axis=-2)
        if not derivative:
            return moment
        dn = -s*perpendicular+c*crossed
        dvn = np.sum(air*dn, axis=-1)
        dt = -dvn[..., None]*n-vn[..., None]*dn
        dspeed = np.divide(np.sum(tangent*dt, axis=-1), speed, out=np.zeros_like(speed), where=speed > 1e-12)
        df = -normal_scale[..., None]*(2*np.abs(vn)[..., None]*dvn[..., None]*n+(vn*np.abs(vn))[..., None]*dn)
        df -= tangent_scale[..., None]*(dspeed[..., None]*tangent+speed[..., None]*dt)
        return moment, np.swapaxes(np.cross(lever, df), -1, -2)

    return evaluate


def prepare_entry_trim(state, vehicle, axis, flow, legacy_q):
    """Choose a static root with surface headroom across fixed bank starts.

    The +Z/flow angle is not fitted or changed. Bank is relative to the flow
    plane; a hypothetical stationary pose is used only to predict panel loads.
    Surface limits, rates and lag still apply to all subsequent commands.
    """
    from scipy.optimize import least_squares, minimize
    from .starship_sixdof_mission import _attitude
    indices = [i for i, p in enumerate(vehicle.aero_panels) if p.name.startswith("flap_")]
    if len(indices) != 4:
        raise ValueError("entry_trim_requires_four_ship_flaps")
    wind_q = _attitude(axis, env.cross(flow, axis))
    limits = np.array([vehicle.aero_panels[i].max_deflection_rad for i in indices])
    # Fixed geometric starts, independent of inventory labels and flight results.
    starts = [np.zeros(4), np.array([.5, .5, 0., 0.])*limits,
              np.array([0., 0., .5, .5])*limits,
              *np.array(list(product(*zip(-limits+.001, limits-.001))))]
    solutions, probes = [], []
    banks = [("wind_plane", math.radians(deg)) for deg in range(0, 91, 15)]+[("legacy_geographic", None)]
    for kind, bank in banks:
        q = legacy_q if bank is None else dyn.quaternion_multiply(wind_q, dyn.axis_angle((0., 0., 1.), bank))
        predicted = dyn.observe(replace(state, q_body_to_eci=q, omega_body_rad_s=(0., 0., 0.)), vehicle)
        pressure = predicted["dynamic_pressure_pa"]
        if pressure <= 1e-12:
            return {"status": "unavailable", "reason": "flow_too_small", "actual_state_assigned": False}
        physical = frozen_surface_model(vehicle, predicted, indices)
        def value(angles):
            return physical(angles)/pressure
        def jac(angles):
            return physical(angles, derivative=True)[1]/pressure
        minimum = math.inf
        for start in starts:
            solved = least_squares(value, start, jac=jac, bounds=(-limits, limits), max_nfev=100,
                ftol=1e-9, xtol=1e-9, gtol=1e-9)
            norm = float(np.linalg.norm(value(solved.x)))
            minimum = min(minimum, norm)
            if norm > STATIC_RESIDUAL_TOLERANCE_NM_PA:
                continue
            # Refine neutral-angle effort on the equality manifold. This can
            # improve a root near four end stops without exchanging trim error.
            refined = minimize(lambda x: float(np.sum((x/limits)**2)), solved.x,
                jac=lambda x: 2*x/limits**2, bounds=list(zip(-limits, limits)), method="SLSQP",
                constraints={"type": "eq", "fun": value, "jac": jac},
                options={"maxiter": 40, "ftol": 1e-10})
            angles = refined.x if np.linalg.norm(value(refined.x)) <= STATIC_RESIDUAL_TOLERANCE_NM_PA else solved.x
            singular = np.linalg.svd(jac(angles), compute_uv=False)
            ratio = float(singular[-1]/singular[0]) if singular[0] > 0 else 0.
            if ratio <= 1e-4:
                continue
            solutions.append((float(max(abs(angles/limits))), -ratio, kind, bank, angles.copy(), value(angles).copy(), q))
        probes.append({"basis": kind, "bank_rad": bank, "minimum_found_residual_nm_pa": minimum})
    if not solutions:
        return {"status": "unavailable", "reason": "no_controllable_static_root_found",
                "probes": probes, "actual_state_assigned": False, "global_optimum_proven": False}
    _, negative_ratio, kind, bank, angles, torque, target = min(solutions, key=lambda row: row[:2])
    full_angles = list(state.flap_angles_rad)
    for i, angle in zip(indices, angles):
        full_angles[i] = float(angle)
    return {"status": "prepared", "time_s": state.time_s, "basis": kind, "bank_rad": bank,
        "fin_indices": indices, "flap_names": [vehicle.aero_panels[i].name for i in indices],
        "trim_angles_rad": full_angles, "target_q_body_to_eci": list(target),
        "predicted_torque_per_pressure_nm_pa": torque.tolist(), "conditioning_ratio": -negative_ratio,
        "maximum_roll_rate_rad_s": MAXIMUM_ROLL_RATE_RAD_S,
        "probes": probes, "attitude_and_surface_state_assigned": False, "actual_state_assigned": False,
        "global_optimum_proven": False, "trim_is_execution": False}


def entry_preferred(axis, flow, legacy_q, prepared):
    if prepared.get("status") != "prepared" or prepared["basis"] == "legacy_geographic":
        return legacy_q, dyn.rotate(legacy_q, (1., 0., 0.))
    from .starship_sixdof_mission import _attitude
    wind_q = _attitude(axis, env.cross(flow, axis))
    q = dyn.quaternion_multiply(wind_q, dyn.axis_angle((0., 0., 1.), prepared["bank_rad"]))
    return q, dyn.rotate(q, (1., 0., 0.))

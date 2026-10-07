"""Geometric contact of a configured cylindrical envelope with the WGS84 surface.

The cylinder is a conservative hull proxy, not engineering CAD: body z is in
[0, length], x*x+y*y <= radius*radius. Appendages outside that envelope are NOT
covered. The surface is a rotating ellipsoid, without terrain, waves, response,
buoyancy, deformation, or a landing/catch success criterion.

Spatial contact minimizes the ellipsoid quadratic over the entire cylinder;
it does not sample polygon vertices. Time contact locates the first bracketed
endpoint crossing under held commands. A complete swept-volume collision test
is not claimed for an enter-and-leave crossing inside one integration interval.
"""
from __future__ import annotations

import math

import numpy as np

from . import starship_physics as env
from . import starship_sixdof as dyn


def _positive(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite positive")


def _disk_minimum(matrix: np.ndarray, linear: np.ndarray, radius: float) -> np.ndarray:
    """Global minimum of u' A u + 2 b' u on a closed 2D disk (A > 0)."""
    a, b, c = float(matrix[0, 0]), float(matrix[0, 1]), float(matrix[1, 1])
    dx, dy = (float(x) for x in linear)

    def solution(lam):
        aa, cc = a+lam, c+lam
        determinant = aa*cc-b*b
        return np.array([(-cc*dx+b*dy)/determinant, (b*dx-aa*dy)/determinant])

    free = solution(0.0)
    if np.linalg.norm(free) <= radius:
        return free
    # For SPD A, ||(A+lambda I)^-1 b|| <= ||b||/lambda.
    low, high = 0.0, max(1.0, math.hypot(dx, dy)/radius)
    for _ in range(55):
        middle = (low+high)/2
        if np.linalg.norm(solution(middle)) > radius:
            low = middle
        else:
            high = middle
    result = solution(high)
    # Maintain the exactly specified radius at the active disk constraint.
    return result * (radius/float(np.linalg.norm(result)))


def hull_clearance(state: dyn.State6DOF, vehicle: dyn.Vehicle6DOF,
                   length_m: float, radius_m: float) -> dict:
    """Signed ellipsoidal level-set distance and a minimizing hull point.

    ``signed_clearance_m = sqrt(x*x+y*y+(a/b)^2*z*z)-a`` at the
    minimizing point. Its sign is exact for this envelope; its magnitude is
    not geodetic height or Euclidean distance to the ellipsoid. The full disk
    and axial segment constraints are solved, including nose-first and side
    contact, rather than checking the vehicle base alone.
    """
    _positive(length_m, "length_m")
    _positive(radius_m, "radius_m")
    props = dyn.mass_properties(vehicle, state.propellant_kg)
    rotation = np.column_stack([dyn.rotate(state.q_body_to_eci, axis)
                                for axis in ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))])
    origin = np.asarray(state.r_eci_m)-rotation@np.asarray(props.com_body_m)
    weight = np.diag([1., 1., (env.EARTH_EQUATORIAL_RADIUS_M/env.EARTH_POLAR_RADIUS_M)**2])
    quadratic = rotation.T@weight@rotation
    linear = rotation.T@weight@origin
    candidates = []
    # Minima constrained to either end cap (including disk interior).
    for z in (0.0, length_m):
        xy = _disk_minimum(quadratic[:2, :2], linear[:2]+quadratic[:2, 2]*z, radius_m)
        candidates.append(np.array([*xy, z]))
    # Minimize with the axial coordinate free, then retain it only if feasible.
    cross = quadratic[:2, 2]
    reduced = quadratic[:2, :2]-np.outer(cross, cross)/quadratic[2, 2]
    reduced_linear = linear[:2]-cross*linear[2]/quadratic[2, 2]
    xy = _disk_minimum(reduced, reduced_linear, radius_m)
    z = -(linear[2]+float(np.dot(cross, xy)))/quadratic[2, 2]
    if 0 <= z <= length_m:
        candidates.append(np.array([*xy, z]))
    points = [origin+rotation@point for point in candidates]
    values = [float(point@weight@point) for point in points]
    index = min(range(len(values)), key=values.__getitem__)
    point = points[index]
    normal = weight@point
    normal_length = float(np.linalg.norm(normal))
    if normal_length < 1e-12:
        raise ValueError("envelope contains Earth's center; contact normal undefined")
    normal /= normal_length
    return {"signed_clearance_m": math.sqrt(values[index])-env.EARTH_EQUATORIAL_RADIUS_M,
            "clearance_measure": "minimum_ellipsoid_level_set_radius_difference",
            "point_body_m": candidates[index].tolist(), "point_eci_m": point.tolist(),
            "surface_normal_eci": normal.tolist(),
            "geometry": "configured_closed_cylinder_envelope",
            "spatial_method": "convex_quadratic_disk_and_axial_active_sets",
            "angular_vertex_sampling": False, "appendages_outside_radius_modeled": False}


def _receipt(state, vehicle, command, geometry, *, contact, bracket, initial_overlap=False):
    result = {**geometry, "contact": contact, "event_time_s": state.time_s,
              "initial_overlap": initial_overlap, "time_bracket_s": bracket,
              "event_detection": "endpoint_bracket_with_bisection",
              "complete_swept_volume_test": False, "contact_response_modeled": False,
              "landing_verified": False, "starship_vehicle_validated": False}
    if not contact:
        return result
    observation = dyn.observe(state, vehicle, command, atmosphere=False)
    lever = env.add(geometry["point_body_m"], env.scale(observation["com_body_m"], -1))
    # r refers to the instantaneous retained CoM. A fixed body point has an
    # additional -com_dot term when that centroid moves during depletion.
    local_velocity = env.add(env.cross(state.omega_body_rad_s, lever),
                             env.scale(observation["com_rate_body_mps"], -1))
    inertial_velocity = env.add(state.v_eci_mps, dyn.rotate(state.q_body_to_eci, local_velocity))
    surface_velocity = env.cross((0., 0., env.EARTH_ROTATION_RAD_S), geometry["point_eci_m"])
    relative = env.add(inertial_velocity, env.scale(surface_velocity, -1))
    normal_speed = env.dot(relative, geometry["surface_normal_eci"])
    result.update({"point_velocity_eci_mps": list(inertial_velocity),
                   "surface_relative_velocity_mps": list(relative),
                   "surface_relative_speed_mps": env.norm(relative),
                   "surface_normal_speed_mps": normal_speed,
                   "surface_tangential_speed_mps": math.sqrt(max(0., env.dot(relative, relative)-normal_speed**2)),
                   "omega_cross_lever_applied": True, "moving_com_correction_applied": True,
                   "propellant_kg": state.propellant_kg, "q_body_to_eci": list(state.q_body_to_eci),
                   "omega_body_rad_s": list(state.omega_body_rad_s)})
    return result


def find_contact(before: dyn.State6DOF, vehicle: dyn.Vehicle6DOF, command: dyn.Command6DOF,
                 dt_s: float, length_m: float, radius_m: float, *,
                 gravity: bool = True, j2: bool = True, atmosphere: bool = True) -> tuple[dyn.State6DOF, dict]:
    """Advance or return the first bracketed envelope contact without resetting v/q/w.

    The caller must stop on ``receipt['contact']``. Receipt speed is that of the
    contacting material point relative to the rotating surface, not CoM speed.
    Failure in the underlying integrator is propagated; it is not a landing.
    """
    _positive(dt_s, "dt_s")
    if dt_s > 10:
        raise ValueError("dt_s must be at most 10 seconds")
    if any(type(flag) is not bool for flag in (gravity, j2, atmosphere)):
        raise ValueError("environment switches must be boolean")
    first = hull_clearance(before, vehicle, length_m, radius_m)
    if first["signed_clearance_m"] <= 0:
        return before, _receipt(before, vehicle, command, first, contact=True,
                                bracket=[before.time_s, before.time_s], initial_overlap=True)

    def advance(dt):
        return dyn.step(before, vehicle, command, dt, gravity=gravity, j2=j2, atmosphere=atmosphere)

    after = advance(dt_s)
    last = hull_clearance(after, vehicle, length_m, radius_m)
    if last["signed_clearance_m"] > 0:
        return after, _receipt(after, vehicle, command, last, contact=False,
                               bracket=[before.time_s, after.time_s])
    low, high = 0.0, dt_s
    # Re-integrate from the same pre-step state, holding exactly the same command.
    for _ in range(50):
        if high-low <= 1e-8:
            break
        middle = (low+high)/2
        trial = advance(middle)
        geometry = hull_clearance(trial, vehicle, length_m, radius_m)
        if geometry["signed_clearance_m"] > 0:
            low = middle
        else:
            high, after, last = middle, trial, geometry
    return after, _receipt(after, vehicle, command, last, contact=True,
                           bracket=[before.time_s+low, before.time_s+high])

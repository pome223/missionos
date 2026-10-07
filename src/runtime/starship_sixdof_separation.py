"""Instantaneous finite rigid-payload release conserving P and angular momentum.

The parent and child share the parent body datum/orientation before release.
The specified equal/opposite impulse acts at one physical attachment point.
No position gap, velocity target, orientation reset, or angular-rate target is
inserted. Subsequent collision clearance, joint compliance, release duration,
plume interaction, and real dispenser geometry are not modeled here.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import math

from . import starship_physics as vec
from . import starship_sixdof as sd


def _sub(a: sd.Vector, b: sd.Vector) -> sd.Vector:
    return vec.add(a, vec.scale(b, -1))


def _parallel(mass: float, lever: sd.Vector) -> sd.Matrix:
    norm2 = vec.dot(lever, lever)
    return tuple(tuple(mass*((norm2 if i == j else 0.0)-lever[i]*lever[j])
                       for j in range(3)) for i in range(3))


def _sum_matrices(*matrices: sd.Matrix) -> sd.Matrix:
    return tuple(tuple(sum(m[i][j] for m in matrices) for j in range(3)) for i in range(3))


def _matvec(matrix: sd.Matrix, value: sd.Vector) -> sd.Vector:
    return tuple(vec.dot(row, value) for row in matrix)


def _vector(value: sd.Vector, name: str) -> sd.Vector:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError(f"{name} must have three components")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value):
        raise ValueError(f"{name} must be finite")
    return tuple(value)


def _child_vehicle(mass: float, inertia: sd.Matrix) -> sd.Vehicle6DOF:
    # Dry-only child. Placeholder empty tank dimensions have no mass/inertia.
    return sd.Vehicle6DOF(dry_mass_kg=mass, dry_com_body_m=sd.ZERO,
                          dry_inertia_kg_m2=inertia, propellant_capacity_kg=0.0,
                          tank_center_body_m=sd.ZERO, tank_radius_m=1.0, tank_length_m=1.0)


def attach_payload(parent_without: sd.Vehicle6DOF, child_mass_kg: float,
                   child_position_body_m: sd.Vector, child_inertia_kg_m2: sd.Matrix) -> sd.Vehicle6DOF:
    """Compose retained dry mass properties; child tensor is about its own CoM.

    The body datum, tanks, engines and panels do not move. Payload body axes are
    aligned with that datum. All three principal moments must be physical and
    positive; a zero-inertia point payload cannot stand in for a rigid child.
    """
    child = _child_vehicle(child_mass_kg, child_inertia_kg_m2)
    position = _vector(child_position_body_m, "child position")
    total = parent_without.dry_mass_kg + child.dry_mass_kg
    com = vec.scale(vec.add(vec.scale(parent_without.dry_com_body_m, parent_without.dry_mass_kg),
                            vec.scale(position, child.dry_mass_kg)), 1/total)
    inertia = _sum_matrices(parent_without.dry_inertia_kg_m2, child.dry_inertia_kg_m2,
                            _parallel(parent_without.dry_mass_kg, _sub(parent_without.dry_com_body_m, com)),
                            _parallel(child.dry_mass_kg, _sub(position, com)))
    return replace(parent_without, dry_mass_kg=total, dry_com_body_m=com, dry_inertia_kg_m2=inertia)


def _assert_composition(before: sd.Vehicle6DOF, expected: sd.Vehicle6DOF) -> None:
    a, b = asdict(before), asdict(expected)
    for field in ("dry_mass_kg", "dry_com_body_m", "dry_inertia_kg_m2"):
        a.pop(field)
        b.pop(field)
    if a != b:
        raise ValueError("release must preserve parent body datum, tanks, engines and panels")
    actual_values = [before.dry_mass_kg, *before.dry_com_body_m,
                     *(x for row in before.dry_inertia_kg_m2 for x in row)]
    expected_values = [expected.dry_mass_kg, *expected.dry_com_body_m,
                       *(x for row in expected.dry_inertia_kg_m2 for x in row)]
    if any(not math.isclose(x, y, rel_tol=1e-10, abs_tol=1e-8) for x, y in zip(actual_values, expected_values)):
        raise ValueError("parent-before mass/CoM/inertia does not include the finite payload")


def release_payload(state: sd.State6DOF, parent_before: sd.Vehicle6DOF,
                    parent_after: sd.Vehicle6DOF, child_mass_kg: float,
                    child_position_body_m: sd.Vector, child_inertia_kg_m2: sd.Matrix,
                    impulse_body_ns: sd.Vector, *,
                    impulse_application_body_m: sd.Vector | None = None,
                    ) -> tuple[sd.State6DOF, sd.State6DOF, sd.Vehicle6DOF, dict]:
    """Apply +J to child and -J to parent at the common release point.

    Impulse application defaults to the child CoM. The child initially rotates
    with its parent. Parent CoM relocation therefore changes its position AND
    its velocity by omega cross offset; retaining the old CoM velocity would
    lose momentum. An offset attachment gives each body its proper spin kick.
    """
    position = _vector(child_position_body_m, "child position")
    impulse = _vector(impulse_body_ns, "impulse")
    attachment = position if impulse_application_body_m is None else _vector(impulse_application_body_m, "attachment")
    child_vehicle = _child_vehicle(child_mass_kg, child_inertia_kg_m2)
    _assert_composition(parent_before, attach_payload(parent_after, child_mass_kg, position, child_inertia_kg_m2))
    if len(state.engine_states) != len(parent_before.engines) or len(state.flap_angles_rad) != len(parent_before.aero_panels):
        raise ValueError("state does not match parent actuators")
    before = sd.mass_properties(parent_before, state.propellant_kg)
    after = sd.mass_properties(parent_after, state.propellant_kg)
    parent_offset = _sub(after.com_body_m, before.com_body_m)
    child_offset = _sub(position, before.com_body_m)
    impulse_eci = sd.rotate(state.q_body_to_eci, impulse)
    parent_arm = _sub(attachment, after.com_body_m)
    child_arm = _sub(attachment, position)
    parent_spin_delta = sd._solve(after.inertia_kg_m2, vec.cross(parent_arm, vec.scale(impulse, -1)))
    child_spin_delta = sd._solve(child_vehicle.dry_inertia_kg_m2, vec.cross(child_arm, impulse))
    parent_velocity = vec.add(state.v_eci_mps, sd.rotate(state.q_body_to_eci, vec.cross(state.omega_body_rad_s, parent_offset)))
    child_velocity = vec.add(state.v_eci_mps, sd.rotate(state.q_body_to_eci, vec.cross(state.omega_body_rad_s, child_offset)))
    parent_state = replace(state,
        r_eci_m=vec.add(state.r_eci_m, sd.rotate(state.q_body_to_eci, parent_offset)),
        v_eci_mps=vec.add(parent_velocity, vec.scale(impulse_eci, -1/after.mass_kg)),
        omega_body_rad_s=vec.add(state.omega_body_rad_s, parent_spin_delta))
    child_state = sd.State6DOF(time_s=state.time_s,
        r_eci_m=vec.add(state.r_eci_m, sd.rotate(state.q_body_to_eci, child_offset)),
        v_eci_mps=vec.add(child_velocity, vec.scale(impulse_eci, 1/child_mass_kg)),
        q_body_to_eci=state.q_body_to_eci,
        omega_body_rad_s=vec.add(state.omega_body_rad_s, child_spin_delta), propellant_kg=0.0)

    # Compute conservation from RESULT states in the initial CoM's translating
    # frame. Removing the common inertial velocity improves conditioning without
    # removing any relative orbital angular momentum.
    parent_relative_v = _sub(parent_state.v_eci_mps, state.v_eci_mps)
    child_relative_v = _sub(child_state.v_eci_mps, state.v_eci_mps)
    delta_p = vec.add(vec.scale(parent_relative_v, after.mass_kg), vec.scale(child_relative_v, child_mass_kg))
    before_h = sd.rotate(state.q_body_to_eci, _matvec(before.inertia_kg_m2, state.omega_body_rad_s))
    parent_h = sd.rotate(parent_state.q_body_to_eci, _matvec(after.inertia_kg_m2, parent_state.omega_body_rad_s))
    child_h = sd.rotate(child_state.q_body_to_eci, _matvec(child_vehicle.dry_inertia_kg_m2, child_state.omega_body_rad_s))
    parent_orbital = vec.cross(_sub(parent_state.r_eci_m, state.r_eci_m), vec.scale(parent_relative_v, after.mass_kg))
    child_orbital = vec.cross(_sub(child_state.r_eci_m, state.r_eci_m), vec.scale(child_relative_v, child_mass_kg))
    after_h = vec.add(vec.add(parent_h, child_h), vec.add(parent_orbital, child_orbital))
    delta_h = _sub(after_h, before_h)
    p_scale = max(1.0, before.mass_kg*max(1.0, vec.norm(state.v_eci_mps)))
    h_scale = max(1.0, vec.norm(before_h), vec.norm(impulse)*max(1.0, vec.norm(parent_arm)))
    verified = vec.norm(delta_p) <= 1e-10*p_scale and vec.norm(delta_h) <= 1e-8*h_scale
    if not verified:
        raise ValueError("separation momentum check exceeded numeric tolerance")
    receipt = {
        "schema": "missionos.sixdof_payload_release.v1", "time_s": state.time_s,
        "child_mass_kg": child_mass_kg, "child_position_body_m": list(position),
        "child_inertia_kg_m2": child_vehicle.dry_inertia_kg_m2,
        "impulse_body_ns": list(impulse), "impulse_application_body_m": list(attachment),
        "parent_spin_delta_body_rad_s": list(parent_spin_delta), "child_spin_delta_body_rad_s": list(child_spin_delta),
        "mass_before_kg": before.mass_kg, "mass_after_kg": after.mass_kg+child_mass_kg,
        "linear_momentum_residual_kg_mps": list(delta_p),
        "angular_momentum_before_kg_m2_s": list(before_h), "angular_momentum_after_kg_m2_s": list(after_h),
        "angular_momentum_residual_kg_m2_s": list(delta_h),
        "momentum_check_passed": verified,
        "finite_child_inertia": True, "velocity_target_inserted": False,
        "geometry_clearance_verified": False, "real_dispenser_validated": False,
        "parent_before_state": asdict(state), "parent_after_state": asdict(parent_state),
        "child_state": asdict(child_state),
    }
    return parent_state, child_state, child_vehicle, receipt

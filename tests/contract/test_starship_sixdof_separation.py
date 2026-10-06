"""Finite payload composition and conservation across instantaneous release."""
from dataclasses import replace
import math

import pytest

from src.runtime import starship_physics as vec
from src.runtime import starship_sixdof as sd
from src.runtime.starship_sixdof_separation import attach_payload, release_payload


def model():
    return sd.Vehicle6DOF(100, (0, 0, 1), ((100, 5, 0), (5, 110, -2), (0, -2, 120)),
                          20, (.2, -.3, 2), 1, 2)


def initial():
    return sd.State6DOF(42, (7_000_000, 500_000, -300_000), (1200, 7300, -400),
                         sd.axis_angle((1, 2, 3), .7), (.2, -.1, .4), 10)


CHILD_I = ((3, .1, 0), (.1, 4, .2), (0, .2, 5))
POSITION = (2, -1, 4)


def independent_totals(states, models, reference_position, reference_velocity):
    momentum = [0.0]*3
    angular = [0.0]*3
    energy = 0.0
    for state, vehicle in zip(states, models):
        props = sd.mass_properties(vehicle, state.propellant_kg)
        relative_v = [state.v_eci_mps[i]-reference_velocity[i] for i in range(3)]
        relative_r = [state.r_eci_m[i]-reference_position[i] for i in range(3)]
        body_h = [sum(props.inertia_kg_m2[i][j]*state.omega_body_rad_s[j] for j in range(3)) for i in range(3)]
        inertial_h = sd.rotate(state.q_body_to_eci, body_h)
        orbital_h = vec.cross(relative_r, vec.scale(relative_v, props.mass_kg))
        for i in range(3):
            momentum[i] += props.mass_kg*relative_v[i]
            angular[i] += inertial_h[i]+orbital_h[i]
        energy += .5*props.mass_kg*vec.dot(relative_v, relative_v)+.5*vec.dot(state.omega_body_rad_s, body_h)
    return momentum, angular, energy


@pytest.mark.parametrize("impulse,attachment", [((0, 0, 0), None), ((4, -3, 2), None),
                                               ((7, 2, -4), (3, -2, 5))])
def test_release_preserves_linear_and_angular_momentum_from_result_states(impulse, attachment):
    parent_after, s = model(), initial()
    parent_before = attach_payload(parent_after, 10, POSITION, CHILD_I)
    p, c, child, receipt = release_payload(s, parent_before, parent_after, 10, POSITION, CHILD_I,
                                          impulse, impulse_application_body_m=attachment)
    before = independent_totals([s], [parent_before], s.r_eci_m, s.v_eci_mps)
    after = independent_totals([p, c], [parent_after, child], s.r_eci_m, s.v_eci_mps)
    assert after[0] == pytest.approx(before[0], abs=1e-9)
    assert after[1] == pytest.approx(before[1], abs=2e-7)
    if impulse == (0, 0, 0):
        assert after[2] == pytest.approx(before[2], abs=1e-9)
    assert p.propellant_kg == s.propellant_kg and c.propellant_kg == 0
    assert p.q_body_to_eci == c.q_body_to_eci == s.q_body_to_eci
    assert p.time_s == c.time_s == 42
    assert receipt["momentum_check_passed"]
    assert receipt["geometry_clearance_verified"] is False


def test_com_velocity_and_spin_are_not_reset_to_previous_com_velocity():
    after, s = model(), initial()
    before = attach_payload(after, 10, POSITION, CHILD_I)
    p, c, child, receipt = release_payload(s, before, after, 10, POSITION, CHILD_I, (0, 0, 0))
    assert p.v_eci_mps != s.v_eci_mps
    assert c.v_eci_mps != s.v_eci_mps
    assert p.omega_body_rad_s == c.omega_body_rad_s == s.omega_body_rad_s
    assert child.dry_inertia_kg_m2 == CHILD_I
    assert receipt["finite_child_inertia"] is True


def test_com_impulse_no_child_spin_but_parent_has_reaction_torque():
    after, s = model(), initial()
    before = attach_payload(after, 10, POSITION, CHILD_I)
    p, c, _, receipt = release_payload(s, before, after, 10, POSITION, CHILD_I, (5, 0, 0))
    assert c.omega_body_rad_s == s.omega_body_rad_s
    assert p.omega_body_rad_s != s.omega_body_rad_s
    assert receipt["child_spin_delta_body_rad_s"] == [0, 0, 0]


def test_off_center_impulse_applies_equal_opposite_spin_and_orbital_momentum():
    after, s = model(), initial()
    before = attach_payload(after, 10, POSITION, CHILD_I)
    p, c, _, receipt = release_payload(s, before, after, 10, POSITION, CHILD_I, (0, 3, 0),
                                      impulse_application_body_m=(3, -1, 4))
    assert c.omega_body_rad_s != s.omega_body_rad_s
    assert p.omega_body_rad_s != s.omega_body_rad_s
    assert vec.norm(receipt["angular_momentum_residual_kg_m2_s"]) < 2e-7


def test_repeated_attach_and_release_finite_payloads():
    base = model()
    models = [base]
    for _ in range(4):
        models.append(attach_payload(models[-1], 10, POSITION, CHILD_I))
    s = initial()
    for count in reversed(range(1, 5)):
        s, child, child_model, _ = release_payload(s, models[count], models[count-1], 10, POSITION, CHILD_I, (.5, 0, 0))
        assert child_model.dry_mass_kg == 10
        assert child.propellant_kg == 0
    assert models[0] == base


@pytest.mark.parametrize("defect", ["pointmass", "mass", "tank"])
def test_reject_inconsistent_parent_composition(defect):
    after, s = model(), initial()
    before = attach_payload(after, 10, POSITION, CHILD_I)
    if defect == "pointmass":
        before = replace(before, dry_inertia_kg_m2=model().dry_inertia_kg_m2)
    elif defect == "mass":
        before = replace(before, dry_mass_kg=before.dry_mass_kg+1)
    else:
        before = replace(before, tank_center_body_m=(0, 0, 0))
    with pytest.raises(ValueError):
        release_payload(s, before, after, 10, POSITION, CHILD_I, (0, 0, 0))


def test_reject_zero_inertia_point_payload_and_nonfinite_impulse():
    with pytest.raises(ValueError):
        attach_payload(model(), 10, POSITION, ((0, 0, 0),)*3)
    before = attach_payload(model(), 10, POSITION, CHILD_I)
    with pytest.raises(ValueError):
        release_payload(initial(), before, model(), 10, POSITION, CHILD_I, (math.nan, 0, 0))

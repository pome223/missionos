"""Analytic and independent geometry checks for the explicit cylinder envelope."""
from dataclasses import replace
import math

import numpy as np
import pytest

from src.runtime import starship_physics as env
from src.runtime import starship_sixdof as dyn
from src.runtime import starship_sixdof_contact as contact


def vehicle():
    return dyn.Vehicle6DOF(100., (0., 0., 5.), ((900., 0., 0.), (0., 900., 0.), (0., 0., 100.)),
                           10., (0., 0., 5.), 1., 10.)


def state(altitude=6., q=None, **kwargs):
    defaults = dict(time_s=0., r_eci_m=(env.EARTH_EQUATORIAL_RADIUS_M+altitude, 0., 0.),
                    v_eci_mps=(-10., 0., 0.), q_body_to_eci=q or dyn.axis_angle((0., 1., 0.), math.pi/2),
                    omega_body_rad_s=(0., 0., 0.), propellant_kg=0.)
    defaults.update(kwargs)
    return dyn.State6DOF(**defaults)


def test_vertical_equatorial_end_cap_and_clearance_are_analytic():
    result = contact.hull_clearance(state(), vehicle(), 10., 1.)
    assert result["signed_clearance_m"] == pytest.approx(1., abs=1e-8)
    assert result["point_body_m"] == pytest.approx((0., 0., 0.), abs=1e-8)
    assert result["angular_vertex_sampling"] is False


def test_nose_first_impact_is_found_while_base_is_above_surface():
    q = dyn.axis_angle((0., 1., 0.), -math.pi/2)
    result = contact.hull_clearance(state(altitude=3., q=q), vehicle(), 10., 1.)
    assert result["signed_clearance_m"] == pytest.approx(-2., abs=1e-8)
    assert result["point_body_m"] == pytest.approx((0., 0., 10.), abs=1e-8)


def test_sideways_cylinder_uses_side_interior_not_just_cap_vertices():
    result = contact.hull_clearance(state(altitude=6., q=dyn.IDENTITY), vehicle(), 10., 1.)
    assert result["signed_clearance_m"] == pytest.approx(5., abs=1e-8)
    assert result["point_body_m"] == pytest.approx((-1., 0., 5.), abs=1e-8)


def test_polar_contact_uses_oblate_earth_not_equatorial_sphere():
    s = state(q=dyn.IDENTITY, r_eci_m=(0., 0., env.EARTH_POLAR_RADIUS_M+6.))
    result = contact.hull_clearance(s, vehicle(), 10., 1.)
    assert result["signed_clearance_m"] == pytest.approx(env.EARTH_EQUATORIAL_RADIUS_M/env.EARTH_POLAR_RADIUS_M, abs=1e-8)
    assert result["point_body_m"] == pytest.approx((0., 0., 0.), abs=1e-8)


@pytest.mark.parametrize("axis,angle", [((1., 2., 3.), .7), ((0., 1., 0.), 1.55), ((1., -.3, .1), 2.8)])
def test_quadratic_minimum_is_conservative_against_independent_dense_surface(axis, angle):
    q = dyn.axis_angle(axis, angle)
    base = env.surface_state(53., -71., 30., 0.)
    s = state(q=q, r_eci_m=base.r)
    result = contact.hull_clearance(s, vehicle(), 10., 1.)
    theta = np.linspace(0., 2*math.pi, 1201)
    zz = np.linspace(0., 10., 81)
    side = np.array([[math.cos(t), math.sin(t), z] for z in zz for t in theta])
    # Include cap centers; ring resolution bounds only this independent oracle.
    points = np.concatenate([side, np.array([[0., 0., 0.], [0., 0., 10.]])])
    rotation = np.column_stack([dyn.rotate(q, a) for a in ((1.,0.,0.), (0.,1.,0.), (0.,0.,1.))])
    inertial = np.asarray(s.r_eci_m)+(points-np.array([0., 0., 5.]))@rotation.T
    scaled = inertial*np.array([1., 1., env.EARTH_EQUATORIAL_RADIUS_M/env.EARTH_POLAR_RADIUS_M])
    sampled = np.linalg.norm(scaled, axis=1).min()-env.EARTH_EQUATORIAL_RADIUS_M
    assert result["signed_clearance_m"] <= sampled+1e-8
    assert sampled-result["signed_clearance_m"] < .002
    witness = result["point_body_m"]
    assert 0 <= witness[2] <= 10
    assert math.hypot(*witness[:2]) <= 1+1e-12


def test_contact_is_bracketed_and_velocity_is_not_clamped():
    before = state()
    after, receipt = contact.find_contact(before, vehicle(), dyn.Command6DOF(), .2, 10., 1.,
                                          gravity=False, atmosphere=False)
    assert receipt["contact"] is True
    assert after.time_s == pytest.approx(.1, abs=1e-8)
    assert after.v_eci_mps == before.v_eci_mps
    assert after.q_body_to_eci == before.q_body_to_eci
    assert -1e-6 <= receipt["signed_clearance_m"] <= 0
    assert receipt["time_bracket_s"][1]-receipt["time_bracket_s"][0] <= 1e-8
    assert receipt["landing_verified"] is False
    assert receipt["contact_response_modeled"] is False


def test_no_contact_advances_the_whole_requested_interval():
    after, receipt = contact.find_contact(state(altitude=100), vehicle(), dyn.Command6DOF(), .2, 10., 1.,
                                          gravity=False, atmosphere=False)
    assert receipt["contact"] is False
    assert after.time_s == .2
    assert receipt["complete_swept_volume_test"] is False


def test_contact_speed_includes_spin_at_the_contact_point_and_earth_rotation():
    before = state(altitude=5.)
    earth = (0., 0., env.EARTH_ROTATION_RAD_S)
    before = replace(before, v_eci_mps=env.cross(earth, before.r_eci_m),
                     omega_body_rad_s=env.add(dyn.inverse_rotate(before.q_body_to_eci, earth), (0., 2., 0.)))
    after, receipt = contact.find_contact(before, vehicle(), dyn.Command6DOF(), .1, 10., 1.)
    assert after == before
    assert receipt["initial_overlap"] is True
    assert receipt["surface_relative_speed_mps"] == pytest.approx(10., abs=1e-8)
    assert receipt["omega_cross_lever_applied"] is True
    assert receipt["surface_tangential_speed_mps"] == pytest.approx(10., abs=1e-8)


def test_contact_material_point_velocity_includes_changing_body_centroid():
    # This engine consumes exactly one kg/s. The tank centroid is above dry CoM.
    engine = dyn.Engine("main", (0., 0., 0.), 100., 100./env.STANDARD_GRAVITY_MPS2)
    v = replace(vehicle(), tank_center_body_m=(0., 0., 7.), engines=(engine,))
    com_z = (100.*5.+10.*7.)/110.
    before = state(altitude=com_z, propellant_kg=10., engine_states=(dyn.EngineState(1.),))
    earth = (0., 0., env.EARTH_ROTATION_RAD_S)
    before = replace(before, v_eci_mps=env.cross(earth, before.r_eci_m),
                     omega_body_rad_s=dyn.inverse_rotate(before.q_body_to_eci, earth))
    _, receipt = contact.find_contact(before, v, dyn.Command6DOF((dyn.EngineCommand(True, 1.),)), .1, 10., 1.)
    # Independent finite difference of the material base location at fixed CoM.
    eps = 1e-4
    next_com = (100.*5.+(10.-eps)*7.)/(110.-eps)
    expected = (com_z-next_com)/eps
    assert receipt["surface_normal_speed_mps"] == pytest.approx(expected, abs=2e-8)
    assert receipt["surface_tangential_speed_mps"] < 1e-7
    assert receipt["moving_com_correction_applied"] is True


def test_changing_integration_interval_does_not_change_bracketed_contact_time():
    full, _ = contact.find_contact(state(), vehicle(), dyn.Command6DOF(), .2, 10., 1.,
                                    gravity=False, atmosphere=False)
    mid, first = contact.find_contact(state(), vehicle(), dyn.Command6DOF(), .06, 10., 1.,
                                      gravity=False, atmosphere=False)
    divided, second = contact.find_contact(mid, vehicle(), dyn.Command6DOF(), .06, 10., 1.,
                                           gravity=False, atmosphere=False)
    assert first["contact"] is False and second["contact"] is True
    assert divided.time_s == pytest.approx(full.time_s, abs=1e-8)
    assert divided.v_eci_mps == full.v_eci_mps


@pytest.mark.parametrize("length,radius", [(0., 1.), (10., -1.), (math.inf, 1.), (10., math.nan), (True, 1.)])
def test_reject_invalid_envelope(length, radius):
    with pytest.raises(ValueError):
        contact.hull_clearance(state(), vehicle(), length, radius)


def test_initial_overlap_still_rejects_invalid_step_or_command():
    for dt in (0., -1., math.inf, 11.):
        with pytest.raises(ValueError):
            contact.find_contact(state(altitude=3.), vehicle(), dyn.Command6DOF(), dt, 10., 1.)
    with pytest.raises(ValueError, match="count"):
        contact.find_contact(state(altitude=3.), vehicle(), dyn.Command6DOF((dyn.EngineCommand(),)), .1, 10., 1.)

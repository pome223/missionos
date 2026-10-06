"""Analytic mechanics, actuator and event checks; no spacecraft fit claims."""
from dataclasses import asdict, replace
import math

import pytest

from src.runtime import starship_physics as env
from src.runtime import starship_sixdof as sd


def vehicle(**kwargs):
    values = dict(dry_mass_kg=100.0, dry_com_body_m=(0.0, 0.0, 0.0),
                  dry_inertia_kg_m2=((100.0, 0.0, 0.0), (0.0, 110.0, 0.0), (0.0, 0.0, 120.0)),
                  propellant_capacity_kg=20.0, tank_center_body_m=(0.0, 0.0, 0.0),
                  tank_radius_m=1.0, tank_length_m=2.0)
    values.update(kwargs)
    return sd.Vehicle6DOF(**values)


def state(**kwargs):
    values = dict(time_s=0.0, r_eci_m=(7_000_000.0, 0.0, 0.0), v_eci_mps=(0.0, 0.0, 0.0),
                  q_body_to_eci=(1.0, 0.0, 0.0, 0.0), omega_body_rad_s=(0.0, 0.0, 0.0),
                  propellant_kg=0.0)
    values.update(kwargs)
    return sd.State6DOF(**values)


def advance(s, v, c=sd.Command6DOF(), duration=1.0, dt=.05, **kw):
    for _ in range(round(duration/dt)):
        s = sd.step(s, v, c, dt, gravity=False, atmosphere=False, **kw)
    return s


def test_quaternion_rotation_convention_and_antiparallel_mapping():
    q = sd.axis_angle((0, 0, 1), math.pi/2)
    assert sd.rotate(q, (1, 0, 0)) == pytest.approx((0, 1, 0), abs=1e-14)
    assert sd.inverse_rotate(q, (0, 1, 0)) == pytest.approx((1, 0, 0), abs=1e-14)
    for direction in ((0, 0, -1), (1, 0, 0), (.2, .3, .7)):
        q = sd.quaternion_from_two_vectors((0, 0, 1), direction)
        assert sd.rotate(q, (0, 0, 1)) == pytest.approx(env.unit(direction), abs=1e-12)
        assert sd.quaternion_multiply(q, sd.quaternion_conjugate(q)) == pytest.approx(sd.IDENTITY)


def test_constant_body_torque_matches_analytic_rotation_from_rest():
    v, s = vehicle(), state()
    result = advance(s, v, duration=4.0, external_torque_body_nm=(0, 0, 12.0))
    assert result.omega_body_rad_s == pytest.approx((0, 0, .4), abs=1e-10)
    assert result.q_body_to_eci == pytest.approx(sd.axis_angle((0, 0, 1), .8), abs=1e-8)
    assert result.r_eci_m == s.r_eci_m


def test_body_force_rotates_with_integrated_attitude_and_changes_translation():
    s = state(omega_body_rad_s=(0, 0, 1))
    result = advance(s, vehicle(), duration=2.0, external_force_body_n=(100.0, 0, 0))
    assert result.v_eci_mps == pytest.approx((math.sin(2), 1-math.cos(2), 0), abs=2e-7)
    assert env.add(result.r_eci_m, env.scale(s.r_eci_m, -1)) == pytest.approx(
        (1-math.cos(2), 2-math.sin(2), 0), abs=2e-7)


def test_free_asymmetric_full_inertia_conserves_inertial_momentum_and_energy():
    v = vehicle(dry_inertia_kg_m2=((100, 12, -7), (12, 125, 4), (-7, 4, 140)))
    s = state(omega_body_rad_s=(.3, -.2, .4))
    initial = sd.observe(s, v, atmosphere=False)
    result = advance(s, v, duration=10.0, dt=.025)
    final = sd.observe(result, v, atmosphere=False)
    assert final["angular_momentum_eci_kg_m2_s"] == pytest.approx(initial["angular_momentum_eci_kg_m2_s"], abs=1e-7)
    assert final["rotational_energy_j"] == pytest.approx(initial["rotational_energy_j"], abs=1e-8)
    assert sum(x*x for x in result.q_body_to_eci) == pytest.approx(1.0, abs=1e-14)
    assert result.omega_body_rad_s != s.omega_body_rad_s


def test_tank_mass_properties_full_tensor_and_derivatives():
    v = vehicle(tank_center_body_m=(2.0, -1.0, 3.0))
    p = sd.mass_properties(v, 10)
    assert p.mass_kg == 110
    assert p.com_body_m == pytest.approx((2/11, -1/11, 3/11))
    assert p.inertia_kg_m2[0][1] > 0
    assert p.inertia_kg_m2[0][2] < 0
    delta = .001
    a, b = sd.mass_properties(v, 10-delta), sd.mass_properties(v, 10+delta)
    for i in range(3):
        assert (b.com_body_m[i]-a.com_body_m[i])/(2*delta) == pytest.approx(p.dcom_dpropellant_m_per_kg[i], rel=1e-8)
        for j in range(3):
            assert (b.inertia_kg_m2[i][j]-a.inertia_kg_m2[i][j])/(2*delta) == pytest.approx(p.dinertia_dpropellant_m2[i][j], abs=1e-8)
    assert sd.mass_properties(v, 0).inertia_kg_m2 == v.dry_inertia_kg_m2


def test_rocket_equation_and_burnout_split_no_fuel_or_impulse_beyond_empty():
    e = sd.Engine("center", (0, 0, -2), 1000, 10, min_throttle=0)
    v = vehicle(engines=(e,))
    s = state(propellant_kg=10, engine_states=(sd.EngineState(throttle=1),))
    c = sd.Command6DOF((sd.EngineCommand(True, 1),))
    result = sd.step(s, v, c, 2.0, gravity=False, atmosphere=False)
    expected = 10*env.STANDARD_GRAVITY_MPS2*math.log(110/100)
    assert result.propellant_kg == 0
    assert result.v_eci_mps == pytest.approx((0, 0, expected), abs=2e-5)
    assert result.omega_body_rad_s == (0, 0, 0)
    next_state = sd.step(result, v, c, 1, gravity=False, atmosphere=False)
    assert next_state.v_eci_mps == result.v_eci_mps
    assert sd.observe(result, v, c)["mass_flow_kg_s"] == 0


def test_throttle_finite_response_and_rate_limit():
    e = sd.Engine("center", (0, 0, -2), 20, 300, min_throttle=0,
                  throttle_time_constant_s=.5, throttle_rate_per_s=100)
    v = vehicle(engines=(e,))
    s = state(propellant_kg=10, engine_states=(sd.EngineState(),))
    c = sd.Command6DOF((sd.EngineCommand(True, 1),))
    result = advance(s, v, c, duration=1)
    assert result.engine_states[0].throttle == pytest.approx(1-math.exp(-2), abs=4e-7)
    slow = replace(v, engines=(replace(e, throttle_rate_per_s=.1),))
    result = advance(s, slow, c, duration=1)
    assert result.engine_states[0].throttle == pytest.approx(.1, abs=1e-12)
    assert result.propellant_kg < s.propellant_kg


def test_gimbal_changes_torque_then_attitude_and_trajectory():
    e = sd.Engine("gimbal", (0, 0, -2), 100, 300, min_throttle=0)
    v = vehicle(engines=(e,))
    s = state(propellant_kg=10, engine_states=(sd.EngineState(1),))
    c = sd.Command6DOF((sd.EngineCommand(True, 1, gimbal_y_rad=.1),))
    result = advance(s, v, c, duration=.5)
    assert 0 < result.engine_states[0].gimbal_y_rad < .1
    assert result.omega_body_rad_s[1] < 0
    assert result.q_body_to_eci[2] < 0
    assert result.v_eci_mps[0] > 0
    assert abs(result.engine_states[0].gimbal_y_rad) <= e.gimbal_rate_rad_s*.5


def test_engine_out_uses_geometry_and_does_not_prescribe_attitude():
    engines = (sd.Engine("left", (-1, 0, -2), 100, 300, min_throttle=0),
               sd.Engine("right", (1, 0, -2), 100, 300, min_throttle=0))
    v = vehicle(engines=engines)
    s = state(propellant_kg=10, engine_states=(sd.EngineState(1), sd.EngineState(1, available=False)))
    c = sd.Command6DOF((sd.EngineCommand(True, 1),)*2)
    result = advance(s, v, c, duration=.5)
    assert result.omega_body_rad_s[1] > 0
    observation = sd.observe(result, v, c, atmosphere=False)
    assert observation["engine_loads"][0]["thrust_n"] == 100
    assert observation["engine_loads"][1]["thrust_n"] == 0
    assert observation["attitude_prescribed"] is False


def test_rcs_mount_axis_produces_physical_pair_torque_and_consumes_fuel():
    engines = (sd.Engine("a", (1, 0, 0), 10, 80, min_throttle=0, max_gimbal_rad=0, direction_body=(0, 1, 0)),
               sd.Engine("b", (-1, 0, 0), 10, 80, min_throttle=0, max_gimbal_rad=0, direction_body=(0, -1, 0)))
    v = vehicle(engines=engines)
    s = state(propellant_kg=10, engine_states=(sd.EngineState(1),)*2)
    c = sd.Command6DOF((sd.EngineCommand(True, 1),)*2)
    result = advance(s, v, c, duration=.5)
    assert result.v_eci_mps == (0, 0, 0)
    assert result.omega_body_rad_s[2] > 0
    assert result.propellant_kg < 10
    assert sd.observe(result, v)["thrust_torque_body_nm"] == pytest.approx((0, 0, 20))


def test_panel_force_opposes_local_wind_and_generates_torque():
    panel = sd.AeroPanel("flap", (0, 0, 2), (1, 0, 0), 1)
    v = vehicle(aero_panels=(panel,))
    # Surface atmosphere corotates; add +X velocity relative to it.
    r = (env.EARTH_EQUATORIAL_RADIUS_M+1000, 0, 0)
    velocity = env.add(env.cross((0, 0, env.EARTH_ROTATION_RAD_S), r), (100, 0, 0))
    s = state(r_eci_m=r, v_eci_mps=velocity, flap_angles_rad=(0,))
    c = sd.Command6DOF(flap_angles_rad=(0,))
    obs = sd.observe(s, v, c)
    assert obs["aero_force_body_n"][0] < 0
    assert obs["aero_torque_body_nm"][1] < 0
    assert env.dot(obs["panel_loads"][0]["force_body_n"], obs["panel_loads"][0]["local_air_velocity_body_mps"]) < 0
    result = sd.step(s, v, c, .01, gravity=False)
    assert result.omega_body_rad_s[1] < 0
    assert result.q_body_to_eci != s.q_body_to_eci


def test_panel_actuator_is_rate_limited_and_different_orientation_changes_load():
    panel = sd.AeroPanel("flap", (0, 0, 2), (1, 0, 0), 1,
                         hinge_axis_body=(0, 1, 0), deflection_rate_rad_s=.1)
    v = vehicle(aero_panels=(panel,))
    s = state(flap_angles_rad=(0,))
    result = advance(s, v, sd.Command6DOF(flap_angles_rad=(.4,)), duration=1)
    assert result.flap_angles_rad == pytest.approx((.1,), abs=1e-12)
    assert result.q_body_to_eci == s.q_body_to_eci


def test_failed_surface_crossing_is_not_clamped_to_safe_landing():
    s = state(r_eci_m=(env.EARTH_EQUATORIAL_RADIUS_M+1, 0, 0), v_eci_mps=(-10, 0, 0))
    result = sd.step(s, vehicle(), sd.Command6DOF(), .2, gravity=False, atmosphere=False)
    assert sd.observe(result, vehicle())["altitude_m"] == pytest.approx(-1, abs=1e-8)
    assert result.v_eci_mps == (-10, 0, 0)


def test_explicit_instantaneous_mass_convention_and_roundtrip():
    v = vehicle(engines=(sd.Engine("a", (0, 0, -2), 100, 300),))
    s = state(propellant_kg=10, engine_states=(sd.EngineState(),))
    c = sd.Command6DOF((sd.EngineCommand(True, .5),))
    assert sd.state_from_dict(asdict(s)) == s
    assert sd.vehicle_from_dict(asdict(v)) == v
    assert sd.command_from_dict(asdict(c)) == c
    obs = sd.observe(s, v, c)
    assert obs["variable_mass_convention"] == "instantaneous_properties_without_depletion_rate_loads"
    assert obs["depletion_rate_loads_modeled"] is False
    assert obs["six_dof_integrated"] is True
    assert obs["starship_vehicle_validated"] is False


@pytest.mark.parametrize("kwargs", [dict(propellant_kg=-1), dict(q_body_to_eci=(0, 0, 0, 0)),
                                   dict(omega_body_rad_s=(0, math.nan, 0)), dict(time_s=True)])
def test_reject_invalid_states(kwargs):
    with pytest.raises(ValueError):
        state(**kwargs)


@pytest.mark.parametrize("matrix", [((1, 0, 0), (0, 1, 0), (0, 0, 3)),
                                    ((1, 2, 0), (2, 1, 0), (0, 0, 1)),
                                    ((1, 1, 0), (0, 1, 0), (0, 0, 1))])
def test_reject_unphysical_or_nonsymmetric_inertia(matrix):
    with pytest.raises(ValueError):
        vehicle(dry_inertia_kg_m2=matrix)


def test_reject_unapproved_actuator_domain_and_missing_engine_state():
    v = vehicle(engines=(sd.Engine("a", (0, 0, 0), 100, 300),))
    with pytest.raises(ValueError, match="count"):
        sd.step(state(), v, sd.Command6DOF(), .1)
    s = state(propellant_kg=10, engine_states=(sd.EngineState(),))
    with pytest.raises(ValueError, match="minimum"):
        sd.step(s, v, sd.Command6DOF((sd.EngineCommand(True, .1),)), .1)
    with pytest.raises(ValueError, match="gimbal"):
        sd.step(s, v, sd.Command6DOF((sd.EngineCommand(True, .5, .9),)), .1)
    with pytest.raises(ValueError, match="capacity"):
        sd.mass_properties(v, 21)


def test_gravity_gradient_vanishes_on_a_principal_radial_axis():
    v, s = vehicle(), state()
    obs = sd.observe(s, v, atmosphere=False)
    assert obs["gravity_gradient_torque_body_nm"] == [0, 0, 0]
    assert obs["central_gravity_gradient_modeled"] is True
    assert obs["j2_gravity_gradient_modeled"] is False
    assert sd.derivatives(s, v, sd.Command6DOF(), atmosphere=False)[10:13] == [0, 0, 0]


def test_gravity_gradient_off_axis_matches_analytic_sign_magnitude_and_r_cubed():
    v = vehicle()
    s = state(q_body_to_eci=sd.axis_angle((0, 0, 1), math.pi/4))
    # Radial n in body = (1/sqrt(2), -1/sqrt(2), 0).
    # (n cross I n)_z = (Iyy-Ixx)*nx*ny = -5 kg m^2.
    expected_torque_z = -5*3*env.EARTH_MU_M3_S2/(7_000_000**3)
    obs = sd.observe(s, v, atmosphere=False)
    assert obs["gravity_gradient_torque_body_nm"] == pytest.approx((0, 0, expected_torque_z), abs=1e-17)
    derivative = sd.derivatives(s, v, sd.Command6DOF(), atmosphere=False)
    assert derivative[10:13] == pytest.approx((0, 0, expected_torque_z/120), abs=1e-17)
    distant = replace(s, r_eci_m=(14_000_000, 0, 0))
    assert sd.observe(distant, v)["gravity_gradient_torque_body_nm"][2] == pytest.approx(expected_torque_z/8)
    for kwargs in ({"gravity": False}, {"gravity_gradient": False}):
        assert sd.derivatives(s, v, sd.Command6DOF(), atmosphere=False, **kwargs)[10:13] == [0, 0, 0]
        assert sd.observe(s, v, **kwargs)["central_gravity_gradient_modeled"] is False


def test_gravity_gradient_uses_evolving_attitude_inside_rk_stages():
    v, s = vehicle(), state(omega_body_rad_s=(0, 0, 1))
    # Initial torque is zero; freezing it for a whole step would keep wz == 1.
    assert sd.observe(s, v)["gravity_gradient_torque_body_nm"] == [0, 0, 0]
    result = sd.step(s, v, sd.Command6DOF(), 1, atmosphere=False)
    disabled = sd.step(s, v, sd.Command6DOF(), 1, atmosphere=False, gravity_gradient=False)
    assert result.omega_body_rad_s[2] < disabled.omega_body_rad_s[2] - 1e-9
    assert disabled.omega_body_rad_s[2] == pytest.approx(1)

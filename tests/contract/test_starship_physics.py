"""Independent physical invariants for the prescribed-attitude 3DOF kernel."""

from dataclasses import replace
import json
import math

import pytest

from src.runtime.starship_physics import (
    AIR_SPECIFIC_GAS_CONSTANT,
    ATMOSPHERE_GEOPOTENTIAL_RADIUS_M,
    EARTH_EQUATORIAL_RADIUS_M as RE,
    EARTH_MU_M3_S2 as MU,
    EARTH_ROTATION_RAD_S,
    STANDARD_GRAVITY_MPS2 as G0,
    Control,
    State3D,
    Vehicle,
    add,
    aerodynamic_force,
    air_relative_velocity,
    dot,
    ecef_to_eci,
    ecef_to_geodetic,
    eci_to_ecef,
    geodetic_to_ecef,
    gravity_acceleration,
    local_frame,
    norm,
    observe,
    orbital_elements,
    scale,
    specific_energy,
    standard_atmosphere,
    step,
    surface_state,
)


PASSIVE = Vehicle(1000.0, 0.0, 0.0, 0.0, 300.0, 0.0)


@pytest.mark.parametrize("lat,lon,height", [
    (0, 0, 0), (25.997, -97.156, 20), (-65, 170, 86_000),
    (89.99999, -160, 1000), (90, 0, 0), (-90, 0, 400_000),
    (45, -179.9, 36_000_000), (40, 90, -30),
])
def test_wgs84_round_trip_and_poles(lat, lon, height):
    point = geodetic_to_ecef(math.radians(lat), math.radians(lon), height)
    result = ecef_to_geodetic(point)
    assert math.degrees(result[0]) == pytest.approx(lat, abs=1e-9)
    assert math.degrees(result[1]) == pytest.approx(lon, abs=1e-9)
    assert result[2] == pytest.approx(height, abs=1e-6)


def test_reference_frame_rotation_is_invertible_and_quarter_day_rotates_axes():
    quarter_sidereal_day = math.pi/(2*EARTH_ROTATION_RAD_S)
    assert ecef_to_eci((1, 0, 0), quarter_sidereal_day) == pytest.approx((0, 1, 0), abs=1e-15)
    vector = (123, -52, 9999)
    assert eci_to_ecef(ecef_to_eci(vector, 1739), 1739) == pytest.approx(vector, abs=1e-10)


def test_rotating_ground_station_has_zero_air_speed_and_orthonormal_local_axes():
    state = surface_state(26, -97, 100, 5, 1700)
    assert norm(air_relative_velocity(state)) < 1e-10
    assert norm(state.v) > 400
    up, east, north = local_frame(state)
    assert [norm(a) for a in (up, east, north)] == pytest.approx([1, 1, 1])
    assert abs(dot(up, east))+abs(dot(up, north))+abs(dot(east, north)) < 1e-14
    data = observe(state, PASSIVE)
    assert data["ground_speed_mps"] < 1e-10
    assert data["latitude_deg"] == pytest.approx(26)
    assert data["longitude_deg"] == pytest.approx(-97)


@pytest.mark.parametrize("h,temperature,pressure", [
    (0, 288.15, 101325), (11000, 216.65, 22632.06),
    (20000, 216.65, 5474.889), (32000, 228.65, 868.0187),
    (47000, 270.65, 110.9063), (51000, 270.65, 66.93887),
    (71000, 214.65, 3.956420), (84852, 186.946, 0.3733836),
])
def test_standard_atmosphere_matches_published_geopotential_layer_values(h, temperature, pressure):
    # NASA/TM-2005-213659 layer table; conversion to geometric height is explicit.
    geometric = ATMOSPHERE_GEOPOTENTIAL_RADIUS_M*h/(ATMOSPHERE_GEOPOTENTIAL_RADIUS_M-h)
    atmosphere = standard_atmosphere(geometric)
    assert atmosphere["temperature_k"] == pytest.approx(temperature, abs=0.00001)
    assert atmosphere["pressure_pa"] == pytest.approx(pressure, rel=2e-6)
    assert atmosphere["density_kg_m3"] == pytest.approx(pressure/(AIR_SPECIFIC_GAS_CONSTANT*temperature), rel=2e-6)


def test_upper_atmosphere_is_continuous_but_explicitly_surrogate():
    below = standard_atmosphere(86000)
    above = standard_atmosphere(86000.001)
    assert below["regime"] == "coesa1976_lower_molecular_scale"
    assert above["regime"] == "unvalidated_exponential_above_86km"
    assert below["density_kg_m3"] == pytest.approx(above["density_kg_m3"], rel=1e-6)
    assert standard_atmosphere(1_000_000)["density_kg_m3"] == 0


def test_j2_acceleration_matches_negative_potential_gradient():
    state = State3D(0, (5_000_000, -2_000_000, 4_000_000), (0, 0, 0), 0)
    analytic = gravity_acceleration(state.r)
    gradient = []
    for axis in range(3):
        offset = tuple(0.5 if i == axis else 0 for i in range(3))
        plus = replace(state, r=add(state.r, offset))
        minus = replace(state, r=add(state.r, scale(offset, -1)))
        gradient.append(-(specific_energy(plus)-specific_energy(minus)))
    assert analytic == pytest.approx(gradient, abs=3e-8)
    assert norm(add(analytic, scale(gravity_acceleration(state.r, j2=False), -1))) > 0.001


def _orbit(steps, j2=False):
    radius = RE+400_000
    initial = State3D(0, (radius, 0, 0), (0, math.sqrt(MU/radius)*math.cos(0.6), math.sqrt(MU/radius)*math.sin(0.6)), 0)
    period = 2*math.pi*math.sqrt(radius**3/MU)
    state = initial
    for _ in range(steps):
        state = step(state, PASSIVE, Control(), period/steps, j2=j2, atmosphere=False, stop_at_ground=False)
    return initial, state


def test_circular_orbit_returns_to_initial_state_without_j2():
    initial, final = _orbit(2000)
    assert norm(add(final.r, scale(initial.r, -1))) < 0.001
    assert norm(add(final.v, scale(initial.v, -1))) < 1e-6
    elements = orbital_elements(initial)
    assert elements["perigee_altitude_m"] == pytest.approx(400_000, abs=1e-6)
    assert elements["apogee_altitude_m"] == pytest.approx(400_000, abs=1e-6)
    assert elements["inclination_deg"] == pytest.approx(math.degrees(0.6))


def test_j2_conserves_its_own_energy_and_resolution_converges():
    initial, coarse = _orbit(200, j2=True)
    _, fine = _orbit(400, j2=True)
    coarse_error = abs(specific_energy(coarse)-specific_energy(initial))
    fine_error = abs(specific_energy(fine)-specific_energy(initial))
    assert fine_error < coarse_error/20
    assert fine_error/abs(specific_energy(initial)) < 1e-9
    assert norm(add(fine.r, scale(initial.r, -1))) > 1000  # Physical J2 precession, not numerical closure.


def test_vacuum_finite_burn_matches_tsiolkovsky_and_mass_conservation():
    vehicle = Vehicle(500, 0, 0, 0, 300, 20000)
    control = Control(1, (1, 0, 0))
    state = State3D(0, (RE+400_000, 0, 0), (0, 0, 0), 500)
    burn_time = 500*300*G0/20000
    while state.time_s < burn_time+10:
        state = step(state, vehicle, control, min(0.2, burn_time+10-state.time_s), gravity=False, atmosphere=False, stop_at_ground=False)
    assert state.propellant_kg == 0
    assert state.v[0] == pytest.approx(300*G0*math.log(2), abs=1e-7)
    assert state.v[1:] == (0, 0)
    coasting = step(state, vehicle, control, 100, gravity=False, atmosphere=False, stop_at_ground=False)
    assert coasting.v == state.v  # Requested thrust cannot create propellant.


def test_fuel_depletion_inside_one_step_does_not_spread_thrust_over_remaining_time():
    vehicle = Vehicle(1000, 0, 0, 0, 300, 300*G0)
    state = State3D(0, (RE+400_000, 0, 0), (0, 0, 0), 1)
    control = Control(1, (1, 0, 0))
    result = step(state, vehicle, control, 10, gravity=False, atmosphere=False, stop_at_ground=False)
    separate = step(state, vehicle, control, 1, gravity=False, atmosphere=False, stop_at_ground=False)
    separate = step(separate, vehicle, control, 9, gravity=False, atmosphere=False, stop_at_ground=False)
    assert result.propellant_kg == 0
    assert result.r == separate.r
    assert result.v == separate.v
    assert result.v[0] == pytest.approx(300*G0*math.log(1001/1000), abs=1e-9)


def test_drag_dissipates_relative_energy_and_lift_is_perpendicular_to_airflow():
    surface = surface_state(0, 0, 10000)
    state = replace(surface, v=add(surface.v, (0, 1000, -100)))
    drag_vehicle = Vehicle(1000, 20, 1.2, 0, 300, 0)
    lift_vehicle = replace(drag_vehicle, cd=0, cl=0.8)
    flow = air_relative_velocity(state)
    drag = aerodynamic_force(state, drag_vehicle, Control())
    assert dot(flow, drag) < 0
    positive = aerodynamic_force(state, lift_vehicle, Control(bank_rad=0))
    negative = aerodynamic_force(state, lift_vehicle, Control(bank_rad=math.pi))
    assert dot(flow, positive) == pytest.approx(0, abs=1e-5)
    assert norm(add(positive, negative)) < 1e-8
    quarter = aerodynamic_force(state, lift_vehicle, Control(bank_rad=math.pi/2))
    assert dot(positive, quarter)/(norm(positive)*norm(quarter)) == pytest.approx(0, abs=1e-15)


def test_ground_event_solves_time_and_preserves_impact_velocity():
    state = State3D(0, (RE+100, 0, 0), (-10, 0, 0), 0)
    result = step(state, PASSIVE, Control(), 20, gravity=False, atmosphere=False)
    assert result.time_s == pytest.approx(10, abs=1e-6)
    assert result.r == pytest.approx((RE, 0, 0), abs=1e-8)
    assert result.v == (-10, 0, 0)
    assert observe(result, PASSIVE)["vertical_speed_mps"] == pytest.approx(-10, abs=1e-12)
    assert step(result, PASSIVE, Control(), 20, gravity=False, atmosphere=False) == result


def test_powered_launch_from_surface_can_leave_the_ground():
    vehicle = Vehicle(1000, 0, 0, 0, 300, 40_000)
    initial = surface_state(26, -97, propellant_kg=1000)
    result = step(initial, vehicle, Control(1, local_frame(initial)[0]), 0.1)
    assert result.time_s == 0.1
    assert observe(result, vehicle)["altitude_m"] > 0.04


def test_unbound_orbit_is_json_safe_and_does_not_claim_apoapsis():
    state = State3D(0, (RE+400_000, 0, 0), (0, 15000, 0), 0)
    result = observe(state, PASSIVE)
    assert result["orbit"]["status"] == "unbound_or_degenerate"
    assert result["orbit"]["apogee_altitude_m"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
def test_nonfinite_or_boolean_inputs_are_rejected(bad):
    with pytest.raises(ValueError):
        State3D(0, (RE, bad, 0), (0, 0, 0), 0)
    with pytest.raises(ValueError):
        replace(PASSIVE, cd=bad)
    with pytest.raises(ValueError):
        Control(throttle=bad)
    with pytest.raises(ValueError):
        step(surface_state(0, 0, 100), PASSIVE, Control(), bad)


def test_unphysical_controls_and_subsurface_initial_conditions_are_rejected():
    with pytest.raises(ValueError):
        Control(1.1, (1, 0, 0))
    with pytest.raises(ValueError):
        Control(1, (0, 0, 0))
    with pytest.raises(ValueError):
        replace(PASSIVE, cd=-1)
    with pytest.raises(ValueError):
        step(surface_state(0, 0, -20), PASSIVE, Control(), 1)

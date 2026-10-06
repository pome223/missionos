"""Short physical wind checks; no full flight, weather or catch-value claim."""
from copy import deepcopy
from dataclasses import asdict, FrozenInstanceError, replace
import math
from pathlib import Path
import json

import pytest

from src.runtime import starship_physics as env
from src.runtime import starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import vehicle as mission_vehicle, _sample, stack_vehicle, simulate
from src.runtime.starship_wind import WindField, wind_from_dict, wind_from_profile
from src.runtime.starship_wind_verifier import validate_wind_profile, verify_wind_observation

ROOT = Path(__file__).resolve().parents[2]


def profile(wind=None):
    result = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    if wind is not None:
        result["environment"] = {"wind": json.loads(json.dumps(asdict(wind)))}
    return result


def plate_vehicle(wind=None, *, mass=1e6, engines=()):
    return dyn.Vehicle6DOF(mass, (0., 0., 0.), ((mass, 0., 0.), (0., mass, 0.), (0., 0., mass)),
        20., (0., 0., 0.), 1., 2., engines=engines,
        aero_panels=(dyn.AeroPanel("plate", (0., 0., 0.), (0., 1., 0.), 1., 2., 0., max_deflection_rad=0.),),
        wind=wind)


def state(*, time=0., lat=0., lon=0., fuel=0., engines=()):
    base = env.surface_state(lat, lon, 1000., time_s=time)
    return dyn.State6DOF(time, base.r, base.v, dyn.IDENTITY, (0., 0., 0.), fuel,
                        tuple(dyn.EngineState(throttle=1.) for _ in engines), (0.,))


def command(v):
    return dyn.Command6DOF(tuple(dyn.EngineCommand(enabled=True, throttle=1.) for _ in v.engines), (0.,))


@pytest.mark.parametrize("boundary", ("return", "catch"))
def test_windy_booster_samples_cross_independent_record_boundary(boundary):
    """Both producers share _sample; check the real finite integration paths."""
    from src.runtime.starship_booster_catch import simulate_catch
    from src.runtime.starship_booster_catch_verifier import verify_catch
    from src.runtime.starship_booster_recovery import simulate_recovery
    from src.runtime.starship_booster_recovery_verifier import verify_recovery
    from src.runtime.starship_sixdof_mission import _attitude
    p = profile(WindField((3., -2., 0.), (.5, .2, 0.), 12., .4))
    catch = json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())
    if boundary == "catch":
        run = json.loads(json.dumps(simulate_catch(p, catch, duration_s=.2)))
        verdict = verify_catch(run, p, catch)
    else:
        body = mission_vehicle(p, "booster")
        origin = env.surface_state(p["launch"]["latitude_deg"], p["launch"]["longitude_deg"], 80000., time_s=100.)
        up, east, _ = env.local_frame(origin)
        initial = dyn.State6DOF(100., origin.r,
            env.add(origin.v, env.add(env.scale(up, 600.), env.scale(east, 500.))),
            _attitude(up, east), (.001, .002, .003), 260000.,
            tuple(dyn.EngineState() for _ in body.engines), tuple(0. for _ in body.aero_panels))
        saved = json.loads(json.dumps(asdict(initial)))
        run = json.loads(json.dumps(simulate_recovery(p, saved, catch, duration_s=.2)))
        verdict = verify_recovery(run, saved, p, catch)
    assert verdict["passed"], verdict
    assert run["samples"][-1]["time_s"] > run["samples"][0]["time_s"]
    assert all("wind_eci_mps" in sample and "air_speed_mps" in sample for sample in run["samples"])
    for sample in run["samples"]:
        assert verify_wind_observation(sample, p)["passed"]


def test_default_and_explicit_zero_physical_states_and_samples_are_exactly_equal():
    engine = dyn.Engine("main", (0., 0., 0.), 20000., 200., min_throttle=0.)
    base = plate_vehicle(engines=(engine,))
    zero = replace(base, wind=WindField())
    a = b = state(time=7., fuel=2., engines=(engine,))
    for dt in (.1, .25, .5, 1., .1):
        a, b = dyn.step(a, base, command(base), dt), dyn.step(b, zero, command(zero), dt)
        assert asdict(a) == asdict(b)
        assert dyn.observe(a, base) == dyn.observe(b, zero)
        assert _sample(a, base, "zero_wind") == _sample(b, zero, "zero_wind")


@pytest.mark.parametrize("lat,lon,time", [(0., 0., 0.), (25.9, -97.2, 600.), (65., 150., 2000.)])
def test_declared_local_enu_transforms_to_current_inertial_axes(lat, lon, time):
    w = WindField((2., -3., .5), (1., 2., -.25), 20., .4)
    s = state(lat=lat, lon=lon, time=time)
    up, east, north = env.local_frame(env.State3D(time, s.r_eci_m, s.v_eci_mps, 0.))
    enu = w.enu(time)
    expected = tuple(sum(enu[j]*axis[i] for j, axis in enumerate((east, north, up))) for i in range(3))
    assert w.eci(s.r_eci_m, time) == pytest.approx(expected, abs=1e-12)


def test_opposite_winds_change_only_aero_loads_at_an_identical_state():
    s = state()
    east, west = plate_vehicle(WindField((5., 0., 0.))), plate_vehicle(WindField((-5., 0., 0.)))
    before = asdict(s)
    a, b = dyn.observe(s, east), dyn.observe(s, west)
    assert a["ground_speed_mps"] == b["ground_speed_mps"] == 0.
    assert a["air_speed_mps"] == b["air_speed_mps"] == 5.
    assert a["aero_force_body_n"][1] > 0 > b["aero_force_body_n"][1]
    assert a["thrust_force_body_n"] == b["thrust_force_body_n"]
    assert a["mass_kg"] == b["mass_kg"]
    assert asdict(s) == before
    moved = dyn.step(s, east, command(east), .5, gravity=False)
    assert moved.v_eci_mps[1] > s.v_eci_mps[1]
    assert moved.r_eci_m != s.r_eci_m


def test_air_and_ground_speed_split_in_samples_and_independent_checker():
    w, s = WindField((2., 0., 0.)), state(lat=25.9, lon=-97.2, time=500.)
    v, p = plate_vehicle(w), profile(w)
    observed, recorded = dyn.observe(s, v), _sample(s, v, "wind_test")
    assert observed["ground_speed_mps"] == 0.
    assert observed["air_speed_mps"] == pytest.approx(2., abs=1e-12)
    assert recorded["ground_speed_mps"] == 0.
    assert recorded["air_speed_mps"] == pytest.approx(2., abs=1e-12)
    assert verify_wind_observation(recorded, p)["passed"]
    assert verify_wind_observation(recorded, p)["dynamics_reexecuted"] is False


@pytest.mark.parametrize("scale", [1+4e-9, 1-4e-9])
def test_independent_air_rotation_normalizes_valid_near_unit_high_speed_state(scale):
    w = WindField((2., 0., 0.))
    base = state()
    quaternion = tuple(component*scale for component in dyn.axis_angle((1., 0., 0.), 1.))
    s = replace(base, q_body_to_eci=quaternion, v_eci_mps=env.add(base.v_eci_mps, (0., 7500., 0.)))
    assert 0 < abs(sum(x*x for x in quaternion)-1.) < 1e-8
    saved = _sample(s, plate_vehicle(w), "near_unit_wind")
    before = deepcopy(saved)
    assert verify_wind_observation(saved, profile(w))["passed"]
    assert saved == before  # normalized rotation is calculation-only.


@pytest.mark.parametrize("quaternion", [[0., 0., 0., 0.], [float("nan"), 0., 0., 0.],
                                        [1+6e-9, 0., 0., 0.]])
def test_air_rotation_normalization_does_not_relax_invalid_quaternion_gate(quaternion):
    w = WindField((2., 0., 0.))
    saved = _sample(state(), plate_vehicle(w), "wind_test")
    saved["q_body_to_eci"] = quaternion
    assert not verify_wind_observation(saved, profile(w))["passed"]
    with pytest.raises(ValueError):
        replace(state(), q_body_to_eci=quaternion)


def test_harmonic_curvature_uses_absolute_rk_stage_clock_in_real_airload():
    amplitude, period, initial_time, duration = 5., 20., 2., 1.
    w = WindField((0., 0., 0.), (amplitude, 0., 0.), period)
    calm, windy = plate_vehicle(), plate_vehicle(w)
    s = state(time=initial_time)
    a = dyn.step(s, calm, command(calm), duration, gravity=False)
    b = dyn.step(s, windy, command(windy), duration, gravity=False)
    angular = 2*math.pi/period
    integral = duration/2-(math.sin(2*angular*(initial_time+duration))-math.sin(2*angular*initial_time))/(4*angular)
    density = dyn.observe(s, calm)["atmosphere"]["density_kg_m3"]
    # Large mass limits motion: the exact sin² plate-force integral is a useful
    # independent approximation, distinct from sampling gust at macrostep start.
    expected_delta = density*amplitude**2*integral/calm.dry_mass_kg
    delta = b.v_eci_mps[1]-a.v_eci_mps[1]
    assert delta == pytest.approx(expected_delta, rel=5e-5, abs=1e-10)
    frozen_delta = density*(amplitude*math.sin(angular*initial_time))**2*duration/calm.dry_mass_kg
    assert abs(delta-frozen_delta) > .1*expected_delta


def test_fuel_root_and_post_depletion_gust_keep_absolute_clock(monkeypatch):
    engine = dyn.Engine("main", (0., 0., 0.), 1000., 200., min_throttle=0.)
    w = WindField((1., 0., 0.), (2., 0., 0.), 4., .3)
    v, s = plate_vehicle(w, engines=(engine,)), state(time=12.3, fuel=.03, engines=(engine,))
    clocks = []
    original = dyn._rk4
    def trace(*args, **kwargs):
        dt, clock = args[4], args[11] if len(args) > 11 else kwargs.get("time_s", 0.)
        clocks.append((clock, dt))
        return original(*args, **kwargs)
    monkeypatch.setattr(dyn, "_rk4", trace)
    coarse = dyn.step(s, v, command(v), .4, gravity=False)
    recorded_clocks = list(clocks)
    assert coarse.propellant_kg == 0.
    assert len(recorded_clocks) > 50  # actual fuel-event bisection occurred.
    assert all(s.time_s <= t <= s.time_s+.4 for t, _ in recorded_clocks)
    assert any(t > s.time_s and t < s.time_s+.125 for t, _ in recorded_clocks)
    fine = s
    for _ in range(80):
        fine = dyn.step(fine, v, command(v), .005, gravity=False)
    assert coarse.v_eci_mps == pytest.approx(fine.v_eci_mps, abs=1e-9)
    assert coarse.r_eci_m == pytest.approx(fine.r_eci_m, abs=1e-7)


def test_atmosphere_disabled_has_no_wind_force_or_state_effect():
    base = plate_vehicle()
    windy = replace(base, wind=WindField((10., -5., 2.), (3., 1., -1.), 4.))
    s = replace(state(time=8.), omega_body_rad_s=(.1, -.08, .05))
    a = dyn.step(s, base, command(base), .5, atmosphere=False)
    b = dyn.step(s, windy, command(windy), .5, atmosphere=False)
    assert asdict(a) == asdict(b)
    observation = dyn.observe(s, windy, atmosphere=False)
    assert observation["wind_applied"] is False
    assert observation["wind_eci_mps"] == [0., 0., 0.]
    assert observation["aero_force_body_n"] == (0., 0., 0.)
    assert verify_wind_observation(observation, profile(windy.wind), atmosphere=False)["passed"]


def test_profile_binding_serialization_stack_and_immutability():
    w = WindField((2., 1., 0.))
    p = profile(w)
    before = deepcopy(p)
    booster, ship = mission_vehicle(p, "booster"), mission_vehicle(p)
    assert booster.wind == ship.wind == w
    assert stack_vehicle(p, ship, booster).wind == w
    assert dyn.vehicle_from_dict(asdict(booster)) == booster
    legacy = asdict(plate_vehicle())
    legacy.pop("wind")
    assert dyn.vehicle_from_dict(legacy).wind is None
    assert wind_from_profile(p) == w
    assert validate_wind_profile(p) == p["environment"]["wind"]
    assert p == before
    with pytest.raises(FrozenInstanceError):
        w.mean_enu_mps = (3., 0., 0.)
    external = [1., 0., 0.]
    bound = WindField(external)
    external[0] = 99.
    assert bound.mean_enu_mps == (1., 0., 0.)


def test_short_production_harness_with_numpy_control_values_keeps_advancing():
    p = profile(WindField((2., 0., 0.), (1., 0., 0.)))
    run = simulate(p, duration_s=.2)
    assert run["outcome"]["termination"] == "time_limit"
    assert run["outcome"]["duration_s"] == .2
    # Saved JSON normalizes numerical scalar subtypes before independent checks.
    saved = json.loads(json.dumps(run["samples"], allow_nan=False))
    assert len(saved) >= 2
    assert all(verify_wind_observation(sample, p)["passed"] for sample in saved)
    assert run["final_state"]["r_eci_m"] != run["initial_state"]["r_eci_m"]


@pytest.mark.parametrize("field,value", [
    ("schema", "other"), ("frame", "body"), ("mean_enu_mps", [True, 0., 0.]),
    ("mean_enu_mps", [float("nan"), 0., 0.]), ("mean_enu_mps", [101., 0., 0.]),
    ("mean_enu_mps", [80., 80., 0.]), ("gust_amplitude_enu_mps", [0., 0.]),
    ("gust_period_s", True), ("gust_period_s", 0.), ("gust_period_s", 3.99),
    ("gust_period_s", 3600.01), ("gust_phase_rad", float("inf")), ("gust_phase_rad", 2*math.pi+.01),
])
def test_both_profile_validators_reject_invalid_closed_wind(field, value):
    p = profile(WindField())
    p["environment"]["wind"][field] = value
    with pytest.raises(ValueError):
        wind_from_profile(p)
    with pytest.raises(ValueError):
        validate_wind_profile(p)


@pytest.mark.parametrize("value", [[], True, {"wind": {}, "callback": "anything"}, {"wind": {}}])
def test_unknown_environment_and_incomplete_wind_are_rejected(value):
    p = {"environment": value}
    with pytest.raises(ValueError):
        wind_from_profile(p)
    with pytest.raises(ValueError):
        validate_wind_profile(p)


def test_callbacks_and_unknown_wind_keys_are_not_invoked():
    def forbidden(*_):
        pytest.fail("A declared wind must not invoke a callback")
    value = asdict(WindField())
    value["callback"] = forbidden
    with pytest.raises(ValueError):
        wind_from_dict(value)
    value = asdict(WindField())
    value["mean_enu_mps"] = forbidden
    with pytest.raises(ValueError):
        wind_from_dict(value)
    with pytest.raises(ValueError):
        replace(plate_vehicle(), wind=forbidden)


@pytest.mark.parametrize("mutation", [
    lambda s, p: s.update(wind_eci_mps=[0., 0., 0.]),
    lambda s, p: s.update(air_speed_mps=s["air_speed_mps"]+1.),
    lambda s, p: s.update(ground_speed_mps=s["air_speed_mps"]),
    lambda s, p: s.update(dynamic_pressure_pa=s["dynamic_pressure_pa"]+1.),
    lambda s, p: s.update(wind_applied=False),
    lambda s, p: p["environment"]["wind"].update(gust_phase_rad=1.),
    lambda s, p: p["environment"]["wind"].update(mean_enu_mps=[3., 0., 0.]),
])
def test_independent_checker_rejects_modified_field_and_observations(mutation):
    w = WindField((2., 0., 0.), (1., 0., 0.), 20., .1)
    p, s = profile(w), state(time=2.)
    sample = _sample(s, plate_vehicle(w), "wind_test")
    assert verify_wind_observation(sample, p)["passed"]
    mutation(sample, p)
    assert not verify_wind_observation(sample, p)["passed"]


def test_absent_wind_remains_optional_and_undeclared_wind_is_refused():
    assert wind_from_profile({}) is None
    assert wind_from_profile({"environment": {"wind": None}}) is None
    assert validate_wind_profile({}) is None
    assert verify_wind_observation({}, {})["passed"]
    assert not verify_wind_observation({"wind_enu_mps": [1., 0., 0.]}, {})["passed"]

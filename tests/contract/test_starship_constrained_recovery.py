"""Development guidance still executes finite inherited state and fault limits."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import vehicle, _attitude
from src.runtime.starship_constrained_recovery import simulate_constrained_recovery, balanced_main_pair, nonvertical_corridor_ready, landing_actuation_demand
from src.runtime.starship_booster_recovery_verifier import verify_recovery

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def inputs():
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())
    booster = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 80000., time_s=100.)
    up, east, _ = env.local_frame(point)
    state = dyn.State6DOF(100., point.r, env.add(point.v, env.add(env.scale(up, 600.), env.scale(east, 500.))),
        _attitude(up, east), (.001, .002, .003), 260000.,
        tuple(dyn.EngineState(available=i != 5, throttle=.8 if i < 3 else 0.) for i in range(len(booster.engines))),
        tuple(0. for _ in booster.aero_panels))
    return profile, catch, json.loads(json.dumps(asdict(state)))


def test_force_request_moves_real_state_without_replacing_initial_or_failed_engine(inputs):
    profile, catch, initial = inputs
    before = deepcopy(inputs)
    run = json.loads(json.dumps(simulate_constrained_recovery(profile, initial, catch, duration_s=.4)))
    assert inputs == before
    points = run["recovery_record"]["checkpoints"]
    assert points[0]["state"] == initial
    assert run["initial_state"] == initial
    assert run["final_state"] != initial
    assert run["final_state"]["propellant_kg"] < initial["propellant_kg"]
    assert all(not p["state"]["engine_states"][5]["available"] for p in points)
    assert run["outcome"]["integration_steps"] == len(points)-1
    assert run["events"][0]["event"] == "constrained_return_start"
    assert run["events"][-1]["event"] == "time_limit"
    assert points[-1]["command"] is None
    assert not run["outcome"]["handoff_reached"]
    assert not verify_recovery(run, initial, profile, catch)["passed"]


@pytest.mark.parametrize("duration", [True, 0, -1, float("nan"), float("inf"), 1200.1])
def test_invalid_duration_cannot_run(inputs, duration):
    profile, catch, initial = inputs
    with pytest.raises(ValueError, match="invalid_constrained_recovery_duration"):
        simulate_constrained_recovery(profile, initial, catch, duration_s=duration)


def test_unrepresentable_clock_cannot_return_empty_success(inputs):
    profile, catch, initial = inputs
    state = replace(dyn.state_from_dict(initial), time_s=1e20)
    with pytest.raises(ValueError, match="constrained_recovery_clock_must_advance"):
        simulate_constrained_recovery(profile, asdict(state), catch, duration_s=1.)


def test_opposing_pair_cancels_geometry_moment_without_changing_minimum_throttle(inputs):
    profile, _, initial = inputs
    body, state = vehicle(profile, "booster"), dyn.state_from_dict(initial)
    indices, throttle = balanced_main_pair(state, body, 270000.*9.81)
    assert indices == (3, 8)
    assert .4 <= throttle <= 1.
    force = [0., 0., 0.]
    moment = [0., 0., 0.]
    for i in indices:
        f = env.scale(body.engines[i].direction_body, body.engines[i].max_thrust_n*throttle)
        force = env.add(force, f)
        moment = env.add(moment, env.cross(body.engines[i].position_body_m, f))
    assert force[2] == pytest.approx(270000.*9.81)
    assert env.norm(moment) < 1e-8
    assert body.engines[0].min_throttle == .4
    assert balanced_main_pair(state, body, 7e6) is None
    assert balanced_main_pair(state, body, 1e6) is None


def test_opposing_pair_never_selects_a_failed_engine(inputs):
    profile, _, initial = inputs
    state = dyn.state_from_dict(initial)
    engines = list(state.engine_states)
    engines[3] = replace(engines[3], available=False)
    state = replace(state, engine_states=tuple(engines))
    selected, _ = balanced_main_pair(state, vehicle(profile, "booster"), 270000.*9.81)
    assert selected == (4, 9)
    assert all(state.engine_states[i].available for i in selected)


@pytest.mark.parametrize("constraint", ["position", "velocity", "tilt", "roll", "rate", "fuel"])
def test_descent_corridor_uses_actual_simultaneous_nonvertical_gates(inputs, constraint):
    from src.runtime.starship_booster_catch import _initialized
    from src.runtime.starship_booster_recovery import _tower_observation
    profile, catch, _ = inputs
    body, state = _initialized(profile, catch, "booster_catch")
    observed = _tower_observation(state, body, profile, catch)
    assert nonvertical_corridor_ready(observed)
    if constraint == "position":
        observed["position_error_enu_m"][0] = observed["limits"]["horizontal_position_m"]+.01
    elif constraint == "velocity":
        observed["pins"][0]["relative_velocity_enu_mps"][0] = observed["limits"]["pin_horizontal_speed_mps"]+.01
    elif constraint == "tilt":
        observed["tilt_deg"] = observed["limits"]["attitude_angle_deg"]+.01
    elif constraint == "roll":
        observed["body_x_east_angle_deg"] = observed["limits"]["attitude_angle_deg"]+.01
    elif constraint == "rate":
        observed["body_rate_rad_s"] = observed["limits"]["body_rate_rad_s"]+.001
    else:
        observed["propellant_kg"] = observed["limits"]["propellant_reserve_kg"]-1.
    assert not nonvertical_corridor_ready(observed)


def test_opposite_thrust_direction_is_not_credited_as_positive_braking(inputs):
    profile, _, initial = inputs
    state, body = dyn.state_from_dict(initial), vehicle(profile, "booster")
    axis = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
    count, throttle, pair, detail = landing_actuation_demand(state, body, env.scale(axis, -1.), 15e6)
    assert count == 3 and throttle == .4 and pair is None
    assert detail["requested_axis_thrust_alignment"] == pytest.approx(-1.)
    assert detail["landing_alignment_slew"]["translational_thrust_admitted"] is False
    positive = landing_actuation_demand(state, body, axis, 15e6)
    assert positive[0] > 3

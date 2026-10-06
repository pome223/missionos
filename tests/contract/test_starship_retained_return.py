"""Behavioral checks for the bounded terminal-preparation policy."""
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from src.runtime import starship_physics as env
from src.runtime import starship_sixdof as dyn
from src.runtime.starship_retained_return import new_record, terminal_budget
from src.runtime.starship_sixdof_mission import _attitude, _sample, simulate, vehicle


def fixture():
    p = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    v = vehicle(p)
    base = env.surface_state(0, 0, 6000, 100000)
    up, east, north = env.local_frame(base)
    s = dyn.State6DOF(0, base.r, env.add(base.v, env.scale(up, -140)), _attitude(east, north),
                     (0, 0, 0), 100000, tuple(dyn.EngineState() for _ in v.engines),
                     tuple(0. for _ in v.aero_panels))
    return _sample(s, v, "ballistic_return"), p


def test_tilted_body_requires_preparation_beyond_vertical_stopping_distance():
    s, p = fixture()
    budget = terminal_budget(s, p)
    ideal = 140**2/(2*budget["aligned_net_acceleration_mps2"])
    assert budget["required_altitude_m"] > ideal+2000
    assert budget["preparation_time_s"] > 10
    assert budget["available_landing_engines"] == 3
    assert type(budget["trigger"]) is bool
    json.dumps(budget, allow_nan=False)


def test_heavier_slower_to_brake_configuration_gets_more_preparation():
    s, p = fixture()
    heavier = deepcopy(s)
    heavier["mass_kg"] *= 1.2
    heavier["inertia_kg_m2"] = [[x*1.2 for x in row] for row in s["inertia_kg_m2"]]
    assert terminal_budget(heavier, p)["required_altitude_m"] > terminal_budget(s, p)["required_altitude_m"]


def test_body_rate_and_engine_loss_are_not_ignored():
    s, p = fixture()
    rotating = deepcopy(s)
    rotating["omega_body_rad_s"] = [.1, .1, 0]
    degraded = deepcopy(s)
    degraded["engine_states"][0]["available"] = False
    assert terminal_budget(rotating, p)["preparation_time_s"] > terminal_budget(s, p)["preparation_time_s"]
    assert terminal_budget(degraded, p)["required_altitude_m"] > terminal_budget(s, p)["required_altitude_m"]


@pytest.mark.parametrize("condition", ["ascending", "supersonic", "no_thrust", "empty_tank"])
def test_outside_policy_applicability_never_claims_trigger(condition):
    s, p = fixture()
    if condition == "ascending":
        s["v_eci_mps"][0] = 140
    elif condition == "supersonic":
        s["v_eci_mps"][0] = -1000
    elif condition == "no_thrust":
        for engine in s["engine_states"]:
            engine["available"] = False
    else:
        s["propellant_kg"] = 0
    result = terminal_budget(s, p)
    assert not result["trigger"] and not result["applicable"]
    json.dumps(result, allow_nan=False)


def test_roll_rotation_is_not_mistaken_for_thrust_axis_slew():
    s, p = fixture()
    # Rotate 180 degrees about body Z. Thrust axis and required turn unchanged.
    roll = deepcopy(s)
    roll["q_body_to_eci"] = dyn.quaternion_multiply(s["q_body_to_eci"], (0, 0, 0, 1))
    assert terminal_budget(roll, p)["thrust_axis_angle_rad"] == pytest.approx(math.pi/2)
    assert terminal_budget(roll, p)["required_altitude_m"] == pytest.approx(terminal_budget(s, p)["required_altitude_m"])


def test_policy_must_be_explicit_and_scoped_to_retained_fault():
    _, p = fixture()
    assert new_record("fixed_v1")["status"] == "fixed"
    with pytest.raises(ValueError, match="unknown return policy"):
        new_record("LLM says safe")
    with pytest.raises(ValueError, match="requires deployment_no_effect"):
        simulate(p, scenario="launch", duration_s=.1, return_policy="mass_state_terminal_v1")


@pytest.mark.parametrize("policy", ["mass_state_terminal_v1", "mass_state_terminal_v2", "mass_state_terminal_v3"])
def test_approved_policy_does_not_activate_before_retained_return_observation(policy):
    _, p = fixture()
    run = simulate(p, scenario="deployment_no_effect", duration_s=.2, return_policy=policy)
    assert run["retained_return"]["status"] == "not_activated"
    assert run["retained_return"]["activation"] is None
    assert run["retained_return"]["trigger"] is None
    assert run["outcome"]["termination"] == "time_limit"

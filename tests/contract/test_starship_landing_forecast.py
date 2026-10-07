"""Short six-DOF forecasts and explicit context/budget/tamper boundaries."""
from copy import deepcopy
from dataclasses import asdict, replace
import inspect
import json
from pathlib import Path

import pytest

from src.runtime import starship_landing_context as contexts
from src.runtime import starship_landing_forecast_verifier as verifier
from src.runtime.starship_landing_forecast import forecast

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def profile():
    return json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())


@pytest.fixture(scope="module")
def config():
    return json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())


def snapshot(state, profile, config, deadline=100.):
    from src.runtime.starship_attitude_reference import ConditionedGeographicFrame
    ref = ConditionedGeographicFrame(state.q_body_to_eci, state.time_s,
                                    maximum_roll_rate_rad_s=profile["guidance"]["attitude_frequency_rad_s"])
    # Include an acquired bridge, to exercise stateful roll restoration.
    ref.bridging = True
    return contexts.capture(asdict(state), ref, profile, config, deadline_s=state.time_s+deadline,
        previous_axis=(0., 0., 1.), entry_axis=(0., 0., 1.), next_entry_update_s=state.time_s,
        entry_diagnostic={})


@pytest.fixture(scope="module")
def origin(profile, config):
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import _attitude, vehicle
    booster = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 1200., time_s=400.)
    up, east, _ = env.local_frame(point)
    state = dyn.State6DOF(400., point.r, env.add(point.v, env.scale(up, -50.)), _attitude(up, east),
        (.001, -.002, .003), 60000., tuple(dyn.EngineState(available=i != 5) for i in range(len(booster.engines))),
        tuple(0. for _ in booster.aero_panels))
    return snapshot(state, profile, config)


@pytest.fixture(scope="module")
def delayed(origin, profile, config):
    return json.loads(json.dumps(forecast(origin, profile, config, delay_s=2., duration_s=3.)["run"], allow_nan=False))


def check(run, origin, profile, config, delay=2., duration=3.):
    result = verifier.verify_forecast(run, origin, profile, config, delay_s=delay, duration_s=duration)
    assert not result["prediction_is_execution"] and not result["physical_execution"]
    assert not result["catch_supported"] and not result["mission_completed"]
    return result


def test_wait_advances_real_state_and_preserves_missing_engine(delayed, origin, profile, config):
    result = check(delayed, origin, profile, config)
    assert result["passed"], result
    rows = delayed["recovery_record"]["checkpoints"]
    before = [p for p in rows if p["time_s"] < 402.]
    assert before and all(p["phase"] == "recovery_entry_coast" for p in before)
    assert before[-1]["state"]["r_eci_m"] != before[0]["state"]["r_eci_m"]
    assert all(p["state"]["engine_states"][5]["available"] is False for p in rows)
    assert any(e["event"] == "landing_stage_requested" and e["time_s"] >= 402.-1e-8 for e in delayed["events"])


def test_forecast_never_mutates_caller_or_grants_execution(origin, profile, config):
    before = deepcopy(origin)
    value = forecast(origin, profile, config, delay_s=0., duration_s=.6)
    assert origin == before and value["prediction_is_execution"] is False
    saved = json.loads(json.dumps(value["run"], allow_nan=False))
    verdict = check(saved, origin, profile, config, delay=0., duration=.6)
    assert verdict["passed"], verdict
    value["run"]["booster_separation_state"]["engine_states"][0]["available"] = False
    value["run"]["recovery_record"]["input_separation_state"]["propellant_kg"] = 0.
    assert origin == before  # Returned dictionaries must not alias the origin.


@pytest.mark.parametrize("delay,duration", [(True, 3.), (1., 3.), (-2., 3.), (4., 3.),
    (None, True), (None, 0.), (None, 90.1), (None, float("nan"))])
def test_invalid_budget_refused_before_simulation(origin, profile, config, delay, duration):
    with pytest.raises(ValueError, match="invalid_landing_forecast_budget"):
        forecast(origin, profile, config, delay_s=delay, duration_s=duration)


def test_inherited_deadline_cannot_be_extended(origin, profile, config):
    value = deepcopy(origin)
    value["deadline_s"] = value["state"]["time_s"]+.5
    with pytest.raises(ValueError, match="invalid_landing_forecast_budget"):
        forecast(value, profile, config, delay_s=0., duration_s=.6)


@pytest.mark.parametrize("field", ["bridging", "quaternion", "time_s", "maximum_roll_rate_rad_s"])
def test_invalid_controller_memory_refused(origin, profile, config, field):
    value = deepcopy(origin)
    ref = value["context"]["reference"]
    ref[field] = {"bridging": 1, "quaternion": [1., 1., 0., 0.], "time_s": 401.,
                  "maximum_roll_rate_rad_s": 99.}[field]
    with pytest.raises(ValueError, match="invalid_landing_context"):
        forecast(value, profile, config, delay_s=0., duration_s=.6)


def test_context_cannot_override_production_run(origin, profile, config):
    from src.runtime.starship_booster_recovery import simulate_recovery
    with pytest.raises(ValueError, match="landing_context_requires_forecast"):
        simulate_recovery(profile, origin["state"], config, _landing_context=origin, duration_s=.6)


def test_ordinary_checker_rejects_forecast_even_with_flag_removed(delayed, origin, profile, config):
    from src.runtime.starship_booster_recovery_verifier import verify_recovery
    for flag in (True, False):
        run = deepcopy(delayed)
        run["recovery_record"]["forecast_only"] = flag
        assert not verify_recovery(run, origin["state"], profile, config)["passed"]


@pytest.mark.parametrize("field", ["fuel", "actuator", "deadline", "delay", "reserve", "success"])
def test_independent_forecast_checker_rejects_modified_records(delayed, origin, profile, config, field):
    run = deepcopy(delayed)
    record = run["recovery_record"]
    if field == "fuel":
        record["checkpoints"][1]["state"]["propellant_kg"] += 1.
    elif field == "actuator":
        record["checkpoints"][1]["state"]["engine_states"][5]["available"] = True
    elif field == "deadline":
        record["resolved_duration_s"] = 1200.
    elif field == "delay":
        record["landing_start_forecast"]["delay_s"] = 0.
    elif field == "reserve":
        record["handoff"]["observation"]["limits"]["propellant_reserve_kg"] = 0.
    else:
        record["mission_completed"] = True
    assert not check(run, origin, profile, config)["passed"]


def test_near_terminal_fixture_can_predict_arrival_without_claiming_support(profile, config):
    from src.runtime.starship_booster_catch import _initialized
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    _, state = _initialized(profile, config, "booster_catch")
    up = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
    state = replace(state, r_eci_m=env.add(state.r_eci_m, up))
    value = snapshot(state, profile, config)
    run = json.loads(json.dumps(forecast(value, profile, config, delay_s=0., duration_s=2.)["run"], allow_nan=False))
    verdict = check(run, value, profile, config, delay=0., duration=2.)
    assert verdict["passed"] and verdict["predicted_handoff_eligible"], verdict
    assert verdict["catch_supported"] is False


def test_checker_does_not_import_integrator_or_forecast_producer():
    source = inspect.getsource(verifier)
    assert "starship_sixdof import" not in source and "starship_landing_forecast import" not in source


@pytest.mark.parametrize("field", ["physical_execution_invoked", "launch_connected_catch_supported", "missionos_dispatch",
    "dispatch_authority_created", "execution_authorized", "prediction_is_execution", "production_policy_admitted", "model_value_demonstrated"])
def test_forecasts_cannot_claim_missionos_dispatch_or_adoption(delayed, origin, profile, config, field):
    run = deepcopy(delayed)
    run["outcome"][field] = True
    assert not check(run, origin, profile, config)["passed"]


@pytest.mark.parametrize("field,value", [("orbit_gate_reached", True), ("payload_released_count", 26), ("final_ground_speed_mps", 0.)])
def test_booster_prediction_cannot_claim_payload_or_false_speed(delayed, origin, profile, config, field, value):
    run = deepcopy(delayed)
    run["outcome"][field] = value
    assert not check(run, origin, profile, config)["passed"]


@pytest.mark.parametrize("fault", ["late", "missing"])
def test_scheduled_request_cannot_be_late_or_missing(delayed, origin, profile, config, fault):
    run = deepcopy(delayed)
    if fault == "late":
        for event in run["events"]:
            if event["event"] == "landing_stage_requested":
                event["time_s"] += .3
    else:
        run["events"] = [e for e in run["events"] if e["event"] != "landing_stage_requested"]
    verdict = check(run, origin, profile, config)
    assert not verdict["passed"] and verdict["issues"][0]["code"] == "scheduled_burn", verdict


def test_real_contact_before_scheduled_burn_is_an_honest_negative(profile, config):
    from src.runtime.starship_booster_catch import _initialized
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    _, state = _initialized(profile, config, "booster_catch")
    up = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
    state = replace(state, r_eci_m=env.add(state.r_eci_m, env.scale(up,-4.)),
                    v_eci_mps=env.add(state.v_eci_mps,env.scale(up,-100.)))
    value = snapshot(state, profile, config)
    run = json.loads(json.dumps(forecast(value, profile, config, delay_s=4., duration_s=5.)["run"],allow_nan=False))
    verdict = check(run,value,profile,config,delay=4.,duration=5.)
    assert verdict["passed"] and not verdict["predicted_handoff_eligible"], verdict
    assert run["final_state"]["time_s"] < state.time_s+4


def test_coast_trim_claims_are_checked(delayed, origin, profile, config):
    run = deepcopy(delayed)
    point = run["recovery_record"]["checkpoints"][0]
    point["navigation"]["predicted_trim_flap_angles_rad"] = [99.]*6
    point["navigation"]["steady_trim_feasible"] = True
    assert not check(run, origin, profile, config)["passed"]

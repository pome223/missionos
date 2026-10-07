"""Short actual return/terminal continuations and independent tamper tests.

These initialized fixtures do not establish a launch-to-catch mission.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import inspect
import json
from pathlib import Path

import pytest

from src.runtime import starship_booster_recovery_verifier as verifier

ROOT = Path(__file__).resolve().parents[2]


def saved(value):
    return json.loads(json.dumps(value, allow_nan=False))


@pytest.fixture(scope="module")
def profile():
    return json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())


@pytest.fixture(scope="module")
def configuration():
    return json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())


@pytest.fixture(scope="module")
def negative(profile, configuration):
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import _attitude, vehicle
    from src.runtime.starship_booster_recovery import simulate_recovery
    booster = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                              50000., 200000., time_s=180.)
    up, east, north = env.local_frame(point)
    state = dyn.State6DOF(180., point.r, env.add(point.v, env.add(env.scale(up, 600.), env.scale(east, 1000.))),
                          _attitude(up, north), (.001, -.002, .003), 200000.,
                          tuple(dyn.EngineState(throttle=.4 if i < 3 else 0., available=i != 5) for i in range(len(booster.engines))),
                          tuple(0. for _ in booster.aero_panels))
    initial = saved(asdict(state))
    return initial, saved(simulate_recovery(profile, initial, configuration, duration_s=.6))


@pytest.fixture(scope="module")
def terminal(profile, configuration):
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_booster_catch import _initialized, simulate_catch
    from src.runtime.starship_booster_recovery import simulate_recovery
    _, state = _initialized(profile, configuration, "booster_catch")
    # One metre above the separate terminal fixture: recovery has to move into
    # its measured gate before the exact state can continue through contact.
    up = dyn.rotate(state.q_body_to_eci, (0., 0., 1.))
    state = replace(state, r_eci_m=env.add(state.r_eci_m, up))
    initial = saved(asdict(state))
    run = saved(simulate_recovery(profile, initial, configuration, duration_s=2.))
    assert run["outcome"]["termination"] == "catch_handoff", run["outcome"]
    catch = saved(simulate_catch(profile, configuration, initial_state=run["final_state"]))
    return initial, run, catch


@pytest.fixture(scope="module")
def trimmed_terminal(terminal, profile, configuration):
    from src.runtime.starship_booster_catch import simulate_catch
    initial, run, _ = terminal
    catch = saved(simulate_catch(profile, configuration, initial_state=run["final_state"],
                                duration_s=30., control_policy="net_thrust_trim_v1"))
    return initial, run, catch


def check(run, initial, profile, configuration, catch=None):
    result = verifier.verify_recovery(run, initial, profile, configuration, catch_run=catch)
    assert not any(result[k] for k in ("catch_verified", "physical_execution", "physical_execution_invoked", "mission_completed", "launch_connected"))
    json.dumps(result, allow_nan=False)
    return result


def reject(run, initial, profile, configuration, catch=None, code=None):
    result = check(run, initial, profile, configuration, catch)
    assert not result["passed"], result
    assert not result["handoff_reached"] and not result["catch_supported_after_handoff"]
    if code:
        assert result["issues"][0]["code"] == code, result


def test_negative_return_preserves_every_separation_field(negative, profile, configuration):
    initial, run = negative
    result = check(run, initial, profile, configuration)
    assert result["passed"] and not result["handoff_reached"], result
    assert run["final_state"]["engine_states"][5]["available"] is False


def test_initialized_arrival_is_not_awarded_as_catch_support(terminal, profile, configuration):
    initial, run, catch = terminal
    result = check(run, initial, profile, configuration, catch)
    assert result["passed"] and result["handoff_reached"] and not result["catch_supported_after_handoff"], result
    assert result["catch_verification"]["passed"]
    assert catch["outcome"]["termination"] == "surface_contact"
    assert run["final_state"] == run["recovery_record"]["handoff"]["state"]


def test_exact_arrived_state_can_be_supported_by_finite_trim_controller(trimmed_terminal, profile, configuration):
    initial, run, catch = trimmed_terminal
    result = check(run, initial, profile, configuration, catch)
    assert result["passed"] and result["handoff_reached"] and result["catch_supported_after_handoff"], result
    assert catch["catch_record"]["control_policy"] == "net_thrust_trim_v1"
    assert catch["catch_record"]["configuration"] == configuration
    assert catch["outcome"]["termination"] == "supported_settled"
    assert result["catch_verification"]["observed_settle_s"] >= configuration["settle_time_s"]-1e-8


def test_verifier_does_not_import_producer_or_integrator():
    source = inspect.getsource(verifier)
    assert "from .starship_booster_catch_verifier import verify_catch" in source
    for name in ("starship_booster_recovery import", "starship_booster_catch import", "starship_sixdof import", "import numpy"):
        assert name not in source


def test_material_point_velocity_has_analytic_moving_centroid_term(profile, configuration):
    p = deepcopy(profile)
    p["launch"].update(latitude_deg=0., longitude_deg=0.)
    fuel, dry, throttle = 30000., p["booster"]["dry_mass_kg"], .55
    centroid = (dry*p["booster"]["dry_com_z_m"]+fuel*p["booster"]["tank_center_z_m"])/(dry+fuel)
    flow = 2*throttle*p["booster"]["engine_thrust_n"]/(p["booster"]["engine_isp_s"]*9.80665)
    rate = -flow*dry*(p["booster"]["tank_center_z_m"]-p["booster"]["dry_com_z_m"])/(dry+fuel)**2
    radius = 6378137.+configuration["support_height_m"]+3.4-(60.-centroid)
    state = {"time_s": 0., "r_eci_m": [radius, 0., 0.], "v_eci_mps": [-1., 7.292115e-5*radius, 0.],
             "q_body_to_eci": [.5, .5, .5, .5], "omega_body_rad_s": [0., 7.292115e-5, 0.],
             "propellant_kg": fuel, "flap_angles_rad": [0.]*6,
             "engine_states": [{"available": True, "throttle": throttle if i < 2 else 0., "gimbal_x_rad": 0., "gimbal_y_rad": 0.}
                               for i in range(45)]}
    arrival = verifier._arrival(state, p, configuration)
    assert arrival["eligible"] is True
    assert arrival["com_rate_body_mps"] == pytest.approx([0., 0., rate])
    for index, pin in enumerate(arrival["pins"]):
        assert pin["position_enu_m"] == pytest.approx([-6. if index == 0 else 6., 0., 103.4])
        assert pin["velocity_enu_mps"] == pytest.approx([0., 0., -1.-rate], abs=1e-9)
    # Earth-relative body x roll matters as well as being upright: reverse it
    # while preserving the upright body axis and the geometry no longer fits.
    state["q_body_to_eci"] = [-.5, .5, -.5, .5]
    assert verifier._arrival(state, p, configuration)["eligible"] is False


@pytest.mark.parametrize("field", verifier._STATE)
def test_separation_mutations_rejected(negative, profile, configuration, field):
    initial, original = negative
    run = deepcopy(original)
    changed = run["recovery_record"]["input_separation_state"]
    if field == "engine_states":
        changed[field][5]["available"] = True
    elif field == "flap_angles_rad":
        changed[field][-1] += .01
    elif isinstance(changed[field], list):
        changed[field][0] += .01
    else:
        changed[field] += .01
    reject(run, initial, profile, configuration, code="separation_binding")


def test_boolean_zero_is_not_an_inherited_numeric_actuator(negative, profile, configuration):
    initial, original = negative
    run = deepcopy(original)
    run["recovery_record"]["input_separation_state"]["engine_states"][4]["throttle"] = False
    reject(run, initial, profile, configuration, code="separation_binding")


@pytest.mark.parametrize("field", verifier._STATE)
def test_handoff_cannot_reset_any_physical_state(terminal, profile, configuration, field):
    initial, run, original = terminal
    catch = deepcopy(original)
    changed = catch["initial_state"]
    if field == "engine_states":
        changed[field][0]["throttle"] += .001
    elif field == "flap_angles_rad":
        changed[field][-1] += .001
    elif isinstance(changed[field], list):
        changed[field][0] += .001
    else:
        changed[field] += .001
    reject(run, initial, profile, configuration, catch, "catch_binding")


@pytest.mark.parametrize("field", ["physical_execution", "catch_verified", "launch_connected", "state_reset", "mission_completed"])
def test_unsupported_claims_fail_closed(negative, profile, configuration, field):
    initial, original = negative
    run = deepcopy(original)
    run["outcome"][field] = True
    reject(run, initial, profile, configuration, code="claim_boundary")


@pytest.mark.parametrize("forecast_flag", [True, None, "missing"])
def test_counterfactual_record_cannot_replace_executed_return(negative, profile, configuration, forecast_flag):
    initial, original = negative
    run = deepcopy(original)
    if forecast_flag == "missing":
        run["recovery_record"].pop("forecast_only")
    else:
        run["recovery_record"]["forecast_only"] = forecast_flag
    reject(run, initial, profile, configuration, code="claim_boundary")


@pytest.mark.parametrize("field", ["position_error_enu_m", "pin_height_above_support_m", "com_rate_body_mps"])
def test_arrival_is_reconstructed_not_read_from_metrics(terminal, profile, configuration, field):
    initial, original, catch = terminal
    run = deepcopy(original)
    run["recovery_record"]["handoff"]["observation"][field][0] += .1
    reject(run, initial, profile, configuration, catch, "handoff_observation")


def test_material_point_velocity_cannot_omit_centroid_motion(terminal, profile, configuration):
    initial, original, catch = terminal
    run = deepcopy(original)
    run["recovery_record"]["handoff"]["observation"]["pins"][0]["relative_velocity_enu_mps"][2] += .01
    reject(run, initial, profile, configuration, catch, "handoff_observation")


def test_gate_cannot_be_widened_to_accept_an_outside_state(terminal, profile, configuration):
    initial, original, catch = terminal
    run = deepcopy(original)
    handoff = run["recovery_record"]["handoff"]
    handoff["observation"]["limits"]["pin_horizontal_speed_mps"] *= 10
    handoff["limits"] = deepcopy(handoff["observation"]["limits"])
    reject(run, initial, profile, configuration, catch, "handoff_limits")


def test_arrival_requires_actual_continuation(terminal, profile, configuration):
    initial, run, _ = terminal
    reject(run, initial, profile, configuration, code="catch_binding")


def test_initialized_catch_cannot_substitute_for_arrived_state(terminal, profile, configuration):
    initial, run, original = terminal
    catch = deepcopy(original)
    catch["catch_record"]["initialization"]["kind"] = "terminal_initialized"
    reject(run, initial, profile, configuration, catch, "catch_binding")


@pytest.mark.parametrize("policy", [None, "unknown", "weld_pose", True])
def test_unknown_terminal_control_policy_rejected(terminal, profile, configuration, policy):
    initial, run, original = terminal
    catch = deepcopy(original)
    catch["catch_record"]["control_policy"] = policy
    reject(run, initial, profile, configuration, catch, "catch_binding")


def test_negative_cannot_invoke_catch(negative, terminal, profile, configuration):
    initial, run = negative
    reject(run, initial, profile, configuration, terminal[2], "catch_binding")


def test_corrupted_contact_evidence_rejected_after_exact_handoff(terminal, profile, configuration):
    initial, run, original = terminal
    catch = deepcopy(original)
    catch["catch_record"]["frames"][-1]["pins"][0]["normal_force_n"] += 1.
    reject(run, initial, profile, configuration, catch, "catch_verification")


@pytest.mark.parametrize("mutation", ["teleport", "attitude", "refuel", "availability", "gimbal", "fin", "clock"])
def test_saved_state_discontinuities_rejected(negative, profile, configuration, mutation):
    initial, original = negative
    run = deepcopy(original)
    cp, sample = run["recovery_record"]["checkpoints"][1], run["samples"][1]
    state = cp["state"]
    if mutation == "teleport":
        state["r_eci_m"][0] += 1000.
    elif mutation == "attitude":
        state["q_body_to_eci"] = [0., 1., 0., 0.]
    elif mutation == "refuel":
        state["propellant_kg"] += 10000.
    elif mutation == "availability":
        state["engine_states"][5]["available"] = True
    elif mutation == "gimbal":
        state["engine_states"][0]["gimbal_x_rad"] = -.13
    elif mutation == "fin":
        state["flap_angles_rad"][-1] = .4
    else:
        state["time_s"] = initial["time_s"]
        cp["time_s"] = state["time_s"]
    sample.update(deepcopy(state))
    # Keep redundant mass/CG fields consistent so those cannot be the only
    # reason that an impossible refueling transition is rejected.
    mass, centroid, rate, _ = verifier._flow_and_centroid(state, profile)
    sample.update(mass_kg=mass, com_z_m=centroid[2], com_rate_body_mps=rate)
    cp["com_rate_body_mps"] = rate
    reject(run, initial, profile, configuration)


def test_prediction_never_substitutes_for_executed_truth(negative, profile, configuration):
    initial, original = negative
    run = deepcopy(original)
    run["recovery_record"]["boostback_plan"]["prediction_is_execution"] = True
    reject(run, initial, profile, configuration, code="prediction_binding")


def test_forecast_budget_cannot_be_exceeded(negative, profile, configuration):
    initial, original = negative
    run = deepcopy(original)
    run["recovery_record"]["full_coast_prediction_count"] = 25
    reject(run, initial, profile, configuration, code="prediction_binding")


def test_forecast_count_requires_separate_receipts(negative, profile, configuration):
    initial, original = negative
    run = deepcopy(original)
    run["recovery_record"]["full_coast_prediction_count"] = 1
    reject(run, initial, profile, configuration, code="prediction_binding")


def test_navigation_cannot_promote_a_forecast_to_execution(negative, profile, configuration):
    initial, original = negative
    run = deepcopy(original)
    run["recovery_record"]["checkpoints"][0]["navigation"]["cutoff_prediction"] = {"prediction_is_execution": True}
    reject(run, initial, profile, configuration, code="prediction_binding")


def test_forecast_cannot_refresh_the_remaining_time_budget(negative, profile, configuration):
    initial, original = negative
    run = deepcopy(original)
    record = run["recovery_record"]
    record["full_coast_prediction_count"] = 1
    record["cutoff_predictions"] = [{"time_s": initial["time_s"], "prediction": {
        "prediction_is_execution": False, "model": "same_finite_6dof_policy_continuation",
        "termination": "time_limit", "position_enu_m": [0., 0., 0.], "velocity_enu_mps": [0., 0., 0.],
        "terminal_propellant_kg": 0., "elapsed_s": .6, "remaining_duration_s": 1200.}}]
    reject(run, initial, profile, configuration, code="prediction_binding")


@pytest.mark.parametrize("mutation", ["angle", "deflection", "moment", "capacity", "imposed", "infeasible",
                                     "future_pressure", "current_pressure", "future_scaling", "future_capacity",
                                     "missing_future_pressure", "missing_future_moment"])
def test_trim_prediction_does_not_expand_finite_actuator_authority(profile, mutation):
    diagnostic = {"candidate_count": 15, "entry_angle_deg": 45., "steady_trim_feasible": True,
                  "trim_is_executed_deflection": False, "predicted_trim_flap_angles_rad": [0.]*6,
                  "predicted_residual_moment_body_nm": [10., 20., 30.], "available_residual_jet_torque_nm": 80000.,
                  "predicted_panel_force_magnitudes_n": [100.]*6, "prediction_dynamic_pressure_pa": 100.,
                  "trim_validation_dynamic_pressure_pa": 35000.,
                  "trim_validation_residual_moment_body_nm": [3500., 7000., 10500.],
                  "predicted_force_enu_n": [0., 0., 100.], "trim_rejected_candidate_count": 10}
    verifier._trim_diagnostics(diagnostic, profile)
    if mutation == "angle":
        diagnostic["entry_angle_deg"] = 60.01
    elif mutation == "deflection":
        diagnostic["predicted_trim_flap_angles_rad"][-1] = .5
    elif mutation == "moment":
        diagnostic["predicted_residual_moment_body_nm"][0] = 80001.
    elif mutation == "capacity":
        diagnostic["available_residual_jet_torque_nm"] = 80001.
    elif mutation == "imposed":
        diagnostic["trim_is_executed_deflection"] = True
    elif mutation == "infeasible":
        diagnostic["steady_trim_feasible"] = False
    elif mutation == "future_pressure":
        diagnostic["trim_validation_dynamic_pressure_pa"] = 100.
        diagnostic["trim_validation_residual_moment_body_nm"] = [10., 20., 30.]
    elif mutation == "current_pressure":
        diagnostic["prediction_dynamic_pressure_pa"] = 40000.
    elif mutation == "future_scaling":
        diagnostic["trim_validation_residual_moment_body_nm"][0] = 3501.
    elif mutation == "future_capacity":
        diagnostic["trim_validation_residual_moment_body_nm"][0] = 80001.
        diagnostic["predicted_residual_moment_body_nm"][0] = 80001./350.
    elif mutation == "missing_future_pressure":
        del diagnostic["trim_validation_dynamic_pressure_pa"]
    else:
        del diagnostic["trim_validation_residual_moment_body_nm"]
    with pytest.raises(verifier._Invalid):
        verifier._trim_diagnostics(diagnostic, profile)


def test_rate_settle_timeout_cannot_be_asserted_without_elapsed_time(negative, profile, configuration):
    initial, original = negative
    run = deepcopy(original)
    run["outcome"]["termination"] = "rate_settle_failed"
    run["events"][-1]["event"] = "rate_settle_failed"
    reject(run, initial, profile, configuration, code="outcome")


@pytest.mark.parametrize("bad", [None, {}, {"body_id": "booster", "bad": float("nan")}, {"body_id": "booster", "bad": (1, 2)}])
def test_malformed_json_is_a_failed_receipt(bad, negative, profile, configuration):
    reject(bad, negative[0], profile, configuration)


def test_cyclic_input_is_bounded(negative, profile, configuration):
    run = {"body_id": "booster"}
    run["loop"] = run
    reject(run, negative[0], profile, configuration, code="input_limit")

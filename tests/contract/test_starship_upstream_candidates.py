"""Finite upstream requests, inherited faults and explicit experiment scope."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from src.runtime.starship_booster_recovery import simulate_recovery
from src.runtime.starship_booster_recovery_verifier import verify_recovery, _capture_planning, _Invalid
from scripts.study_starship_upstream_candidates import needs_contact_continuation, same_physical_replay, main, load_inputs

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def inputs():
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_sixdof_mission import _attitude, vehicle
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    config = json.loads((ROOT/"examples/spaceflight/starship-catch-profile.json").read_text())
    body = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"], 80000., time_s=100.)
    up, east, _ = env.local_frame(point)
    state = dyn.State6DOF(100., point.r, env.add(point.v, env.add(env.scale(up, 600.), env.scale(east, 500.))),
        _attitude(up, east), (.001, .002, .003), 260000.,
        tuple(dyn.EngineState(available=i != 5) for i in range(len(body.engines))), tuple(0. for _ in body.aero_panels))
    return profile, config, json.loads(json.dumps(asdict(state)))


def run(inputs, index, **kwargs):
    profile, config, initial = inputs
    return json.loads(json.dumps(simulate_recovery(deepcopy(profile), deepcopy(initial), deepcopy(config),
        duration_s=.6, _development_cutoff_time_s=100.3, _development_boostback_candidate_index=index, **kwargs), allow_nan=False))


@pytest.fixture(scope="module")
def candidate(inputs):
    return run(inputs, 0)


def check(value, inputs, index=0):
    p, c, initial = inputs
    return verify_recovery(value, initial, p, c, development_cutoff_time_s=100.3,
                           development_boostback_candidate_index=index)


def test_finite_command_alternatives_change_physical_trajectory(inputs, candidate):
    result = check(candidate, inputs)
    assert result["passed"], result
    other = run(inputs, 4)
    assert check(other, inputs, 4)["passed"]
    assert candidate["final_state"] != other["final_state"]
    for value in (candidate, other):
        assert value["recovery_record"]["input_separation_state"] == inputs[2]
        assert all(not point["state"]["engine_states"][5]["available"] for point in value["recovery_record"]["checkpoints"])


@pytest.mark.parametrize("index", [True, -1, 5, 1.0, "1"])
def test_invalid_indices_refused(inputs, index):
    with pytest.raises(ValueError, match="invalid_development_boostback_candidate"):
        run(inputs, index)


def test_production_and_forecast_cannot_take_development_candidate(inputs):
    p, c, initial = inputs
    with pytest.raises(ValueError, match="invalid_development_boostback_candidate"):
        simulate_recovery(p, initial, c, duration_s=.6, _development_boostback_candidate_index=0)
    for options in ({"_forecast": True}, {"_development_fin_allocation": True},
                    {"_development_landing_probe_time_s": 100.4}):
        with pytest.raises(ValueError, match="invalid_development"):
            run(inputs, 0, **options)


def test_marker_not_admitted_by_ordinary_checker(inputs, candidate):
    assert not check(candidate, inputs, None)["passed"]
    stripped = deepcopy(candidate)
    del stripped["recovery_record"]["development_boostback_candidate"]
    assert not check(stripped, inputs, None)["passed"]  # Plan rule still binds the alternative.


@pytest.mark.parametrize("field", ["index", "selection", "adoption", "missionos", "marker"])
def test_tampered_experiment_refused(inputs, candidate, field):
    value = deepcopy(candidate)
    if field == "index":
        value["recovery_record"]["development_boostback_candidate"]["candidate_index"] = 1
    elif field == "selection":
        value["recovery_record"]["boostback_plan"]["selected"] = value["recovery_record"]["boostback_plan"]["candidates"][1]
    elif field == "marker":
        value["recovery_record"]["development_boostback_candidate"]["recording"] = "sparse"
    else:
        value["outcome"]["production_policy_admitted" if field == "adoption" else "missionos_dispatch"] = True
    assert not check(value, inputs)["passed"]


def test_intermediate_arrival_requires_only_specific_contact_dependency():
    value = {"passed": False, "issues": [{"code": "catch_binding", "detail": "Eligible handoff requires the actual terminal continuation"}]}
    assert needs_contact_continuation(value)
    value["issues"].append({"code": "separation_binding"})
    assert not needs_contact_continuation(value)
    assert not needs_contact_continuation({"passed": True, "issues": []})


def test_real_arrival_dependency_then_exact_state_contact(inputs):
    from dataclasses import replace
    from src.runtime import starship_physics as env, starship_sixdof as dyn
    from src.runtime.starship_booster_catch import _initialized, simulate_catch
    p, c, _ = inputs
    _, state = _initialized(p, c, "booster_catch")
    state = replace(state, r_eci_m=env.add(state.r_eci_m, dyn.rotate(state.q_body_to_eci, (0., 0., 1.))))
    initial = json.loads(json.dumps(asdict(state)))
    value = json.loads(json.dumps(simulate_recovery(p, initial, c, duration_s=2.)))
    preliminary = verify_recovery(value, initial, p, c)
    assert needs_contact_continuation(preliminary)
    assert not preliminary["passed"] and not preliminary["catch_supported_after_handoff"]
    catch = json.loads(json.dumps(simulate_catch(p, c, initial_state=deepcopy(value["final_state"]),
                                               duration_s=30., control_policy="net_thrust_trim_v1")))
    assert verify_recovery(value, initial, p, c, catch_run=catch)["passed"]
    catch["initial_state"]["propellant_kg"] += 1.
    assert not verify_recovery(value, initial, p, c, catch_run=catch)["passed"]


def baseline_files(directory, inputs):
    from src.runtime.starship_landing_context import digest
    from hashlib import sha256
    p, c, initial = inputs
    raw = json.dumps({"run": run(inputs, None), "catch_run": None}).encode()
    (directory/"cutoff-minus-00.json").write_bytes(raw)
    (directory/"inputs.json").write_text(json.dumps({"profile": p, "catch_profile": c, "initial_state": initial,
        "reference_cutoff_time_s": 100.3, "reference_study_sha256": "fixture"}))
    comparison = {"schema": "missionos.starship_boostback_comparison.v1", "verification_passed": True,
        "initial_state_sha256": digest(initial), "conditions": [{"cutoff_time_s": 100.3,
        "run_file": "cutoff-minus-00.json", "run_sha256": sha256(raw).hexdigest()}]*5}
    (directory/"comparison.json").write_text(json.dumps(comparison))


@pytest.mark.parametrize("field", ["raw_hash", "initial_hash", "cutoff", "profile"])
def test_baseline_lineage_refused(tmp_path, inputs, field):
    baseline_files(tmp_path, inputs)
    assert load_inputs(tmp_path)[1] == 100.3
    path = tmp_path/("inputs.json" if field == "profile" else "comparison.json")
    value = json.loads(path.read_text())
    if field == "profile":
        value["profile"]["booster"]["dry_mass_kg"] += 1.
    elif field == "initial_hash":
        value["initial_state_sha256"] = "incorrect"
    else:
        value["conditions"][0]["run_sha256" if field == "raw_hash" else "cutoff_time_s"] = "incorrect"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_inputs(tmp_path)


def test_invalid_input_attempt_is_retained(tmp_path):
    destination = tmp_path/"failed"
    with pytest.raises(FileNotFoundError):
        main(["--approve-simulation", "--baseline-dir", str(tmp_path/"missing"), "--output-dir", str(destination)])
    assert (destination/"attempt.json").exists()
    failure = json.loads((destination/"failure.json").read_text())
    assert failure["returned_recovery_count"] == 0 and failure["verified_cases"] == 0
    with pytest.raises(SystemExit):
        main(["--approve-simulation", "--baseline-dir", str(tmp_path), "--output-dir", str(destination)])


def test_full_baseline_replay_comparison_cannot_ignore_commands(candidate):
    assert same_physical_replay(candidate, candidate)
    other = deepcopy(candidate)
    other["recovery_record"]["checkpoints"][0]["command"]["engines"][0]["throttle"] = .99
    assert not same_physical_replay(candidate, other)


@pytest.mark.parametrize("fuel,elapsed,accepted", [(60000., .1, True), (60000.01, .1, False),
    (260000., 70., True), (260000., 69.999, False)])
def test_guard_predicate_from_measured_event_state(inputs, candidate, fuel, elapsed, accepted):
    # Predicate-unit fixtures, not physically integrated cutoff trajectories.
    p, c, initial = inputs
    record = deepcopy(candidate["recovery_record"])
    events = deepcopy(candidate["events"])
    state = deepcopy(initial)
    state.update(time_s=initial["time_s"]+elapsed, propellant_kg=fuel)
    events += [{"event": "boostback_ignition", "time_s": initial["time_s"]},
        {"event": "boostback_complete_rate_settle", "time_s": state["time_s"], "state": state,
         "remaining_propellant_kg": fuel, "cutoff_basis": "fuel_or_time_guard"}]
    if accepted:
        _capture_planning(record, events, p, c, 100.3, 0)
    else:
        with pytest.raises(_Invalid):
            _capture_planning(record, events, p, c, 100.3, 0)


@pytest.mark.parametrize("change", ["fuel_summary", "duplicate", "missing"])
def test_unbound_guard_evidence_refused(inputs, candidate, change):
    p, c, initial = inputs
    record = deepcopy(candidate["recovery_record"])
    events = deepcopy(candidate["events"])
    state = deepcopy(initial)
    state["propellant_kg"] = 60000.
    event = {"event": "boostback_complete_rate_settle", "time_s": state["time_s"], "state": state,
             "remaining_propellant_kg": 60000., "cutoff_basis": "fuel_or_time_guard"}
    if change == "fuel_summary":
        event["remaining_propellant_kg"] -= 1.
        events.append(event)
    elif change == "duplicate":
        events += [event, event]
    else:
        record["checkpoints"][-1]["phase"] = "recovery_entry_coast"
    with pytest.raises(_Invalid):
        _capture_planning(record, events, p, c, 100.3, 0)


def test_missing_low_altitude_step_refused(inputs, candidate):
    value = deepcopy(candidate)
    del value["recovery_record"]["checkpoints"][2]
    del value["samples"][2]
    value["outcome"]["integration_steps"] -= 1  # Count alone cannot mask the skipped .1 s interval.
    assert not check(value, inputs)["passed"]


def test_cli_requires_opt_in_and_refuses_overwrite(tmp_path):
    with pytest.raises(SystemExit):
        main(["--baseline-dir", str(tmp_path), "--output-dir", str(tmp_path/"new")])
    (tmp_path/"retained.txt").write_text("previous evidence")
    with pytest.raises(SystemExit):
        main(["--approve-simulation", "--baseline-dir", str(tmp_path), "--output-dir", str(tmp_path)])
    assert not (tmp_path/"new").exists()

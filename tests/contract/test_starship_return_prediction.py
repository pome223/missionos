"""Input and evidence boundaries for the opt-in offline prediction tool."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
from pathlib import Path

import pytest

from scripts.study_starship_return_candidates import main
from src.runtime import starship_sixdof as dyn
from src.runtime.starship_artifacts import write_verified_input
from src.runtime.starship_return_prediction import snapshot, validate_origin, forecast
from src.runtime.starship_return_prediction_verifier import compare_saved_suffix
from src.runtime.starship_sixdof_mission import vehicle


def example():
    profile = json.loads((Path(__file__).parents[2]/"examples/spaceflight/starship-sixdof-profile.json").read_text())
    craft = vehicle(profile, payload_count=0)
    radius = 6678137.
    state = dyn.State6DOF(1000., (radius, 0., 0.), (0., math.sqrt(3.986004418e14/radius), 0.),
        (.5, -.5, .5, .5), (.001, 0., 0.), 100000.,
        tuple(dyn.EngineState(throttle=.01, gimbal_x_rad=min(.002, e.max_gimbal_rad)) for e in craft.engines),
        tuple(min(.003, p.max_deflection_rad) for p in craft.aero_panels))
    return profile, state, snapshot({**asdict(state), "phase": "orbital_coast"}, retained_count=0)


def test_writer_verifies_the_saved_json_representation_and_preserves_evidence(tmp_path):
    value = {"state": ((1., 2., 3.),), "engines": ({"available": True},)}
    path = tmp_path/"artifact.json"
    saved = write_verified_input(path, value)
    assert saved == json.loads(path.read_text()) == {"state": [[1., 2., 3.]], "engines": [{"available": True}]}
    assert isinstance(value["state"], tuple)
    with pytest.raises(FileExistsError):
        write_verified_input(path, {"overwritten": True})
    for invalid in (float("nan"), float("inf"), object()):
        with pytest.raises((ValueError, TypeError)):
            write_verified_input(tmp_path/"invalid.json", invalid)
        assert not (tmp_path/"invalid.json").exists()


def test_origin_keeps_finite_actuators_without_reset(tmp_path):
    profile, state, origin = example()
    origin = write_verified_input(tmp_path/"origin.json", origin)
    resumed, _ = validate_origin(origin, profile, 1090., 3600.)
    assert resumed == state
    assert resumed.engine_states[0].throttle == .01
    assert resumed.flap_angles_rad[-1] == .003


def test_short_finite_forecast_preserves_origin_and_samples_fast_attitude_motion():
    profile, state, _ = example()
    state = replace(state, omega_body_rad_s=(0., 0., .3))
    origin = snapshot({**asdict(state), "phase": "orbital_coast"}, retained_count=0)
    before = deepcopy(origin)
    result = forecast(origin, profile, return_time_s=1002.5, duration_s=3.)
    assert origin == before
    assert result["outcome"]["termination"] == "time_limit"
    assert any(1000. < s["time_s"] < 1002. for s in result["samples"])
    assert result["final_state"]["time_s"] == 1003.
    assert result["final_state"]["q_body_to_eci"] != state.q_body_to_eci
    assert result["runtime_admission"] is False


@pytest.mark.parametrize("mutation", ["phase", "missing_actuator", "inventory", "past", "too_late", "nan", "short", "horizon", "truth_basis"])
def test_unsupported_origins_and_candidate_times_rejected(mutation):
    profile, state, origin = example()
    when, horizon = 1090., 3600.
    if mutation == "phase":
        with pytest.raises(ValueError):
            snapshot({**asdict(state), "phase": "ballistic_return"}, retained_count=0)
        return
    if mutation == "missing_actuator":
        origin["state"]["engine_states"] = origin["state"]["engine_states"][:-1]
    if mutation == "inventory":
        origin["retained_count"] = True
    if mutation == "past":
        when = 999.
    if mutation == "too_late":
        when = 1181.
    if mutation == "nan":
        origin["state"]["propellant_kg"] = float("nan")
    if mutation == "short":
        horizon = 90.
    if mutation == "horizon":
        horizon = 4001.
    if mutation == "truth_basis":
        origin["basis"] = "observations"
    with pytest.raises(ValueError):
        validate_origin(origin, profile, when, horizon)


def fixture():
    _, state, origin = example()
    final = replace(state, time_s=1002.)
    samples = [{**asdict(s), "phase": "orbital_coast"} for s in (state, final)]
    result = {"origin": origin, "return_time_s": 1090., "events": [], "samples": samples,
              "final_state": asdict(final), "outcome": {"termination": "time_limit", "contact_receipt": None},
              "prediction_is_execution": False, "runtime_admission": False}
    request = {"origin": origin, "return_time_s": 1090.}
    return json.loads(json.dumps([result, deepcopy(result), request]))


@pytest.mark.parametrize("mutation", ["state", "actuator", "time", "event", "claim", "origin", "fuel", "nan"])
def test_reproduction_rejects_changed_physical_records(mutation):
    result, reference, request = fixture()
    assert compare_saved_suffix(result, reference, request)["passed"]
    if mutation == "state":
        result["final_state"]["r_eci_m"][0] += .001
    if mutation == "actuator":
        result["samples"][1]["engine_states"][0]["throttle"] = 0.
    if mutation == "time":
        result["return_time_s"] += .1
    if mutation == "event":
        result["events"].append({"event": "deorbit_ignition_command", "time_s": 1001.})
    if mutation == "claim":
        result["runtime_admission"] = True
    if mutation == "origin":
        result["origin"]["retained_count"] = 1
    if mutation == "fuel":
        result["samples"][1]["propellant_kg"] += 1.
    if mutation == "nan":
        result["samples"][1]["propellant_kg"] = float("nan")
    assert not compare_saved_suffix(result, reference, request)["passed"]


def test_cli_no_opt_in_or_overwrite(tmp_path):
    argv = ["--nominal", "missing", "--delayed", "missing", "--delay-s", "0", "--output-dir", str(tmp_path/"new")]
    with pytest.raises(SystemExit) as result:
        main(argv)
    assert result.value.code == 2 and not (tmp_path/"new").exists()
    with pytest.raises(SystemExit) as result:
        main(["--approve-simulation", *argv[:-1], str(tmp_path)])
    assert result.value.code == 2

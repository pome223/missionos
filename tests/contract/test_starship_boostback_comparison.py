"""Scheduled development runs cannot become approved MissionOS policy.

Short initialized states exercise the boundary, not launch-to-catch performance.
"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from scripts import run_starship_boostback_comparison as experiment
from src.runtime import starship_booster_recovery as recovery
from src.runtime import starship_booster_recovery_verifier as verifier
from src.runtime import starship_physics as env, starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import _attitude, vehicle

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def configured():
    profile = json.loads((ROOT / experiment.SIXDOF_PROFILE).read_text())
    catch = json.loads((ROOT / experiment.CATCH_PROFILE).read_text())
    booster = vehicle(profile, "booster")
    point = env.surface_state(profile["launch"]["latitude_deg"], profile["launch"]["longitude_deg"],
                              50000., 200000., time_s=180.)
    up, east, _ = env.local_frame(point)
    state = dyn.State6DOF(180., point.r, env.add(point.v, env.scale(up, 600.)),
        _attitude(up, east), (0., 0., 0.), 200000.,
        tuple(dyn.EngineState() for _ in booster.engines), tuple(0. for _ in booster.aero_panels))
    return profile, catch, json.loads(json.dumps(asdict(state)))


@pytest.fixture(scope="module")
def scheduled(configured):
    profile, catch, initial = configured
    result = recovery.simulate_recovery(profile, initial, catch, duration_s=.6, _development_cutoff_time_s=180.2)
    return json.loads(json.dumps(result, allow_nan=False))


@pytest.mark.parametrize("cutoff", [True, 0, 180., -1, float("nan"), float("inf"), 181.])
def test_invalid_cutoff_rejected_before_propagation(configured, cutoff):
    profile, catch, initial = configured
    with pytest.raises(ValueError, match="invalid_development_cutoff"):
        recovery.simulate_recovery(profile, initial, catch, duration_s=.6, _development_cutoff_time_s=cutoff)


def test_scheduled_cutoff_is_executed_not_a_forecast(configured, scheduled):
    profile, catch, initial = configured
    events = [e for e in scheduled["events"] if e["event"] == "boostback_complete_rate_settle"]
    assert len(events) == 1 and events[0]["cutoff_basis"] == "development_scheduled_cutoff"
    assert events[0]["time_s"] == pytest.approx(180.2)
    assert scheduled["recovery_record"]["forecast_only"] is False
    assert scheduled["recovery_record"]["full_coast_prediction_count"] == 0
    assert scheduled["initial_state"]["engine_states"] == initial["engine_states"]
    result = verifier.verify_recovery(scheduled, initial, profile, catch, development_cutoff_time_s=180.2)
    assert result["passed"], result
    assert result["handoff_reached"] is False and result["launch_connected"] is False


@pytest.mark.parametrize("mutation", ["production", "wrong_input", "lost_marker", "event_clock", "renamed_basis", "forecast"])
def test_experimental_scope_or_modified_receipt_cannot_pass(configured, scheduled, mutation):
    profile, catch, initial = configured
    run = deepcopy(scheduled)
    argument = {"development_cutoff_time_s": 180.2}
    if mutation == "production":
        argument = {}
    elif mutation == "wrong_input":
        argument["development_cutoff_time_s"] = 180.3
    elif mutation == "lost_marker":
        run["recovery_record"].pop("development_cutoff")
    elif mutation == "event_clock":
        run["events"][0]["state"]["time_s"] += .01
    elif mutation == "renamed_basis":
        next(e for e in run["events"] if e["event"] == "boostback_complete_rate_settle")["cutoff_basis"] = "admitted_capture_prediction"
    else:
        run["recovery_record"]["forecast_only"] = True
    result = verifier.verify_recovery(run, initial, profile, catch, **argument)
    assert result["passed"] is False and result["issues"], result


def test_forecast_cannot_inherit_development_schedule(configured):
    profile, catch, initial = configured
    with pytest.raises(ValueError, match="invalid_development_cutoff"):
        recovery.simulate_recovery(profile, initial, catch, duration_s=.6, _forecast=True, _development_cutoff_time_s=180.2)


def test_cli_requires_opt_in_and_preserves_existing_evidence(tmp_path):
    argv = ["--separation-study", str(tmp_path / "absent.json"), "--output-dir", str(tmp_path)]
    with pytest.raises(SystemExit):
        experiment.main(argv)
    evidence = tmp_path / "failure.json"
    evidence.write_text("retained failure")
    with pytest.raises(SystemExit):
        experiment.main(["--approve-simulation", *argv])
    assert evidence.read_text() == "retained failure"

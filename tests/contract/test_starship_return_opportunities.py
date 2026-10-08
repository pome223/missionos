"""The following-orbit screen has fixed input bounds and cannot grant return."""
from copy import deepcopy

import pytest

from scripts.probe_starship_return_opportunities import candidate_set, main
from scripts.verify_starship_return_refactor import compare_runs
from src.runtime.starship_return_prediction import opportunity_window, validate_origin
from tests.contract.test_starship_return_prediction import example


def test_four_candidates_are_determined_without_future_flight_results():
    profile, state, origin = example()
    saved = deepcopy(origin)
    choices = candidate_set(origin, profile, 1090.)
    times = choices["return_times_s"]
    assert times["nominal"] == 1090. and times["delay60"] == 1150.
    assert 5000 < choices["osculating_period_s"] < 6000
    assert times["next_orbit"] == 1090.+choices["osculating_period_s"]
    assert times["next_orbit60"] == times["next_orbit"]+60.
    assert choices["runtime_admission"] is False and origin == saved
    restored, _ = validate_origin(origin, profile, times["next_orbit"], 10000., window=choices["window"])
    assert restored == state


@pytest.mark.parametrize("mutation", ["past_anchor", "moving_anchor", "bigger_budget", "wrong_schema", "missing_anchor", "too_late", "too_long"])
def test_window_does_not_silently_widen_old_bounds_or_roll_forward(mutation):
    profile, _, origin = example()
    window = opportunity_window(1090.)
    when, duration = 7090., 10000.
    if mutation == "past_anchor":
        window["scheduled_return_time_s"] = 999.
    elif mutation == "moving_anchor":
        window["scheduled_return_time_s"] += 6000.
    elif mutation == "bigger_budget":
        window["maximum_delay_s"] += 1.
    elif mutation == "wrong_schema":
        window["schema"] = "authority"
    elif mutation == "missing_anchor":
        del window["scheduled_return_time_s"]
    elif mutation == "too_late":
        when += .001
    else:
        duration += .001
    with pytest.raises(ValueError):
        validate_origin(origin, profile, when, duration, window=window)
    with pytest.raises(ValueError):
        validate_origin(origin, profile, 7090., 10000.)


def test_probe_requires_opt_in_before_reading_or_writing(tmp_path):
    argv = ["--reference-dir", "missing", "--candidate", "nominal", "--output-dir", str(tmp_path/"new")]
    with pytest.raises(SystemExit) as failure:
        main(argv)
    assert failure.value.code == 2 and not (tmp_path/"new").exists()


@pytest.mark.parametrize("field", ["samples", "events", "final_state", "booster_run", "satellites"])
def test_refactor_regression_ignores_only_source_identity(field):
    old = {"development_source_sha256": {"file": "old"}, field: [1, 2, 3]}
    new = {**deepcopy(old), "development_source_sha256": {"file": "new"}}
    assert all(compare_runs(old, new).values())
    new[field][0] += 1
    assert not all(compare_runs(old, new).values())

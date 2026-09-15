import pytest

from scripts.tb3_prediction.wait_clearance import verify_clearance


def run_observations(exposures, start=4.0, current=1.0):
    state = {"t": start, "i": 0}

    def tick():
        state["t"] += 0.125
        state["i"] += 1

    return verify_clearance(
        lambda: exposures[min(state["i"], len(exposures) - 1)],
        tick,
        lambda: state["t"],
        0,
        current,
    )


def test_observed_small_residual_can_clear_without_an_unnecessary_detour():
    r = run_observations([0.15, 0.12, 0.08, 0.04])
    assert r["passed"] and r["extension_elapsed_sim_s"] == 0.375


def test_persistent_residual_stops_at_one_second_without_claiming_clearance():
    r = run_observations([0.15])
    assert not r["passed"] and r["extension_elapsed_sim_s"] == 1


def test_large_remaining_obstacle_does_not_receive_more_wait_budget():
    r = run_observations([0.5])
    assert not r["passed"] and r["extension_elapsed_sim_s"] == 0


def test_no_observed_progress_does_not_receive_more_wait_budget():
    r = run_observations([0.15], current=0.15)
    assert not r["passed"] and not r["extension_eligible"]


def test_extension_never_resets_the_context_based_five_second_deadline():
    r = run_observations([0.15], start=4.75)
    assert not r["passed"] and r["extension_elapsed_sim_s"] == 0.25


def test_reversing_progress_cancels_extension():
    r = run_observations([0.15, 0.3])
    assert not r["passed"] and r["extension_elapsed_sim_s"] == 0.125
    assert r["reason"] == "observed_clearance_progress_reversed"


def test_initially_clear_observation_needs_no_extension():
    r = run_observations([0])
    assert r["passed"] and r["extension_elapsed_sim_s"] == 0


def test_invalid_observation_is_rejected():
    with pytest.raises(ValueError):
        run_observations([float("nan")])

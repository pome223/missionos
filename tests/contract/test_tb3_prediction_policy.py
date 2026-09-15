"""Admission and data-leakage checks for the navigation development baseline."""

import pytest

from scripts.tb3_prediction.policy import predict_choice
from scripts.tb3_prediction.nwm_assets import encode_action


def history(y=-1.1, v=0.18):
    return [{"t_s": i / 10, "x_m": 0.0, "y_m": y + v * i / 10} for i in range(21)]


def test_opposite_timing_requires_distinct_executable_candidates():
    clearing = predict_choice(history(), [-1.0, 0.0])
    lingering = predict_choice(history(0.0, 0.015), [-1.0, 0.0])
    assert clearing["candidate"] == "wait"
    assert lingering["candidate"] == "detour"
    assert clearing["learned_wam_invoked"] is False
    assert lingering["vla_invoked"] is False


@pytest.mark.parametrize(
    "forbidden", ["scenario", "future_y_m", "actual_success", "candidate_label"]
)
def test_selector_rejects_future_or_case_identity_in_observation(forbidden):
    rows = history()
    rows[-1][forbidden] = 1
    with pytest.raises(ValueError, match="only timestamp"):
        predict_choice(rows, [-1.0, 0.0])


@pytest.mark.parametrize("bad_time", [0.0, float("nan"), float("inf")])
def test_selector_rejects_invalid_observation_times(bad_time):
    rows = history()
    rows[-1]["t_s"] = bad_time
    with pytest.raises(ValueError):
        predict_choice(rows, [-1.0, 0.0])


def test_short_horizon_cannot_rank_unobserved_terminal_event():
    with pytest.raises(ValueError, match="outside prediction horizon"):
        predict_choice(history(), [-1.0, 0.0], horizon_s=0.8)


def test_missing_history_cannot_be_reported_as_prediction():
    with pytest.raises(ValueError, match="history"):
        predict_choice(history()[:1], [-1.0, 0.0])


def test_nwm_zero_motion_is_not_zero_normalized_x():
    assert encode_action(0, 0, 0) == pytest.approx([-1 / 3, 0, 0])
    # Round trip through published scaling to recover the metric action.
    x, y, yaw = encode_action(0.2, -0.1, 0.25)
    assert ((x + 1) / 2 * 7.5 - 2.5) * 0.255 == pytest.approx(0.2)
    assert ((y + 1) / 2 * 8 - 4) * 0.255 == pytest.approx(-0.1)
    assert yaw == 0.25


def test_velocity_estimate_tolerates_observer_endpoint_lag():
    rows = history()
    # Last observation lags the sample clock by one actor update (100 ms).
    rows[-1]["y_m"] = rows[-2]["y_m"]
    decision = predict_choice(rows, [-1.0, 0.0])
    assert decision["velocity_estimate_mps"][1] == pytest.approx(0.18, abs=0.003)
    assert decision["candidate"] == "wait"

import pytest

from scripts.tb3_prediction.image_motion_guard import check_edges


TIMES = [0, 0.5, 1, 1.5]


def test_constant_translation_can_agree_with_clear_forecast():
    result = check_edges([[100, 190], [85, 175], [70, 160], [55, 145]], TIMES)
    assert result["wait_consistent"] is True


def test_remaining_obstruction_contradicts_clear_forecast():
    result = check_edges([[100, 210], [95, 205], [90, 200], [85, 195]], TIMES)
    assert result["wait_consistent"] is False
    assert result["reason"] == "observed_edges_still_block"


def test_clipped_leading_history_does_not_falsely_look_stationary():
    result = check_edges([[140, 223], [110, 223], [80, 223], [50, 215]], TIMES)
    assert result["wait_consistent"] is True
    assert result["velocity_px_s"] == -60


def test_clipped_trailing_edge_has_unknown_extent():
    result = check_edges([[140, 223], [110, 223], [80, 223], [50, 223]], TIMES)
    assert result["wait_consistent"] is None


def test_nonlinear_motion_is_not_claimed_as_a_contradiction():
    result = check_edges([[100, 190], [80, 170], [100, 190], [80, 170]], TIMES)
    assert result["wait_consistent"] is None


def test_stationary_obstacle_still_blocks():
    assert check_edges([[80, 160]] * 4, TIMES)["wait_consistent"] is False


@pytest.mark.parametrize("times", [[0, 0.5, 0.5, 1.5], [0, 0.5, 1, float("nan")], [0, 1]])
def test_invalid_time_contract_is_rejected(times):
    with pytest.raises(ValueError):
        check_edges([[80, 160]] * 4, times)


def test_unobserved_obstacle_is_not_claimed_clear():
    assert check_edges([None] * 4, TIMES)["wait_consistent"] is None


def test_small_jitter_relative_to_motion_still_detects_a_blocked_route():
    result = check_edges([[73, 223], [57, 223], [48, 223], [27, 209]], [0, 0.528, 1.056, 1.584])
    assert result["wait_consistent"] is False
    assert result["reason"] == "observed_edges_still_block"


def test_fast_motion_with_small_relative_jitter_retains_clearance():
    result = check_edges([[148, 223], [127, 223], [112, 210], [83, 179]], [0, 0.528, 1.056, 1.584])
    assert result["wait_consistent"] is True

import pytest

from scripts.tb3_prediction.camera_history_policy import (
    approaching,
    history_decision,
    learned_decision,
)


def assessed(incoming=False, consistency=None):
    return {"incoming": {"approaching": incoming}, "motion": {"wait_consistent": consistency}}


def test_future_blockage_cannot_be_discarded_when_current_route_is_clear():
    assert learned_decision(0.0294, 0.9678, assessed(True)) == "detour"


def test_learned_future_clearance_can_allow_wait_for_a_partly_hidden_approaching_actor():
    a = assessed(True)
    assert history_decision(0, a) == "detour"
    assert learned_decision(0, 0.066, a) == "wait"


def test_both_policies_retain_direct_when_no_observed_or_predicted_hazard():
    a = assessed()
    assert learned_decision(0, 0.01, a) == history_decision(0, a) == "direct"


def test_prediction_does_not_override_a_supported_motion_contradiction():
    assert learned_decision(0.8, 0.01, assessed(False, False)) == "detour"


def test_pessimistic_model_does_not_discard_supported_observed_clearance():
    a = assessed(True, True)
    assert learned_decision(0.146, 0.566, a) == history_decision(0.146, a) == "wait"


def test_supported_clearance_does_not_add_a_wait_when_no_hazard_exists():
    assert learned_decision(0, 0.01, assessed(False, True)) == "direct"


def test_history_uses_time_and_visible_edges_when_one_early_frame_has_no_obstacle():
    a = approaching([None, [200, 223], [175, 223], [149, 223]], [0, 0.5, 1, 1.5])
    assert a["approaching"] and a["velocity_px_s"] < 0


def test_motion_away_from_route_does_not_trigger_anticipatory_wait():
    assert not approaching([[149, 223], [175, 223], [200, 223], [220, 223]], [0, 0.5, 1, 1.5])[
        "approaching"
    ]


@pytest.mark.parametrize("current,forecast", [(float("nan"), 0), (0, float("inf")), (0, -1)])
def test_invalid_scores_are_rejected(current, forecast):
    with pytest.raises(ValueError):
        learned_decision(current, forecast, assessed())


def test_small_existing_overlap_does_not_hide_further_entry_from_history_baseline():
    a = approaching([[206, 223], [181, 223], [164, 223], [144, 223]], [0, 0.528, 1.056, 1.584])
    assert a["approaching"]
    assert (
        history_decision(0.0294, {"incoming": a, "motion": {"wait_consistent": None}}) == "detour"
    )

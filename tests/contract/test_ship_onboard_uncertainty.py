from copy import deepcopy

import pytest

from src.runtime.ship_onboard import choose_onboard_action
from src.runtime.ship_onboard_uncertainty import _clearance_time, choose_initial_gate


def history(x=0, v=2, a=0):
    return [
        {"observed_at_s": t, "obstacle_x_m": x + v * t + a * t * t / 2} for t in (0, 0.5, 1, 1.5, 2)
    ]


@pytest.mark.parametrize("motion", [(0, 2, 0), (0, 0.15, 0), (0, 4, -1), (20, 0, 0)])
def test_existing_nominal_choices_and_authority_are_preserved(motion):
    points = history(*motion)
    base = choose_onboard_action(points, "onboard_stopping")
    gate = choose_onboard_action(points, "onboard_uncertainty")
    assert gate["action"] == base["action"]
    assert not gate["uncertainty_gate"]["dispatch_allowed"]
    assert not gate["vla_invoked"] and not gate["wam_invoked"]


def test_braking_prediction_holds_at_rest_and_requires_two_samples():
    assert _clearance_time([10, 2, 0], after=0, deceleration=1, horizon=30) is None
    assert _clearance_time([14, 0, 0], horizon=30) == 0.5


def test_bad_fit_abstains_instead_of_inventing_confidence():
    points = history()
    points[2]["obstacle_x_m"] += 3
    base = choose_onboard_action(points, "onboard_stopping")
    result = choose_initial_gate(points, base, airspeed_mps=12)
    assert result["action"] == base["action"]
    assert result["uncertainty_gate"]["reason"] == "poor_fit_retain_baseline"


def test_acceleration_can_change_proposal_without_creating_dispatch_authority():
    points = history(12, -3, 3)
    assert choose_onboard_action(points, "onboard_stopping")["action"] == "detour"
    gate = choose_onboard_action(points, "onboard_uncertainty")
    assert gate["action"] == "wait"
    assert gate["uncertainty_gate"]["action_changed"]
    assert not gate["uncertainty_gate"]["dispatch_allowed"]
    assert gate["uncertainty_gate"]["reason"] == "all_hypotheses_favor_wait"


@pytest.mark.parametrize("fault", ["future_truth", "gap", "reversed", "nan", "too_few"])
def test_unobserved_or_invalid_data_cannot_enter_filter(fault):
    points = history()
    base = choose_onboard_action(points, "onboard_stopping")
    if fault == "future_truth":
        points[0]["future_clearance_s"] = 2
    elif fault == "gap":
        points[-1]["observed_at_s"] = 5
    elif fault == "reversed":
        points[1]["observed_at_s"] = points[0]["observed_at_s"]
    elif fault == "nan":
        points[2]["obstacle_x_m"] = float("nan")
    else:
        points = points[:3]
    with pytest.raises(ValueError):
        choose_initial_gate(points, base, airspeed_mps=12)


def test_hidden_future_cannot_change_the_proposal():
    points = history()
    before = deepcopy(points)
    first = choose_onboard_action(points, "onboard_uncertainty")
    second = choose_onboard_action(deepcopy(points), "onboard_uncertainty")
    assert points == before and first == second
    assert first["uncertainty_gate"]["observed_through_s"] == 2


def test_optional_policy_reaches_real_cli_preflight_without_starting_docker(tmp_path):
    from click.testing import CliRunner
    from missionos_cli.cli import missionos

    result = CliRunner().invoke(
        missionos,
        [
            "ship-delivery",
            "run-sitl",
            "--urban-case",
            "brake_stop",
            "--urban-policy",
            "onboard_uncertainty",
            "--output-dir",
            str(tmp_path / "run"),
        ],
    )
    assert result.exit_code != 0
    assert "approve" in result.output.lower()
    assert not (tmp_path / "run").exists()

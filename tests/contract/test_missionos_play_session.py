"""Contract tests for the deterministic ``missionos play`` trade-off model.

These lock in the core game property: turning a knob actually changes the
situation, and "go higher" is never a free win.
"""

import pytest

from src.runtime.delivery_recovery_decision import DeliveryRecoveryAction
from src.runtime.missionos_play_scenario import load_scenario
from src.runtime.missionos_play_session import (
    PlayKnobs,
    compare_plans,
    evaluate_plan,
)

pytestmark = pytest.mark.contract


@pytest.fixture()
def scenario():
    return load_scenario()


def test_higher_altitude_improves_clearance_but_costs_return_reserve(scenario) -> None:
    base = PlayKnobs(altitude_m=2900, route="direct", payload_kg=1.0)
    clearances = []
    reserves = []
    for altitude in (2900, 3000, 3100, 3200):
        plan = evaluate_plan(scenario, base.with_(altitude_m=float(altitude)))
        clearances.append(plan.clearance_m)
        reserves.append(plan.return_reserve_wh)

    assert all(a < b for a, b in zip(clearances, clearances[1:])), clearances
    assert all(a > b for a, b in zip(reserves, reserves[1:])), reserves


def test_higher_altitude_increases_effective_wind(scenario) -> None:
    base = PlayKnobs(altitude_m=2900, route="direct", declared_wind_mps=6.0)
    low = evaluate_plan(scenario, base)
    high = evaluate_plan(scenario, base.with_(altitude_m=3200.0))
    assert high.effective_wind_mps > low.effective_wind_mps


def test_clearance_below_rule_recommends_raising_to_minimum_safe_altitude(scenario) -> None:
    # direct ridge is 2950 m; at 2960 m clearance is 10 m, under the 30 m rule.
    knobs = PlayKnobs(altitude_m=2960, route="direct", min_clearance_rule_m=30.0)
    plan = evaluate_plan(scenario, knobs)
    assert plan.status == "warning"
    assert "terrain_clearance_below_minimum" in plan.risk_labels
    assert plan.recommended_altitude_m == 2980.0  # ridge 2950 + 30 rule


def test_over_margin_altitude_recommends_trimming_down(scenario) -> None:
    knobs = PlayKnobs(altitude_m=3150, route="direct", min_clearance_rule_m=30.0)
    plan = evaluate_plan(scenario, knobs)
    assert plan.status == "ready"
    assert plan.recommended_altitude_m == 2980.0
    assert "2980" in plan.recommendation_reason


def test_altitude_above_ceiling_blocks_and_escalates(scenario) -> None:
    knobs = PlayKnobs(altitude_m=3300, route="direct")
    plan = evaluate_plan(scenario, knobs)
    assert plan.status == "blocked"
    assert "altitude_above_vehicle_ceiling" in plan.risk_labels
    assert plan.recommendation is DeliveryRecoveryAction.OPERATOR_ESCALATION_REQUIRED


def test_wind_over_limit_recommends_return_to_home(scenario) -> None:
    knobs = PlayKnobs(altitude_m=3000, route="direct", declared_wind_mps=9.6)
    plan = evaluate_plan(scenario, knobs)
    assert plan.status == "blocked"
    assert "wind_above_recovery_limit" in plan.risk_labels
    assert plan.recommendation is DeliveryRecoveryAction.RETURN_TO_HOME_RECOMMENDED


def test_detour_trades_distance_for_lower_terrain_ceiling(scenario) -> None:
    direct = evaluate_plan(scenario, PlayKnobs(altitude_m=3000, route="direct"))
    west = evaluate_plan(scenario, PlayKnobs(altitude_m=3000, route="west"))
    delta = compare_plans(direct, west)
    # west is longer but skirts a lower ridge -> more clearance head-room.
    assert delta.route_distance_m > 0
    assert delta.clearance_m > 0
    # ... and the extra distance costs return reserve.
    assert delta.return_reserve_wh < 0


def test_recommendation_is_always_a_recovery_action(scenario) -> None:
    for altitude in (2900, 2960, 3000, 3150, 3300):
        plan = evaluate_plan(scenario, PlayKnobs(altitude_m=float(altitude)))
        assert isinstance(plan.recommendation, DeliveryRecoveryAction)

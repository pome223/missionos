"""Observed-data gates, charged re-observation, and baseline preservation boundaries."""

from copy import deepcopy
import itertools
import json

from click.testing import CliRunner
import pytest

from missionos_cli.cli import missionos
from src.runtime.ship_dynamic_routes import WINDOW_PROTOCOL, generate_cases as old_cases
from src.runtime import ship_uncertainty_gate as gate


def observation(end=0, deadline=60, noisy=False):
    rows = []
    for i in range(16):
        t = (i - 15) * 0.25
        rows.append(
            {"observed_at_s": t, "direct_x_m": 20 + (2 if i % 2 and noisy else 0), "detour_x_m": 30}
        )
    return {"history": rows, "deadline_s": deadline, "observed_through_s": end}


def test_braking_model_stops_instead_of_reversing():
    assert gate.brake_position([10, 2, 0], 3, 2, 1) == 15.5
    assert gate.brake_position([10, 2, 0], 10, 2, 1) == 16
    assert gate.brake_position([10, -2, 0], 10, 2, 1) == 4


def test_keep_feasible_baseline_and_abstain_on_bad_fit():
    result = gate.assess_gate(observation(), 0.5, 0.5, 0)
    assert not result["recommend_detour"]
    assert result["reason"] == "baseline_predicted_feasible"
    result = gate.assess_gate(observation(noisy=True), 0.5, 0.5, 0)
    assert not result["recommend_detour"]
    assert result["reason"] == "poor_motion_fit"


@pytest.mark.parametrize(
    "mutation", ["future_field", "future_frame", "future_clock", "stale", "deadline", "retreat"]
)
def test_future_and_inconsistent_execution_context_are_rejected(mutation):
    obs = observation()
    retreat = 0
    if mutation == "future_field":
        obs["turn_at_s"] = 4
    elif mutation == "future_frame":
        obs["history"][-1]["observed_at_s"] = 1
    elif mutation == "future_clock":
        obs["observed_through_s"] = 1
    elif mutation == "stale":
        obs["observed_through_s"] = 0.25
    elif mutation == "deadline":
        obs["deadline_s"] = float("nan")
    else:
        retreat = 1
    with pytest.raises(ValueError):
        gate.assess_gate(obs, 0.5, 0.5, retreat)


def test_reobservation_cost_includes_full_retreat(monkeypatch):
    requested = []

    def history_at(stamp):
        requested.append(stamp)
        return observation(stamp)

    def decide(obs, now, start, retreat, **kwargs):
        gate.validate_observation(obs, now)
        return {
            "at_s": now,
            "observed_through_s": obs["observed_through_s"],
            "retreat_s": retreat,
            "recommend_detour": now == 2.5,
        }

    monkeypatch.setattr(gate, "assess_gate", decide)
    outcome = gate.run_controller(history_at, lambda r, t: 50, 60, "shadow_gate")
    assert requested == [0, 1, 2]
    assert outcome["departure_s"] == 4.5  # decision at 2.5 plus 2 seconds back to branch
    assert outcome["finish_s"] == 40.5  # 4.5 + 12 + 1 + 23
    assert all(e["observed_through_s"] + 0.5 == e["at_s"] for e in outcome["decisions"])


def test_shadow_fallback_does_not_delay_baseline(monkeypatch):
    def reject(obs, now, start, retreat, **kwargs):
        return {"at_s": now, "retreat_s": retreat, "recommend_detour": False}

    monkeypatch.setattr(gate, "assess_gate", reject)

    def sensor(route, stamp):
        return 50 if stamp >= 10 else 0

    def history(stamp):
        return observation(stamp, 31)

    base = gate.run_controller(history, sensor, 31, "baseline")
    shadow = gate.run_controller(history, sensor, 31, "shadow_gate")
    assert shadow["success"] == base["success"]
    assert shadow["finish_s"] == base["finish_s"] == 28.5
    assert len(shadow["decisions"]) == 6


def test_holding_can_lose_a_baseline_success():
    def history(stamp):
        return observation(stamp, 25)

    assert gate.run_controller(history, lambda r, t: 50, 25, "baseline")["success"]
    assert not gate.run_controller(history, lambda r, t: 50, 25, "hold_gate")["success"]


def test_sensor_cannot_rewrite_mission_deadline():
    for policy in ("raw_ca", "patient_ca", "initial_gate", "shadow_gate", "hold_gate"):
        with pytest.raises(ValueError, match="deadline"):
            gate.run_controller(lambda t: observation(t, 80), lambda r, t: 50, 60, policy)


def test_regression_initial_observations_exactly_match_previous_panel():
    for new, old in zip(
        itertools.islice(gate.generate_cases(), 4320), old_cases(WINDOW_PROTOCOL), strict=True
    ):
        obs = gate.observed_history(new, 0)
        assert {k: obs[k] for k in ("history", "deadline_s")} == old["online_input"]
    old_profiles = {
        tuple(p) for g in WINDOW_PROTOCOL["cohorts"].values() for p in g["profiles_p_v_a"]
    }
    assert not old_profiles & {tuple(p) for p in gate.PROTOCOL["fresh_profiles_p_v_a"]}


def test_future_variants_share_initial_online_input():
    first = list(itertools.islice(gate.generate_cases(), 2))
    assert first[0]["evaluator_only"] != first[1]["evaluator_only"]
    assert gate.observed_history(first[0], 0) == gate.observed_history(first[1], 0)


def test_freeze_and_cli_preserve_protocol(tmp_path):
    runner = CliRunner()
    prefix = ["ship-delivery", "uncertainty-screen"]
    assert runner.invoke(missionos, prefix).exit_code == 2
    frozen = tmp_path / "frozen.json"
    assert runner.invoke(missionos, prefix + ["--freeze", str(frozen)]).exit_code == 0
    initial = frozen.read_bytes()
    assert runner.invoke(missionos, prefix + ["--freeze", str(frozen)]).exit_code == 1
    assert runner.invoke(missionos, prefix + ["--protocol", str(frozen)]).exit_code == 2
    assert (
        runner.invoke(
            missionos, prefix + ["--protocol", str(frozen), "--output", str(frozen)]
        ).exit_code
        == 1
    )
    assert frozen.read_bytes() == initial
    altered = deepcopy(json.loads(initial))
    altered["protocol"]["fit_rms_limit_m"] = 10
    frozen.write_text(json.dumps(altered))
    with pytest.raises(ValueError):
        gate.read_freeze(frozen)

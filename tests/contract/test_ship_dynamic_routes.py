"""Causality, guarded route execution, and prediction input contracts."""

from copy import deepcopy
import itertools
import json
import math

from click.testing import CliRunner
import pytest

from missionos_cli.cli import missionos
from src.runtime.ship_dynamic_routes import (
    PROTOCOL,
    WINDOW_PROTOCOL,
    forecast_parameters,
    freeze_protocol,
    generate_cases,
    guard_threshold,
    propagate,
    read_freeze,
    run_procedure,
    select_candidate,
    world_position,
)


def first_case():
    return next(generate_cases())


def test_bounded_motion_integrates_velocity_cap_and_continuous_change():
    assert propagate(0, 1, 1, 5) == (15.5, 4)
    assert propagate(0, 1, -1, 7) == (-15.5, -4)
    world = first_case()["evaluator_only"]
    other = {**world, "future_variant": "unannounced_turn"}
    for route in PROTOCOL["routes"]:
        assert world_position(world, route, 4) == world_position(other, route, 4)
        assert (
            abs(world_position(other, route, 4.000001) - world_position(other, route, 4))
            <= 0.0000041
        )
        positions = [world_position(other, route, t / 10) for t in range(1000)]
        assert all(abs(b - a) <= 0.400000001 for a, b in zip(positions, positions[1:]))


def test_guard_never_uses_future_and_preserves_crossing_margin():
    observations = []

    def observe(route, stamp):
        observations.append(stamp)
        return 20

    outcome = run_procedure(observe, "direct_wait", 60, 0.5, trace=True)
    assert outcome["finish_s"] == 24.5
    assert observations == [6.0, 6.5]
    assert max(observations) == outcome["cross_at_s"]
    assert outcome["guard_lower_bound_m"] >= PROTOCOL["clearance_margin_m"]
    assert not run_procedure(lambda r, t: guard_threshold() - 0.01, "direct_wait", 60, 0.5)[
        "success"
    ]


def test_switch_charges_wait_retreat_and_second_approach():
    def sensor(route, stamp):
        return 0 if route == "direct" else 50

    result = run_procedure(sensor, "direct_switch4", 60, 0.5, trace=True)
    assert result["success"] and result["switched"] and result["route"] == "detour"
    assert result["finish_s"] == 52.5  # .5 + 6 + 4 + 6 + 12 + 1 + 23
    assert result["gate_wait_s"] == 4
    assert not run_procedure(sensor, "direct_wait", 60, 0.5)["success"]
    assert not run_procedure(sensor, "direct_switch4", 50, 0.5)["success"]
    assert run_procedure(sensor, "detour_wait", 50, 0.5)["finish_s"] == 36.5


def test_deadline_failures_do_not_emit_backwards_or_overdue_events():
    for candidate in PROTOCOL["candidates"]:
        for budget, latency in ((10, 41.5), (25, 0.5), (40, 10)):
            result = run_procedure(lambda r, t: 0, candidate, budget, latency, trace=True)
            stamps = [e["at_s"] for e in result["events"]]
            assert stamps == sorted(stamps)
            assert all(t <= budget for t in stamps)
            assert not result["success"]


@pytest.mark.parametrize("mutation", ["future", "future_frame", "case_id", "nonfinite", "oracle"])
def test_selector_rejects_future_metadata(mutation):
    data = deepcopy(first_case()["online_input"])
    policy = "constant_acceleration"
    if mutation == "future":
        data["history"][-1]["clear_at_s"] = 1
    elif mutation == "future_frame":
        data["history"][-1]["observed_at_s"] = 1
    elif mutation == "case_id":
        data["case_id"] = "future-leak"
    elif mutation == "nonfinite":
        data["history"][0]["direct_x_m"] = math.nan
    else:
        policy = "oracle"
    with pytest.raises(ValueError):
        select_candidate(data, policy)


def test_identical_pasts_do_not_select_different_actions_for_hidden_futures():
    cases = list(itertools.islice(generate_cases(), 2))
    assert cases[0]["online_input"] == cases[1]["online_input"]
    assert cases[0]["evaluator_only"] != cases[1]["evaluator_only"]
    for policy in PROTOCOL["policies"]:
        assert select_candidate(cases[0]["online_input"], policy) == select_candidate(
            cases[1]["online_input"], policy
        )


def test_ca_estimate_from_past_matches_declared_continued_motion():
    # Exact quadratic history should recover the simple kinematic model; this
    # tests future estimation from observations without giving it actor params.
    for case in generate_cases():
        if (
            case["noise_amplitude_m"]
            or case["evaluator_only"]["future_variant"] != "continued_motion"
        ):
            continue
        params = forecast_parameters(case["online_input"], "constant_acceleration")
        for route in PROTOCOL["routes"]:
            for stamp in (0, 3, 8, 20, 60):
                assert propagate(*params[route], stamp)[0] == pytest.approx(
                    world_position(case["evaluator_only"], route, stamp), abs=1e-7
                )


def test_freeze_and_cli_fail_closed(tmp_path):
    path = tmp_path / "freeze.json"
    receipt = freeze_protocol(path)
    assert read_freeze(path) == receipt
    with pytest.raises(FileExistsError):
        freeze_protocol(path)
    altered = deepcopy(receipt)
    altered["protocol"]["crossing_s"] = 0
    path.write_text(json.dumps(altered))
    with pytest.raises(ValueError):
        read_freeze(path)
    runner = CliRunner()
    prefix = ["ship-delivery", "dynamic-routes-screen"]
    assert runner.invoke(missionos, prefix).exit_code == 2
    new = tmp_path / "cli-freeze.json"
    assert runner.invoke(missionos, prefix + ["--freeze", str(new)]).exit_code == 0
    assert (
        runner.invoke(missionos, prefix + ["--protocol", str(new), "--output", str(new)]).exit_code
        == 2
    )
    assert runner.invoke(missionos, prefix + ["--freeze", str(new)]).exit_code == 1


def test_window_panel_uses_fresh_motion_parameters_and_bound_protocol(tmp_path):
    old = {tuple(p) for grid in PROTOCOL["cohorts"].values() for p in grid["profiles_p_v_a"]}
    new = {tuple(p) for grid in WINDOW_PROTOCOL["cohorts"].values() for p in grid["profiles_p_v_a"]}
    assert not old & new
    assert len(new) == 12
    path = tmp_path / "window-freeze.json"
    freeze_protocol(path, "windows")
    assert read_freeze(path)["protocol"] == WINDOW_PROTOCOL
    cases = generate_cases(WINDOW_PROTOCOL)
    first = next(cases)
    assert first["evaluator_only"]["profiles"]["direct"] == [-22, 1.8, 0]
    result = CliRunner().invoke(
        missionos,
        ["ship-delivery", "dynamic-routes-screen", "--protocol", str(path), "--panel", "sparse"],
    )
    assert result.exit_code == 2

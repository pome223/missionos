"""Admission boundaries and algebraic checks independent of panel generation."""

from collections import defaultdict
import json

import pytest
from click.testing import CliRunner

from missionos_cli.cli import missionos
from src.runtime.ship_wam_headroom import (
    PROTOCOL,
    candidate_outcome,
    detour_seconds,
    digest,
    freeze_protocol,
    read_freeze,
    run_screen,
    select_candidate,
)


def observed(slack=30):
    return {
        "slack_s": slack,
        "history": [{"observed_at_s": t, "obstacle_x_m": 8 + t} for t in (-2, -1, 0)],
    }


@pytest.fixture(scope="module")
def screen(tmp_path_factory):
    path = tmp_path_factory.mktemp("headroom") / "freeze.json"
    freeze_protocol(path)
    return run_screen(path)


def test_sampling_and_latency_are_real_costs_in_the_analytic_model():
    assert candidate_outcome("wait", 2, 3)["extra_s"] == 2.5
    assert not candidate_outcome("wait", 2.01, 2.5)["budget_feasible"]
    assert candidate_outcome("wait", 2, 30, 10)["extra_s"] == 10
    assert candidate_outcome("wait_2_then_detour", 2, 30)["extra_s"] == 2 + detour_seconds()
    assert candidate_outcome("wait_8_then_detour", 2, 30)["extra_s"] == 2.5
    assert candidate_outcome("wait", None, 200)["reason"] == "clearance_timeout"
    assert all(
        not candidate_outcome(c, 0, 10, 41.19)["budget_feasible"] for c in PROTOCOL["candidates"]
    )


def test_budget_rule_attains_feasibility_upper_bound_beyond_the_frozen_grid():
    # If B >= D the detour succeeds. If B < D every detour (including a
    # delayed one) fails, leaving confirmed direct clearance as the only option.
    for slack in (0, 0.5, 1, 2, 7, 21, 22.166, detour_seconds(), 23, 70, 125):
        for clear in (None, 0, 0.001, 1.5, 6.7, 21.7, 22, 119.5, 120, 200):
            chosen = select_candidate(observed(slack), "budget_detour_else_wait")
            outcomes = [candidate_outcome(c, clear, slack) for c in PROTOCOL["candidates"]]
            assert candidate_outcome(chosen, clear, slack)["budget_feasible"] == any(
                row["budget_feasible"] for row in outcomes
            )


@pytest.mark.parametrize("mutation", ["future", "future_row", "case_id", "nan", "oracle"])
def test_future_and_oracle_are_not_online_inputs(mutation):
    data = observed()
    policy = "onboard_stopping"
    if mutation == "future":
        data["history"][-1]["clear_at_s"] = 3
    elif mutation == "future_row":
        data["history"].append({"observed_at_s": 1, "obstacle_x_m": 9})
    elif mutation == "case_id":
        data["case_id"] = "persistent"
    elif mutation == "nan":
        data["slack_s"] = float("nan")
    else:
        policy = "oracle"
    with pytest.raises(ValueError):
        select_candidate(data, policy)


def test_frozen_panel_has_disjoint_states_and_aliased_futures(screen):
    groups = defaultdict(list)
    split_keys = defaultdict(set)
    for row in screen["cases"]:
        assert digest(row["online_input"]) == row["online_input_sha256"]
        groups[row["online_input_sha256"]].append(row)
        # Exclude slack as well: evaluation changes observed motion, not just deadlines.
        split_keys[row["split"]].add(digest(row["online_input"]["history"]))
    assert not split_keys["development"] & split_keys["evaluation"]
    for group in groups.values():
        assert len(group) == 4
        assert len({json.dumps(r["policy_choices"], sort_keys=True) for r in group}) == 1
        assert len({r["encoded_input_oracle_candidate"] for r in group}) == 1
        assert len({r["evaluator_only"]["clear_at_s"] for r in group}) >= 3
    assert all(s["cases"] == 192 for s in screen["summaries"].values())
    assert not screen["screen_admits_native_wam_followup"]
    assert not any(screen[k] for k in ("wam_invoked", "vla_invoked", "px4_runtime_invoked"))
    for summary in screen["summaries"].values():
        paired = summary["zero_latency_oracle_vs_each"]["budget_detour_else_wait"]
        assert paired["feasibility_gains"] == paired["feasibility_losses"] == 0


def test_freeze_tampering_and_overwrite_are_rejected(tmp_path):
    path = tmp_path / "freeze.json"
    receipt = freeze_protocol(path)
    with pytest.raises(FileExistsError):
        freeze_protocol(path)
    receipt["protocol_sha256"] = "0" * 64
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="changed"):
        read_freeze(path)


def test_cli_requires_prior_freeze_and_preserves_it(tmp_path):
    runner = CliRunner()
    prefix = ["ship-delivery", "wam-headroom"]
    assert runner.invoke(missionos, prefix).exit_code == 2
    frozen, output = tmp_path / "freeze.json", tmp_path / "result.json"
    assert runner.invoke(missionos, prefix + ["--freeze", str(frozen)]).exit_code == 0
    before = frozen.read_bytes()
    assert (
        runner.invoke(
            missionos, prefix + ["--protocol", str(frozen), "--output", str(frozen)]
        ).exit_code
        == 2
    )
    result = runner.invoke(missionos, prefix + ["--protocol", str(frozen), "--output", str(output)])
    assert result.exit_code == 0, result.output[-1000:]
    assert frozen.read_bytes() == before
    assert len(json.loads(output.read_text())["cases"]) == 384

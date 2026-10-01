"""The 60 s primary endpoint must not turn censored futures into negatives."""

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import evaluate_yokohama_pad_judge as evaluation

FIXTURE = Path(__file__).parent / "fixtures/yokohama_pad_judge_answers.json"


@pytest.mark.parametrize(
    "returned,truncated,expected",
    [
        (True, True, "observed_return"),
        (True, False, "observed_return"),
        (False, False, "fully_observed_no_return"),
        (False, True, "censored"),
        (False, None, "censored"),
    ],
)
def test_truth_with_observation_censoring(returned, truncated, expected):
    assert (
        evaluation.truth_classification(dict(reentered=returned, horizon_truncated=truncated))
        == expected
    )


def test_summary_separates_censored_decisions_from_confirmed_negatives():
    rows = [
        dict(
            reentered=returned,
            horizon_truncated=truncated,
            motion="approaching",
            arm=dict(status="valid", action=action, wait_seconds=10 if action == "wait" else 0),
        )
        for returned, truncated, action in [
            (True, True, "wait"),
            (False, False, "wait"),
            (False, True, "wait"),
            (False, True, "enter"),
        ]
    ]
    s = evaluation.summarize_arms(rows, ["arm"])["arm"]
    assert s["reentry_visible"]["n"] == 1
    assert s["fully_observed_no_return"]["n"] == s["fully_observed_no_return"]["wait"] == 1
    assert s["censored"]["n"] == 2 and s["censored"]["wait"] == 1
    assert "no_reentry" not in s


@pytest.mark.parametrize("horizon", [0, -1, float("nan"), float("inf")])
def test_invalid_horizon_is_rejected_before_reading_sources(horizon):
    with pytest.raises(ValueError, match="finite and positive"):
        evaluation.points(Path("missing"), horizon)


@pytest.fixture(scope="module")
def recorded():
    rows = [r for b in evaluation.BUNDLES for r in evaluation.points(evaluation.REPO / b, 60)]
    return rows, json.loads(FIXTURE.read_text())


def test_actual_saved_answers_keep_primary_horizon_and_censoring(recorded):
    rows, saved = copy.deepcopy(recorded)
    assert evaluation.replay_answers(rows, saved, 60) == ["deepseek_v1", "deepseek_v2"]
    assert len(rows) == 133 and sum(r["horizon_truncated"] for r in rows) == 132
    assert sum(evaluation.truth_classification(r) == "observed_return" for r in rows) == 56
    assert sum(evaluation.truth_classification(r) == "fully_observed_no_return" for r in rows) == 0
    censored = [r for r in rows if evaluation.truth_classification(r) == "censored"]
    assert len(censored) == 77
    assert min(r["observed_future_s"] for r in censored) == pytest.approx(4, abs=1)
    assert max(r["observed_future_s"] for r in censored) == pytest.approx(27, abs=1)
    s = evaluation.summarize_arms(rows, ["deepseek_v1", "deepseek_v2"])
    assert [s[a]["reentry_visible"]["wait"] for a in s] == [32, 32]
    assert [s[a]["reentry_not_visible"]["wait"] for a in s] == [22, 2]
    assert [s[a]["censored"]["wait"] for a in s] == [76, 0]
    assert all(s[a]["fully_observed_no_return"]["n"] == 0 for a in s)
    # Free-form provider text and identifiers never enter the public answer fixture.
    assert all(
        set(r[a]) == {"status", "action", "wait_seconds"}
        for r in saved["rows"]
        for a in ("deepseek_v1", "deepseek_v2")
    )


@pytest.mark.parametrize(
    "change", ["duplicate", "missing", "context", "horizon", "action", "bound"]
)
def test_replay_refuses_unbound_or_invalid_saved_answers(recorded, change):
    rows, saved = copy.deepcopy(recorded)
    if change == "duplicate":
        saved["rows"][1] = saved["rows"][0]
    elif change == "missing":
        saved["rows"].pop()
    elif change == "context":
        saved["rows"][0]["context_sha256"] = "0" * 64
    elif change == "horizon":
        saved["horizon_s"] = 20
    elif change == "action":
        saved["rows"][0]["deepseek_v1"]["action"] = "dispatch"
    else:
        saved["rows"][0]["deepseek_v1"]["wait_seconds"] = 29
    with pytest.raises(ValueError):
        evaluation.replay_answers(rows, saved, 60)


def test_offline_entrypoint_replays_without_provider(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(evaluation.REPO / "scripts/evaluate_yokohama_pad_judge.py"),
            "--replay",
            str(FIXTURE),
            "--output",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.returncode == 0
    output = json.loads((tmp_path / "judge-ab.json").read_text())
    assert output["horizon_s"] == 60 and output["external_api_calls"] == 0
    assert output["answer_source"] == "saved"
    assert output["truth_counts"] == dict(
        observed_return=56, fully_observed_no_return=0, censored=77
    )
    assert all(
        "rationale" not in r[a] for r in output["rows"] for a in ("deepseek_v1", "deepseek_v2")
    )

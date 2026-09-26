from copy import deepcopy

import pytest

from src.runtime import ship_onboard_comparison as comparison


def rows():
    return [
        {
            "run_id": f"{case}-{policy}",
            "case": case,
            "policy": policy,
            "world_sha256": "world",
            "sources_sha256": {"worker": "same"},
            "image_id": "image",
            "scenario_parameters": {"wind_mps": 4},
            "action": "wait" if case == "short_clear" else "detour",
            "urban_elapsed_s": 60 if policy == "onboard_stopping" else 70,
            "inference_elapsed_s": 0 if policy == "onboard_stopping" else 10,
            "matched_image_rule_replay": {
                "onboard_stopping": {"action": "wait" if case == "short_clear" else "detour"}
            },
        }
        for case in ("short_clear", "long_block", "brake_stop")
        for policy in ("onboard_stopping", "onboard_vlm_forecast")
    ]


def compare(monkeypatch, records):
    monkeypatch.setattr(comparison, "reverify_onboard_run", lambda index: deepcopy(records[index]))
    return comparison.compare_onboard_runs(range(len(records)))


def test_successful_local_vlm_comparison_does_not_claim_native_vla_wam(monkeypatch):
    result = compare(monkeypatch, rows())
    assert result["mean_model_minus_rule_s"] == 10
    assert result["step2_native_vla_wam_completed"] is False
    assert not any(p["model_differs_from_rule_on_same_images"] for p in result["pairs"])


@pytest.mark.parametrize(
    "key", ["world_sha256", "sources_sha256", "image_id", "scenario_parameters"]
)
def test_unmatched_flights_cannot_be_pooled(monkeypatch, key):
    records = rows()
    records[-1][key] = "different"
    with pytest.raises(ValueError, match="condition differs"):
        compare(monkeypatch, records)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "reused_run"])
def test_all_predeclared_pairs_are_required(monkeypatch, fault):
    records = rows()
    if fault == "missing":
        records.pop()
    elif fault == "duplicate":
        records[-1] = deepcopy(records[0])
    else:
        records[-1]["run_id"] = records[0]["run_id"]
    with pytest.raises(ValueError, match="Exactly one"):
        compare(monkeypatch, records)

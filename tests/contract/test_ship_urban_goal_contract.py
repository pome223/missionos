"""Pure CPU geometry/forecast contracts; no processes, models or vehicles."""

import copy
import math

import pytest

from src.runtime.ship_urban_goal_contract import (
    CONTEXT_SCHEMA,
    FORECAST_SCHEMA,
    context_digest,
    digest,
    segment_clearance,
    validate_goal_forecast,
    validate_goal_observation,
)

LOWER = [1010, -20, -31]
UPPER = [1200, 20, -29]


def observation():
    return {
        "run_id": "goal-fixture", "sequence": 1, "observed_at_s": 10.0,
        "image_observed_at_s": 10.0, "image_sha256": "a" * 64,
        "position_valid": True, "position_ned_m": [1019, 0, -30],
        "velocity_ned_mps": [0, 0, 0], "phase": "urban", "ap_mode": "hold",
        "battery_fraction": 0.8,
        "goal_context": {
            "schema_version": CONTEXT_SCHEMA, "observed_at_s": 10.0,
            "coverage": {"lower_ned_m": LOWER.copy(), "upper_ned_m": UPPER.copy()},
            "obstacles": [{"id": "observed-box", "center_ned_m": [1021, -0.25, -30],
                           "half_size_m": [0.15, 0.30, 0.5]}],
        },
    }


def validate(row):
    return validate_goal_observation(
        row, now_s=10.1, max_age_s=2, lower_ned_m=LOWER, upper_ned_m=UPPER
    )


def responses(row):
    vla = {
        "fixture": True, "dispatch_allowed": False,
        "candidates": [
            {"id": "straight", "target_ned_m": [1023, 0, -30]},
            {"id": "right", "target_ned_m": [1023, 1, -30]},
        ],
    }
    wam = {
        "fixture": True, "dispatch_allowed": False, "forecast_schema": FORECAST_SCHEMA,
        "proposal_sha256": digest(vla), "observation_context_sha256": context_digest(row),
        "selected_candidate_id": "right",
        "forecasts": [
            {"candidate_id": c["id"], "candidate_sha256": digest(c),
             "predicted_clear": c["id"] == "right"}
            for c in vla["candidates"]
        ],
    }
    return vla, wam


def test_observed_obstacle_changes_clearance_without_changing_goal_or_corridor():
    row = observation()
    context = validate(row)
    start = row["position_ned_m"]
    assert segment_clearance(start, [1023, 0, -30], context, 0.25) == {
        "allowed": False, "blocking_obstacle_ids": ["observed-box"]
    }
    assert segment_clearance(start, [1023, -1, -30], context, 0.25)["allowed"] is False
    assert segment_clearance(start, [1023, 1, -30], context, 0.25)["allowed"] is True
    context["obstacles"][0]["center_ned_m"][1] = 0.25
    assert segment_clearance(start, [1023, 1, -30], context, 0.25)["allowed"] is False
    assert segment_clearance(start, [1023, -1, -30], context, 0.25)["allowed"] is True


@pytest.mark.parametrize("field", ["future_obstacles", "scenario", "expected_choice"])
def test_hidden_future_or_scenario_fields_are_not_observations(field):
    row = observation()
    row[field] = "right"
    with pytest.raises(ValueError, match="invalid_fields:observation"):
        validate(row)


@pytest.mark.parametrize("where", ["context", "coverage", "obstacle"])
def test_unknown_nested_context_fields_are_rejected(where):
    row = observation()
    target = {"context": row["goal_context"], "coverage": row["goal_context"]["coverage"],
              "obstacle": row["goal_context"]["obstacles"][0]}[where]
    target["next_obstacle"] = [1025, 1, -30]
    with pytest.raises(ValueError, match="invalid_fields"):
        validate(row)


@pytest.mark.parametrize("stamp", [7.9, 10.05, 11, math.nan, math.inf, True])
def test_context_must_be_fresh_and_not_newer_than_sensor_observation(stamp):
    row = observation()
    row["goal_context"]["observed_at_s"] = stamp
    with pytest.raises(ValueError):
        validate(row)


def test_coverage_must_include_entire_approved_corridor_and_swept_clearance():
    row = observation()
    row["goal_context"]["coverage"]["upper_ned_m"][0] = 1199
    with pytest.raises(ValueError, match="incomplete_corridor_coverage"):
        validate(row)
    context = observation()["goal_context"]
    with pytest.raises(ValueError, match="outside_observed_coverage"):
        segment_clearance([1010, 0, -30], [1014, 0, -30], context, 0.25)


@pytest.mark.parametrize("change", ["duplicate", "too_many", "long_id", "zero_size",
                                   "negative_size", "nonfinite", "bool_size", "outside"])
def test_unbounded_or_ambiguous_obstacle_geometry_is_rejected(change):
    row = observation()
    boxes = row["goal_context"]["obstacles"]
    if change == "duplicate":
        boxes.append(copy.deepcopy(boxes[0]))
    elif change == "too_many":
        boxes[:] = [dict(boxes[0], id=f"box-{i}") for i in range(33)]
    elif change == "long_id":
        boxes[0]["id"] = "x" * 65
    elif change == "outside":
        boxes[0]["center_ned_m"][2] = -31
    else:
        boxes[0]["half_size_m"][0] = {
            "zero_size": 0, "negative_size": -1, "nonfinite": math.nan, "bool_size": True,
        }[change]
    with pytest.raises(ValueError):
        validate(row)


def test_context_binding_ignores_refresh_time_and_order_but_not_obstacle_or_coverage_changes():
    row = observation()
    row["goal_context"]["obstacles"].append({
        "id": "second", "center_ned_m": [1040, 0, -30], "half_size_m": [1, 1, 0.5],
    })
    original = context_digest(row)
    fresh = copy.deepcopy(row)
    fresh["goal_context"]["observed_at_s"] = 10.1
    fresh["goal_context"]["obstacles"].reverse()
    assert context_digest(fresh) == original
    fresh["goal_context"]["obstacles"][0]["center_ned_m"][0] += 0.1
    assert context_digest(fresh) != original
    fresh = copy.deepcopy(row)
    fresh["goal_context"]["coverage"]["upper_ned_m"][0] += 1
    assert context_digest(fresh) != original


def test_clearance_checks_segment_interior_and_blocks_tangency():
    context = observation()["goal_context"]
    context["obstacles"] = [
        {"id": "mid-leg", "center_ned_m": [1021, 0, -30], "half_size_m": [0.25, 0.25, 0.5]}
    ]
    assert segment_clearance([1019, 0.5, -30], [1023, 0.5, -30], context, 0.25)["allowed"] is False
    assert segment_clearance([1019, 0.5001, -30], [1023, 0.5001, -30], context, 0.25)["allowed"] is True


def test_forecast_binds_each_candidate_and_context_but_is_not_rules_authority():
    row = observation()
    vla, wam = responses(row)
    assert len(validate_goal_forecast(vla, wam, row)) == 2
    # Even a syntactically valid forecast claiming the blocked leg is clear is
    # insufficient: independent Rules still rejects its observed swept segment.
    wam["selected_candidate_id"] = "straight"
    wam["forecasts"][0]["predicted_clear"] = True
    validate_goal_forecast(vla, wam, row)
    assert segment_clearance(row["position_ned_m"], [1023, 0, -30], row, 0.25)["allowed"] is False


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown", "wrong_candidate_hash",
                                   "wrong_proposal", "changed_context", "numeric_clear",
                                   "selected_blocked", "no_selected", "native", "extra_field"])
def test_missing_malformed_or_unbound_forecast_never_passes(change):
    row = observation()
    vla, wam = responses(row)
    if change == "missing":
        wam["forecasts"].pop()
    elif change == "duplicate":
        wam["forecasts"][1] = copy.deepcopy(wam["forecasts"][0])
    elif change == "unknown":
        wam["forecasts"][1]["candidate_id"] = "invented"
    elif change == "wrong_candidate_hash":
        wam["forecasts"][1]["candidate_sha256"] = "0" * 64
    elif change == "wrong_proposal":
        wam["proposal_sha256"] = "0" * 64
    elif change == "changed_context":
        row["goal_context"]["obstacles"][0]["center_ned_m"][1] = 0.25
    elif change == "numeric_clear":
        wam["forecasts"][1]["predicted_clear"] = 1
    elif change == "selected_blocked":
        wam["selected_candidate_id"] = "straight"
    elif change == "no_selected":
        wam["selected_candidate_id"] = None
    elif change == "native":
        wam["fixture"] = False
    else:
        wam["forecasts"][0]["future_truth"] = True
    with pytest.raises(ValueError):
        validate_goal_forecast(vla, wam, row)

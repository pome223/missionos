"""Goal similarity may be evidence without being a calibrated safety forecast."""

from copy import deepcopy

import pytest

from missionos_core.prediction import (
    GOAL_COMPATIBILITY_METRICS,
    GOAL_COMPATIBILITY_SCHEMA,
    OptionForecast,
    PredictionBinding,
    PredictionOption,
    PredictionRegistry,
    PredictionRequest,
    validate_forecast_metrics,
)
from src.intelligence.mission_assurance_agent import MissionSituation
from src.intelligence.prediction_evidence import (
    AUTHORITY_FLAGS,
    capture_prediction_evidence,
    receive_prediction_evidence,
)

BINDING = PredictionBinding(
    "aerial-fixture", "a" * 64, "navigate.v1", "b" * 64, "fixture.v1", "aerial.v1"
)
REQUEST = PredictionRequest(
    "request1", "observation1", 100.0, BINDING, {}, (PredictionOption("move", 2.0, {}),)
)


def goal_state(metric_id="uanwm_hep_residual", cost=1.25):
    return {
        "goal_compatibility": {
            "schema_version": GOAL_COMPATIBILITY_SCHEMA,
            "metric_id": metric_id,
            "raw_cost": cost,
            "lower_is_better": True,
            "risk_assessed": False,
        }
    }


def forecast(risk=None, state=None):
    class Predictor:
        binding = BINDING

        def predict(self, request):
            return (OptionForecast("move", 2.0, risk, state),)

    registry = PredictionRegistry()
    registry.register(Predictor())
    return registry.forecast(REQUEST, now=101)


def evidence_fixture():
    envelope = capture_prediction_evidence(
        REQUEST,
        forecast(state=goal_state()),
        execution_id="aerial-run1",
        state_revision="revision1",
        source_ref="fixture:camera",
    )
    situation = MissionSituation(
        situation_id="situation1",
        observed_at="2026-09-22T00:00:00Z",
        mission_contract={"prediction_contract": BINDING.mission_contract},
        progress={},
        observations={"camera_ref": "fixture:camera"},
        constraints={"prediction_context": deepcopy(envelope["context"])},
        uncertainty={},
        source_refs=("fixture:camera",),
        source_schema_version="fixture.v1",
        input_digest="original",
        execution_scope="fixture",
    )
    return situation, envelope


@pytest.mark.parametrize("metric_id", sorted(GOAL_COMPATIBILITY_METRICS))
@pytest.mark.parametrize("cost", [0, 0.25, 3.0])
def test_goal_cost_is_admitted_without_inventing_a_risk_score(metric_id, cost):
    state = goal_state(metric_id, cost)
    result = forecast(state=state)
    assert result["status"] == "available"
    assert result["forecasts"][0]["risk_score"] is None
    assert result["forecasts"][0]["future_state"] == state
    assert all(result[key] is False for key in AUTHORITY_FLAGS)


@pytest.mark.parametrize("risk", [0, 0.4, 1])
def test_numeric_risk_forecasts_still_require_no_goal_metric(risk):
    result = forecast(risk, {"battery_remaining": 0.6})
    assert result["status"] == "available"
    assert result["forecasts"][0]["risk_score"] == risk
    situation, envelope = evidence_fixture()
    envelope["forecast"] = result
    _, receipt = receive_prediction_evidence(situation, envelope, now=101, max_age_seconds=3)
    assert receipt["status"] == "adopted"


@pytest.mark.parametrize("risk", [True, False, -0.1, 1.1, float("nan"), float("inf"), "0.2"])
def test_invalid_numeric_risk_is_rejected_by_both_boundaries(risk):
    assert forecast(risk, {})["status"] == "unavailable"
    situation, envelope = evidence_fixture()
    envelope["forecast"]["forecasts"][0]["risk_score"] = risk
    _, receipt = receive_prediction_evidence(situation, envelope, now=101, max_age_seconds=3)
    assert receipt["status"] == "rejected"


INVALID_METRIC_FIELDS = [
    ("schema_version", "unknown.v1"),
    ("metric_id", "unknown_goal_metric"),
    ("metric_id", []),
    ("raw_cost", -0.01),
    ("raw_cost", float("nan")),
    ("raw_cost", float("inf")),
    ("raw_cost", float("-inf")),
    ("raw_cost", True),
    ("raw_cost", False),
    ("raw_cost", "0.1"),
    ("raw_cost", None),
    ("lower_is_better", False),
    ("lower_is_better", 1),
    ("risk_assessed", True),
    ("risk_assessed", 0),
    ("risk_assessed", None),
    ("collision_probability", 0.0),
]


@pytest.mark.parametrize("key,value", INVALID_METRIC_FIELDS)
def test_malformed_null_risk_metric_rejected_at_core_and_evidence_boundaries(key, value):
    state = goal_state()
    state["goal_compatibility"][key] = value
    with pytest.raises(ValueError, match="invalid_goal_compatibility"):
        validate_forecast_metrics(None, state)
    assert forecast(state=state)["status"] == "unavailable"
    situation, envelope = evidence_fixture()
    previously_adopted, _ = receive_prediction_evidence(
        situation, envelope, now=101, max_age_seconds=3
    )
    envelope["forecast"]["forecasts"][0]["future_state"] = state
    rejected, receipt = receive_prediction_evidence(
        previously_adopted, envelope, now=101, max_age_seconds=3
    )
    assert receipt["status"] == "rejected"
    assert "forecasts" not in rejected.uncertainty["prediction_evidence"]


@pytest.mark.parametrize("key", sorted(goal_state()["goal_compatibility"]))
def test_missing_metric_field_rejected_at_both_boundaries(key):
    state = goal_state()
    del state["goal_compatibility"][key]
    assert forecast(state=state)["status"] == "unavailable"
    situation, envelope = evidence_fixture()
    envelope["forecast"]["forecasts"][0]["future_state"] = state
    _, receipt = receive_prediction_evidence(situation, envelope, now=101, max_age_seconds=3)
    assert receipt["status"] == "rejected"


@pytest.mark.parametrize("state", [None, {}, [], "unknown", {"goal_compatibility": None}])
def test_null_risk_without_typed_goal_metric_rejected_at_both_boundaries(state):
    assert forecast(state=state)["status"] == "unavailable"
    situation, envelope = evidence_fixture()
    envelope["forecast"]["forecasts"][0]["future_state"] = state
    _, receipt = receive_prediction_evidence(situation, envelope, now=101, max_age_seconds=3)
    assert receipt["status"] == "rejected"


def test_goal_evidence_remains_uncertainty_without_feasibility_or_authority():
    situation, envelope = evidence_fixture()
    updated, receipt = receive_prediction_evidence(situation, envelope, now=101, max_age_seconds=3)
    assert receipt["status"] == "adopted"
    admitted = updated.uncertainty["prediction_evidence"]["forecasts"][0]
    assert admitted["risk_score"] is None
    assert admitted["future_state"]["goal_compatibility"]["risk_assessed"] is False
    assert updated.observations == situation.observations
    assert receipt["feasibility_established"] is False
    assert receipt["llm_judgment_invoked"] is False
    assert all(receipt[key] is False for key in AUTHORITY_FLAGS)
    assert "selected_option" not in receipt

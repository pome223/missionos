"""Mission-neutral prediction admission; synthetic outcomes are not physical truth."""

from dataclasses import replace
from copy import deepcopy

import pytest

from missionos_core.prediction import (
    OptionForecast,
    PredictionBinding,
    PredictionOption,
    PredictionRegistry,
    PredictionRequest,
)
from src.intelligence.mission_assurance_agent import MissionSituation
from src.intelligence.prediction_evidence import (
    capture_prediction_evidence,
    receive_prediction_evidence,
)


class Predictor:
    binding = PredictionBinding(
        "battery-model", "a" * 64, "delivery.v1", "b" * 64, "fixture.v1", "battery.v1"
    )

    def predict(self, request):
        return (OptionForecast("wait", 2.0, 0.2, {"battery_remaining": 0.6}),)


def fixture(now=100.0):
    request = PredictionRequest(
        "req1",
        "obs1",
        now,
        Predictor.binding,
        {"battery": 0.7},
        (PredictionOption("wait", 2.0, {}),),
    )
    registry = PredictionRegistry()
    registry.register(Predictor())
    envelope = capture_prediction_evidence(
        request,
        registry.forecast(request, now=now),
        execution_id="mission-run1",
        state_revision="revision1",
        source_ref="fixture:battery-sensor",
    )
    situation = MissionSituation(
        situation_id="situation1",
        observed_at="2026-09-19T00:00:00Z",
        mission_contract={"prediction_contract": "delivery.v1"},
        progress={},
        observations={"battery": 0.7},
        constraints={"prediction_context": deepcopy(envelope["context"])},
        uncertainty={},
        source_refs=("fixture:battery-sensor",),
        source_schema_version="fixture.v1",
        input_digest="original",
        execution_scope="fixture",
    )
    return situation, envelope


def test_admission_is_evidence_not_fact_or_authority():
    situation, envelope = fixture()
    updated, receipt = receive_prediction_evidence(situation, envelope, now=101, max_age_seconds=3)
    assert receipt["status"] == "adopted"
    assert updated.observations == situation.observations
    assert updated.input_digest != situation.input_digest
    assert situation.uncertainty == {}
    assert updated.uncertainty["prediction_evidence"]["forecasts"][0]["future_state"] == {
        "battery_remaining": 0.6
    }
    for key in (
        "approval_recorded",
        "dispatch_authority_created",
        "completion_claimed",
        "physical_execution_invoked",
        "feasibility_established",
        "llm_judgment_invoked",
    ):
        assert receipt[key] is False
    envelope["forecast"]["forecasts"][0]["risk_score"] = 1
    assert updated.uncertainty["prediction_evidence"]["forecasts"][0]["risk_score"] == 0.2


@pytest.mark.parametrize(
    "key", ["execution_id", "state_revision", "source_ref", "observation_id", "binding"]
)
def test_current_context_change_invalidates_forecast(key):
    situation, envelope = fixture()
    situation.constraints["prediction_context"][key] = "changed"
    _, receipt = receive_prediction_evidence(situation, envelope, now=101, max_age_seconds=3)
    assert receipt["status"] == "rejected"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda e: e["forecast"].update(status="unavailable", forecasts=[]),
        lambda e: e["forecast"].update(request_sha256="wrong"),
        lambda e: e["forecast"].update(verification_basis="observed"),
        lambda e: e["forecast"].update(approval_recorded=True),
        lambda e: e["forecast"]["binding"].update(policy_sha256="wrong"),
        lambda e: e["forecast"]["forecasts"][0].update(horizon_seconds=3),
        lambda e: e["forecast"]["forecasts"][0].update(risk_score=float("nan")),
        lambda e: e["forecast"]["forecasts"].append(e["forecast"]["forecasts"][0]),
        lambda e: e["forecast"]["forecasts"][0].update(future_state="invalid"),
        lambda e: e["request"].update(observed_at=99),
        lambda e: e.update(schema_version="unknown"),
    ],
)
def test_invalid_evidence_replaces_prior_admission_without_forecasts(mutation):
    situation, envelope = fixture()
    updated, _ = receive_prediction_evidence(situation, envelope, now=101, max_age_seconds=3)
    mutation(envelope)
    rejected, receipt = receive_prediction_evidence(updated, envelope, now=101, max_age_seconds=3)
    assert receipt["status"] == "rejected"
    assert "forecasts" not in rejected.uncertainty["prediction_evidence"]


@pytest.mark.parametrize("now", [99, 104])
def test_old_or_future_observation_rejected(now):
    situation, envelope = fixture()
    _, receipt = receive_prediction_evidence(situation, envelope, now=now, max_age_seconds=3)
    assert receipt["reason"] == "stale_or_future_observation"


def test_mission_and_missing_context_rejected():
    situation, envelope = fixture()
    for changed in (replace(situation, mission_contract={}), replace(situation, constraints={})):
        _, receipt = receive_prediction_evidence(changed, envelope, now=101, max_age_seconds=3)
        assert receipt["status"] == "rejected"


@pytest.mark.parametrize("risk", [0.0, 1.0])
def test_admission_does_not_choose_response_from_risk(risk):
    situation, envelope = fixture()
    envelope["forecast"]["forecasts"][0]["risk_score"] = risk
    _, receipt = receive_prediction_evidence(situation, envelope, now=101, max_age_seconds=3)
    assert receipt["status"] == "adopted"
    assert "selected_option" not in receipt
    assert "proposed_response_kind" not in receipt


@pytest.mark.parametrize("age", [0, -1, float("nan"), float("inf"), True])
def test_invalid_age_policy_is_caller_error(age):
    situation, envelope = fixture()
    with pytest.raises(ValueError, match="invalid_freshness_policy"):
        receive_prediction_evidence(situation, envelope, now=101, max_age_seconds=age)


@pytest.mark.parametrize('change', ['none', 'stale', 'revision', 'missing', 'changed'])
def test_dispatch_revalidates_original_prediction_against_current_owner(change):
    from src.intelligence.prediction_evidence import revalidate_incident_prediction
    situation, envelope = fixture()
    updated, receipt = receive_prediction_evidence(situation, envelope, now=101, max_age_seconds=30)
    graph = {'prediction_admission':receipt, 'mission_situation':updated.to_dict()}
    context = deepcopy(envelope['context'])
    now = 140 if change == 'stale' else 102
    if change == 'revision': context['state_revision'] = 'revision2'
    if change == 'missing': envelope = None
    if change == 'changed': envelope['forecast']['forecasts'][0]['risk_score'] = .1
    check = revalidate_incident_prediction(graph, envelope=envelope, current_context=context, now=now)
    assert check['status'] == ('adopted' if change == 'none' else 'rejected')
    assert check['dispatch_authority_created'] is False

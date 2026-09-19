from dataclasses import replace
import time

import pytest

from missionos_core.prediction import (
    OptionForecast,
    PredictionBinding,
    PredictionOption,
    PredictionRegistry,
    PredictionRequest,
    bind_prediction_observation,
)

BINDING = PredictionBinding("fixture", "a" * 64, "mission", "b" * 64, "environment", "state.v1")


class FixturePredictor:
    binding = BINDING

    def predict(self, request):
        return tuple(
            OptionForecast(o.option_id, o.horizon_seconds, 0.8, {"height": 2})
            for o in request.options
        )


def request():
    return PredictionRequest(
        "request1",
        "observation1",
        time.time(),
        BINDING,
        {"height": 3},
        (PredictionOption("move", 2.0, {}),),
    )


def registry():
    result = PredictionRegistry()
    result.register(FixturePredictor())
    return result


def test_forecast_does_not_grant_authority():
    result = registry().forecast(request())
    assert result["status"] == "available"
    assert result["verification_basis"] == "model_inferred"
    assert not result["dispatch_authority_created"]
    assert not result["approval_recorded"]
    assert not result["completion_claimed"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_sha256", "c" * 64),
        ("policy_sha256", "c" * 64),
        ("mission_contract", "different"),
        ("environment_contract", "other"),
        ("input_schema", "other"),
    ],
)
def test_cross_contract_use_rejected(field, value):
    req = request()
    req = replace(req, binding=replace(req.binding, **{field: value}))
    assert registry().forecast(req)["reason"] == "binding_mismatch"


def test_stale_missing_and_duplicate_options():
    req = request()
    assert (
        registry().forecast(replace(req, observed_at=req.observed_at - 100))["status"]
        == "unavailable"
    )
    assert PredictionRegistry().forecast(req)["reason"] == "model_not_registered"
    assert registry().forecast(replace(req, options=req.options * 2))["reason"] == "invalid_options"


def test_backend_cannot_rewrite_input():
    class Mutating(FixturePredictor):
        def predict(self, req):
            req.state["height"] = -1
            return super().predict(req)

    reg = PredictionRegistry()
    reg.register(Mutating())
    req = request()
    assert reg.forecast(req)["status"] == "unavailable"
    assert req.state["height"] == 3


def test_outcome_must_match_request_option_and_horizon():
    req = request()
    result = registry().forecast(req)
    args = dict(
        request_sha256=req.digest(),
        observation_id=req.observation_id,
        option_id="move",
        horizon_seconds=2.0,
        outcome_ref="sim:1",
        source_kind="simulator_observation",
    )
    receipt = bind_prediction_observation(result, **args)
    assert receipt["status"] == "bound"
    assert receipt["request_sha256"] == req.digest()
    assert "classification" not in receipt
    assert "observed_collapse" not in receipt
    assert not receipt["completion_claimed"]
    for k, v in [
        ("request_sha256", "bad"),
        ("observation_id", "other"),
        ("option_id", "other"),
        ("horizon_seconds", 1.0),
        ("source_kind", "prediction"),
    ]:
        assert bind_prediction_observation(result, **(args | {k: v}))["status"] == "incomparable"

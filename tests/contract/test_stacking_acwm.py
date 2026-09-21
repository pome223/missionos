import time
from dataclasses import replace

import numpy as np
import pytest
from missionos_core.prediction import PredictionOption, PredictionRegistry, PredictionRequest
from src.prediction.stacking import MACRO
from src.prediction.stacking_acwm import MACRO_ID, StackingACWMPredictor


def example():
    objects = np.zeros((10, 7)); objects[:, 3] = 1
    physics = np.ones((10, 11))
    return {"image": np.zeros((240, 240, 3), dtype=np.uint8).tolist(),
            "objects": objects.tolist(), "velocity": np.zeros((10, 6)).tolist(),
            "physics": physics.tolist(), "robot": np.zeros(12).tolist(), "count": 1,
            "plan": [0, 0, 0.84, *MACRO]}


def setup(override=None):
    calls = []
    def backend(state):
        calls.append(state)
        return {"model_sha256": "a" * 64, "readout_sha256": "b" * 64,
                "horizon_seconds": 14.2, "macro_id": MACRO_ID,
                "readout_output": 0.6, "invocation_id": "test", **(override or {})}
    provider = StackingACWMPredictor(backend, model_sha256="a" * 64,
                                   readout_sha256="b" * 64, policy_sha256="c" * 64)
    registry = PredictionRegistry(); registry.register(provider)
    request = PredictionRequest("request", "current-observation", time.time(), provider.binding,
                                example(), (PredictionOption("continue", 14.2,
                                {"macro": MACRO_ID, "readout_sha256": "b" * 64}),))
    return registry, request, calls


def test_forecast_is_evidence_only():
    registry, request, calls = setup()
    result = registry.forecast(request)
    assert result["status"] == "available" and len(calls) == 1
    assert result["forecasts"][0]["risk_score"] == 0.6
    assert result["verification_basis"] == "model_inferred"
    assert all(result[k] is False for k in ["approval_recorded", "dispatch_authority_created",
                                           "physical_execution_invoked", "completion_claimed"])


@pytest.mark.parametrize("key", ["future_actions", "actions", "future_frames", "collapsed"])
def test_future_fields_rejected_before_backend(key):
    registry, request, calls = setup(); request.state[key] = []
    assert registry.forecast(request)["status"] == "unavailable" and not calls


@pytest.mark.parametrize("change", [{"model_sha256": "d" * 64}, {"readout_sha256": "d" * 64},
                                    {"horizon_seconds": 28.4}, {"readout_output": float('nan')}])
def test_backend_mismatch_fails_closed(change):
    registry, request, _ = setup(change)
    assert registry.forecast(request)["status"] == "unavailable"


def test_no_synthetic_bank_forecast():
    registry, request, calls = setup()
    request = replace(request, options=(*request.options, PredictionOption("bank", 14.2, {})))
    assert registry.forecast(request)["status"] == "unavailable" and not calls


def test_stale_request_never_reaches_backend():
    registry, request, calls = setup()
    assert registry.forecast(replace(request, observed_at=0))["status"] == "unavailable"
    assert not calls

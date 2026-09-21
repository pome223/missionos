import time
from dataclasses import replace

import numpy as np
import pytest
from missionos_core.prediction import (
    PredictionOption,
    PredictionRegistry,
    PredictionRequest,
)
from src.prediction.stacking import MACRO
from src.prediction.stacking_acwm import MACRO_ID
from src.prediction.stacking_acwm_options import StackingACWMOptionsPredictor


def example():
    objects = np.zeros((10, 7))
    objects[:, 3] = 1
    physics = np.ones((10, 11))
    return {
        "image": np.zeros((240, 240, 3), dtype=np.uint8).tolist(),
        "objects": objects.tolist(),
        "velocity": np.zeros((10, 6)).tolist(),
        "physics": physics.tolist(),
        "robot": np.zeros(12).tolist(),
        "count": 1,
        "plan": [0, 0, 0.84, *MACRO],
    }


def setup(override=None):
    calls = []

    def backend(state, option):
        calls.append((state, option))
        return {
            "model_sha256": "a" * 64,
            "readout_sha256": "b" * 64,
            "horizon_seconds": 28.4 if option == "continue" else 14.2,
            "macro_id": MACRO_ID,
            "option_id": option,
            "readout_output": 0.6,
            "invocation_id": "test",
            **(override or {}),
        }

    provider = StackingACWMOptionsPredictor(
        backend, model_sha256="a" * 64, readout_sha256="b" * 64, policy_sha256="c" * 64
    )
    registry = PredictionRegistry()
    registry.register(provider)
    request = PredictionRequest(
        "request",
        "current-observation",
        time.time(),
        provider.binding,
        example(),
        tuple(
            PredictionOption(
                option, horizon, {"macro": MACRO_ID, "readout_sha256": "b" * 64}
            )
            for option, horizon in [("continue", 28.4), ("bank", 14.2)]
        ),
    )
    return registry, request, calls


def test_forecast_is_evidence_only():
    registry, request, calls = setup()
    result = registry.forecast(request)
    assert result["status"] == "available" and len(calls) == 2
    assert result["forecasts"][0]["risk_score"] == 0.6
    assert result["verification_basis"] == "model_inferred"
    assert all(
        result[k] is False
        for k in [
            "approval_recorded",
            "dispatch_authority_created",
            "physical_execution_invoked",
            "completion_claimed",
        ]
    )


@pytest.mark.parametrize(
    "key", ["future_actions", "actions", "future_frames", "collapsed"]
)
def test_future_fields_rejected_before_backend(key):
    registry, request, calls = setup()
    request.state[key] = []
    assert registry.forecast(request)["status"] == "unavailable" and not calls


@pytest.mark.parametrize(
    "change",
    [
        {"model_sha256": "d" * 64},
        {"readout_sha256": "d" * 64},
        {"horizon_seconds": 1.8},
        {"readout_output": float("nan")},
    ],
)
def test_backend_mismatch_fails_closed(change):
    registry, request, _ = setup(change)
    assert registry.forecast(request)["status"] == "unavailable"


def test_requires_both_options():
    registry, request, calls = setup()
    request = replace(request, options=request.options[:1])
    assert registry.forecast(request)["status"] == "unavailable" and not calls


def test_stale_request_never_reaches_backend():
    registry, request, calls = setup()
    assert registry.forecast(replace(request, observed_at=0))["status"] == "unavailable"
    assert not calls


def test_second_option_failure_does_not_return_partial_forecast():
    registry, request, calls = setup({"option_id": "continue"})
    result = registry.forecast(request)
    assert len(calls) == 2
    assert result["status"] == "unavailable"
    assert not result.get("forecasts")


def test_both_backend_calls_receive_same_current_state_with_isolated_arrays():
    registry, request, calls = setup()
    assert registry.forecast(request)["status"] == "available"
    a, b = calls[0][0], calls[1][0]
    for key in a:
        np.testing.assert_array_equal(a[key], b[key])
        assert not np.shares_memory(a[key], b[key])

"""Mission-scoped forecasts. Prediction never grants execution authority."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

PREDICTION_SCHEMA = "missionos_core_prediction.v1"
GOAL_COMPATIBILITY_SCHEMA = "missionos_goal_compatibility.v1"
GOAL_COMPATIBILITY_METRICS = frozenset({"uanwm_hep_residual", "anwm_goal_image_mse"})


def validate_forecast_metrics(risk_score: Any, future_state: Any) -> None:
    """Validate risk or an explicitly uncalibrated goal-compatibility forecast.

    Goal costs rank candidate outcomes; they do not estimate collision risk,
    establish feasibility, or prove that a learned model executed. Keeping risk
    null preserves that distinction instead of coercing similarity into safety.
    """
    if not isinstance(future_state, dict):
        raise ValueError("invalid_future_state")
    if risk_score is not None:
        if type(risk_score) not in (int, float) or not 0 <= risk_score <= 1:
            raise ValueError("invalid_risk")
        return
    metric = future_state.get("goal_compatibility")
    if not isinstance(metric, dict) or set(metric) != {
        "schema_version",
        "metric_id",
        "raw_cost",
        "lower_is_better",
        "risk_assessed",
    }:
        raise ValueError("invalid_goal_compatibility")
    cost = metric["raw_cost"]
    if (
        metric["schema_version"] != GOAL_COMPATIBILITY_SCHEMA
        or not isinstance(metric["metric_id"], str)
        or metric["metric_id"] not in GOAL_COMPATIBILITY_METRICS
        or type(cost) not in (int, float)
        or not 0 <= cost < math.inf
        or metric["lower_is_better"] is not True
        or metric["risk_assessed"] is not False
    ):
        raise ValueError("invalid_goal_compatibility")


def prediction_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class PredictionBinding:
    model_id: str
    model_sha256: str
    mission_contract: str
    policy_sha256: str
    environment_contract: str
    input_schema: str


@dataclass(frozen=True)
class PredictionOption:
    option_id: str
    horizon_seconds: float
    parameters: dict[str, Any]


@dataclass(frozen=True)
class PredictionRequest:
    request_id: str
    observation_id: str
    observed_at: float
    binding: PredictionBinding
    state: dict[str, Any]
    options: tuple[PredictionOption, ...]

    def digest(self) -> str:
        return prediction_digest(asdict(self))


@dataclass(frozen=True)
class OptionForecast:
    option_id: str
    horizon_seconds: float
    risk_score: float | None
    future_state: dict[str, Any]


class MissionPredictor(Protocol):
    binding: PredictionBinding

    def predict(self, request: PredictionRequest) -> tuple[OptionForecast, ...]: ...


class PredictionRegistry:
    """Dispatch only to a matching mission/policy/environment/input contract."""

    def __init__(self) -> None:
        self._providers: dict[str, MissionPredictor] = {}

    def register(self, provider: MissionPredictor) -> None:
        key = provider.binding.model_id
        if key in self._providers:
            raise ValueError("duplicate prediction model")
        self._providers[key] = provider

    def forecast(
        self, request: PredictionRequest, *, max_age_seconds: float = 60, now: float | None = None
    ) -> dict[str, Any]:
        # Take a JSON snapshot so a mutable backend cannot rewrite the caller's input.
        raw = json.loads(json.dumps(asdict(request), allow_nan=False))
        request = PredictionRequest(
            raw["request_id"],
            raw["observation_id"],
            raw["observed_at"],
            PredictionBinding(**raw["binding"]),
            raw["state"],
            tuple(PredictionOption(**o) for o in raw["options"]),
        )
        digest = request.digest()
        result: dict[str, Any] = {
            "schema_version": PREDICTION_SCHEMA,
            "request_id": request.request_id,
            "observation_id": request.observation_id,
            "request_sha256": digest,
            "binding": asdict(request.binding),
            "status": "unavailable",
            "reason": "",
            "forecasts": [],
            "verification_basis": "model_inferred",
            "approval_recorded": False,
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "completion_claimed": False,
        }
        age = (time.time() if now is None else now) - request.observed_at
        provider = self._providers.get(request.binding.model_id)
        ids = [o.option_id for o in request.options]
        if not request.request_id or not request.observation_id:
            result["reason"] = "missing_identity"
        elif not math.isfinite(age) or not 0 <= age <= max_age_seconds:
            result["reason"] = "stale_or_future_observation"
        elif not ids or len(set(ids)) != len(ids) or any(not i for i in ids):
            result["reason"] = "invalid_options"
        elif any(
            not math.isfinite(o.horizon_seconds) or o.horizon_seconds <= 0 for o in request.options
        ):
            result["reason"] = "invalid_horizon"
        elif provider is None:
            result["reason"] = "model_not_registered"
        elif request.binding != provider.binding:
            result["reason"] = "binding_mismatch"
        else:
            try:
                forecasts = provider.predict(request)
                if request.digest() != digest:
                    raise ValueError("provider_mutated_input")
                if len(forecasts) != len(ids):
                    raise ValueError("incomplete_forecast")
                by_id = {f.option_id: f for f in forecasts}
                if set(by_id) != set(ids):
                    raise ValueError("forecast_option_mismatch")
                for option in request.options:
                    f = by_id[option.option_id]
                    if f.horizon_seconds != option.horizon_seconds:
                        raise ValueError("forecast_horizon_mismatch")
                    validate_forecast_metrics(f.risk_score, f.future_state)
                    prediction_digest(asdict(f))
                result.update(status="available", forecasts=[asdict(f) for f in forecasts])
            except Exception as exc:
                result["reason"] = f"predictor_rejected:{type(exc).__name__}"
        return result


def bind_prediction_observation(
    forecast: dict[str, Any],
    *,
    request_sha256: str,
    observation_id: str,
    option_id: str,
    horizon_seconds: float,
    outcome_ref: str,
    source_kind: str,
) -> dict[str, Any]:
    """Bind an observation reference without interpreting its mission-specific outcome.

    This checks identity and horizon, not source authenticity or outcome correctness.
    """
    result: dict[str, Any] = {
        "schema_version": "missionos_core_prediction_observation_binding.v1",
        "status": "incomparable",
        "completion_claimed": False,
        "physical_execution_invoked": False,
        "outcome_ref": outcome_ref,
    }
    if (
        forecast["status"] != "available"
        or forecast["request_sha256"] != request_sha256
        or forecast["observation_id"] != observation_id
        or not outcome_ref
        or source_kind not in ("simulator_observation", "hardware_observation")
    ):
        return result
    options = [f for f in forecast["forecasts"] if f["option_id"] == option_id]
    if len(options) != 1 or options[0]["horizon_seconds"] != horizon_seconds:
        return result
    result.update(
        status="bound",
        request_sha256=request_sha256,
        observation_id=observation_id,
        option_id=option_id,
        horizon_seconds=horizon_seconds,
        source_kind=source_kind,
    )
    return result

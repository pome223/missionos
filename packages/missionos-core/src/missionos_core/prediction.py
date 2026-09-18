"""Mission-scoped forecasts. Prediction never grants execution authority."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

PREDICTION_SCHEMA = "missionos_core_prediction.v1"


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
    risk_score: float
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
                    if not math.isfinite(f.risk_score) or not 0 <= f.risk_score <= 1:
                        raise ValueError("invalid_risk")
                    prediction_digest(asdict(f))
                result.update(status="available", forecasts=[asdict(f) for f in forecasts])
            except Exception as exc:
                result["reason"] = f"predictor_rejected:{type(exc).__name__}"
        return result


def compare_prediction(
    forecast: dict[str, Any],
    *,
    request_sha256: str,
    observation_id: str,
    option_id: str,
    horizon_seconds: float,
    collapsed: bool,
    threshold: float,
    outcome_ref: str,
    source_kind: str,
) -> dict[str, Any]:
    """Compare a bound observed branch; no mission completion promotion."""
    result: dict[str, Any] = {
        "schema_version": "missionos_core_prediction_comparison.v1",
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
        or type(collapsed) is not bool
        or not 0 <= threshold <= 1
    ):
        return result
    options = [f for f in forecast["forecasts"] if f["option_id"] == option_id]
    if len(options) != 1 or options[0]["horizon_seconds"] != horizon_seconds:
        return result
    danger = options[0]["risk_score"] >= threshold
    result.update(
        status="compared",
        option_id=option_id,
        horizon_seconds=horizon_seconds,
        predicted_danger=danger,
        observed_collapse=collapsed,
        classification=("TP" if collapsed else "FP") if danger else ("FN" if collapsed else "TN"),
        source_kind=source_kind,
    )
    return result

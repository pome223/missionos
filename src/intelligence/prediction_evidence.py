"""Prediction evidence admission for Mission Assurance; no judgment or authority."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, replace
from typing import Any

from missionos_core.prediction import (
    PREDICTION_SCHEMA,
    PredictionBinding,
    PredictionOption,
    PredictionRequest,
    prediction_digest,
)
from src.intelligence.mission_assurance_agent import MissionSituation

ENVELOPE_SCHEMA = "missionos_prediction_evidence.v1"
RECEIPT_SCHEMA = "missionos_assurance_prediction_admission.v1"
CONTEXT_FIELDS = {"execution_id", "state_revision", "source_ref", "observation_id", "binding"}
AUTHORITY_FLAGS = (
    "approval_recorded",
    "dispatch_authority_created",
    "physical_execution_invoked",
    "completion_claimed",
)


def snapshot(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def capture_prediction_evidence(
    request: PredictionRequest,
    forecast: dict,
    *,
    execution_id: str,
    state_revision: str,
    source_ref: str,
) -> dict:
    """Called by a trusted observation owner at prediction time, before any action."""
    return snapshot(
        {
            "schema_version": ENVELOPE_SCHEMA,
            "context": {
                "execution_id": execution_id,
                "state_revision": state_revision,
                "source_ref": source_ref,
                "observation_id": request.observation_id,
                "binding": asdict(request.binding),
            },
            "request": asdict(request),
            "forecast": forecast,
        }
    )


def _reason(envelope: dict, situation: MissionSituation, now: float, max_age_seconds: float) -> str:
    if not math.isfinite(now) or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        return "invalid_freshness_policy"
    if set(envelope) != {"schema_version", "context", "request", "forecast"}:
        return "invalid_envelope"
    if envelope["schema_version"] != ENVELOPE_SCHEMA:
        return "invalid_envelope_schema"
    current = situation.constraints.get("prediction_context")
    captured = envelope["context"]
    for ctx in (current, captured):
        if not isinstance(ctx, dict) or set(ctx) != CONTEXT_FIELDS:
            return "missing_or_invalid_context"
        if any(
            not isinstance(ctx[k], str) or not ctx[k].strip() for k in CONTEXT_FIELDS - {"binding"}
        ):
            return "missing_or_invalid_context"
    if current != captured:
        return "context_invalidated"
    binding = PredictionBinding(**current["binding"])
    if any(not isinstance(v, str) or not v.strip() for v in asdict(binding).values()):
        return "invalid_binding"
    if situation.mission_contract.get("prediction_contract") != binding.mission_contract:
        return "mission_contract_mismatch"
    raw = dict(envelope["request"])
    raw["binding"] = PredictionBinding(**raw["binding"])
    raw["options"] = tuple(PredictionOption(**o) for o in raw["options"])
    request = PredictionRequest(**raw)
    if request.binding != binding or request.observation_id != current["observation_id"]:
        return "request_context_mismatch"
    if not isinstance(request.request_id, str) or not request.request_id.strip():
        return "invalid_request_id"
    if (
        type(request.observed_at) not in (int, float)
        or not 0 <= now - request.observed_at <= max_age_seconds
    ):
        return "stale_or_future_observation"
    forecast = envelope["forecast"]
    if forecast.get("schema_version") != PREDICTION_SCHEMA:
        return "invalid_forecast_schema"
    if forecast.get("verification_basis") != "model_inferred":
        return "invalid_evidence_source"
    if any(forecast.get(k) is not False for k in AUTHORITY_FLAGS):
        return "authority_claim_rejected"
    if (
        forecast.get("request_sha256") != request.digest()
        or forecast.get("request_id") != request.request_id
        or forecast.get("observation_id") != request.observation_id
        or forecast.get("binding") != asdict(binding)
    ):
        return "forecast_binding_mismatch"
    if forecast.get("status") == "unavailable":
        return "forecast_unavailable"
    if forecast.get("status") != "available":
        return "invalid_forecast_status"
    ids = [o.option_id for o in request.options]
    if not ids or len(ids) != len(set(ids)) or any(not isinstance(i, str) or not i for i in ids):
        return "invalid_options"
    forecasts = forecast["forecasts"]
    if not isinstance(forecasts, list) or len(forecasts) != len(ids):
        return "invalid_forecasts"
    by_id = {f["option_id"]: f for f in forecasts}
    if set(by_id) != set(ids):
        return "invalid_forecasts"
    for option in request.options:
        f = by_id[option.option_id]
        if (
            type(option.horizon_seconds) not in (int, float)
            or option.horizon_seconds <= 0
            or type(f["horizon_seconds"]) not in (int, float)
            or f["horizon_seconds"] != option.horizon_seconds
        ):
            return "horizon_mismatch"
        if type(f["risk_score"]) not in (int, float) or not 0 <= f["risk_score"] <= 1:
            return "invalid_risk"
        if not isinstance(f["future_state"], dict):
            return "invalid_future_state"
    return "bound_current_model_evidence"


def receive_prediction_evidence(
    situation: MissionSituation,
    envelope: dict,
    *,
    now: float,
    max_age_seconds: float,
) -> tuple[MissionSituation, dict]:
    """Adopt/reject evidence, without selecting a response or asserting feasibility.

    The situation's prediction_context is supplied independently by its trusted
    observation owner. A revision must change after any world-changing action.
    Hashes provide identity, not authentication of that owner or physical truth.
    """
    if (
        type(now) not in (int, float)
        or not math.isfinite(now)
        or type(max_age_seconds) not in (int, float)
        or not math.isfinite(max_age_seconds)
        or max_age_seconds <= 0
    ):
        raise ValueError("invalid_freshness_policy")
    digest = None
    try:
        envelope = snapshot(envelope)
        digest = prediction_digest(envelope)
        reason = _reason(envelope, situation, now, max_age_seconds)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        reason = "malformed_evidence"
    adopted = reason == "bound_current_model_evidence"
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "status": "adopted" if adopted else "rejected",
        "reason": reason,
        "evidence_source": "model_inferred" if adopted else "unaccepted",
        "evidence_sha256": digest,
        "source_situation_id": situation.situation_id,
        "source_situation_sha256": prediction_digest(situation.to_dict()),
        "evaluated_at": now,
        "max_age_seconds": max_age_seconds,
        "freshness_status": "current" if adopted else "not_accepted",
        "feasibility_established": False,
        "llm_judgment_invoked": False,
        **{k: False for k in AUTHORITY_FLAGS},
    }
    # Replace, rather than retain, earlier prediction evidence on every admission.
    uncertainty = snapshot(dict(situation.uncertainty))
    uncertainty["prediction_evidence"] = {"receipt": receipt}
    if adopted:
        uncertainty["prediction_evidence"].update(
            context=envelope["context"],
            request_sha256=envelope["forecast"]["request_sha256"],
            forecasts=envelope["forecast"]["forecasts"],
            forecast_status="available",
            verification_basis="model_inferred",
        )
    else:
        uncertainty["prediction_evidence"]["forecast_status"] = "not_adopted"
    updated = replace(situation, uncertainty=uncertainty)
    updated = replace(updated, input_digest=prediction_digest(updated.to_dict()))
    return updated, snapshot(receipt)


def revalidate_incident_prediction(graph: dict, *, envelope, current_context, now: float) -> dict:
    """Dispatch revalidation uses the trusted owner's *current* independent context."""
    original = graph.get("prediction_admission") or {}
    if (not original or original.get("status") == "not_supplied") and envelope is None:
        return {"status": "not_supplied", "dispatch_authority_created": False}
    if original.get("status") != "adopted":
        return {"status": "rejected", "reason": "prediction_judgment_missing", "dispatch_authority_created": False}
    try:
        if prediction_digest(envelope) != original.get("evidence_sha256"):
            raise ValueError("prediction_evidence_changed")
        situation = MissionSituation.from_dict(graph["mission_situation"])
        situation = replace(situation, constraints={**situation.constraints, "prediction_context": current_context})
        _, receipt = receive_prediction_evidence(situation, envelope, now=now, max_age_seconds=30)
        return receipt
    except (KeyError, TypeError, ValueError):
        return {"status": "rejected", "reason": "prediction_evidence_missing_or_changed", "dispatch_authority_created": False}

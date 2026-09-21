"""Opt-in ACWM composition with the existing Assurance/Rules/Executor boundaries."""

from __future__ import annotations

from dataclasses import asdict
import io
import json
from pathlib import Path
import re
import signal
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import numpy as np
from missionos_core.prediction import (
    PredictionBinding,
    PredictionOption,
    PredictionRequest,
)
from src.intelligence.mission_assurance_agent import MissionAssuranceAgent
from src.prediction.service import make_server
from src.prediction.stacking_acwm import (
    MACRO_ID,
    StackingACWMPredictor,
    validate_acwm_input,
)
from src.prediction.stacking_mission import DeepSeekJudge, GovernedStackingSession


class GovernedACWMPredictor(StackingACWMPredictor):
    # Lab metadata consumed by the existing HTTP health and observation paths.
    threshold = 0.5
    horizon_steps = 284


class GovernedACWMStackingSession(GovernedStackingSession):
    macro_id = MACRO_ID
    model_limit = (
        "Neural future-video generation followed by a frozen visual readout. "
        "Exact current simulator state is supplied. Only the next 14.2-second "
        "placement is forecast; bank and delayed terminal stability are unpredicted."
    )

    def score_evidence(self, forecast, next_count):
        return {
            "schema_version": "stacking_acwm_score_scope.v1",
            "status": "unavailable_missing_bank_forecast",
            "current_count": next_count - 1,
            "next_count": next_count,
            "risk_meaning": "Uncalibrated classifier output from generated video, not collapse probability.",
            "positive_class": "A score-bearing block drops more than 0.03 m during the placement.",
            "continue_horizon_seconds": 14.2,
            "bank_forecast_available": False,
            "terminal_hold_forecast_available": False,
            "bank_risk": None,
            "bank_proxy_points": None,
            "continue_then_bank_proxy_points": None,
            "calibrated": False,
            "recommended_option": None,
            "dispatch_authority_created": False,
        }

    def prediction_decision(self, body):
        ident = body["request_id"]
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", ident):
            raise ValueError("invalid request id")
        if ident in self.requests:
            raise ValueError("duplicate request id")
        if not self.allow_simulator_decisions:
            raise ValueError("simulator session not enabled")
        state = validate_acwm_input(body["state"])
        binding = PredictionBinding(**body["binding"])
        if binding != self.provider.binding:
            raise ValueError("session binding mismatch")
        request = PredictionRequest(
            ident,
            body["observation_id"],
            body["observed_at"],
            binding,
            body["state"],
            tuple(PredictionOption(**o) for o in body["options"]),
        )
        forecast = self.registry.forecast(request)
        if forecast["status"] != "available":
            raise ValueError("ACWM_forecast_unavailable:" + forecast["reason"])
        score = forecast["forecasts"][0]["risk_score"]
        result = {
            "schema_version": "missionos_simulator_prediction_decision.v1",
            "request_id": ident,
            "request_sha256": request.digest(),
            "observation_id": request.observation_id,
            "forecast": forecast,
            "selected_option": ("bank" if score >= self.provider.threshold else "continue"),
            "decision_policy": "acwm_reference_only_before_assurance",
            "threshold": self.provider.threshold,
            "existing_count": int(state["count"]) - 1,
            "llm_invoked": False,
            "scope": "opt_in_simulator_lab",
            "governed_mission": True,
            "hardware_dispatch_authorized": False,
        }
        # The inherited method replaces this audit reference with the actual LLM proposal.
        self.requests[ident] = result
        return result


def serve_acwm_mission(
    *,
    backend_url,
    model_sha256,
    readout_sha256,
    policy_sha256,
    output: Path,
    llm_model,
    seeds,
    approve_simulator_mission=False,
    operator="",
    port=0,
):
    address = urlparse(backend_url)
    if address.scheme != "http" or address.hostname not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        raise ValueError("explicit_loopback_ACWM_backend_required")
    if approve_simulator_mission and not operator.strip():
        raise ValueError("operator_authorization_reference_required")

    def backend(state):
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **state)
        with urlopen(
            Request(
                backend_url,
                data=buffer.getvalue(),
                headers={"Content-Type": "application/octet-stream"},
            ),
            timeout=150,
        ) as response:
            return json.load(response)

    provider = GovernedACWMPredictor(
        backend,
        model_sha256=model_sha256,
        readout_sha256=readout_sha256,
        policy_sha256=policy_sha256,
    )
    judge = DeepSeekJudge(llm_model, output.parent / "llm")
    session = GovernedACWMStackingSession(
        provider,
        output,
        agent=MissionAssuranceAgent(judge),
        seeds=seeds,
        approve=approve_simulator_mission,
        operator=operator,
    )
    server = make_server(session, port=port)
    session.save(
        "ready",
        {
            "port": server.server_port,
            "binding": asdict(provider.binding),
            "llm_model": llm_model,
            "llm_model_sha256": None,
        },
    )
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM, previous)

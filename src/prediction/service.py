"""Opt-in loopback prediction/decision/observation boundary for simulator labs.

This service has no actuator or Gateway dispatch route. A separately authorized
simulator executor consumes decisions; this is not an LLM or hardware integration.
"""

from __future__ import annotations

import json
import math
import re
import signal
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from missionos_core.prediction import (
    PredictionBinding,
    PredictionOption,
    PredictionRegistry,
    PredictionRequest,
    prediction_digest,
)

from src.prediction.stacking import compare_stacking_prediction


class PredictionSession:
    def __init__(self, provider, output: Path, *, allow_simulator_decisions: bool):
        self.provider = provider
        self.registry = PredictionRegistry()
        self.registry.register(provider)
        self.output = output
        output.mkdir(parents=True, exist_ok=False)
        self.allow_simulator_decisions = allow_simulator_decisions
        self.requests: dict[str, dict[str, Any]] = {}
        self.observations: set[tuple[str, str]] = set()

    def decide(self, body: dict[str, Any]) -> dict[str, Any]:
        from src.prediction.stacking import validate_state

        ident = body["request_id"]
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", ident):
            raise ValueError("invalid request id")
        if ident in self.requests:
            raise ValueError("duplicate request id")
        if not self.allow_simulator_decisions:
            raise ValueError("simulator session not enabled")
        x = validate_state(body["state"])
        binding = PredictionBinding(**body["binding"])
        if binding != self.provider.binding:
            raise ValueError("session binding mismatch")
        # Current-state fallback is explicitly a policy, never a prediction.
        tilt = max(
            [0.0]
            + [
                math.degrees(math.acos(max(-1.0, min(1.0, 1 - 2 * (q[1] ** 2 + q[2] ** 2)))))
                for q in x["objects"][: int(x["count"]) - 1, 3:]
            ]
        )
        drift = max(
            [0.0]
            + [
                float(((x["objects"][i, :2] - x["accepted"][i, :2]) ** 2).sum() ** 0.5)
                for i in range(int(x["count"]) - 1)
            ]
        )
        options = tuple(PredictionOption(**o) for o in body["options"])
        expected_horizon = self.provider.horizon_steps / 20
        if [(o.option_id, o.horizon_seconds) for o in options] != [
            ("continue", expected_horizon),
            ("bank", 14.2),
        ]:
            raise ValueError("unsupported option contract")
        if any(o.parameters != {"macro": "stacking.fixed_vla_placement.v1"} for o in options):
            raise ValueError("unsupported action parameters")
        request = PredictionRequest(
            ident,
            body["observation_id"],
            body["observed_at"],
            binding,
            body["state"],
            options,
        )
        forecast = self.registry.forecast(request)
        if forecast["reason"] in (
            "stale_or_future_observation",
            "missing_identity",
            "binding_mismatch",
            "invalid_options",
            "invalid_horizon",
        ):
            raise ValueError(forecast["reason"])
        threshold = self.provider.threshold
        stop = tilt > 5 or drift > 0.003
        mode = "current_state_fallback"
        if forecast["status"] == "available":
            risk = {f["option_id"]: f["risk_score"] for f in forecast["forecasts"]}
            stop = risk["continue"] >= threshold and risk["bank"] < risk["continue"]
            mode = "configured_wam_stop_rule"
        result = {
            "schema_version": "missionos_simulator_prediction_decision.v1",
            "request_id": ident,
            "request_sha256": request.digest(),
            "observation_id": request.observation_id,
            "forecast": forecast,
            "selected_option": "bank" if stop else "continue",
            "decision_policy": mode,
            "threshold": threshold,
            "existing_count": int(x["count"]) - 1,
            "llm_invoked": False,
            "scope": "opt_in_simulator_lab",
            "governed_mission": hasattr(self, "dispatch"),
            "hardware_dispatch_authorized": False,
        }
        # Persist before responding, hence before any simulator outcome is available.
        (self.output / f"{ident}-decision.json").write_text(json.dumps(result, indent=2))
        self.requests[ident] = result
        return result

    def observe(self, body: dict[str, Any]) -> dict[str, Any]:
        ident = body["request_id"]
        decision = self.requests[ident]
        role = body["role"]
        key = (ident, role)
        if role not in ("selected_execution", "counterfactual_hold") or key in self.observations:
            raise ValueError("invalid or duplicate outcome role")
        if body["request_sha256"] != decision["request_sha256"]:
            raise ValueError("outcome request mismatch")
        if body["observation_id"] != decision["observation_id"]:
            raise ValueError("outcome observation mismatch")
        option = body["option_id"]
        if option != decision["selected_option"]:
            raise ValueError("outcome does not match dispatched simulator option")
        if role == "counterfactual_hold" and option != "continue":
            raise ValueError("counterfactual hold requires continuation")
        result = body["result"]
        steps = result["horizon_steps"]
        if steps != (568 if role == "counterfactual_hold" else 284):
            raise ValueError("unexpected executed horizon")
        drops = result["per_object_drop"]
        if len(drops) != 10 or any(not math.isfinite(v) or v < 0 for v in drops):
            raise ValueError("invalid drop measurements")
        collapsed = max(drops) > 0.03
        if type(result["collapsed"]) is not bool or result["collapsed"] != collapsed:
            raise ValueError("collapse label not supported by measured drop")
        if result["technical_failure"] is not None:
            raise ValueError("unscorable technical failure")
        count = result["count_after"]
        expected_count = decision["existing_count"] + (option == "continue")
        if type(count) is not int or not decision["existing_count"] <= count <= expected_count:
            raise ValueError("invalid observed block count")
        if not collapsed and count != expected_count:
            raise ValueError("incomplete noncollapsed option")
        if type(result["score"]) is not int or result["score"] != (0 if collapsed else count):
            raise ValueError("score not supported by observed outcome")
        predicted = decision["forecast"]
        receipt = compare_stacking_prediction(
            predicted,
            request_sha256=body["request_sha256"],
            observation_id=body["observation_id"],
            option_id=option,
            horizon_seconds=steps / 20,
            collapsed=collapsed,
            threshold=decision["threshold"],
            outcome_ref=body["outcome_ref"],
            source_kind="simulator_observation",
        )
        receipt.update(
            request_id=ident,
            role=role,
            observed_result_sha256=prediction_digest(result),
            observed_score=result["score"],
            observed_horizon_steps=steps,
            decision_policy=decision["decision_policy"],
        )
        (self.output / f"{ident}-{role}.json").write_text(json.dumps(receipt, indent=2))
        self.observations.add(key)
        return receipt


def serve(
    *,
    model: Path,
    model_sha256: str,
    policy_sha256: str,
    output: Path,
    port: int = 0,
    allow_simulator_decisions: bool = False,
) -> None:
    from src.prediction.stacking import StackingPredictor

    provider = StackingPredictor(model, model_sha256, policy_sha256)
    session = PredictionSession(
        provider, output, allow_simulator_decisions=allow_simulator_decisions
    )

    server = make_server(session, port=port)
    (output / "ready.json").write_text(
        json.dumps({"port": server.server_port, "binding": asdict(provider.binding)})
    )

    def stop_service(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop_service)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM, previous)


def make_server(session: PredictionSession, *, port: int = 0) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, result):
            data = json.dumps(result, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != "/health":
                return self.send(404, {"error": "unknown route"})
            self.send(
                200,
                {
                    "binding": asdict(session.provider.binding),
                    "threshold": session.provider.threshold,
                    "continue_horizon_seconds": session.provider.horizon_steps / 20,
                    "scope": "opt_in_simulator_lab",
                    "governed_mission": hasattr(session, "dispatch"),
                    "readout_sha256": getattr(session.provider, "readout_sha256", None),
                },
            )

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2_000_000:
                    raise ValueError("invalid body size")
                body = json.loads(
                    self.rfile.read(size),
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
                )
                if self.path == "/decide":
                    result = session.decide(body)
                elif self.path == "/dispatch" and hasattr(session, "dispatch"):
                    result = session.dispatch(body)
                elif self.path == "/observe":
                    result = session.observe(body)
                else:
                    return self.send(404, {"error": "unknown route"})
                self.send(200, result)
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                self.send(400, {"error": str(exc)})

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    return server

"""Actual MissionOS HTTP boundary with a synthetic predictor, no private artifacts."""

from dataclasses import asdict
import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from missionos_core.prediction import OptionForecast, PredictionBinding
from src.prediction.service import PredictionSession, make_server
from src.prediction.stacking import ENVIRONMENT, INPUT_SCHEMA, MACRO, MISSION


class SyntheticPredictor:
    binding = PredictionBinding("fixture", "a" * 64, MISSION, "b" * 64, ENVIRONMENT, INPUT_SCHEMA)
    threshold = 0.5
    horizon_steps = 568

    def predict(self, request):
        return (OptionForecast("continue", 28.4, 0.9, {}), OptionForecast("bank", 14.2, 0.1, {}))


def body():
    return {
        "request_id": "step1",
        "observation_id": "sim:1",
        "observed_at": time.time(),
        "binding": asdict(SyntheticPredictor.binding),
        "options": [
            {
                "option_id": k,
                "horizon_seconds": h,
                "parameters": {"macro": "stacking.fixed_vla_placement.v1"},
            }
            for k, h in [("continue", 28.4), ("bank", 14.2)]
        ],
        "state": {
            "count": 1,
            "objects": [[0, 0, 0.82, 1, 0, 0, 0]] * 10,
            "velocity": [[0] * 6] * 10,
            "physics": [[0.05, 0.04, 0.04, 0.07, 0.7, 0.005, 0.0001, 0, 0, 0, 1]] * 10,
            "robot": [0] * 12,
            "accepted": [[0] * 3] * 10,
            "plan": [0, 0, 0.82] + MACRO,
        },
    }


def test_actual_http_decision_observation_and_rejection(tmp_path):
    session = PredictionSession(
        SyntheticPredictor(), tmp_path / "session", allow_simulator_decisions=True
    )
    server = make_server(session)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def post(route, payload):
        with urlopen(
            Request(
                f"http://127.0.0.1:{server.server_port}" + route,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=5,
        ) as r:
            return json.load(r)

    try:
        d = post("/decide", body())
        assert d["selected_option"] == "bank"
        assert (session.output / "step1-decision.json").exists()
        o = {
            "request_id": "step1",
            "request_sha256": d["request_sha256"],
            "observation_id": "sim:1",
            "role": "selected_execution",
            "option_id": "bank",
            "outcome_ref": "sim:result",
            "result": {
                "horizon_steps": 284,
                "per_object_drop": [0] * 10,
                "collapsed": False,
                "technical_failure": None,
                "score": 0,
            },
        }
        receipt = post("/observe", o)
        assert receipt["classification"] == "TN"
        assert not receipt["completion_claimed"]
        with pytest.raises(HTTPError):
            post("/observe", o)
        with pytest.raises(HTTPError):
            post("/decide", body())
        b = body()
        b["request_id"] = "stale"
        b["observed_at"] -= 100
        with pytest.raises(HTTPError):
            post("/decide", b)
        b = body()
        b["request_id"] = "future"
        b["state"]["future"] = {}
        with pytest.raises(HTTPError):
            post("/decide", b)
        b = body()
        b["request_id"] = "fallback"
        session.registry._providers.clear()
        d = post("/decide", b)
        assert d["decision_policy"] == "current_state_fallback"
        assert d["forecast"]["status"] == "unavailable"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

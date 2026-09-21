"""Synthetic forecast and judge; real HTTP/Assurance/Rules/Verifier integration."""

import json
import threading
import time
from dataclasses import asdict
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import pytest
from missionos_core.prediction import prediction_digest
from src.intelligence.mission_assurance_agent import (
    MissionAssuranceAgent,
    ModelJudgment,
)
from src.prediction.service import make_server
from src.prediction.stacking import MACRO
from src.prediction.stacking_acwm import MACRO_ID
from src.prediction.stacking_acwm_options import (
    StackingACWMOptionsPredictor,
    GovernedACWMOptionsSession,
)


@pytest.mark.parametrize("choice", ["continue", "hold"])
def test_two_option_acwm_governed_http(tmp_path, choice):
    calls = []

    def backend(state, option):
        calls.append("prediction:" + option)
        return {
            "model_sha256": "a" * 64,
            "readout_sha256": "b" * 64,
            "horizon_seconds": 28.4 if option == "continue" else 14.2,
            "option_id": option,
            "macro_id": MACRO_ID,
            "readout_output": 0.1,
            "invocation_id": "fixture-video",
        }

    class Judge:
        def judge(self, prompt):
            calls.append("judgment")
            s = prompt["mission_situation"]
            assert (
                s["uncertainty"]["prediction_evidence"]["receipt"]["status"]
                == "adopted"
            )
            scope = s["constraints"]["stacking_score_comparison"]
            assert scope["status"] == "both_option_forecasts_available"
            assert scope["bank_risk"] == 0.1
            assert scope["continue_horizon_seconds"] == 28.4
            assert scope["terminal_hold_forecast_available"] is True
            assert scope["continue_then_bank_proxy_points"] is None
            assert scope["recommended_option"] is None
            return ModelJudgment(
                output={
                    "proposed_response_kind": choice,
                    "parameters": {},
                    "rationale": "Synthetic judgment",
                    "expected_outcome": "Measured next",
                    "uncertainty": "Uncalibrated predictions",
                    "operator_question": "Review",
                },
                invocation_evidence={"invocation_kind": "fixture"},
            )

    provider = StackingACWMOptionsPredictor(
        backend, model_sha256="a" * 64, readout_sha256="b" * 64, policy_sha256="c" * 64
    )
    session = GovernedACWMOptionsSession(
        provider,
        tmp_path / "session",
        agent=MissionAssuranceAgent(Judge()),
        seeds=[1],
        approve=True,
        operator="fixture",
    )
    server = make_server(session)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    def post(route, body):
        with urlopen(
            Request(
                f"http://127.0.0.1:{server.server_port}{route}",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=10,
        ) as r:
            return json.load(r)

    objects = np.zeros((10, 7))
    objects[:, 3] = 1
    state = {
        "image": np.zeros((240, 240, 3), dtype=np.uint8).tolist(),
        "objects": objects.tolist(),
        "velocity": np.zeros((10, 6)).tolist(),
        "physics": np.ones((10, 11)).tolist(),
        "robot": np.zeros(12).tolist(),
        "count": 1,
        "plan": [0, 0, 0.84, *MACRO],
    }
    body = {
        "request_id": "game-1-step-1",
        "seed": 1,
        "state_revision": 1,
        "observation_id": "sim:1:1",
        "observed_at": time.time(),
        "binding": asdict(provider.binding),
        "state": state,
        "options": [
            {
                "option_id": "continue",
                "horizon_seconds": 28.4,
                "parameters": {"macro": MACRO_ID, "readout_sha256": "b" * 64},
            },
            {
                "option_id": "bank",
                "horizon_seconds": 14.2,
                "parameters": {"macro": MACRO_ID, "readout_sha256": "b" * 64},
            },
        ],
    }
    try:
        for key in ("future_actions", "actions", "future_frames", "collapsed"):
            with pytest.raises(HTTPError):
                post("/decide", body | {"state": state | {key: []}})
        assert not calls
        d = post("/decide", body)
        assert calls == ["prediction:continue", "prediction:bank", "judgment"]
        option = "bank" if choice == "hold" else "continue"
        assert d["selected_option"] == option
        assert not d["dispatch_authority_created"]
        assert d["llm_invoked"] == d["proposal"]["model_inference_invoked"]
        assert len(d["forecast"]["forecasts"]) == 2
        dispatch_body = {
            "request_id": body["request_id"],
            "decision_sha256": prediction_digest(d),
            "approval_policy_sha256": d["approval_policy_sha256"],
            "state_revision": 1,
            "observation_id": body["observation_id"],
            "state_sha256": prediction_digest(state),
            "observed_at": time.time(),
        }
        for k, v in [
            ("approval_policy_sha256", "wrong"),
            ("state_revision", 2),
            ("state_sha256", "changed"),
            ("observed_at", time.time() - 400),
        ]:
            with pytest.raises(HTTPError):
                post("/dispatch", dispatch_body | {k: v})
        dispatch = post("/dispatch", dispatch_body)
        assert (
            dispatch["dispatch_authority_created"] and not dispatch["executor_invoked"]
        )
        count = int(option == "continue")
        receipt = post(
            "/observe",
            {
                "request_id": body["request_id"],
                "request_sha256": d["request_sha256"],
                "observation_id": body["observation_id"],
                "option_id": option,
                "role": "selected_execution",
                "outcome_ref": "fixture:result",
                "ticket": dispatch["ticket"],
                "runtime_invocation": {
                    "invocation_kind": "simulator_motor_loop",
                    "motor_steps": 284,
                },
                "result": {
                    "horizon_steps": 284,
                    "per_object_drop": [0] * 10,
                    "collapsed": False,
                    "technical_failure": None,
                    "score": count,
                    "count_after": count,
                },
            },
        )
        assert receipt["observed_score"] == count
        assert receipt["mission_complete"] == (option == "bank")
        if option == "bank":
            assert receipt["status"] != "incomparable"
        else:
            assert receipt["status"] == "incomparable"  # hold not executed yet
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize(
    "scope,expected",
    [
        ("both_option_forecasts_available", "ACWM_OPTIONS_JUDGE_INSTRUCTION"),
        ("unavailable_missing_bank_forecast", "ACWM_JUDGE_INSTRUCTION"),
    ],
)
def test_acwm_deepseek_uses_matching_forecast_scope(
    tmp_path, monkeypatch, scope, expected
):
    import src.prediction.stacking_mission as module

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-not-a-real-key")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(
                {
                    "id": "fixture-response",
                    "model": "deepseek-flash",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "proposed_response_kind": "hold",
                                        "parameters": {},
                                        "rationale": "fixture",
                                        "expected_outcome": "fixture",
                                        "uncertainty": "fixture",
                                        "operator_question": "fixture",
                                    }
                                )
                            },
                        }
                    ],
                    "usage": {"completion_tokens": 10},
                }
            ).encode()

    def send(request, timeout):
        assert request.full_url == "https://api.deepseek.com/chat/completions"
        assert request.get_header("Authorization") == "Bearer synthetic-not-a-real-key"
        assert json.loads(request.data)["thinking"] == {"type": "disabled"}
        assert json.loads(request.data)["messages"][0]["content"] == getattr(
            module, expected
        )
        return Response()

    monkeypatch.setattr(module, "urlopen", send)
    judge = module.DeepSeekJudge("deepseek-flash", tmp_path / "llm")
    result = judge.judge(
        {
            "mission_situation": {
                "constraints": {"stacking_score_comparison": {"status": scope}}
            }
        }
    )
    assert result.invocation_evidence["invocation_kind"] == "llm_api"
    assert result.invocation_evidence["model_sha256"] is None
    saved = next((tmp_path / "llm").glob("*.json")).read_text()
    assert "synthetic-not-a-real-key" not in saved and "Authorization" not in saved

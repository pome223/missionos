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
from src.prediction.stacking_acwm_mission import (
    GovernedACWMPredictor,
    GovernedACWMStackingSession,
)


@pytest.mark.parametrize("choice", ["continue", "hold"])
def test_acwm_governed_http_without_invented_bank_forecast(tmp_path, choice):
    calls = []

    def backend(state):
        calls.append("prediction")
        return {
            "model_sha256": "a" * 64,
            "readout_sha256": "b" * 64,
            "horizon_seconds": 14.2,
            "macro_id": MACRO_ID,
            "readout_output": 0.1,
            "invocation_id": "fixture-video",
        }

    class Judge:
        def judge(self, prompt):
            calls.append("judgment")
            s = prompt["mission_situation"]
            assert s["uncertainty"]["prediction_evidence"]["receipt"]["status"] == "adopted"
            scope = s["constraints"]["stacking_score_comparison"]
            assert scope["status"] == "unavailable_missing_bank_forecast"
            assert scope["bank_risk"] is None
            assert scope["continue_then_bank_proxy_points"] is None
            assert scope["recommended_option"] is None
            return ModelJudgment(
                output={
                    "proposed_response_kind": choice,
                    "parameters": {},
                    "rationale": "Synthetic judgment",
                    "expected_outcome": "Measured next",
                    "uncertainty": "Bank future unavailable",
                    "operator_question": "Review",
                },
                invocation_evidence={"invocation_kind": "fixture"},
            )

    provider = GovernedACWMPredictor(
        backend, model_sha256="a" * 64, readout_sha256="b" * 64, policy_sha256="c" * 64
    )
    session = GovernedACWMStackingSession(
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
                "horizon_seconds": 14.2,
                "parameters": {"macro": MACRO_ID, "readout_sha256": "b" * 64},
            }
        ],
    }
    try:
        for key in ("future_actions", "actions", "future_frames", "collapsed"):
            with pytest.raises(HTTPError):
                post("/decide", body | {"state": state | {key: []}})
        assert not calls
        d = post("/decide", body)
        assert calls == ["prediction", "judgment"]
        option = "bank" if choice == "hold" else "continue"
        assert d["selected_option"] == option
        assert not d["dispatch_authority_created"]
        assert d["llm_invoked"] == d["proposal"]["model_inference_invoked"]
        assert len(d["forecast"]["forecasts"]) == 1
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
        assert dispatch["dispatch_authority_created"] and not dispatch["executor_invoked"]
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
            assert receipt["status"] == "incomparable"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_executor_captures_state_after_camera_refresh(monkeypatch):
    from types import SimpleNamespace
    from scripts import run_stacking_mission_e2e as executor

    env = SimpleNamespace(revision=0)

    def camera(env, np, mujoco):
        env.revision = 1
        return np.zeros((240, 240, 3), dtype=np.uint8)

    def gate(env, obs, place, accepted):
        assert env.revision == 1, "must not capture stale derived poses before camera refresh"
        return {
            k: np.array(0)
            for k in ("objects", "velocity", "physics", "robot", "count", "plan", "accepted")
        }

    monkeypatch.setattr(executor, "acwm_current_image", camera)
    game = SimpleNamespace(np=np, mujoco=None, gate_inputs=gate)
    state = executor.predictor_inputs(
        game, env, None, None, [], {"binding": {"input_schema": "stacking.current_image_macro.v1"}}
    )
    assert "accepted" not in state and state["image"].shape == (240, 240, 3)


def test_unavailable_acwm_never_invokes_assurance(tmp_path):
    class NeverJudge:
        def judge(self, prompt):
            raise AssertionError("unavailable forecast must not reach the judge")

    def unavailable(state):
        raise TimeoutError("fixture GPU unavailable")

    provider = GovernedACWMPredictor(
        unavailable, model_sha256="a" * 64, readout_sha256="b" * 64, policy_sha256="c" * 64
    )
    session = GovernedACWMStackingSession(
        provider,
        tmp_path / "unavailable",
        agent=MissionAssuranceAgent(NeverJudge()),
        seeds=[1],
        approve=True,
        operator="fixture",
    )
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
        "request_id": "unavailable",
        "seed": 1,
        "state_revision": 1,
        "observation_id": "sim:1:1",
        "observed_at": time.time(),
        "binding": asdict(provider.binding),
        "state": state,
        "options": [
            {
                "option_id": "continue",
                "horizon_seconds": 14.2,
                "parameters": {"macro": MACRO_ID, "readout_sha256": "b" * 64},
            }
        ],
    }
    with pytest.raises(ValueError, match="ACWM_forecast_unavailable"):
        session.decide(body)
    assert not session.pending

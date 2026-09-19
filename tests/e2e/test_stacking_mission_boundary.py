"""Public synthetic IO; real Assurance validation, policy storage and HTTP boundary."""

from dataclasses import asdict
import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from missionos_core.prediction import OptionForecast, PredictionBinding, prediction_digest
from src.intelligence.mission_assurance_agent import MissionAssuranceAgent, ModelJudgment
from src.prediction.stacking import ENVIRONMENT, INPUT_SCHEMA, MACRO, MISSION
from src.prediction.stacking_mission import GovernedStackingSession
from src.prediction.service import make_server


class Predictor:
    binding = PredictionBinding("fixture", "a" * 64, MISSION, "b" * 64, ENVIRONMENT, INPUT_SCHEMA)
    threshold = 0.5
    horizon_steps = 568

    def predict(self, request):
        return (OptionForecast("continue", 28.4, 0.9, {}), OptionForecast("bank", 14.2, 0.1, {}))


class Judge:
    def judge(self, prompt):
        assert (
            prompt["mission_situation"]["uncertainty"]["prediction_evidence"]["receipt"]["status"]
            == "adopted"
        )
        return ModelJudgment(
            output={
                "proposed_response_kind": "hold",
                "parameters": {},
                "rationale": "Fixture stop",
                "expected_outcome": "Fixture bank",
                "uncertainty": "Synthetic",
                "operator_question": "Review",
            },
            invocation_evidence={"invocation_kind": "fixture"},
        )


def request_body():
    return {
        "request_id": "game-1-step-1",
        "seed": 1,
        "state_revision": 1,
        "observation_id": "sim:1:1",
        "observed_at": time.time(),
        "binding": asdict(Predictor.binding),
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


@pytest.mark.parametrize("approve", [False, True])
def test_http_authority_and_observation_boundaries(tmp_path, approve):
    session = GovernedStackingSession(
        Predictor(),
        tmp_path / "session",
        agent=MissionAssuranceAgent(Judge()),
        seeds=[1],
        approve=approve,
        operator="fixture-operator",
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
        ) as response:
            return json.load(response)

    try:
        body = request_body()
        d = post("/decide", body)
        assert d["selected_option"] == "bank" and not d["dispatch_authority_created"]
        request = {
            "request_id": body["request_id"],
            "decision_sha256": prediction_digest(d),
            "approval_policy_sha256": d["approval_policy_sha256"],
            "state_revision": 1,
            "observation_id": body["observation_id"],
            "state_sha256": prediction_digest(body["state"]),
            "observed_at": time.time(),
        }
        for field, value in [
            ("approval_policy_sha256", "missing"),
            ("state_revision", 2),
            ("observed_at", time.time() - 400),
            ("decision_sha256", "tampered"),
            ("state_sha256", "changed"),
        ]:
            with pytest.raises(HTTPError):
                post("/dispatch", request | {field: value})
        if not approve:
            with pytest.raises(HTTPError):
                post("/dispatch", request)
            return
        dispatch = post("/dispatch", request)
        assert dispatch["dispatch_authority_created"] and not dispatch["executor_invoked"]
        with pytest.raises(HTTPError):
            post("/dispatch", request)
        outcome = {
            "request_id": body["request_id"],
            "request_sha256": d["request_sha256"],
            "observation_id": body["observation_id"],
            "option_id": "bank",
            "role": "selected_execution",
            "outcome_ref": "fixture:outcome",
            "ticket": dispatch["ticket"],
            "runtime_invocation": {"invocation_kind": "simulator_motor_loop", "motor_steps": 284},
            "result": {
                "horizon_steps": 284,
                "per_object_drop": [0] * 10,
                "collapsed": False,
                "technical_failure": None,
                "score": 0,
                "count_after": 0,
            },
        }
        with pytest.raises(HTTPError):
            post("/observe", outcome | {"ticket": "wrong"})
        observed = post("/observe", outcome)
        assert observed["mission_complete"] and observed["observed_score"] == 0
        assert not observed["physical_execution_invoked"]
        with pytest.raises(HTTPError):
            post("/observe", outcome)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_terminal_hold_is_authorized_and_verified_separately(tmp_path):
    class ContinueJudge(Judge):
        def judge(self, prompt):
            result = super().judge(prompt)
            result.output["proposed_response_kind"] = "continue"
            return result

    session = GovernedStackingSession(
        Predictor(),
        tmp_path / "terminal",
        agent=MissionAssuranceAgent(ContinueJudge()),
        seeds=[1],
        approve=True,
        operator="fixture-operator",
    )
    session.revisions[1] = 10  # Synthetic state at the tenth decision, not a live game claim.
    body = request_body()
    body.update(request_id="game-1-step-10", state_revision=10)
    body["state"]["count"] = 10
    d = session.decide(body)
    dispatch = session.dispatch(
        {
            "request_id": body["request_id"],
            "decision_sha256": prediction_digest(d),
            "approval_policy_sha256": d["approval_policy_sha256"],
            "state_revision": 10,
            "observation_id": body["observation_id"],
            "state_sha256": prediction_digest(body["state"]),
            "observed_at": time.time(),
        }
    )
    assert dispatch["maximum_motor_steps"] == 568
    outcome = {
        "request_id": body["request_id"],
        "request_sha256": d["request_sha256"],
        "observation_id": body["observation_id"],
        "option_id": "continue",
        "role": "selected_execution",
        "outcome_ref": "fixture:10",
        "ticket": dispatch["ticket"],
        "runtime_invocation": {"invocation_kind": "simulator_motor_loop", "motor_steps": 284},
        "result": {
            "horizon_steps": 284,
            "per_object_drop": [0] * 10,
            "collapsed": False,
            "technical_failure": None,
            "score": 10,
            "count_after": 10,
        },
    }
    with pytest.raises(ValueError, match="selected_execution_must_be_verified"):
        session.observe(
            outcome
            | {"role": "terminal_hold", "result": outcome["result"] | {"horizon_steps": 568}}
        )
    receipt = session.observe(outcome)
    assert not receipt["mission_complete"] and receipt["status"] == "incomparable"
    with pytest.raises(ValueError):
        session.observe(outcome | {"role": "counterfactual_hold"})
    final = session.observe(
        outcome | {"role": "terminal_hold", "result": outcome["result"] | {"horizon_steps": 568}}
    )
    assert (
        final["mission_complete"]
        and final["observed_score"] == 10
        and final["status"] == "compared"
    )


@pytest.mark.parametrize("failure", ["unavailable", "revoked", "expired_forecast"])
def test_failure_cannot_reach_budget_reservation(tmp_path, failure):
    class SpyJudge(Judge):
        calls = 0

        def judge(self, prompt):
            self.calls += 1
            return super().judge(prompt)

    judge = SpyJudge()
    session = GovernedStackingSession(
        Predictor(),
        tmp_path / "blocked",
        agent=MissionAssuranceAgent(judge),
        seeds=[1],
        approve=True,
        operator="fixture-operator",
    )
    body = request_body()
    if failure == "unavailable":
        session.registry._providers.clear()
        with pytest.raises(ValueError, match="prediction_not_adopted"):
            session.decide(body)
        assert judge.calls == 0
    else:
        decision = session.decide(body)
        if failure == "revoked":
            session.store.revoke(decision["approval_policy_sha256"])
        else:
            session.pending[body["request_id"]]["evidence"]["request"]["observed_at"] -= 400
        with pytest.raises(ValueError):
            session.dispatch(
                {
                    "request_id": body["request_id"],
                    "decision_sha256": prediction_digest(decision),
                    "approval_policy_sha256": decision["approval_policy_sha256"],
                    "state_revision": 1,
                    "observation_id": body["observation_id"],
                    "state_sha256": prediction_digest(body["state"]),
                    "observed_at": time.time(),
                }
            )
    with session.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


def test_deepseek_records_response_without_credentials(tmp_path, monkeypatch):
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
        return Response()

    monkeypatch.setattr(module, "urlopen", send)
    judge = module.DeepSeekJudge("deepseek-flash", tmp_path / "llm")
    result = judge.judge({"fixture": True})
    assert result.invocation_evidence["invocation_kind"] == "llm_api"
    assert result.invocation_evidence["model_sha256"] is None
    saved = next((tmp_path / "llm").glob("*.json")).read_text()
    assert "synthetic-not-a-real-key" not in saved and "Authorization" not in saved


@pytest.mark.parametrize(
    "next_count,continue_risk,bank_risk,expected_delta",
    [
        (6, 0.08459337597468057, 0.007821687687475343, 0.5315481825892943),
        (9, 0.2194472881399866, 0.0225465541029936, -0.794653160435931),
        (1, 0.0, 0.0, 1.0),
        (10, 1.0, 0.0, -9.0),
    ],
)
def test_score_comparison_explains_tradeoff_without_selecting(
    next_count, continue_risk, bank_risk, expected_delta
):
    from src.prediction.stacking_mission import stacking_score_comparison

    result = stacking_score_comparison(
        {
            "status": "available",
            "forecasts": [
                {"option_id": "continue", "risk_score": continue_risk, "horizon_seconds": 28.4},
                {"option_id": "bank", "risk_score": bank_risk, "horizon_seconds": 14.2},
            ],
        },
        next_count,
    )
    assert result["continue_minus_bank_proxy_points"] == pytest.approx(expected_delta)
    assert result["calibrated"] is False
    assert result["recommended_option"] is None
    assert result["dispatch_authority_created"] is False
    assert "selected_option" not in result


def test_score_comparison_rejects_incomparable_horizons():
    from src.prediction.stacking_mission import stacking_score_comparison

    with pytest.raises(ValueError, match="terminal_hold"):
        stacking_score_comparison(
            {
                "status": "available",
                "forecasts": [
                    {"option_id": "continue", "risk_score": 0.1, "horizon_seconds": 14.2},
                    {"option_id": "bank", "risk_score": 0.0, "horizon_seconds": 14.2},
                ],
            },
            6,
        )

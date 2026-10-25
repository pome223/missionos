"""Real inherited FD/host/decoder boundary with synthetic answers, no plant/API."""

from copy import deepcopy
import socket
import subprocess
import threading

import pytest
from src.intelligence import starship_restored_agent as agent
from src.runtime import starship_restored_decision_pipe as pipe
from src.runtime.starship_inflight_runtime import approval_pipe
from src.runtime.starship_restored_deployment import RestoredDeploymentJudgments
from src.runtime.starship_restored_operations import RegisteredRestoredOperations
from src.runtime.starship_restored_prefix import PENDING_DEPLOYMENT_DECISION
from tests.contract.test_starship_restored_intent import definition, summary, request
from tests.contract.test_starship_restored_agent import SyntheticTransport


@pytest.fixture(autouse=True)
def no_physics_or_api(monkeypatch):
    from src.runtime import starship_sixdof as dyn, starship_physics as env
    from src.runtime import starship_restored_return as returning
    from src.intelligence.starship_m2_broker import DeadlineTransport

    def forbidden(*_a, **_k):
        raise AssertionError("Boundary smoke forbids plant/provider/network/process")

    for owner, name in (
        (dyn, "step"),
        (dyn, "_rk4"),
        (dyn, "_rhs"),
        (env, "step"),
        (returning, "forecast_restored_return"),
        (DeadlineTransport, "_invoke"),
        (socket, "socket"),
        (socket, "create_connection"),
        (subprocess, "Popen"),
    ):
        monkeypatch.setattr(owner, name, forbidden)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def channels():
    a, b = approval_pipe()
    c, d = approval_pipe()
    yield a, b, c, d
    for endpoint in (a, b, c, d):
        endpoint.close()


def test_client_v2_source_mismatch_rejects_before_any_frame_or_seen_slot(channels):
    a, b, c, _d = channels
    value = request()
    value["operational_intent"]["source_sha256"] = "f" * 64
    client = pipe.RestoredDecisionClient(
        context={"run_id": "intent", "mission_id": "test", "vehicle_id": "ship"},
        plan_sha256="a" * 64,
        source_sha256="b" * 64,
        request_writer=b,
        response_reader=c,
        clock=lambda: 100.0,
    )
    with pytest.raises(ValueError, match="source_differs_from_pipe"):
        client.begin(value)
    assert client.sequence == 0 and client.pending is None and not client.seen and a.poll() is None


@pytest.mark.parametrize("second_choice", ["continue_deployment", "withhold_deployment"])
def test_real_fd_ops_deployment_client_host_production_decoder_preserves_even_contrary_choice(
    channels, second_choice
):
    a, b, c, d = channels
    clock = Clock()
    sent = []
    binding = {
        "context": {"run_id": "intent", "mission_id": "test", "vehicle_id": "ship"},
        "plan_sha256": "a" * 64,
        "source_sha256": "b" * 64,
    }
    client = pipe.RestoredDecisionClient(
        **binding, request_writer=b, response_reader=c, clock=clock
    )
    ops = RegisteredRestoredOperations(definition())
    decisions = RestoredDeploymentJudgments(
        client, scope_sha256="c" * 64, notice=ops.deployment_notice, clock=clock, sleep=clock.sleep
    )

    class Agent:
        def __init__(self, *, active, clock):
            self.active, self.clock = active, clock

        def assess(self, req, **kwargs):
            action = "continue_deployment" if req["facts"]["released_count"] == 0 else second_choice
            answer = {
                "model": agent.JEV_MODEL,
                "answers": {
                    "decision": {
                        "type": "choice",
                        "choice": action,
                        "probabilities": {"continue_deployment": 0.59, "withhold_deployment": 0.41},
                        "confidence": 0.18,
                    }
                },
            }
            transport = SyntheticTransport(answer)
            response = agent.RestoredAgent(
                transport=transport, active=self.active, clock=self.clock
            ).assess(req, **kwargs)
            sent.append(deepcopy(transport.sent))
            return response

    host = pipe.RestoredDecisionHost(
        **binding, reader=a, writer=d, agent_factory=Agent, clock=clock
    )

    def complete(slot, start):
        assert decisions(summary(slot, start)) is PENDING_DEPLOYMENT_DECISION
        for _ in range(1000):
            host.tick()
            record = host.record()
            if record["requests"] and record["requests"][-1]["delivered_result"] is not None:
                break
            threading.Event().wait(0.001)
        else:
            pytest.fail("Injected decoder did not finish")
        return decisions(summary(slot, start + 0.25, started=start))

    assert complete(0, 2000.0) == "continue_deployment"
    assert complete(13, 2100.0) == second_choice
    evidence = decisions.record()
    assert len(evidence["decisions"]) == 2 and len(sent) == 2
    first, second = [row["request"] for row in evidence["decisions"]]
    assert first["schema"] == agent.SCHEMA and "operational_intent" not in first
    assert second["schema"] == "missionos.starship_restored_decision_request.v2"
    assert second["deadline_s"] - second["time_s"] == 35.0
    assert second["facts"]["release_ready"] is True
    assert second["facts"]["effect.continue_deployment.intent_met_if_completed"] is False
    assert second["facts"]["effect.withhold_deployment.intent_met_if_completed"] is True
    assert evidence["decisions"][1]["fallback_used"] is False
    assert evidence["decisions"][1]["applied_action"] == second_choice
    assert sent[1]["state"]["operational_intent"]["evaluation_point"] == "closed_inventory"
    assert "desired final inventory outcome" in sent[1]["questions"]["decision"]["instructions"]
    assert "operational_intent" not in sent[0]["state"]
    assert "desired final inventory outcome" not in sent[0]["questions"]["decision"]["instructions"]
    assert client.record()["provider_calls_by_worker"] == 0
    assert all(not row["exchange"]["model_inference_invoked"] for row in evidence["decisions"])
    with pytest.raises(ValueError, match="duplicate_or_skipped"):
        decisions(summary(13, 2200.0))
    assert len(sent) == 2  # Low confidence or contrary answer causes no retry.


def test_host_rejects_forged_v2_source_before_agent_creation(channels):
    a, b, _c, d = channels
    value = request()
    value["operational_intent"]["source_sha256"] = "f" * 64
    binding = {
        "context": {"run_id": "intent", "mission_id": "test", "vehicle_id": "ship"},
        "plan_sha256": "a" * 64,
        "source_sha256": "b" * 64,
    }
    frame = {
        "schema": pipe.REQUEST_FRAME,
        **binding,
        "sequence": 1,
        "request_id": value["request_id"],
        "request_sha256": pipe._digest(value),
        "request": value,
        "started_wall_s": 100.0,
        "wall_deadline_s": 135.0,
    }
    b.send(frame)
    host = pipe.RestoredDecisionHost(
        **binding,
        reader=a,
        writer=d,
        agent_factory=lambda **_k: pytest.fail("Must reject before making an agent"),
        clock=lambda: 100.0,
    )
    with pytest.raises(ValueError, match="source_differs_from_pipe"):
        host.tick()
    assert host.sequence == 0 and host.pending is None and host.sessions == []

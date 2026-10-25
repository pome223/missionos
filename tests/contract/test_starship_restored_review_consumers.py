"""Production V2 consumers over inherited FDs; invented data, no real flight.

Only plant advancement and forecast calculation are replaced with scripted
saved-fixture data. Deployment, coordinator construction, decision requests,
FD transport, production response decoding, review routing, fresh observation
reacquisition, and the approval-request writer are the production consumers.
No operator approves anything and no return is dispatched by these tests.
"""
from copy import deepcopy
from hashlib import sha256
import gzip
import json
from pathlib import Path
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest

from src.intelligence.starship_restored_agent import RestoredAgent
from src.runtime import starship_restored_coordinator as control
from src.runtime import starship_restored_contract as contract
from src.runtime import starship_restored_return as returning
from src.runtime.starship_inflight_runtime import approval_pipe
from src.runtime.starship_restored_decision_pipe import RestoredDecisionClient, RestoredDecisionHost
from src.runtime.starship_restored_deployment import RestoredDeploymentJudgments
from src.runtime.starship_restored_prefix import PENDING_DEPLOYMENT_DECISION
from src.runtime.starship_restored_return_review import DEFAULT_POLICY, policy_digest
from tests.contract.test_starship_restored_admission import case
from tests.contract.test_starship_restored_coordinator import Clock


@pytest.fixture(autouse=True)
def no_actual_physics_forecast_network_or_subprocess(monkeypatch):
    from src.runtime import starship_sixdof as dyn, starship_physics as env
    from src.runtime import starship_sixdof_mission as mission

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Consumer smoke cannot integrate physics, calculate forecasts, invoke network or subprocesses")

    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"), (env, "step"),
            (mission, "advance_plant"), (returning, "advance_plant"), (returning, "control"),
            (returning, "forecast_restored_return"), (returning, "continue_restored_return"),
            (socket, "socket"), (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def consumers(tmp_path, monkeypatch):
    nominal, later = case(), case(candidate_id="next_orbit")
    sources = deepcopy(nominal[4])
    for filename in ("starship_restored_coordinator.py", "starship_restored_return_context.py",
            "starship_restored_return_review.py"):
        path = Path(control.__file__).with_name(filename)
        sources["src/runtime/"+filename] = sha256(path.read_bytes()).hexdigest()
    raw = json.loads(nominal[1].source_record_bytes())
    raw["source_sha256"] = sources
    encoded = control._wire(raw)
    origin = contract.ActualRestoredOrigin(sha256(encoded).hexdigest(), encoded, nominal[3], sources)
    clock, pairs, payloads, advances = Clock(), [approval_pipe() for _ in range(7)], [], []
    context = {"run_id": "review-consumers", "mission_id": "synthetic", "vehicle_id": "ship"}
    config = {"deep_action": "request_scope_amendment", "deep_valid": True}

    class Transport:
        def _invoke(self, provider, payload, receipt, decode):
            payloads.append((provider, deepcopy(payload)))
            if provider == "typesafe":
                public = payload["state"]
                if public["phase"] == "deployment":
                    action = "continue_deployment" if public["evidence"]["released_count"] == 0 else "withhold_deployment"
                    probabilities = {name: .9 if name == action else .1 for name in public["allowed_actions"]}
                    confidence = .9
                else:
                    action, confidence = "keep_reserved_return", .17
                    probabilities = {"keep_reserved_return": .31, "request_scope_amendment": .30,
                        "request_deep_reasoning": .26, "wait": .10, "select_nominal": .02, "select_next_orbit": .01}
                    assert set(probabilities) == set(public["allowed_actions"])
                response = {"model": payload["model"], "answers": {"decision": {
                    "type": "choice", "choice": action, "confidence": confidence, "probabilities": probabilities}}}
            else:
                response = {"model": payload["model"], "choices": [{"finish_reason": "stop" if config["deep_valid"] else "length",
                    "message": {"role": "assistant", "content": json.dumps({"action": config["deep_action"],
                        "summary": "Synthetic review of the original return inputs.", "reference_ids": ["time_s"]})}}]}
            receipt.update(mode="synthetic_fixture", model_inference_invoked=False,
                model_inference_status="not_invoked_synthetic_fixture", status="synthetic_decoder_only")
            try:
                answer, model = decode(response)
            except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
                receipt["status"] = "invalid_response"
                return None
            receipt.update(status="succeeded", model_id=model)
            return answer

    def factory(*, active, clock):
        return RestoredAgent(transport=Transport(), active=active, clock=clock)

    client = RestoredDecisionClient(context=context, plan_sha256="a"*64, source_sha256=control._hash(sources),
        request_writer=pairs[0][1], response_reader=pairs[1][0], clock=clock, return_review_policy=DEFAULT_POLICY)
    host = RestoredDecisionHost(context=context, plan_sha256="a"*64, source_sha256=control._hash(sources),
        reader=pairs[0][0], writer=pairs[1][1], clock=clock, agent_factory=factory, return_review_policy=DEFAULT_POLICY)
    deployment = RestoredDeploymentJudgments(client, scope_sha256="d"*64, clock=clock, sleep=clock.sleep)

    def pump_host():
        host.tick()
        if host.sessions:
            host.sessions[-1]["thread"].join(.1)
        host.tick()

    def release_slot(slot, when):
        summary = {"time_s": when, "released_count": slot, "retained_count": 26-slot,
            "release_ready": True, "decision_started_s": None}
        assert deployment(summary) is PENDING_DEPLOYMENT_DECISION
        pump_host()
        return deployment({**summary, "time_s": when+2., "decision_started_s": when})

    assert release_slot(0, 1900.) == "continue_deployment"
    assert release_slot(13, 1950.) == "withhold_deployment"
    assert client.pending is None

    class SavedForecasts:
        """Analytic saved contacts only; not a prediction implementation."""
        def __init__(self, requests, *, output_dir, **_kwargs):
            self.requests, self.output_dir, self.state = requests, output_dir, "created"
        def start(self):
            self.output_dir.mkdir()
            (self.output_dir/"profile.json").write_bytes(nominal[3])
            (self.output_dir/"origin-000.json").write_bytes(origin.source_record_bytes())
            control._save(self.output_dir/"job.json", {"profile_file": "profile.json",
                "input_sha256": {"profile.json": sha256(nominal[3]).hexdigest(),
                    "origin-000.json": origin.source_record_sha256}})
            control._save(self.output_dir/"batch.claim", {"synthetic_saved_forecast_only": True})
            self.state = "running"
            return self
        def poll(self):
            results = []
            for request, original in zip(self.requests, (nominal, later), strict=True):
                value = original[0].record()
                value.update(binding=request.record(), initial_binding=request.record())
                result = returning.RestoredReturnResult(control._wire(value))
                folder = self.output_dir/request.candidate_id
                folder.mkdir()
                (folder/"prediction.json.gz").write_bytes(gzip.compress(result._record_json, mtime=0))
                for filename in ("result.json", "integration.json", "started.json", "worker.claim"):
                    control._save(folder/filename, {"synthetic_saved_forecast_only": True})
                results.append(result)
            self.state = "completed"
            return tuple(results)
        def close(self):
            self.state = "closed"

    monkeypatch.setattr(control, "RestoredForecastBatch", SavedForecasts)
    coordinator = control.RestoredReturnCoordinator(origin, profile_raw_bytes=nominal[3],
        expected_source_sha256=sources, integration_policy=control.RestoredIntegrationPolicy(2),
        context=context, plan_sha256="a"*64, output_dir=tmp_path/"governance", decision_client=client,
        ready_writer=pairs[2][1], ready_reader=pairs[3][0], approval_request_writer=pairs[4][1],
        approval_grant_reader=pairs[5][0], telemetry_writer=pairs[6][1], sites=nominal[6],
        notice_resources=lambda _snapshot: (nominal[7], nominal[8]), notice_generations=(0,),
        deployment_record=deployment.record(), clock=clock, sleep=clock.sleep,
        return_context_projection=True, return_review_policy=DEFAULT_POLICY)
    assert type(coordinator) is control.RestoredReturnCoordinator
    assert coordinator.stage == "created" and not coordinator.output.exists() and advances == []
    coordinator.start()
    owner = {"snapshot": coordinator.execution.snapshot()}
    coordinator.execution._context["actual_snapshot_provider"] = lambda: deepcopy(owner["snapshot"])

    def scripted_step():
        snapshot = coordinator.execution.snapshot()
        snapshot["state"]["time_s"] += .25
        coordinator.execution._context["snapshot"] = snapshot
        owner["snapshot"] = deepcopy(snapshot)
        coordinator.execution._context["coast_history"].append({"state": deepcopy(snapshot["state"]), "retained_count": 13})
        advances.append(snapshot["state"]["time_s"])
        return snapshot

    monkeypatch.setattr(coordinator.execution, "step_once", scripted_step)
    # Only a numerical admission fixture is injected, as in the existing
    # coordinator contracts. Its invented contact is not physical evidence.
    numerical = control.admit_restored_candidate
    def fixture_admission(*args, **kwargs):
        result = numerical(*args, **kwargs)
        result.update(accepted=True, reasons=[], numerical_contact_capability_met=True, matched_synthetic_site_ids=["synth"])
        return result
    monkeypatch.setattr(control, "admit_restored_candidate", fixture_admission)
    ready = pairs[2][0].poll()
    pairs[3][1].send({"schema": control.ACK, "ready_sha256": control._hash(ready), "nonce": ready["nonce"],
        "context": context, "plan_sha256": "a"*64, "source_sha256": control._hash(sources),
        "binding_sha256": ready["binding_sha256"], "initial_reservation_sha256": ready["initial_reservation_sha256"], "ledger_ready": True})

    def run_until_choice():
        for _ in range(30):
            pump_host()
            coordinator.tick()
            while pairs[6][0].poll() is not None:
                pass
            if coordinator.decisions:
                return coordinator.decisions[-1]
            threading.Event().wait(.001)
        pytest.fail("Scripted coordinator failed to consume its real FD judgment")

    yield SimpleNamespace(coordinator=coordinator, client=client, host=host, pairs=pairs,
        deployment=deployment, payloads=payloads, config=config, run=run_until_choice, advances=advances)
    coordinator.close()
    coordinator.execution._iterator.close()
    for pair in pairs:
        for endpoint in pair:
            endpoint.close()


def test_real_v2_consumer_chain_reaches_only_unapproved_scope_request(consumers):
    s, c = consumers, consumers.coordinator
    decision = s.run()
    assert c.stage == c.actor.status == "awaiting_operator"
    assert decision["original_action"] == "request_scope_amendment"
    assert decision["disposition"] == "scope_amendment_requested"
    request = s.pairs[4][0].poll()
    assert request is not None and request["candidate_sha256"] == decision["candidate"]["proposal_sha256"]
    assert request["context"] == s.client.binding["context"]
    assert c.actor.revision == 0 and c.changes == [] and c.dispatch is None
    assert s.pairs[5][0].poll() is None
    result = s.client.completed[-1]
    assert result["return_review"]["route"] == "rules_uncertainty_review"
    assert result["attempts"][0]["judgment"]["action"] == "keep_reserved_return"
    assert result["attempts"][0]["judgment"]["status"] == "valid"
    assert result["judgment"]["action"] == "request_scope_amendment"
    assert result["execution_permission_granted"] is False
    assert result["model_inference_invoked"] is False
    assert [provider for provider, _ in s.payloads] == ["typesafe", "typesafe", "typesafe", "deepseek"]
    assert [row["applied_action"] for row in s.deployment.completed] == ["continue_deployment", "withhold_deployment"]
    assert all(row["exchange"]["return_review"]["route"] == "not_applicable" for row in s.deployment.completed)
    assert all(row["fallback_used"] is False for row in s.deployment.completed)
    assert c.record()["decision_client"]["return_review_policy_sha256"] == policy_digest(DEFAULT_POLICY)
    assert c.record()["handshake"]["ready"] == c.ready
    assert all(row["provider_receipt"]["model_inference_invoked"] is False
        for exchange in s.client.completed for row in [item["judgment"] for item in exchange["attempts"]])


def test_real_coordinator_honors_contrary_deep_keep_without_fabricated_amendment(consumers):
    s, c = consumers, consumers.coordinator
    s.config["deep_action"] = "keep_reserved_return"
    decision = s.run()
    assert decision["original_action"] == "keep_reserved_return"
    assert decision["disposition"] == "valid_keep_reservation_unchanged"
    assert c.stage == "waiting_reserved" and c.changes == [] and c.actor.revision == 0
    assert s.pairs[4][0].poll() is None and c.dispatch is None
    assert s.client.completed[-1]["action"] == "keep_reserved_return"


def test_real_coordinator_uses_unavailable_path_after_failed_required_review(consumers):
    s, c = consumers, consumers.coordinator
    s.config["deep_valid"] = False
    decision = s.run()
    result = s.client.completed[-1]
    assert result["status"] == "no_valid_judgment" and result["action"] is None
    assert result["attempts"][0]["judgment"]["action"] == "keep_reserved_return"
    assert decision["original_action"] is None
    assert decision["disposition"] == "unavailable_preserved_preapproved_nominal"
    assert c.stage == "waiting_reserved" and c.changes == [] and c.actor.revision == 0
    assert s.pairs[4][0].poll() is None and c.dispatch is None

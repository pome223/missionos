"""Real private IPC/SQLite boundaries with declared observation/judge fixtures.

No spacecraft integration, native provider call or hardware action occurs.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from threading import Event
from types import SimpleNamespace

import pytest

from src.runtime import starship_tower_broker as broker_module
from src.runtime import starship_tower_supervision as tower
from src.runtime.starship_operator_resolution import OperatorResolutionError, OperatorResolutionLedger, SCOPE

KEY = b"explicit-broker-fixture-signing-key-32bytes"
CONTEXT = {"session_id": "session-1", "plan_id": "plan-1", "plan_sha256": "a"*64,
           "run_id": "run-1", "request_id": "request-1"}
SOURCE = "b"*64


class ProcessFixture:
    ended = False
    def poll(self):
        return 0 if self.ended else None


class ClockFixture:
    def __init__(self):
        self.now = 1000.
    def __call__(self):
        return self.now


class JudgeFixture:
    fixture_only = True
    def __init__(self, routes=("bounded",), action="continue"):
        self.routes, self.action, self.calls = routes, action, []
    def judge(self, prompt):
        self.calls.append(deepcopy(prompt))
        route = self.routes[min(len(self.calls)-1, len(self.routes)-1)]
        return SimpleNamespace(output={"proposed_response_kind": self.action, "parameters": {}},
            invocation_evidence={"assessment_route": route}, model_inference_invoked=True)


def scope():
    return {"schema": tower.SCOPE_SCHEMA, "scope": SCOPE, "context": deepcopy(CONTEXT),
        "approval_record_sha256": "c"*64, "source_sha256": SOURCE, "issued_simulation_time_s": 0.,
        "issued_wall_time_s": 1000., "original_simulation_deadline_s": 85., "original_wall_deadline_s": 1075.,
        "maximum_observation_age_s": 2., "observation_collection_allowed": True, "human_resolution_allowed": True}


def observation(simulation=10., wall=1000., ready=True):
    state = {"time_s": simulation, "r_eci_m": [6378137., 0., 0.], "v_eci_mps": [0., 0., 0.],
        "q_body_to_eci": [1., 0., 0., 0.], "omega_body_rad_s": [0., 0., 0.], "propellant_kg": 78000.,
        "engine_states": [{"throttle": 0., "gimbal_x_rad": 0., "gimbal_y_rad": 0., "available": True} for _ in range(45)],
        "flap_angles_rad": [0.]*6}
    return tower.tower_observation(context=CONTEXT, observation_id=f"obs-{simulation}".replace(".", "-"),
        state=state, wall_time_s=wall, tower_ready=ready, return_mode="capture")


def system(tmp_path, judge=None):
    mailbox = tmp_path/"mailbox"
    ledger = OperatorResolutionLedger(KEY, store_path=tmp_path/"ledger.sqlite3")
    worker_ledger = OperatorResolutionLedger(KEY, store_path=tmp_path/"ledger.sqlite3")
    process, clock, source = ProcessFixture(), ClockFixture(), {"current": True}
    judge = judge or JudgeFixture()
    broker = broker_module.TowerBroker(mailbox, CONTEXT["run_id"], scope(), process, ledger, judge,
        lambda: source["current"], mode="fixture", clock=clock)
    worker = tower.TowerSupervision(scope(), resolution_ledger=worker_ledger, resolution_signing_key=KEY)
    return SimpleNamespace(mailbox=mailbox, ledger=ledger, worker_ledger=worker_ledger, worker=worker,
                           broker=broker, process=process, clock=clock, source=source, judge=judge)


def publish(system, obs, *, active=True, frame_changes=None):
    frame = {"schema": broker_module.FRAME_SCHEMA, "context": deepcopy(CONTEXT), "source_sha256": SOURCE,
        "observation": deepcopy(obs), "simulation_time_s": obs["simulation_time_s"], "wall_time_s": obs["wall_time_s"],
        "run_active": active}
    frame.update(frame_changes or {})
    broker_module.write_tower_frame(system.mailbox, "latest-observation.json", frame, replace=True)


def child_tick(system, obs, **changes):
    return system.worker.tick(obs, simulation_time_s=obs["simulation_time_s"], wall_time_s=obs["wall_time_s"],
        run_active=True, source_sha256=SOURCE, plan_sha256=CONTEXT["plan_sha256"], **changes)


def routing(system, obs=None):
    obs = obs or observation()
    publish(system, obs)
    request = child_tick(system, obs).routing_request
    broker_module.write_tower_frame(system.mailbox, "routing-request-1.json", request)
    return request


def human_pending(system, *, ready=True):
    request = routing(system, observation(ready=ready))
    system.broker.step()
    response = json.loads((system.mailbox/"routing-response-1.json").read_text())
    system.clock.now = 1001.
    obs = observation(11., 1001., ready=ready)
    publish(system, obs)
    human = child_tick(system, obs, routing_proposal=response).human_request
    assert human is not None
    broker_module.write_tower_frame(system.mailbox, "human-request.json", human)
    return request, human, obs


def resolve(system, request, obs, **changes):
    values = {"context": deepcopy(CONTEXT), "request_sha256": request["sha256"],
        "observed_evidence_sha256": obs["evidence_sha256"], "action": "continue_capture"}
    values.update(changes)
    return system.broker.resolve(**values)


def test_private_atomic_mailbox_and_single_bound_proposal(tmp_path):
    item = system(tmp_path)
    request = routing(item)
    status = item.broker.step()
    response = json.loads((item.mailbox/"routing-response-1.json").read_text())
    assert response["request_sha256"] == request["sha256"]
    assert response["proposed_action"] == "continue_capture"
    assert response["classifier_evidence"]["model_inference_invoked"] is False
    assert status["routing_calls_attempted"] == status["routing_replies_published"] == 1
    item.broker.step()
    assert len(item.judge.calls) == 1
    assert item.mailbox.stat().st_mode & 0o077 == 0
    assert all(path.stat().st_mode & 0o077 == 0 for path in item.mailbox.iterdir())
    assert KEY not in b"".join(path.read_bytes() for path in item.mailbox.iterdir())


def test_initial_child_startup_waits_without_spending_or_inventing_a_state(tmp_path):
    item = system(tmp_path)
    assert item.broker.step()["status"] == "waiting_for_child"
    assert item.broker.pending()["latest_observation"] is None
    assert item.judge.calls == []
    item.clock.now = 1075.
    assert item.broker.step()["status"] == "original_deadline_expired_without_frame"
    assert not (item.mailbox/"human-response.json").exists()


def test_second_classifier_call_requires_two_seconds_of_new_collection(tmp_path):
    item = system(tmp_path, JudgeFixture(("need_observation", "bounded")))
    routing(item, observation(ready=None))
    item.broker.step()
    response = json.loads((item.mailbox/"routing-response-1.json").read_text())
    item.clock.now = 1001.
    next_obs = observation(11., 1001., ready=None)
    publish(item, next_obs)
    operation = child_tick(item, next_obs, routing_proposal=response).observation_request
    broker_module.write_tower_frame(item.mailbox, "observation-request.json", operation)
    item.clock.now = 1003.
    fresh = observation(13., 1003.)
    publish(item, fresh)
    followup = child_tick(item, fresh).routing_request
    broker_module.write_tower_frame(item.mailbox, "routing-request-2.json", followup)
    status = item.broker.step()
    assert status["routing_calls_attempted"] == 2 and status["followup_calls"] == 1
    assert (item.mailbox/"routing-response-2.json").exists()
    item.broker.step()
    assert len(item.judge.calls) == 2


def test_second_call_without_actual_collection_does_not_spend_a_slot(tmp_path):
    item = system(tmp_path)
    request = routing(item)
    item.broker.step()
    second = deepcopy(request)
    second["round"] = 2
    second["sha256"] = tower._digest({key: value for key, value in second.items() if key != "sha256"})
    broker_module.write_tower_frame(item.mailbox, "routing-request-2.json", second)
    with pytest.raises(broker_module.TowerBrokerError, match="lacks_later_observation"):
        item.broker.step()
    assert len(item.judge.calls) == 1


def test_shared_sqlite_signed_human_request_and_fresh_response_roundtrip(tmp_path):
    item = system(tmp_path, JudgeFixture(("human_review",)))
    _, request, old = human_pending(item)
    pending = item.broker.pending()
    assert pending["pending"] and pending["request"] == request
    item.clock.now = 1002.
    latest = observation(12., 1002.)
    publish(item, latest)
    with pytest.raises(broker_module.TowerBrokerError, match="latest_evidence_changed"):
        resolve(item, request, old)
    decision = resolve(item, request, latest)
    assert decision == item.worker_ledger.records(CONTEXT)["decision"]
    assert decision == json.loads((item.mailbox/"human-response.json").read_text())
    assert decision["human_identity_authenticated"] is False
    assert decision["executor_command_issued"] is False
    assert item.broker.pending()["pending"] is False
    with pytest.raises(OperatorResolutionError, match="already_consumed"):
        resolve(item, request, latest)


def test_concurrent_human_resolutions_consume_once_and_write_one_response(tmp_path):
    item = system(tmp_path, JudgeFixture(("human_review",)))
    _, request, obs = human_pending(item)
    def attempt(index):
        try:
            return resolve(item, request, obs, action="continue_capture" if index else "divert")
        except OperatorResolutionError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    winners = [value for value in results if isinstance(value, dict)]
    assert len(winners) == 1
    assert results.count("resolution_already_consumed") == 3
    assert json.loads((item.mailbox/"human-response.json").read_text()) == winners[0]


@pytest.mark.parametrize("change", ["process", "source", "stale", "wall_deadline", "sim_deadline", "frame_inactive"])
def test_pending_choice_refuses_lifecycle_freshness_or_original_deadline_changes(tmp_path, change):
    item = system(tmp_path, JudgeFixture(("human_review",)))
    _, request, obs = human_pending(item)
    if change == "process":
        item.process.ended = True
    elif change == "source":
        item.source["current"] = False
    elif change == "stale":
        item.clock.now = 1004.
    elif change == "wall_deadline":
        item.clock.now = 1075.
    elif change == "sim_deadline":
        obs = observation(85., 1001.)
        publish(item, obs)
    else:
        publish(item, obs, active=False)
    assert not item.broker.pending()["pending"]
    with pytest.raises(broker_module.TowerBrokerError):
        resolve(item, request, obs)
    assert item.ledger.records(CONTEXT)["decision"] is None


def test_deadline_fallback_is_signed_divert_and_never_human_approval(tmp_path):
    item = system(tmp_path, JudgeFixture(("human_review",)))
    _, request, _ = human_pending(item)
    item.clock.now = 1075.
    publish(item, observation(85., 1075.))
    status = item.broker.step()
    assert status["status"] == "original_deadline_expired"
    decision = json.loads((item.mailbox/"human-response.json").read_text())
    assert decision["source"] == "timeout_fallback"
    assert decision["effective_action"] == "divert"
    assert decision["request_sha256"] == request["sha256"]
    assert decision["operator_response_received"] is False
    assert decision["executor_command_issued"] is False
    assert len(item.judge.calls) == 1


def test_unknown_readiness_never_grants_capture_but_operator_may_divert(tmp_path):
    item = system(tmp_path, JudgeFixture(("human_review",)))
    _, request, obs = human_pending(item, ready=None)
    with pytest.raises(OperatorResolutionError, match="tower_readiness_unknown"):
        resolve(item, request, obs)
    assert resolve(item, request, obs, action="divert")["effective_action"] == "divert"


@pytest.mark.parametrize("field,value", [("session_id", "other"), ("run_id", "other"), ("plan_sha256", "f"*64)])
def test_cross_context_operator_cannot_consume_signed_pending_request(tmp_path, field, value):
    item = system(tmp_path, JudgeFixture(("human_review",)))
    _, request, obs = human_pending(item)
    with pytest.raises(broker_module.TowerBrokerError, match="cross_context"):
        resolve(item, request, obs, context={**CONTEXT, field: value})
    assert item.ledger.records(CONTEXT)["decision"] is None


def test_provider_latency_does_not_lock_status_or_allow_duplicate_calls(tmp_path):
    entered, release = Event(), Event()
    class SlowJudge(JudgeFixture):
        def judge(self, prompt):
            entered.set()
            assert release.wait(3)
            return super().judge(prompt)
    item = system(tmp_path, SlowJudge())
    routing(item)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(item.broker.step)
        try:
            assert entered.wait(3)
            assert item.broker.pending()["pending"] is False
            assert item.broker.step()["routing_calls_attempted"] == 1
        finally:
            release.set()
        assert result.result(timeout=3)["routing_replies_published"] == 1
    assert len(item.judge.calls) == 1


@pytest.mark.parametrize("end", ["process", "source", "deadline"])
def test_late_provider_answer_is_not_published_after_lifecycle_changes(tmp_path, end):
    entered, release = Event(), Event()
    class SlowJudge(JudgeFixture):
        def judge(self, prompt):
            entered.set()
            assert release.wait(3)
            return super().judge(prompt)
    item = system(tmp_path, SlowJudge())
    routing(item)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(item.broker.step)
        try:
            assert entered.wait(3)
            if end == "process":
                item.process.ended = True
            elif end == "source":
                item.source["current"] = False
            else:
                item.clock.now = 1075.
        finally:
            release.set()
        assert result.result(timeout=3)["status"] == "late_routing_proposal_not_published"
    assert len(item.judge.calls) == 1
    assert not (item.mailbox/"routing-response-1.json").exists()


@pytest.mark.parametrize("change", ["symlink", "oversized", "duplicate_key", "hidden_truth", "source", "human_signature"])
def test_malformed_mailbox_cannot_be_promoted_to_provider_or_operator_authority(tmp_path, change):
    item = system(tmp_path, JudgeFixture(("human_review",)))
    request = routing(item)
    path = item.mailbox/"routing-request-1.json"
    if change == "symlink":
        external = tmp_path/"outside.json"
        path.replace(external)
        path.symlink_to(external)
    elif change == "oversized":
        path.write_bytes(b"x"*(broker_module.MAX_FRAME_BYTES+1))
    elif change == "duplicate_key":
        raw = path.read_bytes()
        path.write_bytes(raw[:-1]+b',"round":1}')
    elif change == "hidden_truth":
        request["hidden_future_tower_recovery_s"] = 50.
        request["sha256"] = tower._digest({key: value for key, value in request.items() if key != "sha256"})
        path.write_bytes(broker_module._canonical(request))
    elif change == "source":
        publish(item, observation(), frame_changes={"source_sha256": "f"*64})
    else:
        item.broker.step()
        response = json.loads((item.mailbox/"routing-response-1.json").read_text())
        item.clock.now = 1001.
        obs = observation(11., 1001.)
        publish(item, obs)
        human = child_tick(item, obs, routing_proposal=response).human_request
        human["signature"] = "f"*64
        broker_module.write_tower_frame(item.mailbox, "human-request.json", human)
    try:
        status = item.broker.step()
        assert status["status"] == "routing_request_rejected_without_retry"
    except (broker_module.TowerBrokerError, ValueError):
        pass
    assert len(item.judge.calls) == (1 if change == "human_signature" else 0)
    assert not (item.mailbox/"human-response.json").exists()


def test_private_directory_exclusive_frames_and_broker_constructor_do_not_allow_reuse(tmp_path):
    item = system(tmp_path)
    with pytest.raises((broker_module.TowerBrokerError, FileExistsError)):
        broker_module.TowerBroker(item.mailbox, CONTEXT["run_id"], scope(), item.process, item.ledger, JudgeFixture(),
            lambda: True, clock=item.clock)
    broker_module.write_tower_frame(item.mailbox, "human-request.json", {"fixture": True})
    with pytest.raises(FileExistsError):
        broker_module.write_tower_frame(item.mailbox, "human-request.json", {"changed": True})
    assert not any(path.name.endswith(".tmp") for path in item.mailbox.iterdir())
    unsafe = tmp_path/"unsafe"
    unsafe.mkdir(mode=0o755)
    os.chmod(unsafe, 0o755)
    with pytest.raises(broker_module.TowerBrokerError, match="private"):
        broker_module.write_tower_frame(unsafe, "latest-observation.json", {})


def test_shared_integrity_key_is_required_but_provider_keys_are_never_child_inputs(tmp_path):
    item = system(tmp_path)
    assert item.broker.status()["provider_keys_serialized"] is False
    with pytest.raises(OperatorResolutionError, match="store_key_mismatch"):
        OperatorResolutionLedger(b"other-explicit-fixture-signing-key-32bytes", store_path=tmp_path/"ledger.sqlite3")

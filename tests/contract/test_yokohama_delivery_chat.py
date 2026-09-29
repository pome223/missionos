import copy
import json
import os
import subprocess
import threading

import pytest

from src.gateway import yokohama_delivery_chat as chat
from src.gateway.yokohama_delivery_chat import YokohamaChatService, requested
from src.intelligence import yokohama_delivery_agents as agents
from src.runtime.task_store import TaskStore
from src.runtime.yokohama_payload import digest

AGENTS = dict(
    provider=agents.PROVIDER,
    planner=dict(agent_name=agents.PLANNER, model_id="deepseek-v4-flash"),
    judge=dict(agent_name=agents.JUDGE, model_id="deepseek-v4-flash"),
    planner_timeout_seconds=30,
    judge_timeout_seconds=20,
)
PLAN = dict(
    supported=True,
    destination_id=agents.DESTINATION,
    summary="船から横浜の配送パッドへ荷物を届けて戻ります。",
    reason="",
)


@pytest.fixture
def service(tmp_path, monkeypatch):
    python = tmp_path / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("fixture")
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_SITL_PYTHON", str(python))
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_OUTPUT_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("RUN_MISSIONOS_YOKOHAMA_DELIVERY_SIM", "1")
    monkeypatch.delenv("MISSIONOS_YOKOHAMA_CITY_MODELS", raising=False)
    monkeypatch.setattr(agents, "configuration", lambda: copy.deepcopy(AGENTS))
    monkeypatch.setattr(
        agents, "plan", lambda text, expected: dict(output=dict(PLAN), invocation={})
    )
    current = YokohamaChatService(TaskStore(str(tmp_path / "tasks.db")))
    monkeypatch.setattr(current, "running_simulators", lambda: [])
    return current


def plan(service, session="one"):
    return service.plan("横浜の配送パッドへ荷物を届けて", session)


def context(task):
    return dict(
        yokohama_delivery_task_id=task["task_id"],
        yokohama_proposal_sha256=task["artifacts"]["yokohama_proposal_sha256"],
    )


def test_only_yokohama_delivery_requests_are_claimed():
    assert requested("横浜の配送パッドへ荷物を届けて")
    assert requested("Deliver the parcel in Yokohama")
    assert not requested("会議室Aへ届けて")
    assert not requested("東京駅から秋葉原へドローンで配送して")
    assert not requested("横浜でGo2に配送して")


def test_agents_must_be_enabled_before_planning(tmp_path, monkeypatch):
    monkeypatch.delenv("RUN_MISSIONOS_YOKOHAMA_AGENTS", raising=False)
    with pytest.raises(ValueError, match="有効"):
        agents.configuration()


def test_unsupported_reading_is_a_clarification(service, monkeypatch):
    monkeypatch.setattr(
        agents,
        "plan",
        lambda text, expected: dict(
            output=dict(supported=False, destination_id="", summary="", reason="配送先は一つです"),
            invocation={},
        ),
    )
    with pytest.raises(ValueError, match="配送先は一つです"):
        plan(service)


def test_proposal_binds_route_judge_limits_and_simulator_arguments(service):
    task = plan(service)
    proposal = task["artifacts"]["yokohama_delivery_proposal"]
    assert task["status"] == "proposed" and service.active is None
    assert proposal["city_models"] == "fixture"
    assert proposal["pad_queue"]["mission_judge"]["may_only_add_wait"] is True
    args = proposal["simulator_arguments"]
    assert args[args.index("--pad-mission-judge") + 1] == "gateway"
    assert "--approve-sitl" not in args


def test_execution_needs_approval_opt_in_and_unchanged_inputs(service, monkeypatch):
    task = plan(service)
    with pytest.raises(ValueError, match="承認"):
        service.execute(task)
    approved = service.approve(task, "one")
    monkeypatch.delenv("RUN_MISSIONOS_YOKOHAMA_DELIVERY_SIM")
    with pytest.raises(ValueError, match="有効"):
        service.execute(approved)
    monkeypatch.setenv("RUN_MISSIONOS_YOKOHAMA_DELIVERY_SIM", "1")
    tampered = copy.deepcopy(approved)
    tampered["artifacts"]["yokohama_delivery_proposal"]["city_models"] = "native"
    with pytest.raises(ValueError, match="変化"):
        service.execute(tampered)
    changed = dict(AGENTS, judge_timeout_seconds=5)
    monkeypatch.setattr(agents, "configuration", lambda: changed)
    with pytest.raises(ValueError, match="変化"):
        service.execute(approved)
    assert service.active is None


def test_running_simulator_or_second_run_is_refused(service, monkeypatch):
    approved = service.approve(plan(service), "one")
    monkeypatch.setattr(service, "running_simulators", lambda: ["missionos-yokohama-x"])
    with pytest.raises(ValueError, match="動作中"):
        service.execute(approved)
    monkeypatch.setattr(service, "running_simulators", lambda: [])
    started = threading.Event()
    monkeypatch.setattr(service, "_worker", lambda *args: started.set())
    first = service.execute(approved)
    assert started.wait(2) and first["status"] == "starting"
    other = service.approve(plan(service, "two"), "two")
    with pytest.raises(ValueError, match="別の横浜配送"):
        service.execute(other)


def test_cross_session_and_forged_context_are_rejected(service):
    task = plan(service)
    with pytest.raises(ValueError):
        service.task(context(task), "two")
    forged = dict(context(task), yokohama_proposal_sha256="0" * 64)
    with pytest.raises(ValueError):
        service.task(forged, "one")


def test_simulator_receives_no_model_keys(service, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-not-for-simulator")
    seen = {}

    class Done:
        pid, returncode = 1, 0

        def poll(self):
            return 0

        def send_signal(self, _):
            pass

    def popen(args, **kwargs):
        seen.update(args=args, env=kwargs["env"])
        return Done()

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(service, "_finish", lambda *args: None)
    task = service.approve(plan(service), "one")
    base = service.outputs / task["task_id"]
    base.mkdir(parents=True)
    (base / "approved.json").write_text("{}")
    service._worker(task["task_id"], service.inputs(), base / "run", base / "approved.json")
    assert "DEEPSEEK_API_KEY" not in seen["env"]
    assert "--approve-sitl" in seen["args"] and "--approval-manifest" in seen["args"]


def judge_request(folder, observation_id="pad_judge_1"):
    request = dict(
        schema="missionos.yokohama-pad-judge-request.v1",
        observation_id=observation_id,
        situation={},
        remaining_wait_seconds=30,
        decisions_remaining=1,
        allowed_actions=["enter", "wait"],
    )
    request["judge_request_id"] = digest(request)
    folder.mkdir(parents=True)
    (folder / "request.json").write_text(json.dumps(request))
    return request


def test_gateway_answers_each_judge_request_once(service, monkeypatch, tmp_path):
    calls = []

    def judge(request, expected):
        calls.append(request["observation_id"])
        return dict(
            judge_request_id=request["judge_request_id"],
            judge_status="valid",
            decision=dict(
                observation_id=request["observation_id"],
                action="wait",
                wait_seconds=5,
                rationale="先行機がまだ近くにいます。",
            ),
            invocation=dict(model_id="deepseek-v4-flash", response_sha256="a" * 64),
        )

    monkeypatch.setattr(agents, "judge", judge)
    task = plan(service)
    proposal = task["artifacts"]["yokohama_delivery_proposal"]
    run = tmp_path / "run"
    judge_request(run / "pad-judge/000")
    judged = set()
    service._judge_pending(task["task_id"], run, proposal, judged)
    service._judge_pending(task["task_id"], run, proposal, judged)
    assert calls == ["pad_judge_1"]
    answer = json.loads((run / "pad-judge/000/response.json").read_text())
    assert answer["decision"]["action"] == "wait"
    record = service.store.get(task["task_id"])["artifacts"]["yokohama_judge_decisions"]
    assert record[0]["rationale"] == "先行機がまだ近くにいます。"


def test_forged_judge_request_stops_the_relay(service, tmp_path):
    task = plan(service)
    run = tmp_path / "run"
    judge_request(run / "pad-judge/000")
    path = run / "pad-judge/000/request.json"
    forged = json.loads(path.read_text())
    forged["remaining_wait_seconds"] = 999
    path.write_text(json.dumps(forged))
    with pytest.raises(ValueError, match="Unbound"):
        service._judge_pending(
            task["task_id"], run, task["artifacts"]["yokohama_delivery_proposal"], set()
        )


@pytest.mark.parametrize("failing", [None, "payload"])
def test_completion_requires_run_and_every_verifier(service, monkeypatch, tmp_path, failing):
    run = tmp_path / "run"
    run.mkdir()
    (run / "result.json").write_text(json.dumps(dict(status="passed", run_id="r")))

    def verifier(args, **kwargs):
        name = args[1].rsplit("verify_yokohama_", 1)[1][:-3]
        status = "failed" if name == failing else "passed"
        (run / f"verification-{name}.json").write_text(json.dumps(dict(status=status)))

    monkeypatch.setattr(subprocess, "run", verifier)
    task = service.approve(plan(service), "one")
    service.store.update(task["task_id"], status="running")
    service._finish(task["task_id"], run, service.inputs(), 0)
    done = service.store.get(task["task_id"])
    assert done["status"] == ("completed" if failing is None else "needs_attention")
    assert set(done["artifacts"]["yokohama_verification"]) == set(chat.VERIFIERS)


def test_chat_turns_plan_approve_and_run(service, monkeypatch):
    monkeypatch.setattr(service, "_worker", lambda *args: None)
    registered = {}

    def register(value, session_id):
        registered.update(value, mission_designer_context_ref="mission_designer_context:x")
        return dict(registered)

    first = service.handle({}, "横浜の配送パッドへ荷物を届けて", "one", {}, register)
    assert first["routed_action"] == "mission_designer_plan" and "/approve" in first["message"]
    approved = service.handle({}, "/approve", "one", registered, register)
    assert approved["operation_result"]["summary"]["status"] == "approved"
    started = service.handle({}, "/run", "one", registered, register)
    assert started["operation_result"]["summary"]["status"] == "starting"
    assert service.handle({}, "今日の天気は？", "one", {}, register) is None


def test_restart_does_not_resume_an_inflight_flight(service):
    task = plan(service)
    service.store.update(task["task_id"], status="running")
    restarted = YokohamaChatService(service.store)
    assert restarted.store.get(task["task_id"])["status"] == "needs_attention"
    assert restarted.active is None and os.environ["RUN_MISSIONOS_YOKOHAMA_DELIVERY_SIM"] == "1"

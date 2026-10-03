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
    monkeypatch.setattr(current, "_kill_group", lambda proc: None)
    return current


def plan(service, session="one"):
    return service.plan("横浜の配送パッドへ荷物を届けて", session)


def context(task):
    return dict(
        yokohama_delivery_task_id=task["task_id"],
        yokohama_proposal_sha256=task["artifacts"]["yokohama_proposal_sha256"],
    )


def service_receipt(run, task):
    """Evidence fixture for the independent service; no simulator is invoked."""
    from hashlib import sha256
    job = run.parent / "vehicle-service"
    job.mkdir(exist_ok=True)
    proposal = task["artifacts"]["yokohama_delivery_proposal"]
    binding = chat._digest(proposal)
    raw = json.dumps(dict(proposal=proposal,
                          approval=task["artifacts"]["yokohama_delivery_approval"])).encode()
    (job / "approved.json").write_bytes(raw)
    result = json.loads((run / "result.json").read_text())
    result.update(decision_backend=proposal["city_models"], cleanup=True)
    (run / "result.json").write_text(json.dumps(result))
    (run / "config.json").write_text(json.dumps({
        "operator_approval_manifest_sha256": sha256(raw).hexdigest(),
    }))
    (job / "status.json").write_text(json.dumps(dict(
        schema="missionos.yokohama-vehicle-service.v1", state="finished", exit_code=0,
        proposal_sha256=binding, approval_manifest_sha256=sha256(raw).hexdigest(),
        city_models=proposal["city_models"], proposal_id=proposal["proposal_id"],
        physical_execution_invoked=False,
        result_sha256=sha256((run / "result.json").read_bytes()).hexdigest(),
    )))


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

        def wait(self, timeout=None):
            return self.returncode

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
    assert seen["args"][1].endswith("run_yokohama_vehicle_service.py")
    assert "--decision-backend" not in seen["args"]


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


@pytest.mark.parametrize("failing", [None, "payload", "vehicle_service"])
def test_completion_requires_run_and_every_verifier(service, monkeypatch, tmp_path, failing):
    run = tmp_path / "run"
    run.mkdir()
    (run / "result.json").write_text(json.dumps(dict(status="passed", run_id="r")))

    def verifier(args, env, identity):
        name = args[1].rsplit("verify_yokohama_", 1)[1][:-3]
        status = "failed" if name == failing else "passed"
        (run / f"verification-{name}.json").write_text(json.dumps(dict(status=status)))

    monkeypatch.setattr(service, "_verify", verifier)
    task = service.approve(plan(service), "one")
    service.store.update(task["task_id"], status="running")
    service_receipt(run, task)
    if failing == "vehicle_service":
        (run.parent / "vehicle-service/status.json").write_text("{}")
    service._finish(task["task_id"], run, service.inputs(), 0)
    done = service.store.get(task["task_id"])
    assert done["status"] == ("completed" if failing is None else "needs_attention")
    assert set(done["artifacts"]["yokohama_verification"]) == set(chat.VERIFIERS) | {"vehicle_service"}


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


@pytest.mark.parametrize("stage", ["reserved", "popen", "running", "verification"])
@pytest.mark.parametrize("cancel", [False, True])
def test_close_joins_worker_and_preserves_stop(service, monkeypatch, stage, cancel):
    import threading

    entered, release = threading.Event(), threading.Event()
    processes = []

    class Process:
        pid, returncode, interrupts = 99, None, 0

        def poll(self):
            return self.returncode

        def send_signal(self, _):
            self.interrupts += 1

        def wait(self, timeout=None):
            self.returncode = -2
            return self.returncode

        def kill(self):
            self.returncode = -9

    def popen(*args, **kwargs):
        proc = Process()
        processes.append(proc)
        if stage == "popen":
            entered.set()
            assert release.wait(5)
        if stage == "verification":
            proc.returncode = 0
        return proc

    original = service._worker

    def worker(*args):
        if stage == "reserved":
            entered.set()
            assert release.wait(5)
        original(*args)

    def follow(*args):
        entered.set()
        assert release.wait(5)
        return 0

    def verify(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return None

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(service, "_worker", worker)
    monkeypatch.setattr(service, "_follow", follow if stage == "running" else lambda *a: 0)
    monkeypatch.setattr(service, "_verify", verify)
    monkeypatch.setattr(service, "_judge_pending", lambda *a: None)
    task = service.approve(plan(service), "one")
    if stage == "verification":
        original_finish = service._finish

        def prepare_finish(identity, folder, inputs, code):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "result.json").write_text('{"status":"passed"}')
            original_finish(identity, folder, inputs, code)

        monkeypatch.setattr(service, "_finish", prepare_finish)
    service.execute(task)
    assert entered.wait(5)
    if cancel:
        service.cancel(service.store.get(task["task_id"]))
    closer = threading.Thread(target=service.close)
    closer.start()
    assert service.stopping.wait(5)
    release.set()
    closer.join(5)
    assert not closer.is_alive()
    assert not service.worker.is_alive()
    assert all(p.poll() is not None for p in processes)
    assert all(p.interrupts <= 1 for p in processes)
    assert service.store.get(task["task_id"])["status"] == (
        "canceled" if cancel else "needs_attention"
    )
    with pytest.raises(ValueError):
        service.execute(task)


def test_close_reaps_real_cpu_worker(service, tmp_path):
    import sys
    import threading
    import time

    task = service.approve(plan(service), "one")
    root = tmp_path / "cpu-runtime"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    marker = root / "started"
    (scripts / "run_yokohama_vehicle_service.py").write_text(
        "from pathlib import Path\nimport time\n"
        f"Path({str(marker)!r}).write_text('ready')\n"
        "while True: time.sleep(0.05)\n"
    )
    service.root = root
    service._kill_group = YokohamaChatService._kill_group
    base = service.outputs / task["task_id"]
    base.mkdir(parents=True)
    manifest = base / "approved.json"
    manifest.write_text("{}")
    service.active = task["task_id"]
    service.worker = threading.Thread(
        target=service._worker,
        args=(task["task_id"], {"python": sys.executable}, base / "run", manifest),
    )
    service.worker.start()
    deadline = time.monotonic() + 5
    while not marker.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    proc = service.process
    service.close()
    assert not service.worker.is_alive()
    assert proc.poll() is not None
    assert service.active is None and service.process is None
    assert service.store.get(task["task_id"])["status"] == "needs_attention"


def test_service_restart_waits_for_old_worker(service, monkeypatch):
    import threading

    release = threading.Event()
    key = str(service.store.db_path.resolve())
    monkeypatch.setattr(chat, "get_task_store", lambda: service.store)
    monkeypatch.setattr(chat, "_services", {key: service})
    service.worker = threading.Thread(target=release.wait)
    service.worker.start()
    service.stopping.set()
    try:
        with pytest.raises(ValueError, match="停止中"):
            chat.service()
    finally:
        release.set()
        service.worker.join(5)
    restarted = chat.service()
    assert restarted is not service and not restarted.stopping.is_set()


@pytest.mark.parametrize("verification", [False, True])
@pytest.mark.parametrize("before_registration", [False, True])
@pytest.mark.parametrize("cancel_only", [False, True])
def test_stop_reaps_real_process_group_with_stubborn_descendant(
    service, tmp_path, monkeypatch, verification, before_registration, cancel_only
):
    import os
    import sys
    import threading
    import time

    task = service.approve(plan(service), "one")
    service.root = tmp_path / "process-tree"
    scripts = service.root / "scripts"
    scripts.mkdir(parents=True)
    service._kill_group = YokohamaChatService._kill_group
    if cancel_only:
        original_reap = service._reap
        monkeypatch.setattr(
            service, "_reap", lambda proc, grace=180: original_reap(proc, grace=0.2)
        )
    marker = service.root / "child.pid"
    child = (
        "import os,signal,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "while True: time.sleep(.05)\n"
    )
    parent = (
        "import subprocess,signal,sys,time\n"
        + ("signal.signal(signal.SIGINT,signal.SIG_IGN)\n" if verification or cancel_only else "")
        + f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
        + "while True: time.sleep(.05)\n"
    )
    filename = (f"verify_yokohama_{chat.VERIFIERS[0]}.py" if verification
                else "run_yokohama_vehicle_service.py")
    (scripts / filename).write_text(parent)
    base = service.outputs / task["task_id"]
    base.mkdir(parents=True)
    run = base / "run"
    run.mkdir()
    (run / "result.json").write_text('{"status":"passed"}')
    manifest = base / "approved.json"
    manifest.write_text("{}")
    service.active = task["task_id"]
    service.store.update(task["task_id"], status="running")
    target = service._finish if verification else service._worker
    args = (
        (task["task_id"], run, {"python": sys.executable}, 0)
        if verification
        else (task["task_id"], {"python": sys.executable}, run, manifest)
    )
    release_registration = threading.Event()
    if before_registration:
        original_popen = subprocess.Popen

        def delayed_popen(*args, **kwargs):
            proc = original_popen(*args, **kwargs)
            assert release_registration.wait(5)
            return proc

        monkeypatch.setattr(subprocess, "Popen", delayed_popen)
    service.worker = threading.Thread(target=target, args=args)
    service.worker.start()
    deadline = time.monotonic() + 5
    while not marker.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    child_pid = int(marker.read_text())
    started = time.monotonic()
    if cancel_only:
        service.cancel(service.store.get(task["task_id"]))
        release_registration.set()
        service.worker.join(5)
    else:
        closer = threading.Thread(target=service.close)
        closer.start()
        assert service.stopping.wait(5)
        release_registration.set()
        closer.join(5)
        assert not closer.is_alive()
    assert time.monotonic() - started < 5
    assert not service.worker.is_alive()
    assert service.verification_process is None
    assert service.store.get(task["task_id"])["status"] == (
        "canceled" if cancel_only else "needs_attention"
    )
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        pass
    else:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, text=True
        )
        assert not state.stdout.strip() or state.stdout.strip().startswith("Z")


def test_map_reservation_fences_chat_before_container_creation(service, monkeypatch):
    from src.gateway.yokohama_map import MapService, MapRequest
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_MAP_BACKEND", "fixture")
    monkeypatch.setenv("RUN_MISSIONOS_YOKOHAMA_MAP_FIXTURE", "1")
    map_service = MapService(service.store, service.outputs / "map")
    task = map_service.select(MapRequest(session_id="same_gateway_session", goal_xy_m=[217.894, -115.179]), "operator")
    body = MapRequest(session_id="same_gateway_session", task_id=task["task_id"], plan_sha256=task["artifacts"]["plan_sha256"])
    map_service.action("approve", body, "operator")
    entered, release = threading.Event(), threading.Event()
    original = map_service._run
    def blocked(*args):
        entered.set()
        release.wait(5)
        return original(*args)
    monkeypatch.setattr(map_service, "_run", blocked)
    approved_chat = service.approve(plan(service), "one")
    try:
        map_service.action("execute", body, "operator")
        assert entered.wait(2)
        assert service.running_simulators() == []  # dispatch must fence the pre-Docker window
        with pytest.raises(ValueError, match="別の横浜配送"):
            service.execute(approved_chat)
    finally:
        map_service.action("cancel", body, "operator")
        release.set()
        map_service.worker.join(10)
    assert service.dispatch.owner is None


def test_chat_reservation_fences_map_preparation(service, monkeypatch):
    from src.gateway.yokohama_map import MapService, MapRequest
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_MAP_BACKEND", "fixture")
    monkeypatch.setenv("RUN_MISSIONOS_YOKOHAMA_MAP_FIXTURE", "1")
    map_service = MapService(service.store, service.outputs / "map")
    task = map_service.select(MapRequest(session_id="same_gateway_session", goal_xy_m=[217.894, -115.179]), "operator")
    body = MapRequest(session_id="same_gateway_session", task_id=task["task_id"], plan_sha256=task["artifacts"]["plan_sha256"])
    map_service.action("approve", body, "operator")
    # Existing chat tests stub the worker. Preserve its dispatch reservation until explicit cleanup.
    monkeypatch.setattr(service, "_worker", lambda *args: None)
    started = service.execute(service.approve(plan(service), "one"))
    try:
        with pytest.raises(ValueError, match="配送"):
            map_service.action("execute", body, "operator")
    finally:
        service.dispatch.release(started["task_id"])


@pytest.mark.parametrize("failure", ["event", "thread"])
def test_chat_preparation_failure_releases_shared_dispatch(service, monkeypatch, failure):
    approved = service.approve(plan(service), "one")
    if failure == "event":
        original = service.store.append_event
        def fail(*args, **kwargs):
            if kwargs.get("event_type") == "yokohama_dispatch_reserved":
                raise OSError("fixture database failure")
            return original(*args, **kwargs)
        monkeypatch.setattr(service.store, "append_event", fail)
    else:
        monkeypatch.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(OSError("fixture thread failure")))
    with pytest.raises(OSError):
        service.execute(approved)
    assert service.dispatch.owner is None and service.active is None and service.worker is None


@pytest.mark.parametrize("backend,invoked,label", [("jev", False, "Jev contract fixture"), ("deepseek", True, "DeepSeek")])
def test_response_reports_fixture_and_real_judge_separately(service, backend, invoked, label):
    task = plan(service)
    task["artifacts"]["yokohama_delivery_proposal"]["agents"]["judge"]["backend"] = backend
    task["artifacts"]["yokohama_judge_decisions"] = [
        dict(judge_status="valid", rationale="Adapter template", invocation={"inference_invoked": invoked})
    ]
    response = service.response(task, {}, "status")
    assert response["llm_judgment_invoked"] is invoked
    assert label in response["message"]


@pytest.mark.parametrize("changed_source", [
    "scripts/yokohama_altitude_contract.py",
    "scripts/smoke_px4_gazebo_sitl_mission_upload.py",
])
def test_altitude_transport_dependency_change_invalidates_chat_approval(service,tmp_path,monkeypatch,changed_source):
    root=tmp_path/"source-fixture"
    for name in chat.SOURCES:
        target=root/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((service.root/name).read_bytes())
    service.root=root
    approved=service.approve(plan(service),"one")
    changed=root/changed_source
    changed.write_bytes(changed.read_bytes()+b"\n# changed transport implementation\n")
    started=[]
    monkeypatch.setattr(service,"_worker",lambda *args: started.append(args))
    with pytest.raises(ValueError,match="変化"):
        service.execute(approved)
    assert not started and service.active is None

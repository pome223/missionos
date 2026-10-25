"""Live-scope composition with real HTTP handlers and FIFO/ledger, no models or plant."""
from copy import deepcopy
import os
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.runtime import starship_m3_live as m3
from src.runtime.starship_m3_inflight import (
    BASE_CASE, CASE, LiveApprovalHost, NOMINAL_BURN_S, binding, validate_binding, worker_runtime,
)
from src.runtime.starship_replanning import digest
from src.runtime.starship_inflight_runtime import WorkerApprovalActor
from src.gateway.starship_inflight_approval import InflightApprovalService, build_inflight_approval_router, PREFIX

TOKEN = "explicit-scripted-test-operator-token-not-secret"


def bound(authority=None):
    authority = authority or m3.contract("live", CASE)
    value = binding(authority, run_id="flight1", plan_sha256="a"*64,
        source_sha256={"source.py": "b"*64}, started=time.monotonic())
    return authority, value


def test_new_scope_is_explicit_canonical_and_default_does_not_gain_it():
    old = m3.contract("live")
    a, b = bound()
    assert not old["inflight_scope_amendment_enabled"] and "inflight_policy" not in old
    assert a["m2_authority"]["case"] == a["prefix_authority"]["case"] == BASE_CASE
    assert a["inflight_policy"]["initial_scope"]["commands"] == ["select_nominal", "keep_reserved_return"]
    assert a["maximum_llm_calls"] is None and a["maximum_model_calls"] is None
    assert a["maximum_human_inflight_commands"] == 0
    assert validate_binding(b, a, run_id="flight1", sources=b["source_sha256"]) == b["inflight_contract"]
    assert "fixture_policy" not in b["inflight_contract"]
    assert m3.SCENARIOS["sixdof_m3_release_fault"] == "release_fault"


@pytest.mark.parametrize("path,value", [
    (("run_id",), "other"), (("authority_sha256",), "c"*64),
    (("source_sha256",), {"other": "b"*64}), (("plan_sha256",), "not-a-hash"),
    (("inflight_contract", "initial_scope", "commands"), ["select_nominal", "keep_reserved_return", "select_next_orbit"]),
    (("inflight_contract", "bounds", "reservation_cutoff_s"), 99999.),
    (("inflight_contract", "bounds", "wall_deadline_s"), 99999.),
])
def test_binding_tamper_rejected(path, value):
    a, b = bound()
    target = b
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        validate_binding(b, a, run_id="flight1", sources={"source.py": "b"*64})


def test_actual_nominal_cutoff_rechecked_not_widened():
    from src.runtime.starship_inflight_executor import validate_inflight_executor_contract
    a, b = bound()
    with pytest.raises(ValueError, match="approval_must_protect_nominal_cutoff"):
        validate_inflight_executor_contract(b["inflight_contract"], a["m2_authority"]["return_authority"],
            run_id="flight1-return", nominal_burn_s=NOMINAL_BURN_S+1)


def test_credentials_never_cross_worker_environment(monkeypatch):
    from src.runtime.starship_mission_control import worker_environment
    for key in ("MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_TOKEN", "MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_ID",
                "MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_SOURCE", "DEEPSEEK_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(key, "fixture-secret")
    assert not any(key in worker_environment() for key in os.environ if key.startswith("MISSIONOS_STARSHIP_INFLIGHT_OPERATOR"))
    assert "DEEPSEEK_API_KEY" not in worker_environment()


@pytest.fixture(params=[CASE, "inflight_op_scope_01"])
def runtime(tmp_path, monkeypatch, request):
    monkeypatch.setenv("MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setenv("MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_ID", "scripted-test-operator")
    monkeypatch.setenv("MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_SOURCE", "scripted_test_driver")
    authority, value = bound(m3.contract("live", request.param))
    service = InflightApprovalService()
    host = LiveApprovalHost(tmp_path, value, signer=digest, service=service)
    fds = tuple(os.dup(fd) for fd in host.fds)
    worker = worker_runtime(value, authority, run_id="flight1", sources=value["source_sha256"],
        request_fd=fds[0], grant_fd=fds[1], telemetry_fd=fds[2])
    for pipe in host.child_ends:
        pipe.close()
    app = FastAPI()
    app.include_router(build_inflight_approval_router(service))
    client = TestClient(app)
    contract = value["inflight_contract"]
    state = {"run_active": True, "simulation_time_s": 1600., "revision": 0,
        "scope": deepcopy(contract["initial_scope"]), "reservation_sha256": "c"*64,
        "observation": {"state_sha256": "d"*64, "simulation_time_s": 1600., "wall_time_s": time.monotonic()}}
    actions = []
    def command(action, current, grant):
        actions.append(action)
        state.update(scope=deepcopy(grant["scope"]), revision=grant["revision"], reservation_sha256="e"*64)
        return {"applied": True, "revision": grant["revision"], "reservation_sha256": "e"*64}
    actor = WorkerApprovalActor(context=contract["context"], scope=contract["initial_scope"], revision=0,
        request_writer=worker["request_writer"], grant_reader=worker["grant_reader"], bounds=contract["bounds"],
        admission=lambda action, state: {"accepted": True, "candidate_sha256": "f"*64}, command=command)
    def telemetry():
        state["observation"]["wall_time_s"] = time.monotonic()
        state["observation"]["simulation_time_s"] = state["simulation_time_s"]
        worker["snapshot_sink"](deepcopy(state))
        frame = host.telemetry_read.poll()
        assert frame is not None
        host.telemetry.accept(frame)
    yield host, actor, client, state, actions, telemetry
    worker["request_writer"].close()
    worker["grant_reader"].close()
    os.close(fds[2])
    host.close()


@pytest.mark.parametrize("choice", ["approve", "reject", "expire"])
def test_production_http_and_fifo_one_scope_request(runtime, choice):
    host, actor, client, state, actions, telemetry = runtime
    base = PREFIX+"/runs/flight1-return/missions/flight1/vehicles/ship"
    headers = {"Authorization": "Bearer "+TOKEN}
    assert client.get(base+"/pending", headers=headers).status_code == 409
    telemetry()
    assert client.get(base+"/pending", headers=headers).json()["request"] is None
    actor.request_change(request_id="flight1-return-inflight-1", candidate_sha256="f"*64, state=deepcopy(state))
    host.bridge.tick()
    assert client.get(base+"/pending").status_code == 401
    shown = client.get(base+"/pending", headers=headers)
    assert shown.status_code == 200 and TOKEN not in shown.text
    request = shown.json()["request"]
    body = {"request_sha256": request["sha256"], "observed_state_sha256": request["observed_state_sha256"],
            "candidate_sha256": request["candidate_sha256"]}
    endpoint = base+"/requests/"+request["request_id"]+"/"+("approve" if choice == "expire" else choice)
    if choice == "expire":
        state["simulation_time_s"] = actor.bounds["simulation_deadline_s"]
        telemetry()
        assert client.post(endpoint, headers=headers, json=body).status_code == 409
    else:
        response = client.post(endpoint, headers=headers, json=body)
        assert response.status_code == 200 and response.json()["executor_command_issued"] is False
        assert not actions
    host.bridge.tick()
    actor.tick(deepcopy(state))
    signed = host.ledger.status(request["request_id"])
    assert signed["consumption"]["approved"] is (choice == "approve")
    assert client.post(endpoint, headers=headers, json=body).status_code == 409
    assert host.operator_source == "scripted_test_driver"
    if choice == "approve":
        assert actions == ["select_next_orbit"] and state["revision"] == 1
        state["simulation_time_s"] += .2
        telemetry()
        assert actor.tick(deepcopy(state))["status"] == "reservation_confirmed"
    else:
        assert actions == [] and state["revision"] == 0 and actor.status == "fallback_preserved"
    assert not actor.record()["physical_return_verified"]
    assert not actor.record()["host_signature_verified_by_worker"]
    assert not host.bridge.record()["executor_command_issued"]


def test_canonical_live_seam_rejected_before_physics_without_outer_grant(tmp_path):
    from src.runtime.starship_m2_runner import run_simulation
    a, b = bound()
    with pytest.raises(ValueError, match="m3_live_inflight_outer_authority_required"):
        run_simulation({}, BASE_CASE, tmp_path/"never-created", mode="live", authority=a["m2_authority"],
            concurrent_prefix=True, expected_prefix_authority=a["prefix_authority"],
            inflight_runtime={"inflight": b["inflight_contract"], "request_writer": None, "grant_reader": None,
                "snapshot_sink": lambda _: None, "clock": time.monotonic})
    assert not (tmp_path/"never-created").exists()


def test_return_broker_constructs_agent_with_exact_approved_scope(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.intelligence import starship_m2_broker as broker
    authority, value = bound()
    captured = []
    def agent(envelope, active, *, inflight_contract=None):
        captured.append(deepcopy(inflight_contract))
        return SimpleNamespace()
    monkeypatch.setattr(broker, "ReplanningAgent", agent)
    broker.serve_phase(tmp_path, authority["m2_authority"], "return", "flight1",
        SimpleNamespace(poll=lambda: 0), lambda: True, inflight_contract=value["inflight_contract"])
    assert captured == [value["inflight_contract"]]
    assert "select_next_orbit" not in captured[0]["initial_scope"]["commands"]


def test_simulator_cli_refuses_operator_environment_before_loading_authority(tmp_path):
    from pathlib import Path
    import subprocess
    import sys
    from src.runtime.starship_mission_control import worker_environment
    environment = worker_environment()
    environment["MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_TOKEN"] = TOKEN
    result = subprocess.run([sys.executable, "scripts/run_starship_m3.py", "--approve-simulation",
        "--mode", "live", "--case", CASE, "--authority-file", str(tmp_path/"absent.json"),
        "--mailbox", str(tmp_path/"absent-mailbox"), "--run-id", "fixture-run",
        "--output-dir", str(tmp_path/"absent-output")], cwd=Path(__file__).resolve().parents[2],
        env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "host_operator_credentials_forbidden" in result.stderr and TOKEN not in result.stderr
    assert not (tmp_path/"absent-output").exists()


def test_real_child_process_inherits_only_explicit_pipe_end_and_clean_environment(monkeypatch):
    import sys
    from src.runtime.starship_mission_control import _run_simulator
    from src.runtime.starship_inflight_runtime import approval_pipe
    reader, writer = approval_pipe()
    monkeypatch.setenv("MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-only-not-secret")
    code = ("import os,json; assert 'DEEPSEEK_API_KEY' not in os.environ; "
            "assert not any(k.startswith('MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_') for k in os.environ); "
            f"os.write({writer.fd}, b'{{\"probe\":true}}\\n')")
    try:
        assert _run_simulator([sys.executable, "-c", code], timeout=10, pass_fds=(writer.fd,)) == 0
        assert reader.poll() == {"probe": True}
    finally:
        reader.close()
        writer.close()


def test_scoped_cli_composes_final_artifact_once_after_host_evidence(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from scripts import run_starship_m3 as cli
    from src.runtime import starship_m3_inflight as host_module, starship_m2_runner
    a, b = bound()
    frozen = {"source.py": "b"*64}
    monkeypatch.setattr(cli, "sources", lambda: frozen)
    monkeypatch.setattr(cli, "return_sources", lambda: {})
    monkeypatch.setattr(m3, "sources", lambda: frozen)
    monkeypatch.setattr(m3, "validate_worker", lambda *a, **k: None)
    runtime = {"inflight": b["inflight_contract"]}
    monkeypatch.setattr(host_module, "worker_runtime", lambda *a, **k: runtime)
    monkeypatch.setattr(starship_m2_runner, "run_simulation", lambda *a, **k: {"return_study": None, "handoff": None})
    evidence = {"schema": "fixture-host-evidence"}
    def completed(directory, bound_value):
        assert (tmp_path/"results"/"flight-study.json").exists()
        assert not (tmp_path/"results"/"study.json").exists()
        assert bound_value == b
        return evidence
    monkeypatch.setattr(host_module, "await_host_evidence", completed)
    monkeypatch.setattr(cli, "verify", lambda *a, **k: {"passed": True, "case_accepted": True, "issues": []})
    path = tmp_path/"binding.json"
    path.write_text(json.dumps({**b, "signature": "fixture-host-signature"}))
    args = SimpleNamespace(case=CASE, output_dir=tmp_path/"results", mode="live", mailbox=tmp_path,
        run_id="flight1", inflight_binding=path, inflight_request_fd=8, inflight_grant_fd=9, inflight_telemetry_fd=10)
    assert cli.execute(args, a) == 0
    final = json.loads((args.output_dir/"study.json").read_text())
    assert final["inflight_host_evidence"] == evidence
    from src.runtime.starship_artifacts import read_verified_input, CHECKPOINT_SCHEMA
    reference = json.loads((args.output_dir/"flight-study.json").read_text())
    assert reference["schema"] == CHECKPOINT_SCHEMA
    assert "inflight_host_evidence" not in read_verified_input(args.output_dir/"flight-study.json")
    manifest = json.loads((args.output_dir/"manifest.json").read_text())
    from hashlib import sha256
    assert manifest["files"]["study.json"] == sha256((args.output_dir/"study.json").read_bytes()).hexdigest()


def test_budget_failure_preserves_old_verdict_and_replaces_success(tmp_path, monkeypatch):
    import json
    from scripts import run_starship_m3 as cli
    from src.runtime.starship_runtime_budget import BudgetExceeded
    class Budget:
        def __init__(self, output, authority):
            self.output = output
            self.failure = {"reason": "minimum_free_bytes"}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            raise BudgetExceeded(self.failure)
        def failure_artifact(self):
            return self.failure
    monkeypatch.setattr(cli, "RuntimeBudget", Budget)
    monkeypatch.setattr(cli, "validate_worker", lambda *a, **k: None)
    def completed(args, authority):
        args.output_dir.mkdir()
        cli.write_verified_input(args.output_dir/"verification.json", {"passed": True, "case_accepted": True, "issues": []})
        return 0
    monkeypatch.setattr(cli, "execute", completed)
    authority = tmp_path/"authority.json"
    authority.write_text(json.dumps({"inflight_scope_amendment_enabled": False}))
    output = tmp_path/"output"
    assert cli.main(["--approve-simulation", "--mode", "fixture", "--case", "orbital_update",
        "--authority-file", str(authority), "--mailbox", str(tmp_path), "--run-id", "fixture",
        "--output-dir", str(output)]) == 4
    assert json.loads((output/"verification-before-budget-failure.json").read_text())["passed"] is True
    assert json.loads((output/"verification.json").read_text())["passed"] is False


def test_host_evidence_reader_waits_for_atomic_publication(tmp_path, monkeypatch):
    from pathlib import Path
    from threading import Event, Thread
    from src.runtime import starship_m3_inflight as host_module
    _, b = bound()
    value = {"schema": "missionos.starship_m3_inflight_host_evidence.v1",
        "binding_sha256": digest(b), "failure": None}
    flushed, allow_publish, reader_waiting = Event(), Event(), Event()
    original_replace, original_sleep = Path.replace, time.sleep
    def pause_replace(path, destination):
        if path.name == "inflight-host-result.pending.json":
            flushed.set()
            assert allow_publish.wait(2)
        return original_replace(path, destination)
    def reader_sleep(delay):
        reader_waiting.set()
        original_sleep(delay)
    monkeypatch.setattr(Path, "replace", pause_replace)
    monkeypatch.setattr(host_module.time, "sleep", reader_sleep)
    failures, results = [], []
    def publish():
        try:
            host_module.publish_host_evidence(tmp_path, value)
        except BaseException as error:
            failures.append(error)
    def read():
        try:
            results.append(host_module.await_host_evidence(tmp_path, b, timeout_s=2))
        except BaseException as error:
            failures.append(error)
    publisher = Thread(target=publish)
    publisher.start()
    assert flushed.wait(2)
    assert not (tmp_path/"inflight-host-result.json").exists()
    reader = Thread(target=read)
    reader.start()
    assert reader_waiting.wait(2)
    assert results == []
    allow_publish.set()
    publisher.join(2)
    reader.join(2)
    assert not publisher.is_alive() and not reader.is_alive()
    assert not failures and results == [value]

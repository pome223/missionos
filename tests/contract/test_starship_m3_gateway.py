"""M3 chat approval/process/receipt wiring, with no provider or numerical flight."""

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.gateway import starship_chat
from src.runtime import starship_m3_live as m3
from src.runtime import starship_mission_control as control
from src.runtime.starship_replanning import digest
from src.runtime.starship_sixdof_catalog import SIXDOF_PROFILE

SCENARIO = "sixdof_m3_orbital_comparison"


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "fixture")
    for key in ("DEEPSEEK_API_KEY", "TYPESAFE_API_KEY", "JEV_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(control, "_sources", lambda _: {SIXDOF_PROFILE: "a" * 64})
    from src.runtime import starship_return_feasibility
    monkeypatch.setattr(starship_return_feasibility, "readiness", lambda _: (None, None, None))
    return control.StarshipMissionService(tmp_path, planner=lambda _: {
        "proposal": {"scenario": SCENARIO, "rationale": "unit boundary", "uncertainties": ["no flight"]}})


def start_fixture(service, monkeypatch):
    planned = service.plan("operator", "Starship M3")
    plan = planned["plan"]
    ref = ("operator", plan["id"], plan["sha256"])
    service.approve(*ref)
    processes, threads = [], []
    monkeypatch.setattr(control.subprocess, "Popen", lambda args, **kwargs:
                        processes.append((args, kwargs)) or SimpleNamespace(pid=os.getpid()))
    monkeypatch.setattr(control, "Thread", lambda **kwargs:
                        threads.append(kwargs) or SimpleNamespace(start=lambda: None))
    running = service.execute(*ref)
    return plan, ref, running, processes, threads


def test_m3_scenario_catalog_and_sources_use_new_boundary(monkeypatch):
    assert SCENARIO in control.CATALOG
    monkeypatch.setattr(m3, "sources", lambda: {"unit-source": "c" * 64})
    assert control._sources(SCENARIO) == {"unit-source": "c" * 64}


def test_one_signed_grant_binds_all_four_phase_mailboxes(service, monkeypatch):
    planned = service.plan("operator", "Starship M3")
    plan = planned["plan"]
    ref = ("operator", plan["id"], plan["sha256"])
    assert plan["backend"] == "starship_concurrent_mission_control"
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval_required"):
        service.execute(*ref)
    approved = service.approve(*ref)
    assert approved["approval"]["scope"] == m3.SCOPE
    assert approved["approval"]["mission_envelope"] == plan["mission_envelope"]
    assert approved["approval"]["physical_execution_authorized"] is False
    assert approved["approval"]["authenticated_operator_identity"] is False
    _, ref, running, processes, threads = start_fixture(service, monkeypatch)
    run_id = running["execution"]["run_id"]
    folder = service.root / ("run-" + run_id) / "supervision"
    assert {p.name for p in folder.iterdir() if p.is_dir()} == {"ascent", "booster", "deployment", "return"}
    assert json.loads((folder / "authority.json").read_text()) == {
        "run_id": run_id, "authority_sha256": digest(plan["mission_envelope"]["m2_authority"])}
    assert json.loads((folder / "m3-authority.json").read_text()) == {
        "run_id": run_id, "authority_sha256": digest(plan["mission_envelope"])}
    assert len(processes) == 1
    assert processes[0][0][1].endswith("scripts/run_starship_mission_worker.py")
    host = [row for row in threads if row["target"].__module__ == "src.intelligence.starship_m3_broker"]
    assert len(host) == 1 and host[0]["args"][1] == plan["mission_envelope"]
    assert host[0]["args"][0] == folder and host[0]["args"][2] == run_id
    with pytest.raises(control.StarshipMissionError):
        service.execute(*ref)


def test_m3_worker_environment_never_inherits_host_credentials(monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "live")
    keys = {"DEEPSEEK_API_KEY", "TYPESAFE_API_KEY", "JEV_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"}
    for name in keys:
        monkeypatch.setenv(name, "unit-fixture-not-a-key")
    authority = m3.contract("live")
    assert authority["maximum_model_calls"] is None
    assert authority["maximum_jev_calls"] == 26 and authority["maximum_llm_calls"] is None
    environment = control.process_environment({"scenario": SCENARIO, "mission_envelope": authority})
    assert not keys & environment.keys()
    assert environment["MISSIONOS_LLM_BACKEND"] == "off"


@pytest.mark.parametrize("fault", ["scope", "inner_authority", "sources", "mode"])
def test_preflight_rejects_changed_grant_before_worker_spawn(service, monkeypatch, fault):
    plan = service.plan("operator", "Starship M3")["plan"]
    ref = ("operator", plan["id"], plan["sha256"])
    service.approve(*ref)
    if fault == "sources":
        monkeypatch.setattr(control, "_sources", lambda _: {SIXDOF_PROFILE: "b" * 64})
    elif fault == "mode":
        monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "live")
    else:
        state = service._load("operator")
        if fault == "scope":
            state["approval"]["scope"] = "local_simulation_only"
        else:
            state["approval"]["mission_envelope"]["m2_authority"]["maximum_model_calls"] += 1
        service._save("operator", state)
    monkeypatch.setattr(control.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("unapproved worker"))
    with pytest.raises(control.StarshipMissionError):
        service.execute(*ref)


@pytest.mark.parametrize("accepted", [True, False])
def test_worker_calls_m3_cli_then_reverifies_signed_artifacts(service, monkeypatch, accepted):
    plan, ref, running, _, _ = start_fixture(service, monkeypatch)
    run_id = running["execution"]["run_id"]
    output = service.root / ("run-" + run_id) / "results"
    verdict = {"passed": True, "case_accepted": accepted, "issues": [],
               "case_issues": [] if accepted else ["flight_outcome_unresolved"], "m3_completed": False}
    checks = []

    def worker(args, timeout, *, pass_fds=()):
        assert pass_fds == ()  # Ordinary M3 flights carry no in-flight approval channels.
        assert args[1].endswith("scripts/run_starship_m3.py")
        assert timeout == 2400
        assert args[args.index("--case") + 1] == "orbital_update"
        assert args[args.index("--run-id") + 1] == run_id
        assert args[args.index("--mailbox") + 1].endswith("/supervision")
        envelope = json.loads(Path(args[args.index("--authority-file") + 1]).read_text())
        assert envelope == plan["mission_envelope"]
        output.mkdir()
        profile = json.loads((control.REPO / SIXDOF_PROFILE).read_text())
        (output / "study.json").write_text(json.dumps({"profile": profile, "authority": envelope}))
        (output / "verification.json").write_text(json.dumps({"passed": False}))
        (output / "report.html").write_text("<p>unit stub: no numerical flight</p>")
        (output / "manifest.json").write_text(json.dumps({"files": {}}))
        return 0 if accepted else 3

    def verify(study, **kwargs):
        checks.append(kwargs)
        assert study["authority"] == kwargs["expected_authority"] == plan["mission_envelope"]
        assert kwargs["expected_sources"] == plan["source_sha256"]
        assert kwargs["expected_return_sources"] == {"return-fixture": "b" * 64}
        assert kwargs["expected_run_id"] == run_id
        return deepcopy(verdict)

    from src.runtime import starship_replanning
    monkeypatch.setattr(starship_replanning, "source_hashes", lambda: {"return-fixture": "b" * 64})
    monkeypatch.setattr(control, "_run_simulator", worker)
    monkeypatch.setattr(m3, "verify", verify)
    assert control.execute_worker(service.root, run_id) == 0
    result = service.status(ref[0], ref[1])
    assert result["status"] == ("verified" if accepted else "unresolved")
    assert result["mission_completed"] is False and result["physical_execution"] is False
    assert result["execution"]["process_role"] == "credential_free_m3_simulator_worker"
    assert result["execution"]["worker_receipt_verified"] is True
    assert result["execution"]["simulator_provider_credentials_present"] is False
    assert len(checks) == 1
    stored = json.loads(service.read_artifact(ref[0], ref[1], "verification.json").read_text())
    assert stored == verdict  # Host replaced the worker's untrusted verdict.
    manifest = json.loads(service.read_artifact(ref[0], ref[1], "manifest.json").read_text())
    assert manifest["files"]["verification.json"] == sha256((output / "verification.json").read_bytes()).hexdigest()


def test_worker_rejects_profile_change_before_invoking_m3_verifier(service, monkeypatch):
    _, ref, running, _, _ = start_fixture(service, monkeypatch)
    run_id = running["execution"]["run_id"]
    output = service.root / ("run-" + run_id) / "results"

    def worker(_args, **_kwargs):
        output.mkdir()
        (output / "study.json").write_text(json.dumps({"profile": {"changed": True}}))
        return 0

    monkeypatch.setattr(control, "_run_simulator", worker)
    monkeypatch.setattr(m3, "verify", lambda *_a, **_kw: pytest.fail("unbound study"))
    assert control.execute_worker(service.root, run_id) == 2
    result = service.status(ref[0], ref[1])
    assert result["status"] == "failed"
    assert result["execution"]["failure_reason"] == "m3_execution_input_binding_mismatch"


def test_plan_text_describes_the_actual_m3_grant(service):
    planned = service.plan("operator", "Starship M3")
    response = starship_chat._response("Starship M3", "plan", planned, "operator")
    assert "M3管制" in response["message"]
    assert "上昇・ブースター・放出・帰還" in response["message"]
    assert "帰還時刻は変更不可" not in response["message"]
    assert response["progress_counted"] is False


def test_http_smoke_cli_preserves_m3_unresolved_exit_code(monkeypatch, tmp_path):
    path = control.REPO / "scripts/smoke_starship_chat_gateway.py"
    spec = importlib.util.spec_from_file_location("m3_chat_smoke", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "run", lambda *_a: {"m3": {"case_accepted": False}})
    monkeypatch.setattr(script.sys, "argv", [str(path), "--port", "12345", "--scenario", SCENARIO,
                                            "--output-dir", str(tmp_path)])
    assert script.main() == 3

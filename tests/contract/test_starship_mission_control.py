"""Authority binding, process isolation and actual local simulation receipts."""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from src.runtime import starship_mission_control as control


def fixture_proposal(_text):
    return {"proposal": {"scenario": "dispenser_comparison", "rationale": "Test fixture.",
                         "uncertainties": ["Synthetic conditions only."]},
            "invocation": {"model_inference_invoked": False, "provider": "fixture"}}


@pytest.fixture
def service(tmp_path):
    return control.StarshipMissionService(tmp_path, planner=fixture_proposal)


def plan(service):
    state = service.plan("operator-session", "Starship dispenser comparison")
    return ("operator-session", state["plan"]["id"], state["plan"]["sha256"])


def test_no_approval_no_subprocess(service, monkeypatch):
    ref = plan(service)
    def forbidden(*args, **kwargs):
        pytest.fail("A proposal must not invoke a worker")
    monkeypatch.setattr(control.subprocess, "Popen", forbidden)
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval_required"):
        service.execute(*ref)
    assert service.current(ref[0])["execution"] == {}


@pytest.mark.parametrize("bad", [{"execute": True}, {"scenario": "shell_command"}])
def test_planner_cannot_extend_catalog_or_authority(service, bad):
    result = fixture_proposal("")
    result["proposal"].update(bad)
    service.planner = lambda _: result
    with pytest.raises(control.StarshipMissionError, match="planner_proposal_rejected"):
        plan(service)


@pytest.mark.parametrize("expected", [True, 0, "hardware_live", "sixdof_gimbal_step ", {}, []])
def test_expected_scenario_restriction_is_validated_before_planner_or_state(service, monkeypatch, expected):
    service.planner = lambda _: pytest.fail("Invalid restriction must not call a planner")
    monkeypatch.setattr(control, "_sources", lambda _: pytest.fail("Invalid restriction must not read source authority"))
    with pytest.raises(control.StarshipMissionError, match="invalid_expected_scenario"):
        service.plan("selected-session", "Starship gimbal test", expected_scenario=expected)
    assert service.current("selected-session") is None


def test_misbehaving_planner_cannot_persist_an_unexpected_selected_scenario(service, monkeypatch):
    response = fixture_proposal("")
    response["proposal"]["scenario"] = "sixdof_engine_out"
    service.planner = lambda _: response
    monkeypatch.setattr(control, "_sources", lambda _: pytest.fail("Mismatch must precede source/plan construction"))
    with pytest.raises(control.StarshipMissionError, match="planner_scenario_mismatch"):
        service.plan("selected-session", "Starship sixdof_gimbal_step", expected_scenario="sixdof_gimbal_step")
    assert service.current("selected-session") is None
    with pytest.raises(control.StarshipMissionError, match="no_current_starship_plan"):
        service.approve("selected-session", "unexpected", "a"*64)


def test_selected_scenario_mismatch_preserves_existing_session_revision(service):
    prior = service.plan("selected-session", "Starship comparison", expected_scenario="dispenser_comparison")
    before = service._session_path("selected-session").read_bytes()
    response = fixture_proposal("")
    response["proposal"]["scenario"] = "sixdof_engine_out"
    service.planner = lambda _: response
    with pytest.raises(control.StarshipMissionError, match="planner_scenario_mismatch"):
        service.plan("selected-session", "Starship gimbal", expected_scenario="sixdof_gimbal_step")
    assert service._session_path("selected-session").read_bytes() == before
    assert service.current("selected-session")["plan"]["id"] == prior["plan"]["id"]
    assert service.current("selected-session")["approval"] is None


def test_stale_displayed_plan_and_cross_session_rejected(service):
    old = plan(service)
    fresh = plan(service)
    with pytest.raises(control.StarshipMissionError, match="plan_binding_mismatch"):
        service.approve(*old)
    with pytest.raises(control.StarshipMissionError, match="no_current"):
        service.approve("another-session", *fresh[1:])
    assert service.current(fresh[0])["approval"] is None


def test_pending_provider_cannot_overwrite_concurrent_approval(service):
    ref = plan(service)
    def delayed(_):
        service.approve(*ref)
        return fixture_proposal("")
    service.planner = delayed
    with pytest.raises(control.StarshipMissionError, match="plan_changed_during_planning"):
        plan(service)
    assert service.current(ref[0])["status"] == "approved"


def test_source_change_invalidates_approval_and_run(service, monkeypatch):
    ref = plan(service)
    original = control._sources
    monkeypatch.setattr(control, "_sources", lambda _: {"changed": "source"})
    with pytest.raises(control.StarshipMissionError, match="source_changed"):
        service.approve(*ref)
    monkeypatch.setattr(control, "_sources", original)
    service.approve(*ref)
    monkeypatch.setattr(control, "_sources", lambda _: {"changed": "source"})
    with pytest.raises(control.StarshipMissionError, match="approved_source_changed"):
        service.execute(*ref)


def test_flight_approval_binds_reference_observations_and_verifier_contracts(service, monkeypatch):
    from src.runtime.starship_study import source_hashes
    sources = control._sources("flight14_inspired")
    assert set(source_hashes()) <= set(sources)
    assert "src/runtime/runtime_claim_evidence.py" in sources
    assert "packages/missionos-core/src/missionos_core/mission_contract.py" in sources
    proposal = fixture_proposal("")
    proposal["proposal"]["scenario"] = "flight14_inspired"
    service.planner = lambda _: proposal
    ref = plan(service)
    service.approve(*ref)
    # A changed observation input is rejected before a worker can be launched.
    changed = {**sources, "examples/spaceflight/starship-flight14-observations.json": "changed"}
    monkeypatch.setattr(control, "_sources", lambda _: changed)
    with pytest.raises(control.StarshipMissionError, match="approved_source_changed"):
        service.execute(*ref)


def test_approval_expires_and_signed_grant_cannot_be_modified(service):
    now = time.time()
    service.clock = lambda: now
    ref = plan(service)
    approved = service.approve(*ref)
    assert "signature" not in approved["approval"]
    assert approved["approval"]["authenticated_operator_identity"] is False
    now += control.APPROVAL_TTL_S
    with pytest.raises(control.StarshipMissionError, match="expired_or_consumed"):
        service.execute(*ref)
    now -= control.APPROVAL_TTL_S
    state = service._load(ref[0])
    state["approval"]["scope"] = "physical_execution"
    service._save(ref[0], state)
    with pytest.raises(control.StarshipMissionError, match="signature_invalid"):
        service.execute(*ref)


def test_failed_spawn_consumes_approval(service, monkeypatch):
    ref = plan(service)
    service.approve(*ref)
    def fail(*args, **kwargs):
        raise OSError("deliberately not exposed")
    monkeypatch.setattr(control.subprocess, "Popen", fail)
    state = service.execute(*ref)
    assert state["status"] == "failed"
    assert state["approval"]["consumed_by_run"]
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval_required"):
        service.execute(*ref)


def test_worker_without_receipt_is_failed_not_completed(service):
    ref = plan(service)
    state = service._load(ref[0])
    state.update(status="running", execution={"run_id": "a" * 32, "status": "running"})
    service._save(ref[0], state)
    service._observe_exit(ref[0], "a" * 32, SimpleNamespace(wait=lambda: 2))
    result = service.status(ref[0])
    assert result["status"] == "failed"
    assert result["execution"]["failure_reason"] == "worker_exited_without_receipt"
    assert result["mission_completed"] is False


def test_worker_environment_drops_provider_and_cloud_state(monkeypatch):
    for name in ("DEEPSEEK_API_KEY", "TYPESAFE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                 "JEV_API_KEY", "HOME", "REDIS_URL", "MISSIONOS_LLM_BACKEND"):
        monkeypatch.setenv(name, "not-a-secret")
    env = control.worker_environment()
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "PYTHONPATH",
                        "PYTHONUNBUFFERED", "MISSIONOS_LLM_BACKEND"}
    assert env["MISSIONOS_LLM_BACKEND"] == "off"


def test_actual_comparison_worker_verifies_and_excludes_parent_credentials(service, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-sentinel-not-a-credential")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-sentinel-not-a-credential")
    ref = plan(service)
    service.approve(*ref)
    launched = service.execute(*ref)
    assert launched["execution"]["subprocess_spawned"] is True
    assert launched["execution"]["worker_pid"] != os.getpid()
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval_required"):
        service.execute(*ref)
    with pytest.raises(control.StarshipMissionError, match="execution_in_progress"):
        plan(service)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = service.status(ref[0])
        if result["status"] != "running":
            break
        time.sleep(.05)
    assert result["status"] == "verified", result
    assert result["execution"]["worker_receipt_verified"] is True
    assert result["execution"]["provider_credentials_present"] is False
    assert result["physical_execution"] is False
    assert result["mission_completed"] is False
    path = service.read_artifact(ref[0], ref[1], "study.json")
    assert json.loads(path.read_text())
    with pytest.raises(control.StarshipMissionError, match="artifact_not_allowlisted"):
        service.read_artifact(ref[0], ref[1], "../approval-signing.key")
    path.write_text("{}")
    with pytest.raises(control.StarshipMissionError, match="saved_artifact_changed"):
        service.read_artifact(ref[0], ref[1], "study.json")
    with pytest.raises(FileExistsError):
        control.execute_worker(service.root, launched["execution"]["run_id"])


def test_failed_simulator_preserves_bounded_error_in_local_worker_log(capsys):
    child = "import sys; sys.stderr.write('x'*10000+'failure marker'); sys.exit(2)"
    assert control._run_simulator([sys.executable, "-c", child], timeout=5) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Simulator subprocess failed:\n")
    assert captured.err.endswith("failure marker\n")
    assert len(captured.err) < 8300


def test_timeout_reaps_descendant_even_when_direct_child_already_exited(tmp_path):
    # A child exits immediately; its grandchild holds the inherited pipes open.
    marker = tmp_path / "survived"
    grandchild = f"import time; from pathlib import Path; time.sleep(1.2); Path({str(marker)!r}).write_text('bad')"
    child = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{grandchild!r}])"
    with pytest.raises(subprocess.TimeoutExpired):
        control._run_simulator([sys.executable, "-c", child], timeout=.1)
    time.sleep(1.3)
    assert not marker.exists()

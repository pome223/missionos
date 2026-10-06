"""Jev scope approval, credential role separation and real broker/worker boundary."""

import json
import sys
import time
from types import SimpleNamespace

import pytest

from src.runtime import starship_mission_control as control
from src.runtime import starship_jev_shadow as shadow


def proposal(_):
    return {"proposal": {"scenario": "dispenser_jev_shadow", "rationale": "Shadow only.",
                         "uncertainties": ["No real-time or value claim."]},
            "invocation": {"model_inference_invoked": False}}


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", "fixture")
    return control.StarshipMissionService(tmp_path, planner=proposal)


def reference(service):
    result = service.plan("shadow-operator", "Starship Jev shadow")
    return result, ("shadow-operator", result["plan"]["id"], result["plan"]["sha256"])


def test_off_profile_cannot_create_plan(service, monkeypatch):
    monkeypatch.delenv("MISSIONOS_STARSHIP_JEV_SHADOW_MODE")
    with pytest.raises(control.StarshipMissionError, match="jev_shadow_not_configured"):
        reference(service)


def test_fixture_approval_cannot_turn_into_live(service, monkeypatch):
    result, ref = reference(service)
    assert result["plan"]["jev_shadow"]["maximum_provider_calls"] == 0
    approved = service.approve(*ref)
    assert approved["approval"]["scope"] == "local_simulation_and_jev_shadow"
    monkeypatch.setenv("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-sentinel")
    env = control.process_environment(result["plan"])
    assert env["MISSIONOS_STARSHIP_JEV_SHADOW_MODE"] == "fixture"
    assert "TYPESAFE_API_KEY" not in env


def test_live_key_is_given_only_to_observer_broker(service, monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-sentinel")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "other-sentinel")
    result, ref = reference(service)
    approved = service.approve(*ref)
    assert approved["approval"]["jev_shadow"]["maximum_provider_calls"] == 22
    assert "sentinel" not in json.dumps(approved)
    env = control.process_environment(result["plan"])
    assert env["TYPESAFE_API_KEY"] == "test-sentinel"
    assert "DEEPSEEK_API_KEY" not in env
    assert "TYPESAFE_API_KEY" not in control.worker_environment()
    assert "TYPESAFE_API_KEY" not in control.process_environment({"scenario": "dispenser_comparison"})


def test_old_simulation_scope_cannot_be_reused_for_shadow(service):
    _, ref = reference(service)
    service.approve(*ref)
    state = service._load(ref[0])
    state["approval"]["scope"] = "local_simulation_only"
    unsigned = {k: v for k, v in state["approval"].items() if k != "signature"}
    state["approval"]["signature"] = service._signature(unsigned)
    service._save(ref[0], state)
    with pytest.raises(control.StarshipMissionError, match="approval_binding"):
        service.execute(*ref)


def test_live_configuration_loss_does_not_consume_grant(service, monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", "live")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    _, ref = reference(service)
    service.approve(*ref)
    with pytest.raises(control.StarshipMissionError, match="configuration_required"):
        service.execute(*ref)
    assert service.current(ref[0])["approval"]["consumed_by_run"] is None


def test_jev_only_launcher_never_loads_a_deepseek_key(tmp_path, monkeypatch):
    from scripts import start_starship_gateway as launcher
    reads = []
    def secret(project, name):
        reads.append((project, name))
        return "fixture-test-key"
    monkeypatch.setattr(launcher, "_read_secret", secret)
    env = launcher.build_environment(SimpleNamespace(
        port=18797, state_dir=tmp_path, project="test-project", deepseek_secret="deepseek-key",
        jev_secret="jev-key", fixture_planner=True, enable_live_models=False,
        enable_live_jev_shadow=True,
    ))
    assert reads == [("test-project", "jev-key")]
    assert "DEEPSEEK_API_KEY" not in env
    assert env["MISSIONOS_STARSHIP_PLANNER_MODE"] == "fixture"
    assert env["MISSIONOS_STARSHIP_JEV_SHADOW_MODE"] == "live"


def test_real_broker_process_verifies_all_cases_without_provider_keys(service, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "must-not-enter-fixture")
    _, ref = reference(service)
    service.approve(*ref)
    launched = service.execute(*ref)
    assert launched["execution"]["subprocess_spawned"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = service.status(ref[0])
        if result["status"] != "running":
            break
        time.sleep(.1)
    assert result["status"] == "verified", result
    assert result["execution"]["process_role"] == "jev_shadow_observer_broker"
    assert result["execution"]["provider_credentials_present"] is False
    assert result["execution"]["simulator_provider_credentials_present"] is False
    bundle = json.loads(service.read_artifact(ref[0], ref[1], "study.json").read_text())
    assert len(bundle["execution_runs"]) == 60
    assert len(bundle["shadow"]["records"]) == 22
    assert bundle["shadow"]["worker"]["worker_pid"] != launched["execution"]["worker_pid"]


def test_broker_rejects_live_router_in_fixture_before_spawning(tmp_path):
    class Router:
        mode = "live"
        max_calls = 22
    with pytest.raises(ValueError, match="injected_router_rejected"):
        shadow.run_shadow(tmp_path / "bad", mode="fixture", router=Router())
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("invalid", ["duplicate", "identifier", "bool_index"])
def test_invalid_worker_frame_is_rejected_before_an_extra_router_call(tmp_path, monkeypatch, invalid):
    from src.intelligence.starship_jev_router import StarshipJevRouter
    frame = {"kind": "fault_observation", "case_ref": "a" * 64, "step_index": 1,
             "public_history": {"observations": [{"time_ticks": 0, "healthy": False},
                                                    {"time_ticks": 2, "healthy": False}],
                                "retry_results": [{"start_ticks": 0, "end_ticks": 2, "success": False}]},
             "public_budget": {"time_ticks": 2, "attempts": 1, "released": 0,
                               "deadline_ticks": 12, "max_attempts": 5, "payload_count": 3,
                               "retry_ticks": 2, "wait_ticks": 1, "tick_s": 2}}
    if invalid == "identifier":
        frame["case_ref"] = "contains-hidden-data"
    if invalid == "bool_index":
        frame["step_index"] = True
    frames = [frame, frame] if invalid == "duplicate" else [frame]
    original = shadow.subprocess.Popen
    def process(_args, **kwargs):
        code = "import sys;sys.stdout.write(" + repr("".join(json.dumps(f) + "\n" for f in frames)) + ")"
        return original([sys.executable, "-c", code], **kwargs)
    monkeypatch.setattr(shadow.subprocess, "Popen", process)
    router = StarshipJevRouter(mode="fixture")
    called = []
    route = router.route
    def observed(history, budget):
        called.append(True)
        return route(history, budget)
    monkeypatch.setattr(router, "route", observed)
    with pytest.raises(ValueError, match="invalid_observation"):
        shadow.run_shadow(tmp_path / "invalid", mode="fixture", router=router)
    assert len(called) == (1 if invalid == "duplicate" else 0)

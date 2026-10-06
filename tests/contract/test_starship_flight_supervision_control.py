"""Approval/source/mode binding for the active flight-supervision boundary."""
from types import SimpleNamespace

import pytest

from scripts import start_starship_gateway as launcher
from src.gateway import starship_chat
from src.intelligence.starship_mission_planner import plan_starship_request
from src.runtime import starship_mission_control as control


def _plan(tmp_path, monkeypatch, mode="fixture"):
    monkeypatch.setenv("MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE", mode)
    service = control.StarshipMissionService(tmp_path, planner=lambda text: plan_starship_request(text, "fixture"))
    state = service.plan("flight-operator", "Starship 六自由度の飛行中監督")
    return service, state, ("flight-operator", state["plan"]["id"], state["plan"]["sha256"])


def test_scope_is_visible_before_approval_and_is_bound_to_grant(tmp_path, monkeypatch):
    service, state, ref = _plan(tmp_path, monkeypatch)
    assert state["plan"]["scenario"] == "sixdof_deployment_supervised"
    assert state["approval"] is None and state["execution"] == {}
    scope = state["plan"]["flight_supervision"]
    assert scope["allowed_actions"] == ["hold", "skip_remaining_deployment"]
    assert scope["maximum_commands"] == 1
    visible = starship_chat._response("test", "plan", state, ref[0])["message"]
    assert "見送る操作1回" in visible and "Jev最大0回" in visible
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval_required"):
        service.execute(*ref)
    approved = service.approve(*ref)
    assert approved["approval"]["scope"] == "local_simulation_and_bounded_flight_supervision"
    assert approved["approval"]["flight_supervision"] == scope
    assert approved["approval"]["authenticated_operator_identity"] is False


def test_off_is_not_silently_enabled(tmp_path, monkeypatch):
    with pytest.raises(control.StarshipMissionError, match="flight_supervisor_not_configured"):
        _plan(tmp_path, monkeypatch, "off")


def test_configuration_change_or_credential_absence_prevents_spawn(tmp_path, monkeypatch):
    service, state, ref = _plan(tmp_path, monkeypatch)
    service.approve(*ref)
    monkeypatch.setenv("MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE", "live")
    with pytest.raises(control.StarshipMissionError, match="configuration_changed"):
        service.execute(*ref)
    plan = dict(state["plan"], flight_supervision=dict(state["plan"]["flight_supervision"], mode="live"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(control.StarshipMissionError, match="credentials_required"):
        control.process_environment(plan)


def test_live_worker_never_receives_provider_keys(tmp_path, monkeypatch):
    _, state, _ = _plan(tmp_path, monkeypatch, "live")
    for name in ("DEEPSEEK_API_KEY", "TYPESAFE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.setenv(name, "fixture-sentinel")
    env = control.process_environment(state["plan"])
    assert not any("KEY" in name or "CREDENTIAL" in name for name in env)


@pytest.mark.parametrize("source", ["src/runtime/starship_flight_broker.py",
    "src/runtime/starship_flight_supervision.py", "src/runtime/starship_flight_supervision_verifier.py",
    "src/intelligence/starship_flight_supervisor.py", "src/intelligence/space_ops_investigator.py",
    "src/agents/model_config.py"])
def test_provider_and_verifier_source_changes_invalidate_grant(tmp_path, monkeypatch, source):
    service, state, ref = _plan(tmp_path, monkeypatch)
    service.approve(*ref)
    assert source in state["plan"]["source_sha256"]
    monkeypatch.setattr(control, "_sources", lambda _: {**state["plan"]["source_sha256"], source: "changed"})
    with pytest.raises(control.StarshipMissionError, match="approved_source_changed"):
        service.execute(*ref)


def test_live_supervisor_opt_in_loads_both_keys_without_live_planning(tmp_path, monkeypatch):
    reads = []
    monkeypatch.setattr(launcher, "_read_secret", lambda project, name: reads.append(name) or "sentinel")
    env = launcher.build_environment(SimpleNamespace(
        port=18808, project="fixture-project", state_dir=tmp_path,
        enable_live_models=False, enable_live_jev_shadow=False, fixture_planner=True,
        enable_live_flight_supervisor=True, deepseek_secret="fixture-deepseek", jev_secret="fixture-jev"))
    assert reads == ["fixture-deepseek", "fixture-jev"]
    assert env["MISSIONOS_STARSHIP_PLANNER_MODE"] == "fixture"
    assert env["MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE"] == "live"
    assert env["MISSIONOS_AGENT_MISSIONOS_STARSHIP_PLANNER_AGENT_LLM_BACKEND"] == "deepseek"


def test_wrong_run_mailbox_does_not_invoke_provider(tmp_path, monkeypatch):
    import json
    from src.runtime.starship_flight_broker import serve_flight_request
    from src.intelligence.starship_flight_supervisor import FlightSupervisor
    (tmp_path / "request.json").write_text(json.dumps({"request_id": "other-run"}))
    monkeypatch.setattr(FlightSupervisor, "assess", lambda *args: pytest.fail("wrong run reached provider"))
    serve_flight_request(tmp_path, "current-run", "fixture", SimpleNamespace(poll=lambda: None))
    assert not (tmp_path / "response.json").exists()


def test_observation_plan_discloses_and_binds_the_extra_read_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE", "fixture")
    service = control.StarshipMissionService(tmp_path, planner=lambda text: plan_starship_request(text, "fixture"))
    state = service.plan("observer", "Starship 追加観測を要求してから再判断したい")
    plan = state["plan"]
    assert plan["scenario"] == "sixdof_observation_supervised"
    scope = plan["flight_supervision"]
    assert scope["maximum_observation_requests"] == 1 and scope["maximum_commands"] == 1
    visible = starship_chat._response("test", "plan", state, "observer")["message"]
    assert "追加の放出状態報告を最大1回" in visible and "75秒" in visible
    approved = service.approve("observer", plan["id"], plan["sha256"])
    assert approved["approval"]["flight_supervision"] == scope


def test_collection_broker_reassesses_once_without_reusing_old_observations(tmp_path, monkeypatch):
    import json
    from src.runtime.starship_flight_broker import serve_flight_request
    from src.intelligence.starship_flight_supervisor import FlightSupervisor
    first = {"request_id": "current-run", "observations": [{"time_s": 100.}, {"time_s": 102.}]}
    (tmp_path / "request.json").write_text(json.dumps(first))
    seen = []
    def assess(self, request):
        seen.append(request)
        if len(seen) == 1:
            (tmp_path / "request-followup.json").write_text(json.dumps({**first, "observations": [{"time_s": 104.}, {"time_s": 106.}]}))
        return {"route": "need_observation" if len(seen) == 1 else "bounded", "action": "hold"}
    monkeypatch.setattr(FlightSupervisor, "assess", assess)
    serve_flight_request(tmp_path, "current-run", "fixture", SimpleNamespace(poll=lambda: None), observation_collection=True)
    assert len(seen) == 2 and (tmp_path / "response-followup.json").exists()


def test_collection_broker_discards_a_followup_outside_original_deadline(tmp_path, monkeypatch):
    import json
    from src.runtime.starship_flight_broker import serve_flight_request
    from src.intelligence.starship_flight_supervisor import FlightSupervisor
    first = {"request_id": "current-run", "observations": [{"time_s": 100.}, {"time_s": 102.}]}
    (tmp_path / "request.json").write_text(json.dumps(first))
    seen = []
    def assess(self, request):
        seen.append(request)
        (tmp_path / "request-followup.json").write_text(json.dumps({**first, "observations": [{"time_s": 178.}, {"time_s": 180.}]}))
        return {"route": "need_observation", "action": "hold"}
    monkeypatch.setattr(FlightSupervisor, "assess", assess)
    serve_flight_request(tmp_path, "current-run", "fixture", SimpleNamespace(poll=lambda: None), observation_collection=True)
    assert len(seen) == 1 and not (tmp_path / "response-followup.json").exists()


def test_elapsed_provider_window_prevents_another_assessment(tmp_path, monkeypatch):
    import json
    from src.runtime import starship_flight_broker as broker
    from src.intelligence.starship_flight_supervisor import FlightSupervisor
    clock = [0.]
    monkeypatch.setattr(broker.time, "monotonic", lambda: clock[0])
    first = {"request_id": "current-run", "observations": [{"time_s": 100.}, {"time_s": 102.}]}
    (tmp_path / "request.json").write_text(json.dumps(first))
    seen = []
    def assess(self, request):
        seen.append(request)
        clock[0] = 76.
        (tmp_path / "request-followup.json").write_text(json.dumps({**first, "observations": [{"time_s": 104.}, {"time_s": 106.}]}))
        assert self._run_active() is False
        return {"route": "need_observation", "action": "hold"}
    monkeypatch.setattr(FlightSupervisor, "assess", assess)
    broker.serve_flight_request(tmp_path, "current-run", "fixture", SimpleNamespace(poll=lambda: None), observation_collection=True)
    assert len(seen) == 1 and not (tmp_path / "response-followup.json").exists()

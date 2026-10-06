"""Narrow mission-layer fixture registration; no public catalog activation."""
import json
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from src.gateway import server, starship_chat
from src.intelligence.starship_mission_planner import plan_starship_request
from src.runtime import starship_mission_control as control
from src.runtime import starship_tower_broker as ipc, starship_tower_supervision as tower
from src.runtime.starship_operator_resolution import OperatorResolutionLedger
from src.runtime.starship_return_sites import ReturnSites
from src.runtime.starship_tower_actor import TowerActor

KEY = b"explicit-gateway-fixture-integrity-key-32bytes"
SESSION = "starship-operator-"+"a"*24
RUN = "b"*32


class ProcessFixture:
    pid = os.getpid()
    def __init__(self):
        self.ended = Event()
    def poll(self):
        return 0 if self.ended.is_set() else None
    def wait(self):
        self.ended.wait(5)
        return 0


class Judge:
    fixture_only = True
    def judge(self, prompt):
        return SimpleNamespace(output={"proposed_response_kind": "operator_escalation", "parameters": {}},
            invocation_evidence={"assessment_route": "human_review"}, model_inference_invoked=False)


def sites_hash():
    config = json.loads(Path("examples/spaceflight/starship-return-sites-model-test.json").read_text())
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text())
    return ReturnSites.from_dict(config, profile=profile, catch_config=catch).sha256


def return_sites():
    config = json.loads(Path("examples/spaceflight/starship-return-sites-model-test.json").read_text())
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    catch = json.loads(Path("examples/spaceflight/starship-catch-profile.json").read_text())
    return ReturnSites.from_dict(config, profile=profile, catch_config=catch)


def make_run(service, *, session=SESSION, run_id=RUN):
    service.plan(session, "Starship sixdof_gimbal_step", expected_scenario="sixdof_gimbal_step")
    with service._lock():
        state = service._load(session)
        plan = state["plan"]
        plan["tower_supervision"] = control.tower_contract(return_sites_sha256=sites_hash())
        plan["source_sha256"] = control._plan_sources(plan)
        plan["sha256"] = control._digest({k: v for k, v in plan.items() if k != "sha256"})
        service._save(session, state)
    service.approve(session, plan["id"], plan["sha256"])
    process = ProcessFixture()
    with service._lock():
        state = service._load(session)
        state["approval"]["consumed_by_run"] = run_id
        state["approval"]["signature"] = service._signature({k: v for k, v in state["approval"].items() if k != "signature"})
        state.update(status="running", execution={"run_id": run_id, "status": "running", "subprocess_spawned": True,
            "worker_pid": process.pid, "started_at_epoch_s": service.clock(), "physical_execution": False})
        service._save(session, state)
    run_dir = service.root/("run-"+run_id)
    run_dir.mkdir(mode=0o700)
    scope = {"schema": tower.SCOPE_SCHEMA, "scope": plan["tower_supervision"]["scope"],
        "context": {"session_id": session, "plan_id": plan["id"], "plan_sha256": plan["sha256"], "run_id": run_id, "request_id": "request-1"},
        "approval_record_sha256": control._digest(state["approval"]), "source_sha256": control._digest(plan["source_sha256"]),
        "issued_simulation_time_s": 10., "issued_wall_time_s": 1000., "original_simulation_deadline_s": 85.,
        "original_wall_deadline_s": 1075., "maximum_observation_age_s": 2., "observation_collection_allowed": True,
        "human_resolution_allowed": True}
    ledger = OperatorResolutionLedger(KEY, store_path=run_dir/"tower-resolution.sqlite3")
    clock = SimpleNamespace(now=1000.)
    declared = return_sites()
    actor = TowerActor(run_dir/"tower", scope, declared, declared.sha256, local_integrity_key=KEY,
        ledger_path=run_dir/"tower-resolution.sqlite3", clock=lambda: clock.now)
    broker = ipc.TowerBroker(run_dir/"tower", run_id, scope, process, ledger, Judge(), lambda: True, clock=lambda: clock.now)
    service.register_tower_broker(session, plan["id"], plan["sha256"], run_id, broker=broker)
    return SimpleNamespace(service=service, plan=plan, scope=scope, process=process, ledger=ledger, broker=broker, actor=actor,
        clock=clock, root=run_dir, context={"session_id": session, "plan_id": plan["id"], "plan_sha256": plan["sha256"]})


def human_pending(item, ready=True):
    body = {"time_s": 11., "r_eci_m": [6378137., 0., 0.], "v_eci_mps": [0., 0., 0.],
        "q_body_to_eci": [1., 0., 0., 0.], "omega_body_rad_s": [0., 0., 0.], "propellant_kg": 78000.,
        "engine_states": [{"available": True, "throttle": 0., "gimbal_x_rad": 0., "gimbal_y_rad": 0.} for _ in range(45)],
        "flap_angles_rad": [0.]*6}
    observation = tower.tower_observation(context=item.scope["context"], observation_id="fixture-11", state=body,
        wall_time_s=item.clock.now, tower_ready=ready, return_mode="capture")
    request = item.ledger.create_pending(context=item.scope["context"], initial_observation=tower._ledger_observation(observation),
        issued_simulation_time_s=11., issued_wall_time_s=item.clock.now, original_simulation_deadline_s=85.,
        original_wall_deadline_s=1075., maximum_observation_age_s=2., preapproved_scope=item.scope["scope"])
    ipc.write_tower_frame(item.root/"tower", "human-request.json", request)
    ipc.write_tower_frame(item.root/"tower", "latest-observation.json", {"schema": ipc.FRAME_SCHEMA,
        "context": item.scope["context"], "source_sha256": item.scope["source_sha256"], "observation": observation,
        "simulation_time_s": 11., "wall_time_s": item.clock.now, "run_active": True}, replace=True)
    return request, observation


@pytest.mark.parametrize("failure", ["publish", "status"])
def test_committed_choice_followed_by_failure_reports_unknown_receipt_and_retains_one_use(run_fixture, monkeypatch, failure):
    item = run_fixture
    request, observation = human_pending(item)
    monkeypatch.setattr(starship_chat, "get_starship_service", lambda: control.StarshipMissionService(item.service.root))
    if failure == "publish":
        def fail(decision):
            raise OSError("fixture publication lost after ledger commit")
        monkeypatch.setattr(item.broker, "_publish_human", fail)
    else:
        def fail(*args):
            raise ValueError("fixture status unavailable after ledger commit")
        monkeypatch.setattr(control.StarshipMissionService, "tower_status", fail)
    response = starship_chat.handle_starship_tower_operator({"action": "tower_resolve", "session_id": SESSION,
        "starship_context": item.context, "run_id": RUN, "request_id": item.scope["context"]["request_id"],
        "request_sha256": request["sha256"], "observed_evidence_sha256": observation["evidence_sha256"], "choice": "divert"})
    decision = item.ledger.records(item.scope["context"])["decision"]
    assert decision is not None and decision["effective_action"] == "divert"
    assert response["operation_result"]["mutation_outcome_uncertain"] is True
    assert response["operation_result"]["resend_authorized"] is False
    assert "受付の確定状況は未確認" in response["message"] and "再送せず" in response["message"]
    assert "操作は行っていません" not in response["message"]
    with pytest.raises(ValueError, match="already_consumed"):
        item.ledger.resolve(context=item.scope["context"], request_sha256=request["sha256"],
            observed_evidence_sha256=observation["evidence_sha256"], latest_observation=tower._ledger_observation(observation),
            action="divert", simulation_time_s=11., wall_time_s=item.clock.now, run_active=True)


@pytest.fixture
def run_fixture(tmp_path):
    service = control.StarshipMissionService(tmp_path/"state", planner=lambda text: plan_starship_request(text, "fixture"))
    item = make_run(service)
    try:
        yield item
    finally:
        item.process.ended.set()


def test_registration_is_visible_from_a_fresh_service_and_choice_is_one_use(run_fixture):
    item = run_fixture
    request, observation = human_pending(item)
    service = control.StarshipMissionService(item.service.root)
    pending = service.tower_status(SESSION, item.plan["id"], item.plan["sha256"], RUN)
    assert pending["tower_pending"]["pending"] is True
    assert pending["tower_pending"]["request"]["sha256"] == request["sha256"]
    assert "signature" not in pending["tower_pending"]["request"]
    resolved = service.tower_resolve(SESSION, item.plan["id"], item.plan["sha256"], RUN, "request-1",
        request["sha256"], observation["evidence_sha256"], "divert")
    assert resolved["tower_pending"]["pending"] is False
    assert resolved["tower_pending"]["decision"]["human_identity_authenticated"] is False
    assert resolved["tower_pending"]["decision"]["executor_command_issued"] is False


def test_normal_catalog_has_no_tower_authority_or_active_run(tmp_path):
    service = control.StarshipMissionService(tmp_path/"state", planner=lambda text: plan_starship_request(text, "fixture"))
    planned = service.plan(SESSION, "Starship sixdof_gimbal_step", expected_scenario="sixdof_gimbal_step")
    assert "tower_supervision" not in planned["plan"] and planned["execution"] == {}
    with pytest.raises(control.StarshipMissionError):
        service.tower_status(SESSION, planned["plan"]["id"], planned["plan"]["sha256"], RUN)


@pytest.mark.parametrize("field,value", [("session", "other"), ("plan", "other"), ("sha", "f"*64), ("run", "f"*32), ("request", "other")])
def test_context_cannot_be_transplanted(run_fixture, field, value):
    item = run_fixture
    request, observation = human_pending(item)
    values = {"session": SESSION, "plan": item.plan["id"], "sha": item.plan["sha256"], "run": RUN, "request": "request-1"}
    values[field] = value
    with pytest.raises(control.StarshipMissionError):
        item.service.tower_resolve(values["session"], values["plan"], values["sha"], values["run"], values["request"],
            request["sha256"], observation["evidence_sha256"], "divert")
    assert item.ledger.records(item.scope["context"])["decision"] is None


def test_global_lease_prevents_another_session_budget(run_fixture):
    with pytest.raises(control.StarshipMissionError, match="another_tower_run_lease_exists"):
        make_run(run_fixture.service, session="starship-operator-"+"f"*24, run_id="f"*32)


@pytest.mark.parametrize("change", ["source", "grant", "lease", "scope", "stale", "evidence"])
def test_relevant_binding_changes_fail_closed(run_fixture, change, monkeypatch):
    item = run_fixture
    request, observation = human_pending(item)
    expected = observation["evidence_sha256"]
    if change == "source":
        monkeypatch.setattr(control, "_plan_sources", lambda _: {})
    elif change == "grant":
        with item.service._lock():
            state = item.service._load(SESSION)
            state["approval"]["tower_supervision"]["maximum_directives"] = 2
            item.service._save(SESSION, state)
    elif change == "lease":
        (item.service.root/"tower-active-run.json").write_text("{}")
    elif change == "scope":
        item.broker._scope["original_wall_deadline_s"] += 1.
    elif change == "stale":
        item.clock.now += 3.
    else:
        expected = "f"*64
    with pytest.raises(control.StarshipMissionError):
        item.service.tower_resolve(SESSION, item.plan["id"], item.plan["sha256"], RUN, "request-1", request["sha256"], expected, "divert")
    assert item.ledger.records(item.scope["context"])["decision"] is None


@pytest.fixture
def gateway(run_fixture, monkeypatch, tmp_path):
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit
    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path/"tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path/"memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path/"audit.log"))
    monkeypatch.setenv("GATEWAY_API_KEY", "")
    monkeypatch.setenv("GATEWAY_HOST", "127.0.0.1")
    monkeypatch.setenv("GATEWAY_PORT", "18822")
    monkeypatch.setattr(starship_chat, "get_starship_service", lambda: control.StarshipMissionService(run_fixture.service.root))
    monkeypatch.setattr(server, "run_missionos_autonomy_conversation", lambda *args, **kwargs: pytest.fail("Generic route invoked"))
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    yield TestClient(server.create_missionos_gateway().app, base_url="http://127.0.0.1:18822")
    reset_settings()
    reset_task_store()
    audit._audit_logger = None


def headers():
    return {"Origin": "http://127.0.0.1:18822", "Referer": "http://127.0.0.1:18822/missionos/starship/operator",
            "Sec-Fetch-Site": "same-origin", "X-MissionOS-Operator": "starship-v1"}


def payload(item, action="tower_status"):
    return {"action": action, "session_id": SESSION, "starship_context": item.context, "run_id": RUN}


def test_narrow_http_pending_and_resolve_never_enter_generic_route(gateway, run_fixture):
    item = run_fixture
    request, observation = human_pending(item)
    pending = gateway.post("/missionos/starship/operator/actions", headers=headers(), json=payload(item))
    assert pending.status_code == 200 and pending.json()["operation_result"]["tower_pending"]["pending"] is True
    resolved = gateway.post("/missionos/starship/operator/actions", headers=headers(), json={**payload(item, "tower_resolve"),
        "request_id": "request-1", "request_sha256": request["sha256"], "observed_evidence_sha256": observation["evidence_sha256"], "choice": "divert"})
    assert resolved.status_code == 200
    assert resolved.json()["operation_result"]["tower_pending"]["decision"]["effective_action"] == "divert"


@pytest.mark.parametrize("change", [{"choice": "set_thrust"}, {"gimbal_rad": 0.}, {"starship_context": None},
    {"request_sha256": "invalid"}, {"request_id": "../../file"}])
def test_http_closed_choices_refuse_unbound_or_numeric_actions(gateway, run_fixture, change):
    item = run_fixture
    request, observation = human_pending(item)
    body = {**payload(item, "tower_resolve"), "request_id": "request-1", "request_sha256": request["sha256"],
            "observed_evidence_sha256": observation["evidence_sha256"], "choice": "divert", **change}
    assert gateway.post("/missionos/starship/operator/actions", headers=headers(), json=body).status_code == 400
    assert item.ledger.records(item.scope["context"])["decision"] is None


def test_untrusted_browser_origin_is_still_refused(gateway, run_fixture):
    human_pending(run_fixture)
    response = gateway.post("/missionos/starship/operator/actions", headers={**headers(), "Origin": "https://untrusted.example"}, json=payload(run_fixture))
    assert response.status_code == 403

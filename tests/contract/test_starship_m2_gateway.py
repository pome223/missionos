"""Provider/approval boundaries only; no paid call or numerical flight."""

import json
from types import SimpleNamespace

import pytest

from src.intelligence import starship_m2_broker as broker
from src.runtime import starship_mission_control as control
from src.runtime.starship_m2_contract import m2_contract
from src.runtime.starship_m2_control import SCENARIOS, SCOPE, validate_worker_input
from src.runtime.starship_m2_decision_verifier import verify_decisions
from src.runtime.starship_mission_director import MissionDirector, contract as deployment_contract
from src.runtime.starship_replanning import ROOT, digest
from src.intelligence.starship_flight_supervisor import _receipt
from src.intelligence.starship_mission_director import fixture_decision


def observation(now=500.0):
    return {
        "time_s": now,
        "phase": "orbital_coast",
        "released_count": 0,
        "release_acknowledged": False,
        "sequencer_state": "running",
        "fuel_kg": 100000.0,
        "return_deadline_s": 2280.7,
        "tower_ready": False,
        "operations_notice": "",
        "hold_expires_at_s": None,
        "numerical_tools": {
            "orbit_release_feasible": True,
            "retained_payload_present": True,
            "capture_corridor_certified": False,
            "mechanism_status": "not_collected",
        },
    }


def request(remaining=15.0):
    envelope = deployment_contract("live")
    return envelope, {
        "schema": "missionos.starship_director_request.v5",
        "request_id": "run-deployment:1",
        "point": "deployment_monitor",
        "observation": observation(),
        "allowed_actions": ["continue", "hold"],
        "envelope_sha256": digest(envelope),
        "decision_deadline_s": 500.0 + remaining,
    }


def valid_provider(wire):
    value = json.loads(wire)
    criteria = value["questions"]["decision"]["criteria"]
    return json.dumps(
        {
            "model": broker.JEV_MODEL,
            "answers": {
                "decision": {
                    "type": "choice",
                    "choice": "continue",
                    "probabilities": {k: float(k == "continue") for k in criteria},
                    "confidence": 1.0,
                }
            },
        }
    ).encode()


@pytest.mark.parametrize("remaining,expected", [(15.0, 13.0), (75.0, 30.0), (5.0, 3.0)])
def test_real_transport_argument_uses_remaining_budget_without_deepseek(
    monkeypatch, remaining, expected
):
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "unit-fixture-only")
    calls = []

    def send(endpoint, wire, key, **kwargs):
        calls.append((endpoint, json.loads(wire), kwargs))
        return "response", valid_provider(wire)

    monkeypatch.setattr(broker, "_send_once", send)
    envelope, req = request(remaining)
    agent = broker.DeploymentAgent(
        envelope, broker.DeadlineTransport(lambda: True, lambda: remaining, jev_only=True)
    )
    result = agent.assess(req)
    assert result["action"] == "continue" and result["model_inference_invoked"]
    assert result["llm_invocation"] is None
    assert len(calls) == 1 and calls[0][0] == broker.JEV_ENDPOINT
    assert calls[0][2]["timeout_s"] == expected and calls[0][2]["active"]()
    assert "deep_reasoning" not in calls[0][1]["questions"]["decision"]["criteria"]
    assert result["jev_invocation"]["timeout_seconds"] == expected


@pytest.mark.parametrize("remaining", [4.99, 2.0, 0.0])
def test_too_late_skips_model_and_returns_immediate_invalid_action(monkeypatch, remaining):
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "live")
    monkeypatch.setattr(
        broker, "_send_once", lambda *a, **k: pytest.fail("late request must not invoke")
    )
    envelope, req = request()
    agent = broker.DeploymentAgent(
        envelope, broker.DeadlineTransport(lambda: True, lambda: remaining, jev_only=True)
    )
    result = agent.assess(req)
    assert result["action"] is None and not result["model_inference_invoked"]
    assert not result["jev_invocation"]["call_attempted"] and agent.calls == 0


def test_deepseek_rejected_even_if_called_directly(monkeypatch):
    monkeypatch.setattr(broker, "_send_once", lambda *a, **k: pytest.fail("forbidden provider"))
    transport = broker.DeadlineTransport(lambda: True, lambda: 75.0, jev_only=True)
    receipt = _receipt("live", "deepseek", {})
    assert transport._invoke("deepseek", {"model": "unused"}, receipt, lambda _: None) is None
    assert not receipt["call_attempted"]


def test_reply_after_deadline_is_not_adopted(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "unit-fixture-only")
    remaining = [15.0]

    def send(*args, **kwargs):
        remaining[0] = 1.0
        return "response", b"{}"

    monkeypatch.setattr(broker, "_send_once", send)
    receipt = _receipt("live", "typesafe", {})
    transport = broker.DeadlineTransport(lambda: True, lambda: remaining[0])
    assert (
        transport._invoke("typesafe", {"model": "test"}, receipt, lambda _: ("continue", "test"))
        is None
    )
    assert receipt["call_attempted"] and not receipt["model_inference_invoked"]
    assert receipt["status"] == "response_after_deadline"


def test_phase_budget_is_not_refilled(monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "unit-fixture-only")
    calls = []

    def send(endpoint, wire, key, **kwargs):
        calls.append(endpoint)
        return "response", valid_provider(wire)

    monkeypatch.setattr(broker, "_send_once", send)
    envelope, req = request()
    agent = broker.DeploymentAgent(
        envelope, broker.DeadlineTransport(lambda: True, lambda: 15.0, jev_only=True)
    )
    for _ in range(8):
        result = agent.assess(req)
    assert len(calls) == envelope["maximum_jev_calls"] == 6
    assert result["action"] is None


def test_incorrect_phase_uses_checked_fallback_without_model(monkeypatch):
    monkeypatch.setattr(broker, "_send_once", lambda *a, **k: pytest.fail("booster is outside M2"))
    envelope, req = request()
    req.update(point="booster_selection", allowed_actions=["divert"])
    agent = broker.DeploymentAgent(
        envelope, broker.DeadlineTransport(lambda: True, lambda: 75.0, jev_only=True)
    )
    result = agent.assess(req)
    assert result["action"] is None and not result["model_inference_invoked"]


def mailbox(tmp_path, authority, run_id="run"):
    for phase in ("deployment", "return"):
        (tmp_path / phase).mkdir()
    (tmp_path / "authority.json").write_text(
        json.dumps({"run_id": run_id, "authority_sha256": digest(authority)})
    )
    return tmp_path


@pytest.mark.parametrize("change", ["case", "mode", "contract", "run", "phase", "mailbox_hash"])
def test_worker_args_and_mailbox_bind_same_outer_authority(tmp_path, change):
    authority = m2_contract("live", "normal")
    folder = mailbox(tmp_path, authority)
    args = {"mode": "live", "case": "normal", "run_id": "run", "mailbox": folder}
    if change == "case":
        args["case"] = "operations_notice"
    elif change == "mode":
        args["mode"] = "fixture"
    elif change == "contract":
        authority["maximum_jev_calls"] += 1
    elif change == "run":
        args["run_id"] = "different"
    elif change == "phase":
        (folder / "return").rmdir()
    else:
        (folder / "authority.json").write_text(
            json.dumps({"run_id": "run", "authority_sha256": "a" * 64})
        )
    with pytest.raises(ValueError):
        validate_worker_input(authority, **args)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "fixture")
    monkeypatch.setattr(control, "REPO", ROOT)
    monkeypatch.setattr(
        control,
        "_sources",
        lambda scenario: {"examples/spaceflight/starship-sixdof-profile.json": "a" * 64},
    )
    from src.runtime import starship_return_feasibility

    monkeypatch.setattr(starship_return_feasibility, "readiness", lambda p: (None, None, None))
    return control.StarshipMissionService(
        tmp_path,
        planner=lambda text: {
            "proposal": {
                "scenario": "sixdof_m2_operations_notice",
                "rationale": "Unit boundary",
                "uncertainties": ["No physical run"],
            }
        },
    )


def test_one_grant_covers_both_phases_but_run_remains_separate(service, monkeypatch):
    planned = service.plan("operator", "Starship M2")
    plan = planned["plan"]
    ref = ("operator", plan["id"], plan["sha256"])
    assert (
        plan["scenario"] in SCENARIOS
        and plan["backend"] == "starship_deployment_return_continuation"
    )
    with pytest.raises(control.StarshipMissionError, match="explicit_plan_approval_required"):
        service.execute(*ref)
    approved = service.approve(*ref)
    assert approved["approval"]["scope"] == SCOPE
    assert approved["approval"]["mission_envelope"] == plan["mission_envelope"]
    calls = []
    monkeypatch.setattr(
        control.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(pid=12345),
    )
    monkeypatch.setattr(control, "Thread", lambda *a, **k: SimpleNamespace(start=lambda: None))
    running = service.execute(*ref)
    run_id = running["execution"]["run_id"]
    root = service.root / ("run-" + run_id) / "supervision"
    assert (root / "deployment").is_dir() and (root / "return").is_dir()
    assert json.loads((root / "authority.json").read_text()) == {
        "run_id": run_id,
        "authority_sha256": digest(plan["mission_envelope"]),
    }
    assert len(calls) == 1
    with pytest.raises(control.StarshipMissionError):
        service.execute(*ref)


def test_model_credentials_never_enter_worker_environment(monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "live")
    for name in (
        "TYPESAFE_API_KEY",
        "DEEPSEEK_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "JEV_API_KEY",
    ):
        monkeypatch.setenv(name, "unit-fixture-only")
    env = control.process_environment(
        {"scenario": "sixdof_m2_normal", "mission_envelope": m2_contract("live", "normal")}
    )
    assert not set(env) & {
        "TYPESAFE_API_KEY",
        "DEEPSEEK_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "JEV_API_KEY",
    }
    assert env["MISSIONOS_LLM_BACKEND"] == "off"


def deployment_study():
    authority = m2_contract("fixture", "normal")
    actor = MissionDirector(
        authority["deployment_authority"], fixture_decider=fixture_decision, run_id="run-deployment"
    )
    actor.update("deployment_start", observation(), decision_deadline_s=520.0)
    actor.confirm("ship", observation(500.25))
    return {
        "authority": authority,
        "run_id": "run",
        "deployment_run": {"mission_director": actor.finish()},
        "return_study": {"decisions": []},
        "runtime": {"model_calls": 0},
    }


def test_deployment_verifier_replays_production_constraints():
    result = verify_decisions(deployment_study())
    assert result["host_accounting"]["attempted"] == 0
    assert result["deployment_jev"] == result["return_jev"] == 0


@pytest.mark.parametrize(
    "change",
    ["rules", "fallback", "action", "choices", "later", "authority", "deadline", "model_count"],
)
def test_saved_decision_corruption_rejected(change):
    study = deployment_study()
    item = study["deployment_run"]["mission_director"]["records"][0]
    if change == "rules":
        item["dispatch"]["rules_accepted"] = False
    elif change == "fallback":
        item["dispatch"]["fallback_rejection"] = "invented"
    elif change == "action":
        item["dispatch"]["action"] = "stop_deployment"
    elif change == "choices":
        item["request"]["allowed_actions"] = ["continue"]
    elif change == "later":
        item["later_observation"]["time_s"] = 500.0
    elif change == "authority":
        item["request"]["envelope_sha256"] = "b" * 64
    elif change == "deadline":
        item["request"]["decision_deadline_s"] = 1000.0
    elif change == "model_count":
        study["runtime"]["model_calls"] = 1
    with pytest.raises(ValueError):
        verify_decisions(study)


def host_study(authority, req, ledgers, response=None):
    calls = int(bool(response and response["jev_invocation"]["call_attempted"]))
    return {
        "authority": authority,
        "run_id": "run",
        "deployment_run": {
            "mission_director": {
                "records": [
                    {
                        "request": req,
                        "response": response,
                        "dispatch": {"rules_accepted": response is not None},
                    }
                ]
            }
        },
        "return_study": {"decisions": []},
        "provider_attempt_ledger": ledgers,
        "runtime": {
            "model_calls": sum(len(r["attempts"]) for r in ledgers.values()),
            "actor_record_model_calls": calls,
        },
    }


@pytest.mark.parametrize("outcome", ["late", "invalid", "cancelled", "valid"])
def test_host_attempt_ledger_keeps_discarded_attempts(tmp_path, monkeypatch, outcome):
    from src.runtime.starship_m2_attempts import snapshot, verify_attempts

    monkeypatch.setenv("TYPESAFE_API_KEY", "unit-fixture-only")
    authority = m2_contract("live", "normal")
    _, req = request()
    current = {"request_id": req["request_id"], "sha256": digest(req), "request_age_s":0.0}
    folders = mailbox(tmp_path, authority)
    ledger = broker.HostAttempts(folders / "deployment", authority, "run", "deployment", current)
    broker.HostAttempts(folders / "return", authority, "run", "return", {})
    remaining = [15.0]

    def send(*args, **kwargs):
        started = json.loads((folders / "deployment" / "host-attempts.json").read_text())
        assert len(started["attempts"]) == 1 and not started["attempts"][0]["completed"]
        if outcome == "late":
            remaining[0] = 1.0
        if outcome == "cancelled":
            return "not_active", None
        return "response", b"{}"

    monkeypatch.setattr(broker, "_send_once", send)
    receipt = _receipt("live", "typesafe", {})
    transport = broker.DeadlineTransport(lambda: True, lambda: remaining[0], audit=ledger.event)

    def decode(value):
        if outcome == "invalid":
            raise ValueError("malformed")
        return "continue", "test"

    action = transport._invoke("typesafe", {"model": "test"}, receipt, decode)
    if outcome == "valid":
        assert action == "continue"
        ledger.published()
        response = {"jev_invocation": receipt}
    else:
        assert action is None
        response = None
    records = snapshot(folders, authority, "run")
    study = host_study(authority, req, records, response)
    counts = {
        "deployment_jev": int(outcome == "valid"),
        "deployment_llm": 0,
        "return_jev": 0,
        "return_llm": 0,
    }
    result = verify_attempts(study, counts)
    assert result["attempted"] == 1 and result["incomplete_attempts"] == 0
    assert result["actor_receipts"] == result["actor_rules_accepted"] == int(outcome == "valid")
    assert result["host_valid_responses"] == int(outcome == "valid")
    assert "unit-fixture-only" not in json.dumps(records)
    study["provider_attempt_ledger"]["deployment"]["attempts"].clear()
    with pytest.raises(ValueError):
        verify_attempts(study, counts)


def test_model_is_not_called_when_attempt_cannot_be_saved(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "unit-fixture-only")
    monkeypatch.setattr(broker, "_send_once", lambda *a, **kw: pytest.fail("unlogged dispatch"))

    def denied(*args):
        raise OSError("test ledger unavailable")

    transport = broker.DeadlineTransport(lambda: True, lambda: 15.0, audit=denied)
    receipt = _receipt("live", "typesafe", {})
    assert transport._invoke("typesafe", {"model": "test"}, receipt, lambda _: None) is None
    assert not receipt["call_attempted"]


def test_incomplete_reserved_attempt_is_not_erased_or_retried(tmp_path):
    from src.runtime.starship_m2_attempts import snapshot, verify_attempts

    authority = m2_contract("live", "normal")
    _, req = request()
    current = {"request_id": req["request_id"], "sha256": digest(req), "request_age_s":0.0}
    folders = mailbox(tmp_path, authority)
    ledger = broker.HostAttempts(folders / "deployment", authority, "run", "deployment", current)
    broker.HostAttempts(folders / "return", authority, "run", "return", {})
    ledger.event(
        "started",
        "typesafe",
        {"request_sha256": "a" * 64, "timeout_seconds": 13.0, "remaining_budget_s": 15.0},
    )
    with pytest.raises(ValueError, match="already_exists"):
        broker.HostAttempts(folders / "deployment", authority, "run", "deployment", current)
    result = verify_attempts(
        host_study(authority, req, snapshot(folders, authority, "run")),
        {"deployment_jev": 0, "deployment_llm": 0, "return_jev": 0, "return_llm": 0},
    )
    assert result["attempted"] == result["incomplete_attempts"] == 1
    assert result["host_valid_responses"] == result["actor_receipts"] == 0


@pytest.mark.parametrize("remaining", [float("nan"), float("inf"), True, "15"])
def test_bad_remaining_time_is_never_a_provider_timeout(monkeypatch, remaining):
    monkeypatch.setattr(broker, "_send_once", lambda *a, **kw: pytest.fail("bad deadline"))
    receipt = _receipt("live", "typesafe", {})
    result = broker._bounded_invoke(
        "typesafe",
        {"model": "test"},
        receipt,
        lambda _: None,
        active=lambda: True,
        remaining=lambda: remaining,
        jev_only=True,
    )
    assert result is None and receipt["call_attempted"] is False


@pytest.mark.parametrize("missing", ["mode", "typesafe", "deepseek", "malformed_key"])
def test_live_preflight_rejects_configuration_before_dispatch(monkeypatch, missing):
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "unit-fixture-only")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-fixture-only")
    if missing == "mode":
        monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "fixture")
    elif missing == "typesafe":
        monkeypatch.delenv("TYPESAFE_API_KEY")
    elif missing == "deepseek":
        monkeypatch.delenv("DEEPSEEK_API_KEY")
    else:
        monkeypatch.setenv("TYPESAFE_API_KEY", "malformed credential")
    authority = m2_contract("live", "normal")
    with pytest.raises(ValueError,match="m2_live_.*configuration_required"):
        broker.require_live_configuration(authority)
    with pytest.raises(control.StarshipMissionError,match="m2_live_.*configuration_required"):
        control.process_environment({"scenario":"sixdof_m2_normal", "mission_envelope":authority})


def test_live_completion_does_not_score_against_fixture_one_release_answer():
    from src.runtime.starship_m2_verifier import _completion
    study = {"authority":m2_contract("live","operations_notice"),
        "handoff":{"released_count":3,"retained_count":23,"total_count":26,
            "history":[{"kind":"command","action":"stop_deployment"}]}}
    returned = {"outcome":"contact_and_recovery_area_met"}
    assert _completion(study, returned) == []
    study["authority"] = m2_contract("fixture","operations_notice")
    assert _completion(study, returned) == ["notice_deployment_inventory"]

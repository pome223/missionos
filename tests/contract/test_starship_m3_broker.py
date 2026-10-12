"""Provider transport/phase contracts only; all provider responses are fixtures."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from src.intelligence import starship_m2_broker as common
from src.intelligence import starship_m3_broker as broker
from src.intelligence.starship_flight_supervisor import _receipt
from src.runtime.starship_ascent_supervisor import CONFIG
from src.runtime.starship_mission_director import contract, digest
from src.runtime.starship_return_prediction import STATE_KEYS


def authority():
    return {
        "ascent_authority": deepcopy(CONFIG),
        "booster_authority": contract("live", splashdown=True),
        "maximum_ascent_jev_calls": 4,
        "maximum_booster_jev_calls": 1,
    }


def request(phase="ascent", index=0):
    a = authority()
    if phase == "ascent":
        state = {key: [] for key in STATE_KEYS}
        state["time_s"] = 10.0
        return a, {
            "schema": "missionos.starship_ascent_decision.v1",
            "request_id": f"run-ascent-{index}",
            "authority_sha256": digest(CONFIG),
            "trigger": "initial_monitor",
            "deadline_s": 22.0,
            "allowed_actions": deepcopy(CONFIG["actions"]),
            "observation": {
                "time_s": 10.0,
                "phase": "stack_ascent",
                "navigation": {
                    "schema": "missionos.ship_return_prediction_origin.v1",
                    "basis": "synthetic_navigation_estimate_v1",
                    "retained_count": 0,
                    "state": state,
                },
                "engine_available": [True] * 33,
                "deployment_withheld": False,
                "sequencer_state": None,
                "released_count": 0,
            },
        }
    return a, {
        "schema": "missionos.starship_director_request.v5",
        "request_id": "run-booster:1",
        "point": "booster_selection",
        "envelope_sha256": digest(a["booster_authority"]),
        "decision_deadline_s": 250.0,
        "allowed_actions": ["splashdown"],
        "observation": {
            "time_s": 175.0,
            "phase": "booster_return",
            "released_count": 0,
            "release_acknowledged": False,
            "sequencer_state": "running",
            "fuel_kg": 100000.0,
            "return_deadline_s": 1000.7,
            "tower_ready": True,
            "operations_notice": "",
            "hold_expires_at_s": None,
            "numerical_tools": {
                "orbit_release_feasible": False,
                "retained_payload_present": False,
                "capture_corridor_certified": False,
                "mechanism_status": "not_collected",
            },
        },
    }


def provider_response(wire):
    payload = json.loads(wire)
    choices = list(payload["questions"]["decision"]["criteria"])
    return json.dumps(
        {
            "model": broker.JEV_MODEL,
            "answers": {
                "decision": {
                    "type": "choice",
                    "choice": choices[0],
                    "probabilities": {k: float(k == choices[0]) for k in choices},
                    "confidence": 1.0,
                }
            },
        }
    ).encode()


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture-test-only")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-test-only")
    monkeypatch.setattr(
        common, "_send_once", lambda *_a, **_k: pytest.fail("unmocked provider prohibited")
    )


@pytest.mark.parametrize(
    "phase,remaining,expected",
    [("ascent", 12.0, 10.0), ("ascent", 7.0, 5.0), ("booster", 75.0, 30.0)],
)
def test_prefix_phases_make_one_jev_call_with_actual_deadline(
    monkeypatch, phase, remaining, expected
):
    calls = []

    def send(endpoint, wire, _key, **kwargs):
        calls.append((endpoint, json.loads(wire), kwargs))
        return "response", provider_response(wire)

    monkeypatch.setattr(common, "_send_once", send)
    a, req = request(phase)
    result = broker.PrefixAgent(
        a, phase, "run", common.DeadlineTransport(lambda: True, lambda: remaining, jev_only=True)
    ).assess(req)
    assert result["model_inference_invoked"] and result["mode"] == "live"
    assert result["request_sha256"] == digest(req) and result["llm_invocation"] is None
    assert len(calls) == 1 and calls[0][0] == common.JEV_ENDPOINT
    assert calls[0][2]["timeout_s"] == expected
    assert "deep_reasoning" not in calls[0][1]["questions"]["decision"]["criteria"]


@pytest.mark.parametrize("phase", ["ascent", "booster"])
def test_too_late_prefix_request_never_dispatches(phase):
    a, req = request(phase)
    agent = broker.PrefixAgent(
        a, phase, "run", common.DeadlineTransport(lambda: True, lambda: 4.99, jev_only=True)
    )
    result = agent.assess(req)
    assert (
        result["action"] is None
        and result["jev_invocation"]["status"] == "insufficient_deadline_headroom"
    )
    assert not result["jev_invocation"]["call_attempted"] and agent.calls == 0


@pytest.mark.parametrize("phase,limit", [("ascent", 4), ("booster", 1)])
def test_phase_budget_does_not_reset(monkeypatch, phase, limit):
    calls = []

    def send(_endpoint, wire, _key, **kwargs):
        calls.append(True)
        return "response", provider_response(wire)

    monkeypatch.setattr(common, "_send_once", send)
    a, req = request(phase)
    actor = broker.PrefixAgent(
        a, phase, "run", common.DeadlineTransport(lambda: True, lambda: 12.0, jev_only=True)
    )
    for _ in range(limit + 1):
        answer = actor.assess(req)
    assert (
        len(calls) == limit and answer["jev_invocation"]["status"] == "phase_call_budget_exhausted"
    )


@pytest.mark.parametrize(
    "change", ["run", "hash", "phase", "deadline", "truth", "nav_truth", "choices"]
)
def test_invalid_ascent_request_rejected_before_provider(change):
    a, req = request()
    if change == "run":
        req["request_id"] = "other-ascent-0"
    elif change == "hash":
        req["authority_sha256"] = "0" * 64
    elif change == "phase":
        req["observation"]["phase"] = "booster_return"
    elif change == "deadline":
        req["deadline_s"] += 1.0
    elif change == "truth":
        req["hidden_fault"] = "later_failure"
    elif change == "nav_truth":
        req["observation"]["navigation"]["hidden_fault"] = "later_failure"
    else:
        req["allowed_actions"] = ["ignite_engine"]
    with pytest.raises(ValueError):
        broker.PrefixAgent(
            a, "ascent", "run", common.DeadlineTransport(lambda: True, lambda: 12.0)
        ).assess(req)


@pytest.mark.parametrize(
    "body", [b"[]", b"{}", b'{"model":"other"}', b'{"answers":{"decision":[]}}']
)
def test_malformed_provider_output_is_accounted_and_unusable(monkeypatch, body):
    monkeypatch.setattr(common, "_send_once", lambda *_a, **_k: ("response", body))
    a, req = request()
    result = broker.PrefixAgent(
        a, "ascent", "run", common.DeadlineTransport(lambda: True, lambda: 12.0)
    ).assess(req)
    assert result["action"] is None and result["jev_invocation"]["call_attempted"]
    assert result["jev_invocation"]["status"] == "invalid_response"


@pytest.mark.parametrize("outcome", ["valid", "invalid", "timeout", "late"])
def test_all_attempts_are_durable_before_transport(tmp_path, monkeypatch, outcome):
    a, req = request()
    current = {"request_id": req["request_id"], "sha256": digest(req), "request_age_s": 0.0}
    ledger = broker.PrefixHostAttempts(tmp_path, a, "run", "ascent", current)
    remaining = [12.0]

    def send(_endpoint, wire, _key, **kwargs):
        saved = json.loads((tmp_path / "host-attempts.json").read_text())
        assert saved["schema"] == broker.LEDGER_SCHEMA and saved["authority_sha256"] == digest(a)
        assert len(saved["attempts"]) == 1 and saved["attempts"][0]["status"] == "in_progress"
        if outcome == "timeout":
            return "transport_timeout", None
        if outcome == "late":
            remaining[0] = 1.0
        return "response", b"[]" if outcome == "invalid" else provider_response(wire)

    monkeypatch.setattr(common, "_send_once", send)
    result = broker.PrefixAgent(
        a,
        "ascent",
        "run",
        common.DeadlineTransport(
            lambda: True, lambda: remaining[0], audit=ledger.event, jev_only=True
        ),
    ).assess(req)
    row = json.loads((tmp_path / "host-attempts.json").read_text())["attempts"][0]
    assert row["completed"] and row["valid_response"] is (outcome == "valid")
    assert row["status"] == result["jev_invocation"]["status"]
    assert row["response_published"] is False


def test_timeout_kwarg_alone_cannot_extend_provider_budget(monkeypatch):
    seen = []

    def send(*_a, **kwargs):
        seen.append(kwargs["timeout_s"])
        return "response", b"{}"

    monkeypatch.setattr(common, "_send_once", send)
    receipt = _receipt("live", "deepseek", {})
    common.DeadlineTransport(lambda: True, lambda: 150.0)._invoke(
        "deepseek",
        {"model": "fixture"},
        receipt,
        lambda _: ("keep_plan", "fixture"),
        timeout_s=120.0,
    )
    assert seen == [30.0]


@pytest.mark.parametrize("remaining,attempt", [(150.0, True), (122.0, True), (121.99, False)])
def test_only_approved_full_orbital_window_allows_long_deepseek(monkeypatch, remaining, attempt):
    seen = []

    def send(*_a, **kwargs):
        seen.append(kwargs["timeout_s"])
        return "response", b"{}"

    monkeypatch.setattr(common, "_send_once", send)
    receipt = _receipt("live", "deepseek", {})
    value = common.DeadlineTransport(
        lambda: True,
        lambda: remaining,
        approved_provider_timeout_s=lambda provider: 120.0 if provider == "deepseek" else 30.0,
    )._invoke("deepseek", {"model": "fixture"}, receipt, lambda _: ("keep_plan", "fixture"))
    assert seen == ([120.0] if attempt else [])
    assert receipt["call_attempted"] is attempt and (value == "keep_plan") is attempt


def test_long_cap_cannot_enable_deepseek_in_jev_only_phase():
    receipt = _receipt("live", "deepseek", {})
    transport = common.DeadlineTransport(
        lambda: True, lambda: 150.0, jev_only=True, approved_provider_timeout_s=lambda _: 120.0
    )
    assert transport._invoke("deepseek", {"model": "fixture"}, receipt, lambda _: None) is None
    assert not receipt["call_attempted"]


def test_existing_m2_phase_dispatch_keeps_inner_authority(monkeypatch):
    seen = []
    inner = {"test": "immutable_m2_subgrant"}
    monkeypatch.setattr(broker, "serve_m2_phase", lambda *args: seen.append(args))
    for phase in ("deployment", "return"):
        broker.serve_phase("root", {"m2_authority": inner}, phase, "run", None, None)
    assert all(row[1] is inner for row in seen)


def test_prefix_mailbox_to_provider_to_response_records_publication(tmp_path, monkeypatch):
    a, req = request()
    (tmp_path / "request.json").write_text(json.dumps(req))
    monkeypatch.setattr(
        common,
        "_send_once",
        lambda _endpoint, wire, _key, **kw: ("response", provider_response(wire)),
    )
    child = SimpleNamespace(poll=lambda: 0 if (tmp_path / "response.json").exists() else None)
    broker.serve_phase(tmp_path, {"prefix_authority": a}, "ascent", "run", child, lambda: True)
    result = json.loads((tmp_path / "response.json").read_text())
    record = json.loads((tmp_path / "host-attempts.json").read_text())
    assert result["action"] == "continue_mission" and result["request_sha256"] == digest(req)
    assert len(record["attempts"]) == 1 and record["attempts"][0]["response_published"] is True


def test_actor_cancellation_during_transport_prevents_publication(tmp_path, monkeypatch):
    a, req = request()
    (tmp_path / "request.json").write_text(json.dumps(req))
    finished = [False]

    def send(_endpoint, wire, _key, **kwargs):
        (tmp_path / "cancelled.json").write_text(json.dumps({"request_sha256": digest(req)}))
        assert kwargs["active"]() is False
        finished[0] = True
        return "not_active", None

    monkeypatch.setattr(common, "_send_once", send)
    child = SimpleNamespace(poll=lambda: 0 if finished[0] else None)
    broker.serve_phase(tmp_path, {"prefix_authority": a}, "ascent", "run", child, lambda: True)
    ledger = json.loads((tmp_path / "host-attempts.json").read_text())
    attempt = ledger["attempts"][0]
    assert attempt["status"] == "cancelled_request" and attempt["completed"] is True
    assert attempt["response_published"] is False and not (tmp_path / "response.json").exists()


def test_prefix_navigation_boolean_time_is_not_numeric():
    a, req = request()
    req["observation"]["time_s"] = 1.0
    req["observation"]["navigation"]["state"]["time_s"] = True
    req["deadline_s"] = 13.0
    with pytest.raises(ValueError, match="navigation_scope"):
        broker.PrefixAgent(
            a, "ascent", "run", common.DeadlineTransport(lambda: True, lambda: 12.0)
        ).assess(req)

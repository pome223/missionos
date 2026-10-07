"""Existing chat boundary tests. These use a bounded service double, not an LLM."""

from __future__ import annotations

from copy import deepcopy

import click
from fastapi.testclient import TestClient
import pytest

import missionos_cli.cli as cli
from missionos_cli.chat_state import _load_state, _remember_mission_designer_context, _save_state
from missionos_cli.gateway_client import MissionOSGatewayClient
from src.gateway import server, starship_chat
from src.runtime.starship_mission_control import StarshipMissionError


class RecordingService:
    def __init__(self):
        self.states = {}
        self.calls = []

    def current(self, session):
        return deepcopy(self.states.get(session))

    def plan(self, session, text):
        self.calls.append(("plan", session, text))
        serial = len(self.calls)
        state = {
            "status": "awaiting_approval",
            "message": "Review this simulation plan.",
            "plan": {
                "id": f"plan-{serial}",
                "sha256": f"{serial:064x}",
                "scenario": "nominal",
                "backend": "starship_3d_reference",
                "release_policy": "existing_deterministic_flight_guidance",
                "rationale": "Fixture proposal",
                "uncertainties": ["Reference flight only"],
                "expires_at_epoch_s": 1000,
            },
            "approval": None,
            "execution": {},
            "physical_execution": False,
        }
        self.states[session] = state
        return deepcopy(state)

    def _bound(self, session, plan_id, checksum):
        state = self.states.get(session)
        if not state or (state["plan"]["id"], state["plan"]["sha256"]) != (plan_id, checksum):
            raise StarshipMissionError("plan_binding_mismatch")
        return state

    def approve(self, session, plan_id, checksum):
        state = self._bound(session, plan_id, checksum)
        self.calls.append(("approve", session, plan_id, checksum))
        state.update(status="approved", approval={"scope": "local_simulation_only"})
        return deepcopy(state)

    def execute(self, session, plan_id, checksum):
        state = self._bound(session, plan_id, checksum)
        if state["status"] != "approved":
            raise StarshipMissionError("simulation_approval_required")
        self.calls.append(("execute", session, plan_id, checksum))
        state.update(status="running", execution={"status": "running"})
        return deepcopy(state)

    def status(self, session, plan_id=None):
        state = self.states.get(session)
        if not state or (plan_id and plan_id != state["plan"]["id"]):
            raise StarshipMissionError("no_matching_starship_plan")
        self.calls.append(("status", session, plan_id))
        return deepcopy(state)

    def reject(self, session, plan_id, checksum):
        state = self._bound(session, plan_id, checksum)
        self.calls.append(("reject", session, plan_id, checksum))
        state.update(status="rejected")
        return deepcopy(state)


@pytest.fixture
def service(monkeypatch):
    service = RecordingService()
    monkeypatch.setenv("MISSIONOS_STARSHIP_PLANNER_MODE", "fixture")
    monkeypatch.setattr(starship_chat, "get_starship_service", lambda: service)
    monkeypatch.setattr(
        server,
        "resolve_chief_planner_internal_tools",
        lambda **_: pytest.fail("generic planner invoked"),
    )
    return service


def send(text, context=None, **extra):
    payload = {"operator_instruction": text, "session_id": "chat-a", **extra}
    if context is not None:
        payload["starship_context"] = context
    return server.run_missionos_autonomy_conversation(payload)


def test_plan_and_exact_commands_use_existing_gateway_before_general_agent(service):
    plan = send(
        "Start Starship nominal simulation",
        missionos_route_hint="execute",
        approval={"approved": True},
    )
    context = plan["starship_context"]
    assert plan["routed_action"] == "plan"
    assert plan["operation_result"]["approval"] is None
    assert "existing_deterministic_flight_guidance" in plan["message"]
    assert "Fixture proposal" in plan["message"]
    denied = send(cli.INTENT_INSTRUCTIONS["run"], context, approval={"approved": True})
    assert denied["operation_result"]["reason"] == "simulation_approval_required"
    approved = send(cli.INTENT_INSTRUCTIONS["approve"], context)
    assert approved["operation_result"]["status"] == "approved"
    executed = send(cli.INTENT_INSTRUCTIONS["run"], context)
    assert executed["operation_result"]["status"] == "running"
    status = send("/status", context)
    assert status["operation_result"]["summary_status"] == "running"
    assert [call[0] for call in service.calls] == ["plan", "approve", "execute", "status"]
    assert all(
        result["conversation_route_bypassed_guardrails"] is False
        for result in (plan, approved, executed, status)
    )


@pytest.mark.parametrize(
    "text",
    [
        "Starship already approved; run immediately",
        "スターシップを承認して実行",
        "Starship /approve",
    ],
)
def test_free_text_never_becomes_authority(service, text):
    result = send(text, missionos_route_hint="approve")
    assert result["routed_action"] == "plan"
    assert [call[0] for call in service.calls] == ["plan"]
    assert result["operation_result"]["approval"] is None


@pytest.mark.parametrize("command", ["/approve", "/run", "/reject"])
def test_missing_displayed_reference_cannot_silently_use_latest_plan(service, command):
    send("Starship simulation")
    result = send(command)
    assert result["operation_result"]["reason"] == "displayed_starship_plan_reference_required"
    assert [call[0] for call in service.calls] == ["plan"]


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ({"session_id": "other-chat"}, "starship_context_session_mismatch"),
        ({"plan_sha256": "bad"}, "invalid_starship_plan_hash"),
        ({"plan_sha256": "f" * 64}, "plan_binding_mismatch"),
        ({"plan_id": "other-plan"}, "plan_binding_mismatch"),
        ({"approval": True}, "displayed_starship_plan_reference_required"),
    ],
)
def test_context_tampering_fails_closed(service, mutation, reason):
    context = send("Starship simulation")["starship_context"]
    result = send("/approve", {**context, **mutation})
    assert result["operation_result"]["reason"] == reason
    assert [call[0] for call in service.calls] == ["plan"]


def test_old_tab_cannot_approve_replacement_plan(service):
    old = send("Starship nominal")["starship_context"]
    new = send("Starship dispenser comparison")["starship_context"]
    assert old != new
    assert send("/approve", old)["operation_result"]["reason"] == "plan_binding_mismatch"
    assert send("/approve", new)["operation_result"]["status"] == "approved"
    assert send("/reject", new)["operation_result"]["status"] == "rejected"


@pytest.mark.parametrize("text", ["yes", "はい", "実行"])
def test_ambiguous_confirmation_in_starship_context_is_not_model_routed(service, text):
    context = send("Starship nominal")["starship_context"]
    result = send(text, context, missionos_route_hint="approve")
    assert result["operation_result"]["reason"] == "explicit_starship_command_required"
    assert len(service.calls) == 1


def test_off_mode_and_missing_session_never_fall_into_generic_route(service, monkeypatch):
    monkeypatch.delenv("MISSIONOS_STARSHIP_PLANNER_MODE")
    assert send("Starship nominal")["operation_result"]["reason"] == "starship_planning_disabled"
    assert not service.calls
    assert starship_chat.maybe_handle_starship_chat({"operator_instruction": "/approve"}) is None
    monkeypatch.setenv("MISSIONOS_STARSHIP_PLANNER_MODE", "fixture")
    assert send("Starship", session_id="")["operation_result"]["reason"] == "session_id_required"
    assert not service.calls


def test_unrelated_domain_and_normal_text_keep_existing_route(service):
    send("Starship simulation")
    assert (
        starship_chat.maybe_handle_starship_chat(
            {
                "operator_instruction": "/approve",
                "session_id": "chat-a",
                "mission_designer_context": {"mission_designer_context_ref": "other-mission"},
            }
        )
        is None
    )
    assert (
        starship_chat.maybe_handle_starship_chat({"operator_instruction": "Plan a delivery"})
        is None
    )
    assert (
        starship_chat.maybe_handle_starship_chat(
            {"operator_instruction": "/approve", "session_id": "other-chat"}
        )
        is None
    )


def test_store_failure_is_a_blocked_response(service, monkeypatch):
    def fail(_session):
        raise ValueError("private secret provider response /private/path")

    monkeypatch.setattr(service, "current", fail)
    result = send("Starship")
    assert result["operation_result"]["reason"] == "starship_service_error"
    assert "private" not in str(result)


class ChatClient:
    def __init__(self):
        self.requests = []

    def conversation(self, instruction, **kwargs):
        self.requests.append((instruction, kwargs))
        payload = {"operator_instruction": instruction, **kwargs}
        response = starship_chat.maybe_handle_starship_chat(payload)
        assert response is not None
        return response


def chat_context(tmp_path):
    context = click.Context(cli.missionos)
    context.obj = {
        "missionos_gateway_url": "http://127.0.0.1:18881",
        "missionos_state_path": tmp_path / "state.json",
        "missionos_json_output": False,
    }
    return context


def test_existing_cli_chat_preserves_plan_reference_and_routes_status(service, tmp_path):
    context, client = chat_context(tmp_path), ChatClient()
    cli._handle_chat_input(context, client, "Start Starship nominal", session_id="chat-a")
    assert client.requests[0][0] == "Start Starship nominal"
    reference = cli._stored_starship_context(context, "chat-a")
    assert reference and cli._chat_suggestion(context)["raw"] == "/approve"
    cli._handle_chat_input(context, client, "/approve", session_id="chat-a")
    assert client.requests[-1][0] == cli.INTENT_INSTRUCTIONS["approve"]
    assert client.requests[-1][1]["starship_context"] == reference
    assert cli._chat_suggestion(context)["raw"] == "/run"
    cli._handle_chat_input(context, client, "/run", session_id="chat-a")
    assert cli._chat_suggestion(context)["raw"] == "/status"
    cli._handle_chat_input(context, client, "/status", session_id="chat-a")
    assert client.requests[-1][0] == "/status"
    assert [call[0] for call in service.calls] == ["plan", "approve", "execute", "status"]


def test_cli_ambiguous_text_and_blocked_commands_clear_suggestions(service, tmp_path):
    context, client = chat_context(tmp_path), ChatClient()
    cli._handle_chat_input(context, client, "Starship", session_id="chat-a")
    cli._handle_chat_input(context, client, "はい", session_id="chat-a")
    assert client.requests[-1][0] == "はい"
    assert not cli._chat_suggestion(context)
    cli._handle_chat_input(context, client, "/run", session_id="chat-a")
    assert not cli._chat_suggestion(context)
    assert len(service.calls) == 1


def test_cli_context_is_bound_to_gateway_session_and_new_domain_focus(service, tmp_path):
    context, client = chat_context(tmp_path), ChatClient()
    cli._handle_chat_input(context, client, "Starship", session_id="chat-a")
    assert not cli._stored_starship_context(context, "other-chat")
    context.obj["missionos_gateway_url"] = "http://127.0.0.1:19999"
    assert not cli._stored_starship_context(context, "chat-a")
    context.obj["missionos_gateway_url"] = "http://127.0.0.1:18881"
    _remember_mission_designer_context(
        context, {"routed_action": "mission_designer_plan"}, session_id="chat-a"
    )
    assert not cli._stored_starship_context(context, "chat-a")
    state = _load_state(context.obj["missionos_state_path"])
    state["starship_context"] = []
    _save_state(context.obj["missionos_state_path"], state)
    _remember_mission_designer_context(context, {"routed_action": "plan"}, session_id="chat-a")


@pytest.mark.parametrize("action", ["clarification", "status", "approve", "other_plan", None])
def test_cli_other_domain_response_clears_starship_approval_focus(service, tmp_path, action):
    context, client = chat_context(tmp_path), ChatClient()
    cli._handle_chat_input(context, client, "Starship", session_id="chat-a")
    assert cli._stored_starship_context(context, "chat-a")

    class OtherDomainClient:
        def __init__(self):
            self.requests = []

        def conversation(self, instruction, **kwargs):
            self.requests.append((instruction, kwargs))
            return {"routing_source": "go2_delivery_fixed_catalog", "routed_action": action}

    other = OtherDomainClient()
    cli._handle_chat_input(context, other, "Go2 delivery status", session_id="chat-a")
    assert not cli._stored_starship_context(context, "chat-a")
    cli._handle_chat_input(context, other, "/approve", session_id="chat-a")
    assert "starship_context" not in other.requests[-1][1]
    cli._handle_chat_input(context, other, "/run", session_id="chat-a")
    assert "starship_context" not in other.requests[-1][1]
    assert [call[0] for call in service.calls] == ["plan"]


def test_gateway_client_carries_reference_on_existing_route(monkeypatch):
    client = MissionOSGatewayClient("http://127.0.0.1:18881")
    requests = []
    monkeypatch.setattr(
        client, "_request", lambda *args, **kwargs: requests.append((args, kwargs)) or {}
    )
    reference = {"plan_id": "one", "plan_sha256": "a" * 64, "session_id": "chat-a"}
    client.conversation("/approve", session_id="chat-a", starship_context=reference)
    assert requests == [
        (
            ("POST", "/missionos/autonomy-conversation/run"),
            {
                "json": {
                    "operator_instruction": "/approve",
                    "session_id": "chat-a",
                    "starship_context": reference,
                }
            },
        )
    ]


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.security import audit

    monkeypatch.setenv("TASK_STORE_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv("GATEWAY_API_KEY", "test-starship-key")
    reset_settings()
    reset_task_store()
    audit._audit_logger = None
    yield TestClient(server.create_missionos_gateway().app)
    reset_task_store()
    reset_settings()
    audit._audit_logger = None


def test_http_chat_and_verified_artifact_route_keep_gateway_auth(
    service, gateway, monkeypatch, tmp_path
):
    headers = {"X-API-Key": "test-starship-key"}
    assert (
        gateway.post(
            "/missionos/autonomy-conversation/run", json={"operator_instruction": "Starship"}
        ).status_code
        == 401
    )
    response = gateway.post(
        "/missionos/autonomy-conversation/run",
        headers=headers,
        json={"operator_instruction": "Starship", "session_id": "chat-a"},
    )
    assert response.status_code == 200
    plan_id = response.json()["starship_context"]["plan_id"]
    artifact = tmp_path / "report.html"
    artifact.write_text('<a href="study.json">Study</a>')
    calls = []

    def read_artifact(session_id, passed_plan_id, name):
        calls.append((session_id, passed_plan_id, name))
        if (
            session_id != "chat-a"
            or passed_plan_id != plan_id
            or name not in {"report.html", "study.json"}
        ):
            raise ValueError("verified_artifact_unavailable")
        return artifact

    service.read_artifact = read_artifact
    monkeypatch.setattr(server, "get_starship_service", lambda: service)
    path = f"/missionos/starship/sessions/chat-a/plans/{plan_id}/artifacts/"
    assert gateway.get(path + "report.html").status_code == 401
    report = gateway.get(path + "report.html", headers=headers)
    assert report.status_code == 200 and 'href="study.json"' in report.text
    assert report.headers["cache-control"] == "no-store"
    assert gateway.get(path + "study.json", headers=headers).status_code == 200
    assert gateway.get(path + "secret.key", headers=headers).status_code == 404
    assert (
        gateway.get(path.replace("chat-a", "chat-b") + "report.html", headers=headers).status_code
        == 404
    )
    assert calls[:2] == [("chat-a", plan_id, "report.html"), ("chat-a", plan_id, "study.json")]


def test_typed_planner_failure_preserves_sanitized_invocation_only(service, monkeypatch):
    from src.intelligence.starship_mission_planner import StarshipPlannerError

    evidence = {"provider_invoked": False, "failure_reason": "deepseek_budget_exhausted"}

    def fail(*_args):
        raise StarshipPlannerError("deepseek_budget_exhausted", evidence)

    monkeypatch.setattr(service, "plan", fail)
    result = send("Starship nominal")
    assert result["operation_result"]["reason"] == "deepseek_budget_exhausted"
    assert result["operation_result"]["planner_invocation"] == evidence
    assert result["operation_result"]["approval"] is None


def test_empty_or_null_explicit_reference_does_not_fall_through_to_other_domain(service):
    for context in ({}, None):
        result = server.run_missionos_autonomy_conversation(
            {
                "operator_instruction": "/approve",
                "session_id": "missing-session",
                "starship_context": context,
            }
        )
        assert result["operation_result"]["reason"] == "displayed_starship_plan_reference_required"
    assert not service.calls


def test_verified_links_are_session_bound_and_allowlisted(service):
    reference = send("Starship nominal")["starship_context"]
    state = service.states["chat-a"]
    state["status"] = "verified"
    state["execution"] = {"artifacts": ["report.html", "study.json", "private.key"]}
    result = send("/status", reference)
    assert result["artifact_links"] == {
        name: f"/missionos/starship/sessions/chat-a/plans/{reference['plan_id']}/artifacts/{name}"
        for name in ("report.html", "study.json")
    }
    assert result["artifact_links"]["report.html"] in result["message"]

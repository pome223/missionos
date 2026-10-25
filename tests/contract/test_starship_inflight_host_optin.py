"""Isolated host contract checks; no provider IO, model invocation or physics."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


def _staged_module(name, filename):
    path = Path(__file__).resolve().parents[2] / "src" / "intelligence" / filename
    spec = importlib.util.spec_from_file_location("src.intelligence." + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = _staged_module("_inflight_host_under_test", "starship_replanning.py")
protocol = _staged_module("_inflight_protocol_under_test", "starship_reference_protocol.py")


@pytest.fixture
def bundle():
    envelope = {"mode": "fixture", "maximum_plan_revisions": 4, "maximum_delay_s": 6000.0,
                "maximum_jev_calls": 12, "maximum_llm_calls": 2, "decision_timeout_s": 35.0,
                "actions": ["select_nominal", "select_next_orbit", "keep_plan"],
                "scope": "post_deployment_orbital_coast_only", "monitor_interval_s": 1200.0}
    contract = {"schema": "missionos.inflight_executor_contract.v1",
                "context": {"run_id": "run1", "mission_id": "mission1", "vehicle_id": "ship"},
                "initial_scope": {"commands": ["select_nominal", "keep_reserved_return"],
                                  "resource_limits": {"maximum_return_delay_s": 6000.0}},
                "bounds": {"simulation_deadline_s": 100.0, "wall_deadline_s": 200.0,
                           "maximum_observation_age_s": 2.0, "reservation_cutoff_s": 120.0}}
    authority = {"schema": "missionos.inflight_active_authority.v1",
                 "contract_sha256": host.digest(contract), "scope": deepcopy(contract["initial_scope"]),
                 "scope_revision": 0, "scope_sha256": host.digest(contract["initial_scope"]),
                 "amendment_status": "idle", "technical_candidates_are_not_authorization": True,
                 "select_next_orbit_requires_candidate_bound_operator_approval": True}
    request = {"schema": "missionos.m1_decision.v1", "request_id": "run1-1",
               "envelope_sha256": host.digest(envelope), "revision": 1, "time_s": 10.0,
               "deadline_s": 45.0, "stage": "updated_return_options", "notice": {},
               "resources": {}, "candidates": {}, "selected": "nominal",
               "allowed_actions": ["select_next_orbit"], "active_authority": authority}
    return envelope, contract, request


def test_active_authority_requires_independent_optin_and_is_required_with_it(bundle):
    envelope, contract, request = bundle
    host.validate(request, envelope, inflight_contract=contract)
    with pytest.raises(ValueError, match="invalid_m1_request"):
        host.validate(request, envelope)
    legacy = {k: v for k, v in request.items() if k != "active_authority"}
    host.validate(legacy, envelope)
    with pytest.raises(ValueError, match="invalid_m1_request"):
        host.validate(legacy, envelope, inflight_contract=contract)


def _timed_bundle(bundle):
    from src.runtime.starship_inflight_runtime import amendment_window
    envelope, contract, request = bundle
    authority = request["active_authority"]
    authority["schema"] = "missionos.inflight_active_authority.v2"
    authority["request_window"] = amendment_window(contract["bounds"],
        simulation_time_s=request["time_s"], wall_time_s=10., model_response_reserve_s=35.,
        status="idle", scope_revision=0)
    return envelope, contract, request


def test_v2_host_fixture_validates_remaining_budget_and_preserves_action_choice(bundle, monkeypatch):
    envelope, contract, request = _timed_bundle(bundle)
    monkeypatch.setattr(host, "FlightSupervisor", lambda *_a, **_k: object())
    result = host.ReplanningAgent(envelope, lambda: False, inflight_contract=contract).assess(request)
    assert result["action"] == "select_next_orbit"
    assert result["model_inference_invoked"] is False
    window = request["active_authority"]["request_window"]
    assert window["remaining_simulation_s"] == 90.
    assert window["remaining_wall_s"] == 190.
    assert "wall_deadline_s" not in window and "observed_wall_time_s" not in window


@pytest.mark.parametrize("field,value", [
    ("simulation_deadline_s", 101.), ("remaining_simulation_s", 91.),
    ("model_response_reserve_s", 1.), ("operator_response_reserve_s", 0.),
    ("request_available", False), ("remaining_wall_s", float("nan")),
])
def test_v2_host_rejects_forged_timing_facts(bundle, field, value):
    envelope, contract, request = _timed_bundle(bundle)
    request["active_authority"]["request_window"][field] = value
    with pytest.raises(ValueError, match="invalid_inflight_active_authority"):
        host.validate(request, envelope, inflight_contract=contract)


@pytest.mark.parametrize("clock_kind", ["simulation", "wall"])
def test_v2_host_rejects_unavailable_action_but_accepts_monitoring(bundle, clock_kind):
    from src.runtime.starship_inflight_runtime import amendment_window
    envelope, contract, request = _timed_bundle(bundle)
    if clock_kind == "simulation":
        request.update(time_s=100., deadline_s=135.)
    request["active_authority"]["request_window"] = amendment_window(contract["bounds"],
        simulation_time_s=request["time_s"], wall_time_s=200. if clock_kind == "wall" else 10.,
        model_response_reserve_s=35., status="idle", scope_revision=0)
    with pytest.raises(ValueError, match="invalid_inflight_active_authority"):
        host.validate(request, envelope, inflight_contract=contract)
    request["allowed_actions"] = ["keep_plan"]
    host.validate(request, envelope, inflight_contract=contract)


def test_v2_public_input_preserves_reference_binding_without_absolute_wall_times(bundle, monkeypatch):
    envelope, contract, request = _timed_bundle(bundle)
    envelope["mode"] = "live"
    request["envelope_sha256"] = host.digest(envelope)
    monkeypatch.setenv(host.MODE_ENV, "live")
    monkeypatch.setattr(host, "FlightSupervisor", lambda *_a, **_k: object())
    seen = []
    class StopBeforeModel(Exception):
        pass
    def capture(*args):
        seen.append(deepcopy(args[-1]))
        raise StopBeforeModel
    monkeypatch.setattr(host, "_receipt", capture)
    with pytest.raises(StopBeforeModel):
        host.ReplanningAgent(envelope, lambda: False, inflight_contract=contract).assess(request)
    assert seen[0]["active_authority"] == request["active_authority"]
    assert "scheduling assumptions" in seen[0]["objective"]
    assert "wall_deadline_s" not in seen[0]["active_authority"]["request_window"]


@pytest.mark.parametrize("field,value", [
    ("scope_revision", True), ("scope_revision", 2), ("scope_revision", 1),
    ("contract_sha256", "0" * 64), ("scope_sha256", "0" * 64),
    ("technical_candidates_are_not_authorization", False),
    ("select_next_orbit_requires_candidate_bound_operator_approval", False),
    ("amendment_status", "reservation_confirmed"), ("amendment_status", "approved"),
    ("extra_authority", True),
])
def test_invalid_or_unbound_authority_rejected(bundle, field, value):
    envelope, contract, request = bundle
    request["active_authority"][field] = value
    with pytest.raises(ValueError, match="invalid_inflight_active_authority"):
        host.validate(request, envelope, inflight_contract=contract)


@pytest.mark.parametrize("field,value", [
    ("commands", ["select_nominal", "keep_reserved_return", "select_next_orbit", "land_now"]),
    ("resource_limits", {"maximum_return_delay_s": 6001.0}),
])
def test_expanded_scope_cannot_exceed_exact_one_command_amendment(bundle, field, value):
    envelope, contract, request = bundle
    authority = request["active_authority"]
    authority["scope"]["commands"].append("select_next_orbit")
    authority.update(scope_revision=1, amendment_status="reservation_confirmed",
                     select_next_orbit_requires_candidate_bound_operator_approval=False)
    authority["scope"][field] = value
    authority["scope_sha256"] = host.digest(authority["scope"])
    with pytest.raises(ValueError, match="invalid_inflight_active_authority"):
        host.validate(request, envelope, inflight_contract=contract)


@pytest.mark.parametrize("status", ["reservation_changed_waiting_observation", "reservation_confirmed",
                                    "reservation_confirmation_failed", "reservation_update_unconfirmed"])
def test_exact_expanded_scope_is_valid_without_claiming_dispatch(bundle, status):
    envelope, contract, request = bundle
    authority = request["active_authority"]
    authority["scope"]["commands"].append("select_next_orbit")
    authority.update(scope_revision=1, amendment_status=status,
                     scope_sha256=host.digest(authority["scope"]),
                     select_next_orbit_requires_candidate_bound_operator_approval=False)
    host.validate(request, envelope, inflight_contract=contract)


def test_request_cannot_substitute_its_own_contract_or_run(bundle):
    envelope, contract, request = bundle
    forged = deepcopy(contract)
    forged["bounds"]["wall_deadline_s"] += 1
    request["active_authority"]["contract_sha256"] = host.digest(forged)
    with pytest.raises(ValueError, match="invalid_inflight_active_authority"):
        host.validate(request, envelope, inflight_contract=contract)
    request["active_authority"]["contract_sha256"] = host.digest(contract)
    request["request_id"] = "another-run-1"
    with pytest.raises(ValueError, match="invalid_inflight_active_authority"):
        host.validate(request, envelope, inflight_contract=contract)


@pytest.mark.parametrize("change", ["initial_scope", "deadline", "bound_type", "missing_cutoff"])
def test_invalid_trusted_contract_rejected_before_agent_transport(bundle, monkeypatch, change):
    envelope, contract, _request = bundle
    monkeypatch.setattr(host, "FlightSupervisor", lambda *_a, **_k: pytest.fail("transport constructed"))
    if change == "initial_scope":
        contract["initial_scope"]["commands"].append("select_next_orbit")
    elif change == "deadline":
        contract["bounds"]["simulation_deadline_s"] = 121.0
    elif change == "bound_type":
        contract["bounds"]["maximum_observation_age_s"] = True
    else:
        contract["bounds"].pop("reservation_cutoff_s")
    with pytest.raises(ValueError, match="invalid_inflight_host_contract"):
        host.ReplanningAgent(envelope, lambda: False, inflight_contract=contract)


def test_host_fixture_path_retains_expected_contract_and_never_calls_transport(bundle, monkeypatch):
    envelope, contract, request = bundle
    monkeypatch.setattr(host, "FlightSupervisor", lambda *_a, **_k: object())
    agent = host.ReplanningAgent(envelope, lambda: False, inflight_contract=contract)
    contract["bounds"]["wall_deadline_s"] += 1
    result = agent.assess(request)
    assert result["action"] == "select_next_orbit"
    assert result["model_inference_invoked"] is False
    assert result["request_sha256"] == host.digest(request)
    legacy = {k: v for k, v in request.items() if k != "active_authority"}
    assert host.ReplanningAgent(envelope, lambda: False).assess(legacy) == host.fixture_decision(legacy, envelope)


def test_public_input_contains_authority_and_no_blanket_approval_claim(bundle, monkeypatch):
    envelope, contract, request = bundle
    envelope["mode"] = "live"
    request["envelope_sha256"] = host.digest(envelope)
    monkeypatch.setenv(host.MODE_ENV, "live")
    monkeypatch.setattr(host, "FlightSupervisor", lambda *_a, **_k: object())
    seen = []

    class CapturedBeforeInference(Exception):
        pass

    def capture(*args):
        seen.append(deepcopy(args[-1]))
        raise CapturedBeforeInference

    monkeypatch.setattr(host, "_receipt", capture)
    with pytest.raises(CapturedBeforeInference):
        host.ReplanningAgent(envelope, lambda: False, inflight_contract=contract).assess(request)
    public = seen[0]
    assert public["active_authority"] == request["active_authority"]
    assert "already approved these choices" not in public["objective"]
    assert "technical ceiling" in public["objective"]
    assert "request for candidate-bound operator approval" in public["objective"]
    assert public["delegation"]["operator_approved"] is False
    assert public["delegation"]["operator_approved_active_scope_only"] is True
    assert public["delegation"]["selection_schedules_future_execution"] is False


@pytest.mark.parametrize("change", ["remove", "change", "unsolicited", "unsolicited_null"])
def test_reference_protocol_binds_presence_and_value_of_optional_authority(bundle, change):
    _envelope, _contract, request = bundle
    public = {key: deepcopy(request[key]) for key in (
        "time_s", "stage", "notice", "resources", "candidates", "selected", "allowed_actions", "active_authority")}
    if change == "remove":
        public.pop("active_authority")
    elif change == "change":
        public["active_authority"]["scope_revision"] = 1
    else:
        request.pop("active_authority")
        if change == "unsolicited_null":
            public["active_authority"] = None
    payload = {"messages": [{"content": "stub"}, {"content": json.dumps(public)}]}
    result = protocol.audit_response(request, payload, b"{}", 0.1,
        expected_payload_sha256=host.digest(payload), policy=protocol.policy_for("ids_low_4096"))
    assert result["errors"] == ["decision_input_mismatch"]


def _fixture_policy_bundle(bundle):
    envelope, contract, request = bundle
    contract["fixture_policy"] = deepcopy(host.INFLIGHT_FIXTURE_POLICY)
    envelope["actions"] += ["evaluate_returns", "wait_for_update"]
    request["envelope_sha256"] = host.digest(envelope)
    request["active_authority"]["contract_sha256"] = host.digest(contract)
    return envelope, contract, request


def test_registered_fixture_policy_is_bound_to_expected_contract_and_fixture_only(bundle):
    envelope, contract, request = _fixture_policy_bundle(bundle)
    host.validate(request, envelope, inflight_contract=contract)
    removed = deepcopy(contract)
    removed.pop("fixture_policy")
    with pytest.raises(ValueError, match="invalid_inflight_active_authority"):
        host.validate(request, envelope, inflight_contract=removed)
    envelope["mode"] = "live"
    request["envelope_sha256"] = host.digest(envelope)
    with pytest.raises(ValueError, match="invalid_inflight_host_contract"):
        host.validate(request, envelope, inflight_contract=contract)


@pytest.mark.parametrize("field,value", [
    ("id", "unregistered"), ("maximum_scope_requests", 2), ("maximum_scope_requests", True),
    ("model_inference_invoked", True), ("extra", "unregistered"),
])
def test_fixture_policy_cannot_expand_or_change_registered_behavior(bundle, field, value):
    envelope, contract, request = _fixture_policy_bundle(bundle)
    contract["fixture_policy"][field] = value
    request["active_authority"]["contract_sha256"] = host.digest(contract)
    with pytest.raises(ValueError, match="invalid_inflight_host_contract"):
        host.fixture_decision(request, envelope, inflight_contract=contract)


def test_policy_evaluates_initially_then_requests_next_orbit_without_scenario_cue(bundle):
    envelope, contract, request = _fixture_policy_bundle(bundle)
    request.update(stage="normal_monitoring", selected=None, allowed_actions=["keep_plan", "evaluate_returns"])
    first = host.fixture_decision(request, envelope, inflight_contract=contract)
    assert first["action"] == "evaluate_returns"
    request.update(stage="updated_return_options", candidates={"nominal": {}, "next_orbit": {}},
                   selected="nominal", allowed_actions=["select_nominal", "select_next_orbit", "evaluate_returns", "keep_plan"])
    second = host.fixture_decision(request, envelope, inflight_contract=contract)
    assert second["action"] == "select_next_orbit"
    assert "recovery_service_forecast" not in request["notice"]
    for response in (first, second):
        assert response["fixture_policy_sha256"] == host.digest(contract["fixture_policy"])
        assert response["mode"] == "fixture" and response["model_inference_invoked"] is False
        assert response["jev_invocation"] is response["llm_invocation"] is None


@pytest.mark.parametrize("status,revision,expected", [
    ("awaiting_operator", 0, "keep_plan"),
    ("reservation_changed_waiting_observation", 1, "keep_plan"),
    ("reservation_confirmed", 1, "keep_plan"),
    ("fallback_preserved", 0, "select_nominal"),
    ("fallback_not_admitted", 0, "select_nominal"),
    ("reservation_confirmation_failed", 1, "select_nominal"),
    ("reservation_update_unconfirmed", 0, "keep_plan"),
    ("reservation_update_unconfirmed", 1, "keep_plan"),
])
def test_policy_never_requests_second_amendment_or_forecast_after_attempt(bundle, status, revision, expected):
    envelope, contract, request = _fixture_policy_bundle(bundle)
    authority = request["active_authority"]
    authority.update(amendment_status=status, scope_revision=revision,
                     select_next_orbit_requires_candidate_bound_operator_approval=revision == 0)
    if revision:
        authority["scope"]["commands"].append("select_next_orbit")
        authority["scope_sha256"] = host.digest(authority["scope"])
    request.update(stage="orbital_event", allowed_actions=["select_next_orbit", "evaluate_returns", "select_nominal", "keep_plan", "wait_for_update"])
    for _ in range(2):
        response = host.fixture_decision(request, envelope, inflight_contract=contract)
        assert response["action"] == expected
        assert response["action"] not in {"select_next_orbit", "evaluate_returns"}


def test_policy_uses_bounded_wait_when_rejected_and_nominal_unavailable(bundle):
    envelope, contract, request = _fixture_policy_bundle(bundle)
    request["active_authority"]["amendment_status"] = "fallback_not_admitted"
    request["allowed_actions"] = ["select_next_orbit", "evaluate_returns", "wait_for_update"]
    assert host.fixture_decision(request, envelope, inflight_contract=contract)["action"] == "wait_for_update"
    request["allowed_actions"] = ["select_next_orbit", "evaluate_returns"]
    with pytest.raises(ValueError, match="inflight_fixture_no_preserving_action"):
        host.fixture_decision(request, envelope, inflight_contract=contract)


def test_normal_m2_fixture_keeps_legacy_behavior_without_registered_policy(bundle):
    envelope, contract, request = bundle
    envelope["event_supervision"] = {"scope": "post_deployment_orbital_coast_only"}
    envelope["actions"].append("evaluate_returns")
    request.update(stage="normal_monitoring", selected=None, context_generation=0, trigger_event_ids=[],
                   allowed_actions=["keep_plan", "evaluate_returns"], envelope_sha256=host.digest(envelope))
    legacy = {key: value for key, value in request.items() if key != "active_authority"}
    expected = {"request_id": legacy["request_id"], "request_sha256": host.digest(legacy),
                "action": "keep_plan", "mode": "fixture", "jev_invocation": None,
                "llm_invocation": None, "model_inference_invoked": False}
    assert host.fixture_decision(legacy, envelope) == expected
    response = host.fixture_decision(request, envelope, inflight_contract=contract)
    assert response["action"] == "keep_plan" and "fixture_policy_sha256" not in response

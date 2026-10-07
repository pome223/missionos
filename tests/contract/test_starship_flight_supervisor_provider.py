"""Provider-boundary tests; no external calls, simulator control or credentials."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Event

import pytest

from src.intelligence import starship_flight_supervisor as supervisor


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    for name in (supervisor.MODE_ENV, "TYPESAFE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(supervisor, "_deepseek_model", lambda: "deepseek-v4-flash")

    def blocked(*args):
        pytest.fail("Unmocked provider call")

    monkeypatch.setattr(supervisor, "_send_once", blocked)


@pytest.fixture
def public_request():
    first = {"observation_id": "observation-1", "sequence": 1, "time_s": 650.,
             "phase": "orbital_coast", "payload_released_count": 0,
             "release_attempt_count": 1, "release_acknowledged": True,
             "sequencer_state": "inhibited", "perigee_altitude_m": 180000.,
             "dynamic_pressure_pa": 0., "body_rate_rad_s": .0001,
             "propellant_kg": 40000., "return_deadline_s": 1200.}
    second = {**first, "observation_id": "observation-2", "sequence": 2, "time_s": 652.}
    return {"schema": "missionos.starship_flight_supervision_request.v1",
            "request_id": "local-correlation-not-for-provider", "allowed_actions": list(supervisor.ACTIONS),
            "observations": [first, second]}


def jev_body(route="bounded"):
    return json.dumps({"model": supervisor.JEV_MODEL, "answers": {"assessment_route": {
        "type": "choice", "choice": route, "confidence": .7,
        "probabilities": {key: .7 if key == route else .1 for key in supervisor.ROUTES},
    }}}).encode()


def llm_body(action="skip_remaining_deployment", **message_fields):
    return json.dumps({"model": "deepseek-v4-flash", "choices": [{
        "finish_reason": "stop", "message": {"role": "assistant",
            "content": json.dumps({"action": action}), **message_fields},
    }]}).encode()


def mock_live(monkeypatch, route="bounded", action="skip_remaining_deployment", bodies=None):
    monkeypatch.setenv(supervisor.MODE_ENV, "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-jev-not-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-llm-not-secret")
    seen = []

    def send(endpoint, wire, key):
        seen.append((endpoint, json.loads(wire), key))
        if bodies is not None:
            return bodies[len(seen)-1]
        return "response", jev_body(route) if len(seen) == 1 else llm_body(action)

    monkeypatch.setattr(supervisor, "_send_once", send)
    return seen


def assert_proposal_only(result):
    assert set(result) == {"request_id", "observation_id", "action", "route", "jev_invocation", "llm_invocation"}
    for invocation in (result["jev_invocation"], result["llm_invocation"]):
        assert invocation["schema_version"] == "runtime_invocation_evidence.v1"
        for field in ("approval_granted", "dispatch", "execution_authorized", "physical_execution",
                      "model_value_claim", "raw_prompt_recorded", "raw_response_recorded", "confidence_calibrated"):
            assert invocation[field] is False
        assert invocation["retries"] == 0
        assert invocation["redirects"] == 0


def test_fixture_exercises_cascade_without_inference(public_request):
    before = deepcopy(public_request)
    result = supervisor.FlightSupervisor("fixture").assess(public_request)
    assert result["route"] == "deep_reasoning"
    assert result["action"] == "skip_remaining_deployment"
    assert result["request_id"] == public_request["request_id"]
    assert result["observation_id"] == "observation-2"
    assert public_request == before
    for key in ("jev_invocation", "llm_invocation"):
        assert result[key]["status"] == "fixture_only"
        assert result[key]["call_attempted"] is False
        assert result[key]["model_inference_invoked"] is False
    assert_proposal_only(result)


@pytest.mark.parametrize("mode,status", [("off", "disabled"), ("live", "live_opt_in_required")])
def test_no_opt_in_preserves_hold(public_request, mode, status):
    result = supervisor.FlightSupervisor(mode).assess(public_request)
    assert result["action"] == "hold"
    assert result["jev_invocation"]["status"] == status
    assert result["llm_invocation"]["status"] == "not_routed"
    assert_proposal_only(result)


@pytest.mark.parametrize("route,action,calls", [
    ("bounded", "skip_remaining_deployment", 1),
    ("need_observation", "hold", 1), ("human_review", "hold", 1),
    ("deep_reasoning", "skip_remaining_deployment", 2),
])
def test_real_branch_routing_is_conditional(monkeypatch, public_request, route, action, calls):
    seen = mock_live(monkeypatch, route)
    result = supervisor.FlightSupervisor("live").assess(public_request)
    assert result["route"] == route
    assert result["action"] == action
    assert len(seen) == calls
    assert result["jev_invocation"]["model_inference_invoked"] is True
    assert result["llm_invocation"]["model_inference_invoked"] is (calls == 2)
    assert_proposal_only(result)


def test_llm_hold_is_respected(monkeypatch, public_request):
    mock_live(monkeypatch, "deep_reasoning", "hold")
    result = supervisor.FlightSupervisor("live").assess(public_request)
    assert result["action"] == "hold"
    assert result["llm_invocation"]["call_succeeded"] is True


def test_provider_receives_only_public_observations_and_procedure(monkeypatch, public_request):
    seen = mock_live(monkeypatch, "deep_reasoning")
    supervisor.FlightSupervisor("live").assess(public_request)
    jev_state = seen[0][1]["state"]
    llm_state = json.loads(seen[1][1]["messages"][1]["content"])
    assert jev_state == llm_state
    assert set(jev_state) == {"observations", "allowed_actions", "procedure", "scope"}
    serialized = json.dumps([wire for _, wire, _ in seen])
    for secret in (public_request["request_id"], "observation-1", "observation-2", "test-jev-not-secret", "test-llm-not-secret"):
        assert secret not in serialized
    assert "not a SpaceX flight procedure" in jev_state["procedure"]
    assert seen[0][0] == supervisor.JEV_ENDPOINT
    assert seen[1][0] == supervisor.DEEPSEEK_ENDPOINT


@pytest.mark.parametrize("target", ["public_request", "observation"])
@pytest.mark.parametrize("key", ["scenario", "future_recovery_time", "fault_type", "seed", "expected_action"])
def test_hidden_extras_rejected_before_network(monkeypatch, public_request, target, key):
    seen = mock_live(monkeypatch)
    (public_request if target == "public_request" else public_request["observations"][0])[key] = "private-world"
    result = supervisor.FlightSupervisor("live").assess(public_request)
    assert result["action"] == "hold"
    assert result["jev_invocation"]["status"] == "invalid_input"
    assert not seen
    assert "private-world" not in json.dumps(result)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(allowed_actions=["hold", "retry"]),
    lambda r: r.update(request_id="../../secret"),
    lambda r: r["observations"].pop(),
    lambda r: r["observations"][1].update(time_s=650.),
    lambda r: r["observations"][1].update(sequence=1),
    lambda r: r["observations"][1].update(observation_id="observation-1"),
    lambda r: r["observations"][0].update(release_acknowledged=1),
    lambda r: r["observations"][0].update(sequence=True),
    lambda r: r["observations"][0].update(propellant_kg=float("nan")),
    lambda r: r["observations"][0].update(body_rate_rad_s=float("inf")),
    lambda r: r["observations"][0].update(phase="landing_burn"),
    lambda r: r["observations"][0].update(sequencer_state="enabled"),
])
def test_invalid_observations_hold_before_network(monkeypatch, public_request, mutate):
    seen = mock_live(monkeypatch)
    mutate(public_request)
    result = supervisor.FlightSupervisor("live").assess(public_request)
    assert result["action"] == "hold"
    assert result["jev_invocation"]["status"] == "invalid_input"
    assert not seen


@pytest.mark.parametrize("mutate", [
    lambda r: r["observations"][1].update(payload_released_count=1),
    lambda r: r["observations"][1].update(release_attempt_count=2),
    lambda r: r["observations"][0].update(release_acknowledged=False),
    lambda r: r["observations"][0].update(sequencer_state="skipped"),
    lambda r: r["observations"][1].update(return_deadline_s=651.),
])
def test_unestablished_procedure_preconditions_do_not_ask_model(monkeypatch, public_request, mutate):
    seen = mock_live(monkeypatch)
    mutate(public_request)
    result = supervisor.FlightSupervisor("live").assess(public_request)
    assert result["action"] == "hold"
    assert result["jev_invocation"]["status"] == "procedure_precondition_not_met"
    assert not seen


@pytest.mark.parametrize("status", ["transport_failed", "transport_timeout"])
def test_jev_failure_does_not_fall_through_to_llm(monkeypatch, public_request, status):
    seen = mock_live(monkeypatch, bodies=[(status, None)])
    result = supervisor.FlightSupervisor("live").assess(public_request)
    assert result["action"] == "hold"
    assert result["jev_invocation"]["status"] == status
    assert result["llm_invocation"]["call_attempted"] is False
    assert len(seen) == 1


@pytest.mark.parametrize("body", [
    b'{"model":"jev-1.13.0","model":"jev-1.13.0"}',
    b'{"model":"jev-1.13.0","answers":{"assessment_route":NaN}}',
    b"not json", b"x" * (supervisor.MAX_RESPONSE_BYTES+1),
    jev_body().replace(b"jev-1.13.0", b"jev-unexpected"),
    jev_body().replace(b'"bounded"', b'"execute_reset"'),
])
def test_malformed_jev_preserves_hold(monkeypatch, public_request, body):
    seen = mock_live(monkeypatch, bodies=[("response", body)])
    result = supervisor.FlightSupervisor("live").assess(public_request)
    assert result["action"] == "hold"
    assert result["jev_invocation"]["status"] in {"invalid_response", "response_too_large"}
    assert len(seen) == 1


@pytest.mark.parametrize("body", [
    llm_body("retry"), llm_body(content='{"action":"hold","action":"skip_remaining_deployment"}'),
    llm_body(content='{"action":"skip_remaining_deployment","approved":true}'),
    llm_body(tool_calls=[{"id": "tool"}]),
    llm_body().replace(b'"stop"', b'"length"'),
    llm_body().replace(b'"deepseek-v4-flash"', b'"unexpected-model"'),
    llm_body(content='{"action":"test-llm-not-secret"}'),
])
def test_malformed_llm_cannot_expand_actions(monkeypatch, public_request, body):
    seen = mock_live(monkeypatch, bodies=[("response", jev_body("deep_reasoning")), ("response", body)])
    result = supervisor.FlightSupervisor("live").assess(public_request)
    assert result["action"] == "hold"
    assert result["llm_invocation"]["status"] == "invalid_response"
    assert result["jev_invocation"]["model_inference_invoked"] is True
    assert result["llm_invocation"]["model_inference_invoked"] is False
    assert len(seen) == 2
    assert "test-llm-not-secret" not in json.dumps(result)


def test_no_retry_after_failure_or_success(monkeypatch, public_request):
    seen = mock_live(monkeypatch, bodies=[("transport_timeout", None)])
    adapter = supervisor.FlightSupervisor("live")
    assert adapter.assess(public_request)["jev_invocation"]["status"] == "transport_timeout"
    again = adapter.assess(public_request)
    assert again["jev_invocation"]["status"] == "budget_exhausted"
    assert again["action"] == "hold"
    assert len(seen) == 1


def test_concurrent_assessments_cannot_duplicate_provider_send(monkeypatch, public_request):
    mock_live(monkeypatch)
    entered, release = Event(), Event()
    seen = []

    def send(*args):
        seen.append(args)
        entered.set()
        assert release.wait(3)
        return "response", jev_body()

    monkeypatch.setattr(supervisor, "_send_once", send)
    adapter = supervisor.FlightSupervisor("live")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(adapter.assess, public_request)
        assert entered.wait(3)
        second = pool.submit(adapter.assess, public_request).result(timeout=3)
        release.set()
        first = first.result(timeout=3)
    assert first["action"] == "skip_remaining_deployment"
    assert second["jev_invocation"]["status"] == "budget_exhausted"
    assert len(seen) == 1


def test_worker_exit_during_jev_prevents_new_deepseek_call(monkeypatch, public_request):
    mock_live(monkeypatch)
    running = True
    seen = []

    def send(*args):
        nonlocal running
        seen.append(args)
        running = False
        return "response", jev_body("deep_reasoning")

    monkeypatch.setattr(supervisor, "_send_once", send)
    result = supervisor.FlightSupervisor("live", active=lambda: running).assess(public_request)
    assert len(seen) == 1
    assert result["jev_invocation"]["model_inference_invoked"] is True
    assert result["llm_invocation"]["status"] == "not_routed_after_run_end"
    assert result["llm_invocation"]["call_attempted"] is False
    assert result["action"] == "hold"


def test_inactive_run_never_starts_provider(monkeypatch, public_request):
    seen = mock_live(monkeypatch)
    result = supervisor.FlightSupervisor("live", active=lambda: False).assess(public_request)
    assert not seen
    assert result["jev_invocation"]["status"] == "not_routed_after_run_end"
    assert result["action"] == "hold"


def test_lifecycle_rechecked_immediately_before_conditional_send(monkeypatch, public_request):
    seen = mock_live(monkeypatch, route="deep_reasoning")

    def model_configuration():
        nonlocal running
        running = False
        return "deepseek-v4-flash"

    running = True
    monkeypatch.setattr(supervisor, "_deepseek_model", model_configuration)
    result = supervisor.FlightSupervisor("live", active=lambda: running).assess(public_request)
    assert len(seen) == 1
    assert result["llm_invocation"]["status"] == "not_routed_after_run_end"
    assert result["action"] == "hold"

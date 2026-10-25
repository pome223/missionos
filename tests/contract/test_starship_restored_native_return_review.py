"""Saved routing checks using invented receipts; no actual model or flight."""
from copy import deepcopy

import pytest

from src.intelligence.starship_restored_agent import public_input
from src.runtime import starship_restored_decision_pipe as pipe
from src.runtime import starship_restored_native_verifier as native
from src.runtime import starship_restored_return_review as producer
from tests.contract.test_starship_restored_decision_pipe import request
from tests.contract import test_starship_restored_native_verifier as prior
from tests.contract import test_starship_restored_native_return_context as context_tests

saved_only = prior.saved_only
POLICY = {"schema": "missionos.starship_restored_return_review_policy.v1",
    "confidence_floor": .5, "selected_action_margin_floor": .1}
CONTEXT = {"run_id": "saved-review", "mission_id": "synthetic", "vehicle_id": "ship"}
SOURCE = {native.RETURN_REVIEW_SOURCE: "a"*64}
PLAN = "b"*64


def judgment(req, provider, action, *, confidence=.17, probabilities=None):
    receipt = {"provider": provider, "request_input_sha256": native._hash(public_input(req, provider=provider)),
        "model_inference_invoked": True, "retries": 0, "mode": "live", "status": "succeeded",
        "model_id": "deepseek-flash" if provider == "deepseek" else "jev-1.13.0",
        "request_model": "deepseek-flash" if provider == "deepseek" else "jev-1.13.0",
        "model_inference_status": "confirmed_by_valid_response", "request_sha256": "a"*64,
        "response_sha256": "b"*64, "request_bytes": 100, "response_bytes_read": 100}
    for flag in ("approval_granted", "execution_authorized", "dispatch", "physical_execution",
            "raw_prompt_recorded", "raw_response_recorded", "raw_reasoning_recorded"):
        receipt[flag] = False
    for flag in ("call_attempted", "call_succeeded", "response_received", "complete_response_observed"):
        receipt[flag] = True
    if provider == "typesafe":
        receipt.update(native_choice_confidence=confidence,
            native_choice_probabilities=probabilities or dict(zip(req["allowed_actions"], [.4, .35, .25], strict=True)))
    return {"schema": "missionos.starship_restored_judgment.v1", "request_id": req["request_id"],
        "request_sha256": native._hash(req), "provider": provider, "status": "valid", "action": action,
        "provider_receipt": receipt, "model_inference_invoked": True, "execution_permission_granted": False}


def attempt(req, provider, answer, remaining):
    return {"provider": provider, "request_sha256": native._hash(req), "decision_deadline_s": req["deadline_s"],
        "remaining_s": remaining, "judgment": answer}


def protocol(*, explicit=False, confidence=.17, second=True, deep_valid=True, remaining=160.):
    req = request()
    first = judgment(req, "typesafe", "request_deep_reasoning" if explicit else "select_nominal", confidence=confidence)
    attempts = [attempt(req, "typesafe", first, 180.)]
    if second:
        deep_req = pipe._deep_request(req, remaining, return_review_policy=POLICY)
        final = judgment(deep_req, "deepseek", "select_next_orbit")
        if not deep_valid:
            final.update(status="no_valid_judgment", action=None, model_inference_invoked=False)
            final["provider_receipt"].update(status="timed_out", model_inference_invoked=False, call_succeeded=False)
        attempts.append(attempt(deep_req, "deepseek", final, remaining))
    result = pipe._result(req, attempts, reason=None if deep_valid else "no_valid_provider_judgment",
        return_review_policy=POLICY)
    binding = {"context": CONTEXT, "plan_sha256": PLAN, "source_sha256": native._hash(SOURCE),
        "return_review_policy_sha256": native._hash(POLICY)}
    frame = {"schema": pipe.REQUEST_FRAME_V2, **binding, "sequence": 1, "request_id": req["request_id"],
        "request_sha256": native._hash(req), "request": req, "started_wall_s": 100., "wall_deadline_s": 280.}
    session = {"frame": frame, "attempts": attempts,
        "started_attempts": [{key: item[key] for key in ("provider", "request_sha256", "decision_deadline_s", "remaining_s")}
            for item in attempts], "thread_result": deepcopy(result), "delivered_result": deepcopy(result)}
    client = {"schema": pipe.CLIENT_V2, **binding, "return_review_policy": POLICY, "pending_request": None,
        "provider_calls_by_worker": 0, "execution_permission_granted": False, "results": [deepcopy(result)]}
    host = {"schema": pipe.HOST_V2, **binding, "return_review_policy": POLICY,
        "execution_permission_granted": False, "requests": [session]}
    return req, first, result, client, host


def forbidden_producer(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Independent checker called producer routing or policy validator")
    for name in ("review_route", "validate_policy", "policy_digest"):
        monkeypatch.setattr(producer, name, forbidden)
        if hasattr(pipe, name):
            monkeypatch.setattr(pipe, name, forbidden)


def checked(client, host, policy=POLICY):
    return native._judgments(client, host, context=CONTEXT, plan_sha256=PLAN, source=SOURCE,
        mode="live", expected_return_review_policy=policy)


def test_independent_route_and_provider_counts_preserve_original_judgment(monkeypatch):
    req, original, result, client, host = protocol()
    forbidden_producer(monkeypatch)
    native._checked_exchange(result, req, POLICY)
    exchanges, calls, invokes = checked(client, host)
    assert calls == invokes == 2
    assert exchanges[req["request_id"]]["exchange"]["attempts"][0]["judgment"] == original
    assert result["judgment"]["action"] == "select_next_orbit"
    review = native._review_verification(exchanges, POLICY)
    assert review["policy_triggered_count"] == review["completed_policy_review_count"] == 1
    assert review["unresolved_policy_review_count"] == review["explicit_jev_delegation_count"] == 0
    assert review["review_is_approval"] is False and review["original_jev_judgments_preserved"] is True


@pytest.mark.parametrize("confidence,probs,action,route", [
    (.5, [.55, .45, 0.], "select_nominal", "jev_final"),
    (.499, [.9, .1, 0.], "select_nominal", "rules_uncertainty_review"),
    (.9, [.54, .46, 0.], "select_nominal", "rules_uncertainty_review"),
    (.9, [.1, .9, 0.], "select_nominal", "rules_uncertainty_review"),
    (0., [.9, .1, 0.], "select_nominal", "rules_uncertainty_review"),
    (1., [.9, .1, 0.], "select_nominal", "jev_final"),
])
def test_exact_boundaries_and_selected_margin_not_top_two(monkeypatch, confidence, probs, action, route):
    req = request()
    original = judgment(req, "typesafe", action, confidence=confidence,
        probabilities=dict(zip(req["allowed_actions"], probs, strict=True)))
    expected = producer.review_route(req, original, POLICY)
    forbidden_producer(monkeypatch)
    actual = native._review_route(req, original, POLICY)
    assert actual == expected and actual["route"] == route


@pytest.mark.parametrize("bad", [True, None, -1., 1.1, float("nan"), "0.1"])
def test_invalid_metrics_are_not_promoted_to_secondary_review(monkeypatch, bad):
    req = request()
    original = judgment(req, "typesafe", "select_nominal")
    original["provider_receipt"]["native_choice_confidence"] = bad
    expected = producer.review_route(req, original, POLICY)
    forbidden_producer(monkeypatch)
    actual = native._review_route(req, original, POLICY)
    assert actual == expected and actual["route"] == "invalid_jev_metrics"
    assert actual["review_required"] is False and actual["confidence"] is None


@pytest.mark.parametrize("mutation", ["proof", "metrics", "no_marker", "v1", "policy", "frame", "counter", "started", "deep_input", "silent_fallback"])
def test_forged_or_dropped_routing_binding_and_secondary_evidence_rejected(monkeypatch, mutation):
    req, original, result, client, host = protocol()
    if mutation == "proof":
        client["results"][0]["return_review"]["confidence"] = .9
    elif mutation == "metrics":
        for answer in (client["results"][0], host["requests"][0]["thread_result"], host["requests"][0]["delivered_result"]):
            answer["attempts"][0]["judgment"]["provider_receipt"]["native_choice_confidence"] = .9
    elif mutation == "no_marker":
        del client["results"][0]["return_review"]
    elif mutation == "v1":
        client["schema"] = "missionos.starship_restored_decision_client.v1"
    elif mutation == "policy":
        client["return_review_policy"] = {**POLICY, "confidence_floor": .2}
    elif mutation == "frame":
        host["requests"][0]["frame"]["return_review_policy_sha256"] = "e"*64
    elif mutation == "counter":
        host["requests"][0]["started_attempts"].append(deepcopy(host["requests"][0]["started_attempts"][-1]))
    elif mutation == "started":
        host["requests"][0]["started_attempts"][-1]["provider"] = "typesafe"
    elif mutation == "deep_input":
        host["requests"][0]["attempts"][-1]["request_sha256"] = native._hash(req)
    else:
        client["results"][0]["judgment"] = deepcopy(original)
        client["results"][0]["action"] = original["action"]
    forbidden_producer(monkeypatch)
    with pytest.raises(ValueError):
        checked(client, host)


@pytest.mark.parametrize("bad", [None, False, {}, {**POLICY, "confidence_floor": .4}])
def test_v2_not_admitted_by_missing_or_wrong_external_policy(monkeypatch, bad):
    _, _, _, client, host = protocol()
    forbidden_producer(monkeypatch)
    with pytest.raises(ValueError):
        checked(client, host, bad)


@pytest.mark.parametrize("second,valid,expected_calls", [(False, True, 1), (True, False, 2)])
def test_failed_or_uncompleted_secondary_is_not_adopted_as_jev_final(monkeypatch, second, valid, expected_calls):
    req, original, result, client, host = protocol(second=second, deep_valid=valid)
    forbidden_producer(monkeypatch)
    exchanges, calls, _ = checked(client, host)
    assert calls == expected_calls and result["status"] == "no_valid_judgment" and result["action"] is None
    assert result["attempts"][0]["judgment"] == original
    review = native._review_verification(exchanges, POLICY)
    assert review["completed_policy_review_count"] == 0 and review["unresolved_policy_review_count"] == 1
    assert exchanges[req["request_id"]]["native_choice"] is False


def test_explicit_jev_delegation_has_its_own_count(monkeypatch):
    _, _, _, client, host = protocol(explicit=True)
    forbidden_producer(monkeypatch)
    exchanges, calls, invokes = checked(client, host)
    assert calls == invokes == 2
    review = native._review_verification(exchanges, POLICY)
    assert review["explicit_jev_delegation_count"] == 1 and review["policy_triggered_count"] == 0


def test_policy_and_return_context_roundtrip_through_full_saved_verifier(monkeypatch):
    original_case = prior.case

    def extra_source(*args, **kwargs):
        values = list(original_case(*args, **kwargs))
        values[4] = {**values[4], native.RETURN_REVIEW_SOURCE: "e"*64}
        return tuple(values)

    monkeypatch.setattr(prior, "case", extra_source)
    record, profile, args, _, _ = context_tests.context_case(monkeypatch)
    host = native._read(args["host_record_raw"])
    replacement = {}
    for session in host["decision_host"]["requests"]:
        req = session["frame"]["request"]
        answer = deepcopy(session["attempts"][0]["judgment"])
        if req["phase"] == "return":
            probabilities = {action: .1 for action in req["allowed_actions"]}
            probabilities[answer["action"]] = 1.-.1*(len(probabilities)-1)
            answer["provider_receipt"].update(status="succeeded", native_choice_confidence=.17,
                native_choice_probabilities=probabilities)
        attempts = [attempt(req, "typesafe", answer, req["deadline_s"]-req["time_s"])]
        if req["phase"] == "return":
            selected = pipe._deep_request(req, 160., return_review_policy=POLICY)
            final = judgment(selected, "deepseek", answer["action"])
            attempts.append(attempt(selected, "deepseek", final, 160.))
        result = pipe._result(req, attempts, return_review_policy=POLICY)
        replacement[req["request_id"]] = result
        session.update(attempts=attempts, thread_result=deepcopy(result), delivered_result=deepcopy(result),
            started_attempts=[{key: row[key] for key in ("provider", "request_sha256", "decision_deadline_s", "remaining_s")}
                for row in attempts])
        session["frame"].update(schema=pipe.REQUEST_FRAME_V2, return_review_policy_sha256=native._hash(POLICY))
    for value in (record["execution"], host, host["decision_host"], record["governance"]["decision_client"]):
        value.update(return_review_policy=deepcopy(POLICY), return_review_policy_sha256=native._hash(POLICY))
    host["decision_host"]["schema"] = pipe.HOST_V2
    client = record["governance"]["decision_client"]
    client["schema"] = pipe.CLIENT_V2
    client["results"] = [replacement[row["request_id"]] for row in client["results"]]
    for row in record["governance"]["deployment"]["decisions"]:
        row["exchange"] = replacement[row["request"]["request_id"]]
    args.update(host_record_raw=native._wire(host), expected_host_record_sha256=native._hash(host),
        expected_return_review_policy=POLICY)
    before = deepcopy(record)
    forbidden_producer(monkeypatch)
    context_tests.forbid_projection(monkeypatch)
    result = native.verify_restored_native_mission(record, profile, **args)
    assert result["passed"], result
    assert result["physical_success"] is result["ai_control_completed"] is True
    assert result["hosted_model_attempts"] == result["confirmed_native_inferences"] == 4
    assert result["return_review"]["policy_triggered_count"] == result["return_review"]["completed_policy_review_count"] == 1
    assert result["return_review"]["unresolved_policy_review_count"] == 0 and record == before
    # Outer source/host/execution assertions remain external expectations; the
    # V2 records cannot authorize their own new routing policy.
    for target in (record["execution"], host, host["decision_host"], client):
        retained = target.pop("return_review_policy_sha256")
        args.update(host_record_raw=native._wire(host), expected_host_record_sha256=native._hash(host))
        rejected = native.verify_restored_native_mission(record, profile, **args)
        assert not rejected["passed"], rejected
        target["return_review_policy_sha256"] = retained
    args.update(host_record_raw=native._wire(host), expected_host_record_sha256=native._hash(host),
        expected_return_review_policy=None)
    assert not native.verify_restored_native_mission(record, profile, **args)["passed"]

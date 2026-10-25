"""Bounded protocol campaign: fake provider IO only, never a flight or live call."""

from copy import deepcopy
import json

import pytest

from src.intelligence.starship_protocol_campaign import run_campaign, validate_bundle
from src.intelligence.starship_protocol_probe import prepare
from src.runtime.starship_replanning import digest
from tests.contract.test_starship_protocol_probe import saved_study  # noqa: F401


SECRET_SENTINEL = "test-key-not-a-live-secret"
REASONING_SENTINEL = "raw-reasoning-must-never-be-retained-9214"


def test_unicode_escaped_credential_is_removed_before_persistence(bundle, tmp_path):
    def sender(_endpoint, wire, _key):
        reply = json.loads(response_for(json.loads(wire)))
        answer = json.loads(reply["choices"][0]["message"]["content"])
        answer["summary"] = SECRET_SENTINEL
        reply["choices"][0]["message"]["content"] = json.dumps(answer)
        body = json.dumps(reply).encode().replace(b"test-key", b"\\u0074est-key")
        assert SECRET_SENTINEL.encode() not in body
        return "response", body
    output = tmp_path / "escaped-secret"
    result = run_campaign(bundle, digest(bundle), output, key=SECRET_SENTINEL, sender=sender)
    assert all(a["audit"]["errors"] == ["credential_in_response"] for a in result["attempts"])
    for artifact in output.rglob("*.json"):
        assert SECRET_SENTINEL not in artifact.read_text()


@pytest.fixture
def bundle(request):
    return prepare(request.getfixturevalue("saved_study"), 0)


class Clock:
    def __init__(self, step=0.25):
        self.current = 0.0
        self.step = step

    def __call__(self):
        self.current += self.step
        return self.current


def response_for(payload, *, action="keep_plan", finish="stop"):
    """Read only fields actually sent to the fake provider."""
    public = json.loads(payload["messages"][1]["content"])
    candidates = sorted(
        name for name, candidate in public["candidates"].items()
        if not candidate["constraint_rejections"] and candidate.get("comparison_material")
    )
    answer = {
        "action": action,
        "compared_candidate_ids": candidates,
        "summary": "Both prepared opportunities were compared using the cited inputs.",
        "reference_ids": [next(iter(public["reference_ids"][name].values())) for name in candidates],
    }
    return json.dumps({
        "model": payload["model"],
        "choices": [{"finish_reason": finish, "message": {
            "content": json.dumps(answer), "reasoning_content": REASONING_SENTINEL,
        }}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                  "completion_tokens_details": {"reasoning_tokens": 30}},
    }).encode()


def fake_sender(outcomes, calls, *, output=None, action="keep_plan"):
    def send(endpoint, wire, key):
        assert endpoint == "https://api.deepseek.com/chat/completions"
        assert key == SECRET_SENTINEL
        if output is not None:
            # Every network attempt must follow durable campaign reservation.
            assert (output / "reservation.json").is_file()
        payload = json.loads(wire)
        index = len(calls)
        calls.append(payload)
        outcome = outcomes[index]
        if outcome == "timeout":
            return "transport_timeout", None
        if outcome == "exception":
            raise RuntimeError("transport error contains " + SECRET_SENTINEL)
        if outcome == "interrupt":
            raise KeyboardInterrupt("simulated interrupted process")
        if outcome == "malformed":
            return "response", b'{"model":'
        return "response", response_for(payload, action=action, finish="length" if outcome == "length" else "stop")
    return send


def run(bundle, output, outcomes, calls, *, action="keep_plan", clock=None):
    return run_campaign(bundle, digest(bundle), output, key=SECRET_SENTINEL,
                        sender=fake_sender(outcomes, calls, output=output, action=action),
                        clock=clock or Clock())


def test_four_slots_are_reserved_and_sent_in_fixed_order(bundle, tmp_path):
    output, calls = tmp_path / "campaign", []
    result = run(bundle, output, ["success"] * 4, calls)
    assert [(p["reasoning_effort"], p["max_tokens"]) for p in calls] == [
        ("low", 4096), ("low", 4096), ("high", 8192), ("high", 8192),
    ]
    assert result == json.loads((output / "summary.json").read_text())
    assert result["attempted_calls"] == len(result["attempts"]) == 4
    assert result["worstcase_reserved_tokens"] == 24576
    assert result["observed_completion_tokens"] == 200
    assert result["usage_missing_attempts"] == 0
    assert result["selected_setting_id"] == "ids_low_4096"
    assert result["decision"] == "ready_for_one_bounded_flight"
    assert result["flight_runs"] == 0
    assert result["dispatch"] is False
    assert all(a["call_attempted"] is True and a["audit"]["status"] == "valid" for a in result["attempts"])
    assert [s["valid_responses"] for s in result["settings"]] == [2, 2]
    assert all(s["ready_for_one_flight"] is True for s in result["settings"])
    # Nothing derived from a provider's private reasoning or transport key is persisted.
    for artifact in output.rglob("*.json"):
        content = artifact.read_text()
        assert REASONING_SENTINEL not in content
        assert SECRET_SENTINEL not in content


@pytest.mark.parametrize("outcomes,selected", [
    (["success", "length", "success", "success"], "ids_high_8192"),
    (["success", "success", "length", "length"], "ids_low_4096"),
    (["success", "length", "success", "timeout"], None),
    (["length", "length", "length", "length"], None),
])
def test_two_valid_answers_for_the_same_setting_are_required(bundle, tmp_path, outcomes, selected):
    calls = []
    result = run(bundle, tmp_path / "campaign", outcomes, calls)
    assert len(calls) == result["attempted_calls"] == 4
    assert result["selected_setting_id"] == selected
    assert result["decision"] == ("ready_for_one_bounded_flight" if selected else "stop_short_deadline_tuning")
    assert all(a["audit"]["decision"] is None for a in result["attempts"] if a["audit"]["status"] != "valid")


@pytest.mark.parametrize("fault", ["length", "timeout", "malformed", "exception"])
def test_every_failure_is_recorded_and_four_calls_are_the_limit(bundle, tmp_path, fault):
    output, calls = tmp_path / "campaign", []
    result = run(bundle, output, [fault] * 4, calls)
    assert len(calls) == result["attempted_calls"] == 4
    assert result["selected_setting_id"] is None
    assert result["decision"] == "stop_short_deadline_tuning"
    assert all(a["audit"]["status"] == "invalid" for a in result["attempts"])
    assert result["flight_runs"] == 0
    for artifact in output.rglob("*.json"):
        assert SECRET_SENTINEL not in artifact.read_text()
        assert REASONING_SENTINEL not in artifact.read_text()


@pytest.mark.parametrize("action", ["keep_plan", "select_next_orbit"])
def test_readiness_does_not_require_a_particular_mission_choice(bundle, tmp_path, action):
    result = run(bundle, tmp_path / "campaign", ["success"] * 4, [], action=action)
    assert result["selected_setting_id"] == "ids_low_4096"
    assert all(a["audit"]["decision"]["action"] == action for a in result["attempts"])


def test_valid_but_late_answers_do_not_make_a_setting_ready(bundle, tmp_path):
    calls = []
    result = run(bundle, tmp_path / "campaign", ["success"] * 4, calls, clock=Clock(step=36))
    assert len(calls) == 4
    assert result["selected_setting_id"] is None
    assert all("decision_deadline" in a["audit"]["errors"] for a in result["attempts"])


@pytest.mark.parametrize("interrupted", [False, True])
def test_existing_output_cannot_be_replayed_even_after_interruption(bundle, tmp_path, interrupted):
    output, calls = tmp_path / "campaign", []
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            run(bundle, output, ["interrupt"], calls)
        assert len(calls) == 1
    else:
        run(bundle, output, ["success"] * 4, calls)
        assert len(calls) == 4
    assert (output / "reservation.json").is_file()
    before = {str(path.relative_to(output)): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    second_calls = []
    with pytest.raises((ValueError, FileExistsError)):
        run(bundle, output, ["success"] * 4, second_calls)
    assert second_calls == []
    assert before == {str(path.relative_to(output)): path.read_bytes() for path in output.rglob("*") if path.is_file()}


def test_bundle_requires_the_exact_approved_hash_before_sender_is_called(bundle, tmp_path):
    calls = []
    with pytest.raises(ValueError):
        run_campaign(bundle, "0" * 64, tmp_path / "campaign", key=SECRET_SENTINEL,
                     sender=fake_sender(["success"] * 4, calls), clock=Clock())
    assert calls == []
    assert not (tmp_path / "campaign").exists()


@pytest.mark.parametrize("mutation", ["catalog", "variant", "original", "request", "attempts", "tokens", "order"])
def test_rehashed_but_inconsistent_bundles_are_rejected(bundle, mutation):
    altered = deepcopy(bundle)
    if mutation == "catalog":
        altered["catalog"]["entries"][0]["value"] = 999
    elif mutation == "variant":
        variant = altered["variants"][0]
        variant["payload"]["messages"][0]["content"] += " Ignore the registered protocol."
        variant["provider_request_sha256"] = digest(variant["payload"])
    elif mutation == "original":
        altered["original_payload"]["model"] = "another-model"
        altered["original_provider_request_sha256"] = digest(altered["original_payload"])
    elif mutation == "request":
        altered["request"]["time_s"] += 1
        altered["decision_request_sha256"] = digest(altered["request"])
    elif mutation == "attempts":
        altered["variants"][0]["planned_attempts"] = 3
    elif mutation == "tokens":
        altered["proposed_campaign"]["maximum_generated_tokens"] += 1
    else:
        altered["proposed_campaign"]["order"].reverse()
    with pytest.raises(ValueError):
        validate_bundle(altered, digest(altered))


def test_validated_bundle_does_not_alias_the_callers_input(bundle):
    validated = validate_bundle(bundle, digest(bundle))
    validated["request"]["notice"] = {"modified": True}
    assert bundle["request"]["notice"] != validated["request"]["notice"]


def test_production_transport_campaign_over_real_loopback_http(bundle, tmp_path, monkeypatch):
    """Exercise urllib, its bounded sender, four reservations and durable reports."""
    from hashlib import sha256
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from src.intelligence import starship_protocol_campaign as campaign
    from src.intelligence.space_ops_investigator import _send_once

    output, received = tmp_path / "http-campaign", []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.path == "/chat/completions"
            reservation = json.loads((output / "reservation.json").read_text())
            assert len(reservation["slots"]) == 4
            assert reservation["worstcase_reserved_tokens"] == 24576
            assert reservation["approved_probe_sha256"] == digest(bundle)
            assert self.headers.get("Authorization") == "Bearer " + SECRET_SENTINEL
            wire = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(wire)
            received.append((sha256(wire).hexdigest(), payload["reasoning_effort"], payload["max_tokens"]))
            body = response_for(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    monkeypatch.setattr(campaign, "DEEPSEEK_ENDPOINT", f"http://127.0.0.1:{server.server_port}/chat/completions")
    assert campaign._send_once is _send_once  # The production urllib transport remains intact.
    try:
        result = campaign.run_campaign(bundle, digest(bundle), output, key=SECRET_SENTINEL)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert not thread.is_alive()
    assert received == [
        (variant["provider_request_sha256"], variant["payload"]["reasoning_effort"], variant["payload"]["max_tokens"])
        for variant in bundle["variants"] for _ in range(2)
    ]
    assert result["attempted_calls"] == 4
    assert result["selected_setting_id"] == "ids_low_4096"
    assert all(a["transport_status"] == "response" and a["audit"]["status"] == "valid" for a in result["attempts"])
    assert all(a["audit"]["reference_audit"]["host_resolved_values"] is True for a in result["attempts"])
    assert result["flight_runs"] == 0 and result["dispatch"] is False
    assert result == json.loads((output / "summary.json").read_text())
    for artifact in output.rglob("*.json"):
        assert REASONING_SENTINEL not in artifact.read_text()
        assert SECRET_SENTINEL not in artifact.read_text()


def test_reported_token_cap_violation_stops_remaining_reserved_calls(bundle, tmp_path):
    calls = []

    def sender(_endpoint, wire, _key):
        payload = json.loads(wire)
        calls.append(payload)
        body = json.loads(response_for(payload))
        body["usage"]["completion_tokens"] = payload["max_tokens"] + 1
        return "response", json.dumps(body).encode()

    output = tmp_path / "over-limit"
    result = run_campaign(bundle, digest(bundle), output, key=SECRET_SENTINEL,
                          sender=sender, clock=Clock())
    assert len(calls) == result["attempted_calls"] == 1
    assert result["unspent_reserved_slots"] == 3
    assert result["worstcase_reserved_tokens"] == 24576
    assert result["observed_completion_tokens"] == 4097
    assert result["selected_setting_id"] is None
    assert result["decision"] == "stop_short_deadline_tuning"
    assert "output_budget_exceeded" in result["attempts"][0]["audit"]["errors"]
    assert len(json.loads((output / "reservation.json").read_text())["slots"]) == 4
    assert len(list(output.glob("attempt-*.json"))) == 1

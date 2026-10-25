"""The new long-window replay has its own two slots and immutable old evidence."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from src.intelligence import starship_orbital_protocol_campaign as campaign
from src.intelligence.starship_protocol_probe import prepare as original_prepare
from src.intelligence.starship_reference_protocol import policy_for
from src.runtime.starship_replanning import digest
from tests.contract.test_starship_protocol_probe import saved_study  # noqa: F401

SECRET = "orbital-probe-test-secret-only"
PRIVATE_REASONING = "private-reasoning-should-not-be-retained"


@pytest.fixture
def original(request):
    return original_prepare(request.getfixturevalue("saved_study"), 0)


@pytest.fixture
def bundle(original):
    return campaign.prepare_bundle(original, digest(original))


class Clock:
    def __init__(self, step=.25):
        self.value, self.step = 0., step

    def __call__(self):
        self.value += self.step
        return self.value


def reply_for(payload, *, action="keep_plan", finish="stop"):
    public = json.loads(payload["messages"][1]["content"])
    ids = sorted(name for name, row in public["candidates"].items()
                 if not row["constraint_rejections"] and row["comparison_material"])
    answer = {
        "action": action, "compared_candidate_ids": ids,
        "summary": "Compare both prepared options and preserve the earlier opportunity.",
        "reference_ids": [next(iter(public["reference_ids"][name].values())) for name in ids],
    }
    return json.dumps({
        "model": payload["model"],
        "choices": [{"finish_reason": finish, "message": {
            "content": json.dumps(answer), "reasoning_content": PRIVATE_REASONING,
        }}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180,
                  "completion_tokens_details": {"reasoning_tokens": 40}},
    }).encode()


def fake_sender(outcomes, calls, output, *, action="keep_plan"):
    def send(_endpoint, wire, key, *, timeout_s):
        assert timeout_s == 120 and key == SECRET
        reserved = json.loads((output / "reservation.json").read_text())
        assert reserved["worstcase_reserved_tokens"] == 32768
        assert len(reserved["slots"]) == 2 and reserved["flight_authority"] is False
        payload = json.loads(wire)
        outcome = outcomes[len(calls)]
        calls.append(payload)
        if outcome == "timeout":
            return "transport_timeout", None
        if outcome == "exception":
            raise RuntimeError(SECRET)
        if outcome == "interrupt":
            raise KeyboardInterrupt()
        if outcome == "malformed":
            return "response", b'{"malformed":'
        body = reply_for(payload, action=action, finish="length" if outcome == "length" else "stop")
        if outcome == "cap_violation":
            value = json.loads(body)
            value["usage"]["completion_tokens"] = 16385
            body = json.dumps(value).encode()
        return "response", body
    return send


def execute(bundle, path, outcomes, calls, **options):
    return campaign.run_campaign(bundle, digest(bundle), path, key=SECRET,
                                 sender=fake_sender(outcomes, calls, path),
                                 clock=options.get("clock", Clock()))


def test_preparation_is_new_and_original_protocol_is_unchanged(original):
    before = deepcopy(original)
    bundle = campaign.prepare_bundle(original, digest(original))
    assert original == before
    assert original["proposed_campaign"]["maximum_attempts"] == 4
    assert original["proposed_campaign"]["maximum_generated_tokens"] == 24576
    assert bundle["original_probe"] == original
    assert bundle["original_probe"] is not original
    assert bundle["provider_calls"] == bundle["flight_runs"] == 0
    assert bundle["original_wire_verified"] is True
    assert bundle["original_provider_request_sha256"] == original["original_provider_request_sha256"]
    assert bundle["policy"] == policy_for(campaign.SETTING_ID)
    assert bundle["payload"]["reasoning_effort"] == "low"
    assert bundle["payload"]["max_tokens"] == 16384
    assert bundle["proposed_campaign"]["decision_deadline_s"] == 150
    assert bundle["proposed_campaign"]["transport_timeout_s"] == 120
    assert bundle["held_out"] is False and bundle["model_value_claim"] is False
    assert all(digest(bundle["payload"]) != row["provider_request_sha256"] for row in original["variants"])


def test_validates_full_original_wire_and_new_approval_before_any_call(original, bundle, tmp_path):
    for field in ("original_wire_verified", "original_payload"):
        altered = deepcopy(original)
        if field == "original_wire_verified":
            altered[field] = False
        else:
            altered[field]["messages"][1]["content"] += " altered"
        with pytest.raises(ValueError):
            campaign.prepare_bundle(altered, digest(altered))
    called = []
    output = tmp_path / "no-authority"
    with pytest.raises(ValueError):
        campaign.run_campaign(bundle, "0" * 64, output, key=SECRET,
                              sender=fake_sender(["success"] * 2, called, output))
    assert called == [] and not output.exists()


@pytest.mark.parametrize("field", ["policy", "payload", "campaign", "request", "source", "claimed_done"])
def test_rehashed_modified_bundle_is_rejected(bundle, field):
    changed = deepcopy(bundle)
    if field == "policy":
        changed["policy"]["maximum_output_tokens"] += 1
    elif field == "payload":
        changed["payload"]["messages"][0]["content"] += " extra instruction"
        changed["provider_request_sha256"] = digest(changed["payload"])
    elif field == "campaign":
        changed["proposed_campaign"]["maximum_attempts"] = 3
    elif field == "request":
        changed["original_probe"]["request"]["time_s"] += 1
        changed["original_probe_sha256"] = digest(changed["original_probe"])
    elif field == "source":
        changed["source_sha256"][next(iter(changed["source_sha256"]))] = "0" * 64
    else:
        changed["provider_calls"] = 1
    with pytest.raises(ValueError):
        campaign.validate_bundle(changed, digest(changed))


@pytest.mark.parametrize("outcomes,expected", [
    (["success", "success"], 2), (["success", "length"], 1),
    (["length", "length"], 0), (["timeout", "success"], 1),
    (["malformed", "exception"], 0),
])
def test_exact_two_attempts_only_two_valid_admit_one_separate_flight(bundle, tmp_path, outcomes, expected):
    output, calls = tmp_path / "campaign", []
    result = execute(bundle, output, outcomes, calls)
    assert len(calls) == result["attempted_calls"] == 2
    assert [(row["reasoning_effort"], row["max_tokens"]) for row in calls] == [("low", 16384)] * 2
    assert result["valid_responses"] == expected
    assert result["ready_for_one_separately_bounded_flight"] is (expected == 2)
    assert result["maximum_admitted_followup_flights"] == (1 if expected == 2 else 0)
    assert result["decision"] == ("ready_for_one_separately_bounded_flight" if expected == 2
                                  else "stop_orbital_protocol_testing")
    assert result["worstcase_reserved_tokens"] == 32768
    assert result["flight_runs"] == 0 and result["dispatch"] is False
    assert result["held_out"] is False and result["reliability_estimate"] is False
    assert result == json.loads((output / "summary.json").read_text())
    for artifact in output.glob("*.json"):
        assert SECRET not in artifact.read_text()
        assert PRIVATE_REASONING not in artifact.read_text()


@pytest.mark.parametrize("action", ["keep_plan", "select_next_orbit"])
def test_admission_does_not_prefer_a_mission_action(bundle, tmp_path, action):
    output, calls = tmp_path / "campaign", []
    result = campaign.run_campaign(bundle, digest(bundle), output, key=SECRET,
                                  sender=fake_sender(["success"] * 2, calls, output, action=action),
                                  clock=Clock())
    assert result["valid_responses"] == 2
    assert all(row["audit"]["decision"]["action"] == action for row in result["attempts"])


@pytest.mark.parametrize("interrupted", [False, True])
def test_no_resume_or_replay_even_after_partial_interruption(bundle, tmp_path, interrupted):
    output, calls = tmp_path / "campaign", []
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            execute(bundle, output, ["interrupt"], calls)
        assert len(calls) == 1
    else:
        execute(bundle, output, ["success"] * 2, calls)
        assert len(calls) == 2
    saved = {path.name: path.read_bytes() for path in output.glob("*.json")}
    attempted = []
    with pytest.raises(FileExistsError):
        execute(bundle, output, ["success"] * 2, attempted)
    assert attempted == []
    assert saved == {path.name: path.read_bytes() for path in output.glob("*.json")}


def test_reported_cap_violation_preserves_budget_and_stops_second_slot(bundle, tmp_path):
    calls = []
    result = execute(bundle, tmp_path / "cap", ["cap_violation"], calls)
    assert len(calls) == result["attempted_calls"] == 1
    assert result["unspent_reserved_slots"] == 1
    assert result["stop_reason"] == "reported_token_cap_violation"
    assert result["maximum_admitted_followup_flights"] == 0
    assert result["observed_completion_tokens"] == 16385
    assert result["worstcase_reserved_tokens"] == 32768


def test_escaped_secret_is_not_persisted(bundle, tmp_path):
    def sender(_endpoint, wire, _key, *, timeout_s):
        assert timeout_s == 120
        value = json.loads(reply_for(json.loads(wire)))
        answer = json.loads(value["choices"][0]["message"]["content"])
        answer["summary"] = SECRET
        value["choices"][0]["message"]["content"] = json.dumps(answer)
        body = json.dumps(value).encode().replace(b"orbital-probe", b"\\u006frbital-probe")
        assert SECRET.encode() not in body
        return "response", body
    output = tmp_path / "secret"
    result = campaign.run_campaign(bundle, digest(bundle), output, key=SECRET, sender=sender, clock=Clock())
    assert result["valid_responses"] == 0
    assert all("credential_in_response" in row["audit"]["errors"] for row in result["attempts"])
    assert all(SECRET not in path.read_text() for path in output.glob("*.json"))


def test_late_audits_and_source_change_do_not_admit(bundle, tmp_path, monkeypatch):
    result = execute(bundle, tmp_path / "late", ["success"] * 2, [], clock=Clock(step=151))
    assert result["valid_responses"] == 0
    assert all("decision_deadline" in row["audit"]["errors"] for row in result["attempts"])
    original_hashes = campaign._source_hashes
    calls, output = [], tmp_path / "changed"
    send = fake_sender(["success"], calls, output)

    def changing_sender(*args, **kwargs):
        result = send(*args, **kwargs)
        monkeypatch.setattr(campaign, "_source_hashes", lambda: {"changed.py": "0" * 64})
        return result
    result = campaign.run_campaign(bundle, digest(bundle), output, key=SECRET,
                                  sender=changing_sender, clock=Clock())
    assert len(calls) == 1 and result["stop_reason"] == "source_changed"
    assert result["valid_responses"] == 0 and result["unspent_reserved_slots"] == 1
    monkeypatch.setattr(campaign, "_source_hashes", original_hashes)


def test_prepare_cli_does_not_read_secrets_or_call_provider(original, tmp_path, monkeypatch):
    script = Path(campaign.__file__).resolve().parents[2] / "scripts/starship_orbital_protocol.py"
    spec = importlib.util.spec_from_file_location("starship_orbital_protocol_cli_test", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "_read_secret", lambda *_: pytest.fail("prepare read a credential"))
    source = tmp_path / "original.json"
    source.write_text(json.dumps(original))
    prior_bytes = source.read_bytes()
    output = tmp_path / "prepared"
    monkeypatch.setattr(sys, "argv", [str(script), "prepare", "--original-probe", str(source),
                                    "--approved-original-sha256", digest(original),
                                    "--output-dir", str(output)])
    assert cli.main() == 0
    stored = json.loads((output / "bundle.json").read_text())
    summary = json.loads((output / "summary.json").read_text())
    assert summary["bundle_sha256"] == digest(stored)
    assert summary["provider_calls"] == summary["flight_runs"] == 0
    assert source.read_bytes() == prior_bytes
    campaign.validate_bundle(stored, summary["bundle_sha256"])


def test_new_transport_policy_over_real_loopback_http(bundle, tmp_path, monkeypatch):
    """Production urllib crosses HTTP only after both fixed slots are reserved."""
    from hashlib import sha256
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    output, received = tmp_path / "http-campaign", []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            reservation = json.loads((output / "reservation.json").read_text())
            assert reservation["worstcase_reserved_tokens"] == 32768
            assert reservation["transport_timeout_s"] == 120
            assert self.headers.get("Authorization") == "Bearer " + SECRET
            wire = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(wire)
            received.append((sha256(wire).hexdigest(), payload["reasoning_effort"], payload["max_tokens"]))
            body = reply_for(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    worker.start()
    monkeypatch.setattr(campaign, "DEEPSEEK_ENDPOINT", f"http://127.0.0.1:{server.server_port}/chat/completions")
    try:
        result = campaign.run_campaign(bundle, digest(bundle), output, key=SECRET)
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
    assert not worker.is_alive()
    assert received == [(bundle["provider_request_sha256"], "low", 16384)] * 2
    assert result["valid_responses"] == 2 and result["flight_runs"] == 0
    assert result["maximum_admitted_followup_flights"] == 1
    assert all(row["audit"]["reference_audit"]["host_resolved_values"] for row in result["attempts"])

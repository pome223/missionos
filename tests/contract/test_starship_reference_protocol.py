"""Registered reference protocols and bounded transport, without hosted IO."""

from copy import deepcopy
from threading import Event, Thread
import time

import pytest

from src.intelligence import space_ops_investigator as transport
from src.intelligence import starship_protocol_probe as legacy
from src.intelligence import starship_reference_protocol as shared
from src.runtime.starship_replanning import digest
from tests.contract.test_starship_protocol_probe import (
    saved_study, answer_for, provider_body,  # noqa: F401
)


@pytest.fixture
def bundle(request):
    return legacy.prepare(request.getfixturevalue("saved_study"), 0)


def test_legacy_wire_hashes_remain_identical_to_frozen_protocol(bundle):
    assert [v["provider_request_sha256"] for v in bundle["variants"]] == [
        "cfd1b5742ab79b41dc422bbb8413297e14c65e92d55964209c1a5e8ce7ab28b0",
        "1a09b33132a7422d80b575ca3c04014f221ce81347d2801fae1e87ab3b692217",
    ]
    for variant in bundle["variants"]:
        payload = shared.build_payload(bundle["request"], bundle["original_payload"],
                                       policy=shared.policy_for(variant["setting_id"]))
        assert payload == variant["payload"]
        body = provider_body(payload, answer_for(bundle))
        common = shared.audit_response(bundle["request"], payload, body, 1,
                                       expected_payload_sha256=digest(payload),
                                       policy=shared.policy_for(variant["setting_id"]))
        previous = legacy.audit_response(bundle["request"], payload, body, 1,
                                         expected_payload_sha256=digest(payload))
        assert common == previous


def test_long_policy_is_separate_and_does_not_expand_legacy_settings(bundle):
    policy = shared.policy_for("ids_orbital_low_16384")
    assert policy == {"setting_id": "ids_orbital_low_16384", "reasoning_effort": "low",
                      "maximum_output_tokens": 16384, "transport_timeout_s": 120,
                      "decision_timeout_s": 150}
    payload = shared.build_payload(bundle["request"], bundle["original_payload"], policy=policy)
    assert payload["max_tokens"] == 16384 and payload["reasoning_effort"] == "low"
    with pytest.raises(ValueError, match="unregistered_probe_setting"):
        legacy.build_payload(bundle["request"], bundle["original_payload"], "low", 16384)
    body = provider_body(payload, answer_for(bundle))
    audit = shared.audit_response(bundle["request"], payload, body, 120,
                                  expected_payload_sha256=digest(payload), policy=policy)
    assert audit["status"] == "valid"
    assert audit["reference_audit"]["host_resolved_values"] is True
    assert audit["reference_audit"]["model_numeric_values_verified"] is False
    assert legacy.audit_response(bundle["request"], payload, body, 120,
                                 expected_payload_sha256=digest(payload))["status"] == "invalid"
    audit = shared.audit_response(bundle["request"], payload, body, 150.001,
                                  expected_payload_sha256=digest(payload), policy=policy)
    assert audit["errors"] == ["decision_deadline"]
    original = bundle["variants"][0]["payload"]
    assert legacy.audit_response(bundle["request"], original,
                                 provider_body(original, answer_for(bundle)), 36,
                                 expected_payload_sha256=digest(original))["errors"] == ["decision_deadline"]


@pytest.mark.parametrize("field,value", [
    ("setting_id", "unregistered"), ("reasoning_effort", "high"),
    ("maximum_output_tokens", 32768), ("transport_timeout_s", 121),
    ("decision_timeout_s", 151), ("extra", True),
])
def test_registered_policy_cannot_be_changed_by_a_caller(bundle, field, value):
    policy = shared.policy_for("ids_orbital_low_16384")
    policy[field] = value
    with pytest.raises(ValueError, match="unregistered_probe_setting"):
        shared.build_payload(bundle["request"], bundle["original_payload"], policy=policy)
    assert shared.policy_for("ids_orbital_low_16384")["decision_timeout_s"] == 150


def test_long_response_keeps_payload_and_original_request_binding(bundle):
    policy = shared.policy_for("ids_orbital_low_16384")
    payload = shared.build_payload(bundle["request"], bundle["original_payload"], policy=policy)
    body = provider_body(payload, answer_for(bundle))
    changed = deepcopy(bundle["request"])
    changed["notice"] = {}
    window_changed = deepcopy(bundle["request"])
    window_changed["planning_window"] = {"admitted": True, "provider_timeout_s": 120}
    assert shared.audit_response(window_changed, payload, body, 40,
                                 expected_payload_sha256=digest(payload), policy=policy)["errors"] == ["decision_input_mismatch"]
    assert shared.audit_response(changed, payload, body, 40,
                                 expected_payload_sha256=digest(payload), policy=policy)["errors"] == ["decision_input_mismatch"]
    assert shared.audit_response(bundle["request"], payload, body, 40,
                                 expected_payload_sha256="0" * 64, policy=policy)["errors"] == ["provider_request_mismatch"]
    assert shared.audit_response(bundle["request"], payload,
                                 provider_body(payload, answer_for(bundle), finish="length"), 40,
                                 expected_payload_sha256=digest(payload), policy=policy)["errors"] == ["output_limit"]


class Response:
    def __init__(self, data=b"{}", read=None):
        self.data, self.read_callback = data, read

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        assert limit == transport.MAX_RESPONSE_BYTES + 1
        return self.read_callback() if self.read_callback else self.data


def fake_open(monkeypatch, response):
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, request.data, timeout))
            return response

    monkeypatch.setattr(transport, "_provider_opener", lambda: Opener())
    return calls


def test_three_argument_transport_stays_30_seconds_and_explicit_120_is_per_request(monkeypatch):
    calls = fake_open(monkeypatch, Response())
    endpoint = "https://api.deepseek.com/chat/completions"
    assert transport._send_once(endpoint, b"{}", "stub") == ("response", b"{}")
    assert transport._send_once(endpoint, b"{}", "stub", timeout_s=120) == ("response", b"{}")
    assert transport._send_once(endpoint, b"{}", "stub") == ("response", b"{}")
    assert len(calls) == 3
    for (_, _, received), expected in zip(calls, (30, 120, 30)):
        assert expected - 1 < received <= expected
    assert transport.TIMEOUT_SECONDS == 30


@pytest.mark.parametrize("timeout", [True, 0, -1, 120.1, float("inf"), float("nan"), 10**1000, "120"])
def test_bad_timeout_is_rejected_before_network(monkeypatch, timeout):
    monkeypatch.setattr(transport, "_provider_opener", lambda: pytest.fail("network was reached"))
    with pytest.raises(ValueError, match="invalid_provider_request_budget"):
        transport._send_once("https://api.deepseek.com/chat/completions", b"{}", "stub", timeout_s=timeout)


def test_absolute_deadline_rejects_late_response_without_retry(monkeypatch):
    release, done = Event(), Event()

    def read():
        release.wait(1)
        done.set()
        return b"late response"

    calls = fake_open(monkeypatch, Response(read=read))
    started = time.monotonic()
    try:
        assert transport._send_once("https://api.deepseek.com/chat/completions", b"{}", "stub", timeout_s=0.02) == ("transport_timeout", None)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
    assert done.wait(1)
    assert len(calls) == 1


def test_cancellation_returns_before_read_and_discards_late_completion(monkeypatch):
    active, reading, release, finished = Event(), Event(), Event(), Event()
    active.set()

    def read():
        reading.set()
        release.wait(1)
        return b"late response"

    calls = fake_open(monkeypatch, Response(read=read))
    result = []

    def send():
        result.append(transport._send_once("https://api.deepseek.com/chat/completions", b"{}", "stub",
                                           timeout_s=120, active=active.is_set))
        finished.set()

    thread = Thread(target=send, daemon=True)
    thread.start()
    try:
        assert reading.wait(1)
        active.clear()
        assert finished.wait(0.5)
        assert result == [("not_active", None)]
    finally:
        release.set()
        thread.join(timeout=1)
    assert not thread.is_alive()
    assert len(calls) == 1
    assert result == [("not_active", None)]


def test_inactive_lifecycle_does_not_start_request(monkeypatch):
    monkeypatch.setattr(transport, "_provider_opener", lambda: pytest.fail("network was reached"))
    assert transport._send_once("https://api.deepseek.com/chat/completions", b"{}", "stub",
                                timeout_s=120, active=lambda: False) == ("not_active", None)

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler
import pytest

from src.intelligence.jev_assurance import JevAssuranceJudge, JevShadowJudge
from src.intelligence.mission_assurance_agent import (
    MissionAssuranceAgent,
    MissionSituation,
    ModelJudgment,
    MissionAssuranceJudgeError,
    MissionAssuranceJudgeUnavailable,
    configured_mission_assurance_agent,
)


def situation():
    return MissionSituation(
        "fixture",
        "2026-09-20T00:00:00Z",
        {},
        {},
        {},
        {},
        {},
        ("fixture",),
        "fixture.v1",
        "input",
        "fixture",
    )


def response(payload):
    labels = payload["questions"]["response"]["criteria"]
    return {
        "model": "fixture-jev",
        "answers": {
            "response": {
                "type": "choice",
                "choice": "hold",
                "confidence": 0.9,
                "probabilities": {k: float(k == "hold") for k in labels},
            },
            "review": {"type": "choice", "choice": "bounded"},
        },
    }


def test_jev_bounded_choice_never_creates_authority():
    result = (
        MissionAssuranceAgent(JevAssuranceJudge(transport=response)).evaluate(situation()).to_dict()
    )
    assert result["proposed_response_kind"] == "hold"
    assert result["judgment_status"] == "proposal_guardrail_passed"
    assert result["parameters"] == {}
    assert result["model_invocation_evidence"]["rationale_source"] == "adapter_template"
    for key in ("dispatch_authority_created", "physical_execution_invoked", "progress_counted"):
        assert result[key] is False


@pytest.mark.parametrize("defect", ["choice", "nan", "missing", "probability_sum", "timeout"])
def test_jev_invalid_or_failed_response_escalates(defect):
    def broken(payload):
        if defect == "timeout":
            raise TimeoutError("provider timeout")
        result = response(payload)
        answer = result["answers"]["response"]
        if defect == "choice":
            answer["choice"] = "approved"
        if defect == "nan":
            answer["confidence"] = float("nan")
        if defect == "missing":
            del answer["probabilities"]["continue"]
        if defect == "probability_sum":
            answer["probabilities"]["hold"] = 0.1
        return result

    result = MissionAssuranceAgent(JevAssuranceJudge(transport=broken)).evaluate(situation())
    assert result.proposed_response_kind == "operator_escalation"
    assert result.judgment_status == "failed"


@pytest.mark.parametrize("fail", [True, False])
def test_shadow_cannot_change_primary_output(fail):
    primary_output = {
        "proposed_response_kind": "continue",
        "parameters": {},
        "rationale": "primary",
        "expected_outcome": "primary",
        "uncertainty": "primary",
        "operator_question": "primary",
    }

    class Primary:
        def judge(self, prompt):
            return ModelJudgment(deepcopy(primary_output), {"provider": "fixture"})

    def transport(payload):
        if fail:
            raise TimeoutError()
        return response(payload)

    result = MissionAssuranceAgent(
        JevShadowJudge(Primary(), JevAssuranceJudge(transport=transport))
    ).evaluate(situation())
    assert result.proposed_response_kind == "continue"
    assert result.rationale == "primary"
    shadow = result.model_invocation_evidence["jev_shadow"]
    assert shadow["used_for_decision"] is False
    assert shadow["status"] == ("failed" if fail else "observed")
    if not fail:
        assert shadow["agrees_with_primary"] is False


def test_default_off_preserves_missing_provider_without_invocation(monkeypatch):
    for variable in (
        "MISSIONOS_JEV_MODE",
        "MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED",
        "MISSIONOS_MISSION_ASSURANCE_COMMAND",
    ):
        monkeypatch.delenv(variable, raising=False)
    result = configured_mission_assurance_agent().evaluate(situation())
    assert result.judgment_status == "not_configured"
    assert result.model_inference_invoked is False


def test_explicit_code_only_judge_and_missing_shadow_do_not_become_model_calls(monkeypatch):
    class CodeOnlyJudge:
        def judge(self, prompt):
            return ModelJudgment(
                output={
                    "proposed_response_kind": "replan",
                    "parameters": {},
                    "rationale": "Deterministic test proposal.",
                    "expected_outcome": "A bounded proposal is recorded.",
                    "uncertainty": "No model or physical execution.",
                    "operator_question": "Review the proposal separately.",
                },
                invocation_evidence={"invocation_kind": "deterministic_fixture"},
                model_inference_invoked=False,
            )

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    result = MissionAssuranceAgent(JevShadowJudge(CodeOnlyJudge())).evaluate(situation())
    assert result.proposed_response_kind == "replan"
    assert result.model_inference_invoked is False
    assert result.judgment_mode == "deterministic_routing"
    shadow = result.model_invocation_evidence["jev_shadow"]
    assert shadow["status"] == "not_configured"
    assert shadow["model_inference_invoked"] is False


@pytest.mark.parametrize("shadow_called", [False, True])
def test_shadow_failure_preserves_invocation_when_primary_is_unavailable(shadow_called):
    class Unavailable:
        def judge(self, prompt):
            raise MissionAssuranceJudgeUnavailable("disabled")

    class BrokenShadow:
        def judge(self, prompt):
            raise MissionAssuranceJudgeError(
                "fixture_typed_failure",
                status="failed",
                invoked=shadow_called,
                invocation_evidence={"validation_reason": "fixture_failure"},
            )

    result = MissionAssuranceAgent(JevShadowJudge(Unavailable(), BrokenShadow())).evaluate(
        situation()
    )
    assert result.judgment_status == "not_configured"
    assert result.model_inference_invoked is shadow_called
    shadow = result.model_invocation_evidence["jev_shadow"]
    assert shadow["validation_reason"] == "fixture_typed_failure"
    assert shadow["invocation_evidence"]["validation_reason"] == "fixture_failure"
    assert shadow["incumbent_error_type"] == "MissionAssuranceJudgeUnavailable"


def test_both_typed_failures_are_retained_in_shadow():
    class Broken:
        def __init__(self, label):
            self.label = label

        def judge(self, prompt):
            raise MissionAssuranceJudgeError(
                self.label,
                status="failed",
                invoked=True,
                invocation_evidence={"validation_reason": self.label},
            )

    result = MissionAssuranceAgent(JevShadowJudge(Broken("primary"), Broken("shadow"))).evaluate(
        situation()
    )
    shadow = result.model_invocation_evidence["jev_shadow"]
    assert shadow["incumbent_failure"]["invocation_evidence"]["validation_reason"] == "primary"
    assert shadow["invocation_evidence"]["validation_reason"] == "shadow"
    assert shadow["used_for_decision"] is False
    assert result.proposed_response_kind == "operator_escalation"


def test_invalid_provider_body_is_hashed_and_never_records_key_or_body(monkeypatch):
    from src.intelligence import jev_assurance

    marker = "fixture-private-token-not-a-real-key"
    monkeypatch.setenv("TYPESAFE_API_KEY", marker)

    class BadResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, amount):
            assert amount == jev_assurance.MAX_RESPONSE_BYTES + 1
            return ("not json " + marker).encode()

    def fake_urlopen(request, timeout):
        assert request.headers["Authorization"] == "Bearer " + marker
        assert marker.encode() not in request.data
        return BadResponse()

    monkeypatch.setattr(
        jev_assurance, "_provider_opener", lambda: SimpleNamespace(open=fake_urlopen)
    )
    result = MissionAssuranceAgent(JevAssuranceJudge()).evaluate(situation())
    assert result.judgment_status == "failed" and result.model_inference_invoked
    assert result.model_invocation_evidence["validation_reason"] == "invalid_jev_json"
    assert result.model_invocation_evidence["raw_response_recorded"] is False
    assert marker not in json.dumps(result.to_dict())


def test_provider_transport_fixes_endpoint_disables_proxy_and_bounds_body(monkeypatch):
    from src.intelligence import jev_assurance

    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture-key")
    monkeypatch.setenv("HTTPS_PROXY", "https://unused.invalid:443")
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, amount):
            seen["read_amount"] = amount
            return b"{}"

    def fake_open(request, timeout):
        seen["url"] = request.full_url
        assert request.get_method() == "POST"
        assert request.headers["Authorization"] == "Bearer fixture-key"
        return Response()

    def fake_builder(*handlers):
        proxies = [handler for handler in handlers if isinstance(handler, ProxyHandler)]
        assert len(proxies) == 1 and proxies[0].proxies == {}
        assert any(isinstance(handler, jev_assurance._NoRedirect) for handler in handlers)
        return SimpleNamespace(open=fake_open)

    monkeypatch.setattr(jev_assurance, "build_opener", fake_builder)
    assert JevAssuranceJudge()._request({"bounded": True}) == {}
    assert seen == {"url": "https://api.typesafe.ai/v1/systemone", "read_amount": 65537}


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_does_not_send_bearer_to_another_origin(monkeypatch, status):
    from src.intelligence.jev_assurance import _provider_opener

    received = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(status)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/target")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [Thread(target=server.serve_forever, daemon=True) for server in (origin, target)]
    for thread in threads:
        thread.start()
    # Proxy inheritance would send this request elsewhere instead of our fixture.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    try:
        request = Request(
            f"http://127.0.0.1:{origin.server_port}/redirect",
            data=b"{}",
            headers={"Authorization": "Bearer fixture-only-token"},
        )
        with pytest.raises(HTTPError) as error:
            _provider_opener().open(request, timeout=2)
        assert error.value.code == status
        error.value.close()
        assert received == []
    finally:
        for server in (origin, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


@pytest.mark.parametrize("failure", ["oversize", "http_error", "redirect"])
def test_transport_failure_does_not_record_raw_body_or_exception(monkeypatch, failure):
    from src.intelligence import jev_assurance

    marker = "fixture-private-marker"
    monkeypatch.setenv("TYPESAFE_API_KEY", marker)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, amount):
            assert amount == 65537
            return b"x" * amount

    def fake_open(request, timeout):
        if failure in {"http_error", "redirect"}:
            raise HTTPError(
                request.full_url, 401 if failure == "http_error" else 302, marker, None, None
            )
        return Response()

    monkeypatch.setattr(jev_assurance, "_provider_opener", lambda: SimpleNamespace(open=fake_open))
    result = MissionAssuranceAgent(JevAssuranceJudge()).evaluate(situation())
    assert result.judgment_status == "failed"
    assert result.model_inference_invoked is False
    evidence = result.model_invocation_evidence
    assert evidence["call_attempted"] is True
    assert evidence["model_inference_status"] == "unconfirmed_after_attempt"
    assert evidence["complete_response_observed"] is False
    assert evidence["validation_reason"] == (
        "jev_response_too_large" if failure == "oversize" else "jev_transport_failed"
    )
    assert evidence["raw_response_recorded"] is False
    if failure == "oversize":
        assert evidence["response_bytes_read"] == 65537
        assert evidence["complete_response_observed"] is False
        assert "response_sha256" not in evidence
    assert marker not in json.dumps(result.to_dict())

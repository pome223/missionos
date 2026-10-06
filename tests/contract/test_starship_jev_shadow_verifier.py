"""Independent saved-record checks; fixture and mocked transport only."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from src.intelligence import starship_jev_router as router_module
from src.runtime.starship_dispenser_experiment import run_dispenser_experiment
from src.runtime.starship_jev_shadow import source_hashes
from src.runtime.starship_jev_shadow_report import build_report
from src.runtime.starship_jev_shadow_verifier import expected_fault_frames, verify_shadow_bundle


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@pytest.fixture(scope="module")
def baseline():
    return run_dispenser_experiment(approved=True)


def make_bundle(baseline, router, mode="fixture"):
    runs = [
        deepcopy(row)
        for row in baseline["policy_runs"]
        if row["split"] == "eval" and row["policy"] == "history_rule"
    ]
    records = []
    # Build test observations from the persisted public history, independently of
    # the verifier's prior-receipt walk.
    for run in runs:
        for step in run["steps"]:
            if not any(row["success"] is False for row in step["public_history"]["retry_results"]):
                continue
            frame = {
                "kind": "fault_observation",
                "case_ref": sha256(run["world_id"].encode()).hexdigest(),
                "step_index": step["index"],
                "public_history": deepcopy(step["public_history"]),
                "public_budget": deepcopy(step["public_budget"]),
            }
            records.append(
                {
                    "frame": frame,
                    "frame_sha256": digest(frame),
                    "routing": router.route(frame["public_history"], frame["public_budget"]),
                }
            )
            break
    return {
        "schema": "missionos.starship_jev_shadow_bundle.v1",
        "baseline": deepcopy(baseline),
        "execution_runs": runs,
        "shadow": {
            "mode": mode,
            "trigger": "first_decision_after_observed_retry_failure",
            "maximum_calls": 22,
            "records": records,
            "worker": {
                "kind": "complete",
                "worker_pid": 12345,
                "provider_credentials_present": False,
                "emitted_count": len(records),
                "evaluation_count": len(runs),
            },
            "worker_exit_code": 0,
            "source_sha256": source_hashes(),
            "physical_execution": False,
            "used_for_decision": False,
            "real_time_guarantee": False,
        },
    }


@pytest.fixture
def bundle(baseline):
    return make_bundle(baseline, router_module.StarshipJevRouter(mode="fixture"))


def test_fixture_full_baseline_reruns_and_independent_fault_frames_verify(bundle):
    verdict = verify_shadow_bundle(bundle)
    assert verdict["verified"], verdict
    assert verdict["summary"] == {
        "evaluation_count": 60,
        "baseline_runs_checked": 870,
        "reruns_checked": 60,
        "terminal_match_count": 60,
        "mode": "fixture",
        "trigger_count": 22,
        "no_trigger_count": 38,
        "recorded_calls_attempted": 0,
        "recorded_responses_observed": 0,
        "route_counts": {"bounded": 22},
        "status_counts": {"fixture_only": 22},
    }
    assert not verdict["provider_invocation_authenticated"]
    assert not verdict["worker_process_authenticated"]
    assert not verdict["model_value_evaluated"]
    assert not verdict["real_time_guarantee"]
    assert [record["frame"] for record in bundle["shadow"]["records"]] == expected_fault_frames(
        bundle["execution_runs"]
    )
    assert verify_shadow_bundle(json.loads(json.dumps(bundle)))["verified"]


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda b: b["execution_runs"].pop(), "execution_runs.exact_baseline_match"),
        (
            lambda b: b["execution_runs"][0]["terminal"].update(release_count=99),
            "execution_runs.exact_baseline_match",
        ),
        (lambda b: b["execution_runs"].reverse(), "execution_runs.exact_baseline_match"),
        (lambda b: b["shadow"]["records"].pop(), "shadow.records.count"),
        (lambda b: b["shadow"]["records"].reverse(), "public_frame"),
        (
            lambda b: b["shadow"]["records"].__setitem__(1, deepcopy(b["shadow"]["records"][0])),
            "public_frame",
        ),
        (lambda b: b["shadow"].update(used_for_decision=True), "shadow.used_for_decision"),
        (lambda b: b["shadow"].update(real_time_guarantee=True), "shadow.real_time_guarantee"),
        (lambda b: b["shadow"].update(physical_execution=0), "shadow.physical_execution"),
        (lambda b: b["shadow"].update(maximum_calls=23), "shadow.maximum_calls"),
        (lambda b: b["shadow"].update(worker_exit_code=False), "shadow.worker_exit_code"),
        (lambda b: b["shadow"]["worker"].update(worker_pid=True), "shadow.worker.worker_pid"),
        (
            lambda b: b["shadow"]["worker"].update(provider_credentials_present=True),
            "shadow.worker.credentials",
        ),
        (lambda b: b["shadow"]["source_sha256"].clear(), "shadow.source_sha256"),
        (
            lambda b: b["baseline"]["policy_runs"][0]["terminal"].update(release_count=9),
            "baseline.independent_verification",
        ),
    ],
)
def test_changed_execution_or_contract_is_rejected(bundle, mutation, reason):
    mutation(bundle)
    verdict = verify_shadow_bundle(bundle)
    assert verdict["verified"] is False
    assert reason in verdict["reasons"][0]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda frame: frame.update(case_ref="0" * 64),
        lambda frame: frame.update(step_index=frame["step_index"] + 1),
        lambda frame: frame.update(recovery_time_s=8),
        lambda frame: frame["public_budget"].update(released=True),
        lambda frame: frame["public_history"].update(world_id="private-world"),
        lambda frame: frame["public_history"]["observations"].append(
            {"time_ticks": 12, "healthy": True}
        ),
        lambda frame: frame["public_history"]["retry_results"][-1].update(success=True),
    ],
)
def test_hidden_future_or_rebound_frames_are_rejected_even_with_recomputed_hash(bundle, mutation):
    record = bundle["shadow"]["records"][0]
    mutation(record["frame"])
    record["frame_sha256"] = digest(record["frame"])
    verdict = verify_shadow_bundle(bundle)
    assert not verdict["verified"]
    assert "public_frame" in verdict["reasons"][0]


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda r: r.update(target_role="deepseek_proposal_only"), "target_role"),
        (lambda r: r.update(route="deep_reasoning", target_role="deepseek_proposal_only"), "route"),
        (lambda r: r["invocation"].update(approval_granted=True), "approval_granted"),
        (lambda r: r["invocation"].update(executor_influenced=True), "executor_influenced"),
        (lambda r: r["invocation"].update(model_inference_invoked=True), "model_inference_invoked"),
        (lambda r: r["invocation"].update(call_attempted=True), "call_attempted"),
        (lambda r: r["invocation"].update(request_sha256="a" * 64), "request_sha256"),
        (lambda r: r["invocation"].update(public_input_sha256="a" * 64), "public_input_sha256"),
        (lambda r: r["invocation"].update(latency_ms=float("nan")), "latency_ms"),
        (lambda r: r["invocation"].update(maximum_calls=True), "maximum_calls"),
    ],
)
def test_routing_authority_or_invocation_claim_tampering_fails(bundle, mutation, reason):
    mutation(bundle["shadow"]["records"][0]["routing"])
    verdict = verify_shadow_bundle(bundle)
    assert not verdict["verified"]
    assert reason in verdict["reasons"][0]


def live_router(monkeypatch, outcome):
    monkeypatch.setenv("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", "live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture-only-not-a-secret")
    response = json.dumps(
        {
            "model": "jev-test",
            "answers": {
                "assessment_route": {
                    "type": "choice",
                    "choice": "deep_reasoning",
                    "confidence": 0.7,
                    "probabilities": {
                        "bounded": 0.1,
                        "need_observation": 0.1,
                        "human_review": 0.1,
                        "deep_reasoning": 0.7,
                    },
                }
            },
        }
    ).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, amount):
            assert amount == 65537
            return {
                "succeeded": response,
                "invalid_response": b"not JSON",
                "response_too_large": b"x" * 65537,
            }[outcome]

    def open_request(*_args, **_kwargs):
        if outcome == "transport_failed":
            raise OSError("fixture-private-error-not-to-be-recorded")
        return Response()

    monkeypatch.setattr(
        router_module, "_provider_opener", lambda: SimpleNamespace(open=open_request)
    )
    return router_module.StarshipJevRouter(mode="live")


@pytest.mark.parametrize(
    "outcome,observed,route",
    [
        ("succeeded", 22, "deep_reasoning"),
        ("transport_failed", 0, "unavailable"),
        ("invalid_response", 22, "unavailable"),
        ("response_too_large", 22, "unavailable"),
    ],
)
def test_mocked_live_outcomes_preserve_failures_without_execution_changes(
    baseline, monkeypatch, outcome, observed, route
):
    bundle = make_bundle(baseline, live_router(monkeypatch, outcome), mode="live")
    verdict = verify_shadow_bundle(bundle)
    assert verdict["verified"], verdict
    assert verdict["summary"]["recorded_calls_attempted"] == 22
    assert verdict["summary"]["recorded_responses_observed"] == observed
    assert verdict["summary"]["route_counts"] == {route: 22}
    assert verdict["summary"]["terminal_match_count"] == 60
    assert "fixture-private-error" not in json.dumps(bundle)


def test_live_slots_must_match_recorded_attempt_order(baseline, monkeypatch):
    bundle = make_bundle(baseline, live_router(monkeypatch, "succeeded"), mode="live")
    bundle["shadow"]["records"][1]["routing"]["invocation"]["reserved_call_slot"] = 1
    verdict = verify_shadow_bundle(bundle)
    assert not verdict["verified"] and "reserved_call_slot" in verdict["reasons"][0]


def test_no_trigger_for_a_failure_at_terminal_boundary():
    run = {"world_id": "minimal", "steps": [{"receipt": {"action": "retry", "success": False}}]}
    assert expected_fault_frames([run]) == []


def test_report_names_limits_and_escapes_record_content(bundle):
    verdict = verify_shadow_bundle(bundle)
    assert verdict["verified"]
    html = build_report(bundle, verdict)
    for text in (
        "固定fixture",
        "38",
        "870",
        "DeepSeek呼出しは実行しません",
        "実時間での応答を保証しません",
        "Jevの有用性は評価していません",
        "追加の推論が必要だったか",
    ):
        assert text in html
    assert "<script" not in html and "http://" not in html and "https://" not in html
    bundle["shadow"]["records"][0]["routing"]["route"] = '<img src=x onerror="bad">'
    html = build_report(bundle, {"verified": False, "summary": {}, "reasons": ["<unsafe>"]})
    assert "<img src=x" not in html
    assert "&lt;img" in html and "&lt;unsafe&gt;" in html
    assert "保存記録の照合：不合格・未確認" in html


def test_live_configuration_without_credential_is_not_an_inference_claim(baseline, monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", "live")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    bundle = make_bundle(baseline, router_module.StarshipJevRouter(mode="live"), mode="live")
    verdict = verify_shadow_bundle(bundle)
    assert verdict["verified"], verdict
    assert verdict["summary"]["recorded_calls_attempted"] == 0
    assert verdict["summary"]["recorded_responses_observed"] == 0
    assert verdict["summary"]["status_counts"] == {"credential_unavailable": 22}
    html = build_report(bundle, verdict)
    assert "live設定（呼出しの有無・成否は記録で確認）" in html
    assert "Jev APIへの呼出しを含む記録" not in html


def test_timeout_records_remain_unconfirmed_and_never_supply_a_route(baseline, monkeypatch):
    bundle = make_bundle(baseline, live_router(monkeypatch, "transport_failed"), mode="live")
    for record in bundle["shadow"]["records"]:
        record["routing"]["invocation"]["status"] = "transport_timeout"
    verdict = verify_shadow_bundle(bundle)
    assert verdict["verified"], verdict
    assert verdict["summary"]["status_counts"] == {"transport_timeout": 22}
    assert verdict["summary"]["recorded_responses_observed"] == 0
    assert verdict["summary"]["route_counts"] == {"unavailable": 22}

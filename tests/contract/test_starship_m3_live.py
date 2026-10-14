"""Cross-flight composition and provider-deadline checks; no numerical/model calls."""
from copy import deepcopy

import pytest

from src.runtime import starship_m3_live as m3
from src.runtime.starship_replanning import digest


@pytest.fixture
def bundle(monkeypatch):
    from src.runtime import starship_m2_verifier, starship_m3_prefix_verifier, starship_m3_prefix
    monkeypatch.setattr(starship_m3_prefix, "EXTRA_SOURCES", ("source",))
    authority = m3.contract("live")
    prefix = {"authority": authority["prefix_authority"], "run_id": "run", "profile": {},
        "source_sha256": {"source": "abc"}, "prefix_run": {"booster_mission_director": {"records": []},
            "mission_director": {"records": []}}, "trace": [], "handoff": {},
        "deployment_attempt_ledger": {"attempts": []},
        "provider_attempt_ledger": {"ascent": {"attempts": []}, "booster": {"attempts": []}},
        "ascent": {"decisions": []}}
    inner = {"profile": {}, "concurrent_prefix": prefix, "deployment_run": deepcopy(prefix["prefix_run"]),
        "trace": [], "handoff": {}, "provider_attempt_ledger": {"deployment": {"attempts": []}},
        "runtime": {"model_calls": 0, "wall_time_s": 1.0}, "return_study": {"decisions": []}}
    full_sources = {"source": "abc", **authority["prefix_authority"]["booster_recovery_report_source_sha256"]}
    study = {"schema": "missionos.starship_m3_control_study.v1", "authority": authority,
        "run_id": "run", "profile": {}, "source_sha256": full_sources, "sources_unchanged": True,
        "m3_completed": False, "human_inflight_commands": 0, "retry_performed": False, "physical_execution": False,
        "m2_study": inner}
    monkeypatch.setattr(starship_m2_verifier, "verify", lambda *a, **k:
        {"passed": True, "issues": [], "case_issues": []})
    monkeypatch.setattr(starship_m3_prefix_verifier, "verify", lambda *a, **k:
        {"passed": True, "issues": [], "booster_splashdown": {"controlled_water_entry_envelope_met": True}})
    kwargs = {"expected_authority": deepcopy(authority), "expected_sources": full_sources,
        "expected_return_sources": {}, "expected_run_id": "run"}
    return study, kwargs


def test_composed_evidence_without_real_model_receipts_never_claims_ai_control(bundle):
    study, kwargs = bundle
    result = m3.verify(study, **kwargs)
    assert result["passed"]
    assert result["case_accepted"] and not result["ai_control_demonstrated"]
    assert not result["live_phase_coverage_complete"]
    assert result["case_issues"] == []


@pytest.mark.parametrize("field", ["run_id", "profile", "source_sha256", "prefix_run", "trace", "handoff", "deployment_attempt_ledger"])
def test_individually_valid_prefix_cannot_substitute_different_flight(bundle, field):
    study, kwargs = bundle
    p = study["m2_study"]["concurrent_prefix"]
    p[field] = "other" if field == "run_id" else ["other"] if field == "trace" else {"other": "value"}
    result = m3.verify(study, **kwargs)
    assert not result["passed"] and not result["case_accepted"]
    assert result["issues"]


def test_failure_after_subverifiers_pass_cannot_leave_outer_pass_true(bundle):
    study, kwargs = bundle
    study["m2_study"]["concurrent_prefix"]["provider_attempt_ledger"]["ascent"]["attempts"] = [{}] * 5
    result = m3.verify(study, **kwargs)
    assert "m3_total_provider_budget" in result["issues"]
    assert not result["passed"] and not result["case_accepted"]


def test_live_contract_budget_matches_nested_phase_grants():
    a = m3.contract("live")
    assert a["maximum_jev_calls"] == 26 and a["maximum_llm_calls"] is None
    assert a["maximum_model_calls"] is None
    assert digest(a["prefix_authority"]) == a["prefix_authority_sha256"]
    assert a["inflight_scope_amendment_enabled"] is False


@pytest.mark.parametrize("bad", [-1, 2400, float("nan"), True])
def test_runtime_budget_cannot_claim_success_after_deadline(bundle, bad):
    study, kwargs = bundle
    study["m2_study"]["runtime"]["wall_time_s"] = bad
    result = m3.verify(study, **kwargs)
    assert not result["passed"] and "m3_wall_time_budget" in result["issues"]


def test_manifest_cannot_omit_registered_source(bundle):
    study, kwargs = bundle
    study["m2_study"]["concurrent_prefix"]["source_sha256"] = {}
    assert not m3.verify(study, **kwargs)["passed"]


def test_plan_refuses_changed_report_source_before_provider_configuration(monkeypatch):
    a = m3.contract("live")
    sources = dict(a["prefix_authority"]["booster_recovery_report_source_sha256"])
    sources[next(iter(sources))] = "0" * 64
    monkeypatch.setattr("src.intelligence.starship_m2_broker.require_live_configuration",
        lambda *_: pytest.fail("provider configuration must not be reached"))
    with pytest.raises(ValueError, match="approved_recovery_report_source_mismatch"):
        m3.plan_fields("live", "orbital_update", sources)


def test_early_failure_always_has_false_judgment_and_empty_phase_map(bundle):
    study, kwargs = bundle
    study["sources_unchanged"] = False
    verdict = m3.verify(study, **kwargs)
    assert not verdict["passed"] and verdict["booster_recovery_judgment_observed"] is False
    assert verdict["live_phase_decisions"] == {}

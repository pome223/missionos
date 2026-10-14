"""Scheduler and approved count semantics; no hosted model or physics run."""
from copy import deepcopy

import pytest

from src.runtime.starship_call_limits import (
    call_count_allowed, combined_call_limit, has_call_slot, remaining_calls,
)
from src.runtime.starship_m2_contract import m2_contract, m2_contract_matches
from src.runtime.starship_m3_live import contract as m3_contract
from src.runtime.starship_replanning import contract, digest
from tests.contract.test_starship_m123_provider_scheduling import comparison_actor
from tests.contract.test_starship_replanning_event_runtime import make_candidate


def test_scheduler_reserves_repeated_distinct_deepseek_comparisons(monkeypatch, tmp_path):
    actor, _ = comparison_actor(monkeypatch, tmp_path)
    actor.envelope["mode"] = "live"
    actor.envelope["maximum_llm_calls"] = None
    actor.candidates = {name: make_candidate(actor, name) for name in actor.times}
    actor.comparison_context["ready"] = True
    allowed = ["select_nominal", "select_next_orbit"]
    for index in range(3):
        actor.comparison_context["generation_sha256"] = str(index) * 64
        assert actor.choose("orbital_event", allowed) in allowed
        row = actor.decisions[-1]
        assert row["request_disposition"] == "submitted"
        assert row["provider_budget"]["provider"] == "deepseek"
        assert row["provider_budget"]["maximum_slots"] is None
        assert row["provider_budget"]["used_slots"] == index
    # Unlimited deep reasoning does not re-offer the same comparison generation.
    actor.choose("selected_plan_monitoring", allowed)
    assert actor.decisions[-1]["provider_budget"]["provider"] == "jev"
    actor.choose("selected_plan_monitoring", allowed)
    assert actor.decisions[-1]["request_disposition"] == "budget_exhausted"


def test_current_contracts_propagate_explicit_null_without_hidden_total_cap():
    returning = contract("live", "comparison_orbital_update")
    m2, m3 = m2_contract("live", "orbital_update"), m3_contract("live")
    assert returning["maximum_llm_calls"] is None
    for envelope in (m2, m3):
        assert envelope["maximum_llm_calls"] is None
        assert envelope["maximum_model_calls"] is None
        assert type(envelope["maximum_jev_calls"]) is int
    assert m3["maximum_wall_time_s"] == 2400
    assert returning["orbital_planning_window"]["provider_timeout_s"] == 120.0
    assert returning["maximum_forecasts"] == 20


def test_recorded_finite_contract_keeps_original_cap_and_is_not_upgraded():
    old = m2_contract("live", "orbital_update", legacy_call_limits=True)
    assert old["maximum_llm_calls"] == 1 and old["maximum_model_calls"] == 23
    assert m2_contract_matches(old)
    assert m2_contract_matches(m2_contract("live", "orbital_update"))
    altered = deepcopy(old)
    altered["maximum_llm_calls"] = None
    assert not m2_contract_matches(altered)
    assert digest(old) != digest(m2_contract("live", "orbital_update"))


@pytest.mark.parametrize("bad", [True, -1, 1.5, float("inf"), "unlimited"])
def test_unlimited_is_only_explicit_json_null(bad):
    assert not has_call_slot(0, bad)
    assert not call_count_allowed(1, bad)
    with pytest.raises(ValueError):
        combined_call_limit(3, bad)
    with pytest.raises(ValueError):
        remaining_calls(0, bad)


def test_unlimited_counts_remain_validated_and_finite_caps_enforced():
    assert has_call_slot(1000, None) and call_count_allowed(1001, None)
    assert remaining_calls(1000, None) is None
    assert combined_call_limit(27, None) is None
    assert not call_count_allowed(True, None)
    assert not has_call_slot(-1, None)
    assert call_count_allowed(1, 1) and not has_call_slot(1, 1)
    assert not call_count_allowed(2, 1)


def test_approval_display_names_unlimited_without_rendering_none():
    from src.gateway.starship_chat import _deepseek_call_limit
    assert _deepseek_call_limit(m3_contract("live")) == "DeepSeek回数上限なし（判断期限は適用）"
    assert _deepseek_call_limit(m3_contract("live", legacy_call_limits=True, legacy_booster_jev=True)) == "DeepSeek最大1回"


def test_historical_m3_grant_has_consistent_finite_nested_prefix():
    old = m3_contract("live", legacy_call_limits=True, legacy_booster_jev=True)
    assert old["maximum_llm_calls"] == 1 and old["maximum_model_calls"] == 28
    assert old["m2_authority"] == old["prefix_authority"]["m2_authority"]
    assert old["m2_authority_sha256"] == old["prefix_authority"]["m2_authority_sha256"]
    assert old["prefix_authority"]["maximum_model_calls"] == 11

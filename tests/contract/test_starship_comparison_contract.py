"""Authority and observation boundaries of the frozen comparison campaign."""

from copy import deepcopy
import json

import pytest

from src.runtime.starship_replanning import (
    COMPARISON_CASES,
    COMPARISON_CONFIG,
    ROOT,
    SCENARIOS,
    contract,
    notices,
    resources,
)


def test_new_cases_require_explicit_authority_without_expanding_legacy_cases():
    for case in COMPARISON_CASES:
        assert SCENARIOS[f"sixdof_m1_{case}"] == case
        e = contract("fixture", case)
        assert e["event_supervision"]["scope"] == "post_deployment_orbital_coast_only"
        assert e["comparison_preparation"]["initial_candidates"] == ["nominal"]
        assert e["maximum_forecasts"] == 20
        assert e["maximum_workers"] == 2
        assert not e["physical_execution_authorized"]
    for case in (
        None,
        "normal",
        "recovery_update",
        "decision_timeout",
        "event_normal",
        "event_tradeoff",
        "event_timeout",
    ):
        e = contract("fixture", case)
        assert "comparison_preparation" not in e
        assert e["operations"]["waiting"]["initial_energy_kwh"] == 30.0
        assert e["operations"]["waiting"]["power_kw"] == 4.0


def test_future_campaign_schedule_is_not_part_of_approved_policy_material():
    e = contract("fixture", "comparison_update")
    serialized = json.dumps(e)
    for private_field in (
        "evaluation_conditions",
        "notice_update_at_s",
        "model_mode",
        "comparison_update",
        "revised_east_delay_after_contact_s",
    ):
        assert private_field not in serialized
    before = notices("comparison_update", 1349.9)
    after = notices("comparison_update", 1350.0)
    assert before["sequence"] == 1 and after["sequence"] == 2
    assert before["recovery_service_forecast"]["east_delay_after_contact_s"] == [450.0, 1050.0]
    assert after["recovery_service_forecast"]["east_delay_after_contact_s"] == [6600.0, 9900.0]
    assert before["areas"] == after["areas"]
    assert "6600" not in json.dumps(before) and "comparison_update" not in json.dumps(after)


def test_normal_notice_only_updates_publication_metadata():
    before, after = notices("comparison_normal", 1349.9), notices("comparison_normal", 1350.0)
    assert before["sequence"] != after["sequence"]
    for key in ("sequence", "issued_at_s"):
        before.pop(key)
        after.pop(key)
    assert before == after


def test_waiting_costs_leave_next_opportunity_admissible_but_near_energy_floor():
    e = contract("fixture", "comparison_update")
    scheduled = 2280.7
    nominal = resources(e, scheduled, scheduled)
    delayed = resources(e, scheduled + 5451.0, scheduled)
    assert nominal["energy_remaining_kwh"] == 22.0
    floor = e["operations"]["waiting"]["minimum_energy_kwh"]
    assert 0.0 < delayed["energy_remaining_kwh"] - floor < 1.0
    assert delayed["temperature_upper_k"] < e["operations"]["waiting"]["maximum_temperature_k"]
    assert delayed["propellant_loss_upper_kg"] > nominal["propellant_loss_upper_kg"]
    assert delayed["elapsed_wait_s"] == 5451.0


def test_test_failure_is_explicit_fixture_authority_and_not_delivered_notice():
    with pytest.raises(ValueError, match="comparison_tool_failure_fixture_only"):
        contract("live", "comparison_tool_failure")
    e = contract("fixture", "comparison_tool_failure")
    assert e["comparison_preparation"]["synthetic_tool_failure"] == "cancel_on_material_update"
    assert "synthetic_tool_failure" not in json.dumps(notices("comparison_tool_failure", 1400.0))
    assert (
        "synthetic_tool_failure"
        not in contract("live", "comparison_update")["comparison_preparation"]
    )


def test_frozen_conditions_and_authority_are_defensively_copied():
    cfg = json.loads((ROOT / COMPARISON_CONFIG).read_text())
    assert cfg["frozen_before_evaluation"]
    assert set(cfg["evaluation_conditions"]) == COMPARISON_CASES
    assert cfg["evaluation_conditions"]["comparison_late"]["notice_update_at_s"] == 2250.0
    assert cfg["evaluation_conditions"]["comparison_timeout"][
        "persistent_model_timeout_after_update"
    ]
    first = contract("fixture", "comparison_update")
    expected = deepcopy(first)
    first["comparison_preparation"]["update_candidates"].clear()
    first["operations"]["waiting"]["initial_energy_kwh"] = 0
    assert contract("fixture", "comparison_update") == expected
    n = notices("comparison_update", 1400.0)
    n["recovery_service_forecast"]["west_delay_after_contact_s"].clear()
    assert notices("comparison_update", 1400.0)["recovery_service_forecast"][
        "west_delay_after_contact_s"
    ] == [900.0, 2100.0]


def test_comparison_and_reasoning_share_existing_finite_limits():
    e = contract("live", "comparison_update")
    assert e["maximum_jev_calls"] == 16
    assert e["maximum_llm_calls"] == 2
    assert e["decision_timeout_s"] == 35.0
    assert e["batch_timeout_s"] == 600.0
    assert e["event_supervision"]["deadline_margin_s"] == 60.0
    prep = e["comparison_preparation"]
    assert prep["minimum_start_budget_s"] == 180.0
    assert prep["deep_reasoning"]["maximum_output_tokens"] == 4096
    assert prep["deep_reasoning"]["thinking"] == "enabled"
    assert prep["deep_reasoning"]["model"] == "deepseek-flash"
    assert prep["deep_reasoning"]["reasoning_effort"] == "high"
    assert prep["decision_quality_audit"] == "observed_interval_pareto_v1"

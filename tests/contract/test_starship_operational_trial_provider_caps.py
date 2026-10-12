"""Provider caps for registered operational trials; no model or flight calls."""

from copy import deepcopy

import pytest

from src.runtime.starship_m2_attempts import provider_timeout_limit
from src.runtime.starship_m2_contract import m2_contract
from src.runtime.starship_operational_trials import TRIALS
from src.runtime.starship_orbital_window import decision_window
from src.runtime.starship_replanning import notices


def comparison_request(authority):
    request = {
        "request_id": "test-return-1", "time_s": 2000.0,
        "notice": notices(authority["return_case"], 2000.0),
        "selected": "nominal", "comparison_context": {"ready": True},
        "allowed_actions": ["keep_plan", "select_nominal", "select_next_orbit"],
        "candidates": {
            name: {"return_time_s": when, "constraint_rejections": [],
                   "comparison_material": {"waiting_at_burn": {"energy_remaining_kwh": energy}}}
            for name, when, energy in (("nominal", 2280.0, 22.0), ("next_orbit", 7730.0, 12.9))
        },
    }
    request["planning_window"] = decision_window(authority["return_authority"], request)
    request["deadline_s"] = request["time_s"] + request["planning_window"]["duration_s"]
    return request


@pytest.mark.parametrize("case", ["orbital_update", *TRIALS])
def test_registered_trial_keeps_existing_independent_phase_provider_caps(case):
    authority = m2_contract("live", case)
    request = comparison_request(authority)
    assert request["planning_window"]["admitted"] is True
    assert provider_timeout_limit(authority, "return", "deepseek", request) == 120.0
    assert provider_timeout_limit(authority, "return", "typesafe", request) == 30.0
    for phase in ("deployment", "ascent", "booster"):
        assert provider_timeout_limit(authority, phase, "deepseek", request) == 30.0
    assert authority["maximum_llm_calls"] is None


@pytest.mark.parametrize("case", TRIALS)
@pytest.mark.parametrize("fault", ["expanded_cap", "foreign_binding", "different_case"])
def test_trial_requires_exact_canonical_per_case_authority(case, fault):
    original = m2_contract("live", case)
    request = comparison_request(original)
    authority = deepcopy(original)
    if fault == "expanded_cap":
        authority["return_authority"]["orbital_planning_window"]["provider_timeout_s"] = 121.0
    elif fault == "foreign_binding":
        authority["return_authority"]["operational_trial"]["manifest_sha256"] = "0" * 64
    else:
        authority["case"] = "op_trial_02" if case != "op_trial_02" else "op_trial_03"
    with pytest.raises(ValueError, match="m2_orbital_window_authority"):
        provider_timeout_limit(authority, "return", "deepseek", request)


@pytest.mark.parametrize("case", TRIALS)
def test_trial_without_deadline_headroom_cannot_claim_long_provider_window(case):
    authority = m2_contract("live", case)
    request = comparison_request(authority)
    request["candidates"]["nominal"]["return_time_s"] = 2100.0
    request["planning_window"] = decision_window(authority["return_authority"], request)
    request["deadline_s"] = request["time_s"] + request["planning_window"]["duration_s"]
    assert request["planning_window"]["admitted"] is False
    assert provider_timeout_limit(authority, "return", "deepseek", request) == 30.0
    request["planning_window"].update(admitted=True, provider_timeout_s=120.0, duration_s=150.0)
    request["deadline_s"] = request["time_s"] + 150.0
    with pytest.raises(ValueError, match="m2_orbital_window_request"):
        provider_timeout_limit(authority, "return", "deepseek", request)


def test_old_normal_authority_remains_at_legacy_cap():
    authority = m2_contract("live", "normal")
    request = comparison_request(authority)
    request["planning_window"].update(admitted=True, provider_timeout_s=120.0, duration_s=150.0)
    assert provider_timeout_limit(authority, "return", "deepseek", request) == 30.0

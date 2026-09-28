import copy
import pytest
from src.runtime.yokohama_mission_response import evaluate_fixture, select_fixture_response


def report():
    return dict(
        request_id="wind-1",
        phase="sea_inbound",
        region="sea",
        observed_sim_s=10,
        now_sim_s=11,
        battery_fraction=0.7,
        calm_duration_sim_s=0,
        hazard_active=True,
        local_fallback_policy_ref="approved-route-contingency-v1",
        refuges=[
            dict(
                id="shore-A",
                kind="shore",
                eta_s=50,
                feasibility_age_s=1,
                preapproved=True,
                route_clear=True,
                wind_reachable=True,
                arrival_area_available=True,
                estimated_arrival_battery_fraction=0.5,
            )
        ],
    )


def test_sea_requests_refuge_instead_of_hover_and_shared_agent_has_no_authority():
    result = evaluate_fixture(report())
    p = result["proposal"]
    assert p["parameters"] == dict(action="divert_to_preapproved_refuge", refuge_id="shore-A")
    assert p["proposed_response_kind"] == "replan"
    assert p["judgment_status"] == "proposal_guardrail_passed"
    assert not any(
        p[k]
        for k in (
            "model_inference_invoked",
            "operator_approved",
            "dispatch_request_sent",
            "physical_execution_invoked",
        )
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("route_clear", False),
        ("wind_reachable", False),
        ("arrival_area_available", False),
        ("estimated_arrival_battery_fraction", 0.1),
        ("feasibility_age_s", 3),
        ("preapproved", False),
    ],
)
def test_sea_never_assumes_land_is_reachable(field, value):
    r = report()
    r["refuges"][0][field] = value
    assert select_fixture_response(r)[0] == "operator_escalation"


def test_ship_can_be_better_refuge_than_land():
    r = report()
    ship = copy.deepcopy(r["refuges"][0])
    ship.update(id="ship", kind="ship", eta_s=20)
    r["refuges"].append(ship)
    assert select_fixture_response(r)[1]["refuge_id"] == "ship"


def test_land_holds_only_at_verified_site():
    r = report()
    r.update(region="land", safe_hold_verified=True)
    assert select_fixture_response(r) == ("hold", dict(action="hold_at_verified_site"))
    r["safe_hold_verified"] = False
    assert select_fixture_response(r)[0] == "replan"


def test_wind_reduction_alone_never_resumes_mission():
    r = report()
    r.update(
        hazard_active=False,
        calm_duration_sim_s=12,
        stable_vehicle_observed=True,
        fresh_route_revalidated=True,
        mission_deadline_valid=True,
    )
    assert select_fixture_response(r)[0] == "continue"
    for key in ("stable_vehicle_observed", "fresh_route_revalidated", "mission_deadline_valid"):
        bad = copy.deepcopy(r)
        bad[key] = False
        assert select_fixture_response(bad)[0] == "operator_escalation"
    r["calm_duration_sim_s"] = 9
    assert select_fixture_response(r)[0] == "operator_escalation"


def test_stale_request_or_missing_local_fallback_rejected():
    r = report()
    r["now_sim_s"] = 13
    with pytest.raises(ValueError):
        select_fixture_response(r)
    r = report()
    r.pop("local_fallback_policy_ref")
    with pytest.raises(ValueError):
        select_fixture_response(r)

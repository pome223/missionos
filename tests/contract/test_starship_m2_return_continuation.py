"""State and authority boundaries for deployment-to-return continuation."""

from copy import deepcopy
import pytest
from src.runtime.starship_replanning_verifier import admit
from src.runtime.starship_return_time_search import suggest_time
from test_starship_replanning import candidate


def bound_candidate():
    c, e, o, n = candidate()
    b = {
        "handoff_sha256": "a" * 64,
        "authority_sha256": "b" * 64,
        "profile_sha256": "c" * 64,
        "generation": 4,
        "released_count": 1,
        "retained_count": 25,
    }
    c["deployment_binding"] = deepcopy(b)
    o["retained_count"] = 25
    for t in c["trials"]:
        t["origin"] = {"retained_count": 25, "state": o["state"]}
    return c, e, o, n, b


def test_expected_inventory_and_generation_are_required():
    c, e, o, n, b = bound_candidate()
    assert admit(c, e, o, n, 2280.0, expected_deployment_binding=b)["accepted"]
    assert "unexpected_deployment_binding" in admit(c, e, o, n, 2280.0)["reasons"]


@pytest.mark.parametrize(
    "fault",
    [
        "candidate_generation",
        "retained_observation",
        "one_trial_inventory",
        "boolean_inventory",
        "authority",
    ],
)
def test_return_forecast_for_another_deployment_never_dispatches(fault):
    c, e, o, n, b = bound_candidate()
    if fault == "candidate_generation":
        c["deployment_binding"]["generation"] = 3
    if fault == "retained_observation":
        o["retained_count"] = 24
    if fault == "one_trial_inventory":
        c["trials"][3]["origin"]["retained_count"] = 0
    if fault == "boolean_inventory":
        o["retained_count"] = True
    if fault == "authority":
        c["deployment_binding"]["authority_sha256"] = "d" * 64
    assert (
        "deployment_inventory_or_generation_binding"
        in admit(c, e, o, n, 2280.0, expected_deployment_binding=b)["reasons"]
    )


def test_search_suggests_once_without_admitting_or_moving_area(monkeypatch):
    from src.runtime import starship_return_time_search as module

    rows = [
        {"return_time_s": 8000.0 + off, "integration_scale": 1.0, "position": off}
        for off in (-120.0, 0.0, 120.0)
    ]
    area = {"approved": {"radius_m": 125000.0, "position": -40.0}}
    before = deepcopy(area)
    monkeypatch.setattr(module, "metrics", lambda t: t)
    monkeypatch.setattr(
        module, "site_distance", lambda t, a: abs(t["position"] - a["position"]) * 1000
    )
    r = suggest_time(rows, 8000.0, area)
    assert r["return_time_s"] == 7960.0
    assert r["screen_target_area"] == "approved"
    assert r["method"] == "quadratic_distance_screen_not_admission"
    assert area == before
    rows[2]["return_time_s"] += 1
    with pytest.raises(ValueError, match="invalid_return_search_grid"):
        suggest_time(rows, 8000.0, area)


def test_no_contact_has_no_time_proposal():
    assert suggest_time([], 8000.0, {"a": {"radius_m": 1}}) is None

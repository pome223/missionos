"""Case gates bind live reaffirmation to actual stored forecasts and dispatch.

Synthetic scalar records exercise the verification boundary, not flight physics
or model quality. No simulator or inference is invoked by these tests.
"""

from copy import deepcopy
import math

import pytest

from src.runtime.starship_replanning import contract
from src.runtime.starship_replanning_verifier import (
    admit,
    case_reasons,
    checksum,
    event_tradeoff_witnesses,
    metrics,
)


@pytest.fixture
def reaffirmation():
    envelope = contract("live", case="event_tradeoff")
    envelope["operations"]["areas"] = {
        "east": {"latitude_deg": 0.0, "longitude_deg": 0.0, "radius_m": 125000.0}
    }
    state = {
        "time_s": 1450.25,
        "r_eci_m": [6678137.0, 0.0, 0.0],
        "v_eci_mps": [0.0, 7700.0, 0.0],
        "q_body_to_eci": [1.0, 0.0, 0.0, 0.0],
        "omega_body_rad_s": [0.0, 0.0, 0.0],
        "propellant_kg": 100000.0,
        "engine_states": [{"available": True}],
    }
    observation = {"state": state}
    angle = 7.292115e-5 * 10000.0
    cs, sn = math.cos(angle), math.sin(angle)
    contact = {
        "event_time_s": 10000.0,
        "surface_relative_speed_mps": 4.0,
        "q_body_to_eci": [
            math.sqrt(0.5),
            -sn * math.sqrt(0.5),
            cs * math.sqrt(0.5),
            0.0,
        ],
        "surface_normal_eci": [cs, sn, 0.0],
        "point_eci_m": [6378137.0 * cs, 6378137.0 * sn, 0.0],
        "propellant_kg": 60000.0,
    }
    origin = {"state": {**deepcopy(state), "time_s": 1000.0}}
    trial = {
        "origin": origin,
        "outcome": {"contact_receipt": contact},
        "final_state": {"omega_body_rad_s": [0.0, 0.0, 0.0]},
        "coast": [{**deepcopy(state), "time_s": t} for t in (1450.0, 1451.0)],
    }
    candidate = {
        "return_time_s": 2280.0,
        "scheduled_s": 2280.0,
        "envelope_sha256": checksum(envelope),
        "notice_sequence": 1,
        "signs": [0, -1, 1],
        "trials": [
            {**deepcopy(trial), "integration_scale": s} for s in (1.0, 1.0, 1.0, 0.5)
        ],
    }
    candidates = {
        "nominal": candidate,
        "next_orbit": {**deepcopy(candidate), "return_time_s": 7732.0},
    }
    notice = {
        "sequence": 2,
        "issued_at_s": 1450.0,
        "expires_at_s": 12000.0,
        "areas": {"east": [9000.0, 11000.0]},
    }
    public = {}
    for name, c in candidates.items():
        check = admit(c, envelope, observation, notice, 2280.0)
        assert check["accepted"]
        public[name] = {
            "candidate_sha256": checksum(c),
            "return_time_s": c["return_time_s"],
            "constraint_rejections": [],
            "predicted_contacts": [metrics(t) for t in c["trials"]],
            "areas": ["east"],
        }
    request = {
        "request_id": "recorded-live-reaffirmation",
        "time_s": 1450.0,
        "deadline_s": 1490.0,
        "selected": "nominal",
        "revision": 1,
        "notice": notice,
        "allowed_actions": ["select_nominal", "select_next_orbit", "keep_plan"],
        "candidates": public,
    }
    record = {
        "request": request,
        "response": {
            "request_id": request["request_id"],
            "request_sha256": checksum(request),
            "action": "keep_plan",
            "mode": "live",
            "model_inference_invoked": True,
        },
        "accepted_action": "keep_plan",
        "later_time_s": 1450.25,
        "later_observation": observation,
        "fallback": False,
        "interrupted_by_events": False,
    }
    revision = {
        "revision": 1,
        "time_s": 1200.0,
        "candidate_id": "nominal",
        "candidate_sha256": checksum(candidate),
        "candidate_snapshot": deepcopy(candidate),
        "basis": "ai",
        "decision_request_id": "old-booking-at-sequence-one",
    }
    return {
        "envelope": envelope,
        "scheduled_s": 2280.0,
        "candidates": candidates,
        "decisions": [record],
        "plan_revisions": [revision],
        "dispatch": {"revision": 1, "candidate_id": "nominal", "time_s": 2280.0},
    }


def rebind(study):
    r = study["decisions"][0]
    r["response"]["request_sha256"] = checksum(r["request"])


@pytest.mark.parametrize("action", ["keep_plan", "select_nominal"])
def test_live_reaffirmation_of_same_booking_needs_no_new_revision(
    reaffirmation, action
):
    r = reaffirmation["decisions"][0]
    r["accepted_action"] = r["response"]["action"] = action
    assert (
        case_reasons(reaffirmation, "event_tradeoff", "contact_and_recovery_area_met")
        == []
    )
    witness = event_tradeoff_witnesses(
        reaffirmation, reaffirmation["plan_revisions"][0]
    )[0]
    assert witness["existing_booking_reaffirmed"] is True
    assert witness["both_options_admissible"] is True
    assert witness["request_navigation_observation_rechecked"] is False


def test_live_new_revision_also_qualifies(reaffirmation):
    r = reaffirmation["decisions"][0]
    r["request"]["selected"] = None
    r["request"]["revision"] = 0
    r["accepted_action"] = r["response"]["action"] = "select_nominal"
    v = reaffirmation["plan_revisions"][0]
    v["time_s"] = r["later_time_s"]
    v["decision_request_id"] = r["request"]["request_id"]
    rebind(reaffirmation)
    assert (
        case_reasons(reaffirmation, "event_tradeoff", "contact_and_recovery_area_met")
        == []
    )


@pytest.mark.parametrize("missing", ["public", "stored", "hash", "permitted"])
def test_missing_second_forecast_remains_a_real_case_failure(reaffirmation, missing):
    request = reaffirmation["decisions"][0]["request"]
    if missing == "public":
        del request["candidates"]["next_orbit"]
    elif missing == "stored":
        del reaffirmation["candidates"]["next_orbit"]
    elif missing == "hash":
        request["candidates"]["next_orbit"]["candidate_sha256"] = "f" * 64
    else:
        request["allowed_actions"].remove("select_next_orbit")
    rebind(reaffirmation)
    assert case_reasons(
        reaffirmation, "event_tradeoff", "contact_and_recovery_area_met"
    ) == ["two_admissible_options_after_correction_not_demonstrated"]


@pytest.mark.parametrize(
    "tamper",
    [
        "stale",
        "fallback",
        "fixture",
        "action",
        "revision",
        "dispatch",
        "late",
        "old_notice",
        "response_hash",
        "selected_hash",
        "unobserved_state",
        "revoked_area",
        "wrong_public_contact",
    ],
)
def test_reaffirmation_must_actually_control_the_current_dispatched_plan(
    reaffirmation, tamper
):
    r = reaffirmation["decisions"][0]
    if tamper == "stale":
        r["interrupted_by_events"] = True
    elif tamper == "fallback":
        r["fallback"] = True
    elif tamper == "fixture":
        r["response"]["model_inference_invoked"] = False
    elif tamper == "action":
        r["response"]["action"] = "refresh_status"
    elif tamper == "revision":
        r["request"]["revision"] = 0
        rebind(reaffirmation)
    elif tamper == "dispatch":
        reaffirmation["dispatch"]["candidate_id"] = "next_orbit"
    elif tamper == "late":
        reaffirmation["dispatch"]["time_s"] = 1449.0
    elif tamper == "old_notice":
        r["request"]["notice"]["sequence"] = 1
        rebind(reaffirmation)
    elif tamper == "response_hash":
        r["response"]["request_sha256"] = "f" * 64
    elif tamper == "selected_hash":
        r["request"]["candidates"]["nominal"]["candidate_sha256"] = "f" * 64
        rebind(reaffirmation)
    elif tamper == "unobserved_state":
        r["later_observation"]["state"]["r_eci_m"][0] += 1000.0
    elif tamper == "revoked_area":
        r["request"]["notice"]["areas"] = {}
        rebind(reaffirmation)
    else:
        r["request"]["candidates"]["nominal"]["predicted_contacts"][0]["speed_mps"] = (
            1.0
        )
        rebind(reaffirmation)
    assert "actual_ai_replanning_not_executed" in case_reasons(
        reaffirmation, "event_tradeoff", "contact_and_recovery_area_met"
    )


def test_self_reported_two_options_do_not_replace_independent_admission(reaffirmation):
    candidate = reaffirmation["candidates"]["next_orbit"]
    candidate["trials"][0]["coast"][0]["r_eci_m"][0] += 1000.0
    public = reaffirmation["decisions"][0]["request"]["candidates"]["next_orbit"]
    public["candidate_sha256"] = checksum(candidate)
    assert public["constraint_rejections"] == []
    rebind(reaffirmation)
    assert case_reasons(
        reaffirmation, "event_tradeoff", "contact_and_recovery_area_met"
    ) == ["two_admissible_options_after_correction_not_demonstrated"]


def test_either_admissible_candidate_can_be_the_reaffirmed_booking(reaffirmation):
    r = reaffirmation["decisions"][0]
    r["request"]["selected"] = "next_orbit"
    candidate = reaffirmation["candidates"]["next_orbit"]
    v = reaffirmation["plan_revisions"][0]
    v["candidate_id"] = "next_orbit"
    v["candidate_sha256"] = checksum(candidate)
    v["candidate_snapshot"] = deepcopy(candidate)
    reaffirmation["dispatch"]["candidate_id"] = "next_orbit"
    rebind(reaffirmation)
    assert (
        case_reasons(reaffirmation, "event_tradeoff", "contact_and_recovery_area_met")
        == []
    )


def test_older_second_candidate_is_resolved_from_its_recorded_snapshot(reaffirmation):
    old = deepcopy(reaffirmation["candidates"]["next_orbit"])
    reaffirmation["plan_revisions"].insert(
        0,
        {
            "candidate_id": "next_orbit",
            "candidate_snapshot": old,
            "revision": 0,
            "time_s": 1100.0,
        },
    )
    reaffirmation["candidates"]["next_orbit"]["notice_sequence"] = 2
    assert (
        case_reasons(reaffirmation, "event_tradeoff", "contact_and_recovery_area_met")
        == []
    )

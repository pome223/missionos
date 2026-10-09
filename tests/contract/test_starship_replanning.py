"""M1 authority/freshness/uncertainty and observation isolation regressions."""

from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from src.runtime.starship_replanning import contract, digest, estimate, uncertainty_origins, notices
from src.runtime.starship_replanning_verifier import admit, metrics, site_distance, wait_reasons
from src.runtime.starship_return_prediction import validate_origin, opportunity_window
from src.runtime import starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import vehicle


def example():
    p = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    craft = vehicle(p, payload_count=0)
    s = dyn.State6DOF(
        1000.0,
        (6678137.25, 0.0, 0.0),
        (0.0, 7725.0023, 0.0),
        (0.5, -0.5, 0.5, 0.5),
        (0.0, 0.0, 0.0),
        100002.3,
        tuple(dyn.EngineState() for _ in craft.engines),
        tuple(0.0 for _ in craft.aero_panels),
    )
    return p, s


def candidate():
    p, s = example()
    e = contract("fixture")
    e["operations"]["areas"] = {
        "east": {"latitude_deg": 0.0, "longitude_deg": 0.0, "radius_m": 125000.0}
    }
    observation = estimate(s)
    center = observation["state"]
    angle = 7.292115e-5 * 5000.0
    cs, sn = math.cos(angle), math.sin(angle)
    q = [math.sqrt(0.5), -sn * math.sqrt(0.5), cs * math.sqrt(0.5), 0.0]
    receipt = {
        "event_time_s": 5000.0,
        "surface_relative_speed_mps": 4.0,
        "q_body_to_eci": q,
        "surface_normal_eci": [cs, sn, 0.0],
        "point_eci_m": [6378137.0 * cs, 6378137.0 * sn, 0.0],
        "propellant_kg": 60000.0,
    }
    trial = {
        "final_state": {"omega_body_rad_s": [0.0, 0.0, 0.0]},
        "outcome": {"contact_receipt": receipt},
        "coast": [
            {
                k: center[k]
                for k in (
                    "time_s",
                    "r_eci_m",
                    "v_eci_mps",
                    "q_body_to_eci",
                    "omega_body_rad_s",
                    "propellant_kg",
                )
            }
        ],
    }
    c = {
        "return_time_s": 2280.0,
        "scheduled_s": 2280.0,
        "envelope_sha256": digest(e),
        "notice_sequence": 1,
        "signs": [0, -1, 1],
        "trials": [
            {**deepcopy(trial), "integration_scale": scale} for scale in (1.0, 1.0, 1.0, 0.5)
        ],
    }
    notice = {
        "sequence": 1,
        "issued_at_s": 900.0,
        "expires_at_s": 12000.0,
        "areas": {"east": [4500.0, 5600.0]},
    }
    return c, e, observation, notice


def test_independent_rotating_geometry_and_contact():
    c, e, o, n = candidate()
    m = metrics(c["trials"][0])
    assert abs(m["longitude_deg"]) < 1e-9 and abs(m["latitude_deg"]) < 1e-9
    assert m["tilt_deg"] < 1e-5
    assert site_distance(m, e["operations"]["areas"]["east"]) < 0.001
    assert admit(c, e, o, n, 2280.0)["accepted"]


@pytest.mark.parametrize(
    "fault",
    [
        "fuel",
        "speed",
        "tilt",
        "area",
        "time",
        "expiry",
        "state",
        "attitude",
        "authority",
        "uncertainty",
        "deadline",
        "energy",
        "heat",
    ],
)
def test_progress_rejected_even_if_a_model_chooses_it(fault):
    c, e, o, n = candidate()
    if fault == "fuel":
        c["trials"][2]["outcome"]["contact_receipt"]["propellant_kg"] = 27000.0
    if fault == "speed":
        c["trials"][1]["outcome"]["contact_receipt"]["surface_relative_speed_mps"] = 5.01
    if fault == "tilt":
        c["trials"][0]["outcome"]["contact_receipt"]["q_body_to_eci"] = [1.0, 0.0, 0.0, 0.0]
    if fault == "area":
        n["areas"] = {}
    if fault == "time":
        n["areas"]["east"] = [5001.0, 5600.0]
    if fault == "expiry":
        n["expires_at_s"] = 999.0
    if fault == "state":
        o["state"]["r_eci_m"][0] += 100.0
    if fault == "attitude":
        o["state"]["q_body_to_eci"] = [1.0, 0.0, 0.0, 0.0]
    if fault == "authority":
        c["envelope_sha256"] = "bad"
    if fault == "uncertainty":
        c["trials"].pop()
    if fault == "deadline":
        c["return_time_s"] = 9000.0
    if fault in ("energy", "heat"):
        c["return_time_s"] = 8000.0
        e["operations"]["waiting"]["power_kw" if fault == "energy" else "warming_k_per_s"] = 100.0
        c["envelope_sha256"] = digest(e)
    assert not admit(c, e, o, n, 2280.0)["accepted"]


def test_no_rolling_extension_or_infinite_wait():
    e = contract("fixture")
    assert wait_reasons(e, 8281.0, 2280.0) == ["original_deadline"]
    assert not wait_reasons(e, 8000.0, 2280.0)


def test_observation_is_quantized_and_has_no_scenario_future_or_mass():
    p, s = example()
    o = estimate(s)
    assert o["state"]["propellant_kg"] != s.propellant_kg
    assert o["state"]["r_eci_m"][0] != s.r_eci_m[0]
    text = json.dumps(o)
    assert all(x not in text for x in ("scenario", "mass_kg", "future", "inertia", "notice"))
    variants = uncertainty_origins(o)
    assert o == variants[0] and len(variants) == 3
    assert variants[1]["state"]["propellant_kg"] == o["state"]["propellant_kg"] - 100.0
    assert variants[2]["state"]["propellant_kg"] == o["state"]["propellant_kg"] + 100.0
    validate_origin(o, p, 7800.0, 10000.0, window=opportunity_window(2280.0))
    with pytest.raises(ValueError):
        validate_origin(o, p, 8300.0, 10000.0, window=opportunity_window(2280.0))


def test_updated_status_is_not_revealed_in_earlier_observation():
    early = notices("recovery_update", 1000.0)
    late = notices("recovery_update", 4800.0)
    assert early["areas"] == {} and "10000" not in json.dumps(early)
    assert late["sequence"] > early["sequence"] and late["areas"]["west"] == [10000.0, 11000.0]


def test_m1_approval_covers_envelope_and_source_and_is_not_automatic(tmp_path, monkeypatch):
    from src.runtime.starship_mission_control import StarshipMissionService, StarshipMissionError

    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "fixture")

    def planner(_):
        return {
            "proposal": {
                "scenario": "sixdof_m1_replan",
                "rationale": "Test.",
                "uncertainties": ["Synthetic."],
            },
            "invocation": {"model_inference_invoked": False, "provider": "fixture"},
        }

    svc = StarshipMissionService(tmp_path, planner=planner)
    state = svc.plan("test", "Starship return replanning")
    plan = state["plan"]
    args = ("test", plan["id"], plan["sha256"])
    with pytest.raises(StarshipMissionError, match="explicit_plan_approval"):
        svc.execute(*args)
    approved = svc.approve(*args)
    assert approved["approval"]["mission_envelope"] == plan["mission_envelope"]
    approved = svc._load("test")
    svc._valid_grant(approved)
    approved["plan"]["mission_envelope"]["maximum_delay_s"] += 1
    with pytest.raises(StarshipMissionError):
        svc._valid_grant(approved)


def test_worker_environment_excludes_model_and_cloud_keys(monkeypatch):
    from src.runtime.starship_mission_control import worker_environment

    for key in (
        "TYPESAFE_API_KEY",
        "DEEPSEEK_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "JEV_API_KEY",
    ):
        monkeypatch.setenv(key, "never-send-to-worker")
    assert "never-send-to-worker" not in json.dumps(worker_environment())


def test_forecast_process_boundary_returns_persisted_json_types(tmp_path, monkeypatch):
    import numpy as np
    from src.runtime import starship_replanning_executor as actor

    payload = {
        "final_state": {"omega_body_rad_s": (0.0, 0.0, 0.0)},
        "outcome": {"contact_receipt": {"propellant_kg": np.float64(60000.0)}},
        "samples": [],
        "wall_time_s": np.float64(0.1),
    }
    monkeypatch.setattr(actor, "forecast", lambda *a, **k: payload)
    _, s = example()
    result = actor.prediction_job(
        (
            estimate(s),
            {"integration": {"powered_dt_s": 0.1, "coast_dt_s": 2.0}},
            2280.0,
            2280.0,
            str(tmp_path / "forecast.json"),
            1.0,
        )
    )
    assert type(result["outcome"]["contact_receipt"]["propellant_kg"]) is float
    assert type(result["wall_time_s"]) is float
    assert isinstance(result["final_state"]["omega_body_rad_s"], list)


def test_service_loss_persists_for_later_decisions(monkeypatch):
    from types import SimpleNamespace
    from src.runtime.starship_replanning_executor import ReplanningExecutor

    actor = ReplanningExecutor.__new__(ReplanningExecutor)
    actor.case = "decision_timeout"
    actor.service_failed = False
    actor.envelope = contract("fixture")
    actor.s = SimpleNamespace(time_s=5000.0)
    actor.scheduled = 2280.0
    actor.decisions = []
    actor.candidates = {}
    actor.revision = 0
    actor.selected = None
    actor.mailbox = None
    actor.run_id = "outage-test"
    actor.p = {"integration": {"coast_dt_s": 2.0}}
    actor.checks = lambda notice: {}
    actor.observation = lambda: {"state": {"time_s": actor.s.time_s}}
    actor.event = lambda *args, **kwargs: None
    actor.wait = lambda label, poll, limit: poll()
    assert actor.choose("updated_recovery_status", ["evaluate_returns"]) is None
    assert actor.choose("updated_return_options", ["select_next_orbit"]) is None
    assert actor.choose("selected_plan_monitoring", ["keep_plan"]) is None
    assert all(r["response"] is None and r["synthetic_timeout_injected"] for r in actor.decisions)


def test_withholding_selected_plan_reenters_planning(monkeypatch):
    from types import SimpleNamespace
    from src.runtime import starship_replanning_executor as module

    actor = module.ReplanningExecutor.__new__(module.ReplanningExecutor)
    actor.envelope = contract("fixture")
    actor.envelope["monitor_interval_s"] = 50.0
    actor.s = SimpleNamespace(time_s=0.0)
    actor.scheduled = 100.0
    actor.times = {"nominal": 100.0, "next_orbit": 200.0}
    actor.case = "normal"
    actor.sources = {}
    actor.revision = 0
    actor.candidates = {"nominal": {}, "next_orbit": {}}
    actor.controller = SimpleNamespace(return_time_s=1e9)
    actor.contact = None
    actor.dispatch = None
    calls = []

    def selection():
        name = "nominal" if not calls else "next_orbit"
        calls.append(name)
        actor.selected = name
        actor.revision += 1
        return True

    choices = iter(["evaluate_returns", "wait_for_update", "keep_plan", "keep_plan", "keep_plan"])
    actor.choose = lambda *args: next(choices)
    actor.calculate = lambda names: True
    actor.plan_return = selection
    actor.coast_to = lambda t: setattr(actor.s, "time_s", t)
    actor.refresh_ground = lambda: setattr(actor.s, "time_s", actor.s.time_s + 1.0)
    actor.event = lambda *args, **kwargs: None
    actor.observation = lambda: {"state": {"time_s": actor.s.time_s}}
    actor.step = lambda: setattr(actor, "contact", True)
    actor.finish = lambda: actor.dispatch
    monkeypatch.setattr(module, "source_hashes", lambda: {})
    monkeypatch.setattr(module, "admit", lambda *a: {"accepted": True})
    result = actor.run()
    assert calls == ["nominal", "next_orbit"]
    assert result["candidate_id"] == "next_orbit" and result["time_s"] == 200.0


def completion_record():
    return {
        "dispatch": {"candidate_id": "next_orbit", "revision": 2},
        "plan_revisions": [
            {
                "revision": 1,
                "candidate_id": "next_orbit",
                "basis": "ai",
                "decision_request_id": "first",
            },
            {
                "revision": 2,
                "candidate_id": "next_orbit",
                "basis": "checked_fallback",
                "decision_request_id": "second",
            },
        ],
        "decisions": [
            {
                "request": {"request_id": "first", "stage": "return_options"},
                "accepted_action": "select_next_orbit",
                "response": {"model_inference_invoked": True},
                "synthetic_timeout_injected": False,
                "request_disposition": "submitted",
            },
            {
                "request": {"request_id": "second", "stage": "updated_recovery_status"},
                "accepted_action": None,
                "response": None,
                "synthetic_timeout_injected": True,
                "request_disposition": "synthetic_outage",
            },
        ],
    }


def test_abandoned_ai_selection_does_not_credit_executed_fallback():
    from src.runtime.starship_replanning_verifier import case_reasons

    s = completion_record()
    assert case_reasons(s, "recovery_update", "contact_and_recovery_area_met") == [
        "actual_ai_replanning_not_executed"
    ]
    s["dispatch"]["revision"] = 1
    assert case_reasons(s, "recovery_update", "contact_and_recovery_area_met") == []
    assert "contact_and_area_not_met" in case_reasons(s, "recovery_update", "unresolved")
    assert "normal_return_changed" in case_reasons(s, "normal", "contact_and_recovery_area_met")


def test_timeout_case_cannot_hide_recovered_response_by_omitting_injection_flag():
    from src.runtime.starship_replanning_verifier import case_reasons

    s = completion_record()
    assert case_reasons(s, "decision_timeout", "contact_and_recovery_area_met") == []
    later = deepcopy(s["decisions"][0])
    later["request"]["stage"] = "selected_plan_monitoring"
    later["request"]["request_id"] = "third"
    s["decisions"].append(later)
    assert case_reasons(s, "decision_timeout", "contact_and_recovery_area_met") == [
        "persistent_timeout_and_fallback_missing"
    ]


def test_budget_exhaustion_has_own_fresh_observation_and_cannot_reuse_response():
    from types import SimpleNamespace
    from src.runtime.starship_replanning_executor import ReplanningExecutor
    from src.runtime.starship_replanning_verifier import decision_budget_reasons

    actor = ReplanningExecutor.__new__(ReplanningExecutor)
    actor.case = "normal"
    actor.service_failed = False
    actor.envelope = contract("fixture")
    actor.envelope["maximum_jev_calls"] = 1
    actor.s = SimpleNamespace(time_s=1000.0)
    actor.scheduled = 2280.0
    actor.decisions = []
    actor.candidates = {}
    actor.revision = 0
    actor.selected = None
    actor.mailbox = None
    actor.run_id = "budget-test"
    actor.p = {"integration": {"coast_dt_s": 2.0}}
    actor.checks = lambda notice: {}
    actor.observation = lambda: {"state": {"time_s": actor.s.time_s}}
    actor.event = lambda *args, **kwargs: None
    actor.step = lambda: setattr(actor.s, "time_s", actor.s.time_s + 2.0)
    actor.wait = lambda label, poll, limit: (actor.step(), poll())[1]
    assert actor.choose("normal_monitoring", ["keep_plan"]) == "keep_plan"
    assert actor.choose("return_options", ["select_nominal"]) is None
    assert actor.choose("selected_plan_monitoring", ["keep_plan"]) is None
    assert len({r["request"]["request_id"] for r in actor.decisions}) == 3
    assert all(r["later_time_s"] > r["request"]["time_s"] for r in actor.decisions)
    assert all(r["response"] is None for r in actor.decisions[1:])
    assert decision_budget_reasons(actor.decisions, 1) == []
    forged = deepcopy(actor.decisions)
    forged[1]["response"] = forged[0]["response"]
    assert decision_budget_reasons(forged, 1) == ["invalid_budget_exhaustion"]
    assert decision_budget_reasons(actor.decisions[1:], 1) == ["invalid_budget_exhaustion"] * 2


def test_verified_but_unresolved_receipt_remains_readable_without_success_label(
    tmp_path, monkeypatch
):
    from hashlib import sha256
    from src.runtime.starship_mission_control import (
        StarshipMissionService,
        _write,
        StarshipMissionError,
    )

    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "fixture")

    def planner(_):
        return {
            "proposal": {
                "scenario": "sixdof_m1_replan",
                "rationale": "Test",
                "uncertainties": ["Synthetic"],
            },
            "invocation": {"model_inference_invoked": False, "provider": "fixture"},
        }

    svc = StarshipMissionService(tmp_path, planner=planner)
    projected = svc.plan("receipt-test", "Starship return replanning")
    state = svc._load("receipt-test")
    run_id = "a" * 32
    state["status"] = "running"
    state["execution"] = {"status": "running", "run_id": run_id, "worker_pid": 99}
    svc._save("receipt-test", state)
    output = tmp_path / ("run-" + run_id) / "results"
    output.mkdir(parents=True)
    artifact = output / "verification.json"
    artifact.write_text('{"passed":true,"case_accepted":false,"outcome":"unresolved"}')
    receipt = {
        "run_id": run_id,
        "plan_sha256": state["plan"]["sha256"],
        "worker_pid": 99,
        "verification_passed": True,
        "verification": json.loads(artifact.read_text()),
        "artifact_sha256": {"verification.json": sha256(artifact.read_bytes()).hexdigest()},
        "provider_credentials_present": False,
        "ended_at_epoch_s": 123.0,
    }
    receipt["signature"] = svc._signature(receipt)
    _write(output.parent / "result.json", receipt)
    result = svc.status("receipt-test")
    assert result["status"] == "unresolved"
    assert result["execution"]["verification"]["passed"] is True
    assert result["execution"]["verification"]["case_accepted"] is False
    assert (
        svc.read_artifact("receipt-test", projected["plan"]["id"], "verification.json") == artifact
    )
    artifact.write_text("{}")
    with pytest.raises(StarshipMissionError, match="saved_artifact_changed"):
        svc.read_artifact("receipt-test", projected["plan"]["id"], "verification.json")


def test_refined_grid_must_also_reach_the_unchanged_area_and_contact_limits():
    c, e, o, n = candidate()
    assert admit(c, e, o, n, 2280.0)["accepted"]
    c["trials"][3]["outcome"]["contact_receipt"]["surface_relative_speed_mps"] = 5.1
    assert not admit(c, e, o, n, 2280.0)["accepted"]
    c, e, o, n = candidate()
    c["trials"][3]["integration_scale"] = 1.0
    assert "uncertainty_or_refinement_trials_missing" in admit(c, e, o, n, 2280.0)["reasons"]


def test_m1_chat_displays_its_return_delegation_not_the_older_director_scope(tmp_path, monkeypatch):
    from src.runtime.starship_mission_control import StarshipMissionService
    from src.gateway.starship_chat import _response

    monkeypatch.setenv("MISSIONOS_STARSHIP_MISSION_DIRECTOR_MODE", "fixture")

    def planner(_):
        return {
            "proposal": {
                "scenario": "sixdof_m1_replan",
                "rationale": "Test",
                "uncertainties": ["Synthetic"],
            },
            "invocation": {"model_inference_invoked": False, "provider": "fixture"},
        }

    state = StarshipMissionService(tmp_path, planner=planner).plan("display-test", "Starship M1")
    message = _response("Starship M1", "plan", state, "display-test")["message"]
    assert "最大6000秒" in message and "計画更新最大4回" in message
    assert "帰還時刻は変更不可" not in message and "保留は合計30秒" not in message
    assert "放出・タワーキャッチの追加権限" in message


def test_event_forecast_survives_benign_notice_update_but_rechecks_current_clearance():
    c, e, o, n = candidate()
    e["event_supervision"] = {"maximum_coast_body_rate_rad_s": 0.02}
    c["envelope_sha256"] = digest(e)
    for trial in c["trials"]:
        trial["origin"] = deepcopy(o)
    n["sequence"] += 1
    assert admit(c, e, o, n, 2280.0)["accepted"] is True
    n["areas"] = {}
    result = admit(c, e, o, n, 2280.0)
    assert result["accepted"] is False
    assert "area_or_availability" in result["reasons"]
    assert "forecast_notice_binding" not in result["reasons"]


def test_non_event_m1_preserves_original_forecast_notice_binding():
    c, e, o, n = candidate()
    n["sequence"] += 1
    result = admit(c, e, o, n, 2280.0)
    assert result["accepted"] is False
    assert "forecast_notice_binding" in result["reasons"]


def deadline_fallback_example():
    c, e, o, n = candidate()
    e["event_supervision"] = {
        "maximum_coast_body_rate_rad_s": 0.02,
        "commitment_response": "preserve_currently_admissible_booking_without_model_wait",
    }
    c["id"] = "nominal"
    c["envelope_sha256"] = digest(e)
    o["state"]["time_s"] = 2250.0
    for trial in c["trials"]:
        trial["origin"] = deepcopy(o)
        trial["coast"][0]["time_s"] = o["state"]["time_s"]
    record = {
        "request": {
            "time_s": 2250.0,
            "deadline_s": 2285.0,
            "selected": "nominal",
            "revision": 1,
            "notice": n,
            "commitment_guard": {
                "candidate_id": "nominal",
                "candidate_sha256": digest(c),
                "return_time_s": 2280.0,
                "guard_horizon_s": 35.25,
            },
        },
        "request_disposition": "deadline_fallback",
        "response": None,
        "accepted_action": None,
        "fallback": True,
        "interrupted_by_events": False,
        "later_time_s": 2250.0,
        "later_observation": o,
    }
    study = {
        "profile": {"integration": {"coast_dt_s": 0.25}},
        "scheduled_s": 2280.0,
        "plan_revisions": [
            {"revision": 1, "candidate_id": "nominal", "time_s": 1200.0, "candidate_snapshot": c}
        ],
    }
    return record, study, e


def test_deadline_fallback_rechecks_booked_candidate_and_does_not_charge_model_budget():
    from src.runtime.starship_replanning_verifier import (
        deadline_fallback_reasons,
        decision_budget_reasons,
    )

    record, study, e = deadline_fallback_example()
    assert deadline_fallback_reasons(record, study, e) == []
    assert decision_budget_reasons([record] * 20, 0) == []


@pytest.mark.parametrize(
    "fault",
    [
        "scope",
        "horizon",
        "too_early",
        "late",
        "no_booking",
        "new_candidate",
        "candidate_hash",
        "notice_revoked",
        "engine_failed",
        "response",
        "accepted_action",
        "stale",
        "fallback_false",
        "elapsed",
        "unexpected_guard",
    ],
)
def test_deadline_fallback_cannot_bypass_booking_current_state_or_clearance(fault):
    from src.runtime.starship_replanning_verifier import deadline_fallback_reasons

    record, study, e = deadline_fallback_example()
    guard = record["request"]["commitment_guard"]
    if fault == "scope":
        e["event_supervision"]["commitment_response"] = "unapproved"
    elif fault == "horizon":
        guard["guard_horizon_s"] += 1
    elif fault in ("too_early", "late"):
        now = 2244.0 if fault == "too_early" else 2280.3
        record["request"]["time_s"] = record["later_time_s"] = now
        record["later_observation"]["state"]["time_s"] = now
    elif fault == "no_booking":
        study["plan_revisions"] = []
    elif fault == "new_candidate":
        guard["candidate_id"] = record["request"]["selected"] = "next_orbit"
    elif fault == "candidate_hash":
        guard["candidate_sha256"] = "a" * 64
    elif fault == "notice_revoked":
        record["request"]["notice"]["areas"] = {}
    elif fault == "engine_failed":
        record["later_observation"]["state"]["engine_states"][0]["available"] = False
    elif fault in ("response", "accepted_action"):
        record[fault] = {"action": "keep_plan"} if fault == "response" else "keep_plan"
    elif fault == "stale":
        record["interrupted_by_events"] = True
    elif fault == "fallback_false":
        record["fallback"] = False
    elif fault == "elapsed":
        record["later_time_s"] += 1
    elif fault == "unexpected_guard":
        record["request_disposition"] = "submitted"
    assert deadline_fallback_reasons(record, study, e)


def test_deadline_fallback_allows_only_one_finite_step_of_burn_overshoot():
    from src.runtime.starship_replanning_verifier import deadline_fallback_reasons

    record, study, e = deadline_fallback_example()
    record["request"]["time_s"] = record["later_time_s"] = 2280.25
    record["later_observation"]["state"]["time_s"] = 2280.25
    candidate = study["plan_revisions"][0]["candidate_snapshot"]
    for trial in candidate["trials"]:
        trial["coast"][0]["time_s"] = 2280.25
    record["request"]["commitment_guard"]["candidate_sha256"] = digest(candidate)
    assert deadline_fallback_reasons(record, study, e) == []


def preserved_outage_booking():
    record, study, envelope = deadline_fallback_example()
    record["request"].update(stage="orbital_event", request_id="outage-1")
    record["request"]["notice"]["sequence"] = 2
    record["synthetic_timeout_injected"] = True
    study["envelope"] = envelope
    study["dispatch"] = {"candidate_id": "nominal", "revision": 1, "time_s": 2280.0}
    study["plan_revisions"][0].update(basis="fixture", decision_request_id="before-outage")
    study["decisions"] = [record]
    return study


def test_outage_can_execute_existing_booking_without_inventing_another_revision():
    from src.runtime.starship_replanning_verifier import case_reasons

    study = preserved_outage_booking()
    assert len(study["plan_revisions"]) == 1
    assert study["plan_revisions"][0]["basis"] == "fixture"
    assert case_reasons(study, "event_timeout", "contact_and_recovery_area_met") == []


@pytest.mark.parametrize(
    "fault", ["response", "no_injection", "stale", "no_fallback", "other_booking", "notice_revoked"]
)
def test_preserved_outage_booking_requires_actual_checked_fallback_evidence(fault):
    from src.runtime.starship_replanning_verifier import case_reasons

    study = preserved_outage_booking()
    record = study["decisions"][0]
    if fault == "response":
        record["response"] = {"action": "keep_plan"}
    elif fault == "no_injection":
        record["synthetic_timeout_injected"] = False
    elif fault == "stale":
        record["interrupted_by_events"] = True
    elif fault == "no_fallback":
        record["fallback"] = False
    elif fault == "other_booking":
        record["request"]["selected"] = "next_orbit"
    elif fault == "notice_revoked":
        record["request"]["notice"]["areas"] = {}
    assert "persistent_timeout_and_fallback_missing" in case_reasons(
        study, "event_timeout", "contact_and_recovery_area_met"
    )

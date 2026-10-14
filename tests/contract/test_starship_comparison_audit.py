"""Comparison accounting and conservative audit; no model or physical-flight calls."""

from copy import deepcopy

import pytest

from src.runtime.starship_replanning import digest, notices
from src.runtime.starship_replanning_comparison import candidate_material
from src.runtime.starship_replanning_verifier import (
    admit,
    case_reasons,
    comparison_quality_record,
    expected_comparison_material,
    metrics,
    verify_comparison,
)
from tests.contract.test_starship_replanning_event_runtime import actor_harness, make_candidate


def comparison_study(monkeypatch, tmp_path):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1300.0, case="comparison_update")

    def calculate(names, **_kwargs):
        before = actor.s.time_s
        actor.forecast_count += 4 * len(names)
        for name in names:
            actor.candidates[name] = make_candidate(actor, name)
        actor.step()
        actor.pending.append(
            {
                "operation": "return_forecasts",
                "start_time_s": before,
                "end_time_s": actor.s.time_s,
                "wall_time_s": 1.0,
                "cutoff_time_s": _kwargs["cutoff_time_s"],
            }
        )
        actor.last_calculation_result = {"status": "completed", "reason": "predictions_available"}
        return True

    actor.calculate = calculate
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    notice = notices(actor.case, actor.s.time_s)
    observation = actor.observation()
    public = {}
    for name, candidate in actor.candidates.items():
        check = admit(candidate, actor.envelope, observation, notice, actor.scheduled)
        public[name] = {
            "candidate_sha256": digest(candidate),
            "return_time_s": candidate["return_time_s"],
            "predicted_contacts": [metrics(t) for t in candidate["trials"]],
            "constraint_rejections": check["reasons"],
            "areas": check["areas"],
            "comparison_material": candidate_material(
                candidate, actor.envelope, notice, actor.scheduled
            ),
        }
    request = {
        "request_id": "audit-1",
        "time_s": actor.s.time_s,
        "notice": notice,
        "candidates": public,
        "allowed_actions": ["select_nominal"],
        "selected": None,
        "comparison_context": deepcopy(actor.comparison_context),
    }
    record = {
        "request": request,
        "request_observation": observation,
        "response": {"action": "select_nominal", "decision_basis": None},
        "accepted_action": "select_nominal",
    }
    return actor, {
        "comparison_preparations": actor.comparison_preparations,
        "opportunities": actor.times,
        "profile": actor.p,
        "source_sha256": actor.sources,
        "scheduled_s": actor.scheduled,
        "forecast_count": actor.forecast_count,
        "pending_operations": actor.pending,
        "plan_revisions": [],
        "supervision_observations": actor.monitor.observations,
        "decisions": [record],
    }


def test_candidate_future_costs_and_independent_reconstruction(monkeypatch, tmp_path):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1352.0, case="comparison_update")
    notice = notices(actor.case, actor.s.time_s)
    nominal = candidate_material(
        make_candidate(actor, "nominal"), actor.envelope, notice, actor.scheduled
    )
    later_candidate = make_candidate(actor, "next_orbit")
    later = candidate_material(later_candidate, actor.envelope, notice, actor.scheduled)
    assert later == expected_comparison_material(
        later_candidate, actor.envelope, notice, actor.scheduled
    )
    assert nominal["waiting_at_burn"]["elapsed_wait_s"] == 0
    assert (
        later["waiting_at_burn"]["energy_remaining_kwh"]
        < nominal["waiting_at_burn"]["energy_remaining_kwh"]
    )
    assert (
        later["waiting_at_burn"]["energy_remaining_kwh"]
        > actor.envelope["operations"]["waiting"]["minimum_energy_kwh"]
    )
    assert set(later["area_service_estimates"]) == {"west"}
    assert later["reserve_after_loss_kg"][0] < later["sampled_contact_ranges"]["reserve_kg"][0]


def test_missing_service_evidence_is_indeterminate(monkeypatch, tmp_path):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1352.0, case="comparison_update")
    notice = notices(actor.case, actor.s.time_s)
    notice.pop("recovery_service_forecast")
    materials = {
        name: candidate_material(
            make_candidate(actor, name), actor.envelope, notice, actor.scheduled
        )
        for name in ("nominal", "next_orbit")
    }
    record = {
        "request": {"request_id": "quality", "selected": "nominal"},
        "response": None,
        "accepted_action": "keep_plan",
    }
    row = comparison_quality_record(
        record, materials, actor.envelope["comparison_preparation"]["quality_objectives"]
    )
    assert row["status"] == "indeterminate"


def test_quality_uses_conservative_intervals_and_not_a_dispatch_gate(monkeypatch, tmp_path):
    actor, _ = actor_harness(monkeypatch, tmp_path, now=1352.0, case="comparison_update")
    notice = notices(actor.case, actor.s.time_s)
    material = candidate_material(
        make_candidate(actor, "nominal"), actor.envelope, notice, actor.scheduled
    )
    better, worse = deepcopy(material), deepcopy(material)
    worse["area_service_estimates"]["east"]["estimated_completion_time_s"] = [20000, 24000]
    better["area_service_estimates"]["east"]["estimated_completion_time_s"] = [18000, 21000]
    record = {
        "request": {"request_id": "quality", "selected": "worse"},
        "response": None,
        "accepted_action": "keep_plan",
    }
    objectives = actor.envelope["comparison_preparation"]["quality_objectives"]
    # Overlap forbids claiming completion superiority based on a midpoint.
    row = comparison_quality_record(record, {"better": better, "worse": worse}, objectives)
    assert row["status"] == "not_demonstrably_dominated"
    better["area_service_estimates"]["east"]["estimated_completion_time_s"] = [18000, 19000]
    row = comparison_quality_record(record, {"better": better, "worse": worse}, objectives)
    assert row["status"] == "dominated" and row["dominating_candidate_ids"] == ["better"]
    assert record["accepted_action"] == "keep_plan"
    assert "accepted" not in row and "passed" not in row


def test_preparation_audit_matches_production_record(monkeypatch, tmp_path):
    actor, study = comparison_study(monkeypatch, tmp_path)
    issues, quality = verify_comparison(study, actor.envelope, actor.case)
    assert issues == []
    assert quality["role"] == "posthoc_not_dispatch_or_case_gate"
    assert quality["counts"]["indeterminate"] == 1


def screened_preparation_study(monkeypatch, tmp_path, outcome="completed"):
    """Exercise preparation records with finite tool doubles, no propagation."""
    actor, _ = actor_harness(monkeypatch, tmp_path, now=3000.0, case="comparison_update")
    actor.envelope["comparison_preparation"]["initial_candidates"] = ["next_orbit"]

    def calculate(names, **budget):
        start, center = actor.s.time_s, actor.times["next_orbit"]
        before = actor.forecast_count
        started = outcome != "screen_not_started"
        if started:
            actor.forecast_count += 3
            for _ in range(302 if outcome == "screen_only_failure" else 2):
                actor.step()
        accepted = outcome == "completed"
        actor.return_time_search = {
            "start_time_s": start, "end_time_s": actor.s.time_s, "center_s": center,
            "forecast_count_before": before, "forecast_count_after": actor.forecast_count,
            "requested_names": list(names), "work_started": started,
            "accepted_for_trial": accepted, "cutoff_time_s": start + 200.0,
            "search_cutoff_exceeded": outcome == "screen_only_failure",
            "pending_operation_index": len(actor.pending) if started else None,
        }
        if started:
            actor.pending.append({
                "operation": "return_forecasts", "start_time_s": start,
                "end_time_s": actor.s.time_s, "wall_time_s": actor.s.time_s - start,
                "cutoff_time_s": start + 200.0,
            })
        if accepted:
            actor.times["next_orbit"] = center - 40.0
        if outcome == "screen_only_failure":
            actor.last_calculation_result = {
                "status": "not_started", "reason": "forecast_batch_budget_exceeded",
            }
            return False
        start = actor.s.time_s
        actor.forecast_count += 4 * len(names)
        actor.candidates.update({name: make_candidate(actor, name) for name in names})
        actor.step()
        actor.pending.append({
            "operation": "return_forecasts", "start_time_s": start,
            "end_time_s": actor.s.time_s, "wall_time_s": 1.0,
            "cutoff_time_s": budget["cutoff_time_s"],
        })
        actor.last_calculation_result = {"status": "completed", "reason": "predictions_available"}
        return True

    actor.calculate = calculate
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    return actor, {
        "comparison_preparations": actor.comparison_preparations,
        "opportunities": actor.times, "profile": actor.p, "source_sha256": actor.sources,
        "scheduled_s": actor.scheduled, "forecast_count": actor.forecast_count,
        "pending_operations": actor.pending, "return_time_search": actor.return_time_search,
        "plan_revisions": [], "supervision_observations": actor.monitor.observations,
        "decisions": [],
    }


@pytest.mark.parametrize("outcome,counts,operations", [
    ("completed", 7, 2), ("screen_failure_then_admission", 7, 2),
    ("screen_only_failure", 3, 1), ("screen_not_started", 4, 1),
])
def test_screen_and_admission_have_separate_bounded_operation_accounting(
    monkeypatch, tmp_path, outcome, counts, operations,
):
    actor, study = screened_preparation_study(monkeypatch, tmp_path, outcome)
    prep = study["comparison_preparations"][0]
    assert prep["forecast_count_after"] == counts
    assert prep["pending_operation_index"] == 0
    assert prep["pending_operation_end_index"] == operations
    assert verify_comparison(study, actor.envelope, actor.case)[0] == []
    if outcome == "completed":
        assert study["opportunities"]["next_orbit"] != study["return_time_search"]["center_s"]
    elif outcome == "screen_only_failure":
        assert prep["end_time_s"] > prep["budget"]["cutoff_time_s"]
        assert prep["status"] == "not_started" and not actor.candidates
        assert actor.comparison_context["ready"] is False


@pytest.mark.parametrize("mutation", [
    "missing_end", "gap", "overlap", "wrong_charge", "screen_count", "screen_cutoff", "extra_operation",
])
def test_comparison_search_operation_and_count_tampering_is_rejected(monkeypatch, tmp_path, mutation):
    actor, study = screened_preparation_study(monkeypatch, tmp_path)
    prep = study["comparison_preparations"][0]
    ops = study["pending_operations"]
    if mutation == "missing_end":
        prep["pending_operation_end_index"] = 1
    elif mutation == "gap":
        ops[1]["start_time_s"] += 1.0
    elif mutation == "overlap":
        ops[1]["start_time_s"] -= 1.0
    elif mutation == "wrong_charge":
        prep["forecast_count_after"] -= 3
        study["forecast_count"] = prep["forecast_count_after"]
    elif mutation == "screen_count":
        study["return_time_search"]["forecast_count_before"] += 1
    elif mutation == "screen_cutoff":
        ops[0]["cutoff_time_s"] += 1
    else:
        ops.append(deepcopy(ops[-1]))
        prep["pending_operation_end_index"] += 1
    reasons, _ = verify_comparison(study, actor.envelope, actor.case)
    assert "comparison_calculation_deadline_or_accounting" in reasons


def test_failed_screen_cannot_claim_ready_or_adopt_a_candidate_after_overrun(monkeypatch, tmp_path):
    actor, study = screened_preparation_study(monkeypatch, tmp_path, "screen_only_failure")
    study["comparison_preparations"][0]["status"] = "completed"
    reasons, _ = verify_comparison(study, actor.envelope, actor.case)
    assert "comparison_calculation_deadline_or_accounting" in reasons


def test_forecast_operation_outside_any_preparation_is_not_unaccounted(monkeypatch, tmp_path):
    actor, study = screened_preparation_study(monkeypatch, tmp_path)
    study["pending_operations"].append(deepcopy(study["pending_operations"][-1]))
    reasons, _ = verify_comparison(study, actor.envelope, actor.case)
    assert "comparison_forecast_accounting" in reasons


@pytest.mark.parametrize(
    "mutation,issue",
    [
        ("cost", "comparison_candidate_costs"),
        ("cutoff", "comparison_calculation_budget"),
        ("late", "comparison_calculation_deadline_or_accounting"),
        ("snapshot", "comparison_forecast_snapshot_binding"),
        ("notice", "comparison_request_context"),
    ],
)
def test_audit_rejects_tampered_evidence(monkeypatch, tmp_path, mutation, issue):
    actor, study = comparison_study(monkeypatch, tmp_path)
    row = study["comparison_preparations"][0]
    if mutation == "cost":
        study["decisions"][0]["request"]["candidates"]["nominal"]["comparison_material"][
            "waiting_at_burn"
        ]["energy_remaining_kwh"] += 1
    elif mutation == "cutoff":
        row["budget"]["cutoff_time_s"] += 100
    elif mutation == "late":
        row["end_time_s"] = row["budget"]["cutoff_time_s"] + 1
    elif mutation == "snapshot":
        next(iter(row["candidate_snapshots_after"].values()))["source_sha256"] = {"wrong": "hash"}
    else:
        study["decisions"][0]["request"]["notice"]["text"] = "future information"
    issues, _ = verify_comparison(study, actor.envelope, actor.case)
    assert issue in issues


def test_request_cannot_use_prediction_outside_completed_preparation(monkeypatch, tmp_path):
    actor, study = comparison_study(monkeypatch, tmp_path)
    record = study["decisions"][0]
    request = record["request"]
    candidate = make_candidate(actor, "next_orbit")
    # A globally available snapshot does not establish request-time delivery.
    row = study["comparison_preparations"][0]
    row["candidate_snapshots_after"][digest(candidate)] = candidate
    check = admit(
        candidate, actor.envelope, record["request_observation"], request["notice"], actor.scheduled
    )
    request["candidates"]["next_orbit"] = {
        "candidate_sha256": digest(candidate),
        "return_time_s": candidate["return_time_s"],
        "predicted_contacts": [metrics(trial) for trial in candidate["trials"]],
        "constraint_rejections": check["reasons"],
        "areas": check["areas"],
        "comparison_material": candidate_material(
            candidate, actor.envelope, request["notice"], actor.scheduled
        ),
    }
    issues, _ = verify_comparison(study, actor.envelope, actor.case)
    assert "comparison_request_available_evidence" in issues


@pytest.mark.parametrize(
    "mutation,issue",
    [
        ("initial", "comparison_preparation_context"),
        ("work", "comparison_preparation_context"),
        ("count", "comparison_forecast_accounting"),
        ("ready", "comparison_request_preparation_binding"),
        ("zero_cost", "comparison_candidate_costs"),
    ],
)
def test_boolean_numbers_do_not_pass_comparison_audit(monkeypatch, tmp_path, mutation, issue):
    actor, study = comparison_study(monkeypatch, tmp_path)
    row = study["comparison_preparations"][0]
    if mutation == "initial":
        row["initial"] = 1
    elif mutation == "work":
        row["work_performed"] = 1
    elif mutation == "count":
        row["forecast_count_before"] = False
    elif mutation == "ready":
        study["decisions"][0]["request"]["comparison_context"]["ready"] = 1
    else:
        material = study["decisions"][0]["request"]["candidates"]["nominal"]["comparison_material"]
        assert material["waiting_at_burn"]["elapsed_wait_s"] == 0.0
        material["waiting_at_burn"]["elapsed_wait_s"] = False
    issues, _ = verify_comparison(study, actor.envelope, actor.case)
    assert issue in issues


def add_updated_preparation(actor, study, *, booked):
    from dataclasses import replace

    actor.selected = "nominal" if booked else None
    if booked:
        study["plan_revisions"].append(
            {
                "revision": 1,
                "time_s": actor.s.time_s,
                "candidate_id": "nominal",
                "candidate_sha256": digest(actor.candidates["nominal"]),
            }
        )
    actor.s = replace(actor.s, time_s=1352.0)
    actor.monitor.poll(actor.observation(), notices(actor.case, actor.s.time_s), actor.selected)
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    study["forecast_count"] = actor.forecast_count
    return study["comparison_preparations"][-1]


def test_budget_protects_valid_unbooked_option(monkeypatch, tmp_path):
    actor, study = comparison_study(monkeypatch, tmp_path)
    row = add_updated_preparation(actor, study, booked=False)
    assert row["selected_before"] is None
    assert row["budget"]["protected_burn_s"] is None
    assert row["budget"]["admissible_candidate_ids"] == ["nominal"]
    assert row["budget"]["opportunity_cutoff_time_s"] < actor.times["nominal"]
    assert row["budget"]["cutoff_time_s"] <= row["start_time_s"] + actor.envelope["batch_timeout_s"]
    issues, _ = verify_comparison(study, actor.envelope, actor.case)
    assert issues == []
    row["budget"]["admissible_candidate_ids"] = []
    row["budget"]["opportunity_cutoff_time_s"] = actor.times["next_orbit"]
    issues, _ = verify_comparison(study, actor.envelope, actor.case)
    assert "comparison_calculation_budget" in issues


def test_booking_cannot_be_omitted_or_bound_to_obsolete_revision(monkeypatch, tmp_path):
    actor, study = comparison_study(monkeypatch, tmp_path)
    row = add_updated_preparation(actor, study, booked=True)
    issues, _ = verify_comparison(study, actor.envelope, actor.case)
    assert issues == []
    original = deepcopy(row)
    row["selected_before"] = None
    row["selected_after"] = None
    row["budget"]["protected_burn_s"] = None
    issues, _ = verify_comparison(study, actor.envelope, actor.case)
    assert "comparison_booking_observation_binding" in issues
    row.clear()
    row.update(original)
    study["plan_revisions"].append(
        {
            "revision": 2,
            "time_s": row["start_time_s"] - 1,
            "candidate_id": "next_orbit",
            "candidate_sha256": "newer-approved-choice",
        }
    )
    issues, _ = verify_comparison(study, actor.envelope, actor.case)
    assert "comparison_booking_approval_missing" in issues


@pytest.mark.parametrize("extra", ["forecasts", "preparation"])
def test_normal_case_rejects_unnecessary_comparison_work(extra):
    study = {
        "dispatch": {"candidate_id": "nominal", "revision": 1},
        "plan_revisions": [{"candidate_id": "nominal", "revision": 1}],
        "forecast_count": 4,
        "comparison_preparations": [
            {
                "initial": True,
                "requested_candidate_ids": ["nominal"],
                "forecast_count_before": 0,
                "forecast_count_after": 4,
            }
        ],
    }
    assert case_reasons(study, "comparison_normal", "contact_and_recovery_area_met") == []
    if extra == "forecasts":
        study["forecast_count"] = 8
    else:
        study["comparison_preparations"].append({"initial": False, "work_performed": False})
    assert "normal_optional_computation_not_avoided" in case_reasons(
        study, "comparison_normal", "contact_and_recovery_area_met"
    )

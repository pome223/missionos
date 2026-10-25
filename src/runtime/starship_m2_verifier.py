"""Independent saved-record checks for M2 deployment -> return continuation.

No tracker, producer, controller or propagator is called. Reuse independent
scalar observation checks and the return record verifier, not a plant rerun.
"""

import math

from .starship_deployment_handoff_verifier import (
    STATE,
    _digest,
    _hash,
    _history,
    _number,
    _observed_origin,
    _require,
    _same,
    _sequence_evidence,
    _state,
    _strict,
    _vehicle_inventory,
)
from .starship_replanning_verifier import verify as verify_return, metrics, site_distance
from .starship_orbital_window import reserved_decision_s
from .starship_call_limits import call_count_allowed
from .starship_m2_attempts import authority_call_limits_valid

BINDING_KEYS = (
    "handoff_sha256",
    "authority_sha256",
    "profile_sha256",
    "generation",
    "released_count",
    "retained_count",
)


def _no_model(value):
    if type(value) is dict:
        return all(
            not (key in {"model_inference_invoked", "call_attempted"} and item is True)
            and _no_model(item)
            for key, item in value.items()
        )
    return all(_no_model(item) for item in value) if type(value) is list else True


def _verified_ascent_sequence(study):
    run = study["deployment_run"]
    if not any(e["event"] == "supervised_deployment_withheld" for e in run["events"]):
        return False
    prefix = study.get("concurrent_prefix")
    _require(type(prefix) is dict and _same(prefix.get("prefix_run"), run)
             and _same(prefix.get("trace"), study["trace"])
             and _same(prefix.get("handoff"), study["handoff"]), "ascent_prefix_execution_binding")
    from .starship_m3_prefix_verifier import verify as verify_prefix
    checked = verify_prefix(prefix)
    _require(checked["passed"] is True and checked["handoff_ready"] is True,
             "ascent_prefix_independent_verification")
    return True


def _handoff(study):
    authority, profile, run, handoff = (
        study[key] for key in ("authority", "profile", "deployment_run", "handoff")
    )
    _require(
        set(handoff)
        == {
            "schema",
            "inventory_basis",
            "development_only",
            "dispatch_authorized",
            "authority_sha256",
            "profile_sha256",
            "generation",
            "total_count",
            "released_count",
            "retained_count",
            "execution_state",
            "origin",
            "history",
            "release_observations",
            "handoff_sha256",
        }
        and handoff["schema"] == "missionos.deployment_return_handoff.v1"
        and handoff["inventory_basis"] == "synthetic_confirmed_separation_counter_v1"
        and handoff["development_only"] is True
        and handoff["dispatch_authorized"] is False,
        "handoff_scope",
    )
    _require(
        handoff["handoff_sha256"]
        == _digest({k: v for k, v in handoff.items() if k != "handoff_sha256"})
        and handoff["authority_sha256"] == _digest(authority)
        and handoff["profile_sha256"] == _digest(profile) == authority["profile_sha256"],
        "handoff_identity",
    )
    total = profile["payload"]["count"]
    _require(
        type(total) is int
        and 1 <= total <= 26
        and type(handoff["total_count"]) is int
        and total == handoff["total_count"],
        "total_inventory",
    )
    history, releases, latest, generation = _history(study["trace"], total)
    _sequence_evidence(run, study["trace"], ascent_withhold_verified=_verified_ascent_sequence(study))
    _require(
        _same(handoff["history"], history)
        and _same(handoff["release_observations"], releases)
        and type(handoff["generation"]) is int
        and handoff["generation"] == generation,
        "handoff_history_binding",
    )
    released = latest["released_count"]
    _require(
        type(handoff["released_count"]) is int
        and type(handoff["retained_count"]) is int
        and handoff["released_count"] == released
        and handoff["retained_count"] == total - released
        and type(run["outcome"]["payload_released_count"]) is int
        and run["outcome"]["payload_released_count"] == released,
        "observed_inventory_binding",
    )
    state = handoff["execution_state"]
    _state(state)
    _require(
        _same(state, run["final_state"])
        and run["scenario"] == "launch"
        and run["outcome"]["phase"] == run["samples"][-1]["phase"] == "orbital_coast"
        and run["outcome"]["orbit_gate_reached"] is True
        and run["outcome"]["termination"] == "time_limit"
        and _same(state, {k: run["samples"][-1][k] for k in STATE}),
        "execution_state_or_phase",
    )
    _require(
        0 <= state["time_s"] - latest["time_s"] <= 2.001
        and math.isclose(state["time_s"], authority["checkpoint_time_s"], rel_tol=0.0, abs_tol=1e-8)
        and _same(run["outcome"]["duration_s"], state["time_s"]),
        "handoff_timeframe",
    )
    _vehicle_inventory(profile, run["final_vehicle"], total - released)
    _require(
        _same(handoff["origin"], _observed_origin(state, total - released)),
        "observed_origin_binding",
    )
    events = [e for e in run["events"] if e["event"] == "payload_released"]
    _require(
        len(events) == len(releases) == len(run["satellites"]) == released, "release_event_count"
    )
    _require(
        all(_number(e["time_s"]) for e in events)
        and all(a["time_s"] < b["time_s"] for a, b in zip(events, events[1:])),
        "release_event_chronology",
    )
    for event, observed, satellite in zip(events, releases, run["satellites"]):
        _require(
            observed["previous_observation_time_s"]
            <= event["time_s"]
            <= observed["observed_time_s"]
            and _same(satellite["release_state"]["time_s"], event["time_s"]),
            "release_event_observation",
        )
    commands = [
        {"kind": "command", **{k: e[k] for k in ("action", "time_s")}}
        for e in run["events"]
        if e["event"] == "managed_mission_command" and e["point"].startswith("deployment")
    ]
    _require(
        _same(commands, [r for r in history if r["kind"] == "command"]), "command_trace_binding"
    )
    _require(
        not any(
            e["event"] in {"return_requested", "deorbit_ignition_command"} for e in run["events"]
        ),
        "return_before_handoff",
    )
    director = run["mission_director"]
    _require(
        _same(director["envelope"], authority["deployment_authority"])
        and director["response_fault"] == authority["deployment_response_fault"]
        and run["mission_management_case"] == authority["deployment_case"],
        "deployment_grant_binding",
    )
    returning = study["return_study"]
    _require(
        _same(returning["deployment"], {"authority": authority, "handoff": handoff, "launch": run})
        and _same(returning["launch"], run)
        and _same(returning["execution_origin"], state),
        "same_plant_continuation",
    )
    cutoffs = [e["time_s"] for e in run["events"] if e["event"] == "orbit_cutoff_command"]
    _require(
        len(cutoffs) == 1 and _number(cutoffs[0]) and 0 <= cutoffs[0] < state["time_s"],
        "original_cutoff",
    )
    scheduled = cutoffs[0] + profile["guidance"]["coast_before_return_s"]
    _require(
        state["time_s"] < scheduled and _same(scheduled, returning["scheduled_s"]),
        "original_return_schedule",
    )
    _require(
        not any(
            e["event"] in {"payload_released", "managed_mission_command"}
            for e in returning["execution"]["events"]
        ),
        "inventory_changed_after_handoff",
    )
    return {k: handoff[k] for k in BINDING_KEYS}


def _search_evidence(study, binding):
    """Check bounded search evidence without invoking the search or propagator."""
    returning, authority = study["return_study"], study["authority"]
    origin = study["handoff"]["origin"]["state"]
    radius = math.sqrt(sum(x * x for x in origin["r_eci_m"]))
    energy = sum(x * x for x in origin["v_eci_mps"]) / 2 - 3.986004418e14 / radius
    _require(energy < 0, "search_origin_not_bound")
    a = -3.986004418e14 / (2 * energy)
    center = returning["scheduled_s"] + 2 * math.pi * math.sqrt(a * a * a / 3.986004418e14)
    times = returning["opportunities"]
    _require(
        set(times) == {"nominal", "next_orbit"} and times["nominal"] == returning["scheduled_s"],
        "search_original_schedule_changed",
    )
    row = returning.get("return_time_search")
    if row is None:
        _require(
            math.isclose(times["next_orbit"], center, rel_tol=0.0, abs_tol=1e-7),
            "unrecorded_time_search",
        )
        return
    cfg = authority["return_time_search"]
    _require(
        _same(row["configuration"], cfg)
        and _same(row["deployment_binding"], binding)
        and _same(row["source_sha256"], study["return_source_sha256"])
        and math.isclose(row["center_s"], center, rel_tol=0.0, abs_tol=1e-7)
        and row["return_admission_granted"] is False,
        "search_authority_binding",
    )
    _require(
        row["start_time_s"] == row["origin"]["state"]["time_s"]
        and row["end_time_s"] == row["later_observation"]["state"]["time_s"]
        and row["origin"]["retained_count"] == binding["retained_count"],
        "search_clock_or_inventory",
    )
    elapsed = row["end_time_s"]-row["start_time_s"]
    wall = row["wall_time_s"]
    _require(_number(elapsed) and elapsed >= 0 and _number(wall) and wall >= 0, "search_clock_values")
    exhausted = row.get("clock_budget_exhausted", False)
    _require(type(exhausted) is bool, "search_clock_disposition")
    if "clock_debt_s" in row:
        _require(_number(row["clock_debt_s"]) and math.isclose(row["clock_debt_s"], max(0., wall-elapsed),
                 rel_tol=0., abs_tol=1e-6) and exhausted == (row["clock_debt_s"] > 1e-6), "search_clock_debt_amount")
    if exhausted:
        outcome = returning["execution"].get("outcome", {})
        stops = [e for e in returning["execution"]["events"] if e.get("event") == "m2_return_search_stopped"]
        _require("clock_debt_s" in row and row["clock_debt_s"] > 1e-6
            and row["accepted_for_trial"] is False and returning.get("dispatch") is None
            and returning["execution"]["final_state"]["time_s"] == row["end_time_s"]
            and outcome.get("termination") == "return_search_clock_debt_unresolved"
            and outcome.get("stop_reason") == "return_search_clock_debt" and outcome.get("contact_receipt") is None
            and len(stops) == 1 and stops[0].get("time_s") == row["end_time_s"]
            and stops[0].get("record_sha256") == _digest(row)
            and returning["forecast_count"] == row["forecast_count_after"], "search_clock_debt_must_stop_unresolved")
    else:
        _require(elapsed >= wall-1e-6, "search_clock_or_inventory")
    if "clock_horizon_s" in row:
        _require(
            _number(row["clock_horizon_s"])
            and row["clock_horizon_s"] == returning["scheduled_s"] + authority["return_authority"]["maximum_delay_s"]
            and _number(row["end_time_s"]) and row["end_time_s"] <= row["clock_horizon_s"]
            and type(row.get("search_cutoff_exceeded")) is bool
            and row["search_cutoff_exceeded"] == (row["end_time_s"] >= row["cutoff_time_s"])
            and (not row["search_cutoff_exceeded"] or row["accepted_for_trial"] is False),
            "search_catchup_horizon_or_cutoff",
        )
    count = row["forecast_count_after"] - row["forecast_count_before"]
    _require(
        type(count) is int
        and count in (0, cfg["maximum_screen_forecasts"])
        and row["forecast_count_after"] <= returning["forecast_count"],
        "search_forecast_budget",
    )
    rows = row["trials"]
    _require(len(rows) in (0, 3) and (not rows or count == 3), "search_grid_incomplete")
    for trial, offset in zip(rows, cfg["coarse_offsets_s"]):
        _require(
            _same(trial["origin"], row["origin"])
            and trial["integration_scale"] == 1.0
            and _number(trial["return_time_s"])
            and math.isclose(trial["return_time_s"], center + offset, rel_tol=0.0, abs_tol=1e-7),
            "search_trial_binding",
        )
    proposal = row["suggestion"]
    if "requested_names" in row:
        requested = row["requested_names"]
        _require(type(requested) is list and requested and len(set(requested)) == len(requested)
                 and set(requested) <= {"nominal", "next_orbit"}
                 and row["reserved_trial_count"] == 4*len(requested), "search_trial_reservation")
        if count:
            _require(row["forecast_count_after"]+row["reserved_trial_count"]
                     <= authority["return_authority"]["maximum_forecasts"], "search_consumed_trial_budget")
    preparations = returning.get("comparison_preparations", [])
    associated = [p for p in preparations if p["start_time_s"] == row["start_time_s"]
                  and p["forecast_count_before"] == row["forecast_count_before"]]
    if associated and count:
        _require("budget" in row, "search_preparation_budget_missing")
    if "budget" in row:
        # Independently recompute the shared deadline allocation. The screen
        # cannot extend the preparation or spend its admission reservation.
        e, budget = authority["return_authority"], row["budget"]
        _require(type(budget) is dict and set(budget) == {
            "total_budget_s", "preparation_cutoff_time_s", "minimum_admission_budget_s",
            "reserved_admission_budget_s", "screen_budget_s",
        } and all(_number(v) for v in budget.values()), "search_preparation_budget_values")
        total = budget["total_budget_s"]
        deadline = budget["preparation_cutoff_time_s"]
        dt = returning["profile"]["integration"]["coast_dt_s"]
        minimum = e["comparison_preparation"].get("minimum_start_budget_s", dt)
        trials = cfg["admission_trials"]*len(row["requested_names"])
        reserve = min(total, max(minimum, total*trials/(cfg["maximum_screen_forecasts"]+trials)))
        screen_limit = total-reserve
        lead = e["batch_timeout_s"]+reserved_decision_s(e)+e["event_supervision"]["deadline_margin_s"]
        cutoff = min(center-cfg["half_width_s"]-lead,
                     returning["scheduled_s"]+e["maximum_delay_s"], deadline,
                     row["start_time_s"]+screen_limit)
        if "nominal" in row["requested_names"]:
            cutoff = min(cutoff, returning["scheduled_s"]-lead)
        _require(0 <= total <= e["batch_timeout_s"]
                 and math.isclose(total, max(0., deadline-row["start_time_s"]), rel_tol=0., abs_tol=1e-7)
                 and _same(budget["minimum_admission_budget_s"], minimum)
                 and math.isclose(budget["reserved_admission_budget_s"], reserve, rel_tol=0., abs_tol=1e-7)
                 and math.isclose(budget["screen_budget_s"], screen_limit, rel_tol=0., abs_tol=1e-7)
                 and math.isclose(row["cutoff_time_s"], cutoff, rel_tol=0., abs_tol=1e-7)
                 and row["decision_lead_s"] == lead,
                 "search_preparation_budget_binding")
        _require(type(row["work_started"]) is bool and row["work_started"] == (count > 0)
                 and (not count or row["start_time_s"]+dt < cutoff)
                 and (not row["accepted_for_trial"] or wall <= screen_limit),
                 "search_preparation_budget_execution")
        if preparations:
            _require(len(associated) == 1, "search_preparation_owner")
            preparation = associated[0]
            _require(preparation["missing_candidate_ids"] == row["requested_names"]
                     and math.isclose(preparation["budget"]["available_sim_s"], total,
                                      rel_tol=0., abs_tol=1e-7)
                     and preparation["budget"]["cutoff_time_s"] == deadline
                     and (not count or preparation["selected_before"] is None),
                     "search_preparation_owner_binding")
    if "pending_operation_index" in row:
        index = row["pending_operation_index"]
        operations = returning.get("pending_operations", [])
        _require(type(index) is int and 0 <= index < len(operations), "search_pending_operation_index")
        operation = operations[index]
        _require(operation["operation"] == "return_forecasts"
                 and operation["start_time_s"] == row["start_time_s"]
                 and operation["end_time_s"] == row["end_time_s"]
                 and operation["wall_time_s"] == wall
                 and operation["cutoff_time_s"] == row["cutoff_time_s"]
                 and (row["accepted_for_trial"] or operation["expired"] is True),
                 "search_pending_operation_binding")
    if proposal is not None:
        # Recompute scalar selection from persisted trials; never call the producer.
        _require(len(rows) == 3 and all(metrics(t) is not None for t in rows), "search_contact_required")
        h = cfg["half_width_s"]
        possible = []
        for name, area in sorted(authority["return_authority"]["operations"]["areas"].items()):
            availability = row["notice"]["areas"].get(name)
            if "requested_names" in row and (availability is None or not all(
                    availability[0] <= metrics(t)["time_s"] <= min(availability[1], row["notice"]["expires_at_s"])
                    for t in rows)):
                continue
            y = [site_distance(metrics(t), area)**2 for t in rows]
            curve = (y[2]+y[0]-2*y[1])/(2*h*h)
            slope = (y[2]-y[0])/(2*h)
            offset = max(-h, min(h, -slope/(2*curve))) if curve > 0 else cfg["coarse_offsets_s"][y.index(min(y))]
            possible.append(((y[1]+slope*offset+curve*offset*offset)/area["radius_m"]**2, name, center+offset))
        _require(bool(possible), "search_available_area_required")
        _, expected_area, expected_when = min(possible)
        _require(proposal["screen_target_area"] == expected_area
                 and math.isclose(proposal["return_time_s"], expected_when, rel_tol=0., abs_tol=1e-7),
                 "search_suggestion_recalculation")
    if row["accepted_for_trial"]:
        _require(
            len(rows) == 3
            and type(proposal) is dict
            and proposal["method"] == "quadratic_distance_screen_not_admission"
            and proposal["screen_target_area"]
            in authority["return_authority"]["operations"]["areas"]
            and center - cfg["half_width_s"]
            <= proposal["return_time_s"]
            <= center + cfg["half_width_s"]
            and times["next_orbit"] == proposal["return_time_s"]
            and "shutdown_error_type" not in row,
            "search_suggestion_outside_scope",
        )
        e = authority["return_authority"]
        lead = (
            e["batch_timeout_s"]
            + reserved_decision_s(e)
            + e["event_supervision"]["deadline_margin_s"]
        )
        _require(
            row["end_time_s"] < min(center, proposal["return_time_s"]) - lead,
            "search_changed_expiring_opportunity",
        )
    else:
        _require(
            math.isclose(times["next_orbit"], center, rel_tol=0.0, abs_tol=1e-7),
            "failed_search_changed_time",
        )
    events = [e for e in returning["execution"]["events"] if e["event"] == "m2_return_time_screen"]
    _require(
        len(events) == 1 and events[0]["record_sha256"] == _digest(row), "search_execution_receipt"
    )


def _completion(study, return_verdict):
    from .starship_operational_trials import is_trial
    case, handoff = study["authority"]["case"], study["handoff"]
    commands = [r["action"] for r in handoff["history"] if r["kind"] == "command"]
    issues = []
    if return_verdict["outcome"] != "contact_and_recovery_area_met":
        issues.append("return_unresolved_or_contact_area_failed")
    if (case in {"normal", "hold_resume", "orbital_update"} or is_trial(case)) and handoff["released_count"] != handoff["total_count"]:
        issues.append("complete_deployment_not_observed")
    if (case == "orbital_update" or is_trial(case)) and not return_verdict.get("case_accepted", False):
        issues.append("orbital_comparison_case_incomplete")
    if case == "normal":
        dispatch = study["return_study"]["dispatch"]
        if (
            dispatch is None
            or dispatch["candidate_id"] != "nominal"
            or any(action != "continue" for action in commands)
        ):
            issues.append("normal_plan_unnecessarily_changed")
    if case in {"operations_notice", "release_fault"} and "stop_deployment" not in commands:
        issues.append("deployment_stop_not_observed")
    if study["authority"]["mode"] == "fixture" and case == "operations_notice" and handoff["released_count"] != 1:
        issues.append("notice_deployment_inventory")
    if study["authority"]["mode"] == "fixture" and case == "release_fault" and (
        handoff["released_count"] != 0 or "collect_status" not in commands
    ):
        issues.append("release_fault_confirmation_missing")
    if case == "hold_resume" and not any(
        a == "hold" and "continue" in commands[i + 1 :] for i, a in enumerate(commands)
    ):
        issues.append("hold_and_resume_not_observed")
    return issues


def verify(
    study, *, expected_authority, expected_sources, expected_return_sources, expected_run_id
):
    """Record integrity and M2 outcome are separate; unresolved is not success."""
    result = {
        "schema": "missionos.starship_m2_verification.v1",
        "passed": False,
        "issues": [],
        "case_accepted": False,
        "case_issues": [],
        "outcome": "unresolved",
        "mission_completed": False,
        "physical_execution": False,
        "independent_physics_validation": False,
    }
    try:
        _require(
            _strict(study) and study["schema"] == "missionos.starship_m2_study.v1", "study_schema"
        )
        _require(
            _same(study["authority"], expected_authority)
            and study["run_id"] == expected_run_id
            and _same(study["source_sha256"], expected_sources)
            and _same(study["return_source_sha256"], expected_return_sources),
            "approved_input_binding",
        )
        _require(
            bool(expected_sources)
            and all(
                not name.startswith(("/", "../")) and _hash(value)
                for name, value in expected_sources.items()
            )
            and bool(expected_return_sources)
            and all(expected_sources.get(k) == v for k, v in expected_return_sources.items()),
            "source_manifest_binding",
        )
        authority = expected_authority
        _require(
            authority["schema"] == "missionos.starship_m2_authority.v1"
            and authority["mode"] in ("fixture", "live")
            and authority_call_limits_valid(authority)
            and authority["physical_execution_authorized"] is False
            and authority["ship_return_dispatch_authorized"] is True,
            "m2_authority_scope",
        )
        expected_budget = {
            "checkpoint_time_s": authority["checkpoint_time_s"],
            "maximum_model_calls": authority["maximum_model_calls"],
            "maximum_attempts": 1,
            **{
                k: authority["return_authority"][k]
                for k in (
                    "maximum_forecasts",
                    "maximum_workers",
                    "forecast_timeout_s",
                    "batch_timeout_s",
                    "maximum_delay_s",
                )
            },
        }
        _require(_same(study["budget"], expected_budget), "execution_budget_binding")
        runtime = study["runtime"]
        _require(
            runtime["schema"] == "missionos.starship_m2_runtime.v1"
            and runtime["mode"] == authority["mode"]
            and runtime["boundary"]
            == (
                "local_cli_fixture_continuation"
                if authority["mode"] == "fixture"
                else "host_broker_mailbox_continuation"
            )
            and runtime["deployment_started"]
            is runtime["continuation_started"]
            is runtime["sources_unchanged"]
            is True
            and runtime["retry_performed"] is runtime["physical_execution"] is False
            and type(runtime["model_calls"]) is type(runtime["human_inflight_commands"]) is int
            and call_count_allowed(runtime["model_calls"], authority["maximum_model_calls"])
            and runtime["human_inflight_commands"] == 0
            and _number(runtime["wall_time_s"])
            and runtime["wall_time_s"] >= 0,
            "runtime_scope",
        )
        _require(
            "runtime_failure" not in study and "runtime_failure" not in study["return_study"],
            "runtime_failure",
        )
        _require(
            authority["mode"] == "live"
            or (
                _no_model(study["deployment_run"]["mission_director"]["records"])
                and _no_model(study["return_study"]["decisions"])
            ),
            "unexpected_model_invocation",
        )
        from .starship_m2_decision_verifier import verify_decisions

        result["provider_attempts"] = verify_decisions(study)
        binding = _handoff(study)
        _search_evidence(study, binding)
        returning = verify_return(
            study["return_study"],
            expected_envelope=authority["return_authority"],
            expected_case=authority["return_case"],
            expected_sources=expected_return_sources,
            expected_run_id=expected_run_id + "-return",
            expected_deployment_binding=binding,
        )
        result.update(
            {
                "deployment_binding": binding,
                "return_verification": returning,
                "outcome": returning["outcome"],
                "issues": returning["issues"],
                "passed": returning["passed"],
            }
        )
        # event_normal chooses notice semantics, not the M2 outcome requirement.
        result["case_issues"] = _completion(study, returning)
        result["case_accepted"] = result["passed"] and not result["case_issues"]
    except (
        KeyError,
        TypeError,
        ValueError,
        IndexError,
        OverflowError,
        ZeroDivisionError,
        RecursionError,
    ) as error:
        result["passed"] = result["case_accepted"] = False
        result["issues"].append(
            str(error) if type(error) is ValueError else f"malformed_record:{type(error).__name__}"
        )
    return result
